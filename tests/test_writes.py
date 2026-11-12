"""Phase 2: charges and flags written to a fake Spoolman."""

import json
import logging
import math
import re
import sqlite3
from dataclasses import fields, replace

import httpx
import pytest

from spoolman_prusalink.accounting import ToolState
from spoolman_prusalink.app import Bridge
from spoolman_prusalink.color import quantize
from spoolman_prusalink.config import Config, PrinterConfig
from spoolman_prusalink.db import Database, read_ledger, read_state
from spoolman_prusalink.printer_sync import COREONEL_PREAMBLE, PRESETS
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


class Printer:
    def __init__(self):
        self.extruded = 1000
        self.unloads = 0
        self.at_unload = 0
        self.material = {"name": "PLA", "color": "#73737b"}
        self.job = None
        self.job_name = None
        self.down = False
        self.uploads = []  # (path, gcode)
        self.busy = False  # Reject uploads with 409, as while printing.
        self.refused = 0  # Uploads rejected with 409.
        self.printer_type = "8.1.0"  # Core One L
        self.boot_id = 1
        self.filament_endpoint = True
        self.error = None  # Status code for every request, such as 401.
        self.upload_error = None  # Status code for sync job uploads.
        self.counters = True  # MINI and MK3.5 firmware report only the material.
        self.states = []  # States the next status reads report, before the usual one.
        self.hidden = False  # Report no tools, as with the tool disabled (T5).

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("printer offline")
        if self.error:
            return httpx.Response(self.error, text="oops")
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"text": "PrusaLink", "printer": self.printer_type})
        if request.url.path == "/api/v1/filament":
            if not self.filament_endpoint:
                return httpx.Response(404)
            tool = {
                "index": 0,
                "extruded": self.extruded,
                "unload_count": self.unloads,
                "extruded_at_unload": self.at_unload,
            }
            if not self.counters:
                tool = {"index": 0}
                if self.material:
                    tool["material"] = reported(self.material)
                return httpx.Response(200, json={"tools": [tool]})
            if self.material:
                tool["material"] = reported(self.material)
            tools = [] if self.hidden else [tool]
            return httpx.Response(200, json={"boot_id": self.boot_id, "tools": tools})
        if request.url.path == "/api/v1/job":
            if not self.job:
                return httpx.Response(204)
            return httpx.Response(
                200, json={"id": self.job, "file": {"display_name": self.job_name}}
            )
        if request.method == "PUT" and request.url.path.startswith("/api/v1/files/"):
            if self.busy or self.job:
                self.refused += 1
                return httpx.Response(409)
            if self.upload_error:
                return httpx.Response(self.upload_error, text="oops")
            assert request.headers["Print-After-Upload"] == "?1"
            gcode = request.content.decode()
            self.uploads.append((request.url.path, gcode))
            self.run_job(gcode)
            return httpx.Response(201)
        if request.url.path == "/api/v1/status":
            state = self.states.pop(0) if self.states else "PRINTING" if self.job else "IDLE"
            data = {"printer": {"state": state}}
            if self.job:
                data["job"] = {"id": self.job}
            return httpx.Response(200, json=data)
        raise AssertionError(f"unexpected printer request {request.method} {request.url}")

    def run_job(self, gcode):
        """Apply the job's M865 lines, as the firmware would, and finish at once."""
        for line in gcode.splitlines():
            if line.startswith("M865 X"):
                name = line.split('N"')[1].split('"')[0]
                # R resets every temperature to the firmware default first.
                reset = zip(TEMPERATURE_KEYS, DEFAULT_TEMPERATURES, strict=True)
                material = {"name": name, **dict(reset)}
                for key, letter in zip(TEMPERATURE_KEYS, "TPB", strict=True):
                    if m := re.search(rf"\s{letter}(\d+)\b", line):
                        material[key] = int(m.group(1))
                if 'O"' in line:
                    material["color"] = quantize("#" + line.split('O"#')[1][:6])
                self.material = material
            if line.startswith("M865.1"):
                self.material = {**self.material, "instance_uuid": line.split('C"')[1][:36]}


TEMPERATURE_KEYS = ("nozzle_temperature", "preheat_temperature", "bed_temperature")
DEFAULT_TEMPERATURES = (215, 170, 60)
PRESET_TEMPERATURES = {k: (p.nozzle, p.preheat, p.bed) for k, p in PRESETS.items()}


def reported(material):
    """`material` as the printer reports it: a menu load takes the preset's temperatures."""
    base = PRESET_TEMPERATURES.get(material["name"].upper(), DEFAULT_TEMPERATURES)
    return {**dict(zip(TEMPERATURE_KEYS, base, strict=True)), **material}


PRINTER_KEYS = {f.name for f in fields(PrinterConfig)}


def make(tmp_path, spools, **cfg):
    fake, printer = FakeSpoolman(spools), Printer()
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
        clients={
            "default": PrusaLink(
                "http://printer", "k", transport=httpx.MockTransport(printer.handle)
            )
        },
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


async def test_charges_are_applied_and_recorded(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()  # baseline
    printer.extruded = 1500
    await bridge.poll()
    assert fake.used == {19: [500.0]}
    applied = [r for r in ledger(tmp_path) if "use_length" in r]
    assert len(applied) == 1 and applied[0]["applied"]
    assert read_state(tmp_path / "bridge.db").outbox == []
    await bridge.aclose()


async def test_usage_while_ambiguous_is_kept_over_a_reboot(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()  # baseline at 1000
    fake.spools[20], fake.spools[21] = spool(20), spool(21)
    printer.extruded = 1500
    await bridge.poll()  # ambiguous, skipped
    printer.boot_id, printer.extruded = 2, 10
    fake.spools.pop(20), fake.spools.pop(21)
    await bridge.poll()
    assert fake.used == {19: [500.0, 10.0]}
    await bridge.aclose()


async def test_spoolman_outage_queues_and_retries_in_order(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    fake.fail_use = [503]
    await bridge.poll()
    assert fake.used == {}
    assert [r["use_length"] for r in read_state(tmp_path / "bridge.db").outbox] == [500.0]

    printer.extruded = 1600
    await bridge.poll()
    assert fake.used == {19: [500.0, 100.0]}
    assert read_state(tmp_path / "bridge.db").outbox == []
    await bridge.aclose()


async def test_queued_charge_survives_restart(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    fake.fail_use = [503]
    await bridge.poll()
    await bridge.aclose()

    # A new bridge process: it loads the outbox and sends it even if the printer is down.
    restarted, _, _ = make(tmp_path, [])
    restarted.spoolman = bridge.spoolman.__class__(
        "http://spoolman", flag_field="bridge_flag", transport=httpx.MockTransport(fake.handle)
    )
    restarted.printers[0].client = PrusaLink(
        "http://printer", "k", transport=httpx.MockTransport(printer.handle)
    )
    printer.down = True
    await restarted.poll()
    assert fake.used == {19: [500.0]}
    await restarted.aclose()


class FailingCommit:
    """A database connection whose commits fail while their transaction writes ledger rows.

    `failed` holds the failed transaction's writes, as (statement start, row count).
    """

    def __init__(self, conn):
        self.conn, self.writes, self.failed = conn, [], None

    def execute(self, sql, *args):
        if sql == "BEGIN IMMEDIATE":
            self.writes = []
        elif sql == "COMMIT" and self.wrote("INSERT INTO ledger"):
            self.failed = self.writes
            raise sqlite3.OperationalError("disk I/O error")
        elif sql != "ROLLBACK":
            self.writes.append((sql, 1))
        return self.conn.execute(sql, *args)

    def executemany(self, sql, rows):
        rows = list(rows)
        self.writes.append((sql, len(rows)))
        return self.conn.executemany(sql, rows)

    def wrote(self, start, writes=None):
        """How many rows a statement starting with `start` wrote."""
        return sum(
            n for sql, n in (self.writes if writes is None else writes) if sql.startswith(start)
        )


async def test_a_failed_save_in_process_saves_nothing(tmp_path):
    """The events, the queued charges and the counters are saved together or not at all."""
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    db = tmp_path / "bridge.db"
    state, records = read_state(db), ledger(tmp_path)
    conn = bridge.db._conn
    bridge.db._conn = FailingCommit(conn)
    printer.unloads, printer.at_unload, printer.extruded = 1, 1400, 1500
    with pytest.raises(sqlite3.OperationalError):
        await bridge.poll()
    # One transaction held the events, the queued charges and the tool's counters.
    failed = bridge.db._conn.failed
    assert bridge.db._conn.wrote("INSERT INTO ledger", failed) == 2
    assert bridge.db._conn.wrote("INSERT INTO outbox", failed) == 2
    assert bridge.db._conn.wrote("INSERT OR REPLACE INTO tools", failed) == 1
    assert read_state(db) == state
    assert ledger(tmp_path) == records
    assert fake.used == {}
    bridge.db._conn = conn

    # The bridge stops on the error; started again, it charges the same usage.
    bridge = restart(bridge)
    await bridge.poll()
    assert fake.used == {19: [400.0, 100.0]}
    assert [r.get("event") for r in ledger(tmp_path)[len(records) :]] == [
        "unload",
        "swap_unconfirmed",
        None,
        None,
    ]
    await bridge.aclose()


async def test_a_failed_save_in_flush_keeps_the_charge_queued(tmp_path):
    """A sent charge leaves the outbox in the same transaction that records it."""
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    fake.fail_use = [503]
    await bridge.poll()
    db = tmp_path / "bridge.db"
    state, records = read_state(db), ledger(tmp_path)
    assert [r["use_length"] for r in state.outbox] == [500.0]
    conn = bridge.db._conn
    bridge.db._conn = FailingCommit(conn)
    with pytest.raises(sqlite3.OperationalError):
        await bridge.flush()
    # One transaction held the record and the charge leaving the outbox.
    failed = bridge.db._conn.failed
    assert bridge.db._conn.wrote("INSERT INTO ledger", failed) == 1
    assert bridge.db._conn.wrote("DELETE FROM outbox", failed) == 1
    assert fake.used == {19: [500.0]}  # Spoolman took it,
    assert read_state(db) == state  # but it's still queued,
    assert ledger(tmp_path) == records  # with no record of it.
    bridge.db._conn = conn

    # Sent again at restart: charges are at least once.
    bridge = restart(bridge)
    assert await bridge.flush()
    assert fake.used == {19: [500.0, 500.0]}
    assert read_state(db).outbox == []
    assert [r["use_length"] for r in ledger(tmp_path) if r.get("applied")] == [500.0]
    await bridge.aclose()


async def test_rejected_charge_is_dropped_not_blocking(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    fake.fail_use = [422]
    await bridge.poll()
    printer.extruded = 1600
    await bridge.poll()
    assert fake.used == {19: [100.0]}
    failed = [r for r in ledger(tmp_path) if r.get("failed")]
    assert [(r["use_length"], r["failed"]) for r in failed] == [(500.0, "422")]
    await bridge.aclose()


async def test_flush_counts_queued_charges_by_outcome(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer.extruded = 1500
    fake.fail_use = [503]
    await bridge.poll()  # The charge stays queued.
    printer.extruded = 1600
    fake.fail_use = [422]
    await bridge.poll()  # The queued charge is dropped, then the new one goes.
    assert fake.used == {19: [100.0]}
    assert caplog.text.count("sent 0 queued charge(s); dropped 1") == 1
    await bridge.aclose()


async def test_confirmed_swap_charges_new_before_refunding_old(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.extruded = 1, 1000, 1116
    await bridge.poll()  # unconfirmed swap: 116 to spool 19
    fake.spools[20] = spool(20)
    await bridge.poll()  # user scans spool 20 in
    uses = [w for w in fake.writes if w[0] == "use"]
    assert uses == [("use", 19, 116.0), ("use", 20, 116.0), ("use", 19, -116.0)]
    await bridge.aclose()


async def test_two_step_swap_flags_the_old_spool_then_moves_its_usage(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.extruded = 1, 1000, 1116
    await bridge.poll()  # unconfirmed swap: 116 to spool 19
    fake.spools[19] = spool(19, location=None)  # spool 19 moved out first
    printer.extruded += 50
    await bridge.poll()
    assert "moved out" in fake.flag(19)
    fake.spools[20] = spool(20)  # then spool 20 moved in
    await bridge.poll()
    uses = [w for w in fake.writes if w[0] == "use"]
    assert uses == [("use", 19, 116.0), ("use", 20, 116.0), ("use", 19, -116.0), ("use", 20, 50.0)]
    assert not fake.flag(19)
    await bridge.aclose()


async def test_unconfirmed_swap_flag_is_set_then_cleared(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    assert ("field", "/api/v1/field/spool/bridge_flag") in fake.writes
    printer.unloads, printer.at_unload = 1, 1000
    await bridge.poll()
    assert "unloaded with no Spoolman change" in fake.flag(19)
    assert read_state(tmp_path / "bridge.db").flagged == [19]

    printer.job = 7  # A print starts at its first extrusion: the swap is assumed.
    await bridge.poll()
    printer.extruded += 50
    await bridge.poll()
    assert "assumed reloaded and 50 mm" in fake.flag(19)
    assert "provisionally" in fake.flag(19)

    # The provisional charge grows with the next print. The flag shows the new amount
    # once the print ends, not on every poll.
    printer.job = 8
    await bridge.poll()
    patches = fake.writes.count(("patch", 19))
    for _ in range(3):
        printer.extruded += 50
        await bridge.poll()
    assert fake.writes.count(("patch", 19)) == patches
    printer.job = None
    await bridge.poll()
    assert "assumed reloaded and 200 mm" in fake.flag(19)

    # The next unload keeps the usage where it is and clears the flag.
    printer.unloads, printer.at_unload = 2, printer.extruded
    await bridge.poll()
    assert bridge.printers[0].tools[0].provisional is None
    assert "provisionally" not in fake.flag(19)
    assert sum(fake.used[19]) == 200
    await bridge.aclose()


async def test_drift_flag_is_optional(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    printer.material = {"name": "PLA"}  # Menu load: color gone.
    await bridge.poll()
    assert "printer filament differs" in fake.flag(19)
    await bridge.aclose()

    bridge, fake, printer = make(tmp_path / "b", [spool(19)], flag_drift=False)
    printer.material = {"name": "PLA"}
    await bridge.poll()
    assert fake.flag(19) is None
    await bridge.aclose()


async def test_a_skipped_tool_keeps_its_drift_flag(tmp_path, caplog):
    """While the location is ambiguous (S11), the drift flag stands, like the drift note."""
    caplog.set_level("INFO")
    bridge, fake, printer = make(
        tmp_path, [spool(19), spool(20, location="Shelf"), spool(21, location="Shelf")]
    )
    printer.material = {"name": "PLA"}  # Menu load: color gone.
    await bridge.poll()
    flag = fake.flag(19)
    assert "printer filament differs" in flag
    patches = fake.writes.count(("patch", 19))
    for spool_id in (20, 21):
        fake.spools[spool_id]["location"] = LOC
    for _ in range(2):
        await bridge.poll()
    assert "location holds spools" in caplog.text
    assert fake.flag(19) == flag
    for spool_id in (20, 21):
        fake.spools[spool_id]["location"] = "Shelf"
    await bridge.poll()
    assert fake.flag(19) == flag
    assert fake.writes.count(("patch", 19)) == patches
    assert levels(caplog, "flag on spool 19") == ["INFO"]

    # Compared again with the printer put back: the flag clears.
    printer.material = {"name": "PLA", "color": "#73737b"}
    await bridge.poll()
    assert fake.flag(19) is None
    await bridge.aclose()


async def test_a_tool_the_printer_stops_reporting_keeps_its_flags(tmp_path, caplog):
    """A tool the printer doesn't report (T5) keeps its state and drift flags meanwhile."""
    caplog.set_level("INFO")
    bridge, fake, printer = make(tmp_path, [spool(19)], printer_sync=False)
    printer.material = {"name": "PLA"}
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, 1000
    await bridge.poll()
    flag = fake.flag(19)
    assert "unloaded with no Spoolman change" in flag and "printer filament differs" in flag
    patches = fake.writes.count(("patch", 19))
    printer.hidden = True
    for _ in range(2):
        await bridge.poll()
    assert "doesn't report tool index 0" in caplog.text
    assert fake.flag(19) == flag
    printer.hidden = False
    await bridge.poll()
    assert fake.flag(19) == flag
    assert fake.writes.count(("patch", 19)) == patches
    await bridge.aclose()


async def test_flags_written_by_others_are_left_alone(tmp_path):
    bridge, fake, _ = make(tmp_path, [spool(19), spool(5, location="Shelf", flag="mine")])
    await bridge.poll()
    await bridge.poll()
    assert fake.flag(5) == "mine"
    assert not [w for w in fake.writes if w == ("patch", 5)]
    await bridge.aclose()


async def test_unchanged_flags_are_not_rewritten(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    printer.material = {"name": "PLA"}
    for _ in range(3):
        await bridge.poll()
    assert [w for w in fake.writes if w[0] == "patch"] == [("patch", 19)]
    await bridge.aclose()


async def test_flags_disabled(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)], flag_field=None)
    printer.material = {"name": "PLA"}
    await bridge.poll()
    assert not [w for w in fake.writes if w[0] == "patch"]
    assert ("field", "/api/v1/field/spool/bridge_flag") not in fake.writes
    await bridge.aclose()


# Phase 3: eviction and printer sync


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


def jobs(printer):
    return [g for _, g in printer.uploads]


async def test_spoolman_first_swap_evicts_and_writes_printer(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    await bridge.poll()  # baseline; printer matches spool 19's color, no UUID on the spool
    assert jobs(printer) == []

    fake.spools[20] = spool_x(20)  # scan spool 20 in: staged
    await bridge.poll()
    assert fake.spools[19]["location"] == LOC and jobs(printer) == []

    printer.unloads, printer.at_unload, printer.material = 1, 1000, None  # unload
    await bridge.poll()
    assert fake.spools[19]["location"] is None  # evicted at the unload
    assert jobs(printer) == []  # tool empty: nothing to write yet

    printer.extruded, printer.material = 1116, {"name": "PLA"}  # menu load
    await bridge.poll()
    (gcode,) = jobs(printer)
    minted = json.loads(fake.spools[20]["extra"]["instance_uuid"])
    assert gcode.splitlines()[:3] == [
        'M862.3 P "COREONEL" ; printer model check',
        "M862.5 P2 ; g-code level check",
        'M862.6 P"Input shaper" ; FW feature check',
    ]
    assert 'M865 X R N"PLA" T215 B60 P170 C20 D15 E38 O"#ff0000" L0' in gcode
    assert f'M865.1 I0 C"{minted}"' in gcode
    assert gcode.rstrip().endswith(f'M865.1 I0 C"{minted}"')
    assert "M117" not in gcode
    assert printer.uploads[0][0] == "/api/v1/files/usb/spoolman-sync.gcode"

    await bridge.poll()  # printer now matches: no second job, no flag
    assert len(jobs(printer)) == 1
    assert bridge.printers[0].tools[0].needs_sync is False
    assert fake.flag(20) is None
    await bridge.aclose()


async def swap_to_menu_loaded_20(bridge, fake, printer):
    """Spoolman first: spool 20 in, an unload, then a menu load. A write of 20 is due."""
    await bridge.poll()
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, None
    await bridge.poll()
    printer.extruded, printer.material = 1116, {"name": "PLA"}


async def test_failed_uuid_mint_holds_the_write_until_stored(tmp_path, caplog):
    """The spool gets its UUID before the printer gets the spool (W1, #119)."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    caplog.set_level("DEBUG")
    await swap_to_menu_loaded_20(bridge, fake, printer)
    fake.fail_uuid = 500
    for _ in range(3):
        await bridge.poll()
    assert jobs(printer) == []
    assert stored_uuid(fake, 20) is None
    assert bridge.printers[0].tools[0].needs_sync is True
    assert levels(caplog, "can't store an instance UUID on spool 20") == [
        "WARNING",
        "DEBUG",
        "DEBUG",
    ]
    assert caplog.text.count("printer will be set to spool 20 when idle") == 1

    fake.fail_uuid = None
    caplog.clear()
    await bridge.poll()
    minted = stored_uuid(fake, 20)
    (gcode,) = jobs(printer)
    assert f'M865.1 I0 C"{minted}"' in gcode
    assert printer.material["instance_uuid"] == minted
    assert lines(caplog, 20) == [("INFO", f"gave spool 20 instance UUID {minted}")]

    caplog.clear()
    await bridge.poll()  # The printer matches: nothing more to write or mint.
    assert len(jobs(printer)) == 1
    assert "instance UUID" not in caplog.text
    assert fake.flag(20) is None
    await bridge.aclose()


async def test_uuid_field_of_another_type_writes_without_a_uuid(tmp_path, caplog):
    """A UUID key that can't be set up doesn't hold the write: no UUID is written."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    caplog.set_level("INFO")
    fake.fields.append({"key": "instance_uuid", "name": "UUID", "field_type": "integer"})
    await swap_to_menu_loaded_20(bridge, fake, printer)
    await bridge.poll()
    (gcode,) = jobs(printer)
    assert 'M865 X R N"PLA"' in gcode and "M865.1" not in gcode
    assert stored_uuid(fake, 20) is None
    assert bridge.printers[0].tools[0].sync_attempts == 1
    await bridge.poll()  # The printer matches: nothing held, nothing written again.
    assert len(jobs(printer)) == 1
    assert caplog.text.count("can't set up the 'instance_uuid' spool field") == 1
    await bridge.aclose()


@pytest.mark.parametrize("status", [500, 408, 429])
async def test_uuid_field_setup_failing_holds_the_write_until_stored(tmp_path, caplog, status):
    """A field read that fails passes: the write waits, then goes out with the UUID."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    caplog.set_level("DEBUG")
    await swap_to_menu_loaded_20(bridge, fake, printer)
    fake.fail_field = status
    for _ in range(2):
        await bridge.poll()
    assert jobs(printer) == []
    assert levels(caplog, "can't set up the 'instance_uuid' spool field") == ["WARNING", "DEBUG"]

    fake.fail_field = None
    await bridge.poll()
    minted = stored_uuid(fake, 20)
    (gcode,) = jobs(printer)
    assert minted is not None and f'M865.1 I0 C"{minted}"' in gcode
    assert levels(caplog, "'instance_uuid' spool field is set up") == ["INFO"]
    await bridge.aclose()


async def test_uuid_field_refused_by_the_fields_api_writes_without_a_uuid(tmp_path, caplog):
    """A 4xx from the fields API lasts, like a key of another type: no UUID is written."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    caplog.set_level("DEBUG")
    await swap_to_menu_loaded_20(bridge, fake, printer)
    fake.fail_field = 403
    for _ in range(2):
        await bridge.poll()
    (gcode,) = jobs(printer)
    assert 'M865 X R N"PLA"' in gcode and "M865.1" not in gcode
    assert stored_uuid(fake, 20) is None
    assert levels(caplog, "can't set up the 'instance_uuid' spool field") == ["WARNING"]
    await bridge.aclose()


async def test_uuid_field_fixed_then_failing_holds_the_write(tmp_path, caplog):
    """A key of another type writes without a UUID; once fixed, a 500 holds the write."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    caplog.set_level("INFO")
    wrong = {"key": "instance_uuid", "name": "UUID", "field_type": "integer"}
    fake.fields.append(wrong)
    await swap_to_menu_loaded_20(bridge, fake, printer)
    await bridge.poll()
    (gcode,) = jobs(printer)
    assert "M865.1" not in gcode

    # Someone deletes the wrong field and edits the spool, but the field read fails.
    fake.fields.remove(wrong)
    fake.spools[20]["filament"]["color_hex"] = "00ff00"
    fake.fail_field = 500
    for _ in range(2):
        await bridge.poll()
    assert len(jobs(printer)) == 1
    assert stored_uuid(fake, 20) is None

    fake.fail_field = None
    await bridge.poll()
    minted = stored_uuid(fake, 20)
    assert minted is not None
    assert len(jobs(printer)) == 2
    assert 'O"#00ff00"' in jobs(printer)[-1]
    assert f'M865.1 I0 C"{minted}"' in jobs(printer)[-1]
    assert caplog.text.count("can't set up the 'instance_uuid' spool field") == 2
    assert caplog.text.count("'instance_uuid' spool field is set up") == 1
    await bridge.aclose()


async def test_failed_uuid_mint_leaves_a_failing_sync_job_failing(tmp_path, caplog):
    """A write waiting on the mint is still needed: the job's failure isn't over."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    caplog.set_level("INFO")
    await swap_to_menu_loaded_20(bridge, fake, printer)
    printer.upload_error = 500
    await bridge.poll()
    assert caplog.text.count("sync job failed (500)") == 1

    # Someone clears the UUID minted for the failed job, and Spoolman won't take another.
    fake.spools[20]["extra"].pop("instance_uuid")
    fake.fail_uuid = 500
    await bridge.poll()
    assert "sync job no longer needed" not in caplog.text

    fake.fail_uuid = printer.upload_error = None
    await bridge.poll()
    (gcode,) = jobs(printer)
    assert f'M865.1 I0 C"{stored_uuid(fake, 20)}"' in gcode
    assert caplog.text.count("sync job working again") == 1
    assert "sync job no longer needed" not in caplog.text
    await bridge.aclose()


async def test_own_sync_job_is_not_a_print_start(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, 1000
    await bridge.poll()  # unconfirmed swap
    printer.job, printer.job_name = 50, "spoolman-sync.gcode"
    await bridge.poll()
    # The counter moves during the sync job, so only the file-name filter keeps it from
    # counting as a print start.
    printer.extruded += 50
    await bridge.poll()
    assert bridge.printers[0].tools[0].swap is not None
    printer.job, printer.job_name = 51, "part.bgcode"
    await bridge.poll()
    printer.extruded += 50
    await bridge.poll()
    assert bridge.printers[0].tools[0].swap is None
    await bridge.aclose()


async def test_emptied_location_has_no_spool_none_note(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)], sync_drift=True)
    await bridge.poll()
    caplog.set_level("INFO")
    fake.spools[19]["location"] = None  # Moved out, with nothing in its place.
    printer.material = {"name": "PETG"}
    for _ in range(3):
        await bridge.poll()
    assert jobs(printer) == []
    assert "spool None" not in caplog.text
    assert caplog.text.count("spool 19 is no longer in the location") == 1
    # Spool 19 is still loaded, so the drift is reported, once, not written.
    assert caplog.text.count("printer differs from spool 19") == 1
    assert "printer filament differs" in fake.flag(19)
    await bridge.aclose()


async def test_busy_printer_defers_write(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    fake.spools[20] = spool_x(20)
    printer.busy = True
    await bridge.poll()
    assert jobs(printer) == [] and bridge.printers[0].tools[0].sync_attempts == 0
    printer.busy = False
    await bridge.poll()
    assert len(jobs(printer)) == 1
    await bridge.aclose()


async def test_non_preset_without_temps_is_flagged_not_written(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    fake.spools[20] = spool_x(20, material="PLA-CF")
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    await bridge.poll()
    assert jobs(printer) == []
    assert "set its extruder and bed temperatures" in fake.flag(20)
    patches = fake.writes.count(("patch", 20))

    # The flag stays through a print, and the warning isn't repeated each poll.
    await bridge.poll()
    printer.job, printer.extruded = 7, 1200
    await bridge.poll()
    assert "set its extruder and bed temperatures" in fake.flag(20)
    printer.job = None
    await bridge.poll()
    await bridge.poll()
    assert jobs(printer) == []
    assert fake.writes.count(("patch", 20)) == patches
    assert caplog.text.count("] can't write spool 20 to the printer") == 1
    assert "printer will be set to spool 20" not in caplog.text

    fake.spools[20] = spool_x(20, material="PLA-CF", temps=(220, 60))
    await bridge.poll()
    (gcode,) = jobs(printer)
    assert 'M865 X R N"PLA-CF" T220 B60 O"#ff0000" L0' in gcode
    await bridge.aclose()


async def test_pps_with_temps_is_flagged_not_written_and_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    fake.spools[20] = spool_x(20, material="PPS", temps=(320, 105))
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    for _ in range(3):
        await bridge.poll()
    assert jobs(printer) == []
    assert "HT idler-door" in fake.flag(20)
    assert caplog.text.count("] can't write spool 20") == 1
    assert "printer will be set" not in caplog.text
    await bridge.aclose()


async def test_pps_loaded_from_the_menu_clears_the_flag(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    await bridge.poll()
    uuid = json.dumps("91da34d9-c1c1-470f-bac7-185a2346f86a")
    fake.spools[20] = spool_x(20, material="PPS", temps=(320, 105), extra={"instance_uuid": uuid})
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    await bridge.poll()
    await bridge.poll()
    assert "HT idler-door" in fake.flag(20)

    # The user loads it from the printer's menu, as the flag says: the printer shows
    # the preset's name, but not the spool's color or UUID.
    caplog.set_level("INFO")
    printer.material = {"name": "PPS", "color": "#000000"}
    for _ in range(3):
        await bridge.poll()
    assert fake.flag(20) is None
    assert jobs(printer) == []
    st = bridge.printers[0].tools[0]
    assert (st.needs_sync, st.sync_attempts) == (False, 0)
    assert "differs" not in caplog.text
    await bridge.aclose()


async def test_bad_uuid_in_spoolman_is_flagged_not_written(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    await bridge.poll()
    bad = json.dumps('44c406a5-c6f5-4440-bfba-e8060564b717"\nG1 Z-10\n;')
    fake.spools[20] = spool_x(20, extra={"instance_uuid": bad})
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    for _ in range(3):
        await bridge.poll()
    assert jobs(printer) == []
    assert "isn't a UUID" in fake.flag(20)
    assert caplog.text.count("] can't write spool 20") == 1
    await bridge.aclose()


async def test_gives_up_and_flags_after_failed_writes(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    printer.run_job = lambda gcode: None  # The job runs but changes nothing.
    await bridge.poll()
    fake.spools[20] = spool_x(20)
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    for _ in range(4):
        await bridge.poll()
    assert len(jobs(printer)) == 2
    assert "didn't take" in fake.flag(20)
    await bridge.aclose()


async def test_drift_is_flagged_not_overwritten_unless_enabled(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    printer.material = {"name": "PLA"}
    await bridge.poll()
    await bridge.poll()
    assert jobs(printer) == [] and "printer filament differs" in fake.flag(19)
    await bridge.aclose()

    bridge, fake, printer = make(tmp_path / "b", [spool(19)], sync_drift=True)
    printer.material = {"name": "PLA"}
    await bridge.poll()
    assert len(jobs(printer)) == 1
    await bridge.poll()
    assert fake.flag(19) is None
    await bridge.aclose()


async def test_a_flag_runs_no_job(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, 1000  # unconfirmed swap, printer idle
    await bridge.poll()
    await bridge.poll()
    assert "filament was unloaded" in fake.flag(19)
    assert jobs(printer) == []
    await bridge.aclose()


async def test_unconfirmed_swap_still_writes_assigned_spool(tmp_path):
    # A jam: unload, reload the same spool from the menu (color dropped), no new print.
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    await bridge.poll()
    (gcode,) = jobs(printer)
    assert gcode.splitlines()[3].startswith("G28 O")
    assert 'O"#73767d" L0' in gcode
    assert bridge.printers[0].tools[0].swap is not None  # Writing doesn't confirm the swap.
    await bridge.aclose()


async def test_staged_swap_waits_for_unload_before_writing(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)], sync_drift=True)
    printer.material = {"name": "PLA"}
    fake.spools[20] = spool_x(20)
    await bridge.poll()  # baseline with 19 and 20 in the location: ambiguous, skipped
    fake.spools.pop(20)
    await bridge.poll()  # baseline on 19; sync_drift writes 19
    assert len(jobs(printer)) == 1
    printer.material = {"name": "PLA"}  # Menu load again
    fake.spools[20] = spool_x(20)  # 20 staged
    await bridge.poll()
    assert len(jobs(printer)) == 1
    await bridge.aclose()


async def swap_to_spool_20(bridge, fake, printer):
    """Baseline on spool 19, then a Spoolman-first swap to spool 20 and a menu load."""
    await bridge.poll()
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, None
    await bridge.poll()
    printer.extruded, printer.material = 1116, {"name": "PLA"}
    await bridge.poll()


async def test_preamble_for_wrong_model_blocks_writes_and_flags(tmp_path, caplog):
    bridge, fake, printer = make(
        tmp_path, [spool(19)], uuid_field="instance_uuid", gcode_preamble=COREONEL_PREAMBLE
    )
    printer.printer_type = "1.4.0"  # MK4
    await swap_to_spool_20(bridge, fake, printer)
    assert jobs(printer) == []
    assert "printer is MK4 (1.4.0): gcode_preamble is for COREONEL" in caplog.text
    assert "can't write to the printer: gcode_preamble is for COREONEL" in fake.flag(20)
    assert fake.used[20] == [116.0]  # charging is unaffected
    await bridge.aclose()


async def test_mk4_gets_its_own_preamble_and_no_uuid(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    printer.printer_type = "1.4.0"
    await swap_to_spool_20(bridge, fake, printer)
    (gcode,) = jobs(printer)
    assert gcode.startswith('M862.3 P "MK4" ; printer model check')
    assert "M865.1" not in gcode  # the MK4 build doesn't keep UUIDs
    assert "instance_uuid" not in (fake.spools[20].get("extra") or {})
    await bridge.poll()  # the printer matches without a UUID: no second job
    assert len(jobs(printer)) == 1
    await bridge.aclose()


async def test_mini_is_unsupported(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    printer.printer_type = "2.1.0"
    printer.counters = False
    for _ in range(3):
        await bridge.poll()
    assert caplog.text.count("has no usage counters") == 1
    assert 0 not in bridge.printers[0].tools
    await bridge.aclose()


async def test_unknown_model_blocks_writes(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    printer.printer_type = "99.1.0"
    await swap_to_spool_20(bridge, fake, printer)
    assert jobs(printer) == []
    assert "printer type '99.1.0' isn't one the bridge knows" in fake.flag(20)
    await bridge.aclose()


async def test_matching_preamble_for_other_model_writes(tmp_path):
    preamble = ('M862.3 P "MK4S" ; printer model check', "M862.5 P2")
    bridge, fake, printer = make(
        tmp_path, [spool(19)], uuid_field="instance_uuid", gcode_preamble=preamble
    )
    printer.printer_type = "1.4.1"
    await swap_to_spool_20(bridge, fake, printer)
    (gcode,) = jobs(printer)
    assert gcode.startswith('M862.3 P "MK4S"')
    await bridge.aclose()


async def test_model_checked_once_per_boot(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    seen = []
    handle = printer.handle

    def counting(request):
        seen.append(request.url.path)
        return handle(request)

    bridge.printers[0].client = PrusaLink(
        "http://printer", "k", transport=httpx.MockTransport(counting)
    )
    for _ in range(3):
        await bridge.poll()
    assert seen.count("/api/version") == 1
    printer.boot_id = 2
    await bridge.poll()
    assert seen.count("/api/version") == 2
    await bridge.aclose()


async def test_missing_filament_endpoint_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    printer.filament_endpoint = False
    for _ in range(3):
        await bridge.poll()
    assert caplog.text.count("printer firmware has no /api/v1/filament") == 1
    # With one printer the line reads as it always has, with no printer label in front.
    (record,) = [r for r in caplog.records if "isn't supported" in r.getMessage()]
    assert record.getMessage().startswith("printer firmware has no /api/v1/filament")
    assert "printer read failed" not in caplog.text
    assert 0 not in bridge.printers[0].tools

    printer.filament_endpoint = True
    caplog.set_level("INFO")
    await bridge.poll()
    # At the problem's level, ERROR, so whoever saw it sees it end.
    assert levels(caplog, "printer now reports filament; resuming") == ["ERROR"]
    assert 0 in bridge.printers[0].tools
    await bridge.aclose()


async def test_runout_reload_keeps_old_uuid_and_gets_rewritten(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    fake.spools[19]["extra"] = {"instance_uuid": json.dumps("91da34d9-c1c1-470f-bac7-185a2346f86a")}
    printer.material = {**printer.material, "instance_uuid": "91da34d9-c1c1-470f-bac7-185a2346f86a"}
    await bridge.poll()  # baseline: the printer matches spool 19
    assert jobs(printer) == []

    printer.unloads, printer.at_unload = 1, 1000  # runout: M600 unloads, reload keeps metadata
    printer.extruded = 1100
    await bridge.poll()  # unconfirmed swap
    fake.spools[19]["location"] = None
    fake.spools[20] = spool(20)  # same product and color, no UUID yet
    await bridge.poll()  # swap confirmed
    (gcode,) = jobs(printer)
    minted = json.loads(fake.spools[20]["extra"]["instance_uuid"])
    assert f'M865.1 I0 C"{minted}"' in gcode
    await bridge.poll()
    assert len(jobs(printer)) == 1 and bridge.printers[0].tools[0].needs_sync is False
    await bridge.aclose()


async def test_printer_read_error_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    printer.error = 401
    for _ in range(4):
        await bridge.poll()
    assert caplog.text.count("printer read failed") == 1
    printer.error = 500  # A different error is new.
    for _ in range(2):
        await bridge.poll()
    assert caplog.text.count("printer read failed") == 2
    printer.error = None
    for _ in range(2):
        await bridge.poll()
    assert levels(caplog, "printer reads working again after") == ["WARNING"]
    await bridge.aclose()


async def test_spoolman_read_problems_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    fake.down = True
    for _ in range(4):
        await bridge.poll()
    assert caplog.text.count("Spoolman unreachable") == 1
    assert "read failed" not in caplog.text
    fake.down = False
    fake.fail_read = 500
    for _ in range(3):
        await bridge.poll()
    assert "Spoolman reachable again" not in caplog.text  # It answers, badly.
    assert levels(caplog, "Spoolman read failed") == ["WARNING"]
    fake.fail_read = None
    for _ in range(2):
        await bridge.poll()
    assert levels(caplog, "Spoolman reads working again after") == ["WARNING"]
    await bridge.aclose()


async def test_charge_failures_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer.extruded = 1500
    fake.fail_use = [503, 503, 503]
    for _ in range(3):
        await bridge.poll()
    assert caplog.text.count("charge to spool 19 failed") == 1
    await bridge.poll()
    assert fake.used == {19: [500.0]}
    assert caplog.text.count("sent 1 queued charge(s)") == 1
    printer.extruded = 1600
    await bridge.poll()
    assert caplog.text.count("sent ") == 1
    await bridge.aclose()


async def test_charge_catching_up_after_a_failure_logs_at_warning(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("DEBUG", "spoolman_prusalink")
    await bridge.poll()
    printer.extruded = 1500
    fake.fail_use = [503, 503, 500]  # The same error twice, then a different one.
    for _ in range(3):
        await bridge.poll()
    assert levels(caplog, "charge to spool 19") == ["WARNING", "DEBUG", "WARNING"]
    await bridge.poll()
    assert levels(caplog, "sent 1 queued charge(s)") == ["WARNING"]
    await bridge.aclose()


async def test_charges_flushing_with_no_failure_log_no_catch_up(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("DEBUG", "spoolman_prusalink")
    await bridge.poll()
    printer.extruded = 1500
    await bridge.poll()
    assert fake.used == {19: [500.0]}
    assert levels(caplog, "queued charge") == []
    assert levels(caplog, "charge to spool 19") == []
    await bridge.aclose()


def lines(caplog, spool_id):
    """(level, message) of each record at INFO and above that names the spool."""
    return [
        (r.levelname, r.getMessage())
        for r in caplog.records
        if r.levelno >= logging.INFO and f"spool {spool_id}" in r.getMessage()
    ]


async def test_evict_failure_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, None
    fake.fail_patch = 500
    for _ in range(4):
        await bridge.poll()
    assert fake.spools[19]["location"] == LOC
    assert caplog.text.count("can't move spool 19 out") == 1
    assert caplog.text.count("can't update the flag on spool 19") <= 1
    fake.fail_patch = None
    caplog.clear()
    await bridge.poll()
    assert fake.spools[19]["location"] is None
    # A move is a one-time action, so it logs one INFO line even after a failure.
    assert lines(caplog, 19) == [("INFO", f"[{LOC}] moved spool 19 to no location")]
    await bridge.aclose()


async def test_failed_eviction_ends_when_the_spool_is_moved_by_hand(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, None
    fake.fail_patch = 500
    await bridge.poll()
    assert caplog.text.count("can't move spool 19 out") == 1
    fake.fail_patch = None
    fake.spools[19]["location"] = "Shelf"  # The user moves it out.
    for _ in range(2):
        await bridge.poll()
    assert bridge.printers[0].tools[0].evict == ()
    assert levels(caplog, "spool 19 no longer needs moving out") == ["INFO"]
    await bridge.aclose()


async def test_failed_eviction_stays_pending_through_the_next_swap(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, 1000
    fake.fail_patch = 500
    await bridge.poll()  # 19 -> 20; 19 can't be moved out
    fake.spools[21] = spool_x(21)
    await bridge.poll()  # 21 staged
    printer.unloads = 2
    await bridge.poll()  # 20 -> 21
    assert (bridge.printers[0].tools[0].active, bridge.printers[0].tools[0].evict) == (21, (19, 20))
    fake.fail_patch = None
    await bridge.poll()
    assert fake.spools[19]["location"] is None and fake.spools[20]["location"] is None
    assert bridge.printers[0].tools[0].evict == ()
    await bridge.aclose()


async def test_flag_failure_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, 1000
    fake.fail_patch = 500
    for _ in range(4):
        await bridge.poll()
    assert caplog.text.count("can't update the flag on spool 19") == 1
    fake.fail_patch = None
    caplog.clear()
    await bridge.poll()
    flag = fake.flag(19)
    assert "unloaded with no Spoolman change" in flag
    assert lines(caplog, 19) == [("INFO", f"set flag on spool 19: {flag}")]
    await bridge.aclose()


async def test_flag_failure_ends_when_spoolman_already_matches(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, 1000
    fake.fail_patch = 500
    await bridge.poll()
    assert caplog.text.count("can't update the flag on spool 19") == 1
    # The user writes the same flag themselves, so there's nothing left to update.
    fake.spools[19]["extra"]["bridge_flag"] = json.dumps(bridge._flags[19])
    await bridge.poll()
    assert levels(caplog, "flag on spool 19 is up to date") == ["INFO"]
    await bridge.aclose()


async def test_field_setup_failure_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    fake.fail_field = 500
    for _ in range(4):
        await bridge.poll()
    # Once each for the flag and claim fields.
    assert caplog.text.count("spool field") == 2
    fake.fail_field = None
    await bridge.poll()
    assert caplog.text.count("spool field is set up") == 2
    assert caplog.text.count("spool field") == 4
    await bridge.aclose()


async def test_busy_printer_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    fake.spools[20] = spool_x(20)
    printer.busy = True
    for _ in range(4):
        await bridge.poll()
    assert caplog.text.count("printer busy") == 1
    printer.busy = False
    await bridge.poll()
    assert len(jobs(printer)) == 1
    await bridge.aclose()


async def test_sync_job_failure_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    fake.spools[20] = spool_x(20)
    printer.upload_error = 500
    for _ in range(4):
        await bridge.poll()
    assert caplog.text.count("printer sync job failed") == 1
    printer.upload_error = None
    await bridge.poll()
    assert len(jobs(printer)) == 1
    assert caplog.text.count("printer sync job working again") == 1
    await bridge.aclose()


async def written_state(tmp_path, **cfg):
    """A bridge that has swapped to spool 20 and written it to the printer."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid", **cfg)
    await swap_to_spool_20(bridge, fake, printer)
    assert len(jobs(printer)) == 1
    await bridge.poll()
    assert len(jobs(printer)) == 1
    return bridge, fake, printer


@pytest.mark.parametrize("sync_drift", [False, True])
@pytest.mark.parametrize("reason", ["drift", "edited", "needs_sync"])
async def test_emptied_location_reports_only_real_drift(tmp_path, caplog, reason, sync_drift):
    bridge, fake, printer = await written_state(tmp_path, sync_drift=sync_drift)
    caplog.set_level("INFO")
    fake.spools[20]["location"] = None  # Moved out, with nothing in its place.
    if reason == "drift":
        printer.material = {"name": "PETG"}
    elif reason == "edited":
        fake.spools[20]["filament"]["color_hex"] = "00ff00"
    else:
        bridge.printers[0].tools[0] = replace(bridge.printers[0].tools[0], needs_sync=True)
    for _ in range(3):
        await bridge.poll()
    assert len(jobs(printer)) == 1  # only the original write
    assert "spool None" not in caplog.text
    flag = fake.flag(20) or ""
    if reason in ("drift", "edited"):
        # A Spoolman edit the printer won't get is a real difference: report the
        # field, not the edit as a pending write.
        assert caplog.text.count("printer differs from spool 20") == 1
        assert "printer filament differs" in flag
        assert "since the last write" not in caplog.text
    else:
        assert "printer differs" not in caplog.text
        assert "printer filament differs" not in flag
    await bridge.aclose()


async def test_spoolman_color_edit_is_written(tmp_path):
    bridge, fake, printer = await written_state(tmp_path)
    fake.spools[20]["filament"]["color_hex"] = "00ff00"
    await bridge.poll()
    assert len(jobs(printer)) == 2
    assert 'O"#00ff00"' in jobs(printer)[-1]
    await bridge.poll()
    assert len(jobs(printer)) == 2
    await bridge.aclose()


async def test_spoolman_temperature_edit_is_written(tmp_path):
    bridge, fake, printer = await written_state(tmp_path)
    fake.spools[20]["filament"]["settings_extruder_temp"] = 205
    await bridge.poll()
    assert len(jobs(printer)) == 2
    assert " T205 " in jobs(printer)[-1]
    await bridge.poll()
    assert len(jobs(printer)) == 2
    await bridge.aclose()


async def test_spoolman_edit_that_doesnt_apply_is_retried_then_flagged(tmp_path):
    bridge, fake, printer = await written_state(tmp_path, flag_drift=True)
    printer.run_job = lambda gcode: None  # Cancelled on the screen: nothing changes.
    fake.spools[20]["filament"]["color_hex"] = "00ff00"
    for _ in range(4):
        await bridge.poll()
    assert len(jobs(printer)) == 3  # The swap write, then the edit twice (W5).
    assert "didn't take" in fake.flag(20)
    assert "printer filament differs" not in fake.flag(20)
    await bridge.aclose()


async def test_temperature_edit_that_doesnt_apply_is_retried_then_flagged(tmp_path):
    # The printer reports temperatures, so a temperature-only edit is checked like a color.
    bridge, fake, printer = await written_state(tmp_path, flag_drift=True)
    printer.run_job = lambda gcode: None  # Cancelled on the screen: nothing changes.
    fake.spools[20]["filament"]["settings_extruder_temp"] = 205
    for _ in range(4):
        await bridge.poll()
    assert len(jobs(printer)) == 3  # The swap write, then the edit twice (W5).
    assert "didn't take" in fake.flag(20)
    assert "nozzle printer=215 spoolman=205" in fake.flag(20)
    await bridge.aclose()


@pytest.mark.parametrize("sync_drift", [False, True])
async def test_temperature_changed_on_the_printer_is_drift(tmp_path, caplog, sync_drift):
    bridge, fake, printer = await written_state(tmp_path, flag_drift=True, sync_drift=sync_drift)
    printer.material = {**printer.material, "bed_temperature": 70}
    await bridge.poll()
    assert "bed printer=70 spoolman=60" in caplog.text
    if sync_drift:
        assert len(jobs(printer)) == 2
        await bridge.poll()
        assert fake.flag(20) is None
    else:
        assert len(jobs(printer)) == 1
        assert "printer filament differs: bed printer=70 spoolman=60" in fake.flag(20)
    await bridge.aclose()


async def test_spoolman_edit_is_not_drift_while_its_job_runs(tmp_path):
    bridge, fake, printer = await written_state(tmp_path, flag_drift=True)
    queued = []

    def start_job(gcode):  # The job starts but hasn't reached M865 yet.
        printer.job, printer.job_name = 60, "spoolman-sync.gcode"
        queued.append(gcode)

    printer.run_job = start_job
    fake.spools[20]["filament"]["color_hex"] = "00ff00"
    await bridge.poll()
    assert len(jobs(printer)) == 2
    await bridge.poll()
    assert fake.flag(20) is None
    printer.job = None
    Printer.run_job(printer, queued[0])
    await bridge.poll()
    await bridge.poll()
    assert len(jobs(printer)) == 2 and fake.flag(20) is None
    assert (
        bridge.printers[0].tools[0].needs_sync is False
        and bridge.printers[0].tools[0].sync_attempts == 0
    )
    await bridge.aclose()


async def test_print_started_during_the_poll_holds_the_sync_job(tmp_path, caplog):
    # The poll reads IDLE, but a print starts before the upload. The re-check right
    # before the upload sees it, and the job waits for the next idle poll.
    caplog.set_level("INFO")
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    printer.states = ["IDLE"] * 4 + ["PRINTING"]  # The fifth read is the re-check.
    await swap_to_spool_20(bridge, fake, printer)
    assert jobs(printer) == [] and printer.refused == 0
    assert "printer became PRINTING; sync job waits" in caplog.text
    assert bridge.printers[0].tools[0].sync_attempts == 0  # Nothing counted as written.
    await bridge.poll()
    assert len(jobs(printer)) == 1
    # The wait ends with the job, which its own line reports: no separate recovery line.
    assert caplog.text.count("ran printer job") == 1
    assert "no longer needed" not in caplog.text
    await bridge.aclose()


async def test_sync_job_waiting_on_the_same_busy_state_logs_once(tmp_path, caplog):
    caplog.set_level("INFO")
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    printer.states = ["IDLE"] * 4 + ["PRINTING", "IDLE", "PRINTING"]  # Re-checks: 5th, 7th.
    await swap_to_spool_20(bridge, fake, printer)
    await bridge.poll()
    assert jobs(printer) == []
    assert levels(caplog, "became PRINTING; sync job waits") == ["INFO"]
    await bridge.poll()
    assert len(jobs(printer)) == 1
    assert "no longer needed" not in caplog.text
    await bridge.aclose()


async def test_sync_delay_waits_for_the_printer_to_stay_idle(tmp_path, caplog):
    caplog.set_level("INFO")
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid", sync_delay=300)
    now = [1000.0]
    bridge.clock = lambda: now[0]
    await swap_to_spool_20(bridge, fake, printer)  # Idle since the first poll.
    assert jobs(printer) == []
    now[0] += 240
    await bridge.poll()
    assert jobs(printer) == []
    assert caplog.text.count("sync job waits until the printer has been idle for 300s") == 1
    printer.job = 140  # The user starts a print: the wait starts over after it.
    await bridge.poll()
    printer.job = None
    await bridge.poll()  # Idle again: the wait starts here.
    now[0] += 299
    await bridge.poll()
    assert jobs(printer) == []
    now[0] += 1
    await bridge.poll()
    assert len(jobs(printer)) == 1
    await bridge.aclose()


async def test_sync_delay_logs_a_fractional_delay(tmp_path, caplog):
    caplog.set_level("INFO")
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid", sync_delay=2.5)
    bridge.clock = lambda: 1000.0
    await swap_to_spool_20(bridge, fake, printer)
    assert "until the printer has been idle for 2.5s" in caplog.text
    await bridge.aclose()


@pytest.mark.parametrize("failure", ["printer", "spoolman"])
async def test_sync_delay_restarts_after_polls_that_miss_the_printer(tmp_path, failure):
    # A print the bridge never saw, because the printer read failed or a queued
    # charge kept failing, mustn't count as idle time.
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid", sync_delay=300)
    now = [1000.0]
    bridge.clock = lambda: now[0]
    await bridge.poll()  # Idle from t=1000.
    if failure == "printer":
        printer.error = 503
    else:
        fake.fail_use = [503] * 5
        printer.extruded = 1100
        await bridge.poll()  # The charge fails and is queued.
    await bridge.poll()  # No printer read: the read or the queued charge fails.
    now[0] += 4000
    printer.error = None
    fake.fail_use = []
    await swap_to_spool_20(bridge, fake, printer)
    assert jobs(printer) == []  # The wait started over when reads came back.
    now[0] += 300
    await bridge.poll()
    assert len(jobs(printer)) == 1
    await bridge.aclose()


async def test_wait_for_a_running_sync_job_is_not_logged(tmp_path, caplog):
    bridge, fake, printer = await written_state(tmp_path)
    queued = []

    def start_job(gcode):  # The job starts but hasn't reached M865 yet.
        printer.job, printer.job_name = 60, "spoolman-sync.gcode"
        queued.append(gcode)

    printer.run_job = start_job
    caplog.clear()
    fake.spools[20]["filament"]["color_hex"] = "00ff00"
    await bridge.poll()
    assert len(jobs(printer)) == 2
    # The printer still shows the old color while the job runs. That's the job
    # pending, not another reason to write.
    await bridge.poll()
    printer.job = None
    Printer.run_job(printer, queued[0])
    await bridge.poll()
    assert caplog.text.count("] printer will be set to spool 20") == 1
    # Once the printer shows it, the note's end is logged once, at the note's level.
    assert levels(caplog, "] no longer: printer will be set to spool 20") == ["WARNING"]
    assert bridge.printers[0].tools[0].needs_sync is False
    await bridge.aclose()


@pytest.mark.parametrize("flag_drift", [True, False])
@pytest.mark.parametrize(
    "bad",
    [
        "{44c406a5-c6f5-4440-bfba-e8060564b717}",
        '44c406a5-c6f5-4440-bfba-e8060564b717"\nG1 Z-10\n;',
    ],
)
async def test_spoolman_edit_to_a_bad_uuid_is_flagged_not_written(
    tmp_path, caplog, flag_drift, bad
):
    bridge, fake, printer = await written_state(tmp_path, flag_drift=flag_drift)
    caplog.set_level("INFO")
    fake.spools[20]["extra"]["instance_uuid"] = json.dumps(bad)
    for _ in range(3):
        await bridge.poll()
    assert len(jobs(printer)) == 1
    assert "isn't a UUID" in fake.flag(20)
    assert "printer filament differs" not in fake.flag(20)
    assert caplog.text.count("] can't write spool 20") == 1
    await bridge.aclose()


async def test_printer_change_after_write_is_drift(tmp_path):
    bridge, fake, printer = await written_state(tmp_path, flag_drift=True)
    printer.material = {"name": "PETG"}  # changed on the printer, not in Spoolman
    await bridge.poll()
    assert len(jobs(printer)) == 1
    assert "printer filament differs" in fake.flag(20)
    await bridge.aclose()


async def test_printer_change_with_a_spoolman_edit_is_drift_not_an_edit(tmp_path, caplog):
    """The printer no longer shows the last write, so the edit isn't written over it."""
    bridge, fake, printer = await written_state(tmp_path, flag_drift=True)
    caplog.set_level("INFO")
    printer.material = {"name": "PETG"}  # changed on the printer
    fake.spools[20]["filament"]["color_hex"] = "00ff00"  # and in Spoolman
    for _ in range(2):
        await bridge.poll()
    assert len(jobs(printer)) == 1
    assert "printer filament differs" in fake.flag(20)
    assert caplog.text.count("printer differs from spool 20") == 1
    assert "since the last write" not in caplog.text
    await bridge.aclose()


async def test_spool_deleted_before_its_write_runs_holds_usage_and_writes_nothing(tmp_path):
    """Spool 20 is listed, then deleted before the poll's charge: its edit isn't written,
    and its usage goes to the next spool, which is written instead."""
    bridge, fake, printer = await written_state(tmp_path)
    fake.spools[20]["filament"]["color_hex"] = "00ff00"  # A write is due.
    printer.extruded += 50
    fake.fail_use = [404]  # Deleted between the list read and the charge.
    used = list(fake.used[20])
    await bridge.poll()
    assert len(jobs(printer)) == 1
    assert fake.used[20] == used
    st = bridge.printers[0].tools[0]
    assert (st.active, st.deleted_spool, st.held_mm) == (None, 20, 50)

    del fake.spools[20]
    fake.spools[21] = spool_x(21, color="0000ff")
    await bridge.poll()
    assert fake.used[21] == [50.0]
    assert len(jobs(printer)) == 2 and 'O"#0000ff"' in jobs(printer)[-1]
    await bridge.aclose()


async def test_fresh_install_does_not_rewrite(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    for _ in range(2):
        await bridge.poll()
    assert jobs(printer) == []
    await bridge.aclose()


def weighted(spool_json, remaining, net=1000.0):
    """Give a spool a net weight and usage, as Spoolman reports them. Spoolman clamps its
    own remaining_weight at 0, so the bridge works from used_weight."""
    spool_json["filament"]["weight"] = net
    spool_json["used_weight"] = net - remaining
    spool_json["remaining_weight"] = max(remaining, 0)
    return spool_json


async def runout_mid_print(tmp_path, remaining):
    bridge, fake, printer = make(tmp_path, [weighted(spool(19), remaining)])
    printer.job = 131
    await bridge.poll()  # baseline mid-print
    printer.extruded = 1500
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.extruded = 1, 1500, 1500  # runout
    await bridge.poll()
    return bridge, fake, printer


async def test_runout_with_plenty_left_is_flagged(tmp_path):
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    assert "unloaded mid-print with 399 g left" in fake.flag(19)  # 400 g less 500 mm
    await bridge.poll()  # the same spool stays active: the flag stays
    assert "unloaded mid-print" in fake.flag(19)
    assert 19 in read_state(tmp_path / "bridge.db").runouts
    await bridge.aclose()


async def test_runout_flag_survives_the_bridges_own_charges(tmp_path):
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    for _ in range(3):  # Same spool reloaded; printing goes on.
        printer.extruded += 500
        await bridge.poll()
    assert fake.used[19][-1] > 0
    assert "unloaded mid-print" in fake.flag(19)
    await bridge.aclose()


async def test_runout_flag_clears_when_weight_is_edited(tmp_path, caplog):
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    caplog.set_level("INFO")
    fake.spools[19]["used_weight"] = 990.0  # The user corrects it: 10 g left.
    await bridge.poll()
    assert fake.flag(19) is None or "mid-print" not in fake.flag(19)
    assert 19 not in read_state(tmp_path / "bridge.db").runouts
    assert caplog.text.count("weight was edited in Spoolman") == 1
    await bridge.aclose()


async def test_runout_flag_clears_when_weight_is_edited_with_a_charge_queued(tmp_path):
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    printer.extruded += 500
    fake.fail_use = [503]  # The charge stays queued past the next read.
    await bridge.poll()
    assert bridge.state.outbox
    fake.spools[19]["used_weight"] = 990.0  # The user corrects it meanwhile.
    await bridge.poll()  # Sends the charge, whose response includes the edit.
    assert 19 not in bridge.state.runouts
    await bridge.aclose()


async def test_runout_flag_clears_when_a_charge_response_is_lost(tmp_path):
    # The resent charge is applied twice, so its response doesn't match: read as an edit.
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    printer.extruded += 500
    fake.lose_use = 1
    await bridge.poll()
    await bridge.poll()
    assert fake.used[19][-1] == fake.used[19][-2]  # Sent twice.
    assert 19 not in bridge.state.runouts
    await bridge.aclose()


async def test_runout_flag_clears_when_initial_weight_is_edited(tmp_path):
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    fake.spools[19]["initial_weight"] = 700.0
    await bridge.poll()
    assert 19 not in read_state(tmp_path / "bridge.db").runouts
    await bridge.aclose()


async def test_runout_flag_takes_a_charge_it_cant_check_as_the_bridges_own(tmp_path):
    # Without the charge's weight, the response can't be checked, so it's taken as the
    # bridge's own charge. Otherwise the old stored weight would read as an edit next time.
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    printer.extruded += 500
    fake.fail_use = [503]  # The charge stays queued.
    await bridge.poll()
    assert bridge.state.outbox
    bridge.state.outbox[0]["grams"] = None  # Charged while the spool wasn't known.
    assert await bridge.flush()
    assert bridge.state.runouts[19].used == fake.spools[19]["used_weight"]
    await bridge.poll()
    assert 19 in bridge.state.runouts
    assert "unloaded mid-print" in fake.flag(19)
    await bridge.aclose()


async def test_runout_near_empty_is_not_flagged(tmp_path):
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=30.0)
    assert fake.flag(19) is None or "mid-print" not in fake.flag(19)
    await bridge.aclose()


async def test_runout_flag_clears_when_spool_is_next_active(tmp_path):
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    fake.spools[19]["location"] = None
    fake.spools[20] = spool(20)  # confirms the swap: spool 20 goes in
    await bridge.poll()
    assert "mid-print" in (fake.flag(19) or "")
    fake.spools[20]["location"] = None
    fake.spools[19]["location"] = LOC  # spool 19 back in the tool later
    printer.unloads, printer.at_unload = 2, printer.extruded
    await bridge.poll()
    assert fake.flag(19) is None or "mid-print" not in fake.flag(19)
    await bridge.aclose()


async def test_runout_flag_on_an_unlisted_spool_is_left_alone(tmp_path):
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    fake.spools[20] = spool(20)  # confirms the swap: spool 19 is evicted
    await bridge.poll()
    runout = replace(bridge.state.runouts[19])
    fake.spools[19]["archived"] = True  # Spoolman's list leaves it out now.
    for _ in range(2):
        await bridge.poll()
    assert read_state(tmp_path / "bridge.db").runouts[19] == runout

    # Unarchived with its weight corrected: that's an edit, which clears the flag.
    fake.spools[19]["archived"] = False
    fake.spools[19]["used_weight"] = 990.0
    await bridge.poll()
    assert 19 not in read_state(tmp_path / "bridge.db").runouts
    await bridge.aclose()


async def test_unload_outside_a_print_is_not_a_runout(tmp_path):
    bridge, fake, printer = make(tmp_path, [weighted(spool(19), 400.0)])
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, 1000
    await bridge.poll()
    assert "mid-print" not in (fake.flag(19) or "")
    await bridge.aclose()


async def test_negative_remaining_is_logged_once(tmp_path, caplog):
    caplog.set_level("INFO")
    bridge, fake, printer = make(tmp_path, [weighted(spool(19), -12.0)])
    for _ in range(3):
        printer.extruded += 100
        await bridge.poll()
    assert caplog.text.count("shows -12 g remaining") == 1
    assert fake.flag(19) is None
    # Correcting the weight ends it with a line at the problem's own level, INFO.
    weighted(fake.spools[19], 50.0)
    for _ in range(2):
        printer.extruded += 100
        await bridge.poll()
    assert levels(caplog, "spool 19 no longer shows a negative weight") == ["INFO"]
    await bridge.aclose()


async def test_negative_remaining_that_recovers_while_loaded_logs_both_at_info(tmp_path, caplog):
    caplog.set_level("INFO")
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    assert "remaining" not in caplog.text
    weighted(fake.spools[19], -3.0)
    for _ in range(2):
        await bridge.poll()
    assert levels(caplog, "spool 19 shows -3 g remaining") == ["INFO"]
    weighted(fake.spools[19], 40.0)
    for _ in range(2):
        await bridge.poll()
    assert levels(caplog, "spool 19 no longer shows a negative weight") == ["INFO"]
    assert bridge._negative == set()
    await bridge.aclose()


async def test_negative_spool_that_leaves_and_rejoins_the_list_logs_one_start(tmp_path, caplog):
    caplog.set_level("INFO")
    bridge, fake, printer = make(tmp_path, [weighted(spool(19), -12.0)])
    await bridge.poll()
    fake.spools[19]["archived"] = True  # Spoolman's list leaves it out.
    await bridge.poll()
    fake.spools[19]["archived"] = False
    await bridge.poll()
    assert levels(caplog, "shows -12 g remaining") == ["INFO"]
    assert "no longer shows a negative" not in caplog.text
    await bridge.aclose()


async def test_negative_remaining_ends_when_the_spool_is_unloaded(tmp_path, caplog):
    caplog.set_level("INFO")
    bridge, fake, printer = make(tmp_path, [weighted(spool(19), -12.0)])
    await bridge.poll()
    assert levels(caplog, "shows -12 g remaining") == ["INFO"]
    fake.spools[20] = spool_x(20)  # Staged, so the unload swaps 19 out.
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, 1000
    for _ in range(2):
        await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert bridge._negative == set()
    assert "negative weight" not in caplog.text
    assert levels(caplog, "shows -12 g remaining") == ["INFO"]
    await bridge.aclose()


# Deleted and archived active spools (M3, M3a, Q7)


async def test_deleted_spool_hold_logs_no_end_until_it_ends(tmp_path, caplog):
    # Polls with and without extrusion while the hold lasts, as at a print's end.
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer.extruded = 1500
    await bridge.poll()
    del fake.spools[19]
    for extruded in (1700, 1800, 1800, 1900, 1900):
        printer.extruded = extruded
        await bridge.poll()
    st = read_state(tmp_path / "bridge.db").printers["default"].tools[0]
    assert (st.deleted_spool, st.held_mm) == (19, 400)
    assert "no longer" not in caplog.text
    assert caplog.text.count("spool 19 was deleted in Spoolman") == 1
    await bridge.aclose()


async def test_held_usage_over_a_reboot_logs_no_end_until_it_ends(tmp_path, caplog):
    # Usage held with no spool assigned, kept over a reboot, then polls without usage.
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    del fake.spools[19]["location"]
    printer.unloads, printer.at_unload = 1, 1000
    printer.extruded = 1100
    await bridge.poll()
    await bridge.poll()
    assert bridge.printers[0].tools[0].active is None
    assert "holding usage: no spool assigned" in caplog.text
    printer.boot_id, printer.extruded, printer.unloads, printer.at_unload = 2, 0, 0, 0
    for _ in range(3):
        await bridge.poll()
    assert bridge.printers[0].tools[0].held_mm == 100
    assert "no longer" not in caplog.text
    fake.spools[20] = spool(20)
    await bridge.poll()
    assert levels(caplog, "no longer: holding usage: no spool assigned") == ["WARNING"]
    await bridge.aclose()


async def test_deleted_spool_holds_usage_for_the_next_spool(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer.extruded = 1500
    await bridge.poll()
    del fake.spools[19]
    for extruded in (1700, 1800, 1900):  # Printing on with the spool deleted.
        printer.extruded = extruded
        await bridge.poll()
    assert fake.used == {19: [500.0]}
    assert fake.lookups == [19]
    st = read_state(tmp_path / "bridge.db").printers["default"].tools[0]
    assert (st.active, st.deleted_spool, st.held_mm) == (None, 19, 400)
    assert caplog.text.count("spool 19 was deleted in Spoolman") == 1
    # The hold is logged by the line above, not as a tool note each poll.
    assert "holding usage since spool 19 was deleted" not in caplog.text
    assert "rejected" not in caplog.text

    fake.spools[20] = spool(20)  # The user re-creates the spool and moves it in.
    await bridge.poll()
    await bridge.poll()
    assert fake.used == {19: [500.0], 20: [400.0]}
    assert [r["reason"] for r in ledger(tmp_path) if r.get("spool_id") == 20] == [
        "held after spool 19 was deleted"
    ]
    assert caplog.text.count("held after spool 19 was deleted") == 2  # Event and charge.
    st = read_state(tmp_path / "bridge.db").printers["default"].tools[0]
    assert (st.active, st.deleted_spool, st.held_mm) == (20, None, 0)
    assert levels(caplog, "no longer holding usage for a deleted spool") == ["WARNING"]
    await bridge.aclose()


async def test_deleted_spool_hold_that_ends_and_starts_again_logs_each(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer.extruded = 1500
    await bridge.poll()
    del fake.spools[19]
    printer.extruded = 1700
    await bridge.poll()
    fake.spools[20] = spool(20)  # Takes the held usage, which ends the hold.
    await bridge.poll()
    await bridge.poll()
    del fake.spools[20]
    printer.extruded = 1900
    await bridge.poll()
    printer.extruded = 2000
    await bridge.poll()
    messages = [
        r.getMessage()
        for r in caplog.records
        if "was deleted in Spoolman" in r.getMessage() or "no longer holding" in r.getMessage()
    ]
    assert [m.split("] ")[1].split(" ")[0] for m in messages] == ["spool", "no", "spool"]
    assert "spool 19 was deleted" in messages[0] and "spool 20 was deleted" in messages[2]
    await bridge.aclose()


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


async def test_deleted_spool_hold_restored_after_a_restart_is_logged_and_ends(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    await bridge.poll()
    del fake.spools[19]
    printer.extruded = 1700
    await bridge.poll()

    # The hold comes back from the database; nothing looks the spool up again.
    caplog.set_level("INFO")
    caplog.clear()
    bridge = restart(bridge)
    for extruded in (1800, 1900, 2000):
        printer.extruded = extruded
        await bridge.poll()
    st = bridge.printers[0].tools[0]
    assert (st.deleted_spool, st.held_mm) == (19, 500)
    assert levels(caplog, "spool 19 was deleted in Spoolman; holding its usage") == ["WARNING"]

    fake.spools[20] = spool(20)  # The next spool takes the held usage.
    await bridge.poll()
    await bridge.poll()
    assert fake.used[20][0] == 500.0
    assert levels(caplog, "no longer holding usage for a deleted spool") == ["WARNING"]
    await bridge.aclose()


async def test_deleted_spool_hold_restored_after_a_restart_is_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    await bridge.poll()
    del fake.spools[19]
    printer.extruded = 1700
    await bridge.poll()
    caplog.set_level("INFO")
    caplog.clear()
    bridge = restart(bridge)
    for extruded in (1800, 1900, 2000):
        printer.extruded = extruded
        await bridge.poll()
    assert levels(caplog, "spool 19 was deleted in Spoolman") == ["WARNING"]
    await bridge.aclose()


async def test_deleted_spool_hold_moving_to_another_spool_logs_again(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    await bridge.poll()
    del fake.spools[19]
    printer.extruded = 1700
    await bridge.poll()
    caplog.set_level("INFO")
    caplog.clear()
    bridge.printers[0].tools[0] = replace(bridge.printers[0].tools[0], deleted_spool=21)
    await bridge.poll()
    await bridge.poll()
    assert levels(caplog, "spool 21 was deleted in Spoolman") == ["WARNING"]
    assert "no longer holding" not in caplog.text
    await bridge.aclose()


async def test_spool_deleted_during_an_outage_is_charged_to_the_next(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    await bridge.poll()
    assert fake.used == {19: [500.0]}

    # Stopped while the spool is deleted and printing goes on.
    del fake.spools[19]
    printer.extruded = 1900

    # Back with a new spool moved in: it gets the 400 mm printed since the last poll.
    bridge = restart(bridge)
    await bridge.poll()
    fake.spools[20] = spool(20)
    await bridge.poll()
    await bridge.poll()
    assert fake.used == {19: [500.0], 20: [400.0]}
    st = read_state(tmp_path / "bridge.db").printers["default"].tools[0]
    assert (st.active, st.held_mm) == (20, 0)
    await bridge.aclose()


async def held_live_for_deleted_spool(tmp_path):
    """Spool 19 is deleted during a live run, which holds 400 mm for the next spool."""
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    await bridge.poll()
    del fake.spools[19]
    for extruded in (1700, 1900):
        printer.extruded = extruded
        await bridge.poll()
    st = read_state(tmp_path / "bridge.db").printers["default"].tools[0]
    assert (st.deleted_spool, st.held_mm) == (19, 400)
    return bridge, fake, printer


async def test_usage_held_before_an_outage_is_kept(tmp_path):
    bridge, fake, printer = await held_live_for_deleted_spool(tmp_path)
    printer.extruded = 2100  # 200 mm printed while stopped.

    # The next spool gets the usage held before and the 200 mm.
    bridge = restart(bridge)
    fake.spools[20] = spool(20)
    await bridge.poll()
    await bridge.poll()
    assert fake.used == {19: [500.0], 20: [400.0, 200.0]}
    await bridge.aclose()


async def assert_after_outage(tmp_path, live, middle, after, used):
    """Run `live`, then `middle` with the bridge stopped, then `after` from a restart.

    Each step of `middle` changes the printer or Spoolman. Asserts what Spoolman was
    charged, and returns the final state.
    """
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await live(bridge, fake, printer)
    for step in middle:
        step(fake, printer)
    bridge = restart(bridge)
    await after(bridge, fake, printer)
    await bridge.aclose()
    assert fake.used == used
    return read_state(tmp_path / "bridge.db")


def extrude(mm):
    def step(fake, printer):
        printer.extruded = mm

    return step


async def test_open_swap_survives_an_outage(tmp_path):
    async def live(bridge, fake, printer):
        await bridge.poll()
        printer.extruded = 1500
        await bridge.poll()
        printer.unloads, printer.at_unload, printer.extruded = 1, 1500, 1600
        await bridge.poll()
        assert bridge.printers[0].tools[0].swap.mm == 100

    async def after(bridge, fake, printer):
        await bridge.poll()  # The 200 mm printed while stopped.
        fake.spools[19]["location"] = None
        fake.spools[20] = spool(20)
        await bridge.poll()

    await assert_after_outage(
        tmp_path,
        live,
        [extrude(1700), extrude(1800)],
        after,
        {19: [500.0, 100.0, 200.0, -300.0], 20: [300.0]},
    )


async def test_claim_window_survives_an_outage(tmp_path):
    async def live(bridge, fake, printer):
        await bridge.poll()
        printer.extruded = 1100
        await bridge.poll()

    async def after(bridge, fake, printer):
        await bridge.poll()
        fake.spools[20] = spool_x(20, extra=dict(CLAIMED))
        await bridge.poll()

    await assert_after_outage(
        tmp_path,
        live,
        [extrude(1200), extrude(1300)],
        after,
        {19: [100.0, 200.0, -300.0], 20: [300.0]},
    )


async def test_usage_held_then_charged_during_an_outage_is_charged_once(tmp_path):
    async def live(bridge, fake, printer):
        await bridge.poll()
        printer.extruded = 1500
        await bridge.poll()

    def no_filament(fake, printer):
        printer.material, printer.extruded = None, 1700

    def filament_back(fake, printer):
        printer.material, printer.extruded = {"name": "PLA", "color": "#73737b"}, 1800

    async def after(bridge, fake, printer):
        await bridge.poll()
        await bridge.poll()

    await assert_after_outage(
        tmp_path, live, [no_filament, filament_back], after, {19: [500.0, 300.0]}
    )


async def test_provisional_charge_survives_an_outage(tmp_path):
    async def live(bridge, fake, printer):
        await bridge.poll()
        printer.unloads, printer.at_unload, printer.extruded = 1, 1000, 1100
        await bridge.poll()
        printer.job = 7
        await bridge.poll()
        printer.extruded = 1200  # First extrusion: the swap is assumed, provisionally.
        await bridge.poll()
        assert bridge.printers[0].tools[0].provisional.mm == 200

    async def after(bridge, fake, printer):
        await bridge.poll()

    state = await assert_after_outage(
        tmp_path, live, [extrude(1300), extrude(1400)], after, {19: [100.0, 100.0, 200.0]}
    )
    assert state.tools("default")[0].provisional.mm == 400


async def test_claim_made_during_an_outage_is_handled(tmp_path):
    async def live(bridge, fake, printer):
        await bridge.poll()
        printer.extruded = 1100
        await bridge.poll()

    def claim(fake, printer):
        fake.spools[20] = spool_x(20, extra=dict(CLAIMED))

    async def after(bridge, fake, printer):
        await bridge.poll()
        assert claim_box(fake, 20) is False

    await assert_after_outage(tmp_path, live, [claim], after, {19: [100.0, -100.0], 20: [100.0]})


async def test_404_in_flush_holds_the_charge(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer.extruded = 1500
    fake.fail_use = [503]
    await bridge.poll()  # The charge stays queued.
    del fake.spools[19]
    printer.extruded = 1600
    await bridge.poll()  # The queued charge gets a 404.
    await bridge.poll()
    assert fake.used == {} and fake.lookups == []
    st = read_state(tmp_path / "bridge.db").printers["default"].tools[0]
    assert (st.active, st.deleted_spool, st.held_mm) == (None, 19, 600)
    held = [r for r in ledger(tmp_path) if r.get("held")]
    assert [(r["spool_id"], r["failed"]) for r in held] == [(19, "404")]
    assert caplog.text.count("spool 19 was deleted in Spoolman") == 1
    assert "dropping it" not in caplog.text
    assert caplog.text.count("sent 0 queued charge(s); held 1 for the next spool") == 1

    fake.spools[21] = spool(21)
    await bridge.poll()
    assert fake.used == {21: [600.0]}
    await bridge.aclose()


async def test_staged_spool_is_flagged_while_usage_is_held(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    fake.spools[20] = spool(20)  # Staged.
    await bridge.poll()
    del fake.spools[19]
    printer.extruded = 1300
    await bridge.poll()
    await bridge.poll()
    assert "usage is being held because spool 19 was deleted" in fake.flag(20)
    assert fake.writes.count(("patch", 20)) == 1
    assert fake.used == {}

    printer.unloads, printer.at_unload = 1, 1300  # Swap commit.
    await bridge.poll()
    assert fake.used == {20: [300.0]}
    assert fake.flag(20) is None
    await bridge.aclose()


async def test_archived_spool_is_charged_and_flagged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    fake.spools[19]["archived"] = True
    for extruded in (1100, 1200, 1300):
        printer.extruded = extruded
        await bridge.poll()
    assert fake.used == {19: [100.0, 100.0, 100.0]}
    # The charges carry the spool's filament data though it's no longer listed (#56).
    charged = [r for r in ledger(tmp_path) if r.get("spool_id") == 19 and "use_length" in r]
    assert [r["grams"] for r in charged] == [0.298] * 3
    assert "spool 19 (Grey): +100 mm" in caplog.text
    assert fake.lookups == [19]
    assert fake.flag(19) == f"{LOC}: spool is archived but still loaded"
    assert fake.writes.count(("patch", 19)) == 1
    assert caplog.text.count("spool 19 is archived but still loaded") == 1

    printer.unloads, printer.at_unload = 1, 1300  # Unloaded: the flag clears.
    await bridge.poll()
    await bridge.poll()
    assert fake.flag(19) is None
    assert fake.lookups == [19]
    await bridge.aclose()


@pytest.mark.parametrize("failure", ["http", "garbled"])
async def test_failed_lookup_of_a_spool_missing_from_the_list_is_logged_once(
    tmp_path, caplog, failure
):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    fake.unlisted = {19}
    if failure == "http":
        fake.fail_lookup = 500
    else:
        fake.garble_lookup = True
    for extruded in (1100, 1200, 1300):
        printer.extruded = extruded
        await bridge.poll()
    # Looked up again each poll, but the problem is logged once.
    assert fake.lookups == [19, 19, 19]
    assert caplog.text.count("can't look up spool 19, which is missing") == 1
    assert bridge._archived == {}
    assert "archived" not in caplog.text and "deleted" not in caplog.text
    # Each poll is still charged, without filament data, as for a spool that's merely unlisted.
    assert fake.used == {19: [100.0, 100.0, 100.0]}
    charged = [r for r in ledger(tmp_path) if r.get("spool_id") == 19 and "use_length" in r]
    assert [r["grams"] for r in charged] == [None] * 3
    # Once the lookup works again the problem clears with one INFO line, so a new
    # failure is logged afresh.
    fake.fail_lookup, fake.garble_lookup = None, False
    caplog.clear()
    printer.extruded = 1400
    await bridge.poll()
    assert fake.lookups == [19] * 4
    recovered = [r for r in caplog.records if "look up" in r.getMessage()]
    assert [(r.levelname, r.getMessage()) for r in recovered] == [
        ("INFO", f"[{LOC}] can look up spool 19 again")
    ]
    # The same failure again is a new problem, so it's logged again.
    if failure == "http":
        fake.fail_lookup = 500
    else:
        fake.garble_lookup = True
    printer.extruded = 1500
    await bridge.poll()
    assert caplog.text.count("can't look up spool 19, which is missing") == 1
    await bridge.aclose()


async def test_failed_lookup_ends_when_the_spool_is_listed_again(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    fake.unlisted, fake.fail_lookup = {19}, 500
    printer.extruded = 1100
    await bridge.poll()
    assert caplog.text.count("can't look up spool 19") == 1
    fake.unlisted = set()  # Listed again: no lookup is needed, so none is made.
    printer.extruded = 1200
    await bridge.poll()
    assert fake.lookups == [19]
    assert levels(caplog, "spool 19 no longer needs looking up") == ["INFO"]
    await bridge.aclose()


async def test_first_model_check_is_logged_at_info(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    assert levels(caplog, "printer is COREONEL (8.1.0)") == ["INFO"]
    await bridge.aclose()


async def test_spool_missing_from_a_stale_list_is_looked_up_again_only_if_still_missing(
    tmp_path, caplog
):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    # The list was read just before spool 19 was unarchived, so it's missing but active.
    fake.unlisted = {19}
    for extruded in (1100, 1200):  # Missing again: nothing remembered it, so looked up again.
        printer.extruded = extruded
        await bridge.poll()
    assert fake.lookups == [19, 19]
    assert "archived" not in caplog.text and "deleted" not in caplog.text
    assert bridge._archived == {}
    # Charged without its filament data while it's missing.
    assert "spool 19 (unknown): +100 mm" in caplog.text
    charged = [r for r in ledger(tmp_path) if r.get("spool_id") == 19 and "use_length" in r]
    assert [r["grams"] for r in charged] == [None, None]
    # The next list has it, so it isn't looked up again.
    fake.unlisted = set()
    printer.extruded = 1300
    await bridge.poll()
    assert fake.lookups == [19, 19]
    assert fake.used == {19: [100.0, 100.0, 100.0]}
    assert "spool 19 is no longer in the" in caplog.text
    assert "spool 19 is back in the location" in caplog.text
    await bridge.aclose()


# Claims


CLAIMED = {"bridge_claim": "true"}


def claim_box(fake, spool_id):
    raw = fake.spools[spool_id].get("extra", {}).get("bridge_claim")
    return None if raw is None else json.loads(raw)


async def test_wrong_spool_loaded_then_claimed(tmp_path):
    # S5: unload, load a different spool, forget Spoolman, print, then claim it.
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    assert {
        "key": "bridge_claim",
        "name": "Claim held usage",
        "field_type": "boolean",
    } in fake.fields
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    printer.extruded = 1116  # menu load of spool 20, but Spoolman still has 19
    await bridge.poll()

    printer.job = 7
    await bridge.poll()
    printer.extruded = 1500  # first extrusion: the swap is assumed, provisionally
    await bridge.poll()
    assert bridge.printers[0].tools[0].provisional.mm == 500
    assert "500 mm" in fake.flag(19) and "Claim held usage" in fake.flag(19)
    printer.extruded = 3000
    await bridge.poll()
    printer.job = None
    await bridge.poll()
    assert "2000 mm" in fake.flag(19)

    # The user moves spool 20 into the location and ticks its claim box.
    fake.spools[20] = spool_x(20, color="00ff00", extra=dict(CLAIMED))
    uses_before = len([w for w in fake.writes if w[0] == "use"])
    await bridge.poll()
    uses = [w for w in fake.writes if w[0] == "use"][uses_before:]
    assert uses == [("use", 20, 2000.0), ("use", 19, -2000.0)]
    assert sum(fake.used[19]) == 0
    st = bridge.printers[0].tools[0]
    assert (st.active, st.provisional, st.swap) == (20, None, None)
    assert fake.spools[19]["location"] is None  # evicted
    assert claim_box(fake, 20) is False
    assert fake.flag(19) is None
    assert 'O"#00ff00"' in jobs(printer)[-1]
    reasons = [r["reason"] for r in ledger(tmp_path) if "reason" in r]
    assert "claimed usage since load" in reasons

    await bridge.poll()
    assert read_state(tmp_path / "bridge.db").claimed == []
    assert len([w for w in fake.writes if w[0] == "use"]) == uses_before + 2
    await bridge.aclose()


async def test_claim_outside_tool_location_is_flagged_until_moved_in(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    fake.spools[20] = spool_x(20, location="Shelf", extra=dict(CLAIMED))
    for _ in range(3):
        await bridge.poll()
    assert "isn't in a printer tool's location" in fake.flag(20)
    assert claim_box(fake, 20) is True
    assert bridge.printers[0].tools[0].active == 19
    assert caplog.text.count("spool 20 is claimed but isn't in a tool's location") == 1

    fake.spools[20]["location"] = LOC
    await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert claim_box(fake, 20) is False
    assert levels(caplog, "claimed spool 20 is no longer waiting on a location") == ["WARNING"]
    assert fake.flag(20) is None
    assert fake.spools[19]["location"] is None
    await bridge.aclose()


async def test_claim_outside_tool_location_logs_again_when_the_location_changes(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    fake.spools[20] = spool_x(20, location="Shelf", extra=dict(CLAIMED))
    await bridge.poll()
    await bridge.poll()
    fake.spools[20]["location"] = "Drawer"
    await bridge.poll()
    await bridge.poll()
    waiting = "spool 20 is claimed but isn't in a tool's location"
    assert levels(caplog, waiting) == ["WARNING", "WARNING"]
    assert "'Drawer'" in caplog.text
    await bridge.aclose()


async def test_several_claims_pick_the_most_recently_used(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer.extruded = 1300
    await bridge.poll()
    fake.spools[20] = spool_x(20, extra=dict(CLAIMED))  # staged, never used
    fake.spools[19]["extra"].update(CLAIMED)
    fake.spools[19]["last_used"] = "2026-11-06T10:00:00Z"
    await bridge.poll()
    # Spool 19 was used more recently, so its claim wins: it's confirmed, nothing moves.
    assert bridge.printers[0].tools[0].active == 19
    assert fake.used == {19: [300.0]}
    assert (claim_box(fake, 19), claim_box(fake, 20)) == (False, False)
    assert caplog.text.count("several spools claimed at once: chose spool 19") == 1
    assert "clearing the claims on spools 20" in caplog.text

    # Neither used: the highest ID wins.
    fake.spools[19]["last_used"] = None
    fake.spools[19]["extra"].update(CLAIMED)
    fake.spools[20]["extra"].update(CLAIMED)
    await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert caplog.text.count("several spools claimed at once: chose spool 20") == 1
    assert (claim_box(fake, 19), claim_box(fake, 20)) == (False, False)
    await bridge.aclose()


async def test_staged_spool_flagged_after_reboot(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    printer.boot_id, printer.extruded = 2, 0
    await bridge.poll()
    assert "printer restarted while spool 20 is staged" in fake.flag(20)
    assert "tick 'Claim held usage'" in fake.flag(20)
    await bridge.aclose()


async def test_claim_failing_to_clear_is_handled_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    fake.spools[20] = spool_x(20, extra=dict(CLAIMED))
    fake.fail_patch = 500
    for _ in range(3):
        await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert read_state(tmp_path / "bridge.db").claimed == [20]
    assert caplog.text.count("claimed in place of spool 19") == 1
    assert caplog.text.count("can't clear the claim on spool 20") == 1
    fake.fail_patch = None
    caplog.clear()
    await bridge.poll()
    assert claim_box(fake, 20) is False
    # The UUID mint failed with the claim, and its write waited for it (W1).
    assert lines(caplog, 20) == [
        ("INFO", "cleared the claim on spool 20"),
        ("INFO", f"gave spool 20 instance UUID {stored_uuid(fake, 20)}"),
    ]
    await bridge.poll()
    assert read_state(tmp_path / "bridge.db").claimed == []
    await bridge.aclose()


async def test_claim_unticked_without_a_failure_logs_nothing(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    fake.spools[20] = spool_x(20, extra=dict(CLAIMED))
    await bridge.poll()
    bridge.state.claimed.append(20)  # Handled, but Spoolman shows it clear.
    fake.spools[20]["extra"]["bridge_claim"] = "false"
    await bridge.poll()
    assert "no longer needs its claim cleared" not in caplog.text
    await bridge.aclose()


async def test_claim_failing_to_clear_ends_when_the_user_unticks_it(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    fake.spools[20] = spool_x(20, extra=dict(CLAIMED))
    fake.fail_patch = 500
    await bridge.poll()
    assert caplog.text.count("can't clear the claim on spool 20") == 1
    fake.fail_patch = None
    fake.spools[20]["extra"]["bridge_claim"] = "false"  # The user unticks it.
    for _ in range(2):
        await bridge.poll()
    assert read_state(tmp_path / "bridge.db").claimed == []
    assert levels(caplog, "spool 20 no longer needs its claim cleared") == ["INFO"]
    assert "cleared the claim on spool 20" not in caplog.text
    await bridge.aclose()


async def test_claim_cleared_in_spoolman_is_dropped(tmp_path, caplog):
    """The bridge can't clear a handled claim, then the user clears it: it's forgotten,
    so ticking it again is a new claim."""
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    fake.spools[20] = spool_x(20, extra=dict(CLAIMED))
    fake.fail_patch = 500
    await bridge.poll()
    assert read_state(tmp_path / "bridge.db").claimed == [20]
    fake.fail_patch = None
    fake.spools[20]["extra"]["bridge_claim"] = "false"  # Cleared by the user.
    await bridge.poll()
    assert read_state(tmp_path / "bridge.db").claimed == []
    assert "cleared the claim on spool 20" not in caplog.text

    fake.spools[20]["extra"].update(CLAIMED)
    await bridge.poll()
    assert caplog.text.count("spool 20 claimed: confirmed as loaded") == 1
    assert claim_box(fake, 20) is False
    assert caplog.text.count("cleared the claim on spool 20") == 1
    await bridge.aclose()


async def test_claim_takes_usage_held_for_a_deleted_spool(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    fake.spools[20] = spool_x(20)  # staged
    await bridge.poll()
    del fake.spools[19]
    printer.extruded = 1300
    await bridge.poll()
    st = bridge.printers[0].tools[0]
    assert (st.active, st.deleted_spool, st.held_mm) == (None, 19, 300)
    assert "usage is being held" in fake.flag(20)

    fake.spools[20]["extra"].update(CLAIMED)
    await bridge.poll()
    st = bridge.printers[0].tools[0]
    assert (st.active, st.deleted_spool, st.held_mm) == (20, None, 0)
    assert fake.used == {20: [300.0]}
    assert claim_box(fake, 20) is False
    assert fake.flag(20) is None
    await bridge.aclose()


async def test_claim_clears_the_runout_flag(tmp_path):
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    fake.spools[20] = spool(20)  # confirms the swap: spool 20 goes in, 19 is evicted
    await bridge.poll()
    assert fake.spools[19]["location"] is None
    assert "mid-print" in fake.flag(19)

    # Spool 19 was the one reloaded after all: move it back in and claim it.
    fake.spools[19]["location"] = LOC
    fake.spools[19]["extra"].update(CLAIMED)
    await bridge.poll()
    assert bridge.printers[0].tools[0].active == 19
    assert 19 not in read_state(tmp_path / "bridge.db").runouts
    assert fake.flag(19) is None or "mid-print" not in fake.flag(19)
    await bridge.aclose()


async def delete_during_unconfirmed_swap(tmp_path, queue_swap_charge):
    """Spool 19 unloads, the new filament is loaded and purged (charged to 19 under the
    unconfirmed swap), then 19 is deleted and spool 20 moved in."""
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded, printer.unloads, printer.at_unload = 1500, 1, 1500
    await bridge.poll()  # unconfirmed swap on 19
    printer.extruded = 1620  # load and purge
    if queue_swap_charge:
        fake.fail_use = [503]
    await bridge.poll()
    del fake.spools[19]
    fake.spools[20] = spool(20)
    printer.extruded = 1700
    await bridge.poll()
    await bridge.poll()
    return bridge, fake


async def test_deleting_swap_spool_holds_usage_since_unload(tmp_path):
    bridge, fake = await delete_during_unconfirmed_swap(tmp_path, queue_swap_charge=False)
    assert fake.used[19] == [500.0, 120.0]
    assert sum(fake.used[20]) == 200.0  # 120 since the unload, then 80
    await bridge.aclose()


async def test_deleting_swap_spool_with_queued_charge_holds_it_once(tmp_path):
    bridge, fake = await delete_during_unconfirmed_swap(tmp_path, queue_swap_charge=True)
    assert fake.used[19] == [500.0]
    assert sum(fake.used[20]) == 200.0
    await bridge.aclose()


async def test_404_for_a_spool_no_longer_active_drops_the_charge(tmp_path, caplog):
    """Spool 19's last charge is queued when the tool swaps to 20, then 19 is deleted:
    the usage was 19's, so it's dropped, not held for spool 20."""
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    fake.spools[20] = spool(20)
    printer.extruded, printer.unloads, printer.at_unload = 1500, 1, 1500
    fake.fail_use = [503]
    await bridge.poll()  # Swap to 20; the 500 mm for 19 stays queued.
    assert bridge.printers[0].tools[0].active == 20
    assert [r["spool_id"] for r in bridge.state.outbox] == [19]
    del fake.spools[19]
    printer.extruded = 1600
    await bridge.poll()
    assert fake.used == {20: [100.0]}
    assert "Spoolman rejected a +500 mm charge to spool 19 (404" in caplog.text
    st = read_state(tmp_path / "bridge.db").printers["default"].tools[0]
    assert (st.active, st.deleted_spool, st.held_mm) == (20, None, 0)
    failed = [r for r in ledger(tmp_path) if r.get("failed")]
    assert [(r["spool_id"], r["failed"], r.get("held")) for r in failed] == [(19, "404", None)]
    await bridge.aclose()


async def test_nothing_held_logs_no_held_charge(tmp_path, caplog):
    caplog.set_level("INFO")
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    del fake.spools[19]
    await bridge.poll()  # deleted while idle: nothing held
    fake.spools[20] = spool(20)
    await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert "charged it 0 mm" not in caplog.text
    assert not any(r.get("event") == "held_charged" for r in ledger(tmp_path))
    await bridge.aclose()


# Scenarios in docs/scenarios.md that the tests above don't cover


async def test_write_due_mid_print_waits_for_the_print_to_end(tmp_path):
    # W2, P2: the runout reload keeps spool 19's metadata, and Spoolman confirms the
    # swap to spool 20 during the pause. The write waits until the print ends.
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    fake.spools[19]["location"] = None
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    for _ in range(3):
        printer.extruded += 100
        await bridge.poll()
    assert jobs(printer) == []
    assert printer.refused == 0  # The bridge waits; it doesn't try and get 409.
    printer.job = None
    await bridge.poll()
    (gcode,) = jobs(printer)
    assert 'O"#ff0000"' in gcode
    await bridge.poll()
    assert len(jobs(printer)) == 1
    await bridge.aclose()


async def test_print_started_right_after_a_load_delays_the_write(tmp_path):
    # W4: the user loads spool 20 and starts a print before an idle poll sees the load.
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    fake.spools[20] = spool_x(20)  # staged
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, None
    await bridge.poll()
    printer.extruded, printer.material = 1116, {"name": "PLA"}  # menu load
    printer.job = 7
    await bridge.poll()
    for extruded in (1300, 1500):
        printer.extruded = extruded
        await bridge.poll()
    assert jobs(printer) == []  # The print runs with the menu's metadata.
    assert printer.refused == 0
    assert sum(fake.used[20]) == 500
    printer.job = None
    await bridge.poll()
    (gcode,) = jobs(printer)
    assert 'O"#ff0000"' in gcode
    await bridge.aclose()


UUID19 = "91da34d9-c1c1-470f-bac7-185a2346f86a"
UUID20 = "0b7d4c6e-5f2a-4b8e-9c3d-2e1f0a9b8c7d"


async def test_spoolman_change_to_the_reported_uuid_switches_at_once(tmp_path):
    # S12: no unload, but the printer already reports spool 20's UUID: a correction.
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    fake.spools[19]["extra"] = {"instance_uuid": json.dumps(UUID19)}
    printer.material = {**printer.material, "instance_uuid": UUID19}
    await bridge.poll()  # baseline: the printer matches spool 19
    printer.extruded = 1100
    await bridge.poll()
    printer.material = {**printer.material, "instance_uuid": UUID20}
    fake.spools[20] = spool(20)
    fake.spools[20]["extra"] = {"instance_uuid": json.dumps(UUID20)}
    await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert fake.spools[19]["location"] is None  # evicted, as for a swap
    printer.extruded = 1300
    await bridge.poll()
    assert fake.used == {19: [100.0], 20: [200.0]}
    assert jobs(printer) == []  # the printer already shows spool 20
    await bridge.aclose()


async def test_print_start_with_a_spool_staged_flags_both(tmp_path):
    # S9: spool 20 is staged and never unloaded into the tool.
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    assert fake.flag(19) is None and fake.flag(20) is None
    printer.job = 7
    await bridge.poll()
    printer.extruded += 100  # first extrusion: the print starts
    await bridge.poll()
    assert "spool 20 is staged but not loaded" in fake.flag(19)
    assert "spool 20 is staged but not loaded" in fake.flag(20)
    assert bridge.printers[0].tools[0].active == 19 and fake.used == {19: [100.0]}
    await bridge.aclose()


async def test_unload_before_a_jobs_first_extrusion_is_not_a_runout(tmp_path):
    # M5a: a print starts at its first extrusion (Q4), so this unload isn't mid-print.
    bridge, fake, printer = make(tmp_path, [weighted(spool(19), 400.0)])
    await bridge.poll()
    printer.job = 7  # preheating
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, 1000
    await bridge.poll()
    assert "mid-print" not in (fake.flag(19) or "")
    assert 19 not in bridge.state.runouts
    await bridge.aclose()


async def test_runout_of_a_spool_with_no_net_weight_is_not_flagged(tmp_path):
    # M5a: with no net weight in Spoolman, there's no threshold to compare against.
    unweighed = spool(19)
    unweighed["used_weight"] = 100.0
    bridge, fake, printer = make(tmp_path, [unweighed])
    printer.job = 131
    await bridge.poll()  # baseline mid-print
    printer.extruded = 1500
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, 1500  # runout
    await bridge.poll()
    await bridge.poll()
    assert "mid-print" not in (fake.flag(19) or "")
    assert 19 not in bridge.state.runouts
    await bridge.aclose()


@pytest.mark.parametrize("net", [1000.0, 2000.0, 3000.0])
@pytest.mark.parametrize("over", [True, False])
async def test_runout_threshold_scales_with_net_weight(tmp_path, net, over):
    # M5a: flagged with more than 5% of the net weight left.
    remaining = net * 0.05 + (1 if over else -1)
    bridge, fake, printer = make(tmp_path, [weighted(spool(19), remaining, net=net)])
    printer.job = 131
    await bridge.poll()  # baseline mid-print
    printer.unloads, printer.at_unload = 1, 1000  # runout, nothing charged since
    await bridge.poll()
    assert ("mid-print" in (fake.flag(19) or "")) is over
    assert (19 in bridge.state.runouts) is over
    await bridge.aclose()


async def test_spoolman_outage_while_printing_catches_up_once(tmp_path):
    # M1: accounting is skipped while Spoolman's spool list can't be read. The counter
    # isn't advanced, so the next poll charges everything since, once.
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()  # baseline at 1000
    printer.extruded = 1100
    await bridge.poll()
    fake.down = True
    for extruded in (1200, 1300, 1400):
        printer.extruded = extruded
        await bridge.poll()
    assert fake.used == {19: [100.0]}
    fake.down = False
    printer.extruded = 1500
    await bridge.poll()
    assert fake.used == {19: [100.0, 400.0]}
    assert bridge.printers[0].tools[0].charged == 1500
    await bridge.poll()
    assert fake.used == {19: [100.0, 400.0]}
    assert bridge.state.outbox == []
    await bridge.aclose()


async def test_claims_disabled(tmp_path):
    # Claims: claim_field = "" in the config turns them off, which loads as None.
    bridge, fake, printer = make(tmp_path, [spool(19)], claim_field=None)
    await bridge.poll()
    assert ("field", "/api/v1/field/spool/bridge_claim") not in fake.writes
    assert not [f for f in fake.fields if f["key"] == "bridge_claim"]
    fake.spools[20] = spool_x(20, extra=dict(CLAIMED))
    printer.extruded = 1100
    for _ in range(2):
        await bridge.poll()
    assert bridge.printers[0].tools[0].active == 19  # staged, not claimed
    assert claim_box(fake, 20) is True
    assert fake.used == {19: [100.0]}
    assert fake.spools[19]["location"] == LOC
    await bridge.aclose()


@pytest.mark.parametrize(
    "field, value, written",
    [
        ("material", "PETG", 'N"PETG"'),
        ("uuid", UUID20, f'M865.1 I0 C"{UUID20}"'),
    ],
)
async def test_spoolman_edit_is_written(tmp_path, field, value, written):
    # W14: an edit with no swap is written when idle, once.
    bridge, fake, printer = await written_state(tmp_path)
    if field == "uuid":
        fake.spools[20]["extra"]["instance_uuid"] = json.dumps(value)
    else:
        fake.spools[20]["filament"][field] = value
    await bridge.poll()
    assert len(jobs(printer)) == 2
    assert written in jobs(printer)[-1]
    await bridge.poll()
    assert len(jobs(printer)) == 2
    await bridge.aclose()


async def test_spoolman_name_edit_is_not_written(tmp_path):
    # W14: the printer's filament name is the material, so a name-only edit
    # changes nothing the printer holds and starts no job.
    bridge, fake, printer = await written_state(tmp_path)
    fake.spools[20]["filament"]["name"] = "Red"
    await bridge.poll()
    assert len(jobs(printer)) == 1
    await bridge.aclose()


async def test_spoolman_name_edit_is_written_when_the_filament_has_no_material(tmp_path):
    # W14: with no material, the printer's filament name is the Spoolman name.
    bridge, fake, printer = await written_state(tmp_path)
    filament = fake.spools[20]["filament"]
    filament.update(material=None, settings_extruder_temp=215, settings_bed_temp=60)
    await bridge.poll()
    assert len(jobs(printer)) == 2
    filament["name"] = "Red"
    await bridge.poll()
    assert len(jobs(printer)) == 3
    assert 'N"Red"' in jobs(printer)[-1]
    await bridge.aclose()


@pytest.mark.parametrize(
    "printer_type, preamble, logged",
    [
        ("1.4.0", COREONEL_PREAMBLE, "printer is MK4 (1.4.0): gcode_preamble is for COREONEL"),
        ("99.1.0", None, "printer is unknown (99.1.0): printer type '99.1.0' isn't one"),
    ],
)
async def test_model_problem_logged_once(tmp_path, caplog, printer_type, preamble, logged):
    # W10: logged once, however many polls; writes stay off and charging goes on.
    bridge, fake, printer = make(
        tmp_path, [spool(19)], uuid_field="instance_uuid", gcode_preamble=preamble
    )
    printer.printer_type = printer_type
    await swap_to_spool_20(bridge, fake, printer)
    for _ in range(3):
        printer.extruded += 100
        await bridge.poll()
    assert jobs(printer) == []
    assert caplog.text.count(logged) == 1
    assert caplog.text.count("] can't write spool 20 to the printer") == 1
    assert fake.writes.count(("patch", 20)) == 1
    assert fake.used[20] == [116.0, 100.0, 100.0, 100.0]
    await bridge.aclose()


async def test_records_name_the_printer(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    fake.fail_use = [503]
    await bridge.poll()
    queued = read_state(tmp_path / "bridge.db").outbox
    assert [(r["printer"], r["tool"], r["spool_id"]) for r in queued] == [("default", 0, 19)]
    await bridge.poll()
    records = ledger(tmp_path)
    assert records and all(r["printer"] == "default" for r in records)
    await bridge.aclose()


def two_printers(tmp_path, spools, **cfg):
    """A bridge with printers "a" and "b", each with one tool, against one fake Spoolman.

    Returns the bridge, the fake Spoolman and the two fake printers by name.
    """
    fake = FakeSpoolman(spools)
    printers = {"a": Printer(), "b": Printer()}
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
            transport=httpx.MockTransport(fake.handle),
        ),
        clients={
            name: PrusaLink(f"http://{name}", "k", transport=httpx.MockTransport(p.handle))
            for name, p in printers.items()
        },
    )
    return bridge, fake, printers


async def test_404_in_flush_holds_the_charge_for_the_records_printer(tmp_path, caplog):
    # X11: a rejected charge is held for the tool named by the record's printer.
    bridge, fake, _ = two_printers(tmp_path, [spool(21, "Tool a")])
    a, b = (bridge.state.tools(name) for name in ("a", "b"))
    a[0] = ToolState(boot_id=1, charged=100, active=21, assigned=21)
    b[0] = ToolState(boot_id=1, charged=700, active=19, assigned=19)
    record = {"tool": 0, "spool_id": 19, "extruded_mm": 600, "use_length": 600.0}
    bridge.state.outbox = [
        {**record, "printer": "b"},
        {**record, "printer": "nowhere", "extruded_mm": 50, "use_length": 50.0},
    ]
    assert await bridge.flush()
    assert (b[0].active, b[0].deleted_spool, b[0].held_mm) == (None, 19, 600)
    assert (a[0].active, a[0].deleted_spool, a[0].held_mm) == (21, None, 0)
    assert "[Tool b] spool 19 was deleted in Spoolman" in caplog.text
    # A record for a printer that isn't configured can't be held, so it's dropped.
    assert "Spoolman rejected a +50 mm charge to spool 19 (404" in caplog.text
    assert fake.used == {}
    await bridge.aclose()


TAG_K = "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"


def tagged(uuid=TAG_K):
    """PLA loaded from a tag: the firmware reports its material and package UUIDs."""
    return {
        "name": "PLA",
        "color": "#73737b",
        "instance_uuid": uuid,
        "material_uuid": "0b1c6b8e-8d0f-4f8e-9a63-2b1f0d6c7e11",
        "package_uuid": "5d6e7f80-1a2b-4c3d-8e9f-0a1b2c3d4e5f",
    }


async def start_print(bridge, printer):
    """Start a print the bridge sees start: a new job, then extrusion in it."""
    printer.job = 7
    await bridge.poll()
    printer.extruded += 100
    await bridge.poll()


def stored_uuid(fake, spool_id):
    raw = fake.spools[spool_id]["extra"].get("instance_uuid")
    return json.loads(raw) if raw else None


@pytest.mark.parametrize("before", [None, {"name": "PLA", "color": "#73737b"}])
async def test_watched_tag_load_is_adopted(tmp_path, before):
    """An empty tool or a menu type, then a tag: the bridge saw it appear and adopts it."""
    bridge, fake, printer = make(tmp_path, [spool(20)], uuid_field="instance_uuid")
    printer.material = before
    await bridge.poll()  # baseline
    printer.material = tagged()
    await bridge.poll()
    assert stored_uuid(fake, 20) == TAG_K
    await bridge.poll()
    assert jobs(printer) == []
    await bridge.aclose()


async def test_tag_loaded_before_start_is_kept_not_adopted(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(20)], uuid_field="instance_uuid")
    printer.material = tagged()
    for _ in range(2):
        await bridge.poll()
    assert stored_uuid(fake, 20) is None
    assert jobs(printer) == []
    await bridge.aclose()


async def test_old_spools_tag_after_a_restart_is_not_adopted(tmp_path):
    """Spool 19's tag comes back after a restart with spool 20 in the tool: 20 is minted."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    printer.material = tagged()  # 19's tag, loaded before the bridge started.
    await bridge.poll()
    assert stored_uuid(fake, 19) is None

    # Spoolman first: 20 in, then a runout mid-print, so 19 goes out at the unload. The
    # tool reads empty until the reload.
    fake.spools[20] = spool(20)
    await bridge.poll()
    await start_print(bridge, printer)
    printer.unloads, printer.at_unload, printer.material = 1, printer.extruded, None
    await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20

    # The bridge restarts, then the printer shows 19's tag again, as a runout reload does.
    # The clients are reused: the restarted bridge closes them.
    restarted = restart(bridge)
    await restarted.poll()
    printer.material = tagged()
    await restarted.poll()
    printer.job = None  # The print ends; the write runs once the printer is idle.
    await restarted.poll()
    await restarted.poll()
    minted = stored_uuid(fake, 20)
    assert minted is not None and minted != TAG_K
    assert printer.material["instance_uuid"] == minted
    await restarted.aclose()


def other_tag(uuid=TAG_K):
    """A tag whose name, color and temperatures all differ from spool 20's in Spoolman."""
    return {
        **tagged(uuid),
        "name": "PETG",
        "color": "#ff0000",
        "nozzle_temperature": 250,
        "bed_temperature": 80,
    }


@pytest.mark.parametrize("sync_drift", [False, True])
async def test_tag_with_other_values_is_not_written(tmp_path, caplog, sync_drift):
    """A tool loaded from a tag is left alone, whatever Spoolman says (Q11)."""
    bridge, fake, printer = make(
        tmp_path, [spool(20)], uuid_field="instance_uuid", sync_drift=sync_drift
    )
    await bridge.poll()  # baseline
    st = bridge.printers[0].tools[0]
    bridge.printers[0].tools[0] = replace(st, needs_sync=True, sync_attempts=1)
    printer.material = other_tag()
    for _ in range(3):
        await bridge.poll()
    assert jobs(printer) == []
    assert stored_uuid(fake, 20) == TAG_K  # Still adopted.
    st = bridge.printers[0].tools[0]
    assert (st.needs_sync, st.sync_attempts) == (False, 0)
    assert not fake.flag(20)
    assert "differs" not in caplog.text
    await bridge.aclose()


@pytest.mark.parametrize("from_tag", [False, True])
async def test_unconfirmed_swap_writes_back_only_over_a_menu_load(tmp_path, from_tag):
    """W11 writes the assigned spool over a menu load, but not over a newly loaded tag."""
    bridge, fake, printer = make(tmp_path, [spool(20)], uuid_field="instance_uuid")
    await bridge.poll()  # baseline
    printer.unloads, printer.at_unload, printer.material = 1, printer.extruded, None
    await bridge.poll()
    assert bridge.printers[0].tools[0].swap is not None
    printer.material = other_tag() if from_tag else {"name": "PETG"}
    for _ in range(2):
        await bridge.poll()
    assert len(jobs(printer)) == (0 if from_tag else 1)
    assert (stored_uuid(fake, 20) is None) is from_tag  # A write-back mints a UUID.
    await bridge.aclose()


async def test_tag_of_another_spool_is_drift_not_a_write(tmp_path, caplog):
    """The loaded tag's UUID is spool 21's in Spoolman: reported once, never written."""
    other = spool(21, location="Shelf")
    other["extra"]["instance_uuid"] = json.dumps(TAG_K)
    bridge, fake, printer = make(tmp_path, [spool(20), other], uuid_field="instance_uuid")
    await bridge.poll()  # baseline
    printer.material = other_tag()
    for _ in range(3):
        await bridge.poll()
    assert jobs(printer) == []
    detail = f"the loaded tag is spool 21's ({TAG_K})"
    assert caplog.text.count(f"printer differs from spool 20: {detail}") == 1
    assert fake.flag(20) == f"{LOC}: printer filament differs: {detail}"
    assert stored_uuid(fake, 20) is None
    await bridge.aclose()


def with_uuid(data, value):
    data["extra"]["instance_uuid"] = json.dumps(value)
    return data


async def assert_flag_kept_while_ambiguous(bridge, fake, spool_id):
    """Make the location ambiguous (S11) and back: the spool's flag stays, unwritten."""
    flag = fake.flag(spool_id)
    patches = fake.writes.count(("patch", spool_id))
    for other in (30, 31):
        fake.spools[other] = spool(other)
    for _ in range(2):
        await bridge.poll()
    assert 0 in bridge.printers[0].tools and fake.flag(spool_id) == flag
    for other in (30, 31):
        fake.spools[other]["location"] = "Shelf"
    await bridge.poll()
    assert fake.flag(spool_id) == flag
    assert fake.writes.count(("patch", spool_id)) == patches


async def test_a_skipped_tool_keeps_its_tagged_drift_flag(tmp_path):
    """A tag that's another spool's is drift (Q11); its flag stands through an S11 skip."""
    other = spool(21, location="Shelf")
    other["extra"]["instance_uuid"] = json.dumps(TAG_K)
    bridge, fake, printer = make(tmp_path, [spool(20), other], uuid_field="instance_uuid")
    await bridge.poll()  # baseline
    printer.material = other_tag()
    await bridge.poll()
    assert (
        fake.flag(20) == f"{LOC}: printer filament differs: the loaded tag is spool 21's ({TAG_K})"
    )
    await assert_flag_kept_while_ambiguous(bridge, fake, 20)
    await bridge.aclose()


async def test_a_skipped_tool_keeps_its_uuid_conflict_flag(tmp_path):
    """A tag whose UUID differs from the spool's; the conflict flag stands through an S11 skip."""
    bridge, fake, printer = make(
        tmp_path, [with_uuid(spool(19), "1f0e7d2a-3f6c-4d7b-9a43-6c1f0e7d2a11")]
    )
    await bridge.poll()  # baseline: 19, with no UUID on the printer
    printer.boot_id, printer.extruded, printer.material = 2, 0, other_tag()
    await bridge.poll()
    assert "the loaded tag's instance UUID is" in fake.flag(19)
    await assert_flag_kept_while_ambiguous(bridge, fake, 19)
    await bridge.aclose()


async def test_claim_of_the_loaded_tags_spool_writes_nothing(tmp_path):
    """Spool 20's tag is loaded while Spoolman has 19, then 20 is claimed: not a leftover."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    printer.material = other_tag()
    await bridge.poll()  # baseline: the tag is seen with 19
    fake.spools[20] = spool(20)
    fake.spools[20]["extra"].update(CLAIMED)
    for _ in range(3):
        await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert jobs(printer) == []
    await bridge.aclose()


async def test_correction_to_the_loaded_tags_spool_writes_nothing(tmp_path):
    """S12: the user gives spool 20 the loaded tag's UUID and moves it in: nothing written."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    printer.material = other_tag()
    await bridge.poll()  # baseline: the tag is seen with 19
    fake.spools[19]["location"] = "Shelf"
    fake.spools[20] = with_uuid(spool(20), TAG_K)
    for _ in range(3):
        await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert jobs(printer) == []
    await bridge.aclose()


async def test_claim_after_an_unload_missed_across_a_reboot_writes_nothing(tmp_path):
    """20's tag goes in while the printer is off: a conflict on 19, then 20 is claimed."""
    bridge, fake, printer = make(
        tmp_path, [with_uuid(spool(19), "1f0e7d2a-3f6c-4d7b-9a43-6c1f0e7d2a11")]
    )
    await bridge.poll()  # baseline: 19, with no UUID on the printer
    printer.boot_id, printer.extruded, printer.material = 2, 0, other_tag()
    await bridge.poll()
    assert "the loaded tag's instance UUID is" in fake.flag(19)
    fake.spools[20] = spool(20)
    fake.spools[20]["extra"].update(CLAIMED)
    for _ in range(3):
        await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert jobs(printer) == []
    await bridge.aclose()


async def test_runout_leftover_then_a_claim_is_written_over(tmp_path):
    """A runout reload keeps 19's tag; claiming 20 then writes 20 over it."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    printer.material = tagged()
    await bridge.poll()  # baseline: 19's tag, seen with 19
    printer.job = 7
    await bridge.poll()
    printer.extruded += 100  # the print starts
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, printer.extruded  # runout, then the reload
    await bridge.poll()
    printer.extruded += 50
    await bridge.poll()
    fake.spools[20] = spool(20)
    fake.spools[20]["extra"].update(CLAIMED)
    await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    printer.job = None
    await bridge.poll()
    assert len(jobs(printer)) == 1
    minted = stored_uuid(fake, 20)
    assert minted not in (None, TAG_K)
    assert printer.material["instance_uuid"] == minted
    await bridge.aclose()


async def test_tag_is_not_written_with_uuids_off(tmp_path, caplog):
    """With UUIDs off, a new tag after a swap is still left alone, and isn't drift."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field=None)
    await bridge.poll()  # baseline
    fake.spools[20] = spool(20)
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, printer.extruded, None
    await bridge.poll()
    printer.material = other_tag()
    for _ in range(3):
        await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert jobs(printer) == []
    assert not fake.flag(20)
    assert "differs" not in caplog.text
    await bridge.aclose()


async def test_runout_leftover_is_written_over_with_uuids_off(tmp_path):
    """With UUIDs off, a reload that keeps 19's tag with 20 active is still written over."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field=None)
    printer.material = tagged()
    await bridge.poll()  # baseline: 19's tag, seen with 19
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    await start_print(bridge, printer)
    printer.unloads, printer.at_unload = 1, printer.extruded  # runout; the reload keeps it
    await bridge.poll()
    printer.job = None
    for _ in range(2):
        await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert len(jobs(printer)) == 1 and "M865.1" not in jobs(printer)[0]
    assert "material_uuid" not in printer.material
    await bridge.aclose()


async def test_idle_reload_of_the_tag_after_a_spoolman_first_swap_writes_nothing(tmp_path):
    """20's tag is loaded while Spoolman has 19. S1: 20 moved in, unloaded, reloaded from
    its tag. An unload outside a print never keeps the type, so it's not a leftover."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    printer.material = other_tag()
    await bridge.poll()  # baseline: the tag is seen with 19
    fake.spools[20] = spool(20)
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, printer.extruded, None
    await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    printer.material = other_tag()
    for _ in range(3):
        await bridge.poll()
    assert jobs(printer) == []
    assert stored_uuid(fake, 20) is None  # Whose tag it is stays unknown: not adopted.
    await bridge.aclose()


async def test_idle_reload_of_the_tag_then_a_claim_writes_nothing(tmp_path):
    """20's tag is loaded while Spoolman has 19, unloaded and reloaded with no Spoolman
    change, then 20 is claimed: not a leftover, so nothing is written."""
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    printer.material = other_tag()
    await bridge.poll()  # baseline: the tag is seen with 19
    printer.unloads, printer.at_unload, printer.material = 1, printer.extruded, None
    await bridge.poll()
    printer.material = other_tag()
    await bridge.poll()
    assert bridge.printers[0].tools[0].swap is not None
    fake.spools[20] = spool(20)
    fake.spools[20]["extra"].update(CLAIMED)
    for _ in range(3):
        await bridge.poll()
    assert bridge.printers[0].tools[0].active == 20
    assert jobs(printer) == []
    assert stored_uuid(fake, 20) is None
    await bridge.aclose()


async def test_write_keeps_the_written_uuid_for_a_spool_whose_uuid_was_cleared(tmp_path):
    """Spoolman's UUID for spool 20 is cleared and the spool edited: the write keeps the
    UUID the printer already has from the last write, rather than minting another."""
    bridge, fake, printer = await written_state(tmp_path)
    minted = stored_uuid(fake, 20)
    assert printer.material["instance_uuid"] == minted
    del fake.spools[20]["extra"]["instance_uuid"]
    fake.spools[20]["filament"]["color_hex"] = "00ff00"
    await bridge.poll()
    assert len(jobs(printer)) == 2
    assert f'C"{minted}"' in jobs(printer)[-1] and 'O"#00ff00"' in jobs(printer)[-1]
    assert stored_uuid(fake, 20) is None  # Kept on the printer, not stored.
    await bridge.poll()
    assert len(jobs(printer)) == 2
    await bridge.aclose()
