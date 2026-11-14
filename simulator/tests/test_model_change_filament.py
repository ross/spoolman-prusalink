"""A filament change during a print, `M600 T<n>`: Tune > Change filament (F8, F34), and the
status a load or unload reads in a print and while idle (F36).

The counts are worked by hand from the firmware's moves, with the gap before and after
each, as in `test_model_auto_retract.py`. The XL's gap stops at 40 mm and counts times
380/400 = 0.95; INDX's stops at 27 mm and counts times 550/576. A filament change parks
with a retract to 8 mm, rams with the `runout` sequence and unloads; the reload feeds the
slow and fast lengths, purges, primes 4 and retracts 8; the resume puts the gap back.
"""

from __future__ import annotations

import pytest

from simulator import prusalink
from simulator.model import (
    Busy,
    Invalid,
    JobPhase,
    Material,
    PrintSpec,
    Reason,
    Segment,
    SimTool,
    State,
)
from simulator.tests.util import PLA, loaded, make

XL, INDX = "3.1.0", "8.10.0"
RATIO = {XL: 0.95, INDX: 550 / 576}
PETG = Material("PETG", "#00ff00")


def counts(p) -> list[float]:
    """Each tool's counter in commanded mm."""
    return [t.extruded / RATIO[p.model.code] for t in p.tools.values()]


def start(sim, p, *segments: Segment) -> None:
    """Start a print and run it to PRINTING, answering each preview question."""
    p.start_print(PrintSpec(list(segments), preheat_s=0))
    sim.tick(0.5)
    while p.job.phase is JobPhase.HOLD:
        p.tap_through()
    sim.tick(0.31)
    assert p.job.phase is JobPhase.PRINTING


def status(p) -> str:
    return prusalink.status(p)["printer"]["state"]


# A change on another tool.


@pytest.mark.parametrize("high_flow", [False, True])
def test_a_change_on_another_xl_tool_counts_on_that_tool_and_changes_back(high_flow):
    sim, p = make(XL, [loaded(0), loaded(1, PETG)])
    p.high_flow = high_flow
    start(sim, p, Segment(0, 100))
    sim.advance(1)
    before = counts(p)[0]
    p.change_filament(1)
    # The pick is outside the dialog and moves no E; the dialog reads ATTENTION. T1 from
    # gap 0: the park's retract to 8 (gap 8); the runout ramming feeds the 8 back (gap 0),
    # then +7 counts 7 and -50 -5 -50 retract its 105 mm, so the unload's 105 has nothing
    # left. The unload counts at once.
    assert p.selected_tool == 1 and status(p) == "ATTENTION"
    assert str(p.job.attention) == "color change tool 1"
    assert p.tools[1].unload_count == 1 and p.tools[1].material is None
    assert counts(p) == pytest.approx([before, 7])
    assert p.job.restore == (1, 0)

    p.load(1)  # the reload keeps the type
    sim.advance(60)
    # From gap 40: 30 slow (gap 10); 50 fast, 40 count (gap 0); the 27 mm purge, doubled
    # on a high-flow nozzle; -8 (gap 8); the prime +4 (gap 4); -8 (gap 12).
    purge = 54 if high_flow else 27
    assert counts(p) == pytest.approx([before, 7 + 40 + purge])
    assert p.state is State.PAUSED and p.tools[1].path.gap == pytest.approx(12)
    assert p.tools[1].material.name == "PETG" and p.tools[1].material.color == "#00ff00"

    p.resume()
    # T1 back to the change's gap 0: 12 fed, nothing counts. Then the change back to T0,
    # with no purge.
    assert p.tools[1].path.gap == 0 and p.selected_tool == 0
    assert [e.text for e in sim.events if e.text.startswith("pick")][-2:] == ["pick T1", "pick T0"]
    assert counts(p) == pytest.approx([before, 7 + 40 + purge])
    assert p.state is State.PRINTING
    sim.advance(60)
    # T0 also counts 37 of the start's priming: -2 for the cleanup (gap 2), then 39 refill
    # the 2 and count 37, and the -0.8 +0.8 and the end's -0.8 count nothing. T1 gets
    # none of the profile's tool-change moves.
    assert counts(p) == pytest.approx([100 + 37, 7 + 40 + purge])
    assert not any("tool change" in e.text for e in sim.events)


def test_a_change_on_another_indx_tool_counts_on_that_tool_with_no_resume_purge():
    sim, p = make(INDX, [loaded(0), loaded(1, PETG)])
    start(sim, p, Segment(0, 100))
    sim.advance(1)
    before = counts(p)[0]
    p.change_filament(1)
    # T1 from gap 0: the park's retract to 8 (gap 8); the 2 mm unload purge (gap 6); the
    # ramming feeds the 6 back (gap 0) and counts 17, leaving 34 retracted and the gap
    # full at 27; the unload's other 21 move nothing counted.
    assert p.selected_tool == 1 and p.tools[1].unload_count == 1
    assert counts(p) == pytest.approx([before, 17])

    p.load(1)
    sim.advance(60)
    # From gap 27: 15 slow (gap 12); 25 fast, 13 count; the cleaner's 25; its
    # auto-retract +8 -8 +1 -1 counts 8 and stores 8 (gap 8); the prime feeds the 8 back
    # (gap 0) and counts 4; -8 (gap 8). The reload doesn't dock.
    assert counts(p) == pytest.approx([before, 17 + 13 + 25 + 8 + 4])
    assert p.selected_tool == 1 and p.tools[1].path.gap == pytest.approx(8)

    p.resume()
    # The restore feeds T1's 8 back (gap 0), counting nothing. The change back to T0
    # isn't a pause's resume, so T0 gets no cleaner purge.
    assert p.selected_tool == 0 and p.tools[1].path.gap == 0
    assert counts(p) == pytest.approx([before, 67])
    assert not any("purged 25 mm at the resume" in e.text for e in sim.events)
    sim.advance(60)
    # T0 also counts the start's priming: G427's cleaner purge, 25 then the auto-retract's
    # 8, then 1.6 + 5.4907 after the stored 8 comes back; its -0.8 +0.8 and the end's -0.8
    # count nothing. 67 * 550/576 = 63.98 on T1, with none of the profile's tool-change
    # moves.
    assert counts(p) == pytest.approx([100 + 25 + 8 + 1.6 + 5.4907, 67])
    assert not any("tool change" in e.text for e in sim.events)
    assert p.tools[1].extruded == pytest.approx(63.98, abs=0.01)


def test_a_change_on_the_picked_tool_is_the_runout_s_change():
    printers = [make(), make()]
    for sim, p in printers:
        p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
        sim.advance(2)
    (_, runout), (_, change) = printers
    runout.runout(0)
    change.change_filament(0)
    assert change.job.attention.reason is Reason.COLOR_CHANGE
    for attr in ("extruded", "unload_count", "material", "kept_type"):
        assert getattr(change.tools[0], attr) == getattr(runout.tools[0], attr), attr
    assert change.tools[0].path == runout.tools[0].path
    assert change.job.restore == runout.job.restore


def test_a_change_on_a_tool_with_no_type_to_heat_for_changes_nothing():
    sim, p = make(XL, [loaded(0), loaded(1), SimTool(2)])
    start(sim, p, Segment(0, 100))
    sim.advance(1)
    before = (counts(p), [t.unload_count for t in p.tools.values()])
    p.change_filament(2)
    # Its target is 0, so the change returns as too cold, and M600 changes back.
    assert p.state is State.PRINTING and p.selected_tool == 0
    assert (counts(p), [t.unload_count for t in p.tools.values()]) == before
    assert "T2 has no type to heat for: nothing changes" in [e.text for e in sim.events]


def test_a_change_on_a_tool_the_menu_unloaded_loads_back_the_type_it_had():
    sim, p = make(XL, [loaded(0), loaded(1, PETG)])
    p.unload(1)
    sim.advance(10)
    p.select_tool(0)
    start(sim, p, Segment(0, 100))
    sim.advance(1)
    p.change_filament(1)
    # No type stored, so the unload counts nothing; the reload writes the type the menu
    # unload left (`for_tool_heuristic`'s previous type).
    assert p.state is State.ATTENTION and p.tools[1].unload_count == 1
    p.load(1)
    sim.advance(60)
    assert p.tools[1].material.name == "PETG"


def test_a_change_on_an_untyped_tool_loads_the_type_the_file_names():
    sim, p = make(XL, [loaded(0), SimTool(1)])
    # The preview asks to turn the sensors off for the empty T1, then about its type.
    start(sim, p, Segment(0, 100, "PLA"), Segment(1, 10, "PETG"))
    sim.advance(1)
    p.change_filament(1)
    assert p.state is State.ATTENTION and p.tools[1].unload_count == 0
    p.load(1)
    sim.advance(60)
    material = p.tools[1].material
    assert (material.name, material.adhoc, material.nozzle) == ("PETG", False, 230)


# A power cut during a change on another tool. `M600_execute` picks tool n before the
# dialog and changes back only after it (`M600.cpp:222-236, 319`). The dialog isn't a pause,
# so the print resumes on its own (`printer_paused`, `power_panic.cpp:983`). The resume sets
# the tool the cut found picked, n: INDX saves it as the active tool, and the XL boots with
# n's Dwarf on the head (`power_panic.cpp:380-395, 1049-1055`). The resume issues no tool
# change, and the injected `M600` isn't in the file, so n prints until the file's next tool
# change. The counters restart at the boot, with every gap at 0 and untrusted, unless a
# stored auto-retract distance sets it.


def cut_during_the_change(sim, p, tool: int, point: str) -> None:
    """Cut while the change on `tool` waits for the reload, or once the reload has written
    the type."""
    p.change_filament(tool)
    if point == "load":
        p.load(tool)
        while not p.tools[tool].process.typed:
            sim.tick(0.1)
    p.reboot(keep_job=True)


@pytest.mark.parametrize("point", ["wait", "load"])
def test_a_cut_during_a_change_on_another_xl_tool_prints_on_it_to_the_next_change(point):
    sim, p = make(XL, [loaded(0), loaded(1, PETG)])
    start(sim, p, Segment(0, 50), Segment(1, 50))
    sim.advance(1)
    fed = p.job.fed_mm
    cut_during_the_change(sim, p, 1, point)
    assert p.resuming.resumed_on == 1 and p.selected_tool == 1
    sim.advance(p.settings.panic_idle_seconds + p.settings.recover_seconds + 0.01)
    # The XL neither retracts at the cut nor moves filament at the resume.
    assert p.job.phase is JobPhase.UNPARK and counts(p) == [0, 0]
    sim.advance(100)
    assert p.state is State.FINISHED
    # T1 prints the rest of T0's segment from gap 0, then the file's change to T1 finds it
    # picked and runs its moves on it: the ramming 4.1575 counts, -20 (gap 20), the first
    # pick's 20 refill the gap and its 9.238 count. Then T1's segment, and the tower's last
    # ramming 4.1575; the end's -0.8 counts nothing. T0 never moves again.
    t1 = 50 - fed + 4.1575 + 9.238 + 50 + 4.1575
    assert counts(p) == pytest.approx([0, t1])
    assert "tool change T1 -> T1" in [e.text for e in sim.events]
    # The reload stopped where it was: T1 holds filament once the type is written.
    assert p.tools[1].path.loaded == (point == "load")
    assert p.tools[1].material == (PETG if point == "load" else None)


@pytest.mark.parametrize("point", ["wait", "load"])
def test_a_cut_during_a_change_on_an_unused_indx_tool_prints_on_it_to_the_next_change(point):
    sim, p = make(INDX, [loaded(0), loaded(1), loaded(2, PETG)])
    start(sim, p, Segment(0, 50), Segment(1, 50))
    sim.advance(1)
    fed = p.job.fed_mm
    cut_during_the_change(sim, p, 2, point)
    # A print that used several tools boots with none picked (F32); the resume sets T2.
    assert p.resuming.resumed_on == 2 and p.selected_tool is None
    with pytest.raises(Invalid, match="T2 is in use"):
        p.set_tool_enabled(2, False)
    sim.advance(p.settings.panic_idle_seconds + p.settings.recover_seconds / 2 + 0.01)
    # The power-panic purge in the cleaner runs on T2, from gap 0: 25 count. The cut's
    # retract on T2 counted nothing.
    assert p.selected_tool == 2 and counts(p) == pytest.approx([0, 0, 25])
    sim.advance(100)
    assert p.state is State.FINISHED
    # T2 prints the rest of T0's segment. The file's change to T1 retracts T2 0.8 + 8. T1
    # boots at gap 8 from the auto-retract G427 stored, so its first pick feeds the 8 back
    # uncounted, then counts 1.6 + 4.1596 and its segment's 50. Its -0.8 +0.8 and the
    # end's -0.8 count nothing.
    assert counts(p) == pytest.approx([0, 1.6 + 4.1596 + 50, 25 + 50 - fed])
    assert p.tools[2].path.loaded == (point == "load")


def test_the_cut_s_retract_is_on_the_picked_tool():
    """The planner forgets a stored auto-retract distance on the tool it retracts, which is
    the one on the head, not the print's own."""
    sim, p = make(INDX, [loaded(0), loaded(1), loaded(2, PETG)])
    start(sim, p, Segment(0, 50), Segment(1, 50))
    sim.advance(1)
    p.change_filament(2)
    p.tools[2].path.auto_retracted = 20.0
    before = (p.tools[0].path.gap, p.tools[0].path.auto_retracted)
    p.reboot(keep_job=True)
    picked = p.tools[2].path
    assert (picked.auto_retracted, picked.gap, picked.gap_valid) == (None, 0.0, False)
    # T0 keeps what it stored: the boot sets its gap from that.
    assert p.tools[0].path.auto_retracted == before[1]


# Refusals.


def test_a_change_needs_a_print_that_has_begun_printing():
    sim, p = make(XL, [loaded(0), loaded(1)])
    with pytest.raises(Busy, match="needs a print in progress"):
        p.change_filament(1)
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=5))
    sim.tick(0.5)
    p.tap_through()  # Print on the tools-mapping screen
    sim.tick(0.31)
    assert p.job.phase is JobPhase.PREHEAT
    # The Tune menu's item is enabled once something is printed (`max_printed_z > 0`).
    with pytest.raises(Busy, match="needs a print in progress"):
        p.change_filament(1)
    sim.advance(6)
    p.pause()
    # The firmware offers the item while paused; the simulator doesn't model that.
    with pytest.raises(Busy, match="needs a print in progress"):
        p.change_filament(1)


def test_a_change_refuses_a_disabled_or_unknown_tool():
    sim, p = make(XL, [loaded(0), loaded(1, enabled=False)])
    start(sim, p, Segment(0, 100))
    with pytest.raises(Invalid, match="tool 1 is disabled"):
        p.change_filament(1)
    with pytest.raises(Invalid, match="has no tool 4"):
        p.change_filament(4)


# What the status reads.


def phases(sim, p) -> set[tuple[str, str]]:
    """Tick through the running load or unload; return each (process phase, status)."""
    seen = set()
    while any(t.process is not None for t in p.tools.values()):
        proc = next(t.process for t in p.tools.values() if t.process is not None)
        seen.add((proc.phase, status(p)))
        sim.tick(0.05)
    return seen


def test_a_load_or_unload_reads_attention_in_a_print_and_busy_while_idle():
    sim, p = make(INDX, [loaded(0), loaded(1, PLA)])
    start(sim, p, Segment(0, 100))
    sim.advance(1)
    assert status(p) == "PRINTING"
    p.change_filament(1)
    # The pick reads PRINTING, but it moves nothing and takes no time here. The unload
    # runs at once, in the dialog.
    assert status(p) == "ATTENTION"
    p.load(1)
    reload = phases(sim, p)
    assert {phase for phase, _ in reload} == {"SLOW", "FAST", "PURGE", "RAM", "PRIME", "RETRACT"}
    assert {state for _, state in reload} == {"ATTENTION"}
    assert status(p) == "PAUSED"
    p.resume()
    assert status(p) == "PRINTING"
    p.cancel()
    sim.advance(5)
    p.dismiss()

    p.unload(1)
    unload = phases(sim, p)
    assert {phase for phase, _ in unload} == {"PURGE", "RAM", "UNLOAD"}
    assert {state for _, state in unload} == {"BUSY"}
    p.load(1, source="preset", name="PETG")
    load = phases(sim, p)
    assert {phase for phase, _ in load} == {"SLOW", "FAST", "PURGE", "RAM"}
    assert {state for _, state in load} == {"BUSY"}
    assert status(p) == "IDLE"
