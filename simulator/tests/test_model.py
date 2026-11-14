"""The printer model: tools, loads, prints, power, and snapshots."""

from __future__ import annotations

import json

import pytest

from simulator import gcode, prusalink, sync
from simulator.model import (
    Busy,
    Invalid,
    JobPhase,
    Material,
    PrintSpec,
    Reason,
    Segment,
    SimTool,
    Simulation,
    State,
)
from simulator.preview import Severity
from simulator.tests.util import PLA, loaded, make, run, sliced
from spoolman_prusalink.printer_sync import PRINTER_MODELS, build_job, default_preamble

PETG = Material("PETG", "#186bb5")


def tool0(printer):
    return printer.tools[0]


TEMPERATURE_KEYS = ("nozzle_temperature", "preheat_temperature", "bed_temperature")


def shown(printer, index=0):
    """The material the API shows for a tool, without its temperatures, or None."""
    for t in prusalink.filament(printer)["tools"]:
        if t["index"] == index:
            m = t.get("material")
            return None if m is None else {k: v for k, v in m.items() if k not in TEMPERATURE_KEYS}
    raise AssertionError(f"tool {index} not in the filament endpoint")


def one_segment(mm=100.0, **kw):
    return PrintSpec([Segment(0, mm)], **kw)


# Unload and load.


def test_unload_bookkeeping():
    sim, p = make()
    tool0(p).extruded = 100.7
    p.unload(0)
    assert p.state is State.BUSY
    run(sim, 2.9)
    assert tool0(p).unload_count == 0
    run(sim, 0.2)
    t = tool0(p)
    # From gap 0 the unload's ramming counts its 8 mm feed and 15 of its 55 mm feed before
    # the unload is recorded: 100.7 + 23 * 0.95 = 122.55.
    assert (t.unload_count, t.extruded_at_unload, t.loaded) == (1, 122, False)
    assert t.material is None and t.previous == PLA
    assert t.gap == 40
    assert p.state is State.IDLE
    assert shown(p) is None


def test_menu_use_dismisses_the_finished_screen():
    sim, p = make()
    p.screen = State.FINISHED
    p.unload(0)
    run(sim, 3.1)
    assert p.state is State.IDLE
    p.screen = State.STOPPED
    p.load(0, source="preset", name="PETG")
    sim.tick(40)  # 108 mm of load and 253 mm of auto-retract at 10 mm/s
    assert p.state is State.IDLE


def test_load_runs_its_steps():
    # A Core One L loads 40 mm slow, 20 mm fast, writes the type, purges 40 mm and
    # retracts 8, at 10 mm/s.
    sim, p = make()
    p.unload(0)
    run(sim, 3.1)
    base = tool0(p).extruded
    p.load(0, source="preset", name="PETG")
    assert p.state is State.BUSY and tool0(p).process.phase == "SLOW"
    sim.tick(4.0)  # the slow load fills the 40 mm gap: nothing counted
    assert tool0(p).extruded == base and tool0(p).gap == 0
    assert tool0(p).process.phase == "FAST"
    sim.tick(1.5)  # 15 of the 20 mm fast load: 15 * 0.95
    assert tool0(p).extruded == pytest.approx(base + 14.25)
    assert shown(p) is None
    sim.tick(0.5)  # the fast load ends and the type is written before the purge
    assert shown(p) == {"name": "PETG"}
    assert tool0(p).process.phase == "PURGE"
    assert not tool0(p).loaded
    sim.tick(4.0)  # the purge: (20 + 40) * 0.95
    assert tool0(p).extruded == pytest.approx(base + 57)
    assert tool0(p).process.phase == "RETRACT"
    sim.tick(0.8)  # the retract only grows the gap, to 8
    t = tool0(p)
    # The auto-retract feeds the tracker's 8 mm back at once (gap 0), then runs its steps.
    assert t.process.phase == "RAM" and not t.loaded
    assert t.extruded == pytest.approx(base + 57) and t.gap == 0
    sim.tick(25.3)  # 253 mm of steps: the 8 mm feed and 15 of the 55 mm feed count
    assert t.loaded and t.process is None
    assert t.extruded == pytest.approx(base + 57 + 23 * 0.95)
    assert (t.gap, t.gap_valid, t.auto_retracted) == (40, False, 55)
    assert p.state is State.IDLE


def test_fresh_load_on_loaded_tool_feeds_under_old_material():
    sim, p = make(slow_load_mm=5, fast_load_mm=5)
    p.load(0, source="preset", name="PETG", purge_mm=10)
    sim.tick(0.5)  # the gap is 0, so the slow load counts: 5 * 0.95
    assert shown(p)["name"] == "PLA"
    assert tool0(p).extruded == pytest.approx(4.75)
    sim.tick(1.0)
    assert shown(p) == {"name": "PETG"}


def test_one_tick_runs_a_whole_load():
    sim, p = make()
    p.unload(0)
    sim.tick(3.0)
    base = tool0(p).extruded
    p.load(0, source="preset", name="ASA", color="#DE3129")
    sim.tick(100.0)
    t = tool0(p)
    assert t.loaded and t.process is None and p.state is State.IDLE
    # (20 + 40) past the 40 mm gap, then the auto-retract's 8 + 15: 83 * 0.95.
    assert t.extruded - base == pytest.approx(78.85)
    assert t.material == Material("ASA", "#de3129", **p.model.preset_temperatures("ASA"))


def test_menu_load_clears_color_and_uuid():
    sim, p = make()
    p.unload(0)
    sim.tick(3)
    p.load(0, source="preset", name="PETG")
    sim.tick(40)
    assert tool0(p).material == Material("PETG", adhoc=False, **p.model.preset_temperatures("PETG"))
    assert tool0(p).previous == PLA  # a preset leaves the ad-hoc slot alone
    p.unload(0)
    sim.tick(3)
    p.load(0, source="preset", name="PLA", color="#ff0000")
    sim.tick(40)
    picked = Material("PLA", "#ff0000", **p.model.preset_temperatures("PLA"))
    assert tool0(p).material == picked and tool0(p).previous == picked


def test_runout_reload_restores_a_preset_not_the_adhoc_slot():
    sim, p = make()
    p.unload(0)
    sim.tick(3)
    p.load(0, source="preset", name="PETG")
    sim.tick(40)
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 1)
    p.runout(0)
    p.load(0)
    sim.tick(20)
    assert tool0(p).material == Material("PETG", adhoc=False, **p.model.preset_temperatures("PETG"))
    assert tool0(p).previous == PLA


def test_tag_load_sets_uuid_on_uuids_model_only():
    uuid = "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"
    sim, p = make()
    p.unload(0)
    sim.tick(3)
    p.load(0, source="tag", name="PETG", color="#186BB5", uuid=uuid.upper())
    sim.tick(40)
    tag = shown(p)
    assert tag.pop("material_uuid") and tag.pop("package_uuid")
    assert tag == {"name": "PETG", "color": "#186bb5", "instance_uuid": uuid}

    # The bridge's write starts the type afresh: only its own instance UUID is left.
    sync.apply(p, gcode.parse(f'M865 X R N"PETG" O"#186bb5" L0\nM865.1 I0 C"{uuid}"'))
    assert shown(p) == {"name": "PETG", "color": "#186bb5", "instance_uuid": uuid}

    sim, mk4 = make("1.4.1", [SimTool(0)])
    with pytest.raises(Invalid):
        mk4.load(0, source="tag", name="PETG", uuid=uuid)


def test_load_checks_parameters():
    sim, p = make()
    with pytest.raises(Invalid):
        p.load(0, source="preset", name="TOOLONGNAME")
    with pytest.raises(Invalid):
        p.load(0, source="preset", name="PLA", color="red")
    with pytest.raises(Invalid):
        p.load(0, source=None)  # keep previous is only for a runout reload
    with pytest.raises(Invalid):
        p.load(3, source="preset", name="PLA")


def test_load_and_unload_while_off_count_nothing_until_boot():
    sim, p = make()
    p.set_power("off")
    p.unload(0)
    assert tool0(p).unload_count == 0 and tool0(p).material == PLA
    p.set_power("on")
    t = tool0(p)
    assert t.unload_count == 1 and t.material is None and t.previous == PLA
    p.set_power("off")
    p.load(0, source="preset", name="PETG")
    assert tool0(p).loaded and tool0(p).material == Material(
        "PETG", adhoc=False, **p.model.preset_temperatures("PETG")
    )
    assert tool0(p).extruded == 0


def test_extrude_fills_gap_then_counts_and_retract_clamps():
    sim, p = make()
    p.retract(0, 100)
    assert tool0(p).gap == 40
    p.extrude(0, 50)  # 40 refill the gap, (50 - 40) * 0.95 counts
    assert tool0(p).gap == 0 and tool0(p).extruded == pytest.approx(9.5)
    p.start_print(PrintSpec([Segment(0, 10)]))
    with pytest.raises(Busy):
        p.extrude(0, 5)


# Prints.


def test_print_phases_and_start_blip():
    sim, p = make()
    p.start_print(one_segment(10))
    assert p.job_counter == 1 and p.state is State.PRINTING
    assert prusalink.status(p)["job"]["id"] == 1
    sim.tick(0.5)  # scanning done: the blip hides the job
    assert p.job.phase is JobPhase.BLIP and p.state is State.IDLE
    assert "job" not in prusalink.status(p) and prusalink.job(p) is None
    assert not p.can_print()
    sim.tick(0.3)
    assert p.job.phase is JobPhase.PREHEAT and p.state is State.PRINTING
    assert prusalink.job(p)["state"] == "PRINTING"
    sim.tick(5.0)
    assert p.job.phase is JobPhase.PRINTING
    assert tool0(p).extruded == 0
    sim.tick(1.0)
    assert tool0(p).extruded == pytest.approx(5 * 0.95)
    sim.tick(1.0)
    assert p.job.phase is JobPhase.PARKING and p.state is State.PRINTING
    sim.tick(1.0)
    assert p.job is None and p.state is State.FINISHED
    assert prusalink.job(p) is None
    # The end retracts 8 mm (gap 8), and the auto-retract feeds them back (gap 0), then
    # counts its 8 mm feed and 15 of its 55 mm feed: (10 + 23) * 0.95.
    assert tool0(p).extruded == pytest.approx(31.35)


def test_start_blip_can_be_turned_off():
    sim, p = make(start_blip=False)
    p.start_print(one_segment(10))
    sim.tick(0.5)
    assert p.job.phase is JobPhase.PREHEAT


def test_large_tick_carries_leftover_through_phases():
    sim, p = make()
    p.start_print(one_segment(100))
    sim.tick(0.5 + 0.3 + 5 + 2)  # scan, blip, preheat, then 2 s of printing
    assert p.job.phase is JobPhase.PRINTING
    assert p.job.fed_mm == pytest.approx(10)
    assert tool0(p).extruded == pytest.approx(9.5)


def test_one_tick_runs_a_whole_print():
    sim, p = make()
    p.start_print(one_segment(100, duration_s=10))
    sim.tick(1000)
    assert p.job is None and p.state is State.FINISHED
    assert tool0(p).extruded == pytest.approx(95 + 23 * 0.95)  # the print, then the auto-retract


def test_duration_sets_the_rate():
    sim, p = make()
    p.start_print(one_segment(100, duration_s=10, preheat_s=0))
    sim.tick(0.8 + 5)
    assert p.job.fed_mm == pytest.approx(50)


def test_print_without_filament_waits_for_a_load():
    sim, p = make(tools=[SimTool(0)])
    p.start_print(one_segment(10, preheat_s=0))
    sim.tick(1)
    assert p.state is State.ATTENTION and p.job.phase is JobPhase.WAIT_LOAD
    assert tool0(p).unload_count == 0
    p.load(0, source="preset", name="PLA")
    sim.tick(40)
    assert p.state is State.PAUSED
    p.resume()
    sim.tick(10)
    assert p.state is State.FINISHED


def test_pause_resume_cancel_dismiss():
    sim, p = make()
    with pytest.raises(Busy):
        p.pause()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 1)
    before = tool0(p).extruded
    p.pause()
    assert p.state is State.PAUSED and prusalink.job(p)["state"] == "PAUSED"
    sim.tick(10)
    assert tool0(p).extruded == before
    p.resume()
    assert p.state is State.PRINTING
    sim.tick(1)
    assert tool0(p).extruded > before
    with pytest.raises(Busy):
        p.dismiss()
    p.cancel()
    assert p.state is State.PRINTING and p.job.phase is JobPhase.PARKING
    with pytest.raises(Busy):
        p.cancel()
    sim.tick(1)
    assert p.state is State.STOPPED and p.job is None
    p.dismiss()
    assert p.state is State.IDLE


def test_pause_during_preheat_resumes_preheat():
    sim, p = make()
    p.start_print(one_segment(100))
    sim.tick(0.8 + 2)
    p.pause()
    p.resume()
    assert p.job.phase is JobPhase.PREHEAT
    sim.tick(3)
    assert p.job.phase is JobPhase.PRINTING


def test_runout_keeps_previous_and_reload_restores_it():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 2)
    assert tool0(p).extruded == pytest.approx(9.5)
    p.runout(0)
    t = tool0(p)
    assert p.state is State.ATTENTION and str(p.job.attention) == "runout tool 0"
    # The park retracts 8 mm, the runout ramming feeds them back and counts its 7 mm feed
    # before the unload is recorded: (10 + 7) * 0.95 = 16.15.
    assert (t.unload_count, t.extruded_at_unload, t.material) == (1, 16, None)
    assert t.previous == PLA
    assert prusalink.job(p)["state"] == "PAUSED"
    with pytest.raises(Busy):
        p.resume()
    p.load(0)  # keep previous
    sim.tick(4 + 3.6 + 0.1)
    assert shown(p)["instance_uuid"] == PLA.instance_uuid
    assert p.state is State.ATTENTION
    sim.tick(10)
    assert p.state is State.PAUSED and tool0(p).loaded
    p.resume()
    sim.tick(100)
    assert p.state is State.FINISHED


def test_jam_adds_an_extra_purge_on_reload():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 1)
    p.jam(0)
    assert str(p.job.attention) == "jam tool 0"
    before = tool0(p).extruded
    p.load(0)
    sim.tick(100)
    # Past the 40 mm gap: the 20 mm fast load, the 40 mm purge and the 60 mm jam purge,
    # times 0.95. The retract, prime and retract that follow only move within the gap.
    assert tool0(p).extruded == pytest.approx(before + (20 + 40 + 60) * 0.95)
    assert p.state is State.PAUSED


def test_jam_cleared_without_unloading_counts_nothing():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 1)
    p.jam(0, unload=False)
    assert p.state is State.ATTENTION
    with pytest.raises(Invalid):
        p.load(0)
    p.clear_jam()
    assert p.state is State.PAUSED and tool0(p).unload_count == 0
    p.resume()
    assert p.state is State.PRINTING


def test_color_change_in_one_large_tick():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0, color_changes_mm=[40, 400]))
    sim.tick(1000)
    assert p.state is State.ATTENTION and str(p.job.attention) == "color change tool 0"
    assert p.job.fed_mm == 40
    # 40 mm printed, then the runout ramming's 7 mm feed: 47 * 0.95 = 44.65.
    assert tool0(p).unload_count == 1 and tool0(p).extruded_at_unload == 44
    p.load(0)
    sim.tick(100)
    p.resume()
    sim.tick(1000)
    assert p.state is State.FINISHED


def test_multi_tool_print_attributes_extrusion_and_purges():
    tools = [loaded(0, PETG), loaded(1, PETG), SimTool(2), loaded(3, PETG, enabled=False)]
    sim, p = make("8.10.0", tools, toolchange_purge_mm=30)
    with pytest.raises(Invalid):
        p.start_print(PrintSpec([Segment(3, 10)]))
    spec = PrintSpec([Segment(0, 100), Segment(1, 200), Segment(0, 100)], preheat_s=0)
    p.start_print(spec)
    sim.tick(0.8 + 20 + 1)  # 105 mm in: the change to T1 and its purge happened
    assert p.selected_tool == 1
    # INDX feeds 550/576 of each mm commanded, purges included.
    assert p.tools[1].extruded == pytest.approx((30 + 5) * 550 / 576)
    sim.tick(1000)
    assert p.state is State.FINISHED
    assert p.tools[0].extruded == pytest.approx((200 + 30) * 550 / 576)
    assert p.tools[1].extruded == pytest.approx((200 + 30) * 550 / 576)
    assert p.tools[2].extruded == 0
    assert all(t.unload_count == 0 for t in p.tools.values())


def test_tool_change_to_empty_tool_waits_for_it():
    sim, p = make("8.10.0", [loaded(0, PETG), SimTool(1)])
    p.start_print(PrintSpec([Segment(0, 10), Segment(1, 10)], preheat_s=0))
    sim.tick(100)
    assert p.job.phase is JobPhase.WAIT_LOAD and str(p.job.attention) == "no filament in tool 1"


# Power and boot.


def test_reboot_resets_counters_and_keeps_the_rest():
    sim, p = make()
    tool0(p).extruded, tool0(p).unload_count = 500, 2
    p.job_counter, p.message, p.homed = 7, "hi", True
    boot = p.boot_id
    p.reboot()
    t = tool0(p)
    assert (t.extruded, t.unload_count, t.extruded_at_unload) == (0, 0, 0)
    assert t.material == PLA and p.job_counter == 7
    assert p.boot_id != boot and p.message is None and not p.homed


def test_power_loss_resume_keeps_the_job():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 2)
    p.reboot(keep_job=True)
    assert p.job.id == 1 and p.state is State.BUSY and tool0(p).extruded == 0
    sim.tick(1)  # halfway through the recovery the nozzle clean purges 25 mm: 25 * 0.95
    assert p.state is State.BUSY and tool0(p).extruded == pytest.approx(23.75)
    sim.tick(1)  # recovered: the head unparks
    assert p.state is State.PAUSED and tool0(p).extruded == pytest.approx(23.75)
    sim.tick(1)  # unparked: the print goes on
    assert p.state is State.PRINTING and tool0(p).extruded == pytest.approx(23.75)
    sim.tick(1)
    assert tool0(p).extruded > 23.75
    p.reboot()
    assert p.job is None and p.state is State.IDLE and p.job_counter == 1


def test_power_off_freezes_and_power_on_boots():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 2)
    p.set_power("off")
    before = (tool0(p).extruded, p.job.fed_mm)
    sim.tick(100)
    assert (tool0(p).extruded, p.job.fed_mm) == before
    with pytest.raises(Busy):
        p.reboot()
    boot = p.boot_id
    p.set_power("on")
    assert p.boot_id != boot and p.job is None and p.state is State.IDLE
    assert tool0(p).extruded == 0 and p.job_counter == 1


def test_hung_printer_keeps_running():
    sim, p = make()
    p.start_print(one_segment(10, preheat_s=0))
    p.set_power("hung")
    sim.tick(100)
    assert p.state is State.FINISHED
    p.set_power("on")
    assert p.state is State.FINISHED  # un-hanging isn't a reboot


def test_idle_sensor_clear_on_boot():
    sim, p = make()
    tool0(p).loaded = False  # pulled out while off
    p.reboot()
    assert tool0(p).unload_count == 1 and tool0(p).previous == PLA


def test_idle_sensor_clear_on_select_tool():
    tools = [loaded(0, PETG), SimTool(1, material=Material("PLA"))]
    sim, p = make("8.10.0", tools)
    p.select_tool(1)
    assert p.selected_tool == 1 and p.tools[1].unload_count == 1
    p.select_tool(0)
    assert p.tools[0].unload_count == 0
    p.set_sensor_fault(0, True)
    p.select_tool(1)
    p.select_tool(0)
    assert p.tools[0].unload_count == 1
    sim, single = make()
    with pytest.raises(Invalid):
        single.select_tool(0)


def test_counter_anomalies():
    sim, p = make()
    tool0(p).extruded = 100
    p.counter_step_back(0, 30)
    assert tool0(p).extruded == 70
    p.counter_step_back(0, 300)
    assert tool0(p).extruded == 0
    p.set_extruded_at_unload(0, 9999)
    assert prusalink.filament(p)["tools"][0]["extruded_at_unload"] == 9999


def test_disable_tool_rules():
    sim, p = make("8.10.0", [loaded(0, PETG), loaded(1, PETG)])
    with pytest.raises(Invalid):
        p.set_tool_enabled(0, False)  # selected
    p.set_tool_enabled(1, False)
    with pytest.raises(Invalid):
        p.unload(1)
    p.set_tool_enabled(1, True)
    p.unload(1)


# Sync jobs and G-code.

JOB = """M862.3 P "COREONEL" ; printer model check
M862.5 P2 ; g-code level check
M862.6 P"Input shaper" ; FW feature check
G28 O ; home only if not homed
M865 X R N"PETG" T230 B85 P170 C30 D15 E45 O"#186BB5" L0
M865.1 I0 C"8B6A3E3E-3F6C-4D7B-9A43-6C1F0E7D2A11"
M117 Spoolman: test
"""


def run_sync(sim, p, body=JOB, name="spoolman.gcode"):
    p.upload(name, body, print_after=True, overwrite=True)
    sim.tick(0.8)


def test_sync_job_applies_and_finishes():
    sim, p = make()
    p.upload("spoolman.gcode", JOB, print_after=True, overwrite=True)
    assert prusalink.job(p)["file"]["display_name"] == "spoolman.gcode"
    sim.tick(0.5)
    assert p.state is State.IDLE and prusalink.job(p) is None  # the blip
    sim.tick(0.3)
    assert p.state is State.PRINTING and prusalink.job(p) is not None
    m = tool0(p).material
    uuid = "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"
    assert m == Material("PETG", "#186bb5", uuid, nozzle=230, preheat=170, bed=85)
    assert p.message == "Spoolman: test" and p.homed
    sim.tick(3 + 1)
    assert p.state is State.FINISHED and p.job is None and p.job_counter == 1


def test_upload_refusals():
    sim, p = make()
    p.upload("a.gcode", "M117 x\n", print_after=False, overwrite=False)
    with pytest.raises(Busy, match="File already exists"):
        p.upload("a.gcode", "M117 x\n", print_after=True, overwrite=False)
    p.start_print(one_segment(100))
    with pytest.raises(Busy, match="Can't print right now"):
        p.upload("b.gcode", "M117 y\n", print_after=True, overwrite=False)
    assert "b.gcode" in p.files
    sim, p = make()
    run_sync(sim, p, name="c.gcode")
    with pytest.raises(Busy, match="File is busy"):
        p.upload("c.gcode", JOB, print_after=True, overwrite=True)


def test_the_preview_counts_the_job_then_asks():
    sim, p = make()
    p.upload("s.gcode", JOB.replace("COREONEL", "MINI"), print_after=True, overwrite=True)
    # The job is counted at once and reads PRINTING while the file is scanned.
    assert p.job_counter == 1 and p.job.id == 1 and p.state is State.PRINTING
    assert prusalink.status(p)["job"]["id"] == 1
    sim.tick(0.5)
    assert p.state is State.ATTENTION
    assert str(p.job.attention) == "preview: for printer MINI"
    assert prusalink.status(p)["job"]["id"] == 1
    assert prusalink.job(p)["state"] == "PAUSED" and prusalink.job(p)["id"] == 1
    assert not p.can_print()
    sim.tick(100)  # nothing times out
    assert p.state is State.ATTENTION and tool0(p).material == PLA  # nothing applied yet
    p.tap_through()  # Print
    assert p.job.phase is JobPhase.BLIP and p.state is State.IDLE and not p.job_visible()
    sim.tick(0.3)
    assert p.state is State.PRINTING and tool0(p).material.name == "PETG"
    sim.tick(10)
    assert p.state is State.FINISHED and p.job_counter == 1


def test_abort_at_the_preview_ends_idle_with_the_id_used():
    sim, p = make()
    p.upload("s.gcode", JOB.replace("COREONEL", "MINI"), print_after=True, overwrite=True)
    sim.tick(0.5)
    extruded = tool0(p).extruded
    p.cancel()  # Abort
    assert p.job is None and p.state is State.IDLE and p.job_counter == 1
    assert "s.gcode" in p.files
    assert tool0(p).extruded == extruded and tool0(p).auto_retracted is None  # nothing moved
    run_sync(sim, p)
    assert p.job.id == 2


def test_the_preview_takes_a_group_and_asks_in_compatibility_mode():
    # A Core One L takes a Core One file as its own, and an MMU3 name as its base model.
    for name in ("COREONE", "COREONELMMU3"):
        sim, p = make()
        run_sync(sim, p, JOB.replace("COREONEL", name))
        assert p.state is State.PRINTING
    # A Core One takes an MK4S file in compatibility mode, which asks.
    sim, p = make("7.1.0")
    run_sync(sim, p, JOB.replace("COREONEL", "MK4S"))
    assert str(p.job.attention) == "preview: compatibility mode for MK4S"


def test_a_fatal_question_offers_only_abort():
    sim, p = make()
    p.set_checks(Severity.ABORT)
    run_sync(sim, p, JOB.replace("COREONEL", "MK4S"))
    assert p.job.attention.fatal and p.state is State.ATTENTION
    with pytest.raises(Busy, match="offers only Abort"):
        p.tap_through()
    assert p.actions_json()["tap_through"] == "the preview offers only Abort; cancel answers it"
    p.cancel()
    assert p.job is None and p.state is State.IDLE


def test_checks_set_to_ignore_ask_nothing():
    sim, p = make()
    p.set_checks(Severity.IGNORE)
    run_sync(sim, p, JOB.replace("COREONEL", "MK4S"))
    assert p.state is State.PRINTING and tool0(p).material.name == "PETG"


def test_indx_asks_about_the_lock_under_any_setting():
    for severity in Severity:
        sim, p = make("8.10.0")
        p.set_checks(severity)
        run_sync(sim, p, JOB.replace("COREONEL", "COREONEL-INDX"))
        assert p.job.waits_for(Reason.PREVIEW) and not p.job.attention.fatal
        assert p.job.attention.detail == "not sliced for the INDX lock"
        p.tap_through()
        sim.tick(0.3)
        assert p.state is State.PRINTING


def test_an_upload_during_the_question_is_refused_and_kept():
    sim, p = make()
    run_sync(sim, p, JOB.replace("COREONEL", "MK4S"))
    with pytest.raises(Busy, match="Can't print right now"):
        p.upload("b.gcode", JOB, print_after=True, overwrite=False)
    assert p.files["b.gcode"] == JOB and p.job.name == "spoolman.gcode"


def test_the_bridge_default_job_asks_nothing_on_the_models_it_writes_to():
    """The bridge's own job, built by its `build_job`, runs as it is on every model it
    writes to."""
    writable = [c for c, m in PRINTER_MODELS.items() if m.unsupported is None]
    assert writable == ["1.3.9", "1.3.10", "1.4.0", "1.4.1", "7.1.0", "7.2.0", "8.1.0"]
    for code in writable:
        sim, p = make(code)
        run_sync(sim, p, build_job(default_preamble(PRINTER_MODELS[code]), []))
        assert p.state is State.PRINTING and p.job.phase is JobPhase.RUN, code


def test_parse_error_holds_and_tap_through_stops():
    sim, p = make()
    run_sync(sim, p, sliced(p, 'M865 X N"WAYTOOLONG" L0\n'))
    assert p.state is State.ATTENTION and str(p.job.attention).startswith("parse error: line 4")
    p.tap_through()
    sim.tick(2)
    assert p.state is State.STOPPED


def test_m865_on_missing_tool_is_a_parse_error():
    sim, p = make()
    run_sync(sim, p, sliced(p, 'M865 X R N"PLA" L3\n'))
    assert str(p.job.attention).startswith("parse error")


def test_m865_writes_disabled_and_unconfigured_tools():
    sim, p = make("8.10.0", [loaded(0, PETG), SimTool(1, enabled=False)])
    run_sync(sim, p, sliced(p, 'M865 X R N"PLA" L1\nM865 X R N"ASA" L5\n'))
    assert p.job.phase is JobPhase.RUN
    assert p.tools[1].material.name == "PLA" and not p.tools[1].enabled
    assert p.tools[5].material.name == "ASA" and not p.tools[5].enabled
    assert [t["index"] for t in prusalink.filament(p)["tools"]] == [0]
    sim, p = make("8.10.0", [loaded(0, PETG)])
    run_sync(sim, p, sliced(p, 'M865 X R N"PLA" L8\n'))
    assert str(p.job.attention).startswith("parse error")


def test_stuck_write_ignores_m865():
    sim, p = make()
    p.set_stuck_write(0, True)
    run_sync(sim, p)
    assert tool0(p).material == PLA
    assert p.message == "Spoolman: test"


# Snapshots.


def test_snapshot_round_trip_mid_print_and_mid_load():
    tools = [loaded(0, PETG), SimTool(1, material=None), loaded(2, PETG)]
    sim, p = make("8.10.0", tools)
    p.high_flow = True
    p.start_print(PrintSpec([Segment(0, 100), Segment(2, 50)], color_changes_mm=[80]))
    p.upload("x.gcode", "M117 x\n", print_after=False, overwrite=False)
    sim.tick(0.8 + 5 + 3)
    p.runout(0)
    p.load(0)
    sim.tick(2)

    data = json.loads(json.dumps(sim.snapshot()))
    other = Simulation()
    other.restore(data)
    copy = other.printers["p"]
    copy.boot_id = p.boot_id  # a restore boots afresh
    assert copy.to_json() == p.to_json()
    assert copy.job == p.job and copy.tools == p.tools and copy.high_flow
    assert copy.tools[0].process.steps == p.tools[0].process.steps  # the load in progress
    for s in (sim, other):
        s.tick(100)
        s.printers["p"].resume()
        s.tick(1000)
    assert copy.to_json() == p.to_json()
    assert copy.state is State.ATTENTION  # the color change at 80 mm
    assert other.now == sim.now


def test_restore_boots_afresh_and_keeps_the_job_counter():
    sim, p = make()
    data = json.loads(json.dumps(sim.snapshot()))
    boot = p.boot_id
    run_sync(sim, p)
    sim.tick(10)
    assert p.job_counter == 1
    sim.restore(data)
    p = sim.printers["p"]
    assert p.boot_id != boot and p.job_counter == 1


def test_restore_refuses_other_printers():
    sim, p = make()
    data = sim.snapshot()
    data["printers"][0]["name"] = "other"
    with pytest.raises(Invalid):
        sim.restore(data)


def test_events_log_actions_and_state_changes():
    sim, p = make()
    p.unload(0)
    kinds = [e.kind for e in sim.events]
    assert kinds == ["action", "state"]
    assert sim.events[-1].text == "IDLE -> BUSY"


def test_idle_sensor_clear_after_a_sync_job_writes_an_empty_tool():
    sim, p = make(tools=[SimTool(0)])
    run_sync(sim, p, sliced(p, 'M865 X R N"PLA" L0\n'))
    assert shown(p) == {"name": "PLA"}
    sim.tick(1)
    assert shown(p) == {"name": "PLA"} and tool0(p).unload_count == 0
    sim.tick(10)
    assert p.state is State.FINISHED
    assert shown(p) is None and tool0(p).unload_count == 1


def test_idle_sensor_clear_on_a_sensor_fault():
    tools = [loaded(0, PETG), loaded(1, PETG)]
    sim, p = make("8.10.0", tools)
    p.set_sensor_fault(1, True)  # not the selected tool
    sim.tick(1)
    assert p.tools[1].unload_count == 0
    p.set_sensor_fault(0, True)
    sim.tick(1)
    sim.tick(1)
    assert tool0(p).unload_count == 1


def test_runout_needs_the_selected_tool():
    sim, p = make("8.10.0", [loaded(0, PETG), loaded(1, PETG)])
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
    sim.tick(0.8 + 1)
    with pytest.raises(Invalid, match="isn't the selected tool"):
        p.runout(1)
    with pytest.raises(Invalid, match="isn't the selected tool"):
        p.jam(1)
    p.runout(0)


def test_cancel_stops_a_reload():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 1)
    p.runout(0)
    p.load(0)
    sim.tick(1)  # still filling the gap
    p.cancel()
    assert tool0(p).process is None and not tool0(p).loaded and tool0(p).material is None
    sim.tick(2)
    assert p.state is State.STOPPED and p.can_print()

    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 1)
    p.runout(0)
    p.load(0)
    sim.tick(4 + 3.6 + 1)  # into the purge: the type is set
    p.cancel()
    assert tool0(p).loaded and tool0(p).material == PLA


def test_no_print_while_a_tool_process_runs():
    sim, p = make()
    p.unload(0)
    assert p.state is State.BUSY and not p.can_print()


def test_tool_change_into_an_empty_tool_purges_after_the_reload():
    sim, p = make("8.10.0", [loaded(0, PETG), SimTool(1)])
    p.start_print(PrintSpec([Segment(0, 10), Segment(1, 10)], preheat_s=0))
    sim.tick(0.8 + 10)
    assert p.job.phase is JobPhase.WAIT_LOAD and p.job.toolchange_purge
    p.load(1, source="preset", name="PLA")
    sim.tick(40)
    # T1 was never unloaded, so its gap is 0 and every forward mm counts: the 15 mm slow
    # load, the 25 mm fast load and the cleaner's 25 mm purge. The cleaner's auto-retract
    # counts its 8 mm feed and leaves 8 mm stored (gap 8); the prime feeds those back
    # (gap 0) and counts its 4 mm. 77 * 550/576. The 8 mm retract after the prime only
    # opens the gap.
    assert p.tools[1].extruded == pytest.approx(77 * 550 / 576)
    assert p.tools[1].gap == pytest.approx(8)
    p.resume()  # back to the empty tool's gap 0: 8 fed, then the tool change purges 30
    assert p.tools[1].extruded == pytest.approx((77 + 30) * 550 / 576)
    sim.tick(10)
    assert p.state is State.FINISHED
    # The end retracts 8 mm, which INDX counts as fully retracted: no auto-retract.
    assert p.tools[1].extruded == pytest.approx((77 + 30 + 10) * 550 / 576)


def test_sync_job_progress_moves_with_time():
    sim, p = make()
    run_sync(sim, p)
    assert p.job.phase is JobPhase.RUN and p.job.progress() == 0
    sim.tick(1.5)
    assert p.job.progress() == 50


def test_home_sets_the_flag_only_when_idle():
    sim, p = make()
    assert not p.homed
    p.home()
    assert p.homed
    p.reboot()
    assert not p.homed
    p.start_print(PrintSpec([Segment(0, 10)], duration_s=1))
    assert p.homed  # a print starts by homing
    with pytest.raises(Busy):
        p.home()


def test_power_loss_with_a_cold_bed_waits_for_the_user():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 2)
    p.set_power("off")
    sim.tick(600)
    p.set_power("on", resume=True, cold_bed=True)
    assert p.job.id == 1 and p.state is State.BUSY
    sim.tick(2)
    assert p.state is State.ATTENTION and prusalink.job(p)["state"] == "PAUSED"
    assert tool0(p).extruded == 0  # no nozzle clean before the user answers
    sim.tick(60)
    assert p.state is State.ATTENTION  # nothing happens until the user resumes
    p.resume()
    assert p.state is State.BUSY
    sim.tick(2)
    assert p.state is State.PAUSED and tool0(p).extruded == pytest.approx(25 * 0.95)
    sim.tick(1)
    assert p.state is State.PRINTING


def test_cancel_during_the_power_panic_recovery_cleans_nothing():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 2)
    p.reboot(keep_job=True)
    assert p.state is State.BUSY and tool0(p).extruded == 0
    p.cancel()
    sim.tick(5)
    # No clean, but the stop retracts and auto-retracts. The boot left the tracker at gap 0
    # and invalid, so nothing is fed back first: the retract opens 8 mm, the sequence's
    # 8 mm feed refills them, and only 15 of its 55 mm feed count: 15 * 0.95.
    assert p.job is None and p.state is State.STOPPED and tool0(p).extruded == 14.25


def test_power_loss_resume_skips_the_idle_sensor_clear():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 2)
    p.set_power("off")
    p.unload(0)  # pulled out by hand while off
    p.set_power("on", resume=True)
    assert p.job.id == 1 and p.state is State.BUSY
    assert tool0(p).unload_count == 0 and tool0(p).material == PLA
    sim.tick(2 + 1)  # recovered and unparked: the empty tool stops the print
    assert p.state is State.ATTENTION and tool0(p).extruded == 0


def test_power_loss_while_paused_comes_back_paused():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 2)
    p.pause()
    p.reboot(keep_job=True)
    assert p.job.id == 1 and p.state is State.PAUSED and tool0(p).extruded == 0
    p.resume()
    assert p.state is State.PRINTING


def test_upload_during_the_start_blip_is_dropped():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.5)
    assert p.job.phase is JobPhase.BLIP
    p.upload("spoolman.gcode", JOB, print_after=True, overwrite=True)  # no Busy
    assert p.job.id == 1 and p.job.kind == "user" and "spoolman.gcode" in p.files
    sim.tick(0.3 + 1)
    assert p.state is State.PRINTING and p.job.kind == "user"


def temperatures(printer, index=0):
    m = next(t for t in prusalink.filament(printer)["tools"] if t["index"] == index)["material"]
    return tuple(m[k] for k in TEMPERATURE_KEYS)


def test_preset_load_reports_the_preset_temperatures():
    sim, p = make()
    p.unload(0)
    sim.tick(3.0)
    p.load(0, source="preset", name="PC")
    sim.tick(100.0)
    assert temperatures(p) == (275, 250, 100)


def test_temperatures_are_set_when_the_material_is_built():
    sim, p = make(tools=[SimTool(0)])
    # A TPU tag starts from FLEX; other names match a preset exactly.
    p.load(0, source="tag", name="TPU", uuid="8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11")
    sim.tick(100.0)
    assert temperatures(p) == (240, 210, 50)
    p.unload(0)
    sim.tick(3.0)
    p.load(0, source="preset", name="PLA", color="#ff0000")
    sim.tick(100.0)
    sync.apply(p, gcode.parse('M865 X N"PC" L0'))  # without R, only renames
    assert temperatures(p) == (215, 170, 60)
    sim, p = make(tools=[SimTool(0)])
    p.load(0, source="tag", name="petg", uuid="8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11")
    sim.tick(100.0)
    assert temperatures(p) == (215, 170, 60)


def test_a_written_color_reads_back_in_5_bits_a_channel():
    """F25: the firmware stores a color as Color555, so `#73767d` reads back `#73737b`."""
    sim, p = make()
    sync.apply(p, gcode.parse('M865 X R N"PLA" O"#73767d" L0\n'))
    assert prusalink.filament(p)["tools"][0]["material"]["color"] == "#73737b"
    assert tool0(p).previous.color == "#73737b"
    p.unload(0)
    run(sim, 3.1)
    p.load(0, source="preset", name="PLA", color="#73767d")
    run(sim, 20)
    assert tool0(p).material.color == "#73737b"


def test_a_snapshot_keeps_the_checks_and_the_question():
    sim, p = make()
    p.set_checks(Severity.ABORT)
    run_sync(sim, p, JOB.replace("COREONEL", "MINI"))
    data = json.loads(json.dumps(sim.snapshot()))
    other = Simulation()
    other.restore(data)
    copy = other.printers["p"]
    assert copy.checks is Severity.ABORT and copy.job == p.job
    assert copy.job.attention.fatal
    assert p.to_json()["checks"] == "abort"
    assert p.to_json()["job"]["attention"]["fatal"] is True
    assert sim.to_json()["options"]["checks"] == ["ignore", "warn", "abort"]


def test_a_running_job_logs_its_checks_and_skips_other_lines():
    sim, p = make()
    run_sync(sim, p, sliced(p, "G1 X10\nhello\n"))
    logged = [e.text for e in sim.events if e.kind == "gcode"]
    assert logged[:3] == [
        "checked at the preview: ModelCheck(name='COREONEL')",
        "checked at the preview: LevelCheck(level=2)",
        "checked at the preview: FeatureCheck(name='Input Shaper')",
    ]
    assert logged[3:] == ["ignored: G1 X10", "ignored: hello"]
