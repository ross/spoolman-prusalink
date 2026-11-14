"""Where a toolchanger's picked tool rests after a print, a pause and a boot (F32).

A finished print's end G-code parks the tool on both toolchangers (`P0 S1` in the stock
profiles), so the firmware's end retracts and auto-retracts nothing. A stopped XL print
keeps its tool; INDX docks it after the end (`pre_finalize_print`). INDX docks at a pause
and the resume picks the tool again and purges it in the nozzle cleaner. A boot keeps the
picked tool, except on INDX after a cut during a print that used several tools.
"""

from __future__ import annotations

import pytest

from simulator.model import JobPhase, PrintSpec, Segment, SimTool, State
from simulator.tests.util import loaded, make, sliced

XL, INDX = "3.1.0", "8.10.0"
RATIO = {XL: 0.95, INDX: 550 / 576}
TOOLCHANGERS = pytest.mark.parametrize("model", [XL, INDX])


def counts(p) -> list[float]:
    """Each tool's counter in commanded mm."""
    return [t.extruded / RATIO[p.model.code] for t in p.tools.values()]


def print_spec(*segments: tuple[int, float]) -> PrintSpec:
    return PrintSpec([Segment(t, mm) for t, mm in segments], preheat_s=0)


def start(sim, p, spec: PrintSpec) -> None:
    """Start `spec` and run it to PRINTING, through the tools-mapping screen."""
    p.start_print(spec)
    sim.tick(0.5)
    p.tap_through()  # Print on the tools-mapping screen
    sim.tick(0.31)
    assert p.job.phase is JobPhase.PRINTING


# A print's end.


@TOOLCHANGERS
def test_a_finished_print_parks_its_tool_first_so_the_end_moves_nothing(model):
    sim, p = make(model, [loaded(0), loaded(1)])
    start(sim, p, print_spec((0, 10), (1, 10)))
    sim.advance(10)
    assert p.state is State.FINISHED and p.selected_tool is None
    # T1: the pickup's 30 mm purge and 10 printed, with no end retract or auto-retract.
    assert counts(p) == pytest.approx([10, 40])
    assert p.tools[1].path.gap == 0 and p.tools[1].path.auto_retracted == 0
    assert "end G-code parks T1" in [e.text for e in sim.events]


def test_a_stopped_xl_print_keeps_its_tool_and_the_end_retracts_and_auto_retracts():
    sim, p = make(XL, [loaded(0), loaded(1)])
    start(sim, p, print_spec((0, 10), (1, 100)))
    sim.advance(2.5)  # T1 picked, partway through
    before = counts(p)[1]
    p.cancel()
    assert p.selected_tool == 1
    # The end retracts to 8, then the auto-retract feeds the 8 back and counts 23.
    assert counts(p)[1] - before == pytest.approx(23)
    assert p.tools[1].path.auto_retracted == 55
    sim.advance(5)
    assert p.state is State.STOPPED and p.selected_tool == 1


def test_a_stopped_indx_print_retracts_and_docks_its_tool():
    sim, p = make(INDX, [loaded(0), loaded(1)])
    start(sim, p, print_spec((0, 10), (1, 100)))
    sim.advance(2.5)
    before = counts(p)[1]
    p.cancel()
    # The 8 mm retract counts as fully retracted, so no auto-retract; then the dock.
    assert p.selected_tool is None and counts(p)[1] == pytest.approx(before)
    assert p.tools[1].path.gap == 8 and p.tools[1].path.auto_retracted == 8
    assert "dock T1" in [e.text for e in sim.events]


def test_a_sync_job_keeps_the_xl_s_tool_and_auto_retracts_it():
    sim, p = make(XL, [loaded(0), loaded(1)])
    p.select_tool(1)
    p.upload("sync.gcode", sliced(p, "M117 hi\n"), print_after=True, overwrite=False)
    sim.advance(10)
    assert p.state is State.FINISHED and p.selected_tool == 1
    # From gap 0: the retract to 8, then the auto-retract feeds it back and counts 23.
    assert counts(p) == pytest.approx([0, 23])
    p.upload("sync2.gcode", sliced(p, "M117 again\n"), print_after=True, overwrite=False)
    sim.advance(10)
    # Fully retracted now: the second job's end moves nothing.
    assert p.selected_tool == 1 and counts(p) == pytest.approx([0, 23])


@pytest.mark.parametrize("picked", [None, 1])
def test_a_sync_job_on_indx_leaves_no_tool_picked(picked):
    sim, p = make(INDX, [loaded(0), loaded(1)])
    p.selected_tool = picked
    p.upload("sync.gcode", sliced(p, "M117 hi\n"), print_after=True, overwrite=False)
    sim.advance(10)
    assert p.state is State.FINISHED and p.selected_tool is None
    assert counts(p) == pytest.approx([0, 0])


# Pause and resume.


def test_the_xl_keeps_its_tool_through_a_pause():
    sim, p = make(XL, [loaded(0), loaded(1)])
    start(sim, p, print_spec((0, 100), (1, 10)))
    sim.advance(1)
    p.pause()
    assert p.selected_tool == 0
    before = counts(p)
    p.resume()
    assert counts(p) == before and p.state is State.PRINTING
    sim.advance(60)
    assert counts(p) == pytest.approx([100, 40])


def test_indx_docks_at_a_pause_and_the_resume_picks_and_purges():
    sim, p = make(INDX, [loaded(0), loaded(1)])
    start(sim, p, print_spec((0, 100), (1, 10)))
    sim.advance(1)
    before = counts(p)[0]
    p.pause()
    # The retract to 8 counts nothing, then the dock.
    assert p.state is State.PAUSED and p.selected_tool is None
    assert counts(p)[0] == pytest.approx(before) and p.tools[0].path.gap == 8
    p.resume()
    # The pick, then 25 mm: 8 refill the gap, 17 count. The auto-retract counts 8.
    assert p.selected_tool == 0 and counts(p)[0] == pytest.approx(before + 17 + 8)
    assert p.tools[0].path.auto_retracted == 8
    assert p.state is State.PRINTING
    texts = [e.text for e in sim.events]
    assert texts.index("dock T0") < texts.index("pick T0")
    sim.advance(60)
    # The print's feed takes the stored 8 back into the gap, and counts the rest.
    assert counts(p) == pytest.approx([100 + 25, 40])


def test_an_indx_pause_in_preheat_resumes_to_preheat_after_the_purge():
    sim, p = make(INDX, [loaded(0)])
    p.start_print(PrintSpec([Segment(0, 10)], preheat_s=5))
    sim.tick(0.8)
    assert p.job.phase is JobPhase.PREHEAT
    p.pause()
    assert p.selected_tool is None
    p.resume()
    assert p.job.phase is JobPhase.PREHEAT and p.selected_tool == 0
    assert counts(p) == pytest.approx([25])


def test_a_cancel_from_an_indx_pause_finds_no_tool_and_moves_nothing():
    sim, p = make(INDX, [loaded(0)])
    p.start_print(print_spec((0, 100)))
    sim.advance(2)
    p.pause()
    before = (counts(p), p.tools[0].path.gap)
    p.cancel()
    assert p.selected_tool is None and (counts(p), p.tools[0].path.gap) == before
    sim.advance(5)
    assert p.state is State.STOPPED


def test_the_resume_after_an_indx_runout_restores_the_gap_with_no_purge():
    sim, p = make(INDX, [loaded(0)])
    p.start_print(print_spec((0, 100)))
    sim.advance(2)
    p.runout(0)
    assert p.selected_tool == 0 and p.job.restore == (0, 0)
    p.load(0)  # keep
    sim.advance(60)
    before = counts(p)
    p.resume()
    assert p.job.restore is None and p.selected_tool == 0
    assert "T0 purged 25 mm at the resume" not in [e.text for e in sim.events]
    assert counts(p) == before


def test_a_snapshot_keeps_a_filament_change_s_restore():
    sim, p = make(INDX, [loaded(0)])
    p.start_print(print_spec((0, 100)))
    sim.advance(2)
    p.runout(0)
    sim.restore(sim.snapshot())
    assert sim.printer("p").job.restore == (0, 0)


# Boot.


@pytest.mark.parametrize(("model", "picked"), [(XL, 1), (INDX, 1), (INDX, None)])
def test_an_idle_reboot_keeps_the_picked_tool(model, picked):
    sim, p = make(model, [loaded(0), loaded(1)])
    p.selected_tool = picked
    p.reboot()
    assert p.selected_tool == picked


def test_a_cut_in_a_multi_tool_indx_print_boots_with_none_picked():
    sim, p = make(INDX, [loaded(0), loaded(1)], start_blip=False)
    start(sim, p, print_spec((0, 10), (1, 100)))
    sim.advance(2.5)
    assert p.selected_tool == 1
    p.set_power("off")
    p.unload(1)  # pulled out by hand while off: the type stays
    p.set_power("on", resume=True)
    # No tool, so no idle clear in the window: T1 keeps its type and counts no unload.
    assert p.selected_tool is None and p.job is None and p.resuming is not None
    sim.tick(p.settings.panic_idle_seconds - 0.1)
    assert p.tools[1].unload_count == 0 and p.tools[1].material is not None
    # The resume picks the job's tool before the nozzle clean purges on it.
    sim.tick(0.2)
    assert p.job.phase is JobPhase.RECOVER and p.selected_tool == 1
    sim.tick(p.settings.recover_seconds)
    assert counts(p)[1] == pytest.approx(25)


@pytest.mark.parametrize("model", [XL, INDX])
def test_a_cut_in_a_one_tool_print_keeps_the_tool_and_the_window_clears_it(model):
    sim, p = make(model, [loaded(0), loaded(1)])
    p.selected_tool = 1
    p.start_print(print_spec((1, 100)))
    sim.advance(2.5)
    p.set_power("off")
    p.unload(1)
    p.set_power("on", resume=True)
    assert p.selected_tool == 1
    assert p.tools[1].unload_count == 1 and p.tools[1].material is None


def test_a_cut_in_a_multi_tool_xl_print_keeps_the_tool_on_the_head():
    sim, p = make(XL, [loaded(0), loaded(1)])
    start(sim, p, print_spec((0, 10), (1, 100)))
    sim.advance(2.5)
    p.reboot(keep_job=True)
    assert p.selected_tool == 1


def test_an_indx_print_paused_then_cut_resumes_with_a_pick_and_a_purge():
    sim, p = make(INDX, [loaded(0), SimTool(1)])
    start(sim, p, print_spec((0, 100)))
    sim.advance(2)
    p.pause()
    p.reboot(keep_job=True)
    assert p.selected_tool is None
    sim.advance(10)
    # The resume's recovery sets the print's tool; the paused print moves nothing.
    assert p.state is State.PAUSED and p.selected_tool == 0 and counts(p) == [0, 0]
    p.resume()
    # The tracker boots at gap 0 and doesn't know the pause's 8 mm retract, so all 25
    # count, and the auto-retract's 8.
    assert counts(p)[0] == pytest.approx(25 + 8)
