#!/usr/bin/env python3
"""Every tool converts a real file inside the built image.

The unit tests fake LibreOffice, pandoc and Chromium, so they prove the server's
routing and never that a conversion works. `convert_md_to_pdf` failed on every
input for as long as the image lacked `lmodern.sty`, with the tests green: the
first real run said `LaTeX Error: File 'lmodern.sty' not found`.

Run inside the image, with no control-plane variables so results come back
inline; CI runs it between the build and the push:

    docker run --rm -v "$PWD/scripts:/smoke:ro" --entrypoint python <image> /smoke/smoke.py

Exits 1 naming each tool whose output is missing or is not the format asked for.
"""
from __future__ import annotations

import base64
import io
import sys
import zipfile

sys.path.insert(0, "/app")
import server  # noqa: E402

REPORT = """---
title: Quarterly report for Northwind Traders
---

# Summary

Revenue reached €2.3M in Q3; accents such as città and qualità print.

| Region | Revenue |
|---|---|
| North | €1.4M |
"""

DECK = """---
title: Q3 for the Northwind board
---

## Revenue grew 18% on the quarter

- €2.3M in Q3
- Three new accounts

## We ask for a second sales hire

::: notes
Speaker notes.
:::
"""

PAGE = """<!doctype html><html lang="en"><head><style>
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}header{background:#123;color:#fff;padding:24px}
</style></head><body><header><h1>Acme Widgets</h1></header><main class="grid"><p>Fast</p><p>Cheap</p></main></body></html>"""


def b64(text: str | bytes) -> str:
    data = text.encode() if isinstance(text, str) else text
    return base64.b64encode(data).decode()


def body(result: dict) -> bytes:
    return base64.b64decode(result["contents_base64"])


def is_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def is_zip_with(data: bytes, member: str) -> bool:
    try:
        return member in zipfile.ZipFile(io.BytesIO(data)).namelist()
    except zipfile.BadZipFile:
        return False


def main() -> int:
    failed: list[str] = []

    def check(name: str, run, ok) -> bytes | None:
        try:
            data = body(run())
        except Exception as err:  # noqa: BLE001 — every failure is reported by name
            failed.append(f"{name}: {type(err).__name__}: {str(err)[-300:]}")
            return None
        if not ok(data):
            failed.append(f"{name}: the output is not the format asked for ({len(data)} bytes)")
            return None
        print(f"ok {name} ({len(data)} bytes)")
        return data

    docx = check("convert_md_to_docx", lambda: server.convert_md_to_docx(input_b64=b64(REPORT)), lambda d: is_zip_with(d, "word/document.xml"))
    check("convert_md_to_pdf", lambda: server.convert_md_to_pdf(input_b64=b64(REPORT)), is_pdf)
    pptx = check("convert_md_to_pptx", lambda: server.convert_md_to_pptx(input_b64=b64(DECK)), lambda d: is_zip_with(d, "ppt/slides/slide2.xml"))
    check("convert_html_to_pdf", lambda: server.convert_html_to_pdf(input_b64=b64(PAGE)), is_pdf)
    check("render_document", lambda: server.render_document(input_b64=b64(REPORT)), is_pdf)
    xlsx = check(
        "create_xlsx",
        lambda: server.create_xlsx(sheets=[{"name": "Sales", "rows": [["Region", "Q1", "Total"], ["North", 10, "=B2*2"]]}]),
        lambda d: is_zip_with(d, "xl/worksheets/sheet1.xml"),
    )
    if docx:
        check("convert_docx_to_pdf", lambda: server.convert_docx_to_pdf(input_b64=b64(docx)), is_pdf)
        check("convert_docx_to_odt", lambda: server.convert_docx_to_odt(input_b64=b64(docx)), lambda d: is_zip_with(d, "content.xml"))
    if pptx:
        check("convert_pptx_to_pdf", lambda: server.convert_pptx_to_pdf(input_b64=b64(pptx)), is_pdf)
        check("convert_pptx_to_odp", lambda: server.convert_pptx_to_odp(input_b64=b64(pptx)), lambda d: is_zip_with(d, "content.xml"))
    if xlsx:
        check("convert_xlsx_to_pdf", lambda: server.convert_xlsx_to_pdf(input_b64=b64(xlsx)), is_pdf)
        check("convert_xlsx_to_ods", lambda: server.convert_xlsx_to_ods(input_b64=b64(xlsx)), lambda d: is_zip_with(d, "content.xml"))

    if failed:
        print("\n".join(f"x  {line}" for line in failed))
        return 1
    print("every tool converted a real file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
