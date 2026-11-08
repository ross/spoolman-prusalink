# Several printers in one bridge

This is the plan for one bridge instance that charges several printers'
usage to one Spoolman. It lands as a stack of PRs that stay unmerged until
the direction is settled, and each PR before the last leaves a single-printer
bridge working as it does today, so the stack can stop at any point.

## Goal and non-goals

The goal is a `[[printers]]` list in the config, with each printer's tools
mapped to Spoolman locations, and one process that polls them all, reads
Spoolman once per poll, and reconciles flags, claims, and evictions across
every printer.

Not in scope:

- Several Spoolman instances. One bridge serves one Spoolman.
- Multi-tool printers (INDX, XL). Their support stays as it is: the config,
  accounting, and state already handle several tools per printer, and the
  model check still refuses to write to them. A multi-tool printer is one
  `[[printers]]` entry with several tools. Adding the per-tool write job is
  separate work.
- Replacing `state.json` and `ledger.jsonl` with SQLite. That's a separate
  change, after this one. This plan only shapes the state and the records so
  SQLite can take them over without reshaping.
- Per-printer dry run. Dry run stays one setting for the whole bridge.

## Why one process

One container per printer works today, except where two printers touch one
spool. Flags, claims, runouts, and evictions are keyed by spool, and a spool
moves between printers in normal use: a swap on one printer evicts it, and
the next print on another printer loads it. Two containers then write the
same flag field with no coordination, and the second container's view of
the spool is what Spoolman shows. One process computes every printer's
desired flags, and writes each spool's flag once.

The other gains are smaller: one Spoolman read per poll instead of one per
printer, one state file, and one deploy.

## Design

### Config

Add a `[[printers]]` array. Each entry has a `name`, `url`, an `api_key` or
an `api_key_env` naming the environment variable that holds it, the settings
that are per printer today (`gcode_preamble`, `sync_storage`,
`sync_file`, `uuids`), and its own `tools` entries:

```toml
[[printers]]
name = "core-one-l"
url = "http://192.168.1.22"
api_key_env = "PRUSALINK_API_KEY_CORE_ONE_L"
[[printers.tools]]
index = 0
location = "Core One L+"

[[printers]]
name = "mk4s"
url = "http://192.168.1.23"
api_key = "..."
[[printers.tools]]
index = 0
location = "MK4S"
```

The name keys the printer's state and appears in ledger records, so it must
be stable. Changing it leaves the old name's state in the file: the bridge
warns once and starts the new name from a baseline (B1). The old entry is
kept, not deleted, because it can hold usage for a deleted spool or spools
waiting for eviction.

Today's `[printer]` table and top-level `[[tools]]` keep working as a
one-printer list named `default`, or the `name` set in `[printer]`, and the
environment-only mode stays single-printer. `PRUSALINK_URL` and
`PRUSALINK_API_KEY` override only that form: with a `[[printers]]` list,
they're an error, because there's no one printer for them to apply to.

The loader rejects:

- A location mapped to more than one tool across all printers. Claims and
  the log prefix rely on a location naming exactly one tool. This replaces
  the "two printers' bridges sharing a location" row in the out-of-scope
  list, which becomes a config error.
- Two printers with one URL, which would double every charge.
- Two printers with one name.

### The bridge split

`Bridge` today holds two kinds of state. Per printer: the PrusaLink client,
the tool states, pending printer writes, the job-name cache, the printer
outage, the unsupported-firmware flag, the model check, the last printer
state, per-tool notes, and the provisional-flag cache. Spoolman-wide, keyed
by spool: the outbox, flagged spools, runouts, claims, shown notices, desired
flags, the archived-spool cache, the ready extra fields, and the Spoolman
outage.

Move the per-printer group into a `PrinterBridge` class with `process()`,
`check_missing()`, `sync_printer()`, and the model check. `Bridge` keeps the
Spoolman client, the spool-keyed state, `flush()`, `evict()`,
`sync_claims()`, `sync_flags()`, and a list of printer bridges. The claims
resolver maps a location to a printer and tool pair instead of a bare tool
index. `Once`, the per-problem logger, stays on `Bridge`, with the printer
name added to keys that were per tool.

The accounting module, the PrusaLink and Spoolman clients, printer sync,
drift, and color don't change.

### The poll loop

One tick serves every printer:

1. `flush()` the shared outbox.
2. Read Spoolman once.
3. For each printer: read status and filament, `check_missing()`,
   `process()`, and, when idle, `sync_printer()`. A printer that's off or
   failing is skipped for this tick and logged through its own `Outage`, and
   the others carry on. Today a printer read failure returns from `poll()`
   before the Spoolman-wide steps, which must not happen with several
   printers.
4. `evict()`, `sync_claims()`, and `sync_flags()` once, over every printer's
   tools.

The interval is the shortest any printer asks for: `poll_printing` while any
printer is busy. Idle printers get two cheap GETs each at that rate, which
keeps the Spoolman read shared. Per-printer tasks with their own intervals
would need either a Spoolman read per printer or a cached list, and several
writers to one state file, so they're left for the SQLite change if ever.

When Spoolman is unreachable, every printer skips accounting, as today.

`once` polls every printer once.

### State and ledger

Bump `STATE_VERSION` to 3. Tool states nest under the printer name:

```json
{
  "version": 3,
  "printers": {"core-one-l": {"tools": {"0": {}}}},
  "outbox": [],
  "flagged": [],
  "noticed": [],
  "runouts": {},
  "claimed": []
}
```

The spool-keyed sections stay at the top level: they're Spoolman-wide. One
file keeps the invariant that a crash can't advance `charged` without the
charge surviving, since the outbox and every printer's tools are written
together.

A version 2 file loads only when exactly one printer is configured: its tools
move under that printer's name. With several printers configured, the bridge
refuses to start and says so, because it can't tell which printer the tools
belong to.

Outbox and ledger records gain a `printer` field with the name. The
held-for-deleted lookup in `flush()` (M3), which finds a tool by the record's
`tool`, keys on `printer` and `tool`. Both fields are what a later SQLite
schema needs for its keys, so the history imports without back-filling.

Log lines keep the `[location]` prefix. Locations are unique across printers,
so the prefix already says which printer a line is about.

### Flags, notices, claims, and evictions

- **Flags.** `process()` already joins several texts for one spool with
  ` | `, each prefixed with its location. A spool that two printers have
  something to say about gets both texts. No conflict policy is needed.
- **Notices.** A flag shown with `M117` goes to the printer whose tool
  location holds the spool. A flag on a spool in no tool's location, such as
  an unplaced claim, is shown on no printer. `noticed` stays one list, keyed
  by text.
- **Claims.** The location says which printer and tool. The rest is
  unchanged.
- **Evictions.** `evict()` moves a spool out of a tool's location based on
  the spool list read at the start of the tick. With two printers, moving the
  evicted spool straight into the other printer's location is the normal
  next step, and if the user does it between the read and the move, the
  bridge overwrites the user's move. `evict()` re-reads the spool just before
  moving it, and skips the move if it has left the location. This also
  protects the single-printer case and could land on its own.
- **A spool active in two tools.** It happens when a printer misses an
  unload, which a reboot can cause (Q2), and the spool is then loaded
  elsewhere. Nothing today notices. The bridge logs it once per pair and
  flags the spool naming both locations. The accounting is unchanged: both
  tools charge it until each sees its own unload or a claim.

## Workstreams

The stack, in order. Each PR says which scenarios it tests. Up to and
including the state PR, the bridge behaves as it does today for one printer,
and each of those PRs could merge on its own or be dropped.

1. **Plan.** This document.
2. **Config.** `PrinterConfig` and `Config.printers`, the `[[printers]]`
   loader, the one-printer compatibility form, the API key sources, and the
   cross-printer checks. Config tests. `Bridge.from_config()` still takes the
   first and only printer.
3. **Bridge split.** Extract `PrinterBridge` with no behavior change.
   `Bridge` holds exactly one. The two large test files reach into `Bridge`
   internals in about 70 places and need a mechanical update for the moved
   attributes; the test helpers that build a bridge are the place to absorb
   most of it.
4. **State and records.** Version 3, the migration, the printer field on
   records, and the held-for-deleted lookup by printer and tool. Tests for
   loading a version 2 file with one printer and refusing it with two.
5. **Several printers.** The loop over printers, the shared Spoolman read,
   independent outages, the interval rule, notice routing, the
   active-in-two-tools check, and the eviction re-read. Unit tests for the
   X scenarios below, with two fake printers against one fake Spoolman. The
   `once` command covers every printer.
6. **Docs and examples.** `README.md`, `config.example.toml`,
   `compose.example.yaml`. The scenario table lives in `scenarios.md` under
   "Several printers", the live checks are session 5 in `live-checks.md`,
   and the out-of-scope row about shared locations names the config error.

The eviction re-read and the active-in-two-tools check are useful on one
printer too. If the stack stalls, they can be pulled out and merged alone.

## Cross-printer scenarios

The scenarios, X1 to X16, are the "Several printers" section of
`scenarios.md`. Each is tested with two fake printers against one fake
Spoolman in `tests/test_printers.py`. The live checks that need a second
printer are session 5 in `live-checks.md`.
