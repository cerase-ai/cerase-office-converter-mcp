"""Filling in a PDF that was received, without rebuilding it.

The assistant's container runs no Python, so a skill that told it to read a
form, stamp a signature or overlay a date needed libraries it could not import.
The five tools here do that work instead. Every fixture PDF is built with
reportlab inside the test (no binary fixture is committed), the AcroForm case
with reportlab's own `canvas.acroForm`, and the results are read back with
pdfplumber, pypdf and pypdfium2 — the same libraries the tools use, run for
real.

The property the whole change turns on: the original pages are never re-laid
out. Everything added is drawn on an overlay page merged ON TOP, so an
untouched page renders pixel for pixel as it came.

    python -m pytest tests/ -q
"""
from __future__ import annotations

import base64
import io

import pdfplumber
import pypdf
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageChops
from pypdf import PdfReader
from pypdf.generic import NameObject, NumberObject
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

import server

AGENT = "agent-7"


# ─── Fixtures built here, with reportlab ─────────────────────────

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _out(result: dict) -> bytes:
    return base64.b64decode(result["contents_base64"])


def _text_pdf(pages: list[list[tuple[str, float, float]]], size=A4) -> bytes:
    """One page per inner list; each entry is (text, x, baseline) in reportlab's
    bottom-left points, so a word's box is predictable."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=size)
    for lines in pages:
        for text, x, baseline in lines:
            pdf.drawString(x, baseline, text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _form_pdf(size=A4) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=size)
    width, height = size
    pdf.acroForm.textfield(name="nome", x=72, y=height - 120, width=200, height=20, forceBorder=True)
    pdf.acroForm.checkbox(name="consenso", x=72, y=height - 160, size=16, buttonStyle="check", borderWidth=1)
    pdf.drawString(72, height - 220, "Luogo")
    pdf.save()
    return buffer.getvalue()


def _line_pdf(size=A4) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=size)
    pdf.setLineWidth(2)
    pdf.line(72, size[1] - 300, 400, size[1] - 300)
    pdf.save()
    return buffer.getvalue()


def _signature_png(width=200, height=80) -> bytes:
    """A photographed signature: a dark stroke on white paper."""
    image = Image.new("RGB", (width, height), (255, 255, 255))
    for x in range(20, width - 20):
        image.putpixel((x, height // 2), (0, 0, 0))
        image.putpixel((x, height // 2 + 1), (0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _coloured_signature_png(width=200, height=80) -> bytes:
    """A signature in ink: a pure-blue stroke and a pure-red stamp on white paper."""
    image = Image.new("RGB", (width, height), (255, 255, 255))
    for x in range(20, width - 20):
        for dy in range(4):
            image.putpixel((x, 18 + dy), (0, 0, 255))
            image.putpixel((x, 48 + dy), (255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _with_rotation(data: bytes, degrees: int) -> bytes:
    reader = pypdf.PdfReader(io.BytesIO(data))
    reader.pages[0][NameObject("/Rotate")] = NumberObject(degrees)
    writer = pypdf.PdfWriter()
    writer.add_page(reader.pages[0])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _render(data: bytes, page: int = 1, scale: float = 1.0) -> Image.Image:
    document = pdfium.PdfDocument(data)
    return document[page - 1].render(scale=scale).to_pil()


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    """Results come back inline; no test reaches the control-plane or the network."""
    monkeypatch.delenv("CERASE_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("CERASE_INTERNAL_SECRET", raising=False)


# ─── inspect_pdf ─────────────────────────────────────────────────

def test_inspect_pdf_lists_pages_and_form_fields():
    result = server.inspect_pdf(input_b64=_b64(_form_pdf()))
    assert [page["number"] for page in result["pages"]] == [1]
    assert result["pages"][0]["width"] == pytest.approx(A4[0], abs=0.5)
    assert result["pages"][0]["height"] == pytest.approx(A4[1], abs=0.5)
    fields = {field["name"]: field for field in result["fields"]}
    assert fields["nome"]["type"] == "text"
    assert fields["consenso"]["type"] == "checkbox"
    for field in fields.values():
        assert field["page"] == 1
        assert len(field["box"]) == 4


def test_inspect_pdf_reports_no_fields_for_a_pdf_without_a_form():
    data = _text_pdf([[("Plain", 72, 700)]])
    assert server.inspect_pdf(input_b64=_b64(data))["fields"] == []


# ─── find_text_in_pdf ────────────────────────────────────────────

def test_find_text_in_pdf_returns_the_box_of_a_drawn_word_within_a_point():
    x, baseline = 100.0, 700.0
    data = _text_pdf([[("Luogo", x, baseline)]])
    match = server.find_text_in_pdf(input_b64=_b64(data), text="luogo")["matches"][0]
    assert match["page"] == 1
    assert match["text"].lower() == "luogo"
    assert match["box"][0] == pytest.approx(x, abs=1)
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        word = pdf.pages[0].extract_words()[0]
    assert match["box"][1] == pytest.approx(word["top"], abs=1)
    assert match["box"][3] == pytest.approx(word["bottom"], abs=1)


def test_find_text_in_pdf_matches_a_run_of_words_and_a_page():
    data = _text_pdf([[("Città di Bari", 100, 700)], [("Città di Bari", 100, 700)]])
    matches = server.find_text_in_pdf(input_b64=_b64(data), text="città di bari")["matches"]
    assert [match["page"] for match in matches] == [1, 2]
    only = server.find_text_in_pdf(input_b64=_b64(data), text="città di bari", page=2)["matches"]
    assert [match["page"] for match in only] == [2]


def test_find_text_in_pdf_returns_nothing_when_the_text_is_absent():
    data = _text_pdf([[("Luogo", 100, 700)]])
    assert server.find_text_in_pdf(input_b64=_b64(data), text="Assente")["matches"] == []


def test_find_text_in_pdf_matches_a_word_inside_a_longer_word():
    data = _text_pdf([[("Luogo:", 100, 700)]])
    match = server.find_text_in_pdf(input_b64=_b64(data), text="Luogo")["matches"][0]
    assert match["text"] == "Luogo"
    assert match["box"][0] == pytest.approx(100, abs=1)
    expected_x1 = 100 + pdfmetrics.stringWidth("Luogo", "Helvetica", 12)
    assert match["box"][2] == pytest.approx(expected_x1, abs=1)


def test_find_text_in_pdf_matches_a_word_before_an_underscore_run():
    data = _text_pdf([[("Luogo:______", 100, 700)]])
    match = server.find_text_in_pdf(input_b64=_b64(data), text="Luogo")["matches"][0]
    expected_x1 = 100 + pdfmetrics.stringWidth("Luogo", "Helvetica", 12)
    assert match["box"][2] == pytest.approx(expected_x1, abs=1)
    # It stops at the word: the colon after it is not part of the box.
    assert match["box"][2] < 100 + pdfmetrics.stringWidth("Luogo:", "Helvetica", 12)


# ─── add_to_pdf ──────────────────────────────────────────────────

def test_add_to_pdf_writes_text_to_the_right_of_an_anchor():
    data = _text_pdf([[("Luogo", 72, 700)]])
    result = server.add_to_pdf(
        input_b64=_b64(data),
        items=[{"text": "Bari", "anchor": "Luogo", "page": 1}],
    )
    with pdfplumber.open(io.BytesIO(_out(result))) as pdf:
        words = {word["text"]: word for word in pdf.pages[0].extract_words()}
    anchor, added = words["Luogo"], words["Bari"]
    assert added["x0"] > anchor["x1"]
    # The same line: their vertical spans overlap.
    assert added["top"] < anchor["bottom"] and added["bottom"] > anchor["top"]


def test_add_to_pdf_leaves_an_untouched_page_pixel_identical():
    data = _text_pdf([[("One", 72, 700)], [("Two", 72, 700)]])
    result = server.add_to_pdf(
        input_b64=_b64(data),
        items=[{"text": "Added", "page": 1, "x": 300, "y": 300}],
    )
    assert _render(_out(result), 2).tobytes() == _render(data, 2).tobytes()


def test_add_to_pdf_changes_only_the_item_box_on_a_touched_page():
    data = _text_pdf([[("Original", 72, 700)]])
    x, y = 300.0, 300.0
    result = server.add_to_pdf(
        input_b64=_b64(data),
        items=[{"text": "Added", "page": 1, "x": x, "y": y, "size": 18}],
    )
    before = _render(data).convert("RGB")
    after = _render(_out(result)).convert("RGB")
    changed = ImageChops.difference(before, after).getbbox()
    assert changed is not None  # something was added
    # getbbox is (left, upper, right, lower) in pixels; the item's own box plus
    # one pixel for the renderer's antialiasing must contain every change.
    assert changed[0] >= x - 1 and changed[1] >= y - 1
    assert changed[2] <= x + 120 + 1 and changed[3] <= y + 18 + 1


def test_a_signature_with_a_white_background_does_not_cover_the_line():
    data = _line_pdf()
    signature = _signature_png()
    line_y = 300.0
    drawn_height = 80.0  # the image is 200x80, drawn 200 pt wide
    result = server.add_to_pdf(
        input_b64=_b64(data),
        items=[{
            "image_b64": _b64(signature),
            "page": 1,
            "x": 72,
            "y": line_y - drawn_height / 2,
            "width": 200,
        }],
    )
    image = _render(_out(result)).convert("RGB")
    row = int(line_y)
    darks = [sum(image.getpixel((x, row))) for x in range(90, 250)]
    assert min(darks) < 200  # the line survives under the cleared white paper


def test_a_coloured_stroke_survives_the_white_cut_and_the_line_shows_through():
    data = _line_pdf()  # a line at top-down y = 300
    signature = _coloured_signature_png()
    result = server.add_to_pdf(
        input_b64=_b64(data),
        items=[{"image_b64": _b64(signature), "page": 1, "x": 72, "y": 260, "width": 200}],
    )
    image = _render(_out(result)).convert("RGB")
    blue = image.getpixel((150, 280))
    red = image.getpixel((150, 310))
    assert blue[2] > 180 and blue[0] < 80 and blue[1] < 80
    assert red[0] > 180 and red[1] < 80 and red[2] < 80
    # The white paper around the strokes is still transparent: the line is dark.
    assert sum(image.getpixel((150, 300))) < 200


def test_add_to_pdf_places_text_after_an_anchor_with_a_colon():
    data = _text_pdf([[("Luogo:______", 72, 700)]])
    result = server.add_to_pdf(
        input_b64=_b64(data),
        items=[{"text": "Bari", "anchor": "Luogo:", "page": 1}],
    )
    with pdfplumber.open(io.BytesIO(_out(result))) as pdf:
        # The value is written onto the dotted line, so its characters interleave
        # with the underscores; the letter itself is what must start after the colon.
        added = [char for char in pdf.pages[0].chars if char["text"] == "B"]
    assert added, "the added text is not on the page"
    colon_x1 = 72 + pdfmetrics.stringWidth("Luogo:", "Helvetica", 12)
    assert added[0]["x0"] > colon_x1


def test_handwriting_text_is_embedded_with_its_accents():
    data = _text_pdf([[("", 72, 700)]])
    result = server.add_to_pdf(
        input_b64=_b64(data),
        items=[{"text": "Città", "page": 1, "x": 72, "y": 100, "font": "handwriting"}],
    )
    with pdfplumber.open(io.BytesIO(_out(result))) as pdf:
        assert "Città" in pdf.pages[0].extract_text()


def test_all_pages_puts_the_item_on_every_page():
    data = _text_pdf([[("One", 72, 700)], [("Two", 72, 700)]])
    result = server.add_to_pdf(
        input_b64=_b64(data),
        items=[{"text": "Bozza", "all_pages": True, "x": 72, "y": 100}],
    )
    with pdfplumber.open(io.BytesIO(_out(result))) as pdf:
        assert all("Bozza" in page.extract_text() for page in pdf.pages)


def test_add_to_pdf_refuses_an_anchor_that_is_not_on_the_page():
    data = _text_pdf([[("Luogo", 72, 700)]])
    with pytest.raises(ValueError, match="Nonesiste"):
        server.add_to_pdf(
            input_b64=_b64(data),
            items=[{"text": "Bari", "anchor": "Nonesiste", "page": 1}],
        )


def test_add_to_pdf_refuses_a_rotated_page():
    data = _with_rotation(_text_pdf([[("Luogo", 72, 700)]]), 90)
    with pytest.raises(ValueError, match="rotated"):
        server.add_to_pdf(
            input_b64=_b64(data),
            items=[{"text": "Bari", "page": 1, "x": 72, "y": 72}],
        )


def test_add_to_pdf_refuses_a_page_out_of_range():
    data = _text_pdf([[("Luogo", 72, 700)]])
    with pytest.raises(ValueError, match="out of range"):
        server.add_to_pdf(
            input_b64=_b64(data),
            items=[{"text": "Bari", "page": 3, "x": 72, "y": 72}],
        )


def _embedded_image_widths(data: bytes) -> list[int]:
    reader = pypdf.PdfReader(io.BytesIO(data))
    xobjects = reader.pages[0]["/Resources"]["/XObject"]
    widths = []
    for reference in xobjects.values():
        obj = reference.get_object()
        if obj.get("/Subtype") == "/Image":
            widths.append(int(obj["/Width"]))
    return widths


def test_an_oversized_signature_is_downscaled_to_300_dpi():
    big = Image.new("RGB", (4000, 1500), (255, 255, 255))
    for x in range(0, 4000, 2):
        big.putpixel((x, 750), (0, 0, 0))
    buffer = io.BytesIO()
    big.save(buffer, format="PNG")
    data = _text_pdf([[("One", 72, 700)]])
    result = server.add_to_pdf(
        input_b64=_b64(data),
        items=[{"image_b64": _b64(buffer.getvalue()), "page": 1, "x": 72, "y": 100, "width": 140}],
    )
    widths = _embedded_image_widths(_out(result))
    # ceil(140 / 72 * 300) = 584 pixels is all a drawing 140 pt wide can hold.
    assert widths and max(widths) <= 584


def test_a_small_signature_is_not_upscaled():
    small = _signature_png(width=200, height=80)
    data = _text_pdf([[("One", 72, 700)]])
    result = server.add_to_pdf(
        input_b64=_b64(data),
        items=[{"image_b64": _b64(small), "page": 1, "x": 72, "y": 100, "width": 140}],
    )
    assert max(_embedded_image_widths(_out(result))) == 200


# ─── fill_pdf_form ───────────────────────────────────────────────

def test_fill_pdf_form_fills_a_text_field_and_keeps_it_editable():
    result = server.fill_pdf_form(input_b64=_b64(_form_pdf()), fields={"nome": "Bari"}, flatten=False)
    reader = PdfReader(io.BytesIO(_out(result)))
    assert reader.get_form_text_fields()["nome"] == "Bari"


def test_fill_pdf_form_with_flatten_leaves_no_editable_field():
    result = server.fill_pdf_form(input_b64=_b64(_form_pdf()), fields={"nome": "Bari"})
    data = _out(result)
    assert not PdfReader(io.BytesIO(data)).get_fields()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        assert "Bari" in pdf.pages[0].extract_text()


def test_fill_pdf_form_refuses_an_unknown_field_and_writes_nothing():
    with pytest.raises(ValueError, match="cognome"):
        server.fill_pdf_form(input_b64=_b64(_form_pdf()), fields={"cognome": "Rossi"})


# ─── render_pdf_page ─────────────────────────────────────────────

def test_render_pdf_page_delivers_a_png_the_page_size_times_the_scale():
    result = server.render_pdf_page(input_b64=_b64(_text_pdf([[("One", 72, 700)]])), page=1, scale=2.0)
    image = Image.open(io.BytesIO(_out(result)))
    assert image.format == "PNG"
    assert abs(image.width - A4[0] * 2) <= 2
    assert abs(image.height - A4[1] * 2) <= 2


def test_an_output_name_with_a_folder_is_written_once_under_outputs(monkeypatch):
    # An assistant passed `outputs/x.png`, read back `outputs/outputs/x.png`, and attached that.
    written = []
    monkeypatch.setattr(server, "_write_workspace_file",
                        lambda agent_id, path, data, binding="": written.append(path) or True)
    data = _b64(_text_pdf([[("One", 72, 700)]]))
    result = server.render_pdf_page(input_b64=data, page=1, agent_id="7", output_filename="outputs/anteprima.png")
    assert (result["path"], result["filename"], written) == ("outputs/anteprima.png", "anteprima.png", ["outputs/anteprima.png"])
    with pytest.raises(ValueError, match="names no file"):
        server.render_pdf_page(input_b64=data, page=1, agent_id="7", output_filename="outputs/")


def test_render_pdf_page_refuses_a_page_out_of_range():
    data = _text_pdf([[("One", 72, 700)]])
    with pytest.raises(ValueError, match="out of range"):
        server.render_pdf_page(input_b64=_b64(data), page=2)


# ─── The workspace guard, as for the existing tools ──────────────

def test_a_path_outside_the_workspace_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("CERASE_TOOL_WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="escapes the workspace root"):
        server._safe_local_path(str(tmp_path / ".." / "etc" / "passwd"))
    with pytest.raises(ValueError):
        server.inspect_pdf(path=str(tmp_path / ".." / "etc" / "passwd"))
