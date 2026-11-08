"""Spool swaps between prints: docs/scenarios.md rows S1, S2 and S14."""

from __future__ import annotations

from integration.conftest import ASA, PETG, World, charges, events

# A load counts the feed after the gap, then the purge under the new material.
LOAD_MM = 36 + 40


async def baseline(w: World) -> None:
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    assert charges(records) == []


async def test_s1_spoolman_first_swap(world: World):
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    # Spoolman first: B joins A in the location. A stays active and is still charged.
    await w.move("B", w.tool_location)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert len(events(records, "swap_staged")) == 1
    assert w.charged(records, "A") == 20

    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "unload")) == 1
    committed = events(records, "swap_committed")
    assert [e["message"] for e in committed] == [f"swap done: spool {a} -> {b}"]
    assert w.charged(records, "A") == 30
    assert await w.spoolman.location(w.spools["A"]) is None  # Evicted to no location.

    before = w.tool.extruded
    records = await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    records += await w.poll()
    assert w.tool.extruded - before == LOAD_MM
    assert w.charged(records, "B") == LOAD_MM
    assert w.charged(records, "A") == 0
    assert await w.used("B") > 0


async def test_s2_filament_first_swap(world: World):
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    assert w.charged(records, "A") == 30
    flag = await w.flag("A")
    assert flag is not None and "unloaded" in flag

    # Spoolman catches up before the next poll: A out, B in.
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    confirmed = events(records, "swap_confirmed")
    assert len(confirmed) == 1
    assert f"from spool {a} to {b}" in confirmed[0]["message"]
    assert await w.flag("A") is None

    records = await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    records += await w.poll()
    assert w.charged(records, "B") == LOAD_MM
    assert w.charged(records, "A") == 0


async def test_s14_several_swaps_between_polls(world: World):
    w = world
    await baseline(w)

    # A -> B -> C at the printer with the bridge not looking, then Spoolman says A -> C.
    w.printer.extrude(0, 30)
    await w.unload()
    await w.swap_cycle(source="tag", name=PETG[0], color=PETG[1])
    await w.unload()
    await w.swap_cycle(source="tag", name=ASA[0], color=ASA[1])
    await w.move("A", w.shelf)
    await w.move("C", w.tool_location)

    records = await w.poll()
    unloads = events(records, "unload")
    assert len(unloads) == 1
    assert unloads[0]["message"].startswith("2 unload(s)")
    committed = events(records, "swap_committed")
    assert [e["message"] for e in committed] == [
        f"swap done: spool {w.spools['A']} -> {w.spools['C']}"
    ]
    # Everything up to the second unload, B's load included, is A's.
    assert w.charged(records, "A") == 30 + LOAD_MM

    records += await w.poll()
    assert w.charged(records, "C") == LOAD_MM
    assert w.charged(records, "B") == 0
    assert {r["spool_id"] for r in charges(records)} == {w.spools["A"], w.spools["C"]}
    assert await w.used("B") == 0
