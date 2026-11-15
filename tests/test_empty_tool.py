"""Usage on a tool with no filament (C7): reported lost while a user's print runs, held for
the next spool otherwise. Which printer states count as printing is decided in
`PrinterBridge.process`, so these tests run the whole bridge."""

import logging

from harness import end_job, fill, ledger, levels, make, spool, start_job

from simulator.model import JobPhase, PrintSpec, Segment, State

HOLD = "holding usage until filament is reported"


def events(records):
    return [r["event"] for r in records if "event" in r]


def charged(fake, spool_id):
    return sum(fake.used.get(spool_id, []))


async def test_a_runout_s_reload_in_attention_is_charged_to_the_spool(tmp_path, caplog):
    """P2: the reload reads `ATTENTION` with the job, and counts its feed before the type
    comes back (F3, F36). That feed is held, not lost, and goes to the spool with the
    rest of the reload."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = make(tmp_path, [spool(19)])
    station.poll = lambda: bridge.poll()
    printer = station.printer
    await bridge.poll()  # the baseline
    printer.start_print(PrintSpec([Segment(0, 200)], duration_s=40, preheat_s=0))
    await station.run_until(
        lambda: printer.job is not None and printer.job.phase is JobPhase.PRINTING,
        poll_every=0.5,
    )
    station.advance(1)
    await bridge.poll()
    printer.runout(0)
    await bridge.poll()
    assert printer.state is State.ATTENTION and station.material() == (None, None, None)
    before, at_unload = charged(fake, 19), station.tool.extruded_at_unload

    mark = len(ledger(tmp_path))
    printer.load(0)
    await station.run_until(lambda: printer.state is State.PAUSED, poll_every=0.1)
    printer.resume()
    await station.run_until(lambda: printer.job is None, poll_every=1.0, limit=60)
    await bridge.poll()
    # A poll saw the feed with no type: the hold starts, then ends with the type.
    assert levels(caplog, HOLD) == ["WARNING", "WARNING"]
    assert "lost_empty" not in events(ledger(tmp_path)[mark:])
    assert charged(fake, 19) - before == station.counter - at_unload
    await bridge.aclose()


async def test_a_sync_job_on_an_empty_tool_holds_its_usage(tmp_path, caplog):
    """`read()` hides the sync job's ID, so the bridge's own job isn't a user's print: usage
    with no filament during it is held. A user's print then reports it lost."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = make(tmp_path, [spool(19)])
    await bridge.poll()  # the baseline
    before = charged(fake, 19)
    start_job(station, "spoolman-sync.gcode")
    fill(station.tool, None)
    station.tool.extruded += 50
    await bridge.poll()
    assert station.printer.state is State.PRINTING
    assert levels(caplog, HOLD) == ["WARNING"]
    assert events(ledger(tmp_path)) == ["baseline"]
    assert bridge.printers[0].tools[0].charged == 1000

    end_job(station)
    start_job(station)
    station.tool.extruded += 30
    await bridge.poll()
    assert events(ledger(tmp_path)) == ["baseline", "lost_empty"]
    assert ledger(tmp_path)[-1]["mm"] == 80  # the sync job's 50, held, goes with it
    assert charged(fake, 19) == before
    await bridge.aclose()
