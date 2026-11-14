"""The watched regions: finding them, their fingerprints and how a change is reported."""

import re

import pytest

from firmware import compare, watch
from firmware.source import Unreadable
from firmware.tests import fixtures


def watched(kind, anchor, fingerprint="0" * 16):
    return watch.Watched("the region", "a/file", kind, anchor, "X in y.py", fingerprint, "c" * 40)


def region(text, kind, anchor):
    return watch.region(text, watched(kind, anchor))


@pytest.mark.parametrize("row", watch.WATCHED, ids=lambda w: w.name)
def test_every_watched_region_is_in_the_fixtures(row):
    found = watch.region(fixtures.FILES[row.path], row)
    assert watch.fingerprint(found) == row.fingerprint


@pytest.mark.parametrize("row", watch.WATCHED, ids=lambda w: w.name)
def test_every_watched_row_records_a_full_commit(row):
    # The report knows a wrong fingerprint by the recorded commit being the checked one,
    # whose full hash it compares.
    assert re.fullmatch("[0-9a-f]{40}", row.commit)


def test_the_fixtures_have_no_changes():
    assert watch.changes(fixtures.FakeSource()) == []


def test_an_unchanged_region_at_a_newer_commit_is_ok():
    # The recorded commit is where the region was last read, so a newer commit with the same
    # text needs no update.
    newer = "f" * 40
    assert all(w.commit != newer for w in watch.WATCHED)
    assert watch.changes(fixtures.FakeSource(commit=newer)) == []


def test_a_function_runs_from_its_line_to_its_closing_brace():
    found = region(fixtures.PRINTER_MODEL_CPP, "function", "gcode_compatibility_report_constexpr")
    assert found.startswith("constexpr PrinterGCodeCompatibilityReport gcode_compat")
    assert found.endswith("    return result;\n}")
    # The comments are gone.
    assert "Recursively" not in found
    assert "upgrade_from(PrinterModel::xl);" in found


def test_a_call_isnt_a_definition():
    # The fixture also calls the function, inside a lambda, a return and a static_assert.
    anchor = "gcode_compatibility_report_constexpr"
    assert fixtures.PRINTER_MODEL_CPP.count(f"{anchor}(") == 4
    found = region(fixtures.PRINTER_MODEL_CPP, "function", anchor)
    assert found.startswith("constexpr PrinterGCodeCompatibilityReport " + anchor)
    text = "void f() {\n    if (g(x)) {\n        h();\n    }\n}\nvoid g(int x) {}\n"
    assert region(text, "function", "g") == "void g(int x) {}"


def test_a_qualified_name_isnt_another_function():
    text = "void A::run() { x(); }\nvoid run() { y(); }\n"
    assert region(text, "function", "run") == "void run() { y(); }"
    assert region(text, "function", "A::run") == "void A::run() { x(); }"


@pytest.mark.parametrize(
    "text, problem",
    [
        ("void g() {}\n", "0 definitions of f"),
        ("// void f() {}\n", "0 definitions of f"),
        ("void f();\n", "0 definitions of f"),
        ("void f() {}\nint f(int a) const {}\n", "2 definitions of f"),
        ("void f() {\n    if (x) {\n}\n", "unbalanced {}"),
        ("void f(int a {}\n", "unbalanced \\(\\)"),
    ],
)
def test_a_function_found_other_than_once_fails(text, problem):
    with pytest.raises(Unreadable, match=rf"can't read a/file: {problem}, for the region"):
        region(text, "function", "f")


def test_braces_in_literals_dont_count():
    # A digit separator isn't a literal's start, so the `'` after it opens the next one.
    text = "void f() {\n    puts(\"}\");\n    int n = 8'000; char c = '}';\n}\nint g;\n"
    assert region(text, "function", "f").endswith("int n = 8'000; char c = '}';\n}")


def test_a_statement_runs_to_its_semicolon():
    found = region(fixtures.POWER_PANIC_CPP, "statement", "constexpr bool power_panic_retracts")
    # The comment is gone.
    assert " ".join(found.split()) == "constexpr bool power_panic_retracts = !HAS_DWARF();"
    # Brackets and literals hold no end.
    text = '    int a = f(x; y)[g(";")] + 1; int b;\n'
    assert region(text, "statement", "int a") == 'int a = f(x; y)[g(";")] + 1;'


def test_a_statement_with_a_block_runs_to_its_closing_brace():
    found = region(fixtures.POWER_PANIC_CPP, "statement", "if (power_panic_retracts &&")
    assert found.startswith("if (power_panic_retracts && PhysicalToolIndex::")
    assert found.endswith("planner.start_moving();\n        }")
    text = "if (a) {\n    if (b) { x(); }\n    y();\n} else {\n    z();\n}\n"
    assert region(text, "statement", "if (a)") == "if (a) {\n    if (b) { x(); }\n    y();\n}"


@pytest.mark.parametrize(
    "text, problem",
    [
        ("int b = 1;\n", "0 lines start int a"),
        ("// int a = 1;\n", "0 lines start int a"),
        ("int ab = 1;\n", "0 lines start int a"),
        ("x = 1; int a = 1;\n", "0 lines start int a"),
        ("int a = 1;\nint a = 2;\n", "2 lines start int a"),
        ("int a = f(1\n", "int a isn't closed"),
        ("int a { 1;\n", "unbalanced {}"),
    ],
)
def test_a_statement_found_other_than_once_fails(text, problem):
    with pytest.raises(Unreadable, match=rf"{problem}, for the region"):
        region(text, "statement", "int a")


def test_a_branch_runs_to_the_next_directive_of_its_conditional():
    found = region(fixtures.BOARD_H, "directive", "#elif BOARD() == BOARD_XL_DEV_KIT_XLB()")
    assert found.splitlines() == [
        "#elif BOARD() == BOARD_XL_DEV_KIT_XLB()",
        "    #define BOARD_IS_XLBUDDY()        1 ",
        "    #define BOARD_IS_XL_DEV_KIT_XLB() 1",
    ]
    text = "#if A\n#  if B\nx\n#  else\ny\n#  endif\nz\n#elif C\n#endif\n"
    assert region(text, "directive", "#if A") == "#if A\n#  if B\nx\n#  else\ny\n#  endif\nz"
    assert region(text, "directive", "#else") == "#  else\ny"


def test_another_directive_is_its_own_line():
    text = "#define A() 1 // one\n#define AB() 2\n"
    assert region(text, "directive", "#define A()") == "#define A() 1 "
    # The anchor ends at a word's end.
    assert region(text, "directive", "#define A") == "#define A() 1 "


@pytest.mark.parametrize(
    "text, problem",
    [
        ("#if A\n#endif\n", "0 lines start #if B"),
        ("// #if B\n", "0 lines start #if B"),
        ("#if BC\n#endif\n", "0 lines start #if B"),
        ("#if B\n#endif\n#if B\n#endif\n", "2 lines start #if B"),
        ("#if B\nx\n", "#if B isn't closed"),
        ("#if B\n#if C\n#endif\n", "#if B isn't closed"),
    ],
)
def test_a_directive_found_other_than_once_fails(text, problem):
    with pytest.raises(Unreadable, match=rf"{problem}, for the region"):
        region(text, "directive", "#if B")


def test_a_cmake_command_is_its_own_lines():
    found = region(fixtures.OPTIONS_CMAKE, "cmake", "set(BUDDY_BOARDS")
    assert found == 'set(BUDDY_BOARDS "BUDDY" "XBUDDY" "XLBUDDY")'
    text = 'set(A\n    "x" # one\n    )\nset(AB "y")\n'
    assert region(text, "cmake", "set(A") == 'set(A\n    "x" \n    )'


def test_a_cmake_if_runs_to_its_endif():
    found = region(fixtures.OPTIONS_CMAKE, "cmake", "if(BOARD IN_LIST BUDDY_BOARDS OR")
    assert found.splitlines() == [
        'if(BOARD IN_LIST BUDDY_BOARDS OR BOARD STREQUAL "XL_DEV_KIT_XLB")',
        "  set(BOARD_IS_MASTER_BOARD true)",
        "else()",
        "  set(BOARD_IS_MASTER_BOARD false)",
        "endif()",
    ]
    text = "if(A)\n  if(B)\n  endif()\n  set(C 1)\nendif()\nset(D 1)\n"
    assert region(text, "cmake", "if(A") == "if(A)\n  if(B)\n  endif()\n  set(C 1)\nendif()"


@pytest.mark.parametrize(
    "text, problem",
    [
        ("set(B 1)\n", "0 commands start if\\(A"),
        ("# if(A)\n", "0 commands start if\\(A"),
        ("if(A)\nendif()\nif(A)\nendif()\n", "2 commands start if\\(A"),
        ("if(A)\n  set(B 1)\n", "if\\(A isn't closed"),
        ("if(A\n", "if on line 1 isn't closed"),
    ],
)
def test_a_cmake_command_found_other_than_once_fails(text, problem):
    with pytest.raises(Unreadable, match=rf"{problem}, for the region"):
        region(text, "cmake", "if(A")


def test_only_code_changes_the_fingerprint():
    text = fixtures.PAUSE_SETTINGS_CPP
    before = watch.fingerprint(region(text, "function", "pause::Settings::purge_length"))
    for same in (
        text.replace("        // Double the purge length for HF nozzles\n", ""),
        text.replace("    float r;", "    float r; /* the length */"),
        text.replace("    float r;\n", "  float   r;\n\n\n"),
    ):
        assert watch.fingerprint(region(same, "function", "pause::Settings::purge_length")) == (
            before
        )
    changed = text.replace("? 2 : 1", "? 3 : 1")
    assert watch.fingerprint(region(changed, "function", "pause::Settings::purge_length")) != (
        before
    )


def changed(commit):
    row = watch.WATCHED[0]
    files = {**fixtures.FILES, row.path: fixtures.PRINTER_MODEL_CPP.replace("xl", "XL")}
    [change] = watch.changes(fixtures.FakeSource(files, commit), [row])
    assert change.watched == row
    assert change.now != row.fingerprint
    return row, change


def test_a_changed_region_is_reported_with_the_diff_to_read():
    newer = "abcdef0123456789" * 2 + "abcdef01"
    row, change = changed(newer)
    assert row.commit == "7d8bf6858b363c453c0617bceab5e66997d8fc25"
    assert change.commit == newer
    assert str(change) == (
        "the compatibility upgrades in src/common/printer_model.cpp changed since 7d8bf6858b36:"
        " check UPGRADES in simulator/compatibility.py against the diff, then record"
        f" fingerprint {change.now} and commit {newer}\n"
        "    in a firmware checkout, after git fetch if it lacks abcdef012345:"
        " git diff 7d8bf6858b36..abcdef012345 -- src/common/printer_model.cpp"
    )
    lines, ok = compare.report([], (), [change])
    assert not ok
    assert lines[-3:] == [
        "watched source:",
        f"  {change}",
        "0 unexpected differences, 0 expected, 0 stale expected entries, 1 changed watched regions",
    ]
    lines, ok = compare.report([], (), [])
    assert ok
    assert lines[-2] == "watched source: ok"


def test_a_changed_region_at_its_recorded_commit_is_a_wrong_fingerprint():
    row, change = changed(watch.WATCHED[0].commit)
    assert str(change) == (
        f"the compatibility upgrades in src/common/printer_model.cpp has fingerprint"
        f" {change.now} at 7d8bf6858b36, its recorded commit, not {row.fingerprint}: the"
        " recorded fingerprint is wrong; check UPGRADES in simulator/compatibility.py"
    )
