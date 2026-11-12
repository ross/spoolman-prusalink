"""The printers of `world`: what each starts with, and its `Station`.

`world` builds every printer from a `Spec`: its model, and for each tool the spool
that's loaded and where that spool is in Spoolman. Printer "it" is always there. With
`world_printers` at 2 there's also printer "b": a Core One L with its own PrusaLink
server on the same `Simulation`, location `<prefix> LB`, and spool E's white PLA
loaded. `World.at(name)` gives either printer as a `Station`. Ledger records name
their printer, so `printer_of()` splits them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from aiohttp.test_utils import TestServer

from integration import station
from integration.temperatures import TEMPERATURES, WRITTEN
from simulator.model import MODELS, Material, PrintSpec, SimPrinter, SimTool
from spoolman_prusalink.accounting import ToolState
from spoolman_prusalink.config import PrinterConfig

if TYPE_CHECKING:
    from integration.conftest import World

KEY = "it-key"
# Spool E, loaded in printer b: a PLA that none of A to D share.
PLA_WHITE = ("PLA", "#f2f2f2")
FIRST = "it"
SECOND = "b"


@dataclass
class Load:
    """What a tool starts with: a spool, loaded in the tool and in its Spoolman location."""

    spool: str  # The spool's name in `World.spools`, such as "A".
    filament: tuple[str, str]  # Material and color.
    location: str
    extruded: float  # The tool's counter.
    uuid: str | None = None  # The instance UUID on the spool and on the printer, if any.


@dataclass
class Spec:
    """One printer for `world` to build: its name, model code, and tools in index order."""

    name: str
    model: str
    loads: list[Load]
    config: dict = field(default_factory=dict)  # Overrides for its `PrinterConfig`.


@dataclass(kw_only=True)
class Station(station.Station):
    """One printer of `world`, as `World.at()` returns it.

    The moves that tick return the ledger records the bridge wrote meanwhile.
    """

    world: World
    locations: dict[int, str]  # Tool index -> Spoolman location, as the bridge is configured.
    # Serves the printer's PrusaLink to the bridge; `start` sets it.
    server: TestServer = field(init=False)

    def __post_init__(self) -> None:
        # The world's bridge changes at a restart, so look it up at each poll.
        self.poll = lambda: self.world.bridge.poll()

    @property
    def name(self) -> str:
        return self.printer.name

    @property
    def location(self) -> str:
        """Tool 0's Spoolman location."""
        return self.locations[0]

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


async def start(spec: Spec, world: World) -> tuple[Station, PrinterConfig]:
    """Add `spec`'s printer to the world's simulation and serve it.

    Returns its station and the bridge's config for it. The spools its tools hold are
    `world`'s to create.
    """
    tools = {}
    for index, load in enumerate(spec.loads):
        material = Material(*load.filament, load.uuid, **WRITTEN)
        tools[index] = SimTool(
            index,
            loaded=True,
            material=replace(material),
            previous=replace(material),
            extruded=load.extruded,
        )
    printer = SimPrinter(spec.name, 0, KEY, MODELS[spec.model], tools=tools)
    world.sim.add(printer)
    locations = {index: load.location for index, load in enumerate(spec.loads)}
    made = Station(world.sim, printer, world=world, locations=locations)
    made.server = TestServer(made.app())
    await made.server.start_server()
    config = PrinterConfig(
        name=spec.name,
        url=f"http://{made.server.host}:{made.server.port}",
        api_key=KEY,
        tools=dict(locations),
        **spec.config,
    )
    return made, config
