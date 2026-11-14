"""Change all on a toolchanger: the Filament menu's `M9934` (`Config::execute` in
`src/common/multi_filament_change.cpp`).

The numbers are worked by hand as in `test_model_auto_retract.py`. Each start has T0 loaded
and typed, T1 typed with its filament pulled out by hand while the printer was off, and T2
with no type and no filament. The boot leaves every tracker at gap 0, not trusted.

XL (40 mm gap, counts times 0.95):
- Unload from gap 0: the unload ramming +8 -43 -8 -4 +20 -20 +55 -55 +20 -20 counts 23,
  then -50; it retracts the whole 105, so the unload's own retract is 0. Gap 40.
- Load from gap 40: 30 slow + 50 fast = 80, 40 past the gap; the 27 mm purge; -8 (gap 8);
  the auto-retract feeds the 8 back, then counts 23. 40 + 27 + 23 = 90.
- Load from gap 0: 80 + 27 + 23 = 130.
So T0 and T1 count 23 + 90 = 113, and T2 130.

INDX (27 mm gap, counts times 550/576):
- Unload from gap 0: the 2 mm unload purge counts 2; the ramming +8 +8 -20 -3 +3 +19 +2
  -20 -3 +3 -6 -8 counts 17 and ends at gap 27. 19.
- Load from gap 27: 15 slow + 25 fast = 40, 13 past the gap; the cleaner's 25 mm purge;
  its M1705 auto-retract +8 -8 +1 -1 counts 8 and stores 8, which the load's own
  auto-retract finds fully retracted. 13 + 25 + 8 = 46.
- Load from gap 0: 40 + 25 + 8 = 73.
So T0 and T1 count 19 + 46 = 65, and T2 73.
"""

from __future__ import annotations

import json

import pytest

from simulator.model import Busy, Invalid, JobPhase, PrintSpec, Segment, SimTool, State
from simulator.tests.test_model_pick import picks
from simulator.tests.util import loaded, make

XL, INDX = "3.1.0", "8.10.0"
INDX_RATIO = 550 / 576


def three_tools(model: str):
    """T0 loaded and typed, T1 typed but emptied by hand while off, T2 empty with no type.
    INDX has docked its tool, so none is picked; the XL has T0 on its head."""
    sim, p = make(model, [loaded(0), loaded(1), SimTool(2)])
    if model == INDX:
        p.selected_tool = None
    p.set_power("off")
    p.unload(1)  # by hand: the type stays until the idle sensor clear reads the sensor
    p.set_power("on")
    t1 = p.tools[1]
    assert not t1.path.loaded and t1.material is not None and t1.unload_count == 0
    return sim, p


def run_to_idle(sim, p) -> list[State]:
    """Tick until no load or unload runs; return the state read at each tick."""
    states = []
    while any(t.process is not None for t in p.tools.values()):
        sim.tick(0.1)
        states.append(p.state)
    return states


@pytest.mark.parametrize(
    ("model", "counted", "ratio", "after", "picked"),
    [
        (XL, (113, 113, 130), 0.95, 2, ["pick T1", "pick T2"]),
        (
            INDX,
            (65, 65, 73),
            INDX_RATIO,
            None,
            ["pick T0", "dock T0", "pick T1", "dock T1", "pick T2", "dock T2"],
        ),
    ],
)
def test_change_all_changes_each_tool_in_turn(model, counted, ratio, after, picked):
    sim, p = three_tools(model)
    start = len(sim.events)
    p.change_all([2, 0, 1], name="PETG", color="#ff0000")
    states = [p.state, *run_to_idle(sim, p)]
    # BUSY from the first tool to the last, then IDLE: no poll sees a gap between tools.
    assert states[-1] is State.IDLE and set(states[:-1]) == {State.BUSY}
    logged = [e.text for e in sim.events[start:] if e.kind == "state"]
    assert logged == ["IDLE -> BUSY", "BUSY -> IDLE"]
    # A stored type unloads first, which counts, sensor or not; no type only loads.
    assert [t.unload_count for t in p.tools.values()] == [1, 1, 0]
    for t, mm in zip(p.tools.values(), counted, strict=True):
        assert t.extruded == pytest.approx(mm * ratio), t.index
        assert t.path.loaded and t.material.name == "PETG" and t.material.color == "#ff0000"
    assert p.selected_tool == after
    # Each pick is inside the dialog, and INDX docks only after the load.
    assert picks(sim)[-len(picked) :] == picked
    assert p.queue == []


def test_a_tool_left_out_keeps_its_filament():
    sim, p = make(XL, [loaded(0), loaded(1)])
    p.change_all([1], name="PETG")
    run_to_idle(sim, p)
    assert p.tools[0].material.name == "PLA" and p.tools[0].extruded == 0
    assert p.tools[1].material.name == "PETG" and p.tools[1].unload_count == 1


def test_a_cut_drops_what_is_left():
    sim, p = three_tools(XL)
    p.change_all([0, 1, 2], name="PETG")
    while p.selected_tool != 1:
        sim.tick(0.1)
    p.reboot()
    assert p.queue == [] and all(t.process is None for t in p.tools.values())
    assert p.state is State.IDLE and p.selected_tool == 1  # the XL's tool stays on the head
    sim.advance(60)
    assert p.tools[2].material is None and p.tools[2].extruded == 0


def test_a_snapshot_mid_change_carries_the_rest():
    sim, p = three_tools(INDX)
    p.change_all([0, 1, 2], name="PETG")
    sim.advance(5)
    assert p.queue
    sim.restore(json.loads(json.dumps(sim.snapshot())))
    p = sim.printers["p"]
    run_to_idle(sim, p)
    assert [t.unload_count for t in p.tools.values()] == [1, 1, 0]
    assert [round(t.extruded / INDX_RATIO) for t in p.tools.values()] == [65, 65, 73]


def test_a_queued_tool_cant_be_disabled():
    sim, p = make(XL, [loaded(0), loaded(1), SimTool(2)])
    p.change_all([0, 2], name="PETG")
    with pytest.raises(Invalid, match="^T2 is in use$"):
        p.set_tool_enabled(2, False)
    p.set_tool_enabled(1, False)


def test_a_power_panic_resume_waits_for_the_last_tool():
    """The printer is idle in the window before a power-panic resume, so Change all runs;
    the resume then waits until the last tool is done."""
    sim, p = make(XL, [loaded(0), loaded(1), SimTool(2)])
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
    sim.advance(1)
    p.tap_through()  # Print, on the tools-mapping screen
    sim.advance(3)
    assert p.job.phase is JobPhase.PRINTING
    p.reboot(keep_job=True)
    p.change_all([1, 2], name="PETG")
    held = []
    while any(t.process is not None for t in p.tools.values()):
        held.append(p.job is None and p.state is State.BUSY)
        sim.tick(0.1)
    assert all(held) and len(held) * 0.1 > 3 * sim.settings.panic_idle_seconds
    assert any("resume waits" in e.text for e in sim.events)
    sim.advance(sim.settings.panic_idle_seconds)
    assert p.resuming is None and p.job is not None
    assert p.tools[2].material.name == "PETG"


def test_change_all_refusals():
    _, p = make("8.1.0")
    with pytest.raises(Invalid, match="^COREONEL has one tool$"):
        p.change_all([0], name="PLA")
    sim, p = make(XL, [loaded(0), loaded(1), SimTool(2), loaded(3, enabled=False)])
    with pytest.raises(Invalid, match="^change all needs at least one tool$"):
        p.change_all([], name="PLA")
    with pytest.raises(Invalid, match="^tool 3 is disabled$"):
        p.change_all([0, 3], name="PLA")
    with pytest.raises(Invalid, match="^p has no tool 7$"):
        p.change_all([7], name="PLA")
    with pytest.raises(Invalid, match="^XL has no PPS preset$"):
        p.change_all([0], name="PPS")
    assert p.state is State.IDLE and p.queue == []
    p.unload(0)
    with pytest.raises(Busy, match="^can't change all while BUSY$"):
        p.change_all([1], name="PLA")
    run_to_idle(sim, p)
    p.start_print(PrintSpec([Segment(1, 100)], preheat_s=0))
    with pytest.raises(Busy, match="^can't change all while PRINTING$"):
        p.change_all([2], name="PLA")
    p.cancel()
    p.set_power("off")
    with pytest.raises(Busy, match="^p is off$"):
        p.change_all([2], name="PLA")
