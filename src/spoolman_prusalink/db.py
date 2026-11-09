"""The bridge's SQLite database: per-tool state, flags, runouts, claims, and the outbox."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from pathlib import Path

from .accounting import ToolState
from .state import PrinterState, Runout, State

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tools (
    printer TEXT NOT NULL,
    tool INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (printer, tool)
);
CREATE TABLE printer_flags (
    printer TEXT NOT NULL,
    spool_id INTEGER NOT NULL,
    texts TEXT NOT NULL,
    PRIMARY KEY (printer, spool_id)
);
CREATE TABLE seen_uuids (
    printer TEXT NOT NULL,
    tool INTEGER NOT NULL,
    uuid TEXT NOT NULL,
    spool_id INTEGER,
    runout INTEGER NOT NULL,
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
CREATE TABLE runouts (
    spool_id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    net REAL,
    used REAL,
    printer TEXT
);
"""

# The tables `save` diffs by key: their key columns and their other columns.
_KEYED = {
    "tools": (("printer", "tool"), ("data",)),
    "printer_flags": (("printer", "spool_id"), ("texts",)),
    "seen_uuids": (("printer", "tool"), ("uuid", "spool_id", "runout")),
    "flagged": (("spool_id",), ()),
    "claimed": (("spool_id",), ()),
    "runouts": (("spool_id",), ("text", "net", "used", "printer")),
}
# Outbox columns copied out of each record; the `record` column holds the rest.
_OUTBOX_COLUMNS = ("printer", "tool", "spool_id", "use_length")

Rows = dict[str, dict[tuple, tuple]]


class DatabaseError(Exception):
    """The database can't be used: locked by another bridge, or not one this bridge reads."""


def lock_path(path: Path) -> Path:
    """The lock file beside the database at `path`."""
    return path.with_name(path.name + ".lock")


class Database:
    """The bridge's database, locked for the life of the process.

    `save` writes only the rows that changed since the last `load` or `save`, in one
    transaction, so a poll that changes nothing writes nothing.
    """

    def __init__(self, path: Path, conn: sqlite3.Connection | None, lock_fd: int) -> None:
        self.path = path
        self._conn = conn
        self._lock_fd: int | None = lock_fd
        self._rows: Rows = {name: {} for name in _KEYED}
        self._outbox: list[tuple[int, str]] = []  # (id, canonical record), oldest first.

    @classmethod
    def open(cls, path: Path, *, create: bool = True) -> Database:
        """Lock and open the database at `path`.

        With `create` false, as in dry run, a missing database isn't created, and neither
        is the schema of an empty one: it loads as an empty state, can't be saved, and
        the file is left as it was. Raise DatabaseError if another process holds the lock
        or the file isn't a database of a version this bridge reads.
        """
        path = Path(path)
        lock_fd = _lock(lock_path(path))
        try:
            conn = _connect(path, create) if create or path.exists() else None
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
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def load(self) -> State:
        """Read the state, and take it as what `save` compares against."""
        if self._conn is None:
            self._rows = {name: {} for name in _KEYED}
            self._outbox = []
            return State()
        try:
            state, self._rows, self._outbox = _read(self._conn)
        except (sqlite3.Error, ValueError) as e:
            raise DatabaseError(f"{self.path}: {e}") from e
        return state

    def save(self, state: State) -> bool:
        """Write the rows of `state` that changed since the last load or save.

        All of them go in one transaction. Return whether anything was written.
        """
        if self._conn is None:
            raise DatabaseError(f"{self.path} was opened without create, so it can't be saved")
        rows = _rows(state)
        texts = [_canonical(r) for r in state.outbox]
        old = [text for _, text in self._outbox]
        # Charges leave the front of the outbox and join at the back. Find how many left:
        # the most that keeps the rest a prefix of the current outbox. If none fits, all
        # of them left and every current charge is new.
        gone = next(k for k in range(len(old) + 1) if texts[: len(old) - k] == old[k:])
        new = texts[len(old) - gone :]
        changes = {
            name: (
                [k for k in self._rows[name] if k not in rows[name]],
                [(k, v) for k, v in rows[name].items() if self._rows[name].get(k) != v],
            )
            for name in _KEYED
        }
        if not gone and not new and not any(d or u for d, u in changes.values()):
            return False
        conn = self._conn
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
            conn.executemany(
                "DELETE FROM outbox WHERE id = ?", [(i,) for i, _ in self._outbox[:gone]]
            )
            added = []
            for text in new:
                record = json.loads(text)
                columns = tuple(record.pop(c, None) for c in _OUTBOX_COLUMNS)
                cur = conn.execute(
                    "INSERT INTO outbox (printer, tool, spool_id, use_length, record) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (*columns, json.dumps(record, sort_keys=True)),
                )
                added.append((cur.lastrowid, text))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        self._rows = rows
        self._outbox = self._outbox[gone:] + added
        return True


def read_state(path: Path) -> State:
    """Read the state from the database at `path` without locking it, as a reader beside
    a running bridge does. A missing database, or one with no schema, reads as an empty
    state."""
    path = Path(path)
    if not path.exists():
        return State()
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if not _check(conn, path):
            return State()
        return _read(conn)[0]
    except (sqlite3.Error, ValueError) as e:
        raise DatabaseError(f"{path}: {e}") from e
    finally:
        conn.close()


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


def _connect(path: Path, create: bool) -> sqlite3.Connection | None:
    """Open the database, creating the schema if it has none and `create` is true.

    Without `create`, a database with no schema is closed untouched and None returned:
    the journal mode pragma alone would write to it.
    """
    # Autocommit: `save` opens and commits its transactions itself.
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        ready = _check(conn, path)
        if not ready and not create:
            conn.close()
            return None
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


def _rows(state: State) -> Rows:
    """The keyed tables' rows for `state`, as `save` compares them."""
    rows: Rows = {name: {} for name in _KEYED}
    for name, p in state.printers.items():
        for tool, st in p.tools.items():
            rows["tools"][(name, tool)] = (_canonical(st.to_json()),)
        for spool_id, texts in p.flags.items():
            rows["printer_flags"][(name, spool_id)] = (json.dumps(list(texts)),)
        for tool, (uuid, spool_id, runout) in p.seen_uuids.items():
            rows["seen_uuids"][(name, tool)] = (uuid, spool_id, int(runout))
    for spool_id in state.flagged:
        rows["flagged"][(spool_id,)] = ()
    for spool_id in state.claimed:
        rows["claimed"][(spool_id,)] = ()
    for spool_id, r in state.runouts.items():
        rows["runouts"][(spool_id,)] = (r.text, r.net, r.used, r.printer)
    return rows


def _read(conn: sqlite3.Connection) -> tuple[State, Rows, list[tuple[int, str]]]:
    """Read the state, its keyed rows as stored, and the outbox as (id, record) pairs."""
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
        state.printers.setdefault(printer, PrinterState()).tools[tool] = ToolState.from_json(
            json.loads(data)
        )
    for (printer, spool_id), (texts,) in rows["printer_flags"].items():
        state.printers.setdefault(printer, PrinterState()).flags[spool_id] = json.loads(texts)
    for (printer, tool), (uuid, spool_id, runout) in rows["seen_uuids"].items():
        seen = state.printers.setdefault(printer, PrinterState()).seen_uuids
        seen[tool] = (uuid, spool_id, bool(runout))
    state.printers = dict(sorted(state.printers.items()))
    state.flagged = [k for (k,) in rows["flagged"]]
    state.claimed = [k for (k,) in rows["claimed"]]
    state.runouts = {k: Runout(*v) for (k,), v in rows["runouts"].items()}
    outbox = []
    for id_, *columns, extra in conn.execute(
        "SELECT id, printer, tool, spool_id, use_length, record FROM outbox ORDER BY id"
    ):
        record = json.loads(extra)
        record.update(
            (k, v) for k, v in zip(_OUTBOX_COLUMNS, columns, strict=True) if v is not None
        )
        state.outbox.append(record)
        outbox.append((id_, _canonical(record)))
    return state, rows, outbox
