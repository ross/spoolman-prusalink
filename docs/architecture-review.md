# Architecture review

This is a list of design problems found in a review on 2026-11-12, with the
rough shape of a rework for each. Every finding was checked against main
`9402330`. Each workstream is meant to be picked up by a later session as
its own stack of PRs.

Findings name functions, not line numbers, which move with every merge.
Counts are as of `9402330`.

## How to use this document

- Pick a workstream from [Order and dependencies](#order-and-dependencies),
  not a single finding. Several findings share tests and state, so they land
  together or block each other.
- Don't keep backwards compatibility. The project is pre-release with one
  test deployment, so a rework can change the saved state, the ledger
  records, or the config without a migration. Say in the PR what the
  deployment has to do, such as starting from an empty database.
- When a workstream merges, delete its findings from this document. When the
  last one goes, delete the document.

What was checked: every file in `src/spoolman_prusalink` was read in full,
and the bridge findings (B1 to B11) come from that reading. The test (T1 to
T8) and simulator (S1 to S6) findings come from two separate reviews. A
second pass then checked every finding against the code.

## The common pattern

Stack #105 removed `Once`, which coupled problem state to log levels and
held both away from the code that owned them. Most findings below are
relatives of that problem:

- State that has no single owner, kept consistent by convention.
- A value stored in its rendered form (G-code, prose) and parsed back.
- One module reaching into another's private fields.
- The same rule or list written out in several places.

The rule from #105 still applies to every rework here: state lives at the
code that owns it, and the call site that detects a change logs it with a
literal level. Don't add a central object that owns levels.

## Bridge

### B1. Tag-UUID trust has no owner

The rules for trusting an instance UUID read from a tag are a state machine
spread over `PrinterBridge` and `Bridge`:

- `_seen_uuid` (`app.py`) is a positional 4-tuple of UUID, spool,
  appeared, and runout. Code reads it as `seen[1]`, `seen[2]`, and `seen[3]`.
  A 14-line comment explains the field.
- `PrinterState.seen_uuids` (`state.py`) is a 3-tuple copy of the same
  data. `_see_uuid` and `_seen_unload` write both by hand.
- `_uuid_gap`, `_adopts` (a 3-tuple per tool), `Bridge._refused_uuids`, and
  `Bridge._unowned_uuids` hold the rest.
- `_tag_uuid` (`app.py`) is named like a query, but it records what it
  saw and changes saved state. It returns `("adopt", uuid)`,
  `("keep", uuid)`, or `("conflict", uuid)` as strings.
- Nothing saves the change. It relies on the `_save()` at the end of
  `process()`.

Why it hurts: a change to any one rule means tracing six fields through five
methods. The tests pin the tuple layout (see T1), so the shape can't change
without rewriting them.

Shape: a typed per-tool record, such as a `SeenTag` dataclass, that the
database stores as is. A pure function takes the record, the reported
material, the active spool, whether that spool is certain, and the spool
list. It returns the new record and a decision from an enum. That's the form
`accounting.account()` already has. The caller saves and acts.

Size: large. Do it with T1.

### B2. `process()` does five jobs

`PrinterBridge.process()` (`app.py`) is about 180 lines. For each tool it
runs the accounting, builds ledger records, collects flag texts, decides
UUID trust, and classifies a mismatch as a pending write or drift.

Most conditions are reported on two channels with separately worded text:
`notes` for the log and `flags` for Spoolman. Symptoms of the two channels
having no shared model:

- `_notes` (`app.py`) reports an ended note by splitting its text on
  `"] "` to recover the location.
- `_COMPARED` (`app.py`) finds the notes about a comparison by testing
  whether the key is a tuple whose first item is `"printer"` or `"uuid"`.
  It keeps those notes standing while a tool is skipped.
- `_compared_flags` and `_keep_compared` do the same job for flags, with
  their own dictionary. The two mechanisms don't share a key.
- `_provisional_shown` (`app.py`) is state that exists only so a flag's
  text doesn't change on every poll, because the text has a growing number
  in it.

Shape: pure functions that turn a tool's state into a list of conditions.
Each condition has a key, a spool, a log text, an optional flag text, and
whether it depends on comparing the printer with the spool. The code that
creates a condition gives it the literal level it logs at. One differ finds
the starts and ends, logs each at its condition's level, and collects the
flags. `process()` becomes the loop that calls them.

Stack #105 left `_notes` as it was, because it already logs on change. This
finding is about how notes are keyed and paired with flags, not about when
they log.

Size: large.

### B3. Saved state stores presentation, not facts

- `ToolState.written` (`accounting.py`) holds the `M865` lines of the
  last write. Three places parse it back with regular expressions:
  `_written_uuid` (`app.py`), `printer_matches_write`
  (`printer_sync.py`), and `_written_temperatures`
  (`printer_sync.py`).
- `_edited` (`app.py`) decides that a spool was edited in Spoolman by
  comparing freshly rendered G-code with the saved text. A change to how
  `filament_lines` renders a line would make every tool that still shows
  the bridge's last write look edited after an upgrade, and the bridge
  would write each one once more. Tagged tools are never written, so they
  aren't affected.
- `Notice.message`, `Runout.text`, and `PrinterState.flags` are saved as
  prose. A wording fix doesn't reach state that already exists.

Shape: save what was written as fields (name, color, temperatures, UUID) and
save notices and runouts as a kind with parameters. Render G-code and prose
at the point of use. `PrinterState.flags` can then hold conditions from B2.

Size: medium.

### B4. The `Bridge` and `PrinterBridge` boundary leaks both ways

The split is "keyed by tool" against "keyed by spool", but each side works
on the other's private fields:

- `PrinterBridge` rebuilds `bridge._archived` in `check_missing`, once per
  printer per poll. `Bridge.sync_flags` writes the same cache. The printer
  also reads `bridge._refused_uuids` and `bridge._unusable_fields`, and
  calls `bridge._save`, `bridge._mint_uuid`, and `bridge._adopt_uuid`. It
  writes `bridge.state.runouts`, `bridge.state.claimed`, and
  `bridge.state.outbox`.
- `Bridge` sets `p._idle_since` in `poll`, calls `p._hold_deleted` from
  `flush`, reads `p._printer_reach.down` and `p._last_state` in `interval`,
  and assigns `printer.tools[tool]` in `evict`.

Shape: small owners for the spool-keyed concerns, each with public methods:
the archived-spool cache, the UUID registry (mint, adopt, refused, unowned),
and the flags. `PrinterBridge` gets the few it needs instead of a
back-reference to the whole `Bridge`. `Bridge` asks a printer for what it
needs through public methods.

Size: medium. Easier after B1 and B2, which remove most of the reach-ins.

### B5. The poll order is an implicit protocol

`Bridge.poll()` (`app.py`) calls its phases in a fixed order, and the
phases talk through fields:

- `process()` fills `_pending_writes` and `_adopts`. `adopt_uuids()` removes
  tools from `_pending_writes`. `sync_printer()` consumes both.
- `flags` is a dictionary passed down as an out-parameter and filled by
  `_claims`, `process`, `_check_shared`, and `_finish_flags`. The result is
  left in `_flags` for `sync_flags`.
- `sync_printer` sets `_last_state = "BUSY"`, a state the printer didn't
  report, so that `interval()` polls again soon.
- There are seven `_save()` call sites. Several mutations of saved state
  rely on a later save by other code. `_hold_deleted` has a comment that
  says so.

Why it hurts: reordering two phases, or adding one, can break another phase
with no failing type or signature.

Shape: each phase returns a value and the next takes it as an argument.
`process()` returns the writes it wants, the adoptions, the flags, and the
ledger records. `poll()` passes them on. A "poll again soon" request is its
own field. Each phase saves once, at its end.

Size: medium. Falls out of B2 for the most part.

### B6. Charges and events are untyped dictionaries

A charge is a dictionary with 11 keys built in `process()` (`app.py`).
It goes to the outbox, then to `flush`, then to the ledger.

- Readers are defensive about a shape the bridge itself writes:
  `rec.get("tool", -1)` and `int(rec.get("extruded_mm", 0))`, in `flush`
  and `_hold_deleted`.
- `db.save` works out which charges left the outbox by comparing lists of
  canonical JSON (`db.py`), because the outbox is a plain list that its
  owner mutates.
- The ledger tells a charge from an event by whether the record has an
  `event` key (`db.py`).
- `Event` is a kind and a message (`accounting.py`). Amounts and spool
  IDs exist only in the prose, so about 78 integration test lines compare
  messages exactly (see T7).

Shape: `ChargeRecord` and `EventRecord` dataclasses. An outbox object with
push and pop that knows its own row IDs, so the database is told what
changed. Events carry fields (millimeters, from spool, to spool) and the
message is rendered from them.

Size: medium.

### B7. Write-sync state sits in the accounting state

`needs_sync`, `sync_attempts`, and `written` are fields of `ToolState`.
`account()` sets `needs_sync` and resets `sync_attempts`
(`accounting.py`), although the accounting knows nothing else about
printer writes. `app.py` rewrites all three in `_no_write` and
`sync_printer`.

Shape: `account()` reports "the active spool changed or the tool unloaded"
in its result. The write logic owns its own per-tool state, saved beside the
tool state.

Size: small to medium. Do it with B3, which reshapes `written`.

### B8. Raw `httpx` errors reach every layer

`app.py` has 18 `except` sites that name `httpx` errors. Seven of them also
catch `ValueError` and `KeyError`, because the caller has to know what the
client's parsing can raise. Status codes are interpreted far from the
client: 404 in `flush` and `sync_flags`, 409 in `sync_printer`, 5xx in
`flush`, and the other 4xx codes in `_unusable`.

Why it hurts: a `KeyError` from a bug in bridge code inside one of those
`try` blocks is logged as a bad answer from the service.

Shape: the clients raise their own errors: unreachable, bad answer, not
found, busy. The clients catch parse errors where they parse. `app.py`
catches the client errors only.

Size: small to medium. Independent of the rest.

### B9. `account()` keeps its tallies, legal states, and event kinds by convention

`account()` (`accounting.py`) is about 500 lines in six numbered steps.
It's pure, well documented, and has 94 tests through its public signature.
The order of the steps is real: unload, claim, assignment, print start,
usage. The length isn't the problem. These are:

- **Step 3 is a 13-branch `elif` chain** where order is priority.
- **Event classification is split.** `WARNINGS` is in `accounting.py`,
  `_REPEATING` is in `app.py`, and `process()` has a special case for
  `HOLD_DELETED`. `ACTIVE_UNASSIGNED` is used for five different messages,
  including "is back in the location", and `ASSIGNMENT_CHANGED` for two.

The rework is in two parts. Neither changes behavior, and the existing
tests are the check.

Part 1, a small stack of its own:

- **One event table** gives each kind its severity and whether it repeats.
  Split the overloaded kinds.

Part 2, with B6: each numbered step becomes a function over `last` and
`current`. B6 gives events fields, which already rewrites every `event()`
call, so do the split in the same stack.

Not planned: a formal state machine, with a named mode per tool and defined
transitions. The conditions overlap (a provisional charge with a staged
spool, a deleted spool with a staged spool), so the modes multiply.
`violations()` in `accounting.py` lists the rules for a legal state, which
is what such a machine would be derived from. Reconsider it against those
rules.

Size: small for part 1, medium for part 2.

### B10. Smaller items

- The `tools` table holds a JSON blob per tool, `printer_flags` holds JSON,
  and `seen_uuids` has columns. `SCHEMA_VERSION` doesn't cover the blob's
  shape.
- "Only tool 0 holds a UUID" is written as `tool == 0` in seven places in
  `app.py`, `drift.py`, and `printer_sync.py`.
- `Runout.printer` is saved and never read.
- `Spoolman.set_extra_text` and `drift.printer_name` only call another
  function.
- `COREONEL_PREAMBLE` is used only by tests.
- Stale comments: "Used from phase 3" (`config.py`), "used by phase 3
  writes" (`prusalink.py`), "Pending firmware fields" (`models.py`).
- `config.py` has three near-identical number parsers (`_number`,
  `_interval`, `_delay`), and `load()` keeps the single-printer checks "in
  the order they always have".
- Log lines name a printer three ways: `label`, `_prefix`, and
  `[location]`.
- `docs/sqlite.md` and `docs/multi-printer.md` describe plans that have
  merged, in the future tense.

### B11. Printer settings are configured globally

A setup can mix printers, so a setting that describes a printer belongs to
that printer's config entry. Five settings under `[bridge]` describe a
printer and are read by `PrinterBridge` through `bridge.config`:

| Setting | What it describes |
| --- | --- |
| `length_factor` | How far the printer's extruded length is from real usage. Two printers can need different factors, and today they share one. |
| `printer_sync`, `sync_delay`, `sync_drift` | Whether and when the bridge writes to the printer |
| `flag_drift` | Whether drift on the printer becomes a flag |

`uuid_field`, `flag_field`, and `claim_field` name Spoolman fields and stay
global.

Shape: move the five settings to `PrinterConfig`, set in the `[printer]`
table or in each `[[printers]]` entry, with no global default. One place
per setting is less error prone than a default with overrides.
`PrinterBridge` then reads them from its own config. It still reads
`claim_field` and `uuid_field` through `bridge.config`, so this removes
five of the reads that B4 lists, not the back-reference.

Size: small.

## Tests and tooling

### The test layers

Each layer keeps the one below it honest. The findings in this section are
judged against these roles.

| Layer | Role | Runs |
| --- | --- | --- |
| Unit suite (`tests/`, `simulator/tests/`) | Covers everything. The 99.4% floor counts this suite only, as a cheap check that nothing lacks a test. | Constantly during development, so it stays fast and needs no container. |
| Integration suite (`integration/`) | Checks that the unit suite's fakes and state setup haven't diverged from a real Spoolman. | In CI and before a merge. Development doesn't depend on it. |
| Live checks and firmware reading | Keep the simulator behaving like a real printer, which can't be put in a test loop. | By hand, at the printer. |

Some scenarios are tested in both suites. That's accepted. Don't move
coverage out of the unit suite to remove it.

### T1. Tests pin the bridge's private state

Tests read, set, or call private attributes of the bridge in about 150
places. The most used are `_tag_uuid` (17), `db._conn` (16), `_adopt_uuid`
(13), `_seen_uuid` (12), `_notes` (11), and `_loaded_tag` (10).

- Tests write `_seen_uuid` as a positional 4-tuple, for example
  `tests/test_app.py`.
- `tests/test_failures.py` sets `_sync_busy`, `_sync_failed`, and
  `_sync_delaying` to reach each log line.
- `tests/test_writes.py` replaces `bridge.db._conn` with a failing
  connection.

Why it hurts: B1, B2, and B5 can't land without rewriting these tests. The
unit suite has to reach every branch, and while the logic sits inside
`process()`, calling a private method or setting a private field is the
cheapest way to do that.

Shape: make the branches cheap to reach in public. Once B1 and B2 turn the
decisions into pure public functions, test those functions directly, as
`tests/test_accounting.py` tests `account()`. Drive problem states through
the fakes' behavior, which already have `upload_error` and `busy`. Give
`Database` a way to inject a failing connection.

Size: large. Do it with B1.

### T2. Nothing checks the unit suite's fake printer

`tests/test_writes.py` is 3,487 lines with 158 tests and 475
`bridge.poll()` calls. It runs on a hand-written
`FakeSpoolman` and a fake `Printer` that has its own `M865` parser and its
own default temperatures, which copy `simulator/gcode.py` and
`simulator/model.py`. `tests/test_failures.py` and
`tests/test_printers.py` import from it.

Why it hurts: the fake Spoolman is checked by the integration suite, and
the simulator is checked by the live checks. The fake `Printer` is a third
model of the firmware that neither checks. A firmware behavior fixed in the
simulator doesn't reach it, so these tests can pass against a stale
printer.

Shape: replace the fake `Printer` with `SimPrinter`, in process. Keep
`FakeSpoolman`. Move the shared harness into the existing
`tests/conftest.py`.

`SimPrinter` can run in process: its PrusaLink views are plain methods
(`status_json`, `filament_json`, `job_json`, `version_json`, `upload`), and
time passes only when a test calls `tick`. What's missing is the routing.
It lives in aiohttp handlers in `simulator/prusalink.py`, and the
integration suite reaches it over a real socket.

1. Move the routing and the power, key, and fault gate out of the aiohttp
   handlers into one function that takes a method, path, headers, and body,
   and returns a status and JSON. The aiohttp app and a new
   `httpx.MockTransport` handler both call it, so the routes exist once.
   This is also the first step of S4.
2. Cover what the fake can do. The fake returns any status code for every
   request (`error`) or for uploads only (`upload_error`), and scripts the
   next status states (`states`). `make_app` takes a `before_request` hook
   that can answer or drop a single request, which covers the first two.
   Keep that hook in the new function. Check `states` against the
   simulator's `Fault`, `Power`, and `Firmware` settings, and use the hook
   for what they don't cover. Don't add test-only faults to the simulator.
3. Port the tests. The fake finishes a sync job at once, and the simulator
   takes ticks, so tests need the tick helpers that `World` has. They're in
   `integration/conftest.py`, which can't be imported as a module, so move
   them into a plain module that both suites import. T6 rewrites the same
   helpers.

Size: large.

### T3. A behavior change is made in two suites

Most W rows of `docs/scenarios.md` are tested in both
`tests/test_writes.py` and `integration/test_writes.py`.

The duplication is accepted (see [the test layers](#the-test-layers)).
Nothing to do now. If changing both suites for each behavior change becomes
a burden, narrow the integration suite to the scenarios where Spoolman's
own behavior decides the outcome. Don't narrow the unit suite.

### T6. Harness duplication

- The `world` fixture (`integration/conftest.py`) branches on six
  indirect parameters. `Station` (`integration/printers.py`)
  reimplements `World`'s `start_print`, `swap_cycle`, `unload`, `counter`,
  and `material`, and has drifted: `Station.swap_cycle` lacks a temperature
  default that `World.swap_cycle` adds.
- `baseline()` is defined in seven integration test files.
- Helpers that assert "logged once" differ by file. Some check the level
  and some don't. Logging a problem once is product behavior, so one helper
  should define it.

Shape: build `World` from a list of station specs and have it delegate to
`Station`. One `baseline` and one log-assertion helper in `conftest.py`.

Size: medium.

### T7. Tests match prose

About 78 integration lines compare an event's message exactly, and over
300 lines across both suites match log text. B6
removes the cause for events. Keep one prose check per event kind.

### T8. Scripts and scenario table

- `script/demo` and `script/integration` each detect the container engine,
  pin `ghcr.io/donkie/spoolman:0.27.1`, and wait for health.
  `compose.example.yaml` uses `:latest`, a version CI doesn't test.
- The Status column of `docs/scenarios.md` names 157 tests by hand, in 88
  rows.
  `tests/test_integration_table.py` checks them, so the cost is the editing,
  not drift.

Size: small.

## Simulator

The simulator review read every file in `simulator/`.

### S1. Printer state is stored beside the job phase

`simulator/model.py` has 26 `_set_state()` calls, nearly all paired with a
`job.phase = ...` write. While a job exists, the state follows from the
phase. The `HOLD` phase covers three situations, told apart by `job.jam`,
`job.id is None`, and comparing `attention` with the text `"model check"`.
`static/app.js` reads the attention text too: with regular expressions to
find a tool index, and with `===` and `startsWith` to enable a button.

Shape: derive the state from the phase with one table and store only the
idle screen. Make the attention reason an enum with a tool index. Split
`SimJob` into a user job and a sync job.

Size: medium.

### S2. The page reimplements the model's rules

- The Resume button is disabled unless the state is `PAUSED`, but
  `resume()` also handles the wait on a cold bed after a power panic, which
  is `ATTENTION`. The page can't continue that print (#134).
- The page's `SETTINGS` list (`app.js`) lacks `recover_seconds`,
  `unpark_seconds`, and `panic_purge_mm`.
- `canStart`, `dLoad`, `dUnload`, and `IDLEISH` copy model rules, and the
  option lists copy the enums.

Shape: `to_json` publishes the actions available for each printer and tool,
the setting fields, and the enum values. The page renders them.

Size: medium.

### S4. `SimPrinter` holds too much

`SimPrinter` (`model.py`) holds the physical and job state machine,
the PrusaLink wire format, the page's JSON view, a G-code interpreter, and
checks on raw JSON input. The trace recorder samples the wire format in
process, so the format must stay callable without HTTP.

Shape: a pure `prusalink.render(printer)` for the wire views, the G-code
interpreter in its own module, and input checks in the control layer.

Size: large. Do S1 and S5 first.

### S5. Tool material rules are kept by convention

`_set_material` (`model.py`) keeps a tool's material, its ad-hoc slot
(`previous`), and `kept_type` in step. `_apply_uuid` (`model.py`),
`_tool_from_config`, and test setup in `simulator/tests/util.py`,
`integration/conftest.py`, and `integration/printers.py` write
the fields directly. `SimTool.temps` copies temperatures that `Material`
already has.

Shape: methods on `SimTool` own material writes, with a "loaded with"
constructor for test setup. Fold `temps` into the slot's `Material`.

Size: medium.

### S6. Smaller items

- Config-to-model conversion lives in `model.py` (`from_config`,
  `_tool_from_config`), with an import cycle hidden
  behind `TYPE_CHECKING`. Material name, color, and UUID checks exist in
  both `config.py` and `model.py`.
- The control API dispatches with `getattr(printer, action)(**body)`
  (`control.py`) and catches `TypeError` around the whole call, so a
  bug inside an action returns 400 "bad parameters". The action list is
  kept in `control.py`, the README, and `app.js`.
- `Simulation` mixes the clock with web streaming, and `log()` always calls
  `changed()` (`model.py`). `integration/conftest.py` counts
  requests by parsing log text.
- "UUIDs only on tool 0 of a model that holds them" is written in about
  seven places in `model.py`, `config.py`, and `seed.py`.
- Snapshots are a dump of the internal dataclasses, with shims for older
  snapshots in `_process_from`, `_tool_from` (`kept_type`), and `Material`'s
  optional temperatures.
- `seed` picks spool materials from `CYCLE[tool % 3]` (`seed.py`) and
  mints a random UUID, so the demo printer's preloaded tag UUID never
  matches its seeded spool, and the demo starts with UUID drift (#135).
  This may be intended. Nothing says so.

## Order and dependencies

| # | Workstream | Findings | Depends on |
| --- | --- | --- | --- |
| 2 | Tag-UUID owner | B1, T1 (UUID tests) | Nothing |
| 3 | Conditions and saved facts | B2, B3, B5, B7, T1 (rest) | Workstreams 2 and 10. B7 changes the part of `account()` that B9 part 1 restructures |
| 4 | Typed records | B6, B9 (part 2), T7 | Workstream 10. Easier after 3 |
| 5 | Narrow the bridge boundary | B4 | Workstreams 2 and 3. Easier after B11 |
| 6 | Client errors | B8 | Nothing |
| 7 | Unit suite on the simulator | T2, T6 | Nothing. Before workstreams 2 and 3 if possible, so their tests are ported once |
| 8 | Simulator state | S1, S5, S6, then S2, S4 | Workstream 7 for S4, which continues the routing move in T2 |
| 9 | Small items | B10, B11, T8 | Nothing |
| 10 | Accounting state | B9 (part 1) | Nothing |

Workstreams 6, 9, and 10 are independent and can run at any time. T3
has no work, so it isn't in a workstream.
Of the large ones, workstream 7 is best first, because workstreams 2 and 3
rewrite many of the same tests and would otherwise port them twice. Then
workstream 2: it has the least clear state and the most pinned tests, and
workstreams 3 and 5 get simpler once it's done.
