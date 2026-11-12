# Several printers in one bridge

This is the plan for one bridge instance that charges several printers'
usage to one Spoolman. It lands as a stack of PRs that stay unmerged until
the direction is settled, and each PR before the loop PR leaves a
single-printer bridge working as it does today, so the stack can stop at any
point before that one.

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

## Why one process

One container per printer breaks down wherever two printers touch one
spool, and that's normal use: a swap on one printer evicts the spool, and the
next print on another printer loads it. Flags, claims, and runouts are keyed
by spool, and each tool's pending evictions name spools. Two containers then
want different text in one flag field and each rewrites it every poll, so
neither view holds. A claim on a spool in the other printer's location looks
unplaced to each container, which flags and logs it, and shows it on the
wrong printer. The other printer's charges look like weight edits, which
clear a runout flag early. And only a process that sees every tool can
notice a spool active in two of them. One process computes every printer's
desired flags and writes each spool's flag once.

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
warns at each start and starts the new name from a baseline (B1). The old
entry is kept, not deleted, so nothing is lost by a typo, but it's inert: its
pending evictions never run, and a queued charge of its that Spoolman
rejects is dropped rather than held.

Rules the loader applies that the examples don't show: a name matches
`[A-Za-z0-9_.-]{1,64}`; `api_key` and `api_key_env` are exclusive; mixing
`[printer]` or `[[tools]]` with `[[printers]]` is an error, as is
`SPOOLMAN_PRUSALINK_LOCATION`; a URL that doesn't parse is a config error;
URLs are compared ignoring the case of the scheme and host, the default port,
and a trailing slash, with IPv6 brackets kept.

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

1. `flush()` the shared outbox. If that fails, the tick ends, as today.
2. Read every printer's status and filament. A printer that's off or that answers
   with an error is tracked by its own `Reachability`: one warning when the
   trouble starts, changes kind, or answers with a new error, an hourly
   reminder, and one warning when it ends. Either is skipped for this tick,
   and the others carry on. If no printer answers, the tick ends here, as today with one.
3. Read Spoolman once.
4. For each printer that answered: `check_missing()` and `process()`. A
   printer that didn't answer contributes the flag texts from its last
   `process()`, kept in its state, so an outage doesn't clear its flags.
5. The active-in-two-tools check, then the runout flags, then the ` | `
   join of each spool's texts.
6. `flush()` again, then `evict()` and `sync_claims()` over every printer's
   tools.
7. `sync_printer()` for each idle printer that answered. It runs after every
   `process()` because notices need the finished flags.
8. `sync_flags()`.

The interval is the shortest any printer asks for: `poll_printing` while any
reachable printer is busy or hasn't reported a state yet. The state comes
from the status read, so a printer whose filament endpoint is missing still
counts as idle or busy. Idle printers get two cheap GETs each at that rate,
which keeps the Spoolman read shared. Per-printer tasks with their own
intervals would need either a Spoolman read per printer or a cached list,
and several writers to one state file, so they're left for the SQLite change
if ever.

Printer-level log lines carry no location, so they're prefixed with the
printer's name when several are configured, and are unchanged with one.

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
move under that printer's name, and its outbox records and runouts are
stamped with it.
With several printers configured, the bridge refuses to start and says so,
because it can't tell which printer the tools belong to. The migrated file
reaches disk only when a poll saves, so the message says to poll once.

Each printer's state also keeps the flag texts its tools last asked for, and
a runout flag records the printer it ran out on, for notices.

Outbox and ledger records gain a `printer` field with the name. The
held-for-deleted lookup in `flush()` (M3), which finds a tool by the record's
`tool`, keys on `printer` and `tool`. Both fields are what a later SQLite
schema needs for its keys. Ledger lines from before this change have no
`printer`, so an import assigns them to the migrated printer's name.

Log lines keep the `[location]` prefix. Locations are unique across printers,
so the prefix already says which printer a line is about.

### Flags, notices, claims, and evictions

- **Flags.** Each tool's texts are prefixed with its location, and the
  bridge joins a spool's texts with ` | `. A spool that two printers have
  something to say about gets both texts. No conflict policy is needed. A
  printer that can't be read keeps its last texts, so an outage changes
  nothing in Spoolman.
- **Notices.** A flag shown with `M117` goes to the printer whose tool
  location holds the spool. A spool in no tool's location has no holder, so
  the printer that raised the flag shows it. A flag that no printer raised, on
  a spool in no tool's location, such as an unplaced claim, a shared-spool
  flag after the spool left every tool, or a runout whose printer isn't
  configured (from before names, or after a rename), is written to Spoolman
  and shown on no screen: a notice on an arbitrary printer wouldn't be acted
  on there. With one printer, those are the only notices it stops
  showing. `noticed` stays one list, keyed by text, so each text is shown
  once.
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
   "Several printers", the live checks are under "A second printer" in `live-checks.md`,
   and the out-of-scope row about shared locations names the config error.

The eviction re-read narrows the race to the instant between the re-read and
the write; it doesn't close it. It and the active-in-two-tools check are
useful on one printer too. If the stack stalls, they can be pulled out and merged alone.

## Cross-printer scenarios

The scenarios, X1 to X16, are the "Several printers" section of
`scenarios.md`. Each is tested with two fake printers against one fake
Spoolman in `tests/test_printers.py`. The live checks that need a second
printer are under "A second printer" in `live-checks.md`.
