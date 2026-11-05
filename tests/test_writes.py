"""Phase 2: charges and flags written to a fake Spoolman."""

import json

import httpx

from spoolman_prusalink.app import Bridge
from spoolman_prusalink.color import quantize
from spoolman_prusalink.config import Config
from spoolman_prusalink.printer_sync import COREONEL_PREAMBLE
from spoolman_prusalink.prusalink import PrusaLink
from spoolman_prusalink.spoolman import Spoolman
from spoolman_prusalink.state import load_state

LOC = "Core One L+"


class FakeSpoolman:
    """Just enough of Spoolman's API: spools, use, PATCH extra, and spool fields."""

    def __init__(self, spools):
        self.spools = {s["id"]: s for s in spools}
        self.fields = []
        self.used = {}  # spool id -> list of use_length calls applied
        self.fail_use = []  # status codes to return for upcoming use calls
        self.writes = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/api/v1/spool" and method == "GET":
            return httpx.Response(200, json=list(self.spools.values()))
        if path == "/api/v1/field/spool" and method == "GET":
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
            return httpx.Response(200, json=self.spools[spool_id])
        if path.startswith("/api/v1/spool/") and method == "PATCH":
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
        self.counters = True  # MINI and MK3.5 firmware report only the material.

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("printer offline")
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
                          flag_field=config.flag_field,
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

    printer.job = 7  # A print starts: the swap is assumed, which leaves a notice.
    await bridge.poll()
    assert "assuming spool 19 was reloaded" in fake.flag(19)

    printer.job = 8  # The next print clears the notice.
    await bridge.poll()
    assert fake.flag(19) is None
    assert load_state(tmp_path / "state.json").flagged == []
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
    assert not [w for w in fake.writes if w[0] in ("patch", "field")]
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
    assert bridge.tools[0].swap is not None
    printer.job, printer.job_name = 51, "part.bgcode"
    await bridge.poll()
    assert bridge.tools[0].swap is None
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


async def test_non_preset_without_temps_is_flagged_not_written(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    fake.spools[20] = spool_x(20, material="PLA-CF")
    printer.unloads, printer.at_unload, printer.material = 1, 1000, {"name": "PLA"}
    await bridge.poll()
    assert jobs(printer) == []
    assert "set its extruder and bed temperatures" in fake.flag(20)

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
