"""One simulated printer and the moves a test makes on it.

A plain module, so the unit suite in `tests/` imports it too. The simulator's time moves
only when a test ticks it, so every move here ticks the simulation itself and nothing
waits on the wall clock.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from aiohttp import web

from simulator.model import (
    Event,
    JobPhase,
    PrintSpec,
    Reason,
    SimPrinter,
    SimTool,
    Simulation,
    State,
)
from simulator.prusalink import Hook, Reply, make_app, make_transport
from spoolman_prusalink.accounting import FIRST_EXTRUSION_MM

# Short phases keep a scenario to a few hundred ticks. None is zero, so every phase
# still takes at least one tick and the bridge can see it if a test polls there.
SETTINGS = dict(
    unload_seconds=1.0,
    load_rate_mm_s=100.0,
    scan_seconds=0.2,
    blip_seconds=0.2,
    park_seconds=0.2,
    sync_job_seconds=0.5,
    preheat_seconds=0.5,
    print_rate_mm_s=100.0,
    start_blip=True,
    mmu_connect_seconds=0.2,
)

# The simulator's clock task ticks in steps of this many seconds, so tests do too.
STEP = 0.1


def advance(sim: Simulation, seconds: float) -> None:
    """Move simulated time on by `seconds`, in the clock task's steps."""
    t = 0.0
    while t < seconds - 1e-9:
        sim.tick(min(STEP, seconds - t))
        t += STEP


@dataclass(slots=True)
class Station:
    """A simulated printer, with the bridge's poll for the moves that poll while they wait."""

    sim: Simulation
    printer: SimPrinter
    # Polls the bridge once. The moves that take `poll_every` need it.
    poll: Callable[[], Awaitable[object]] | None = None
    # Runs before the simulator serves each request, with its method and path. It can
    # return an `Answer` to send instead, or `simulator.prusalink.DROP` to drop the
    # request; None serves the request as usual.
    before_request: Hook | None = None

    def _hook(self, method: str, path: str) -> Reply:
        return None if self.before_request is None else self.before_request(method, path)

    def app(self) -> web.Application:
        """The printer's PrusaLink as an aiohttp app, to serve on a socket."""
        return make_app(self.sim, self.printer.name, self._hook)

    def transport(self) -> httpx.MockTransport:
        """The printer's PrusaLink as an httpx transport, served in process."""
        return make_transport(self.sim, self.printer.name, self._hook)

    @property
    def tool(self) -> SimTool:
        return self.tool_at(0)

    @property
    def counter(self) -> int:
        """Tool 0's extrusion counter as the printer reports it, in whole millimetres."""
        return self.counter_at(0)

    def tool_at(self, tool: int) -> SimTool:
        return self.printer.tools[tool]

    def counter_at(self, tool: int) -> int:
        """A tool's extrusion counter as the printer reports it, in whole millimetres."""
        return int(self.tool_at(tool).extruded)

    def material(self, tool: int = 0) -> tuple[str | None, str | None, str | None]:
        """A tool's stored name, color and instance UUID."""
        m = self.tool_at(tool).material
        return (None, None, None) if m is None else (m.name, m.color, m.instance_uuid)

    def requests(self, method: str = "PUT") -> list[Event]:
        """The requests for `method` this printer has logged, each as "METHOD path status"."""
        return [
            e
            for e in self.sim.events
            if e.kind == "request" and e.printer == self.printer.name and e.data["method"] == method
        ]

    def advance(self, seconds: float) -> None:
        advance(self.sim, seconds)

    def tick_until(self, done: Callable[[], bool], *, limit: float = 30.0) -> None:
        """Tick until `done()`. Fails rather than spin if that takes over `limit` seconds."""
        t = 0.0
        while not done():
            assert t < limit, f"not done after {limit} simulated seconds"
            self.sim.tick(STEP)
            t += STEP

    def finish_job(self) -> None:
        """Tick until the printer has no job, as when a sync job runs to its end."""
        self.tick_until(lambda: self.printer.job is None)

    async def run_until(
        self, done: Callable[[], bool], *, limit: float = 30.0, poll_every: float | None = None
    ) -> None:
        """Tick until `done()`, polling every `poll_every` simulated seconds if set.

        Fails rather than spin if `done()` never comes true within `limit` seconds.
        """
        t = since_poll = 0.0
        while not done():
            assert t < limit, f"not done after {limit} simulated seconds"
            self.sim.tick(STEP)
            t += STEP
            since_poll += STEP
            if poll_every is not None and since_poll >= poll_every - 1e-9:
                assert self.poll is not None, "this station has no bridge to poll"
                await self.poll()
                since_poll = 0.0

    async def unload(self, tool: int = 0) -> None:
        """Unload a tool from the menu, tick until the unload is done, and pull the filament
        out of the sensor, as a user who changes spools does: the menu unload leaves it
        there (F48)."""
        self.printer.unload(tool)
        await self.run_until(lambda: self.tool_at(tool).process is None)
        if self.tool_at(tool).path.at_sensor:
            self.printer.pull(tool)

    async def swap_cycle(self, poll_every: float | None = None, *, tool: int = 0, **load) -> None:
        """Load a tool and tick until the load is done; `load` goes to `SimPrinter.load`."""
        self.printer.load(tool, **load)
        await self.run_until(lambda: self.tool_at(tool).process is None, poll_every=poll_every)

    def begin_print(self, spec: PrintSpec) -> None:
        """Start a print and tick through the scan, to the preview's first question or the
        start blip."""
        self.printer.start_print(spec)
        self.tick_until(lambda: self.printer.job.phase is not JobPhase.SCANNING)

    async def start_print(self, spec: PrintSpec) -> None:
        """Start a print and poll until the bridge counts it as started.

        On a toolchanger the tools-mapping screen comes up after the scan for every print
        it applies to; with nothing wrong this answers Print at once. A test that wants to
        see that question uses `begin_print`. The bridge sees a print start when the
        counter rises more than `FIRST_EXTRUSION_MM` past where a poll first saw the job
        (Q4), so this polls in the preheat, ticks until the first tool's counter is past
        that, and polls again. A print whose first tool counts no more than that while its
        job shows never starts for that tool: then the second poll comes once the job is
        gone.
        """
        assert self.poll is not None, "this station has no bridge to poll"
        first = self.tool_at(spec.segments[0].tool)
        self.begin_print(spec)
        job = self.printer.job
        if job.waits_for(Reason.TOOLS_MAPPING) and job.attention.detail is None:
            self.printer.tap_through()
        self.tick_until(lambda: job.phase not in (JobPhase.SCANNING, JobPhase.BLIP))
        assert self.printer.state is State.PRINTING
        await self.poll()
        seen = int(first.extruded)
        self.tick_until(
            lambda: int(first.extruded) - seen > FIRST_EXTRUSION_MM or self.printer.job is not job
        )
        await self.poll()
