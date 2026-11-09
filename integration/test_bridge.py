"""The bridge itself: docs/scenarios.md rows B1, B2, B4 and B6.

B3, a kill between a charge being accepted and the outbox being saved, is a known limit
with no way in from outside the bridge, so it has no test here.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest

from integration.conftest import ASA, LOAD_MM, PETG, World, charges, events, print_spec
from simulator.model import State
from spoolman_prusalink.db import read_state


def dry(records: list[dict]) -> list[dict]:
    """Charges a dry run recorded: logged only, never sent, so never `applied`."""
    return [r for r in records if "use_length" in r and r.get("dry_run")]


async def test_b1_first_start(world: World):
    """First start: the counter is the baseline, and use before it isn't charged."""
    w = world
    assert w.counter == 1000
    records = await w.poll()
    assert [e["message"] for e in events(records)] == [
        f"baseline extruded=1000 spool={w.spools['A']}"
    ]
    assert charges(records) == []

    w.printer.extrude(0, 10)
    records = await w.poll()
    assert [(r["use_length"], r["reason"]) for r in charges(records)] == [(10, "extruded")]
    assert w.charged(w.since(0), "A") == 10


async def test_b2_restart_mid_print(world: World):
    """Restart mid-print: one catch-up charge, with no double charge and no loss."""
    w = world
    await w.poll()
    start = w.counter
    await w.start_print(print_spec())
    w.advance(0.5)
    await w.poll()
    before = w.since(0)

    # The bridge stops for a second of printing, then starts again on the same state.
    w.advance(1.0)
    await w.restart()
    assert w.printer.state is State.PRINTING
    records = await w.poll()
    assert events(records) == []
    assert [(r["use_length"], r["reason"]) for r in charges(records)] == [
        (w.counter - before[-1]["extruded"], "extruded")
    ]

    await w.run_until(
        lambda: w.printer.state is State.FINISHED and w.printer.job is None, poll_every=1.0
    )
    await w.poll()
    records = w.since(0)
    assert w.charged(records, "A") == w.counter - start
    assert all(r["extruded_mm"] > 0 for r in charges(records))
    grams = round(sum(r["grams"] for r in charges(records)), 2)
    assert round(await w.used("A"), 2) == grams


async def test_b4_database_lost(world: World):
    """The database lost: a new baseline, use since the last poll lost, the swap forgotten."""
    w = world
    a = w.spools["A"]
    await w.poll()
    w.printer.extrude(0, 20)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    assert w.charged(records, "A") == 20
    flag = await w.flag("A")
    assert flag is not None and "unloaded" in flag

    # The load after the unload isn't polled before the database goes.
    await w.swap_cycle(source="tag", name=PETG[0], color=PETG[1], uuid=str(uuid.uuid4()))
    await w.bridge.aclose()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{w.cfg.db_file}{suffix}").unlink(missing_ok=True)
    await w.restart()
    records = await w.poll()
    assert [e["message"] for e in events(records)] == [f"baseline extruded={w.counter} spool={a}"]
    assert charges(records) == []

    # Spoolman catches up, but the open swap is gone: B is only staged, and the load stays
    # lost rather than moving to B.
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert [e["event"] for e in events(records)] == ["swap_staged"]
    assert charges(records) == []
    assert w.charged(w.since(0), "A") == 20
    assert await w.used("B") == 0
    # With the swap forgotten, B's tag on the printer is drift on A, the active spool: its
    # UUID isn't A's. The tag's name and color aren't compared, since it isn't written.
    drift = await w.flag("A")
    assert drift is not None and "printer filament differs" in drift


async def test_b6_dry_run_then_live(world: World, caplog: pytest.LogCaptureFixture):
    """Dry run: charges only logged and recorded, then a live run catches up from its state."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, c = w.spools["A"], w.spools["C"]
    await w.poll()
    w.printer.extrude(0, 10)
    records = await w.poll()
    assert w.charged(records, "A") == 10
    state = read_state(w.cfg.db_file)
    used = await w.used("A")
    puts = len(w.requests("PUT"))

    # Dry run, with two swaps at the printer (A -> B -> C) and Spoolman updated to C.
    await w.restart(dry_run=True)
    mark = w.mark()
    w.printer.extrude(0, 30)
    await w.unload()
    # A poll while A's unconfirmed-swap flag is due: a dry run doesn't write it either.
    await w.poll()
    assert await w.flag("A") is None
    await w.swap_cycle(source="tag", name=PETG[0], color=PETG[1])
    await w.unload()
    # Loaded from the menu, so a write of C is due once the bridge sees the swap.
    await w.swap_cycle(source="preset", name=ASA[0])
    await w.move("A", w.shelf)
    await w.move("C", w.tool_location)
    await w.poll()
    w.printer.extrude(0, 5)
    await w.poll()
    records = w.since(mark)
    assert records != [] and all(r["dry_run"] is True for r in records)
    assert [e["event"] for e in events(records)] == [
        "unload",
        "swap_unconfirmed",
        "unload",
        "swap_committed",
    ]
    assert charges(records) == []
    # Two polls saw the swaps: the B load is charged to A while its swap is unconfirmed.
    assert [(r["spool_id"], r["use_length"]) for r in dry(records)] == [
        (a, 30),
        (a, LOAD_MM),
        (c, LOAD_MM),
        (c, 5),
    ]
    assert any("WOULD CHARGE" in r.getMessage() for r in caplog.records)
    assert any("WOULD RUN" in r.getMessage() for r in caplog.records)
    # Nothing changed in Spoolman, on the printer or in the database.
    assert await w.used("A") == used
    assert await w.used("C") == 0
    assert await w.flag("A") is None
    assert await w.spoolman.extra(c, "instance_uuid") is None
    assert len(w.requests("PUT")) == puts
    assert read_state(w.cfg.db_file) == state

    # Live again: everything since the last live state, as after an outage, in one poll.
    await w.restart(dry_run=False)
    mark = w.mark()
    records = await w.poll()
    assert all(r["dry_run"] is False for r in records)
    unloads = events(records, "unload")
    assert len(unloads) == 1 and unloads[0]["message"].startswith("2 unload(s)")
    assert [e["event"] for e in events(records)] == ["unload", "swap_committed"]
    assert [(r["spool_id"], r["use_length"]) for r in charges(records)] == [
        (a, 30 + LOAD_MM),
        (c, LOAD_MM + 5),
    ]
    assert await w.used("A") > used
    assert await w.used("C") > 0
