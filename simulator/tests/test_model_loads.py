"""Loads and manual moves on each extruder, counted as the firmware's filament tracker counts.

The expected numbers are worked by hand from the firmware's lengths: what goes past the
retraction gap counts, times the feed ratio, 380/400 = 0.95 on a Nextruder and 550/576 on
INDX.
"""

from __future__ import annotations

import pytest

from simulator.model import JobPhase, Material, PrintSpec, Segment, SimTool, State
from simulator.tests.util import loaded, make

INDX_RATIO = 550 / 576
PLA = Material("PLA", adhoc=False)


def menu_load(sim, p) -> tuple[float, float]:
    """Load tool 0 from the menu; return what it counted before and after the type appeared."""
    at_type = []
    sim.listeners.append(
        lambda e: at_type.append(p.tools[0].extruded) if "type set" in e.text else None
    )
    base = p.tools[0].extruded
    p.load(0, source="preset", name="PETG")
    sim.advance(100)
    assert p.tools[0].loaded and p.tools[0].process is None and p.state is State.IDLE
    (typed,) = at_type
    return typed - base, p.tools[0].extruded - typed


def unloaded(model: str, **settings):
    """A printer whose tool 0 was just unloaded from the menu, so its gap is full."""
    sim, p = make(model, [loaded(0, PLA)], **settings)
    p.unload(0)
    sim.advance(10)
    assert p.tools[0].gap == p.model.gap_mm
    return sim, p


# From a full gap: (slow + fast - gap) before the type, then the purge.
FULL_GAP = {
    # MK4S: 40 mm gap; 30 slow + 50 fast = 80, 40 past the gap; 27 mm purge.
    "1.4.1": (40 * 0.95, 27 * 0.95, 8),
    # XL: the MK4's numbers.
    "3.1.0": (40 * 0.95, 27 * 0.95, 8),
    # Core One L: 40 slow + 20 fast = 60, 20 past the gap; 40 mm purge.
    "8.1.0": (20 * 0.95, 40 * 0.95, 8),
    # iX: 40 slow + 45 fast = 85, 45 past the gap; 40 mm purge, no retract after.
    "4.1.0": (45 * 0.95, 40 * 0.95, 0),
    # Core One L INDX: 27 mm gap; 15 slow + 25 fast = 40, 13 past it; the cleaner's
    # 25 mm purge, and the auto-retract after it isn't modeled.
    "8.10.0": (13 * INDX_RATIO, 25 * INDX_RATIO, 0),
}


@pytest.mark.parametrize("model", FULL_GAP)
def test_a_menu_load_from_a_full_gap(model):
    before, after, gap = FULL_GAP[model]
    sim, p = unloaded(model)
    assert menu_load(sim, p) == pytest.approx((before, after))
    # Without a nozzle cleaner the purge ends with an 8 mm retract, which opens the gap.
    assert p.tools[0].gap == pytest.approx(gap)


@pytest.mark.parametrize(
    ("model", "before"),
    [
        ("8.1.0", 60 * 0.95),  # 40 slow + 20 fast, all counted
        ("1.4.1", 80 * 0.95),  # 30 slow + 50 fast
        ("8.10.0", 40 * INDX_RATIO),  # 15 slow + 25 fast
    ],
)
def test_a_load_after_a_boot_counts_from_the_first_mm(model, before):
    sim, p = make(model, [SimTool(0)])
    p.reboot()
    assert p.tools[0].gap == 0
    assert menu_load(sim, p)[0] == pytest.approx(before)


@pytest.mark.parametrize(
    ("model", "after"),
    [
        ("8.1.0", 80 * 0.95),  # the 40 mm purge doubled
        ("1.4.1", 54 * 0.95),  # the 27 mm purge doubled
        ("4.1.0", 80 * 0.95),  # the 40 mm purge doubled
        ("8.10.0", 25 * INDX_RATIO),  # the cleaner's 25 mm, not doubled
    ],
)
def test_a_high_flow_nozzle_doubles_the_purge_except_on_indx(model, after):
    sim, p = unloaded(model)
    p.high_flow = True
    assert menu_load(sim, p)[1] == pytest.approx(after)


def test_the_overrides_apply_and_unset_back_to_the_models():
    sim, p = unloaded("8.1.0", slow_load_mm=10, fast_load_mm=50, purge_mm=5, feed_ratio=1.0)
    # 10 slow leaves 30 of the 40 mm gap, so 20 of the 50 fast count; the 5 mm purge.
    assert menu_load(sim, p) == pytest.approx((20, 5))
    assert p.feed_ratio == 1.0
    sim.settings.update(
        {"slow_load_mm": None, "fast_load_mm": None, "purge_mm": None, "feed_ratio": None}
    )
    p.unload(0)
    sim.advance(10)
    assert menu_load(sim, p) == pytest.approx((20 * 0.95, 40 * 0.95))
    assert p.feed_ratio == 0.95


def test_an_override_of_0_skips_the_step():
    sim, p = unloaded("8.1.0", slow_load_mm=0, fast_load_mm=0)
    # Nothing feeds before the type; the 40 mm purge only fills the gap.
    assert menu_load(sim, p) == (0, 0)
    sim, p = unloaded("4.1.0", slow_load_mm=0, fast_load_mm=0, purge_mm=0)
    p.load(0, source="preset", name="PETG")  # the iX's load is then only the type
    assert p.tools[0].loaded and p.tools[0].material.name == "PETG"
    assert p.tools[0].process is None and p.state is State.IDLE


def test_the_load_purge_override_wins_over_the_setting():
    sim, p = unloaded("8.1.0", purge_mm=5)
    at = p.tools[0]
    p.load(0, source="preset", name="PETG", purge_mm=10)
    sim.advance(100)
    assert at.extruded == pytest.approx((20 + 10) * 0.95)


def test_manual_moves_count_past_the_gap_and_retracts_stop_at_its_limit():
    sim, p = make("8.10.0", [loaded(0, PLA)])
    p.retract(0, 5)
    p.retract(0, 5)
    assert p.tools[0].gap == 10
    p.extrude(0, 4)  # inside the gap: nothing counts
    assert (p.tools[0].gap, p.tools[0].extruded) == (6, 0)
    p.retract(0, 100)  # the gap stops at INDX's 27 mm
    assert p.tools[0].gap == 27
    p.extrude(0, 30)  # 27 refill it, 3 count
    assert p.tools[0].gap == 0
    assert p.tools[0].extruded == pytest.approx(3 * INDX_RATIO)


def printing_runout(model: str):
    """A print that ran 5 mm on a tool with no gap, then ran out."""
    sim, p = make(model, [loaded(0, PLA)])
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
    sim.tick(0.8 + 1)
    assert p.tools[0].extruded == pytest.approx(5 * 0.95)
    p.runout(0)
    assert p.tools[0].gap == 40
    return sim, p


def test_a_reload_during_a_print_on_the_mk4():
    sim, p = printing_runout("1.4.1")
    before = p.tools[0].extruded
    p.load(0)
    sim.advance(100)
    assert p.job.phase is JobPhase.PAUSED
    # Gap 40. Slow 30: gap 10. Fast 50: 10 refill it, 40 count. Purge 27: counts. The
    # purge's retract 8: gap 8. The prime 4: gap 4, nothing counts. The retract 8: gap 12.
    assert p.tools[0].extruded - before == pytest.approx((40 + 27) * 0.95)
    assert p.tools[0].gap == pytest.approx(12)
    p.resume()  # back to the gap 0 the runout found: 12 fed, all refill the gap
    assert p.tools[0].extruded - before == pytest.approx((40 + 27) * 0.95)
    assert p.tools[0].gap == 0
    sim.tick(1)  # 5 mm printed, all counted
    assert p.tools[0].extruded - before == pytest.approx((40 + 27 + 5) * 0.95)


def test_a_reload_during_a_print_on_the_ix():
    sim, p = printing_runout("4.1.0")
    before = p.tools[0].extruded
    p.load(0)
    sim.advance(100)
    # Gap 40. Slow 40: gap 0. Fast 45: counts. Purge 40: counts, and no retract after it.
    # The prime 4: counts. The retract 8: gap 8.
    assert p.tools[0].extruded - before == pytest.approx((45 + 40 + 4) * 0.95)
    assert p.tools[0].gap == pytest.approx(8)
    p.resume()  # back to the gap 0 the runout found: 8 fed, all refill the gap
    assert p.tools[0].extruded - before == pytest.approx((45 + 40 + 4) * 0.95)
    assert p.tools[0].gap == 0
    sim.tick(1)  # 5 mm printed, all counted
    assert p.tools[0].extruded - before == pytest.approx((45 + 40 + 4 + 5) * 0.95)


def test_a_reload_resumes_at_the_gap_the_runout_found():
    sim, p = make("1.4.1", [loaded(0, PLA)])
    p.retract(0, 10)
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
    sim.tick(0.8 + 1)  # 5 mm printed into the 10 mm gap: gap 5, nothing counts
    assert (p.tools[0].gap, p.tools[0].extruded) == (5, 0)
    p.runout(0)
    p.load(0)
    sim.advance(100)
    # As on the MK4 above: (40 + 27) counted, gap 12.
    assert p.tools[0].extruded == pytest.approx((40 + 27) * 0.95)
    assert p.tools[0].gap == pytest.approx(12)
    p.resume()  # 12 - 5 = 7 fed: gap 5, nothing counts
    assert p.tools[0].gap == pytest.approx(5)
    assert p.tools[0].extruded == pytest.approx((40 + 27) * 0.95)
    sim.tick(1)  # 5 mm printed refill the gap
    assert p.tools[0].extruded == pytest.approx((40 + 27) * 0.95)
    sim.tick(1)  # 5 mm printed, all counted
    assert p.tools[0].extruded == pytest.approx((40 + 27 + 5) * 0.95)


def test_a_reload_in_the_preheat_resumes_at_its_gap():
    sim, p = make("1.4.1", [loaded(0, PLA)])
    p.retract(0, 10)
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=5))
    sim.tick(0.8 + 1)
    assert p.job.phase is JobPhase.PREHEAT
    p.runout(0)  # the gap is still the 10 mm retract
    p.load(0)
    sim.advance(100)
    assert p.tools[0].gap == pytest.approx(12)
    p.resume()  # 12 - 10 = 2 fed: gap 10, back to the preheat
    assert p.job.phase is JobPhase.PREHEAT
    assert p.tools[0].gap == pytest.approx(10)
    assert p.tools[0].extruded == pytest.approx((40 + 27) * 0.95)


def test_a_power_loss_after_a_reload_drops_the_restore():
    sim, p = make("1.4.1", [loaded(0, PLA)])
    p.retract(0, 10)
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
    sim.tick(0.8 + 1)
    p.runout(0)  # at gap 5
    p.load(0)
    sim.advance(100)
    p.reboot(keep_job=True)  # the counters and gaps restart at 0
    assert p.job.phase is JobPhase.PAUSED
    p.resume()  # the filament change never got to its restore, so nothing moves
    assert (p.tools[0].extruded, p.tools[0].gap) == (0, 0)


def test_a_plain_pause_and_resume_move_nothing():
    sim, p = make("1.4.1", [loaded(0, PLA)])
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
    sim.tick(0.8 + 1)
    counted = p.tools[0].extruded
    p.pause()
    sim.tick(10)
    p.resume()
    assert (p.tools[0].extruded, p.tools[0].gap) == (counted, 0)


def test_the_page_sees_the_models_defaults():
    sim, p = make("8.10.0", [loaded(0, PLA)])
    p.high_flow = True
    m = p.to_json()["model"]
    assert m["feed_ratio"] == INDX_RATIO
    assert (m["slow_load_mm"], m["fast_load_mm"], m["purge_mm"]) == (15, 25, 25)
    assert p.to_json()["high_flow"]
    sim, p = make("8.1.0", [loaded(0, PLA)])
    p.high_flow = True
    assert p.to_json()["model"]["purge_mm"] == 80
    kinds = {f["name"]: f["kind"] for f in sim.to_json()["setting_fields"]}
    assert kinds["purge_mm"] == kinds["feed_ratio"] == "optional"
    assert (kinds["time_scale"], kinds["start_blip"]) == ("number", "bool")


def test_the_process_shows_the_step_and_what_is_left():
    sim, p = unloaded("8.1.0")
    p.load(0, source="preset", name="PETG")
    sim.tick(4.5)  # the 40 mm slow load, then 5 of the 20 mm fast load
    assert p.to_json()["tools"][0]["process"] == {
        "kind": "load",
        "phase": "FAST",
        "remaining_mm": pytest.approx(15),
    }
