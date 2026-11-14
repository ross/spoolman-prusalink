"""What a power cut and the power-panic resume count and show, per model.

A print that wasn't paused retracts 8 mm at the cut, except on the XL, which has a Dwarf.
The boot restarts the counters and leaves the tracker at gap 0. The printer then sits IDLE
with no job until the firmware's autostart resumes the print, at least 1.5 s later
(`print_utils_loop` in `src/common/print_utils.cpp`). Then the resume either feeds the 8 mm
back at the end of BUSY, or, on INDX, purges 25 mm in the nozzle cleaner halfway through
BUSY instead, with no auto-retract after (`src/common/power_panic.cpp`). From gap 0 each
feed counts in full, times the feed ratio: 380/400 on a Nextruder, 550/576 on INDX.
"""

from __future__ import annotations

import json

import pytest

from simulator import prusalink
from simulator.model import (
    Busy,
    Invalid,
    JobPhase,
    PrintSpec,
    Reason,
    Segment,
    Settings,
    SimPrinter,
    Simulation,
    State,
)
from simulator.tests.util import loaded, make, sliced

NEXTRUDER_RATIO = 380 / 400
INDX_RATIO = 550 / 576
FED_BACK = 8 * NEXTRUDER_RATIO
PURGED = 25 * INDX_RATIO
WINDOW = Settings().panic_idle_seconds
# A Core One L, an XL and a Core One L INDX: one model per feed system with power panic.
WINDOW_MODELS = ["8.1.0", "3.1.0", "8.10.0"]


def printing(model: str = "8.1.0") -> tuple[Simulation, SimPrinter]:
    """A printer 1 s into a print on tool 0, with the preheat skipped."""
    sim, p = make(model)
    p.start_print(PrintSpec([Segment(0, 100)], duration_s=10, preheat_s=0))
    sim.tick(0.8 + 1)
    assert p.state is State.PRINTING
    return sim, p


def job_log(sim: Simulation) -> list[str]:
    return [e.text for e in sim.events if e.kind == "job"]


def in_window(p: SimPrinter) -> bool:
    """Whether the printer shows what the window shows: IDLE, with no job."""
    return (
        p.state is State.IDLE
        and p.job is None
        and prusalink.status(p)["printer"]["state"] == "IDLE"
        and "job" not in prusalink.status(p)
        and prusalink.job(p) is None
    )


@pytest.mark.parametrize(
    ("model", "during", "after"),
    [
        ("1.4.0", 0.0, FED_BACK),  # MK4
        ("1.4.1", 0.0, FED_BACK),  # MK4S
        ("4.1.0", 0.0, FED_BACK),  # iX: its G12 only wipes
        ("7.1.0", 0.0, FED_BACK),  # Core One
        ("8.1.0", 0.0, FED_BACK),  # Core One L
        ("7.10.0", PURGED, PURGED),  # Core One INDX
        ("8.10.0", PURGED, PURGED),  # Core One L INDX
        ("3.1.0", 0.0, 0.0),  # XL: no retract, nothing to feed back
    ],
)
def test_the_resume_counts_per_model(model, during, after):
    sim, p = printing(model)
    p.reboot(keep_job=True)
    t = p.tools[0]
    assert in_window(p) and t.extruded == 0
    sim.tick(WINDOW)
    assert p.state is State.BUSY and t.extruded == 0
    sim.tick(p.settings.recover_seconds / 2 + 0.01)
    assert p.state is State.BUSY and t.extruded == pytest.approx(during)
    sim.tick(p.settings.recover_seconds / 2)
    assert p.state is State.PAUSED and t.extruded == pytest.approx(after)
    sim.tick(p.settings.unpark_seconds)
    assert p.state is State.PRINTING and t.extruded > after  # the print goes on


@pytest.mark.parametrize("model", WINDOW_MODELS)
def test_the_window_reads_idle_with_no_job_then_the_job_comes_back(model):
    sim, p = printing(model)
    job, boot = p.job.id, p.boot_id
    p.reboot(keep_job=True)
    assert p.boot_id != boot and in_window(p)
    assert p.to_json()["job"] is None and p.job_counter == job
    sim.tick(WINDOW - 0.01)
    assert in_window(p)
    sim.tick(0.01)
    assert p.state is State.BUSY and p.job.phase is JobPhase.RECOVER
    assert prusalink.job(p)["id"] == job and p.job_counter == job
    assert job_log(sim)[-1] == f"job {job} resumes after the power panic"


@pytest.mark.parametrize(
    ("model", "counted"), [("8.1.0", FED_BACK), ("3.1.0", 0.0), ("8.10.0", PURGED)]
)
def test_the_window_counts_an_unload_then_the_print_goes_on_empty(model, counted):
    # The filament is pulled out while the power is off. The window isn't printing, so the
    # idle sensor clear counts the unload and clears the type. The resume checks no sensor,
    # and a runout needs the sensor to go from filament to none, so the resume's feed back
    # or purge counts, and the print goes on through the empty tool with the counter
    # climbing (F29).
    sim, p = printing(model)
    p.set_power("off")
    p.unload(0)  # pulled out by hand while off
    p.set_power("on", resume=True)
    t = p.tools[0]
    assert in_window(p) and t.unload_count == 1 and t.material is None
    sim.tick(WINDOW + p.settings.recover_seconds + 0.01)
    assert p.state is State.PAUSED and t.extruded == pytest.approx(counted)
    sim.tick(p.settings.unpark_seconds)
    assert p.state is State.PRINTING
    climbed = t.extruded
    sim.tick(2)
    assert p.job.phase is JobPhase.PRINTING and t.extruded > climbed > 0
    assert (t.unload_count, t.material, t.path.loaded) == (1, None, False)


def test_the_clear_reads_the_picked_tool_only():
    # On an XL the window's clear reads the Dwarf on the head. Picking another tool in the
    # window runs the clear on that one, and the resume picks the print's tool again.
    sim, p = make("3.1.0", [loaded(0), loaded(1)])
    p.start_print(PrintSpec([Segment(0, 100)], duration_s=10, preheat_s=0))
    sim.tick(0.5)
    p.tap_through()  # Print on the tools-mapping screen
    sim.tick(0.3 + 1)
    p.set_power("off")
    p.unload(1)  # pulled out by hand while off
    p.set_power("on", resume=True)
    assert p.tools[1].unload_count == 0 and p.tools[1].material is not None
    p.select_tool(1)
    assert p.tools[1].unload_count == 1 and p.tools[1].material is None
    sim.tick(WINDOW + p.settings.recover_seconds + p.settings.unpark_seconds + 0.01)
    assert p.state is State.PRINTING and p.selected_tool == 0


@pytest.mark.parametrize("start", ["upload", "start_print"])
def test_a_print_started_in_the_window_drops_the_resume(start):
    sim, p = printing()
    p.reboot(keep_job=True)
    if start == "upload":
        p.upload("sync.gcode", sliced(p, "M117 hi\n"), print_after=True, overwrite=False)
    else:
        p.start_print(PrintSpec([Segment(0, 10)], preheat_s=0))
    assert p.job.id == 2 and p.resuming is None
    assert "job 1: resume dropped by the new job" in job_log(sim)
    sim.tick(60)
    assert p.job is None and p.state is State.FINISHED
    assert "job 1 resumes after the power panic" not in job_log(sim)


@pytest.mark.parametrize("operation", ["load", "unload"])
def test_a_menu_operation_in_the_window_delays_the_resume(operation):
    sim, p = printing()
    p.set_power("off")
    if operation == "load":
        p.unload(0)  # pulled out by hand while off, so the window's clear empties it
    p.set_power("on", resume=True)
    if operation == "load":
        p.load(0, source="preset", name="PETG")
    else:
        p.unload(0)
    assert p.state is State.BUSY and p.job is None
    sim.tick(WINDOW)
    assert p.state is State.BUSY and p.job is None and p.resuming is not None
    assert job_log(sim)[-1] == f"resume waits {WINDOW:g} s: the printer is BUSY"
    # The resume starts at the first try after the operation ends.
    t, ended = p.tools[0], None
    while p.job is None:
        sim.tick(0.1)
        if ended is None and t.process is None:
            ended = sim.now
    assert t.process is None and p.job.phase is JobPhase.RECOVER
    assert sim.now - ended <= WINDOW + 1e-6
    # The operation ran on the tool and the resume goes on as ever.
    assert t.path.loaded == (operation == "load")
    sim.tick(p.settings.recover_seconds + p.settings.unpark_seconds + 0.01)
    assert p.state is State.PRINTING


def test_a_cut_in_the_window_keeps_the_resume_and_a_reboot_drops_it():
    sim, p = printing()
    p.reboot(keep_job=True)
    sim.tick(WINDOW / 2)
    p.set_power("off")
    p.set_power("on", resume=True)  # the printer was idle: nothing retracts
    assert in_window(p) and p.resuming.id == 1
    sim.tick(WINDOW + p.settings.recover_seconds + 0.01)
    assert p.state is State.PAUSED and p.tools[0].extruded == pytest.approx(FED_BACK)

    sim, p = printing()
    p.reboot(keep_job=True)
    with pytest.raises(Busy, match="nothing to cancel"):
        p.cancel()
    p.reboot()
    assert in_window(p) and p.resuming is None
    assert job_log(sim)[-1] == "job 1: resume dropped by the reboot"
    sim.tick(60)
    assert in_window(p)

    sim, p = printing()
    p.reboot(keep_job=True)
    p.set_power("off")
    p.set_power("on")
    assert p.resuming is None


def test_the_job_s_tools_stay_in_use_in_the_window():
    sim, p = make("3.1.0", [loaded(0), loaded(1)])
    p.start_print(PrintSpec([Segment(0, 100), Segment(1, 100)], duration_s=10, preheat_s=0))
    sim.tick(0.5)
    p.tap_through()  # Print on the tools-mapping screen
    sim.tick(0.3 + 1)
    p.reboot(keep_job=True)
    with pytest.raises(Invalid, match="T1 is in use"):
        p.set_tool_enabled(1, False)


def test_a_snapshot_mid_window_resumes_the_same():
    sim, p = printing("8.10.0")
    p.reboot(keep_job=True)
    sim.tick(WINDOW / 3)
    data = json.loads(json.dumps(sim.snapshot()))
    other = Simulation()
    other.restore(data)
    copy = other.printers["p"]
    assert copy.resuming == p.resuming and copy.resume_in == pytest.approx(p.resume_in)
    copy.boot_id = p.boot_id  # a restore boots afresh
    for s in (sim, other):
        s.tick(WINDOW)
    assert copy.to_json() == p.to_json() and copy.job == p.job
    assert copy.state is State.BUSY and copy.job.id == 1


def test_indx_doesnt_auto_retract_after_the_purge():
    sim, p = printing("8.10.0")
    p.reboot(keep_job=True)
    sim.tick(WINDOW + p.settings.recover_seconds + 0.01)
    t = p.tools[0]
    # The purge leaves the filament at the nozzle, gap 0, with no distance stored.
    assert (t.path.gap, t.path.gap_valid, t.path.auto_retracted) == (0.0, True, None)
    assert job_log(sim)[-1:] == ["nozzle clean purged 25 mm"]


def test_the_cut_forgets_a_stored_auto_retract():
    sim, p = printing()
    p.tools[0].path.auto_retracted = 20.0
    p.reboot(keep_job=True)
    t = p.tools[0]
    # Kept, the boot would have set gap 20, and the 8 mm fed back would count nothing.
    assert (t.path.auto_retracted, t.path.gap, t.path.gap_valid) == (None, 0.0, False)
    sim.tick(WINDOW + p.settings.recover_seconds + 0.01)
    assert p.state is State.PAUSED and t.extruded == pytest.approx(FED_BACK)


@pytest.mark.parametrize("model", WINDOW_MODELS)
def test_a_cold_bed_waits_for_the_user_after_the_window(model):
    # A cold bed sends the resume straight to waiting, with no BUSY first
    # (`powerpanic_resume` in `src/common/marlin_server.cpp`).
    sim, p = printing(model)
    p.reboot(keep_job=True, cold_bed=True)
    assert in_window(p)
    sim.tick(WINDOW)
    assert p.job.phase is JobPhase.AWAIT_RESUME and p.state is State.ATTENTION
    assert p.tools[0].extruded == 0


def test_a_cold_bed_feeds_back_once():
    sim, p = printing()
    p.reboot(keep_job=True, cold_bed=True)
    sim.tick(WINDOW + 60)
    assert p.job.phase is JobPhase.AWAIT_RESUME and p.tools[0].extruded == 0
    p.resume()
    sim.tick(p.settings.recover_seconds + p.settings.unpark_seconds + 0.01)
    assert p.state is State.PRINTING
    assert job_log(sim).count("fed back 8 mm") == 1
    assert p.tools[0].extruded > FED_BACK  # and the print goes on


@pytest.mark.parametrize("model", WINDOW_MODELS)
def test_a_paused_print_moves_nothing_and_comes_back_paused(model):
    # The resume of a paused print reads BUSY while it homes and parks the head, then
    # PAUSED, and moves no filament (`ResumeState::ParkForPause` in
    # `src/common/power_panic.cpp`).
    sim, p = printing(model)
    p.pause()
    p.tools[0].path.auto_retracted = 20.0
    p.reboot(keep_job=True)
    t = p.tools[0]
    assert in_window(p) and t.extruded == 0 and t.path.auto_retracted == 20.0
    sim.tick(WINDOW)
    assert p.state is State.BUSY and p.job.phase is JobPhase.RECOVER
    sim.tick(p.settings.recover_seconds + 0.01)
    assert p.state is State.PAUSED and t.extruded == 0
    p.resume()
    assert p.state is State.PRINTING
    assert not any(s.startswith(("fed back", "nozzle clean")) for s in job_log(sim))


def test_a_paused_print_with_a_cold_bed_waits_for_the_user_first():
    """`auto_recover` doesn't look at whether the print was paused (`resume_print`)."""
    sim, p = printing()
    p.pause()
    p.reboot(keep_job=True, cold_bed=True)
    sim.tick(WINDOW)
    assert p.state is State.ATTENTION and p.job.phase is JobPhase.AWAIT_RESUME
    p.resume()
    assert p.state is State.BUSY
    sim.tick(p.settings.recover_seconds + 0.01)
    assert p.state is State.PAUSED and p.tools[0].extruded == 0
    assert not any(s.startswith(("fed back", "nozzle clean")) for s in job_log(sim))


@pytest.mark.parametrize("paused", [False, True])
def test_the_bed_decides_again_at_a_cut_in_the_window(paused):
    sim, p = printing()
    if paused:
        p.pause()
    # Warm, then cold: the resume waits for the user.
    p.reboot(keep_job=True)
    p.reboot(keep_job=True, cold_bed=True)
    sim.tick(WINDOW)
    assert p.state is State.ATTENTION and p.job.phase is JobPhase.AWAIT_RESUME

    sim, p = printing()
    if paused:
        p.pause()
    # Cold, then warm: it resumes on its own.
    p.reboot(keep_job=True, cold_bed=True)
    p.reboot(keep_job=True)
    sim.tick(WINDOW)
    assert p.state is State.BUSY and p.job.phase is JobPhase.RECOVER
    sim.tick(p.settings.recover_seconds + 0.01)
    assert p.job.phase is (JobPhase.PAUSED if paused else JobPhase.UNPARK)


# The MINI and the XL dev kit build no power panic (HAS_POWER_PANIC in
# `ProjectOptions.cmake`): a cut loses the print, and the printer boots idle.
NO_POWER_PANIC = ["2.1.0", "5.1.0"]


@pytest.mark.parametrize("cold_bed", [False, True])
@pytest.mark.parametrize("model", NO_POWER_PANIC)
def test_a_cut_without_power_panic_loses_the_print(model, cold_bed):
    sim, p = printing(model)
    _, plain = printing(model)
    plain.reboot()
    p.reboot(keep_job=True, cold_bed=cold_bed)
    t = p.tools[0]
    assert p.state is State.IDLE and p.job is None and p.resuming is None
    assert p.job_counter == plain.job_counter
    # Nothing retracts at the cut: the tool boots as a plain reboot leaves it.
    assert t.path == plain.tools[0].path
    assert (t.extruded, t.unload_count) == (0, 0)
    assert [e.text for e in sim.events if e.kind == "action"][-1] == (
        "power loss, print lost: no power panic"
    )
    sim.tick(WINDOW + p.settings.recover_seconds + p.settings.unpark_seconds + 10)
    assert p.state is State.IDLE and p.job is None and t.extruded == 0
    assert not any(s.startswith(("fed back", "nozzle clean")) for s in job_log(sim))


@pytest.mark.parametrize("model", [*NO_POWER_PANIC, *WINDOW_MODELS])
def test_the_idle_sensor_clear_runs_at_every_boot(model):
    # With power panic or without, the boot isn't printing, so the clear counts the tool
    # pulled out while off.
    sim, p = printing(model)
    p.set_power("off")
    p.unload(0)  # pulled out by hand while off
    p.set_power("on", resume=True)
    assert p.tools[0].unload_count == 1 and p.job is None


# A cut during a filament change dialog: a runout, a jam or a color change.
#
# The firmware's `was_paused` is its pause state, `print_state == Paused` (`printer_paused`
# in `marlin_server.cpp:1493-1495`, `ac_fault_isr` in `power_panic.cpp:983`). A filament
# change runs inside `M600` or `M1601` while the print state stays printing, or resuming
# when a runout or a jam interrupted a move, and never enters it (`Pause::filament_change`
# in `pause.cpp`). So the cut resumes the print as one cut while printing: through BUSY,
# with the retract at the cut and the feed back or the purge, then PRINTING with the dialog
# gone. The tool's type is what the cut left: none after the unload.


# What the resume counts on the picked tool, one model per feed system.
RESUME_COUNTS = pytest.mark.parametrize(
    ("model", "counted"), [("8.1.0", FED_BACK), ("3.1.0", 0.0), ("8.10.0", PURGED)]
)


def resumed(sim: Simulation, p: SimPrinter) -> None:
    """Run the window and the recovery to the end of BUSY."""
    sim.tick(WINDOW)
    assert p.state is State.BUSY and p.job.phase is JobPhase.RECOVER
    sim.tick(p.settings.recover_seconds + 0.01)
    assert p.state is State.PAUSED and p.job.phase is JobPhase.UNPARK


@RESUME_COUNTS
def test_a_cut_while_a_runout_waits_for_the_reload_resumes_on_the_empty_tool(model, counted):
    sim, p = printing(model)
    p.runout(0)
    t = p.tools[0]
    assert p.job.phase is JobPhase.WAIT_LOAD and t.unload_count == 1 and t.material is None
    p.reboot(keep_job=True)
    # The window's clear finds no type to clear on the empty tool.
    assert in_window(p) and (t.unload_count, t.material, t.path.loaded) == (0, None, False)
    resumed(sim, p)
    # The resume's feed back or purge counts from the boot's gap 0: 8 * 0.95 on a Core
    # One L, 25 * 550/576 on INDX, nothing on the XL.
    assert t.extruded == pytest.approx(counted)
    sim.tick(p.settings.unpark_seconds)
    assert p.state is State.PRINTING and prusalink.job(p)["state"] == "PRINTING"
    # The print goes on through the empty tool with no runout (F29): a runout needs the
    # sensor to see the filament go. Only the end of the print stops it.
    sim.tick(2)
    assert p.state is State.PRINTING and t.extruded > counted + 9
    assert (t.unload_count, t.material, t.path.loaded) == (0, None, False)
    with pytest.raises(Busy, match="can't load during a print"):
        p.load(0, source="preset", name="PLA")
    sim.tick(60)
    assert p.state is State.FINISHED


@pytest.mark.parametrize("typed", [False, True])
@RESUME_COUNTS
def test_a_cut_during_the_reload_leaves_the_filament_in_and_resumes(model, counted, typed):
    sim, p = printing(model)
    p.runout(0)
    p.load(0)  # the reload keeps the type
    t = p.tools[0]
    sim.tick(0.1)
    while typed and not t.process.typed:
        sim.tick(0.1)
    assert t.process.typed == typed
    p.reboot(keep_job=True)
    # The load stops where the cut finds it, with the filament in, so the window's clear
    # finds filament. The type is written at the purge's start: before it the tool has
    # none, after it the reload's.
    assert in_window(p) and t.path.loaded and t.unload_count == 0
    assert (t.material is not None) == typed
    resumed(sim, p)
    assert t.extruded == pytest.approx(counted)
    sim.tick(p.settings.unpark_seconds + 1)
    assert p.state is State.PRINTING and t.path.loaded and (t.material is not None) == typed


@pytest.mark.parametrize("cleared", [False, True])
@RESUME_COUNTS
def test_a_cut_at_a_jam_resumes_on_its_own(model, counted, cleared):
    # A jam cleared in place is the dialog's question, before or after the user answers,
    # not a pause.
    sim, p = printing(model)
    p.jam(0, unload=False)
    if cleared:
        p.clear_jam()
        assert p.state is State.PAUSED
    else:
        assert p.state is State.ATTENTION
    p.reboot(keep_job=True)
    assert in_window(p) and p.resuming.id == 1
    resumed(sim, p)
    assert p.tools[0].extruded == pytest.approx(counted)
    sim.tick(p.settings.unpark_seconds)
    assert p.state is State.PRINTING and p.tools[0].material is not None


def test_a_cut_during_a_color_change_in_the_file_runs_it_again():
    # The cut saves the file position of the command running, the `M600` itself, so the
    # resume replays it (`stop_and_save` in `crash_recovery.cpp`, `get_current_sdpos` in
    # `queue.h`).
    sim, p = make()
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0, color_changes_mm=[20]))
    sim.tick(0.8 + 5)
    t = p.tools[0]
    assert p.job.phase is JobPhase.WAIT_LOAD and p.job.waits_for(Reason.COLOR_CHANGE)
    p.reboot(keep_job=True)
    resumed(sim, p)
    assert t.extruded == pytest.approx(FED_BACK)
    sim.tick(p.settings.unpark_seconds)
    # Back at the change: the park retracts to 8 (gap 8), the runout ramming feeds the 8
    # back and counts its 7. The empty tool has no type, so the unload counts nothing.
    assert p.job.phase is JobPhase.WAIT_LOAD and str(p.job.attention) == "color change tool 0"
    assert p.job.fed_mm == 20 and t.unload_count == 0
    assert t.extruded == pytest.approx(FED_BACK + 7 * 0.95)
    p.load(0)
    sim.tick(30)
    p.resume()
    sim.tick(60)
    assert p.state is State.FINISHED
    assert [e.text for e in sim.events].count("color change at 20 mm") == 2


# A second cut during the resume.
#
# `resume_print` marks every cut from then on a nested one, and only `atomic_reset`, at
# `ResumeState::Finish`, clears the mark and erases the saved print (`power_panic.cpp:228-237,
# 269-279, 583`). A nested cut doesn't retract (`:732`), and it saves the first cut's flags,
# tool and file position again (`:946-951, 982-1106`), so the next boot resumes the print
# again as the first cut left it. That covers BUSY and a cold bed's wait. In UNPARK the
# resume has ended: the cut is an ordinary one, but the crash recovery unparks the head, so
# it counts as crashed and doesn't retract either (`marlin_server.cpp:2099-2106`,
# `power_panic.cpp:984`). Each resume runs its own feed back or purge: from the boot's gap 0,
# 8 * 0.95 = 7.6 on a Core One L, 25 * 550/576 = 23.87 on INDX, nothing on the XL.

RECOVER, UNPARK = Settings().recover_seconds, Settings().unpark_seconds
# Where the second cut falls, in seconds after the first boot, and whether the first
# resume's purge and feed back have run by then. INDX purges halfway through BUSY, and the
# others feed back at its end.
SECOND_CUTS = {
    "recover, before the purge": (WINDOW + RECOVER / 4, False, False),
    "recover, after the purge": (WINDOW + RECOVER * 3 / 4, True, False),
    "unpark": (WINDOW + RECOVER + UNPARK / 2, True, True),
}


def boot_gaps(monkeypatch, p: SimPrinter) -> list[float]:
    """Record tool 0's gap each time a boot restarts its tracker: what the cut left."""
    path = p.tools[0].path
    seen: list[float] = []
    boot = path.boot

    def record(limit: float) -> None:
        seen.append(path.gap)
        boot(limit)

    monkeypatch.setattr(path, "boot", record)
    return seen


@pytest.mark.parametrize("point", SECOND_CUTS)
@RESUME_COUNTS
def test_a_second_cut_during_the_resume_resumes_again_without_a_retract(
    monkeypatch, model, counted, point
):
    sim, p = printing(model)
    job = p.job.id
    p.reboot(keep_job=True)
    at, purged, fed_back = SECOND_CUTS[point]
    sim.tick(at)
    assert p.job.phase is (JobPhase.UNPARK if point == "unpark" else JobPhase.RECOVER)
    t = p.tools[0]
    gap = t.path.gap
    gaps = boot_gaps(monkeypatch, p)
    p.reboot(keep_job=True)
    # No retract: the boot finds the gap the resume left, where a retract would add 8.
    assert gaps == [gap]
    assert in_window(p) and p.resuming.id == job and t.extruded == 0
    resumed(sim, p)
    assert prusalink.job(p)["id"] == job and p.job_counter == job
    # The second resume's own feed back or purge, from the boot's gap 0.
    assert t.extruded == pytest.approx(counted)
    sim.tick(UNPARK)
    assert p.state is State.PRINTING and t.extruded > counted
    # The first resume's moves that ran before the cut, then the second resume's.
    log = job_log(sim)
    if model == "8.10.0":
        assert log.count("nozzle clean purged 25 mm") == 1 + purged
    elif model == "8.1.0":
        assert log.count("fed back 8 mm") == 1 + fed_back
    else:  # the XL retracts nothing and feeds nothing back
        assert not any(s.startswith(("fed back", "nozzle clean")) for s in log)


def test_a_cut_after_the_resume_ends_is_an_ordinary_one(monkeypatch):
    # PRINTING again: the cut retracts 8 mm, and the next resume feeds them back.
    sim, p = printing()
    p.reboot(keep_job=True)
    sim.tick(WINDOW + RECOVER + UNPARK + 1)
    assert p.state is State.PRINTING and p.tools[0].path.gap == 0
    gaps = boot_gaps(monkeypatch, p)
    p.reboot(keep_job=True)
    assert gaps == [8]
    resumed(sim, p)
    assert p.tools[0].extruded == pytest.approx(FED_BACK)
    assert job_log(sim).count("fed back 8 mm") == 2


def test_a_third_cut_resumes_as_the_second():
    # Every boot's resume marks its cuts nested, so a cut in the second resume's BUSY is as
    # the second cut was. No cut came after a feed back, so it runs once, at the last
    # resume: 7.6.
    sim, p = printing()
    for _ in range(3):
        p.reboot(keep_job=True)
        sim.tick(WINDOW + RECOVER / 2)
        assert p.state is State.BUSY and p.job.id == 1
    sim.tick(RECOVER / 2 + 0.01)
    assert p.job.phase is JobPhase.UNPARK
    assert p.tools[0].extruded == pytest.approx(FED_BACK)
    assert job_log(sim).count("fed back 8 mm") == 1


def test_a_second_cut_in_a_paused_print_s_resume_comes_back_paused(monkeypatch):
    # The nested cut keeps the first cut's `was_paused`, though the print state is then
    # `PowerPanic_Resume`, not `Paused`, so the print comes back paused with nothing moved.
    sim, p = printing()
    p.pause()
    p.reboot(keep_job=True)
    sim.tick(WINDOW + RECOVER / 2)
    assert p.state is State.BUSY
    gap = p.tools[0].path.gap
    gaps = boot_gaps(monkeypatch, p)
    p.reboot(keep_job=True)
    assert gaps == [gap]
    sim.tick(WINDOW)
    assert p.state is State.BUSY
    sim.tick(RECOVER + 0.01)
    assert p.state is State.PAUSED and p.job.phase is JobPhase.PAUSED
    assert p.tools[0].extruded == 0
    p.resume()
    assert p.state is State.PRINTING
    assert not any(s.startswith(("fed back", "nozzle clean")) for s in job_log(sim))


@pytest.mark.parametrize("model", WINDOW_MODELS)
def test_the_bed_decides_again_after_a_cut_during_the_resume(model):
    # Warm, then cold at the second boot: the resume waits for the user. A cut in that wait
    # is nested too: warm at the third boot, the print resumes on its own.
    sim, p = printing(model)
    p.reboot(keep_job=True)
    sim.tick(WINDOW + RECOVER / 4)
    p.reboot(keep_job=True, cold_bed=True)
    sim.tick(WINDOW)
    assert p.state is State.ATTENTION and p.job.phase is JobPhase.AWAIT_RESUME
    p.reboot(keep_job=True)
    assert in_window(p) and p.resuming.id == 1
    sim.tick(WINDOW)
    assert p.state is State.BUSY and p.job.phase is JobPhase.RECOVER
    sim.tick(RECOVER + UNPARK + 0.01)
    assert p.state is State.PRINTING


def test_a_cut_during_the_resume_after_a_cold_bed_s_wait_resumes_on_its_own():
    sim, p = printing()
    p.reboot(keep_job=True, cold_bed=True)
    sim.tick(WINDOW)
    p.resume()
    assert p.state is State.BUSY
    p.reboot(keep_job=True)
    sim.tick(WINDOW)
    assert p.state is State.BUSY and p.job.phase is JobPhase.RECOVER
    sim.tick(RECOVER + 0.01)
    assert p.tools[0].extruded == pytest.approx(FED_BACK)
    assert job_log(sim).count("fed back 8 mm") == 1


def test_a_second_cut_on_indx_boots_with_no_tool_and_the_resume_picks_it_again():
    # A print on T0 then T1, cut in T1's segment. Each boot finds the last picked tool
    # invalid, so neither window runs an idle sensor clear, and each resume picks T1 before
    # it purges on it: 23.87 after each boot.
    sim, p = make("8.10.0", [loaded(0), loaded(1)])
    p.start_print(PrintSpec([Segment(0, 10), Segment(1, 100)], duration_s=20, preheat_s=0))
    sim.tick(0.5)
    p.tap_through()  # Print on the tools-mapping screen
    while p.job.segment == 0 or p.job.phase is not JobPhase.PRINTING:
        sim.tick(0.1)
    p.reboot(keep_job=True)
    assert p.selected_tool is None
    sim.tick(WINDOW + RECOVER * 3 / 4)
    assert p.selected_tool == 1
    p.reboot(keep_job=True)
    assert p.selected_tool is None and in_window(p)
    resumed(sim, p)
    assert p.selected_tool == 1 and p.tools[1].extruded == pytest.approx(PURGED)
    assert p.tools[0].extruded == 0
    assert job_log(sim).count("nozzle clean purged 25 mm") == 2


def test_a_second_cut_keeps_the_xl_tool_the_first_cut_found():
    # The first cut comes while a filament change on T1 waits for the reload, so the resume
    # prints on T1 until the file's next tool change. The nested cut saves nothing new, so
    # the second resume prints on T1 too.
    sim, p = make("3.1.0", [loaded(0), loaded(1)])
    p.start_print(PrintSpec([Segment(0, 100)], duration_s=20, preheat_s=0))
    sim.tick(0.5)
    p.tap_through()  # Print on the tools-mapping screen
    sim.tick(0.3 + 1)
    p.change_filament(1)
    p.reboot(keep_job=True)
    assert p.resuming.resumed_on == 1
    sim.tick(WINDOW + RECOVER / 2)
    p.reboot(keep_job=True)
    assert p.resuming.resumed_on == 1 and p.selected_tool == 1
    sim.tick(WINDOW + RECOVER + UNPARK + 1)
    assert p.state is State.PRINTING and p.selected_tool == 1
    assert p.tools[1].extruded > 0 and p.tools[0].extruded == 0
