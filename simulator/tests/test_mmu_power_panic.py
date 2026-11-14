"""A boot and the power-panic resume on a printer with an MMU3 (F45).

The resume has no MMU step of its own (`src/common/power_panic.cpp` mentions the MMU only to
cut its power). What it counts follows from the MMU's state and the tracker's rules, on an
MK4S, where a move counts what passes the gap, which stops at 40 mm, times 0.95:

- The cut retracts 8 mm on the one extruder, through the planner, so it counts on the
  selected slot, or on none and leaves the gap where it was. A retract counts nothing.
- The boot restarts the counters, starts the tracker at gap 0, untrusted, and starts the MMU:
  no slot selected, connecting for `mmu_connect_seconds` (`MMU2::Start`). The filament stays
  where it was: FINDA still sees the slot that was fed.
- While the MMU connects, every move counts on slot 0, whichever slot's filament moves
  (`currently_selected_virtual_tool`). Once it's Active, nothing counts and the gap stays
  where it is, until a `T` selects a slot (`planner.cpp:2206-2219`). How long it connects is
  inferred: 10 s by default.
- The window before the resume is 1.5 s, the recovery 2 s and the unpark 1 s. The resume
  feeds the 8 mm back at the end of the recovery, 3.5 s after the boot, so on slot 0 by
  default, and the print goes on 4.5 s after the boot, at 5 mm/s.
- The print's next tool change then changes from no slot: the old slot's wipe-tower unload,
  which runs before the `T`, counts on slot 0 or on none, the `T` makes no -80 and its feed
  counts on none, and the new slot counts from its load on (D9, inferred).
"""

from __future__ import annotations

import pytest

from simulator import prusalink
from simulator.model import MODELS, Busy, JobPhase, PrintSpec, Reason, Segment, State
from simulator.tests.test_mmu_prints import PARK_OUT, START, counts, spec, start
from simulator.tests.test_model import PETG
from simulator.tests.test_model_edges import messages
from simulator.tests.test_model_power_panic import in_window
from simulator.tests.util import PLA, make, make_mmu, sliced

WINDOW, RECOVER, UNPARK = 1.5, 2.0, 1.0
RATE = 5.0  # the print's mm/s
PICK = 32 + 54.63  # PLA's load and first purge on a slot's first pick
# The print feeds while the MMU connects, from 4.5 s after the boot to 10 s.
CONNECTING_MM = (10 - WINDOW - RECOVER - UNPARK) * RATE
TWO = [(0, 40), (1, 40)]


def printing_to(sim, p, fed: float) -> None:
    """Tick until the print has fed `fed` mm of its segments."""
    while p.job.fed_mm < fed - 1e-6:
        sim.tick(0.1)
    assert p.job.fed_mm == pytest.approx(fed) and p.state is State.PRINTING


def two_color_cut(fed: float, connect: float = 10.0):
    """An MK4S with an Active MMU and slots 0 and 1 typed PLA, cut at `fed` mm into a print
    of 40 mm on slot 0 then 40 on slot 1. The boot connects for `connect` seconds."""
    sim, p = make_mmu(types={0: PLA, 1: PLA}, mmu_connect_seconds=connect)
    start(sim, p, spec(*TWO))
    printing_to(sim, p, fed)
    before = counts(p)
    p.reboot(keep_job=True)
    return sim, p, before


def finish(sim, p) -> None:
    sim.advance(60)
    assert p.state is State.FINISHED


def boot_gap(monkeypatch, p) -> list[float]:
    """Record the shared path's gap each time a boot starts the tracker: what the cut left."""
    seen: list[float] = []
    path = p.mmu.path
    boot = path.boot

    def record(limit: float) -> None:
        seen.append(path.gap)
        boot(limit)

    monkeypatch.setattr(path, "boot", record)
    return seen


def test_the_cut_retracts_on_the_selected_slot_and_the_boot_keeps_the_filament(monkeypatch):
    sim, p = make_mmu(types={0: PLA, 1: PLA})
    start(sim, p, spec(*TWO))
    printing_to(sim, p, 50)
    assert p.selected_tool == 1 and p.mmu.path.gap == 0
    gaps = boot_gap(monkeypatch, p)
    p.reboot(keep_job=True)
    # The tracker saw the 8 mm retract on slot 1: the gap grew from 0 to 8.
    assert gaps[0] == 8
    # The boot: counters at 0, the gap at 0 and untrusted, no slot selected, the MMU
    # connecting, and slot 1's filament still in the extruder, past the selector.
    assert in_window(p) and p.resuming.id == 1
    assert all(t.extruded == 0 and t.unload_count == 0 for t in p.tools.values())
    assert (p.mmu.path.gap, p.mmu.path.gap_valid) == (0, False)
    assert p.selected_tool is None and p.mmu.connecting == 10 and not p.mmu.active
    assert p.mmu.fed == 1 and p.mmu.path.loaded
    assert [p.tools[i].material.name for i in (0, 1)] == ["PLA", "PLA"]
    assert prusalink.info(p)["mmu"] is True
    assert [t["index"] for t in prusalink.filament(p)["tools"]] == [0, 1, 2, 3, 4]


def test_the_window_refuses_the_mmu_menu_and_a_print_while_the_mmu_connects():
    sim, p, _ = two_color_cut(10)
    refusals = [
        lambda: p.load_to_nozzle(1),
        lambda: p.unload(0),
        lambda: p.eject(2),
        lambda: p.change_all([2], name="PETG"),
        lambda: p.preload_all("PLA"),
        lambda: p.start_print(spec((0, 10))),
    ]
    for call in refusals:
        with pytest.raises(Busy, match="the MMU is connecting"):
            call()
    assert p.to_json()["actions"]["start_print"] == "the MMU is connecting"
    assert p.to_json()["tools"][0]["actions"]["unload"] == "the MMU is connecting"
    sim.advance(WINDOW)
    assert p.job.phase is JobPhase.RECOVER


def test_the_window_runs_no_idle_sensor_clear():
    # The clear is off by the MMU's setting, connecting too: an extruder sensor that reads
    # empty in the window clears no type and counts no unload, where a single-tool
    # printer's would.
    sim, p, _ = two_color_cut(10)
    p.set_sensor_fault(0, True)
    sim.advance(WINDOW / 2)
    assert in_window(p)
    assert all(t.unload_count == 0 for t in p.tools.values())
    assert [p.tools[i].material.name for i in (0, 1)] == ["PLA", "PLA"]
    single = make("1.4.1")[1]
    single.set_sensor_fault(0, True)
    single.tick(0.1)
    assert single.tools[0].unload_count == 1


def test_an_unload_in_the_window_after_the_mmu_connects_delays_the_resume():
    # With a window shorter than the resume's, the menu works there. Unload Filament on the
    # fed slot reads BUSY, so the resume waits for it, as for any menu operation. With no
    # slot selected its ramming counts nothing and leaves the gap at the boot's 0. The print
    # then goes on through the empty extruder, counting nothing, until its tool change loads
    # slot 1.
    sim, p, _ = two_color_cut(10, connect=1)
    sim.advance(1.1)
    assert p.mmu.active and in_window(p)
    p.unload(0)
    sim.advance(WINDOW)
    assert p.state is State.BUSY and p.job is None and p.resuming is not None
    while p.job is None:
        sim.tick(0.1)
    assert p.mmu.fed is None and not p.mmu.path.loaded and p.selected_tool is None
    finish(sim, p)
    # Slot 1's first pick from gap 0, its 40 mm and the tower's unload at the end.
    assert counts(p) == pytest.approx({0: 0, 1: PICK + 40 + PARK_OUT, 2: 0, 3: 0, 4: 0})
    assert all(t.unload_count == 0 for t in p.tools.values())


def test_an_upload_in_the_window_drops_the_resume_on_an_mmu():
    # The bridge's sync job in the window (BF26). The MMU still connects, so the preview
    # asks to unload the MMU's filament first (F43), and Yes is refused: what it unloads
    # then isn't read. No runs the job while the MMU still connects, so the end's
    # auto-retract isn't skipped: it runs and counts on slot 0, though slot 1's filament is
    # in the extruder.
    sim, p, _ = two_color_cut(50)
    p.upload("sync.gcode", sliced(p, "M117 hi\n"), print_after=True, overwrite=False)
    assert p.job.kind == "sync" and p.resuming is None
    assert "job 1: resume dropped by the new job" in messages(sim, "job")
    sim.advance(2)
    assert p.state is State.ATTENTION and p.job.waits_for(Reason.MMU_LOADED)
    assert not p.mmu.active and all(t.extruded == 0 for t in p.tools.values())
    with pytest.raises(Busy, match="while the MMU connects"):
        p.unload_first()
    p.tap_through()
    sim.advance(6)
    assert p.state is State.FINISHED and p.job is None and not p.mmu.active
    assert p.tools[0].extruded > 0 and p.tools[1].extruded == 0
    assert p.mmu.fed == 1 and p.mmu.path.loaded
    assert "job 1 resumes after the power panic" not in messages(sim, "job")


def test_an_upload_in_the_window_on_an_active_mmu_waits_at_the_unload_question():
    # BF26 with the MMU Active before the upload: FINDA still sees slot 1's filament, so the
    # sync job waits at the preview's unload question (F43) as it does while the MMU
    # connects, and the resume is gone all the same.
    sim, p, _ = two_color_cut(50, connect=0.5)
    sim.advance(1.0)
    assert p.mmu.active and p.state is State.IDLE and p.resuming is not None
    p.upload("sync.gcode", sliced(p, "M117 hi\n"), print_after=True, overwrite=False)
    assert p.resuming is None
    sim.advance(6)
    assert p.state is State.ATTENTION and p.job.waits_for(Reason.MMU_LOADED)
    assert p.job.kind == "sync" and p.mmu.fed == 1
    assert "job 1 resumes after the power panic" not in messages(sim, "job")


@pytest.mark.parametrize(
    ("connect", "fed_back", "printed"),
    [
        # Connecting past the print's restart: the 8 mm and the first 27.5 mm on slot 0.
        (10, 8, CONNECTING_MM),
        # Active between the feed back, at 3.5 s, and the restart, at 4.5 s: the 8 mm alone.
        (4, 8, 0),
        # Active before the feed back: nothing.
        (3, 0, 0),
    ],
)
def test_a_one_color_print_resumed_counts_on_slot_0_while_the_mmu_connects(
    connect, fed_back, printed
):
    # A print on slot 2, cut 10 mm in. Nothing counts on slot 2 again: the print counts on
    # slot 0 while the MMU connects, then on none to its end. The end's tower-less unload,
    # -0.7 and `M702`, counts on none too, and leaves no slot.
    sim, p = make_mmu(types={0: PLA, 2: PETG}, mmu_connect_seconds=connect)
    start(sim, p, spec((2, 100, "PETG")))
    printing_to(sim, p, 10)
    assert counts(p)[2] == pytest.approx(17 + 53 + 10)
    p.reboot(keep_job=True)
    sim.advance(WINDOW + RECOVER)
    assert p.job.phase is JobPhase.UNPARK and p.state is State.PAUSED
    assert counts(p)[0] == pytest.approx(fed_back)
    finish(sim, p)
    assert counts(p) == pytest.approx({0: fed_back + printed, 1: 0, 2: 0, 3: 0, 4: 0})
    assert p.selected_tool is None and p.mmu.fed is None and not p.mmu.path.loaded
    assert messages(sim, "job").count("fed back 8 mm") == 1


def test_a_two_color_print_cut_in_its_first_segment():
    sim, p, before = two_color_cut(10)
    # Before the cut: slot 0, the start's 17 + 53 and 10 mm of its segment, 80. The bridge
    # gets what it polled of that; the boot restarts the counters.
    assert before[0] == pytest.approx(START + 10)
    finish(sim, p)
    # After the boot:
    # 1. The cut's -8 counts nothing. The resume's +8 counts on slot 0: 8.
    # 2. The print goes on at 10 mm. While the MMU connects, 5.5 s, it counts 27.5 on
    #    slot 0: 35.5, to 37.5 mm. The last 2.5 mm of the segment count on none, and the gap
    #    stays at 0.
    # 3. The change to slot 1: slot 0's tower unload runs with no slot selected and counts
    #    nothing; `T1` from none makes no -80, and its feed counts nothing. Slot 1's first
    #    pick from gap 0: 86.63.
    # 4. Slot 1's 40 mm: 126.63. The end: the tower's unload of slot 1, 35.9518: 162.5818.
    #    -0.7 and `M702`'s +20 into the 40 mm gap count nothing.
    assert counts(p) == pytest.approx(
        {0: 8 + CONNECTING_MM, 1: PICK + 40 + PARK_OUT, 2: 0, 3: 0, 4: 0}
    )
    assert pytest.approx(35.5) == 8 + CONNECTING_MM
    assert pytest.approx(162.5818) == PICK + 40 + PARK_OUT
    assert "tool change none -> T1" in messages(sim, "job")
    assert all(t.unload_count == 0 for t in p.tools.values())


def test_a_two_color_print_cut_in_its_second_segment():
    sim, p, before = two_color_cut(50)
    # Before the cut: slot 0, 70 + 40 + its unload and the firmware's feed at the change,
    # 242.8518: 352.8518. Slot 1, its first pick and 10 mm: 96.63.
    assert before == pytest.approx({0: 352.8518, 1: PICK + 10, 2: 0, 3: 0, 4: 0})
    finish(sim, p)
    # After the boot:
    # 1. The cut's -8 on slot 1 counts nothing. The resume's +8 counts on slot 0: 8.
    # 2. Slot 1's filament prints on, 27.5 mm counted on slot 0 while the MMU connects:
    #    35.5. The last 2.5 mm count on none.
    # 3. The end: the tower's unload of slot 1, -0.7 and `M702` run with no slot selected
    #    and count nothing. Slot 1 counts nothing after the boot.
    assert counts(p) == pytest.approx({0: 8 + CONNECTING_MM, 1: 0, 2: 0, 3: 0, 4: 0})
    assert p.selected_tool is None and p.mmu.fed is None and not p.mmu.path.loaded


def test_an_mmu_active_before_the_resume_counts_nothing_until_the_tool_change():
    sim, p, _ = two_color_cut(10, connect=1)
    finish(sim, p)
    # The resume's 8 mm and the rest of slot 0's segment count on none. The change's
    # unload and feed count on none, then slot 1 counts as above.
    assert counts(p) == pytest.approx({0: 0, 1: PICK + 40 + PARK_OUT, 2: 0, 3: 0, 4: 0})


def test_a_tool_change_while_the_mmu_connects_waits_for_it():
    # Cut 35 mm in: the print reaches the change 1 s after it goes on, while the MMU still
    # connects. Its last 5 mm count on slot 0, and slot 0's tower unload runs before the
    # `T`, so it counts on slot 0 too: 35.9518 from gap 0, which leaves the gap at 40. The
    # `T` waits for the MMU, then changes from none: no -80, and the feed counts on none,
    # so the gap stays at 40. Slot 1's first pick then fills the 40 first: 46.63.
    sim, p, _ = two_color_cut(35)
    finish(sim, p)
    assert counts(p) == pytest.approx(
        {0: 8 + 5 + PARK_OUT, 1: PICK - 40 + 40 + PARK_OUT, 2: 0, 3: 0, 4: 0}
    )
    assert "T1 waits for the MMU to connect" in messages(sim, "job")
    assert "tool change none -> T1" in messages(sim, "job")
    assert messages(sim, "mmu").count("MMU active") == 1


@pytest.mark.parametrize(("wait", "fed_back"), [(60, 0), (0, 8)])
def test_a_cold_bed_waits_for_the_user_and_the_mmu_connects_meanwhile(wait, fed_back):
    # The resume's 8 mm counts on slot 0 only when the user resumes while the MMU still
    # connects.
    sim, p = make_mmu(types={0: PLA, 2: PLA})
    start(sim, p, spec((2, 100)))
    printing_to(sim, p, 10)
    p.reboot(keep_job=True, cold_bed=True)
    sim.advance(WINDOW + wait)
    assert p.state is State.ATTENTION and p.job.phase is JobPhase.AWAIT_RESUME
    p.resume()
    sim.advance(RECOVER + UNPARK)
    assert p.state is State.PRINTING and counts(p)[0] == pytest.approx(fed_back)
    assert counts(p)[2] == 0


def test_a_paused_print_comes_back_paused_and_prints_on_no_slot():
    sim, p = make_mmu(types={0: PLA, 2: PLA})
    start(sim, p, spec((2, 100)))
    printing_to(sim, p, 10)
    p.pause()
    p.reboot(keep_job=True)
    sim.advance(WINDOW + RECOVER)
    # A paused print: no retract at the cut and no feed back at the resume.
    assert p.state is State.PAUSED and all(t.extruded == 0 for t in p.tools.values())
    sim.advance(10)
    assert p.mmu.active and p.selected_tool is None
    p.resume()
    finish(sim, p)
    assert all(t.extruded == 0 for t in p.tools.values())


MENU = {
    "load_to_nozzle": lambda p: p.load_to_nozzle(2),
    "unload": lambda p: p.unload(2),
    "preload": lambda p: p.change_all([1, 3], name="PETG"),
    "preload_all": lambda p: p.preload_all("PETG"),
}


@pytest.mark.parametrize(
    ("operation", "slot"),
    [("load_to_nozzle", 2), ("unload", 2), ("preload", None), ("preload_all", None)],
)
def test_a_cut_during_an_mmu_operation_boots_with_no_slot_and_nothing_to_resume(operation, slot):
    # The operation's own side is in test_mmu_operations.py. The boot's: no process, no
    # change all queued, no slot selected, the MMU connecting, nothing counted, and no print
    # to resume. Where the filament is stays as the operation left it.
    sim, p = make_mmu(types={0: PLA, 2: PLA}, slot=slot)
    MENU[operation](p)
    sim.advance(1)
    assert p.state is State.BUSY
    fed, loaded = p.mmu.fed, p.mmu.path.loaded
    p.set_power("off")
    p.set_power("on", resume=True)
    assert in_window(p) and p.resuming is None and not p.queue
    assert all(t.process is None and t.unload_count == 0 for t in p.tools.values())
    assert all(t.extruded == 0 for t in p.tools.values())
    assert p.selected_tool is None and not p.mmu.active
    assert (p.mmu.fed, p.mmu.path.loaded) == (fed, loaded)


def test_a_second_cut_in_the_window_restarts_the_mmus_connecting(monkeypatch):
    # The printer is idle in the window, so the second cut retracts nothing and keeps the
    # resume. The second boot starts the MMU connecting again, 10 s from then: the resume's
    # 8 mm counts on slot 0.
    sim, p, _ = two_color_cut(50)
    sim.advance(WINDOW / 2)
    gaps = boot_gap(monkeypatch, p)
    p.reboot(keep_job=True)
    assert gaps[0] == 0 and in_window(p) and p.resuming.id == 1
    assert p.mmu.connecting == 10
    sim.advance(WINDOW + RECOVER)
    assert counts(p)[0] == pytest.approx(8)


def test_a_cut_after_the_resume_with_no_slot_selected_retracts_on_none(monkeypatch):
    # Resumed in slot 0's segment with the MMU Active: no slot is selected, so the second
    # cut's 8 mm retract leaves the gap where the boot started it. The second resume's 8 mm
    # counts on slot 0 again while the MMU connects.
    sim, p, _ = two_color_cut(10, connect=1)
    sim.advance(WINDOW + RECOVER + UNPARK + 1)
    assert p.state is State.PRINTING and p.mmu.active and p.selected_tool is None
    gaps = boot_gap(monkeypatch, p)
    sim.settings.mmu_connect_seconds = 10
    p.reboot(keep_job=True)
    assert gaps[0] == 0
    sim.advance(WINDOW + RECOVER)
    assert counts(p)[0] == pytest.approx(8)


def test_a_plain_reboot_leaves_the_slot_in_the_extruder_with_none_selected():
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2, mmu_connect_seconds=1)
    p.reboot()
    assert p.selected_tool is None and p.mmu.fed == 2 and p.mmu.path.loaded
    sim.advance(1)
    assert p.mmu.active
    # Load To Nozzle won't free the extruder with no slot selected; Unload Filament on the
    # fed slot does, and counts nothing.
    with pytest.raises(Busy, match="no slot is selected; unload it first"):
        p.load_to_nozzle(0)
    p.unload(2)
    sim.advance(5)
    assert p.state is State.IDLE and all(t.extruded == 0 for t in p.tools.values())
    assert p.mmu.fed is None and not p.mmu.path.loaded
    p.load_to_nozzle(0)
    sim.advance(60)
    assert p.selected_tool == 0 and p.mmu.fed == 0


def test_a_cut_with_the_mmu_off_resumes_as_a_single_tool_printer():
    # With the MMU off the printer is a single-tool MK4S on slot 0: the cut, the boot, the
    # window and the resume count as on the plain model.
    printers = []
    for mmu in (True, False):
        if mmu:
            sim, p = make_mmu(slot=0)
            p.set_mmu(False)
        else:
            sim, p = make("1.4.1")
        p.start_print(PrintSpec([Segment(0, 100)], duration_s=10, preheat_s=0))
        sim.advance(1.8)
        p.reboot(keep_job=True)
        printers.append((sim, p))
    for _ in range(20):
        readings = []
        for sim, p in printers:
            sim.advance(0.5)
            readings.append((p.state, p.selected_tool, round(p.tools[0].extruded, 6)))
        assert readings[0] == readings[1]
    assert printers[0][1].tools[0].extruded > 8 * 0.95


def test_every_model_that_takes_an_mmu_has_power_panic():
    # The MINI and the XL dev kit build no power panic, and neither is an MMU build.
    takes = [m for m in MODELS.values() if m.takes_mmu]
    assert takes and all(m.power_panic for m in takes)
    assert not any(m.takes_mmu for m in MODELS.values() if not m.power_panic)
