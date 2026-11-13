# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## What this is

`spoolman-prusalink` is a sidecar service, called the bridge, that charges filament usage
from Prusa printers to spools in [Spoolman](https://github.com/Donkie/Spoolman). It polls
each printer's PrusaLink API, maps each tool to a Spoolman location, charges the spool in
that location, flags problems on the spool, and writes the spool's material back to the
printer. One bridge serves one Spoolman and any number of printers.

The bridge needs `GET /api/v1/filament`, which exists only on the
`prusalink-filament-endpoint` branch of a Prusa-Firmware-Buddy fork. Stock firmware
doesn't have it.

The project is pre-release, so there's no backwards compatibility to keep.

## Commands

The `script/` directory follows the
[Scripts to Rule Them All](https://github.com/github/scripts-to-rule-them-all) pattern.
Every script runs `script/bootstrap` first, which creates the checkout's own `.venv`, so
a fresh clone or worktree needs no setup step.

| Command | What it does |
| --- | --- |
| `script/test` | Runs the unit suite with branch coverage. Fails below the floor in `pyproject.toml`. |
| `script/test tests/test_app.py -k unload` | Runs one file or test. Arguments go to pytest, and the coverage floor doesn't apply. |
| `script/lint` | Runs `ruff check` and `ruff format --check`. Changes nothing. |
| `script/format` | Applies ruff's formatting and safe fixes. |
| `script/integration` | Runs the end-to-end tests. Arguments go to pytest, for example `-k runout`. |
| `script/cibuild` | Runs what CI runs for each Python version: bootstrap, lint, and the unit suite with coverage. |
| `script/demo` | Runs a Spoolman container, the simulator, and the bridge together. |
| `script/server` | Runs the bridge with `./config.toml`. `script/server once` polls once and exits. |
| `script/firmware-check` | Compares the copied firmware tables with the firmware source. |
| `.venv/bin/python -m integration.table --write` | Regenerates the scenario table in `integration/README.md`. |

Before you finish a change, run `script/test` and `script/lint`. CI also runs
`script/integration` and `docker build .`.

## Layout

| Path | What it holds |
| --- | --- |
| `src/spoolman_prusalink/` | The bridge. The Docker image ships only this code. |
| `simulator/` | A stand-in for Prusa printers. It serves the PrusaLink endpoints the bridge calls, over a physical model of tools, counters, and jobs. The bridge never imports it. |
| `integration/` | The end-to-end tests. The bridge and the simulator run in the test process, against a real Spoolman. |
| `firmware/` | The code behind `script/firmware-check`. |
| `tests/`, `simulator/tests/`, `firmware/tests/` | The unit suite. |
| `docs/` | The scenario list, the printer behaviors, and plans. |

## Bridge architecture

The poll loop is in `app.py`, in two classes:

- `Bridge` owns the loop and everything keyed by spool: the charge outbox, flags, claims,
  and evictions. `Bridge.poll()` sends charges left from an earlier poll, reads every
  printer, reads Spoolman's spools once, processes each printer, and then reconciles
  Spoolman.
- `PrinterBridge` is one printer's side: it reads the printer, accounts its tools, and
  writes to it. Its `process()` returns a `Plan` of charges, events, and writes for
  `Bridge` to save and carry out.

The decisions are in pure code, and `app.py` does the I/O around them:

- `accounting.account()` is a pure function. It takes one tool's stored state, the
  printer's reading, the spools in the tool's location, the job, and any claim. It
  returns the charges, the events, and the new state. Most behavior changes start here.
- `accounting.violations()` lists what's illegal in a tool state. A fixture in the root
  `conftest.py` checks every state that goes into or comes out of `account()`, in every
  suite.
- `events.py` and `conditions.py` hold typed, frozen records. An event is something that
  happened, and it goes to the log and the ledger. A condition is something that's wrong
  now, and it lasts from the poll that finds it to the poll that doesn't. Each record
  renders its log line and its Spoolman flag text from its fields, so add a field
  instead of formatting a string where the record is created.
- `tags.py` decides when the bridge can trust a tag's instance UUID as the spool's
  identity.
- `drift.py` compares the printer's loaded filament with the assigned spool.
- `printer_sync.py` builds the G-code job that sets the printer's filament. PrusaLink has
  no write endpoint for filament, so the bridge uploads a small `M865` job and runs it as
  a print.

The clients, `prusalink.py` and `spoolman.py`, raise only the `ServiceError` hierarchy
in `errors.py`. The rest of the bridge never sees `httpx` or parsing errors. It catches
only `ServiceError`, so an error from the bridge's own code isn't mistaken for a bad
answer.

The bridge keeps its state in SQLite through `db.py`: per-tool state, flags, runouts,
claims, the charge outbox, and the ledger. `state.py` is the same state in memory. The
bridge saves a charge to the outbox before sending it, so a failed send or a restart
doesn't lose the charge.

`SCHEMA_VERSION` in `db.py` stays at 1 until the first release. Until then, change the
saved state in place, with no migration.

`config.py` reads a TOML file and takes secrets from the environment. Environment
variables alone can configure one single-tool printer.

## Tests

The tests have three layers, and each keeps the one below it honest.
`integration/README.md` has the reasoning.

- **Unit suite.** It covers everything, needs no container, and stays fast. The coverage
  floor, `fail_under` in `pyproject.toml`, counts this suite only. When coverage rises,
  raise the floor. Don't lower it. Remove dead code instead of excluding it from
  coverage: the code has no `pragma: no cover`.
- **Integration suite.** It checks that the unit suite's fakes haven't diverged from a
  real Spoolman. Both suites test some scenarios, and that's accepted. Don't move
  coverage out of the unit suite to remove the overlap.
- **Live checks.** Traces recorded from the real printer are in
  `simulator/tests/traces/`, and `simulator/tests/test_traces.py` replays them against
  the simulator. `docs/printer-behaviors.md` is the record.

These points aren't obvious from any one file:

- The unit suite's harness, `tests/harness.py`, runs the real bridge against a fake
  Spoolman and the simulator's `SimPrinter`, in process. It imports
  `integration/station.py`, so a change to that module affects both suites.
- Nothing waits on the wall clock. The simulator's time moves only when a test ticks it,
  and the bridge polls only when a test calls `poll()`.
- Run the integration tests only through `script/integration`. It starts a throwaway
  Spoolman container for each run, with Podman or Docker, because two runs that share a
  Spoolman flag each other's spools. A plain `pytest integration` skips every test.
- Tests and scripts never read the bridge's own settings, such as `SPOOLMAN_URL`, so a
  shell set up for a real bridge can't point them at a real Spoolman. Give a new setting
  its own name, as with `INTEGRATION_SPOOLMAN_URL` and `DEMO_SPOOLMAN_URL`.
- Name an integration test `test_<row>_<short name>`, where the row is a scenario ID from
  `docs/scenarios.md`, such as `S7` or `W13`. Give it a docstring that says what it
  checks. The table in `integration/README.md` is generated from the names and
  docstrings, and `tests/test_integration_table.py` fails when the table or the scenario
  rows are out of step with the tests.

## Scenarios and printer behaviors

`docs/scenarios.md` lists every event the bridge handles, what the bridge does for each,
and how well it's tested. Each row has an ID, and code comments, tests, and commits refer
to rows by ID. The letter is the row's group:

| Letter | Group |
| --- | --- |
| S | Swaps between prints |
| P | During a print |
| C | Printer state and the counters |
| W | Writing to the printer |
| M | Spoolman |
| B | The bridge itself |
| K | Claims |
| X | Several printers |
| T | Multi-tool |

When you change a behavior, update its row. When you add a behavior, add a row.

`docs/printer-behaviors.md` lists what the printer reports through PrusaLink. Each
behavior has its firmware source and the trace that checks the simulator against it.
Change the simulator's printer behavior only on that evidence: the firmware source or a
trace.

## Firmware tables

`printer_sync.py` and the simulator hold copies of tables from the firmware: printer
models, preset filaments, reset defaults, and tag aliases. `script/firmware-check`
rebuilds the tables from the firmware source and lists each difference. A weekly
workflow runs it.

After you change a copied table, run the check. If a difference is intended, add it to
`EXPECTED` in `firmware/compare.py`. The check also fails when an expected difference no
longer occurs.

## Conventions

- **Logging.** Log a problem once, when it starts, and stay quiet until it changes or
  ends. Log its end once, at the same level. Never log the same problem on every poll.
  Conditions and `Reachability` in `app.py` do this, so use them instead of adding a log
  call to the poll path.
- **Ruff.** The line length is 100, and the rule sets are `E`, `F`, `I`, `B`, `UP`, and
  `SIM`.
- **Python.** The oldest supported version is 3.11, and the image ships 3.13. CI tests
  both.
- **README.** `README.md` is the user guide, and it describes the bridge's behavior in
  detail. When a change alters what a user sees, update the README in the same change.
