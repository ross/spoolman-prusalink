"""Read tables out of the firmware's source files.

Each reader takes a file's text and raises `Unreadable` when the text doesn't have the
structure it expects, so a change in the firmware's layout fails the check rather than
passing it. The C++ readers understand the preprocessor conditions and value expressions
the files use today, nothing more: see `evaluate` and `active_lines`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import NoReturn

from firmware.source import Unreadable

MODELS_FILE = "include/common/printer_model_data.hpp"
OPTIONS_FILE = "ProjectOptions.cmake"
PRESETS_FILE = "src/common/filament_presets.cpp"
FILAMENT_FILE = "src/common/filament.hpp"
ALIASES_FILE = "src/feature/openprinttag/data_utils.cpp"
TRACKER_FILE = "src/feature/filament_tracker/filament_tracker.hpp"

# A macro such as `HAS_LOADCELL` or `PRINTER_IS_PRUSA_MINI`, without the `()`, to its value
# for one build. Raises KeyError for a macro it doesn't know.
Macros = Callable[[str], bool]
Value = int | bool | str | None


def printer_models(text: str) -> dict[str, str]:
    """Return each printer model's type code, as "a.b.c", with its `id_str`."""
    try:
        region = _one(r"printer_model_info\s*\{(.*?)\n\};", text, "printer_model_info array")
        entries = re.split(r"PrinterModelInfo\s*\{", region)[1:]
        if not entries:
            raise ValueError("no PrinterModelInfo entries")
        models: dict[str, str] = {}
        for entry in entries:
            version = _one(
                r"\.version\s*=\s*\{\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\}", entry, ".version", 3
            )
            name = _one(r'\.id_str\s*=\s*"([^"]*)"', entry, ".id_str")
            code = ".".join(version)
            if code in models:
                raise ValueError(f"model {code} appears twice")
            models[code] = name
    except ValueError as e:
        raise Unreadable(MODELS_FILE, str(e)) from None
    return models


def features(text: str, needed: Iterable[str]) -> dict[str, frozenset[str]]:
    """Return the builds (`PRINTER` values) each needed feature is set for.

    Reads the `set_feature_for_printers` and `set_feature_for_printers_master_board` calls.
    The master-board form is the same for a printer's own build. A feature has to be set
    exactly once, outside any `if()` or other block, and nothing else may set it; anything
    else would make its value depend on more than the printer.
    """
    needed = set(needed)
    found: dict[str, frozenset[str]] = {}
    try:
        blocks: list[tuple[str, int]] = []
        for name, args, line in _cmake_commands(text):
            words = _cmake_words(args)
            if name in _CMAKE_OPENERS:
                blocks.append((name, line))
            elif name in _CMAKE_CLOSERS:
                if not blocks:
                    raise ValueError(f"{name}() on line {line} closes nothing")
                blocks.pop()
            elif name in ("set_feature_for_printers", "set_feature_for_printers_master_board"):
                if not words or words[0] not in needed:
                    continue
                feature = words[0]
                if feature in found:
                    raise ValueError(f"{feature} is set more than once (again on line {line})")
                if blocks:
                    opener, at = blocks[-1]
                    raise ValueError(f"{feature} is set inside {opener}() on line {at}")
                found[feature] = frozenset(words[1:])
            elif name in _CMAKE_SETTERS and words and words[0] in needed:
                raise ValueError(f"{words[0]} is also set by {name}() on line {line}")
        if blocks:
            opener, at = blocks[-1]
            raise ValueError(f"{opener}() on line {at} isn't closed")
        missing = sorted(needed - found.keys())
        if missing:
            raise ValueError(f"no set_feature_for_printers call for {', '.join(missing)}")
    except ValueError as e:
        raise Unreadable(OPTIONS_FILE, str(e)) from None
    return found


# Fields the bridge or the simulator copies, and fields the comparison leaves out on
# purpose. A preset field in neither set fails the read, so a new firmware field gets a
# look rather than passing unnoticed.
PRESET_FIELDS = frozenset(
    {
        "name",
        "nozzle_temperature",
        "nozzle_preheat_temperature",
        "heatbed_temperature",
        "chamber_min_temperature",
        "chamber_max_temperature",
        "chamber_target_temperature",
        "requires_filtration",
        "is_flexible",
        "requires_ht_idler_door",
    }
)
# The heatbreak temperature is an iX-only parameter, and the only abrasive presets are
# PPS and PPA, which the bridge doesn't write. These aren't evaluated at all.
IGNORED_FIELDS = frozenset({"heatbreak_temperature", "is_abrasive"})


def presets(text: str, macros: Macros) -> dict[str, dict[str, Value]]:
    """Return the preset filaments of one build: each name with its field values.

    Fields an entry leaves out take `PresetFilamentParameters`' member defaults. A field
    the build doesn't have (such as the chamber fields on a printer without a chamber) is
    absent from the result.
    """
    try:
        text = _strip_comments(text)
        struct = _one(
            r"struct PresetFilamentParameters\s*\{(.*?)\n\};",
            text,
            "struct PresetFilamentParameters",
        )
        defaults = _members(" ".join(active_lines(struct.splitlines(), macros)))
        table = _one(
            r"preset_filament_parameters_constexpr\s*\{(.*?)\n\};",
            text,
            "preset_filament_parameters_constexpr",
        )
        table = "\n".join(active_lines(table.splitlines(), macros))
        result: dict[str, dict[str, Value]] = {}
        for kind, fields in _entries(table):
            given = dict(_initializers(fields, kind))
            unknown = given.keys() - defaults.keys()
            if unknown:
                raise ValueError(f"{kind} sets {', '.join(sorted(unknown))}, which isn't a member")
            values: dict[str, Value] = {}
            for field, default in defaults.items():
                if field in IGNORED_FIELDS:
                    continue
                expr = given.get(field, default)
                if expr is None:
                    raise ValueError(f"{kind} has no value for {field}")
                values[field] = evaluate(expr, macros)
            name = values.get("name")
            if not isinstance(name, str) or name in result:
                raise ValueError(f"{kind} has a missing or repeated name")
            result[name] = values
        if not result:
            raise ValueError("no presets")
    except ValueError as e:
        raise Unreadable(PRESETS_FILE, str(e)) from None
    return result


def reset_defaults(text: str) -> tuple[int, int, int]:
    """Return `FilamentTypeParameters`' default nozzle, preheat and bed temperatures."""
    try:
        text = _strip_comments(text)
        struct = _one(
            r"struct FilamentTypeParameters\s*\{(.*?)\n\};", text, "struct FilamentTypeParameters"
        )
        names = ("nozzle_temperature", "nozzle_preheat_temperature", "heatbed_temperature")
        values = [int(_one(rf"\b{n}\s*=\s*(\d+)\s*;", struct, f"default for {n}")) for n in names]
    except ValueError as e:
        raise Unreadable(FILAMENT_FILE, str(e)) from None
    return values[0], values[1], values[2]


def aliases(text: str) -> dict[str, str]:
    """Return the tag abbreviations the OpenPrintTag reader maps onto another preset.

    Reads each `abbreviation.abbreviation == "X"` test followed by
    `base_type = PresetFilamentType::Y;`: the TPU workaround in the
    `FilamentParametersInfo` constructor.
    """
    text = _strip_comments(text)
    found = re.findall(
        r'abbreviation\.abbreviation\s*==\s*"([^"]+)"\s*\)\s*\{\s*'
        r"base_type\s*=\s*PresetFilamentType::(\w+)\s*;",
        text,
    )
    tests = len(re.findall(r"abbreviation\.abbreviation\s*==", text))
    if not found:
        raise Unreadable(ALIASES_FILE, "no abbreviation mapped onto a preset")
    if tests != len(found):
        raise Unreadable(ALIASES_FILE, "an abbreviation test doesn't map onto a preset")
    return dict(found)


_GAP_NAME = r"\bextruder_to_nozzle_distance\s*="


def tracker_gap(text: str, macros: Macros) -> float | None:
    """Return `extruder_to_nozzle_distance` for one build, or None where it's an #error.

    The value sits in an `#if` chain; a build none of its branches covers can't build the
    filament tracker.
    """
    try:
        lines = _strip_comments(text).splitlines()
        at = next(
            (i for i, line in enumerate(lines) if re.search(_GAP_NAME, line)),
            None,
        )
        if at is None:
            raise ValueError("no extruder_to_nozzle_distance")
        block = _enclosing_block(lines, at)
        active = "\n".join(active_lines(block, macros))
        if re.search(r"^\s*#\s*error\b", active, re.M):
            return None
        return float(
            _one(
                r"extruder_to_nozzle_distance\s*=\s*(\d+(?:\.\d*)?)f?\s*;",
                active,
                "numeric extruder_to_nozzle_distance",
            )
        )
    except ValueError as e:
        raise Unreadable(TRACKER_FILE, str(e)) from None


def active_lines(lines: Iterable[str], macros: Macros) -> list[str]:
    """Return the lines the preprocessor keeps for one build.

    Handles `#if`, `#elif`, `#else` and `#endif` with conditions `evaluate` reads. Keeps an
    active `#error` line for the caller to see. Any other directive is an error.
    """
    out: list[str] = []
    # Per open #if: whether the enclosing lines are active, whether a branch was taken,
    # whether the current branch is active, and whether #else was seen.
    stack: list[list[bool]] = []

    def active() -> bool:
        return not stack or stack[-1][2]

    for line in lines:
        m = re.match(r"\s*#\s*(\w+)\s*(.*?)\s*$", line)
        if not m:
            if active():
                out.append(line)
            continue
        directive, rest = m.groups()
        # Conditions are evaluated in inactive branches too, so an unknown macro fails
        # whatever the build.
        if directive == "if":
            outer = active()
            taken = bool(evaluate(rest, macros)) and outer
            stack.append([outer, taken, taken, False])
        elif directive == "elif":
            if not stack or stack[-1][3]:
                raise ValueError("#elif without #if")
            outer, taken = stack[-1][0], stack[-1][1]
            now = bool(evaluate(rest, macros)) and outer and not taken
            stack[-1][1:3] = [taken or now, now]
        elif directive == "else":
            if not stack or stack[-1][3]:
                raise ValueError("#else without #if")
            outer, taken = stack[-1][0], stack[-1][1]
            stack[-1][1:] = [True, outer and not taken, True]
        elif directive == "endif":
            if not stack:
                raise ValueError("#endif without #if")
            stack.pop()
        elif directive == "error":
            if active():
                out.append(line)
        else:
            raise ValueError(f"unexpected #{directive}")
    if stack:
        raise ValueError("#if without #endif")
    return out


_TOKEN = re.compile(
    r'\s*(?:(?P<num>\d+)\b|(?P<str>"[^"\\]*")|(?P<name>[A-Za-z_]\w*(?:::\w+)*)'
    r"|(?P<op>&&|\|\||[()?:!+-]))"
)
_LITERALS: dict[str, Value] = {"true": True, "false": False, "std::nullopt": None}


def evaluate(expr: str, macros: Macros) -> Value:
    """Evaluate a preprocessor condition or an initializer for one build.

    The grammar: integers, strings, `true`, `false`, `std::nullopt`, macro calls such as
    `HAS_INDX()`, parentheses, `!`, unary and binary `+` and `-`, `&&`, `||` and `?:`. Both
    sides of a `?:` are evaluated, so an unknown macro anywhere fails.
    """
    tokens: list[tuple[str, str]] = []
    pos = 0
    expr = expr.rstrip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            raise ValueError(f"can't evaluate {expr.strip()!r}")
        kind = m.lastgroup or ""
        tokens.append((kind, m.group(kind)))
        pos = m.end()
    if not tokens:
        raise ValueError("empty expression")
    return _Parser(tokens, macros, expr.strip()).parse()


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], macros: Macros, expr: str) -> None:
        self.tokens, self.macros, self.expr, self.i = tokens, macros, expr, 0

    def parse(self) -> Value:
        value = self.ternary()
        if self.i != len(self.tokens):
            self.fail()
        return value

    def fail(self) -> NoReturn:
        raise ValueError(f"can't evaluate {self.expr!r}")

    def peek(self) -> str | None:
        return self.tokens[self.i][1] if self.i < len(self.tokens) else None

    def take(self, op: str) -> bool:
        if self.i < len(self.tokens) and self.tokens[self.i] == ("op", op):
            self.i += 1
            return True
        return False

    def ternary(self) -> Value:
        cond = self.or_()
        if not self.take("?"):
            return cond
        yes = self.ternary()
        if not self.take(":"):
            self.fail()
        no = self.ternary()
        return yes if cond else no

    def or_(self) -> Value:
        value = self.and_()
        while self.take("||"):
            right = self.and_()
            value = bool(value) or bool(right)
        return value

    def and_(self) -> Value:
        value = self.additive()
        while self.take("&&"):
            right = self.additive()
            value = bool(value) and bool(right)
        return value

    def additive(self) -> Value:
        value = self.unary()
        while (op := self.peek()) in ("+", "-"):
            self.i += 1
            right = self.number(self.unary())
            value = self.number(value) + (right if op == "+" else -right)
        return value

    def unary(self) -> Value:
        if self.take("!"):
            return not self.unary()
        if self.take("-"):
            return -self.number(self.unary())
        if self.take("+"):
            return self.number(self.unary())
        return self.primary()

    def number(self, value: Value) -> int:
        if isinstance(value, str) or value is None:
            self.fail()
        return int(value)  # type: ignore[arg-type]

    def primary(self) -> Value:
        if self.i >= len(self.tokens):
            self.fail()
        kind, text = self.tokens[self.i]
        self.i += 1
        if kind == "num":
            return int(text)
        if kind == "str":
            return text[1:-1]
        if kind == "name":
            if text in _LITERALS:
                return _LITERALS[text]
            if not (self.take("(") and self.take(")")):
                self.fail()
            try:
                return bool(self.macros(text))
            except KeyError:
                raise ValueError(f"unknown macro {text}()") from None
        if (kind, text) == ("op", "("):
            value = self.ternary()
            if not self.take(")"):
                self.fail()
            return value
        self.fail()


def _members(body: str) -> dict[str, str | None]:
    """Return each struct member with its default initializer, or None for none."""
    members: dict[str, str | None] = {}
    for decl in body.split(";"):
        decl = " ".join(decl.split())
        if not decl:
            continue
        m = re.fullmatch(r"(?:decltype\([\w:]+\)|[\w:<>]+) (\w+)(?: ?: ?\d+)?(?: ?= ?(.+))?", decl)
        if not m:
            raise ValueError(f"can't read the member {decl!r}")
        name = m.group(1)
        if name in members:
            raise ValueError(f"member {name} appears twice")
        if name not in PRESET_FIELDS | IGNORED_FIELDS:
            raise ValueError(f"unknown member {name}")
        members[name] = m.group(2)
    return members


_ENTRY = re.compile(
    r"\{\s*PresetFilamentType::(\w+)\s*,\s*PresetFilamentParameters\s*\{([^{}]*)\}\s*,?\s*\}"
)


def _entries(table: str) -> list[tuple[str, str]]:
    """Split the preset table into (kind, initializer list) pairs, covering all of it."""
    entries = []
    pos = 0
    for m in _ENTRY.finditer(table):
        if table[pos : m.start()].strip(" \t\n,"):
            raise ValueError(f"unexpected text before {m.group(1)}")
        entries.append((m.group(1), m.group(2)))
        pos = m.end()
    if table[pos:].strip(" \t\n,"):
        raise ValueError("unexpected text after the last preset")
    return entries


def _initializers(fields: str, kind: str) -> Iterable[tuple[str, str]]:
    seen = set()
    for item in fields.split(","):
        if not item.strip():
            continue
        m = re.fullmatch(r"\s*\.(\w+)\s*=\s*(.+?)\s*", item, re.S)
        if not m:
            raise ValueError(f"can't read {kind}'s initializer {item.strip()!r}")
        if m.group(1) in seen:
            raise ValueError(f"{kind} sets {m.group(1)} twice")
        seen.add(m.group(1))
        yield m.group(1), m.group(2)


def _enclosing_block(lines: list[str], at: int) -> list[str]:
    """Return the `#if` ... `#endif` block around line `at`, or just that line."""
    depth = 0
    start = None
    for i in range(at - 1, -1, -1):
        directive = _directive(lines[i])
        if directive == "endif":
            depth += 1
        elif directive in ("if", "ifdef", "ifndef"):
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return [lines[at]]
    depth = 0
    for i in range(start, len(lines)):
        directive = _directive(lines[i])
        if directive in ("if", "ifdef", "ifndef"):
            depth += 1
        elif directive == "endif":
            depth -= 1
            if depth == 0:
                return lines[start : i + 1]
    raise ValueError("#if without #endif")


def _directive(line: str) -> str | None:
    m = re.match(r"\s*#\s*(\w+)", line)
    return m.group(1) if m else None


def _strip_comments(text: str) -> str:
    return re.sub(
        r'("(?:[^"\\\n]|\\.)*")|//[^\n]*|/\*.*?\*/',
        lambda m: m.group(1) or "",
        text,
        flags=re.S,
    )


def _one(pattern: str, text: str, what: str, groups: int = 1):
    """Return the groups of `pattern`'s only match in `text`; fail on none or several."""
    matches = list(re.finditer(pattern, text, re.S))
    if not matches:
        raise ValueError(f"no {what}")
    if len(matches) > 1:
        raise ValueError(f"{what} appears {len(matches)} times")
    return matches[0].group(1) if groups == 1 else matches[0].groups()


_CMAKE_OPENERS = frozenset({"if", "function", "macro", "foreach", "while", "block"})
_CMAKE_CLOSERS = frozenset(
    {"endif", "endfunction", "endmacro", "endforeach", "endwhile", "endblock"}
)
_CMAKE_SETTERS = frozenset({"set", "unset", "option", "define_boolean_option"})
_CMAKE_NAME = re.compile(r"[A-Za-z_]\w*")


def _cmake_commands(text: str) -> Iterable[tuple[str, str, int]]:
    """Yield each command invocation as (lowercase name, argument text, line number)."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "#":
            if re.match(r"#\[=*\[", text[i:]):
                raise ValueError(f"bracket comment on line {_line(text, i)}")
            i = _end_of_line(text, i)
        else:
            m = _CMAKE_NAME.match(text, i)
            if not m:
                raise ValueError(f"unexpected {c!r} on line {_line(text, i)}")
            line = _line(text, i)
            i = m.end()
            while i < n and text[i] in " \t":
                i += 1
            if i >= n or text[i] != "(":
                raise ValueError(f"{m.group()} on line {line} has no arguments")
            start = i + 1
            depth = 1
            i += 1
            while depth:
                if i >= n:
                    raise ValueError(f"{m.group()} on line {line} isn't closed")
                c = text[i]
                if c == '"':
                    i = _end_of_string(text, i, line)
                    continue
                if c == "#":
                    i = _end_of_line(text, i)
                    continue
                if c == "[" and re.match(r"\[=*\[", text[i:]):
                    raise ValueError(f"bracket argument on line {_line(text, i)}")
                depth += {"(": 1, ")": -1}.get(c, 0)
                i += 1
            yield m.group().lower(), text[start : i - 1], line


def _cmake_words(args: str) -> list[str]:
    """Split command arguments into words, unquoting quoted ones and dropping comments."""
    words = []
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|#[^\n]*|([^\s()"#]+)', args):
        if m.group(1) is not None:
            words.append(m.group(1))
        elif m.group(2) is not None:
            words.append(m.group(2))
    return words


def _end_of_line(text: str, i: int) -> int:
    end = text.find("\n", i)
    return len(text) if end < 0 else end


def _end_of_string(text: str, i: int, line: int) -> int:
    m = re.compile(r'"(?:[^"\\]|\\.)*"', re.S).match(text, i)
    if not m:
        raise ValueError(f"unterminated string on line {line}")
    return m.end()


def _line(text: str, i: int) -> int:
    return text.count("\n", 0, i) + 1
