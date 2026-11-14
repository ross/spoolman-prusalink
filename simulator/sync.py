"""The sync job's G-code interpreter: what each parsed command does to a printer.

`gcode` parses the job, and `preview` checks it before it runs. The job's lifecycle (the
phases) stays in `model`, which calls `apply` here. Material writes go through the
`SimTool` methods.

Nothing a line does stops the job. An `M865` the firmware can't run prints an error to the
serial port, or nothing, and the job goes on (`src/marlin_stubs/M865.cpp`); an unknown
command prints "Unknown command" (`GCodeParser::unknown_command_error` in Marlin's
`parser.cpp`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from simulator import gcode
from simulator.tool import DEFAULT_TEMPERATURES, Material, SimTool, describe

if TYPE_CHECKING:
    from simulator.model import SimPrinter

# The firmware's errors for a name it won't take (`FilamentType::can_be_renamed_to` in
# `src/common/filament_common.cpp`). A collision is an error only for a user type, which
# the simulator doesn't model.
NOT_CUSTOMIZABLE = "Filament is not customizable"


def apply(p: SimPrinter, commands: list[gcode.Command]) -> None:
    for c in commands:
        if isinstance(c, gcode.SetFilament):
            _apply_filament(p, c)
        elif isinstance(c, gcode.Rejected):
            p._log("gcode", f"dropped: {c.line} ({c.reason})")
        elif isinstance(c, gcode.Message):
            p.message = c.text
            p._log("gcode", f"M117 {c.text}")
        elif isinstance(c, gcode.Home):
            p.homed = True
            p._log("gcode", "G28: homed")
        elif isinstance(c, gcode.ModelCheck | gcode.LevelCheck | gcode.FeatureCheck):
            # Checked at the preview; a running job ignores them (`M862_3` and `M862_6` in
            # `src/marlin_stubs`).
            p._log("gcode", f"checked at the preview: {c}")
        else:
            p._log("gcode", f"ignored: {c.line}")


def rename_error(name: str, customizable: bool) -> str | None:
    """Why the firmware won't name a type `name`, or None (`can_be_renamed_to`)."""
    if not customizable:
        return NOT_CUSTOMIZABLE
    if not name:
        return "Name must not be empty"
    if name == "---":
        return "Name must not be '---'"
    if not all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in name):
        return "Name must contain only 'a-zA-Z0-9_-' characters"
    return None


def _apply_filament(p: SimPrinter, c: gcode.SetFilament) -> None:
    """`PrusaGcodeSuite::M865`: pick a type, change its parameters if it takes them, then
    load it into tool `L`. An index outside the build's tools doesn't read, as any value
    outside its range doesn't. The config store has a slot for every tool the model can
    have, enabled or not, and every MMU slot whether the MMU is on or off."""
    word = "M865.1" if c.subcode else "M865"
    tools = range(p.model.m865_tools)
    load = c.load if c.load in tools else None
    # The type, in the firmware's order: I, X, then S (`M865.cpp:63-79`). `U`, a user type,
    # isn't modeled.
    if c.slot in tools:
        source: SimTool | None = p.tools.get(c.slot) or SimTool(c.slot, enabled=False)
        material = source.material
        customizable = material is not None and material.adhoc
    elif c.adhoc or c.preset == "#":
        source, customizable = None, True
        target = _tool(p, load) if load is not None else None
        old = target.previous if target is not None else None
        material = replace(old, adhoc=True) if old else Material("Custom")
    elif c.preset in p.model.presets:
        source, customizable = None, False
        material = Material(c.preset, adhoc=False, **p.model.preset_temperatures(c.preset))
    else:
        p._log("gcode", f"{word} failed: Filament type invalid or not specified.")
        return
    error = None if c.name is None else rename_error(c.name, customizable)
    if error:
        p._log("gcode", f"{word} failed: {error}")
        return
    if customizable:
        material = _edited(p, material, c)
        if source is not None:
            if not _writable(p, source, word):
                return
            source.store(_landed(p, source, material))
            p._log("gcode", f"{word} T{source.index} set to {describe(source.material)}")
    if load is None:
        p._log("gcode", f"{word}: no tool loaded")
        return
    t = _tool(p, load)
    if not _writable(p, t, word):
        return
    if source is not None:
        _copy_filament(p, t, source)
        return
    t.store(_landed(p, t, material))
    p._log("gcode", f"{word} T{t.index} set to {describe(t.material)}")


def _tool(p: SimPrinter, index: int) -> SimTool:
    # A write to a disabled or unconfigured tool lands; a disabled tool just isn't reported.
    t = p.tools.get(index)
    if t is None:
        t = p.tools[index] = SimTool(index, enabled=False)
    return t


def _writable(p: SimPrinter, t: SimTool, word: str) -> bool:
    if t.stuck_write:
        p._log("gcode", f"{word} T{t.index} ignored: stuck write")
    return not t.stuck_write


def _edited(p: SimPrinter, material: Material, c: gcode.SetFilament) -> Material:
    """The type's parameters after the line: `R` starts from the defaults, with no name,
    color or UUIDs (`M865.cpp:83-85`, `FilamentTypeParameters` in `filament.hpp`)."""
    if c.reset:
        nozzle, preheat, bed = DEFAULT_TEMPERATURES
        material = Material("", nozzle=nozzle, preheat=preheat, bed=bed)
    else:
        material = replace(material)
    if c.name is not None:
        material.name = c.name
    material.color = c.color or material.color
    for key in ("nozzle", "preheat"):
        if key in c.temps:
            setattr(material, key, c.temps[key])
    if "bed" in c.temps:
        material.bed = c.temps["bed"] % 256  # stored in a byte (`filament.cpp:303-313`)
    if p.model.uuids:  # `M865.1` reads UUIDs only on a build with `HAS_ANFC`
        for key, value in c.uuids.items():
            setattr(material, f"{key}_uuid", value)
    return material


def _landed(p: SimPrinter, t: SimTool, material: Material) -> Material:
    """`material` as tool `t` stores it: only tool 0 of a tag build keeps UUIDs."""
    material = replace(material)
    if not p.model.holds_uuid(t.index):
        material.instance_uuid = material.material_uuid = material.package_uuid = None
    return material


def _copy_filament(p: SimPrinter, t: SimTool, source: SimTool) -> None:
    if source.material is None:
        p._count_unload(t, f"M865 copied empty T{source.index}")
        return
    t.store(_landed(p, t, source.material))
    p._log("gcode", f"M865 T{t.index} copied from T{source.index}: {describe(t.material)}")
