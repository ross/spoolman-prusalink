"""A printer with no tool selected: nothing counts, the gap stays, and the idle sensor clear
doesn't run (`track_extruder_move` under `tools.virtual_tool.has_value()` in
`lib/Marlin/Marlin/src/module/planner.cpp`; `filament_sensors_handler.cpp:294-312`).

Nothing sets `selected_tool` to None on today's printers, so these tests set it directly.
"""

from __future__ import annotations

import pytest

from simulator.feed import Direct, Toolchanger
from simulator.model import Material, State
from simulator.tests.util import PLA, loaded, make, run, sliced

CORE_ONE_L, XL, INDX = "8.1.0", "3.1.0", "8.10.0"


def none_selected(model: str, tools=None, **settings):
    sim, p = make(model, tools, **settings)
    p.selected_tool = None
    return sim, p


@pytest.mark.parametrize("model", [CORE_ONE_L, INDX])
def test_extrude_counts_nothing_and_leaves_the_gap(model):
    """The auto-retract hook still feeds a stored distance back, through the planner, which
    doesn't count it either (`AutoRetract::maybe_deretract_to_nozzle` acts on the physical
    tool and moves through `Planner::buffer_segment`)."""
    _, p = none_selected(model)
    path = p.tools[0].path
    path.gap, path.auto_retracted = 5.0, 8.0
    p.extrude(0, 30)
    assert p.tools[0].extruded == 0
    assert (path.gap, path.gap_valid, path.auto_retracted) == (5.0, True, 0.0)


@pytest.mark.parametrize("model", [CORE_ONE_L, INDX])
def test_retract_leaves_the_gap_and_forgets_the_auto_retract(model):
    _, p = none_selected(model)
    path = p.tools[0].path
    path.gap, path.auto_retracted = 5.0, 8.0
    p.retract(0, 10)
    assert p.tools[0].extruded == 0
    assert (path.gap, path.gap_valid, path.auto_retracted) == (5.0, True, None)


def test_the_idle_clear_needs_a_selected_tool():
    sim, p = none_selected(INDX, [loaded(0), loaded(1)])
    p.tools[1].path.loaded = False
    run(sim, 1)
    assert p.tools[1].material == PLA and p.tools[1].unload_count == 0
    p.selected_tool = 1
    run(sim, 1)
    assert p.tools[1].material is None and p.tools[1].unload_count == 1


def test_boot_with_none_selected_skips_the_idle_clear():
    _, p = none_selected(CORE_ONE_L)
    p.tools[0].path.loaded = False
    p.reboot()
    assert p.tools[0].material == PLA and p.tools[0].unload_count == 0


@pytest.mark.parametrize("model", [CORE_ONE_L, XL])
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
