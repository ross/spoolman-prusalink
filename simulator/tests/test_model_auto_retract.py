"""The auto-retract, its deretract, the ramming of unloads and filament changes, the end of
a print, and what a boot makes of them, counted as the firmware's filament tracker counts.

The expected numbers are worked by hand from the firmware's sequences, with the gap before
and after each move. A Nextruder's gap stops at 40 mm and counts times 380/400 = 0.95;
INDX's stops at 27 mm and counts times 550/576. The sequences:

- Nextruder auto-retract: +8 -43 -8 -4 +20 -20 +55 -55 +20 -20, which leaves 55 mm
  retracted. From gap 0: +8 counts 8 (gap 0); -43 reaches the limit (gap 40, invalid);
  -8 -4 stay there; +20 gap 20; -20 gap 40; +55 refills 40 and counts 15 (gap 0, valid);
  -55 gap 40 (invalid); +20 gap 20; -20 gap 40. 23 counted.
- Nextruder unload: the auto-retract, then -50: 23 counted, 105 mm retracted.
- Nextruder runout: +7 -50 -5 -50. From gap 0: 7 counted, then gap 40.
- INDX auto-retract: +8 -8 +1 -1. From gap 0: 8 counted, gap 8, 8 stored.
- INDX unload and runout: +8 +8 -20 -3 +3 +19 +2 -20 -3 +3 -6 -8. From gap 0: 8 and 8
  count; -20 gap 20; -3 gap 23; +3 gap 20; +19 gap 1; +2 refills 1 and counts 1; -20 gap
  20; -3 gap 23; +3 gap 20; -6 gap 26; -8 gap 27 (invalid). 17 counted.
"""

from __future__ import annotations

import pytest

from simulator.model import JobPhase, Material, PrintSpec, Reason, Segment, SimTool, State
from simulator.tests.util import make

INDX_RATIO = 550 / 576
NEXTRUDER = ("8.1.0", "1.4.1", "4.1.0")  # Core One L, MK4S and iX


def ratio(model: str) -> float:
    return INDX_RATIO if model == "8.10.0" else 0.95


def idle_loaded(model: str, name: str = "PLA", **settings):
    """A printer whose empty tool 0 was just loaded from the menu."""
    sim, p = make(model, [SimTool(0)], **settings)
    p.load(0, source="preset", name=name)
    sim.advance(100)
    assert p.tools[0].loaded and p.tools[0].process is None
    return sim, p


def counted(p, action) -> float:
    """What `action` adds to tool 0's counter, in commanded mm."""
    before = p.tools[0].extruded
    action()
    return (p.tools[0].extruded - before) / ratio(p.model.code)


def tracker(p) -> tuple[float, bool, float | None]:
    t = p.tools[0]
    return t.gap, t.gap_valid, t.auto_retracted


def unload(sim, p) -> None:
    p.unload(0)
    sim.advance(10)
    assert not p.tools[0].loaded and p.tools[0].unload_count == 1


# The idle load.


@pytest.mark.parametrize("model", NEXTRUDER)
def test_an_idle_load_on_a_nextruder_ends_auto_retracted(model):
    sim, p = make(model, [SimTool(0)])
    retracted = []
    sim.listeners.append(
        lambda e: retracted.append(p.tools[0].extruded) if "auto-retracted" in e.text else None
    )
    p.load(0, source="preset", name="PLA")
    sim.advance(100)
    # The load from gap 0 counts its feeds and the purge; the Core One L and MK4S purge
    # ends with an 8 mm retract (gap 8) that the auto-retract feeds back first (gap 0).
    loads = {"8.1.0": 40 + 20 + 40, "1.4.1": 30 + 50 + 27, "4.1.0": 40 + 45 + 40}[model]
    assert retracted == [pytest.approx((loads + 23) * 0.95)]
    assert tracker(p) == (40, False, 55)
    assert p.tools[0].extruded == retracted[0]


def test_an_idle_load_on_indx_auto_retracts_after_the_cleaners_purge():
    sim, p = make("8.10.0", [SimTool(0)])
    p.load(0, source="preset", name="PLA")
    sim.advance(100)
    # 15 slow + 25 fast + the cleaner's 25 mm purge from gap 0, then the cleaner's M1705
    # auto-retract counts 8 and stores 8. The load's own auto-retract then skips: 8 mm is
    # fully retracted on INDX.
    assert p.tools[0].extruded == pytest.approx((15 + 25 + 25 + 8) * INDX_RATIO)
    assert tracker(p) == (8, True, 8)
    assert [e.text for e in sim.events].count("T0 auto-retracted 8 mm: extruded 69") == 1


# The deretract at the next forward move.


@pytest.mark.parametrize("model", NEXTRUDER)
def test_the_first_extrude_after_a_nextruder_load_counts_15_more(model):
    sim, p = idle_loaded(model)
    # The 55 mm deretract refills the 40 mm gap and counts 15 (gap 0, nothing stored);
    # then the 10 mm extrude counts.
    assert counted(p, lambda: p.extrude(0, 10)) == pytest.approx(15 + 10)
    assert tracker(p) == (0, True, 0)
    assert counted(p, lambda: p.extrude(0, 10)) == pytest.approx(10)  # only once


def test_the_first_extrude_after_an_indx_load_counts_nothing_more():
    sim, p = idle_loaded("8.10.0")
    # The 8 mm deretract refills the 8 mm gap (gap 0); then the 10 mm extrude counts.
    assert counted(p, lambda: p.extrude(0, 10)) == pytest.approx(10)
    assert tracker(p) == (0, True, 0)


@pytest.mark.parametrize(("model", "first"), [("8.1.0", 15 + 5), ("8.10.0", 5)])
def test_a_prints_first_mm_deretract(model, first):
    sim, p = idle_loaded(model)
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
    # 5 mm printed: the deretract first, 15 counted on the Core One L, 0 on INDX.
    assert counted(p, lambda: sim.tick(0.8 + 1)) == pytest.approx(first)
    assert tracker(p) == (0, True, 0)


# The menu unload.


@pytest.mark.parametrize("model", NEXTRUDER)
def test_a_nextruder_unloads_an_auto_retracted_tool_cold(model):
    sim, p = idle_loaded(model)
    # No ramming: the unload retracts its length less the 55 mm stored, inside the full
    # gap. Nothing counts.
    assert counted(p, lambda: unload(sim, p)) == 0
    assert tracker(p) == (40, False, None)


@pytest.mark.parametrize("model", NEXTRUDER)
def test_a_nextruder_unload_rams_after_an_extrude(model):
    sim, p = idle_loaded(model)
    p.extrude(0, 10)  # gap 0, valid, 0 stored
    # The unload ramming from gap 0: the auto-retract's 23, then -50.
    assert counted(p, lambda: unload(sim, p)) == pytest.approx(23)


@pytest.mark.parametrize("model", NEXTRUDER)
def test_a_nextruder_unload_with_the_tracker_lost_counts_15(model):
    sim, p = idle_loaded(model)
    p.extrude(0, 10)
    p.retract(0, 50)  # gap 40, invalid, nothing stored
    # The ramming feeds back nothing first. +8 gap 32; -43 gap 40; +20 gap 20; -20 gap
    # 40; +55 refills 40 and counts 15; the rest retracts.
    assert counted(p, lambda: unload(sim, p)) == pytest.approx(15)


def test_an_indx_unload_purges_then_rams():
    sim, p = idle_loaded("8.10.0")  # gap 8, 8 stored
    # The 2 mm purge deretracts the 8 first (gap 0) and counts 2; the ramming then
    # counts 17.
    assert counted(p, lambda: unload(sim, p)) == pytest.approx(2 + 17)
    assert tracker(p) == (27, False, None)


def test_an_indx_unload_after_an_extrude():
    sim, p = idle_loaded("8.10.0")
    p.extrude(0, 10)  # gap 0, 0 stored
    assert counted(p, lambda: unload(sim, p)) == pytest.approx(2 + 17)


def test_an_indx_unload_with_the_tracker_lost_counts_nothing():
    sim, p = idle_loaded("8.10.0")
    p.retract(0, 30)  # gap 27, invalid, nothing stored
    # The purge: gap 25. The ramming feeds back nothing first: +8 gap 17; +8 gap 9; -20
    # gap 27; -3 27; +3 24; +19 5; +2 3; -20 23; -3 26; +3 23; -6 27; -8 27. Nothing
    # reaches the nozzle.
    assert counted(p, lambda: unload(sim, p)) == 0


def test_the_unload_moves_over_its_duration_and_records_at_the_end():
    sim, p = idle_loaded("8.1.0")
    p.extrude(0, 10)
    base = p.tools[0].extruded
    p.unload(0)
    sim.tick(0.5)  # a sixth of the 3 s: 8 deretracted 0, then 8 of the steps' 333 mm
    assert p.tools[0].extruded - base == pytest.approx(8 * 0.95)
    assert p.state is State.BUSY and p.tools[0].unload_count == 0
    sim.tick(2.4)
    assert p.tools[0].unload_count == 0
    assert p.tools[0].extruded - base == pytest.approx(23 * 0.95)
    sim.tick(0.1)
    assert p.tools[0].unload_count == 1 and p.state is State.IDLE
    assert p.tools[0].extruded_at_unload == int(base + 23 * 0.95)


# The filament change during a print.


def printing(model: str):
    """An idle-loaded tool 0 printing 10 mm, past the first move's deretract."""
    sim, p = idle_loaded(model)
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
    sim.tick(0.8 + 2)
    assert tracker(p) == (0, True, 0)
    return sim, p


@pytest.mark.parametrize(("model", "ram"), [("8.1.0", 7), ("4.1.0", 7), ("8.10.0", 17)])
def test_a_runout_parks_and_rams(model, ram):
    sim, p = printing(model)
    # The park retracts 8 (gap 8). A Nextruder's runout ramming feeds them back and counts
    # its 7. INDX first purges 2 into the gap (gap 6), its ramming feeds the 6 back and
    # counts 17.
    assert counted(p, lambda: p.runout(0)) == pytest.approx(ram)
    assert p.tools[0].unload_count == 1
    assert p.job.restore_gap == 0


def test_a_jam_and_a_color_change_ram_too():
    sim, p = printing("8.1.0")
    assert counted(p, lambda: p.jam(0)) == pytest.approx(7)
    sim, p = idle_loaded("8.1.0")
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0, color_changes_mm=[10]))
    # 10 printed after the deretract's 15, then the change's 7.
    assert counted(p, lambda: sim.tick(0.8 + 3)) == pytest.approx(15 + 10 + 7)
    assert p.job.waits_for(Reason.COLOR_CHANGE) and p.tools[0].unload_count == 1


def test_a_runout_in_the_preheat_unloads_an_auto_retracted_nextruder_cold():
    sim, p = idle_loaded("8.1.0")
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=5))
    sim.tick(0.8 + 1)
    assert p.job.phase is JobPhase.PREHEAT
    # 55 stored is past the park's 8, so the park doesn't move; 55 is fully retracted, so
    # the unload is cold: nothing counts, and the tracker's invalid gap records as 0.
    assert counted(p, lambda: p.runout(0)) == 0
    assert p.job.restore_gap == 0


@pytest.mark.parametrize("lost", ["retract", "boot"])
def test_a_change_with_the_tracker_lost_resumes_at_gap_0(lost):
    sim, p = make("1.4.1", [SimTool.loaded_with(0, Material("PLA", adhoc=False))])
    if lost == "retract":
        p.retract(0, 50)  # gap 40: the limit, so the tracker no longer trusts it
    else:
        p.retract(0, 10)
        p.reboot()  # nothing stored: gap 0, but not valid until a feed reaches 0
        p.retract(0, 10)  # gap 10, still not valid
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=5))
    sim.tick(0.8 + 1)
    assert p.job.phase is JobPhase.PREHEAT
    p.runout(0)
    assert p.job.restore_gap == 0  # `get_retracted_distance(...).value_or(0)`
    p.load(0)
    sim.advance(100)
    # MK4S reload from gap 40: 30 slow (gap 10), 50 fast (gap 0), the 27 mm purge, the
    # 8 mm retract (gap 8), the 4 mm prime (gap 4) and the 8 mm retract: gap 12.
    assert p.tools[0].gap == pytest.approx(12)
    p.resume()  # back to gap 0: 12 fed, nothing counts
    assert tracker(p) == (0, True, None)


def test_an_indx_change_in_the_preheat_resumes_at_its_8_mm():
    sim, p = idle_loaded("8.10.0")  # gap 8, valid, 8 stored
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=5))
    sim.tick(0.8 + 1)
    # The 8 stored meets the park's 8, so the park doesn't move. The 2 mm purge feeds
    # the 8 back (gap 0) and counts 2, the ramming 17.
    assert counted(p, lambda: p.runout(0)) == pytest.approx(2 + 17)
    assert p.job.restore_gap == 8
    p.load(0)
    sim.advance(100)
    assert tracker(p) == (8, True, None)
    p.resume()  # already at the 8 mm the change began with: nothing moves
    assert tracker(p) == (8, True, None)


def test_an_indx_reload_auto_retracts_after_its_purge_and_the_prime_deretracts():
    sim, p = printing("8.10.0")
    p.runout(0)  # gap 27, invalid
    before = p.tools[0].extruded
    p.load(0)
    sim.advance(100)
    # 15 slow from gap 27: gap 12; 25 fast: 13 count (gap 0); the 25 mm purge; the
    # cleaner's auto-retract counts 8 (gap 8, 8 stored); the prime deretracts 8 (gap 0)
    # and counts 4; the retract opens 8.
    assert (p.tools[0].extruded - before) / INDX_RATIO == pytest.approx(13 + 25 + 8 + 4)
    assert tracker(p) == (8, True, None)
    p.resume()  # back to the runout's gap 0: 8 fed, nothing counts
    assert tracker(p) == (0, True, None)


# The end of a print.


@pytest.mark.parametrize("model", NEXTRUDER)
def test_a_nextruder_print_ends_with_an_auto_retract(model):
    sim, p = printing(model)
    # The end retracts 8 (gap 8), stores 8, and auto-retracts: the deretract feeds the 8
    # back (gap 0), then 23.
    assert counted(p, lambda: p.cancel()) == pytest.approx(23)
    assert tracker(p) == (40, False, 55)
    assert p.job.phase is JobPhase.PARKING and p.state is State.PRINTING


def test_a_finished_nextruder_print_auto_retracts_while_it_parks():
    sim, p = idle_loaded("8.1.0")
    p.start_print(PrintSpec([Segment(0, 10)], preheat_s=0))
    # 15 deretracted and 10 printed, then the end's 23.
    assert counted(p, lambda: sim.tick(0.8 + 2.1)) == pytest.approx(15 + 10 + 23)
    assert p.job.phase is JobPhase.PARKING
    assert tracker(p) == (40, False, 55)


def test_an_indx_print_ends_retracted_without_an_auto_retract():
    sim, p = printing("8.10.0")
    # The end retracts 8 (gap 8), stores 8, which INDX counts as fully retracted.
    assert counted(p, lambda: p.cancel()) == 0
    assert tracker(p) == (8, True, 8)


def test_a_sync_job_counts_only_on_a_tool_that_fed_since_its_auto_retract():
    sim, p = idle_loaded("8.1.0")
    # Auto-retracted: 55 stored is past the end's 8, and fully retracted.
    p.upload("a.gcode", "M117 hi\n", print_after=True, overwrite=False)
    assert counted(p, lambda: sim.advance(10)) == 0
    p.dismiss()
    p.extrude(0, 10)
    p.upload("b.gcode", "M117 hi\n", print_after=True, overwrite=False)
    assert counted(p, lambda: sim.advance(10)) == pytest.approx(23)
    assert p.state is State.FINISHED


# A boot.


@pytest.mark.parametrize(
    ("model", "boot", "extrude"),
    [
        # 55 stored: the gap clamps to the 40 mm limit, so the tracker doesn't trust it.
        # The extrude's deretract feeds 55: 15 count, then 10.
        ("8.1.0", (40, False, 55), 15 + 10),
        # 8 stored: gap 8, valid. The deretract refills it; the 10 count.
        ("8.10.0", (8, True, 8), 10),
    ],
)
def test_a_boot_hands_the_stored_distance_to_the_tracker(model, boot, extrude):
    sim, p = idle_loaded(model)
    p.reboot()
    assert tracker(p) == boot
    assert counted(p, lambda: p.extrude(0, 10)) == pytest.approx(extrude)


def test_a_boot_after_an_extrude_and_after_a_retract():
    sim, p = idle_loaded("8.1.0")
    p.extrude(0, 10)
    p.reboot()  # 0 stored: gap 0, valid
    assert tracker(p) == (0, True, 0)
    p.retract(0, 5)  # a retract forgets the stored distance
    p.reboot()  # nothing stored: gap 0, not valid
    assert tracker(p) == (0, False, None)
    # Gap 0 counts every mm, valid or not.
    assert counted(p, lambda: p.extrude(0, 10)) == pytest.approx(10)
    assert tracker(p) == (0, True, None)


def test_a_boot_keeps_an_auto_retracted_tool_cold_to_unload():
    sim, p = idle_loaded("8.1.0")
    p.reboot()
    assert counted(p, lambda: unload(sim, p)) == 0


# The switch, and models that don't auto-retract.


def test_auto_retract_off():
    sim, p = make("8.1.0", [SimTool(0)])
    p.auto_retract = False
    p.load(0, source="preset", name="PLA")
    sim.advance(100)
    # The load from gap 0, then the purge's 8 mm retract: gap 8, nothing stored.
    assert p.tools[0].extruded == pytest.approx((40 + 20 + 40) * 0.95)
    assert tracker(p) == (8, True, None)
    assert p.to_json()["auto_retract"] is False
    p.start_print(PrintSpec([Segment(0, 10)], preheat_s=0))
    # 8 refill the gap, 2 count; the end retracts 8 and stores them.
    assert counted(p, lambda: sim.tick(0.8 + 2.1)) == pytest.approx(2)
    assert tracker(p) == (8, True, 8)
    sim.advance(5)
    # The next forward move feeds the 8 back, then counts.
    assert counted(p, lambda: p.extrude(0, 10)) == pytest.approx(10)


@pytest.mark.parametrize("model", ["5.1.0", "2.1.0"])
def test_a_dev_kit_and_a_mini_never_auto_retract(model):
    sim, p = make(model, [SimTool(0)])
    p.load(0, source="preset", name="PLA")
    sim.advance(100)
    assert tracker(p) == (8, True, None)
    p.start_print(PrintSpec([Segment(0, 10)], preheat_s=0))
    sim.advance(10)
    # The end retracts 8 and stores nothing.
    assert tracker(p) == (8, True, None)
    assert not p.to_json()["model"]["auto_retracts"]


# Flexible filament.


def test_flexible_filament_isnt_auto_retracted():
    sim, p = idle_loaded("8.1.0", name="FLEX")
    assert tracker(p) == (8, True, None)
    sim, p = make("8.1.0", [SimTool(0)])
    p.load(
        0, source="tag", name="TPU", color="#000000", uuid="8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"
    )
    sim.advance(100)
    assert tracker(p) == (8, True, None)  # TPU loads as FLEX


def test_indx_rams_flexible_filament_with_its_flexible_sequence():
    sim, p = idle_loaded("8.10.0", name="FLEX")
    # No auto-retract after the cleaner's purge: gap 0, nothing stored.
    assert tracker(p) == (0, True, None)
    # The 2 mm purge counts; the flexible unload ramming is one 50 mm retract.
    assert counted(p, lambda: unload(sim, p)) == pytest.approx(2)


def test_a_cancel_after_a_flexible_runout_doesnt_auto_retract():
    sim, p = idle_loaded("8.1.0", name="FLEX")
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
    sim.tick(0.8 + 2)
    p.runout(0)  # clears the type: the unload's FLEX is the type the firmware falls back on
    assert p.tools[0].material is None and p.tools[0].kept_type.name == "FLEX"
    # From gap 40 an auto-retract would count 15 (+55 refills 40): 15 * 0.95 = 14.25. The
    # firmware skips it for flexible filament, so nothing counts.
    before = p.tools[0].extruded
    p.cancel()
    assert p.tools[0].extruded - before == 0
    assert p.job.phase is JobPhase.PARKING


def test_indx_unloads_a_cleared_flexible_tool_with_its_flexible_sequence():
    sim, p = idle_loaded("8.10.0", name="FLEX")  # gap 0, nothing stored
    p.set_sensor_fault(0, True)
    sim.tick(1)
    sim.tick(1)
    p.set_sensor_fault(0, False)
    assert p.tools[0].loaded and p.tools[0].material is None
    # The 2 mm purge counts, and the flexible ramming only retracts: 2 * 550 / 576 = 1.91.
    # The standard ramming would add its 17: 19 * 550 / 576 = 18.14.
    before = p.tools[0].extruded
    p.unload(0)
    sim.advance(10)
    assert p.tools[0].extruded - before == pytest.approx(1.91, abs=0.005)


def test_a_snapshot_mid_unload_keeps_its_steps():
    sim, p = idle_loaded("8.1.0")
    p.extrude(0, 10)
    p.unload(0)
    sim.tick(1)
    data = sim.snapshot()
    sim.restore(data)
    p = sim.printer("p")
    assert p.tools[0].process.steps and p.tools[0].process.remaining == pytest.approx(2)
    sim.tick(2)
    assert p.tools[0].unload_count == 1
