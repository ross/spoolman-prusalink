"""Spool swaps and claims: docs/scenarios.md rows S1, S2, S5, S9, S13 and S14."""

from __future__ import annotations

import logging

import pytest

from integration.conftest import (
    ASA,
    LOAD_MM,
    PETG,
    World,
    charged,
    charges,
    events,
    logged,
    print_spec,
)
from simulator.model import State


async def baseline(w: World) -> None:
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    assert charges(records) == []


async def test_s1_spoolman_first_swap(world: World):
    """Spoolman updated before the unload: the swap commits there, the load is the new spool's."""
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
    """Filament swapped first: the charge is held, then moves once Spoolman is updated."""
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
    """Two swaps between polls: two unloads seen, the middle spool is charged nothing."""
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


async def stage_b(w: World, caplog: pytest.LogCaptureFixture) -> None:
    """Spoolman says B is in the tool with no unload: B is staged, A still charged."""
    a, b = w.spools["A"], w.spools["B"]
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    msg = f"spool {b} staged; still charging spool {a} until the printer reports an unload"
    assert [e["message"] for e in events(records, "swap_staged")] == [msg]
    assert logged(caplog, logging.INFO, f"[{w.tool_location}] {msg}")


async def test_s5_claim_after_printing_on_the_wrong_spool(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Print charged provisionally to the old spool: claiming the loaded one moves it there."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    assert w.charged(records, "A") == 30
    unloaded = w.mark()

    # B goes in at the printer, Spoolman is forgotten, and a print starts.
    await w.swap_cycle(source="tag", name=PETG[0], color=PETG[1])
    records = await w.poll()
    assert w.charged(records, "A") == LOAD_MM
    # A is still active and the printer shows B's tag, so that poll wrote A back.
    assert len(w.requests("PUT")) == 1
    await w.run_until(lambda: w.printer.job is None)
    records = await w.start_print(print_spec())
    msg = (
        "print started with no Spoolman change since the unload; assuming "
        f"spool {a} was reloaded and charging it provisionally"
    )
    assert [e["message"] for e in events(records, "swap_assumed")] == [msg]
    assert logged(caplog, logging.WARNING, f"[{w.tool_location}] {msg}")
    flag = await w.flag("A")
    assert flag is not None
    assert "charged to it provisionally" in flag and "tick 'Claim held usage'" in flag

    # The user moves B into the location, leaving A there too, and claims B.
    await w.move("B", w.tool_location)
    await w.claim("B")
    records = await w.poll()
    claimed = events(records, "claimed")
    assert len(claimed) == 1
    assert claimed[0]["message"].startswith(f"spool {b} claimed in place of spool {a}: moved ")
    assert claimed[0]["message"].endswith("(usage since the last unload)")
    since_unload = w.since(unloaded)
    assert charged(since_unload, a) == 0
    assert charged(since_unload, b) == w.counter - w.tool.extruded_at_unload
    assert logged(caplog, logging.INFO, f"[{w.tool_location}] moved spool {a} to")
    assert await w.spoolman.location(a) != w.tool_location
    assert logged(caplog, logging.INFO, f"cleared the claim on spool {b}")
    assert await w.spoolman.extra(b, "bridge_claim") is None
    assert await w.flag("A") is None
    puts = len(w.requests("PUT"))

    # The rest of the print is B's.
    mark = w.mark()
    at_claim = w.counter
    await w.run_until(
        lambda: w.printer.state is State.FINISHED and w.printer.job is None, poll_every=1.0
    )
    records = w.since(mark) + await w.poll()
    assert w.charged(records, "A") == 0
    assert w.charged(records, "B") == w.counter - at_claim

    # Idle with the printer showing A: the write of B queued at the claim runs.
    assert len(w.requests("PUT")) == puts + 1
    w.advance(1.5)
    minted = await w.spoolman.extra(b, "instance_uuid")
    assert minted is not None
    assert w.material() == (PETG[0], PETG[1], minted)


async def test_s13_staged_spool_claimed(world: World, caplog: pytest.LogCaptureFixture):
    """Staged spool, no unload: a claim moves the usage since the reboot and makes it active."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    await stage_b(w, caplog)

    w.printer.extrude(0, 30)
    records = await w.poll()
    assert w.charged(records, "A") == 30

    await w.claim("B")
    records = await w.poll()
    assert [e["message"] for e in events(records, "claimed")] == [
        f"spool {b} claimed in place of spool {a}: moved 30 mm from spool {a} "
        "(usage since the last reboot)"
    ]
    assert w.charged(records, "B") == 30
    assert w.charged(records, "A") == -30

    # B is active: the bridge writes it to the printer, and B is charged from here on.
    await w.run_until(lambda: w.printer.job is None)
    minted = await w.spoolman.extra(b, "instance_uuid")
    assert w.material() == (PETG[0], PETG[1], minted)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert w.charged(records, "B") == 20
    assert w.charged(records, "A") == 0


async def test_s9_staged_spool_never_unloaded(world: World, caplog: pytest.LogCaptureFixture):
    """Staged and never unloaded: print starts and reboots flag it, and a claim makes it active."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    await stage_b(w, caplog)

    start = w.counter
    mark = w.mark()
    records = await w.start_print(print_spec())
    msg = f"print started while spool {b} is staged but not loaded; charging spool {a}"
    assert [e["message"] for e in events(records, "staged_at_print")] == [msg]
    assert logged(caplog, logging.WARNING, f"[{w.tool_location}] {msg}")
    flag_a, flag_b = await w.flag("A"), await w.flag("B")
    assert flag_a is not None and msg in flag_a
    assert flag_b is not None and msg in flag_b
    assert flag_b.endswith(" If this spool is the one loaded, tick 'Claim held usage' on it.")

    await w.run_until(
        lambda: w.printer.state is State.FINISHED and w.printer.job is None, poll_every=1.0
    )
    records = w.since(mark) + await w.poll()
    assert w.charged(records, "A") == w.counter - start
    assert w.charged(records, "B") == 0
    w.printer.dismiss()

    w.printer.reboot()
    records = await w.poll()
    assert len(events(records, "reboot")) == 1
    msg = (
        f"printer restarted while spool {b} is staged; an unload just before the restart "
        f"can be missed, so check which spool is loaded. Still charging spool {a}"
    )
    assert [e["message"] for e in events(records, "staged_at_reboot")] == [msg]
    assert logged(caplog, logging.WARNING, f"[{w.tool_location}] {msg}")
    flag_b = await w.flag("B")
    assert flag_b is not None and msg in flag_b

    await w.claim("B")
    records = await w.poll()
    # The reboot restarted the counters, so there's no usage to move.
    assert [e["message"] for e in events(records, "claimed")] == [
        f"spool {b} claimed in place of spool {a}: moved nothing (usage since the last reboot)"
    ]
    await w.run_until(lambda: w.printer.job is None)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert w.charged(records, "B") == 20
    assert w.charged(records, "A") == 0
