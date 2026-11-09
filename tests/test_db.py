import sqlite3

import pytest

from spoolman_prusalink.accounting import EventKind, Notice, Provisional, SwapFlag, ToolState
from spoolman_prusalink.db import SCHEMA_VERSION, Database, DatabaseError, read_state
from spoolman_prusalink.state import PrinterState, Runout, State

TAG = "91da34d9-c1c1-470f-bac7-185a2346f86a"


def full_state() -> State:
    """A state with every field set, including the optional ones."""
    tool = ToolState(
        boot_id=3,
        charged=1200,
        unload_count=2,
        active=7,
        assigned=8,
        evict=(5, 6),
        swap=SwapFlag(8, mm=40, started_job=11),
        job_id=11,
        pending_job=12,
        pending_at=1190,
        notice=Notice(7, EventKind.SWAP_UNCONFIRMED, "unloaded"),
        needs_sync=True,
        sync_attempts=1,
        written="M865 T0",
        held_mm=30,
        deleted_spool=4,
        staged_notice=Notice(8, EventKind.SWAP_UNCONFIRMED, "staged"),
        provisional=Provisional(7, 25, job_id=11),
        since_load={7: 100, 8: 40},
        extruded=1250,
        reboot_pending=True,
    )
    return State(
        printers={
            "core": PrinterState(
                tools={0: tool, 1: ToolState(boot_id=3)},
                flags={7: ["unloaded", "drift"]},
                seen_uuids={0: (TAG, 7, True), 1: (TAG.upper(), None, False)},
            ),
            "mk4s": PrinterState(tools={0: ToolState(boot_id=1, charged=5)}),
        },
        outbox=[
            {"printer": "core", "tool": 0, "spool_id": 7, "use_length": 50.0, "grams": 0.15},
            {"printer": "mk4s", "tool": 0, "spool_id": 9, "use_length": -2.5, "reason": "x"},
        ],
        flagged=[7, 8],
        runouts={
            7: Runout("ran out", 1000.0, 990.5, "core"),
            8: Runout("ran out early", None, None, "mk4s"),
        },
        claimed=[3, 9],
    )


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data" / "bridge.db"


def test_round_trip_of_every_field(path):
    db = Database.open(path)
    assert db.load() == State()
    state = full_state()
    assert db.save(state)
    db.close()
    assert read_state(path) == state
    db = Database.open(path)
    assert db.load() == state
    db.close()


def test_no_write_when_unchanged(path):
    db = Database.open(path)
    state = full_state()
    assert db.save(state)
    assert not db.save(state)
    db.close()
    db = Database.open(path)
    loaded = db.load()
    assert not db.save(loaded)  # Loading takes the saved rows as they are.
    loaded.tools("core")[1] = ToolState(boot_id=4)
    assert db.save(loaded)
    assert not db.save(loaded)
    del loaded.runouts[8]
    loaded.printers["core"].flags.clear()
    assert db.save(loaded)
    db.close()
    assert read_state(path) == loaded


def test_outbox_order_is_kept(path):
    db = Database.open(path)
    state = State()
    charges = [{"printer": "p", "tool": 0, "spool_id": i, "use_length": 1.0} for i in range(5)]
    state.outbox.extend(charges[:3])
    assert db.save(state)
    state.outbox.pop(0)
    state.outbox.extend(charges[3:])
    assert db.save(state)
    assert read_state(path).outbox == charges[1:]
    # The same charge twice, then one of them sent.
    state.outbox.append(charges[4])
    assert db.save(state)
    state.outbox.pop(0)
    assert db.save(state)
    assert read_state(path).outbox == [*charges[2:], charges[4]]
    # Any other change rewrites the outbox in its new order.
    state.outbox.reverse()
    assert db.save(state)
    assert read_state(path).outbox == [charges[4], *reversed(charges[2:])]
    state.outbox.clear()
    assert db.save(state)
    assert read_state(path).outbox == []
    db.close()


def test_outbox_columns(path):
    db = Database.open(path)
    assert db.save(full_state())
    db.close()
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT printer, tool, spool_id, use_length, record FROM outbox").fetchall()
    assert rows == [
        ("core", 0, 7, 50.0, '{"grams": 0.15}'),
        ("mk4s", 0, 9, -2.5, '{"reason": "x"}'),
    ]
    conn.close()


def test_settings(path):
    Database.open(path).close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert version == (str(SCHEMA_VERSION),)
    conn.close()


def test_unknown_schema_version_is_refused(path):
    Database.open(path).close()
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.close()
    with pytest.raises(DatabaseError, match="unsupported schema version '99'"):
        Database.open(path)
    with pytest.raises(DatabaseError, match="unsupported schema version '99'"):
        read_state(path)
    Database.open(path.with_name("other.db")).close()  # The failed open let go of the lock.
    with pytest.raises(DatabaseError, match="unsupported schema version"):
        Database.open(path, create=False)


def test_other_files_are_refused(path):
    path.parent.mkdir()
    path.write_text("not a database, but long enough to look at its header " * 4)
    with pytest.raises(DatabaseError, match=str(path)):
        Database.open(path)
    other = path.with_name("other.db")
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE t (x)")
    conn.close()
    with pytest.raises(DatabaseError, match="isn't a bridge database"):
        Database.open(other)


def test_a_second_opener_is_refused(path):
    db = Database.open(path)
    lock = f"{path}.lock"
    with pytest.raises(DatabaseError, match=f"{lock} is locked"):
        Database.open(path)
    with pytest.raises(DatabaseError, match=f"{lock} is locked"):
        Database.open(path, create=False)  # Dry run too.
    assert read_state(path) == State()  # Reading doesn't need the lock.
    db.close()
    db.close()
    Database.open(path).close()


def test_without_create_a_missing_database_stays_missing(path):
    db = Database.open(path, create=False)
    assert db.load() == State()
    with pytest.raises(DatabaseError, match="can't be saved"):
        db.save(full_state())
    db.close()
    assert not path.exists()
    assert read_state(path) == State()


def test_without_create_an_existing_database_loads(path):
    db = Database.open(path)
    db.save(full_state())
    db.close()
    db = Database.open(path, create=False)
    assert db.load() == full_state()
    db.close()
