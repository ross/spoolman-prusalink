"""The bridge on a toolchanger's Change all (T11): several swaps in one operation.

The charges are worked as in `tests/test_toolchanger_prints.py`: on INDX a move counts
past the tool's 27 mm gap, times 550/576. From gap 0, an unload counts its 2 mm purge and
17 mm of ramming, 19, and leaves the gap full; a load from a full gap counts 13 of its
slow and fast feeds, the cleaner's 25 mm purge and 8 of its auto-retract, 46; a load from
gap 0 counts 40 + 25 + 8 = 73.
"""

from __future__ import annotations

from harness import PLA, ledger, spool_x
from test_toolchanger_prints import indx, totals

from simulator.model import SimTool

SHELF = "shelf"
SWAPS = ("swap_staged", "unload", "swap_committed", "swap_unconfirmed", "swap_confirmed")


async def test_change_all_is_a_swap_on_each_changed_tool(tmp_path):
    """Each changed tool goes through S1 or S2 on its own, as if it were swapped alone."""
    tools = [
        SimTool.loaded_with(0, PLA, extruded=1000.0),
        SimTool.loaded_with(1, PLA, extruded=500.0),
    ]
    bridge, fake, station = indx(tmp_path, tools)
    printer = station.printer
    printer.selected_tool = None  # INDX docked its tool
    locations = bridge.config.printers[0].tools
    fake.spools[3] = spool_x(3, SHELF, material="PETG")
    fake.spools[4] = spool_x(4, SHELF, material="PETG")
    await bridge.poll()  # the baseline
    mark = len(ledger(tmp_path))

    # T0 is Spoolman first (S1): spool 3 replaces spool 1 before the change. T1 is filament
    # first (S2): Spoolman changes after. T2 has no type and no spool: it only loads.
    fake.spools[1]["location"], fake.spools[3]["location"] = SHELF, locations[0]
    await bridge.poll()
    printer.change_all([0, 1, 2], name="PETG")
    await station.run_until(
        lambda: all(t.process is None for t in printer.tools.values()),
        poll_every=1.0,
        limit=120,
    )
    await bridge.poll()
    events = [(r["tool"], r["event"]) for r in ledger(tmp_path)[mark:] if r.get("event") in SWAPS]
    assert events == [
        (0, "swap_staged"),
        (0, "unload"),
        (0, "swap_committed"),
        (1, "unload"),
        (1, "swap_unconfirmed"),
    ]
    # T0: the unload's 19 * 550/576 = 18.14 reads 1018 and goes to spool 1; the load's
    # 46 * 550/576 = 43.92 reads 1062 and goes to spool 3. T1 reads 518 after its unload
    # and 562 after its load, all on spool 2 while the swap is open.
    assert totals(fake) == {1: 18, 3: 44, 2: 62}
    assert "filament was unloaded with no Spoolman change" in fake.flag(2)

    fake.spools[2]["location"], fake.spools[4]["location"] = SHELF, locations[1]
    await bridge.poll()
    # The Spoolman change confirms T1's swap and moves the load's 44 to spool 4.
    assert (1, "swap_confirmed") in [(r["tool"], r.get("event")) for r in ledger(tmp_path)]
    assert totals(fake) == {1: 18, 3: 44, 2: 18, 4: 44}
    assert fake.flag(2) is None
    # T2's load, 73 * 550/576 = 69.7, has no spool in its location: it's held (S6).
    assert printer.tools[2].extruded > 69
    assert all("use_length" not in r for r in ledger(tmp_path) if r["tool"] == 2)
