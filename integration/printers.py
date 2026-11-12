"""A second printer for `world`.

`world` takes `world_printers` at 2 to add printer "b": a Core One L like printer "it",
with its own PrusaLink server on the same `Simulation`, location `<prefix> LB`, and
spool E's white PLA loaded and in that location. `World.at(name)` gives either printer
as a `Station`. Ledger records name their printer, so `printer_of()` splits them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from aiohttp.test_utils import TestServer

from integration import station
from integration.temperatures import TEMPERATURES, WRITTEN
from simulator.model import MODELS, Material, PrintSpec, SimPrinter, SimTool, Simulation
from simulator.prusalink import make_app
from spoolman_prusalink.accounting import ToolState
from spoolman_prusalink.config import PrinterConfig

if TYPE_CHECKING:
    from integration.conftest import SpoolmanHelper, World

# Spool E, loaded in printer b: a PLA that none of A to D share.
PLA_WHITE = ("PLA", "#f2f2f2")
SECOND = "b"


@dataclass(kw_only=True)
class Station(station.Station):
    """One printer of `world`, as `World.at()` returns it.

    The moves that tick return the ledger records the bridge wrote meanwhile.
    """

    world: World
    location: str  # Tool 0's Spoolman location.
    server: TestServer | None = None  # None for printer "it", which `world` serves.

    def __post_init__(self) -> None:
        # The world's bridge changes at a restart, so look it up at each poll.
        self.poll = lambda: self.world.bridge.poll()

    @property
    def name(self) -> str:
        return self.printer.name

    @property
    def state(self) -> ToolState | None:
        """The bridge's state for this printer's tool 0, None before its first read."""
        return self.world.bridge.state.tools(self.name).get(0)

    def extrude(self, mm: float) -> None:
        self.printer.extrude(0, mm)

    async def run_until(
        self, done: Callable[[], bool], *, limit: float = 30.0, poll_every: float | None = None
    ) -> list[dict]:
        mark = self.world.mark()
        await super().run_until(done, limit=limit, poll_every=poll_every)
        return self.world.since(mark)

    async def swap_cycle(
        self, poll_every: float | None = None, *, tool: int = 0, **load
    ) -> list[dict]:
        """Load a tool and tick until the load is done; `load` goes to `SimPrinter.load`.

        A tag load reports `TEMPERATURES` unless `load` gives others.
        """
        if load.get("source") == "tag":
            load.setdefault("temperatures", TEMPERATURES)
        mark = self.world.mark()
        await super().swap_cycle(poll_every, tool=tool, **load)
        return self.world.since(mark)

    async def start_print(self, spec: PrintSpec) -> list[dict]:
        mark = self.world.mark()
        await super().start_print(spec)
        return self.world.since(mark)


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
    )
    return [(SECOND, printer, location, server, config)]
