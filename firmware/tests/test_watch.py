"""The watched regions: finding them, their fingerprints and how a change is reported."""

import pytest

from firmware import compare, watch
from firmware.source import Unreadable
from firmware.tests import fixtures


def watched(kind, anchor, fingerprint="0" * 16):
    return watch.Watched("the region", "a/file", kind, anchor, "X in y.py", fingerprint)


def region(text, kind, anchor):
    return watch.region(text, watched(kind, anchor))


@pytest.mark.parametrize("row", watch.WATCHED, ids=lambda w: w.name)
def test_every_watched_region_is_in_the_fixtures(row):
    found = watch.region(fixtures.FILES[row.path], row)
    assert watch.fingerprint(found) == row.fingerprint


def test_the_fixtures_have_no_changes():
    assert watch.changes(fixtures.FakeSource()) == []


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


def test_a_changed_region_is_reported():
    row = watch.WATCHED[0]
    files = {**fixtures.FILES, row.path: fixtures.PRINTER_MODEL_CPP.replace("xl", "XL")}
    [change] = watch.changes(fixtures.FakeSource(files), [row])
    assert change.watched == row
    assert change.now != row.fingerprint
    assert str(change) == (
        f"the compatibility upgrades in src/common/printer_model.cpp changed (expected"
        f" {row.fingerprint}, now {change.now}): re-read it and check UPGRADES in"
        " simulator/compatibility.py, then record the new fingerprint"
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
