"""A second printer for `world`.

`world` takes `world_printers` at 2 to add printer "b": a Core One L like printer "it",
with its own PrusaLink server on the same `Simulation`, location `<prefix> LB`, and
spool E's white PLA loaded and in that location. `World.at(name)` gives either printer
as a `Station`. Ledger records name their printer, so `printer_of()` splits them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from aiohttp.test_utils import TestServer

from integration.temperatures import WRITTEN
from simulator.model import MODELS, Material, PrintSpec, SimPrinter, SimTool, Simulation, State
from simulator.prusalink import make_app
from spoolman_prusalink.accounting import ToolState
from spoolman_prusalink.config import PrinterConfig

if TYPE_CHECKING:
    from integration.conftest import SpoolmanHelper, World

# Spool E, loaded in printer b: a PLA that none of A to D share.
PLA_WHITE = ("PLA", "#f2f2f2")
SECOND = "b"


@dataclass
class Station:
    """One printer of `world` with its tool 0, as `World.at()` returns it."""

    world: World
    name: str
    printer: SimPrinter
    location: str
    server: TestServer | None = None  # None for printer "it", which `world` serves.

    @property
    def tool(self) -> SimTool:
        return self.printer.tools[0]

    @property
    def counter(self) -> int:
        """Tool 0's extrusion counter as the printer reports it, in whole millimetres."""
        return int(self.tool.extruded)

    @property
    def state(self) -> ToolState | None:
        """The bridge's state for this printer's tool 0, None before its first read."""
        return self.world.bridge.state.tools(self.name).get(0)

    def material(self) -> tuple[str | None, str | None, str | None]:
        m = self.tool.material
        return (None, None, None) if m is None else (m.name, m.color, m.instance_uuid)

    def extrude(self, mm: float) -> None:
        self.printer.extrude(0, mm)

    async def unload(self) -> None:
        self.printer.unload(0)
        await self.world.run_until(lambda: self.tool.process is None)

    async def swap_cycle(self, poll_every: float | None = None, **load) -> list[dict]:
        """Load tool 0 and tick until the load is done; `load` goes to `SimPrinter.load`."""
        self.printer.load(0, **load)
        return await self.world.run_until(lambda: self.tool.process is None, poll_every=poll_every)

    async def start_print(self, spec: PrintSpec) -> list[dict]:
        """Start a print here and poll until the bridge counts it as started.

        As `World.start_print`: two polls once the printer is extruding.
        """
        start = self.tool.extruded
        self.printer.start_print(spec)
        self.world.advance(1.2)
        assert self.printer.state is State.PRINTING and self.tool.extruded > start
        records = await self.world.poll()
        self.world.advance(0.3)
        return records + await self.world.poll()


def printer_of(records: list[dict], name: str) -> list[dict]:
    """The ledger records one printer wrote: charges and events both name it."""
    return [r for r in records if r.get("printer") == name]


async def add_printers(
    count: int,
    sim: Simulation,
    spoolman: SpoolmanHelper,
    vendor: int,
    prefix: str,
    key: str,
    spools: dict[str, int],
    uuids: dict[str, str],
) -> list[tuple[str, SimPrinter, str, TestServer, PrinterConfig]]:
    """Add printer b to `sim` when `count` is 2, with spool E, and serve it.

    Returns its name, simulated printer, location, server and bridge config, for
    `world` to put in its `Config` and its `World`. Adds E to `spools` and `uuids`.
    """
    if count == 1:
        return []
    location = f"{prefix} LB"
    uuid_e = str(uuid.uuid4())
    filament = await spoolman.filament(vendor, *PLA_WHITE)
    spools["E"] = await spoolman.spool(filament, location, "E", uuid_e)
    uuids["E"] = uuid_e
    e = Material(PLA_WHITE[0], PLA_WHITE[1], uuid_e, **WRITTEN)
    tool = SimTool(0, loaded=True, material=replace(e), previous=replace(e), extruded=2000.0)
    printer = SimPrinter(SECOND, 0, key, MODELS["8.1.0"], tools={0: tool})
    sim.add(printer)
    server = TestServer(make_app(sim, printer.name))
    await server.start_server()
    config = PrinterConfig(
        name=SECOND,
        url=f"http://{server.host}:{server.port}",
        api_key=key,
        tools={0: location},
        uuids=True,
    )
    return [(SECOND, printer, location, server, config)]
