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
    Segment,
    SimTool,
    Simulation,
    State,
    preset_temperatures,
)
from simulator.tests.util import PLA, loaded, make, run

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
    assert (t.unload_count, t.extruded_at_unload, t.loaded) == (1, 100, False)
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
    sim.tick(20)
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
    sim.tick(0.8)  # the retract only grows the gap
    t = tool0(p)
    assert t.loaded and t.process is None
    assert t.extruded == pytest.approx(base + 57) and t.gap == pytest.approx(8)
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
    p.load(0, source="preset", name="ASA", color="#DE3129")
    sim.tick(100.0)
    t = tool0(p)
    assert t.loaded and t.process is None and p.state is State.IDLE
    assert t.extruded == pytest.approx(57)  # (20 + 40) * 0.95 past the 40 mm gap
    assert t.material == Material("ASA", "#de3129", **preset_temperatures("ASA"))


def test_menu_load_clears_color_and_uuid():
    sim, p = make()
    p.unload(0)
    sim.tick(3)
    p.load(0, source="preset", name="PETG")
    sim.tick(20)
    assert tool0(p).material == Material("PETG", adhoc=False, **preset_temperatures("PETG"))
    assert tool0(p).previous == PLA  # a preset leaves the ad-hoc slot alone
    p.unload(0)
    sim.tick(3)
    p.load(0, source="preset", name="PLA", color="#ff0000")
    sim.tick(20)
    picked = Material("PLA", "#ff0000", **preset_temperatures("PLA"))
    assert tool0(p).material == picked and tool0(p).previous == picked


def test_runout_reload_restores_a_preset_not_the_adhoc_slot():
    sim, p = make()
    p.unload(0)
    sim.tick(3)
    p.load(0, source="preset", name="PETG")
    sim.tick(20)
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 1)
    p.runout(0)
    p.load(0)
    sim.tick(20)
    assert tool0(p).material == Material("PETG", adhoc=False, **preset_temperatures("PETG"))
    assert tool0(p).previous == PLA


def test_tag_load_sets_uuid_on_uuids_model_only():
    uuid = "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"
    sim, p = make()
    p.unload(0)
    sim.tick(3)
    p.load(0, source="tag", name="PETG", color="#186BB5", uuid=uuid.upper())
    sim.tick(20)
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
        "PETG", adhoc=False, **preset_temperatures("PETG")
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
    assert tool0(p).extruded == pytest.approx(9.5)


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
    assert tool0(p).extruded == pytest.approx(95)


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
    sim.tick(20)
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
    at = int(tool0(p).extruded)
    p.runout(0)
    t = tool0(p)
    assert p.state is State.ATTENTION and str(p.job.attention) == "runout tool 0"
    assert (t.unload_count, t.extruded_at_unload, t.material) == (1, at, None)
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
    assert tool0(p).unload_count == 1 and tool0(p).extruded_at_unload == 38
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


def test_model_check_asks_before_the_job_is_counted():
    sim, p = make()
    p.upload("s.gcode", JOB.replace("COREONEL", "MK4S"), print_after=True, overwrite=True)
    assert p.state is State.ATTENTION and str(p.job.attention) == "model check"
    assert p.job_counter == 0 and p.job.id is None
    assert prusalink.job(p) is None and "job" not in prusalink.status(p)
    assert not p.can_print()
    sim.tick(100)
    assert p.state is State.ATTENTION and tool0(p).material == PLA  # nothing applied yet
    p.tap_through()
    assert p.job_counter == 1 and p.state is State.PRINTING
    assert prusalink.status(p)["job"]["id"] == 1
    sim.tick(0.8)
    assert tool0(p).material.name == "PETG"
    sim.tick(10)
    assert p.state is State.FINISHED


def test_cancel_at_the_model_check_counts_no_job():
    sim, p = make()
    p.upload("s.gcode", JOB.replace("COREONEL", "MK4S"), print_after=True, overwrite=True)
    p.cancel()
    assert p.job is None and p.state is State.IDLE and p.job_counter == 0


def test_model_check_accepts_mmu3_variant_and_auto_accept():
    sim, p = make()
    run_sync(sim, p, JOB.replace("COREONEL", "COREONELMMU3"))
    assert p.state is State.PRINTING
    sim, p = make(auto_accept_model_check=True)
    run_sync(sim, p, JOB.replace("COREONEL", "MK4S"))
    assert p.state is State.PRINTING
    sim, p = make()
    p.set_auto_accept(True)
    run_sync(sim, p, JOB.replace("COREONEL", "MK4S"))
    assert p.state is State.PRINTING


def test_parse_error_holds_and_tap_through_stops():
    sim, p = make()
    run_sync(sim, p, 'M865 X N"WAYTOOLONG" L0\n')
    assert p.state is State.ATTENTION and str(p.job.attention).startswith("parse error: line 1")
    p.tap_through()
    sim.tick(2)
    assert p.state is State.STOPPED


def test_m865_on_missing_tool_is_a_parse_error():
    sim, p = make()
    run_sync(sim, p, 'M865 X R N"PLA" L3\n')
    assert str(p.job.attention).startswith("parse error")


def test_m865_writes_disabled_and_unconfigured_tools():
    sim, p = make("8.10.0", [loaded(0, PETG), SimTool(1, enabled=False)])
    run_sync(sim, p, 'M865 X R N"PLA" L1\nM865 X R N"ASA" L5\n')
    assert p.job.phase is JobPhase.RUN
    assert p.tools[1].material.name == "PLA" and not p.tools[1].enabled
    assert p.tools[5].material.name == "ASA" and not p.tools[5].enabled
    assert [t["index"] for t in prusalink.filament(p)["tools"]] == [0]
    sim, p = make("8.10.0", [loaded(0, PETG)])
    run_sync(sim, p, 'M865 X R N"PLA" L8\n')
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
    run_sync(sim, p, 'M865 X R N"PLA" L0\n')
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
    sim.tick(20)
    # T1 was never unloaded, so its gap is 0 and every forward mm counts: the 15 mm slow
    # load, the 25 mm fast load, the cleaner's 25 mm purge and the 4 mm prime, times
    # 550/576. The 8 mm retract after the prime only opens the gap.
    assert p.tools[1].extruded == pytest.approx(69 * 550 / 576)
    assert p.tools[1].gap == pytest.approx(8)
    p.resume()  # the unpark feeds the 8 mm back, then the tool change purges 30
    assert p.tools[1].extruded == pytest.approx((69 + 30) * 550 / 576)
    sim.tick(10)
    assert p.state is State.FINISHED
    assert p.tools[1].extruded == pytest.approx((69 + 30 + 10) * 550 / 576)


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
    assert p.job is None and p.state is State.STOPPED and tool0(p).extruded == 0


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
