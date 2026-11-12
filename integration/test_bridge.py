"""The bridge itself: docs/scenarios.md rows B1 to B7.

B3, a kill between a charge being accepted and the outbox being saved, is a known limit
with no way in from outside the bridge, so it has no test here.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from integration.conftest import LOAD_MM, PETG, PLA, World, charges, count, events, print_spec
from simulator.model import State
from spoolman_prusalink.app import Bridge
from spoolman_prusalink.db import DatabaseError


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
    lost = w.since(0)  # The ledger goes with the database.
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
    assert w.charged(lost + w.since(0), "A") == 20
    assert await w.used("B") == 0
    # With the swap forgotten, B's tag on the printer is drift on A, the active spool: its
    # UUID isn't A's. The tag's name and color aren't compared, since it isn't written.
    drift = await w.flag("A")
    assert drift is not None and "printer filament differs" in drift


FRESH = pytest.mark.parametrize("world_fields", ["fresh"], indirect=True)


@FRESH
async def test_b5_fields_registered_on_a_fresh_spoolman(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Fresh Spoolman: the bridge registers its flag, claim and UUID fields as it needs them."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    keys = w.fields
    b = w.spools["B"]
    assert not set(keys.values()) & set(await w.spoolman.fields_by_key())

    # The first poll sets up the flag and claim fields, which it reads and clears every poll.
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    fields = await w.spoolman.fields_by_key()
    assert fields[keys["flag"]] == "text"
    assert fields[keys["claim"]] == "boolean"
    assert keys["uuid"] not in fields

    # An unconfirmed swap's flag goes in the new field.
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    assert w.charged(records, "A") == 30
    flag = await w.flag("A")
    assert flag is not None and "unloaded with no Spoolman change" in flag

    # B confirmed and loaded from the menu: the write mints B a UUID, registering the field.
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    await w.poll()
    await w.swap_cycle(source="preset", name=PETG[0])
    records = await w.poll()
    assert w.charged(records, "B") == LOAD_MM
    w.advance(1.5)
    fields = await w.spoolman.fields_by_key()
    assert fields[keys["uuid"]] == "text"
    minted = await w.spoolman.extra(b, keys["uuid"])
    assert minted is not None
    assert w.material() == (PETG[0], PETG[1], minted)
    assert await w.flag("A") is None
    assert not logged_trouble(caplog)


def logged_trouble(caplog: pytest.LogCaptureFixture) -> bool:
    return any("spool field" in r.getMessage() for r in caplog.records)


@FRESH
async def test_b5_field_with_the_wrong_type(world: World, caplog: pytest.LogCaptureFixture):
    """A field key taken by another type: logged once, no flag written, charging unaffected."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    key = w.fields["flag"]
    await w.spoolman.create_field(key, "Not the bridge's", "integer")

    # Record every request that would write the key, so the test sees the bridge never
    # tried, not only that Spoolman refused.
    writes: list[str] = []

    async def note_writes(request):
        if request.method == "PATCH" and key.encode() in request.content:
            writes.append(str(request.url))
        return None

    w.spoolman_faults.hook = note_writes
    await w.poll()
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    # The same spool reloaded from the menu, still unconfirmed, written back (W11), used.
    await w.swap_cycle(poll_every=0.3, source="preset", name=PLA[0])
    await w.run_until(lambda: w.printer.job is None, poll_every=0.3)
    for _ in range(3):
        w.printer.extrude(0, 10)
        await w.poll()
    assert w.charged(w.since(0), "A") == w.counter - 1000 == 30 + LOAD_MM + 30
    assert await w.used("A") > 0

    # The bridge would flag A, but leaves the field alone, and says so once.
    assert "unloaded with no Spoolman change" in w.bridge._flags.get(a, "")
    assert key not in ((await w.spoolman.get(a)).get("extra") or {})
    assert (await w.spoolman.fields_by_key())[key] == "integer"
    trouble = f"can't set up the {key!r} spool field: spool extra field {key!r} exists"
    assert count(caplog, logging.WARNING, trouble) == 1
    assert not any(r.levelno > logging.WARNING for r in caplog.records)
    assert writes == []
    assert not any("can't update the flag" in r.getMessage() for r in caplog.records)


async def test_b6_second_bridge_on_one_database_is_refused(world: World):
    """A second bridge on a running bridge's database refuses to start; the first carries on."""
    w = world
    await w.poll()
    with pytest.raises(DatabaseError, match="another bridge is running on the same database"):
        Bridge.from_config(w.cfg)

    w.printer.extrude(0, 20)
    records = await w.poll()
    assert w.charged(records, "A") == 20

    # Once the first bridge stops, a bridge starts on its database and carries on.
    await w.restart()
    w.printer.extrude(0, 10)
    records = await w.poll()
    assert events(records) == []
    assert w.charged(records, "A") == 10


def foreign(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)")
    conn.execute("INSERT INTO notes (text) VALUES ('keep me')")
    conn.commit()
    conn.close()


def corrupt(path: Path) -> None:
    path.write_bytes(b"not a database\n" * 512)


def newer(path: Path) -> None:
    """Renumber a bridge database's schema to one this bridge doesn't know."""
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "make, error",
    [
        (foreign, "isn't a bridge database"),
        (corrupt, "file is not a database"),
        (newer, "unsupported schema version '99'"),
    ],
    ids=["foreign", "corrupt", "newer"],
)
async def test_b7_database_that_is_not_the_bridges_is_refused(
    world: World, tmp_path: Path, make, error: str
):
    """A database that's another program's, corrupt or a newer schema: refused, left as it was."""
    w = world
    path = tmp_path / "other" / "bridge.db"
    path.parent.mkdir()
    if make is newer:
        # A real bridge database, made by a bridge that has polled once.
        await w.restart(db_file=path)
        await w.poll()
        await w.bridge.aclose()
    make(path)
    files = contents(path.parent)

    with pytest.raises(DatabaseError, match=error):
        Bridge.from_config(replace(w.cfg, db_file=path))
    assert contents(path.parent) == files


def contents(folder: Path) -> dict[str, bytes]:
    """Every file in `folder` but the lock file, which a refused bridge may create."""
    return {p.name: p.read_bytes() for p in folder.iterdir() if not p.name.endswith(".lock")}
