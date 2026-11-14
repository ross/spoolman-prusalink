"""The print preview's checks of a file's `M862` lines, without a printer."""

from __future__ import annotations

import pytest

from simulator.gcode import SCAN_LINES
from simulator.model import MODELS
from simulator.preview import (
    Failed,
    PrintChecks,
    Severity,
    ToolView,
    preview,
    print_checks,
    tool_mapping,
    wrong_types,
)
from spoolman_prusalink.printer_sync import PRINTER_MODELS, build_job, default_preamble

CORE_ONE_L = MODELS["8.1.0"]
OWN = 'M862.3 P "COREONEL"\nM862.5 P2\nM862.6 P"Input shaper"\n'


def rows(text: str, severity: Severity = Severity.WARN, code: str = "8.1.0") -> list[Failed]:
    return preview(MODELS[code], text, severity)


def texts(text: str, severity: Severity = Severity.WARN, code: str = "8.1.0") -> list[str]:
    return [r.text for r in rows(text, severity, code)]


def test_a_file_sliced_for_the_printer_asks_nothing():
    assert rows(OWN) == []


@pytest.mark.parametrize(
    ("text", "row"),
    [
        (OWN.replace("COREONEL", "MINI"), "for printer MINI"),
        (OWN.replace('"COREONEL"', "COREONEL"), "M862.3 names no printer"),
        (OWN.replace("COREONEL", "MK4S"), "compatibility mode for MK4S"),
        (OWN.replace("P2", "P3"), "G-code level 3, above 2"),
        (OWN + 'M862.6 P"Tea"\n', "unsupported features: 'Tea'"),
        (OWN + "M862.6 P Tea\n", "unsupported features: ''"),  # not quoted
        (OWN + 'M862.6 P"MMU3"\n', "sliced for an MMU3, which is off"),
        ('M862.3 P "COREONEL"\n', "not sliced with input shaper"),
    ],
)
def test_each_check(text, row):
    assert rows(text) == [Failed(row, False)]
    assert rows(text, Severity.ABORT) == [Failed(row, True)]
    assert rows(text, Severity.IGNORE) == []


def test_no_model_check_without_m862_3():
    assert rows('M862.6 P"Input shaper"\n') == []


def test_each_model_check_line_counts():
    text = OWN + 'M862.3 P "MINI"\nM862.3 P "XL"\n'
    assert texts(text) == ["for printer MINI"]


def test_feature_names_ignore_case_and_spaces():
    assert rows(OWN.replace("Input shaper", " INPUT SHAPER  ")) == []
    assert rows(OWN + 'M862.6 P"ht hotend"\n') == []  # taken as satisfied


def test_the_mmu3_feature_on_a_build_without_an_mmu_is_unsupported():
    text = 'M862.3 P "iX"\nM862.6 P"MMU3"\n'
    assert texts(text, code="4.1.0") == ["unsupported features: 'MMU3'"]


def test_the_ix_needs_no_input_shaper():
    assert rows('M862.3 P "iX"\n', code="4.1.0") == []


def test_several_failures_share_one_screen_and_the_worst_decides():
    text = 'M862.3 P "MINI"\nM862.5 P9\n'
    want = ["for printer MINI", "G-code level 9, above 2", "not sliced with input shaper"]
    assert texts(text) == want
    assert all(r.fatal for r in rows(text, Severity.ABORT))


INDX_OWN = 'M862.3 P "COREONEL-INDX"\nM862.6 P"Input shaper"\n'


@pytest.mark.parametrize("severity", list(Severity))
def test_the_indx_lock_is_a_warning_whatever_the_setting(severity):
    assert rows(INDX_OWN, severity, "8.10.0") == [Failed("not sliced for the INDX lock", False)]
    assert rows(INDX_OWN + 'M862.6 P"indx LOCK"\n', severity, "8.10.0") == []


def test_ignored_checks_are_rows_when_the_indx_lock_brings_up_the_screen():
    text = INDX_OWN.replace("COREONEL-INDX", "MINI")
    assert rows(text, Severity.IGNORE, "8.10.0") == [
        Failed("for printer MINI", False),
        Failed("not sliced for the INDX lock", False),
    ]
    assert rows(text, Severity.ABORT, "8.10.0")[0] == Failed("for printer MINI", True)


def test_a_check_past_the_scan_window_is_not_seen():
    text = OWN + "G1 X1\n" * (SCAN_LINES - 3) + 'M862.3 P "MINI"\n'
    assert rows(text) == []
    text = OWN + "G1 X1\n" * (SCAN_LINES - 4) + 'M862.3 P "MINI"\n'
    assert texts(text) == ["for printer MINI"]


def test_a_query_line_asks_nothing():
    preamble = default_preamble(PRINTER_MODELS["8.1.0"])
    assert rows(build_job([*preamble, "M862.3 Q", "M862.6 Q"], [])) == []


def test_rows_come_in_the_report_order():
    # `GeneralCheck` order: the model, compatibility mode, the level, input shaper, the INDX
    # lock, the MMU, then unsupported features.
    text = 'M862.3 P "MINI"\nM862.5 P3\nM862.6 P"Tea"\nM862.6 P"MMU3"\n'
    assert texts(text) == [
        "for printer MINI",
        "G-code level 3, above 2",
        "not sliced with input shaper",
        "sliced for an MMU3, which is off",
        "unsupported features: 'Tea'",
    ]
    text = 'M862.3 P "MK4S"\nM862.6 P"Tea"\n'
    assert texts(text, code="8.10.0") == [
        "for printer MK4S",
        "not sliced with input shaper",
        "not sliced for the INDX lock",
        "unsupported features: 'Tea'",
    ]
    assert texts('M862.3 P "COREONEL"\nM862.6 P"Tea"\n') == [
        "not sliced with input shaper",
        "unsupported features: 'Tea'",
    ]


# A print's checks.


def view(loaded=True, material="PLA", enabled=True) -> ToolView:
    return ToolView(enabled, loaded, material if loaded else None)


def test_a_print_on_a_loaded_tool_of_its_type_asks_nothing():
    assert print_checks(1, {0: "PLA"}, {0: view()}) == PrintChecks([], [], [])
    assert wrong_types({0: "PLA"}, {0: view()}) == []


def test_an_empty_used_tool_warns_then_waits_for_a_load():
    checks = print_checks(1, {0: "PLA"}, {0: view(loaded=False)})
    assert checks == PrintChecks([Failed("T0 has no filament", False)], [], [0])


def test_another_type_is_a_question_of_its_own():
    assert print_checks(1, {0: "PETG"}, {0: view()}) == PrintChecks([], [], [])
    assert wrong_types({0: "PETG"}, {0: view()}) == [Failed("T0 holds PLA, not PETG", False)]


def test_the_type_is_compared_exactly():
    assert wrong_types({0: "pla"}, {0: view()}) == [Failed("T0 holds PLA, not pla", False)]
    assert wrong_types({0: "PLA "}, {0: view()}) == [Failed("T0 holds PLA, not PLA ", False)]
    # A tool with no type reports ---, which no named type matches.
    assert wrong_types({0: "PLA"}, {0: view(material=None)}) == [
        Failed("T0 holds ---, not PLA", False)
    ]


def test_a_file_that_names_no_type_skips_the_type_check():
    for want in (None, "---"):
        assert wrong_types({0: want}, {0: view(material="PETG")}) == []
        assert print_checks(5, {0: want, 1: "PLA"}, {0: view(), 1: view()}).mapping == []


def test_only_used_tools_are_checked():
    tools = {0: view(), 1: view(loaded=False), 2: view(enabled=False)}
    assert print_checks(1, {0: "PLA"}, tools) == PrintChecks([], [], [])
    assert wrong_types({0: "PLA"}, {**tools, 1: view(material="ABS")}) == []


def test_a_disabled_used_tool_is_fatal_and_checked_no_further():
    tools = {0: view(enabled=False, loaded=False)}
    found = print_checks(1, {0: "PETG"}, tools)
    assert found.rows == [
        Failed("the print uses more tools than are enabled", False),
        Failed("T0 is disabled", True),
    ]
    assert found.empty == [] and wrong_types({0: "PETG"}, tools) == []


def test_more_used_tools_than_enabled_ones_warns():
    tools = {0: view(), 1: view(enabled=False), 2: view(enabled=False)}
    found = print_checks(5, {0: None, 1: None}, tools)
    assert found.rows == [Failed("the print uses more tools than are enabled", False)]
    assert found.mapping == [Failed("T1 is disabled", True)]


def test_a_toolchanger_asks_about_its_tools_on_the_mapping_screen():
    tools = {0: view(loaded=False), 1: view(material="PETG"), 2: view(enabled=False)}
    found = print_checks(5, {0: "PLA", 1: "PLA", 2: "PLA"}, tools)
    # Tool by tool, in the order of `VirtualToolCheck`: the tool, its filament, its type.
    assert found == PrintChecks(
        [Failed("the print uses more tools than are enabled", False)],
        [
            Failed("T0 has no filament", False),
            Failed("T0 holds ---, not PLA", False),
            Failed("T1 holds PETG, not PLA", False),
            Failed("T2 is disabled", True),
        ],
        [],
        mapped=True,
    )


def test_tool_mapping_needs_two_used_or_two_enabled_tools():
    one = {0: view()}
    two = {0: view(), 1: view()}
    assert not tool_mapping(1, {0: None}, one)  # no toolchanger
    assert not tool_mapping(5, {0: None}, one)  # one tool enabled, one used
    assert tool_mapping(5, {0: None}, two)
    assert tool_mapping(5, {0: None, 1: None}, {0: view(), 1: view(enabled=False)})
    assert not tool_mapping(5, {}, two)
    # With nothing wrong, the screen still shows: it has no rows.
    assert print_checks(5, {0: None}, two) == PrintChecks([], [], [], mapped=True)
    # Without tool mapping, an XL with one tool enabled checks as a single-tool printer does.
    found = print_checks(5, {1: "PLA"}, {0: view(enabled=False), 1: view(loaded=False)})
    assert found == PrintChecks([Failed("T1 has no filament", False)], [], [1])


def test_no_setting_governs_a_print_s_checks():
    # `checks` is for a file's `M862` lines; a print's rows have fixed severities.
    tools = {0: view(loaded=False), 1: view(enabled=False)}
    found = print_checks(5, {0: "PLA", 1: "PLA"}, tools)
    assert [r.fatal for r in found.mapping] == [False, False, True]
