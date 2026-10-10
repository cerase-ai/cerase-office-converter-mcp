"""`convert_html_to_pdf` prints the page it is given and no file of the converter.

Building `render_document` on 8 October showed that the older HTML tool printed
a file of the converter's own container when the HTML named it in an `<iframe>`:
Chromium opens the page as `file://`, and a page opened that way may frame any
other file. `render_document` refuses that with a content security policy in its
template; this tool had none. An assistant may pass HTML it did not write, a
page copied from a mail or the web, so the page's own markup must not be able to
reach into the container.

The file here is invented and written by the test, so a print that carries its
words is a print that read it. They need the real Chromium, so they run inside
the built image (CI runs the suite there with CERASE_REQUIRE_RENDERER=1).

    python -m pytest tests/ -q
"""
from __future__ import annotations

import base64
import io
import os
import uuid

import pytest

import server

def _have(binary: str) -> bool:
    return os.path.isfile(binary) and os.access(binary, os.X_OK)


# The image's own renderers, as in test_render_document.py: a runner's
# /usr/bin/chromium can be a wrapper that never prints, so a Chromium alone is
# not the image.
renderer = pytest.mark.skipif(
    not (_have(server.PANDOC) and _have(server.CHROMIUM)) and not os.environ.get("CERASE_REQUIRE_RENDERER"),
    reason="needs the image's Chromium; CI runs this file inside the built image",
)

SECRET = "Lucertola Fiordaliso 7781"


@pytest.fixture
def no_broker(monkeypatch):
    monkeypatch.delenv("CERASE_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("CERASE_INTERNAL_SECRET", raising=False)


@pytest.fixture
def own_file():
    """A file of the converter's own filesystem that no page should reach."""
    path = f"/tmp/cerase-office-own-{uuid.uuid4().hex}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{SECRET}\n")
    yield path
    os.unlink(path)


def _printed_text(html: str) -> str:
    from pypdf import PdfReader

    result = server.convert_html_to_pdf(input_b64=base64.b64encode(html.encode()).decode(), output_filename="page.pdf")
    pdf = base64.b64decode(result["contents_base64"])
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


@renderer
@pytest.mark.parametrize("element", [
    '<iframe src="file://{path}" width="600" height="200"></iframe>',
    '<object data="file://{path}" type="text/plain" width="600" height="200"></object>',
    '<embed src="file://{path}" type="text/plain" width="600" height="200">',
])
def test_a_page_that_names_a_file_of_the_converter_does_not_print_it(no_broker, own_file, element):
    html = f"<!doctype html><html><head><title>t</title></head><body><p>Prima della cornice.</p>{element.format(path=own_file)}</body></html>"

    text = _printed_text(html)

    assert "Prima della cornice." in text
    assert SECRET not in text


@renderer
def test_a_page_with_markup_before_its_head_does_not_print_it_either(no_broker, own_file):
    # The policy must hold however the page is written: here the frame comes
    # before the page declares a head, where a policy placed in that head would
    # arrive too late.
    html = f'<iframe src="file://{own_file}" width="600" height="200"></iframe><html><head></head><body><p>Dopo.</p></body></html>'

    assert SECRET not in _printed_text(html)


@renderer
def test_a_page_keeps_its_inline_styles_and_its_text(no_broker):
    html = (
        "<!doctype html><html><head><style>.k{font-weight:bold}</style></head>"
        '<body><h1 class="k">Preventivo</h1><p style="color:#333">Totale 8.400,00 €</p></body></html>'
    )

    text = _printed_text(html)

    assert "Preventivo" in text and "8.400,00" in text
