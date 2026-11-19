"""`output_filename` is a bare file name, written under `outputs/`.

An assistant passed `outputs/x.pdf` (or `fow/x.pdf`, or an absolute path) and
the old delivery step quietly kept only the last path part, so the answer named
a file the caller had not asked for. The rule now is explicit: the tool takes a
NAME, writes it to `outputs/<name>`, and refuses a path before it starts any
work — before the input is read, before a converter runs, before anything is
written. The refusal tells the caller the name to use instead.

    python -m pytest tests/ -q
"""
from __future__ import annotations

import base64
import inspect
import io
import re

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import server

AGENT = "7"


# ─── Fixtures built here, with reportlab ─────────────────────────

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _text_pdf() -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.drawString(72, 700, "One")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _form_pdf() -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    pdf.acroForm.textfield(name="nome", x=72, y=height - 120, width=200, height=20, forceBorder=True)
    pdf.save()
    return buffer.getvalue()


# ─── The message and the fakes ───────────────────────────────────

def _refusal(name: str) -> str:
    """The refusal for `name`, whose last path part (on either separator) is
    the name the caller should have passed."""
    last = name.replace("\\", "/").rsplit("/", 1)[-1]
    return (
        f'output_filename is a file name: the file is written to outputs/{last}. '
        f'Call again with "{last}".'
    )


def _record_writes(monkeypatch) -> list[str]:
    written: list[str] = []
    monkeypatch.setattr(
        server,
        "_write_workspace_file",
        lambda agent_id, path, data, binding="": written.append(path) or True,
    )
    return written


def _work_started(*args, **kwargs):
    raise AssertionError("work started")


# Every tool that takes an output name, with the smallest valid arguments
# besides the name and the agent.
TOOLS = [
    pytest.param(
        lambda **kw: server.convert_docx_to_pdf(input_b64=_b64(b"docx bytes"), **kw),
        id="convert_docx_to_pdf",
    ),
    pytest.param(
        lambda **kw: server.convert_html_to_pdf(input_b64=_b64(b"<html><body>One</body></html>"), **kw),
        id="convert_html_to_pdf",
    ),
    pytest.param(
        lambda **kw: server.render_document(input_b64=_b64(b"# One"), **kw),
        id="render_document",
    ),
    pytest.param(
        lambda **kw: server.create_xlsx(sheets=[{"name": "Foglio", "rows": [["Voce", 1]]}], **kw),
        id="create_xlsx",
    ),
    pytest.param(
        lambda **kw: server.fill_pdf_form(fields={"nome": "Bari"}, input_b64=_b64(_form_pdf()), **kw),
        id="fill_pdf_form",
    ),
    pytest.param(
        lambda **kw: server.render_pdf_page(page=1, input_b64=_b64(_text_pdf()), **kw),
        id="render_pdf_page",
    ),
    pytest.param(
        lambda **kw: server.add_to_pdf(
            items=[{"text": "Bari", "page": 1, "x": 72, "y": 100}],
            input_b64=_b64(_text_pdf()),
            **kw,
        ),
        id="add_to_pdf",
    ),
]


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    monkeypatch.delenv("CERASE_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("CERASE_INTERNAL_SECRET", raising=False)


# ─── What a bare name does ───────────────────────────────────────

def test_a_bare_name_is_written_under_outputs(monkeypatch):
    written = _record_writes(monkeypatch)
    result = server.render_pdf_page(
        input_b64=_b64(_text_pdf()), page=1, agent_id=AGENT, output_filename="anteprima.pdf"
    )
    assert (result["path"], result["filename"]) == ("outputs/anteprima.pdf", "anteprima.pdf")
    assert written == ["outputs/anteprima.pdf"]


def test_a_name_that_already_says_outputs_is_accepted(monkeypatch):
    written = _record_writes(monkeypatch)
    result = server.render_pdf_page(
        input_b64=_b64(_text_pdf()), page=1, agent_id=AGENT, output_filename="outputs/anteprima.pdf"
    )
    assert (result["path"], result["filename"]) == ("outputs/anteprima.pdf", "anteprima.pdf")
    assert written == ["outputs/anteprima.pdf"]


# ─── A path is refused, and nothing is written ───────────────────

@pytest.mark.parametrize(
    "name",
    [
        "fow/x.pdf",
        "outputs/fow/x.pdf",
        "/abs/x.pdf",
        "../x.pdf",
        "a\\b.pdf",
    ],
)
def test_a_path_in_the_output_name_is_refused_and_writes_nothing(monkeypatch, name):
    written = _record_writes(monkeypatch)
    with pytest.raises(ValueError, match=re.escape(_refusal(name))):
        server.render_pdf_page(
            input_b64=_b64(_text_pdf()), page=1, agent_id=AGENT, output_filename=name
        )
    assert written == []


@pytest.mark.parametrize("name", ["outputs/"])
def test_a_name_with_no_file_is_refused(monkeypatch, name):
    written = _record_writes(monkeypatch)
    with pytest.raises(ValueError, match="names no file"):
        server.render_pdf_page(
            input_b64=_b64(_text_pdf()), page=1, agent_id=AGENT, output_filename=name
        )
    assert written == []


# ─── The refusal comes before any work ──────────────────────────

@pytest.mark.parametrize("call", TOOLS)
def test_every_tool_refuses_a_path_before_any_work(monkeypatch, call):
    written = _record_writes(monkeypatch)
    for step in (
        "_resolve_input_bytes",
        "_soffice_convert",
        "_pandoc_convert",
        "_chromium_pdf",
        "_build_workbook",
        "_fill_pdf_form",
        "_add_to_pdf",
        "_render_pdf_page",
    ):
        monkeypatch.setattr(server, step, _work_started)
    with pytest.raises(ValueError, match=re.escape(_refusal("fow/x.pdf"))):
        call(output_filename="fow/x.pdf", agent_id=AGENT)
    assert written == []


def test_a_conversion_refuses_the_name_before_the_converter_runs(monkeypatch):
    monkeypatch.setattr(server, "_soffice_convert", _work_started)
    with pytest.raises(ValueError, match=re.escape(_refusal("fow/x.pdf"))):
        server.convert_docx_to_pdf(
            input_b64=_b64(b"docx bytes"), agent_id=AGENT, output_filename="fow/x.pdf"
        )


def test_a_conversion_refuses_the_name_before_the_input_is_read(monkeypatch):
    # No input at all: the old order answered "provide exactly one of
    # `input_b64` or `path`"; the name is refused first.
    monkeypatch.setattr(server, "_resolve_input_bytes", _work_started)
    with pytest.raises(ValueError, match=re.escape(_refusal("fow/x.pdf"))):
        server.convert_docx_to_pdf(agent_id=AGENT, output_filename="fow/x.pdf")


def test_render_pdf_page_refuses_the_name_before_the_input_is_read(monkeypatch):
    monkeypatch.setattr(server, "_resolve_input_bytes", _work_started)
    with pytest.raises(ValueError, match=re.escape(_refusal("fow/x.pdf"))):
        server.render_pdf_page(page=1, agent_id=AGENT, output_filename="fow/x.pdf")


def test_add_to_pdf_refuses_the_name_before_the_input_is_read(monkeypatch):
    monkeypatch.setattr(server, "_resolve_input_bytes", _work_started)
    with pytest.raises(ValueError, match=re.escape(_refusal("fow/x.pdf"))):
        server.add_to_pdf(
            items=[{"text": "Bari", "page": 1, "x": 72, "y": 100}],
            agent_id=AGENT,
            output_filename="fow/x.pdf",
        )


# ─── The description tells the model the rule ───────────────────

def _tools_with_an_output_filename():
    for name, function in vars(server).items():
        if name.startswith("_") or not inspect.isfunction(function):
            continue
        if function.__module__ != server.__name__:
            continue
        if "output_filename" in inspect.signature(function).parameters:
            yield name, function


def test_every_tool_with_an_output_filename_describes_the_rule():
    tools = dict(_tools_with_an_output_filename())
    assert tools, "no tool takes an output_filename"
    for name, function in tools.items():
        description = inspect.getdoc(function) or ""
        assert "file name" in description, f"{name}: the description does not say `file name`"
        assert "outputs/" in description, f"{name}: the description does not say `outputs/`"
        assert "path" in description, f"{name}: the description does not say `path`"
