"""Build the G-code job that sets the printer's loaded filament from Spoolman.

PrusaLink has no write endpoint for filament, so the bridge uploads a small job and runs
it as a print. The job uses `M865` (and `M865.1` for the instance UUID) without moving
filament. See `src/marlin_stubs/M865.cpp` in the firmware.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Spool

# Default compatibility checks for the Core One L. Without them the printer stops on a
# "G-code isn't fully compatible" warning. Other models need their own lines.
COREONEL_PREAMBLE = (
    'M862.3 P "COREONEL" ; printer model check',
    "M862.5 P2 ; g-code level check",
    'M862.6 P"Input shaper" ; FW feature check',
)

# Firmware preset filament types (`src/common/filament_presets.cpp`). `J` accepts only
# these; anything else is an error.
PRESETS = frozenset({
    "PLA", "PETG", "ASA", "PC", "PVB", "ABS", "HIPS", "PP", "FLEX", "PA", "PPS", "PPA",
    "PVA", "BVOH",
})
PRESET_ALIASES = {"TPU": "FLEX", "TPE": "FLEX"}

NAME_LIMIT = 7  # The firmware's filament name holds at most 7 characters.
MESSAGE_LIMIT = 40


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


def preset_for(spool: Spool) -> str | None:
    """The firmware preset to inherit temperatures from, if the material is one."""
    if not spool.material:
        return None
    material = spool.material.strip().upper()
    material = PRESET_ALIASES.get(material, material)
    return material if material in PRESETS else None


def _color(spool: Spool) -> str | None:
    if not spool.color_hex:
        return None
    value = spool.color_hex.strip().lstrip("#").lower()[:6]
    return f"#{value}" if re.fullmatch(r"[0-9a-f]{6}", value) else None


def _message(text: str) -> str:
    """Make text safe for `M117`: ASCII, no `;` (a comment), and short."""
    text = text.encode("ascii", "ignore").decode().replace(";", ",").replace('"', "'")
    text = " ".join(text.split())
    return text[:MESSAGE_LIMIT]


@dataclass(frozen=True)
class ToolWrite:
    tool: int
    spool: Spool
    uuid: str | None  # Instance UUID to write, on tool 0 only.


def filament_lines(write: ToolWrite) -> list[str]:
    """Return the `M865`/`M865.1` lines that load `write.spool` as an ad-hoc filament.

    `R` resets every parameter not given, so the job must supply temperatures, either
    from a base preset (`J`) or explicitly (`T` nozzle, `B` bed). Spoolman's own
    temperatures override the preset's.
    """
    spool, tool = write.spool, write.tool
    name = printer_name(spool)
    if name is None:
        raise CannotSync(f"spool {spool.id} has no material or name to write")
    preset = preset_for(spool)
    if preset is None and not (spool.extruder_temp and spool.bed_temp):
        raise CannotSync(
            f"spool {spool.id}'s material {spool.material!r} isn't a printer preset; set its "
            "extruder and bed temperatures in Spoolman"
        )
    parts = ["M865 X R", f'N"{name}"']
    if preset:
        parts.append(f'J"{preset}"')
    if spool.extruder_temp:
        parts.append(f"T{int(spool.extruder_temp)}")
    if spool.bed_temp:
        parts.append(f"B{int(spool.bed_temp)}")
    color = _color(spool)
    if color:
        parts.append(f'O"{color}"')
    parts.append(f"L{tool}")
    lines = [" ".join(parts)]
    if tool == 0 and write.uuid:
        lines.append(f'M865.1 I0 C"{write.uuid}"')
    return lines


def build_job(
    preamble: tuple[str, ...] | list[str],
    writes: list[ToolWrite],
    notices: list[str],
    multi_tool: bool,
) -> str:
    """Return the job file: compatibility checks, filament writes, then one `M117`."""
    lines = list(preamble)
    labels = []
    for w in writes:
        lines += filament_lines(w)
        label = _message(w.spool.name)
        labels.append(f"T{w.tool + 1} {label}" if multi_tool else label)
    message = ""
    if labels:
        message = "Spoolman: " + ", ".join(labels)
    elif notices:
        message = "Spoolman: " + notices[0]
    if message:
        lines.append(f"M117 {_message(message)}")
    return "\n".join(lines) + "\n"
