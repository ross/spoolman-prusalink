"""Fixtures that run the bridge, the simulator and a real Spoolman together, in lockstep.

Nothing here waits on the wall clock. The simulator's time moves only when a test ticks
it, and the bridge polls only when a test calls `poll()`, so every scenario reads the
same way each run. Spoolman is the one external piece: its URL comes from
`INTEGRATION_SPOOLMAN_URL`, and every test without it is skipped.
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

from integration.printers import Station, add_printers
from integration.temperatures import TEMPERATURES, WRITTEN
from simulator.model import (
    MODELS,
    Material,
    PrintSpec,
    Segment,
    Settings,
    SimPrinter,
    SimTool,
    Simulation,
    State,
)
from simulator.prusalink import Reply, make_app
from spoolman_prusalink.app import Bridge
from spoolman_prusalink.config import Config, PrinterConfig
from spoolman_prusalink.db import read_ledger

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
CLAIM_FIELD = "bridge_claim"

# A load counts the feed after the gap, then the purge under the new material.
LOAD_MM = 36 + 40

PLA = ("PLA", "#73737b")
PETG = ("PETG", "#1f6fb2")
ASA = ("ASA", "#d9342b")
# Spool D's, in tool 1 of a multi-tool world: PETG in a color B doesn't share.
PETG_GREEN = ("PETG", "#2a9d4a")

# The printer `world` simulates, by how many tools it has: a Core One L, or a Core One L
# INDX (no tag reader, no UUIDs, a 27 mm gap) with tools 0 and 1 docked.
WORLD_MODELS = {1: "8.1.0", 2: "8.10.0"}


@pytest.fixture(scope="session")
def spoolman_url() -> str:
    """The Spoolman under test, or a skip that says how to provide one."""
    url = os.environ.get("INTEGRATION_SPOOLMAN_URL")
    if not url:
        pytest.skip(
            "integration tests need a Spoolman: set INTEGRATION_SPOOLMAN_URL "
            "(see integration/README.md)"
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
        # Extra field keys this test owns, deleted at teardown. Only per-test keys go
        # here: deleting a field drops its values on every spool.
        self.fields: list[str] = []

    async def _post(self, path: str, body: dict) -> dict:
        resp = await self.client.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    async def ensure_field(self, key: str, name: str, field_type: str) -> None:
        """Register an extra field if it's missing, as the bridge would on first use.

        Spoolman refuses an extra value for a field it doesn't know, and spool A is
        created with an instance UUID before the bridge has run.
        """
        resp = await self.client.get("/api/v1/field/spool")
        resp.raise_for_status()
        if any(f.get("key") == key for f in resp.json()):
            return
        await self._post(f"/api/v1/field/spool/{key}", {"name": name, "field_type": field_type})

    async def ensure_text_field(self, key: str, name: str) -> None:
        await self.ensure_field(key, name, "text")

    async def fields_by_key(self) -> dict[str, str]:
        """Every spool extra field Spoolman has, as key -> field type."""
        resp = await self.client.get("/api/v1/field/spool")
        resp.raise_for_status()
        return {f["key"]: f["field_type"] for f in resp.json()}

    async def create_field(self, key: str, name: str, field_type: str) -> None:
        """Register one of this test's own fields, which teardown deletes."""
        self.fields.append(key)
        await self._post(f"/api/v1/field/spool/{key}", {"name": name, "field_type": field_type})

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

    async def edit_filament(self, spool_id: int, **fields) -> None:
        """Change the spool's filament, as the user would edit it in Spoolman."""
        filament_id = (await self.get(spool_id))["filament"]["id"]
        resp = await self.client.patch(f"/api/v1/filament/{filament_id}", json=fields)
        resp.raise_for_status()

    async def claim(self, spool_id: int) -> None:
        """Tick the spool's Claim held usage box, as the user would in Spoolman."""
        await self.ensure_field(CLAIM_FIELD, "Claim held usage", "boolean")
        resp = await self.client.patch(
            f"/api/v1/spool/{spool_id}", json={"extra": {CLAIM_FIELD: "true"}}
        )
        resp.raise_for_status()

    async def used(self, spool_id: int) -> float:
        return float((await self.get(spool_id)).get("used_weight") or 0.0)

    async def extra(self, spool_id: int, key: str) -> str | bool | None:
        """Decode an extra field; None if it's unset, empty or false."""
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
        # Only this test's own fields: a test that let the bridge register them records
        # the keys whether or not the bridge got that far.
        for key in self.fields:
            resp = await self.client.delete(f"/api/v1/field/spool/{key}")
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


def print_spec() -> PrintSpec:
    """A 400 mm print that takes 4 s after a 0.5 s preheat."""
    return PrintSpec([Segment(0, 400)], duration_s=4, preheat_s=0.5)


def logged(caplog: pytest.LogCaptureFixture, level: int, text: str) -> bool:
    """Whether the bridge logged `text` at `level`."""
    return any(r.levelno == level and text in r.getMessage() for r in caplog.records)


def count(caplog: pytest.LogCaptureFixture, level: int, text: str) -> int:
    """How many times the bridge logged `text` at `level`."""
    return sum(1 for r in caplog.records if r.levelno == level and text in r.getMessage())


def ledger(cfg: Config) -> list[dict]:
    return read_ledger(cfg.db_file)


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
    # Tool index -> Spoolman location, as the bridge is configured. Tool 0's is `tool_location`.
    locations: dict[int, str] = field(default_factory=dict)
    # The bridge's extra field keys: "uuid", "flag" and "claim". See `world_fields`.
    fields: dict[str, str] = field(
        default_factory=lambda: {"uuid": UUID_FIELD, "flag": FLAG_FIELD, "claim": CLAIM_FIELD}
    )
    # Runs before the simulator serves each request, with its method and path. It can
    # return a response to send instead, or `simulator.prusalink.DROP` to drop the
    # connection; None serves the request as usual.
    before_request: Callable[[str, str], Reply] | None = None
    # --- Several printers (world_printers; see integration/printers.py) ---
    # Printers other than "it", by name.
    others: dict[str, Station] = field(default_factory=dict)

    def at(self, name: str) -> Station:
        """One printer by name, "it" included, with its tool 0."""
        if name == self.printer.name:
            return Station(self, name, self.printer, self.tool_location)
        return self.others[name]

    # --- End of several printers ---

    # Every bridge this world started, the current one last; the fixture closes them all.
    bridges: list[Bridge] = field(default_factory=list)

    async def restart(self, **config) -> None:
        """Stop the bridge and start a new one on the same database, with `config` changed.

        The bridge's restart (B2) and a lost database (B4) are tested this way; the new
        bridge runs on the simulator's clock like the first.
        """
        await self.bridge.aclose()
        self.cfg = replace(self.cfg, **config)
        self.bridge = Bridge.from_config(self.cfg)
        self.bridge.clock = lambda: self.sim.now
        self.bridges.append(self.bridge)

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

    async def finish_print(self) -> list[dict]:
        """Poll every second until the print ends, then once more idle; return the records."""
        mark = self.mark()
        await self.run_until(
            lambda: self.printer.state is State.FINISHED and self.printer.job is None,
            poll_every=1.0,
        )
        return self.since(mark) + await self.poll()

    async def unload(self, tool: int = 0) -> None:
        self.printer.unload(tool)
        await self.run_until(lambda: self.tool_at(tool).process is None)

    async def swap_cycle(
        self, poll_every: float | None = None, *, tool: int = 0, **load
    ) -> list[dict]:
        """Load a tool and tick until the load is done; `load` goes to `SimPrinter.load`.

        A tag load reports `TEMPERATURES` unless `load` gives others.
        """
        if load.get("source") == "tag":
            load.setdefault("temperatures", TEMPERATURES)
        self.printer.load(tool, **load)
        return await self.run_until(
            lambda: self.tool_at(tool).process is None, poll_every=poll_every
        )

    async def start_print(self, spec: PrintSpec) -> list[dict]:
        """Start a print and poll until the bridge counts it as started.

        Scan, blip and a 0.5 s preheat take 0.9 s. The bridge sees a print start when the
        counter moves between two polls of the same job, so this polls twice once the
        printer is extruding.
        """
        first = self.tool_at(spec.segments[0].tool)
        start = first.extruded
        self.printer.start_print(spec)
        self.advance(1.2)
        assert self.printer.state is State.PRINTING and first.extruded > start
        records = await self.poll()
        self.advance(0.3)
        return records + await self.poll()

    async def move(self, name: str, location: str | None) -> None:
        await self.spoolman.move(self.spools[name], location)

    async def claim(self, name: str) -> None:
        await self.spoolman.claim(self.spools[name])

    async def used(self, name: str) -> float:
        return await self.spoolman.used(self.spools[name])

    async def flag(self, name: str) -> str | None:
        return await self.spoolman.extra(self.spools[name], self.fields["flag"])

    def charged(self, records: list[dict], name: str) -> float:
        return charged(records, self.spools[name])

    def requests(self, method: str = "PUT") -> list:
        return [e for e in self.sim.events if e.kind == "request" and e.text.startswith(method)]

    def material(self, tool: int = 0) -> tuple[str | None, str | None, str | None]:
        m = self.tool_at(tool).material
        return (None, None, None) if m is None else (m.name, m.color, m.instance_uuid)

    @property
    def spoolman_faults(self):
        """The bridge's Spoolman transport, which a test can cut or make refuse requests.

        Installed on first use; see `integration/faults.py`.
        """
        from integration.faults import install

        return install(self.bridge)


@pytest.fixture
def world_config(request: pytest.FixtureRequest) -> dict:
    """Bridge settings `world` overrides in its `Config`, such as `sync_drift`.

    None by default. A test sets them with
    `@pytest.mark.parametrize("world_config", [dict(...)], indirect=True)`.
    """
    return getattr(request, "param", {})


@pytest.fixture
def world_tools(request: pytest.FixtureRequest) -> int:
    """How many tools `world`'s printer has: 1 by default, or 2 for a multi-tool world.

    A test asks for two with
    `@pytest.mark.parametrize("world_tools", [2], indirect=True)`.
    """
    tools = getattr(request, "param", 1)
    assert tools in WORLD_MODELS, f"world_tools must be one of {sorted(WORLD_MODELS)}"
    return tools


@pytest.fixture
def world_model(request: pytest.FixtureRequest, world_tools: int) -> str:
    """The printer type code `world`'s printer reports, such as `1.4.0` for an MK4.

    By default it follows `world_tools`. A test asks for a single-tool model that has no
    tag reader, such as an MK4 (`1.4.0`) or an MK3.9 (`1.3.9`), with
    `@pytest.mark.parametrize("world_model", ["1.4.0"], indirect=True)`. Neither the
    printer nor spool A has a UUID then, as with `world_fields` "fresh".
    """
    code = getattr(request, "param", WORLD_MODELS[world_tools])
    assert code in MODELS, f"world_model {code!r} isn't a simulated model"
    assert world_tools == 1 or code == WORLD_MODELS[world_tools]
    return code


@pytest.fixture
def world_printer(request: pytest.FixtureRequest) -> dict:
    """Settings `world` overrides in printer "it"'s `PrinterConfig`, such as `sync_file`.

    None by default. A test sets them with
    `@pytest.mark.parametrize("world_printer", [dict(...)], indirect=True)`.
    """
    return getattr(request, "param", {})


@pytest.fixture
def world_fields(request: pytest.FixtureRequest) -> str:
    """Whether `world` registers the bridge's extra fields first: "shared" or "fresh".

    "shared", the default, registers the `instance_uuid` field before the bridge runs,
    because spool A is created with a UUID, and the bridge configured with the usual
    keys finds `bridge_flag` and `bridge_claim` there from earlier tests.

    "fresh" registers nothing, so the bridge's own field setup runs as on a new
    Spoolman. Spoolman's fields are shared by every spool, and deleting one drops its
    values everywhere, so the bridge gets keys of this test's own instead, such as
    `it_3fa2c1_flag`, in `World.fields`. Spool A has no UUID, in Spoolman or on the
    printer. Teardown deletes the fields, and any a test makes with
    `SpoolmanHelper.create_field`. A test asks for it with
    `@pytest.mark.parametrize("world_fields", ["fresh"], indirect=True)`.
    """
    fields = getattr(request, "param", "shared")
    assert fields in ("shared", "fresh"), "world_fields must be 'shared' or 'fresh'"
    return fields


# --- Several printers (docs/scenarios.md X rows; see integration/printers.py) ---
@pytest.fixture
def world_printers(request: pytest.FixtureRequest) -> int:
    """How many printers `world` has: 1 by default, or 2 to add printer "b".

    A test asks for two with
    `@pytest.mark.parametrize("world_printers", [2], indirect=True)`.
    """
    printers = getattr(request, "param", 1)
    assert printers in (1, 2), "world_printers must be 1 or 2"
    return printers


# --- End of several printers ---


@pytest.fixture
async def world(
    spoolman: SpoolmanHelper,
    prefix: str,
    tmp_path: Path,
    world_config: dict,
    world_tools: int,
    world_printers: int,
    world_fields: str,
    world_model: str,
    world_printer: dict,
):
    """The starting point every scenario shares.

    A Core One L whose tool 0 is loaded with spool A's PLA, A's instance UUID and some
    extrusion on the counter; spool A in the tool's location; B (PETG) and C (ASA) on
    this test's shelf. Each filament weighs 1000 g and no spool has used any, so a
    runout always leaves plenty and trips the mid-print flag. The bridge's clock is the
    simulator's, so `sync_delay` counts simulated seconds.

    With `world_tools` at 2 the printer is a Core One L INDX instead, which shows no
    UUIDs, and tool 1 is loaded with spool D's green PETG, with D in `<prefix> T1`. The
    bridge's config is the same apart from the second tool: the bridge decides from the
    model whether to use UUIDs.

    With `world_printers` at 2 a second Core One L, printer "b", shares the simulation:
    tool 0 loaded with spool E's white PLA and UUID, E in `<prefix> LB`. See
    integration/printers.py.
    """
    tool_location, shelf = f"{prefix} T", f"{prefix} shelf"
    locations = {0: tool_location}
    if world_tools == 2:
        locations[1] = f"{prefix} T1"
    fresh = world_fields == "fresh"
    # No UUID on spool A or the printer: a fresh Spoolman, or a model other than the
    # defaults, which are the ones with a tag reader or a test of their own.
    bare = fresh or world_model not in WORLD_MODELS.values()
    keys = {"uuid": UUID_FIELD, "flag": FLAG_FIELD, "claim": CLAIM_FIELD}
    if fresh:
        own = prefix.replace("-", "_")
        keys = {kind: f"{own}_{kind}" for kind in keys}
        spoolman.fields.extend(keys.values())
    else:
        await spoolman.ensure_text_field(UUID_FIELD, "Instance UUID")
    vendor = await spoolman.vendor()
    uuid_a = str(uuid.uuid4())
    spools = {}
    setup = [("A", PLA, tool_location), ("B", PETG, shelf), ("C", ASA, shelf)]
    if world_tools == 2:
        setup.append(("D", PETG_GREEN, locations[1]))
    for name, (material, color), location in setup:
        filament = await spoolman.filament(vendor, material, color)
        spools[name] = await spoolman.spool(
            filament, location, name, uuid_a if name == "A" and not bare else None
        )

    sim = Simulation(Settings(**SETTINGS))
    a = Material(PLA[0], PLA[1], None if bare else uuid_a, **WRITTEN)
    tools = {0: SimTool(0, loaded=True, material=replace(a), previous=replace(a), extruded=1000.0)}
    if world_tools == 2:
        d = Material(*PETG_GREEN, **WRITTEN)
        tools[1] = SimTool(1, loaded=True, material=replace(d), previous=replace(d), extruded=500.0)
    printer = SimPrinter("it", 0, KEY, MODELS[world_model], tools=tools)
    sim.add(printer)
    w: World

    def before_request(method: str, path: str) -> Reply:
        return None if w.before_request is None else w.before_request(method, path)

    server = TestServer(make_app(sim, printer.name, before_request))
    await server.start_server()
    uuids = {} if bare else {"A": uuid_a}
    others = await add_printers(world_printers, sim, spoolman, vendor, prefix, KEY, spools, uuids)
    cfg = Config(
        spoolman_url=str(spoolman.client.base_url).rstrip("/"),
        printers=(
            PrinterConfig(
                name="it",
                url=f"http://{server.host}:{server.port}",
                api_key=KEY,
                tools=dict(locations),
                **world_printer,
            ),
            *(other[-1] for other in others),
        ),
        db_file=tmp_path / "bridge.db",
        **(
            {"uuid_field": keys["uuid"], "flag_field": keys["flag"], "claim_field": keys["claim"]}
            if fresh
            else {}
        ),
        **world_config,
    )
    bridge = Bridge.from_config(cfg)
    bridge.clock = lambda: sim.now
    w = World(
        sim,
        printer,
        bridge,
        cfg,
        spoolman,
        tool_location,
        shelf,
        spools,
        uuids,
        locations=locations,
        fields=keys,
    )
    for name, sim_printer, location, other_server, _ in others:
        w.others[name] = Station(w, name, sim_printer, location, other_server)
    w.bridges.append(bridge)
    try:
        yield w
    finally:
        for started in w.bridges:
            await started.aclose()
        await server.close()
        for other in w.others.values():
            assert other.server is not None
            await other.server.close()
