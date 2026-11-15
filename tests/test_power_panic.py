"""The bridge across a power cut and the power-panic resume on a Core One L.

After the boot the printer reads `IDLE` with no job until the firmware's autostart resumes
the print, at least 1.5 s later (F12; `print_utils_loop` in `src/common/print_utils.cpp`).
A poll in that window sees an idle printer with nothing printing.
"""

import logging

from harness import jobs, ledger, levels, make, poll_during_job, spool, swap_to_spool_20

from simulator.model import JobPhase, PrintSpec, Segment, State


def events(records):
    return [r["event"] for r in records if "event" in r]


def lost(records):
    """The usage the bridge reported lost on an empty tool in a print."""
    return sum(r["mm"] for r in records if r.get("event") == "lost_empty")


def charged(fake, spool_id):
    return sum(fake.used.get(spool_id, []))


async def start(bridge, station):
    """Start a 40 s print and poll until the bridge has seen it print; return its ID."""
    station.poll = lambda: bridge.poll()
    printer = station.printer
    printer.start_print(PrintSpec([Segment(0, 200)], duration_s=40, preheat_s=0))
    await station.run_until(
        lambda: printer.job is not None and printer.job.phase is JobPhase.PRINTING,
        poll_every=0.5,
    )
    station.advance(1)
    await bridge.poll()
    return printer.job.id


async def test_an_unload_in_the_window_then_the_resumed_print_is_lost(tmp_path, caplog):
    """The filament is pulled out while the power is off. In the window the idle sensor
    clear counts an unload, and the resumed print goes on through the empty tool (F29).
    The bridge reports that usage lost, and the next load is charged its own count."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = make(tmp_path, [spool(19)])
    printer = station.printer
    await bridge.poll()  # the baseline
    job = await start(bridge, station)
    before = charged(fake, 19)
    printer.set_power("off")
    printer.unload(0)  # pulled out by hand while off
    printer.set_power("on", resume=True)
    mark = len(ledger(tmp_path))

    await bridge.poll()  # in the window
    assert printer.state is State.IDLE and printer.job is None
    assert events(ledger(tmp_path)[mark:]) == ["reboot", "unload", "swap_unconfirmed"]

    await station.run_until(lambda: printer.state is State.PRINTING, poll_every=0.5)
    station.advance(3)
    await bridge.poll()
    assert printer.job.id == job and station.material() == (None, None, None)
    assert station.counter > 0

    # BF11, a known fault: the resume's 8 mm feed back counts on the empty tool, in
    # `BUSY`, which holds. That filament came from no spool, so once the print counts on,
    # the bridge reports it lost with the print's usage there (C7, BF24) and charges
    # nothing.
    after = events(ledger(tmp_path)[mark:])
    assert after[:3] == ["reboot", "unload", "swap_unconfirmed"]
    assert set(after[3:]) == {"lost_empty"}
    assert charged(fake, 19) == before
    assert levels(caplog, "holding usage until filament is reported") == []

    await station.run_until(lambda: printer.job is None, poll_every=0.5, limit=60)
    printed = station.counter
    assert lost(ledger(tmp_path)[mark:]) == printed
    await station.swap_cycle(source="preset", name="PLA")
    await bridge.poll()
    load = station.counter - printed
    assert load > 0 and charged(fake, 19) == before + load
    await bridge.aclose()


async def test_a_cut_while_a_runout_waits_loses_the_resumed_print(tmp_path, caplog):
    """A runout unloads the tool, and the power fails while the printer waits for the
    reload. The dialog isn't a pause, so the print resumes on its own and goes on through
    the empty tool (F12, F29). The bridge reports that usage lost, and the next load is
    charged its own count."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = make(tmp_path, [spool(19)])
    printer = station.printer
    await bridge.poll()  # the baseline
    job = await start(bridge, station)
    printer.runout(0)
    mark = len(ledger(tmp_path))
    await bridge.poll()  # waiting for the reload
    assert printer.state is State.ATTENTION
    assert events(ledger(tmp_path)[mark:]) == ["unload", "swap_unconfirmed"]
    before = charged(fake, 19)
    mark = len(ledger(tmp_path))
    printer.set_power("off")
    printer.set_power("on", resume=True)

    await bridge.poll()  # in the window
    assert printer.state is State.IDLE and printer.job is None
    await station.run_until(lambda: printer.state is State.PRINTING, poll_every=0.5)
    station.advance(3)
    await bridge.poll()
    assert printer.job.id == job and station.material() == (None, None, None)
    assert station.counter > 0

    # The empty tool's usage came from no spool, so the bridge reports it lost (C7, BF24),
    # with the resume's 8 mm feed back (BF11), held in `BUSY` until the print counted on.
    # The swap the runout opened stays open.
    after = events(ledger(tmp_path)[mark:])
    assert after[0] == "reboot" and set(after[1:]) == {"lost_empty"}
    assert charged(fake, 19) == before
    assert levels(caplog, "holding usage until filament is reported") == []

    await station.run_until(lambda: printer.job is None, poll_every=0.5, limit=60)
    printed = station.counter
    assert lost(ledger(tmp_path)[mark:]) == printed
    await station.swap_cycle(source="preset", name="PLA")
    await bridge.poll()
    load = station.counter - printed
    assert load > 0 and charged(fake, 19) == before + load
    await bridge.aclose()


async def test_a_sync_job_in_the_window_drops_the_resume(tmp_path):
    """A write waits while the print runs. In the window the printer reads idle, so the
    bridge uploads its sync job, and the firmware gives the resume up (BF26)."""
    bridge, fake, station = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    printer = station.printer
    await swap_to_spool_20(bridge, fake, station)
    station.finish_job()
    await bridge.poll()
    assert len(jobs(station)) == 1
    job = await start(bridge, station)
    fake.spools[20]["filament"]["color_hex"] = "00ff00"  # an edit in Spoolman
    station.advance(1)
    await bridge.poll()
    assert len(jobs(station)) == 1  # the write waits for the print

    printer.set_power("off")
    printer.set_power("on", resume=True)
    assert printer.state is State.IDLE and printer.job is None and printer.resuming.id == job

    # A known fault, BF26 in docs/bridge-findings.md: the first poll after the boot runs
    # the sync job, and the user's print never resumes. A fix changes these lines.
    await poll_during_job(bridge)
    assert len(jobs(station)) == 2 and 'O"#00ff00"' in jobs(station)[-1]
    assert printer.job.kind == "sync" and printer.resuming is None
    station.finish_job()
    await bridge.poll()
    station.advance(60)
    await bridge.poll()
    assert printer.state is State.FINISHED and printer.job is None
    assert printer.job_counter == job + 1
    await bridge.aclose()


async def test_a_second_cut_during_the_resume(tmp_path):
    """The power fails again while the resume unparks the head. The print resumes again
    with its ID, and each resume's 8 mm feed back is charged: the first refills the first
    cut's retract (BF11), and the second feeds filament that the second cut never
    retracted, which leaves the nozzle (F12)."""
    bridge, fake, station = make(tmp_path, [spool(19)])
    printer = station.printer
    await bridge.poll()  # the baseline
    job = await start(bridge, station)
    before = charged(fake, 19)
    printer.set_power("off")
    printer.set_power("on", resume=True)
    mark = len(ledger(tmp_path))
    await bridge.poll()  # in the window
    await station.run_until(
        lambda: printer.job is not None and printer.job.phase is JobPhase.UNPARK,
        poll_every=0.5,
    )
    await bridge.poll()
    assert printer.state is State.PAUSED and station.counter == 7  # 8 * 0.95
    assert charged(fake, 19) == before + 7

    printer.set_power("off")
    printer.set_power("on", resume=True)
    await bridge.poll()  # in the second window
    assert printer.state is State.IDLE and printer.job is None and printer.resuming.id == job
    await station.run_until(lambda: printer.state is State.PAUSED, poll_every=0.5)
    await bridge.poll()
    assert printer.job.id == job and station.counter == 7
    assert charged(fake, 19) == before + 7 + 7

    await station.run_until(lambda: printer.job is None, poll_every=0.5, limit=60)
    await bridge.poll()
    assert charged(fake, 19) == before + 7 + station.counter
    assert events(ledger(tmp_path)[mark:]) == ["reboot", "reboot"]
    await bridge.aclose()
