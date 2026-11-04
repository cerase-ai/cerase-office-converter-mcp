"""`create_xlsx` names text cells that Excel will not treat as numbers or dates.

A spreadsheet built from strings like "4" looks right but cannot be summed, and
that happened to a real customer's sheet. The answer therefore lists every cell
that was passed as TEXT but reads as a number or a date, so the assistant can
correct the call before handing the file over. The workbook itself is not
changed: the tool reports, it does not convert.

    python -m pytest tests/test_text_that_reads_as_a_number.py -q
"""
from __future__ import annotations

import base64
import datetime
import io

import pytest
from openpyxl import load_workbook

import server


@pytest.fixture
def no_broker(monkeypatch):
    monkeypatch.delenv("CERASE_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("CERASE_INTERNAL_SECRET", raising=False)


def _workbook(result: dict):
    return load_workbook(io.BytesIO(base64.b64decode(result["contents_base64"])))


def _named(result: dict) -> list[dict]:
    return result.get("cells_written_as_text") or []


def _by_cell(result: dict) -> dict[str, dict]:
    return {item["cell"]: item for item in _named(result)}


def test_text_numbers_and_dates_are_named_with_sheet_cell_value_and_kind(no_broker):
    result = server.create_xlsx(sheets=[{
        "name": "Ordini",
        "rows": [["Voce", "Quantità", "Data"], ["Ore", "4", "04/11/2026"]],
    }])

    items = _named(result)
    by_cell = _by_cell(result)

    assert set(by_cell) == {"B2", "C2"}
    for item in items:
        assert {"sheet", "cell", "value", "reads_as"} <= set(item)
        assert item["sheet"] == "Ordini"
    assert by_cell["B2"]["value"] == "4"
    assert by_cell["B2"]["reads_as"] == "number"
    assert by_cell["C2"]["value"] == "04/11/2026"
    assert by_cell["C2"]["reads_as"] == "date"


@pytest.mark.parametrize("value", [
    "1.234,56",
    "1,234.56",
    "12,5",
    "-3",
    "15%",
    "€ 1.200",
    "1200 €",
])
def test_italian_and_english_number_shapes_are_named_as_numbers(no_broker, value):
    result = server.create_xlsx(sheets=[{"name": "Numeri", "rows": [["Valore"], [value]]}])

    items = _named(result)
    assert len(items) == 1, items
    assert items[0]["sheet"] == "Numeri"
    assert items[0]["cell"] == "A2"
    assert items[0]["value"] == value
    assert items[0]["reads_as"] == "number"


@pytest.mark.parametrize("value", [
    "04/11/2026",
    "4.11.2026",
    "04-11-2026",
    "4/11/26",
])
def test_date_shapes_are_named_as_dates(no_broker, value):
    result = server.create_xlsx(sheets=[{"name": "Date", "rows": [["Data"], [value]]}])

    items = _named(result)
    assert len(items) == 1, items
    assert items[0]["sheet"] == "Date"
    assert items[0]["cell"] == "A2"
    assert items[0]["value"] == value
    assert items[0]["reads_as"] == "date"


LEFT_ALONE = [
    "00123",
    "0039 333 1234567",
    "+39 333 1234567",
    "IT60X0542811101000000123456",
    "Ordine 4",
    "v2.0.1",
    "",
    "2026-11-04",
]


def test_ids_phones_ibans_words_and_iso_dates_are_left_alone(no_broker):
    rows = [["Valore"]] + [[value] for value in LEFT_ALONE] + [["4"]]
    result = server.create_xlsx(sheets=[{"name": "Misto", "rows": rows}])
    by_cell = _by_cell(result)

    for offset, value in enumerate(LEFT_ALONE):
        cell = f"A{offset + 2}"
        assert cell not in by_cell, f"{value!r} should be left alone"

    # A real text number in the same sheet is still named, so this is not a
    # test that passes merely because nothing is reported.
    assert by_cell["A10"]["reads_as"] == "number"
    # The ISO string was stored as a real date, so it was never text.
    assert _workbook(result)["Misto"]["A9"].value == datetime.datetime(2026, 11, 4)


def test_a_json_number_and_a_formula_are_not_named(no_broker):
    result = server.create_xlsx(sheets=[{
        "name": "Calcolo",
        "rows": [["Testo", "Numero", "Formula"], ["4", 4, "=SUM(B2:B3)"]],
    }])
    by_cell = _by_cell(result)

    assert by_cell["A2"]["reads_as"] == "number"
    assert "B2" not in by_cell
    assert "C2" not in by_cell


def test_header_rows_are_not_checked(no_broker):
    with_header = server.create_xlsx(sheets=[{
        "name": "Intestazione",
        "rows": [["2026"], ["4"]],
    }])
    cells = _by_cell(with_header)
    assert "A1" not in cells
    assert cells["A2"]["reads_as"] == "number"

    no_header = server.create_xlsx(sheets=[{
        "name": "Intestazione",
        "rows": [["2026"]],
        "header_rows": 0,
    }])
    cells = _by_cell(no_header)
    assert cells["A1"]["reads_as"] == "number"


def test_the_answer_tells_the_assistant_how_to_fix_the_call(no_broker):
    named = server.create_xlsx(sheets=[{"name": "S", "rows": [["Valore"], ["4"]]}])
    assert _named(named)
    note = named.get("note")
    assert isinstance(note, str) and note.strip()
    assert "json number" in note.lower()
    assert "yyyy-mm-dd" in note.lower()

    clean = server.create_xlsx(sheets=[{
        "name": "S",
        "rows": [[4, "=SUM(A1:A2)"]],
        "header_rows": 0,
    }])
    assert _named(clean) == []
    assert not clean.get("note")


def test_the_list_is_capped_and_the_total_is_reported(no_broker):
    rows = [["N"]] + [[str(number)] for number in range(1, 301)]
    result = server.create_xlsx(sheets=[{"name": "Molti", "rows": rows}])

    items = _named(result)
    assert 0 < len(items) <= 50
    assert result["cells_written_as_text_total"] == 300


def test_naming_does_not_change_the_cell(no_broker):
    result = server.create_xlsx(sheets=[{
        "name": "Ordini",
        "rows": [["Voce", "Quantità", "Data"], ["Ore", "4", "04/11/2026"]],
    }])

    assert _by_cell(result)["B2"]["reads_as"] == "number"
    cell = _workbook(result)["Ordini"]["B2"]
    assert cell.value == "4"
    assert isinstance(cell.value, str)
