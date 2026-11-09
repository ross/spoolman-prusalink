"""Derive the table of scenarios in integration/README.md from the tests.

The row comes from each test's name and the description from the first line of
its docstring. Run `python -m integration.table --write` to update the README,
`--check` to compare without writing, or no flag to print the table.
"""

import argparse
import ast
import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
README = HERE / "README.md"
BEGIN = "<!-- table:begin -->"
END = "<!-- table:end -->"
GROUPS = "SPWMXTCB"
ROW_ID = re.compile(r"^([a-z]+)(\d+)([a-z]?)$")


@dataclass
class Test:
    row: str
    name: str
    description: str
    file: str
    function: str


def _sort_key(test: Test) -> tuple:
    m = ROW_ID.match(test.row.lower())
    if not m:
        return (2, "", 0, "", test.name)
    letters, number, suffix = m.groups()
    upper = letters.upper()
    rank = GROUPS.index(upper) if upper in GROUPS else len(GROUPS)
    return (0, f"{rank:02d}{upper}", int(number), suffix, test.name)


def _row_of(function: str) -> str:
    for token in function.removeprefix("test_").split("_"):
        if m := ROW_ID.match(token):
            return m.group(1).upper() + m.group(2) + m.group(3)  # M5a, as scenarios.md has it
    return ""


def tests() -> list[Test]:
    found = []
    for path in sorted(HERE.glob("test_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            doc = ast.get_docstring(node) or ""
            found.append(
                Test(
                    row=_row_of(node.name),
                    name=f"{path.name}::{node.name}",
                    description=doc.strip().split("\n")[0].strip() if doc.strip() else "",
                    file=path.name,
                    function=node.name,
                )
            )
    return sorted(found, key=_sort_key)


def render(tests: list[Test]) -> str:
    lines = ["| Row | Test | What it checks |", "| --- | --- | --- |"]
    for t in tests:
        lines.append(f"| {t.row} | `{t.name}` | {t.description} |")
    return "\n".join(lines)


def _block(text: str) -> tuple[int, int]:
    start = text.find(BEGIN)
    end = text.find(END)
    if start < 0 or end < start:
        raise SystemExit(f"{README}: missing {BEGIN} / {END} markers")
    return start + len(BEGIN), end


def read_block() -> str:
    text = README.read_text()
    a, b = _block(text)
    return text[a:b].strip("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="fail if the README is stale")
    group.add_argument("--write", action="store_true", help="update the README")
    args = parser.parse_args(argv)

    table = render(tests())
    if not (args.check or args.write):
        print(table)
        return 0
    text = README.read_text()
    a, b = _block(text)
    if args.write:
        README.write_text(text[:a] + "\n" + table + "\n" + text[b:])
        return 0
    current = text[a:b].strip("\n")
    if current == table:
        return 0
    diff = difflib.unified_diff(
        current.splitlines(), table.splitlines(), "README.md", "generated", lineterm=""
    )
    print("\n".join(diff))
    print("Run `python -m integration.table --write` to update it.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
