"""Build the G-code job that sets the printer's loaded filament from Spoolman.

PrusaLink has no write endpoint for filament, so the bridge uploads a small job and runs
it as a print. The job uses `M865` (and `M865.1` for the instance UUID) without moving
filament. See `src/marlin_stubs/M865.cpp` in the firmware.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace

from .color import same_color
from .models import Material, Spool


@dataclass(frozen=True)
class PrinterModel:
    name: str  # What `M862.3 P"..."` checks for. The firmware compares it exactly.
    unsupported: str | None = None  # Why the bridge can't run it, or None.
    uuids: bool = False  # Whether `M865.1` stores an instance UUID (`HAS_ANFC`).


# The only tool that holds an instance UUID, on a model with `uuids`: `M865.1 I0` sets it,
# and the tag reader reports one there.
UUID_TOOL = 0

_NO_COUNTERS = "its firmware doesn't report usage counters in /api/v1/filament"
_MULTI_TOOL = "multi-tool printers aren't supported yet"

# Printer type codes, as `/api/version` reports them in `printer`. From
# `include/common/printer_model_data.hpp` and `ProjectOptions.cmake` in the firmware: the
# filament tracker is in the MK4 build (MK4, MK4S, MK3.9, MK3.9S), the Core One and the
# Core One L; ANFC, which keeps UUIDs, only in the last two.
PRINTER_MODELS = {
    "1.3.5": PrinterModel("MK3.5", _NO_COUNTERS),
    "1.3.6": PrinterModel("MK3.5S", _NO_COUNTERS),
    "1.3.9": PrinterModel("MK3.9"),
    "1.3.10": PrinterModel("MK3.9S"),
    "1.4.0": PrinterModel("MK4"),
    "1.4.1": PrinterModel("MK4S"),
    "2.1.0": PrinterModel("MINI", _NO_COUNTERS),
    "3.1.0": PrinterModel("XL", _MULTI_TOOL),
    "5.1.0": PrinterModel("XL", _MULTI_TOOL),
    "4.1.0": PrinterModel("iX", _MULTI_TOOL),
    "7.1.0": PrinterModel("COREONE", uuids=True),
    "8.1.0": PrinterModel("COREONEL", uuids=True),
    "7.10.0": PrinterModel("COREONEINDX", _MULTI_TOOL),
    "8.10.0": PrinterModel("COREONEL-INDX", _MULTI_TOOL),
}


def default_preamble(model: PrinterModel) -> tuple[str, ...]:
    """Return the compatibility checks a job for `model` starts with.

    Without them the printer stops on a "G-code isn't fully compatible" warning. The
    level and the input shaper feature are the same on every supported model.
    """
    return (
        f'M862.3 P "{model.name}" ; printer model check',
        "M862.5 P2 ; g-code level check",
        'M862.6 P"Input shaper" ; FW feature check',
    )


_MODEL_CHECK = re.compile(r'^\s*M862\.3\s+P\s*"([^"]+)"', re.IGNORECASE)


def preamble_model(preamble: tuple[str, ...] | list[str]) -> str | None:
    """Return the model name the preamble's `M862.3 P"..."` line checks for."""
    for line in preamble:
        m = _MODEL_CHECK.match(line)
        if m:
            return m.group(1)
    return None


def model_problem(
    printer_type: str | None, preamble: tuple[str, ...] | list[str] | None
) -> str | None:
    """Return why the bridge can't write to this printer, or None if it can.

    `preamble` is the configured override, or None for the model's default. A job
    whose `M862.3` names another model stops on the printer's compatibility warning
    until someone taps through it, so the name has to match exactly. The firmware also
    accepts the model's MMU3 variant name.
    """
    model = PRINTER_MODELS.get(printer_type or "")
    if model is None:
        return f"printer type {printer_type!r} isn't one the bridge knows"
    if model.unsupported:
        return f"{model.name} isn't supported: {model.unsupported}"
    if preamble is None:
        return None
    wanted = preamble_model(preamble)
    if wanted is None:
        return f"gcode_preamble has no M862.3 model check; set one for {model.name}"
    if wanted not in (model.name, f"{model.name}MMU3"):
        return f"gcode_preamble is for {wanted} but the printer is {model.name}"
    return None


@dataclass(frozen=True)
class Preset:
    nozzle: int
    bed: int
    chamber: tuple[int, int, int]  # (target, min, max); only the Core One has a chamber.
    preheat: int = 170
    filtration: bool = False
    flexible: bool = False


# Firmware preset filaments (`src/common/filament_presets.cpp`), with the preheat values
# for printers with a load cell, which every supported model has. `M865 X R` resets every
# parameter to the defaults in the firmware's `filament.hpp` (nozzle 215, preheat 170,
# bed 60, everything else unset or off), and `J` (inherit a preset) is ignored on these
# models, so the job writes each value. PPS and PPA are left out: they need the HT
# idler-door setting, which `M865` can't set. `docs/firmware-pin.txt` records the firmware
# version these were read from.
PRESETS = {
    "PLA": Preset(215, 60, chamber=(20, 15, 38)),
    "PETG": Preset(230, 85, chamber=(30, 15, 45)),
    "ASA": Preset(260, 100, chamber=(70, 40, 75), filtration=True),
    "PC": Preset(275, 100, preheat=250, chamber=(75, 40, 80), filtration=True),
    "PVB": Preset(215, 75, chamber=(20, 15, 38)),
    "ABS": Preset(255, 100, chamber=(70, 40, 75), filtration=True),
    "HIPS": Preset(220, 100, chamber=(70, 40, 75), filtration=True),
    "PP": Preset(240, 100, chamber=(60, 30, 70), filtration=True),
    "FLEX": Preset(240, 50, preheat=210, chamber=(25, 15, 40), filtration=True, flexible=True),
    "PA": Preset(285, 100, preheat=260, chamber=(65, 40, 70)),
    "PVA": Preset(210, 60, preheat=160, chamber=(25, 15, 38)),
    "BVOH": Preset(210, 60, preheat=160, chamber=(25, 15, 38)),
}
PRESET_ALIASES = {"TPU": "FLEX", "TPE": "FLEX"}
# In the firmware only the PPS and PPA presets set `requires_ht_idler_door`, and `M865`
# has no parameter for it, so an ad-hoc filament with these names would load without it.
HT_IDLER_DOOR = frozenset({"PPS", "PPA"})

NAME_LIMIT = 7  # The firmware's filament name holds at most 7 characters.
# The preheat temperature `M865 X R` leaves when the line has no `P`.
DEFAULT_PREHEAT = 170


class CannotSync(ValueError):
    """The spool lacks what the printer needs, such as temperatures."""


def printer_name(spool: Spool) -> str | None:
    """The filament name to write: the material, limited to the firmware's charset.

    The firmware accepts 1 to 7 characters from `a-zA-Z0-9_-`.
    """
    for source in (spool.material, spool.name):
        if source:
            name = re.sub(r"[^A-Za-z0-9_-]", "", source)[:NAME_LIMIT]
            if name:
                return name
    return None


def loaded_from_menu(shown: str, spool: Spool) -> bool:
    """Whether the printer shows `spool`'s HT idler-door material, loaded from its menu.

    The bridge can't write these materials, and a menu load sets the preset's name but
    not the spool's color or instance UUID, so the name is all that can match.
    """
    material = (spool.material or "").strip().upper()
    return material in HT_IDLER_DOOR and shown.strip().upper() == material


def preset_for(spool: Spool) -> str | None:
    """The firmware preset to inherit temperatures from, if the material is one."""
    if not spool.material:
        return None
    material = spool.material.strip().upper()
    material = PRESET_ALIASES.get(material, material)
    return material if material in PRESETS else None


@dataclass(frozen=True)
class Temperatures:
    nozzle: int
    preheat: int
    bed: int


def temperatures(spool: Spool) -> Temperatures:
    """Return the temperatures `filament_lines` writes for `spool`.

    Spoolman's extruder and bed temperatures take priority over the material's firmware
    preset. Raises CannotSync for a material that isn't a preset and lacks either one.
    """
    key = preset_for(spool)
    preset = PRESETS.get(key) if key else None
    nozzle = int(spool.extruder_temp) if spool.extruder_temp else None
    bed = int(spool.bed_temp) if spool.bed_temp else None
    if preset is None:
        if not (nozzle and bed):
            raise CannotSync(
                f"spool {spool.id}'s material {spool.material!r} isn't a printer preset; set "
                "its extruder and bed temperatures in Spoolman"
            )
        return Temperatures(nozzle, DEFAULT_PREHEAT, bed)
    nozzle = nozzle or preset.nozzle
    return Temperatures(nozzle, min(preset.preheat, nozzle), bed or preset.bed)


def _color(spool: Spool) -> str | None:
    if not spool.color_hex:
        return None
    value = spool.color_hex.strip().lstrip("#").lower()[:6]
    return f"#{value}" if re.fullmatch(r"[0-9a-f]{6}", value) else None


@dataclass(frozen=True)
class ToolWrite:
    tool: int
    spool: Spool
    uuid: str | None  # Instance UUID to write, on tool 0 only.


@dataclass(frozen=True)
class WrittenFilament:
    """Every parameter one write sets on a tool, as `M865` and `M865.1` take them.

    The bridge saves this as its last write to the tool. Comparing it with the write a
    spool needs now shows an edit in Spoolman, and comparing it with what the printer
    reports shows a change made on the printer.
    """

    name: str
    nozzle: int
    bed: int
    preheat: int | None = None  # None isn't written, which leaves `DEFAULT_PREHEAT`.
    chamber: tuple[int, int, int] | None = None  # (target, min, max), from a preset.
    filtration: bool = False
    flexible: bool = False
    color: str | None = None  # As "#rrggbb".
    uuid: str | None = None  # The instance UUID, in lowercase. On `UUID_TOOL` only.

    @property
    def temperatures(self) -> Temperatures:
        """The temperatures the printer reports after this write."""
        preheat = DEFAULT_PREHEAT if self.preheat is None else self.preheat
        return Temperatures(self.nozzle, preheat, self.bed)

    def lines(self, tool: int) -> list[str]:
        """Return the G-code lines that write this filament to `tool`.

        `R` resets every parameter, so the first line gives them all.
        """
        parts = ["M865 X R", f'N"{self.name}"', f"T{self.nozzle}", f"B{self.bed}"]
        if self.preheat is not None:
            parts.append(f"P{self.preheat}")
        if self.chamber is not None:
            target, low, high = self.chamber
            parts.append(f"C{target} D{low} E{high}")
        if self.filtration:
            parts.append("F1")
        if self.flexible:
            parts.append("G1")
        if self.color:
            parts.append(f'O"{self.color}"')
        parts.append(f"L{tool}")
        lines = [" ".join(parts)]
        if self.uuid:
            lines.append(f'M865.1 I{UUID_TOOL} C"{self.uuid}"')
        return lines

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "nozzle": self.nozzle,
            "bed": self.bed,
            "preheat": self.preheat,
            "chamber": None if self.chamber is None else list(self.chamber),
            "filtration": self.filtration,
            "flexible": self.flexible,
            "color": self.color,
            "uuid": self.uuid,
        }

    @classmethod
    def from_json(cls, data: dict) -> WrittenFilament:
        chamber = data["chamber"]
        if chamber is not None:
            target, low, high = chamber
            chamber = (int(target), int(low), int(high))
        return cls(
            name=data["name"],
            nozzle=int(data["nozzle"]),
            bed=int(data["bed"]),
            preheat=None if data["preheat"] is None else int(data["preheat"]),
            chamber=chamber,
            filtration=bool(data["filtration"]),
            flexible=bool(data["flexible"]),
            color=data["color"],
            uuid=data["uuid"],
        )


@dataclass(frozen=True)
class WriteState:
    """Where the bridge is with writing a tool's active spool to the printer."""

    # The printer should show the active spool once idle: set when the active spool
    # changes or the tool unloads, and cleared when a poll sees the printer match.
    due: bool = False
    attempts: int = 0  # Sync jobs run since the write became due.
    # What the bridge last wrote to the tool, so an edit in Spoolman can be told apart
    # from a change on the printer. None when the bridge hasn't written this tool.
    written: WrittenFilament | None = None

    def resync(self) -> WriteState:
        """The active spool changed or the tool unloaded: a write is due."""
        return replace(self, due=True, attempts=0)

    def settled(self) -> WriteState:
        """Nothing is to be written: the printer matches, or the tool is left alone."""
        return replace(self, due=False, attempts=0)

    def wrote(self, filament: WrittenFilament) -> WriteState:
        """A sync job wrote `filament`. It stays due until a poll sees the printer
        match, so a write that hasn't shown yet isn't drift and one that didn't take
        is retried (W5)."""
        return WriteState(due=True, attempts=self.attempts + 1, written=filament)


def filament_for(write: ToolWrite) -> WrittenFilament:
    """Return what loading `write.spool` as an ad-hoc filament sets on the tool.

    The values come from the material's firmware preset, with Spoolman's extruder and
    bed temperatures taking priority. A material that isn't a preset needs both
    temperatures in Spoolman. PPS and PPA can't be written at all.
    """
    spool = write.spool
    name = printer_name(spool)
    if name is None:
        raise CannotSync(f"spool {spool.id} has no material or name to write")
    material = (spool.material or "").strip().upper()
    if material in HT_IDLER_DOOR:
        raise CannotSync(
            f"spool {spool.id}'s material {material} needs the printer's HT idler-door "
            "setting, which the bridge can't write; load it from the printer's menu"
        )
    temps = temperatures(spool)
    key = preset_for(spool)
    preset = PRESETS.get(key) if key else None
    return WrittenFilament(
        name=name,
        nozzle=temps.nozzle,
        bed=temps.bed,
        preheat=temps.preheat if preset else None,
        chamber=preset.chamber if preset else None,
        filtration=bool(preset and preset.filtration),
        flexible=bool(preset and preset.flexible),
        color=_color(spool),
        uuid=_uuid(spool, write.uuid) if write.tool == UUID_TOOL and write.uuid else None,
    )


def filament_lines(write: ToolWrite) -> list[str]:
    """Return the `M865`/`M865.1` lines that load `write.spool` as an ad-hoc filament."""
    return filament_for(write).lines(write.tool)


def _uuid(spool: Spool, value: str) -> str:
    """Return `value` as a lowercase UUID, or raise CannotSync if it isn't one.

    Only the plain hyphenated form passes. Drift compares Spoolman's value with the
    printer's ignoring only case, so a value in another form would never match.
    """
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        canonical = None
    if canonical != value.lower():
        raise CannotSync(
            f"spool {spool.id}'s instance UUID {value!r} isn't a UUID; fix or clear it in Spoolman"
        )
    return canonical


def printer_matches_write(material: Material, written: WrittenFilament, uuids: bool) -> bool:
    """Whether the printer still reports what the bridge's last write set.

    Compares what the printer reports: name, color, temperatures and UUID.
    """
    if material.name.casefold() != written.name.casefold():
        return False
    if not same_color(material.color, written.color):
        return False
    if written.temperatures != _reported_temperatures(material):
        return False
    return not uuids or (material.instance_uuid or "").lower() == (written.uuid or "")


def _reported_temperatures(material: Material) -> Temperatures:
    return Temperatures(
        material.nozzle_temperature, material.preheat_temperature, material.bed_temperature
    )


# Every print job ends with the firmware lowering the bed. With Z unhomed, as after a
# boot, it can't tell the bed is already at the bottom and drives it into the stop, so
# home first. `O` skips homing when the position is already known.
HOME_IF_NEEDED = "G28 O ; home only if not homed, so the end-of-job bed move is safe"


def build_job(preamble: tuple[str, ...] | list[str], writes: list[ToolWrite]) -> str:
    """Return the job file: compatibility checks, homing if needed, then filament writes."""
    lines = [*preamble, HOME_IF_NEEDED]
    for w in writes:
        lines += filament_lines(w)
    return "\n".join(lines) + "\n"
