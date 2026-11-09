"""Keep integration/README.md and docs/scenarios.md in step with the tests."""

import re
from pathlib import Path

from integration import table

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = (ROOT / "docs" / "scenarios.md").read_text()
FIX = "Run `python -m integration.table --write`."


def _status(row: str) -> str | None:
    for line in SCENARIOS.splitlines():
        if line.startswith(f"| {row} |"):
            return line
    return None


def test_readme_table_is_current():
    assert table.read_block() == table.render(table.tests()), (
        f"integration/README.md table is stale. {FIX}"
    )


def test_every_test_has_row_and_description():
    for t in table.tests():
        assert t.row, f"{t.name} has no row id (name it test_<row>_<short name>)"
        assert t.description, f"{t.name} has no docstring saying what it checks"


def test_every_test_row_is_end_to_end_in_scenarios():
    for t in table.tests():
        if not t.row:
            continue
        line = _status(t.row)
        assert line is not None, f"{t.name}: row {t.row} is not in docs/scenarios.md"
        marker = f"end to end (`{t.function}`)"
        assert marker in line, f"row {t.row}: Status should contain {marker}"


def test_scenarios_mentions_name_existing_tests():
    names = {t.function for t in table.tests()}
    for m in re.finditer(r"end to end \(`(test_\w+)`\)", SCENARIOS):
        assert m.group(1) in names, f"docs/scenarios.md names {m.group(1)}, which doesn't exist"
