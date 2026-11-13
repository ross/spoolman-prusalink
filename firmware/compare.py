"""Rebuild the firmware's tables per printer model and compare them with the copies.

The bridge (`spoolman_prusalink.printer_sync`) and the simulator (`simulator.model`,
`simulator.tool`) each hold hand-copied tables from Prusa-Firmware-Buddy: the printer
models, the preset filaments, the reset defaults and the tag aliases. This module reads
the same tables from the firmware source for each firmware build and lists every place
a copy differs. A difference on `EXPECTED` is reported but doesn't fail the check.

Each build also carries its extruder numbers (steps per mm, filament change lengths,
ramming sequences and the nozzle cleaner's purge), which nothing compares yet.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from firmware import read
from firmware.source import Source, Unreadable

# Printer type code (`/api/version`'s `printer`) to the firmware build that runs it: the
# `PRINTER` value in `CMakeLists.txt` and `ProjectOptions.cmake`. No file maps one to the
# other directly: the MK4 and MK3.5 builds pick their model at runtime from
# `extended_printer_type.hpp`, as does the XL build for the XLP (`xls`), and the Core One
# Oak is the COREONE build with `SIGNATURE_OAK` (`printer_model.cpp`). The MK3 and MK3S
# (1.3.0, 1.3.1) are listed for G-code compatibility only; no build runs them. A code in
# `printer_model_data.hpp` that's missing here fails the read.
MODEL_BUILDS: dict[str, str | None] = {
    "1.3.0": None,
    "1.3.1": None,
    "1.3.5": "MK3.5",
    "1.3.6": "MK3.5",
    "1.3.9": "MK4",
    "1.3.10": "MK4",
    "1.4.0": "MK4",
    "1.4.1": "MK4",
    "2.1.0": "MINI",
    "3.1.0": "XL",
    "3.1.1": "XL",
    "5.1.0": "XL_DEV_KIT",
    "4.1.0": "iX",
    "7.1.0": "COREONE",
    "7.2.0": "COREONE",
    "8.1.0": "COREONEL",
    "7.10.0": "COREONE_INDX",
    "8.10.0": "COREONEL_INDX",
}

# The builds each `PRINTER_IS_PRUSA_<X>()` is true for, as `include/printers.h` defines
# them: COREONE and COREONEL also cover their INDX builds, and XL covers the dev kit.
PRINTER_IS = {
    "MK4": {"MK4"},
    "MK3_5": {"MK3.5"},
    "MINI": {"MINI"},
    "XL": {"XL", "XL_DEV_KIT"},
    "iX": {"iX"},
    "XL_DEV_KIT": {"XL_DEV_KIT"},
    "COREONE": {"COREONE", "COREONE_INDX"},
    "COREONEL": {"COREONEL", "COREONEL_INDX"},
}

# Each build's `include/marlin/Configuration_<X>.h` and `Configuration_<X>_adv.h`, as the
# `#elif` chains in `Configuration.h` and `Configuration_adv.h` pick them by
# `PRINTER_IS_PRUSA_<X>()`: the INDX builds share their base model's files, which branch
# on `HAS_INDX()` inside. The XL build is the main board's; `Configuration_XL_Dwarf.h` is
# the tool head's.
CONFIGURATIONS = {
    "MINI": "MINI",
    "MK3.5": "MK3.5",
    "MK4": "MK4",
    "XL": "XL",
    "XL_DEV_KIT": "XL_DEV_KIT",
    "iX": "iX",
    "COREONE": "COREONE",
    "COREONE_INDX": "COREONE",
    "COREONEL": "COREONEL",
    "COREONEL_INDX": "COREONEL",
}

# The `ProjectOptions.cmake` features the comparison, the readers and the simulator's
# printer profiles use. A macro outside this list fails the read.
FEATURES = (
    "HAS_LOADCELL",
    "HAS_INDX",
    "HAS_INDX_HEAD",
    "HAS_TOOLCHANGER",
    "HAS_FILAMENT_TRACKER",
    "HAS_ANFC",
    "HAS_NEXTRUDER",
    "HAS_CHAMBER_API",
    "HAS_CHAMBER_FILTRATION_API",
    "HAS_HT_HOTEND",
    "HAS_FILAMENT_HEATBREAK_PARAM",
    "HAS_PAUSE",
    "HAS_AUTO_RETRACT",
    "HAS_SWITCHABLE_AUTO_RETRACT",
    "HAS_NOZZLE_CLEANER",
    "HAS_MMU2",
)

Ramming = tuple[read.Step, ...]


def retracted_distance(sequence: Iterable[read.Step]) -> int:
    """Return how far a ramming sequence leaves the filament retracted, in mm.

    As `RammingSequence::init` in `ramming_sequence.hpp` works it out: a negative E
    retracts, a positive one pushes back, and pushing past the nozzle resets it to 0.
    """
    distance = 0
    for e, _feedrate in sequence:
        distance = max(0, distance - e)
    return distance


@dataclass(frozen=True)
class Pause:
    """A build's filament change lengths in `Configuration_<X>_adv.h`, in mm.

    The fields follow `read.PAUSE_LENGTHS`.
    """

    prime_mm: int
    retract_mm: int
    unload_mm: int
    slow_load_mm: int
    fast_load_mm: int
    purge_mm: int


@dataclass(frozen=True)
class CleanerPurge:
    """What the nozzle cleaner's `purge_clean` G-code does to the filament."""

    e_mm: float
    auto_retract: bool  # whether it runs M1705 after extruding


@dataclass(frozen=True)
class Build:
    name: str
    features: frozenset[str]
    tools: int
    gap_mm: float | None  # None where the filament tracker can't build
    presets: Mapping[str, Mapping[str, read.Value]]
    e_steps: tuple[int, int]  # the extruder's default and calibrated steps per mm
    pause: Pause
    # By `StandardRammingSequence` case, with `<case>_flexible` where flexible filament
    # gets another sequence.
    ramming: Mapping[str, Ramming]
    cleaner_purge: CleanerPurge | None  # None without HAS_NOZZLE_CLEANER

    @property
    def counters(self) -> bool:
        return "HAS_FILAMENT_TRACKER" in self.features

    @property
    def uuids(self) -> bool:
        return "HAS_ANFC" in self.features

    @property
    def feed_ratio(self) -> float:
        """How much filament an E move feeds per mm commanded (`planner.cpp`)."""
        default, calibrated = self.e_steps
        return default / calibrated


@dataclass(frozen=True)
class Firmware:
    commit: str
    models: Mapping[str, str]  # type code to `id_str`, built or not
    builds: Mapping[str, Build]
    defaults: tuple[int, int, int]  # what `M865 R` resets nozzle, preheat and bed to
    aliases: Mapping[str, str]

    def built(self) -> dict[str, Build]:
        """Return each type code some build runs, with that build."""
        return {
            code: self.builds[build]
            for code in self.models
            if (build := MODEL_BUILDS[code]) is not None
        }


def tools(features: frozenset[str]) -> int:
    """Return how many tools a build drives.

    Mirrors `EXTRUDERS` in `include/marlin/Configuration_*.h`, less the "no tool" entry.
    The MMU3 is ignored on purpose: it's a runtime add-on to a single-tool build, and the
    copies treat those builds as single-tool.
    """
    if "HAS_INDX" in features:
        return 8
    if "HAS_TOOLCHANGER" in features:
        return 5
    return 1


def ramming_file(build: str, features: frozenset[str]) -> str:
    """Return the standard ramming sequence file a build compiles.

    Mirrors the `if()` chain in `src/feature/ramming/CMakeLists.txt`.
    """
    if "HAS_NEXTRUDER" in features:
        return read.ramming_file("nextruder")
    if "HAS_INDX" in features:
        return read.ramming_file("indx")
    variants = {"MINI": "mini", "MK3.5": "mk3_5"}
    if build not in variants:
        raise Unreadable(read.RAMMING_CMAKE_FILE, f"no ramming sequence for {build}")
    return read.ramming_file(variants[build])


def macros(build: str, features: Mapping[str, frozenset[str]]) -> read.Macros:
    """Return the macro values for `build`; an unknown macro raises KeyError."""

    def value(name: str) -> bool:
        if name.startswith("PRINTER_IS_PRUSA_"):
            return build in PRINTER_IS[name.removeprefix("PRINTER_IS_PRUSA_")]
        return build in features[name]

    return value


def load(source: Source) -> Firmware:
    """Read the firmware's tables from `source` and evaluate them for every build."""
    models = read.printer_models(source.read(read.MODELS_FILE))
    unknown = sorted(models.keys() - MODEL_BUILDS.keys(), key=_version)
    if unknown:
        listed = ", ".join(f"{code} ({models[code]})" for code in unknown)
        raise Unreadable(read.MODELS_FILE, f"no build for {listed}; add it to MODEL_BUILDS")
    options = source.read(read.OPTIONS_FILE)
    features = read.features(options, FEATURES)
    # A build `PRINTER` can't name would read as a build with no features.
    unknown = sorted({b for b in MODEL_BUILDS.values() if b} - read.printers(options))
    if unknown:
        raise Unreadable(read.OPTIONS_FILE, f"PRINTER_VALID_OPTS doesn't list {', '.join(unknown)}")
    presets_text = source.read(read.PRESETS_FILE)
    tracker_text = source.read(read.TRACKER_FILE)
    # Several builds share each of the files below.
    text = functools.cache(source.read)
    builds = {}
    for name in sorted({b for b in MODEL_BUILDS.values() if b is not None}):
        has = frozenset(f for f, printers in features.items() if name in printers)
        values = macros(name, features)
        configuration = read.configuration_file(CONFIGURATIONS[name])
        advanced = read.advanced_file(CONFIGURATIONS[name])
        ramming = ramming_file(name, has)
        cleaner = None
        if "HAS_NOZZLE_CLEANER" in has:
            cleaner = CleanerPurge(*read.cleaner_purge(text(read.CLEANER_FILE), values))
        builds[name] = Build(
            name=name,
            features=has,
            tools=tools(has),
            gap_mm=read.tracker_gap(tracker_text, values),
            presets=read.presets(presets_text, values),
            e_steps=read.e_steps(text(configuration), values, configuration),
            pause=Pause(*read.pause_lengths(text(advanced), values, advanced)),
            ramming=read.ramming(text(ramming), values, ramming),
            cleaner_purge=cleaner,
        )
    return Firmware(
        commit=source.commit,
        models=models,
        builds=builds,
        defaults=read.reset_defaults(source.read(read.FILAMENT_FILE)),
        aliases=read.aliases(source.read(read.ALIASES_FILE)),
    )


@dataclass(frozen=True)
class Copies:
    """The bridge's and the simulator's copies of the tables."""

    bridge_models: Mapping[str, Any]  # printer_sync.PrinterModel
    no_counters: str
    multi_tool: str
    bridge_presets: Mapping[str, Any]  # printer_sync.Preset
    ht_idler_door: frozenset[str]
    bridge_aliases: Mapping[str, str]
    default_preheat: int
    preset_preheat: int  # the `Preset` dataclass's preheat default
    sim_models: Mapping[str, Any]  # simulator.model.ModelInfo
    sim_presets: Mapping[str, tuple[int, int, int]]
    sim_defaults: tuple[int, int, int]
    sim_aliases: Mapping[str, str]

    @classmethod
    def current(cls) -> Copies:
        from simulator import model, tool
        from spoolman_prusalink import printer_sync as bridge

        preheat = next(f.default for f in fields(bridge.Preset) if f.name == "preheat")
        return cls(
            bridge_models=bridge.PRINTER_MODELS,
            no_counters=bridge._NO_COUNTERS,
            multi_tool=bridge._MULTI_TOOL,
            bridge_presets=bridge.PRESETS,
            ht_idler_door=bridge.HT_IDLER_DOOR,
            bridge_aliases=bridge.PRESET_ALIASES,
            default_preheat=bridge.DEFAULT_PREHEAT,
            preset_preheat=preheat,  # type: ignore[arg-type]
            sim_models=model.MODELS,
            sim_presets=tool.PRESET_TEMPERATURES,
            sim_defaults=tool.DEFAULT_TEMPERATURES,
            sim_aliases=tool.PRESET_ALIASES,
        )


class _Marker:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


# Whether a table has an entry, where the other side has or lacks it.
MISSING: Any = _Marker("missing")
PRESENT: Any = _Marker("present")
ANY: Any = _Marker("any")  # an `Expected` value that matches whatever a difference has


@dataclass(frozen=True)
class Difference:
    table: str
    model: str | None  # a type code, or None for a table not kept per model
    material: str | None
    field: str
    firmware: Any
    copy: Any

    def __str__(self) -> str:
        where = " ".join(p for p in (self.model, self.material, self.field) if p)
        return f"{where}: firmware {self.firmware!r}, copy {self.copy!r}"


# The tables compared, in report order.
TABLES = (
    "bridge PRINTER_MODELS",
    "bridge PRESETS",
    "bridge HT_IDLER_DOOR",
    "bridge DEFAULT_PREHEAT",
    "bridge PRESET_ALIASES",
    "simulator MODELS",
    "simulator PRESET_TEMPERATURES",
    "simulator DEFAULT_TEMPERATURES",
    "simulator PRESET_ALIASES",
)


def compare(firmware: Firmware, copies: Copies) -> list[Difference]:
    """Return every difference between the firmware's tables and the copies."""
    found: list[Difference] = []

    def differ(table: str, model: str | None, material: str | None, field: str, fw, copy):
        if fw != copy:
            found.append(Difference(table, model, material, field, fw, copy))

    built = firmware.built()
    _compare_bridge_models(built, firmware.models, copies, differ)
    _compare_sim_models(built, firmware.models, copies, differ)

    # The presets of each model the bridge writes to. Both copies hold one table for
    # every model; a material only some builds have (PPS and PPA) may be in a copy.
    supported = sorted(
        (c for c, m in copies.bridge_models.items() if m.unsupported is None and c in built),
        key=_version,
    )
    anywhere = {m for b in firmware.builds.values() for m in b.presets}
    for code in supported:
        build = built[code]
        for material, fw in build.presets.items():
            _compare_bridge_preset(code, material, fw, copies.bridge_presets, differ)
            _compare_sim_preset(code, material, fw, copies.sim_presets, differ)
    for material in sorted(copies.bridge_presets.keys() - anywhere):
        differ("bridge PRESETS", None, material, "material", MISSING, PRESENT)
    for material in sorted(copies.sim_presets.keys() - anywhere):
        differ("simulator PRESET_TEMPERATURES", None, material, "material", MISSING, PRESENT)
    door = {
        m
        for code in supported
        for m, fw in built[code].presets.items()
        if fw.get("requires_ht_idler_door")
    }
    differ(
        "bridge HT_IDLER_DOOR", None, None, "materials", sorted(door), sorted(copies.ht_idler_door)
    )

    preheat = firmware.defaults[1]
    differ("bridge DEFAULT_PREHEAT", None, None, "DEFAULT_PREHEAT", preheat, copies.default_preheat)
    differ("bridge DEFAULT_PREHEAT", None, None, "Preset.preheat", preheat, copies.preset_preheat)
    differ(
        "simulator DEFAULT_TEMPERATURES",
        None,
        None,
        "nozzle, preheat, bed",
        firmware.defaults,
        tuple(copies.sim_defaults),
    )
    for table, aliases in (
        ("bridge PRESET_ALIASES", copies.bridge_aliases),
        ("simulator PRESET_ALIASES", copies.sim_aliases),
    ):
        for name in sorted(firmware.aliases.keys() | aliases.keys()):
            differ(
                table,
                None,
                name,
                "alias",
                firmware.aliases.get(name, MISSING),
                aliases.get(name, MISSING),
            )
    return found


Differ = Callable[[str, str | None, str | None, str, Any, Any], None]


def _compare_bridge_models(
    built: Mapping[str, Build], names: Mapping[str, str], copies: Copies, differ: Differ
) -> None:
    table = "bridge PRINTER_MODELS"
    for code in sorted(built.keys() | copies.bridge_models.keys(), key=_version):
        build, model = built.get(code), copies.bridge_models.get(code)
        if build is None or model is None:
            differ(
                table,
                code,
                None,
                "model",
                names[code] if build else MISSING,
                model.name if model else MISSING,
            )
            continue
        differ(table, code, None, "name", names[code], model.name)
        differ(table, code, None, "uuids", build.uuids, model.uuids)
        # The reason the bridge gives has to be true. A model the bridge can't serve has
        # to be refused, for that reason or another: it keeps one reason per model.
        reason = model.unsupported
        label = {
            copies.no_counters: "_NO_COUNTERS",
            copies.multi_tool: "_MULTI_TOOL",
            None: "supported",
        }
        shown = label.get(reason, reason)
        if reason == copies.no_counters and build.counters:
            differ(table, code, None, "unsupported", "has the filament tracker", shown)
        if reason == copies.multi_tool and build.tools == 1:
            differ(table, code, None, "unsupported", "1 tool", shown)
        if reason is None and not build.counters:
            differ(table, code, None, "unsupported", "no filament tracker", shown)
        if reason is None and build.tools > 1:
            differ(table, code, None, "unsupported", f"{build.tools} tools", shown)


def _compare_sim_models(
    built: Mapping[str, Build], names: Mapping[str, str], copies: Copies, differ: Differ
) -> None:
    table = "simulator MODELS"
    for code in sorted(built.keys() | copies.sim_models.keys(), key=_version):
        build, model = built.get(code), copies.sim_models.get(code)
        if build is None or model is None:
            differ(
                table,
                code,
                None,
                "model",
                names[code] if build else MISSING,
                model.name if model else MISSING,
            )
            continue
        differ(table, code, None, "name", names[code], model.name)
        differ(table, code, None, "uuids", build.uuids, model.uuids)
        differ(table, code, None, "counters", build.counters, model.counters)
        differ(table, code, None, "max_tools", build.tools, model.max_tools)
        if build.gap_mm is not None:
            differ(table, code, None, "gap_mm", build.gap_mm, model.gap_mm)


def _compare_bridge_preset(
    code: str,
    material: str,
    fw: Mapping[str, read.Value],
    presets: Mapping[str, Any],
    differ: Differ,
) -> None:
    table = "bridge PRESETS"
    preset = presets.get(material)
    if preset is None:
        differ(table, code, material, "material", PRESENT, MISSING)
        return
    differ(table, code, material, "nozzle", fw["nozzle_temperature"], preset.nozzle)
    differ(table, code, material, "preheat", fw["nozzle_preheat_temperature"], preset.preheat)
    differ(table, code, material, "bed", fw["heatbed_temperature"], preset.bed)
    differ(table, code, material, "flexible", fw["is_flexible"], preset.flexible)
    # These fields exist only in builds with HAS_CHAMBER_API and HAS_CHAMBER_FILTRATION_API.
    if "chamber_target_temperature" in fw:
        chamber = tuple(fw[f"chamber_{k}_temperature"] for k in ("target", "min", "max"))
        differ(table, code, material, "chamber", chamber, tuple(preset.chamber))
    if "requires_filtration" in fw:
        differ(table, code, material, "filtration", fw["requires_filtration"], preset.filtration)


def _compare_sim_preset(
    code: str,
    material: str,
    fw: Mapping[str, read.Value],
    presets: Mapping[str, Any],
    differ: Differ,
) -> None:
    table = "simulator PRESET_TEMPERATURES"
    values = presets.get(material)
    if values is None:
        differ(table, code, material, "material", PRESENT, MISSING)
        return
    nozzle, preheat, bed = values
    differ(table, code, material, "nozzle", fw["nozzle_temperature"], nozzle)
    differ(table, code, material, "preheat", fw["nozzle_preheat_temperature"], preheat)
    differ(table, code, material, "bed", fw["heatbed_temperature"], bed)


@dataclass(frozen=True)
class Expected:
    """A known difference, matched by its place and, where pinned, its values.

    None in `model` or `material` matches any, and so does `ANY` in `firmware` or `copy`.
    Pin the values the reason describes, so the entry can't hide another difference at the
    same place.
    """

    table: str
    model: str | None
    material: str | None
    field: str
    reason: str
    firmware: Any = ANY
    copy: Any = ANY

    def matches(self, d: Difference) -> bool:
        return (
            (self.table, self.field) == (d.table, d.field)
            and self.model in (None, d.model)
            and self.material in (None, d.material)
            and (self.firmware is ANY or self.firmware == d.firmware)
            and (self.copy is ANY or self.copy == d.copy)
        )

    def __str__(self) -> str:
        where = " ".join(p for p in (self.model, self.material, self.field) if p)
        return f"{self.table}: {where}"


# Differences the copies keep on purpose, or haven't caught up with yet. Each entry has to
# match a difference: one that no longer occurs fails the check, so the list stays honest.
EXPECTED = (
    Expected(
        "bridge PRESETS",
        None,
        "PPS",
        "material",
        "permanent: it needs the HT idler-door setting, which M865 can't set",
        PRESENT,
        MISSING,
    ),
    Expected(
        "bridge PRESETS",
        None,
        "PPA",
        "material",
        "permanent: it needs the HT idler-door setting, which M865 can't set",
        PRESENT,
        MISSING,
    ),
)


def report(
    differences: list[Difference], expected: tuple[Expected, ...] | None = None
) -> tuple[list[str], bool]:
    """Return the report's lines, per table, and whether the check passes.

    It passes when every difference is expected (on `EXPECTED` by default) and every
    expected entry still occurs.
    """
    if expected is None:
        expected = EXPECTED
    lines: list[str] = []
    unexpected = 0
    used: set[Expected] = set()
    for table in TABLES:
        mine = [d for d in differences if d.table == table]
        if not mine:
            lines.append(f"{table}: ok")
            continue
        lines.append(f"{table}:")
        for d in mine:
            known = next((e for e in expected if e.matches(d)), None)
            if known is None:
                unexpected += 1
                lines.append(f"  {d}")
            else:
                used.add(known)
                lines.append(f"  {d}  (expected: {known.reason})")
    stale = [e for e in expected if e not in used]
    for e in stale:
        lines.append(f"stale expected difference, no longer found: {e}; remove it from EXPECTED")
    matched = len(differences) - unexpected
    lines.append(
        f"{unexpected} unexpected differences, {matched} expected, "
        f"{len(stale)} stale expected entries"
    )
    return lines, unexpected == 0 and not stale


def _version(code: str) -> tuple[int, ...]:
    return tuple(int(p) for p in code.split("."))
