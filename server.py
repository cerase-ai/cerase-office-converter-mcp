#!/usr/bin/env python3
"""Cerase Office Converter — MCP server.

Exposes cross-format document conversion tools backed by:
  - LibreOffice headless (`soffice --headless --convert-to`) for binary
    Office formats (.docx/.xlsx/.pptx) ↔ ODF (.odt/.ods/.odp) ↔ PDF.
  - pandoc for markup ↔ Office (.md ↔ .docx/.odt + html/latex/...).
  - xelatex (TeX Live) as the PDF engine for pandoc markdown → PDF.
  - headless Chromium for HTML → PDF. LibreOffice reads HTML too, and drops CSS
    grid, flexbox and background colours, so a designed page came out as a
    column of unstyled boxes.
  - pandoc + Chromium for a business document (`render_document`): Markdown to
    an HTML page with the stylesheet in `document/`, printed by the same
    Chromium call. A quote made with `convert_md_to_pdf` read like a LaTeX paper.
  - openpyxl for a workbook built from rows (`create_xlsx`). The assistant's own
    container runs no Python, so a workbook with formulas, a frozen header and
    number formats is made here rather than there.

Input + output both flow through the control-plane file-broker (this is a
SHARED runner that mounts no agent volume), mirroring cerase-deck-renderer:
  - a tool takes EITHER `input_b64` (inline) OR `path` (a workspace file the
    broker reads scoped to the agent);
  - the produced file is written back into the agent's workspace via the broker
    and returned as a `{path}` handle (no 1 MB-truncated base64 over the
    federation). When no agent / broker is configured it falls back to base64.

Custom templates (M-DECK-CUSTOM-TEMPLATE-1 follow-on): the markdown→Office
conversions (pandoc) accept a reference doc to style the output, mirroring how
the deck renderer takes a brand override — pandoc's `--reference-doc=<file>`.
It is supplied EITHER by value (`reference_doc_b64`, inline base64) OR by
reference (`reference_doc_path`, a workspace file the read broker resolves —
for templates too big to inline, e.g. embedded fonts/branding).

LibreOffice startup is slow (~3s cold boot) because of Java + templates init.
To avoid this per call, we run LibreOffice headless and keep a long-running
profile dir per container (spawned once at first conversion, reused after).
"""
from __future__ import annotations

import base64
import datetime
import io
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from typing import Literal
from urllib.parse import urlencode

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

mcp = FastMCP("cerase-office-converter")

SOFFICE = shutil.which("soffice") or "/usr/bin/soffice"
PANDOC = shutil.which("pandoc") or "/usr/bin/pandoc"
XELATEX = shutil.which("xelatex") or "/usr/bin/xelatex"
CHROMIUM = (
    shutil.which("chromium")
    or shutil.which("chromium-browser")
    or "/usr/bin/chromium"
)

# render_document's pandoc template, filter and stylesheet.
DOCUMENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "document")

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


def _load_workspace_bytes(agent_id: str | None, path: str, binding: str = "") -> bytes:
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
    # M-SEC-TOKEN-BINDING-1: the broker requires the calling agent's binding
    # (gateway-injected tool arg) besides the shared bearer.
    headers = {"Authorization": f"Bearer {secret}"}
    if binding:
        headers["X-Cerase-Agent-Binding"] = binding
    req = urllib.request.Request(
        f"{cp}/api/internal/workspace-file/{agent_id}?{qs}",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — internal API
        return r.read()


def _write_workspace_file(
    agent_id: str | None, path: str, data: bytes, binding: str = ""
) -> bool:
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
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/octet-stream",
    }
    if binding:
        headers["X-Cerase-Agent-Binding"] = binding
    req = urllib.request.Request(
        f"{cp}/api/internal/workspace-file/{agent_id}?{qs}",
        data=data,
        method="PUT",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — internal API
        return 200 <= r.status < 300


def _resolve_input_bytes(
    input_b64: str | None, path: str | None, agent_id: str | None, binding: str = ""
) -> bytes:
    """Exactly one of `input_b64` (inline) / `path` (a workspace file)."""
    if bool(input_b64) == bool(path):
        raise ValueError("provide exactly one of `input_b64` or `path`")
    if path:
        return _load_workspace_bytes(agent_id, path, binding)
    return base64.b64decode(input_b64 or "")


def _resolve_reference_doc_bytes(
    reference_doc_b64: str | None,
    reference_doc_path: str | None,
    agent_id: str | None,
    binding: str = "",
) -> bytes | None:
    """Optional pandoc reference doc (a template document to style the output).
    Returns None when neither is given; otherwise AT MOST one of
    `reference_doc_b64` (inline base64) / `reference_doc_path` (a workspace file
    resolved through the read broker — for templates too big to inline, e.g.
    embedded fonts/branding)."""
    if reference_doc_b64 and reference_doc_path:
        raise ValueError(
            "provide at most one of `reference_doc_b64` or `reference_doc_path`"
        )
    if reference_doc_path:
        return _load_workspace_bytes(agent_id, reference_doc_path, binding)
    if reference_doc_b64:
        return base64.b64decode(reference_doc_b64)
    return None


# ─── Conversion engine ───────────────────────────────────────────

def _soffice_convert(input_path: str, target_format: str, outdir: str) -> str:
    """Run LibreOffice headless conversion. Returns path to the produced file.

    soffice exits 0 even when a conversion FAILS (e.g. unsupported filter,
    corrupt input, profile-lock contention), so success cannot be inferred
    from the exit status. Convert into a dedicated output dir — separate
    from the dir holding the input — and require a real artifact to appear
    there: anything found is by construction soffice's product, never the
    input file echoed back. Fail loud otherwise.
    """
    soffice_out = os.path.join(outdir, "soffice-out")
    os.makedirs(soffice_out, exist_ok=True)
    proc = subprocess.run(
        [
            SOFFICE,
            "--headless",
            f"-env:UserInstallation=file://{PROFILE_DIR}",
            "--convert-to", target_format,
            "--outdir", soffice_out,
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
    produced = os.path.join(soffice_out, f"{base}.{ext}")
    if os.path.isfile(produced):
        return produced
    # LibreOffice sometimes uses a different ext (e.g. ods vs xlsx) — any
    # file in the dedicated output dir is a genuine soffice artifact.
    for fn in sorted(os.listdir(soffice_out)):
        candidate = os.path.join(soffice_out, fn)
        if os.path.isfile(candidate):
            return candidate
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    raise RuntimeError(
        f"LibreOffice produced no output converting {os.path.basename(input_path)!r} "
        f"to {target_format!r} (soffice exited 0 but wrote nothing)"
        + (f": {stderr[-500:]}" if stderr else "")
    )


def _pandoc_convert(
    input_path: str,
    target_format: str,
    outdir: str,
    reference_doc: str | None = None,
) -> str:
    """Run pandoc conversion (md → docx/odt). Returns produced file path."""
    base = os.path.splitext(os.path.basename(input_path))[0]
    produced = os.path.join(outdir, f"{base}.{target_format}")
    cmd = [PANDOC, input_path, "-o", produced]
    # PDF via xelatex (better unicode support than pdflatex).
    if target_format == "pdf":
        cmd.extend(["--pdf-engine=xelatex"])
    elif reference_doc:
        # `--reference-doc` styles docx/odt/pptx output from a template document
        # (M-DECK-CUSTOM-TEMPLATE-1 follow-on). pandoc keys it off the OUTPUT
        # format, so the reference doc must be the same family as target_format.
        cmd.append(f"--reference-doc={reference_doc}")
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not os.path.isfile(produced):
        # pandoc's own words are the diagnosis (a missing LaTeX package, a
        # character the font lacks); a bare exit status is not.
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"pandoc could not convert to {target_format} (exit {proc.returncode})"
            + (f": {stderr[-500:]}" if stderr else "")
        )
    return produced


def _do_conversion(
    source_ext: str,
    target_format: str,
    *,
    input_b64: str | None = None,
    path: str | None = None,
    agent_id: str | None = None,
    output_filename: str | None = None,
    reference_doc_b64: str | None = None,
    reference_doc_path: str | None = None,
    agent_binding: str = "",
) -> dict:
    work = tempfile.mkdtemp(prefix="cerase-office-", dir="/tmp")
    try:
        in_path = os.path.join(work, f"input.{source_ext}")
        with open(in_path, "wb") as f:
            f.write(_resolve_input_bytes(input_b64, path, agent_id, agent_binding))
        ref_bytes = _resolve_reference_doc_bytes(
            reference_doc_b64, reference_doc_path, agent_id, agent_binding
        )
        # Pandoc handles markdown-input cases; everything else routes
        # through LibreOffice for higher fidelity binary↔ODF.
        if source_ext in ("md", "markdown"):
            reference_doc: str | None = None
            if ref_bytes is not None:
                if target_format not in ("docx", "odt", "pptx"):
                    raise ValueError(
                        "a reference doc only styles docx/odt/pptx output, "
                        f"not {target_format!r}"
                    )
                reference_doc = os.path.join(work, f"reference.{target_format}")
                with open(reference_doc, "wb") as f:
                    f.write(ref_bytes)
            produced = _pandoc_convert(
                in_path, target_format, work, reference_doc=reference_doc
            )
        else:
            if ref_bytes is not None:
                raise ValueError(
                    "a reference doc applies only to markdown (pandoc) sources; "
                    "LibreOffice conversions don't take one"
                )
            produced = _soffice_convert(in_path, target_format, work)
        with open(produced, "rb") as f:
            out_bytes = f.read()
        filename = output_filename or os.path.basename(produced)
        return _deliver(agent_id, filename, out_bytes, agent_binding)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _deliver(agent_id: str | None, filename: str, out_bytes: bytes, agent_binding: str = "") -> dict:
    """Write a produced file into the caller's workspace and answer with its
    `{path}` handle; without an agent or a broker, answer with the bytes inline
    (dev, or a caller with no workspace). Mirrors the deck renderer."""
    rel = f"outputs/{filename}"
    if _write_workspace_file(agent_id, rel, out_bytes, agent_binding):
        return {"path": rel, "filename": filename, "size_bytes": len(out_bytes)}
    return {
        "filename": filename,
        "size_bytes": len(out_bytes),
        "contents_base64": base64.b64encode(out_bytes).decode("ascii"),
    }


# Every tool takes the same broker-aware inputs: EITHER `input_b64` (inline)
# OR `path` (a workspace file); `agent_id` lets the runner reach the broker;
# `output_filename` names the artifact written back into the workspace. The
# markdown→Office tools also accept an optional pandoc reference doc (a template
# document) by value (`reference_doc_b64`) or by reference (`reference_doc_path`).
def _convert_tool(
    source_ext, target_format, input_b64, path, agent_id, output_filename,
    reference_doc_b64=None, reference_doc_path=None, agent_binding="",
):
    return _do_conversion(
        source_ext, target_format,
        input_b64=input_b64, path=path, agent_id=agent_id, output_filename=output_filename,
        reference_doc_b64=reference_doc_b64, reference_doc_path=reference_doc_path,
        agent_binding=agent_binding,
    )


# ─── Specific tool pairs ─────────────────────────────────────────

@mcp.tool()
def convert_docx_to_pdf(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, agent_binding: str = "") -> dict:
    """Convert a Word .docx → PDF. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("docx", "pdf", input_b64, path, agent_id, output_filename, agent_binding=agent_binding)


@mcp.tool()
def convert_docx_to_odt(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, agent_binding: str = "") -> dict:
    """Convert a Word .docx → LibreOffice .odt. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("docx", "odt", input_b64, path, agent_id, output_filename, agent_binding=agent_binding)


@mcp.tool()
def convert_odt_to_docx(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, agent_binding: str = "") -> dict:
    """Convert a LibreOffice .odt → Word .docx. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("odt", "docx", input_b64, path, agent_id, output_filename, agent_binding=agent_binding)


@mcp.tool()
def convert_pptx_to_pdf(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, agent_binding: str = "") -> dict:
    """Convert a PowerPoint .pptx → PDF. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("pptx", "pdf", input_b64, path, agent_id, output_filename, agent_binding=agent_binding)


@mcp.tool()
def convert_pptx_to_odp(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, agent_binding: str = "") -> dict:
    """Convert a PowerPoint .pptx → LibreOffice .odp. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("pptx", "odp", input_b64, path, agent_id, output_filename, agent_binding=agent_binding)


@mcp.tool()
def convert_odp_to_pptx(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, agent_binding: str = "") -> dict:
    """Convert a LibreOffice .odp → PowerPoint .pptx. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("odp", "pptx", input_b64, path, agent_id, output_filename, agent_binding=agent_binding)


@mcp.tool()
def convert_xlsx_to_pdf(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, agent_binding: str = "") -> dict:
    """Convert an Excel .xlsx → PDF. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("xlsx", "pdf", input_b64, path, agent_id, output_filename, agent_binding=agent_binding)


@mcp.tool()
def convert_xlsx_to_ods(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, agent_binding: str = "") -> dict:
    """Convert an Excel .xlsx → LibreOffice .ods. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("xlsx", "ods", input_b64, path, agent_id, output_filename, agent_binding=agent_binding)


@mcp.tool()
def convert_ods_to_xlsx(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, agent_binding: str = "") -> dict:
    """Convert a LibreOffice .ods → Excel .xlsx. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("ods", "xlsx", input_b64, path, agent_id, output_filename, agent_binding=agent_binding)


@mcp.tool()
def convert_md_to_pdf(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, agent_binding: str = "") -> dict:
    """Convert plain markdown → PDF via pandoc + xelatex. Provide `input_b64` OR a workspace `path`."""
    return _convert_tool("md", "pdf", input_b64, path, agent_id, output_filename, agent_binding=agent_binding)


@mcp.tool()
def convert_md_to_docx(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, reference_doc_b64: str | None = None, reference_doc_path: str | None = None, agent_binding: str = "") -> dict:
    """Convert plain markdown → Word .docx via pandoc. Provide `input_b64` OR a workspace `path`.

    Optionally style the .docx from a template document (pandoc --reference-doc): pass it by value as `reference_doc_b64` (inline base64 of a .docx) OR by reference as `reference_doc_path` (a workspace file — use this for templates too big to inline, e.g. embedded fonts/branding)."""
    return _convert_tool("md", "docx", input_b64, path, agent_id, output_filename, reference_doc_b64=reference_doc_b64, reference_doc_path=reference_doc_path, agent_binding=agent_binding)


@mcp.tool()
def convert_md_to_pptx(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, reference_doc_b64: str | None = None, reference_doc_path: str | None = None, agent_binding: str = "") -> dict:
    """Convert markdown → PowerPoint .pptx via pandoc: editable slides. Provide `input_b64` OR a workspace `path`.

    Slides follow pandoc's rules: a YAML block with `title` (and optionally `subtitle`, `author`, `date`) makes the title slide; each `## heading` starts a slide; a line holding only `---` also starts one; `- ` bullets, tables and images become slide content; a `::: notes` block becomes the slide's speaker notes.

    Optionally style the slides from a template .pptx (pandoc --reference-doc): pass it by value as `reference_doc_b64` OR by reference as `reference_doc_path` (a workspace file)."""
    return _convert_tool("md", "pptx", input_b64, path, agent_id, output_filename, reference_doc_b64=reference_doc_b64, reference_doc_path=reference_doc_path, agent_binding=agent_binding)


# ─── HTML → PDF through Chromium ─────────────────────────────────

_PAPERS = {"a4": "A4", "a3": "A3", "a5": "A5", "letter": "letter", "legal": "legal"}
_ORIENTATIONS = ("portrait", "landscape")


def _page_css(paper: str, orientation: str) -> str:
    """The default page box, and backgrounds printed as they are on screen.

    It goes FIRST in the document, so a page that declares its own `@page`
    keeps it: a later rule of the same weight wins."""
    return (
        "<style>"
        f"@page {{ size: {paper} {orientation}; margin: 0; }} "
        "html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }"
        "</style>"
    )


def _with_page_css(html: str, css: str) -> str:
    match = re.search(r"<head[^>]*>", html, flags=re.IGNORECASE)
    if match:
        return html[: match.end()] + css + html[match.end():]
    match = re.search(r"<html[^>]*>", html, flags=re.IGNORECASE)
    if match:
        return html[: match.end()] + "<head>" + css + "</head>" + html[match.end():]
    return css + html


# What a page given to `convert_html_to_pdf` may load. Chromium opens it as
# `file://`, and a page opened that way may frame, embed or link any other file
# of this container: an `<iframe>` naming one printed it. `render_document`'s
# template refuses that with the same policy; here the page is not ours, so its
# scripts keep running and its images, fonts and stylesheets still load from
# https or `data:` as the tool's description says, and nothing else does.
_PAGE_POLICY = (
    "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' https:; "
    "style-src 'unsafe-inline' https:; font-src https: data:; img-src https: data:; "
    "base-uri 'none'; form-action 'none'"
)

_DOCTYPE = re.compile(r"\A(?:\ufeff)?\s*<!doctype[^>]*>", flags=re.IGNORECASE)


def _with_page_policy(html: str) -> str:
    """The page with the policy as its first element, before any markup it holds.

    Right after the doctype, where the parser opens the head it implies and
    reads the policy before the page's first element: a policy in the page's own
    `<head>` would arrive after a frame written ahead of it. The doctype stays
    first, so the page keeps the rendering mode it asked for."""
    meta = f'<meta http-equiv="Content-Security-Policy" content="{_PAGE_POLICY}">'
    match = _DOCTYPE.match(html)
    if match:
        return html[: match.end()] + meta + html[match.end():]
    return meta + html


def _page_box(paper: str, orientation: str, papers: dict[str, str]) -> str:
    """The `_page_css` for a `paper` among `papers` and an `orientation`, or the
    error naming what is accepted."""
    paper_key = (paper or "A4").strip().lower()
    if paper_key not in papers:
        raise ValueError(f"paper must be one of {', '.join(papers.values())} — not {paper!r}")
    orient = (orientation or "portrait").strip().lower()
    if orient not in _ORIENTATIONS:
        raise ValueError(f"orientation must be portrait or landscape — not {orientation!r}")
    return _page_css(papers[paper_key], orient)


def _chromium_pdf(html: str) -> bytes:
    """Print an HTML page to PDF through headless Chromium and return the PDF.

    The page is written to a file of its own and opened as `file://`, so an
    image or stylesheet it names by a relative path is not found."""
    work = tempfile.mkdtemp(prefix="cerase-office-html-", dir="/tmp")
    try:
        html_path = os.path.join(work, "page.html")
        pdf_path = os.path.join(work, "page.pdf")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        # --no-sandbox: the container has no user namespaces for Chromium's own
        # sandbox, as in the deck renderer. A profile dir under the work dir so
        # nothing persists between calls.
        proc = subprocess.run(
            [
                CHROMIUM,
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--user-data-dir={os.path.join(work, 'profile')}",
                "--run-all-compositor-stages-before-draw",
                "--virtual-time-budget=5000",
                # The flag Chromium 154 reads; the older --print-to-pdf-no-header
                # is ignored and prints the date and the file URL on every page.
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                f"file://{html_path}",
            ],
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0 or not os.path.isfile(pdf_path):
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Chromium produced no PDF (exit {proc.returncode})"
                + (f": {stderr[-500:]}" if stderr else "")
            )
        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)


@mcp.tool()
def convert_html_to_pdf(input_b64: str | None = None, path: str | None = None, agent_id: str | None = None, output_filename: str | None = None, paper: str = "A4", orientation: str = "portrait", agent_binding: str = "") -> dict:
    """Convert an HTML page → PDF through headless Chromium, the way a browser prints it: CSS grid, flexbox, web fonts and background colours are kept. Provide `input_b64` OR a workspace `path`.

    `paper` is A4 (default), A3, A5, letter or legal; `orientation` is portrait (default) or landscape. A page that declares its own `@page` size keeps it.

    Only the HTML file is read: an image or stylesheet it names by a relative path is not found, so put images inline as `data:` URIs or link them by https URL."""
    page_css = _page_box(paper, orientation, _PAPERS)
    html = _resolve_input_bytes(input_b64, path, agent_id, agent_binding).decode("utf-8", errors="replace")
    out_bytes = _chromium_pdf(_with_page_policy(_with_page_css(html, page_css)))

    if output_filename:
        filename = output_filename
    elif path:
        filename = os.path.splitext(os.path.basename(path))[0] + ".pdf"
    else:
        filename = "page.pdf"
    return _deliver(agent_id, filename, out_bytes, agent_binding)


# ─── A business document: Markdown → HTML page → PDF ─────────────

_DOCUMENT_PAPERS = {"a4": "A4", "letter": "letter"}

# Raw HTML in the Markdown is read as text. `document/document.lua` removes what
# this switch does not reach: event-handler attributes, and links and images
# outside the schemes a document needs.
_DOCUMENT_READER = "markdown-raw_html-raw_attribute"


def _document_html(markdown: bytes, page_css: str, template_css: str) -> str:
    """The standalone page `render_document` prints: pandoc's HTML with the page
    box first, then the built-in stylesheet, then `template_css`."""
    if re.search(r"</style", template_css, flags=re.IGNORECASE):
        raise ValueError("template_css is a stylesheet and cannot contain `</style`")
    with open(os.path.join(DOCUMENT_DIR, "style.css"), encoding="utf-8") as f:
        styles = f"<style>\n{f.read()}</style>\n"
    if template_css:
        styles += f"<style>\n{template_css}\n</style>\n"

    work = tempfile.mkdtemp(prefix="cerase-office-doc-", dir="/tmp")
    try:
        source = os.path.join(work, "document.md")
        produced = os.path.join(work, "document.html")
        with open(source, "wb") as f:
            f.write(markdown)
        proc = subprocess.run(
            [
                PANDOC, source,
                "-f", _DOCUMENT_READER,
                "-t", "html5",
                "--standalone",
                f"--template={os.path.join(DOCUMENT_DIR, 'template.html')}",
                f"--lua-filter={os.path.join(DOCUMENT_DIR, 'document.lua')}",
                "--wrap=none",
                "-o", produced,
            ],
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0 or not os.path.isfile(produced):
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"pandoc could not read the document (exit {proc.returncode})"
                + (f": {stderr[-500:]}" if stderr else "")
            )
        with open(produced, encoding="utf-8") as f:
            html = f.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    head_end = html.lower().find("</head>")
    if head_end < 0:
        raise RuntimeError("pandoc wrote a page without a <head>")
    return _with_page_css(html[:head_end] + styles + html[head_end:], page_css)


def _pdf_page_count(pdf: bytes) -> int:
    """The pages of a PDF Chromium printed: its page tree is written uncompressed,
    one `/Type /Page` object per page."""
    return len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", pdf))


@mcp.tool()
def render_document(input_b64: str | None = None, path: str | None = None, output_filename: str = "document.pdf", paper: Literal["A4", "letter"] = "A4", orientation: Literal["portrait", "landscape"] = "portrait", template_css: str | None = None, template_path: str | None = None, agent_id: str | None = None, agent_binding: str = "") -> dict:
    """Render a business document a person will send (a quote, a proposal, a report, a letter) from Markdown to a PDF laid out as one: a title block on the first page, a sans-serif body, tables with a header row and right-aligned amounts, the page number in the footer. Provide `input_b64` OR a workspace `path` to the .md file. `convert_md_to_pdf` remains for plain technical text.

    A YAML block on top makes the title block: `title`, `subtitle`, `client` (or `recipient`), `reference` (or `number`), `date`, `author`, and `lang` (`it`, `en`, `fr`, `de` or `es`), which sets the language of the field labels. The body takes headings, paragraphs, bullet and numbered lists, pipe tables, bold, italic, links, blockquotes and images by https URL. In a pipe table, a `---:` column is right-aligned: use it for amounts. A line holding only `---`, with a blank line above and below, starts a new page. Raw HTML prints as text.

    `paper` is A4 (default) or letter; `orientation` is portrait (default) or landscape. `template_css` is CSS added after the built-in stylesheet, for brand colours and fonts: set `--doc-accent` (title, headings, rules) and `--doc-font` on `:root`, or override any rule. `template_path` names a CSS file in your workspace instead, and is ignored when `template_css` is given.

    The file is written to `outputs/<output_filename>` in your workspace (default `document.pdf`); a name ending in `.html` returns the HTML page instead of the PDF. Returns `{path, filename, size_bytes, format, pages}`, where `pages` is the PDF's page count and is absent for HTML."""
    page_css = _page_box(paper, orientation, _DOCUMENT_PAPERS)
    filename = output_filename or "document.pdf"
    as_html = filename.lower().endswith((".html", ".htm"))
    if not as_html and not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    markdown = _resolve_input_bytes(input_b64, path, agent_id, agent_binding)
    if not template_css and template_path:
        template_css = _load_workspace_bytes(agent_id, template_path, agent_binding).decode("utf-8", errors="replace")
    html = _document_html(markdown, page_css, template_css or "")

    if as_html:
        result = _deliver(agent_id, filename, html.encode("utf-8"), agent_binding)
        result["format"] = "html"
        return result
    pdf = _chromium_pdf(html)
    result = _deliver(agent_id, filename, pdf, agent_binding)
    result["format"] = "pdf"
    result["pages"] = _pdf_page_count(pdf)
    return result


# ─── A workbook built from rows ──────────────────────────────────

class Sheet(BaseModel):
    """One worksheet of `create_xlsx`."""

    name: str = Field(description="Sheet name: at most 31 characters, none of [ ] : * ? / \\.")
    rows: list[list[str | int | float | bool | None]] = Field(description="The rows, top to bottom. A string starting with `=` is a formula (`=SUM(B2:B9)`); a `YYYY-MM-DD` string is stored as a date; null leaves the cell empty.")
    header_rows: int = Field(default=1, description="How many top rows are a header: bold, shaded, and frozen so they stay visible while scrolling. 0 for none.")
    freeze: str | None = Field(default=None, description="The cell to freeze panes at, such as `B2`. Defaults to the first cell under the header.")
    column_widths: dict[str, float] | None = Field(default=None, description="Width per column letter, such as {\"A\": 30}. Columns not named are sized to their content.")
    number_formats: dict[str, str] | None = Field(default=None, description="Excel number format per column letter, applied under the header, such as {\"B\": \"#,##0.00 \\\"€\\\"\", \"C\": \"0.0%\"}.")


_SHEET_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_CELLS = 500_000


def _cell_value(value):
    if isinstance(value, str) and _ISO_DATE.match(value):
        try:
            return datetime.datetime.strptime(value, "%Y-%m-%d"), "yyyy-mm-dd"
        except ValueError:
            return value, None
    return value, None


def _build_workbook(sheets: list[Sheet]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    if not sheets:
        raise ValueError("a workbook needs at least one sheet")
    seen: set[str] = set()
    cells = 0
    for sheet in sheets:
        if not sheet.name or len(sheet.name) > 31:
            raise ValueError(f"sheet name {sheet.name!r} must be 1 to 31 characters")
        if _SHEET_FORBIDDEN.search(sheet.name):
            raise ValueError(f"sheet name {sheet.name!r} cannot contain [ ] : * ? / \\")
        if sheet.name.lower() in seen:
            raise ValueError(f"sheet name {sheet.name!r} is used twice; Excel compares names without case")
        seen.add(sheet.name.lower())
        if not sheet.rows:
            raise ValueError(f"sheet {sheet.name!r} has no rows")
        cells += sum(len(r) for r in sheet.rows)
    if cells > _MAX_CELLS:
        raise ValueError(f"the workbook holds {cells} cells; the limit is {_MAX_CELLS}")

    wb = Workbook()
    wb.remove(wb.active)
    bold = Font(bold=True)
    shade = PatternFill("solid", fgColor="E7E9EE")
    for sheet in sheets:
        ws = wb.create_sheet(sheet.name)
        widths: dict[int, int] = {}
        for r, row in enumerate(sheet.rows, start=1):
            for c, raw in enumerate(row, start=1):
                value, date_format = _cell_value(raw)
                cell = ws.cell(row=r, column=c, value=value)
                if date_format:
                    cell.number_format = date_format
                if r <= sheet.header_rows:
                    cell.font = bold
                    cell.fill = shade
                shown = len(str(raw)) if raw is not None and not (isinstance(raw, str) and raw.startswith("=")) else 8
                widths[c] = max(widths.get(c, 0), shown)
        for c, length in widths.items():
            ws.column_dimensions[get_column_letter(c)].width = min(max(8, round(length * 1.2) + 2), 60)
        for letter, width in (sheet.column_widths or {}).items():
            ws.column_dimensions[letter.upper()].width = width
        for letter, fmt in (sheet.number_formats or {}).items():
            for row in ws.iter_rows(min_row=sheet.header_rows + 1, min_col=ws[letter.upper() + "1"].column, max_col=ws[letter.upper() + "1"].column):
                for cell in row:
                    cell.number_format = fmt
        if sheet.freeze:
            ws.freeze_panes = sheet.freeze.upper()
        elif sheet.header_rows > 0:
            ws.freeze_panes = f"A{sheet.header_rows + 1}"

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@mcp.tool()
def create_xlsx(sheets: list[Sheet], output_filename: str = "workbook.xlsx", agent_id: str | None = None, agent_binding: str = "") -> dict:
    """Create an Excel .xlsx workbook from rows: one or more sheets, real formulas, a bold shaded header frozen at the top, Excel number formats per column, dates stored as dates. The file is written to `outputs/<output_filename>` in your workspace.

    Each sheet is {name, rows, header_rows?, freeze?, column_widths?, number_formats?}. Numbers go in as JSON numbers, not strings, so formulas can add them up.

    For OpenDocument (.ods) convert the result with `convert_xlsx_to_ods`; for a PDF with `convert_xlsx_to_pdf`."""
    filename = output_filename or "workbook.xlsx"
    if not filename.lower().endswith(".xlsx"):
        filename += ".xlsx"
    parsed = [s if isinstance(s, Sheet) else Sheet.model_validate(s) for s in sheets]
    return _deliver(agent_id, filename, _build_workbook(parsed), agent_binding)


# ─── Catch-all generic converter ─────────────────────────────────

@mcp.tool()
def convert(
    source_format: str,
    target_format: str,
    input_b64: str | None = None,
    path: str | None = None,
    agent_id: str | None = None,
    output_filename: str | None = None,
    reference_doc_b64: str | None = None,
    reference_doc_path: str | None = None,
    agent_binding: str = "",
) -> dict:
    """Generic conversion catch-all. Use the typed `convert_*_to_*` tools when the pair is known; fall back here for less common ones (e.g. rtf → odt, html → docx). Provide `input_b64` OR a workspace `path`.

    Supported via LibreOffice: docx/odt/rtf/html/txt/xlsx/ods/csv/pptx/odp. Supported via pandoc: md/markdown sources (target = docx/odt/pptx/pdf/html). For a designed HTML page to PDF use `convert_html_to_pdf`, which prints through a browser.

    For a markdown source going to docx/odt/pptx you may style the output from a template document (pandoc --reference-doc): pass `reference_doc_b64` (inline base64) OR `reference_doc_path` (a workspace file, for templates too big to inline). It does not apply to LibreOffice conversions or to PDF output.
    """
    return _convert_tool(source_format, target_format, input_b64, path, agent_id, output_filename, reference_doc_b64=reference_doc_b64, reference_doc_path=reference_doc_path, agent_binding=agent_binding)


if __name__ == "__main__":
    mcp.run()
