"""Writing Spoolman's spool to the printer: docs/scenarios.md row W1."""

from __future__ import annotations

from integration.conftest import PETG, World, events


async def test_w1_write_after_swap(world: World):
    """Menu load after a Spoolman-first swap: the bridge writes name, color and UUID."""
    w = world
    b = w.spools["B"]
    records = await w.poll()
    assert len(events(records, "baseline")) == 1

    # S1, with B loaded from the menu: the printer gets the preset's name and no color.
    await w.move("B", w.tool_location)
    await w.poll()
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_committed")) == 1
    # No polls during the load, so the first idle poll below is the one that writes.
    await w.swap_cycle(source="preset", name=PETG[0])
    assert w.material() == (PETG[0], None, None)
    assert w.requests("PUT") == []

    # Idle and different from B: the bridge uploads the sync job.
    records = await w.poll()
    assert w.charged(records, "B") == 36 + 40
    puts = w.requests("PUT")
    assert len(puts) == 1 and "spoolman-sync.gcode" in puts[0].text
    w.advance(1.5)
    assert w.printer.job is None
    gcode = [e.text for e in w.sim.events if e.kind == "gcode"]
    assert any(t.startswith("M865 T0 set to") for t in gcode), gcode
    assert any(t.startswith("M865.1 T0") for t in gcode), gcode
    minted = await w.spoolman.extra(b, "instance_uuid")
    assert minted is not None
    assert w.material() == (PETG[0], PETG[1], minted)

    # The write took: the next poll finds nothing to report or write, and no drift flag.
    records = await w.poll()
    assert records == []
    assert len(w.requests("PUT")) == 1
    assert await w.flag("B") is None
