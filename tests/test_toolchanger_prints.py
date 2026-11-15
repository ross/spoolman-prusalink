"""The bridge on a toolchanger's print where the firmware's preview or print differs from a
single-tool printer's.

The charges are worked as in `tests/test_families.py`: on INDX a move counts past the
tool's 27 mm gap, times 550/576, with the stock slicer profile's moves. From gap 0, a print
of 100 mm on T0 then 50 on T1 counts on T0 the start's 25 mm purge and 8 mm auto-retract,
its 1.6 + 5.4907 mm prime and 100: 140.0907 * 550/576 = 133.77. On T1 the same 25 + 8, its
first pick's 1.6 + 4.1596 and 50: 88.7596 * 550/576 = 84.75.
"""

from __future__ import annotations

import logging

import pytest
from harness import LOC, PLA, ledger, levels, make, spool, spool_x, tool_locations

from simulator.model import MODELS, Join, PrintSpec, Reason, Segment, SimTool, State

INDX = "8.10.0"
SHELF = "shelf"
HINT = "printing with no filament reported"


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
    # T0 133.77 and T1 84.75. The end G-code parks T1 first, so the firmware's end
    # retracts and auto-retracts nothing.
    assert totals(fake) == {1: 133, 2: 84}
    records = ledger(tmp_path)[mark:]
    assert [r for r in records if "event" in r] == []
    assert {r["tool"] for r in records if r.get("spool_id") == 1} == {0}
    assert {r["tool"] for r in records if r.get("spool_id") == 2} == {1}
    assert accounts[0].job_id == accounts[1].job_id == 1
    assert fake.flag(1) is None and fake.flag(2) is None
    await bridge.aclose()


async def test_a_print_through_an_empty_tool_reports_its_usage_lost(tmp_path, caplog):
    """The print goes on through a tool with no filament once the sensors are off, and that
    tool's counter counts with no material: the bridge reports its usage lost (C7), charges
    the other tool's spool as usual, and charges the next load its own count only."""
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
    await station.run_until(lambda: station.counter_at(1) > 500)
    await bridge.poll()  # printing on T1
    await station.run_until(lambda: printer.job is None)
    await bridge.poll()  # the rest of the print, first seen with no job
    # T0 133.77. T1 counts 84.75 with no material.
    assert station.counter_at(1) == 584 and station.material(1) == (None, None, None)
    assert totals(fake) == {1: 133}
    assert levels(caplog, "holding usage until filament is reported") == []
    records = ledger(tmp_path)[mark:]
    lost = [(r["event"], r["tool"], r["mm"]) for r in records if "event" in r]
    assert [(event, tool) for event, tool, _ in lost] == [("lost_empty", 1)] * 2
    assert sum(mm for _, _, mm in lost) == 584 - 500
    assert fake.flag(1) is None and fake.flag(2) is None
    # BF25: T1 alone warns, once, and the poll that sees the print over ends it. The idle
    # empty tools, T2 to T7, print nothing and raise nothing.
    hints = [r.getMessage().split(HINT)[0] for r in caplog.records if HINT in r.getMessage()]
    t1 = tool_locations(LOC, 8)[1]
    assert hints == [f"[{t1}] ", f"[{t1}] no longer: "]

    # BF24: the 84 mm never left a spool, so T1's next load is charged its own count.
    await station.swap_cycle(tool=1, source="preset", name="PLA")
    await bridge.poll()
    load = station.counter_at(1) - 584
    assert totals(fake)[2] == load
    await bridge.aclose()


async def test_the_tools_mapping_screens_sensors_off_warns_before_any_usage_is_lost(
    tmp_path, caplog
):
    """BF25: the tools-mapping screen's answer turns the sensors off (F29). The first poll
    that reads the setting off warns, once for the printer, before T1 loses anything. The
    setting stays off after the print, and so does the warning."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    off = "printer: filament sensors are off"
    tools = [SimTool.loaded_with(0, PLA, extruded=1000.0), SimTool(1, extruded=500.0)]
    bridge, fake, station = indx(tmp_path, tools)
    printer = station.printer
    await bridge.poll()  # the baseline
    mark = len(ledger(tmp_path))

    station.begin_print(PrintSpec([Segment(0, 100), Segment(1, 50)], duration_s=2))
    printer.tap_through()  # Print
    printer.tap_through()  # turn the filament sensors off
    assert not printer.sensors and station.counter_at(1) == 500
    await bridge.poll()
    assert levels(caplog, off) == ["WARNING"]
    assert levels(caplog, HINT) == []
    assert [r for r in ledger(tmp_path)[mark:] if "event" in r] == []

    await station.run_until(lambda: printer.job is None, poll_every=0.3)
    await bridge.poll()
    assert [r["event"] for r in ledger(tmp_path)[mark:] if "event" in r][0] == "lost_empty"
    t1 = tool_locations(LOC, 8)[1]
    assert [r.getMessage() for r in caplog.records if HINT in r.getMessage()][0] == (
        f"[{t1}] printing with no filament reported; that usage is charged to no spool. The "
        "filament sensors are off: see the warning about them"
    )
    assert levels(caplog, off) == ["WARNING"]

    printer.set_sensors(True)
    await bridge.poll()
    assert levels(caplog, "printer: no longer: filament sensors are off") == ["WARNING"]
    await bridge.aclose()


async def test_an_idle_empty_tool_keeps_its_held_usage_through_a_print_on_another(tmp_path, caplog):
    """T1 holds usage with no material, as a menu load stopped before the type does. A print
    on T0 alone doesn't move T1's counter, so T1's usage stays held through the print and
    goes to its spool at the next load (C7). No usage is lost, so no tool warns (BF25)."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    tools = [SimTool.loaded_with(0, PLA, extruded=1000.0), SimTool(1, extruded=500.0)]
    bridge, fake, station = indx(tmp_path, tools)
    printer = station.printer
    await bridge.poll()  # the baseline
    station.tool_at(1).extruded += 20  # the feed before the type
    await bridge.poll()
    mark = len(ledger(tmp_path))

    station.begin_print(PrintSpec([Segment(0, 100)], duration_s=2))
    printer.tap_through()  # Print
    await station.run_until(lambda: printer.job is None, poll_every=0.3)
    await bridge.poll()  # the poll after the print
    assert station.counter_at(1) == 520 and station.material(1) == (None, None, None)
    assert [r for r in ledger(tmp_path)[mark:] if "event" in r] == []
    assert set(totals(fake)) == {1}
    assert levels(caplog, HINT) == []

    await station.swap_cycle(tool=1, source="preset", name="PLA")
    await bridge.poll()
    assert totals(fake)[2] == station.counter_at(1) - 500
    await bridge.aclose()


async def test_a_load_on_an_idle_empty_tool_right_after_a_print_is_charged(tmp_path, caplog):
    """A print on T0 alone ends, and a menu load of the empty T1 starts before the next
    poll. T1 never counted in the print, so the load's feed isn't the print's tail: it's
    held until the type appears and charged in full to T1's spool, and nothing warns."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    tools = [SimTool.loaded_with(0, PLA, extruded=1000.0), SimTool(1, extruded=500.0)]
    bridge, fake, station = indx(tmp_path, tools)
    printer = station.printer
    await bridge.poll()  # the baseline
    mark = len(ledger(tmp_path))

    station.begin_print(PrintSpec([Segment(0, 100)], duration_s=2))
    printer.tap_through()  # Print
    await station.run_until(lambda: printer.job is None, poll_every=0.3)
    await station.swap_cycle(poll_every=0.1, tool=1, source="preset", name="PLA")
    await bridge.poll()
    records = ledger(tmp_path)[mark:]
    assert [r for r in records if r.get("event") == "lost_empty"] == []
    assert totals(fake)[2] == station.counter_at(1) - 500
    assert levels(caplog, HINT) == []
    await bridge.aclose()


async def test_an_idle_empty_tool_has_no_tail_after_a_power_panic_resume(tmp_path, caplog):
    """The first poll after a power cut lands once the resume prints on T0. T1, empty, reads
    0 since the boot and printed nothing, so a menu load of T1 right after the print is
    held until the type appears and charged in full to T1's spool, and nothing warns."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    tools = [SimTool.loaded_with(0, PLA, extruded=1000.0), SimTool(1, extruded=500.0)]
    bridge, fake, station = indx(tmp_path, tools)
    printer = station.printer
    await bridge.poll()  # the baseline
    station.begin_print(PrintSpec([Segment(0, 300)], duration_s=20))
    printer.tap_through()  # Print
    await station.run_until(lambda: station.counter_at(0) > 1000)
    await bridge.poll()
    station.advance(1)
    await bridge.poll()
    printer.set_power("off")
    printer.set_power("on", resume=True)
    station.tick_until(lambda: printer.state is State.PRINTING and printer.job is not None)
    await bridge.poll()
    assert bridge.printers[0].tools[1].printed_empty is False
    mark = len(ledger(tmp_path))

    await station.run_until(lambda: printer.job is None, poll_every=0.3, limit=120)
    start = station.counter_at(1)
    await station.swap_cycle(poll_every=0.1, tool=1, source="preset", name="PLA")
    await bridge.poll()
    records = ledger(tmp_path)[mark:]
    assert [r for r in records if r.get("event") == "lost_empty"] == []
    assert totals(fake)[2] == station.counter_at(1) - start
    assert levels(caplog, HINT) == []
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


async def test_a_change_on_a_tool_the_print_has_not_used_follows_p2(tmp_path):
    """T10: Tune > Change filament on T1 while the print is on T0 counts T1's unload
    mid-print. T1 hasn't extruded in this job, and the change's ramming counts less than
    `FIRST_EXTRUSION_MM`, so it isn't T1's print start (Q4): the swap stays open, a
    Spoolman change during the pause confirms it (P2), and the reload goes to the new
    spool."""
    tools = [
        SimTool.loaded_with(0, PLA, extruded=1000.0),
        SimTool.loaded_with(1, PLA, extruded=500.0),
    ]
    bridge, fake, station = indx(tmp_path, tools)
    fake.spools[3] = spool_x(3, SHELF)
    printer = station.printer
    await bridge.poll()  # the baseline
    mark = len(ledger(tmp_path))

    await station.start_print(PrintSpec([Segment(0, 100)], duration_s=20))
    station.advance(2)
    await bridge.poll()
    printer.change_filament(1)
    await bridge.poll()
    # T1's park retract, 2 mm purge and ramming count 17 * 550/576 = 16.23: 516.
    assert station.counter_at(1) == 516 and station.tool_at(1).unload_count == 1
    swaps = ("unload", "swap_unconfirmed", "swap_assumed", "swap_staged", "swap_confirmed")
    events = [(r["tool"], r["event"]) for r in ledger(tmp_path)[mark:] if r.get("event") in swaps]
    assert events == [(1, "unload"), (1, "swap_unconfirmed")]
    assert "unloaded with no Spoolman change" in fake.flag(2)

    # Spool 3 goes into T1's location while the printer waits for the reload (P2).
    mark = len(ledger(tmp_path))
    location = tool_locations(LOC, MODELS[INDX].max_tools)[1]
    fake.spools[2]["location"], fake.spools[3]["location"] = SHELF, location
    await bridge.poll()
    await station.swap_cycle(tool=1, poll_every=1.0)
    assert printer.state is State.PAUSED
    printer.resume()
    await station.run_until(lambda: printer.job is None, poll_every=1.0, limit=60)
    await bridge.poll()
    events = [(r["tool"], r["event"]) for r in ledger(tmp_path)[mark:] if r.get("event") in swaps]
    assert events == [(1, "swap_confirmed")]
    # Spool 2 keeps the 16.23 before the unload. The reload counts 50 more, 47.75, which
    # reads 563, all on spool 3. T0's 100 and its priming, G427's 25 + 8 and the
    # profile's 1.6 + 5.4907, make 140.0907 * 550/576 = 133.77 on spool 1.
    assert station.counter_at(1) == 563
    assert totals(fake) == {1: 133, 2: 16, 3: 47}
    assert fake.flag(2) is None
    await bridge.aclose()


async def test_a_change_on_a_tool_the_print_has_not_used_follows_p3(tmp_path):
    """T10 with no Spoolman change: the unload in a job still pending on T1 starts T1's
    print without the start's effects, so the reload's count doesn't close the swap as
    assumed. The swap stays open after the print, as P3 says, and the reload is charged
    to the old spool until Spoolman says otherwise."""
    tools = [
        SimTool.loaded_with(0, PLA, extruded=1000.0),
        SimTool.loaded_with(1, PLA, extruded=500.0),
    ]
    bridge, fake, station = indx(tmp_path, tools)
    printer = station.printer
    await bridge.poll()  # the baseline
    mark = len(ledger(tmp_path))

    await station.start_print(PrintSpec([Segment(0, 100)], duration_s=20))
    station.advance(2)
    await bridge.poll()
    printer.change_filament(1)
    await bridge.poll()
    await station.swap_cycle(tool=1, poll_every=1.0)
    printer.resume()
    await station.run_until(lambda: printer.job is None, poll_every=1.0, limit=60)
    await bridge.poll()
    swaps = ("unload", "swap_unconfirmed", "swap_assumed", "swap_staged", "swap_confirmed")
    events = [(r["tool"], r["event"]) for r in ledger(tmp_path)[mark:] if r.get("event") in swaps]
    assert events == [(1, "unload"), (1, "swap_unconfirmed")]
    # T1's 16.23 before the unload and the reload's 47.75: 563, all on spool 2.
    assert station.counter_at(1) == 563 and totals(fake) == {1: 133, 2: 63}
    t1 = bridge.printers[0].tools[1]
    assert t1.job_id == 1 and t1.swap is not None and t1.provisional is None
    assert "unloaded with no Spoolman change" in fake.flag(2)
    await bridge.aclose()


async def test_a_spool_join_counts_an_unload_on_the_old_tool_mid_print(tmp_path):
    """T9 and BF13, pinned as the bridge behaves today: a runout on T0 with a spool join to
    T1 counts T0's unload mid-print and goes on with T1. The bridge opens an unconfirmed
    swap on T0, which stays open after the print (P3), and flags spool 1 for the unload
    and, with plenty left in Spoolman, as run out mid-print (M5a). T1's purge and the rest
    of the print are charged to spool 2, with no event."""
    tools = [
        SimTool.loaded_with(0, PLA, extruded=1000.0),
        SimTool.loaded_with(1, PLA, extruded=500.0),
    ]
    bridge, fake, station = indx(tmp_path, tools)
    fake.spools[1]["filament"]["weight"] = 1000.0
    fake.spools[1]["used_weight"], fake.spools[1]["remaining_weight"] = 100.0, 900.0
    printer = station.printer
    await bridge.poll()  # the baseline
    mark = len(ledger(tmp_path))

    spec = PrintSpec([Segment(0, 100)], duration_s=20, spool_join=[Join(0, 1)])
    await station.start_print(spec)
    station.tick_until(lambda: printer.job.fed_mm >= 23 - 1e-9)
    await bridge.poll()
    assert printer.job.fed_mm == pytest.approx(23)
    printer.runout(0)
    await bridge.poll()
    # T0: G427's 25 + 8 and the prime's 1.6 + 5.4907, 23 printed, then the join's 2 mm
    # unload purge and ramming, 19: 82.0907 * 550/576 = 78.39. The dialog reads ATTENTION.
    assert printer.state is State.ATTENTION and station.counter_at(0) == 1078
    assert station.tool_at(0).unload_count == 1 and station.counter_at(1) == 500
    # A known fault, BF13 in docs/bridge-findings.md: the join's unload looks like a
    # runout waiting for a reload of the same tool. A fix in the bridge changes these
    # lines.
    swaps = ("unload", "swap_unconfirmed", "swap_assumed", "swap_staged", "swap_confirmed")
    events = [(r["tool"], r["event"]) for r in ledger(tmp_path)[mark:] if r.get("event") in swaps]
    assert events == [(0, "unload"), (0, "swap_unconfirmed")]

    mark = len(ledger(tmp_path))
    await station.run_until(lambda: printer.job is None, poll_every=1.0, limit=60)
    await bridge.poll()
    # T1 isn't in the file, so G427 doesn't purge it: the join's 25 + 8, then the other
    # 77 mm, 110 * 550/576 = 105.03.
    assert station.counter_at(1) == 605 and station.tool_at(1).unload_count == 0
    assert totals(fake) == {1: 78, 2: 105}
    records = ledger(tmp_path)[mark:]
    assert [r for r in records if "event" in r] == []
    assert {r["tool"] for r in records if r.get("spool_id") == 2} == {1}
    assert "filament was unloaded with no Spoolman change" in fake.flag(1)
    assert "unloaded mid-print with 900 g left" in fake.flag(1)
    assert fake.flag(2) is None
    assert bridge.printers[0].tools[0].swap is not None
    await bridge.aclose()
