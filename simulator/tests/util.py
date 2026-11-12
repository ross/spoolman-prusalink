"""Builders shared by the simulator tests."""

from __future__ import annotations

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


def run(sim: Simulation, seconds: float, step: float = 0.1) -> None:
    """Tick in small steps, as the server does."""
    t = 0.0
    while t < seconds - 1e-9:
        sim.tick(min(step, seconds - t))
        t += step
