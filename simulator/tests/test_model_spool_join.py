"""A runout during a print with a spool join (F35), and without one (F8).

The counts are worked by hand with the gap before and after each move, as in
`test_model_slicer.py`, whose start, tool-change and end moves each print here also runs.
The XL's gap stops at 40 mm and counts times 380/400 = 0.95; INDX's stops at 27 mm and
counts times 550/576. Every tool starts loaded at gap 0 with nothing stored.

- The join's unload on the old tool, from gap 0, with no park retract: on the XL the
  `runout` ramming's +7 counts 7, and -50 -5 -50 retract the whole 105 mm unload. On INDX
  the 2 mm unload purge counts 2, and the ramming +8 +8 -20 -3 +3 +19 +2 counts 17 before
  -20 -3 +3 -6 -8 empty the gap: 19 in all.
- The join's purge on the new tool, from gap 0: on the XL 27 count, -8 and 8 stored; on
  INDX the cleaner's 25 count, then its auto-retract +8 -8 +1 -1 counts 8 and stores 8.
  Then the old tool's gap, 0, comes back on the new tool: the 8 fill the gap and count
  nothing.
"""

from __future__ import annotations

import json

import pytest

from simulator import prusalink
from simulator.model import (
    Busy,
    FilamentPath,
    Invalid,
    JobPhase,
    Join,
    Material,
    PrintSpec,
    Reason,
    Segment,
    SimTool,
    State,
)
from simulator.tests.test_model import reload_ends
from simulator.tests.util import loaded, make

XL, INDX = "3.1.0", "8.10.0"
RATIO = {XL: 0.95, INDX: 550 / 576, "5.1.0": 0.95}
JOIN_UNLOAD = {XL: 7, INDX: 19}
JOIN_PURGE = {XL: 27, INDX: 25 + 8}

XL_FIRST = 37  # the first tool's prime past its 2 mm retract
XL_OTHER = 39  # another used tool's prime
XL_RAM = 4.1575
INDX_START = 25 + 8  # G427's purge in the cleaner and its auto-retract
INDX_PRIME = 1.6 + 5.4907
INDX_PICK = 1.6 + 4.1596


def counts(p) -> list[float]:
    """Each tool's counter in commanded mm."""
    return [t.extruded / RATIO[p.model.code] for t in p.tools.values()]


def status(p) -> str:
    return prusalink.status(p)["printer"]["state"]


def start(sim, p, segments, joins, preheat_s=0) -> None:
    """Start a print with `joins` and run it to PRINTING, or to the end of `preheat_s`'s
    first tick, answering Print on the tools-mapping screen."""
    spec = PrintSpec([Segment(*s) for s in segments], preheat_s=preheat_s, spool_join=joins)
    p.start_print(spec)
    sim.tick(0.5)
    p.tap_through()
    sim.tick(0.31)


def run_join(sim, p) -> None:
    """Tick through the join's unload dialog."""
    sim.tick(sim.settings.unload_seconds)
    assert p.job.phase is JobPhase.PRINTING


@pytest.mark.parametrize("model", [XL, INDX])
def test_a_runout_joins_the_next_tool_for_the_rest_of_the_print(model):
    sim, p = make(model, [loaded(0), loaded(1), loaded(2)])
    start(sim, p, [(0, 30), (1, 30), (0, 30)], [Join(0, 2)])
    sim.advance(2)
    fed = p.job.fed_mm
    # XL: T0's prime counts 37, T1's 39 (and retracts 20). INDX: G427 purges T0 and T1,
    # 33 each, and T0 primes 1.6 + 5.4907.
    first, other = (XL_FIRST, XL_OTHER) if model == XL else (INDX_START + INDX_PRIME, INDX_START)
    assert status(p) == "PRINTING" and counts(p) == pytest.approx([first + fed, other, 0])

    p.runout(0)
    # The unload counts on T0 at once, in the dialog, which reads ATTENTION with the job.
    assert status(p) == "ATTENTION" and p.job.attention is None
    assert prusalink.status(p)["job"]["id"] == 1
    assert counts(p) == pytest.approx([first + fed + JOIN_UNLOAD[model], other, 0])
    assert [t.unload_count for t in p.tools.values()] == [1, 0, 0]
    assert p.tools[0].material is None and p.selected_tool == 0
    assert p.job.mapped == {0: 2}

    run_join(sim, p)
    # The change to T2 and its purge read PRINTING. T2's type isn't set or copied.
    assert status(p) == "PRINTING" and p.selected_tool == 2
    assert counts(p)[2] == pytest.approx(JOIN_PURGE[model] + p.job.fed_mm - fed)
    assert p.tools[2].material == loaded(2).material and p.tools[2].unload_count == 0

    sim.advance(100)
    assert p.state is State.FINISHED
    rest = 30 - fed
    if model == XL:
        # T2 takes the rest of the first segment and the third: it rams 4.1575 and retracts
        # 20 at the change to T1, feeds the 20 back at its later pick and purges 7.0076,
        # and rams the tower's last 4.1575 at the end. T1: its first pick feeds its 20
        # back and purges 9.238, then 30, then rams 4.1575 at the change back.
        expected = [
            first + fed + 7,
            other + 9.238 + 30 + XL_RAM,
            27 + rest + XL_RAM + 7.0076 + 30 + XL_RAM,
        ]
    else:
        # T2's park retracts 0.8 + 8, which its later pick's 1.6 + 8 + 0.8 refill, so it
        # counts as much as T1's first pick, which deretracts G427's stored 8 first.
        expected = [first + fed + 19, other + INDX_PICK + 30, 33 + rest + INDX_PICK + 30]
    assert counts(p) == pytest.approx(expected)
    # The firmware's change runs none of the profile's tool-change moves: only the print's
    # own changes do, between T2 and T1.
    changes = [e.text for e in sim.events if e.text.startswith("tool change")]
    assert changes == ["tool change T2 -> T1", "tool change T1 -> T2"]
    assert [t.unload_count for t in p.tools.values()] == [1, 0, 0]


@pytest.mark.parametrize("high_flow", [False, True])
def test_the_xl_join_s_purge_stores_its_retract_for_the_gap_restore(high_flow):
    sim, p = make(XL, [loaded(0), loaded(1)])
    p.high_flow = high_flow  # `purge_tool` never doubles its 27 mm
    start(sim, p, [(0, 100)], [Join(0, 1)])
    sim.advance(1)
    p.runout(0)
    # The tracker trusts T0's gap 0 when the join begins, and the join keeps it for T1.
    assert p.job.restore == (1, 0)
    sim.tick(sim.settings.unload_seconds - 0.01)
    assert p.tools[1].extruded == 0
    sim.tick(0.01)
    # 27 counted, -8 stored as the auto-retracted distance, then fed back to gap 0.
    assert p.tools[1].path.gap == 0 and p.tools[1].path.auto_retracted is None
    assert "T1 auto-retracted 8 mm: extruded 25" in [e.text for e in sim.events]


def test_the_xl_dev_kit_purges_with_no_retract():
    # The dev kit build has no auto-retract, so `purge_tool` doesn't retract after its 27.
    sim, p = make("5.1.0", [loaded(0), loaded(1)])
    start(sim, p, [(0, 100)], [Join(0, 1)])
    p.runout(0)
    run_join(sim, p)
    assert counts(p)[1] == pytest.approx(27) and p.tools[1].path.auto_retracted is None
    assert not any("auto-retracted" in e.text for e in sim.events)


def test_joins_chain_from_tool_to_tool():
    sim, p = make(XL, [loaded(0), loaded(1), loaded(2)])
    start(sim, p, [(0, 100)], [Join(0, 1), Join(1, 2)])
    sim.advance(2)
    first = p.job.fed_mm
    p.runout(0)
    run_join(sim, p)
    sim.advance(1)
    second = p.job.fed_mm
    p.runout(1)
    # T1 runs out in turn: its unload counts 7 and the print goes on with T2.
    assert status(p) == "ATTENTION" and p.job.mapped == {0: 2}
    run_join(sim, p)
    assert p.selected_tool == 2
    sim.advance(100)
    assert p.state is State.FINISHED
    assert counts(p) == pytest.approx(
        [XL_FIRST + first + 7, 27 + second - first + 7, 27 + 100 - second]
    )
    assert [t.unload_count for t in p.tools.values()] == [1, 1, 0]
    assert p.tools[0].material is None and p.tools[1].material is None


@pytest.mark.parametrize("model", [XL, INDX])
def test_a_runout_at_the_chain_s_end_is_a_filament_change(model):
    sim, p = make(model, [loaded(0), loaded(1)])
    start(sim, p, [(0, 100)], [Join(0, 1)])
    sim.advance(1)
    p.runout(0)
    run_join(sim, p)
    sim.advance(1)
    p.runout(1)
    # No join from T1: `M600` on T1, which waits for its reload.
    assert p.job.phase is JobPhase.WAIT_LOAD and status(p) == "ATTENTION"
    assert str(p.job.attention) == "runout tool 1" and p.job.mapped == {0: 1}
    assert p.tools[1].unload_count == 1 and p.tools[1].material is None
    p.load(1)
    reload_ends(sim, p, 1)
    # The change ends with its reload, and the print goes on with T1 picked.
    assert p.state is State.PRINTING and p.tools[1].material == loaded(1).material
    assert p.selected_tool == 1
    sim.advance(100)
    assert p.state is State.FINISHED
    assert [t.unload_count for t in p.tools.values()] == [1, 1]


def test_a_runout_with_joins_from_other_tools_only_is_a_filament_change():
    sim, p = make(INDX, [loaded(0), loaded(1), loaded(2)])
    start(sim, p, [(0, 100)], [Join(1, 2)])
    p.runout(0)
    assert p.job.phase is JobPhase.WAIT_LOAD and p.job.mapped == {}


def test_a_jam_never_joins():
    # A jam sends `M1601`, which runs the filament change with no `A`.
    sim, p = make(XL, [loaded(0), loaded(1)])
    start(sim, p, [(0, 100)], [Join(0, 1)])
    p.jam(0)
    assert p.job.phase is JobPhase.WAIT_LOAD and p.job.waits_for(Reason.JAM)
    assert p.job.mapped == {} and p.selected_tool == 0


def test_an_empty_join_tool_is_picked_purged_and_printed_on():
    sim, p = make(XL, [loaded(0), SimTool(1)])
    start(sim, p, [(0, 100)], [Join(0, 1)])
    sim.advance(1)
    fed = p.job.fed_mm
    p.runout(0)
    run_join(sim, p)
    # Nothing checks for filament: T1's purge and the rest of the print count with no
    # material (F29), and with no filament to see go, no runout can follow.
    with pytest.raises(Busy, match="T1 has no filament"):
        p.runout(1)
    sim.advance(100)
    assert p.state is State.FINISHED
    assert counts(p)[1] == pytest.approx(27 + 100 - fed)
    assert p.tools[1].material is None and p.tools[1].unload_count == 0


def test_a_join_in_the_preheat_primes_the_join_s_tool():
    # Inferred: the start G-code's tools go through the tool mapping the join set.
    sim, p = make(XL, [loaded(0), loaded(1), loaded(2)])
    start(sim, p, [(0, 10), (1, 10)], [Join(0, 2)], preheat_s=5)
    assert p.job.phase is JobPhase.PREHEAT and p.selected_tool == 0
    p.runout(0)
    run_join(sim, p)
    sim.advance(100)
    assert p.state is State.FINISHED
    # T2 takes T0's place from the start: the cleanup's 2 after the join's purge, then the
    # prime fills the 2 and counts 37.
    assert counts(p) == pytest.approx(
        [7, XL_OTHER + 9.238 + 10 + XL_RAM, 27 + XL_FIRST + 10 + XL_RAM]
    )
    assert "start G-code primed T1, T2" in [e.text for e in sim.events]


# What stops or saves a print around a join.


def test_a_join_can_t_be_paused_and_a_pause_after_it_rests_the_join_s_tool():
    sim, p = make(INDX, [loaded(0), loaded(1)])
    start(sim, p, [(0, 100)], [Join(0, 1)])
    p.runout(0)
    with pytest.raises(Busy, match="nothing to pause"):
        p.pause()
    run_join(sim, p)
    p.pause()
    assert p.selected_tool is None
    p.resume()
    # INDX's resume picks the print's tool, now T1, and purges it.
    assert p.selected_tool == 1
    assert "T1 purged 25 mm at the resume" in [e.text for e in sim.events]


def test_a_cancel_during_the_join_stops_before_the_change():
    sim, p = make(INDX, [loaded(0), loaded(1)])
    start(sim, p, [(0, 100)], [Join(0, 1)])
    p.runout(0)
    p.cancel()
    sim.advance(10)
    assert p.state is State.STOPPED and p.tools[1].extruded == 0
    assert p.tools[0].unload_count == 1 and p.selected_tool is None


def test_a_snapshot_during_the_join_keeps_it():
    sim, p = make(XL, [loaded(0), loaded(1)])
    start(sim, p, [(0, 100)], [Join(0, 1)])
    sim.advance(1)
    fed = p.job.fed_mm
    p.runout(0)
    sim.restore(json.loads(json.dumps(sim.snapshot())))
    p = sim.printer("p")
    job = p.job
    assert (job.phase, job.mapped, job.restore) == (JobPhase.JOIN, {0: 1}, (1, 0))
    assert job.spec.spool_join == [Join(0, 1)]
    sim.advance(100)
    assert p.state is State.FINISHED
    assert counts(p) == pytest.approx([XL_FIRST + fed + 7, 27 + 100 - fed])


@pytest.mark.parametrize("model", [XL, INDX])
def test_a_power_cut_during_the_join_resumes_on_the_old_tool(model):
    # The join maps T0 to T1 before its unload dialog, and changes to T1 only after it
    # (`do_join`, `spool_join.cpp:256-342`). The cut finds T0 picked: INDX saves it as the
    # active tool and the resume sets it, and the XL boots with T0's Dwarf on the head
    # (`power_panic.cpp:380-395, 1049-1055`). The resume issues no tool change and the
    # dialog is gone, so the join's change and purge never run, and the rest of the
    # segment prints on the empty T0 with no runout (F29). The mapping stays for the file's
    # next tool change to T0, which this print doesn't have.
    sim, p = make(model, [loaded(0), loaded(1)])
    start(sim, p, [(0, 100)], [Join(0, 1)])
    sim.advance(1)
    fed = p.job.fed_mm
    p.runout(0)
    p.reboot(keep_job=True)  # the counters restart at 0
    job = p.resuming
    assert (job.mapped, job.restore, job.resumed_on) == ({0: 1}, None, 0)
    sim.advance(100)
    assert p.state is State.FINISHED
    # INDX's resume purges 25 in the nozzle cleaner on T0, from the boot's gap 0, untrusted;
    # its retract at the cut counts nothing. The XL's resume moves no filament. The end's
    # 0.8 retract counts nothing either.
    assert counts(p) == pytest.approx([(25 if model == INDX else 0) + 100 - fed, 0])
    assert p.tools[0].unload_count == 0 and p.tools[0].material is None
    assert p.tools[1].material is not None


def test_after_a_cut_during_the_join_the_file_s_next_change_ends_the_old_tool():
    # T0 prints to the file's change to T2, and the join's mapping sends the file's T0 to T1
    # after it.
    sim, p = make(XL, [loaded(0), loaded(1), loaded(2)])
    start(sim, p, [(0, 50), (2, 50), (0, 50)], [Join(0, 1)])
    sim.advance(1)
    fed = p.job.fed_mm
    p.runout(0)
    p.reboot(keep_job=True)
    sim.advance(200)
    assert p.state is State.FINISHED
    # From the boot's gap 0, untrusted, on each tool. T0: the rest of its segment, then
    # the ramming 4.1575 and -20 at the change. T2: its first pick's 20 and 9.238, its
    # segment, the ramming and -20. T1: a later pick of the file's T0, 20 and 7.0076, the
    # segment, and the tower's last ramming; the end's -0.8 counts nothing.
    assert counts(p) == pytest.approx(
        [50 - fed + XL_RAM, 20 + 7.0076 + 50 + XL_RAM, 20 + 9.238 + 50 + XL_RAM]
    )
    changes = [e.text for e in sim.events if e.text.startswith("tool change")]
    assert changes == ["tool change T0 -> T2", "tool change T2 -> T1"]


def test_a_power_cut_during_a_join_in_the_preheat_primes_the_join_s_tool():
    # Inferred: the start G-code's tool changes, which the replay runs after the resume, go
    # through the join's mapping and end the old tool's turn.
    sim, p = make(XL, [loaded(0), loaded(1), loaded(2)])
    start(sim, p, [(0, 10), (1, 10)], [Join(0, 2)], preheat_s=5)
    p.runout(0)
    p.reboot(keep_job=True)
    assert p.resuming.resumed_on == 0
    sim.advance(100)
    assert p.state is State.FINISHED
    # As `test_a_join_in_the_preheat_primes_the_join_s_tool`, with no join purge and the
    # counters restarted before the start's moves: T2's cleanup -2 from gap 0, then its
    # prime refills the 2 and counts 37.
    assert counts(p) == pytest.approx([0, XL_OTHER + 9.238 + 10 + XL_RAM, XL_FIRST + 10 + XL_RAM])


def test_a_power_cut_after_the_join_resumes_on_the_join_s_tool():
    sim, p = make(INDX, [loaded(0), loaded(1)])
    start(sim, p, [(0, 100)], [Join(0, 1)])
    p.runout(0)
    run_join(sim, p)
    sim.advance(1)
    fed = p.job.fed_mm
    p.reboot(keep_job=True)
    sim.advance(100)
    assert p.state is State.FINISHED
    # The cut retracts T1 8, and the resume purges 25 on it.
    assert counts(p) == pytest.approx([0, 25 + 100 - fed])


@pytest.mark.parametrize(
    ("model", "tools", "segments", "joins", "error"),
    [
        ("8.1.0", [loaded(0)], [0], [Join(0, 1)], "a spool join needs a toolchanger"),
        (
            XL,
            [loaded(0), loaded(1, enabled=False)],
            [0],
            [Join(0, 1)],
            "a spool join needs a toolchanger",
        ),
        (XL, None, [0], [Join(0, 5)], "p has no tool 5"),
        (XL, None, [0], [Join(9, 1)], "p has no tool 9"),
        (XL, None, [0], [Join(0, 3)], "tool 3 is disabled"),
        (XL, None, [0], [Join(3, 1)], "tool 3 is disabled"),
        (XL, None, [0], [Join(0, 0)], "T0 can't join itself"),
        (XL, None, [0, 1], [Join(0, 1)], "T1 prints in this file, so it can't be joined to"),
        (XL, None, [0], [Join(0, 1), Join(0, 2)], "T0 joins on twice"),
        (XL, None, [0, 1], [Join(0, 2), Join(1, 2)], "T2 is joined to twice"),
        (INDX, None, [0], [Join(1, 2), Join(2, 1)], "the joins from T1 loop back to it"),
    ],
)
def test_a_bad_join_is_refused(model, tools, segments, joins, error):
    tools = tools or [loaded(0), loaded(1), loaded(2), loaded(3, enabled=False)]
    sim, p = make(model, tools)
    spec = PrintSpec([Segment(t, 10) for t in segments], spool_join=joins)
    with pytest.raises(Invalid, match=error):
        p.start_print(spec)
    assert p.job is None


def test_a_join_s_tool_is_in_use_for_the_print():
    sim, p = make(XL, [loaded(0), loaded(1), loaded(2)])
    start(sim, p, [(0, 100)], [Join(0, 1), Join(1, 2)])
    with pytest.raises(Invalid, match="T2 is in use"):
        p.set_tool_enabled(2, False)


def test_the_old_tool_s_gap_is_the_one_restored_on_the_join_s_tool():
    sim, p = make(XL, [loaded(0), loaded(1)])
    start(sim, p, [(0, 100)], [Join(0, 1)])
    sim.advance(1)
    p.tools[0].path.gap = 3.0
    p.runout(0)
    assert p.job.restore == (1, 3.0)
    run_join(sim, p)
    assert p.tools[1].path.gap == pytest.approx(3.0)


def test_the_file_s_type_follows_the_join_to_its_tool():
    """`for_tool_heuristic` finds the file's type through the tool mapping, which the join
    changes (`filament_tools.cpp:26-35`, `spool_join.cpp:258-265`)."""
    sim, p = make(XL, [loaded(0), SimTool(1, path=FilamentPath(loaded=True))])
    start(sim, p, [(0, 100, "PLA")], [Join(0, 1)])
    sim.advance(1)
    p.runout(0)
    run_join(sim, p)
    assert p.job.mapped == {0: 1} and p.tools[1].material is None
    # The file's type no longer belongs to T0, which falls back to the type it had.
    p.tools[0].kept_type = Material("ASA")
    assert p._type_to_load(p.tools[0], p.job).name == "ASA"
    # A change on the join's tool reloads the file's type, though the tool had none.
    p.change_filament(1)
    p.load(1)
    sim.advance(60)
    assert p.tools[1].material.name == "PLA"
