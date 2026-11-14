"""The print preview's checks of a file's `M862` lines, without a printer."""

from __future__ import annotations

import pytest

from simulator.gcode import SCAN_LINES
from simulator.model import MODELS
from simulator.preview import Failed, Severity, preview

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
