"""Builders shared by the simulator tests."""

from __future__ import annotations

from simulator.mmu import MMU_SLOTS, MmuUnit
from simulator.model import MODELS, Material, Settings, SimPrinter, SimTool, Simulation

PLA = Material("PLA", "#73737b", "44c406a5-c6f5-4440-bfba-e8060564b717")
KEY = "sim-key"


def loaded(index: int, material: Material = PLA, **kw) -> SimTool:
    return SimTool.loaded_with(index, material, **kw)


def make(
    model: str = "8.1.0", tools: list[SimTool] | None = None, **settings
) -> tuple[Simulation, SimPrinter]:
    """One printer, tool 0 loaded with PLA by default, every delay as configured."""
    tools = tools if tools is not None else [loaded(0)]
    sim = Simulation(Settings(**settings))
    printer = SimPrinter("p", 8001, KEY, MODELS[model], tools={t.index: t for t in tools})
    sim.add(printer)
    return sim, printer


def make_mmu(
    model: str = "1.4.1",
    types: dict[int, Material] | None = None,
    slot: int | None = None,
    **settings,
) -> tuple[Simulation, SimPrinter]:
    """One printer with an MMU3, on and Active: an MK4S by default, slot 0 typed PLA and
    the others empty. `slot` is the slot loaded to the nozzle and selected, as a finished
    load leaves it; None leaves the extruder empty."""
    types = {0: PLA} if types is None else types
    tools = {i: SimTool(i) for i in range(MMU_SLOTS)}
    for i, material in types.items():
        tools[i].store(material)
    unit = MmuUnit() if slot is None else MmuUnit.loaded_with(slot)
    sim = Simulation(Settings(**settings))
    printer = SimPrinter("p", 8001, KEY, MODELS[model], tools=tools, selected_tool=slot, mmu=unit)
    sim.add(printer)
    return sim, printer


def sliced(printer: SimPrinter, body: str) -> str:
    """`body` after the checks a slicer writes for `printer`'s model, so the print preview
    asks nothing about it."""
    model = printer.model
    features = "".join(f'M862.6 P"{f}"\n' for f in sorted(model.checks.required))
    return f'M862.3 P "{model.name}"\nM862.5 P2\n{features}{body}'


def run(sim: Simulation, seconds: float, step: float = 0.1) -> None:
    """Tick in small steps, as the server does."""
    t = 0.0
    while t < seconds - 1e-9:
        sim.tick(min(step, seconds - t))
        t += step
