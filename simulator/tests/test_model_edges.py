"""Refusals, validation and rarely taken paths of the printer model."""

from __future__ import annotations

import json

import pytest

from simulator import gcode
from simulator.model import (
    PHASE_STATES,
    Attention,
    Busy,
    Invalid,
    JobPhase,
    Material,
    PrintSpec,
    Reason,
    Segment,
    Settings,
    SimTool,
    Simulation,
    State,
    SyncJob,
)
from simulator.tests.test_model import one_segment, run_sync, tool0
from simulator.tests.util import PLA, loaded, make, run

TAG = "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"


def messages(sim: Simulation, kind: str = "gcode") -> list[str]:
    return [e.text for e in sim.events if e.kind == kind]


def printing(**kw):
    """A printer 1 s into a 100 mm print on tool 0, with the preheat skipped."""
    sim, p = make(**kw)
    p.start_print(one_segment(100, preheat_s=0))
    sim.tick(0.8 + 1)
    assert p.state is State.PRINTING
    return sim, p


# Unloads that count nothing, and M865 edge cases.


def test_unload_of_an_empty_type_is_not_counted():
    sim, p = make(tools=[SimTool(0)])
    p.apply(gcode.parse("M865 I1 L0"))  # copies "none" over nothing
    assert tool0(p).unload_count == 0
    assert messages(sim, "unload") == ["T0 M865 copied empty T1: no type stored, not counted"]


def test_m865_for_a_tool_the_model_lacks_is_dropped():
    sim, p = make()
    p.apply(gcode.parse('M865 X R N"PLA" L4'))
    assert messages(sim) == ["M865 L4 dropped: no such tool"]
    assert list(p.tools) == [0]


def test_m865_with_nothing_to_write_changes_nothing():
    sim, p = make(tools=[SimTool(0)])
    before = p.to_json()
    p.apply(gcode.parse("M865 L0"))
    assert messages(sim) == ["M865 L0: nothing to write"]
    p.apply(gcode.parse('M865 O"#000000" L0'))  # an edit needs a stored type
    assert messages(sim)[-1] == "M865 L0: nothing to write"
    assert p.to_json() == before


def test_m865_copy_onto_tool_0_of_a_tag_model_keeps_the_uuids():
    sim, p = make("8.1.0", [SimTool(0)])
    p.apply(gcode.parse('M865 X R N"PETG" O"#186bb5" L0'))
    p.apply(gcode.parse(f'M865.1 I0 C"{TAG}"'))
    uuids = tool0(p).material.instance_uuid
    assert uuids == TAG
    p.apply(gcode.parse("M865 I0 L0"))
    assert tool0(p).material.instance_uuid == TAG
    assert "copied from T0" in messages(sim)[-1]


def test_m865_copy_onto_another_tool_drops_the_uuids():
    tagged = Material("PETG", "#186bb5", TAG, True, material_uuid=TAG, package_uuid=TAG)
    sim, p = make("1.4.1", [loaded(0, tagged)])
    p.tools[1] = SimTool(1, enabled=False)
    p.model = type(p.model)("x", "X", uuids=True, max_tools=2)  # only tool 0 holds UUIDs
    p.apply(gcode.parse("M865 I0 L1"))
    copy = p.tools[1].material
    assert (copy.name, copy.instance_uuid, copy.material_uuid, copy.package_uuid) == (
        "PETG",
        None,
        None,
        None,
    )


# Load, unload and manual moves.


def test_load_refusals():
    sim, p = make("1.4.1", [SimTool(0)])
    with pytest.raises(Invalid, match="needs a source"):
        p.load(0)
    with pytest.raises(Invalid, match="source must be tag or preset"):
        p.load(0, source="spool", name="PLA")
    with pytest.raises(Invalid, match="filament name"):
        p.load(0, source="preset", name="TOOLONGNAME")
    with pytest.raises(Invalid, match="UUID or temperatures"):
        p.load(0, source="preset", name="PLA", uuid=TAG)
    with pytest.raises(Invalid, match="UUID or temperatures"):
        p.load(0, source="preset", name="PLA", temperatures=[215, 170, 60])
    with pytest.raises(Invalid, match="has no tag reader"):
        p.load(0, source="tag", name="PLA")
    assert p.state is State.IDLE and tool0(p).process is None


def test_tag_load_validates_its_fields():
    sim, p = make("8.1.0", [SimTool(0)])
    with pytest.raises(Invalid, match="isn't a UUID"):
        p.load(0, source="tag", name="PLA", uuid="not-a-uuid")
    for bad in ([215, 170], [215, 170, 600], [215, 170, "60"], "215", [215, 170, -1]):
        with pytest.raises(Invalid, match="temperatures must be"):
            p.load(0, source="tag", name="PLA", temperatures=bad)
    p.load(0, source="tag", name="PLA", uuid="", temperatures=(200, 150, 50))
    sim.tick(100)
    m = tool0(p).material
    assert (m.nozzle, m.preheat, m.bed, m.instance_uuid) == (200, 150, 50, None)


def test_tag_load_on_another_tool_than_0_has_no_product_uuids():
    sim, p = make("8.1.0", [SimTool(0), SimTool(1)])
    p.load(1, source="tag", name="PETG", color="#186BB5", uuid=TAG)
    sim.tick(100)
    m = p.tools[1].material
    assert (m.name, m.color) == ("PETG", "#186bb5")
    assert m.instance_uuid is None and m.material_uuid is None and m.package_uuid is None


def test_a_load_while_one_is_running_is_refused():
    sim, p = make(tools=[SimTool(0)])
    p.load(0, source="preset", name="PLA")
    with pytest.raises(Busy, match="already running"):
        p.load(0, source="preset", name="PETG")
    sim.tick(100)
    p.unload(0)
    sim.tick(100)
    with pytest.raises(Invalid, match="feed_mm must be a positive number"):
        p.load(0, source="preset", name="PETG", feed_mm=0)
    with pytest.raises(Invalid, match="purge_mm must be a positive number"):
        p.load(0, source="preset", name="PETG", purge_mm=True)
    assert p.state is State.IDLE and tool0(p).process is None


def test_a_load_during_a_print_is_refused():
    sim, p = printing()
    with pytest.raises(Busy, match="can't load during a print"):
        p.load(0, source="preset", name="PETG")
    with pytest.raises(Busy, match="can't unload during a print"):
        p.unload(0)


def test_a_keep_load_outside_a_runout_is_refused_and_temperatures_need_a_tag():
    sim, p = make()
    with pytest.raises(Invalid, match="needs a source"):
        p.load(0)
    assert p.state is State.IDLE and tool0(p).process is None
    sim, p = printing()
    p.runout(0)
    with pytest.raises(Invalid, match="only a tag load takes temperatures"):
        p.load(0, temperatures=[215, 170, 60])


def test_loading_by_hand_while_off():
    sim, p = make(tools=[SimTool(0)])
    p.set_power("off")
    p.load(0, source="preset", name="PLA")
    assert tool0(p).loaded and tool0(p).material.name == "PLA"
    p.unload(0)
    assert not tool0(p).loaded and tool0(p).gap == p.model.gap_mm
    with pytest.raises(Invalid, match="no filament"):
        p.unload(0)


def test_loading_by_hand_while_off_during_a_process_is_refused():
    sim, p = make(tools=[SimTool(0)])
    p.load(0, source="preset", name="PLA")
    p.set_power("off")
    assert tool0(p).process is not None
    with pytest.raises(Busy, match="already running"):
        p.load(0, source="preset", name="PETG")
    sim2, q = make()
    q.unload(0)
    q.set_power("off")
    q.tools[0].loaded = True
    with pytest.raises(Busy, match="already running"):
        q.unload(0)


def test_a_runout_reload_by_hand_while_off_restores_the_kept_type():
    sim, p = printing()
    p.runout(0)
    p.set_power("off")
    p.load(0)
    assert tool0(p).loaded and tool0(p).gap == 0
    assert (tool0(p).material.name, tool0(p).material.instance_uuid) == ("PLA", PLA.instance_uuid)


def test_manual_moves_need_a_loaded_tool_and_a_positive_length():
    sim, p = make(tools=[SimTool(0)])
    with pytest.raises(Invalid, match="no filament"):
        p.extrude(0, 10)
    with pytest.raises(Invalid, match="mm must be a positive number"):
        p.extrude(0, -1)
    sim, p = make()
    with pytest.raises(Invalid, match="mm must be a positive number"):
        p.retract(0, "5")
    p.unload(0)
    with pytest.raises(Busy, match="manual moves need an idle printer"):
        p.extrude(0, 10)


def test_select_tool_refusals():
    sim, p = make()
    with pytest.raises(Invalid, match="has one tool"):
        p.select_tool(0)
    sim, p = make("3.1.0", [loaded(0), loaded(1)])
    with pytest.raises(Invalid, match="no tool 7"):
        p.select_tool(7)
    p.unload(0)
    with pytest.raises(Busy, match="can't pick a tool while"):
        p.select_tool(1)
    sim.tick(10)
    p.select_tool(1)
    assert p.selected_tool == 1


# Starting prints and the control actions that need one.


def test_start_print_validation():
    sim, p = make()
    with pytest.raises(Invalid, match="at least one segment"):
        p.start_print(PrintSpec([]))
    with pytest.raises(Invalid, match="no tool 3"):
        p.start_print(PrintSpec([Segment(3, 10)]))
    with pytest.raises(Invalid, match="slicer_mm must be a positive"):
        p.start_print(PrintSpec([Segment(0, 0)]))
    with pytest.raises(Invalid, match="duration_s must be a positive"):
        p.start_print(PrintSpec([Segment(0, 10)], duration_s=-5))
    with pytest.raises(Invalid, match="preheat_s can't be negative"):
        p.start_print(PrintSpec([Segment(0, 10)], preheat_s=-1))
    assert p.job is None and p.state is State.IDLE


def test_print_spec_from_dict():
    spec = PrintSpec.from_dict(
        {"segments": [{"tool": 1, "slicer_mm": 5}], "duration_s": 9, "color_changes_mm": [2]}
    )
    assert spec == PrintSpec([Segment(1, 5.0)], duration_s=9.0, color_changes_mm=[2.0])
    assert spec.name == "part.bgcode"
    for bad in ({}, {"segments": [{"tool": 0}]}, {"segments": [{"tool": "x", "slicer_mm": 1}]}):
        with pytest.raises(Invalid, match="bad print"):
            PrintSpec.from_dict(bad)


def test_runout_and_jam_need_a_print_with_a_loaded_selected_tool():
    sim, p = make()
    with pytest.raises(Busy, match="needs a print in progress"):
        p.runout(0)
    sim, p = printing()
    p.tools[0].loaded = False
    with pytest.raises(Busy, match="T0 has no filament"):
        p.jam(0)
    p.set_power("off")
    with pytest.raises(Busy, match="needs a print in progress"):
        p.runout(0)


def test_clear_jam_and_tap_through_without_anything_to_answer():
    sim, p = printing()
    with pytest.raises(Busy, match="no jam to clear"):
        p.clear_jam()
    p.runout(0)  # a runout isn't a jam
    with pytest.raises(Busy, match="no jam to clear"):
        p.clear_jam()
    with pytest.raises(Busy, match="nothing to tap through"):
        p.tap_through()


def test_extruded_at_unload_must_be_an_integer():
    sim, p = make()
    for bad in (1.5, "3", True):
        with pytest.raises(Invalid, match="value must be an integer"):
            p.set_extruded_at_unload(0, bad)
    p.set_extruded_at_unload(0, 7)
    assert tool0(p).extruded_at_unload == 7


def test_settings_update_validates_before_changing_anything():
    s = Settings()
    s.update({"start_blip": False, "unload_seconds": 2})
    assert (s.start_blip, s.unload_seconds) == (False, 2)
    for values, message in (
        ({"start_blip": 1}, "start_blip must be true or false"),
        ({"feed_ratio": True}, "feed_ratio must be a number of at least 0"),
        ({"feed_ratio": -1}, "feed_ratio must be a number of at least 0"),
        ({"feed_ratio": "x"}, "feed_ratio must be a number of at least 0"),
        ({"bogus": 1}, "unknown setting 'bogus'"),
        ({"time_scale": 0}, "time_scale must be greater than 0"),
        ({"load_rate_mm_s": 0}, "load_rate_mm_s must be greater than 0"),
        ({"feed_ratio": float("nan")}, "feed_ratio must be a number of at least 0"),
        ({"purge_mm": float("inf")}, "purge_mm must be a number of at least 0"),
    ):
        with pytest.raises(Invalid, match=message):
            s.update({"unload_seconds": 99, **values})
        assert s.unload_seconds == 2


def test_settings_construction_checks_every_field():
    Settings()
    for values, message in (
        ({"time_scale": 0}, "time_scale must be greater than 0"),
        ({"load_rate_mm_s": 0}, "load_rate_mm_s must be greater than 0"),
        ({"print_rate_mm_s": -1}, "print_rate_mm_s must be a number of at least 0"),
        ({"purge_mm": -1}, "purge_mm must be a number of at least 0"),
        ({"feed_ratio": float("nan")}, "feed_ratio must be a number of at least 0"),
        ({"time_scale": float("inf")}, "time_scale must be a number of at least 0"),
        ({"scan_seconds": float("-inf")}, "scan_seconds must be a number of at least 0"),
        ({"purge_mm": True}, "purge_mm must be a number of at least 0"),
        ({"purge_mm": 10**400}, "purge_mm must be a number of at least 0"),
        ({"start_blip": 1}, "start_blip must be true or false"),
    ):
        with pytest.raises(Invalid, match=message):
            Settings(**values)


# Print progress.


def test_consecutive_segments_on_one_tool_make_no_tool_change():
    sim, p = make()
    p.start_print(PrintSpec([Segment(0, 20), Segment(0, 20)], preheat_s=0))
    sim.advance(0.8 + 100)
    assert p.state is State.FINISHED
    assert not [t for t in messages(sim, "job") if "tool change" in t]
    assert p.selected_tool == 0


def test_a_tool_change_to_a_loaded_tool_purges():
    sim, p = make("3.1.0", [loaded(0), loaded(1)])
    p.start_print(PrintSpec([Segment(0, 20), Segment(1, 20)], preheat_s=0))
    sim.advance(0.8 + 100)
    assert p.state is State.FINISHED
    assert "tool change T0 -> T1" in messages(sim, "job")
    assert p.tools[1].extruded > 20 * 0.95


def test_a_print_paused_while_preheating_resumes_preheating():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=5))
    sim.tick(0.8 + 1)
    assert p.job.phase is JobPhase.PREHEAT
    p.pause()
    assert p.state is State.PAUSED and p.job.paused_from is JobPhase.PREHEAT
    p.resume()
    assert p.state is State.PRINTING and p.job.phase is JobPhase.PREHEAT


def test_power_panic_resume_runs_through_recover_and_unpark():
    sim, p = printing()
    p.reboot(keep_job=True)
    assert p.state is State.BUSY and p.job.phase is JobPhase.RECOVER
    sim.tick(p.settings.recover_seconds + 0.01)
    assert p.state is State.PAUSED and p.job.phase is JobPhase.UNPARK
    sim.tick(p.settings.unpark_seconds + 0.01)
    assert p.state is State.PRINTING


def test_power_panic_with_a_cold_bed_waits_for_resume():
    sim, p = printing()
    p.reboot(keep_job=True, cold_bed=True)
    sim.tick(p.settings.recover_seconds + 0.01)
    assert p.state is State.ATTENTION and p.job.phase is JobPhase.AWAIT_RESUME
    p.resume()
    assert p.state is State.BUSY and p.job.phase is JobPhase.RECOVER
    sim.advance(p.settings.recover_seconds + p.settings.unpark_seconds + 0.2)
    assert p.state is State.PRINTING


# Reporting and snapshots.


def test_to_json_reports_an_unload_in_progress():
    sim, p = make()
    p.unload(0)
    sim.tick(1)
    process = p.to_json()["tools"][0]["process"]
    assert process["kind"] == "unload" and process["phase"] == "UNLOAD"
    assert process["remaining_s"] == pytest.approx(p.settings.unload_seconds - 1)


def test_snapshot_mid_unload_finishes_the_unload():
    sim, p = make()
    p.unload(0)
    sim.tick(1)
    other = Simulation()
    other.restore(json.loads(json.dumps(sim.snapshot())))
    q = other.printers["p"]
    assert q.tools[0].process is not None and q.state is State.BUSY
    other.tick(10)
    assert q.state is State.IDLE and not q.tools[0].loaded and q.tools[0].unload_count == 1


def test_snapshot_while_paused_keeps_where_the_print_paused_from():
    sim, p = printing()
    p.pause()
    other = Simulation()
    other.restore(json.loads(json.dumps(sim.snapshot())))
    q = other.printers["p"]
    assert q.job.phase is JobPhase.PAUSED and q.job.paused_from is JobPhase.PRINTING
    q.resume()
    other.tick(1000)
    assert q.state is State.FINISHED


def test_snapshot_keeps_a_sync_job_a_sync_job():
    sim, p = make()
    run_sync(sim, p)
    assert isinstance(p.job, SyncJob)
    other = Simulation()
    other.restore(json.loads(json.dumps(sim.snapshot())))
    q = other.printers["p"]
    assert q.job == p.job and isinstance(q.job, SyncJob)
    other.tick(100)
    assert q.job is None and q.state is State.FINISHED
    assert q.tools[0].material.name == "PETG"


def test_restore_rejects_a_malformed_snapshot():
    sim, _ = make()
    with pytest.raises(Invalid, match="bad snapshot"):
        sim.restore({"printers": []})


def test_restore_rejects_bad_settings_and_changes_nothing():
    sim, _ = make()
    snap = json.loads(json.dumps(sim.snapshot()))
    snap["settings"]["load_rate_mm_s"] = 0
    sim.settings.time_scale = 7
    before = json.dumps(sim.snapshot(), sort_keys=True)
    with pytest.raises(Invalid, match="bad snapshot: load_rate_mm_s must be greater than 0"):
        sim.restore(snap)
    assert json.dumps(sim.snapshot(), sort_keys=True) == before


def test_select_tool_while_off_changes_the_pick_without_touching_the_sensor():
    sim, p = make("3.1.0", [loaded(0), loaded(1, sensor_fault=True)])
    p.set_power("off")
    p.select_tool(1)
    assert p.selected_tool == 1
    assert p.tools[1].unload_count == 0 and p.tools[1].material == PLA


def test_select_tool_while_on_counts_a_faulty_sensor_as_an_unload():
    sim, p = make("3.1.0", [loaded(0), loaded(1, sensor_fault=True)])
    p.select_tool(1)
    assert p.selected_tool == 1
    assert p.tools[1].unload_count == 1 and p.tools[1].material is None


def test_simulation_takes_printers_at_construction():
    _, p = make()
    sim = Simulation(printers=[p])
    assert sim.printer("p") is p and p._sim is sim


# The state follows from the phase.


def test_every_phase_has_a_state():
    assert set(PHASE_STATES) == set(JobPhase)


@pytest.mark.parametrize(
    ("phase", "attention"),
    [
        (JobPhase.PRINTING, Attention(Reason.RUNOUT, 0)),
        (JobPhase.WAIT_LOAD, None),
        (JobPhase.WAIT_LOAD, Attention(Reason.MODEL_CHECK)),
        (JobPhase.AWAIT_RESUME, Attention(Reason.JAM, 0)),
    ],
)
def test_a_phase_takes_only_its_own_reasons(phase, attention):
    job = SyncJob(name="x", gcode="")
    with pytest.raises(ValueError, match="can't wait"):
        job.enter(phase, attention)
    assert job.phase is JobPhase.SCANNING and job.attention is None


def test_leaving_a_wait_clears_its_reason():
    sim, p = printing()
    p.runout(0)
    assert p.job.wait_tool == 0
    p.load(0)
    run(sim, 20)
    assert p.job.phase is JobPhase.PAUSED and p.job.attention is None
    assert p.job.wait_tool is None


def test_each_state_change_is_logged_once():
    sim, p = make()
    p.unload(0)
    run(sim, 3.1)
    p.screen = State.FINISHED  # a screen set directly isn't logged until the next change
    p.load(0, source="preset", name="PLA")
    run(sim, 20)
    changes = [e.text for e in sim.events if e.kind == "state"]
    assert changes == ["IDLE -> BUSY", "BUSY -> IDLE", "IDLE -> BUSY", "BUSY -> IDLE"]


@pytest.mark.parametrize(
    ("attention", "text"),
    [
        (Attention(Reason.MODEL_CHECK), "model check"),
        (Attention(Reason.PARSE_ERROR, detail="line 1: bad"), "parse error: line 1: bad"),
        (Attention(Reason.JAM, 1), "jam tool 1"),
        (Attention(Reason.RUNOUT, 0), "runout tool 0"),
        (Attention(Reason.COLOR_CHANGE, 2), "color change tool 2"),
        (Attention(Reason.NO_FILAMENT, 3), "no filament in tool 3"),
        (Attention(Reason.COLD_BED), "bed cold after the power panic"),
    ],
)
def test_the_screen_text_for_each_reason(attention, text):
    assert str(attention) == text


def test_a_snapshot_from_an_older_simulator_is_refused():
    sim, p = make()
    data = json.loads(json.dumps(sim.snapshot()))
    del data["printers"][0]["tools"][0]["kept_type"]  # older snapshots had no kept type
    with pytest.raises(Invalid, match="bad snapshot"):
        Simulation().restore(data)


def test_load_logs_name_the_type_the_tool_holds():
    """A runout reload by hand logs the kept type, and a load logs the color as stored."""
    sim, p = printing()
    p.runout(0)
    p.set_power("off")
    p.load(0)
    assert (
        messages(sim, "load")[-1]
        == f"T0 loaded by hand while off: PLA {PLA.color} {PLA.instance_uuid}"
    )
    p.set_power("on")
    p.unload(0)
    run(sim, 10)
    p.load(0, source="preset", name="PETG", color="#73767d")
    run(sim, 20)
    assert "T0 type set to PETG #73737b" in messages(sim, "load")


def test_a_tool_can_be_disabled_during_a_sync_job():
    """A sync job has no segments, so only the selected tool is in use."""
    sim, p = make("3.1.0", [loaded(0), loaded(1), loaded(2)])
    p.upload("x.gcode", 'M862.3 P"MK4"\nM117 x\n', print_after=True, overwrite=False)
    assert p.job.waits_for(Reason.MODEL_CHECK)
    p.set_tool_enabled(2, False)
    assert not p.tools[2].enabled
    with pytest.raises(Invalid, match="T0 is in use"):
        p.set_tool_enabled(0, False)


def test_a_reload_by_hand_with_no_type_to_restore_still_loads_the_tool():
    """A runout on a tool that never had a type: the hand reload has nothing to restore."""
    sim, p = printing()
    tool = p.tools[0]
    tool.material = tool.previous = None  # no type was ever stored
    p.runout(0)
    assert tool.kept() is None
    p.set_power("off")
    p.load(0)
    assert tool.loaded and tool.material is None
