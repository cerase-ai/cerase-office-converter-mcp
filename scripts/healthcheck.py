#!/usr/bin/env python3
"""Container liveness for the universal runner, over the protocol it bridges.

The probe this replaced connected to port 3000. That proves the bridge is
listening and nothing more, and the bridge outlives the thing it bridges: kill
the stdio child of a running runner and the listener stays open, so the socket
connect keeps passing and `docker ps` keeps printing healthy for a container
with no MCP server in it. `initialize` keeps passing too -- the bridge replays
the capabilities it recorded during its own startup handshake, without asking
the child anything.

The first request that has to reach the child is `tools/list`, so that is what
this asks for. It is also the request the gateway makes when it federates a
connector, which makes a green here mean the connector is usable rather than
merely present. One connector cannot be probed more deeply than this: the tools
belong to whatever package the operator installed, and calling one of them
would be running a stranger's side effects every thirty seconds.

It also reads the container's pid budget. These runners execute code we did not
write under a deliberately low ceiling, and a package that leaks a thread per
call spends that ceiling silently until some unrelated allocation is refused --
which is how the first one was found, as a numeric library that could no longer
start its worker pool. Reporting above a fraction of the ceiling turns that into
a health signal while the runner still answers.

Exit 0 healthy, exit 1 unhealthy with the reason on stdout, which is where
docker keeps the last output of a healthcheck.

Every knob is an env var so the check can be driven against a stub bridge and a
fake cgroup directory: a probe that cannot be made to fail is not a probe.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

ENDPOINT = os.environ.get("CERASE_HEALTHCHECK_URL", "http://127.0.0.1:3000/mcp")
BUDGET_SECONDS = float(os.environ.get("CERASE_HEALTHCHECK_BUDGET", "8"))
CGROUP_ROOT = os.environ.get("CERASE_HEALTHCHECK_CGROUP", "/sys/fs/cgroup")
PID_CEILING_FRACTION = float(os.environ.get("CERASE_HEALTHCHECK_PID_FRACTION", "0.75"))

_deadline = time.monotonic() + BUDGET_SECONDS


class Unhealthy(Exception):
    """The runner is not serving. Carries the line the operator will read."""


def _remaining() -> float:
    """Per-request timeout, so the whole probe stays inside the timeout docker
    gives it however many requests the handshake takes."""
    return max(0.5, _deadline - time.monotonic())


def _post(body: dict, session: str | None = None) -> tuple[dict, str | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        response = urllib.request.urlopen(request, timeout=_remaining())
        raw = response.read().decode("utf-8", "replace")
        returned_session = response.headers.get("Mcp-Session-Id")
    except urllib.error.HTTPError as exc:
        raise Unhealthy(f"{body.get('method')} answered HTTP {exc.code}") from exc
    except Exception as exc:
        raise Unhealthy(f"{body.get('method')} did not answer: {exc!r}") from exc
    return _decode(raw, body.get("method")), returned_session


def _decode(raw: str, method: str | None) -> dict:
    """Both framings the bridge may answer with: a bare JSON object, or the
    event-stream one where the payload rides on a data line. Detected by the
    presence of a data line anywhere, because the event line comes first."""
    text = raw.strip()
    frames = [
        line[len("data:") :].strip()
        for line in text.splitlines()
        if line.startswith("data:") and line[len("data:") :].strip()
    ]
    if frames:
        text = frames[-1]
    elif "\ndata:" in text or text.startswith("event:"):
        raise Unhealthy(f"{method} answered an event stream with no payload")
    if not text:
        return {}
    try:
        decoded = json.loads(text)
    except ValueError as exc:
        raise Unhealthy(f"{method} answered something that is not JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise Unhealthy(f"{method} answered {type(decoded).__name__}, not an object")
    return decoded


def _list_the_tools() -> int:
    """Full client sequence, because a partial one is what passes while broken:
    initialize, the initialized notification the protocol requires before any
    request, then the listing. Returns how many tools the connector exposes."""
    handshake, session = _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "cerase-healthcheck", "version": "1"},
            },
        }
    )
    if "error" in handshake:
        raise Unhealthy(f"initialize returned an error: {handshake['error']}")
    if not session:
        raise Unhealthy("initialize returned no session id")

    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)

    answer, _ = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    if "error" in answer:
        raise Unhealthy(f"tools/list returned an error: {answer['error']}")
    result = answer.get("result")
    if not isinstance(result, dict):
        raise Unhealthy("tools/list returned no result object")
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise Unhealthy("tools/list returned no tool list")
    return len(tools)


def _pid_budget() -> str:
    """The container's pid usage against the ceiling it was given, as a line to
    print. Raises when the usage is above the fraction that leaves an ordinary
    allocation room to succeed."""
    for current_path, max_path in (
        (f"{CGROUP_ROOT}/pids.current", f"{CGROUP_ROOT}/pids.max"),
        (f"{CGROUP_ROOT}/pids/pids.current", f"{CGROUP_ROOT}/pids/pids.max"),
    ):
        try:
            with open(current_path, encoding="utf-8") as handle:
                current = int(handle.read().strip())
            with open(max_path, encoding="utf-8") as handle:
                ceiling_raw = handle.read().strip()
        except (OSError, ValueError):
            continue
        if ceiling_raw == "max":
            return f"pids {current} of no ceiling"
        try:
            ceiling = int(ceiling_raw)
        except ValueError:
            continue
        if ceiling > 0 and current > ceiling * PID_CEILING_FRACTION:
            raise Unhealthy(
                f"pids {current} of {ceiling} is above "
                f"{int(PID_CEILING_FRACTION * 100)}% of the ceiling -- something "
                "in this container is not releasing threads"
            )
        return f"pids {current} of {ceiling}"
    return "pids unreadable"


def main() -> int:
    try:
        count = _list_the_tools()
        budget = _pid_budget()
    except Unhealthy as exc:
        print(f"unhealthy: {exc}")
        return 1
    print(f"healthy: {count} tools listed, {budget}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
