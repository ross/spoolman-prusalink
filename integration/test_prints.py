"""Prints and what happens during them: docs/scenarios.md rows P1 to P12."""

from __future__ import annotations

import logging

import pytest

from integration.conftest import LOAD_MM, PETG, World, charged, charges, events, logged, print_spec
from simulator.model import PrintSpec, Segment, State


async def test_p2_runout_with_spoolman_updated_during_pause(world: World):
    """Runout mid-print, Spoolman fixed during the pause: the rest goes to the new spool."""
    w = world
    a, b = w.spools["A"], w.spools["B"]
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    start = w.tool.extruded

    # Scan, blip and preheat take 0.9 s, so this is a few tenths of a second of printing.
    # The bridge sees a print start when the counter moves between two polls of the same
    # job, and only a runout after that start counts as mid-print, so poll twice.
    w.printer.start_print(PrintSpec([Segment(0, 400)], duration_s=4, preheat_s=0.5))
    w.advance(1.2)
    assert w.printer.state is State.PRINTING and w.tool.extruded > start
    records = await w.poll()
    w.advance(0.3)
    records += await w.poll()
    assert w.charged(records, "A") == w.tool.extruded - start

    # The runout unloads mid-print with nearly all of A left in Spoolman.
    w.printer.runout(0)
    records = await w.poll()
    assert len(events(records, "unload")) == 1
    assert len(events(records, "swap_unconfirmed")) == 1
    flag = await w.flag("A")
    assert flag is not None and "mid-print with" in flag
    at_runout = w.tool.extruded_at_unload

    # Spoolman is updated during the pause, before the reload.
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_confirmed")) == 1

    # The reload keeps the old type, as the firmware does: the printer shows A again.
    mark = w.mark()
    w.printer.load(0)
    await w.run_until(lambda: w.printer.state is State.PAUSED, poll_every=1.0)
    assert w.material() == ("PLA", "#73737b", w.uuids["A"])
    w.printer.resume()
    await w.run_until(
        lambda: w.printer.state is State.FINISHED and w.printer.job is None, poll_every=1.0
    )
    records = w.since(mark) + await w.poll()
    # Everything after the runout went to B; A got nothing more.
    assert w.charged(records, "A") == 0
    assert w.charged(records, "B") == w.tool.extruded - at_runout
    assert {r["spool_id"] for r in charges(w.since(0))} == {a, b}

    # Idle with the printer still showing A: the queued write of B runs now.
    assert w.requests("PUT") != []
    w.advance(1.5)
    assert w.printer.job is None
    minted = await w.spoolman.extra(b, "instance_uuid")
    assert minted is not None
    assert w.material() == (PETG[0], PETG[1], minted)
    puts = len(w.requests("PUT"))
    records = await w.poll()
    assert len(w.requests("PUT")) == puts
    assert events(records) == []


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


async def baseline(w: World) -> None:
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    assert charges(records) == []


async def stage_b(w: World) -> None:
    """Spoolman says B is in the tool with no unload: B is staged, A still charged."""
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_staged")) == 1


async def finish(w: World, state: State = State.FINISHED) -> list[dict]:
    """Run the print to its end, polling every second, and return what the polls wrote."""
    return await w.run_until(
        lambda: w.printer.state is state and w.printer.job is None, poll_every=1.0
    )


async def test_p8_cancelled_during_preheat_is_not_a_print_start(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Print cancelled in preheat: not a print start, so the open swap is still confirmed."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    await w.swap_cycle(source="tag", name=PETG[0], color=PETG[1])
    records = await w.poll()
    assert w.charged(records, "A") == LOAD_MM
    # A is still active and the printer shows B's tag, so that poll wrote A back.
    await w.run_until(lambda: w.printer.job is None)

    # Scan and blip take 0.4 s, so at 0.5 s the printer is preheating with a job.
    start = w.tool.extruded
    w.printer.start_print(print_spec())
    w.advance(0.5)
    assert w.printer.state is State.PRINTING and w.printer.job is not None
    records = await w.poll()
    assert events(records) == []
    w.advance(0.2)
    records = await w.poll()
    assert events(records) == []
    assert w.tool.extruded == start

    # Spoolman is fixed during the preheat: the swap is still open, so this confirms it.
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert [e["message"] for e in events(records, "swap_confirmed")] == [
        f"swap confirmed: {LOAD_MM} mm moved from spool {a} to {b}"
    ]
    assert w.charged(records, "B") == LOAD_MM
    assert w.charged(records, "A") == -LOAD_MM

    w.printer.cancel()
    records = await finish(w, State.STOPPED)
    assert w.tool.extruded == start
    assert charges(records) == []
    assert events(w.since(0), "swap_assumed") == []
    assert not any("provisionally" in m for m in warnings(caplog))
    assert await w.flag("A") is None


async def test_p9_restart_after_a_failure_is_a_new_print_start(
    world: World, caplog: pytest.LogCaptureFixture
):
    """A print cancelled and started again under a new job ID is a second print start."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    await stage_b(w)
    msg = f"print started while spool {b} is staged but not loaded; charging spool {a}"

    start = w.counter
    mark = w.mark()
    records = await w.start_print(print_spec())
    assert [e["message"] for e in events(records, "staged_at_print")] == [msg]
    first = w.printer.job.id

    # The print fails part way and is cancelled.
    w.advance(0.5)
    w.printer.cancel()
    await finish(w, State.STOPPED)
    w.printer.dismiss()

    records = await w.start_print(print_spec())
    assert w.printer.job.id != first
    assert [e["message"] for e in events(records, "staged_at_print")] == [msg]
    await finish(w)
    records = w.since(mark) + await w.poll()

    assert len(events(w.since(0), "staged_at_print")) == 2
    starts = [r for r in caplog.records if r.levelno == logging.WARNING and msg in r.getMessage()]
    assert len(starts) == 2
    assert w.charged(records, "A") == w.counter - start
    assert w.charged(records, "B") == 0


async def test_p11_pause_and_resume_changes_nothing(world: World, caplog: pytest.LogCaptureFixture):
    """Pause and resume with no filament change: one print start, and the counter carries on."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    await baseline(w)
    # A print start on its own writes nothing, so stage B first: then a start writes
    # staged_at_print, and a resume counted as a second start would write it again.
    await stage_b(w)
    start = w.counter

    mark = w.mark()
    await w.start_print(print_spec())
    w.printer.pause()
    await w.poll()
    paused = w.mark()
    w.advance(2.0)
    assert w.printer.state is State.PAUSED
    assert await w.poll() == []
    assert w.since(paused) == []

    w.printer.resume()
    await finish(w)
    records = w.since(mark) + await w.poll()
    assert [e["event"] for e in events(records)] == ["staged_at_print"]
    assert w.charged(records, "A") == w.counter - start
    assert {r["spool_id"] for r in charges(records)} == {w.spools["A"]}
    # Counted by text: a bridge on a shared Spoolman also warns about other suites' claims.
    assert len([m for m in warnings(caplog) if "print started" in m]) == 1


async def test_p10_spoolman_changed_mid_print_is_staged(world: World):
    """Spoolman changed mid-print with no unload: staged, the old spool charged until unload."""
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    start = w.counter

    mark = w.mark()
    await w.start_print(print_spec())
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_staged")) == 1

    await finish(w)
    records = w.since(mark) + await w.poll()
    assert w.charged(records, "A") == w.counter - start
    assert w.charged(records, "B") == 0
    w.printer.dismiss()

    w.printer.extrude(0, 10)
    await w.unload()
    records = await w.poll()
    assert [e["message"] for e in events(records, "swap_committed")] == [
        f"swap done: spool {a} -> {b}"
    ]
    assert w.charged(records, "A") == 10

    records = await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    records += await w.poll()
    assert w.charged(records, "B") == LOAD_MM
    assert w.charged(records, "A") == 0


async def test_p12_power_panic_resume_keeps_the_job(world: World, caplog: pytest.LogCaptureFixture):
    """Power loss mid-print and resume: a reboot with the same job, so no new print start."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    await stage_b(w)

    mark = w.mark()
    records = await w.start_print(print_spec())
    assert len(events(records, "staged_at_print")) == 1
    job = w.printer.job.id
    before_cut = charged(w.since(mark), a)

    # Some printing the bridge never sees, then the power goes.
    w.advance(0.2)
    w.printer.set_power("off")
    assert await w.poll() == []
    assert logged(caplog, logging.WARNING, "unreachable")
    w.advance(3.0)

    w.printer.set_power("on", resume=True)
    # Recovery: BUSY with the nozzle clean's 25 mm purge, PAUSED while unparking, PRINTING.
    records = await w.run_until(lambda: w.printer.state is not State.BUSY, poll_every=0.5)
    assert w.printer.state is State.PAUSED and w.counter == 25
    records += await w.run_until(lambda: w.printer.state is State.PRINTING, poll_every=0.5)
    assert w.printer.job.id == job
    reboots = events(records, "reboot")
    assert len(reboots) == 1 and "counters restart at 0" in reboots[0]["message"]
    assert logged(caplog, logging.INFO, "reachable again")
    assert events(records, "staged_at_print") == []
    # The reboot flags the staged spool, as row S9 says.
    assert len(events(records, "staged_at_reboot")) == 1
    assert f"spool {b} is staged" in events(records, "staged_at_reboot")[0]["message"]

    # After the reboot A is charged the counter from 0, the nozzle clean's purge included.
    await finish(w)
    records = w.since(mark) + await w.poll()
    assert len(events(records, "staged_at_print")) == 1
    assert w.charged(records, "B") == 0
    assert w.charged(records, "A") == before_cut + w.counter


async def to_mid_print(w: World) -> tuple[int, list[dict]]:
    """Baseline, then start a print and poll until it's counted: the counter at baseline."""
    await baseline(w)
    start = w.counter
    records = await w.start_print(print_spec())
    assert events(records) == []
    return start, records


async def reload_and_resume(w: World) -> list[dict]:
    """Reload the type the unload left, as a runout's reload does, then resume.

    Polls every second, as during a real pause, and returns what the polls wrote.
    """
    w.printer.load(0)
    records = await w.run_until(lambda: w.printer.state is State.PAUSED, poll_every=1.0)
    assert w.material() == ("PLA", "#73737b", w.uuids["A"])
    w.printer.resume()
    return records


async def test_p1_normal_print(world: World):
    """Normal print: each poll charges the active spool the counter's change since the last."""
    w = world
    a = w.spools["A"]
    start, records = await to_mid_print(w)
    assert w.charged(records, "A") == w.counter - start

    mark = w.mark()
    last = w.counter
    while w.printer.state is not State.FINISHED:
        w.advance(1.0)
        records = await w.poll()
        assert w.charged(records, "A") == w.counter - last
        assert events(records) == []
        last = w.counter
    assert w.printer.job is None

    assert w.charged(w.since(mark), "A") > 0
    assert {r["spool_id"] for r in charges(w.since(0))} == {a}
    assert w.charged(w.since(0), "A") == w.counter - start
    assert await w.used("A") > 0
    assert await w.flag("A") is None
    assert w.requests("PUT") == []


async def test_p3_runout_with_spoolman_updated_after_the_print(world: World):
    """Runout, Spoolman fixed after the print: the swap stays open, then moves the rest."""
    w = world
    a, b = w.spools["A"], w.spools["B"]
    start, _ = await to_mid_print(w)

    w.printer.runout(0)
    records = await w.poll()
    assert len(events(records, "unload")) == 1
    assert len(events(records, "swap_unconfirmed")) == 1
    flag = await w.flag("A")
    assert flag is not None and "mid-print with" in flag
    at_runout = w.tool.extruded_at_unload

    # The reload and the rest of the print, with Spoolman left alone: the same job carries
    # on, so it isn't a print start and the swap stays open.
    mark = w.mark()
    await reload_and_resume(w)
    await finish(w)
    records = w.since(mark) + await w.poll()
    assert events(records) == []
    assert events(w.since(0), "swap_assumed") == []
    assert w.charged(w.since(0), "A") == w.counter - start
    assert w.charged(w.since(0), "B") == 0

    # Spoolman is updated after the print: the usage since the runout moves to B.
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    since_runout = w.counter - at_runout
    assert [e["message"] for e in events(records, "swap_confirmed")] == [
        f"swap confirmed: {since_runout} mm moved from spool {a} to {b}"
    ]
    assert w.charged(records, "B") == since_runout
    assert w.charged(records, "A") == -since_runout
    assert w.charged(w.since(0), "A") == at_runout - start

    # Idle with the printer still showing A: the bridge writes B.
    w.advance(1.5)
    assert w.printer.job is None
    minted = await w.spoolman.extra(b, "instance_uuid")
    assert minted is not None
    assert w.material() == (PETG[0], PETG[1], minted)


async def test_p4_runout_reloaded_with_no_spoolman_change(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Runout reloaded, Spoolman never changed: assumed at the next print, kept at the unload."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    start, _ = await to_mid_print(w)

    w.printer.runout(0)
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    at_runout = w.tool.extruded_at_unload
    await reload_and_resume(w)
    await finish(w)
    await w.poll()
    w.printer.dismiss()
    assert events(w.since(0), "swap_assumed") == []

    # The next print's first extrusion closes the swap as the same spool, provisionally.
    records = await w.start_print(print_spec())
    msg = (
        "print started with no Spoolman change since the unload; assuming "
        f"spool {a} was reloaded and charging it provisionally"
    )
    assert [e["message"] for e in events(records, "swap_assumed")] == [msg]
    assert logged(caplog, logging.WARNING, f"[{w.tool_location}] {msg}")
    provisional = w.counter - at_runout
    flag = await w.flag("A")
    assert flag is not None
    assert f"{provisional} mm" in flag and "charged to it provisionally" in flag

    # The next unload keeps the usage on A and clears the provisional flag.
    await finish(w)
    w.printer.dismiss()
    w.printer.extrude(0, 10)
    await w.unload()
    records = await w.poll()
    kept = events(records, "provisional_kept")
    assert len(kept) == 1 and f"provisionally to spool {a}" in kept[0]["message"]
    assert len(events(records, "swap_unconfirmed")) == 1
    flag = await w.flag("A") or ""
    assert "provisionally" not in flag
    assert {r["spool_id"] for r in charges(w.since(0))} == {a}
    assert w.charged(w.since(0), "A") == w.tool.extruded_at_unload - start


async def test_p5_planned_color_changes_are_unloads(world: World):
    """Two `M600` color changes: each an unload, one confirmed in Spoolman, one left open."""
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    start = w.counter
    spec = PrintSpec([Segment(0, 400)], duration_s=4, preheat_s=0.5, color_changes_mm=[150, 300])

    mark = w.mark()
    records = await w.start_print(spec)
    assert events(records) == []
    await w.run_until(lambda: w.printer.state is State.ATTENTION, poll_every=1.0)
    records = await w.poll()
    assert len(events(w.since(mark), "unload")) == 1
    assert len(events(w.since(mark), "swap_unconfirmed")) == 1
    first = w.tool.extruded_at_unload
    assert w.charged(w.since(0), "A") == first - start

    # The first change is a new spool, and Spoolman is updated during the pause.
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_confirmed")) == 1

    # The second change isn't recorded in Spoolman, and stays open past the print.
    mark = w.mark()
    await reload_and_resume(w)
    await w.run_until(lambda: w.printer.state is State.ATTENTION, poll_every=1.0)
    await w.poll()
    assert len(events(w.since(mark), "unload")) == 1
    assert len(events(w.since(mark), "swap_unconfirmed")) == 1
    flag = await w.flag("B")
    assert flag is not None and "mid-print with" in flag
    await reload_and_resume(w)
    await finish(w)
    await w.poll()

    records = w.since(0)
    assert len(events(records, "unload")) == 2
    assert len(events(records, "swap_unconfirmed")) == 2
    assert len(events(records, "swap_confirmed")) == 1
    # The same job carrying on is never a print start.
    assert events(records, "swap_assumed") == []
    assert events(records, "staged_at_print") == []
    assert w.charged(records, "A") == first - start
    assert w.charged(records, "B") == w.counter - first
    assert {r["spool_id"] for r in charges(records)} == {a, b}


async def test_p6_jam_is_an_unconfirmed_swap_and_its_purge_is_charged(world: World):
    """Jam with an unload: an unconfirmed swap, and the reload's longer purge is charged."""
    w = world
    a = w.spools["A"]
    start, _ = await to_mid_print(w)

    w.printer.jam(0)
    assert w.printer.state is State.ATTENTION
    records = await w.poll()
    assert len(events(records, "unload")) == 1
    assert len(events(records, "swap_unconfirmed")) == 1
    at_jam = w.tool.extruded_at_unload

    # The reload after a jam purges 60 mm more than a load.
    mark = w.mark()
    await reload_and_resume(w)
    assert w.counter - at_jam == LOAD_MM + 60
    records = w.since(mark) + await w.poll()
    assert w.charged(records, "A") == LOAD_MM + 60

    await finish(w)
    records = w.since(0) + await w.poll()
    assert events(records, "swap_assumed") == []
    assert {r["spool_id"] for r in charges(records)} == {a}
    assert w.charged(records, "A") == w.counter - start


async def test_p7_cancel_partway_charges_what_was_extruded(world: World):
    """Print cancelled partway: what was extruded is charged, and nothing else happens."""
    w = world
    a = w.spools["A"]
    start, _ = await to_mid_print(w)

    w.advance(1.0)
    w.printer.cancel()
    await finish(w, State.STOPPED)
    stopped = w.counter
    assert start < stopped < start + 400
    records = w.since(0) + await w.poll()
    assert [e["event"] for e in events(records)] == ["baseline"]
    assert {r["spool_id"] for r in charges(records)} == {a}
    assert w.charged(records, "A") == stopped - start
    assert await w.flag("A") is None
    assert w.requests("PUT") == []

    # Dismissed and idle, nothing more is charged.
    w.printer.dismiss()
    w.advance(2.0)
    assert charges(await w.poll()) == []
