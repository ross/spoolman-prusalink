"""The bridge across a power cut and the power-panic resume on a Core One L.

After the boot the printer reads `IDLE` with no job until the firmware's autostart resumes
the print, at least 1.5 s later (F12; `print_utils_loop` in `src/common/print_utils.cpp`).
A poll in that window sees an idle printer with nothing printing.
"""

import logging

from harness import jobs, ledger, levels, make, spool, swap_to_spool_20

from simulator.model import JobPhase, PrintSpec, Segment, State
from spoolman_prusalink.config import SYNC_DELAY


def events(records):
    return [r["event"] for r in records if "event" in r]


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


async def test_an_unload_in_the_window_then_printing_on_an_empty_tool(tmp_path, caplog):
    """The filament is pulled out while the power is off. In the window the idle sensor
    clear counts an unload, and the resumed print goes on through the empty tool (F29)."""
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

    # Two known faults, pinned as the bridge behaves today; a fix changes these lines.
    # BF11: the resume's 8 mm feed back counts on the empty tool. BF24: the bridge holds
    # the empty tool's usage (C7), then charges it to the next spool reported in the
    # tool, with the load's own count, though it never left a spool.
    assert events(ledger(tmp_path)[mark:]) == ["reboot", "unload", "swap_unconfirmed"]
    assert charged(fake, 19) == before
    assert levels(caplog, "holding usage until filament is reported") == ["WARNING"]

    await station.run_until(lambda: printer.job is None, poll_every=0.5, limit=60)
    held = station.counter
    await station.swap_cycle(source="preset", name="PLA")
    await bridge.poll()
    load = station.counter - held
    assert load > 0 and charged(fake, 19) == before + held + load
    await bridge.aclose()


async def test_a_cut_while_a_runout_waits_for_the_reload(tmp_path, caplog):
    """A runout unloads the tool, and the power fails while the printer waits for the
    reload. The dialog isn't a pause, so the print resumes on its own and goes on through
    the empty tool (F12, F29)."""
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

    # A known fault, pinned as the bridge behaves today; a fix changes these lines. BF24:
    # the bridge holds the empty tool's usage, the resume's 8 mm feed back included (BF11),
    # then charges it to the spool reported in the tool at the next load, with the load's
    # own count, though it never left a spool. The swap the runout opened stays open.
    assert events(ledger(tmp_path)[mark:]) == ["reboot"]
    assert charged(fake, 19) == before
    assert levels(caplog, "holding usage until filament is reported") == ["WARNING"]

    await station.run_until(lambda: printer.job is None, poll_every=0.5, limit=60)
    held = station.counter
    await station.swap_cycle(source="preset", name="PLA")
    await bridge.poll()
    load = station.counter - held
    assert load > 0 and charged(fake, 19) == before + held + load
    await bridge.aclose()


async def written_and_printing(tmp_path):
    """A bridge with the default `sync_delay`, on the simulator's time plus `skip[0]`
    seconds, whose write of an edit waits for a running print.

    Returns the bridge, the station, the print's job ID and `skip`, which moves the
    bridge's clock on without moving the printer.
    """
    bridge, fake, station = make(
        tmp_path, [spool(19)], uuid_field="instance_uuid", sync_delay=SYNC_DELAY
    )
    skip = [1e6]
    bridge.clock = lambda: station.sim.now + skip[0]
    await swap_to_spool_20(bridge, fake, station)
    assert jobs(station) == []  # the write waits for the delay
    station.advance(SYNC_DELAY)
    await bridge.poll()
    assert len(jobs(station)) == 1
    station.finish_job()
    await bridge.poll()
    job = await start(bridge, station)
    fake.spools[20]["filament"]["color_hex"] = "00ff00"  # an edit in Spoolman
    station.advance(1)
    await bridge.poll()
    assert len(jobs(station)) == 1  # the write waits for the print
    return bridge, station, job, skip


async def write_after_the_print(bridge, station, job):
    """Run the resumed print to its end, then check that the write waits out the delay
    and runs."""
    printer = station.printer
    await station.run_until(lambda: printer.state is State.PRINTING, poll_every=0.5)
    assert printer.job.id == job and len(jobs(station)) == 1
    await station.run_until(lambda: printer.job is None, poll_every=0.5, limit=60)
    await bridge.poll()
    assert printer.state is State.FINISHED and printer.job_counter == job
    assert len(jobs(station)) == 1
    station.advance(SYNC_DELAY)
    await bridge.poll()
    assert len(jobs(station)) == 2 and 'O"#00ff00"' in jobs(station)[-1]
    station.finish_job()


async def test_a_write_waits_out_the_window_and_the_print_resumes(tmp_path):
    """A write waits while the print runs. The printer reads idle in the window, but the
    idle clock starts over at the boot, so the sync job waits out `sync_delay`: the print
    resumes with its ID, and the write runs once the print has ended (P12, W2)."""
    bridge, station, job, _ = await written_and_printing(tmp_path)
    printer = station.printer
    printer.set_power("off")
    printer.set_power("on", resume=True)
    assert printer.state is State.IDLE and printer.job is None and printer.resuming.id == job

    await bridge.poll()  # in the window
    assert len(jobs(station)) == 1 and printer.resuming.id == job
    await write_after_the_print(bridge, station, job)
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


async def test_a_second_cut_after_an_idle_poll_in_the_first_window(tmp_path):
    """A poll in the first window starts the idle clock and holds the write. With the
    printer idle, the next poll comes `poll_idle` later. In between, the resume starts and
    a second cut reboots the printer, and that poll lands in the second window. No read
    saw the printer busy, but the boot starts the idle clock again, so the write still
    waits and the print resumes (P12, W2)."""
    bridge, station, job, skip = await written_and_printing(tmp_path)
    printer = station.printer
    printer.set_power("off")
    printer.set_power("on", resume=True)
    await bridge.poll()  # in the first window
    assert printer.resuming.id == job and len(jobs(station)) == 1

    station.tick_until(lambda: printer.job is not None)  # the resume runs, with no poll
    assert printer.job.id == job and printer.state is State.BUSY
    printer.set_power("off")
    printer.set_power("on", resume=True)
    skip[0] += bridge.config.poll_idle
    assert bridge.config.poll_idle > SYNC_DELAY
    await bridge.poll()  # in the second window
    assert len(jobs(station)) == 1
    assert printer.state is State.IDLE and printer.resuming.id == job
    await write_after_the_print(bridge, station, job)
    await bridge.aclose()
