#!/usr/bin/env python3
"""Cerase Office Converter — MCP server.

Exposes cross-format document conversion tools backed by:
  - LibreOffice headless (`soffice --headless --convert-to`) for binary
    Office formats (.docx/.xlsx/.pptx) ↔ ODF (.odt/.ods/.odp) ↔ PDF.
  - pandoc for markup ↔ Office (.md ↔ .docx/.odt + html/latex/...).
  - xelatex (TeX Live) as the PDF engine for pandoc markdown → PDF.

All tools accept base64-encoded input and return base64-encoded
output (Cerase MCP convention — see cerase-deck-renderer-mcp).

LibreOffice startup is slow (~3s cold boot) because of Java +
templates init. To avoid this latency hit per call, we run LibreOffice
in headless mode and keep a long-running profile dir per container
(spawned once at first conversion, reused thereafter).
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import uuid

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cerase-office-converter")

SOFFICE = shutil.which("soffice") or "/usr/bin/soffice"
PANDOC = shutil.which("pandoc") or "/usr/bin/pandoc"
XELATEX = shutil.which("xelatex") or "/usr/bin/xelatex"

# Long-running profile so soffice doesn't re-init each call.
PROFILE_DIR = "/tmp/cerase-office-profile"
os.makedirs(PROFILE_DIR, exist_ok=True)


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


def _do_conversion(input_b64: str, source_ext: str, target_format: str) -> dict:
    work = tempfile.mkdtemp(prefix="cerase-office-", dir="/tmp")
    try:
        in_path = os.path.join(work, f"input.{source_ext}")
        with open(in_path, "wb") as f:
            f.write(base64.b64decode(input_b64))
        # Pandoc handles markdown-input cases; everything else routes
        # through LibreOffice for higher fidelity binary↔ODF.
        if source_ext in ("md", "markdown"):
            produced = _pandoc_convert(in_path, target_format, work)
        else:
            produced = _soffice_convert(in_path, target_format, work)
        with open(produced, "rb") as f:
            out_bytes = f.read()
        out_b64 = base64.b64encode(out_bytes).decode("ascii")
        return {
            "filename": os.path.basename(produced),
            "size_bytes": len(out_bytes),
            "contents_base64": out_b64,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ─── Specific tool pairs ─────────────────────────────────────────

@mcp.tool()
def convert_docx_to_pdf(input_b64: str) -> dict:
    """Convert a Word .docx (base64-encoded) → PDF."""
    return _do_conversion(input_b64, "docx", "pdf")


@mcp.tool()
def convert_docx_to_odt(input_b64: str) -> dict:
    """Convert a Word .docx → LibreOffice .odt."""
    return _do_conversion(input_b64, "docx", "odt")


@mcp.tool()
def convert_odt_to_docx(input_b64: str) -> dict:
    """Convert a LibreOffice .odt → Word .docx."""
    return _do_conversion(input_b64, "odt", "docx")


@mcp.tool()
def convert_pptx_to_pdf(input_b64: str) -> dict:
    """Convert a PowerPoint .pptx → PDF."""
    return _do_conversion(input_b64, "pptx", "pdf")


@mcp.tool()
def convert_pptx_to_odp(input_b64: str) -> dict:
    """Convert a PowerPoint .pptx → LibreOffice .odp."""
    return _do_conversion(input_b64, "pptx", "odp")


@mcp.tool()
def convert_odp_to_pptx(input_b64: str) -> dict:
    """Convert a LibreOffice .odp → PowerPoint .pptx."""
    return _do_conversion(input_b64, "odp", "pptx")


@mcp.tool()
def convert_xlsx_to_pdf(input_b64: str) -> dict:
    """Convert an Excel .xlsx → PDF."""
    return _do_conversion(input_b64, "xlsx", "pdf")


@mcp.tool()
def convert_xlsx_to_ods(input_b64: str) -> dict:
    """Convert an Excel .xlsx → LibreOffice .ods."""
    return _do_conversion(input_b64, "xlsx", "ods")


@mcp.tool()
def convert_ods_to_xlsx(input_b64: str) -> dict:
    """Convert a LibreOffice .ods → Excel .xlsx."""
    return _do_conversion(input_b64, "ods", "xlsx")


@mcp.tool()
def convert_md_to_pdf(input_b64: str) -> dict:
    """Convert plain markdown → PDF via pandoc + xelatex."""
    return _do_conversion(input_b64, "md", "pdf")


@mcp.tool()
def convert_md_to_docx(input_b64: str) -> dict:
    """Convert plain markdown → Word .docx via pandoc."""
    return _do_conversion(input_b64, "md", "docx")


# ─── Catch-all generic converter ─────────────────────────────────

@mcp.tool()
def convert(
    input_b64: str,
    source_format: str,
    target_format: str,
) -> dict:
    """Generic conversion catch-all. Use the typed `convert_*_to_*`
    tools when the pair is known; fall back here for less common ones
    (e.g. rtf → odt, html → docx).

    Supported via LibreOffice: docx/odt/rtf/html/txt/xlsx/ods/csv/pptx/odp.
    Supported via pandoc: md/markdown sources (target = docx/odt/pdf/html).
    """
    return _do_conversion(input_b64, source_format, target_format)


if __name__ == "__main__":
    mcp.run()
