"""What a power cut and the power-panic resume count, per model.

A print that wasn't paused retracts 8 mm at the cut, except on the XL, which has a Dwarf.
The boot restarts the counters and leaves the tracker at gap 0. Then the resume either
feeds the 8 mm back at the end of BUSY, or, on INDX, purges 25 mm in the nozzle cleaner
halfway through BUSY instead, with no auto-retract after (`src/common/power_panic.cpp`).
From gap 0 each feed counts in full, times the feed ratio: 380/400 on a Nextruder, 550/576
on INDX.
"""

from __future__ import annotations

import pytest

from simulator.model import JobPhase, PrintSpec, Segment, SimPrinter, Simulation, State
from simulator.tests.util import make

NEXTRUDER_RATIO = 380 / 400
INDX_RATIO = 550 / 576
FED_BACK = 8 * NEXTRUDER_RATIO
PURGED = 25 * INDX_RATIO


def printing(model: str = "8.1.0") -> tuple[Simulation, SimPrinter]:
    """A printer 1 s into a print on tool 0, with the preheat skipped."""
    sim, p = make(model)
    p.start_print(PrintSpec([Segment(0, 100)], duration_s=10, preheat_s=0))
    sim.tick(0.8 + 1)
    assert p.state is State.PRINTING
    return sim, p


def job_log(sim: Simulation) -> list[str]:
    return [e.text for e in sim.events if e.kind == "job"]


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
    assert p.state is State.BUSY and t.extruded == 0
    sim.tick(p.settings.recover_seconds / 2 + 0.01)
    assert p.state is State.BUSY and t.extruded == pytest.approx(during)
    sim.tick(p.settings.recover_seconds / 2)
    assert p.state is State.PAUSED and t.extruded == pytest.approx(after)
    sim.tick(p.settings.unpark_seconds)
    assert p.state is State.PRINTING and t.extruded > after  # the print goes on


def test_indx_doesnt_auto_retract_after_the_purge():
    sim, p = printing("8.10.0")
    p.reboot(keep_job=True)
    sim.tick(p.settings.recover_seconds + 0.01)
    t = p.tools[0]
    # The purge leaves the filament at the nozzle, gap 0, with no distance stored.
    assert (t.gap, t.gap_valid, t.auto_retracted) == (0.0, True, None)
    assert job_log(sim)[-1:] == ["nozzle clean purged 25 mm"]


def test_the_cut_forgets_a_stored_auto_retract():
    sim, p = printing()
    p.tools[0].auto_retracted = 20.0
    p.reboot(keep_job=True)
    t = p.tools[0]
    # Kept, the boot would have set gap 20, and the 8 mm fed back would count nothing.
    assert (t.auto_retracted, t.gap, t.gap_valid) == (None, 0.0, False)
    sim.tick(p.settings.recover_seconds + 0.01)
    assert p.state is State.PAUSED and t.extruded == pytest.approx(FED_BACK)


def test_a_cold_bed_feeds_back_once():
    sim, p = printing()
    p.reboot(keep_job=True, cold_bed=True)
    sim.tick(p.settings.recover_seconds + 0.01)
    assert p.job.phase is JobPhase.AWAIT_RESUME and p.tools[0].extruded == 0
    p.resume()
    sim.tick(p.settings.recover_seconds + p.settings.unpark_seconds + 0.01)
    assert p.state is State.PRINTING
    assert job_log(sim).count("fed back 8 mm") == 1
    assert p.tools[0].extruded > FED_BACK  # and the print goes on


@pytest.mark.parametrize("model", ["8.1.0", "8.10.0"])
def test_a_paused_print_moves_nothing_at_the_cut(model):
    sim, p = printing(model)
    p.pause()
    p.tools[0].auto_retracted = 20.0
    p.reboot(keep_job=True)
    t = p.tools[0]
    assert p.state is State.PAUSED and t.extruded == 0 and t.auto_retracted == 20.0
    p.resume()
    assert p.state is State.PRINTING
    assert not any(s.startswith(("fed back", "nozzle clean")) for s in job_log(sim))


@pytest.mark.parametrize(("model", "counted"), [("8.1.0", FED_BACK), ("8.10.0", PURGED)])
def test_an_empty_tool_still_counts(model, counted):
    # The firmware checks no filament sensor on this path, and the tracker counts every E
    # move (F29).
    sim, p = printing(model)
    p.set_power("off")
    p.unload(0)  # pulled out by hand while off
    p.set_power("on", resume=True)
    assert not p.tools[0].loaded
    sim.tick(p.settings.recover_seconds + 0.01)
    assert p.state is State.PAUSED and p.tools[0].extruded == pytest.approx(counted)
