#!/usr/bin/env python3
"""The gate in front of a connector runner's MCP port.

Every connector we did not write runs in a container of its own on one shared
network, `cerase_runners`, whose only other member is the gateway. A network
says who may talk and never in which direction, so without this any runner
could open another's `:3000/mcp` and call its tools with that runner's
credential -- a stranger's package driving somebody else's Gmail.

So the bridge (mcp-proxy) listens on 127.0.0.1:3001 and this gate owns :3000.
A connection is spliced through only when its first request carries
`X-Cerase-Runner-Token` equal to `CERASE_RUNNER_TOKEN`; anything else gets a 401
and is closed. The control-plane hands each runner its own token when it starts
the container, an HMAC of the container's name, and the gateway derives the
same value from the runner's URL. A runner therefore holds the token that opens
itself and cannot compute the one that opens its neighbour.

The check is per connection, on the first request's head: after that the bytes
are copied both ways untouched, which keeps the event streams and long tool
calls exactly as mcp-proxy serves them. A connection belongs to whoever opened
it, and only a holder of the token gets one.

The gate also serves the files a connector reads or writes on its own disk.
A connector we did not write attaches a file by reading a local path, and saves
a download by writing one, and no assistant's workspace is on this disk. So the
gateway puts a workspace file here before such a call and takes back what the
connector wrote after it, through requests under `/cerase/files/`, which the
gate answers itself and never passes to the bridge. Each call has a folder of
its own under `CERASE_RUNNER_FILES_DIR`:

    POST   /cerase/files/<folder>/         make the folder, answer its path
    PUT    /cerase/files/<folder>/<name>   write the body there, answer its path
    GET    /cerase/files/<folder>/         list its regular files and their sizes
    GET    /cerase/files/<folder>/<name>   read one back
    DELETE /cerase/files/<folder>/         remove the folder and what is in it

The same token opens them, one request per connection. A folder name is 8 to
64 letters, digits, dashes or underscores; a file name is one path segment that
does not start with a dot. A file larger than `CERASE_RUNNER_FILES_MAX_BYTES`
is refused by its declared length before its body is read. A symbolic link in
a folder is neither listed nor served, so a connector cannot hand the gateway a
file from elsewhere on this disk. A folder left behind by a call that never
removed it is deleted an hour after it was last written.

Three properties the callers rely on:

- the gate starts listening only once the bridge accepts connections, because
  the control-plane reads "the port answers" as "the connector is up";
- an empty token refuses everything, so a container started without one is
  unusable and says so rather than open;
- the token is removed from the environment the bridge and its stdio child
  inherit, so the connector's own code never sees it.

Usage:
    runner-gate.py -- <command> [args...]   run the bridge behind the gate;
                                             exits with the bridge's status
    runner-gate.py --probe                   exit 0 when the gate is up and
                                             refuses a request with no token

Standard library only, because it runs in images whose Python differs. Every
port is an environment variable so a test can run it against a stub.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import shutil
import signal
import socket
import stat
import sys
import time
import uuid
from urllib.parse import unquote_to_bytes

LISTEN_HOST = os.environ.get("CERASE_RUNNER_GATE_HOST", "0.0.0.0")  # noqa: S104 - it is the listener
LISTEN_PORT = int(os.environ.get("CERASE_RUNNER_GATE_PORT", "3000"))
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("CERASE_RUNNER_GATE_UPSTREAM_PORT", "3001"))
TOKEN_ENV = "CERASE_RUNNER_TOKEN"
HEADER = b"x-cerase-runner-token"

HEAD_LIMIT = 64 * 1024
HEAD_TIMEOUT = 15.0
CHUNK = 64 * 1024

FILES_PREFIX = "/cerase/files/"
FILES_DIR = os.environ.get("CERASE_RUNNER_FILES_DIR", "/tmp/cerase-files")  # noqa: S108 - the runner's own disk
# The platform's ceiling for one file, which every layer a file crosses is
# built for: 64 MB.
FILES_MAX_BYTES = int(os.environ.get("CERASE_RUNNER_FILES_MAX_BYTES", str(64 * 1024 * 1024)))
FILES_MAX_AGE_S = 3600
# Long enough for a file at the ceiling on a slow link inside the box.
FILES_BODY_TIMEOUT = 180.0
FOLDER_NAME = re.compile(r"[A-Za-z0-9_-]{8,64}")


def _answer(status: bytes, text: bytes) -> bytes:
    return (
        b"HTTP/1.1 " + status + b"\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Length: " + str(len(text)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + text
    )


REFUSED = _answer(b"401 Unauthorized", b"this runner answers the gateway only\n")
TOO_LARGE = _answer(b"431 Request Header Fields Too Large", b"request head too large\n")
UNAVAILABLE = _answer(b"503 Service Unavailable", b"the connector is not answering\n")


def authorised(head: bytes, token: bytes) -> bool:
    """Whether the request head carries this runner's token.

    The first header of that name decides; the comparison is constant-time.
    """
    if not token:
        return False
    for line in head.split(b"\r\n")[1:]:
        name, sep, value = line.partition(b":")
        if sep and name.strip().lower() == HEADER:
            return hmac.compare_digest(value.strip(), token)
    return False


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        # Pass the end on, so the other side finishes its answer and closes.
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, token: bytes) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
    except asyncio.LimitOverrunError:
        writer.write(TOO_LARGE)
        await _close(writer)
        return
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
        await _close(writer)
        return

    if not authorised(head, token):
        try:
            writer.write(REFUSED)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        await _close(writer)
        return

    method, target = _request_line(head)
    if target.startswith(FILES_PREFIX):
        try:
            await _files(method, target[len(FILES_PREFIX):], head, reader, writer)
        except (ConnectionError, OSError):
            pass
        await _close(writer)
        return

    try:
        up_reader, up_writer = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
    except OSError:
        writer.write(UNAVAILABLE)
        await _close(writer)
        return

    up_writer.write(head)
    await asyncio.gather(_copy(reader, up_writer), _copy(up_reader, writer), return_exceptions=True)
    await _close(up_writer)
    await _close(writer)


def _request_line(head: bytes) -> tuple[str, str]:
    """The method and the path of the request, without its query."""
    first = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = first.split(" ")
    if len(parts) < 2:
        return "", ""
    return parts[0].upper(), parts[1].split("?", 1)[0]


def _header(head: bytes, name: bytes) -> str | None:
    for line in head.split(b"\r\n")[1:]:
        key, sep, value = line.partition(b":")
        if sep and key.strip().lower() == name:
            return value.strip().decode("latin-1")
    return None


def _json(status: bytes, body: object) -> bytes:
    data = json.dumps(body, ensure_ascii=False).encode()
    return (
        b"HTTP/1.1 " + status + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(data)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + data
    )


def _no(status: bytes, message: str) -> bytes:
    return _json(status, {"message": message})


def _file_name(raw: str) -> str | None:
    """One path segment, decoded, or None when it could name anything else."""
    try:
        name = unquote_to_bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not name or len(name.encode()) > 255 or name.startswith("."):
        return None
    if any(c in name for c in ("/", "\\", "\x00")):
        return None
    return name


def _sweep(root: str) -> None:
    """Remove the folders no call has written for an hour."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    cutoff = time.time() - FILES_MAX_AGE_S
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            pass


def _folder(root: str, name: str, *, make: bool) -> str | None:
    """The call's folder, made 0700 when asked; None when it is not a real one."""
    path = os.path.join(root, name)
    if make:
        os.makedirs(root, mode=0o700, exist_ok=True)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    return path if stat.S_ISDIR(info.st_mode) else None


def _listing(folder: str) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for entry in sorted(os.scandir(folder), key=lambda e: e.name):
        if entry.name.startswith("."):
            continue
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            files.append({"name": entry.name, "size_bytes": info.st_size})
    return files


async def _send_file(path: str, writer: asyncio.StreamWriter) -> bool:
    """Stream a regular file that is not a link; False when there is none."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return False
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Length: " + str(info.st_size).encode() + b"\r\n"
            b"Connection: close\r\n\r\n"
        )
        while chunk := handle.read(CHUNK):
            writer.write(chunk)
            await writer.drain()
    return True


async def _receive_file(
    folder: str, name: str, length: int, reader: asyncio.StreamReader
) -> str | None:
    """Write exactly `length` bytes of the body to `name`; None when it ended early."""
    final = os.path.join(folder, name)
    partial = os.path.join(folder, f".part-{uuid.uuid4().hex}")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            left = length
            while left > 0:
                chunk = await asyncio.wait_for(reader.read(min(CHUNK, left)), FILES_BODY_TIMEOUT)
                if not chunk:
                    raise ConnectionError("the body ended early")
                handle.write(chunk)
                left -= len(chunk)
        os.replace(partial, final)
    except (ConnectionError, OSError, asyncio.TimeoutError):
        try:
            os.unlink(partial)
        except OSError:
            pass
        return None
    return final


async def _files(
    method: str,
    rest: str,
    head: bytes,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Answer one request of the file exchange (see the module's docstring)."""
    root = FILES_DIR
    _sweep(root)
    folder_name, slash, raw_name = rest.partition("/")
    if not FOLDER_NAME.fullmatch(folder_name) or not slash:
        writer.write(_no(b"400 Bad Request", "a folder is 8 to 64 letters, digits, dashes or underscores"))
        return
    name = None
    if raw_name:
        name = _file_name(raw_name)
        if name is None:
            writer.write(_no(b"400 Bad Request", "a file name is one path segment that does not start with a dot"))
            return

    if method == "POST" and name is None:
        folder = _folder(root, folder_name, make=True)
        writer.write(_json(b"201 Created", {"path": folder}))
        return
    if method == "DELETE" and name is None:
        path = os.path.join(root, folder_name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
        return
    if method == "GET" and name is None:
        folder = _folder(root, folder_name, make=False)
        if folder is None:
            writer.write(_no(b"404 Not Found", "no such folder"))
            return
        writer.write(_json(b"200 OK", {"path": folder, "files": _listing(folder)}))
        return
    if method == "GET" and name is not None:
        folder = _folder(root, folder_name, make=False)
        if folder is None or not await _send_file(os.path.join(folder, name), writer):
            writer.write(_no(b"404 Not Found", "no such file"))
        return
    if method == "PUT" and name is not None:
        declared = _header(head, b"content-length")
        if declared is None or not declared.isdigit():
            writer.write(_no(b"411 Length Required", "a file is sent with its Content-Length"))
            return
        length = int(declared)
        if length > FILES_MAX_BYTES:
            writer.write(_no(
                b"413 Content Too Large",
                f"the file is {length} bytes and a runner takes files up to {FILES_MAX_BYTES} bytes",
            ))
            return
        folder = _folder(root, folder_name, make=True)
        if folder is None:
            writer.write(_no(b"400 Bad Request", "the folder is not a folder"))
            return
        written = await _receive_file(folder, name, length, reader)
        if written is None:
            writer.write(_no(b"400 Bad Request", "the file did not arrive whole"))
            return
        writer.write(_json(b"201 Created", {"path": written, "size_bytes": length}))
        return
    writer.write(_no(b"405 Method Not Allowed", "the file exchange takes POST, PUT, GET and DELETE"))


def _upstream_accepts() -> bool:
    try:
        with socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=1):
            return True
    except OSError:
        return False


async def _serve(command: list[str]) -> int:
    token = os.environ.get(TOKEN_ENV, "").strip().encode()
    if not token:
        print(f"[runner-gate] {TOKEN_ENV} is empty: every request will be refused", file=sys.stderr)
    env = {k: v for k, v in os.environ.items() if k != TOKEN_ENV}

    child = await asyncio.create_subprocess_exec(*command, env=env)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: child.returncode is None and child.send_signal(s))

    exited = asyncio.ensure_future(child.wait())

    # Listen only once the bridge does: until then the port stays closed, as
    # it did when the bridge owned it.
    while not exited.done() and not await asyncio.to_thread(_upstream_accepts):
        await asyncio.wait({exited}, timeout=0.25)
    if exited.done():
        return _status(exited.result())

    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, token), LISTEN_HOST, LISTEN_PORT, limit=HEAD_LIMIT
    )
    print(f"[runner-gate] :{LISTEN_PORT} open, bridge on {UPSTREAM_HOST}:{UPSTREAM_PORT}", file=sys.stderr)
    code = await exited
    server.close()
    return _status(code)


def _status(code: int) -> int:
    return 128 - code if code < 0 else code


def probe() -> int:
    """0 when the gate answers a request without the token with its 401."""
    try:
        with socket.create_connection(("127.0.0.1", LISTEN_PORT), timeout=3) as s:
            s.sendall(b"GET /mcp HTTP/1.0\r\nHost: probe\r\n\r\n")
            answer = s.recv(12)
    except OSError as exc:
        print(f"the gate is not listening: {exc}")
        return 1
    if not answer.startswith(b"HTTP/1.1 401"):
        print(f"the gate did not refuse a request without the token: {answer!r}")
        return 1
    return 0


def main(argv: list[str]) -> int:
    if argv[1:] == ["--probe"]:
        return probe()
    if len(argv) > 2 and argv[1] == "--":
        return asyncio.run(_serve(argv[2:]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
