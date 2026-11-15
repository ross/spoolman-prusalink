"""Usage on a tool with no filament (C7): reported lost while a user's print runs, held for
the next spool otherwise. Which printer states count as printing is decided in
`PrinterBridge.process`, so these tests run the whole bridge."""

import logging

from harness import end_job, fill, ledger, levels, make, menu, restart, spool, start_job

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
    assert levels(caplog, HINT) == []
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


HINT = "printing with no filament reported; that usage is charged to no spool"


async def empty_print(tmp_path):
    """A bridge whose printer is in a user's print with no filament in the tool."""
    bridge, fake, station = make(tmp_path, [spool(19)])
    await bridge.poll()  # the baseline
    start_job(station)
    fill(station.tool, None)
    return bridge, fake, station


async def test_a_print_through_an_empty_tool_warns_once_until_the_print_ends(tmp_path, caplog):
    """BF25: lost usage raises the hint about the filament sensors. It's logged once, stands
    through polls with no new usage, and ends once at the first poll with no print."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    bridge, fake, station = await empty_print(tmp_path)
    for _ in range(3):
        station.tool.extruded += 30
        await bridge.poll()
        await bridge.poll()  # no new usage
    assert levels(caplog, HINT) == ["WARNING"]
    assert events(ledger(tmp_path)) == ["baseline"] + ["lost_empty"] * 3

    end_job(station)
    await bridge.poll()
    await bridge.poll()
    assert levels(caplog, HINT) == ["WARNING", "WARNING"]
    assert levels(caplog, f"no longer: {HINT}") == ["WARNING"]
    assert bridge.printers[0].conditions == {}
    await bridge.aclose()


async def test_the_hint_ends_when_the_filament_comes_back(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = await empty_print(tmp_path)
    station.tool.extruded += 30
    await bridge.poll()
    fill(station.tool, menu("PLA"))
    await bridge.poll()
    assert levels(caplog, f"no longer: {HINT}") == ["WARNING"]
    await bridge.aclose()


async def test_the_hint_stands_across_a_restart_mid_print(tmp_path, caplog):
    """The saved condition stands at the restart's first poll, which counts no new usage.
    The restarted bridge logs it once, as it does every standing condition, and its end
    once."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = await empty_print(tmp_path)
    station.tool.extruded += 30
    await bridge.poll()
    bridge = restart(bridge)
    caplog.clear()
    await bridge.poll()  # no new usage
    assert levels(caplog, HINT) == ["WARNING"]
    for _ in range(3):
        station.tool.extruded += 30
        await bridge.poll()
    end_job(station)
    await bridge.poll()
    assert levels(caplog, HINT) == ["WARNING", "WARNING"]
    assert levels(caplog, f"no longer: {HINT}") == ["WARNING"]
    await bridge.aclose()


async def test_an_empty_tool_with_no_lost_usage_raises_no_hint(tmp_path, caplog):
    """A print on an empty tool whose counter doesn't move, or usage held outside a print,
    loses nothing, so the bridge has no sign that the sensors are off."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = await empty_print(tmp_path)
    await bridge.poll()
    end_job(station)
    await bridge.poll()
    station.tool.extruded += 30  # held for the next spool
    await bridge.poll()
    assert levels(caplog, HINT) == []
    assert events(ledger(tmp_path)) == ["baseline"]
    await bridge.aclose()


async def test_a_print_whose_usage_is_first_seen_after_it_warns_for_one_poll(tmp_path, caplog):
    """The print's tail is the only usage lost: the hint is raised at that poll and ends at
    the next, with no print to keep it."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = await empty_print(tmp_path)
    await bridge.poll()
    end_job(station)
    station.tool.extruded += 30
    await bridge.poll()
    assert events(ledger(tmp_path)) == ["baseline", "lost_empty"]
    await bridge.poll()
    assert levels(caplog, HINT) == ["WARNING", "WARNING"]
    assert levels(caplog, f"no longer: {HINT}") == ["WARNING"]
    await bridge.aclose()
