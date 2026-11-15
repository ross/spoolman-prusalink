"""The sync-job parser."""

from __future__ import annotations

import pytest

from simulator.gcode import (
    SCAN_LINES,
    FeatureCheck,
    Home,
    Ignored,
    LevelCheck,
    Message,
    ModelCheck,
    Rejected,
    SetFilament,
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
            load=0,
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
        SetFilament(1, slot=0, uuids={"instance": "44c406a5-c6f5-4440-bfba-e8060564b717"}),
        Message("Spoolman: test"),
    ]


def test_flags_tools_and_copy():
    (c,) = parse('M865 X N"ASA" F1 G L3')
    assert (c.load, c.adhoc, c.reset, c.temps) == (3, True, False, {"filtration": 1, "flexible": 1})
    (c,) = parse("M865 I2 L1")
    assert (c.slot, c.load) == (2, 1)
    (c,) = parse('M865 S"PETG" X0 R1')
    assert (c.preset, c.adhoc, c.reset) == ("PETG", False, True)


def test_quotes_comments_and_blank_lines():
    commands = parse('\n  ; only a comment\nM862.3 P"A;B" ; trailing\n\nM117 hello ; world\n')
    assert commands == [ModelCheck("A;B"), Message("hello")]


def test_a_line_number_and_a_parameter_after_the_number_run():
    """Marlin and `M865`'s parser skip a leading line number, and need no space between the
    command and its first parameter (`parser.cpp:91-155`, `gcode_basic_parser.cpp:29-69`)."""
    assert parse('N12 M865 X R N"PLA" L0\nN-3M117 hi\nM865X L1\nM865.1I0 C""') == [
        SetFilament(adhoc=True, reset=True, name="PLA", load=0),
        Message("hi"),
        SetFilament(adhoc=True, load=1),
        SetFilament(1, slot=0, uuids={"instance": None}),
    ]


def test_unknown_lines_are_ignored():
    # Marlin takes an uppercase command letter only, so `m865` is an unknown command.
    lines = "T1\nM600\nG1 X10 E5\nm865 X L0\nhello"
    assert parse(lines) == [
        Ignored("T1"),
        Ignored("M600"),
        Ignored("G1 X10 E5"),
        Ignored("m865 X L0"),
        Ignored("hello"),
    ]


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ('M865 X N"PLA L0', "unterminated quotes"),
        ('M865 X N"PL;A" L0', "unterminated quotes"),  # the body ends at the `;`
        ('M865 X N"PLA\\', "unterminated quotes"),  # an escape with nothing after it
        ('M865 x n"PLA" L0', "expected an uppercase parameter letter at 'x"),
        ('M865 X N"PLA" Tabc L0', "expected an uppercase parameter letter at 'abc"),
        ("M865 X =bad", "expected an uppercase parameter letter at '=bad'"),
        ("M865 X L0 L1", "L given twice"),
    ],
)
def test_a_line_the_firmware_cannot_parse_is_rejected(line, reason):
    """`GCodeParser2::parse` fails, so `M865` returns before reading anything."""
    (ok, rejected) = parse(f"M117 ok\n{line}\n")
    assert ok == Message("ok") and isinstance(rejected, Rejected)
    assert rejected.reason.startswith(reason)


@pytest.mark.parametrize(
    ("line", "read"),
    [
        # The name buffer holds 7 bytes; a longer name doesn't read, the rest does.
        ('M865 X R N"WAYTOOLONG" T230 L0', {"name": None, "temps": {"nozzle": 230}}),
        ('M865 X N"P\u00c9TGXYZ" L0', {"name": None}),  # 7 characters, 8 bytes
        ('M865 X N"P L A" L0', {"name": "P L A"}),  # checked when the line runs
        ('M865 X N"" L0', {"name": ""}),
        ('M865 X N"A\\"B" L0', {"name": 'A"B'}),
        ('M865 X N"PLA" O"red" L0', {"color": None}),  # color names aren't modeled
        ('M865 X N"PLA" O"#abc" L0', {"color": "#000abc"}),
        ('M865 X N"PLA" O"#1122334455" L0', {"color": None}),
        ('M865 X T"abc" B"" L0', {"temps": {}}),
        ("M865 X T215.5 B-5 P40000 L0", {"temps": {"nozzle": 215, "bed": -5}}),
        ("M865 X C300 D20 F2 G0 L0", {"temps": {"chamber_min": 20, "flexible": 0}}),
        ("M865 X I9 L300", {"slot": 9, "load": None}),
        ("M865 X Q5 K1 L0", {"load": 0}),  # an unknown parameter is never read
        ('M865 S"" L0', {"preset": None}),
        ('M865.1 I0 C"not-a-uuid"', {"uuids": {}}),
        ('M865.1 I0 C""', {"uuids": {"instance": None}}),
        ('M865.1 I0 C"00000000-0000-0000-0000-000000000000"', {"uuids": {"instance": None}}),
        (
            'M865.1 I0 A"8B6A3E3E3F6C4D7B9A436C1F0E7D2A11"',
            {"uuids": {"material": "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"}},
        ),
        ('M865.1 I0 N"PLA" T230', {"name": None, "temps": {}}),  # M865 parameters only
    ],
)
def test_a_value_that_does_not_read_is_left_out(line, read):
    """The firmware's parser has no error callback, so a value that doesn't read is
    skipped without a word, and the rest of the line still runs."""
    (c,) = parse(line)
    assert isinstance(c, SetFilament)
    assert {key: getattr(c, key) for key in read} == read


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
        # The firmware pops one letter, reads its value if it knows the letter, then skips
        # to the next space, so a `P` glued after another letter is never read
        # (`GCodeInfo::parse_m862` in `src/common/gcode/gcode_info.cpp`).
        ('M862.3 XP"MINI"', Ignored('M862.3 XP"MINI"')),
        ('M862.3 T0P"MINI"', Ignored('M862.3 T0P"MINI"')),
        ("M862.5 XP3", Ignored("M862.5 XP3")),
        ('M862.3 X P"MINI"', ModelCheck("MINI")),
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
