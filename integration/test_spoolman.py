"""Spoolman faults and edits made in Spoolman: docs/scenarios.md rows M1 to M7."""

from __future__ import annotations

import logging

import pytest

from integration.conftest import (
    FLAG_FIELD,
    LOAD_MM,
    PETG,
    World,
    charges,
    count,
    events,
    logged,
    print_spec,
)
from integration.faults import archive_spool, delete_spool, rename_location, set_extra_text
from simulator.model import State


def use_path(spool_id: int) -> str:
    return f"/api/v1/spool/{spool_id}/use"


async def baseline(w: World) -> None:
    records = await w.poll()
    assert len(events(records, "baseline")) == 1


async def set_used(w: World, name: str, grams: float) -> None:
    """Set a spool's used weight, as the user would correct it in Spoolman."""
    resp = await w.spoolman.client.patch(
        f"/api/v1/spool/{w.spools[name]}", json={"used_weight": grams}
    )
    resp.raise_for_status()


async def test_m1_spoolman_unreachable(world: World, caplog: pytest.LogCaptureFixture):
    """Spoolman unreachable: polls skip and log once; queued charges go in order when back."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    faults = w.spoolman_faults
    await baseline(w)
    tool = w.bridge.printers[0].tools[0]

    # Down with nothing queued: each poll reads the printer, fails to read Spoolman and
    # stops there. Nothing is charged and the counter isn't advanced.
    charged_at = tool.charged
    faults.cut()
    for _ in range(3):
        w.printer.extrude(0, 20)
        assert await w.poll() == []
    assert w.bridge.printers[0].tools[0].charged == charged_at
    assert count(caplog, logging.WARNING, "Spoolman unreachable") == 1

    # Back: one poll logs it and charges everything since in one charge.
    faults.restore()
    records = await w.poll()
    assert [r["use_length"] for r in charges(records)] == [60.0]
    assert count(caplog, logging.INFO, "Spoolman reachable again") == 1

    # A charge Spoolman fails with a 503 stays queued.
    caplog.clear()
    faults.answer("PUT", use_path(a), 503)
    w.printer.extrude(0, 30)
    records = await w.poll()
    assert charges(records) == []
    assert [r["use_length"] for r in w.bridge.state.outbox] == [30.0]
    assert logged(caplog, logging.WARNING, f"charge to spool {a} failed (503); 1 queued")

    # Spoolman goes down with it queued: each poll tries the outbox first and stops.
    faults.cut()
    for _ in range(3):
        w.printer.extrude(0, 10)
        assert await w.poll() == []
    assert [r["use_length"] for r in w.bridge.state.outbox] == [30.0]
    assert count(caplog, logging.WARNING, f"charge to spool {a} failed") == 2

    # Back: the queued charge goes first, then everything since, once each.
    faults.restore()
    records = await w.poll()
    assert [r["use_length"] for r in charges(records)] == [30.0, 30.0]
    assert w.bridge.state.outbox == []
    assert count(caplog, logging.WARNING, "sent 1 queued charge(s)") == 1
    await w.poll()
    assert count(caplog, logging.WARNING, "sent ") == 1
    # Every millimetre was charged once: three charges reached Spoolman, for all 120 mm,
    # and the queued one was tried once a poll while Spoolman was down.
    assert w.charged(w.since(0), "A") == 60 + 30 + 30 == w.counter - 1000
    sent = [r for r in faults.requests if r.startswith(f"PUT {use_path(a)} ")]
    down = ["unreachable"] * 3
    assert [r.rsplit(" ", 1)[1] for r in sent] == ["200", "503", *down, "200", "200"]


async def test_m2_rejected_charge_is_dropped(world: World, caplog: pytest.LogCaptureFixture):
    """A charge Spoolman rejects with a 4xx is dropped with an error; later ones go through."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await baseline(w)

    w.spoolman_faults.answer("PUT", use_path(a), 422, {"detail": "no such filament"})
    w.printer.extrude(0, 50)
    records = await w.poll()
    assert charges(records) == []
    failed = [r for r in records if "failed" in r]
    assert [(r["spool_id"], r["use_length"], r["failed"]) for r in failed] == [(a, 50.0, "422")]
    assert w.bridge.state.outbox == []
    msg = f"Spoolman rejected a +50 mm charge to spool {a} (422 "
    assert count(caplog, logging.ERROR, msg) == 1
    assert await w.used("A") == 0

    w.printer.extrude(0, 20)
    records = await w.poll()
    assert [r["use_length"] for r in charges(records)] == [20.0]
    assert await w.used("A") > 0
    assert count(caplog, logging.ERROR, "Spoolman rejected") == 1


async def test_m3_deleted_active_spool_holds_usage(world: World, caplog: pytest.LogCaptureFixture):
    """Active spool deleted: its usage is held, then charged to the next spool made active."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    w.printer.extrude(0, 20)
    await delete_spool(w.spoolman.client, a)
    records = await w.poll()
    tool = w.bridge.printers[0].tools[0]
    assert tool.active is None and tool.deleted_spool == a
    # Found by the list and the lookup, before any charge: nothing was sent and rejected.
    assert charges(records) == [] and [r for r in records if "failed" in r] == []
    found = f"[{w.tool_location}] spool {a} was deleted in Spoolman; holding its usage"
    assert count(caplog, logging.WARNING, found) == 1

    # Usage goes on being held, through an unload, and the warning isn't repeated.
    w.printer.extrude(0, 30)
    await w.poll()
    await w.unload()
    await w.poll()
    assert count(caplog, logging.WARNING, found) == 1
    assert w.bridge.printers[0].tools[0].held_mm == 50

    # B moved in and loaded: it becomes active and takes the held usage, then its load.
    await w.move("B", w.tool_location)
    records = await w.poll()
    records += await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    records += await w.poll()
    held = events(records, "held_charged")
    charged = f"spool {b} is active; charged it 50 mm held after spool {a} was deleted"
    assert [e["message"] for e in held] == [charged]
    assert count(caplog, logging.INFO, charged) == 1
    assert w.charged(records, "B") == 50 + LOAD_MM
    tool = w.bridge.printers[0].tools[0]
    assert tool.active == b and tool.deleted_spool is None and tool.held_mm == 0
    assert count(caplog, logging.WARNING, found) == 1
    holding = f"[{w.tool_location}] holding usage since spool {a} was deleted"
    assert count(caplog, logging.WARNING, holding) == 1


async def test_m3_charge_to_deleted_spool_is_held(world: World, caplog: pytest.LogCaptureFixture):
    """A charge that 404s because the spool was deleted is held for the next spool."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await baseline(w)

    # The charge is queued by a poll that fails to send it, then A is deleted.
    w.spoolman_faults.answer("PUT", use_path(a), 503)
    w.printer.extrude(0, 40)
    await w.poll()
    assert [r["use_length"] for r in w.bridge.state.outbox] == [40.0]
    await delete_spool(w.spoolman.client, a)
    records = await w.poll()
    held = [r for r in records if r.get("held")]
    assert [(r["spool_id"], r["use_length"], r["failed"]) for r in held] == [(a, 40.0, "404")]
    assert charges(records) == []
    tool = w.bridge.printers[0].tools[0]
    assert tool.active is None and tool.deleted_spool == a and tool.held_mm == 40
    found = f"[{w.tool_location}] spool {a} was deleted in Spoolman; holding its usage"
    assert count(caplog, logging.WARNING, found) == 1
    assert not logged(caplog, logging.ERROR, "Spoolman rejected")
    held_msg = "sent 0 queued charge(s); held 1 for the next spool"
    assert count(caplog, logging.WARNING, held_msg) == 1

    # The next spool moved into the location takes it.
    await w.move("C", w.tool_location)
    records = await w.poll()
    assert w.charged(records, "C") == 40
    assert count(caplog, logging.WARNING, found) == 1


async def test_m3a_archived_active_spool(world: World, caplog: pytest.LogCaptureFixture):
    """Active spool archived: still charged and flagged until the next unload."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await baseline(w)

    # Archived spools drop out of Spoolman's list, so A looks moved out (S10): it stays
    # active and charged, and is flagged. Each is logged once, not every poll.
    await archive_spool(w.spoolman.client, a)
    mark = w.mark()
    for _ in range(3):
        w.printer.extrude(0, 10)
        await w.poll()
    records = w.since(mark)
    assert w.charged(records, "A") == 30
    assert all(r["grams"] for r in charges(records, a))  # From the archived spool (#56).
    assert await w.used("A") > 0
    assert w.bridge.printers[0].tools[0].active == a
    found = f"[{w.tool_location}] spool {a} is archived but still loaded; charging it until"
    assert count(caplog, logging.WARNING, found) == 1
    assert len(events(records, "active_unassigned")) == 1
    assert await w.flag("A") == f"{w.tool_location}: spool is archived but still loaded"
    assert count(caplog, logging.INFO, f"set flag on spool {a}") == 1

    # The unload ends it: the flag clears and usage is held for the next spool assigned.
    await w.unload()
    records = await w.poll()
    assert [e["event"] for e in events(records)] == ["unload", "active_unassigned"]
    assert w.bridge.printers[0].tools[0].active is None
    assert await w.flag("A") is None
    await w.swap_cycle(source="tag", name=PETG[0], color=PETG[1])
    records = await w.poll()
    assert charges(records) == []
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert w.charged(records, "B") == LOAD_MM
    assert w.charged(w.since(mark), "A") == 30
    assert count(caplog, logging.WARNING, found) == 1


async def test_m4_location_renamed(world: World, caplog: pytest.LogCaptureFixture):
    """Location renamed: the spool stays active until the next unload, then usage is held."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await baseline(w)

    # Renamed in Spoolman: the tool's location is empty, so A looks moved out (S10).
    renamed = f"{w.tool_location} renamed"
    await rename_location(w.spoolman.client, w.tool_location, renamed)
    assert await w.spoolman.location(a) == renamed
    mark = w.mark()
    for _ in range(2):
        w.printer.extrude(0, 10)
        await w.poll()
    records = w.since(mark)
    msg = f"spool {a} is no longer in the location; still charging it until the printer"
    assert [e["message"] for e in events(records)] == [msg + " reports an unload"]
    assert w.charged(records, "A") == 20
    assert w.bridge.printers[0].tools[0].active == a

    # At the unload A stops being active, and the load after it is held.
    await w.unload()
    records = await w.poll()
    unassigned = f"spool {a} unloaded with no spool in the location; holding usage"
    assert [e["message"] for e in events(records, "active_unassigned")] == [
        unassigned + " until one is assigned"
    ]
    await w.swap_cycle(source="tag", name=PETG[0], color=PETG[1])
    for _ in range(2):
        assert await w.poll() == []
    assert w.bridge.printers[0].tools[0].active is None
    assert count(caplog, logging.WARNING, "holding usage: no spool assigned") == 1

    # A spool moved into the location (by its old name, as configured) takes the hold.
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert w.charged(records, "B") == LOAD_MM
    assert w.charged(w.since(mark), "A") == 20
    assert await w.spoolman.location(a) == renamed


async def test_m5_spool_used_past_empty(world: World, caplog: pytest.LogCaptureFixture):
    """Spool used past empty: still charged, a negative weight logged once, no flag."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await baseline(w)
    # 1 g left of 1000 g; each metre of 1.75 mm PLA at 1.24 g/cm³ is about 3 g.
    await set_used(w, "A", 999.0)

    mark = w.mark()
    for _ in range(4):
        w.printer.extrude(0, 1000)
        await w.poll()
    records = w.since(mark)
    assert [r["use_length"] for r in charges(records, a)] == [1000.0] * 4
    assert await w.used("A") > 1010
    negative = f"spool {a} shows -"
    assert count(caplog, logging.INFO, negative) == 1
    assert logged(caplog, logging.INFO, "g remaining in Spoolman but is still loaded")
    assert await w.flag("A") is None


@pytest.mark.parametrize("used, flagged", [(0.0, True), (960.0, False)], ids=["plenty", "nearly"])
async def test_m5a_runout_with_plenty_left_is_flagged(world: World, used: float, flagged: bool):
    """Runout in a started print with over 5% left flags the spool; a weight edit clears it."""
    w = world
    a = w.spools["A"]
    await baseline(w)
    await set_used(w, "A", used)
    await w.poll()

    # The print is started (two polls with the counter moving), then A runs out.
    await w.start_print(print_spec())
    w.printer.runout(0)
    records = await w.poll()
    assert len(events(records, "unload")) == 1
    mid_print = f"{w.tool_location}: unloaded mid-print with"
    if not flagged:
        # 40 g of 1000 g is under 5%: it really ran out.
        assert mid_print not in (await w.flag("A") or "")
        assert w.bridge.state.runouts == {}
        return
    assert mid_print in (await w.flag("A") or "")
    assert a in w.bridge.state.runouts

    # The same spool reloaded with no Spoolman change, and the print finished: kept.
    w.printer.load(0)
    await w.run_until(lambda: w.printer.state is State.PAUSED, poll_every=1.0)
    w.printer.resume()
    records = await w.run_until(lambda: w.printer.job is None, poll_every=1.0)
    records += await w.poll()
    assert w.charged(records, "A") > 0
    assert mid_print in (await w.flag("A") or "")
    assert "filament was unloaded with no Spoolman change" in (await w.flag("A") or "")

    # The user corrects A's weight in Spoolman: the flag clears.
    await set_used(w, "A", 995.0)
    await w.poll()
    assert mid_print not in (await w.flag("A") or "")
    assert w.bridge.state.runouts == {}


async def test_m6_flags_by_others_left_alone(world: World, caplog: pytest.LogCaptureFixture):
    """A flag someone else wrote stays while the bridge sets and clears its own elsewhere."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b, c = w.spools["A"], w.spools["B"], w.spools["C"]
    await baseline(w)
    await w.spoolman.ensure_text_field(FLAG_FIELD, "Bridge flag")
    for name in "ABC":
        await set_extra_text(w.spoolman.client, w.spools[name], FLAG_FIELD, f"note on {name}")

    # S2: the unload flags A. The bridge's flag replaces the note on A, which is lost when
    # the bridge clears its flag below: only spools it never flags keep their notes.
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    flag = await w.flag("A")
    assert flag is not None and "unloaded with no Spoolman change" in flag
    assert [await w.flag("B"), await w.flag("C")] == ["note on B", "note on C"]

    # Spoolman catches up: the bridge clears its flag on A and leaves B's and C's, even
    # with B now the active spool and loaded.
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_confirmed")) == 1
    await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    await w.poll()
    assert await w.flag("A") is None
    assert [await w.flag("B"), await w.flag("C")] == ["note on B", "note on C"]
    assert logged(caplog, logging.INFO, f"cleared flag on spool {a}")
    for spool_id in (b, c):
        assert not logged(caplog, logging.INFO, f"flag on spool {spool_id}")


async def test_m7_density_edit_applies_to_later_charges(world: World):
    """Density edited in Spoolman: later charges use the new one, earlier aren't recalculated."""
    w = world
    a = w.spools["A"]
    await baseline(w)

    w.printer.extrude(0, 1000)
    first = charges(await w.poll(), a)
    before = await w.used("A")
    assert [r["use_length"] for r in first] == [1000.0]
    assert before == pytest.approx(first[0]["grams"], abs=0.01)

    # Twice the density: the next metre weighs twice as much, and the first stays as it was.
    await w.spoolman.edit_filament(a, density=2.48)
    assert await w.used("A") == pytest.approx(before)
    w.printer.extrude(0, 1000)
    second = charges(await w.poll(), a)
    assert [r["use_length"] for r in second] == [1000.0]
    assert second[0]["grams"] == pytest.approx(2 * first[0]["grams"], abs=0.01)
    assert await w.used("A") == pytest.approx(3 * before, abs=0.01)

    # A charge queued while Spoolman is down is weighed when it arrives: the bridge sends a
    # length, so a density edit made meanwhile applies to it.
    w.spoolman_faults.answer("PUT", use_path(a), 503)
    w.printer.extrude(0, 1000)
    await w.poll()
    assert [r["use_length"] for r in w.bridge.state.outbox] == [1000.0]
    await w.spoolman.edit_filament(a, density=3.72)
    w.spoolman_faults.restore()
    await w.poll()
    assert w.bridge.state.outbox == []
    assert await w.used("A") == pytest.approx(6 * before, abs=0.01)
