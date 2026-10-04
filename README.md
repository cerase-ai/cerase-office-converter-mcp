# cerase-office-converter-mcp

An MCP server that converts documents between office formats and builds Excel
workbooks. Markdown sources go through pandoc (with XeLaTeX for PDF), HTML pages
to PDF through headless Chromium, workbooks through openpyxl, and every other
source through LibreOffice in headless mode. It calls no model.

## Tools

| Tool | What it does | Engine |
|---|---|---|
| `convert_docx_to_pdf` | Word `.docx` to PDF. | LibreOffice |
| `convert_docx_to_odt` | Word `.docx` to OpenDocument `.odt`. | LibreOffice |
| `convert_odt_to_docx` | OpenDocument `.odt` to Word `.docx`. | LibreOffice |
| `convert_pptx_to_pdf` | PowerPoint `.pptx` to PDF. | LibreOffice |
| `convert_pptx_to_odp` | PowerPoint `.pptx` to OpenDocument `.odp`. | LibreOffice |
| `convert_odp_to_pptx` | OpenDocument `.odp` to PowerPoint `.pptx`. | LibreOffice |
| `convert_xlsx_to_pdf` | Excel `.xlsx` to PDF. | LibreOffice |
| `convert_xlsx_to_ods` | Excel `.xlsx` to OpenDocument `.ods`. | LibreOffice |
| `convert_ods_to_xlsx` | OpenDocument `.ods` to Excel `.xlsx`. | LibreOffice |
| `convert_md_to_pdf` | Markdown to PDF. | pandoc + XeLaTeX |
| `convert_md_to_docx` | Markdown to Word `.docx`, optionally styled from a reference document. | pandoc |
| `convert_md_to_pptx` | Markdown to PowerPoint `.pptx` with editable slides, optionally styled from a template `.pptx`. | pandoc |
| `convert_html_to_pdf` | An HTML page to PDF as a browser prints it, keeping CSS grid, flexbox, web fonts and backgrounds; `paper` and `orientation` set the page when the page sets none. | Chromium |
| `create_xlsx` | An Excel `.xlsx` workbook built from rows: several sheets, formulas, a bold frozen header, number formats per column, dates as dates. | openpyxl |
| `convert` | Any pair named by `source_format` and `target_format`, for pairs without a dedicated tool, such as `rtf` to `odt` or `html` to `docx`. Its description lists `docx`, `odt`, `rtf`, `html`, `txt`, `xlsx`, `ods`, `csv`, `pptx` and `odp` for LibreOffice, and `docx`, `odt`, `pptx`, `pdf` and `html` as pandoc targets. | pandoc for `md`/`markdown` sources, LibreOffice otherwise |

Every conversion tool takes exactly one of `input_b64` (the file as base64) or `path` (a
file in the calling assistant's workspace), an optional `output_filename`, and
`agent_id` and `agent_binding`, which the Cerase gateway fills; the model never
sets them. Every tool returns `{path, filename, size_bytes}` when the result
was written into the workspace, or `{filename, size_bytes, contents_base64}`
when it was not. `create_xlsx` takes `sheets` instead of a file: each one a
`name`, its `rows`, and optionally `header_rows`, `freeze`, `column_widths` and
`number_formats`.

`convert_md_to_docx`, `convert_md_to_pptx` and `convert` (for a markdown source going to `docx`,
`odt` or `pptx`) accept a pandoc reference document that styles the output:
`reference_doc_b64` inline, or `reference_doc_path` from the workspace. It must
be of the same format as the output; with any other output format, and with
any LibreOffice conversion, the call is refused.

A conversion that LibreOffice reports as successful but that writes no file is
returned as an error.

Workspace access goes through the Cerase control-plane: a `path` is read
locally when it exists under `CERASE_TOOL_WORKSPACE_ROOT`, otherwise with
`GET /api/internal/workspace-file/<agent_id>?path=…`; the result is written to
`outputs/<filename>` with `PUT` on the same endpoint. Both requests present
`CERASE_INTERNAL_SECRET` as a bearer and `agent_binding` as
`X-Cerase-Agent-Binding`. When `agent_id`, `CERASE_CONTROL_PLANE_URL` or
`CERASE_INTERNAL_SECRET` is missing, the result comes back inline as base64.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `CERASE_CONTROL_PLANE_URL` | none | Control-plane base URL for reading `path` inputs and writing results into the workspace. |
| `CERASE_INTERNAL_SECRET` | none | Bearer token for those requests. |
| `CERASE_TOOL_WORKSPACE_ROOT` | `/workspace` | Directory a `path` is read from locally; a path resolving outside it is never opened. |

## Installation

The connector is published in the Cerase Marketplace as
`studio.guidance/cerase-office-converter`
([marketplace page](https://marketplace.cerase.ai/en/p/studio.guidance/cerase-office-converter)).
Every Cerase appliance installs it at boot, so its assistants have it without
an install step.

Each push to `main` runs the tests and publishes
`ghcr.io/cerase-ai/cerase-office-converter-mcp` (`.github/workflows/publish.yml`).

## Build and run locally

```sh
docker build -t cerase-office-converter-mcp .
docker run --rm -p 3000:3000 cerase-office-converter-mcp
```

The image installs LibreOffice, a headless Java runtime, pandoc, XeLaTeX,
Chromium, openpyxl and the Liberation, DejaVu and Noto fonts. `server.py` speaks MCP over stdio; the image
runs it behind `mcp-proxy`, which serves Streamable HTTP at
`http://localhost:3000/mcp` and SSE at `http://localhost:3000/sse`. Run without
the control-plane variables, every tool returns the converted file as base64.

The image's `HEALTHCHECK` runs `scripts/healthcheck.py`, an MCP client that
completes the handshake and lists the tools over `/mcp`; its
`CERASE_HEALTHCHECK_*` variables exist to point it at a stub in tests.

The tests fake LibreOffice, pandoc, Chromium and the control-plane; openpyxl
runs for real:

```sh
pip install -r requirements-dev.txt
python -m pytest tests/
```

## License

MIT. See [LICENSE](LICENSE).
