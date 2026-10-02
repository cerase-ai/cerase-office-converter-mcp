"""Every tool description this server sends the model is one line per paragraph.

FastMCP serves a tool's docstring as its description, dedented the way Python
3.13 dedents every docstring, so a docstring wrapped at the source's line
length reaches the model with a line break inside every paragraph. Every
prompt text Cerase ships is one line per paragraph with a blank line between
blocks; a list item and an `Args:` entry are one line each.

The rule is cerase-core's (control-plane/tests/Support/HardWrap.php), which
reads these descriptions again in its tool-descriptions fixture. This repo's
own CI is the only one that sees this server before its image is published,
so the same check runs here. The file is identical in each package-only
connector repo. Standard library only: it reads server.py, it does not import
it, and gives the text FastMCP serves.
"""
from __future__ import annotations

import ast
import inspect
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py" if (HERE / "server.py").is_file() else HERE.parent / "server.py"

STRUCTURAL = re.compile(r"^(#{1,6}\s|[-*+]\s|>\s?|\||\d+[.)]\s)")
SECTION = re.compile(r"^(Args|Arguments|Parameters|Raises|Returns|Yields):$")
MANY_ENTRIES = {"Args", "Arguments", "Parameters", "Raises"}
ENTRY = re.compile(r"^\*{0,2}[A-Za-z_][\w.]*(\s*\([^)]*\))?:(\s|$)")


def continuation_lines(text: str) -> list[int]:
    """The 1-based numbers of the lines that continue the line above them."""
    offending: list[int] = []
    in_fence = prev_non_empty = False
    section: dict | None = None
    for i, line in enumerate(re.split(r"\r?\n", text)):
        trimmed = line.lstrip()
        indent = len(line) - len(trimmed)
        if trimmed.startswith(("```", "~~~")):
            in_fence = not in_fence
            prev_non_empty = True
            continue
        if in_fence:
            prev_non_empty = trimmed != ""
            continue
        if trimmed == "":
            prev_non_empty = False
            section = None
            continue
        structural = bool(STRUCTURAL.match(trimmed))
        if section is not None and indent > section["header"]:
            if section["entry"] is None:
                section["entry"] = indent
                structural = True
            elif section["many"] and indent == section["entry"] and ENTRY.match(trimmed):
                structural = True
        elif m := SECTION.match(trimmed.rstrip()):
            section = {"header": indent, "entry": None, "many": m.group(1) in MANY_ENTRIES}
        else:
            section = None
        if prev_non_empty and not structural:
            offending.append(i + 1)
        prev_non_empty = True
    return offending


def tool_descriptions() -> dict[str, str]:
    """Each @mcp.tool's description, as FastMCP serves it."""
    found: dict[str, str] = {}
    for node in ast.walk(ast.parse(SERVER.read_text(encoding="utf-8"))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            if not (isinstance(target, ast.Attribute) and target.attr == "tool"):
                continue
            name, description = node.name, None
            for kw in deco.keywords if isinstance(deco, ast.Call) else []:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = kw.value.value
                if kw.arg == "description" and isinstance(kw.value, ast.Constant):
                    description = kw.value.value
            if description is None:
                description = inspect.cleandoc(ast.get_docstring(node, clean=False) or "")
            found[name] = description.strip()
    return found


class ToolDescriptionsAreOneLinePerParagraph(unittest.TestCase):
    def test_every_tool_description_is_one_line_per_paragraph_and_per_argument(self) -> None:
        descriptions = tool_descriptions()
        self.assertTrue(descriptions, f"no @mcp.tool found in {SERVER}")
        wrapped = {name: lines for name, text in descriptions.items() if (lines := continuation_lines(text))}
        self.assertEqual(
            wrapped,
            {},
            "these docstrings continue a line at the listed line numbers: reflow each paragraph, "
            "list item and Args entry to one line",
        )

    def test_the_rule_finds_each_kind_of_wrap(self) -> None:
        whole = "Read a file.\n\nArgs:\n    path: where it is.\n    agent_id: bound.\n\nReturns:\n    dict with `text`."
        self.assertEqual(continuation_lines(whole), [])
        self.assertEqual(continuation_lines("Read a file\nfrom the workspace."), [2])
        self.assertEqual(continuation_lines("Args:\n    path: where\n        it is.\n    agent_id: bound."), [3])
        self.assertEqual(continuation_lines("Returns:\n    dict with\n    `text`."), [3])
        self.assertEqual(continuation_lines("The states:\n  - `booked` — later\n    and not yet\nTell the user."), [3, 4])


if __name__ == "__main__":
    unittest.main()
