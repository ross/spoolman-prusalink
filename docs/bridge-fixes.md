# Bridge fixes

This is the plan for fixing what [bridge-findings.md](bridge-findings.md)
records: BF1 to BF29, the places where the simulator work showed the bridge
charges the wrong amount or the wrong spool, misses a swap, or fails to
write to the printer. The findings were held until the simulator was done,
so they could be planned as a whole. The plan groups them into ten
workstreams that sessions can take up one at a time, each from a cold
start with only the repository.

Draft, 2026-11-14. No workstream has started. Every approach below is a
proposal, read from the code but not built: the session that takes a
workstream checks it against the code before building on it.

bridge-findings.md stays the record of each finding: what happens, the
evidence, and its status. This plan is the record of the work. It points
at findings by BF ID and doesn't repeat them, so read each finding there
before you start on it.

## Decisions for Ross

Each decision blocks the workstreams it names. Nothing else in this plan
waits on Ross. When a decision is made, rewrite its row as the answer and
update the workstreams it changes.

| ID | Question | Recommendation | Blocks |
| --- | --- | --- | --- |
| D1 | In what order do the workstreams go? | As [Order and parallelism](#order-and-parallelism) lists: what protects the printer first (workstreams 1 and 3, then 2), then charging (4 to 6), then the toolchangers before the INDX upgrade (7 and 8), then the MMU (9). This breaks the stage table's rule that, within stage 1, charges come before writes: BF26 loses a print, and the write fixes are small. | The order only. |
| D2 | Does stage 1 ship alone, merged and deployed to the Core One L before stage 2 or 4 work merges? | Yes. Workstream 7 can be built at the same time, but it merges after workstreams 2 and 3, which change the same code in `app.py` and `printer_sync.py`. | Merging workstreams 7 and 9. |
| D3 | How does BF26 get fixed: a positive `sync_delay` default, a guard in the bridge after a boot that interrupted a print, or wait for the F12 trace? | A positive default, at most `poll_idle`. The code already holds a write while the printer has been idle for less than `sync_delay`, and a new `PrinterBridge` or a failed read starts that clock again, so the first idle poll after a boot only starts it. That covers a bridge that restarts in the window too, as when one power cut takes down the printer and the bridge's host. A second cut during the resume opens a second window, which the delay alone misses when a poll in the first window started the clock and the next poll, `poll_idle` later, lands in the second; workstream 1 starts the clock again at each new `boot_id` to close that. What it misses: a config that sets `sync_delay = 0`, and a window that outlasts a poll. What it costs: every write waits one more idle poll, up to 60 s by default. The guard instead keeps ordinary writes at the first idle poll and covers a config with 0, but it misses a restarted bridge: what the printer was doing before the boot is known only in memory. Add the guard as well only if writes at the first idle poll matter to you. Until the default ships, set `sync_delay` above 0 on your own printer. The F12 trace confirms that the window ends within one poll. | Workstream 1. |
| D4 | Keep `gcode_preamble`, or remove it? | Remove it, unless your own config sets it. Its only effects found are BF2, BF3 and BF4, and the default preamble passes every check on every model the bridge writes to. Pre-release, there's no compatibility to keep. If you keep it, workstream 3 validates it instead. | Workstream 3, and the INDX lock line in workstream 7. |
| D5 | Amend Q4 so that a ramming or a load isn't a print's first extrusion? | Yes. Q4's intent, that a cancelled print changes nothing, holds; only what counts as the first extrusion narrows. | Workstream 4. |
| D6 | Amend C7: usage counted on a tool with no filament while a user's print runs is reported lost, not charged to the next spool? | Yes. That filament came from no spool. The hold during a load, which S1 and S2 rely on, stays as it is. | Workstream 5. |
| D7 | BF9: accept and document the auto-retract's count, or correct it in the bridge? | Wait for the F3 and F5 traces (sittings A and B), then accept and document it if they show about 28.5 mm per auto-retract. A correction needs the auto-retract setting and the filament's flexibility, which the bridge can't read, and the error is about 0.09 g each time. | Workstream 6. |
| D8 | MMU3 printers, which you'll never own: fix the findings, detect the MMU and refuse it, or document it? | Detect it from `/api/v1/info`'s `mmu`, and while it's on, charge nothing and write nothing, logged once, as for a printer with no counters. BF16 alone over-charges about 0.63 g per tool change, so a multi-color print's charges are far off, and BF15 and BF17 have no fix the bridge can see. The middle option, writes off and charging on with the errors documented, is cheaper still and keeps those errors. | Workstream 9. |
| D9 | BF16's open question: if the MMU is fixed (D8), does the target charge count the slicer's 17 mm and 32 mm loads? | Moot under the recommended D8. If you choose to fix, aim at what leaves the spool for good, without the loads: the next unload returns that filament to the MMU. | Workstream 9, only if D8 is "fix". |
| D10 | Do writes to multi-tool models (BF14) cover the XL as well as INDX? | Yes. The job already writes per tool, so the XL needs only its refusal lifted and its presets compared. The README keeps the XL as untested. | Workstream 7. |
| D11 | Mark these Accepted: BF6, BF11 (once the F12 trace confirms the 8 mm), BF13, BF22? | Yes for all four. Reasons are under [Findings not planned](#findings-not-planned). | Workstreams 8 and 10. |
| D12 | BF12 and BF25 have no fix the bridge can see. Document them and leave them Open? | Yes. Workstream 5 adds a hint for BF25, and workstream 8 documents BF12. | Workstreams 5 and 8. |

## Goals and non-goals

The goal: for every finding, the bridge does what the finding says it
should, or the finding is Accepted, or the README says what the bridge
does and why. Each fix flips the test that pins today's behavior and
updates the finding's Status and its scenario rows.

Not in scope:

- **Changing what's copied from the firmware to make a fix pass.** The
  simulator's behavior and the tables copied from the firmware follow the
  firmware source and the traces, so a fix doesn't change them. If a fix
  shows one is wrong, that's a separate change, made on firmware or trace
  evidence first. The checks in `firmware/compare.py` of the bridge's own
  tables, such as `PRINTER_MODELS` and `PRESETS`, change with the bridge.
- **Anything the findings don't cover**, such as Q12 or a websocket
  listener for Spoolman.
- **Firmware changes.** The bridge reads what the firmware fork serves
  today. Findings that only a new firmware reading could fix are under
  [Findings not planned](#findings-not-planned).

## Status

Update a workstream's row when you start it and when it merges. Each
finding has one owner, the workstream that sets its Status in
bridge-findings.md. A workstream that only touches a finding owned
elsewhere names it under "Also touches".

| # | Workstream | Owns | Also touches | Stage | Size | Waits on | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | [The power-panic window](#1-the-power-panic-window) | BF26 | | 1 | Small | D3 | Not started |
| 2 | [A sync job at a question](#2-a-sync-job-at-a-question) | BF5 | BF4, BF7, BF18 | 1 | Medium | Nothing | Not started |
| 3 | [The custom preamble](#3-the-custom-preamble) | BF2, BF3, BF4 | | 1 | Small | D4 | Not started |
| 4 | [Print start](#4-print-start) | BF10, BF23, BF27 | | 1 | Medium | D5; sitting D confirms | Not started |
| 5 | [Usage on an empty tool](#5-usage-on-an-empty-tool) | BF24, BF25 | | 1 | Medium | D6; sittings C and D confirm | Not started |
| 6 | [The auto-retract](#6-the-auto-retract) | BF9 | | 1 | Small | D7; sittings A and B | Not started |
| 7 | [Writes to toolchangers](#7-writes-to-toolchangers) | BF14, BF1, BF8, BF7 | BF26 | 2 | Medium | D4, D10; workstreams 2 and 3 | Not started |
| 8 | [Toolchanger swaps](#8-toolchanger-swaps) | BF12, BF13 | | 2 | Small | D11, D12 | Not started |
| 9 | [MMU printers](#9-mmu-printers) | BF20, BF15 to BF19, BF28, BF29 | | 4 | Medium | D8 | Not started |
| 10 | [Housekeeping](#10-housekeeping) | BF6, BF11, BF21, BF22 | | 5 | Small | D11; F12 trace for BF11 | Not started |

The sizes rest on reading the code each workstream changes and counting
the pins it flips: small is one function or a default and its tests,
medium is a new input or condition through `account()` or
`PrinterBridge`, or a feature spread over several modules.

## The workstreams

### 1. The power-panic window

- **Findings**: BF26, stage 1.
- **Problem**: After a power cut the printer reads `IDLE` with no job for
  at least 1.5 s before the resume. A write that waited for the print runs
  there, and the print never resumes.
- **Approach (proposal)**: Under D3's recommendation, raise the default
  `sync_delay` in `config.py` (`PrinterConfig` and `_printer_settings`)
  to a positive value no greater than `poll_idle`. `_idle_since` in
  `PrinterBridge` starts as `None` and goes back to `None` on every failed
  read, and `sync_printer` holds while the printer has been idle for less
  than `sync_delay`. So the first idle poll after a boot the bridge saw as
  an outage, or after the bridge starts, never runs a job.
  - Each boot opens its own window, and a second cut during the resume
    keeps the print, so two cuts give two windows (F12, P12). The delay
    alone misses this sequence: a poll lands in the first window, reads
    `IDLE`, starts the clock and holds the job. Because that read was
    idle, the next poll comes `poll_idle` later. In between, the resume
    runs and a second cut reboots the printer, and the next poll lands in
    the second window with the printer idle for longer than `sync_delay`,
    so the job runs. No read in between saw the printer busy or failed.
    Two cuts with no poll between them are safe already: the poll before
    them read the print as busy, so `_note_state` cleared the clock.
  - To close the gap, start the clock again when `read()` sees a new
    `boot_id`. The natural place is the existing
    `self._model_boot != reading.boot_id` branch in `read()`, which runs
    once per boot, and again on later polls while the model can't be
    read, when writes are off anyway. How the reset is written sets the
    cost. Clearing `_idle_since` holds the job at that poll but starts the
    clock only at the next one, because `sync_printer` returns before its
    own `_note_state`: a write after any boot then waits two idle polls.
    Setting `_idle_since` to the current time while the printer reads
    idle makes it one. Both are safe: choose one on purpose.
  - If D3 adds the guard, it goes in `sync_printer` and `read()`, and
    needs the state of the last read before the boot, which `read()`
    overwrites today.
- **Needs**: D3. The F12 trace (sitting C) confirms that one poll covers
  the window.
- **User and saved state**: README, under "Writing to the printer", and
  `config.example.toml` give the new default and say that 0 runs a write
  in the power-panic window. No saved state.
- **Tests**: most write tests expect a job at the first idle poll, so
  `tests/harness.py` and `integration/conftest.py` set `sync_delay = 0`
  unless a test asks for more. `test_a_sync_job_in_the_window_drops_the_resume`
  in `tests/test_power_panic.py` then runs with the default and flips: the
  print resumes and the write runs after it. The default's assertion in
  `tests/test_app.py` changes. P12 and W2 drop the BF26 sentence, and W2
  gains a `test_w2_...` integration test for the window. Add a unit test
  for the sequence above, beside `test_a_second_cut_during_the_resume` in
  `tests/test_power_panic.py`: poll in the first window, let the resume
  run and cut again with no poll between, then poll in the second window
  after `sync_delay` has passed. A test with no poll between the two cuts
  passes without the reset, so it doesn't show the fix. The MMU variant
  in `tests/test_mmu.py` follows workstream 9.
- **Done when**: with the default config, no sync job uploads between a
  boot and the resume of a print, a second cut's boot included.
- **Overlap**: `config.py` (with workstream 3), `read()` in `app.py` (with
  workstream 9), and the fixtures in
  `tests/harness.py` and `integration/conftest.py`, which every workstream's
  tests use.

### 2. A sync job at a question

- **Findings**: owns BF5, stage 1. Also gives BF4's, BF7's and BF18's
  outcome a flag, but workstreams 3, 7 and 9 close those findings.
- **Problem**: When the printer asks about a sync job, the bridge waits in
  `ATTENTION` with no flag and no limit. A declined job ends `IDLE`, the
  printer still differs, and the bridge uploads again, holding the printer
  at a second question.
- **Approach (proposal)**: The bridge already knows its own job by name
  (`_user_job_id`), so a read of `ATTENTION` with the sync job is "the sync
  job waits at a question". Raise a condition on the spools being written
  (logged once, flagged), saying the printer is asking about the bridge's
  job. If that job then ends with the printer still different, flag the
  spool at once with that cause, and don't upload again until the spool,
  the printer's filament or the write changes. `WriteDidntTake` in
  `conditions.py` and `WriteState.attempts` in `printer_sync.py` may carry
  this with a field each; check before adding new ones. The bridge can't
  read why the printer asked: the flag says to check the printer.
- **Needs**: Nothing. Merge it before workstream 7, which makes BF1 and
  BF7 reachable.
- **Why it still matters after workstream 3**: once `gcode_preamble` is
  gone (D4) and MMU writes are off (D8), no configuration found raises a
  question about the bridge's job on a single-tool printer. The
  workstream then guards INDX, where an uncalibrated nozzle cleaner
  refuses every job (BF7) and the lock line has to be right (BF1), and
  any check a later firmware adds. If D4 keeps the preamble, it guards
  BF3's question on the Core One L today.
- **User and saved state**: README, under "Writing to the printer", says
  what the flag means. `WriteState` is saved in the database: change it in
  place.
- **Tests**: no test declines a sync job today. Don't build the tests on
  `gcode_preamble`, which workstream 3 may remove. Patch
  `printer_sync.default_preamble` in the test to leave out the
  `M862.6 P"Input shaper"` line: the simulator's preview then asks under
  the default `checks` and refuses under `abort` (F27, as
  `test_matching_preamble_for_other_model_writes` shows for a custom
  preamble today). Answer with `cancel` and poll. W5 and W6 change, and
  W5 gains a `test_w5_...` integration test for a declined job. BF18's
  pin, `test_a_sync_job_on_a_loaded_mmu_waits_on_the_unload_question`,
  gains the flag, unless workstream 9 turns MMU writes off first.
- **Done when**: a declined or refused sync job flags its spool with the
  cause after one job, and a job waiting at a question is flagged while it
  waits.
- **Overlap**: `sync_printer` and `_classify_mismatch` in `app.py`,
  `conditions.py`, `printer_sync.WriteState`. Collides with workstreams 3,
  7 and 9 in `app.py`.

### 3. The custom preamble

- **Findings**: owns BF2, BF3 and BF4, stage 1.
- **Problem**: `model_problem` refuses a preamble the printer would accept
  (BF2) and accepts one the printer asks about (BF3). The printer's
  check settings then decide whether the job waits or is refused (BF4).
- **Approach (proposal)**: Under D4's recommendation, remove
  `gcode_preamble`: its parsing in `config.py`, the `preamble` argument of
  `model_problem` and `preamble_model` in `printer_sync.py`, and
  `_preamble` in `app.py`, so `_check_model` and the `build_job` call in
  `sync_printer` use `default_preamble` alone. That passes every check on
  the models the bridge writes to, so BF2 and BF3 have nothing left to act
  on, and BF4 needs a preamble that fails a check. Close all three. If D4
  keeps the setting instead: accept the printer's compatibility group
  (BF2), which needs a copy of the firmware's group table in
  `printer_sync.py` and a comparison for it in `firmware/compare.py`;
  require the `M862.6 P"Input shaper"` line (BF3); and say in the README
  that a failing check waits or is refused depending on the printer's
  settings (BF4). Close BF4 with that text and workstream 2's flag.
- **Needs**: D4.
- **User and saved state**: README ("Requirements for a write") and
  `config.example.toml` lose the setting, or gain the rules. No saved
  state.
- **Tests**: `test_matching_preamble_for_other_model_writes` in
  `tests/test_writes.py` and `test_w1_custom_preamble_is_written` in
  `integration/test_configs.py` go with the setting, or flip to "no
  question". The preamble cases of `test_w10_model_the_bridge_cannot_write`
  in `integration/test_writes.py` and of the W10 tests in
  `tests/test_writes.py` go too. W1 and W10 change.
- **Done when**: no configuration lets the bridge upload a job that the
  printer asks about under its default settings, on a model the bridge
  writes to.
- **Overlap**: `printer_sync.py` (`model_problem`, `default_preamble`)
  with workstream 7; `_preamble`, `_check_model` and `sync_printer` in
  `app.py` with workstreams 2, 7 and 9; `config.py` with workstream 1.

### 4. Print start

- **Findings**: owns BF10, BF23 and BF27, stage 1.
- **Problem**: `print_start` in `accounting.py` takes a job's first
  counter rise as the print's start (Q4). A ramming at a cancel (BF10), a
  load at the preview with no poll during it (BF23), or a filament
  change's ramming before the tool's first extrusion (BF27) is that rise.
  The bridge then closes an open swap as assumed, flags the spool, and
  stages a Spoolman change that should have confirmed the swap.
- **Approach (proposal)**: Three changes in `account()`, each to verify:
  - For BF27: an unload in a job that's still pending promotes the job to
    started without the start's effects. The unload already supersedes
    the open swap and commits any staging, so the swap the unload opens
    stays open, as P2 and P3 say for a started print.
  - For BF23: a tool that reported no material at the poll that set
    `pending_at`, and reports one now, loaded in between. Take
    `pending_at` again from the current counter instead of starting the
    print. `ToolState` has `pending_job` and `pending_at` but nothing
    about the material, so it needs a field for "empty when pending".
  - For BF10: a ramming counts once, as the job ends. Either require the
    rise past `pending_at` to exceed what a ramming counts (about 22 mm
    by the source; F26 and F5 measure it), or require a rise at two polls
    of the job. Pick one and say why in Q4.
- **Needs**: D5. The F28 trace (sitting D) confirms BF23 on the printer,
  and F26 and F5 (sittings A and B) the size of a ramming. Build against
  the simulator now; if a trace disagrees, the simulator changes first,
  then the fix.
- **User and saved state**: README, "How usage is attributed", says what
  starts a print. One new `ToolState` field, saved in place.
- **Tests**: `test_a_change_on_a_tool_the_print_has_not_used_looks_like_its_print_start`
  in `tests/test_toolchanger_prints.py` flips. BF10 and BF23 have no kept
  test: add them from their "To reproduce" steps, with the auto-retract on
  (BF22). P8 gains `test_p8_...` integration tests for the cancel ramming
  and the preview load. Q4, P8 and T10 change.
- **Done when**: none of the three paths records `swap_assumed`, and every
  existing print-start test still passes.
- **Overlap**: `accounting.py` (`print_start`, `unload`, `ToolState`) and
  `tests/test_accounting.py`. Collides with workstream 5: run them one
  after another, or as one stack.

### 5. Usage on an empty tool

- **Findings**: owns BF24 and BF25, stage 1. BF25 gets a hint only and
  stays Open (D12).
- **Problem**: Usage counted while a tool reports no material is held and
  charged to the next spool loaded there (C7). That's right for a load's
  feed before the type appears, and wrong for a print that runs through an
  empty tool: after the sensors are turned off (F29), after a power cut
  with the filament pulled out (F12), or on a toolchanger past the
  tools-mapping screen.
- **Approach (proposal)**: `usage()` in `accounting.py` tells the two
  apart. Usage counted with no material while a user's job is printing is
  reported lost, as an event, and `charged` moves past it; usage counted
  with no job stays held, as now. `account()` gets the job only as
  `job_id`, so it needs the printer's state as well, passed from
  `PrinterBridge.process`. Check F8 and F36 first: a filament change's
  reload reads `ATTENTION`, and if its feed counts before the type comes
  back, the rule has to keep that feed. The same event raises a condition
  that says the filament sensor may be off, which is BF25's only signal;
  a power cut gives it too, so the wording allows for both.
- **Needs**: D6, D12. Sitting D's F29 trace confirms the single-tool path
  and F12 the power-cut path.
- **User and saved state**: README, "How usage is attributed", changes the
  held-usage rule. No saved state expected.
- **Tests**: `test_a_print_through_an_empty_tool_holds_its_usage`,
  `test_an_unload_in_the_window_then_printing_on_an_empty_tool` and
  `test_a_cut_while_a_runout_waits_for_the_reload` flip: the load is
  charged its own count only. BF24's rows C7, P12 and T2 change, and C7
  gains a `test_c7_...` integration test for a print through an empty
  tool. BF25's rows P2, S2 and T4 don't change: they describe the sensors
  on, and with them off the bridge still sees no unload. C7 names the new
  hint instead.
- **Done when**: no spool is charged for usage counted during a print on
  a tool with no material, and S1, S2 and the P2 reload charge as before.
- **Overlap**: `accounting.py` (`usage`, `account()`'s signature) and
  `tests/test_accounting.py`'s checking wrapper; `process` in `app.py`;
  `conditions.py`. Collides with workstream 4.

### 6. The auto-retract

- **Findings**: owns BF9, stage 1.
- **Problem**: A Nextruder's auto-retract counts about 30 mm commanded,
  28.5 mm counted, that never leaves the nozzle, after every idle load and
  print end.
- **Approach (proposal)**: Under D7's recommendation, no code: README,
  under "Charging and flags", states the over-count, and BF9 becomes
  Accepted. If D7 chooses a correction, it's a new design: the bridge
  would subtract per model at each print end and idle load, without
  knowing whether the auto-retract is on or the filament is flexible.
- **Needs**: D7, and the F3 and F5 traces.
- **User and saved state**: README only.
- **Tests**: none flip under D7's recommendation. `test_family_story`
  pins the count.
- **Done when**: BF9 is Accepted and the README gives the amount, or the
  correction is designed and planned again.
- **Overlap**: README only.

### 7. Writes to toolchangers

- **Findings**: owns BF14, BF1, BF8 and BF7, stage 2. Extends BF26's
  protection to INDX and the XL, which workstream 1's default covers
  with no model check.
- **Problem**: `model_problem` refuses every multi-tool model, so after
  the INDX upgrade Ross's printer gets no writes. Lifting the refusal makes
  three more findings reachable: the INDX lock question (BF1), INDX's
  presets (BF8), and an uncalibrated nozzle cleaner refusing every job
  (BF7).
- **What exists**: the job already writes per tool.
  `WrittenFilament.lines(tool)` ends each `M865` with `L<tool>` (F22);
  `filament_for` sets a UUID only on `UUID_TOOL`, and `_uuid_tool` only
  on a model that keeps UUIDs, which INDX and the XL don't (F21);
  `build_job` and `sync_printer` take every tool in `plan.writes`; and the
  saved writes are keyed by printer and tool. `_tag_tool` limits Q11 to
  tool 0 of a model with a tag reader, which INDX and the XL aren't.
- **Approach (proposal)**: What remains:
  - Lift `_MULTI_TOOL` from the INDX, XL and XLP rows of `PRINTER_MODELS`
    (D10 for the XL).
  - Add the INDX lock line, `M862.6 P"INDX lock"`, to `default_preamble`
    for the INDX models, from a new `PrinterModel` field (BF1).
  - Give INDX its PC and FLEX presets (BF8). `PRESETS` is one table, read
    by `preset_for`, `temperatures` (and through it
    `drift.differences`) and `filament_for`, so per-model presets mean
    passing the model through those. `script/firmware-check` compares
    `PRESETS` with every model the bridge writes to, so it fails as soon
    as the refusal lifts, until INDX's rows are per model or listed in
    `EXPECTED` in `firmware/compare.py`.
  - Change `firmware/compare.py`'s check of `PRINTER_MODELS`, which today
    reports a difference for every model the bridge writes to whose build
    has more than one tool (`reason is None and build.tools > 1`). It also
    reads `bridge._MULTI_TOOL` into `Copies`, so if lifting the refusal
    leaves that constant unused, remove it from both together, or the
    check fails to start.
  - Name the nozzle-cleaner calibration in the README as a cause of a
    refused job (BF7). Workstream 2's flag is what the user sees.
- **Needs**: D10; D4, since a kept `gcode_preamble` would need per-model
  rules; workstream 2, for BF7's flag; and workstream 3, which changes the
  same code. It has to merge before the INDX upgrade.
- **User and saved state**: README's model table and "Writing to the
  printer". No saved state.
- **Tests**: `test_t6_writes_on_a_multi_tool_model_are_refused` in
  `integration/test_tools.py` becomes a write test (`test_t6_...`), and
  the INDX and XL cases of `test_family_story` flip. T6, W1 and W10
  change. `test_the_bridge_default_job_asks_nothing_on_the_models_it_writes_to`
  in `simulator/tests/test_model.py` asserts the bridge's list of
  writable models, so its list grows with the table: that's a change to
  what it checks of the bridge, not to the simulator's behavior. BF1's printer side
  is `test_indx_asks_about_the_lock_under_any_setting`: the bridge's job
  must not meet it.
- **Done when**: a write lands on every tool of a Core One L INDX and a
  five-tool XL with no question, and `script/firmware-check` passes.
- **Overlap**: `PRINTER_MODELS`, `default_preamble` and `PRESETS` in
  `printer_sync.py`, with workstream 3; `_filament` and `_check_model` in
  `app.py`, with workstreams 2, 3 and 9; `drift.py`; `firmware/compare.py`.
  Start it after workstreams 2 and 3 merge.

### 8. Toolchanger swaps

- **Findings**: owns BF12 and BF13, stage 2.
- **Problem**: A hand swap of a docked tool counts no unload (BF12). A
  spool join counts an unload on the old tool mid-print, which the bridge
  takes as a runout (BF13).
- **Approach (proposal)**: Documentation only. For BF12, README, under
  "How usage is attributed", tells a toolchanger's user to unload from the
  menu or to claim, since nothing the bridge reads shows the swap. For
  BF13, the join's unload does leave the old tool empty, and M5a's flag
  on a spool that ran out with weight left is the flag's purpose, so the
  current behavior stands (D11).
- **Needs**: D11, D12.
- **User and saved state**: README only.
- **Tests**: the pins in `tests/test_toolchanger_prints.py` stay, with
  their comments changed from "a known fault" to the accepted behavior.
  T8 and T9 change status from Gap.
- **Done when**: BF13 is Accepted, and BF12's README text is in.
- **Overlap**: README and `tests/test_toolchanger_prints.py`'s comments,
  which workstream 4 also edits. If D11 rejects BF13's acceptance, it's
  `unload` in `accounting.py` and collides with workstreams 4 and 5.

### 9. MMU printers

- **Findings**: owns BF20, BF15, BF16, BF17, BF18, BF19, BF28 and BF29,
  stage 4.
- **Problem**: The bridge takes an MMU printer for a single-tool one. Its
  swaps are invisible (BF15), tool changes over-charge (BF16), a boot
  charges the wrong slot or none (BF17, BF28, BF29), and writes wait on
  the unload question (BF18) or skip UUIDs (BF19).
- **Approach (proposal)**: Under D8's recommendation, a new `info()` in
  `prusalink.py` reads `/api/v1/info`'s `mmu`. That value is the MMU's
  setting, `mmu2_enabled`, not the MMU's state (F38, citing
  `basic_gets.cpp:229-247`), so it's true while the MMU connects and
  doesn't depend on whether the MMU is up, the MK4 family's bootloader
  phase included. `PrinterBridge.read()` reads it with the model, at each
  boot, and also at each poll, because `M709` switches the MMU with no
  reboot. While it's on, the bridge charges and writes nothing, logged
  once. When it goes off, each tool takes a new baseline, as B1 does.
  Check how today's `NoFilamentEndpoint` path resumes: without a new
  baseline, the count while the MMU was on would be charged at once. That
  also decides BF28: switching off counts an unload on slot 0, which the
  baseline absorbs.
- **Needs**: D8, and D9 only if D8 is "fix".
- **User and saved state**: README's requirements and model table say the
  MMU isn't supported. No saved state beyond the baseline.
- **Tests**: most of `tests/test_mmu.py` and the MMU cases of
  `test_family_story` flip to "charges nothing", and
  `test_t12_two_color_mmu_print` becomes a test that an MMU print charges
  nothing. T12 to T17 change; BF15 to BF19, BF28 and BF29 close as
  covered by the refusal. Don't remove the simulator's MMU: it stays the
  test bed if support is ever wanted.
- **Done when**: with the MMU on, nothing is charged or written; with it
  off, the printer is a single-tool printer from a new baseline.
- **Overlap**: `prusalink.py` (with workstream 10), `read()` and
  `_check_model` in `app.py` (with workstreams 1, 2, 3 and 7).

### 10. Housekeeping

- **Findings**: owns BF6, BF11, BF21 and BF22, stage 5 (BF11 sits in
  stage 1).
- **Approach (proposal)**: Remove `READY` from `IDLE_STATES` in
  `prusalink.py` (BF21): it's never matched, and a dead entry misleads
  the next reader. Mark BF6, BF11 and BF22 Accepted (D11). P12 already
  says the bridge charges each resume's feed back, a second cut's
  included. For BF11, once the F12 trace confirms the 8 mm, P12 also says
  that's where the feed belongs, as "Bridge follow-ups found here" in
  printer-behaviors.md asks.
- **Needs**: D11; the F12 trace for BF11.
- **Tests**: none flip.
- **Done when**: the four findings are fixed or Accepted.
- **Overlap**: `prusalink.py`, with workstream 9: one line, so either
  order works.

## Order and parallelism

Start what protects the Core One L and its data first. Until workstream 1
ships, a positive `sync_delay` on your printer covers BF26.

| When | Workstreams | Why |
| --- | --- | --- |
| Now, in parallel | 1, 4, 10, 8 | Different files: `config.py`, one line of `read()` and the test fixtures; `accounting.py`; one line of `prusalink.py`; the README. Workstream 1 needs D3. |
| Now, one after another | 3, then 2 | Both change `app.py` around `sync_printer`. Workstream 3 also shares `config.py` with workstream 1, a small textual collision. Under D4's recommendation, 3 first: it removes BF3's question from the Core One L at once. |
| After 4 | 5 | Both change `accounting.py` and `account()`. Traces confirm them later. |
| After sittings A and B | 6 | Needs the F3 and F5 traces. |
| After 2 and 3 merge, before the INDX upgrade | 7 | Shares `printer_sync.py` with 3 and `app.py` with 2, 3 and 9. |
| After 7, or before it if D8 comes first | 9 | Shares `read()` and `_check_model` with 1 to 3 and 7. |

## How to work on a workstream

Read in this order: AGENTS.md; this plan's decisions and your workstream;
each finding in bridge-findings.md; the scenario rows and Q rows they
name in scenarios.md; the F rows in printer-behaviors.md; then the code
and the pins your workstream names.

These rules bind, beyond AGENTS.md:

- Set your workstream's Status here to "In progress" in your first PR,
  and to "Merged" with the commit when it merges.
- Flip a finding's pin test in the same change that fixes it, and in the
  same change set the finding's Status in bridge-findings.md and update
  its scenario rows. Only the workstream that owns a finding sets its
  Status. A row that becomes end to end needs an integration test named
  `test_<row>_...`, as AGENTS.md says.
- If a decision you need isn't made, stop and ask Ross: don't pick an
  answer and build on it.
- Don't change the simulator's behavior or a table copied from the
  firmware to make a fix pass. If the fix shows one is wrong, make that a
  separate change, with the firmware source or a trace as its evidence.
  The checks in `firmware/compare.py` of the bridge's own tables change
  with the bridge, in the same change.
- Report a problem through a condition, logged once when it starts and
  once when it ends, never on every poll.
- Update README.md in the same change when a user sees a difference.
- Change saved state in place, with `SCHEMA_VERSION` kept at 1. Say in the
  PR what a deployed bridge needs, if anything.
- Keep the unit suite above its coverage floor, and raise the floor when
  coverage rises.
- Start your branch names with your session's name, such as
  `ar3-print-start`, and put related PRs up as a `gh stack`.
- Send each PR for review through Ross's reviewer session.

## Findings not planned

These get no code. Each is documented, or Accepted under D11.

| Finding | Why |
| --- | --- |
| BF6 | The bridge only writes names inside the firmware's rules and preset names `firmware-check` compares. W5's flag catches the rest. |
| BF7 | The cause can't be read. Workstream 2's flag and workstream 7's README text are all the bridge can do. |
| BF11 | The lost usage needs the counters in the firmware's power-panic data. The resume's feed is charged where it belongs, and so is a second cut's, which leaves the nozzle because that cut doesn't retract. |
| BF12 | No reading shows a docked tool emptied by hand. Only a new firmware field, such as each tool's sensor state, would. |
| BF13 | The charges follow the filament, and the old tool's swap and flag are what an emptied tool needs. |
| BF22 | Changing the harness's default flips many unrelated tests. Workstream 4's new tests turn the auto-retract on themselves. |
| BF25 | No reading says the sensors are off. Workstream 5's hint covers the case the bridge can see. |

Under D8's recommendation, BF15 to BF19, BF28 and BF29 close through the
refusal rather than a fix. If D8 is "fix", BF15 and BF17 still have no
path: nothing the bridge reads shows an MMU swap or which slot a resumed
print uses.

## Conflicts between fixes

- Workstream 5 against S1 and S2: the hold during a load is deliberate.
  The rule that drops printed-empty usage mustn't drop a load's feed or a
  reload's.
- Workstream 4 against short prints: a threshold on the first extrusion
  means a print that extrudes less than a ramming never starts, so its
  swap stays open, as P3 says.
- Workstream 1's default against every write: a write after a menu load
  waits one more idle poll. A user who sets 0 to get it back loses BF26's
  protection.
- Workstream 3 against workstream 2's tests: removing `gcode_preamble`
  removes the only way a configuration makes the bridge's job meet a
  question on a single-tool printer, and D8 removes the MMU's. Workstream
  2's tests patch `default_preamble` instead, so they don't depend on
  either.
- Workstream 7 makes BF1, BF7 and BF8 reachable, and makes BF26 reach INDX
  and the XL. Workstream 1's default covers them, since it doesn't depend
  on the model.
- Workstream 3's group table, if D4 keeps the preamble, must accept only
  names in the printer's own group: names in compatibility mode still ask
  (BF4).
- A BF9 correction, if D7 chooses one, changes the counts workstream 4's
  threshold is measured against.

## Open questions

Left to the work:

- Whether one idle poll always covers the power-panic window. The F12
  trace decides (workstream 1).
- Whether a filament change's reload counts its feed before the type
  comes back (workstream 5). F8 and F36 say what the source shows.
- Which rule excludes a cancel's ramming: a threshold or a rise at two
  polls (workstream 4).
- Whether `/api/v1/info` is cheap enough to read at every poll, or only
  at a boot and when the tool count changes (workstream 9).
