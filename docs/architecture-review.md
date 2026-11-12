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

### B9. `account()` mixes last poll's state with this poll's

`account()` (`accounting.py`) is 490 lines: six numbered steps, about
ten mutable locals, and five closures, two of which use `nonlocal`. It's
pure, well documented, and has 94 tests through its public signature. The order of the
steps is real: unload, claim, assignment, print start, usage. The length
isn't the problem. These are:

- **`s` is half last poll and half this poll.** `s.active`, `s.assigned`,
  and `s.job_id` are the previous poll's values, compared with the new
  locals. `s.charged`, `s.unload_count`, `s.boot_id`, and `s.evict` have
  already been advanced with `replace`. The reader has to know which, field
  by field.
- **The usage tallies are kept by hand.** `swap.mm` and `provisional.mm`
  track what's been charged to their spool. Three charge sites each update
  them: `charge_held`, the unload in step 1, and the usage in step 5. A new
  charge site has to remember to.
- **The legal states aren't written down.** Which combinations of `active`,
  `assigned`, `swap`, `provisional`, and `deleted_spool` can occur is known
  only by reasoning. Three commits have removed branches that turned out to
  be unreachable. The one invariant that's checked, that a provisional
  charge is on the active spool, lives in a wrapper in
  `tests/test_accounting.py`.
- **Step 3 is a 13-branch `elif` chain** where order is priority.
- **Event classification is split.** `WARNINGS` is in `accounting.py`,
  `_REPEATING` is in `app.py`, and `process()` has a special case for
  `HOLD_DELETED`. `ACTIVE_UNASSIGNED` is used for five different messages,
  including "is back in the location", and `ASSIGNMENT_CHANGED` for two.

The rework is in two parts. Neither changes behavior, and the existing
tests are the check.

Part 1, a small stack of its own:

- **Two states.** `last` is the state passed in. It's frozen and never
  reassigned, so `last.x` always means the previous poll. `current` holds
  this poll's values: every step writes to it, and at the end it's the
  result. Nothing else carries tool state, so the loose locals go away.

  Start `current` as a copy of `last`, not empty. Most fields carry over
  unchanged on most polls, and with an empty start a field that a step
  forgets to copy is silently reset. The early returns for a first poll and
  an ambiguous location build their own state today and have the same
  risk.
- **One charge helper owns the tallies.** A charge to a spool updates
  `swap.mm` and `provisional.mm` when they're on that spool, in the helper,
  not at each call. Before changing it, trace the refund paths: a claim and
  a confirmed swap refund a spool and then clear the swap, which looks
  compatible but isn't confirmed.
- **Write the invariants down.** One function checks that a `ToolState` is
  a legal combination. The test wrapper calls it on every poll.
- **One event table** gives each kind its severity and whether it repeats.
  Split the overloaded kinds.

Part 2, with B6: each numbered step becomes a function over `last` and
`current`. B6 gives events fields, which already rewrites every `event()`
call, so do the split in the same stack.

Not planned: a formal state machine, with a named mode per tool and defined
transitions. The conditions overlap (a provisional charge with a staged
spool, a deleted spool with a staged spool), so the modes multiply. The
invariants function from part 1 is what such a machine would be derived
from. Reconsider it once that function exists and shows how many legal
combinations there are.

Size: small for part 1, medium for part 2.

### B10. Smaller items

- The `tools` table holds a JSON blob per tool, `printer_flags` holds JSON,
  and `seen_uuids` has columns. `SCHEMA_VERSION` doesn't cover the blob's
  shape. The blob is `ToolState.to_json` in `accounting.py`, so it belongs
  to workstream 10.

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
the fake Spoolman's settings and the simulated printer's faults and
`before_request` hook, as `tests/harness.py` does. Give `Database` a way to
inject a failing connection.

Size: large. Do it with B1.

### T3. A behavior change is made in two suites

Most W rows of `docs/scenarios.md` are tested in both
`tests/test_writes.py` and `integration/test_writes.py`.

The duplication is accepted (see [the test layers](#the-test-layers)).
Nothing to do now. If changing both suites for each behavior change becomes
a burden, narrow the integration suite to the scenarios where Spoolman's
own behavior decides the outcome. Don't narrow the unit suite.

### T7. Tests match prose

About 78 integration lines compare an event's message exactly, and over
300 lines across both suites match log text. B6
removes the cause for events. Keep one prose check per event kind.

## Simulator

The simulator review read every file in `simulator/`.

### S2. The page reimplements the model's rules

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

Size: large. Do S5 first.

### S5. Tool material rules are kept by convention

`_set_material` (`model.py`) keeps a tool's material, its ad-hoc slot
(`previous`), and `kept_type` in step. `_apply_uuid` (`model.py`),
`_tool_from_config`, and test setup in `simulator/tests/util.py`,
`tests/harness.py` (`fill`), and `integration/printers.py` (`start`) write
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
  `changed()` (`model.py`). `Station.requests` (`integration/station.py`)
  and `jobs` (`tests/harness.py`) find requests by parsing log text.
- "UUIDs only on tool 0 of a model that holds them" is written in about
  seven places in `model.py`, `config.py`, and `seed.py`.
- Snapshots are a dump of the internal dataclasses, with shims for older
  snapshots in `_process_from`, `_tool_from` (`kept_type`), and `Material`'s
  optional temperatures.

## Order and dependencies

| # | Workstream | Findings | Depends on |
| --- | --- | --- | --- |
| 2 | Tag-UUID owner | B1, T1 (UUID tests) | Nothing |
| 3 | Conditions and saved facts | B2, B3, B5, B7, T1 (rest) | Workstreams 2 and 10. B7 changes the part of `account()` that B9 part 1 restructures |
| 4 | Typed records | B6, B9 (part 2), T7 | Workstream 10. Easier after 3 |
| 5 | Narrow the bridge boundary | B4 | Workstreams 2 and 3 |
| 8 | Simulator state | S5, S6, then S2, S4 | Nothing. S4 continues the move that put the PrusaLink routing in `prusalink.serve` |
| 10 | Accounting state | B9 (part 1), B10 | Nothing |

Workstream 10 is independent and can run at any time. T3
has no work, so it isn't in a workstream.
Of the large ones, workstream 2 is best first: it has the least clear state
and the most pinned tests, and workstreams 3 and 5 get simpler once it's
done.
