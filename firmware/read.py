"""Read tables out of the firmware's source files.

Each reader takes a file's text and raises `Unreadable` when the text doesn't have the
structure it expects, so a change in the firmware's layout fails the check rather than
passing it. The C++ readers understand the preprocessor conditions and value expressions
the files use today, nothing more: see `evaluate` and `active_lines`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import NoReturn

from firmware.source import Unreadable

MODELS_FILE = "include/common/printer_model_data.hpp"
GCODE_INFO_FILE = "src/common/gcode/gcode_info.hpp"
OPTIONS_FILE = "ProjectOptions.cmake"
IMAGES_FILE = "utils/presets/presets.json"
PRESETS_FILE = "src/common/filament_presets.cpp"
FILAMENT_FILE = "src/common/filament.hpp"
ALIASES_FILE = "src/feature/openprinttag/data_utils.cpp"
TRACKER_FILE = "src/feature/filament_tracker/filament_tracker.hpp"
RAMMING_CMAKE_FILE = "src/feature/ramming/CMakeLists.txt"
CLEANER_FILE = "src/feature/nozzle_cleaner/nozzle_cleaner.cpp"
AUTO_RETRACT_FILE = "src/feature/auto_retract/auto_retract.hpp"
PAUSE_FILE = "src/marlin_stubs/pause/pause.cpp"
PAUSE_SETTINGS_FILE = "src/marlin_stubs/include/pause_settings.hpp"
CONFIGURATION_FILE = "include/marlin/Configuration.h"
ADVANCED_FILE = "include/marlin/Configuration_adv.h"


def configuration_file(name: str) -> str:
    """Return the path of `Configuration_<name>.h`, such as `Configuration_MK4.h`."""
    return f"include/marlin/Configuration_{name}.h"


def advanced_file(name: str) -> str:
    """Return the path of `Configuration_<name>_adv.h`."""
    return f"include/marlin/Configuration_{name}_adv.h"


def ramming_file(variant: str) -> str:
    """Return the path of the standard ramming sequences for `variant`, such as "indx"."""
    return f"src/feature/ramming/standard_ramming_sequence_{variant}.cpp"


# A macro such as `HAS_LOADCELL` or `PRINTER_IS_PRUSA_MINI`, without the `()`, to its value
# for one build. Raises KeyError for a macro it doesn't know.
Macros = Callable[[str], bool]
Value = int | bool | str | None


@dataclass(frozen=True)
class PrinterModel:
    """One `printer_model_info` entry."""

    name: str  # `id_str`, the name a file's `M862.3` gives the model
    group: str  # its `PrinterModelCompatibilityGroup`


def printer_models(text: str) -> dict[str, PrinterModel]:
    """Return each printer model's type code, as "a.b.c", with its entry."""
    try:
        entries = _model_entries(_strip_comments(text))
    except ValueError as e:
        raise Unreadable(MODELS_FILE, str(e)) from None
    return {code: PrinterModel(name, group) for _, code, name, group in entries}


def mmu_variants(text: str) -> dict[str, str]:
    """Return each `printer_model_mmu_variant` name with its base model's type code.

    `PrinterModelInfo::from_id_str` takes a file's `M862.3` name as the base model.
    """
    try:
        text = _strip_comments(text)
        codes = {model: code for model, code, _, _ in _model_entries(text)}
        variants: dict[str, str] = {}
        for entry in _array_entries(text, "printer_model_mmu_variant", "PrinterModelMMUVariant"):
            model = _enum_field(entry, "model", "PrinterModel")
            name = _id_str(entry)
            if model not in codes:
                raise ValueError(f"the MMU variant {name} names an unknown PrinterModel::{model}")
            if name in variants:
                raise ValueError(f"the MMU variant {name} appears twice")
            variants[name] = codes[model]
    except ValueError as e:
        raise Unreadable(MODELS_FILE, str(e)) from None
    return variants


def _array_entries(text: str, array: str, kind: str) -> list[str]:
    """Return the text inside each `kind { ... }` entry in the brace table `array`.

    The table may hold only those entries: a preprocessor line could leave an entry out of
    some builds, and text before an entry or after one, other than its comma, would be an
    entry of another shape.
    """
    region = _one(rf"{array}\s*\{{(.*?)\n\}};", text, f"{array} array")
    if re.search(r"^[ \t]*#", region, re.M):
        raise ValueError(f"a preprocessor line in {array}")
    before, *pieces = re.split(rf"{kind}\s*(?=\{{)", region)
    if before.strip():
        raise ValueError(f"unexpected {before.strip()!r} before the first {kind} in {array}")
    if not pieces:
        raise ValueError(f"no {kind} entries")
    entries = []
    for piece in pieces:
        end = _closing_brace(piece, 0)
        after = piece[end:].strip()
        if after not in ("", ","):
            raise ValueError(f"unexpected {after!r} after a {kind} in {array}")
        entries.append(piece[1 : end - 1])
    return entries


def _id_str(entry: str) -> str:
    """Return an entry's `.id_str`: one string literal, then the end of the initializer."""
    name, rest = _one(r'\.id_str\s*=\s*"([^"]*)"([^,}]*)', entry, ".id_str", 2)
    if rest.strip():
        raise ValueError(f'can\'t read .id_str = "{name}"{rest.rstrip()}')
    return name


def _enum_field(entry: str, field: str, enum: str) -> str:
    """Return the member a designated initializer `.field = enum::member` names."""
    value = _one(rf"\.{field}\s*=\s*([^,}}]*)", entry, f".{field}").strip()
    m = re.fullmatch(rf"{enum}::(\w+)", value)
    if not m:
        raise ValueError(f"can't read .{field} = {value}")
    return m.group(1)


def _model_entries(text: str) -> list[tuple[str, str, str, str]]:
    """Return each `printer_model_info` entry as (`PrinterModel` member, type code, `id_str`,
    compatibility group). Raises ValueError."""
    found = []
    codes, models = set(), set()
    for entry in _array_entries(text, "printer_model_info", "PrinterModelInfo"):
        model = _enum_field(entry, "model", "PrinterModel")
        group = _enum_field(entry, "compatibility_group", "PrinterModelCompatibilityGroup")
        version = _one(
            r"\.version\s*=\s*\{\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\}", entry, ".version", 3
        )
        name = _id_str(entry)
        code = ".".join(version)
        if code in codes:
            raise ValueError(f"model {code} appears twice")
        if model in models:
            raise ValueError(f"PrinterModel::{model} appears twice")
        codes.add(code)
        models.add(model)
        found.append((model, code, name, group))
    return found


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
        for name, words, line, block in _cmake_statements(text):
            if name in ("set_feature_for_printers", "set_feature_for_printers_master_board"):
                if not words or words[0] not in needed:
                    continue
                feature = words[0]
                if feature in found:
                    raise ValueError(f"{feature} is set more than once (again on line {line})")
                if block:
                    raise ValueError(f"{feature} is set inside {block}")
                found[feature] = frozenset(words[1:])
            elif name in _CMAKE_SETTERS and words and words[0] in needed:
                raise ValueError(f"{words[0]} is also set by {name}() on line {line}")
        missing = sorted(needed - found.keys())
        if missing:
            raise ValueError(f"no set_feature_for_printers call for {', '.join(missing)}")
    except ValueError as e:
        raise Unreadable(OPTIONS_FILE, str(e)) from None
    return found


def printers(text: str) -> frozenset[str]:
    """Return the builds `PRINTER` may name: `PRINTER_VALID_OPTS`, set once at top level."""
    return _set_once(text, "PRINTER_VALID_OPTS")


def buddy_boards(text: str) -> frozenset[str]:
    """Return the `BUDDY_BOARDS`, set once at top level: the buddy main boards."""
    return _set_once(text, "BUDDY_BOARDS")


def _set_once(text: str, variable: str) -> frozenset[str]:
    """Return the list one `set()` at the top level of `ProjectOptions.cmake` gives
    `variable`, each value a plain word.

    Any other statement with `variable` as one of its words fails the read, since CMake
    commands name the variable they write in different places (`string(APPEND X ...)`,
    `list(LENGTH L X)`). So does a value CMake would expand, split or take as a keyword:
    one holding `$` or `;`, an empty one, `CACHE` or `PARENT_SCOPE`. A `${X}` inside a
    longer word only reads the variable, so it passes.
    """
    found = None
    try:
        for name, words, line, block in _cmake_statements(text):
            if variable not in words:
                continue
            if found is not None and name == "set" and words[0] == variable:
                raise ValueError(f"{variable} is set more than once (again on line {line})")
            if block or name != "set" or words[0] != variable:
                raise ValueError(
                    f"{variable} appears in {name}() on line {line}"
                    + (f" inside {block}" if block else "")
                )
            for word in words[1:]:
                if not word or "$" in word or ";" in word or word in _CMAKE_SET_KEYWORDS:
                    raise ValueError(f"can't read {variable}'s value {word!r} on line {line}")
            found = frozenset(words[1:])
        if not found:
            raise ValueError(f"no {variable}")
    except ValueError as e:
        raise Unreadable(OPTIONS_FILE, str(e)) from None
    return found


_IMAGE_KEYS = frozenset({"name", "description", "cacheVariables"})


def images(text: str) -> list[tuple[str, str]]:
    """Return the `PRINTER` and `BOARD` of each firmware image `utils/presets/presets.json`
    lists, as `utils/build.py` loads it.

    The file has to be what it is today: an object holding `$schema` and `presets`, each
    preset holding exactly a unique `name`, a `description` and `cacheVariables`, with
    `PRINTER` and `BOARD` plain strings. A preset that inherits from another, or a value
    with CMake's `${...}` expansion, fails the read rather than being misread.
    """
    try:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"not JSON: {e}") from None
        if not isinstance(data, dict) or data.keys() != {"$schema", "presets"}:
            raise ValueError("the top level isn't an object of $schema and presets")
        if not isinstance(data["presets"], list) or not data["presets"]:
            raise ValueError("presets isn't a list of presets")
        found = []
        names = set()
        for preset in data["presets"]:
            if not isinstance(preset, dict) or preset.keys() != _IMAGE_KEYS:
                raise ValueError(f"a preset isn't an object of {', '.join(sorted(_IMAGE_KEYS))}")
            name, variables = preset["name"], preset["cacheVariables"]
            if not isinstance(name, str) or not name or name in names:
                raise ValueError(f"a missing or repeated preset name {name!r}")
            names.add(name)
            if not isinstance(variables, dict):
                raise ValueError(f"{name}'s cacheVariables isn't an object")
            values = []
            for variable in ("PRINTER", "BOARD"):
                value = variables.get(variable)
                if not isinstance(value, str) or not value or "$" in value:
                    raise ValueError(f"can't read {name}'s {variable} {value!r}")
                values.append(value)
            found.append((values[0], values[1]))
    except ValueError as e:
        raise Unreadable(IMAGES_FILE, str(e)) from None
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


_GAP_DECLARATION = re.compile(r"\bextruder_to_nozzle_distance\s*=(?!=)\s*([^;]*);")


def tracker_gap(text: str, macros: Macros) -> float | None:
    """Return `extruder_to_nozzle_distance` for one build, or None where it's an #error.

    The whole file is evaluated for the build. It needs exactly one active declaration and
    no active `#error`, or an active `#error` and no declaration: a build the header
    stops with `#error` can't build the filament tracker.
    """
    try:
        text = _strip_comments(text)
        if "extruder_to_nozzle_distance" not in text:
            raise ValueError("no extruder_to_nozzle_distance")
        active = "\n".join(active_lines(text.splitlines(), macros))
        values = _GAP_DECLARATION.findall(active)
        error = re.search(r"^\s*#\s*error\b", active, re.M) is not None
        if error and not values:
            return None
        if error or len(values) != 1:
            raise ValueError(
                f"{len(values)} active extruder_to_nozzle_distance declarations"
                + (" and an active #error" if error else "")
            )
        m = re.fullmatch(r"(\d+(?:\.\d*)?)f?", values[0].strip())
        if not m:
            raise ValueError(f"can't read extruder_to_nozzle_distance = {values[0].strip()}")
        return float(m.group(1))
    except ValueError as e:
        raise Unreadable(TRACKER_FILE, str(e)) from None


# What `Configuration.h` and `Configuration_adv.h` include besides the build's own file.
_CONFIGURATION_INCLUDES = frozenset({'"config.h"', "<device/board.h>"})
_CONFIGURATION_INCLUDE = re.compile(r'"Configuration_([\w.]+?)(_adv)?\.h"')


def configuration(text: str, macros: Macros, advanced: bool = False) -> str:
    """Return the `<X>` of the `Configuration_<X>.h` that `Configuration.h` includes for
    one build, or with `advanced`, of the `Configuration_<X>_adv.h` that
    `Configuration_adv.h` includes.

    The file may only hold preprocessor lines: the `#if` chain, `#pragma once` and the
    includes in `_CONFIGURATION_INCLUDES`. Exactly one `Configuration_<X>` include has to be
    active for the build, and no `#error`.
    """
    try:
        lines = _strip_comments(text).splitlines()
        for line in lines:
            if line.strip() and not line.lstrip().startswith("#"):
                raise ValueError(f"unexpected {line.strip()!r}")
        found = []
        for line in active_lines(lines, macros):
            if not line.strip():
                continue
            m = re.match(r"\s*#\s*(\w+)\s*(.*?)\s*$", line)
            if not m:
                raise ValueError(f"unexpected {line.strip()!r}")
            directive, rest = m.groups()
            if directive == "error":
                raise ValueError(f"the build reaches #error {rest}")
            if directive == "pragma":
                if rest != "once":
                    raise ValueError(f"unexpected #pragma {rest}")
                continue
            if rest in _CONFIGURATION_INCLUDES:
                continue
            m = _CONFIGURATION_INCLUDE.fullmatch(rest)
            if not m or bool(m.group(2)) != advanced:
                raise ValueError(f"unexpected #include {rest}")
            found.append(m.group(1))
        if len(found) != 1:
            raise ValueError(f"{len(found)} active Configuration includes")
    except ValueError as e:
        raise Unreadable(ADVANCED_FILE if advanced else CONFIGURATION_FILE, str(e)) from None
    return found[0]


_RAMMING_SOURCE = re.compile(r"standard_ramming_sequence_(\w+)\.cpp")
_CMAKE_SOURCE_KEYWORDS = frozenset({"PRIVATE", "PUBLIC", "INTERFACE", "FILE_SET"})


def ramming_variant(text: str, build: str, features: Mapping[str, frozenset[str]]) -> str:
    """Return the `<variant>` of the `standard_ramming_sequence_<variant>.cpp` that
    `src/feature/ramming/CMakeLists.txt` compiles into one build.

    Evaluates the file's `if()`/`elseif()`/`else()` chain, which may not nest, for the build.
    A condition may only test a feature, by name (a key of `features`, which gives the builds
    each is set for), or `PRINTER STREQUAL "<build>"`. Besides the chain, the file may only
    call `target_sources(firmware PRIVATE ...)` and `message(FATAL_ERROR ...)`, and the build
    may not reach the `message`. Exactly one ramming source has to be compiled.
    """
    try:
        # In an if(): whether a branch was taken, whether this one is active, whether
        # else() was seen. None outside one.
        chain: list[bool] | None = None
        found = []
        for name, args, line in _cmake_commands(text):
            if name == "if" and chain is None:
                value = _cmake_condition(args, build, features, line)
                chain = [value, value, False]
            elif name == "elseif" and chain is not None and not chain[2]:
                # Evaluated after a taken branch too, so an unreadable condition fails for
                # every build.
                value = _cmake_condition(args, build, features, line)
                chain = [chain[0] or value, value and not chain[0], False]
            elif name == "else" and chain is not None and not chain[2] and not args.strip():
                chain = [True, not chain[0], True]
            elif name == "endif" and chain is not None and not args.strip():
                chain = None
            elif name in ("if", "elseif", "else", "endif"):
                raise ValueError(f"unexpected {name}() on line {line}")
            else:
                active = chain is None or chain[1]
                words = _cmake_words(args)
                if name == "target_sources" and words[:2] == ["firmware", "PRIVATE"]:
                    for file in words[2:]:
                        # A variable could name a ramming source, and a later keyword
                        # starts another list with other rules.
                        if "$" in file or file in _CMAKE_SOURCE_KEYWORDS:
                            raise ValueError(f"unexpected {file} in target_sources() line {line}")
                        if "standard_ramming_sequence" not in file:
                            continue
                        m = _RAMMING_SOURCE.fullmatch(file)
                        if not m:
                            raise ValueError(f"can't read the ramming source {file}")
                        if active:
                            found.append(m.group(1))
                elif name == "message" and words[:1] == ["FATAL_ERROR"]:
                    if active:
                        raise ValueError(f"{build} reaches message(FATAL_ERROR) on line {line}")
                else:
                    raise ValueError(f"unexpected {name}() on line {line}")
        if chain is not None:
            raise ValueError("if() isn't closed")
        if len(found) != 1:
            raise ValueError(f"{len(found)} ramming sources for {build}")
    except ValueError as e:
        raise Unreadable(RAMMING_CMAKE_FILE, str(e)) from None
    return found[0]


def _cmake_condition(
    args: str, build: str, features: Mapping[str, frozenset[str]], line: int
) -> bool:
    condition = args.strip()
    m = re.fullmatch(r'PRINTER\s+STREQUAL\s+"([^"\\$]*)"', condition)
    if m:
        return build == m.group(1)
    if not _CMAKE_NAME.fullmatch(condition):
        raise ValueError(f"can't read the condition ({condition}) on line {line}")
    if condition not in features:
        raise ValueError(f"unknown feature {condition} on line {line}")
    return build in features[condition]


# The extruder's steps per mm in `Configuration_<X>.h`: what it runs at, then what feeds
# exactly the commanded length.
E_STEPS = ("DEFAULT_AXIS_STEPS_PER_UNIT_E0", "CALIBRATED_AXIS_STEPS_PER_UNIT_E0")
# The load, unload and purge lengths in `Configuration_<X>_adv.h`, in mm.
PAUSE_LENGTHS = (
    "PARK_PAUSE_PRIME_LENGTH",
    "STANDARD_RETRACT_LENGTH",
    "FILAMENT_CHANGE_UNLOAD_LENGTH",
    "FILAMENT_CHANGE_SLOW_LOAD_LENGTH",
    "FILAMENT_CHANGE_FAST_LOAD_LENGTH",
    "ADVANCED_PAUSE_PURGE_LENGTH",
)


def e_steps(text: str, macros: Macros, path: str) -> tuple[int, int]:
    """Return one build's `E_STEPS` from the `Configuration_<X>.h` at `path`."""
    try:
        default, calibrated = defines(text, E_STEPS, macros)
    except ValueError as e:
        raise Unreadable(path, str(e)) from None
    return default, calibrated


def pause_lengths(text: str, macros: Macros, path: str) -> tuple[int, ...]:
    """Return one build's `PAUSE_LENGTHS`, in order, from the `_adv.h` file at `path`."""
    try:
        return defines(text, PAUSE_LENGTHS, macros)
    except ValueError as e:
        raise Unreadable(path, str(e)) from None


def defines(text: str, names: Iterable[str], macros: Macros) -> tuple[int, ...]:
    """Return the integer each of `names` is `#define`d to for one build, in order.

    Only the `#if` branches that enclose a define of one of `names` are evaluated, so the
    rest of the file may use conditions `evaluate` can't read. Each name has to be defined
    exactly once among the build's active lines, to a plain integer, and never `#undef`ed.
    Raises ValueError otherwise.
    """
    text = _strip_comments(text).replace("\\\n", " ")
    found: dict[str, list[tuple[list[str], str]]] = {name: [] for name in names}
    for line, scope in _scoped(text):
        m = re.match(r"\s*#\s*(\w+)\s*(.*?)\s*$", line)
        if not m or m.group(1) not in ("define", "undef"):
            continue
        name, value = re.match(r"(\w*)(.*)", m.group(2)).groups()  # type: ignore[union-attr]
        if name not in found:
            continue
        if m.group(1) == "undef":
            raise ValueError(f"#undef {name}")
        found[name].append((scope, value))
    values = []
    for name, defined in found.items():
        if not defined:
            raise ValueError(f"no #define {name}")
        active = [value for scope, value in defined if active_lines(scope, macros)]
        if len(active) != 1:
            raise ValueError(f"{name} is defined {len(active)} times for the build")
        if not re.fullmatch(r"-?\d+", active[0].strip()):
            raise ValueError(f"can't read #define {name} {active[0].strip()}")
        values.append(int(active[0]))
    return tuple(values)


_IF_FAMILY = re.compile(r"\s*#\s*(?:if|ifdef|ifndef|elif|else|endif)\b")


def _scoped(text: str) -> list[tuple[str, list[str]]]:
    """Pair each line outside the `#if` family with the lines `active_lines` needs to decide
    that line alone: the `#if`, `#elif` and `#else` lines of the branches around it, a
    stand-in for the line, and an `#endif` for each open `#if`.

    `active_lines` of a line's scope is empty exactly when the line is inactive, and
    evaluates only those conditions, so the rest of the file may use conditions `evaluate`
    can't read.
    """
    # Per open #if, its directive lines so far: the #if and any #elif or #else after it.
    open_ifs: list[list[str]] = []
    scoped = []
    for line in text.splitlines():
        m = re.match(r"\s*#\s*(\w+)", line)
        directive = m.group(1) if m else None
        if directive in ("if", "ifdef", "ifndef"):
            open_ifs.append([line])
        elif directive in ("elif", "else"):
            if not open_ifs:
                raise ValueError(f"#{directive} without #if")
            open_ifs[-1].append(line)
        elif directive == "endif":
            if not open_ifs:
                raise ValueError("#endif without #if")
            open_ifs.pop()
        else:
            enclosing = [d for branches in open_ifs for d in branches]
            scoped.append((line, [*enclosing, "line", *["#endif"] * len(open_ifs)]))
    if open_ifs:
        raise ValueError("#if without #endif")
    return scoped


_FEATURE_NAME = re.compile(
    r'inline\s+constexpr\s+const\s+char\s*\*\s*(m862_6_\w+)\s*=\s*"([^"\\]*)"\s*;'
)


def feature_names(text: str, macros: Macros) -> frozenset[str]:
    """Return the `M862.6` feature names one build knows: the `gcode_info::m862_6_*`
    constants its preprocessor keeps in `gcode_info.hpp`.

    Only the `#if` branches around those constants are evaluated. Any other line that names
    one fails the read, so a constant declared another way can't go unseen.
    """
    try:
        found: dict[str, str] = {}
        for line, scope in _scoped(_strip_comments(text)):
            if "m862_6_" not in line:
                continue
            m = _FEATURE_NAME.fullmatch(line.strip())
            if not m:
                raise ValueError(f"can't read {line.strip()!r}")
            if not active_lines(scope, macros):
                continue
            constant, name = m.groups()
            if constant in found:
                raise ValueError(f"{constant} is declared twice for the build")
            found[constant] = name
        if not found:
            raise ValueError("no M862.6 feature names")
    except ValueError as e:
        raise Unreadable(GCODE_INFO_FILE, str(e)) from None
    return frozenset(found.values())


# The `StandardRammingSequence` cases (`standard_ramming_sequence.hpp`).
RAMMING_CASES = frozenset({"auto_retract", "runout", "unload"})
Step = tuple[int, int]  # a ramming step: E in mm, feedrate in mm/min

_RAMMING_ARRAY = (
    r"static\s+constexpr\s+(?:buddy::)?RammingSequenceArray\s+seq\s*\(\s*\{(.*?)\}\s*\)\s*;"
)
_RAMMING_CASE = re.compile(r"\s*((?:case\s+StandardRammingSequence::\w+\s*:\s*)+)\{")
_RAMMING_BODY = re.compile(
    rf"\s*(?:if\s*\(\s*is_flexible\s*\)\s*\{{\s*{_RAMMING_ARRAY}\s*return\s+seq\s*;\s*\}}\s*)?"
    rf"{_RAMMING_ARRAY}\s*(?:static_assert\s*\([^;]*\)\s*;\s*)?return\s+seq\s*;\s*",
    re.S,
)
_RAMMING_STEP = re.compile(r"\s*\{\s*(-?\d+)\s*,\s*(\d+)\s*\}\s*(?:,|$)")
_RAMMING_FUNCTION = re.compile(r"\bstandard_ramming_sequence\s*\([^()]*\)\s*\{")
# What the files hold around the `switch (seq)`. Anything else, such as a test that returns
# another sequence before the switch, could change which sequence a build uses.
_RAMMING_BEFORE_SWITCH = (
    "",
    "const bool is_flexible = FilamentType::for_tool_heuristic(virtual_tool)"
    ".parameters().is_flexible;",
    "[[maybe_unused]] const bool is_high_flow_nozzle = config_store()"
    ".get_nozzle_is_high_flow(virtual_tool.to_physical());",
)
_RAMMING_AFTER_SWITCH = "bsod_unreachable();"


def ramming(text: str, macros: Macros, path: str) -> dict[str, tuple[Step, ...]]:
    """Return one build's standard ramming sequences, by case name, from the file at `path`.

    Reads the `switch (seq)` in `standard_ramming_sequence(...)`, which may only be preceded
    by a known declaration and followed by `bsod_unreachable();`. Where a case returns
    another sequence for flexible filament first, that one is also given as
    `<name>_flexible`. The cases have to be `runout`, `unload` and, on a build with
    `HAS_AUTO_RETRACT`, `auto_retract`.
    """
    try:
        text = "\n".join(active_lines(_strip_comments(text).splitlines(), macros))
        functions = list(_RAMMING_FUNCTION.finditer(text))
        if len(functions) != 1:
            raise ValueError(f"{len(functions)} standard_ramming_sequence definitions")
        function = text[functions[0].end() : _closing_brace(text, functions[0].end() - 1) - 1]
        switches = list(re.finditer(r"switch\s*\(\s*seq\s*\)\s*\{", function))
        if len(switches) != 1:
            raise ValueError(f"{len(switches)} switch (seq) statements")
        start = switches[0].end()
        stop = _closing_brace(function, start - 1)
        before = _tokens(function[: switches[0].start()])
        if before not in map(_tokens, _RAMMING_BEFORE_SWITCH):
            raise ValueError("unexpected code before switch (seq)")
        if _tokens(function[stop:]) != _tokens(_RAMMING_AFTER_SWITCH):
            raise ValueError("unexpected code after switch (seq)")
        body = function[start : stop - 1]
        result: dict[str, tuple[Step, ...]] = {}
        pos = 0
        while body[pos:].strip():
            m = _RAMMING_CASE.match(body, pos)
            if not m:
                raise ValueError(f"unexpected {body[pos:].split()[0]!r} in switch (seq)")
            end = _closing_brace(body, m.end() - 1)
            arrays = _RAMMING_BODY.fullmatch(body, m.end(), end - 1)
            if not arrays:
                raise ValueError(f"can't read the case block {m.group(1).split()[-1]}")
            flexible, normal = arrays.groups()
            for name in re.findall(r"::(\w+)", m.group(1)):
                if name not in RAMMING_CASES:
                    raise ValueError(f"unknown ramming sequence {name}")
                if name in result:
                    raise ValueError(f"ramming sequence {name} appears twice")
                result[name] = _ramming_steps(normal, name)
                if flexible is not None:
                    result[f"{name}_flexible"] = _ramming_steps(flexible, name)
            pos = end
        wanted = {"runout", "unload"}
        if evaluate("HAS_AUTO_RETRACT()", macros):
            wanted.add("auto_retract")
        cases = {n for n in result if n in RAMMING_CASES}
        if cases != wanted:
            listed, expected = (", ".join(sorted(s)) for s in (cases, wanted))
            raise ValueError(f"the cases are {listed}, not {expected}")
    except ValueError as e:
        raise Unreadable(path, str(e)) from None
    return result


def _ramming_steps(text: str, name: str) -> tuple[Step, ...]:
    steps = []
    pos = 0
    for m in _RAMMING_STEP.finditer(text):
        if m.start() != pos:
            break
        steps.append((int(m.group(1)), int(m.group(2))))
        pos = m.end()
    # The firmware aborts the build on a feedrate that isn't positive (`ramming_sequence.hpp`).
    if not steps or text[pos:].strip() or any(fr <= 0 for _, fr in steps):
        raise ValueError(f"can't read the steps of ramming sequence {name}")
    return tuple(steps)


def cleaner_purge(text: str, macros: Macros) -> tuple[float, bool]:
    """Return how far one build's nozzle cleaner `purge_clean` G-code extrudes, and whether
    it runs `M1705` (auto-retract) after that.

    Only `G750` may move E, and an `M1705` has to come after the last E move.
    """
    try:
        text = "\n".join(active_lines(_strip_comments(text).splitlines(), macros))
        entry = _one(
            r"\{\s*Sequence::purge_clean\s*,\s*\{(.*?)\}\s*\}", text, "Sequence::purge_clean"
        )
        literals, rest = _one(
            r'\.default_gcode\s*=\s*((?:"[^"]*"\s*)+)(?=(.*))', entry, ".default_gcode", 2
        )
        # Only a trailing comma may follow, so a macro between the literals can't hide G-code.
        if not re.fullmatch(r",?\s*", rest, re.S):
            raise ValueError("unexpected code after the .default_gcode literals")
        gcode = "".join(re.findall(r'"([^"]*)"', literals))
        if re.search(r"\\(?!n)", gcode):
            raise ValueError("an escape other than \\n in purge_clean")
        extruded = 0.0
        last_move = retract = None
        for i, line in enumerate(gcode.split("\\n")):
            words = line.split()
            if words[:1] == ["M1705"] and retract is None:
                retract = i
            moves = [w for w in words if w.startswith("E")]
            if not moves:
                continue
            m = re.fullmatch(r"E(-?\d+(?:\.\d*)?)", moves[0])
            if words[0] != "G750" or len(moves) != 1 or not m:
                raise ValueError(f"can't read the E move {line.strip()!r} in purge_clean")
            extruded += float(m.group(1))
            last_move = i
        if retract is not None and (last_move is None or retract < last_move):
            raise ValueError("M1705 before the last E move in purge_clean")
    except ValueError as e:
        raise Unreadable(CLEANER_FILE, str(e)) from None
    return extruded, retract is not None


_MM = r"(\d+(?:\.\d*)?)[fF]?"
_AUTO_RETRACT_CONSTANTS = {
    "full_retract_distance": re.compile(
        rf"static\s+constexpr\s+float\s+full_retract_distance\s*=\s*{_MM}\s*;"
    ),
    "supports_cold_unload": re.compile(
        r"static\s+constexpr\s+bool\s+supports_cold_unload\s*=\s*(true|false)\s*;"
    ),
}


def auto_retract(text: str, macros: Macros) -> tuple[float, bool]:
    """Return one build's `AutoRetract::full_retract_distance` and `supports_cold_unload`.

    Only the `#if` branches around the two constants and any `#error` are evaluated. Each
    has to be declared exactly once for the build, and the build may not reach an `#error`.
    Any other line that names one fails the read.
    """
    try:
        found: dict[str, str] = {}
        for line, scope in _scoped(_strip_comments(text).replace("\\\n", " ")):
            names = [n for n in _AUTO_RETRACT_CONSTANTS if re.search(rf"\b{n}\b", line)]
            error = re.match(r"\s*#\s*error\b", line)
            if not names and not error:
                continue
            m = None
            if len(names) == 1:
                m = _AUTO_RETRACT_CONSTANTS[names[0]].fullmatch(line.strip())
            if names and not m:
                raise ValueError(f"can't read {line.strip()!r}")
            if not active_lines(scope, macros):
                continue
            if error:
                raise ValueError(f"the build reaches {line.strip()}")
            if names[0] in found:
                raise ValueError(f"{names[0]} is declared twice for the build")
            found[names[0]] = m.group(1)  # type: ignore[union-attr]
        missing = [n for n in _AUTO_RETRACT_CONSTANTS if n not in found]
        if missing:
            raise ValueError(f"no {', '.join(missing)} for the build")
    except ValueError as e:
        raise Unreadable(AUTO_RETRACT_FILE, str(e)) from None
    return float(found["full_retract_distance"]), found["supports_cold_unload"] == "true"


# The two bodies of `Pause::unload_wait_temp_process` a build may compile: the unload
# purges first, or goes straight to the ramming.
_UNLOAD_WAIT_BODIES = {
    "if (!ensureSafeTemperatureNotifyProgress()) { return; } set(LoadState::unload_purge);": True,
    "if (!ensureSafeTemperatureNotifyProgress()) { return; } set(LoadState::ram_sequence);": False,
}
# The body of `Pause::unload_purge_process`, around the purge's length.
_UNLOAD_PURGE_BODY = (
    "setPhase(PhasesLoadUnload::Purging_unstoppable); static constexpr float unload_purge_length =",
    "; std::ignore = do_e_move_notify_progress_hotextrude(unload_purge_length,"
    " standard_feedrates::extruder(standard_feedrates::Extruder::advanced_pause_purge,"
    " FilamentType::for_tool_heuristic(settings.virtual_tool())), StopConditions::UserStopped);"
    " set(LoadState::ram_sequence);",
)
# Where the purge's state may appear outside the body above: the progress tables' steps.
_UNLOAD_PURGE_STEP = re.compile(r"\{\s*LoadState::unload_purge\s*,\s*\d+\s*\}")


def unload_purge(text: str, macros: Macros) -> float:
    """Return how far one build purges before an unload's ramming, in mm: the
    `unload_purge_length` of `Pause::unload_purge_process`, or 0 where the build doesn't
    reach it. A build without `HAS_PAUSE` purges 0: `src/marlin_stubs/pause/CMakeLists.txt`
    compiles `pause.cpp` only with it.

    Each of `Pause::unload_wait_temp_process` and `Pause::unload_purge_process` has to be
    defined once. The build's text of each body has to be a known one, apart from the
    purge's length, so a condition, an early return or another move fails the read. The
    wait has to be compiled, and it decides whether the unload purges. The purge function
    has to be compiled exactly when the wait goes to it. `LoadState::unload_purge` may
    appear elsewhere only as a progress table's step, so there's no other way in. Only the
    `#if` branches around and inside the two functions are evaluated.
    """
    try:
        if not evaluate("HAS_PAUSE()", macros):
            return 0.0
        text = _strip_comments(text).replace("\\\n", " ")
        wait = _pause_body(text, "unload_wait_temp_process", macros)
        purge = _pause_body(text, "unload_purge_process", macros)
        if wait is None:
            raise ValueError("the build doesn't compile Pause::unload_wait_temp_process")
        reached = {_tokens(b): r for b, r in _UNLOAD_WAIT_BODIES.items()}.get(_tokens(wait[1]))
        if reached is None:
            raise ValueError("unexpected code in Pause::unload_wait_temp_process")
        start, end = wait[0]
        outside = _UNLOAD_PURGE_STEP.sub("", text[:start] + text[end:])
        if re.search(r"\bLoadState::unload_purge\b", outside):
            raise ValueError("LoadState::unload_purge outside Pause::unload_wait_temp_process")
        if (purge is not None) != reached:
            raise ValueError(
                "the build compiles Pause::unload_purge_process but doesn't reach it"
                if purge
                else "the build reaches Pause::unload_purge_process but doesn't compile it"
            )
        if purge is None:
            return 0.0
        before, after = (_tokens(part) for part in _UNLOAD_PURGE_BODY)
        body = _tokens(purge[1])
        value = body[len(before) : len(body) - len(after)].replace(" ", "")
        m = re.fullmatch(_MM, value)
        if not (body.startswith(before + " ") and body.endswith(" " + after) and m):
            raise ValueError("unexpected code in Pause::unload_purge_process")
    except ValueError as e:
        raise Unreadable(PAUSE_FILE, str(e)) from None
    return float(m.group(1))


_MINIMAL_PURGE = re.compile(
    rf"static\s+constexpr\s+(?:const\s+)?float\s+minimal_purge\s*=\s*{_MM}\s*;"
)


def minimal_purge(text: str) -> float:
    """Return `pause::Settings::minimal_purge`, the shortest purge a load runs, in mm.

    The one line that names it has to declare it to a plain number, outside any `#if`, so
    it's the same for every build. Any other line that names it fails the read.
    """
    try:
        found = []
        for line, scope in _scoped(_strip_comments(text).replace("\\\n", " ")):
            if not re.search(r"\bminimal_purge\b", line):
                continue
            m = _MINIMAL_PURGE.fullmatch(line.strip())
            if not m:
                raise ValueError(f"can't read {line.strip()!r}")
            if scope != ["line"]:
                raise ValueError("minimal_purge is declared inside an #if")
            found.append(m.group(1))
        if len(found) != 1:
            raise ValueError(f"{len(found)} minimal_purge declarations")
    except ValueError as e:
        raise Unreadable(PAUSE_SETTINGS_FILE, str(e)) from None
    return float(found[0])


def _pause_body(text: str, name: str, macros: Macros) -> tuple[tuple[int, int], str] | None:
    """Return where `Pause::<name>`'s definition is in `text` and the build's text of its
    body, or None where the build doesn't compile it."""
    found = list(re.finditer(rf"\bvoid\s+Pause::{name}\s*\([^()]*\)\s*\{{", text))
    if len(found) != 1:
        raise ValueError(f"{len(found)} definitions of Pause::{name}")
    m = found[0]
    if not active_lines(_scope_at(text, m.start()), macros):
        return None
    end = _closing_brace(text, m.end() - 1)
    body = text[m.end() : end - 1]
    return (m.start(), end), "\n".join(active_lines(body.splitlines(), macros))


def _scope_at(text: str, pos: int) -> list[str]:
    """Return what `_scoped` pairs with the line holding `pos`, which isn't a directive."""
    lines = text.splitlines()
    number = text.count("\n", 0, pos)
    index = sum(1 for line in lines[:number] if not _IF_FAMILY.match(line))
    return _scoped(text)[index][1]


def _tokens(code: str) -> str:
    """Return `code`'s words and other characters split by single spaces, so two pieces of
    code compare equal whatever their spacing."""
    return " ".join(re.findall(r"\w+|\S", code))


def _closing_brace(text: str, start: int) -> int:
    """Return the index just past the `}` that closes the `{` at `start`."""
    depth = 0
    for i in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return i + 1
    raise ValueError("unbalanced braces")


# Directives kept as lines when active, for the caller to see or ignore.
_KEPT_DIRECTIVES = frozenset({"error", "include", "pragma"})


def active_lines(lines: Iterable[str], macros: Macros) -> list[str]:
    """Return the lines the preprocessor keeps for one build.

    Handles `#if`, `#elif`, `#else` and `#endif` with conditions `evaluate` reads, and
    `#ifdef` and `#ifndef` on `UNITTESTS`, which no firmware build defines. Keeps active
    `#error`, `#include` and `#pragma` lines for the caller to see. Any other directive is
    an error.
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
        if directive in ("if", "ifdef", "ifndef"):
            if directive == "if":
                value = bool(evaluate(rest, macros))
            elif rest == "UNITTESTS":  # defined only in the firmware's unit-test builds
                value = directive == "ifndef"
            else:
                raise ValueError(f"unexpected #{directive} {rest}")
            outer = active()
            taken = value and outer
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
        elif directive in _KEPT_DIRECTIVES:
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
_CMAKE_SET_KEYWORDS = frozenset({"CACHE", "PARENT_SCOPE"})
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


def _cmake_statements(text: str) -> Iterable[tuple[str, list[str], int, str | None]]:
    """Yield each command other than block openers and closers as (lowercase name, words,
    line number, the innermost enclosing block such as "if() on line 3", or None)."""
    blocks: list[tuple[str, int]] = []
    for name, args, line in _cmake_commands(text):
        if name in _CMAKE_OPENERS:
            blocks.append((name, line))
        elif name in _CMAKE_CLOSERS:
            if not blocks:
                raise ValueError(f"{name}() on line {line} closes nothing")
            blocks.pop()
        else:
            block = f"{blocks[-1][0]}() on line {blocks[-1][1]}" if blocks else None
            yield name, _cmake_words(args), line, block
    if blocks:
        opener, at = blocks[-1]
        raise ValueError(f"{opener}() on line {at} isn't closed")


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
