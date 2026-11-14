"""The stock slicer profile's moves around a toolchanger's print (F33, `simulator.slicer`).

The counts are worked by hand with the gap before and after each move. The XL's gap stops
at 40 mm and counts times 380/400 = 0.95; INDX's stops at 27 mm and counts times 550/576.
Every tool starts at gap 0 with nothing stored, unless a test says otherwise.

- XL start: the first tool retracts 2 (gap 2); each other used tool primes 39 (39 count)
  and retracts 20 (gap 20); then the first tool primes 39 (37 count), retracts 0.8 and
  feeds it back. A change rams 4.1575 on the old tool and retracts 20; the new tool feeds
  the 20 back and purges 9.238 on its first pick, 7.0076 on a later one. The end rams
  4.1575 on the last tool when the print uses several, then retracts 0.8.
- INDX start: each used tool purges 25 in the nozzle cleaner (25 count) and auto-retracts
  +8 -8 +1 -1 (8 count, gap 8, 8 stored). The first tool deretracts the 8 (gap 0), counts
  1.6 and 5.4907, retracts 0.8 and feeds it back. A change retracts the old tool 0.8, then
  8 in the park (gap 8.8). On its first pick the new tool counts 1.6 and the 4.1596 purge
  after any deretract; on a later one its 1.6 + 8 + 0.8 fill the 8.8 and count 1.6, then
  the purge. Each pick ends with a 0.8 retract fed back. The end retracts 0.8.
"""

from __future__ import annotations

import pytest

from simulator.model import Invalid, JobPhase, PrintSpec, Segment, SimTool, State
from simulator.slicer import INDX_SLICER
from simulator.tests.util import loaded, make

XL, INDX = "3.1.0", "8.10.0"
RATIO = {XL: 0.95, INDX: 550 / 576}

XL_FIRST = 37  # the first tool's prime past its 2 mm retract
XL_OTHER = 39  # another used tool's prime
XL_RAM = 4.1575
INDX_START = 25 + 8  # the cleaner's purge and the auto-retract
INDX_PRIME = 1.6 + 5.4907
INDX_PICK = 1.6 + 4.1596


def spec(*segments: tuple[int, float], **kw) -> PrintSpec:
    return PrintSpec([Segment(t, mm) for t, mm in segments], preheat_s=kw.pop("preheat_s", 0), **kw)


def start(sim, p, print_spec: PrintSpec) -> None:
    """Start the print and answer Print on the tools-mapping screen."""
    p.start_print(print_spec)
    sim.tick(0.5)
    p.tap_through()


def finish(sim, p, print_spec: PrintSpec) -> None:
    start(sim, p, print_spec)
    sim.advance(100)
    assert p.state is State.FINISHED


def counts(p) -> dict[int, float]:
    """Each tool's counter in commanded mm."""
    return {i: t.extruded / RATIO[p.model.code] for i, t in p.tools.items()}


def picks(sim) -> list[str]:
    return [e.text for e in sim.events if e.text.startswith("pick ")]


def test_an_xl_print_primes_the_used_tools_and_purges_each_pick():
    sim, p = make(XL, [loaded(0), loaded(1), loaded(2)])
    finish(sim, p, spec((0, 10), (1, 20), (0, 30)))
    # T0: its prime, 10, the change's ramming, its later pick's purge after the 20 back,
    # 30, and the end's ramming. T1: its prime, its first pick's purge after the 20 back,
    # 20, and the change's ramming, which leaves it at gap 20.
    assert counts(p) == pytest.approx(
        {
            0: XL_FIRST + 10 + XL_RAM + 7.0076 + 30 + XL_RAM,
            1: XL_OTHER + 9.238 + 20 + XL_RAM,
            2: 0,
        }
    )
    assert p.tools[0].path.gap == pytest.approx(0.8)
    assert p.tools[1].path.gap == 20 and p.tools[1].path.auto_retracted == 20
    # The start picks T1 to prime it, then T0; T2 isn't used, so nothing picks it.
    assert picks(sim) == ["pick T1", "pick T0"]
    assert p.tools[2].path.gap == 0 and p.selected_tool is None


def test_an_indx_print_purges_every_used_tool_and_primes_the_first():
    sim, p = make(INDX, [loaded(0), loaded(1), loaded(2)])
    finish(sim, p, spec((0, 10), (1, 10), (0, 10), (1, 10)))
    # Each used tool: the start's purge, its picks, 20 printed. T0 primes; its later pick
    # counts as much as T1's first, since the pick's 8.8 refill what its park retracted.
    assert counts(p) == pytest.approx(
        {
            0: INDX_START + INDX_PRIME + 10 + INDX_PICK + 10,
            1: INDX_START + INDX_PICK + 10 + INDX_PICK + 10,
            2: 0,
        }
    )
    # T0's park left it 0.8 + 8 back; T1 ends with the end G-code's 0.8.
    assert p.tools[0].path.gap == pytest.approx(8.8)
    assert p.tools[1].path.gap == pytest.approx(0.8)
    assert "pick T2" not in picks(sim)


def test_indx_moves_more_on_a_later_pick_than_on_a_first():
    sim, p = make(INDX, [loaded(0), loaded(1)])
    t0, t1 = p.tools[0], p.tools[1]
    # T0 retracts 0.8 + 8 into its park, and T1's first pick from gap 0 counts 1.6, the
    # purge, and its retract fed back.
    p.feed.tool_change(p, t0, t1, INDX_SLICER, first=True)
    assert counts(p) == pytest.approx({0: 0, 1: INDX_PICK})
    assert t0.path.gap == pytest.approx(8.8) and t1.path.gap == 0
    # A later pick of a tool at gap 0 counts its lock move, the 8 and the 0.8 too.
    t0.path.fill(True)
    p.feed.tool_change(p, t1, t0, INDX_SLICER, first=False)
    assert counts(p)[0] == pytest.approx(1.6 + 8 + 0.8 + 4.1596)


@pytest.mark.parametrize(
    ("model", "counted"),
    [
        # The prime of 10 on T0 fills its 2 mm retract and counts 8. T1's purge is 0.
        (XL, {0: 8 + 10 + XL_RAM, 1: 10 + 0 + 10 + XL_RAM}),
        # No prime on T0 past its 1.6; a 10 mm purge on T1.
        (INDX, {0: INDX_START + 1.6 + 10, 1: INDX_START + 1.6 + 10 + 10}),
    ],
)
def test_a_print_can_set_its_own_prime_and_tool_change_purge(model, counted):
    sim, p = make(model, [loaded(0), loaded(1)])
    prime, purge = (10, 0) if model == XL else (0, 10)
    finish(sim, p, spec((0, 10), (1, 10), prime_mm=prime, toolchange_purge_mm=purge))
    assert counts(p) == pytest.approx(counted)


@pytest.mark.parametrize(
    ("model", "counted"),
    [
        # T0: its 37 prime, 10, the change's ramming, its later pick's purge of 5 (the 20
        # back counts nothing), 10, and the end's ramming. T1: its 39 prime, its first
        # pick's purge of 5 after the 20 back, 10, and the change's ramming.
        (XL, {0: XL_FIRST + 10 + XL_RAM + 5 + 10 + XL_RAM, 1: XL_OTHER + 5 + 10 + XL_RAM}),
        # T0: the start's 33, its prime, 10, its later pick (the lock move 1.6, then the
        # purge of 5, since the 8.8 refill what its park retracted), and 10. T1: the
        # start's 33, its first pick's 1.6 and purge of 5, and 10.
        (INDX, {0: INDX_START + INDX_PRIME + 10 + 1.6 + 5 + 10, 1: INDX_START + 1.6 + 5 + 10}),
    ],
)
def test_a_print_s_purge_override_covers_a_later_pick_too(model, counted):
    sim, p = make(model, [loaded(0), loaded(1)])
    finish(sim, p, spec((0, 10), (1, 10), (0, 10), toolchange_purge_mm=5))
    assert counts(p) == pytest.approx(counted)


@pytest.mark.parametrize(
    ("model", "overrides", "error"),
    [
        ("8.1.0", {"prime_mm": 5}, "prime_mm needs a multi-tool slicer profile"),
        ("2.1.0", {"toolchange_purge_mm": 5}, "toolchange_purge_mm needs a multi-tool"),
        (XL, {"prime_mm": -1}, "prime_mm must be a number of at least 0, got -1"),
        (INDX, {"toolchange_purge_mm": True}, "toolchange_purge_mm must be a number"),
    ],
)
def test_an_override_needs_a_toolchanger_and_a_length(model, overrides, error):
    sim, p = make(model, [loaded(0), loaded(1)] if model in RATIO else None)
    with pytest.raises(Invalid, match=error):
        p.start_print(spec((0, 10), **overrides))
    assert p.job is None


@pytest.mark.parametrize(
    ("model", "counted"),
    [
        # No wipe tower with one tool, so no ramming at the end: just its 0.8 retract.
        (XL, XL_FIRST + 10),
        (INDX, INDX_START + INDX_PRIME + 10),
    ],
)
def test_a_one_tool_print_on_a_toolchanger_primes_only_its_tool(model, counted):
    sim, p = make(model, [loaded(0), loaded(1)])
    finish(sim, p, spec((1, 10)))
    assert counts(p) == pytest.approx({0: 0, 1: counted})
    assert p.tools[1].path.gap == pytest.approx(0.8) and p.selected_tool is None


def test_a_print_s_retract_keeps_an_auto_retracted_distance():
    sim, p = make(XL, [SimTool(0), loaded(1)])
    p.load(0, source="preset", name="PLA")
    sim.advance(100)
    assert p.tools[0].path.auto_retracted == 55 and p.tools[0].path.gap == 40
    before = counts(p)[0]
    finish(sim, p, spec((0, 10), (1, 10)))
    # The planner drops the -2 and keeps the 55 (gap 40), so the prime deretracts the 55
    # and counts 15, then 39 (`planner.cpp:2231-2236`).
    assert counts(p)[0] - before == pytest.approx(15 + 39 + 10 + XL_RAM)


# Priming runs once, whatever stops the print around it.

BOTH = {0: XL_FIRST + 10 + XL_RAM, 1: XL_OTHER + 9.238 + 10 + XL_RAM}


def test_a_pause_in_the_preheat_primes_once_after_the_resume():
    sim, p = make(XL, [loaded(0), loaded(1)])
    start(sim, p, spec((0, 10), (1, 10), preheat_s=5))
    sim.tick(0.31)
    assert p.job.phase is JobPhase.PREHEAT
    p.pause()
    p.resume()
    assert p.job.phase is JobPhase.PREHEAT and not p.job.primed
    sim.advance(100)
    assert counts(p) == pytest.approx(BOTH)


def test_a_pause_after_the_priming_doesn_t_prime_again():
    sim, p = make(XL, [loaded(0), loaded(1)])
    start(sim, p, spec((0, 10), (1, 10)))
    sim.tick(0.31)
    assert p.job.phase is JobPhase.PRINTING and p.job.primed
    p.pause()
    p.resume()
    sim.advance(100)
    assert counts(p) == pytest.approx(BOTH)
    assert [e.text for e in sim.events].count("start G-code primed T0, T1") == 1


def test_a_cancel_in_the_preheat_primes_nothing():
    sim, p = make(XL, [loaded(0), loaded(1)])
    start(sim, p, spec((0, 10), (1, 10), preheat_s=5))
    sim.tick(0.31)
    p.cancel()
    sim.advance(10)
    # A stopped XL print keeps its tool: the firmware's end retracts T0 8 and
    # auto-retracts it, 23 counted. T1 never moved.
    assert p.state is State.STOPPED
    assert counts(p) == pytest.approx({0: 23, 1: 0})


def test_a_power_panic_in_the_preheat_primes_once_after_the_resume():
    sim, p = make(XL, [loaded(0), loaded(1)])
    start(sim, p, spec((0, 10), (1, 10), preheat_s=5))
    sim.tick(0.31)
    p.reboot(keep_job=True)  # the counters restart at 0, and the gaps at 0, not trusted
    sim.advance(100)
    assert p.state is State.FINISHED
    assert counts(p) == pytest.approx(BOTH)


def test_a_power_panic_after_a_tool_change_doesn_t_prime_or_change_again():
    sim, p = make(XL, [loaded(0), loaded(1)])
    start(sim, p, spec((0, 10), (1, 10)))
    sim.tick(0.31 + 3)  # T0's 10 at 5 mm/s, then 5 on T1
    assert p.selected_tool == 1 and p.job.primed
    left = 20 - p.job.fed_mm
    p.reboot(keep_job=True)  # the counters restart at 0; the XL doesn't retract at a cut
    sim.advance(100)
    assert p.state is State.FINISHED
    # Only the rest of T1's segment and the end's ramming.
    assert counts(p) == pytest.approx({0: 0, 1: left + XL_RAM})


def test_a_snapshot_keeps_the_priming_done():
    sim, p = make(INDX, [loaded(0), loaded(1)])
    start(sim, p, spec((0, 10), (1, 10)))
    sim.tick(0.31)
    sim.restore(sim.snapshot())
    assert sim.printer("p").job.primed


def test_a_one_tool_xl_prints_as_a_direct_printer():
    # One enabled Dwarf: no multi-tool profile, so no priming and no end park. The
    # firmware's end retracts 8 (gap 8) and its auto-retract feeds them back and counts 23.
    sim, p = make(XL, [loaded(0), loaded(1, enabled=False)])
    assert p.profile is None
    with pytest.raises(Invalid, match="prime_mm needs a multi-tool slicer profile"):
        p.start_print(spec((0, 10), prime_mm=5))
    p.start_print(spec((0, 10)))  # the tools-mapping screen doesn't apply
    sim.advance(100)
    assert p.state is State.FINISHED
    assert counts(p) == pytest.approx({0: 10 + 23, 1: 0})
    assert p.selected_tool == 0 and p.tools[0].path.auto_retracted == 55
