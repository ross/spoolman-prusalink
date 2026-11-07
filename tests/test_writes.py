"""Phase 2: charges and flags written to a fake Spoolman."""

import json
import math
from dataclasses import replace

import httpx
import pytest

from spoolman_prusalink.accounting import UNKNOWN_WRITE
from spoolman_prusalink.app import Bridge
from spoolman_prusalink.color import quantize
from spoolman_prusalink.config import Config
from spoolman_prusalink.printer_sync import COREONEL_PREAMBLE
from spoolman_prusalink.prusalink import PrusaLink
from spoolman_prusalink.spoolman import Spoolman
from spoolman_prusalink.state import Runout, load_state

LOC = "Core One L+"


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
        self.fail_field = None  # Status code for field reads.
        self.lookups = []  # Spool ids read one at a time.

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if self.down:
            raise httpx.ConnectError("spoolman offline")
        if path == "/api/v1/spool" and method == "GET":
            if self.fail_read:
                return httpx.Response(self.fail_read, text="oops")
            return httpx.Response(
                200, json=[s for s in self.spools.values() if not s.get("archived")])
        if path.startswith("/api/v1/spool/") and method == "GET":
            spool_id = int(path.rsplit("/", 1)[1])
            self.lookups.append(spool_id)
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
            body = json.loads(request.content)
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
    return {"id": id, "location": location, "extra": extra,
            "filament": {"id": 1, "name": "Grey", "material": "PLA", "color_hex": "73767d",
                         "diameter": 1.75, "density": 1.24}}


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
        self.printer_type = "8.1.0"  # Core One L
        self.boot_id = 1
        self.filament_endpoint = True
        self.error = None  # Status code for every request, such as 401.
        self.upload_error = None  # Status code for sync job uploads.
        self.counters = True  # MINI and MK3.5 firmware report only the material.

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
            tool = {"index": 0, "extruded": self.extruded, "unload_count": self.unloads,
                    "extruded_at_unload": self.at_unload}
            if not self.counters:
                tool = {"index": 0}
                if self.material:
                    tool["material"] = self.material
                return httpx.Response(200, json={"tools": [tool]})
            if self.material:
                tool["material"] = self.material
            return httpx.Response(200, json={"boot_id": self.boot_id, "tools": [tool]})
        if request.url.path == "/api/v1/job":
            if not self.job:
                return httpx.Response(204)
            return httpx.Response(200, json={"id": self.job,
                                             "file": {"display_name": self.job_name}})
        if request.method == "PUT" and request.url.path.startswith("/api/v1/files/"):
            if self.busy or self.job:
                return httpx.Response(409)
            if self.upload_error:
                return httpx.Response(self.upload_error, text="oops")
            assert request.headers["Print-After-Upload"] == "?1"
            gcode = request.content.decode()
            self.uploads.append((request.url.path, gcode))
            self.run_job(gcode)
            return httpx.Response(201)
        if request.url.path == "/api/v1/status":
            data = {"printer": {"state": "PRINTING" if self.job else "IDLE"}}
            if self.job:
                data["job"] = {"id": self.job}
            return httpx.Response(200, json=data)
        raise AssertionError(f"unexpected printer request {request.method} {request.url}")


    def run_job(self, gcode):
        """Apply the job's M865 lines, as the firmware would, and finish at once."""
        for line in gcode.splitlines():
            if line.startswith("M865 X"):
                name = line.split('N"')[1].split('"')[0]
                material = {"name": name}
                if 'O"' in line:
                    material["color"] = quantize("#" + line.split('O"#')[1][:6])
                self.material = material
            if line.startswith("M865.1"):
                self.material = {**self.material, "instance_uuid": line.split('C"')[1][:36]}


def make(tmp_path, spools, **cfg):
    fake, printer = FakeSpoolman(spools), Printer()
    config = Config(
        printer_url="http://printer", printer_api_key="k", spoolman_url="http://spoolman",
        tools={0: LOC}, state_file=tmp_path / "state.json",
        ledger_file=tmp_path / "ledger.jsonl", dry_run=False, **cfg,
    )
    bridge = Bridge(
        config=config,
        printer=PrusaLink("http://printer", "k", transport=httpx.MockTransport(printer.handle)),
        spoolman=Spoolman("http://spoolman", uuid_field=config.uuid_field,
                          flag_field=config.flag_field, claim_field=config.claim_field,
                          transport=httpx.MockTransport(fake.handle)),
    )
    return bridge, fake, printer


def ledger(tmp_path):
    return [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()]


async def test_charges_are_applied_and_recorded(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()  # baseline
    printer.extruded = 1500
    await bridge.poll()
    assert fake.used == {19: [500.0]}
    applied = [r for r in ledger(tmp_path) if "use_length" in r]
    assert len(applied) == 1 and applied[0]["applied"] and applied[0]["dry_run"] is False
    assert load_state(tmp_path / "state.json").outbox == []
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
    assert [r["use_length"] for r in load_state(tmp_path / "state.json").outbox] == [500.0]

    printer.extruded = 1600
    await bridge.poll()
    assert fake.used == {19: [500.0, 100.0]}
    assert load_state(tmp_path / "state.json").outbox == []
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
    restarted.state = load_state(tmp_path / "state.json")
    restarted.printer = PrusaLink("http://printer", "k",
                                  transport=httpx.MockTransport(printer.handle))
    printer.down = True
    await restarted.poll()
    assert fake.used == {19: [500.0]}
    await restarted.aclose()


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


async def test_unconfirmed_swap_flag_is_set_then_cleared(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    assert ("field", "/api/v1/field/spool/bridge_flag") in fake.writes
    printer.unloads, printer.at_unload = 1, 1000
    await bridge.poll()
    assert "unloaded with no Spoolman change" in fake.flag(19)
    assert load_state(tmp_path / "state.json").flagged == [19]

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
    assert bridge.tools[0].provisional is None
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
    s["filament"] = {**s["filament"], "material": material, "color_hex": color,
                     "settings_extruder_temp": temps[0], "settings_bed_temp": temps[1]}
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
    assert gcode.rstrip().endswith("M117 Spoolman: Grey")
    assert printer.uploads[0][0] == "/api/v1/files/usb/spoolman-sync.gcode"

    await bridge.poll()  # printer now matches: no second job, no flag
    assert len(jobs(printer)) == 1
    assert bridge.tools[0].needs_sync is False
    assert fake.flag(20) is None
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
    assert bridge.tools[0].swap is not None
    printer.job, printer.job_name = 51, "part.bgcode"
    await bridge.poll()
    printer.extruded += 50
    await bridge.poll()
    assert bridge.tools[0].swap is None
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
    assert jobs(printer) == [] and bridge.tools[0].sync_attempts == 0
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
    assert caplog.text.count("can't write spool 20 to the printer") == 1
    assert "printer will be set to spool 20" not in caplog.text

    fake.spools[20] = spool_x(20, material="PLA-CF", temps=(220, 60))
    await bridge.poll()
    (gcode,) = jobs(printer)
    assert 'M865 X R N"PLA-CF" T220 B60 O"#ff0000" L0' in gcode
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


async def test_printer_notices_show_each_flag_once(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)], printer_notices=True)
    await bridge.poll()
    printer.unloads, printer.at_unload = 1, 1000  # unconfirmed swap, printer idle
    await bridge.poll()
    await bridge.poll()
    (gcode,) = jobs(printer)
    assert "M117 Spoolman: Core One L+: filament was unl" in gcode
    assert "M865" not in gcode
    await bridge.aclose()


async def test_dry_run_never_uploads(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)], sync_drift=True)
    bridge.config = __import__("dataclasses").replace(bridge.config, dry_run=True)
    printer.material = {"name": "PLA"}
    caplog.set_level("INFO")
    await bridge.poll()
    assert jobs(printer) == [] and "WOULD RUN printer job" in caplog.text
    assert not [w for w in fake.writes if w[0] != "field"]
    await bridge.aclose()


async def test_dry_run_logs_a_pending_job_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    bridge.config = replace(bridge.config, dry_run=True)
    caplog.set_level("INFO")
    await bridge.poll()
    fake.spools[20] = spool_x(20)  # S1: staged, then unload and load.
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    fake.spools[19]["location"] = None  # Dry run doesn't evict it.
    for _ in range(4):
        await bridge.poll()
    assert jobs(printer) == []
    assert caplog.text.count("WOULD RUN printer job") == 1
    assert caplog.text.count("would give spool 20 instance UUID") == 1

    # A different job is logged again.
    fake.spools[20] = spool_x(20, color="00ff00")
    await bridge.poll()
    await bridge.poll()
    assert caplog.text.count("WOULD RUN printer job") == 2
    assert caplog.text.count("would give spool 20 instance UUID") == 1
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
    assert bridge.tools[0].swap is not None  # Writing doesn't confirm the swap.
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
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid",
                                 gcode_preamble=COREONEL_PREAMBLE)
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
    assert 0 not in bridge.tools
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
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid",
                                 gcode_preamble=preamble)
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

    bridge.printer = PrusaLink("http://printer", "k", transport=httpx.MockTransport(counting))
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
    assert "printer read failed" not in caplog.text
    assert 0 not in bridge.tools

    printer.filament_endpoint = True
    caplog.set_level("INFO")
    await bridge.poll()
    assert "printer now reports filament; resuming" in caplog.text
    assert 0 in bridge.tools
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
    assert len(jobs(printer)) == 1 and bridge.tools[0].needs_sync is False
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
    assert caplog.text.count("printer reads working again") == 1
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
    assert caplog.text.count("Spoolman reachable again") == 1
    assert caplog.text.count("Spoolman read failed") == 1
    fake.fail_read = None
    for _ in range(2):
        await bridge.poll()
    assert caplog.text.count("Spoolman reads working again") == 1
    await bridge.aclose()


async def test_dry_run_holds_queued_charges(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    fake.fail_use = [503]
    await bridge.poll()
    assert len(bridge.state.outbox) == 1

    # Restarted in dry run: the queued charge is neither sent nor dropped.
    bridge.config = replace(bridge.config, dry_run=True)
    caplog.set_level("INFO")
    for _ in range(3):
        await bridge.poll()
    assert fake.used == {}
    assert len(load_state(tmp_path / "state.json").outbox) == 1
    assert caplog.text.count("1 queued charge(s) held until dry run is off") == 1

    bridge.config = replace(bridge.config, dry_run=False)
    await bridge.poll()
    assert fake.used == {19: [500.0]} and bridge.state.outbox == []
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
    await bridge.poll()
    assert fake.spools[19]["location"] is None
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
    assert (bridge.tools[0].active, bridge.tools[0].evict) == (21, (19, 20))
    fake.fail_patch = None
    await bridge.poll()
    assert fake.spools[19]["location"] is None and fake.spools[20]["location"] is None
    assert bridge.tools[0].evict == ()
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
    await bridge.poll()
    assert "unloaded with no Spoolman change" in fake.flag(19)
    assert caplog.text.count("can't update the flag on spool 19") == 1
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
@pytest.mark.parametrize("reason", ["drift", "edited", "unknown_write", "needs_sync"])
async def test_emptied_location_reports_only_real_drift(tmp_path, caplog, reason, sync_drift):
    bridge, fake, printer = await written_state(tmp_path, sync_drift=sync_drift)
    caplog.set_level("INFO")
    fake.spools[20]["location"] = None  # Moved out, with nothing in its place.
    if reason == "drift":
        printer.material = {"name": "PETG"}
    elif reason == "edited":
        fake.spools[20]["filament"]["color_hex"] = "00ff00"
    elif reason == "unknown_write":
        bridge.tools[0] = replace(bridge.tools[0], written=UNKNOWN_WRITE)
    else:
        bridge.tools[0] = replace(bridge.tools[0], needs_sync=True)
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


async def test_printer_change_after_write_is_drift(tmp_path):
    bridge, fake, printer = await written_state(tmp_path, flag_drift=True)
    printer.material = {"name": "PETG"}  # changed on the printer, not in Spoolman
    await bridge.poll()
    assert len(jobs(printer)) == 1
    assert "printer filament differs" in fake.flag(20)
    await bridge.aclose()


async def test_state_from_before_write_tracking_is_rewritten_once(tmp_path):
    uuid19 = "91da34d9-c1c1-470f-bac7-185a2346f86a"
    bridge, fake, printer = make(tmp_path, [spool(20)], uuid_field="instance_uuid")
    printer.material = {**printer.material, "instance_uuid": uuid19}  # left by a runout
    await bridge.poll()  # baseline
    # Simulate state saved by an older bridge: no "written" key.
    state = json.loads((tmp_path / "state.json").read_text())
    del state["tools"]["0"]["written"]
    (tmp_path / "state.json").write_text(json.dumps(state))
    bridge.state = load_state(tmp_path / "state.json")

    await bridge.poll()
    (gcode,) = jobs(printer)
    minted = json.loads(fake.spools[20]["extra"]["instance_uuid"])
    assert " T215 B60 " in gcode and f'C"{minted}"' in gcode
    await bridge.poll()
    assert len(jobs(printer)) == 1
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
    assert 19 in load_state(tmp_path / "state.json").runouts
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
    assert 19 not in load_state(tmp_path / "state.json").runouts
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
    assert 19 not in load_state(tmp_path / "state.json").runouts
    await bridge.aclose()


async def test_runout_flag_from_older_state_learns_the_weights(tmp_path):
    bridge, fake, printer = await runout_mid_print(tmp_path, remaining=400.0)
    bridge.state.runouts[19] = Runout(bridge.state.runouts[19].text)  # As loaded from v1.
    await bridge.poll()
    assert 19 in bridge.state.runouts  # Learns the weights, doesn't read it as an edit.
    fake.spools[19]["used_weight"] = 990.0
    await bridge.poll()
    assert 19 not in bridge.state.runouts
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
    await bridge.aclose()


# Deleted and archived active spools (M3, M3a, Q7)


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
    st = load_state(tmp_path / "state.json").tools[0]
    assert (st.active, st.deleted_spool, st.held_mm) == (None, 19, 400)
    assert caplog.text.count("spool 19 was deleted in Spoolman") == 1
    assert caplog.text.count("holding usage since spool 19 was deleted") == 1
    assert "rejected" not in caplog.text

    fake.spools[20] = spool(20)  # The user re-creates the spool and moves it in.
    await bridge.poll()
    await bridge.poll()
    assert fake.used == {19: [500.0], 20: [400.0]}
    assert [r["reason"] for r in ledger(tmp_path) if r.get("spool_id") == 20] == [
        "held after spool 19 was deleted"]
    assert caplog.text.count("held after spool 19 was deleted") == 2  # Event and charge.
    st = load_state(tmp_path / "state.json").tools[0]
    assert (st.active, st.deleted_spool, st.held_mm) == (20, None, 0)
    await bridge.aclose()


async def test_dry_run_holds_nothing_for_a_deleted_spool(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    printer.extruded = 1500
    await bridge.poll()
    assert fake.used == {19: [500.0]}

    # Restarted in dry run, the spool is deleted and printing goes on.
    bridge.config = replace(bridge.config, dry_run=True)
    caplog.set_level("INFO")
    del fake.spools[19]
    for extruded in (1700, 1800, 1900):
        printer.extruded = extruded
        await bridge.poll()
    assert fake.lookups == [19]
    assert caplog.text.count("would hold its usage") == 1
    st = load_state(tmp_path / "state.json").tools[0]
    assert (st.deleted_spool, st.held_mm) == (None, 0)

    # Live again, with a new spool moved in: the dry-run usage isn't charged to it.
    bridge.config = replace(bridge.config, dry_run=False)
    await bridge.poll()
    fake.spools[20] = spool(20)
    await bridge.poll()
    await bridge.poll()
    assert fake.used == {19: [500.0]}
    st = load_state(tmp_path / "state.json").tools[0]
    assert (st.active, st.held_mm) == (20, 0)
    await bridge.aclose()


def restart(bridge, **cfg):
    """A new bridge on the same printer, Spoolman and state file."""
    config = replace(bridge.config, **cfg)
    return Bridge(config=config, printer=bridge.printer, spoolman=bridge.spoolman,
                  state=load_state(config.state_file))


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
    st = load_state(tmp_path / "state.json").tools[0]
    assert (st.deleted_spool, st.held_mm) == (19, 400)
    return bridge, fake, printer


async def test_dry_run_adds_nothing_to_usage_held_live(tmp_path):
    bridge, fake, printer = await held_live_for_deleted_spool(tmp_path)
    bridge = restart(bridge, dry_run=True)
    for extruded in (2000, 2100):  # 200 mm in dry run.
        printer.extruded = extruded
        await bridge.poll()
    st = load_state(tmp_path / "state.json").tools[0]
    assert (st.deleted_spool, st.held_mm) == (19, 400)

    bridge = restart(bridge, dry_run=False)
    fake.spools[20] = spool(20)
    await bridge.poll()
    await bridge.poll()
    assert fake.used == {19: [500.0], 20: [400.0]}
    await bridge.aclose()


async def test_dry_run_keeps_usage_held_live(tmp_path, caplog):
    bridge, fake, printer = await held_live_for_deleted_spool(tmp_path)
    bridge = restart(bridge, dry_run=True)
    caplog.set_level("INFO")
    fake.spools[20] = spool(20)  # Moved in during dry run.
    for _ in range(3):
        await bridge.poll()
    assert caplog.text.count("WOULD CHARGE spool 20") == 1
    st = load_state(tmp_path / "state.json").tools[0]
    assert (st.deleted_spool, st.held_mm) == (19, 400)

    bridge = restart(bridge, dry_run=False)
    await bridge.poll()
    await bridge.poll()
    assert fake.used == {19: [500.0], 20: [400.0]}
    await bridge.aclose()


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
    st = load_state(tmp_path / "state.json").tools[0]
    assert (st.active, st.deleted_spool, st.held_mm) == (None, 19, 600)
    held = [r for r in ledger(tmp_path) if r.get("held")]
    assert [(r["spool_id"], r["failed"]) for r in held] == [(19, "404")]
    assert caplog.text.count("spool 19 was deleted in Spoolman") == 1
    assert "dropping it" not in caplog.text

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


# Claims


CLAIMED = {"bridge_claim": "true"}


def claim_box(fake, spool_id):
    raw = fake.spools[spool_id].get("extra", {}).get("bridge_claim")
    return None if raw is None else json.loads(raw)


async def test_wrong_spool_loaded_then_claimed(tmp_path):
    # S5: unload, load a different spool, forget Spoolman, print, then claim it.
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    assert {"key": "bridge_claim", "name": "Claim held usage",
            "field_type": "boolean"} in fake.fields
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    printer.extruded = 1116  # menu load of spool 20, but Spoolman still has 19
    await bridge.poll()

    printer.job = 7
    await bridge.poll()
    printer.extruded = 1500  # first extrusion: the swap is assumed, provisionally
    await bridge.poll()
    assert bridge.tools[0].provisional.mm == 500
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
    st = bridge.tools[0]
    assert (st.active, st.provisional, st.swap) == (20, None, None)
    assert fake.spools[19]["location"] is None  # evicted
    assert claim_box(fake, 20) is False
    assert fake.flag(19) is None
    assert 'O"#00ff00"' in jobs(printer)[-1]
    reasons = [r["reason"] for r in ledger(tmp_path) if "reason" in r]
    assert "claimed usage since load" in reasons

    await bridge.poll()
    assert load_state(tmp_path / "state.json").claimed == []
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
    assert bridge.tools[0].active == 19
    assert caplog.text.count("spool 20 is claimed but isn't in a tool's location") == 1

    fake.spools[20]["location"] = LOC
    await bridge.poll()
    assert bridge.tools[0].active == 20
    assert claim_box(fake, 20) is False
    assert fake.flag(20) is None
    assert fake.spools[19]["location"] is None
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
    assert bridge.tools[0].active == 19
    assert fake.used == {19: [300.0]}
    assert (claim_box(fake, 19), claim_box(fake, 20)) == (False, False)
    assert caplog.text.count("several spools claimed at once: chose spool 19") == 1
    assert "clearing the claims on spools 20" in caplog.text

    # Neither used: the highest ID wins.
    fake.spools[19]["last_used"] = None
    fake.spools[19]["extra"].update(CLAIMED)
    fake.spools[20]["extra"].update(CLAIMED)
    await bridge.poll()
    assert bridge.tools[0].active == 20
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
    assert bridge.tools[0].active == 20
    assert load_state(tmp_path / "state.json").claimed == [20]
    assert caplog.text.count("claimed in place of spool 19") == 1
    assert caplog.text.count("can't clear the claim on spool 20") == 1
    fake.fail_patch = None
    await bridge.poll()
    assert claim_box(fake, 20) is False
    await bridge.poll()
    assert load_state(tmp_path / "state.json").claimed == []
    await bridge.aclose()


async def test_dry_run_handles_claim_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    bridge.config = replace(bridge.config, dry_run=True)
    caplog.set_level("INFO")
    await bridge.poll()
    fake.spools[20] = spool_x(20, extra=dict(CLAIMED))
    for _ in range(3):
        await bridge.poll()
    assert bridge.tools[0].active == 20
    assert claim_box(fake, 20) is True
    assert caplog.text.count("would clear the claim on spool 20") == 1
    assert not [w for w in fake.writes if w[0] != "field"]
    await bridge.aclose()


async def test_claim_takes_usage_held_for_a_deleted_spool(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    fake.spools[20] = spool_x(20)  # staged
    await bridge.poll()
    del fake.spools[19]
    printer.extruded = 1300
    await bridge.poll()
    st = bridge.tools[0]
    assert (st.active, st.deleted_spool, st.held_mm) == (None, 19, 300)
    assert "usage is being held" in fake.flag(20)

    fake.spools[20]["extra"].update(CLAIMED)
    await bridge.poll()
    st = bridge.tools[0]
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
    assert bridge.tools[0].active == 19
    assert 19 not in load_state(tmp_path / "state.json").runouts
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


async def test_nothing_held_logs_no_held_charge(tmp_path, caplog):
    caplog.set_level("INFO")
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    del fake.spools[19]
    await bridge.poll()  # deleted while idle: nothing held
    fake.spools[20] = spool(20)
    await bridge.poll()
    assert bridge.tools[0].active == 20
    assert "charged it 0 mm" not in caplog.text
    assert not any(r.get("event") == "held_charged" for r in ledger(tmp_path))
    await bridge.aclose()
