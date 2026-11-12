"""The unit suite's bridge harness: a fake Spoolman, a fake printer, and a bridge on both."""

import json
import math
import re
from dataclasses import replace

import httpx

from spoolman_prusalink.app import Bridge
from spoolman_prusalink.color import quantize
from spoolman_prusalink.config import Config, PrinterConfig
from spoolman_prusalink.db import Database, read_ledger
from spoolman_prusalink.printer_sync import PRESETS
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


PRINTER_KEYS = {"gcode_preamble", "sync_storage", "sync_file", "uuids"}


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


async def swap_to_spool_20(bridge, fake, printer):
    """Baseline on spool 19, then a Spoolman-first swap to spool 20 and a menu load."""
    await bridge.poll()
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, None
    await bridge.poll()
    printer.extruded, printer.material = 1116, {"name": "PLA"}
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
