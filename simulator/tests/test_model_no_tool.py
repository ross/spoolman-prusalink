"""A toolchanger with no tool picked: no filament moves or counts, and the idle sensor clear
doesn't run (`Planner::buffer_segment` in `lib/Marlin/Marlin/src/module/planner.cpp`;
`filament_sensors_handler.cpp:294-312`).

An INDX docks its tool after a menu load or unload, and `park_tool` parks either
toolchanger's. These tests set `selected_tool` to None directly.
"""

from __future__ import annotations

import pytest

from simulator.feed import Direct, Toolchanger
from simulator.model import Busy, Invalid, Material, PrintSpec, Segment, State
from simulator.tests.util import PLA, loaded, make, run, sliced

CORE_ONE_L, XL, INDX = "8.1.0", "3.1.0", "8.10.0"


def none_selected(model: str, tools=None, **settings):
    sim, p = make(model, tools, **settings)
    p.selected_tool = None
    return sim, p


@pytest.mark.parametrize("model", [XL, INDX])
@pytest.mark.parametrize("move", ["extrude", "retract"])
def test_a_manual_move_is_refused(model, move):
    """Move E shows "No tool" and sends no E with none picked (`screen_menu_move.cpp`), so
    nothing moves and the stored distance stays."""
    _, p = none_selected(model)
    path = p.tools[0].path
    path.gap, path.auto_retracted = 5.0, 8.0
    with pytest.raises(Busy, match="^no tool picked$"):
        getattr(p, move)(0, 30)
    assert p.tools[0].extruded == 0
    assert (path.gap, path.gap_valid, path.auto_retracted) == (5.0, True, 8.0)


def test_an_unload_picks_its_tool_and_counts():
    """The menu picks the tool before `M702`, so the unload moves and counts: the 2 mm purge
    feeds back the stored 5 mm into the 5 mm gap first and counts 2, and the ramming counts
    17 (`test_model_auto_retract.py` works it out): 19 * 550 / 576. Then INDX docks it."""
    sim, p = none_selected(INDX, [loaded(0), loaded(1)])
    path = p.tools[0].path
    path.gap, path.auto_retracted = 5.0, 5.0
    p.unload(0)
    assert p.selected_tool == 0
    sim.tick(60)
    assert p.tools[0].extruded == pytest.approx(19 * 550 / 576)
    assert p.tools[0].unload_count == 1 and p.selected_tool is None


def test_the_idle_clear_needs_a_selected_tool():
    sim, p = none_selected(INDX, [loaded(0), loaded(1)])
    p.tools[1].path.loaded = False
    run(sim, 1)
    assert p.tools[1].material == PLA and p.tools[1].unload_count == 0
    p.selected_tool = 1
    run(sim, 1)
    assert p.tools[1].material is None and p.tools[1].unload_count == 1


def test_boot_with_none_selected_skips_the_idle_clear():
    _, p = none_selected(INDX)
    p.tools[0].path.loaded = False
    p.reboot()
    assert p.tools[0].material == PLA and p.tools[0].unload_count == 0


@pytest.mark.parametrize("model", [XL, INDX])
def test_a_sync_jobs_end_moves_nothing(model):
    """The end skips the retract to 8 mm and the auto-retract, and still stores each valid
    gap, as `pre_finalize_print` does for every hotend (`marlin_server.cpp`)."""
    sim, p = none_selected(model)
    p.upload("a.gcode", sliced(p, "M117 hi\n"), print_after=True, overwrite=False)
    sim.advance(10)
    path = p.tools[0].path
    assert p.state is State.FINISHED
    assert p.tools[0].extruded == 0
    assert (path.gap, path.gap_valid, path.auto_retracted) == (0.0, True, 0.0)


def test_no_selected_tool_isnt_one_a_runout_or_a_disable_names():
    sim, p = make(INDX, [loaded(0), loaded(1)])
    p.start_print(PrintSpec([Segment(0, 100)]))
    sim.tick(0.5)
    p.tap_through()  # Print on the tools-mapping screen
    sim.tick(5)  # the print is under way
    p.selected_tool = None
    with pytest.raises(Invalid, match="T0 isn't the selected tool"):
        p.runout(0)

    _, p = none_selected(INDX, [loaded(0), loaded(1)])
    assert p.to_json()["tools"][0]["actions"]["set_tool_enabled"] is None
    p.set_tool_enabled(0, False)
    assert not p.tools[0].enabled


def test_json_and_a_snapshot_keep_none():
    sim, p = none_selected(INDX, [loaded(0), loaded(1)])
    assert p.to_json()["selected_tool"] is None
    sim.restore(sim.snapshot())
    assert sim.printer("p").selected_tool is None


@pytest.mark.parametrize(
    ("model", "system", "count"), [(CORE_ONE_L, Direct, 1), (INDX, Toolchanger, 2)]
)
def test_the_feed_systems_with_and_without_a_selected_tool(model, system, count):
    _, p = make(model, [loaded(i, Material("PETG")) for i in range(count)])
    feed = p.feed
    assert isinstance(feed, system)
    one = p.tools[count - 1]
    # A move names the tool whose path moves, picked or not.
    assert feed.credited(p, one) is one
    assert feed.idle_clear_tool(p) is p.tools[0]
    p.selected_tool = 7  # not a configured tool: no sensor to read
    assert feed.idle_clear_tool(p) is None
    p.selected_tool = None
    assert feed.credited(p, one) is None
    assert feed.idle_clear_tool(p) is None
