"""A toolchanger picks the tool a menu load or unload works on, and INDX docks it after
(`show_tool_selector_dialog` in `src/gui/MItem_filament.cpp`; `M701_2.cpp:135-140,
194-197`). Move E moves only the picked tool, and the Pick/park menu parks it."""

from __future__ import annotations

import pytest

from simulator.model import Busy, Invalid, JobPhase, PrintSpec, Segment, SimTool
from simulator.tests.util import PLA, loaded, make, run

XL, INDX = "3.1.0", "8.10.0"


def picks(sim) -> list[str]:
    """The picks and docks the printer logged, in order."""
    return [e.text for e in sim.events if e.text.startswith(("pick ", "dock "))]


@pytest.mark.parametrize(("model", "after"), [(XL, 1), (INDX, None)])
def test_a_menu_load_picks_its_tool_and_indx_docks_it(model, after):
    sim, p = make(model, [loaded(0), SimTool(1)])
    p.load(1, source="preset", name="PETG")
    assert p.selected_tool == 1
    sim.advance(60)
    assert p.tools[1].path.loaded and p.tools[1].material.name == "PETG"
    # The load counts on the tool it picked, not on the one that was picked before.
    assert p.tools[1].extruded > 0 and p.tools[0].extruded == 0
    assert p.selected_tool == after
    assert picks(sim) == ["pick T1"] + ([] if after == 1 else ["dock T1"])


@pytest.mark.parametrize(("model", "after"), [(XL, 1), (INDX, None)])
def test_a_menu_unload_picks_its_tool_and_indx_docks_it(model, after):
    sim, p = make(model, [loaded(0), loaded(1)])
    p.unload(1)
    assert p.selected_tool == 1
    sim.advance(60)
    assert p.tools[1].unload_count == 1 and p.tools[0].unload_count == 0
    assert p.tools[1].extruded > 0 and p.tools[0].extruded == 0  # the ramming
    assert p.selected_tool == after


def test_a_load_on_a_docked_tool_emptied_by_hand_counts_the_unload_at_the_pick():
    """The pick has no dialog open, so the idle clear reads the newly picked tool's sensor
    before the load starts: its stored type over no filament counts an unload."""
    sim, p = make(INDX, [loaded(0), loaded(1)])
    p.selected_tool = None
    p.tools[1].path.loaded = False
    run(sim, 1)  # docked: the idle clear doesn't read it
    assert p.tools[1].material == PLA and p.tools[1].unload_count == 0
    p.load(1, source="preset", name="PETG")
    assert p.tools[1].unload_count == 1 and p.tools[1].material is None
    sim.advance(60)
    assert p.tools[1].unload_count == 1 and p.tools[1].material.name == "PETG"


def test_an_unload_with_a_sensor_fault_counts_its_unload_at_the_pick():
    sim, p = make(XL, [loaded(0), loaded(1)])
    p.set_sensor_fault(1, True)
    p.unload(1)
    assert p.tools[1].unload_count == 1 and p.tools[1].material is None
    sim.advance(60)
    assert p.tools[1].unload_count == 1  # the unload itself finds no type to count


def test_an_already_picked_tool_isnt_picked_again():
    sim, p = make(XL, [loaded(0), loaded(1)])
    p.unload(0)
    sim.advance(60)
    assert p.selected_tool == 0 and picks(sim) == []


def test_the_preview_s_load_picks_with_no_clear_and_indx_docks_after():
    """The preview's `M701 T<n>` picks inside its dialog, so no idle clear runs there, and
    it docks after because it doesn't resume a print."""
    sim, p = make(INDX, [loaded(0)])
    p.tools[0].path.loaded = False  # emptied by hand while docked: the type stays
    p.selected_tool = None
    p.start_print(PrintSpec([Segment(0, 10)], preheat_s=0))
    sim.tick(0.5)
    p.tap_through()  # Yes: load
    p.load(0, source="preset", name="PETG")
    assert p.job.phase is JobPhase.PREVIEW_LOAD and p.selected_tool == 0
    assert p.tools[0].unload_count == 0
    while p.tools[0].process is not None:
        sim.tick(0.1)
    assert p.job.phase is JobPhase.BLIP and p.selected_tool is None
    assert p.tools[0].unload_count == 0
    sim.advance(5)
    assert p.selected_tool == 0  # the print picks its first tool


def test_a_reload_in_a_print_neither_picks_nor_docks():
    sim, p = make(INDX, [loaded(0)])
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
    sim.advance(3)
    p.runout(0)
    p.load(0)  # keep: the reload the runout waits for
    sim.advance(60)
    assert p.job.phase is JobPhase.PAUSED and p.selected_tool == 0
    assert picks(sim) == []


def test_a_load_cut_short_leaves_the_indx_tool_picked():
    sim, p = make(INDX, [loaded(0), SimTool(1)])
    p.load(1, source="preset", name="PLA")
    sim.tick(0.5)
    p.reboot()
    assert p.tools[1].process is None and p.selected_tool == 1


@pytest.mark.parametrize("move", ["extrude", "retract"])
def test_a_manual_move_needs_the_picked_tool(move):
    _, p = make(XL, [loaded(0), loaded(1)])
    with pytest.raises(Invalid, match="^T1 isn't the picked tool$"):
        getattr(p, move)(1, 5)
    getattr(p, move)(0, 5)
    p.select_tool(1)
    getattr(p, move)(1, 5)
    assert p.tools[1].path.gap > 0 if move == "retract" else p.tools[1].extruded > 0


@pytest.mark.parametrize("model", [XL, INDX])
def test_park_tool_leaves_none_picked(model):
    sim, p = make(model, [loaded(0), loaded(1)])
    p.park_tool()
    assert p.selected_tool is None
    assert [e.text for e in sim.events if e.kind == "action"] == ["park T0"]
    with pytest.raises(Busy, match="^no tool picked$"):
        p.park_tool()
    with pytest.raises(Busy, match="^no tool picked$"):
        p.extrude(0, 5)


def test_park_tool_follows_the_pick_rules():
    _, p = make("8.1.0")
    with pytest.raises(Invalid, match="^COREONEL has one tool$"):
        p.park_tool()
    sim, p = make(XL, [loaded(0), loaded(1)])
    p.start_print(PrintSpec([Segment(0, 100)], preheat_s=0))
    with pytest.raises(Busy, match="^can't pick a tool while PRINTING$"):
        p.park_tool()
    _, p = make(XL, [loaded(0), loaded(1)])
    p.set_power("off")
    p.park_tool()  # by hand
    assert p.selected_tool is None
