"""The unit suite's bridge harness: a fake Spoolman, a simulated printer, and a bridge on both.

The printer is the simulator's `SimPrinter`, served in process. A test sets its state
directly, mostly tool 0's counters and filament, and time passes only when the test ticks.
"""

import json
import math
from dataclasses import fields, replace

import httpx

from integration.station import SETTINGS, Station
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
    preset_temperatures,
)
from simulator.prusalink import DROP, Answer
from spoolman_prusalink.app import Bridge
from spoolman_prusalink.config import Config, PrinterConfig
from spoolman_prusalink.db import Database, read_ledger
from spoolman_prusalink.prusalink import PrusaLink
from spoolman_prusalink.spoolman import Spoolman

LOC = "Core One L+"


def levels(caplog, text):
    """The level of each captured log record whose message contains `text`."""
    return [r.levelname for r in caplog.records if text in r.getMessage()]


class FakeSpoolman:
    """Just enough of Spoolman's API: spools, use, PATCH extra, and spool fields."""

    def __init__(self, spools):
        self.spools = {s["id"]: s for s in spools}
        self.fields = []
        self.used = {}  # spool id -> list of use_length calls applied
        self.fail_use = []  # status codes to return for upcoming use calls
        self.lose_use = 0  # Upcoming use calls to apply, then time out.
        self.writes = []
        self.down = False  # Refuse connections.
        self.fail_read = None  # Status code for spool reads.
        self.fail_patch = None  # Status code for spool PATCHes.
        self.fail_uuid = None  # Status code for PATCHes that store an instance UUID.
        self.fail_field = None  # Status code for field reads.
        self.fail_lookup = None  # Status code for one-at-a-time spool reads.
        self.garble_lookup = False  # Answer one-at-a-time spool reads with a body that isn't JSON.
        self.lookups = []  # Spool ids read one at a time.
        self.unlisted = set()  # Spool ids the list leaves out, as a list read a moment early.

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if self.down:
            raise httpx.ConnectError("spoolman offline")
        if path == "/api/v1/spool" and method == "GET":
            if self.fail_read:
                return httpx.Response(self.fail_read, text="oops")
            return httpx.Response(
                200,
                json=[
                    s
                    for s in self.spools.values()
                    if not s.get("archived") and s["id"] not in self.unlisted
                ],
            )
        if path.startswith("/api/v1/spool/") and method == "GET":
            spool_id = int(path.rsplit("/", 1)[1])
            self.lookups.append(spool_id)
            if self.fail_lookup:
                return httpx.Response(self.fail_lookup, text="oops")
            if self.garble_lookup:
                return httpx.Response(200, text="<html>not json</html>")
            if spool_id not in self.spools:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.spools[spool_id])
        if path == "/api/v1/field/spool" and method == "GET":
            if self.fail_field:
                return httpx.Response(self.fail_field, text="oops")
            return httpx.Response(200, json=self.fields)
        if path.startswith("/api/v1/field/spool/") and method == "POST":
            body = json.loads(request.content)
            self.fields.append({"key": path.rsplit("/", 1)[1], **body})
            self.writes.append(("field", path))
            return httpx.Response(200, json=self.fields)
        if path.endswith("/use") and method == "PUT":
            spool_id = int(path.split("/")[-2])
            if self.fail_use:
                return httpx.Response(self.fail_use.pop(0), text="nope")
            if spool_id not in self.spools:
                return httpx.Response(404, json={"message": "not found"})
            mm = json.loads(request.content)["use_length"]
            self.used.setdefault(spool_id, []).append(mm)
            self.writes.append(("use", spool_id, mm))
            data = self.spools[spool_id]
            if data.get("used_weight") is not None:  # As Spoolman works it out.
                f = data["filament"]
                grams = math.pi * (f["diameter"] / 20) ** 2 * (mm / 10) * f["density"]
                data["used_weight"] = max(data["used_weight"] + grams, 0.0)
            if self.lose_use:
                self.lose_use -= 1
                raise httpx.ReadTimeout("response lost", request=request)
            return httpx.Response(200, json=data)
        if path.startswith("/api/v1/spool/") and method == "PATCH":
            if self.fail_patch:
                return httpx.Response(self.fail_patch, text="oops")
            spool_id = int(path.rsplit("/", 1)[1])
            if spool_id not in self.spools:
                return httpx.Response(404, json={"message": "not found"})
            body = json.loads(request.content)
            if self.fail_uuid and "instance_uuid" in body.get("extra", {}):
                return httpx.Response(self.fail_uuid, text="oops")
            if "location" in body:
                self.spools[spool_id]["location"] = body["location"]
                self.writes.append(("location", spool_id, body["location"]))
            extra = self.spools[spool_id].setdefault("extra", {})
            for k, v in body.get("extra", {}).items():
                if v is None:
                    extra.pop(k, None)
                else:
                    extra[k] = v
            if "extra" in body:
                self.writes.append(("patch", spool_id))
            return httpx.Response(200, json=self.spools[spool_id])
        raise AssertionError(f"unexpected {method} {path}")

    def flag(self, spool_id):
        raw = self.spools[spool_id].get("extra", {}).get("bridge_flag")
        return None if raw is None else json.loads(raw)


def spool(id, location=LOC, flag=None):
    extra = {} if flag is None else {"bridge_flag": json.dumps(flag)}
    return {
        "id": id,
        "location": location,
        "extra": extra,
        "filament": {
            "id": 1,
            "name": "Grey",
            "material": "PLA",
            "color_hex": "73767d",
            "diameter": 1.75,
            "density": 1.24,
        },
    }


PLA = Material("PLA", "#73737b", **preset_temperatures("PLA"))


def menu(name="PLA", color=None, **fields):
    """A material loaded from the printer's menu: the preset's temperatures and no UUID.

    `fields` are `Material` fields that differ, such as `bed=70` for an edit on the
    printer's screen.
    """
    values = {**preset_temperatures(name), **fields}
    return Material(name, color, adhoc=color is not None, **values)


def fill(tool, material):
    """Leave `tool` as a finished load of `material` leaves it, or a finished unload for None.

    Only the filament and its stored type change. The test sets the counters itself.
    """
    tool.material = None if material is None else replace(material)
    tool.loaded = material is not None
    if material is not None and material.adhoc:
        tool.previous = replace(material)


def add_station(sim, name="default"):
    """Add a Core One L to `sim`: tool 0 loaded with grey PLA and 1000 mm on its counter."""
    tool = SimTool(0, extruded=1000.0)
    fill(tool, PLA)
    printer = SimPrinter(name, 0, "k", MODELS["8.1.0"], boot_id=1, tools={0: tool})
    sim.add(printer)
    return Station(sim, printer)


PRINTER_KEYS = {f.name for f in fields(PrinterConfig)}


def make(tmp_path, spools, **cfg):
    """A bridge on a fake Spoolman and one simulated printer, "default", served in process.

    Returns the bridge, the fake Spoolman and the printer's `Station`. A sync job the
    bridge uploads runs only as the test ticks: `Station.finish_job()` runs it to its end.
    """
    fake = FakeSpoolman(spools)
    printer = add_station(Simulation(Settings(**SETTINGS)))
    printer_cfg = PrinterConfig(
        name="default",
        url="http://printer",
        api_key="k",
        tools={0: LOC},
        **{k: cfg.pop(k) for k in list(cfg) if k in PRINTER_KEYS},
    )
    config = Config(
        spoolman_url="http://spoolman",
        printers=(printer_cfg,),
        db_file=tmp_path / "bridge.db",
        **cfg,
    )
    db = Database.open(config.db_file)
    bridge = Bridge(
        config=config,
        state=db.load(),
        db=db,
        clients={"default": PrusaLink("http://printer", "k", transport=printer.transport())},
        spoolman=Spoolman(
            "http://spoolman",
            uuid_field=config.uuid_field,
            flag_field=config.flag_field,
            claim_field=config.claim_field,
            transport=httpx.MockTransport(fake.handle),
        ),
    )
    return bridge, fake, printer


def ledger(tmp_path):
    return read_ledger(tmp_path / "bridge.db")


def spool_x(id, location=LOC, material="PLA", color="ff0000", temps=(None, None), extra=None):
    s = spool(id, location)
    s["filament"] = {
        **s["filament"],
        "material": material,
        "color_hex": color,
        "settings_extruder_temp": temps[0],
        "settings_bed_temp": temps[1],
    }
    s["extra"] = extra or {}
    return s


def jobs(station):
    """The G-code of each sync job the printer accepted."""
    return [e.data["body"] for e in station.requests("PUT") if e.text.endswith(" 201")]


def refused(station):
    """How many uploads the printer refused with 409."""
    return sum(1 for e in station.requests("PUT") if e.text.endswith(" 409"))


def answer(status, method=None):
    """A `Station.before_request` hook that answers `status` to every request, or to
    every request with `method`, in place of the printer."""

    def hook(request_method, path):
        if method is None or request_method == method:
            return Answer(status, {"message": f"answered by the test with {status}"})
        return None

    return hook


def drop(method, path):
    """A `Station.before_request` hook that drops every request, as a lost link does."""
    return DROP


def start_job(station, name="part.bgcode"):
    """Start a print and tick until the printer reports the job; return the job's ID.

    The print is slow enough that its own extrusion never shows in the counter, so the
    test moves the counter itself.
    """
    spec = PrintSpec([Segment(0, 1000)], duration_s=1e9, preheat_s=0, name=name)
    station.printer.start_print(spec)
    station.tick_until(
        lambda: station.printer.job_visible() and station.printer.state is State.PRINTING
    )
    return station.printer.job.id


def end_job(station):
    """Cancel the running job and tick until the printer has none."""
    station.printer.cancel()
    station.finish_job()


async def swap_to_spool_20(bridge, fake, station):
    """Baseline on spool 19, then a Spoolman-first swap to spool 20 and a menu load."""
    await bridge.poll()
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    station.tool.unload_count, station.tool.extruded_at_unload = 1, 1000
    fill(station.tool, None)
    await bridge.poll()
    station.tool.extruded = 1116
    fill(station.tool, menu("PLA"))
    await bridge.poll()


def restart(bridge, **cfg):
    """A new bridge on the same printers, Spoolman and database, which the old one lets go."""
    bridge.db.close()
    config = replace(bridge.config, **cfg)
    db = Database.open(config.db_file)
    return Bridge(
        config=config,
        spoolman=bridge.spoolman,
        clients=bridge.clients,
        state=db.load(),
        db=db,
    )


CLAIMED = {"bridge_claim": "true"}


def two_printers(tmp_path, spools, **cfg):
    """A bridge with printers "a" and "b", each with one tool, against one fake Spoolman.

    Returns the bridge, the fake Spoolman and the two printers' stations by name. They
    share one simulation, so a tick moves both.
    """
    fake = FakeSpoolman(spools)
    sim = Simulation(Settings(**SETTINGS))
    printers = {name: add_station(sim, name) for name in ("a", "b")}
    config = Config(
        spoolman_url="http://spoolman",
        printers=tuple(
            PrinterConfig(name=name, url=f"http://{name}", api_key="k", tools={0: f"Tool {name}"})
            for name in printers
        ),
        db_file=tmp_path / "bridge.db",
        **cfg,
    )
    db = Database.open(config.db_file)
    bridge = Bridge(
        config=config,
        state=db.load(),
        db=db,
        spoolman=Spoolman(
            "http://spoolman",
            uuid_field=config.uuid_field,
            flag_field=config.flag_field,
            claim_field=config.claim_field,
            # Late bound, so a test can replace `fake.handle`.
            transport=httpx.MockTransport(lambda request: fake.handle(request)),
        ),
        clients={
            name: PrusaLink(f"http://{name}", "k", transport=p.transport())
            for name, p in printers.items()
        },
    )
    return bridge, fake, printers
