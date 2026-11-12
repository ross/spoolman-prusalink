"""Spoolman faults and edits made in Spoolman: docs/scenarios.md rows M1 to M7."""

from __future__ import annotations

import json
import logging
import math

import httpx
import pytest

from integration.conftest import (
    CLAIM_FIELD,
    FLAG_FIELD,
    LOAD_MM,
    PETG,
    UUID_FIELD,
    World,
    charges,
    count,
    events,
    logged,
    print_spec,
)
from integration.faults import (
    archive_spool,
    delete_spool,
    rename_location,
    set_extra_text,
    unarchive_spool,
)
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
    assert count(caplog, logging.WARNING, "Spoolman reachable again") == 1

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
    # The hold is logged by the deleted-spool line above, not as a note on each poll.
    holding = f"[{w.tool_location}] holding usage since spool {a} was deleted"
    assert not logged(caplog, logging.WARNING, holding)
    assert not logged(caplog, logging.WARNING, "no longer: holding usage since")


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


async def test_m3_404_for_a_spool_no_longer_active_is_dropped(
    world: World, caplog: pytest.LogCaptureFixture
):
    """A 404 on a charge to a spool no longer active is dropped, not held for the next spool."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    # S1: B moved in, then A unloaded. The poll that commits the swap queues A's last
    # charge, and A is deleted just before it's sent.
    await w.move("B", w.tool_location)
    await w.poll()
    deleted = False

    async def delete_first(request):
        nonlocal deleted
        if not deleted and request.method == "PUT" and request.url.path == use_path(a):
            deleted = True
            await delete_spool(w.spoolman.client, a)
        return None

    w.spoolman_faults.hook = delete_first
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert deleted
    assert len(events(records, "swap_committed")) == 1
    failed = [r for r in records if "failed" in r]
    assert [(r["spool_id"], r["use_length"], r["failed"]) for r in failed] == [(a, 30.0, "404")]
    assert not failed[0].get("held")
    assert charges(records) == []
    msg = f"Spoolman rejected a +30 mm charge to spool {a} (404 "
    assert count(caplog, logging.ERROR, msg) == 1
    tool = w.bridge.printers[0].tools[0]
    assert tool.active == b and tool.deleted_spool is None and tool.held_mm == 0
    assert not logged(caplog, logging.WARNING, f"spool {a} was deleted in Spoolman")

    # B's load is charged to B, and nothing of A's goes with it.
    w.spoolman_faults.hook = None
    records = await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    records += await w.poll()
    assert w.charged(records, "B") == LOAD_MM
    assert events(records, "held_charged") == []


async def test_m3_deleted_during_an_unconfirmed_swap(world: World):
    """Active spool deleted with an unconfirmed swap open: the swap's usage is held once."""
    w = world
    a = w.spools["A"]
    faults = w.spoolman_faults
    await baseline(w)

    # S2: A unloaded and a spool loaded with no Spoolman change. The load is charged to A
    # as the swap's usage.
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    records = await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    records += await w.poll()
    assert w.charged(records, "A") == LOAD_MM

    # The next charge to A fails with 503 over two polls; the second doesn't get past it.
    faults.answer("PUT", use_path(a), 503, times=None)
    w.printer.extrude(0, 20)
    await w.poll()
    await w.poll()
    assert [r["use_length"] for r in w.bridge.state.outbox] == [20.0]
    tool = w.bridge.printers[0].tools[0]
    assert tool.swap is not None and tool.swap.mm == LOAD_MM + 20

    # A is deleted: the queued charge 404s, and the swap's usage is held, the queued
    # part once.
    await delete_spool(w.spoolman.client, a)
    faults.restore()
    records = await w.poll()
    held = [r for r in records if r.get("held")]
    assert [(r["spool_id"], r["use_length"], r["failed"]) for r in held] == [(a, 20.0, "404")]
    tool = w.bridge.printers[0].tools[0]
    assert tool.active is None and tool.deleted_spool == a and tool.swap is None
    assert tool.held_mm == LOAD_MM + 20


async def test_m3_staged_spool_takes_held_usage_at_print_start(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Active spool deleted with another staged: a print start makes it active and charges it."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    start = w.counter

    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_staged")) == 1

    await delete_spool(w.spoolman.client, a)
    w.printer.extrude(0, 20)
    records = await w.poll()
    tool = w.bridge.printers[0].tools[0]
    assert tool.active is None and tool.deleted_spool == a and tool.assigned == b
    assert tool.held_mm == 20
    assert charges(records) == []
    flag = await w.flag("B")
    assert flag is not None and f"because spool {a} was deleted" in flag

    # A print starts with no unload: B is made active and takes everything held.
    records = await w.start_print(print_spec())
    assert [e["message"] for e in events(records, "active_at_print")] == [
        f"print started with spool {b} assigned; making it active in place of deleted spool {a}"
    ]
    held = events(records, "held_charged")
    assert len(held) == 1
    assert held[0]["message"].startswith(f"spool {b} is active; charged it ")
    assert held[0]["message"].endswith(f" mm held after spool {a} was deleted")
    tool = w.bridge.printers[0].tools[0]
    assert tool.active == b and tool.deleted_spool is None and tool.held_mm == 0
    assert w.charged(w.since(0), "B") == w.counter - start
    assert w.charged(w.since(0), "A") == 0
    await w.poll()
    assert await w.flag("B") is None


async def test_m3_unload_with_no_poll_after_the_extrusion(world: World):
    """Deleted spool found, then extrusion and an unload in one poll: the extrusion is held."""
    w = world
    a = w.spools["A"]
    await baseline(w)

    await delete_spool(w.spoolman.client, a)
    await w.poll()
    tool = w.bridge.printers[0].tools[0]
    assert tool.deleted_spool == a and tool.held_mm == 0

    w.printer.extrude(0, 20)
    await w.unload()
    records = await w.poll()
    assert [e["message"] for e in events(records, "unload")] == [
        f"1 unload(s); 20 mm before the unload is held because spool {a} was deleted"
    ]
    assert charges(records) == []
    tool = w.bridge.printers[0].tools[0]
    assert tool.deleted_spool == a and tool.held_mm == 20


async def test_m3_delete_and_recreate(world: World, caplog: pytest.LogCaptureFixture):
    """Deleted spool re-created with the printer's UUID: a correction, charged the held usage."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    await w.move("B", w.tool_location)
    await w.poll()
    await delete_spool(w.spoolman.client, a)
    w.printer.extrude(0, 20)
    records = await w.poll()
    tool = w.bridge.printers[0].tools[0]
    assert tool.active is None and tool.deleted_spool == a and tool.held_mm == 20
    assert events(records, "correction") == []

    # B stands for A re-created: the user gives it the UUID the printer reports.
    await set_extra_text(w.spoolman.client, b, UUID_FIELD, w.uuids["A"])
    w.printer.extrude(0, 10)
    records = await w.poll()
    assert [e["message"] for e in events(records, "correction")] == [
        f"printer already has spool {b}'s filament; making it active"
    ]
    assert [e["message"] for e in events(records, "held_charged")] == [
        f"spool {b} is active; charged it 20 mm held after spool {a} was deleted"
    ]
    assert w.charged(records, "B") == 20 + 10
    tool = w.bridge.printers[0].tools[0]
    assert tool.active == b and tool.deleted_spool is None and tool.held_mm == 0
    # The printer shows A's PLA under that UUID, so B's PETG is written, keeping the UUID.
    w.advance(1.5)
    assert w.material() == (PETG[0], PETG[1], w.uuids["A"])


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
    assert [e["event"] for e in events(records)] == ["unload", "unloaded_unassigned"]
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
    assert [e["message"] for e in events(records, "unloaded_unassigned")] == [
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


@pytest.mark.parametrize(
    "status, body",
    [(500, None), (200, "<html>not JSON</html>")],
    ids=["5xx", "bad-json"],
)
async def test_m1_spool_list_fails(
    world: World, caplog: pytest.LogCaptureFixture, status: int, body: str | None
):
    """Spool list answers 5xx or bad JSON: polls skip, logged once, not as an outage."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    faults = w.spoolman_faults
    await baseline(w)
    charged_at = w.bridge.printers[0].tools[0].charged

    # Spoolman answers, badly: each poll stops after reading the list, and the tool's
    # counter isn't advanced, so nothing is lost.
    faults.answer("GET", "/api/v1/spool", status, body, times=None)
    faults.requests.clear()
    for _ in range(3):
        w.printer.extrude(0, 20)
        assert await w.poll() == []
    assert w.bridge.printers[0].tools[0].charged == charged_at
    assert not w.bridge._spoolman_reach.down
    assert faults.requests.count(f"GET /api/v1/spool {status}") == 3
    failed = "Spoolman read failed, skipping poll"
    assert count(caplog, logging.WARNING, failed) == 1
    assert count(caplog, logging.DEBUG, failed) == 2
    assert not logged(caplog, logging.WARNING, "unreachable")

    # It answers properly again: one line says so, and the usage since goes in one charge.
    faults.restore()
    records = await w.poll()
    assert [r["use_length"] for r in charges(records)] == [60.0]
    await w.poll()
    assert count(caplog, logging.WARNING, "Spoolman reads working again after") == 1
    assert count(caplog, logging.WARNING, failed) == 1
    assert not logged(caplog, logging.WARNING, "reachable again")


@pytest.mark.parametrize("ends", ["looked-up", "listed-again"])
async def test_m3_lookup_of_a_missing_spool_fails(
    world: World, caplog: pytest.LogCaptureFixture, ends: str
):
    """Active spool missing from the list and its lookup fails: charged, logged once."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    faults = w.spoolman_faults
    await baseline(w)

    # A is archived, so it drops out of the list, and the lookup that would say so fails.
    await archive_spool(w.spoolman.client, a)
    faults.answer("GET", f"/api/v1/spool/{a}", 500, times=None)
    mark = w.mark()
    for _ in range(3):
        w.printer.extrude(0, 10)
        await w.poll()
    records = w.since(mark)
    assert w.charged(records, "A") == 30
    assert w.bridge.printers[0].tools[0].active == a
    cant = f"[{w.tool_location}] can't look up spool {a}, which is missing from Spoolman's list"
    assert count(caplog, logging.WARNING, cant) == 1
    assert count(caplog, logging.DEBUG, f"still can't look up spool {a}") == 2
    assert faults.requests.count(f"GET /api/v1/spool/{a} 500") == 3

    if ends == "looked-up":
        # The lookup works again: A is found archived, said once, and flagged.
        faults.restore()
        for _ in range(2):
            w.printer.extrude(0, 10)
            await w.poll()
        assert count(caplog, logging.INFO, f"[{w.tool_location}] can look up spool {a} again") == 1
        archived = f"spool {a} is archived but still loaded"
        assert count(caplog, logging.WARNING, archived) == 1
        assert await w.flag("A") == f"{w.tool_location}: spool is archived but still loaded"
    else:
        # A is unarchived, so the list has it again and no lookup is needed.
        await unarchive_spool(w.spoolman.client, a)
        for _ in range(2):
            w.printer.extrude(0, 10)
            await w.poll()
        assert count(caplog, logging.INFO, f"spool {a} no longer needs looking up") == 1
        assert faults.requests.count(f"GET /api/v1/spool/{a} 500") == 3
    assert w.charged(w.since(mark), "A") == 50
    assert count(caplog, logging.WARNING, cant) == 1


def refuse_flag(w: World, name: str) -> None:
    """Make Spoolman refuse every write to a spool's flag field with a 500."""
    path = f"/api/v1/spool/{w.spools[name]}"
    w.spoolman_faults.answer("PATCH", path, 500, times=None, containing=FLAG_FIELD.encode())


async def test_m6_flag_write_fails(world: World, caplog: pytest.LogCaptureFixture):
    """Writing a flag fails: logged once, retried each poll, and the write logged when it works."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await baseline(w)

    refuse_flag(w, "A")
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    await w.poll()
    assert await w.flag("A") is None
    assert w.spoolman_faults.requests.count(f"PATCH /api/v1/spool/{a} 500") == 2
    cant = f"can't update the flag on spool {a}"
    assert count(caplog, logging.WARNING, cant) == 1
    assert count(caplog, logging.DEBUG, cant) == 1

    w.spoolman_faults.restore()
    await w.poll()
    await w.poll()
    flag = await w.flag("A")
    assert flag is not None and "unloaded with no Spoolman change" in flag
    assert count(caplog, logging.INFO, f"set flag on spool {a}") == 1
    assert count(caplog, logging.WARNING, cant) == 1


@pytest.mark.parametrize("gone", ["deleted", "archived"])
async def test_m6_flagged_spool_gone_while_its_flag_fails(
    world: World, caplog: pytest.LogCaptureFixture, gone: str
):
    """Spool deleted or archived while its flag fails: end logged once; archived flag cleared.

    An archived spool's stale flag is retried until it's cleared, and the clear ends the
    failure. A deleted spool has no flag left, so its failure just ends.
    """
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await baseline(w)

    # S2 flags A; then Spoolman is updated, but clearing A's flag fails.
    w.printer.extrude(0, 30)
    await w.unload()
    await w.poll()
    assert await w.flag("A") is not None
    assert w.bridge.state.flagged == [a]
    refuse_flag(w, "A")
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_confirmed")) == 1
    await w.poll()
    assert await w.flag("A") is not None
    cant = f"can't update the flag on spool {a}"
    assert count(caplog, logging.WARNING, cant) == 1

    # A leaves Spoolman's list, and Spoolman takes writes again. An archived A has its
    # flag cleared, and a deleted A has none. Either way the failure is over: one INFO line.
    logged_before = len(caplog.records)
    if gone == "deleted":
        await delete_spool(w.spoolman.client, a)
    else:
        await archive_spool(w.spoolman.client, a)
    w.spoolman_faults.restore()
    for _ in range(2):
        await w.poll()
    ended = [r for r in caplog.records[logged_before:] if r.levelno >= logging.INFO]
    assert [r.levelno for r in ended] == [logging.INFO], [r.getMessage() for r in ended]
    if gone == "archived":
        assert await w.flag("A") is None

    # Later polls say nothing more about it.
    logged_before = len(caplog.records)
    for _ in range(2):
        await w.poll()
    later = caplog.records[logged_before:]
    assert [r.getMessage() for r in later if r.levelno >= logging.INFO] == []
    assert not any(cant in r.getMessage() for r in later)
    assert count(caplog, logging.WARNING, cant) == 1


async def test_m2_charge_accepted_with_an_unreadable_reply(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Spoolman applies a charge but its 200 reply isn't JSON: the charge counts as sent."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await baseline(w)

    # The charge reaches Spoolman, and the reply the bridge gets back isn't JSON.
    async def unreadable(request: httpx.Request) -> httpx.Response | None:
        if request.method == "PUT" and request.url.path == use_path(a):
            resp = await w.spoolman.client.put(use_path(a), json=json.loads(request.content))
            resp.raise_for_status()
            return httpx.Response(200, text="OK", request=request)
        return None

    w.spoolman_faults.hook = unreadable
    w.printer.extrude(0, 50)
    records = await w.poll()
    assert [r["use_length"] for r in charges(records, a)] == [50.0]
    assert w.bridge.state.outbox == []
    used = await w.used("A")
    assert used == pytest.approx(charges(records, a)[0]["grams"], abs=0.01)

    # The next charge goes on its own: the first isn't sent again.
    w.spoolman_faults.hook = None
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert [r["use_length"] for r in charges(records, a)] == [20.0]
    assert await w.used("A") == pytest.approx(used + charges(records, a)[0]["grams"], abs=0.01)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def runout_flagged(w: World) -> None:
    """M5a: A runs out mid-print with plenty left, so it's flagged."""
    await baseline(w)
    await w.start_print(print_spec())
    w.printer.runout(0)
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    assert w.spools["A"] in w.bridge.state.runouts


async def test_m5a_weight_edited_before_a_charge(world: World, caplog: pytest.LogCaptureFixture):
    """A weight edited between the poll's spool read and a charge still clears the runout flag."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    mid_print = f"{w.tool_location}: unloaded mid-print with"
    await runout_flagged(w)
    w.printer.load(0)
    await w.run_until(lambda: w.printer.state is State.PAUSED, poll_every=1.0)
    w.printer.resume()

    # The user corrects A's weight just before the bridge's next charge to A lands.
    edited: list[bool] = []

    async def edit_first(request: httpx.Request) -> None:
        if not edited and request.method == "PUT" and request.url.path == use_path(a):
            edited.append(True)
            await set_used(w, "A", 500.0)

    w.spoolman_faults.hook = edit_first
    records = await w.run_until(lambda: bool(edited), poll_every=1.0)
    w.spoolman_faults.hook = None
    assert w.charged(records, "A") > 0
    # The charge's reply includes the edit, so the flag's weight isn't moved on to it.
    assert a in w.bridge.state.runouts
    assert w.bridge.state.runouts[a].used < 500

    # The next read sees the edit.
    await w.poll()
    assert w.bridge.state.runouts == {}
    assert mid_print not in (await w.flag("A") or "")
    edit = f"spool {a}'s weight was edited in Spoolman; clearing its runout flag"
    assert count(caplog, logging.INFO, edit) == 1
    await w.run_until(lambda: w.printer.job is None, poll_every=1.0)
    assert count(caplog, logging.INFO, edit) == 1


async def test_m5a_runout_spool_archived_before_its_refund(world: World):
    """Runout spool archived before a refund to it: the flag follows the refund, no false edit."""
    w = world
    a = w.spools["A"]
    mid_print = f"{w.tool_location}: unloaded mid-print with"
    await runout_flagged(w)

    # B goes in by the menu during the pause, charged to A as the open swap's usage.
    w.printer.load(0, source="preset", name=PETG[0])
    await w.run_until(lambda: w.printer.state is State.PAUSED, poll_every=1.0)
    swap_mm = w.bridge.printers[0].tools[0].swap.mm
    assert swap_mm > 0
    used = await w.used("A")

    # A is archived, then Spoolman is updated: the swap confirms and A is refunded.
    await archive_spool(w.spoolman.client, a)
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_confirmed")) == 1
    assert w.charged(records, "A") == -swap_mm
    assert await w.used("A") < used
    assert w.bridge.state.runouts[a].used == pytest.approx(await w.used("A"), abs=0.01)

    # Unarchived later, A's weight matches what the bridge followed: the flag stays.
    await unarchive_spool(w.spoolman.client, a)
    await w.poll()
    assert a in w.bridge.state.runouts
    assert mid_print in (await w.flag("A") or "")


async def test_m5a_refund_past_the_used_weight_is_not_an_edit(
    world: World, caplog: pytest.LogCaptureFixture
):
    """A refund bigger than the runout spool's used weight stops at 0: not taken for an edit."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    mid_print = f"{w.tool_location}: unloaded mid-print with"
    await runout_flagged(w)

    # B goes in by the menu during the pause, charged to A as the open swap's usage.
    w.printer.load(0, source="preset", name=PETG[0])
    await w.run_until(lambda: w.printer.state is State.PAUSED, poll_every=1.0)
    swap_mm = w.bridge.printers[0].tools[0].swap.mm
    assert swap_mm > 0
    used = await w.used("A")
    assert w.bridge.state.runouts[a].used == pytest.approx(used, abs=0.01)

    # A's density is raised, which isn't a weight edit, so the refund of the swap's
    # usage at the new density weighs more than all A has used.
    grams_per_mm = math.pi * (1.75 / 2) ** 2 / 1000  # Per g/cm³ of density.
    density = round(2 * used / (swap_mm * grams_per_mm), 2)
    await w.spoolman.edit_filament(a, density=density)
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_confirmed")) == 1
    refund = charges(records, a)
    assert [r["use_length"] for r in refund] == [-swap_mm]
    assert -refund[0]["grams"] > used

    # Spoolman stops the used weight at 0, and the flag's weight follows it there.
    assert await w.used("A") == 0.0
    assert w.bridge.state.runouts[a].used == 0.0
    for _ in range(2):
        await w.poll()
    assert a in w.bridge.state.runouts
    assert mid_print in (await w.flag("A") or "")
    assert not logged(caplog, logging.INFO, f"spool {a}'s weight was edited in Spoolman")
    assert not logged(caplog, logging.INFO, f"spool {a} shows -")


# --- Notes and flags for M3 and M5 (#111) ---


async def test_m3_staged_spool_flag_and_unload(world: World, caplog: pytest.LogCaptureFixture):
    """Active spool deleted with B staged: B's flag says why, and the unload gives B the usage."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_staged")) == 1

    await delete_spool(w.spoolman.client, a)
    w.printer.extrude(0, 20)
    await w.poll()
    w.printer.extrude(0, 10)
    await w.poll()
    loc = w.tool_location
    assert await w.flag("B") == (
        f"{loc}: usage is being held because spool {a} was deleted. It goes to this spool "
        "at the next unload or print start, or when this spool is claimed."
    )
    found = f"[{loc}] spool {a} was deleted in Spoolman; holding its usage"
    assert count(caplog, logging.WARNING, found) == 1
    assert count(caplog, logging.INFO, f"set flag on spool {b}") == 1

    # The unload makes B active: it takes the 30 mm held, and the hold and flag end once.
    await w.unload()
    records = await w.poll()
    assert [e["message"] for e in events(records, "held_charged")] == [
        f"spool {b} is active; charged it 30 mm held after spool {a} was deleted"
    ]
    assert w.charged(records, "B") == 30
    tool = w.bridge.printers[0].tools[0]
    assert tool.active == b and tool.deleted_spool is None and tool.held_mm == 0
    await w.poll()
    assert await w.flag("B") is None
    ended = f"[{loc}] no longer holding usage for a deleted spool"
    assert count(caplog, logging.WARNING, ended) == 1
    assert count(caplog, logging.INFO, f"cleared flag on spool {b}") == 1
    assert count(caplog, logging.WARNING, found) == 1


async def test_m3_staged_spool_claimed_takes_held_usage(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Active spool deleted with B staged, then B claimed: B is active and takes what's held."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    w.printer.extrude(0, 15)  # A has usage since load, which a claim mustn't move.
    await w.poll()
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_staged")) == 1

    await delete_spool(w.spoolman.client, a)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert charges(records) == []
    tool = w.bridge.printers[0].tools[0]
    assert tool.active is None and tool.deleted_spool == a and tool.held_mm == 20
    loc = w.tool_location
    flag = await w.flag("B")
    assert flag is not None and "or when this spool is claimed" in flag

    # The user ticks Claim held usage on B: B is active, and takes the 20 mm held and
    # the 10 mm since, with no refund to the deleted spool.
    w.printer.extrude(0, 10)
    await w.claim("B")
    records = await w.poll()
    claimed = events(records, "claimed")
    assert [e["message"] for e in claimed] == [
        f"spool {b} claimed in place of deleted spool {a}: "
        "moved nothing (usage since the last reboot)"
    ]
    assert [e["message"] for e in events(records, "held_charged")] == [
        f"spool {b} is active; charged it 20 mm held after spool {a} was deleted"
    ]
    assert [(r["spool_id"], r["use_length"], r["reason"]) for r in charges(records)] == [
        (b, 20.0, f"held after spool {a} was deleted"),
        (b, 10.0, "extruded"),
    ]
    assert [r for r in records if "failed" in r] == []
    tool = w.bridge.printers[0].tools[0]
    assert tool.active == b and tool.deleted_spool is None and tool.held_mm == 0
    assert await w.spoolman.extra(b, CLAIM_FIELD) is None
    assert count(caplog, logging.INFO, f"cleared the claim on spool {b}") == 1

    # The hold and B's flag end once, and once B is written, later use is B's alone.
    await w.run_until(lambda: w.printer.job is None)
    w.printer.extrude(0, 5)
    records = await w.poll()
    assert [(r["spool_id"], r["use_length"]) for r in charges(records)] == [(b, 5.0)]
    assert await w.flag("B") is None
    found = f"[{loc}] spool {a} was deleted in Spoolman; holding its usage"
    ended = f"[{loc}] no longer holding usage for a deleted spool"
    assert count(caplog, logging.WARNING, found) == 1
    assert count(caplog, logging.WARNING, ended) == 1
    assert count(caplog, logging.INFO, f"cleared flag on spool {b}") == 1
    assert count(caplog, logging.INFO, f"cleared the claim on spool {b}") == 1
    assert w.charged(w.since(0), "B") == 35


async def test_m3_deleted_spool_replaced_with_nothing_held(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Active spool deleted, then B moved in before any use: B is active with nothing to charge."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    await delete_spool(w.spoolman.client, a)
    await w.poll()
    loc = w.tool_location
    found = f"[{loc}] spool {a} was deleted in Spoolman; holding its usage"
    assert count(caplog, logging.WARNING, found) == 1
    assert w.bridge.printers[0].tools[0].held_mm == 0

    await w.move("B", loc)
    records = await w.poll()
    assert [e["message"] for e in events(records)] == [f"spool {b} is active"]
    assert charges(records) == []
    tool = w.bridge.printers[0].tools[0]
    assert tool.active == b and tool.deleted_spool is None
    ended = f"[{loc}] no longer holding usage for a deleted spool"
    assert count(caplog, logging.WARNING, ended) == 1

    # The printer still shows A's PLA, so B is written to it; then B's use is charged.
    await w.run_until(lambda: w.printer.job is None)
    w.printer.extrude(0, 10)
    records = await w.poll()
    assert w.charged(records, "B") == 10
    assert count(caplog, logging.WARNING, found) == 1
    assert count(caplog, logging.WARNING, ended) == 1


@pytest.mark.parametrize("end", ["edit", "unload"])
async def test_m5_negative_weight_ends(world: World, caplog: pytest.LogCaptureFixture, end: str):
    """A negative weight ends with one line when edited, and silently when the spool unloads."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await baseline(w)
    await set_used(w, "A", 999.0)
    w.printer.extrude(0, 1000)
    await w.poll()
    await w.poll()  # Spoolman's list is read before the poll's charge is sent.
    negative = f"spool {a} shows -"
    recovered = f"spool {a} no longer shows a negative weight remaining in Spoolman"
    assert count(caplog, logging.INFO, negative) == 1

    if end == "edit":
        await set_used(w, "A", 500.0)
        await w.poll()
        assert count(caplog, logging.INFO, recovered) == 1
    else:
        # S1 to B: A is unloaded and moved out, still negative.
        await w.move("B", w.tool_location)
        await w.poll()
        await w.unload()
        records = await w.poll()
        assert len(events(records, "swap_committed")) == 1
        await w.swap_cycle(source="tag", name=PETG[0], color=PETG[1])
        assert not logged(caplog, logging.INFO, recovered)
    assert w.bridge._negative == set()

    for _ in range(2):
        w.printer.extrude(0, 10)
        await w.poll()
    assert count(caplog, logging.INFO, negative) == 1
    assert count(caplog, logging.INFO, recovered) == (1 if end == "edit" else 0)
