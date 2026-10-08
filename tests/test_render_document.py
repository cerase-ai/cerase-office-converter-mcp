"""`render_document`: Markdown printed as a business document a person will send.

`convert_md_to_pdf` goes through pandoc and XeLaTeX, and a client's quote made
with it read like a LaTeX paper. This tool turns the same Markdown into an HTML
page with a business stylesheet and prints it through the Chromium path
`convert_html_to_pdf` uses.

Two kinds of test live here:

  - the server's own logic, with pandoc and Chromium faked as in the other
    files: arguments, the order of the stylesheets, the page box, the result;
  - what only the real binaries can show — that raw HTML in the Markdown never
    becomes an element, and that a quote prints with its title and its pages.
    They need pandoc and Chromium, so they run inside the built image (CI runs
    the whole suite there with CERASE_REQUIRE_RENDERER=1, which turns a missing
    binary into a failure instead of a skip).

    python -m pytest tests/ -q
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
from html.parser import HTMLParser

import pytest

import server

AGENT = "agent-7"

QUOTE = """---
title: Preventivo per il rinnovo del sito
subtitle: Proposta tecnica ed economica
client: Rossi Arredamenti S.r.l.
reference: "2026/014"
date: 8 ottobre 2026
author: Guidance Studio
lang: it
---

# Oggetto

Il presente preventivo descrive le attività e i costi del rinnovo del sito istituzionale.

| Voce | Giorni | Importo |
|:-----|-------:|--------:|
| Analisi e progetto | 4 | 2.400,00 € |
| Sviluppo | 10 | 6.000,00 € |
| **Totale** | **14** | **8.400,00 €** |

---

# Condizioni

- Validità dell'offerta: 30 giorni.
- Pagamento: 40% all'ordine, 60% alla consegna.
"""

HOSTILE = """---
title: Offerta
---

Testo <script>alert(1)</script> e <img src="file:///etc/passwd"> e <iframe src="file:///etc/hostname"></iframe>.

[cliccami]{onclick="alert(2)"} e [link](javascript:alert(3)) e ![logo](file:///etc/passwd) e ![ok](https://example.com/logo.png){onerror="alert(4)" width=30%}
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _inline(result: dict) -> bytes:
    return base64.b64decode(result["contents_base64"])


def _fake_pdf(pages: int) -> bytes:
    """The shape Chromium writes: an uncompressed page tree, one leaf per page."""
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    leaves = "".join(f"{3 + i} 0 obj\n<</Type /Page\n/Parent 2 0 R>>\nendobj\n" for i in range(pages))
    return (
        "%PDF-1.4\n1 0 obj\n<</Type /Catalog\n/Pages 2 0 R>>\nendobj\n"
        f"2 0 obj\n<</Type /Pages\n/Count {pages}\n/Kids [{kids}]>>\nendobj\n{leaves}%%EOF\n"
    ).encode()


SKELETON = (
    "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<title>t</title>\n</head>\n"
    "<body>\n<main><h1>Pandoc body</h1></main>\n</body>\n</html>\n"
)


class FakeBinaries:
    """pandoc writes a skeleton page; Chromium records the page it was handed
    and writes a PDF of `pages` pages."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.pages = 2
        self.seen_html: str | None = None

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == server.PANDOC:
            with open(argv[argv.index("-o") + 1], "w", encoding="utf-8") as f:
                f.write(SKELETON)
        else:
            src = argv[-1]
            assert src.startswith("file://")
            with open(src[len("file://"):], encoding="utf-8") as f:
                self.seen_html = f.read()
            out = next(a.split("=", 1)[1] for a in argv if a.startswith("--print-to-pdf="))
            with open(out, "wb") as f:
                f.write(_fake_pdf(self.pages))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def argv_for(self, binary: str) -> list[str]:
        for argv in self.calls:
            if argv[0] == binary:
                return argv
        raise AssertionError(f"{binary} was never invoked; calls={self.calls}")


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b""


class FakeBroker:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return FakeResponse()


@pytest.fixture
def binaries(monkeypatch):
    fake = FakeBinaries()
    monkeypatch.setattr(server.subprocess, "run", fake)
    return fake


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setenv("CERASE_CONTROL_PLANE_URL", "http://cerase-control-plane")
    monkeypatch.setenv("CERASE_INTERNAL_SECRET", "internal-secret")
    fake = FakeBroker()
    monkeypatch.setattr(server.urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def no_broker(monkeypatch):
    monkeypatch.delenv("CERASE_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("CERASE_INTERNAL_SECRET", raising=False)


def _builtin_css() -> str:
    with open(os.path.join(server.DOCUMENT_DIR, "style.css"), encoding="utf-8") as f:
        return f.read()


# ─── The tool as the model sees it ───────────────────────────────────────

def _tool(name: str):
    return next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == name)


def test_render_document_is_registered_with_its_arguments():
    schema = _tool("render_document").inputSchema
    props = schema["properties"]
    assert set(props) == {
        "input_b64", "path", "output_filename", "paper", "orientation",
        "template_css", "template_path", "agent_id", "agent_binding",
    }
    assert schema.get("required", []) == []
    assert props["output_filename"]["default"] == "document.pdf"
    assert props["paper"]["default"] == "A4"
    assert props["orientation"]["default"] == "portrait"
    assert set(props["paper"]["enum"]) == {"A4", "letter"}
    assert set(props["orientation"]["enum"]) == {"portrait", "landscape"}


def test_the_description_says_which_documents_it_is_for_and_where_plain_text_goes():
    description = _tool("render_document").description
    for kind in ("quote", "proposal", "report", "letter"):
        assert kind in description
    assert "convert_md_to_pdf" in description
    assert "plain technical text" in description


# ─── What pandoc and Chromium are asked to do ────────────────────────────

def test_pandoc_reads_markdown_with_raw_html_off_and_the_safety_filter_on(binaries, no_broker):
    server.render_document(input_b64=_b64(QUOTE))
    argv = binaries.argv_for(server.PANDOC)
    assert argv[argv.index("-f") + 1] == "markdown-raw_html-raw_attribute"
    assert argv[argv.index("-t") + 1] == "html5"
    assert f"--lua-filter={os.path.join(server.DOCUMENT_DIR, 'document.lua')}" in argv
    assert f"--template={os.path.join(server.DOCUMENT_DIR, 'template.html')}" in argv


def test_the_pdf_is_printed_by_the_same_chromium_call_as_convert_html_to_pdf(binaries, no_broker):
    server.render_document(input_b64=_b64(QUOTE))
    argv = binaries.argv_for(server.CHROMIUM)
    assert "--headless" in argv and "--no-pdf-header-footer" in argv
    assert any(a.startswith("--print-to-pdf=") for a in argv)


def test_template_css_comes_after_the_built_in_stylesheet(binaries, no_broker):
    result = server.render_document(
        input_b64=_b64(QUOTE), template_css=":root { --doc-accent: #b00020; }", output_filename="q.html"
    )
    html = _inline(result).decode()
    builtin = html.index(_builtin_css().strip()[:200])
    brand = html.index("--doc-accent: #b00020;")
    assert builtin < brand < html.index("</head>")


def test_template_path_is_read_from_the_workspace_when_no_template_css_is_given(
    monkeypatch, tmp_path, binaries, no_broker
):
    monkeypatch.setenv("CERASE_TOOL_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "brand.css").write_text("h1 { color: #0a7d3b; }", encoding="utf-8")
    html = _inline(server.render_document(
        input_b64=_b64(QUOTE), template_path=str(tmp_path / "brand.css"), output_filename="q.html"
    )).decode()
    assert "h1 { color: #0a7d3b; }" in html
    # template_css wins when both are given.
    html = _inline(server.render_document(
        input_b64=_b64(QUOTE), template_css="h1 { color: #222; }",
        template_path=str(tmp_path / "brand.css"), output_filename="q.html",
    )).decode()
    assert "h1 { color: #222; }" in html and "#0a7d3b" not in html


def test_a_path_or_template_path_outside_the_workspace_is_never_opened(
    monkeypatch, tmp_path, binaries, no_broker
):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("CERASE_TOOL_WORKSPACE_ROOT", str(root))
    (tmp_path / "outside.css").write_text("h1 { color: red; }", encoding="utf-8")
    (tmp_path / "outside.md").write_text("# secret", encoding="utf-8")
    with pytest.raises(ValueError, match="no local file"):
        server.render_document(input_b64=_b64(QUOTE), template_path=str(root / ".." / "outside.css"))
    with pytest.raises(ValueError, match="no local file"):
        server.render_document(path=str(root / ".." / "outside.md"))
    assert binaries.calls == []


def test_template_css_cannot_close_its_style_element(binaries, no_broker):
    with pytest.raises(ValueError, match="</style"):
        server.render_document(input_b64=_b64(QUOTE), template_css="h1{} </STYLE><script>alert(1)</script>")


@pytest.mark.parametrize("paper, orientation, box", [
    ("A4", "portrait", "size: A4 portrait"),
    ("letter", "landscape", "size: letter landscape"),
    ("a4", "Landscape", "size: A4 landscape"),
])
def test_paper_and_orientation_set_the_page_box(binaries, no_broker, paper, orientation, box):
    server.render_document(input_b64=_b64(QUOTE), paper=paper, orientation=orientation)
    html = binaries.seen_html
    assert box in html
    # The page box comes first: the stylesheet after it sets the margins.
    assert html.index(box) < html.index(_builtin_css().strip()[:200])


def test_a_paper_or_orientation_the_tool_does_not_offer_is_refused(binaries, no_broker):
    with pytest.raises(ValueError, match="paper"):
        server.render_document(input_b64=_b64(QUOTE), paper="A3")
    with pytest.raises(ValueError, match="orientation"):
        server.render_document(input_b64=_b64(QUOTE), orientation="sideways")


def test_input_must_be_given_exactly_once(binaries, no_broker):
    with pytest.raises(ValueError, match="exactly one"):
        server.render_document()
    with pytest.raises(ValueError, match="exactly one"):
        server.render_document(input_b64=_b64(QUOTE), path="quote.md")


# ─── What comes back ─────────────────────────────────────────────────────

def test_a_pdf_goes_to_the_workspace_with_its_format_and_page_count(binaries, broker):
    binaries.pages = 3
    result = server.render_document(input_b64=_b64(QUOTE), agent_id=AGENT, output_filename="preventivo.pdf")
    assert result == {
        "path": "outputs/preventivo.pdf",
        "filename": "preventivo.pdf",
        "size_bytes": len(_fake_pdf(3)),
        "format": "pdf",
        "pages": 3,
    }
    assert broker.requests[-1].get_method() == "PUT"
    assert broker.requests[-1].data == _fake_pdf(3)


def test_without_a_broker_the_pdf_comes_back_inline(binaries, no_broker):
    result = server.render_document(input_b64=_b64(QUOTE), agent_id=AGENT)
    assert result["filename"] == "document.pdf"
    assert result["format"] == "pdf" and result["pages"] == 2
    assert _inline(result) == _fake_pdf(2)
    assert "path" not in result


def test_an_html_name_returns_the_page_and_never_prints_it(binaries, broker):
    result = server.render_document(input_b64=_b64(QUOTE), agent_id=AGENT, output_filename="preventivo.html")
    assert result["path"] == "outputs/preventivo.html"
    assert result["format"] == "html"
    assert "pages" not in result
    assert all(argv[0] != server.CHROMIUM for argv in binaries.calls)
    page = broker.requests[-1].data.decode()
    assert "Pandoc body" in page and _builtin_css().strip()[:200] in page
    assert "size: A4 portrait" in page


def test_an_output_name_without_an_extension_becomes_a_pdf(binaries, no_broker):
    assert server.render_document(input_b64=_b64(QUOTE), output_filename="offerta")["filename"] == "offerta.pdf"


def test_the_page_count_reads_every_leaf_of_a_nested_page_tree():
    # Chromium splits a long document's page tree into intermediate /Pages nodes.
    nested = (
        b"<</Type /Pages /Count 10 /Kids [3 0 R 4 0 R]>>"
        b"<</Type /Pages /Count 8>>" + b"<</Type /Page /Parent 3 0 R>>" * 8
        + b"<</Type /Pages /Count 2>>" + b"<</Type/Page/Parent 4 0 R>>" * 2
    )
    assert server._pdf_page_count(nested) == 10
    assert server._pdf_page_count(_fake_pdf(1)) == 1


# ─── With the real pandoc and Chromium ───────────────────────────────────

def _have(binary: str) -> bool:
    return os.path.isfile(binary) and os.access(binary, os.X_OK)


renderer = pytest.mark.skipif(
    not (_have(server.PANDOC) and _have(server.CHROMIUM)) and not os.environ.get("CERASE_REQUIRE_RENDERER"),
    reason="needs pandoc and Chromium; CI runs this file inside the built image",
)


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v or "") for k, v in attrs}))


def _elements(html: str) -> list[tuple[str, dict]]:
    parser = Elements()
    parser.feed(html)
    return parser.tags


@renderer
def test_a_quote_prints_its_title_block_table_and_two_pages(no_broker):
    from pypdf import PdfReader
    import io

    result = server.render_document(input_b64=_b64(QUOTE), output_filename="preventivo.pdf")
    pdf = _inline(result)
    reader = PdfReader(io.BytesIO(pdf))
    assert result["format"] == "pdf"
    assert result["pages"] == len(reader.pages) == 2

    first = reader.pages[0].extract_text()
    second = reader.pages[1].extract_text()
    flat = lambda text: re.sub(r"\s+", "", text)  # noqa: E731
    assert "Preventivo per il rinnovo del sito" in first
    assert "Rossi Arredamenti S.r.l." in first
    assert "8.400,00 €" in first
    # The horizontal rule starts a new page.
    assert "Condizioni" in second and "Condizioni" not in first
    # The running footer: the page number and the page count.
    assert "1/2" in flat(first) and "2/2" in flat(second)


@renderer
def test_the_html_page_carries_the_stylesheet_and_the_title_block(no_broker):
    html = _inline(server.render_document(input_b64=_b64(QUOTE), output_filename="preventivo.html")).decode()
    assert _builtin_css().strip()[:200] in html
    assert '<header class="title-block">' in html
    assert "Preventivo per il rinnovo del sito" in html
    assert "Proposta tecnica ed economica" in html
    # The labels follow `lang: it`.
    for label, value in (("Cliente", "Rossi Arredamenti S.r.l."), ("Riferimento", "2026/014"), ("Data", "8 ottobre 2026")):
        assert re.search(rf"<dt>{label}</dt>\s*<dd>{re.escape(value)}</dd>", html), label
    # `---:` right-aligns the amounts.
    assert re.search(r'<td style="text-align: right;">2\.400,00 €</td>', html)


@renderer
def test_raw_html_and_unsafe_attributes_never_become_live(no_broker):
    html = _inline(server.render_document(input_b64=_b64(HOSTILE), output_filename="offerta.html")).decode()
    tags = _elements(html)
    names = [tag for tag, _ in tags]
    assert "script" not in names and "iframe" not in names
    for tag, attrs in tags:
        assert not any(k.lower().startswith("on") for k in attrs), (tag, attrs)
        for key in ("src", "href"):
            value = attrs.get(key, "").lower()
            assert not value.startswith(("file:", "javascript:")), (tag, attrs)
    # The raw HTML is still there, as text the reader can see.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # An https image survives, with its width.
    assert any(tag == "img" and attrs.get("src") == "https://example.com/logo.png" for tag, attrs in tags)
    # And the page refuses scripts and local files even where the filter missed one.
    csp = next(attrs["content"] for tag, attrs in tags if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
    assert "default-src 'none'" in csp and "file:" not in csp


@renderer
def test_raw_html_prints_as_text_in_the_pdf(no_broker):
    from pypdf import PdfReader
    import io

    pdf = _inline(server.render_document(input_b64=_b64(HOSTILE)))
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "<script>alert(1)</script>" in text
    assert "root:" not in text  # /etc/passwd was never read into the page
