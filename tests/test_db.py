import errno
import fcntl
import json
import os
import re
import sqlite3

import pytest

from spoolman_prusalink.accounting import Provisional, SwapFlag, ToolState
from spoolman_prusalink.db import (
    SCHEMA_VERSION,
    Database,
    DatabaseError,
    read_ledger,
    read_state,
)
from spoolman_prusalink.events import StagedAtPrint, StagedAtReboot
from spoolman_prusalink.printer_sync import WrittenFilament
from spoolman_prusalink.state import PrinterState, Runout, State
from spoolman_prusalink.tags import SeenTag

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
        notice=StagedAtPrint(8, 7),
        needs_sync=True,
        sync_attempts=1,
        written=WrittenFilament("PLA", 215, 60, color="#8c9099"),
        held_mm=30,
        deleted_spool=4,
        staged_notice=StagedAtReboot(8, 7),
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
                seen_tags={0: SeenTag(TAG, 7, runout=True), 1: SeenTag(TAG.upper())},
            ),
            "mk4s": PrinterState(tools={0: ToolState(boot_id=1, charged=5)}),
        },
        outbox=[
            {"printer": "core", "tool": 0, "spool_id": 7, "use_length": 50.0, "grams": 0.15},
            {"printer": "mk4s", "tool": 0, "spool_id": 9, "use_length": -2.5, "reason": "x"},
        ],
        flagged=[7, 8],
        runouts={
            7: Runout("ran out", 1000.0, 990.5),
            8: Runout("ran out early", None, None),
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
    with pytest.raises(DatabaseError, match="unsupported schema version '99'"):
        read_ledger(path)
    Database.open(path.with_name("other.db")).close()  # The failed open let go of the lock.


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
    assert read_state(path) == State()  # Reading doesn't need the lock.
    assert read_ledger(path) == []
    db.close()
    db.close()
    Database.open(path).close()


def test_a_missing_database_reads_as_empty(path):
    assert read_state(path) == State()
    assert read_ledger(path) == []
    assert not path.exists()


# Records as the bridge writes them, and the odd ones a record could hold.
LEDGER = [
    {
        "time": "2026-11-09T10:00:00+00:00",
        "printer": "core",
        "tool": 0,
        "location": "Core One L+",
        "event": "baseline",
        "message": "baseline extruded=1000 spool=7",
    },
    {
        "time": "2026-11-09T10:00:30+00:00",
        "printer": "core",
        "tool": 0,
        "location": "Core One L+",
        "spool_id": 7,
        "extruded_mm": 500,
        "use_length": 500.0,
        "grams": 1.49,
        "reason": "extruded",
        "boot_id": 3,
        "extruded": 1500,
        "applied": "2026-11-09T10:00:31+00:00",
    },
    {
        "time": "2026-11-09T10:01:00+00:00",
        "printer": "core",
        "tool": 1,
        "spool_id": 9,
        "use_length": -2.5,
        "grams": None,
        "failed": "404",
        "held": True,
    },
    {"printer": "mk4s", "tool": 0, "spool_id": 8, "use_length": 12},
    {"time": None, "tool": 1.0, "spool_id": True, "use_length": "5"},
    {"event": "charge", "kind": "x", "extra": {"a": [1]}, "id": 4},
    {"event": None},
    {},
]


def test_ledger_round_trip(path):
    db = Database.open(path)
    assert db.save(State(), LEDGER[:2])  # Written though the state didn't change.
    assert db.save(State(), LEDGER[2:])
    assert not db.save(State(), [])
    db.close()
    records = read_ledger(path)
    assert records == LEDGER
    # Equal isn't enough: 1 == 1.0 == True.
    assert [{k: type(v) for k, v in r.items()} for r in records] == [
        {k: type(v) for k, v in r.items()} for r in LEDGER
    ]
    assert read_state(path) == State()


def test_ledger_columns(path):
    db = Database.open(path)
    db.save(State(), LEDGER[:3])
    db.close()
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT kind, printer, tool, spool_id, use_length, applied, failed FROM ledger"
    ).fetchall()
    assert rows == [
        ("baseline", "core", 0, None, None, None, None),
        ("charge", "core", 0, 7, 500.0, "2026-11-09T10:00:31+00:00", None),
        ("charge", "core", 1, 9, -2.5, None, "404"),
    ]
    extra = conn.execute("SELECT extra ->> 'grams', extra ->> 'held' FROM ledger").fetchall()
    assert extra == [(None, None), (1.49, None), (None, 1)]
    # What Spoolman accepted, per spool.
    applied = conn.execute(
        "SELECT spool_id, sum(use_length) FROM ledger WHERE applied IS NOT NULL GROUP BY spool_id"
    ).fetchall()
    assert applied == [(7, 500.0)]
    conn.close()


def test_ledger_indexes(path):
    Database.open(path).close()
    conn = sqlite3.connect(path)
    indexes = {
        name: [r[2] for r in conn.execute(f"PRAGMA index_info({name})")]
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'ledger'"
        )
    }
    assert sorted(indexes.values()) == [["printer", "time"], ["spool_id", "time"]]
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM ledger WHERE spool_id = 7").fetchall()
    assert "USING INDEX" in plan[0][3]
    conn.close()


class FailingConnection:
    """A connection whose statements run until one matches `fail`, which raises instead."""

    def __init__(self, conn: sqlite3.Connection, fail: str) -> None:
        self.conn, self.fail = conn, fail

    def execute(self, sql, *args):
        if sql.startswith(self.fail):
            raise sqlite3.OperationalError("disk I/O error")
        return self.conn.execute(sql, *args)

    def executemany(self, sql, *args):
        if sql.startswith(self.fail):
            raise sqlite3.OperationalError("disk I/O error")
        return self.conn.executemany(sql, *args)


@pytest.mark.parametrize("fail", ["INSERT INTO ledger", "COMMIT"])
def test_a_failed_save_writes_nothing(path, fail):
    db = Database.open(path)
    state = full_state()
    assert db.save(state, LEDGER[:1])
    conn = db._conn
    db._conn = FailingConnection(conn, fail)
    state.outbox.pop(0)
    state.outbox.append({"printer": "core", "tool": 0, "spool_id": 7, "use_length": 1.0})
    state.tools("core")[1] = ToolState(boot_id=4)
    with pytest.raises(sqlite3.OperationalError):
        db.save(state, LEDGER[1:2])
    assert read_state(path) == full_state()
    assert read_ledger(path) == LEDGER[:1]
    # The next save writes all of it.
    db._conn = conn
    assert db.save(state, LEDGER[1:2])
    db.close()
    assert read_state(path) == state
    assert read_ledger(path) == LEDGER[:2]


def empty_files(path):
    """A zero-byte file, and an SQLite file with no tables, as a killed first start leaves."""
    path.parent.mkdir()
    path.write_bytes(b"")
    other = path.with_name("other.db")
    conn = sqlite3.connect(other)
    conn.execute("PRAGMA user_version = 0")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("DROP TABLE t")
    conn.close()
    assert other.stat().st_size > 0
    return [path, other]


def test_readers_leave_a_database_with_no_schema_alone(path):
    for f in empty_files(path):
        before = f.read_bytes()
        assert read_state(f) == State()
        assert read_ledger(f) == []
        assert f.read_bytes() == before


def test_a_database_with_no_schema_gets_it(path):
    for f in empty_files(path):
        db = Database.open(f)
        assert db.save(full_state(), LEDGER[:1])
        db.close()
        assert read_state(f) == full_state()
        assert read_ledger(f) == LEDGER[:1]


def test_a_load_failure_lets_go_of_the_lock(path):
    db = Database.open(path)
    db.save(full_state())
    db.close()
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("UPDATE tools SET data = 'not json' WHERE tool = 1")
    with pytest.raises(DatabaseError, match=str(path)):
        Database.open(path)
    with pytest.raises(DatabaseError, match=str(path)):
        read_state(path)
    with conn:
        conn.execute("DROP TABLE runouts")
    with pytest.raises(DatabaseError, match="no such table: runouts"):
        Database.open(path)
    conn.close()
    path.unlink()
    Database.open(path).close()  # Not locked by the failed opens.


def test_a_bad_ledger_read_is_a_database_error(path):
    db = Database.open(path)
    db.save(State(), LEDGER[:1])
    db.close()
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("UPDATE ledger SET extra = 'not json'")
    with pytest.raises(DatabaseError, match=str(path)):
        read_ledger(path)
    with conn:
        conn.execute("DROP TABLE ledger")
    with pytest.raises(DatabaseError, match="no such table: ledger"):
        read_ledger(path)
    conn.close()


class ClosingConnection:
    """A connection that fails on the statement starting with `fail`, and records its close."""

    def __init__(self, conn: sqlite3.Connection, fail: str) -> None:
        self.conn, self.fail, self.closed = conn, fail, False

    def execute(self, sql, *args):
        if sql.startswith(self.fail):
            raise sqlite3.OperationalError("disk I/O error")
        return self.conn.execute(sql, *args)

    def close(self):
        self.closed = True
        self.conn.close()


def test_a_failure_setting_up_the_schema_closes_the_connection(path, monkeypatch):
    path.parent.mkdir()
    made = []
    connect = sqlite3.connect

    def failing(*args, **kwargs):
        made.append(ClosingConnection(connect(*args, **kwargs), "PRAGMA journal_mode"))
        return made[-1]

    monkeypatch.setattr(sqlite3, "connect", failing)
    with pytest.raises(DatabaseError, match=f"{path}: disk I/O error"):
        Database.open(path)
    assert [c.closed for c in made] == [True]
    monkeypatch.undo()
    Database.open(path).close()  # The lock was let go too.


def test_a_reader_that_cannot_open_the_database_is_a_database_error(path):
    path.mkdir(parents=True)  # Exists, but isn't a file SQLite can open.
    with pytest.raises(DatabaseError, match=str(path)):
        read_state(path)
    with pytest.raises(DatabaseError, match=str(path)):
        read_ledger(path)


def test_a_lock_that_fails_for_another_reason_lets_go_of_the_file(path, monkeypatch):
    opened = []
    open_file = os.open

    def recording(*args, **kwargs):
        opened.append(open_file(*args, **kwargs))
        return opened[-1]

    def broken(fd, operation):
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(os, "open", recording)
    monkeypatch.setattr(fcntl, "flock", broken)
    with pytest.raises(OSError, match="no locks available"):
        Database.open(path)
    (fd,) = opened
    with pytest.raises(OSError):  # Closed.
        os.fstat(fd)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: d.pop("extruded"), "missing key 'extruded'"),
        (lambda d: d["swap"].pop("mm"), "missing key 'mm'"),
        (lambda d: d.update(evict=None), "a value of the wrong type"),
        (lambda d: d.update(since_load=None), "a value of the wrong type"),
        # The G-code text an older bridge saved.
        (lambda d: d.update(written='M865 X R N"PLA" T215 B60 L0'), "a value of the wrong type"),
        (lambda d: d["written"].pop("nozzle"), "missing key 'nozzle'"),
    ],
)
def test_a_tools_row_missing_a_key_is_a_database_error(path, edit, message):
    db = Database.open(path)
    state = full_state()
    db.save(state)
    db.close()
    conn = sqlite3.connect(path)
    (printer, tool, data) = conn.execute(
        "SELECT printer, tool, data FROM tools WHERE json_extract(data, '$.swap') IS NOT NULL"
    ).fetchone()
    record = json.loads(data)
    edit(record)
    with conn:
        conn.execute(
            "UPDATE tools SET data = ? WHERE printer = ? AND tool = ?",
            (json.dumps(record), printer, tool),
        )
    conn.close()
    row = f"tools row ({printer!r}, {tool}): {message}"
    with pytest.raises(DatabaseError, match=re.escape(f"{path}: {row}")):
        Database.open(path)
    with pytest.raises(DatabaseError, match=re.escape(f"{path}: {row}")):
        read_state(path)
    path.unlink()
    Database.open(path).close()  # The failed open let go of the lock.
