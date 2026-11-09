# SQLite storage

This is the plan for replacing `state.json` and `ledger.jsonl` with one
SQLite database. It lands as a stack of PRs. The file formats go away with
no migration and no compatibility: the one deployment is wiped and starts
from a baseline.

## Goal and non-goals

The goal is one file, `/data/bridge.db`, that holds the bridge's state, the
charge outbox, and the ledger, written in one transaction per change so a
crash can never leave them disagreeing.

Not in scope:

- Importing an existing `state.json` or `ledger.jsonl`.
- Pruning the ledger. It keeps every record; see [Growth](#growth).
- Tooling to export the ledger or reset the state. They're easy to add if
  they're needed.
- An ORM or any new dependency.

## Why

- **Atomic writes.** `Bridge.flush()` pops a charge, saves `state.json`, then
  appends the charge to `ledger.jsonl`. A crash between the two leaves a
  charge that Spoolman accepted with no `applied` record. `Printer.process()`
  appends events to the ledger and saves the charges to the outbox in
  separate writes. In SQLite each is one transaction.
- **Queries.** Usage per spool, per job, or for a scale check is a `SELECT`,
  not a scan of JSONL.
- **One bridge per file.** The bridge holds a lock beside the database, so a
  second bridge on the same file fails at startup instead of double-charging.

## Design

### Library

The standard library's `sqlite3`, with no ORM. The schema is a handful of
small tables and the bridge already holds its state in memory as
dataclasses, so an ORM's sessions and identity map add a dependency and a
layer without removing any code. Writes are small local transactions, so
calling `sqlite3` from the event loop blocks for milliseconds.

### Schema

Columns hold what's queried or keyed on. A JSON column holds the rest, so
fields that change often, like `ToolState`'s, don't need a schema change.
SQLite's JSON functions (`json_extract`, `->>`) query those columns when
needed.

| Table | Key | Columns |
|---|---|---|
| `meta` | `key` | `value`; holds the schema version |
| `tools` | `printer`, `tool` | `data` (JSON from `ToolState.to_json()`) |
| `printer_flags` | `printer`, `spool_id` | `texts` (JSON list) |
| `seen_uuids` | `printer`, `tool` | `uuid`, `spool_id`, `runout` |
| `outbox` | `id` (autoincrement, gives the order) | `printer`, `tool`, `spool_id`, `use_length`, `record` (JSON) |
| `flagged` | `spool_id` | |
| `claimed` | `spool_id` | |
| `runouts` | `spool_id` | `text`, `net`, `used`, `printer` |
| `ledger` | `id` | `time`, `printer`, `tool`, `kind`, `spool_id`, `use_length`, `applied`, `failed`, `dry_run`, `extra` (JSON) |

`ledger.kind` is `charge` or the event name. `extra` holds the remaining
fields, such as `message`, `reason`, `boot_id`, and `extruded`. Indexes:
`ledger(spool_id, time)` and `ledger(printer, time)`.

A schema version in `meta` lets later PRs change the schema with a migration
step. An unknown version refuses to start, as `state.json` does today.

### Writes

The in-memory `State` stays, and the poll loop keeps its shape. `save_state`
becomes a storage method that writes only the rows that changed since the
last save, so idle polls still write nothing. The ledger append moves into
the same transaction:

- `process()` writes its events, its new outbox rows, and the tool state in
  one transaction.
- `flush()` deletes the outbox row, inserts the ledger record, and saves the
  state in one transaction per charge.
- Dry run writes ledger rows only, as today.

Settings: WAL journal mode with `synchronous=FULL`. Writes are rare, so the
cost of a sync per commit doesn't matter, and these are charge records. WAL
also lets the `sqlite3` CLI read the database while the bridge runs.

### Lock

At startup the bridge takes `fcntl.flock(LOCK_EX | LOCK_NB)` on
`bridge.db.lock`, next to the database, and holds it until exit. If the lock
is taken, it refuses to start and names the file. The lock is a separate
file, not the database: SQLite's own locks are POSIX locks, which the
process loses when it closes any descriptor on the database file, and
`locking_mode=EXCLUSIVE` would also lock out readers such as the `sqlite3`
CLI.

### Config

`state_file` and `ledger_file` are replaced by `db_file`, default
`/data/bridge.db`. The old keys become unknown keys.

### Tests

Tests that read `state.json` or `ledger.jsonl` directly, in `tests/` and
`integration/`, move to shared helpers that read the database. Scenario B4
("state lost") becomes deleting the state tables' rows, or the file. The
version 2 migration and its scenario, X13, go away.

## Growth

The bridge doesn't add a ledger row per poll while idle. While a tool is
printing, it adds about one charge row per poll (`poll_printing`, 30 s by
default), plus a few event rows per print. So the ledger grows with printing
hours per tool, not with printers or tools that sit idle.

The 185 KB a day seen on the test deployment is about 600 JSONL records, or
about 5 hours of printing. A JSONL charge record is about 300 bytes. In
SQLite, with the keys as columns, expect 120 to 150 bytes per row, plus
indexes. Estimates per tool that prints:

| Printing per day | Rows per year | Size per year |
|---|---|---|
| 5 hours | 220,000 | 30 to 40 MB |
| 24 hours | 1,050,000 | 150 to 200 MB |

A farm of 20 printers printing around the clock is 3 to 4 GB a year. SQLite
handles that size, but it's more than many small hosts want. The ledger
keeps one row per charge for now, at full fidelity. If size becomes a
problem, there are two fixes: merge consecutive charges to the same spool
into one row, or prune old rows on a retention setting.

## Stack

1. This plan.
2. Storage module, schema, lock, and the state and outbox moved to it.
   `db_file` replaces `state_file`; the ledger stays JSONL in this PR, with
   `ledger_file`.
3. The ledger moved into the database, with the single-transaction writes
   in `process()` and `flush()`. `ledger_file` goes away.

Each PR updates the docs it affects: the README, `config.example.toml`, the
simulator README, `docs/scenarios.md`, and `integration/README.md`.
