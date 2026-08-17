"""cerase-office-converter MCP — the properties that must not regress.

The weight is on the `{path}` handle. A converted document goes back into the
calling agent's workspace through the write broker and the tool answers with a
handle; the bytes never travel in the MCP result. The federation truncates an
inlined base64 payload at 1 MB, and a truncated .docx is a corrupt .docx that
still reads as a successful conversion — so the large-artifact case is asserted
here byte for byte, not by size alone.

The branch between a handle and inline base64 is the BROKER CONFIGURATION
(agent_id + control-plane URL + internal secret), never the artifact's size.
Both sizes are covered below in both configurations so that a future size
threshold cannot be introduced unnoticed.

Nothing here runs LibreOffice, pandoc or the network: `subprocess.run` is
replaced by a fake that writes the artifact the real binary would have produced
and records the argv, and `urlopen` by a fake broker that records the request.

    python -m pytest tests/ -q
"""
from __future__ import annotations

import base64
import os
import subprocess

import pytest

import server


# A byte pattern rather than random data, so an assertion failure shows where a
# payload was cut instead of an unreadable diff.
def _payload(size: int) -> bytes:
    return (b"CERASE-OFFICE-ARTIFACT-" * (size // 23 + 1))[:size]


LARGE = _payload(1_500_000)  # over the 1 MB federation truncation point
SMALL = _payload(512)

AGENT = "agent-7"
BINDING = "binding-token"


# ─── Fakes: the converter binaries and the control-plane broker ──────────

class FakeBinaries:
    """Stands in for soffice/pandoc: records each argv and writes the file the
    real binary would have produced, so the server's own routing, output-path
    derivation and post-conversion handling all still run."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.payload = SMALL
        self.produce = True
        self.returncode = 0

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if self.produce:
            if argv[0] == server.PANDOC:
                out = argv[argv.index("-o") + 1]
            else:
                outdir = argv[argv.index("--outdir") + 1]
                target = argv[argv.index("--convert-to") + 1].split(":")[0]
                stem = os.path.splitext(os.path.basename(argv[-1]))[0]
                out = os.path.join(outdir, f"{stem}.{target}")
            with open(out, "wb") as f:
                f.write(self.payload)
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"stub stderr")

    def argv_for(self, binary: str) -> list[str]:
        for argv in self.calls:
            if argv[0] == binary:
                return argv
        raise AssertionError(f"{binary} was never invoked; calls={self.calls}")


class FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


class FakeBroker:
    """Records every control-plane call so a test can assert what crossed the
    wire — the point of the write broker is that the bytes go THERE."""

    def __init__(self, body: bytes = b"", status: int = 200):
        self.requests = []
        self.body = body
        self.status = status

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return FakeResponse(self.status, self.body)

    @property
    def last(self):
        assert self.requests, "the broker was never called"
        return self.requests[-1]


def headers_of(req) -> dict:
    # urllib capitalises header names on the way in.
    return {k.lower(): v for k, v in req.headers.items()}


@pytest.fixture
def binaries(monkeypatch):
    fake = FakeBinaries()
    monkeypatch.setattr(server.subprocess, "run", fake)
    return fake


@pytest.fixture
def broker(monkeypatch):
    fake = FakeBroker()
    monkeypatch.setattr(server.urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def broker_configured(monkeypatch):
    monkeypatch.setenv("CERASE_CONTROL_PLANE_URL", "http://cerase-control-plane")
    monkeypatch.setenv("CERASE_INTERNAL_SECRET", "internal-secret")


@pytest.fixture
def broker_unconfigured(monkeypatch):
    monkeypatch.delenv("CERASE_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("CERASE_INTERNAL_SECRET", raising=False)


# ─── The {path} handle — the guarantee with no coverage anywhere else ────

def test_large_artifact_returns_a_path_handle_and_never_inlines_it(
    binaries, broker, broker_configured
):
    binaries.payload = LARGE
    result = server.convert_docx_to_pdf(
        input_b64=base64.b64encode(b"docx bytes").decode(),
        agent_id=AGENT,
        output_filename="report.pdf",
        agent_binding=BINDING,
    )
    assert result == {
        "path": "outputs/report.pdf",
        "filename": "report.pdf",
        "size_bytes": len(LARGE),
    }
    # An inlined payload is the defect: the federation would truncate it at 1 MB
    # and hand the model a corrupt document that reports success.
    assert "contents_base64" not in result


def test_broker_receives_every_byte_of_a_large_artifact(
    binaries, broker, broker_configured
):
    binaries.payload = LARGE
    server.convert_docx_to_pdf(
        input_b64=base64.b64encode(b"docx bytes").decode(),
        agent_id=AGENT,
        output_filename="report.pdf",
    )
    req = broker.last
    assert req.get_method() == "PUT"
    assert req.full_url.endswith("/api/internal/workspace-file/agent-7?path=outputs%2Freport.pdf")
    assert len(req.data) > 1_000_000
    assert req.data == LARGE


def test_broker_request_carries_the_internal_secret_and_the_agent_binding(
    binaries, broker, broker_configured
):
    server.convert_docx_to_pdf(
        input_b64=base64.b64encode(b"docx bytes").decode(),
        agent_id=AGENT,
        output_filename="report.pdf",
        agent_binding=BINDING,
    )
    h = headers_of(broker.last)
    assert h["authorization"] == "Bearer internal-secret"
    assert h["x-cerase-agent-binding"] == BINDING
    assert h["content-type"] == "application/octet-stream"


def test_a_small_artifact_takes_the_same_handle_path(
    binaries, broker, broker_configured
):
    # Size does not select the branch — the broker's availability does. Without
    # this, a size threshold could be introduced and only the large case would
    # notice.
    binaries.payload = SMALL
    result = server.convert_docx_to_pdf(
        input_b64=base64.b64encode(b"docx bytes").decode(),
        agent_id=AGENT,
        output_filename="note.pdf",
    )
    assert result["path"] == "outputs/note.pdf"
    assert "contents_base64" not in result


def test_without_a_broker_the_bytes_come_back_inline(
    binaries, broker, broker_unconfigured
):
    binaries.payload = SMALL
    result = server.convert_docx_to_pdf(
        input_b64=base64.b64encode(b"docx bytes").decode(),
        agent_id=AGENT,
        output_filename="note.pdf",
    )
    assert "path" not in result
    assert base64.b64decode(result["contents_base64"]) == SMALL
    assert result["size_bytes"] == len(SMALL)
    assert broker.requests == []


def test_a_call_with_no_agent_id_falls_back_inline(
    binaries, broker, broker_configured
):
    # A non-agent caller (dev, a direct MCP client) has no workspace to write to.
    binaries.payload = SMALL
    result = server.convert_docx_to_pdf(
        input_b64=base64.b64encode(b"docx bytes").decode(),
        output_filename="note.pdf",
    )
    assert "path" not in result
    assert base64.b64decode(result["contents_base64"]) == SMALL


# ─── Routing: which binary handles which source ──────────────────────────

def test_binary_sources_route_through_libreoffice(binaries, broker, broker_configured):
    server.convert_xlsx_to_ods(
        input_b64=base64.b64encode(b"xlsx bytes").decode(), agent_id=AGENT
    )
    argv = binaries.argv_for(server.SOFFICE)
    assert "--headless" in argv
    assert argv[argv.index("--convert-to") + 1] == "ods"


def test_markdown_to_pdf_asks_pandoc_for_xelatex(binaries, broker, broker_configured):
    server.convert_md_to_pdf(
        input_b64=base64.b64encode(b"# title").decode(), agent_id=AGENT
    )
    assert "--pdf-engine=xelatex" in binaries.argv_for(server.PANDOC)


def test_a_reference_doc_reaches_pandoc_as_a_file(binaries, broker, broker_configured):
    server.convert_md_to_docx(
        input_b64=base64.b64encode(b"# title").decode(),
        reference_doc_b64=base64.b64encode(b"template docx").decode(),
        agent_id=AGENT,
    )
    argv = binaries.argv_for(server.PANDOC)
    ref = [a for a in argv if a.startswith("--reference-doc=")]
    assert len(ref) == 1
    # pandoc reads it off disk, so the template must have been materialised.
    written = ref[0].split("=", 1)[1]
    assert os.path.basename(written) == "reference.docx"


# ─── The guards the server states explicitly ─────────────────────────────

def test_a_reference_doc_is_refused_for_a_libreoffice_source(binaries, broker):
    # Only the generic tool can be asked this — the typed LibreOffice pairs take
    # no reference doc at all, which is the guard one level up.
    with pytest.raises(ValueError, match="only to markdown"):
        server.convert(
            "docx", "odt",
            input_b64=base64.b64encode(b"docx bytes").decode(),
            agent_id=AGENT,
            reference_doc_b64=base64.b64encode(b"template").decode(),
        )


def test_a_reference_doc_is_refused_for_pdf_output(binaries, broker):
    with pytest.raises(ValueError, match="docx/odt/pptx"):
        server.convert(
            "md", "pdf",
            input_b64=base64.b64encode(b"# title").decode(),
            agent_id=AGENT,
            reference_doc_b64=base64.b64encode(b"template").decode(),
        )


def test_input_must_be_given_exactly_once(binaries, broker):
    with pytest.raises(ValueError, match="exactly one"):
        server.convert_docx_to_pdf(agent_id=AGENT)
    with pytest.raises(ValueError, match="exactly one"):
        server.convert_docx_to_pdf(
            input_b64=base64.b64encode(b"x").decode(),
            path="report.docx",
            agent_id=AGENT,
        )


def test_the_two_reference_doc_forms_are_mutually_exclusive():
    with pytest.raises(ValueError, match="at most one"):
        server._resolve_reference_doc_bytes("YQ==", "brand.docx", AGENT)


def test_libreoffice_exiting_zero_without_output_is_an_error(
    binaries, broker, broker_configured
):
    # soffice reports success on a failed conversion, so an empty output dir is
    # the only evidence there is.
    binaries.produce = False
    with pytest.raises(RuntimeError, match="produced no output"):
        server.convert_docx_to_pdf(
            input_b64=base64.b64encode(b"docx bytes").decode(), agent_id=AGENT
        )


# ─── Workspace reads ─────────────────────────────────────────────────────

def test_a_path_escaping_the_workspace_root_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("CERASE_TOOL_WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="escapes the workspace root"):
        server._safe_local_path(str(tmp_path / ".." / "etc" / "passwd"))


def test_a_workspace_path_reads_the_local_mount_first(
    monkeypatch, tmp_path, binaries, broker, broker_unconfigured
):
    monkeypatch.setenv("CERASE_TOOL_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "in.docx").write_bytes(b"local docx bytes")
    assert server._load_workspace_bytes(AGENT, str(tmp_path / "in.docx")) == b"local docx bytes"
    assert broker.requests == []


def test_a_workspace_path_with_no_local_file_and_no_broker_is_an_error(
    monkeypatch, tmp_path, broker_unconfigured
):
    monkeypatch.setenv("CERASE_TOOL_WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="no control-plane"):
        server._load_workspace_bytes(AGENT, str(tmp_path / "absent.docx"))
