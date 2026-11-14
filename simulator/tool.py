"""A tool and the filament in it: the material the firmware stores, its presets, and the
load and unload processes.

These sit below `model`, so a module that `model` imports can build them too.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

# Nozzle, preheat and bed temperatures of the firmware's preset filaments
# (`src/common/filament_presets.cpp`), with the preheat for printers with a load cell.
# `script/firmware-check` compares them with the firmware source.
PRESET_TEMPERATURES = {
    "PLA": (215, 170, 60),
    "PETG": (230, 170, 85),
    "ASA": (260, 170, 100),
    "PC": (275, 250, 100),
    "PVB": (215, 170, 75),
    "ABS": (255, 170, 100),
    "HIPS": (220, 170, 100),
    "PP": (240, 170, 100),
    "FLEX": (240, 210, 50),
    "PA": (285, 260, 100),
    "PPS": (320, 170, 105),
    "PPA": (320, 170, 105),
    "PVA": (210, 160, 60),
    "BVOH": (210, 160, 60),
}
# What `M865 R` resets them to (`FilamentTypeParameters` in `filament.hpp`).
DEFAULT_TEMPERATURES = (215, 170, 60)
# Tag names the firmware loads as another preset: the TPU workaround in
# the `FilamentParametersInfo` constructor in `src/feature/openprinttag/data_utils.cpp`.
# `script/firmware-check` compares them with the firmware source.
PRESET_ALIASES = {"TPU": "FLEX"}
# The presets with `is_flexible` set, which don't auto-retract and ram with a build's
# `*_flexible` sequence where it has one. `script/firmware-check` compares them with the
# firmware source.
FLEXIBLE_PRESETS = frozenset({"FLEX"})

Ramming = tuple[tuple[int, int], ...]  # steps of E in mm and feedrate in mm/min

# The standard ramming sequences each build compiles, by `StandardRammingSequence` case,
# with `<case>_flexible` where flexible filament gets another sequence
# (`src/feature/ramming/standard_ramming_sequence_<variant>.cpp`). A build without an
# `auto_retract` case doesn't auto-retract. `script/firmware-check` compares them with
# the firmware source.
_NEXTRUDER_AUTO_RETRACT: Ramming = (
    (8, 995),
    (-43, 6000),
    (-8, 3000),
    (-4, 1800),
    (20, 600),
    (-20, 470),
    (55, 1740),
    (-55, 6000),
    (20, 340),
    (-20, 210),
)
_NEXTRUDER_UNLOAD: Ramming = (*_NEXTRUDER_AUTO_RETRACT, (-50, 2000))
_NEXTRUDER_RUNOUT: Ramming = ((7, 1500), (-50, 2700), (-5, 50), (-50, 1500))
NEXTRUDER_RAMMING: dict[str, Ramming] = {
    "auto_retract": _NEXTRUDER_AUTO_RETRACT,
    "runout": _NEXTRUDER_RUNOUT,
    "unload": _NEXTRUDER_UNLOAD,
}
_INDX_UNLOAD: Ramming = (
    (8, 1500),
    (8, 1200),
    (-20, 2100),
    (-3, 60),
    (3, 120),
    (19, 1800),
    (2, 600),
    (-20, 2100),
    (-3, 120),
    (3, 120),
    (-6, 120),
    (-8, 360),
)
INDX_RAMMING: dict[str, Ramming] = {
    "auto_retract": ((8, 480), (-8, 4800), (1, 1440), (-1, 1440)),
    "runout": _INDX_UNLOAD,
    "runout_flexible": ((-50, 600),),
    "unload": _INDX_UNLOAD,
    "unload_flexible": ((-50, 600),),
}
_MINI_UNLOAD: Ramming = ((20, 1500), (-50, 2700), (-5, 50), (-50, 1500))
MK3_5_RAMMING: dict[str, Ramming] = {
    "runout": ((7, 1500), (-50, 2700), (-5, 50), (-50, 1500)),
    "unload": _MINI_UNLOAD,
}
MINI_RAMMING: dict[str, Ramming] = {"runout": _MINI_UNLOAD, "unload": _MINI_UNLOAD}


def retracted_distance(sequence: Ramming) -> int:
    """Return how far a ramming sequence leaves the filament retracted, in mm.

    As `RammingSequence::init` in `src/feature/ramming/ramming_sequence.hpp` works it out:
    a negative E retracts, a positive one pushes back, and pushing past the nozzle resets
    it to 0.
    """
    distance = 0
    for e, _feedrate in sequence:
        distance = max(0, distance - e)
    return distance


@dataclass(frozen=True)
class Lengths:
    """A build's filament change lengths in mm (`include/marlin/Configuration_<X>_adv.h`).

    The fields follow the firmware's order: `PARK_PAUSE_PRIME_LENGTH`,
    `STANDARD_RETRACT_LENGTH`, then the `FILAMENT_CHANGE_` unload, slow load, fast load
    and purge lengths. `script/firmware-check` compares them with the firmware source.
    """

    prime_mm: int
    retract_mm: int
    unload_mm: int
    slow_load_mm: int
    fast_load_mm: int
    purge_mm: int


@dataclass(frozen=True)
class Extruder:
    """A build's extruder: how it feeds, changes filament, rams and cleans the nozzle.

    `script/firmware-check` compares each model's with the firmware source.
    """

    # Default and calibrated steps per mm (`include/marlin/Configuration_<X>.h`).
    e_steps: tuple[int, int]
    lengths: Lengths
    # Left out of the hash so a `ModelInfo` stays hashable.
    ramming: dict[str, Ramming] = field(hash=False)
    # Whether the user can turn auto-retract off: `HAS_SWITCHABLE_AUTO_RETRACT`.
    auto_retract_switchable: bool = False
    # What the nozzle cleaner's `purge_clean` G-code extrudes, and whether it then
    # auto-retracts (`src/feature/nozzle_cleaner/nozzle_cleaner.cpp`). None without a
    # nozzle cleaner.
    cleaner_purge_mm: float | None = None
    cleaner_auto_retract: bool = False
    # How far an auto-retract has to have retracted for the filament to count as fully
    # retracted, and whether that lets an unload skip the ramming: `full_retract_distance`
    # and `supports_cold_unload` in `src/feature/auto_retract/auto_retract.hpp`, 20 mm and
    # true on a Nextruder, 8 mm and false on INDX. Only a build that auto-retracts uses
    # them, and `script/firmware-check` doesn't compare them.
    full_retract_mm: float = 20.0
    cold_unload: bool = True
    # What an unload purges before the ramming: 2 mm on INDX (`unload_purge_process` in
    # `src/marlin_stubs/pause/pause.cpp`), not compared either.
    unload_purge_mm: float = 0.0

    @property
    def feed_ratio(self) -> float:
        """How much filament an E move feeds per mm commanded (`planner.cpp`)."""
        default, calibrated = self.e_steps
        return default / calibrated

    @property
    def auto_retracts(self) -> bool:
        """Whether the build auto-retracts at all: it has the sequence for it."""
        return "auto_retract" in self.ramming


_NEXTRUDER_STEPS = (380, 400)
_NEXTRUDER_LENGTHS = Lengths(4, 8, 105, 30, 50, 27)
_CORE_ONE_LENGTHS = Lengths(4, 8, 105, 40, 20, 40)

# Each build's extruder, shared by the models that run the build. The MK4 and XL builds
# have the same one, and so do the Core One and Core One L, and the two INDX builds.
MK4_EXTRUDER = Extruder(
    _NEXTRUDER_STEPS, _NEXTRUDER_LENGTHS, NEXTRUDER_RAMMING, auto_retract_switchable=True
)
# The dev kit build lacks HAS_AUTO_RETRACT, which compiles the auto_retract case out.
XL_DEV_KIT_EXTRUDER = Extruder(
    _NEXTRUDER_STEPS,
    _NEXTRUDER_LENGTHS,
    {k: v for k, v in NEXTRUDER_RAMMING.items() if k != "auto_retract"},
)
IX_EXTRUDER = Extruder(
    _NEXTRUDER_STEPS,
    Lengths(4, 8, 110, 40, 45, 40),
    NEXTRUDER_RAMMING,
    auto_retract_switchable=True,
    cleaner_purge_mm=0.0,
)
CORE_ONE_EXTRUDER = Extruder(
    _NEXTRUDER_STEPS, _CORE_ONE_LENGTHS, NEXTRUDER_RAMMING, auto_retract_switchable=True
)
INDX_EXTRUDER = Extruder(
    (550, 576),
    Lengths(4, 8, 55, 15, 25, 40),
    INDX_RAMMING,
    cleaner_purge_mm=25.0,
    cleaner_auto_retract=True,
    full_retract_mm=8.0,
    cold_unload=False,
    unload_purge_mm=2.0,
)
MK3_5_EXTRUDER = Extruder((266, 280), Lengths(4, 8, 105, 30, 50, 21), MK3_5_RAMMING)
MINI_EXTRUDER = Extruder((325, 325), Lengths(4, 8, 400, 40, 320, 50), MINI_RAMMING)


@dataclass
class Material:
    name: str
    color: str | None = None
    instance_uuid: str | None = None
    # A preset has no color or UUID and ignores edits; tag loads, loads with a picked color,
    # and M865 X writes are ad-hoc. The page shows the flag; PrusaLink doesn't.
    adhoc: bool = True
    # Set when the material is built, as the firmware does: from the preset, the tag or
    # M865. Without them, the firmware's defaults.
    nozzle: int = DEFAULT_TEMPERATURES[0]
    preheat: int = DEFAULT_TEMPERATURES[1]
    bed: int = DEFAULT_TEMPERATURES[2]
    # Set by a tag load only. M865 X R starts afresh without them; edits and runout
    # reloads keep them.
    material_uuid: str | None = None
    package_uuid: str | None = None

    def temperatures(self) -> tuple[int, int, int]:
        """Return the nozzle, preheat and bed temperatures the printer reports."""
        return self.nozzle, self.preheat, self.bed


def color555(color: str | None) -> str | None:
    """Round `#rrggbb` to what the firmware stores: 5 bits a channel, widened back to 8.

    `Color555::from_rgb` keeps each channel's top 5 bits, and `to_color` widens them as
    `(v << 3) | (v >> 2)` (`src/common/utils/color555.hpp:23-54` at 2db93db5).
    """
    if color is None:
        return None
    channels = (int(color[i : i + 2], 16) >> 3 for i in (1, 3, 5))
    return "#" + "".join(f"{(v << 3) | (v >> 2):02x}" for v in channels)


def is_flexible(material: Material | None) -> bool:
    """Whether the firmware treats `material` as flexible: a flexible preset by name, or a
    name in `PRESET_ALIASES` for one. An `M865` flexible flag isn't modeled."""
    if material is None:
        return False
    return PRESET_ALIASES.get(material.name, material.name) in FLEXIBLE_PRESETS


def preset_temperatures(name: str) -> dict[str, int]:
    """Return the temperatures a material named `name` starts from, as `Material` keywords.

    A preset's name matches exactly, as `FilamentType::from_name` does. A name in
    `PRESET_ALIASES`, such as TPU, starts from its preset. Anything else gets the defaults.
    """
    values = PRESET_TEMPERATURES.get(PRESET_ALIASES.get(name, name), DEFAULT_TEMPERATURES)
    return dict(zip(("nozzle", "preheat", "bed"), values, strict=True))


# Steps that move nothing and take no time. Each acts when it comes up.
TYPE_STEP = "TYPE"  # writes the type
AUTO_RETRACT_STEP = "AUTO_RETRACT"  # auto-retracts unless the firmware would skip it
DERETRACT_STEP = "DERETRACT"  # what a ramming sequence starting with a feed does first
RETRACTED_STEP = "RETRACTED"  # stores `mm` as the auto-retracted distance
MARKERS = frozenset({TYPE_STEP, AUTO_RETRACT_STEP, DERETRACT_STEP, RETRACTED_STEP})


@dataclass
class LoadStep:
    """One step of a load or unload: a signed E move in commanded mm, or a marker."""

    # A move's phase is SLOW, FAST, PURGE, PRIME, RETRACT, RAM or UNLOAD; a marker's is
    # one of MARKERS.
    phase: str
    mm: float = 0.0  # what's left of the move: positive feeds, negative retracts


@dataclass
class LoadProcess:
    """A load as the steps left to run, the current one first. It ends with its last step."""

    steps: list[LoadStep]
    material: Material | None  # written at the TYPE step; None restores the unload's type
    kind: str = "load"

    @property
    def phase(self) -> str:
        return self.steps[0].phase

    @property
    def remaining(self) -> float:
        """The mm left in the current step."""
        return abs(self.steps[0].mm)

    @property
    def typed(self) -> bool:
        """Whether the type has been written."""
        return all(s.phase != TYPE_STEP for s in self.steps)


@dataclass
class UnloadProcess:
    """A menu unload: its steps, spread evenly over its duration. It ends when the time is
    up, with every step run."""

    steps: list[LoadStep]
    remaining: float  # seconds
    kind: str = "unload"

    @property
    def phase(self) -> str:
        return self.steps[0].phase if self.steps else "UNLOAD"


@dataclass
class SimTool:
    index: int
    enabled: bool = True
    loaded: bool = False
    material: Material | None = None
    previous: Material | None = None  # the ad-hoc slot: the last ad-hoc material written
    kept_type: Material | None = None  # the type the last unload left, for a runout reload
    extruded: float = 0.0
    unload_count: int = 0
    extruded_at_unload: int = 0
    # The firmware's filament tracker: how far the filament is retracted from the nozzle,
    # and whether it trusts that distance (`filament_tracker.cpp`).
    gap: float = 0.0
    gap_valid: bool = True
    # The auto-retract's stored distance, which survives a boot: None when unknown, 0 when
    # known not to be retracted (`auto_retract.cpp`).
    auto_retracted: float | None = None
    sensor_fault: bool = False
    stuck_write: bool = False
    process: LoadProcess | UnloadProcess | None = None

    # The stored type, the ad-hoc slot and the kept type change only through these methods.

    @classmethod
    def loaded_with(cls, index: int, material: Material | None, **kw: Any) -> SimTool:
        """A tool as a finished load of `material` leaves it, or empty for None."""
        tool = cls(index, **kw)
        tool.fill(material)
        return tool

    def fill(self, material: Material | None) -> None:
        """Leave the tool as a finished load of `material` leaves it, or a finished unload for
        None. Nothing is counted: this is setup, such as a config or a test's starting state.

        The tracker is left at gap 0 and valid, with no auto-retract stored, whatever the
        model: a tool that has fed since its last auto-retract. A real idle load would
        leave the model's auto-retract instead, but the tool doesn't know its model.
        """
        self.loaded = material is not None
        self.gap, self.gap_valid, self.auto_retracted = 0.0, True, None
        self.store(material)

    def store(self, material: Material | None) -> None:
        """Write the stored type. An ad-hoc type also fills the ad-hoc slot (`previous`).

        The slot keeps a color in 5 bits per channel, so the color reads back rounded (F25).
        """
        if material is not None:
            material = replace(material, color=color555(material.color))
        self.material = material
        if material is not None and material.adhoc:
            self.previous = replace(material)

    def clear_type(self) -> None:
        """The firmware writes "none": the type goes, and a runout reload can restore it.

        Writing "none" also forgets the auto-retracted distance (`set_filament_type` in
        `store_definition.cpp`).
        """
        self.kept_type, self.material = self.material, None
        self.auto_retracted = None

    def kept(self) -> Material | None:
        """The type a load without a source restores: the one the unload left, else the slot."""
        kept = self.kept_type or self.previous
        return None if kept is None else replace(kept)


def describe(m: Material | None) -> str:
    if m is None:
        return "nothing"
    extra = "".join(f" {x}" for x in (m.color, m.instance_uuid) if x)
    return m.name + extra
