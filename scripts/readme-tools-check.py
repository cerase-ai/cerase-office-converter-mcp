#!/usr/bin/env python3
"""Fail when README.md's tool table and the tools server.py registers differ.

README.md's `## Tools` section holds a table whose first column is each tool's
name in backticks. This script reads those names, reads the names server.py
registers with FastMCP, and exits 1 naming every tool found on one side only.
A README with no `## Tools` section, or whose section names no tool, fails too.

The server is read with `ast`, never imported, so it needs none of the server's
dependencies. These forms are read, on any `<name>.tool` / `<name>.add_tool`:

    @mcp.tool()                    the function's name
    @mcp.tool("x") / name="x"      x
    mcp.tool(...)(function)        as the decorator
    mcp.add_tool(function)         the function's name, or its `name` argument
    mcp.remove_tool("x")           x is taken off the list

A name the server computes at run time cannot be read statically, and fails
rather than being guessed.

The same file is in every connector repo, byte for byte, and CI checks it
against scripts/readme-tools-check.sha256 before running it. A change to it is
made in every copy, with the new digest.

    python3 scripts/readme-tools-check.py [--server PATH] [--readme PATH]

Both paths default to the files in the directory above this script's own.
Standard library only.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADING = "## Tools"
DELIMITER_ROW = re.compile(r"^\|?(\s*:?-+:?\s*\|)+\s*:?-*:?\s*$")
BACKTICKED = re.compile(r"`([^`]+)`")


class CheckError(Exception):
    """A defect that stops the comparison before it can be made."""


def _literal_name(call: ast.Call, position: int) -> str | None:
    """The tool name a call passes as `name=` or at `position`, or None."""
    node = next((kw.value for kw in call.keywords if kw.arg == "name"), None)
    if node is None and len(call.args) > position:
        node = call.args[position]
    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise CheckError(
        f"line {call.lineno}: the tool name is computed at run time; "
        "this check reads only a string literal"
    )


def _method(node: ast.AST) -> str | None:
    """`tool`, `add_tool` or `remove_tool` when node is `<name>.<that>`."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.attr in ("tool", "add_tool", "remove_tool"):
            return node.attr
    return None


def _function_name(node: ast.AST, line: int) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    raise CheckError(f"line {line}: cannot tell which function is registered as a tool")


def server_tools(source: str, label: str) -> dict[str, int]:
    """Every tool name the server registers, with the line that registers it."""
    try:
        tree = ast.parse(source, filename=label)
    except SyntaxError as exc:
        raise CheckError(f"{label} does not parse: {exc}") from exc

    tools: dict[str, int] = {}
    removed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if _method(deco) == "tool":
                    tools[node.name] = deco.lineno
                elif isinstance(deco, ast.Call) and _method(deco.func) == "tool":
                    tools[_literal_name(deco, 0) or node.name] = deco.lineno
        elif isinstance(node, ast.Call):
            # mcp.tool(...)(function): the outer call's callee is the inner call.
            inner = node.func
            if isinstance(inner, ast.Call) and _method(inner.func) == "tool" and node.args:
                name = _literal_name(inner, 0) or _function_name(node.args[0], node.lineno)
                tools[name] = node.lineno
            elif _method(node.func) == "add_tool" and node.args:
                name = _literal_name(node, 1) or _function_name(node.args[0], node.lineno)
                tools[name] = node.lineno
            elif _method(node.func) == "remove_tool":
                name = _literal_name(node, 0)
                if name is None:
                    raise CheckError(f"line {node.lineno}: remove_tool names no tool")
                removed.add(name)
    for name in removed:
        tools.pop(name, None)
    if not tools:
        raise CheckError(
            f"{label} registers no tool in any form this check reads "
            "(@<server>.tool, <server>.tool(...)(fn), <server>.add_tool)"
        )
    return tools


def readme_tools(text: str, label: str) -> dict[str, int]:
    """Every tool name in the first column of the tables under `## Tools`."""
    lines = text.splitlines()
    in_fence = False
    start = None
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence and line.rstrip() == HEADING:
            start = number
            break
    if start is None:
        raise CheckError(f"{label} has no '{HEADING}' section")

    names: dict[str, int] = {}
    problems: list[str] = []
    row_in_table = 0
    for number, line in enumerate(lines[start:], start + 1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
        if in_fence:
            continue
        if re.match(r"#{1,2}\s", stripped):
            break
        if not stripped.startswith("|"):
            row_in_table = 0
            continue
        row_in_table += 1
        if row_in_table == 1 or DELIMITER_ROW.match(stripped):
            continue
        first_cell = stripped[1:].split("|", 1)[0]
        found = BACKTICKED.search(first_cell)
        if found is None:
            problems.append(f"line {number}: the first column names no tool in backticks")
            continue
        name = found.group(1).strip()
        if name in names:
            problems.append(f"line {number}: `{name}` is listed twice (first on line {names[name]})")
            continue
        names[name] = number
    if problems:
        raise CheckError(f"{label} '{HEADING}':\n  " + "\n  ".join(problems))
    if not names:
        raise CheckError(f"{label} '{HEADING}' holds no table row naming a tool")
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", type=Path, default=ROOT / "server.py")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    args = parser.parse_args(argv)
    server_label = os.path.relpath(args.server)
    readme_label = os.path.relpath(args.readme)

    try:
        registered = server_tools(args.server.read_text(encoding="utf-8"), server_label)
        listed = readme_tools(args.readme.read_text(encoding="utf-8"), readme_label)
    except (CheckError, OSError) as exc:
        print(f"readme-tools-check: {exc}", file=sys.stderr)
        return 1

    missing = sorted(set(registered) - set(listed))
    extra = sorted(set(listed) - set(registered))
    if not missing and not extra:
        print(
            f"{readme_label} '{HEADING}' lists the {len(registered)} "
            f"tool{'' if len(registered) == 1 else 's'} {server_label} registers: "
            f"{', '.join(sorted(registered))}"
        )
        return 0

    print(f"{readme_label} '{HEADING}' and {server_label} disagree:", file=sys.stderr)
    for name in missing:
        print(
            f"  `{name}` is registered by {server_label} (line {registered[name]}) "
            f"and missing from {readme_label}",
            file=sys.stderr,
        )
    for name in extra:
        print(
            f"  `{name}` is listed in {readme_label} (line {listed[name]}) "
            f"and not registered by {server_label}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
