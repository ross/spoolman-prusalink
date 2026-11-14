"""The sync-job parser."""

from __future__ import annotations

import pytest

from simulator.gcode import (
    SCAN_LINES,
    FeatureCheck,
    GcodeError,
    Home,
    Ignored,
    LevelCheck,
    Message,
    ModelCheck,
    SetFilament,
    SetUuid,
    parse,
    scan,
)

BRIDGE_JOB = """M862.3 P "COREONEL" ; printer model check
M862.5 P2 ; g-code level check
M862.6 P"Input shaper" ; FW feature check
G28 O ; home only if not homed, so the end-of-job bed move is safe
M865 X R N"PETG" T230 B85 P170 C30 D15 E45 O"#186BB5" L0
M865.1 I0 C"44C406A5-C6F5-4440-BFBA-E8060564B717"
M117 Spoolman: test
"""


def test_bridge_job():
    commands = parse(BRIDGE_JOB)
    assert commands == [
        ModelCheck("COREONEL"),
        LevelCheck(2),
        FeatureCheck("Input shaper"),
        Home(),
        SetFilament(
            tool=0,
            reset=True,
            adhoc=True,
            name="PETG",
            color="#186bb5",
            temps={
                "nozzle": 230,
                "bed": 85,
                "preheat": 170,
                "chamber_target": 30,
                "chamber_min": 15,
                "chamber_max": 45,
            },
        ),
        SetUuid(0, "44c406a5-c6f5-4440-bfba-e8060564b717"),
        Message("Spoolman: test"),
    ]


def test_flags_tools_and_copy():
    (c,) = parse('m865 x n"ASA" F1 G1 L3')
    assert (c.tool, c.adhoc, c.reset, c.temps) == (3, True, False, {"filtration": 1, "flexible": 1})
    (c,) = parse("M865 I2 L1")
    assert (c.tool, c.copy_from) == (1, 2)
    (c,) = parse('M865 I2 X N"PLA"')
    assert (c.tool, c.copy_from) == (2, None)


def test_quotes_comments_and_blank_lines():
    commands = parse('\n  ; only a comment\nM862.3 P"A;B" ; trailing\n\nM117 hello ; world\n')
    assert commands == [ModelCheck("A;B"), Message("hello")]


def test_unknown_lines_are_ignored():
    assert parse("T1\nM600\nG1 X10 E5") == [Ignored("T1"), Ignored("M600"), Ignored("G1 X10 E5")]


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ('M865 X N"TOOLONGX" L0', "filament name"),
        ('M865 X N"P L A" L0', "filament name"),
        ('M865 X N"PLA" O"red" L0', "color"),
        ('M865 X N"PLA" Tabc L0', "T must be a number"),
        ('M865.1 I0 C"not-a-uuid"', "isn't a UUID"),
        ("M865.1 I0", "needs C"),
        ('M865 X N"PLA L0', "unterminated"),
    ],
)
def test_errors_name_the_line(line, reason):
    with pytest.raises(GcodeError, match=rf"^line 2: .*{reason}"):
        parse(f"M117 ok\n{line}\n")


def test_a_parameter_that_does_not_parse_names_the_rest_of_the_line():
    with pytest.raises(GcodeError, match=r"^line 1: .*can't read '=bad'"):
        parse("M865 X =bad\n")


@pytest.mark.parametrize(
    ("line", "check"),
    [
        ('M862.3 P "MK4S"', ModelCheck("MK4S")),
        ("M862.3 PMK4S", ModelCheck("")),  # not quoted: no name
        ("M862.3", Ignored("M862.3")),  # no P: nothing checked
        ("M862.3 Q", Ignored("M862.3 Q")),  # the query form
        ('M862.3 P"MK4', ModelCheck("")),  # an unterminated quote
        ("M862.5 P3", LevelCheck(3)),
        ("M862.5 Px", LevelCheck(0)),
        ("M862.5", Ignored("M862.5")),
        ('M862.6 P" MMU3  "', FeatureCheck("MMU3")),
        ("M862.6 P MMU3", FeatureCheck("")),
        ("M862.6 Q", Ignored("M862.6 Q")),
        ("M862.1 P0.4", Ignored("M862.1 P0.4")),
    ],
)
def test_checks_never_fail_to_parse(line, check):
    assert parse(line) == [check]


def test_the_scan_reads_checks_from_the_first_200_gcode_lines():
    assert SCAN_LINES == 200  # `search_first_x_gcodes`; the files below are built from it
    head = "\n; a comment\n".join(["G1 X1"] * (SCAN_LINES - 1))
    # M486, blank lines and comments don't count; a line number is skipped.
    text = head + '\nM486 S1\n\n  ; more\nN12 M862.5 P3\nM862.3 P"MINI"\n'
    assert scan(text) == [LevelCheck(3)]


def test_the_scan_ignores_the_rest_of_the_line_and_the_file():
    text = 'm862.3 P"MINI"\nM862.6 P"Input shaper" ; FW\nM865 X N"WAYTOOLONGX"\n'
    assert scan(text) == [FeatureCheck("Input shaper")]  # the firmware matches M862 exactly


def test_the_scan_skips_a_check_with_no_p():
    assert scan('M862.3 Q\nM862.6 Q\nM862.5\nM862.3 P"MINI"\n') == [ModelCheck("MINI")]
