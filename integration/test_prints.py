"""Filament changes during a print: docs/scenarios.md row P2."""

from __future__ import annotations

from integration.conftest import PETG, World, charges, events
from simulator.model import PrintSpec, Segment, State


async def test_p2_runout_with_spoolman_updated_during_pause(world: World):
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
