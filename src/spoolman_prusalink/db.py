"""The bridge's SQLite database: per-tool state, flags, runouts, claims, the outbox, and
the ledger."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from .accounting import ToolState
from .conditions import Condition
from .printer_sync import WriteState, WrittenFilament
from .records import ChargeRecord, EventRecord, Outbox
from .state import PrinterState, Runout, State
from .tags import SeenTag

# Covers everything `save` writes: the tables below, and the shape of the JSON in their
# text columns (`tools.data`, `conditions.data`, `writes.written`, `outbox.record`,
# `ledger.extra`).
# It stays at 1 until the first release, with saved state changed in place and no
# migration. From then on, a change to either one bumps it.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tools (
    printer TEXT NOT NULL,
    tool INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (printer, tool)
);
CREATE TABLE conditions (
    printer TEXT NOT NULL,
    tool INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (printer, tool)
);
CREATE TABLE seen_tags (
    printer TEXT NOT NULL,
    tool INTEGER NOT NULL,
    uuid TEXT NOT NULL,
    spool_id INTEGER,
    runout INTEGER NOT NULL,
    PRIMARY KEY (printer, tool)
);
CREATE TABLE writes (
    printer TEXT NOT NULL,
    tool INTEGER NOT NULL,
    due INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    written TEXT,
    PRIMARY KEY (printer, tool)
);
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer TEXT,
    tool INTEGER,
    spool_id INTEGER NOT NULL,
    use_length REAL NOT NULL,
    record TEXT NOT NULL
);
CREATE TABLE flagged (spool_id INTEGER PRIMARY KEY);
CREATE TABLE claimed (spool_id INTEGER PRIMARY KEY);
CREATE TABLE job_deltas (spool_id INTEGER PRIMARY KEY, delta INTEGER NOT NULL);
CREATE TABLE runouts (
    spool_id INTEGER PRIMARY KEY,
    location TEXT NOT NULL,
    remaining REAL NOT NULL,
    net REAL,
    used REAL
);
CREATE TABLE ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT,
    printer TEXT,
    tool INTEGER,
    kind TEXT NOT NULL,
    spool_id INTEGER,
    use_length,
    applied TEXT,
    failed TEXT,
    extra TEXT NOT NULL
);
CREATE INDEX ledger_spool_time ON ledger (spool_id, time);
CREATE INDEX ledger_printer_time ON ledger (printer, time);
"""

# The tables `save` diffs by key: their key columns and their other columns.
_KEYED = {
    "tools": (("printer", "tool"), ("data",)),
    "conditions": (("printer", "tool"), ("data",)),
    "seen_tags": (("printer", "tool"), ("uuid", "spool_id", "runout")),
    "writes": (("printer", "tool"), ("due", "attempts", "written")),
    "flagged": (("spool_id",), ()),
    "claimed": (("spool_id",), ()),
    "job_deltas": (("spool_id",), ("delta",)),
    "runouts": (("spool_id",), ("location", "remaining", "net", "used")),
}
# Outbox columns copied out of each record; the `record` column holds the rest.
_OUTBOX_COLUMNS = ("printer", "tool", "spool_id", "use_length")

# Ledger columns besides `kind` and `extra`. A charge fills them all but `applied` or
# `failed`, and an event only the first three. `extra` holds the record's other fields.
_LEDGER_COLUMNS = ("time", "printer", "tool", "spool_id", "use_length", "applied", "failed")
# `ChargeRecord` fields that say how Spoolman answered, which a queued charge doesn't have
# yet. Its outbox row leaves them out.
_OUTCOMES = ("applied", "failed", "held")
# `ledger.kind` for a charge. An event's is its kind.
_CHARGE = "charge"

Rows = dict[str, dict[tuple, tuple]]


class DatabaseError(Exception):
    """The database can't be used: locked by another bridge, or not one this bridge reads."""


def lock_path(path: Path) -> Path:
    """The lock file beside the database at `path`."""
    return path.with_name(path.name + ".lock")


class Database:
    """The bridge's database, locked for the life of the process.

    `save` writes only the state rows that changed since the last `load` or `save`, and
    any ledger records given, in one transaction, so a poll that changes nothing writes
    nothing.
    """

    def __init__(self, path: Path, conn: sqlite3.Connection, lock_fd: int) -> None:
        self.path = path
        # The SQLite connection, or None once closed. A test can swap in one that fails.
        self.conn: sqlite3.Connection | None = conn
        self._lock_fd: int | None = lock_fd
        self._rows: Rows = {name: {} for name in _KEYED}

    @classmethod
    def open(cls, path: Path) -> Database:
        """Lock and open the database at `path`, creating it, or its schema, if missing.

        Raise DatabaseError if another process holds the lock or the file isn't a
        database of a version this bridge reads.
        """
        path = Path(path)
        lock_fd = _lock(lock_path(path))
        try:
            conn = _connect(path)
        except BaseException:
            os.close(lock_fd)
            raise
        db = cls(path, conn, lock_fd)
        try:
            db.load()
        except BaseException:
            db.close()
            raise
        return db

    def close(self) -> None:
        """Close the database and release the lock. Safe to call twice."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def load(self) -> State:
        """Read the state, and take it as what `save` compares against."""
        try:
            state, self._rows = _read(self.conn)
        except (sqlite3.Error, ValueError) as e:
            raise DatabaseError(f"{self.path}: {e}") from e
        return state

    def save(self, state: State, ledger: Sequence[ChargeRecord | EventRecord] = ()) -> bool:
        """Write the rows of `state` that changed since the last load or save, and append
        the `ledger` records.

        `state` is the one `load` returned: its outbox says which of its charges changed.

        All of it goes in one transaction, so either all of it is saved or none is.
        Return whether anything was written.
        """
        rows = _rows(state)
        gone, new = state.outbox.changes()
        changes = {
            name: (
                [k for k in self._rows[name] if k not in rows[name]],
                [(k, v) for k, v in rows[name].items() if self._rows[name].get(k) != v],
            )
            for name in _KEYED
        }
        if not gone and not new and not ledger and not any(d or u for d, u in changes.values()):
            return False
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name, (deleted, upserted) in changes.items():
                keys, values = _KEYED[name]
                where = " AND ".join(f"{k} = ?" for k in keys)
                conn.executemany(f"DELETE FROM {name} WHERE {where}", deleted)
                columns = keys + values
                marks = ", ".join("?" * len(columns))
                conn.executemany(
                    f"INSERT OR REPLACE INTO {name} ({', '.join(columns)}) VALUES ({marks})",
                    [k + v for k, v in upserted],
                )
            conn.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in gone])
            added = []
            for rec in new:
                record = rec.to_json()
                columns = tuple(record.pop(c) for c in _OUTBOX_COLUMNS)
                # A charge gets an outcome only as it leaves the outbox.
                for outcome in _OUTCOMES:
                    del record[outcome]
                cur = conn.execute(
                    "INSERT INTO outbox (printer, tool, spool_id, use_length, record) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (*columns, _canonical(record)),
                )
                added.append(cur.lastrowid)
            _insert_ledger(conn, ledger)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        self._rows = rows
        state.outbox.saved(added)
        return True


def read_state(path: Path) -> State:
    """Read the state from the database at `path` without locking it, as a reader beside
    a running bridge does. A missing database, or one with no schema, reads as an empty
    state."""
    path = Path(path)
    conn = _reader(path)
    if conn is None:
        return State()
    try:
        return _read(conn)[0]
    except (sqlite3.Error, ValueError) as e:
        raise DatabaseError(f"{path}: {e}") from e
    finally:
        conn.close()


def read_ledger(path: Path) -> list[dict]:
    """Read every ledger record from the database at `path`, oldest first, without
    locking it. Each is the dict the bridge saved. A missing database, or one with no
    schema, reads as none."""
    path = Path(path)
    conn = _reader(path)
    if conn is None:
        return []
    try:
        names = ("kind", *_LEDGER_COLUMNS, "extra")
        cursor = conn.execute(f"SELECT {', '.join(names)} FROM ledger ORDER BY id")
        return [_ledger_record(dict(zip(names, row, strict=True))) for row in cursor]
    except (sqlite3.Error, ValueError) as e:
        raise DatabaseError(f"{path}: {e}") from e
    finally:
        conn.close()


def _reader(path: Path) -> sqlite3.Connection | None:
    """A read-only connection to the database at `path`, or None if it's missing or has
    no schema."""
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise DatabaseError(f"{path}: {e}") from e
    try:
        if _check(conn, path):
            return conn
    except BaseException:
        conn.close()
        raise
    conn.close()
    return None


def _lock(path: Path) -> int:
    """Take the lock file at `path` for the life of the process, or raise.

    SQLite locks only for a transaction, so it can't stop a second bridge on the same
    database. The lock is a separate file because closing any descriptor on the database
    other than SQLite's own drops SQLite's POSIX locks.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise DatabaseError(
            f"{path} is locked: another bridge is running on the same database"
        ) from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating the schema if it has none."""
    # Autocommit: `save` opens and commits its transactions itself.
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        ready = _check(conn, path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        if not ready:
            # One transaction, so a crash can't leave tables without the version.
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{_SCHEMA}\n"
                f"INSERT INTO meta (key, value) VALUES ('schema_version', '{SCHEMA_VERSION}');\n"
                "COMMIT;"
            )
    except sqlite3.Error as e:
        conn.close()
        raise DatabaseError(f"{path}: {e}") from e
    except BaseException:
        conn.close()
        raise
    return conn


def _check(conn: sqlite3.Connection, path: Path) -> bool:
    """Return whether the database has the schema; raise if it has another one."""
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "meta" not in tables:
            if tables:
                raise DatabaseError(f"{path} isn't a bridge database")
            return False
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error as e:
        raise DatabaseError(f"{path}: {e}") from e
    version = None if row is None else row[0]
    if version != str(SCHEMA_VERSION):
        raise DatabaseError(f"{path}: unsupported schema version {version!r}")
    return True


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True)


def _ledger_row(record: ChargeRecord | EventRecord) -> tuple:
    """The `ledger` row for a record: `kind`, the `_LEDGER_COLUMNS`, and `extra`."""
    if isinstance(record, EventRecord):
        ev = record.event
        extra = {**ev.fields(), "location": record.location, "message": ev.message}
        return (ev.kind.value, record.time, record.printer, record.tool, *[None] * 4, extra)
    extra = record.to_json()
    columns = tuple(extra.pop(c) for c in _LEDGER_COLUMNS)
    if not extra["held"]:
        del extra["held"]  # Only a held charge says so.
    return (_CHARGE, *columns, extra)


def _insert_ledger(conn: sqlite3.Connection, ledger: Sequence[ChargeRecord | EventRecord]) -> None:
    rows = [(*row[:-1], _canonical(row[-1])) for row in map(_ledger_row, ledger)]
    if not rows:
        return
    names = ("kind", *_LEDGER_COLUMNS, "extra")
    conn.executemany(
        f"INSERT INTO ledger ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", rows
    )


def _ledger_record(row: dict) -> dict:
    """The record a ledger row was saved from."""
    record = {k: row[k] for k in _LEDGER_COLUMNS if row[k] is not None}
    if row["kind"] != _CHARGE:
        record["event"] = row["kind"]
    record.update(json.loads(row["extra"]))
    return record


def _rows(state: State) -> Rows:
    """The keyed tables' rows for `state`, as `save` compares them."""
    rows: Rows = {name: {} for name in _KEYED}
    for name, p in state.printers.items():
        for tool, st in p.tools.items():
            rows["tools"][(name, tool)] = (_canonical(st.to_json()),)
        for tool, found in p.conditions.items():
            rows["conditions"][(name, tool)] = (_canonical([c.to_json() for c in found]),)
        for tool, seen in p.seen_tags.items():
            # What the bridge watched isn't saved (see `SeenTag`).
            if seen.uuid is not None:
                rows["seen_tags"][(name, tool)] = (seen.uuid, seen.spool_id, int(seen.runout))
        for tool, w in p.writes.items():
            written = None if w.written is None else _canonical(w.written.to_json())
            rows["writes"][(name, tool)] = (int(w.due), w.attempts, written)
    for spool_id in state.flagged:
        rows["flagged"][(spool_id,)] = ()
    for spool_id in state.claimed:
        rows["claimed"][(spool_id,)] = ()
    for spool_id, delta in state.job_deltas.items():
        if delta:
            rows["job_deltas"][(spool_id,)] = (delta,)
    for spool_id, r in state.runouts.items():
        rows["runouts"][(spool_id,)] = (r.location, r.remaining, r.net, r.used)
    return rows


def _read(conn: sqlite3.Connection) -> tuple[State, Rows]:
    """Read the state and its keyed rows as stored."""
    rows: Rows = {}
    for name, (keys, values) in _KEYED.items():
        columns = ", ".join(keys + values)
        order = ", ".join(keys)
        rows[name] = {
            r[: len(keys)]: r[len(keys) :]
            for r in conn.execute(f"SELECT {columns} FROM {name} ORDER BY {order}")
        }
    state = State()
    for (printer, tool), (data,) in rows["tools"].items():
        try:
            tool_state = ToolState.from_json(json.loads(data))
        except KeyError as e:
            raise ValueError(f"tools row ({printer!r}, {tool}): missing key {e}") from e
        except (TypeError, AttributeError) as e:
            raise ValueError(f"tools row ({printer!r}, {tool}): a value of the wrong type") from e
        state.printers.setdefault(printer, PrinterState()).tools[tool] = tool_state
    for (printer, tool), (data,) in rows["conditions"].items():
        try:
            found = [Condition.from_json(c) for c in json.loads(data)]
        except KeyError as e:
            raise ValueError(f"conditions row ({printer!r}, {tool}): missing key {e}") from e
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"conditions row ({printer!r}, {tool}): a value of the wrong type"
            ) from e
        state.printers.setdefault(printer, PrinterState()).conditions[tool] = found
    for (printer, tool), (uuid, spool_id, runout) in rows["seen_tags"].items():
        seen = state.printers.setdefault(printer, PrinterState()).seen_tags
        seen[tool] = SeenTag(uuid, spool_id, runout=bool(runout))
    for (printer, tool), (due, attempts, written) in rows["writes"].items():
        try:
            filament = None if written is None else WrittenFilament.from_json(json.loads(written))
        except KeyError as e:
            raise ValueError(f"writes row ({printer!r}, {tool}): missing key {e}") from e
        except (TypeError, ValueError) as e:
            raise ValueError(f"writes row ({printer!r}, {tool}): a value of the wrong type") from e
        writes = state.printers.setdefault(printer, PrinterState()).writes
        writes[tool] = WriteState(bool(due), attempts, filament)
    state.printers = dict(sorted(state.printers.items()))
    state.flagged = [k for (k,) in rows["flagged"]]
    state.claimed = [k for (k,) in rows["claimed"]]
    state.job_deltas = {k: delta for (k,), (delta,) in rows["job_deltas"].items()}
    state.runouts = {k: Runout(*v) for (k,), v in rows["runouts"].items()}
    outbox = []
    for id_, *columns, extra in conn.execute(
        "SELECT id, printer, tool, spool_id, use_length, record FROM outbox ORDER BY id"
    ):
        record = {**json.loads(extra), **dict(zip(_OUTBOX_COLUMNS, columns, strict=True))}
        try:
            outbox.append((id_, ChargeRecord.from_json(record)))
        except TypeError as e:
            raise ValueError(f"outbox row {id_}: {e}") from e
    state.outbox = Outbox(outbox)
    return state, rows
