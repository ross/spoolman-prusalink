"""Prints on an MMU3 (F40, F42, F46): the stock MMU3 profile's moves, the firmware's tool
change, the single-tool file and the end of a print.

The counts follow the tracker's rules on an MK4S: a move counts what passes the gap, which
stops at 40 mm, times 0.95. With no slot selected nothing counts and the gap stays where it
was. The profile's moves (`simulator.slicer`), on PLA:

- Start: -2 with no slot selected; `T<first>`; then 17 and the 53 mm purge line on the
  first slot (51 on the Core One).
- A change, old slot: the ramming, 20.9518 in eight moves; -15 -28 -8 -4; a cooling move
  +20 -20; a stamping +50 +5 -55; a cooling move +20 -20; the park, -29. From gap 0 the
  ramming counts in full and -15 -28 take the gap to its 40 mm stop, where -8 and -4 leave
  it. Each +20 then fills half the gap and counts nothing, and the stamping's +50 fills it
  and counts 10, its +5 counts 5, and -55 takes it back to 40. So the old slot counts
  20.9518 + 15 = 35.9518 and ends at gap 40.
- A change, the firmware's `T` with the old slot selected: -80 (gap still 40), then the
  feed, 189.9 + 7 + 50, which fills the 40 and counts 149.9 + 7 + 50 = 206.9 on the old
  slot, though it pushes the new slot's filament. With no slot selected it counts nothing.
- A change, new slot: the 32 mm load (6.4 + 22.4 + 3.2), then the purge: 54.63 on its first
  pick in the print, 29.95 on a later one. From gap 0 all of it counts.
- End: where the print used several slots, the tower's last unload, the old slot's 35.9518;
  then -0.7; then the end G-code's `M702`, the MMU's ramming +20 -50 -5 -50, which counts
  what the +20 passes the gap, and leaves no slot selected.
"""

from __future__ import annotations

import pytest

from simulator.model import (
    Busy,
    Invalid,
    JobPhase,
    PrintSpec,
    Reason,
    Segment,
    Simulation,
    State,
)
from simulator.slicer import CORE_ONE_MMU_SLICER, MK4S_MMU_SLICER
from simulator.tests.test_model import PETG, run_sync
from simulator.tests.test_model_edges import messages
from simulator.tests.util import PLA, make, make_mmu, sliced

RATIO = 0.95
RAM = 20.9518  # PLA's ramming
PARK_OUT = RAM + 15  # PLA's ramming, and the stamping's +50 and +5 past the 40 mm gap
FEED = 189.9 - 40 + 7 + 50  # what the firmware's feed at a change counts past a full gap
START = 17 + 53  # the MK4S profile's load and purge line on the first slot
TWO_COLOR = [(0, 20), (1, 20), (0, 20), (1, 20)]


def spec(*segments: tuple, **kw) -> PrintSpec:
    return PrintSpec([Segment(*s) for s in segments], preheat_s=kw.pop("preheat_s", 0), **kw)


def start(sim, p, print_spec: PrintSpec, question: Reason | None = Reason.TOOLS_MAPPING) -> None:
    """Start the print and answer its preview's `question` with `tap_through`: Print on the
    tools-mapping screen, which an Active MMU shows for any print that has segments when
    FINDA sees no filament, or No at the MMU's unload question when it does, which prints
    with no mapping screen. With `question` None the preview asks nothing."""
    p.start_print(print_spec)
    sim.tick(0.5)
    if question is None:
        assert p.job.attention is None
        return
    assert p.job.waits_for(question)
    p.tap_through()


def finish(sim, p, print_spec: PrintSpec, question: Reason | None = Reason.TOOLS_MAPPING) -> None:
    start(sim, p, print_spec, question)
    sim.advance(100)
    assert p.state is State.FINISHED


def counts(p) -> dict[int, float]:
    """Each slot's counter in commanded mm."""
    return {i: t.extruded / RATIO for i, t in p.tools.items()}


def booted(**kw):
    """An MK4S with an MMU, slots 0 and 1 typed PLA, freshly booted with nothing stored:
    gap 0, no slot selected, nothing in the extruder, the MMU Active."""
    sim, p = make_mmu(types={0: PLA, 1: PLA}, mmu_connect_seconds=1, **kw)
    p.reboot()
    sim.advance(1.5)
    assert p.mmu.active and p.mmu.path.gap == 0 and p.mmu.path.auto_retracted is None
    assert p.selected_tool is None and not p.mmu.path.loaded
    return sim, p


def test_a_two_color_print_counts_the_source_arithmetic():
    sim, p = booted()
    finish(sim, p, spec(*TWO_COLOR))
    # 1. Start. The -2 counts nothing: no slot is selected. `T0` from none makes no -80,
    #    and its 246.9 mm feed counts nothing and leaves the gap at 0. Slot 0 then counts
    #    17 + 53: 70.
    # 2. Segment 1: slot 0, 90.
    # 3. Change 0 -> 1, slot 1's first pick. Slot 0's park-out counts 35.9518 and leaves
    #    the gap at 40; the firmware's -80 leaves it there, and its feed counts 206.9:
    #    slot 0, 90 + 35.9518 + 206.9 = 332.8518. Slot 1 counts 32 + 54.63 = 86.63.
    # 4. Segment 2: slot 1, 106.63.
    # 5. Change 1 -> 0, a later pick. Slot 1, 106.63 + 35.9518 + 206.9 = 349.4818. Slot 0
    #    counts 32 + 29.95: 394.8018.
    # 6. Segment 3: slot 0, 414.8018.
    # 7. Change 0 -> 1. Slot 0, 414.8018 + 242.8518 = 657.6536. Slot 1, 349.4818 + 61.95 =
    #    411.4318.
    # 8. Segment 4: slot 1, 431.4318.
    # 9. End. The tower's last unload on slot 1 counts 35.9518: 467.3836, at gap 40. The
    #    -0.7 counts nothing, nor does `M702`'s +20 into a 40 mm gap, and its -105 leaves
    #    the gap at 40 with no slot selected. The firmware's retract then counts on no slot,
    #    the gap at its stop isn't stored, and the auto-retract is skipped.
    assert counts(p) == pytest.approx({0: 657.6536, 1: 467.3836, 2: 0, 3: 0, 4: 0})
    # The counters read 624 and 444.
    assert [int(t.extruded) for t in p.tools.values()] == [624, 444, 0, 0, 0]
    assert pytest.approx(657.6536) == 70 + 20 + 2 * (PARK_OUT + FEED) + 32 + 29.95 + 20
    assert pytest.approx(467.3836) == 32 + 54.63 + 20 + PARK_OUT + FEED + 32 + 29.95 + 20 + PARK_OUT
    # No type written, no unload counted, and nothing left in the extruder.
    assert all(t.unload_count == 0 for t in p.tools.values())
    assert [p.tools[i].material.name for i in (0, 1)] == ["PLA", "PLA"]
    assert p.selected_tool is None and p.mmu.fed is None and not p.mmu.path.loaded
    assert p.mmu.path.gap == 40 and p.mmu.path.auto_retracted is None
    assert "tool change none -> T0" in messages(sim, "job")
    assert messages(sim, "job").count("tool change T0 -> T1") == 2


def test_the_same_print_again_in_one_boot_counts_40_less_at_its_start():
    # BF29: the first print left the gap at 40, and the second's `T0` from none counts
    # nothing and leaves it there. The start's 17 + 53 then fill the 40 first and count
    # 30. Everything after counts as in the first print.
    sim, p = booted()
    finish(sim, p, spec(*TWO_COLOR))
    first = counts(p)
    finish(sim, p, spec(*TWO_COLOR))
    second = {i: counts(p)[i] - first[i] for i in (0, 1)}
    assert second == pytest.approx({0: first[0] - 40, 1: first[1]})
    assert START - 40 == 30


@pytest.mark.parametrize("slot", [0, 2])
def test_a_one_color_print(slot):
    # A one-color file from the MMU3 profile lists filament used for all five slots, so
    # it isn't single-tool even on slot 0 (`GCodeInfo::is_singletool_gcode`), and the
    # firmware loads nothing first. The -2 and the `T<slot>` count nothing, then 17 + 53
    # count on the slot. No tower: the print's -0.7, then `M702`'s +20 counts 19.3 past
    # it, and unloads.
    sim, p = make_mmu(types={0: PLA, 2: PLA})
    finish(sim, p, spec((slot, 40)))
    counted = START + 40 + 19.3
    assert counts(p)[slot] == pytest.approx(counted)
    assert sum(counts(p).values()) == pytest.approx(counted)
    assert p.selected_tool is None and not p.mmu.path.loaded
    # The extruder is empty, so the end doesn't unload again (`M702 W2`).
    assert "M702 W2: the end of the print unloads into the MMU" not in messages(sim, "job")


def test_a_single_tool_file_is_loaded_and_unloaded_by_the_firmware():
    sim, p = make_mmu(types={0: PLA})
    finish(sim, p, spec((0, 40), single_tool_file=True))
    # `T0` from none, then 35 mm, with no profile moves: 35 + 40. The end retracts 8 on
    # slot 0 and stores the valid gap, 8. Then `M702 W2`: the ramming's +20 first feeds the
    # stored 8 back, which counts nothing, then counts 20, and the slot is unloaded.
    assert counts(p)[0] == pytest.approx(35 + 40 + 20)
    assert p.selected_tool is None and p.mmu.fed is None and not p.mmu.path.loaded
    assert "M702 W2: the end of the print unloads into the MMU" in messages(sim, "job")
    assert p.tools[0].unload_count == 0 and p.tools[0].material.name == "PLA"


def test_a_single_tool_file_prints_on_the_slot_already_selected():
    # The extruder's sensor sees filament and the file names no type, so the preview asks
    # nothing and shows no mapping screen. With a slot selected the firmware loads nothing,
    # and the file prints on that slot.
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2)
    finish(sim, p, spec((0, 40), single_tool_file=True), question=None)
    assert counts(p)[0] == 0
    assert counts(p)[2] == pytest.approx(40 + 20)
    assert p.selected_tool is None and not p.mmu.path.loaded


def test_a_stopped_print_unloads_while_it_reads_printing():
    sim, p = booted()
    start(sim, p, spec(*TWO_COLOR))
    sim.advance(2.3)
    fed = p.job.fed_mm
    assert 0 < fed < 20 and p.selected_tool == 0
    p.cancel()
    # The stop retracts 8 on slot 0 and stores it; `M702 W2` feeds it back, counts the
    # ramming's 20 and unloads, all as the job parks.
    assert p.state is State.PRINTING and p.job.phase is JobPhase.PARKING
    assert p.selected_tool is None and p.mmu.fed is None and not p.mmu.path.loaded
    assert counts(p)[0] == pytest.approx(START + fed + 20)
    sim.advance(5)
    assert p.state is State.STOPPED and p.tools[0].unload_count == 0


def run_sync_past_the_question(sim, p) -> None:
    """Run a sync job on an MMU whose FINDA sees filament: No at the preview's unload
    question runs it with the filament in place."""
    run_sync(sim, p, sliced(p, ""))
    assert p.job.waits_for(Reason.MMU_LOADED)
    p.tap_through()
    sim.tick(0.8)
    assert p.job.phase is JobPhase.RUN


def test_a_stopped_sync_job_on_a_loaded_mmu_unloads():
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2)
    run_sync_past_the_question(sim, p)
    p.cancel()
    # The stop retracts 8 on slot 2, then `M702 W2` counts its ramming's 20 and unloads.
    assert p.state is State.PRINTING and not p.mmu.path.loaded and p.selected_tool is None
    sim.advance(5)
    assert p.state is State.STOPPED
    assert counts(p)[2] == pytest.approx(20)
    assert p.tools[2].unload_count == 0 and p.tools[2].material.name == "PETG"


def test_a_finished_sync_job_leaves_the_filament():
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2)
    run_sync_past_the_question(sim, p)
    sim.advance(5)
    assert p.state is State.FINISHED
    assert p.selected_tool == 2 and p.mmu.fed == 2 and p.mmu.path.loaded
    assert all(t.extruded == 0 for t in p.tools.values())


def test_a_petg_to_pla_print_rams_the_old_filament_and_purges_the_new():
    sim, p = make_mmu(types={0: PETG, 1: PLA})
    finish(sim, p, spec((0, 20, "PETG"), (1, 20, "PLA")))
    # PETG's park-out: its 22.045 ramming, then three cooling moves with two stampings,
    # each stamping counting 15 past the 40 mm gap: 52.045. Then the firmware's 206.9. PLA's
    # first pick, 32 + 54.63; at the end, PLA's tower unload, 35.9518.
    petg_out = 22.045 + 2 * 15
    assert MK4S_MMU_SLICER.petg.park_out() == (
        22.045,
        *(-15.0, -28.0, -8.0, -4.0),
        *(20.0, -20.0, 50.0, 5.0, -55.0, 20.0, -20.0, 50.0, 5.0, -55.0, 20.0, -20.0),
        -29.0,
    )
    assert counts(p) == pytest.approx(
        {0: START + 20 + petg_out + FEED, 1: 32 + 54.63 + 20 + PARK_OUT, 2: 0, 3: 0, 4: 0}
    )


def test_any_other_type_prints_with_plas_moves():
    assert MK4S_MMU_SLICER.filament("ASA") is MK4S_MMU_SLICER.filament(None)
    assert MK4S_MMU_SLICER.filament(None) is MK4S_MMU_SLICER.pla
    assert sum(MK4S_MMU_SLICER.pla.park_out()) == pytest.approx(RAM - 84)
    assert sum(MK4S_MMU_SLICER.petg.park_out()) == pytest.approx(22.045 - 84)
    assert MK4S_MMU_SLICER.pla.pick(first=False) == (6.4, 22.4, 3.2, 29.95)


def test_the_core_ones_purge_line_is_51():
    sim, p = make_mmu("7.1.0", types={2: PLA})
    assert p.profile is CORE_ONE_MMU_SLICER
    finish(sim, p, spec((2, 40)))
    assert counts(p)[2] == pytest.approx(17 + 51 + 40 + 19.3)


def test_a_print_can_set_its_own_purge_line_and_tool_change_purge():
    sim, p = make_mmu(types={0: PLA, 1: PLA})
    finish(sim, p, spec((0, 10), (1, 10), prime_mm=10, toolchange_purge_mm=5))
    # 10 replaces the 53 mm purge line, and 5 the purge after slot 1's 32 mm load.
    assert counts(p) == pytest.approx(
        {0: 17 + 10 + 10 + PARK_OUT + FEED, 1: 32 + 5 + 10 + PARK_OUT, 2: 0, 3: 0, 4: 0}
    )


def test_a_print_with_a_slot_already_selected_changes_from_it():
    sim, p = booted()
    p.load_to_nozzle(1)
    sim.advance(60)
    assert p.state is State.IDLE
    # The load counts 47 from gap 0; its auto-retract then counts 8 and 15 and stores 55,
    # at gap 40.
    loaded = 47 + 8 + 15
    assert counts(p)[1] == pytest.approx(loaded) and p.selected_tool == 1
    finish(sim, p, spec((0, 20), (1, 20)), question=Reason.MMU_LOADED)
    # The start's -2 counts on slot 1, at gap 40. Its `T0` retracts 80 on slot 1, which
    # forgets the stored 55, and the feed counts 206.9 on slot 1. Slot 0 then prints as in a
    # print from none, and slot 1's first pick, segment and tower unload follow.
    assert counts(p) == pytest.approx(
        {
            0: START + 20 + PARK_OUT + FEED,
            1: loaded + FEED + 32 + 54.63 + 20 + PARK_OUT,
            2: 0,
            3: 0,
            4: 0,
        }
    )


def test_a_pause_and_resume_change_nothing_on_an_mmu():
    sim, p = booted()
    start(sim, p, spec(*TWO_COLOR))
    sim.advance(6)
    assert p.job.segment == 1 and p.selected_tool == 1
    p.pause()
    sim.advance(1)
    assert p.state is State.PAUSED and p.selected_tool == 1
    p.resume()
    sim.advance(100)
    assert p.state is State.FINISHED
    assert counts(p) == pytest.approx({0: 657.6536, 1: 467.3836, 2: 0, 3: 0, 4: 0})


def test_a_snapshot_mid_print_goes_on_as_the_print_does():
    sim, p = booted()
    start(sim, p, spec(*TWO_COLOR))
    sim.advance(6)
    assert p.job.segment == 1
    other = Simulation()
    other.restore(sim.snapshot())
    q = other.printers["p"]
    assert q.selected_tool == 1 and q.mmu.fed == 1
    assert all(t.path is q.mmu.path for t in q.tools.values())
    other.advance(100)
    assert q.state is State.FINISHED
    assert counts(q) == pytest.approx({0: 657.6536, 1: 467.3836, 2: 0, 3: 0, 4: 0})


def test_a_print_while_the_mmu_connects_is_refused():
    sim, p = make_mmu(mmu_connect_seconds=5)
    p.reboot()
    with pytest.raises(Busy, match="the MMU is connecting"):
        p.start_print(spec((0, 10)))
    assert p.to_json()["actions"]["start_print"] == "the MMU is connecting"
    sim.advance(5)
    p.start_print(spec((0, 10)))


def test_a_single_tool_file_needs_the_mmu_on_and_tool_0():
    with pytest.raises(Invalid, match="a single-tool file needs an MMU switched on"):
        make()[1].start_print(spec((0, 10), single_tool_file=True))
    sim, p = make_mmu()
    with pytest.raises(Invalid, match="a single-tool file prints on tool 0 alone"):
        p.start_print(spec((0, 10), (1, 10), single_tool_file=True))
    with pytest.raises(Invalid, match="prime_mm needs the MMU profile"):
        p.start_print(spec((0, 10), single_tool_file=True, prime_mm=3))
    p.set_mmu(False)
    with pytest.raises(Invalid, match="a single-tool file needs an MMU switched on"):
        p.start_print(spec((0, 10), single_tool_file=True))


def test_a_tool_change_with_a_slot_selected_retracts_80_first():
    sim, p = make_mmu(slot=2)
    phases = [(s.phase, s.mm) for s in p._mmu_change_steps(selected=True)]
    assert phases == [
        ("RETRACT", -80.0),
        ("FEED", 189.9),
        ("FEED", 7.0),
        ("FEED", 50.0),
        ("SELECT", 0.0),
    ]
