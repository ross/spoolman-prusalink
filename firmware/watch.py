"""Watch the firmware source of the copied rules the check can't rebuild.

A few copies come from firmware that states its rule as control flow or across several
files, not as a table. For each, `WATCHED` records a fingerprint of the source text the
copy was read from, and the check fails when that text changes. A fingerprint says
nothing about whether the copy is right, only that its source changed since someone last
read it.

A region is one of three kinds, each found exactly once in its file:

- "function": a C++ function definition, by its name as the definition spells it, from
  the start of its line to its closing brace.
- "directive": a preprocessor line, by its start. A conditional branch (`#if`, `#elif`,
  `#else` and the like) runs to the next directive of the same conditional.
- "cmake": a CMake command, by its start. An `if()` runs to its `endif()`.

The fingerprint hashes the region with comments removed and each run of whitespace made
one space, so only a change to the code itself trips it.
"""

from __future__ import annotations

import functools
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from firmware import read
from firmware.source import Source, Unreadable

Kind = Literal["function", "directive", "cmake"]


@dataclass(frozen=True)
class Watched:
    name: str
    path: str  # the firmware file
    kind: Kind
    anchor: str  # the function's name, or the start of the directive or command
    copy: str  # what was copied from the region, and where
    fingerprint: str


# The regions the copies come from. When one changes, re-read it, check the copy it names,
# and replace its fingerprint with the one the check prints.
WATCHED = (
    # The upgrade paths: `UPGRADES` in simulator/compatibility.py.
    Watched(
        "the compatibility upgrades",
        "src/common/printer_model.cpp",
        "function",
        "gcode_compatibility_report_constexpr",
        "UPGRADES in simulator/compatibility.py",
        "bf8dbaf2d07d79c5",
    ),
    # The next two guard `GCodeChecks.required` in simulator/compatibility.py. The first
    # requires the Input Shaper and INDX lock flags; the conditions sit among other checks in
    # this function, and nothing narrower can be found reliably. It also sets the "not enough
    # tools" check that `print_checks` in simulator/preview.py copies.
    Watched(
        "the required features",
        "src/feature/compatibility_checks/gcode_compatibility.cpp",
        "function",
        "CompatibilityReport::generate_without_toolmapping",
        "GCodeChecks.required in simulator/compatibility.py and print_checks in"
        " simulator/preview.py",
        "b3cf2415e667a40b",
    ),
    # The second decides which `M862.6` name sets which flag. It parses every `M862` line, so
    # it also trips on unrelated changes: 8 commits touched it in 2026 up to 7d8bf6858.
    Watched(
        "the M862 parser",
        "src/common/gcode/gcode_info.cpp",
        "function",
        "GCodeInfo::parse_m862",
        "GCodeChecks.required in simulator/compatibility.py",
        "7e4c129c699f5571",
    ),
    # The per-tool checks of a print: the disabled tool, the missing filament and the wrong
    # type in `print_checks` and `wrong_types` in simulator/preview.py. The function runs
    # every per-tool check, nozzles and spool join among them, so it also trips on unrelated
    # changes: about 20 commits touched it in 2026 up to 7d8bf6858.
    Watched(
        "the tool checks",
        "src/feature/compatibility_checks/gcode_compatibility.cpp",
        "function",
        "CompatibilityReport::generate_toolmapping_only_noclear",
        "print_checks and wrong_types in simulator/preview.py",
        "57407f963fde6b8d",
    ),
    # When a print goes through the tools-mapping screen: `tool_mapping` in
    # simulator/preview.py.
    Watched(
        "the tool mapping condition",
        "src/common/tools_mapping.cpp",
        "function",
        "is_tool_mapping_possible",
        "tool_mapping in simulator/preview.py",
        "136be5698acb0b96",
    ),
    # The next eight guard `BOARD_IS` in firmware/compare.py. A main board is a buddy board or
    # the dev kit's; of those, only the XL's and the dev kit's define `BOARD_IS_XLBUDDY()`,
    # none defines `BOARD_IS_DWARF()`, and both macros are 0 where no board defines them.
    Watched(
        "the buddy boards",
        read.OPTIONS_FILE,
        "cmake",
        "set(BUDDY_BOARDS",
        "BOARD_IS in firmware/compare.py",
        "3c077407f5ecebdd",
    ),
    Watched(
        "the main board condition",
        read.OPTIONS_FILE,
        "cmake",
        "if(BOARD IN_LIST BUDDY_BOARDS OR",
        "BOARD_IS in firmware/compare.py",
        "c6120ac728f42a92",
    ),
    Watched(
        "the XLBUDDY board's macros",
        "include/device/board.h",
        "directive",
        "#elif BOARD() == BOARD_XLBUDDY()",
        "BOARD_IS in firmware/compare.py",
        "1b5878d336656cec",
    ),
    Watched(
        "the XL dev kit board's macros",
        "include/device/board.h",
        "directive",
        "#elif BOARD() == BOARD_XL_DEV_KIT_XLB()",
        "BOARD_IS in firmware/compare.py",
        "8d2733394e9e16e1",
    ),
    Watched(
        "the BUDDY board's macros",
        "include/device/board.h",
        "directive",
        "#elif BOARD() == BOARD_BUDDY()",
        "BOARD_IS in firmware/compare.py",
        "34ada32dca12a264",
    ),
    Watched(
        "the XBUDDY board's macros",
        "include/device/board.h",
        "directive",
        "#elif BOARD() == BOARD_XBUDDY()",
        "BOARD_IS in firmware/compare.py",
        "b366077de8f45a00",
    ),
    Watched(
        "the BOARD_IS_XLBUDDY default",
        "include/device/board.h",
        "directive",
        "#ifndef BOARD_IS_XLBUDDY",
        "BOARD_IS in firmware/compare.py",
        "6bacb277dac47d33",
    ),
    Watched(
        "the BOARD_IS_DWARF default",
        "include/device/board.h",
        "directive",
        "#ifndef BOARD_IS_DWARF",
        "BOARD_IS in firmware/compare.py",
        "04c9261a6da4db2d",
    ),
    # The load's purge: the high-flow doubling in `SimPrinter._load_lengths`, and the
    # override and that the minimum applies in `SimPrinter._load_steps`, in
    # simulator/model.py. The minimum's value is compared as `MINIMAL_PURGE_MM`.
    Watched(
        "the purge length",
        "src/marlin_stubs/pause/pause_settings.cpp",
        "function",
        "pause::Settings::purge_length",
        "SimPrinter._load_lengths and _load_steps in simulator/model.py",
        "eebdf064fe4b03fc",
    ),
)


@dataclass(frozen=True)
class Change:
    watched: Watched
    now: str  # the region's fingerprint now

    def __str__(self) -> str:
        w = self.watched
        return (
            f"{w.name} in {w.path} changed (expected {w.fingerprint}, now {self.now}):"
            f" re-read it and check {w.copy}, then record the new fingerprint"
        )


def changes(source: Source, watched: Iterable[Watched] = WATCHED) -> list[Change]:
    """Return each watched region whose fingerprint isn't the recorded one."""
    text = functools.cache(source.read)
    found = []
    for w in watched:
        now = fingerprint(region(text(w.path), w))
        if now != w.fingerprint:
            found.append(Change(w, now))
    return found


def fingerprint(region: str) -> str:
    """Return the first 16 hex digits of the SHA-256 of `region` with its whitespace
    runs made single spaces. `region` is already free of comments."""
    normal = " ".join(region.split())
    return hashlib.sha256(normal.encode()).hexdigest()[:16]


def region(text: str, watched: Watched) -> str:
    """Return the text of `watched`'s region in its file's `text`, without comments."""
    try:
        if watched.kind == "cmake":
            return _cmake(_strip_cmake_comments(text), watched.anchor)
        text = read._strip_comments(text)
        if watched.kind == "function":
            return _function(text, watched.anchor)
        return _directive(text, watched.anchor)
    except ValueError as e:
        raise Unreadable(watched.path, f"{e}, for {watched.name}") from None


def _function(text: str, name: str) -> str:
    masked = _mask_literals(text)
    parts = (re.escape(p) for p in name.split("::"))
    head = re.compile(r"(?<![\w:.>~])" + r"\s*::\s*".join(parts) + r"\s*\(")
    # Between the parameters and the body only qualifiers such as `const` may stand, so a
    # call such as `if (name(x)) {` isn't taken for the definition.
    qualifiers = re.compile(r"[\w\s:&*<>,]*\{")
    found = []
    for m in head.finditer(masked):
        body = qualifiers.match(masked, _closing(masked, m.end() - 1, "(", ")"))
        if body:
            found.append((m.start(), body.end() - 1))
    if len(found) != 1:
        raise ValueError(f"{len(found)} definitions of {name}")
    start, brace = found[0]
    start = masked.rfind("\n", 0, start) + 1
    return text[start : _closing(masked, brace, "{", "}")]


def _closing(text: str, start: int, opening: str, closing: str) -> int:
    """Return the index just past the `closing` that matches the `opening` at `start`."""
    depth = 0
    for i in range(start, len(text)):
        depth += {opening: 1, closing: -1}.get(text[i], 0)
        if depth == 0:
            return i + 1
    raise ValueError(f"unbalanced {opening}{closing}")


# A string or character literal. A digit separator, as in `8'000`, follows a word character.
_LITERAL = re.compile(r'"(?:[^"\\\n]|\\.)*"|(?<![\w\'])\'(?:[^\'\\\n]|\\.)+\'')


def _mask_literals(text: str) -> str:
    """Blank the inside of each literal, keeping every index, so its braces don't count."""
    return _LITERAL.sub(lambda m: m.group()[0] + " " * (len(m.group()) - 2) + m.group()[0], text)


_BRANCH = re.compile(r"#\s*(if|ifdef|ifndef|elif|else|endif)\b")


def _directive(text: str, anchor: str) -> str:
    lines = text.replace("\\\n", " ").splitlines()
    starts = [i for i, line in enumerate(lines) if _starts(_directive_text(line), anchor)]
    if len(starts) != 1:
        raise ValueError(f"{len(starts)} lines start {anchor}")
    first = starts[0]
    kind = _BRANCH.match(lines[first].strip())
    if not kind or kind.group(1) == "endif":
        return lines[first]
    depth = 0
    for i in range(first + 1, len(lines)):
        m = _BRANCH.match(lines[i].strip())
        if not m:
            continue
        if m.group(1).startswith("if"):
            depth += 1
        elif depth:
            depth -= m.group(1) == "endif"
        else:
            return "\n".join(lines[first:i])
    raise ValueError(f"{anchor} isn't closed")


def _directive_text(line: str) -> str:
    return re.sub(r"^#\s*", "#", " ".join(line.split()))


def _starts(statement: str, anchor: str) -> bool:
    return re.match(re.escape(anchor) + r"(?!\w)", statement) is not None


def _cmake(text: str, anchor: str) -> str:
    commands = list(read._cmake_commands(text))
    statements = [f"{name}({' '.join(args.split())})" for name, args, _ in commands]
    starts = [i for i, s in enumerate(statements) if _starts(s, anchor)]
    if len(starts) != 1:
        raise ValueError(f"{len(starts)} commands start {anchor}")
    first = last = starts[0]
    if commands[first][0] == "if":
        depth = 0
        for last in range(first, len(commands)):
            depth += {"if": 1, "endif": -1}.get(commands[last][0], 0)
            if depth == 0:
                break
        else:
            raise ValueError(f"{anchor} isn't closed")
    _, args, line = commands[last]
    lines = text.splitlines()
    return "\n".join(lines[commands[first][2] - 1 : line + args.count("\n")])


def _strip_cmake_comments(text: str) -> str:
    return re.sub(r'("(?:[^"\\]|\\.)*")|#[^\n]*', lambda m: m.group(1) or "", text)
