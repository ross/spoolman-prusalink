"""The sync-job parser."""

from __future__ import annotations

import pytest

from simulator.gcode import (
    GcodeError,
    Home,
    Ignored,
    Message,
    ModelCheck,
    SetFilament,
    SetUuid,
    parse,
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
        Ignored("M862.5 P2"),
        Ignored('M862.6 P"Input shaper"'),
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
        ("M862.3", "needs P"),
    ],
)
def test_errors_name_the_line(line, reason):
    with pytest.raises(GcodeError, match=rf"^line 2: .*{reason}"):
        parse(f"M117 ok\n{line}\n")


def test_a_parameter_that_does_not_parse_names_the_rest_of_the_line():
    with pytest.raises(GcodeError, match=r"^line 1: .*can't read '=bad'"):
        parse("M865 X =bad\n")
