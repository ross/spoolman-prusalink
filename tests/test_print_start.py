"""What starts a print: a rise of more than `FIRST_EXTRUSION_MM` past where the bridge
first saw the job (Q4), so a cancel's ramming in the preheat isn't a print start (BF10).

These tests run the simulator with the auto-retract on, which the harness leaves off by
default (BF22): the ramming at a cancel is what they're about.
"""

from __future__ import annotations

import pytest
from harness import LOC, PLA, ledger, make, spool, spool_x

from integration.station import Station
from simulator.model import (
    MODELS,
    Feed,
    Firmware,
    JobPhase,
    PrintSpec,
    Reason,
    Segment,
    Settings,
    SimPrinter,
    SimTool,
    Simulation,
)
from spoolman_prusalink.accounting import FIRST_EXTRUSION_MM

SHELF = "shelf"
SWAPS = ("unload", "swap_unconfirmed", "swap_assumed", "swap_staged", "swap_confirmed")
# Every model whose build auto-retracts and counts usage: the ones a cancel rams on.
RAMMING = [code for code, m in MODELS.items() if m.extruder.auto_retracts and m.counters]


def preheat(station: Station, spec: PrintSpec) -> None:
    """Start `spec` and tick to its preheat, answering Print on a tools-mapping screen."""
    station.begin_print(spec)
    job = station.printer.job
    if job.waits_for(Reason.TOOLS_MAPPING) and job.attention.detail is None:
        station.printer.tap_through()
    station.tick_until(lambda: job.phase is JobPhase.PREHEAT)


@pytest.mark.parametrize("code", RAMMING, ids=lambda code: MODELS[code].name)
def test_a_cancel_in_the_preheat_counts_no_more_than_the_threshold(code):
    # A tool that has fed since its last auto-retract, as `SimTool.fill` leaves it, rams
    # the whole sequence at the cancel. What any tool counts while the job is still
    # visible stays at or under FIRST_EXTRUSION_MM, so a change to the copied ramming
    # tables or feed ratios that breaks that fails here.
    info = MODELS[code]
    tools = {i: SimTool.loaded_with(i, PLA, extruded=1000.0) for i in range(info.max_tools)}
    sim = Simulation(Settings())
    printer = SimPrinter("p", 0, "k", info, firmware=Firmware.FULL, tools=tools, auto_retract=True)
    sim.add(printer)
    station = Station(sim, printer)
    preheat(station, PrintSpec([Segment(0, 100)], duration_s=20, preheat_s=5))
    seen = {i: int(t.extruded) for i, t in tools.items()}
    printer.cancel()
    rises = {i: 0 for i in tools}
    while printer.job is not None:
        for i, t in tools.items():
            rises[i] = max(rises[i], int(t.extruded) - seen[i])
        sim.tick(0.1)
    if info.feed is not Feed.INDX:  # INDX's preheat has no tool picked to ram.
        assert max(rises.values()) > 0, "the cancel didn't ram"
    assert max(rises.values()) <= FIRST_EXTRUSION_MM


async def test_a_cancel_in_the_preheat_leaves_the_swap_open(tmp_path):
    """BF10: an unconfirmed swap, the tool fed after the reload so the cancel rams, and a
    cancel in the preheat with polls while the job parks. The ramming is charged, but it
    isn't the print's start: the swap stays open, and a Spoolman change after the cancel
    still confirms it (P8)."""
    bridge, fake, station = make(tmp_path, [spool(1), spool_x(3, SHELF)], auto_retract=True)
    station.poll = bridge.poll
    printer = station.printer
    await bridge.poll()  # the baseline
    await station.unload()
    await bridge.poll()
    await station.swap_cycle(source="preset", name="PLA")
    await bridge.poll()
    station.finish_job()  # The write of spool 1's color.
    await bridge.poll()
    printer.extrude(0, 30)
    await station.run_until(lambda: station.tool.process is None)
    await bridge.poll()
    mark = len(ledger(tmp_path))

    preheat(station, PrintSpec([Segment(0, 100)], duration_s=20, preheat_s=5))
    await bridge.poll()
    before = station.counter
    printer.cancel()
    await station.run_until(lambda: printer.job is None, poll_every=0.1)
    await bridge.poll()
    # The auto-retract's 23 mm at 0.95, 21.85, on top of the extrude's fraction.
    assert station.counter - before == 22
    records = ledger(tmp_path)[mark:]
    assert [r["event"] for r in records if r.get("event") in SWAPS] == []
    assert [r["extruded_mm"] for r in records if r.get("spool_id") == 1] == [22]
    assert bridge.printers[0].tools[0].swap is not None
    assert "assumed reloaded" not in (fake.flag(1) or "")

    # Spoolman is fixed after the cancel: that confirms the swap.
    mark = len(ledger(tmp_path))
    fake.spools[1]["location"], fake.spools[3]["location"] = SHELF, LOC
    await bridge.poll()
    events = [r["event"] for r in ledger(tmp_path)[mark:] if r.get("event") in SWAPS]
    assert events == ["swap_confirmed"]
    assert bridge.printers[0].tools[0].active == 3
    await bridge.aclose()
