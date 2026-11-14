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

# A parameter is a letter and a value: a quoted string, possibly after spaces (`P "X"`), or
# an unquoted word attached to the letter (`T230`, or nothing for a flag like `X`).
_PARAM = re.compile(r'([A-Za-z])(?:\s*"([^"]*)"|([^\s"]*))')
_WORD = re.compile(r"^([GMT]\d+(?:\.\d+)?)(?=\s|$)", re.IGNORECASE)

# M865 parameters kept for display, by letter.
_TEMPS = {
    "T": "nozzle",
    "B": "bed",
    "P": "preheat",
    "C": "chamber_target",
    "D": "chamber_min",
    "E": "chamber_max",
    "F": "filtration",
    "G": "flexible",
}


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
    tool: int = 0
    reset: bool = False
    adhoc: bool = False
    name: str | None = None
    color: str | None = None
    temps: dict[str, int] = field(default_factory=dict)
    copy_from: int | None = None


@dataclass
class SetUuid:
    tool: int
    uuid: str


@dataclass
class Message:
    text: str


@dataclass
class Ignored:
    line: str


Check = ModelCheck | LevelCheck | FeatureCheck
Command = ModelCheck | LevelCheck | FeatureCheck | Home | SetFilament | SetUuid | Message | Ignored

# The print preview reads a file's checks from its first 200 G-code lines
# (`search_first_x_gcodes` in `src/common/gcode/gcode_info.hpp`).
SCAN_LINES = 200
_LINE_NUMBER = re.compile(r"^N\d[\d\s]*")
_P = re.compile(r"(?:^|\s)P")
_DIGITS = re.compile(r"^\s*(\d*)")


class GcodeError(ValueError):
    """A line the firmware would reject, as "line N: reason"."""


def _strip_comment(line: str) -> str:
    """Cut the line at the first `;` outside quotes."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            return line[:i]
    return line


def _params(rest: str) -> dict[str, str]:
    if rest.count('"') % 2:
        raise ValueError("unterminated quote")
    params: dict[str, str] = {}
    pos = 0
    rest = rest.strip()
    while pos < len(rest):
        if rest[pos].isspace():
            pos += 1
            continue
        m = _PARAM.match(rest, pos)
        if m is None:
            raise ValueError(f"can't read {rest[pos:]!r}")
        value = m.group(2) if m.group(2) is not None else m.group(3)
        params[m.group(1).upper()] = value
        pos = m.end()
    return params


def _int(params: dict[str, str], letter: str) -> int | None:
    if letter not in params:
        return None
    try:
        return int(params[letter])
    except ValueError:
        raise ValueError(f"{letter} must be a number, got {params[letter]!r}") from None


def _set_filament(params: dict[str, str]) -> SetFilament:
    name = params.get("N")
    if name is not None and not NAME_RE.match(name):
        raise ValueError(f"filament name {name!r} must be 1 to 7 of A-Z a-z 0-9 _ -")
    color = params.get("O")
    if color is not None:
        if not COLOR_RE.match(color):
            raise ValueError(f"color {color!r} must look like #rrggbb")
        color = color.lower()
    temps = {}
    for letter, key in _TEMPS.items():
        value = _int(params, letter)
        if value is not None:
            temps[key] = value
    tool = _int(params, "L")
    slot = _int(params, "I")
    # I alone picks the slot to modify; with L it names the tool to copy from.
    if tool is None:
        tool, slot = (slot if slot is not None else 0), None
    return SetFilament(
        tool=tool,
        reset="R" in params,
        adhoc="X" in params,
        name=name,
        color=color,
        temps=temps,
        copy_from=slot,
    )


def _set_uuid(params: dict[str, str]) -> SetUuid:
    raw = params.get("C")
    if raw is None:
        raise ValueError('M865.1 needs C"<uuid>"')
    try:
        value = str(uuid.UUID(raw))
    except ValueError:
        raise ValueError(f"{raw!r} isn't a UUID") from None
    return SetUuid(tool=_int(params, "I") or 0, uuid=value)


def _command(line: str) -> Command:
    m = _WORD.match(line)
    if m is None:
        return Ignored(line)
    word, rest = m.group(1).upper(), line[m.end() :]
    if word == "M117":
        return Message(rest.strip())
    if word in ("M862.3", "M862.5", "M862.6"):
        return _check(word, rest)
    if word == "G28":
        return Home()
    if word == "M865":
        return _set_filament(_params(rest))
    if word == "M865.1":
        return _set_uuid(_params(rest))
    return Ignored(line)


def _check(word: str, rest: str) -> Check:
    """An `M862` check as `GCodeInfo::parse_m862` reads it, which never fails: a `P` that
    isn't a quoted string reads as an empty name (`GcodeBuffer::String::get_string` in
    `src/common/gcode/gcode_buffer.cpp`), and one that isn't a number as level 0."""
    m = _P.search(rest)
    value = rest[m.end() :] if m else ""
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
    `GcodeBuffer::String::skip_gcode` is. The rest of the file isn't read, so a line the
    job would stop on doesn't matter here."""
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
            checks.append(_check(word, line[m.end() :]))
    return checks


def parse(text: str) -> list[Command]:
    commands: list[Command] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        try:
            commands.append(_command(line))
        except ValueError as e:
            raise GcodeError(f"line {number}: {e}") from None
    return commands
