"""The bridge on a toolchanger's print where the firmware's preview or print differs from a
single-tool printer's.

The charges are worked as in `tests/test_families.py`: on INDX a move counts past the
tool's 27 mm gap, times 550/576, and a tool change purges 30 on the new tool.
"""

from __future__ import annotations

from harness import LOC, PLA, ledger, make, spool, tool_locations

from simulator.model import MODELS, PrintSpec, Reason, Segment, SimTool, State

INDX = "8.10.0"


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
    # T0 100 * 550/576 = 95.49. T1 30 + 50 = 80 * 550/576 = 76.39. The end's 8 mm retract
    # is fully retracted on INDX: no auto-retract.
    assert totals(fake) == {1: 95, 2: 76}
    records = ledger(tmp_path)[mark:]
    assert [r for r in records if "event" in r] == []
    assert {r["tool"] for r in records if r.get("spool_id") == 1} == {0}
    assert {r["tool"] for r in records if r.get("spool_id") == 2} == {1}
    assert accounts[0].job_id == accounts[1].job_id == 1
    assert fake.flag(1) is None and fake.flag(2) is None
