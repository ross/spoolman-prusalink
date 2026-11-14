"""The sync job's G-code interpreter: what each parsed command does to a printer.

`gcode` parses the job. The job's lifecycle (holding on a problem, running, the phases) stays
in `model`, which calls `tool_problem` and `apply` here, and `wrong_model` when a file is
printed. Material writes go through the `SimTool` methods.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from simulator import gcode
from simulator.tool import DEFAULT_TEMPERATURES, Material, SimTool, describe

if TYPE_CHECKING:
    from simulator.model import SimPrinter


def wrong_model(p: SimPrinter, body: str) -> str | None:
    """The model a file's M862.3 names when it isn't this one. A parse error is left to the
    job, which stops on it."""
    try:
        commands = gcode.parse(body)
    except gcode.GcodeError:
        return None
    names = (p.model.name, f"{p.model.name}MMU3")
    for c in commands:
        if isinstance(c, gcode.ModelCheck) and c.name not in names:
            return c.name
    return None


def tool_problem(p: SimPrinter, commands: list[gcode.Command]) -> str | None:
    # The firmware takes any index the model can have, enabled or not.
    last = p.model.max_tools - 1
    for c in commands:
        if not isinstance(c, gcode.SetFilament):
            continue
        for index in (c.tool, c.copy_from):
            if index is not None and not 0 <= index <= last:
                return f"M865 names tool {index}; {p.model.name} has tools 0 to {last}"
    return None


def apply(p: SimPrinter, commands: list[gcode.Command]) -> None:
    for c in commands:
        if isinstance(c, gcode.SetFilament):
            _apply_filament(p, c)
        elif isinstance(c, gcode.SetUuid):
            _apply_uuid(p, c)
        elif isinstance(c, gcode.Message):
            p.message = c.text
            p._log("gcode", f"M117 {c.text}")
        elif isinstance(c, gcode.Home):
            p.homed = True
            p._log("gcode", "G28: homed")
        elif isinstance(c, gcode.ModelCheck):
            p._log("gcode", f"M862.3 model check {c.name}")
        else:
            p._log("gcode", f"ignored: {c.line}")


def _apply_filament(p: SimPrinter, c: gcode.SetFilament) -> None:
    if not 0 <= c.tool < p.model.max_tools:
        p._log("gcode", f"M865 L{c.tool} dropped: no such tool")
        return
    # The config store has a slot for every tool the model can have, so a write to a
    # disabled or unconfigured tool lands; a disabled tool just isn't reported.
    t = p.tools.get(c.tool)
    if t is None:
        t = p.tools[c.tool] = SimTool(c.tool, enabled=False)
    if t.stuck_write:
        p._log("gcode", f"M865 L{c.tool} ignored: stuck write")
        return
    if c.copy_from is not None:
        _copy_filament(p, t, p.tools.get(c.copy_from) or SimTool(c.copy_from))
        return
    if c.reset or c.adhoc:
        # An ad-hoc write edits the ad-hoc slot; R starts it afresh.
        old = t.previous
        if c.reset or old is None:
            # R starts from the firmware defaults, not a preset's values.
            nozzle, preheat, bed = DEFAULT_TEMPERATURES
            name = c.name or (old.name if old else "Custom")
            material = Material(name, nozzle=nozzle, preheat=preheat, bed=bed)
        else:
            material = replace(old, adhoc=True)
        material.name = c.name or material.name
        material.color = c.color or material.color
        _apply_temperatures(material, c.temps)
    elif c.name:
        # A preset write: the firmware ignores every parameter but the type. A name the
        # build has no preset for doesn't parse (`FilamentType::from_name` in
        # `src/common/filament_common.cpp`), so `M865` prints an error and stores nothing.
        if c.name not in p.model.presets:
            p._log("gcode", f"M865 L{c.tool} failed: {p.model.name} has no {c.name} preset")
            return
        material = Material(c.name, adhoc=False, **p.model.preset_temperatures(c.name))
        if c.color or c.temps:
            p._log("gcode", f"M865 L{c.tool}: parameters ignored on preset {c.name}")
    elif (c.color or c.temps) and t.material is not None:
        # An edit of the stored type, which only an ad-hoc type takes.
        if not t.material.adhoc:
            p._log("gcode", f"M865 L{c.tool} ignored on preset {t.material.name}")
            return
        material = replace(t.material, color=c.color or t.material.color)
        _apply_temperatures(material, c.temps)
    else:
        p._log("gcode", f"M865 L{c.tool}: nothing to write")
        return
    t.store(material)
    p._log("gcode", f"M865 T{c.tool} set to {describe(t.material)}")


def _copy_filament(p: SimPrinter, t: SimTool, source: SimTool) -> None:
    if source.material is None:
        p._count_unload(t, f"M865 copied empty T{source.index}")
        return
    material = replace(source.material)
    if not p.model.holds_uuid(t.index):
        material.instance_uuid = material.material_uuid = material.package_uuid = None
    t.store(material)
    p._log("gcode", f"M865 T{t.index} copied from T{source.index}: {describe(material)}")


def _apply_uuid(p: SimPrinter, c: gcode.SetUuid) -> None:
    t = p.tools.get(c.tool)
    usable = p.model.holds_uuid(c.tool) and t is not None and t.material is not None
    if not usable or t.stuck_write:
        p._log("gcode", f"M865.1 I{c.tool} dropped")
        return
    if not t.material.adhoc:
        p._log("gcode", f"M865.1 I{c.tool} ignored on preset {t.material.name}")
        return
    t.store(replace(t.material, instance_uuid=c.uuid))
    p._log("gcode", f"M865.1 T{c.tool} instance UUID {c.uuid}")


def _apply_temperatures(material: Material, temps: dict[str, int]) -> None:
    for key in ("nozzle", "preheat", "bed"):
        if key in temps:
            setattr(material, key, temps[key])
