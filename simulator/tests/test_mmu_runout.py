"""Runouts and filament changes on an MMU3 (F44).

The counts follow the tracker's rules on an MK4S, as in `test_mmu_prints.py`: a move counts
what passes the gap, which stops at 40 mm, times 0.95, and with no slot selected nothing
counts and the gap stays where it was.

- An `M600` on slot a printing at gap 0: the park retracts to 8 (gap 8); the MMU's ramming
  +20 fills the 8 and counts 12, and -50 -5 -50 take the gap to its 40 mm stop. The unit
  then ejects the slot, which moves no printer filament. No type is written, so nothing
  counts as an unload.
- The reload, once the filament is in the MMU: the feed, 189.9 + 7 + 50 with no slot
  selected, counts nothing and leaves the gap at 40; the slot is selected; the load to the
  nozzle's 17 fills 17 (gap 23) and its 30 fills the 23 and counts 7; the prime +4 counts 4;
  the retract -8 (gap 8). So 11. The resume feeds the 8 back to the change's gap 0,
  counting nothing.
"""

from __future__ import annotations

import pytest

from simulator.model import Busy, Invalid, JobPhase, Reason, Simulation, State
from simulator.tests.test_mmu_prints import FEED, PARK_OUT, START, booted, counts, spec, start
from simulator.tests.test_model_edges import messages

RELOAD = 7 + 4  # the load to the nozzle past the 40 mm gap, and the prime
RAM_AFTER_PARK = 20 - 8  # the MMU's ramming after the park's retract to 8
FIRST_PICK = 32 + 54.63  # a slot's first pick in the print, PLA
# A print on slot 0 alone: the firmware's `T0` from no slot and its 35 mm, then the
# profile's -2 (gap 2) and 17 + 53, which count 15 + 53 (`test_a_one_color_print`).
ONE_SLOT = 35 + 15 + 53


def printing(sim, p, print_spec, seconds: float = 3.0) -> float:
    """Start the print and run it `seconds`; return the mm fed on slot 0, at gap 0."""
    begun = START if len({s.tool for s in print_spec.segments}) > 1 else ONE_SLOT
    start(sim, p, print_spec)
    sim.advance(seconds)
    assert p.job.phase is JobPhase.PRINTING and p.job.segment == 0
    assert p.selected_tool == 0 and p.mmu.path.gap == 0
    assert counts(p)[0] == pytest.approx(begun + p.job.fed_mm)
    return p.job.fed_mm


def unchanged(p) -> None:
    """No type written and no unload counted on any slot."""
    assert all(t.unload_count == 0 for t in p.tools.values())
    assert all(t.material is None or t.material.name == "PLA" for t in p.tools.values())


def test_a_runout_reloads_the_same_slot_through_the_mmu():
    sim, p = booted()
    fed = printing(sim, p, spec((0, 40), (1, 20)))
    p.runout(0)
    # The unload into the MMU and the eject run at once; the printer then asks for the
    # filament to be loaded into the MMU.
    assert p.state is State.ATTENTION and p.job.phase is JobPhase.WAIT_LOAD
    assert str(p.job.attention) == "runout tool 0"
    assert counts(p)[0] == pytest.approx(START + fed + RAM_AFTER_PARK)
    assert p.selected_tool is None and p.mmu.fed is None and not p.mmu.path.loaded
    assert p.mmu.path.gap == 40 and p.job.restore == (0, 0)
    assert "T0 ejected: load it into the MMU" in messages(sim, "mmu")
    unchanged(p)

    p.load(0)  # the reload keeps the slot's type
    assert p.state is State.ATTENTION and p.tools[0].process is not None
    sim.advance(24)
    # The feed counts nothing, and the slot isn't selected until it ends.
    assert counts(p)[0] == pytest.approx(START + fed + RAM_AFTER_PARK)
    assert p.selected_tool is None and p.mmu.path.gap == 40
    sim.advance(36)
    assert p.state is State.PAUSED
    assert counts(p)[0] == pytest.approx(START + fed + RAM_AFTER_PARK + RELOAD)
    assert p.selected_tool == 0 and p.mmu.fed == 0 and p.mmu.path.loaded
    assert p.mmu.path.gap == 8
    unchanged(p)

    p.resume()
    assert p.state is State.PRINTING and p.mmu.path.gap == 0
    assert counts(p)[0] == pytest.approx(START + fed + RAM_AFTER_PARK + RELOAD)
    sim.advance(100)
    assert p.state is State.FINISHED
    # Slot 0 prints the rest of its 40, then the change to slot 1; slot 1 as in any print.
    assert counts(p) == pytest.approx(
        {
            0: START + 40 + RAM_AFTER_PARK + RELOAD + PARK_OUT + FEED,
            1: FIRST_PICK + 20 + PARK_OUT,
            2: 0,
            3: 0,
            4: 0,
        }
    )
    unchanged(p)


def test_a_runout_with_no_slot_selected_injects_nothing():
    # A print resumed after a power cut runs with no slot selected; here the state is set
    # up directly.
    sim, p = booted()
    fed = printing(sim, p, spec((0, 40), (1, 20)))
    p.selected_tool = None
    p.runout(0)
    assert p.state is State.PRINTING and p.job.attention is None
    assert p.mmu.fed is None and not p.mmu.path.loaded
    assert "T0 ran out at FINDA with no slot selected: nothing injected" in messages(sim, "job")
    sim.advance(100)
    assert p.state is State.FINISHED
    # The rest of slot 0's segment and its unload count nothing, and the change to slot 1
    # comes from no slot: no -80, and its feed counts nothing. Slot 1 then counts as in any
    # print: the gap stayed at 0.
    assert counts(p) == pytest.approx(
        {0: START + fed, 1: FIRST_PICK + 20 + PARK_OUT, 2: 0, 3: 0, 4: 0}
    )
    unchanged(p)


def test_change_filament_on_the_selected_slot_runs_the_mmus_m600():
    sim, p = booted()
    printing(sim, p, spec((0, 40)))
    actions = {t["index"]: t["actions"] for t in p.to_json()["tools"]}
    assert actions[0]["change_filament"] is None
    refused = "Change filament on an MMU changes the selected slot, T0"
    assert actions[1]["change_filament"] == refused
    with pytest.raises(Invalid, match="changes the selected slot, T0"):
        p.change_filament(1)
    p.change_filament(0)
    assert str(p.job.attention) == "color change tool 0"
    p.load(0)
    sim.advance(60)
    p.resume()
    sim.advance(100)
    assert p.state is State.FINISHED
    assert counts(p)[0] == pytest.approx(ONE_SLOT + 40 + RAM_AFTER_PARK + RELOAD + 19.3)
    unchanged(p)


def test_change_filament_with_no_slot_selected_does_nothing():
    sim, p = booted()
    printing(sim, p, spec((0, 40)))
    p.selected_tool = None
    # The menu item stays enabled and sends nothing.
    assert p.to_json()["tools"][3]["actions"]["change_filament"] is None
    p.change_filament(3)
    assert p.state is State.PRINTING and p.job.attention is None
    assert messages(sim, "job")[-1] == "no slot selected: nothing changes"


def test_a_color_change_in_the_file_runs_the_mmus_m600():
    sim, p = booted()
    start(sim, p, spec((0, 40), color_changes_mm=[10]))
    sim.advance(5)
    assert str(p.job.attention) == "color change tool 0"
    assert counts(p)[0] == pytest.approx(ONE_SLOT + 10 + RAM_AFTER_PARK)
    p.load(0)
    sim.advance(60)
    p.resume()
    assert p.job.pending_color_changes == []
    sim.advance(100)
    assert p.state is State.FINISHED
    assert counts(p)[0] == pytest.approx(ONE_SLOT + 40 + RAM_AFTER_PARK + RELOAD + 19.3)
    unchanged(p)


def test_a_color_change_with_no_slot_selected_does_nothing():
    sim, p = booted()
    start(sim, p, spec((0, 40), color_changes_mm=[20]))
    sim.advance(3)
    p.selected_tool = None
    sim.advance(100)
    assert p.state is State.FINISHED and p.job is None
    assert "no slot selected: nothing changes" in messages(sim, "job")
    assert not any(e.text.startswith("T0 ejected") for e in sim.events)


def test_a_power_cut_while_waiting_for_the_reload_resumes_with_no_slot():
    sim, p = booted()
    printing(sim, p, spec((0, 40), (1, 20)))
    p.runout(0)
    p.reboot(keep_job=True)
    sim.advance(5)
    # The dialog is gone and the print goes on with nothing in the extruder and no slot
    # selected, so it counts nothing until the change to slot 1, which comes from no slot.
    assert p.state is State.PRINTING and p.job.attention is None and p.job.segment == 0
    assert p.selected_tool is None and p.mmu.fed is None and not p.mmu.path.loaded
    sim.advance(100)
    assert p.state is State.FINISHED
    assert counts(p) == pytest.approx({0: 0, 1: FIRST_PICK + 20 + PARK_OUT, 2: 0, 3: 0, 4: 0})
    unchanged(p)


def test_a_cancel_while_waiting_for_the_reload_unloads_nothing():
    sim, p = booted()
    fed = printing(sim, p, spec((0, 40)))
    p.runout(0)
    p.cancel()
    assert p.state is State.PRINTING and p.job.phase is JobPhase.PARKING
    sim.advance(5)
    assert p.state is State.STOPPED
    assert counts(p)[0] == pytest.approx(ONE_SLOT + fed + RAM_AFTER_PARK)
    assert "M702 W2: the end of the print unloads into the MMU" not in messages(sim, "job")
    unchanged(p)


def test_a_cancel_during_the_reload_unloads_the_selected_slot():
    sim, p = booted()
    fed = printing(sim, p, spec((0, 40)))
    p.runout(0)
    p.load(0)
    sim.advance(26)
    # Past the feed and the slot switch, 13 mm into the load to the nozzle.
    assert p.selected_tool == 0 and p.mmu.path.loaded
    p.cancel()
    # The stop's `M702 W2` rams +20 into the gap of 40 - 13, counting nothing past it.
    assert p.selected_tool is None and not p.mmu.path.loaded
    assert "M702 W2: the end of the print unloads into the MMU" in messages(sim, "job")
    sim.advance(5)
    assert p.state is State.STOPPED
    assert counts(p)[0] == pytest.approx(ONE_SLOT + fed + RAM_AFTER_PARK)
    unchanged(p)


def test_a_snapshot_while_waiting_for_the_reload_goes_on_as_the_print_does():
    sim, p = booted()
    printing(sim, p, spec((0, 40), (1, 20)))
    p.runout(0)
    other = Simulation()
    other.restore(sim.snapshot())
    q = other.printers["p"]
    assert q.job.waits_for(Reason.RUNOUT) and q.job.restore == (0, 0)
    assert q.selected_tool is None and q.mmu.path.gap == 40
    q.load(0)
    other.advance(60)
    q.resume()
    other.advance(100)
    assert q.state is State.FINISHED
    assert counts(q) == pytest.approx(
        {
            0: START + 40 + RAM_AFTER_PARK + RELOAD + PARK_OUT + FEED,
            1: FIRST_PICK + 20 + PARK_OUT,
            2: 0,
            3: 0,
            4: 0,
        }
    )


def test_a_jam_is_refused_with_the_mmu_on():
    sim, p = booted()
    printing(sim, p, spec((0, 40)))
    with pytest.raises(Invalid, match="a jam isn't modeled with the MMU on"):
        p.jam(0)
    assert p.to_json()["tools"][0]["actions"]["jam"] == "a jam isn't modeled with the MMU on"


def test_runout_guards():
    sim, p = booted()
    with pytest.raises(Busy, match="a runout or jam needs a print in progress"):
        p.runout(0)
    printing(sim, p, spec((0, 40)))
    actions = {t["index"]: t["actions"] for t in p.to_json()["tools"]}
    assert actions[0]["runout"] is None
    assert actions[1]["runout"] == "T1's filament isn't at FINDA"
    with pytest.raises(Invalid, match="T1's filament isn't at FINDA"):
        p.runout(1)
    p.mmu.connecting = 1.0
    with pytest.raises(Busy, match="FINDA reads nothing while the MMU connects"):
        p.runout(0)
    p.mmu.connecting = 0.0


def test_reload_guards():
    sim, p = booted()
    printing(sim, p, spec((0, 40)))
    p.runout(0)
    with pytest.raises(Invalid, match="a load needs a source, tag or preset, outside a runout"):
        p.load(1)
    with pytest.raises(Invalid, match="the MMU's filament menu has no plain load"):
        p.load(0, source="preset", name="PLA")
    with pytest.raises(Invalid, match="the MMU's reload has no purge to set"):
        p.load(0, purge_mm=10)
    p.set_power("off")
    with pytest.raises(Busy, match="p is off"):
        p.load(0)
