"""The bridge on a toolchanger's print where the firmware's preview or print differs from a
single-tool printer's.

The charges are worked as in `tests/test_families.py`: on INDX a move counts past the
tool's 27 mm gap, times 550/576, and a tool change purges 30 on the new tool.
"""

from __future__ import annotations

import logging

from harness import LOC, PLA, ledger, levels, make, spool, spool_x, tool_locations

from simulator.model import MODELS, PrintSpec, Reason, Segment, SimTool, State

INDX = "8.10.0"
SHELF = "shelf"


def indx(tmp_path, tools):
    """A bridge on an eight-tool INDX whose first tools are `tools`, the rest empty, with
    spool N + 1 in tool N's location for each of `tools`."""
    count = MODELS[INDX].max_tools
    locations = tool_locations(LOC, count)
    spools = [spool(t.index + 1, locations[t.index]) for t in tools]
    tools = tools + [SimTool(index) for index in range(len(tools), count)]
    bridge, fake, station = make(tmp_path, spools, model=INDX, tools=tools, auto_retract=True)
    station.poll = lambda: bridge.poll()
    return bridge, fake, station


def totals(fake) -> dict[int, int]:
    return {spool_id: sum(mm) for spool_id, mm in fake.used.items()}


async def test_a_print_waiting_on_the_tools_mapping_screen(tmp_path):
    """While the screen is up the job reads ATTENTION with nothing extruded: the bridge
    counts no print start and charges nothing. After Print, each tool's spool gets its
    moves."""
    tools = [
        SimTool.loaded_with(0, PLA, extruded=1000.0),
        SimTool.loaded_with(1, PLA, extruded=500.0),
    ]
    bridge, fake, station = indx(tmp_path, tools)
    printer = station.printer
    await bridge.poll()  # the baseline
    mark = len(ledger(tmp_path))

    station.begin_print(PrintSpec([Segment(0, 100), Segment(1, 50)], duration_s=2))
    assert printer.job.waits_for(Reason.TOOLS_MAPPING) and printer.state is State.ATTENTION
    for _ in range(5):
        await bridge.poll()
        station.advance(1)
    assert ledger(tmp_path)[mark:] == [] and fake.used == {}
    accounts = bridge.printers[0].tools
    assert all(accounts[t].job_id is None for t in (0, 1))

    printer.tap_through()  # Print
    await station.run_until(lambda: printer.job is None, poll_every=0.3)
    await bridge.poll()
    # T0 100 * 550/576 = 95.49. T1 30 + 50 = 80 * 550/576 = 76.39. The end G-code parks
    # T1 first, so the firmware's end retracts and auto-retracts nothing.
    assert totals(fake) == {1: 95, 2: 76}
    records = ledger(tmp_path)[mark:]
    assert [r for r in records if "event" in r] == []
    assert {r["tool"] for r in records if r.get("spool_id") == 1} == {0}
    assert {r["tool"] for r in records if r.get("spool_id") == 2} == {1}
    assert accounts[0].job_id == accounts[1].job_id == 1
    assert fake.flag(1) is None and fake.flag(2) is None
    await bridge.aclose()


async def test_a_print_through_an_empty_tool_holds_its_usage(tmp_path, caplog):
    """The print goes on through a tool with no filament once the sensors are off, and that
    tool's counter counts with no material: the bridge holds its usage (C7) and charges the
    other tool's spool as usual."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    tools = [SimTool.loaded_with(0, PLA, extruded=1000.0), SimTool(1, extruded=500.0)]
    bridge, fake, station = indx(tmp_path, tools)
    printer = station.printer
    await bridge.poll()  # the baseline
    mark = len(ledger(tmp_path))

    station.begin_print(PrintSpec([Segment(0, 100), Segment(1, 50)], duration_s=2))
    assert str(printer.job.attention) == "tools mapping: T1 has no filament"
    printer.tap_through()  # Print
    printer.tap_through()  # turn the filament sensors off
    await station.run_until(lambda: printer.job is None, poll_every=0.3)
    await bridge.poll()
    # T0 100 * 550/576 = 95.49. T1 counts 30 + 50 = 80 * 550/576 = 76.39 with no material.
    assert station.counter_at(1) == 576 and station.material(1) == (None, None, None)
    assert totals(fake) == {1: 95}
    assert levels(caplog, "holding usage until filament is reported") == ["WARNING"]
    records = ledger(tmp_path)[mark:]
    assert [r for r in records if "event" in r] == []
    assert fake.flag(1) is None and fake.flag(2) is None

    # A known fault, BF24 in docs/bridge-findings.md: the held 76 mm never left a spool,
    # yet the bridge charges it to the next spool reported in T1, with the load's own
    # count, once T1 is loaded. A fix changes this line.
    await station.swap_cycle(tool=1, source="preset", name="PLA")
    await bridge.poll()
    load = station.counter_at(1) - 576
    assert totals(fake)[2] == 576 - 500 + load
    await bridge.aclose()


async def test_a_tool_emptied_by_hand_while_docked_counts_no_unload(tmp_path):
    """BF12, pinned as the bridge behaves today: a finished print leaves no INDX tool
    picked, so a docked tool emptied by hand keeps its type and counts no unload, at the
    boot too. A spool moved into its location stays staged. A menu load on that tool picks
    it first, and the pick's idle sensor clear then counts the unload."""
    tools = [
        SimTool.loaded_with(0, PLA, extruded=1000.0),
        SimTool.loaded_with(1, PLA, extruded=500.0),
    ]
    bridge, fake, station = indx(tmp_path, tools)
    fake.spools[3] = spool_x(3, SHELF)
    printer = station.printer
    await bridge.poll()  # the baseline

    await station.start_print(PrintSpec([Segment(0, 100), Segment(1, 50)], duration_s=2))
    await station.run_until(lambda: printer.job is None, poll_every=0.3)
    await bridge.poll()
    assert printer.selected_tool is None  # the end G-code parked T1
    mark = len(ledger(tmp_path))

    # T1 emptied by hand while the printer is off, and spool 3 moved into its location.
    printer.set_power("off")
    printer.unload(1)
    printer.set_power("on")
    location = tool_locations(LOC, MODELS[INDX].max_tools)[1]
    fake.spools[2]["location"], fake.spools[3]["location"] = SHELF, location
    for _ in range(3):
        await bridge.poll()
        station.advance(1)
    # A known fault, BF12 in docs/bridge-findings.md: with no tool picked, no idle clear
    # runs, so T1 keeps its type, the bridge sees no unload, and spool 3 stays staged. A
    # fix in the bridge changes these lines.
    assert station.tool_at(1).unload_count == 0 and station.material(1)[0] == "PLA"
    records = ledger(tmp_path)[mark:]
    swaps = ("unload", "swap_staged", "swap_committed", "swap_unconfirmed")
    assert [(r["tool"], r["event"]) for r in records if r.get("event") in swaps] == [
        (1, "swap_staged")
    ]

    # A menu load picks T1 first, with no dialog open: the idle clear finds it empty and
    # counts the unload, so the staged swap commits before the load counts.
    mark = len(ledger(tmp_path))
    await station.swap_cycle(tool=1, source="preset", name="PLA")
    await bridge.poll()
    assert station.tool_at(1).unload_count == 1 and printer.selected_tool is None
    records = ledger(tmp_path)[mark:]
    assert [(r["tool"], r["event"]) for r in records if r.get("event") in swaps] == [
        (1, "unload"),
        (1, "swap_committed"),
    ]
    await bridge.aclose()
