#!/usr/bin/env python3
"""Cerase Office Converter — MCP server.

Exposes cross-format document conversion tools backed by:
  - LibreOffice headless (`soffice --headless --convert-to`) for binary
    Office formats (.docx/.xlsx/.pptx) ↔ ODF (.odt/.ods/.odp) ↔ PDF.
  - pandoc for markup ↔ Office (.md ↔ .docx/.odt + html/latex/...).
  - xelatex (TeX Live) as the PDF engine for pandoc markdown → PDF.

Input + output both flow through the control-plane file-broker (this is a
SHARED runner that mounts no agent volume), mirroring cerase-deck-renderer:
  - a tool takes EITHER `input_b64` (inline) OR `path` (a workspace file the
    broker reads scoped to the agent);
  - the produced file is written back into the agent's workspace via the broker
    and returned as a `{path}` handle (no 1 MB-truncated base64 over the
    federation). When no agent / broker is configured it falls back to base64.

LibreOffice startup is slow (~3s cold boot) because of Java + templates init.
To avoid this per call, we run LibreOffice headless and keep a long-running
profile dir per container (spawned once at first conversion, reused after).
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import urllib.request
from urllib.parse import urlencode

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cerase-office-converter")

SOFFICE = shutil.which("soffice") or "/usr/bin/soffice"
PANDOC = shutil.which("pandoc") or "/usr/bin/pandoc"
XELATEX = shutil.which("xelatex") or "/usr/bin/xelatex"

# Long-running profile so soffice doesn't re-init each call.
PROFILE_DIR = "/tmp/cerase-office-profile"
os.makedirs(PROFILE_DIR, exist_ok=True)


# ─── File-broker plumbing (mirror docreader read + deck-renderer write) ──

def _safe_local_path(path: str) -> str:
    """Resolve a workspace path, refusing anything that escapes the shared
    workspace root (path-traversal guard — the agent supplies `path`, so a
    crafted `../../etc/passwd` must not read host files).
    """
    root = os.path.realpath(os.environ.get("CERASE_TOOL_WORKSPACE_ROOT", "/workspace"))
    resolved = os.path.realpath(path)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError("path escapes the workspace root")
    return resolved


def _load_workspace_bytes(agent_id: str | None, path: str) -> bytes:
    """Read a workspace file's CONTENT. Try a local mount first (dev/test where
    CERASE_TOOL_WORKSPACE_ROOT IS the agent's workspace), then fall back to the
    control-plane internal API (it owns workspace access via docker exec) scoped
    to (agent_id, path).
    """
    try:
        local = _safe_local_path(path)
        if os.path.isfile(local):
            with open(local, "rb") as f:
                return f.read()
    except ValueError:
        pass  # not a safe local path → let the control-plane re-guard + serve

    cp = os.environ.get("CERASE_CONTROL_PLANE_URL", "").rstrip("/")
    secret = os.environ.get("CERASE_INTERNAL_SECRET", "")
    if not agent_id or not cp or not secret:
        raise ValueError(
            "workspace `path` given but no local file and no control-plane "
            "configured (agent_id / CERASE_CONTROL_PLANE_URL / CERASE_INTERNAL_SECRET)"
        )
    qs = urlencode({"path": path})
    req = urllib.request.Request(
        f"{cp}/api/internal/workspace-file/{agent_id}?{qs}",
        headers={"Authorization": f"Bearer {secret}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — internal API
        return r.read()


def _write_workspace_file(agent_id: str | None, path: str, data: bytes) -> bool:
    """Write a produced artifact back into the calling agent's workspace via the
    control-plane broker (this runner mounts no agent volume). The control-plane
    owns workspace access (docker exec), scopes the write to (agent_id, path),
    and caps it. Returns True on success, False when not configured — the caller
    then falls back to base64 (dev / a non-agent call).
    """
    cp = os.environ.get("CERASE_CONTROL_PLANE_URL", "").rstrip("/")
    secret = os.environ.get("CERASE_INTERNAL_SECRET", "")
    if not agent_id or not cp or not secret:
        return False
    qs = urlencode({"path": path})
    req = urllib.request.Request(
        f"{cp}/api/internal/workspace-file/{agent_id}?{qs}",
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/octet-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — internal API
        return 200 <= r.status < 300


def _resolve_input_bytes(input_b64: str | None, path: str | None, agent_id: str | None) -> bytes:
    """Exactly one of `input_b64` (inline) / `path` (a workspace file)."""
    if bool(input_b64) == bool(path):
        raise ValueError("provide exactly one of `input_b64` or `path`")
    if path:
        return _load_workspace_bytes(agent_id, path)
    return base64.b64decode(input_b64 or "")


# ─── Conversion engine ───────────────────────────────────────────

def _soffice_convert(input_path: str, target_format: str, outdir: str) -> str:
    """Run LibreOffice headless conversion. Returns path to the produced file."""
    subprocess.run(
        [
            SOFFICE,
            "--headless",
            f"-env:UserInstallation=file://{PROFILE_DIR}",
            "--convert-to", target_format,
            "--outdir", outdir,
            input_path,
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    # soffice picks the basename + target ext.
    base = os.path.splitext(os.path.basename(input_path))[0]
    # `--convert-to pdf` produces base.pdf; `--convert-to odt` produces base.odt.
    ext = target_format.split(":")[0]  # `pdf:writer_pdf_Export` → `pdf`
    produced = os.path.join(outdir, f"{base}.{ext}")
    if not os.path.exists(produced):
        # LibreOffice sometimes uses different ext (e.g. ods vs xlsx).
        for fn in os.listdir(outdir):
            if fn.startswith(base):
                produced = os.path.join(outdir, fn)
                break
    return produced


def _pandoc_convert(input_path: str, target_format: str, outdir: str) -> str:
    """Run pandoc conversion (md → docx/odt). Returns produced file path."""
    base = os.path.splitext(os.path.basename(input_path))[0]
    produced = os.path.join(outdir, f"{base}.{target_format}")
    cmd = [PANDOC, input_path, "-o", produced]
    # PDF via xelatex (better unicode support than pdflatex).
    if target_format == "pdf":
        cmd.extend(["--pdf-engine=xelatex"])
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return produced


def _do_conversion(
    source_ext: str,
    target_format: str,
    *,
    input_b64: str | None = None,
    path: str | None = None,
    agent_id: str | None = None,
    output_filename: str | None = None,
) -> dict:
    work = tempfile.mkdtemp(prefix="cerase-office-", dir="/tmp")
    try:
        in_path = os.path.join(work, f"input.{source_ext}")
        with open(in_path, "wb") as f:
            f.write(_resolve_input_bytes(input_b64, path, agent_id))
        # Pandoc handles markdown-input cases; everything else routes
        # through LibreOffice for higher fidelity binary↔ODF.
        if source_ext in ("md", "markdown"):
            produced = _pandoc_convert(in_path, target_format, work)
        else:
            produced = _soffice_convert(in_path, target_format, work)
        with open(produced, "rb") as f:
            out_bytes = f.read()
        filename = output_filename or os.path.basename(produced)
        # Mirror deck-renderer: write back via the broker, return a {path}
        # handle; fall back to base64 for a dev / non-agent caller.
        rel = f"outputs/{filename}"
        if _write_workspace_file(agent_id, rel, out_bytes):
            return {"path": rel, "filename": filename, "size_bytes": len(out_bytes)}
        return {
            "filename": filename,
            "size_bytes": len(out_bytes),
            "contents_base64": base64.b64encode(out_bytes).decode("ascii"),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


# Every tool takes the same broker-aware inputs: EITHER `input_b64` (inline)
# OR `path` (a workspace file); `agent_id` lets the runner reach the broker;
# `output_filename` names the artifact written back into the workspace.
def _convert_tool(source_ext, target_format, input_b64, path, agent_id, output_filename):
    return _do_conversion(
        source_ext, target_format,
        input_b64=input_b64, path=path, agent_id=agent_id, output_filename=output_filename,
    )


# ─── Specific tool pairs ─────────────────────────────────────────

@mcp.tool()
def convert_docx_to_pdf(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None) -> dict:
    """Convert a Word .docx → PDF. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("docx", "pdf", input_b64, path, agent_id, output_filename)


@mcp.tool()
def convert_docx_to_odt(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None) -> dict:
    """Convert a Word .docx → LibreOffice .odt. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("docx", "odt", input_b64, path, agent_id, output_filename)


@mcp.tool()
def convert_odt_to_docx(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None) -> dict:
    """Convert a LibreOffice .odt → Word .docx. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("odt", "docx", input_b64, path, agent_id, output_filename)


@mcp.tool()
def convert_pptx_to_pdf(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None) -> dict:
    """Convert a PowerPoint .pptx → PDF. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("pptx", "pdf", input_b64, path, agent_id, output_filename)


@mcp.tool()
def convert_pptx_to_odp(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None) -> dict:
    """Convert a PowerPoint .pptx → LibreOffice .odp. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("pptx", "odp", input_b64, path, agent_id, output_filename)


@mcp.tool()
def convert_odp_to_pptx(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None) -> dict:
    """Convert a LibreOffice .odp → PowerPoint .pptx. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("odp", "pptx", input_b64, path, agent_id, output_filename)


@mcp.tool()
def convert_xlsx_to_pdf(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None) -> dict:
    """Convert an Excel .xlsx → PDF. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("xlsx", "pdf", input_b64, path, agent_id, output_filename)


@mcp.tool()
def convert_xlsx_to_ods(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None) -> dict:
    """Convert an Excel .xlsx → LibreOffice .ods. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("xlsx", "ods", input_b64, path, agent_id, output_filename)


@mcp.tool()
def convert_ods_to_xlsx(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None) -> dict:
    """Convert a LibreOffice .ods → Excel .xlsx. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("ods", "xlsx", input_b64, path, agent_id, output_filename)


@mcp.tool()
def convert_md_to_pdf(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None) -> dict:
    """Convert plain markdown → PDF via pandoc + xelatex. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("md", "pdf", input_b64, path, agent_id, output_filename)


@mcp.tool()
def convert_md_to_docx(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None) -> dict:
    """Convert plain markdown → Word .docx via pandoc. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("md", "docx", input_b64, path, agent_id, output_filename)


# ─── Catch-all generic converter ─────────────────────────────────

@mcp.tool()
def convert(
    source_format: str,
    target_format: str,
    input_b64: str | None = None,
    path: str | None = None,
    agent_id: str | None = None,
    output_filename: str | None = None,
) -> dict:
    """Generic conversion catch-all. Use the typed `convert_*_to_*` tools when
    the pair is known; fall back here for less common ones (e.g. rtf → odt,
    html → docx). Provide `input_b64` OR a workspace `path`.

    Supported via LibreOffice: docx/odt/rtf/html/txt/xlsx/ods/csv/pptx/odp.
    Supported via pandoc: md/markdown sources (target = docx/odt/pdf/html).
    """
    return _convert_tool(source_format, target_format, input_b64, path, agent_id, output_filename)


if __name__ == "__main__":
    mcp.run()
