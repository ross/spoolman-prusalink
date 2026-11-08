"""Fixtures that run the bridge, the simulator and a real Spoolman together, in lockstep.

Nothing here waits on the wall clock. The simulator's time moves only when a test ticks
it, and the bridge polls only when a test calls `poll()`, so every scenario reads the
same way each run. Spoolman is the one external piece: its URL comes from
`SPOOLMAN_URL`, and every test without it is skipped.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
import pytest
from aiohttp.test_utils import TestServer

from simulator.model import MODELS, Material, Settings, SimPrinter, SimTool, Simulation
from simulator.prusalink import make_app
from spoolman_prusalink.app import Bridge
from spoolman_prusalink.config import Config, PrinterConfig

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
)
STEP = 0.1
KEY = "it-key"
FLAG_FIELD = "bridge_flag"
UUID_FIELD = "instance_uuid"

PLA = ("PLA", "#73737b")
PETG = ("PETG", "#1f6fb2")
ASA = ("ASA", "#d9342b")


@pytest.fixture(scope="session")
def spoolman_url() -> str:
    """The Spoolman under test, or a skip that says how to provide one."""
    url = os.environ.get("SPOOLMAN_URL")
    if not url:
        pytest.skip(
            "integration tests need a Spoolman: set SPOOLMAN_URL (see integration/README.md)"
        )
    return url.rstrip("/")


class SpoolmanHelper:
    """Creates this test's Spoolman records and removes them afterwards.

    Every record carries the test's prefix: spools in `comment`, the vendor and filaments
    in their names. Teardown deletes only what this helper created, so a Spoolman shared
    with a running bridge is left as it was.
    """

    def __init__(self, client: httpx.AsyncClient, prefix: str) -> None:
        self.client = client
        self.prefix = prefix
        self.spools: list[int] = []
        self.filaments: list[int] = []
        self.vendors: list[int] = []

    async def _post(self, path: str, body: dict) -> dict:
        resp = await self.client.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    async def ensure_text_field(self, key: str, name: str) -> None:
        """Register a text extra field if it's missing, as the bridge would on first use.

        Spoolman refuses an extra value for a field it doesn't know, and spool A is
        created with an instance UUID before the bridge has run.
        """
        resp = await self.client.get("/api/v1/field/spool")
        resp.raise_for_status()
        if any(f.get("key") == key for f in resp.json()):
            return
        await self._post(f"/api/v1/field/spool/{key}", {"name": name, "field_type": "text"})

    async def vendor(self) -> int:
        data = await self._post("/api/v1/vendor", {"name": f"{self.prefix} vendor"})
        self.vendors.append(data["id"])
        return data["id"]

    async def filament(self, vendor_id: int, material: str, color: str, **kw) -> int:
        body = {
            "name": f"{self.prefix} {material}",
            "vendor_id": vendor_id,
            "material": material,
            "density": 1.24,
            "diameter": 1.75,
            "weight": 1000,
            "color_hex": color.lstrip("#"),
            "settings_extruder_temp": 220,
            "settings_bed_temp": 60,
            **kw,
        }
        data = await self._post("/api/v1/filament", body)
        self.filaments.append(data["id"])
        return data["id"]

    async def spool(
        self, filament_id: int, location: str, label: str, instance_uuid: str | None = None
    ) -> int:
        body: dict = {
            "filament_id": filament_id,
            "location": location,
            "comment": f"{self.prefix} {label}",
        }
        if instance_uuid is not None:
            body["extra"] = {UUID_FIELD: json.dumps(instance_uuid)}
        data = await self._post("/api/v1/spool", body)
        self.spools.append(data["id"])
        return data["id"]

    async def get(self, spool_id: int) -> dict:
        resp = await self.client.get(f"/api/v1/spool/{spool_id}")
        resp.raise_for_status()
        return resp.json()

    async def move(self, spool_id: int, location: str | None) -> None:
        resp = await self.client.patch(f"/api/v1/spool/{spool_id}", json={"location": location})
        resp.raise_for_status()

    async def used(self, spool_id: int) -> float:
        return float((await self.get(spool_id)).get("used_weight") or 0.0)

    async def extra(self, spool_id: int, key: str) -> str | None:
        """Decode a text extra field; None if it's unset or empty."""
        raw = (await self.get(spool_id)).get("extra", {}).get(key)
        value = json.loads(raw) if raw else None
        return value or None

    async def location(self, spool_id: int) -> str | None:
        return (await self.get(spool_id)).get("location") or None

    async def tagged(self) -> list[dict]:
        """Every spool whose comment carries this prefix, archived ones included."""
        resp = await self.client.get("/api/v1/spool", params={"allow_archived": True})
        resp.raise_for_status()
        return [s for s in resp.json() if (s.get("comment") or "").startswith(self.prefix)]

    async def cleanup(self) -> None:
        # Spools hold filaments and filaments hold the vendor, so delete in that order.
        # A spool missing from the list is picked up by its comment, in case a test failed
        # between creating it and recording its id.
        spools = set(self.spools) | {s["id"] for s in await self.tagged()}
        for path, ids in (
            ("spool", sorted(spools)),
            ("filament", self.filaments),
            ("vendor", self.vendors),
        ):
            for i in ids:
                resp = await self.client.delete(f"/api/v1/{path}/{i}")
                if resp.status_code != 404:
                    resp.raise_for_status()


@pytest.fixture
def prefix() -> str:
    return f"it-{uuid.uuid4().hex[:6]}"


@pytest.fixture
async def spoolman(spoolman_url: str, prefix: str):
    async with httpx.AsyncClient(base_url=spoolman_url, timeout=30.0) as client:
        helper = SpoolmanHelper(client, prefix)
        try:
            yield helper
        finally:
            await helper.cleanup()


def advance(sim: Simulation, seconds: float) -> None:
    """Move simulated time on by `seconds`, in the clock task's 0.1 s steps."""
    t = 0.0
    while t < seconds - 1e-9:
        sim.tick(min(STEP, seconds - t))
        t += STEP


def ledger(cfg: Config) -> list[dict]:
    path = Path(cfg.ledger_file)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def events(records: list[dict], kind: str | None = None) -> list[dict]:
    """Event records, optionally only one `accounting.EventKind` value."""
    return [r for r in records if "event" in r and (kind is None or r["event"] == kind)]


def charges(records: list[dict], spool_id: int | None = None) -> list[dict]:
    """Charges Spoolman accepted, the ones `flush()` logs with `applied`."""
    return [
        r for r in records if "applied" in r and (spool_id is None or r["spool_id"] == spool_id)
    ]


def charged(records: list[dict], spool_id: int) -> float:
    return round(sum(r["use_length"] for r in charges(records, spool_id)), 1)


@dataclass
class World:
    """One test's printer, bridge and Spoolman records, with the moves a scenario makes.

    `mark()` and `since()` cut the ledger into the part each step of a scenario wrote.
    """

    sim: Simulation
    printer: SimPrinter
    bridge: Bridge
    cfg: Config
    spoolman: SpoolmanHelper
    tool_location: str
    shelf: str
    spools: dict[str, int] = field(default_factory=dict)
    uuids: dict[str, str] = field(default_factory=dict)

    @property
    def tool(self) -> SimTool:
        return self.printer.tools[0]

    def advance(self, seconds: float) -> None:
        advance(self.sim, seconds)

    async def poll(self) -> list[dict]:
        """Poll once and return the ledger records it wrote."""
        mark = self.mark()
        await self.bridge.poll()
        return self.since(mark)

    def mark(self) -> int:
        return len(ledger(self.cfg))

    def since(self, mark: int) -> list[dict]:
        return ledger(self.cfg)[mark:]

    async def run_until(
        self, done: Callable[[], bool], *, limit: float = 30.0, poll_every: float | None = None
    ) -> list[dict]:
        """Tick until `done()`, polling every `poll_every` simulated seconds if set.

        Returns the ledger records written meanwhile. Fails rather than spin if `done()`
        never comes true within `limit` seconds.
        """
        mark = self.mark()
        t = since_poll = 0.0
        while not done():
            assert t < limit, f"not done after {limit} simulated seconds"
            self.sim.tick(STEP)
            t += STEP
            since_poll += STEP
            if poll_every is not None and since_poll >= poll_every - 1e-9:
                await self.bridge.poll()
                since_poll = 0.0
        return self.since(mark)

    async def unload(self) -> None:
        self.printer.unload(0)
        await self.run_until(lambda: self.tool.process is None)

    async def swap_cycle(self, poll_every: float | None = None, **load) -> list[dict]:
        """Load tool 0 and tick until the load is done; `load` goes to `SimPrinter.load`."""
        self.printer.load(0, **load)
        return await self.run_until(lambda: self.tool.process is None, poll_every=poll_every)

    async def move(self, name: str, location: str | None) -> None:
        await self.spoolman.move(self.spools[name], location)

    async def used(self, name: str) -> float:
        return await self.spoolman.used(self.spools[name])

    async def flag(self, name: str) -> str | None:
        return await self.spoolman.extra(self.spools[name], FLAG_FIELD)

    def charged(self, records: list[dict], name: str) -> float:
        return charged(records, self.spools[name])

    def requests(self, method: str = "PUT") -> list:
        return [e for e in self.sim.events if e.kind == "request" and e.text.startswith(method)]

    def material(self) -> tuple[str | None, str | None, str | None]:
        m = self.tool.material
        return (None, None, None) if m is None else (m.name, m.color, m.instance_uuid)


@pytest.fixture
async def world(spoolman: SpoolmanHelper, prefix: str, tmp_path: Path):
    """The starting point every scenario shares.

    A Core One L whose tool 0 is loaded with spool A's PLA, A's instance UUID and some
    extrusion on the counter; spool A in the tool's location; B (PETG) and C (ASA) on
    this test's shelf. Each filament weighs 1000 g and no spool has used any, so a
    runout always leaves plenty and trips the mid-print flag.
    """
    tool_location, shelf = f"{prefix} T", f"{prefix} shelf"
    await spoolman.ensure_text_field(UUID_FIELD, "Instance UUID")
    vendor = await spoolman.vendor()
    uuid_a = str(uuid.uuid4())
    spools = {}
    for name, (material, color), location in (
        ("A", PLA, tool_location),
        ("B", PETG, shelf),
        ("C", ASA, shelf),
    ):
        filament = await spoolman.filament(vendor, material, color)
        spools[name] = await spoolman.spool(
            filament, location, name, uuid_a if name == "A" else None
        )

    sim = Simulation(Settings(**SETTINGS))
    a = Material(PLA[0], PLA[1], uuid_a)
    tool = SimTool(0, loaded=True, material=replace(a), previous=replace(a), extruded=1000.0)
    printer = SimPrinter("it", 0, KEY, MODELS["8.1.0"], tools={0: tool})
    sim.add(printer)
    server = TestServer(make_app(sim, printer.name))
    await server.start_server()
    cfg = Config(
        spoolman_url=str(spoolman.client.base_url).rstrip("/"),
        printers=(
            PrinterConfig(
                name="it",
                url=f"http://{server.host}:{server.port}",
                api_key=KEY,
                tools={0: tool_location},
                uuids=True,
            ),
        ),
        state_file=tmp_path / "state.json",
        ledger_file=tmp_path / "ledger.jsonl",
        dry_run=False,
    )
    bridge = Bridge.from_config(cfg)
    w = World(sim, printer, bridge, cfg, spoolman, tool_location, shelf, spools, {"A": uuid_a})
    try:
        yield w
    finally:
        await bridge.aclose()
        await server.close()
