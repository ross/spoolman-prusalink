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
from spoolman_prusalink import tags
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


def weighed(spool_json, remaining=900.0, net=1000.0):
    """A spool with a net weight and `remaining` left in Spoolman, so a runout flags it."""
    spool_json["filament"]["weight"] = net
    spool_json["used_weight"], spool_json["remaining_weight"] = net - remaining, remaining
    return spool_json


async def swap_open(tmp_path):
    """A Core One L with the auto-retract on, spool 1 in its location, spool 3 on the shelf,
    and an unconfirmed swap open after an unload that leaves the tool empty."""
    bridge, fake, station = make(
        tmp_path, [weighed(spool(1)), spool_x(3, SHELF)], auto_retract=True
    )
    station.poll = bridge.poll
    await bridge.poll()  # the baseline
    await station.unload()
    await bridge.poll()
    assert bridge.printers[0].tools[0].swap is not None
    return bridge, fake, station


def swap_events(tmp_path, mark):
    return [r["event"] for r in ledger(tmp_path)[mark:] if r.get("event") in SWAPS]


async def confirm(bridge, fake, station, tmp_path):
    """Run any sync job the last poll started, then move spool 3 into the location and
    spool 1 out, and poll: that confirms the swap."""
    station.finish_job()
    mark = len(ledger(tmp_path))
    fake.spools[1]["location"], fake.spools[3]["location"] = SHELF, LOC
    await bridge.poll()
    assert swap_events(tmp_path, mark) == ["swap_confirmed"]
    assert bridge.printers[0].tools[0].active == 3


async def preview_load(station, name, poll_during):
    """Start a print on the empty tool and answer the preview's "no filament" screen with a
    load of `name`, polling at the screen and, if `poll_during`, every 0.2 s of the load."""
    printer = station.printer
    station.begin_print(PrintSpec([Segment(0, 100, "PLA")], duration_s=20, preheat_s=5))
    assert printer.job.waits_for(Reason.PREVIEW)  # the warning: T0 has no filament
    await station.poll()
    printer.tap_through()
    assert printer.job.phase is JobPhase.WAIT_LOAD and printer.job.previewing
    printer.load(0, source="preset", name=name)
    await station.run_until(
        lambda: station.tool.process is None, poll_every=0.2 if poll_during else None
    )


async def test_a_preview_load_then_a_declined_type_is_no_print_start(tmp_path):
    """BF23: a load at the preview with no poll during it, then a decline at the
    wrong-filament question. The load's count isn't the print's start, so the swap stays
    open and a later Spoolman change confirms it."""
    bridge, fake, station = await swap_open(tmp_path)
    printer = station.printer
    mark = len(ledger(tmp_path))
    await preview_load(station, "PETG", poll_during=False)
    assert printer.job.waits_for(Reason.WRONG_FILAMENT)
    await bridge.poll()
    printer.cancel()
    station.finish_job()
    await bridge.poll()
    assert swap_events(tmp_path, mark) == []
    assert bridge.printers[0].tools[0].job_id is None
    await confirm(bridge, fake, station, tmp_path)
    await bridge.aclose()


@pytest.mark.parametrize("poll_during", [False, True], ids=["no poll", "polled"])
async def test_a_preview_load_then_a_cancel_in_the_preheat_is_no_print_start(tmp_path, poll_during):
    """BF23 and BF10: a load at the preview, then a cancel in the preheat, which rams the
    tool the load left unretracted. Neither the load nor the ramming is the print's start,
    with or without a poll during the load."""
    bridge, fake, station = await swap_open(tmp_path)
    printer = station.printer
    mark = len(ledger(tmp_path))
    await preview_load(station, "PLA", poll_during)
    station.tick_until(lambda: printer.job.phase is JobPhase.PREHEAT)
    await bridge.poll()
    job = printer.job
    printer.cancel()
    # The poll that sees the job gone can start the bridge's write.
    await station.run_until(lambda: printer.job is not job, poll_every=0.1)
    assert swap_events(tmp_path, mark) == []
    assert bridge.printers[0].tools[0].job_id is None
    # The load and the ramming go to spool 1, as the swap's usage.
    swap = bridge.printers[0].tools[0].swap
    assert swap.mm == station.counter - station.tool.extruded_at_unload
    await confirm(bridge, fake, station, tmp_path)
    await bridge.aclose()


async def test_a_runout_in_the_preheat_follows_p2(tmp_path):
    """BF27 on a single-tool printer: a runout in the preheat, on a tool that rams, before
    the print's first extrusion, with a poll that saw the job pending. It's a runout in the
    print, flagged as P2 says. The swap stays open: a Spoolman change during the pause
    confirms it, and the reload goes to the new spool."""
    spools = [weighed(spool(1)), spool_x(3, SHELF)]
    bridge, fake, station = make(tmp_path, spools, auto_retract=True)
    station.poll = bridge.poll
    printer = station.printer
    await bridge.poll()  # the baseline
    mark = len(ledger(tmp_path))
    preheat(station, PrintSpec([Segment(0, 100)], duration_s=20, preheat_s=5))
    await bridge.poll()
    printer.runout(0)
    station.tick_until(lambda: station.tool.process is None)
    assert printer.job.waits_for(Reason.RUNOUT) and station.tool.unload_count == 1
    await bridge.poll()
    assert swap_events(tmp_path, mark) == ["unload", "swap_unconfirmed"]
    assert "unloaded mid-print with 900 g left" in fake.flag(1)
    assert bridge.printers[0].tools[0].job_id == printer.job.id

    mark = len(ledger(tmp_path))
    fake.spools[1]["location"], fake.spools[3]["location"] = SHELF, LOC
    await bridge.poll()
    before = station.counter
    printer.load(0)
    await station.run_until(lambda: station.tool.process is None, poll_every=1.0)
    printer.resume()
    await station.run_until(lambda: printer.job is None, poll_every=1.0, limit=60)
    await bridge.poll()
    assert swap_events(tmp_path, mark) == ["swap_confirmed"]
    used = {i: sum(mm) for i, mm in fake.used.items()}
    assert used[3] >= station.counter - before
    await bridge.aclose()


def spy_on_tag_unloads(monkeypatch) -> list[bool]:
    """Record the `runout` of each `tags.unloaded` call the bridge makes."""
    calls: list[bool] = []
    unloaded = tags.unloaded

    def spy(seen, runout):
        calls.append(runout)
        return unloaded(seen, runout)

    monkeypatch.setattr(tags, "unloaded", spy)
    return calls


async def test_a_runout_before_any_poll_saw_the_job_is_a_swap_before_the_print(
    tmp_path, monkeypatch
):
    """A runout in the preheat that no poll saw pending: the first poll of the job sees the
    unload too, with the tool empty. The bridge can't tell that from an idle unload and a
    print started on the emptied tool, the common case, so it's a swap before the print:
    no runout flag and no runout tag handling. The job waits pending on the empty tool, the
    reload's count isn't its start, and the print starts at its first extrusion past the
    threshold. With no Spoolman change, that start closes the open swap as assumed, with
    a provisional charge, as for any swap before a print (S4)."""
    calls = spy_on_tag_unloads(monkeypatch)
    spools = [weighed(spool(1)), spool_x(3, SHELF)]
    bridge, fake, station = make(tmp_path, spools, auto_retract=True)
    station.poll = bridge.poll
    printer = station.printer
    await bridge.poll()  # the baseline
    mark = len(ledger(tmp_path))
    preheat(station, PrintSpec([Segment(0, 100)], duration_s=20, preheat_s=5))
    printer.runout(0)
    station.tick_until(lambda: station.tool.process is None)
    await bridge.poll()
    assert swap_events(tmp_path, mark) == ["unload", "swap_unconfirmed"]
    assert calls == [False]
    assert "mid-print" not in fake.flag(1) and bridge.state.runouts == {}
    tool = bridge.printers[0].tools[0]
    assert (tool.job_id, tool.pending_job, tool.pending_empty) == (None, printer.job.id, True)

    printer.load(0)
    await station.run_until(lambda: station.tool.process is None, poll_every=1.0)
    await bridge.poll()
    tool = bridge.printers[0].tools[0]
    assert tool.job_id is None and tool.pending_job == printer.job.id
    assert swap_events(tmp_path, mark) == ["unload", "swap_unconfirmed"]

    printer.resume()
    await station.run_until(lambda: printer.job is None, poll_every=1.0, limit=60)
    await bridge.poll()
    assert swap_events(tmp_path, mark) == ["unload", "swap_unconfirmed", "swap_assumed"]
    tool = bridge.printers[0].tools[0]
    assert tool.swap is None and tool.provisional is not None
    assert bridge.state.runouts == {}
    await bridge.aclose()


async def test_an_idle_unload_then_a_print_on_the_empty_tool_is_a_swap_before_it(
    tmp_path, monkeypatch
):
    """The common case a runout no poll saw pending looks like: unload at idle, start a print on the
    emptied tool before the next poll, and load the new filament at the preview. No runout
    flag and no runout tag handling. The load isn't the print's start; the print's first
    extrusion is, and with no Spoolman change it closes the swap as assumed (S4)."""
    calls = spy_on_tag_unloads(monkeypatch)
    spools = [weighed(spool(1)), spool_x(3, SHELF)]
    bridge, fake, station = make(tmp_path, spools, auto_retract=True)
    station.poll = bridge.poll
    printer = station.printer
    await bridge.poll()  # the baseline
    mark = len(ledger(tmp_path))
    await station.unload()
    station.begin_print(PrintSpec([Segment(0, 100, "PLA")], duration_s=20, preheat_s=5))
    assert printer.job.waits_for(Reason.PREVIEW)  # the warning: T0 has no filament
    await bridge.poll()
    assert swap_events(tmp_path, mark) == ["unload", "swap_unconfirmed"]
    assert calls == [False]
    tool = bridge.printers[0].tools[0]
    assert (tool.job_id, tool.pending_job, tool.pending_empty) == (None, printer.job.id, True)

    printer.tap_through()
    printer.load(0, source="preset", name="PLA")
    station.tick_until(lambda: printer.job.phase is JobPhase.PREHEAT)
    await bridge.poll()
    assert bridge.printers[0].tools[0].job_id is None
    job = printer.job
    await station.run_until(lambda: printer.job is not job, poll_every=1.0, limit=60)
    assert swap_events(tmp_path, mark) == ["unload", "swap_unconfirmed", "swap_assumed"]
    assert "mid-print" not in (fake.flag(1) or "") and bridge.state.runouts == {}
    await bridge.aclose()
