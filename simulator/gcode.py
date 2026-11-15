"""Parse the sync job the bridge uploads.

Only the commands the bridge writes are understood. Everything else parses as `Ignored`, so
the page shows a new command the bridge starts sending instead of hiding it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,7}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# A command as the print preview's scan matches it: the word, then a space or the end.
_WORD = re.compile(r"^([GMT]\d+(?:\.\d+)?)(?=\s|$)")
# A command as a running job reads it. Marlin skips a leading line number and its spaces,
# then takes an uppercase G, M or T and its number, with no space needed before the first
# parameter (`GCodeParser::parse` in `lib/Marlin/Marlin/src/gcode/parser.cpp:91-155`).
# `M865`'s own parser does the same (`GCodeBasicParser::parse`,
# `gcode_basic_parser.cpp:29-69`). Any other line is an unknown command.
_RUN_LINE_NUMBER = re.compile(r"^N-?\d+ *")
_COMMAND = re.compile(r"^([GMT]\d+(?:\.\d+)?)")

# The M865 parameters kept, by letter, with the range `store_option_if_present` reads them
# in: the type of their `FilamentTypeParameters` field. A value outside it is skipped.
_INT16 = (-(2**15), 2**15 - 1)
_UINT8 = (0, 255)
_TEMPS = {
    "T": ("nozzle", _INT16),
    "B": ("bed", _INT16),
    "P": ("preheat", _INT16),
    "C": ("chamber_target", _UINT8),
    "D": ("chamber_min", _UINT8),
    "E": ("chamber_max", _UINT8),
}
_FLAGS = {"F": "filtration", "G": "flexible"}
_UUIDS = {"A": "material", "B": "package", "C": "instance"}
# `name_buf` in `M865.cpp`: `filament_name_buffer_size - 1` bytes.
NAME_BUFFER = 7
_UUID_BUFFER = 36  # `UUID::string_length`
_INTEGER = re.compile(r"\s*[+-]?\d+")


@dataclass
class ModelCheck:
    """`M862.3 P"<name>"`. The name is empty when `P` isn't a quoted string."""

    name: str


@dataclass
class LevelCheck:
    """`M862.5 P<level>`, the G-code level the file needs."""

    level: int


@dataclass
class FeatureCheck:
    """`M862.6 P"<name>"`, a firmware feature the file was sliced for. The name is empty
    when `P` isn't a quoted string."""

    name: str


@dataclass
class Home:
    pass


@dataclass
class SetFilament:
    """An `M865` or `M865.1` line, as `PrusaGcodeSuite::M865` reads it.

    A value that doesn't read is left out, as the firmware skips it: its parser has no error
    callback, so the failure is silent (`M865.cpp:57`, `gcode_parser_helper.cpp:21-24`).
    `slot` and `load` are checked against the build's tools when the line runs.
    """

    subcode: int = 0  # 1 for `M865.1`
    slot: int | None = None  # I: the type in this tool's slot
    adhoc: bool = False  # X: the pending ad-hoc type
    preset: str | None = None  # S: a type by name
    reset: bool = False
    name: str | None = None  # N, left out when it doesn't fit the 7-byte buffer
    color: str | None = None
    temps: dict[str, int] = field(default_factory=dict)
    uuids: dict[str, str | None] = field(default_factory=dict)  # M865.1; None clears one
    load: int | None = None  # L: the tool to load the type into


@dataclass
class Rejected:
    """An `M865` line the firmware's parser rejects as a whole, so it does nothing."""

    line: str
    reason: str


@dataclass
class Message:
    text: str


@dataclass
class Ignored:
    line: str


Check = ModelCheck | LevelCheck | FeatureCheck
Command = ModelCheck | LevelCheck | FeatureCheck | Home | SetFilament | Rejected | Message | Ignored

# The print preview reads a file's checks from its first 200 G-code lines
# (`search_first_x_gcodes` in `src/common/gcode/gcode_info.hpp`).
SCAN_LINES = 200
_LINE_NUMBER = re.compile(r"^N\d[\d\s]*")
_P = re.compile(r"(?:^|\s)P")
_DIGITS = re.compile(r"^\s*(\d*)")


def _strip_comment(line: str) -> str:
    """Cut the line at the first `;` outside quotes."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            return line[:i]
    return line


def _params(body: str) -> dict[str, str]:
    """Each parameter's value, as `GCodeParser2::parse` and `parse_option_value` split the
    body (`gcode_parser.cpp:97-213`): an uppercase letter, then a quoted string with `\\`
    escapes, or the run of `0-9+-.,` that follows. Raise ValueError where the firmware's
    parse fails, which fails the whole line."""
    params: dict[str, str] = {}
    i, end = 0, len(body)
    while True:
        while i < end and body[i].isspace():
            i += 1
        if i == end:
            return params
        letter = body[i]
        if not "A" <= letter <= "Z":
            raise ValueError(f"expected an uppercase parameter letter at {body[i:]!r}")
        if letter in params:
            raise ValueError(f"{letter} given twice")
        i += 1
        while i < end and body[i].isspace():
            i += 1
        if i < end and body[i] == '"':
            value: list[str] = []
            i += 1
            while i < end and body[i] != '"':
                if body[i] == "\\":
                    i += 1
                if i < end:
                    value.append(body[i])
                    i += 1
            if i == end:
                raise ValueError("unterminated quotes")
            params[letter] = "".join(value)
            i += 1
        else:
            start = i
            while i < end and body[i] in "0123456789+-.,":
                i += 1
            params[letter] = body[start:i]


def _integer(value: str | None, low: int, high: int) -> int | None:
    """`store_option_if_present` for an integer: `strtol` reads a leading number and stops
    at what follows (`from_chars_light` in `src/common/str_utils.cpp`), and a value outside
    `low` to `high` is skipped (`gcode_parser.cpp:52-73`)."""
    m = None if value is None else _INTEGER.match(value)
    if m is None:
        return None
    number = int(m.group())
    return number if low <= number <= high else None


def _flag(value: str | None) -> bool | None:
    """`store_option_if_present` for a bool: no value or 1 is true, 0 false
    (`gcode_parser.cpp:31-50`)."""
    return None if value is None else {"": True, "1": True, "0": False}.get(value)


def _color(value: str | None) -> str | None:
    """`Color::from_string` for `#` and hex digits, read as far as they go
    (`src/common/utils/color.cpp:60-82`). Color names and decimal numbers aren't modeled."""
    m = None if value is None else re.match(r"#([0-9a-fA-F]+)", value)
    if m is None or int(m.group(1), 16) > 0xFFFFFFFF:
        return None
    return f"#{int(m.group(1), 16) & 0xFFFFFF:06x}"


def _uuid(value: str) -> str | None:
    """`UUID::from_string`: 32 hex digits, dashes anywhere (`uuid_constexpr.hpp`). None when
    it doesn't read, else the UUID in lowercase, or "" for the nil UUID, which clears it
    (`M865.cpp:146-169`)."""
    digits = value.replace("-", "")
    if len(value) > _UUID_BUFFER or not re.fullmatch(r"[0-9a-fA-F]{32}", digits):
        return None
    value = str(uuid.UUID(digits))
    return "" if value == str(uuid.UUID(int=0)) else value


def _set_filament(line: str, word: str, rest: str) -> SetFilament | Rejected:
    # The body ends at the first `;` or `*`, quoted or not (`gcode_basic_parser.cpp:71-89`).
    try:
        params = _params(re.split(r"[;*]", rest, maxsplit=1)[0])
    except ValueError as e:
        return Rejected(line, str(e))
    get = params.get
    c = SetFilament(
        subcode=1 if word == "M865.1" else 0,
        slot=_integer(get("I"), 0, 255),
        adhoc=bool(_flag(get("X"))),
        preset=get("S") or None,
        reset=bool(_flag(get("R"))),
        load=_integer(get("L"), 0, 255),
    )
    if c.subcode == 1:
        for letter, key in _UUIDS.items():
            value = get(letter)
            if value:  # an empty value clears
                value = _uuid(value)
            if value is not None:
                c.uuids[key] = value or None
        return c
    name = get("N")
    if name is not None and len(name.encode()) <= NAME_BUFFER:
        c.name = name
    c.color = _color(get("O"))
    for letter, (key, (low, high)) in _TEMPS.items():
        value = _integer(get(letter), low, high)
        if value is not None:
            c.temps[key] = value
    for letter, key in _FLAGS.items():
        flag = _flag(get(letter))
        if flag is not None:
            c.temps[key] = int(flag)
    return c


def _command(line: str, raw: str) -> Command:
    m = _COMMAND.match(line)
    if m is None:
        return Ignored(line)
    word, rest = m.group(1), line[m.end() :]
    if word == "M117":
        return Message(rest.strip())
    if word in ("M862.3", "M862.5", "M862.6"):
        return _check(word, rest) or Ignored(line)
    if word == "G28":
        return Home()
    if word in ("M865", "M865.1"):
        return _set_filament(line, word, raw[m.end() :])
    return Ignored(line)


def _check(word: str, rest: str) -> Check | None:
    """An `M862` check as `GCodeInfo::parse_m862` reads it, which never fails: a `P` that
    isn't a quoted string reads as an empty name (`GcodeBuffer::String::get_string` in
    `src/common/gcode/gcode_buffer.cpp`), and one that isn't a number as level 0. A line
    with no `P`, such as the query form `M862.3 Q`, checks nothing: the firmware checks
    only inside its `P` branch."""
    m = _P.search(rest)
    if m is None:
        return None
    value = rest[m.end() :]
    if word == "M862.5":
        digits = _DIGITS.match(value).group(1)
        return LevelCheck(int(digits) if digits else 0)
    quoted = re.match(r'\s*"([^"]*)"', value)
    name = quoted.group(1) if quoted else ""
    if word == "M862.3":
        return ModelCheck(name)
    return FeatureCheck(name.lstrip().rstrip(" "))  # `String::trim` cuts spaces only at the end


def scan(text: str) -> list[Check]:
    """The `M862` checks the print preview sees: those in the file's first `SCAN_LINES`
    G-code lines, as `GCodeInfo::parse_gcode` counts them. A blank line, a comment and
    `M486` don't count, and a line number is skipped. The match is case-sensitive, as
    `GcodeBuffer::String::skip_gcode` is. The rest of the file isn't read."""
    checks: list[Check] = []
    counted = 0
    for raw in text.splitlines():
        line = raw.lstrip()
        if not line or line.startswith(";"):
            continue
        if counted >= SCAN_LINES:
            break
        counted += 1
        line = _LINE_NUMBER.sub("", line)
        m = _WORD.match(line)
        word = m.group(1) if m else ""
        if word == "M486":
            counted -= 1
        elif word in ("M862.3", "M862.5", "M862.6"):
            check = _check(word, line[m.end() :])
            if check is not None:
                checks.append(check)
    return checks


def parse(text: str) -> list[Command]:
    """Every command in the job, in order. Parsing never fails: a line the firmware can't
    run is `Ignored` or `Rejected`, and the job runs on past it."""
    commands: list[Command] = []
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if line:
            # The line number is the same text at the start of both.
            number = _RUN_LINE_NUMBER.match(line)
            cut = number.end() if number else 0
            commands.append(_command(line[cut:], raw.lstrip()[cut:]))
    return commands
