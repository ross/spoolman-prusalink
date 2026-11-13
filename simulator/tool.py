"""A tool and the filament in it: the material the firmware stores, its presets, and the
load and unload processes.

These sit below `model`, so a module that `model` imports can build them too.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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


def preset_temperatures(name: str) -> dict[str, int]:
    """Return the temperatures a material named `name` starts from, as `Material` keywords.

    A preset's name matches exactly, as `FilamentType::from_name` does. A name in
    `PRESET_ALIASES`, such as TPU, starts from its preset. Anything else gets the defaults.
    """
    values = PRESET_TEMPERATURES.get(PRESET_ALIASES.get(name, name), DEFAULT_TEMPERATURES)
    return dict(zip(("nozzle", "preheat", "bed"), values, strict=True))


@dataclass
class LoadProcess:
    phase: str  # FEED_GAP (not counted), FEED (counted, old material), PURGE (new material)
    remaining: float  # mm left in this phase
    feed_mm: float
    purge_mm: float
    material: Material | None  # set at the purge; None restores the type the unload left
    kind: str = "load"


@dataclass
class UnloadProcess:
    remaining: float  # seconds
    kind: str = "unload"


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
    gap: float = 0.0
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
        """
        self.loaded = material is not None
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
        """The firmware writes "none": the type goes, and a runout reload can restore it."""
        self.kept_type, self.material = self.material, None

    def kept(self) -> Material | None:
        """The type a load without a source restores: the one the unload left, else the slot."""
        kept = self.kept_type or self.previous
        return None if kept is None else replace(kept)


def describe(m: Material | None) -> str:
    if m is None:
        return "nothing"
    extra = "".join(f" {x}" for x in (m.color, m.instance_uuid) if x)
    return m.name + extra
