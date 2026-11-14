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
| D1 | In what order do the workstreams go? | As [Order and parallelism](#order-and-parallelism) lists: what protects the printer first (workstreams 1 to 3), then charging (4 to 6), then the toolchangers before the INDX upgrade (7 and 8), then the MMU (9). This breaks the stage table's rule that, within stage 1, charges come before writes: BF26 loses a print, and the three write fixes are small. | The order only. |
| D2 | Does stage 1 ship alone, merged and deployed to the Core One L before stage 2 or 4 work merges? | Yes. Workstream 7 can be built at the same time, but it merges after workstreams 1 and 2, because all three change `sync_printer` in `app.py`. | Merging workstreams 7 and 9. |
| D3 | How does BF26 get fixed: a positive `sync_delay` default, a guard after a boot that interrupted a print, or wait for the F12 trace? | The guard (workstream 1), built now. It protects every config, including one that sets `sync_delay = 0`, and doesn't delay ordinary writes. A positive default delays every write by a poll and changes the default most write tests rely on. Until the guard is deployed, set `sync_delay` above 0 on your own printer: any positive value under `poll_idle` holds the first idle poll after a boot. The F12 trace then confirms that the window ends within one poll. | Workstream 1. |
| D4 | Keep `gcode_preamble`, or remove it? | Remove it. Its only effects found are BF2, BF3 and BF4, and the default preamble passes every check on every model the bridge writes to. Pre-release, there's no compatibility to keep. Keep it only if you use it, and then validate it (workstream 3 lists both). | Workstream 3, and the INDX lock line in workstream 7. |
| D5 | Amend Q4 so that a ramming or a load isn't a print's first extrusion? | Yes. Q4's intent, that a cancelled print changes nothing, holds; only what counts as the first extrusion narrows. | Workstream 4. |
| D6 | Amend C7: usage counted on a tool with no filament while a user's print runs is reported lost, not charged to the next spool? | Yes. That filament came from no spool. The hold during a load, which S1 and S2 rely on, stays as it is. | Workstream 5. |
| D7 | BF9: accept and document the auto-retract's count, or correct it in the bridge? | Wait for the F3 and F5 traces (sittings A and B), then accept and document it if they show about 28.5 mm per auto-retract. A correction needs the auto-retract setting and the filament's flexibility, which the bridge can't read, and the error is about 0.09 g each time. | Workstream 6. |
| D8 | MMU3 printers, which you'll never own: fix the findings, detect the MMU and refuse it, or document it? | Detect it from `/api/v1/info`'s `mmu`, and while it's on, charge nothing and write nothing, logged once, as for a printer with no counters. BF16 alone over-charges about 0.63 g per tool change, so a multi-color print's charges are far off, and BF15 and BF17 have no fix the bridge can see. The middle option, writes off and charging on with the errors documented, is cheaper still and keeps those errors. | Workstream 9. |
| D9 | BF16's open question: if the MMU is fixed (D8), does the target charge count the slicer's 17 mm and 32 mm loads? | Moot under the recommended D8. If you choose to fix, aim at what leaves the spool for good, without the loads: the next unload returns that filament to the MMU. | Workstream 9, only if D8 is "fix". |
| D10 | Does the per-tool write (BF14) cover the XL as well as INDX? | Yes. It's the same code and the simulator covers both. The README keeps the XL as untested. | Workstream 7. |
| D11 | Mark these Accepted: BF6, BF11 (once the F12 trace confirms the 8 mm), BF13, BF22? | Yes for all four. Reasons are under [Findings not planned](#findings-not-planned). | Workstreams 8 and 10. |
| D12 | BF12 and BF25 have no fix the bridge can see. Document them and leave them Open? | Yes. Workstream 5 adds a hint for BF25, and workstream 8 documents BF12. | Workstreams 5 and 8. |

## Goals and non-goals

The goal: for every finding, the bridge does what the finding says it
should, or the finding is Accepted, or the README says what the bridge
does and why. Each fix flips the test that pins today's behavior and
updates the finding's Status and its scenario rows.

Not in scope:

- **Changing the simulator or `firmware/` to make a fix pass.** The
  simulator follows the firmware source and the traces. If a fix shows the
  simulator is wrong, that's a separate change, made on firmware or trace
  evidence first.
- **Anything the findings don't cover**, such as Q12 or a websocket
  listener for Spoolman.
- **Firmware changes.** The bridge reads what the firmware fork serves
  today. Findings that only a new firmware reading could fix are under
  [Findings not planned](#findings-not-planned).

## Status

Update a workstream's row when you start it and when it merges.

| # | Workstream | Findings | Stage | Size | Waits on | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | [The power-panic window](#1-the-power-panic-window) | BF26 | 1 | Small | D3 | Not started |
| 2 | [A sync job at a question](#2-a-sync-job-at-a-question) | BF5, BF4, BF18 | 1 | Medium | Nothing | Not started |
| 3 | [The custom preamble](#3-the-custom-preamble) | BF2, BF3, BF4 | 1 | Small | D4 | Not started |
| 4 | [Print start](#4-print-start) | BF10, BF23, BF27 | 1 | Medium | D5; sitting D confirms | Not started |
| 5 | [Usage on an empty tool](#5-usage-on-an-empty-tool) | BF24, BF25 | 1 | Medium | D6; sittings C and D confirm | Not started |
| 6 | [The auto-retract](#6-the-auto-retract) | BF9 | 1 | Small | D7; sittings A and B | Not started |
| 7 | [Writes to toolchangers](#7-writes-to-toolchangers) | BF14, BF1, BF8, BF7 | 2 | Large | D2, D4, D10; workstream 2 | Not started |
| 8 | [Toolchanger swaps](#8-toolchanger-swaps) | BF12, BF13 | 2 | Small | D11, D12 | Not started |
| 9 | [MMU printers](#9-mmu-printers) | BF20, BF15 to BF19, BF28, BF29 | 4 | Medium | D8 | Not started |
| 10 | [Housekeeping](#10-housekeeping) | BF6, BF11, BF21, BF22 | 5 | Small | D11; F12 trace for BF11 | Not started |

The sizes rest on reading the code each workstream changes and counting
the pins it flips: small is one function and its tests, medium is a new
input or condition through `account()` or `PrinterBridge`, and large is a
new feature across several modules.

## The workstreams

### 1. The power-panic window

- **Findings**: BF26, stage 1.
- **Problem**: After a power cut the printer reads `IDLE` with no job for
  at least 1.5 s before the resume. A write that waited for the print runs
  there, and the print never resumes.
- **Approach (proposal)**: `PrinterBridge.sync_printer` holds the job
  when the printer rebooted while the bridge last saw it busy with a
  user's job. It holds until the printer has stayed idle for a full poll,
  or the resumed job is seen. The bridge has to remember the state of the
  last read before the boot: `_last_state` is overwritten by the first
  read after it, in `read()`. That memory is in-process only, so a bridge
  restarted inside the window isn't guarded; say so in P12.
- **Needs**: D3. The F12 trace (sitting C) confirms that one poll covers
  the window. If it doesn't, the guard's length changes, not its shape.
- **User and saved state**: README, under "Writing to the printer", says
  a write after a power cut waits. No saved state.
- **Tests**: `test_a_sync_job_in_the_window_drops_the_resume` in
  `tests/test_power_panic.py` flips: the print resumes and the write runs
  after it. P12 and W2 drop the BF26 sentence. W2 becomes end to end for
  this case: add `test_w2_...` in `integration/test_prints.py` or
  `test_writes.py`. The MMU variant in `tests/test_mmu.py` follows
  workstream 9.
- **Done when**: no sync job uploads between a boot and the resume of a
  print the bridge saw running, and a write after an ordinary idle boot
  still runs at the first idle poll.
- **Overlap**: `sync_printer` and `read()` in `app.py`, shared with
  workstreams 2, 7 and 9.

### 2. A sync job at a question

- **Findings**: BF5 and the part of BF4 the bridge sees, stage 1; BF18's
  silence, stage 4.
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
  the printer's filament or the write changes. That replaces W5's second
  attempt for this case. The bridge can't read why the printer asked: the
  flag says to check the printer's compatibility settings.
- **Needs**: Nothing. It doesn't need workstream 3: a sync job meets a
  question today through BF3.
- **User and saved state**: README, under "Writing to the printer", says
  what the flag means. The "don't upload again" memory belongs in
  `printer_sync.WriteState`, which the database keeps: change it in place.
- **Tests**: no test declines a sync job today. Add unit tests on the
  BF5 reproduction (a preamble without the input-shaper line, then
  `cancel`, and `checks = "abort"`). W5 and W6 change, and W5's
  integration test gains `test_w5_...` for a declined job. BF18's pin,
  `test_a_sync_job_on_a_loaded_mmu_waits_on_the_unload_question`, gains
  the flag, unless workstream 9 turns MMU writes off first.
- **Done when**: a declined or refused sync job flags its spool with the
  cause after one job, and a job waiting at a question is flagged while it
  waits.
- **Overlap**: `sync_printer` and `_classify_mismatch` in `app.py`,
  `conditions.py`, `printer_sync.WriteState`. Collides with workstreams 1
  and 7.

### 3. The custom preamble

- **Findings**: BF2, BF3, BF4, stage 1.
- **Problem**: `model_problem` refuses a preamble the printer would accept
  (BF2) and accepts one the printer asks about (BF3). The printer's
  check settings then decide whether the job waits or is refused (BF4).
- **Approach (proposal)**: Under D4's recommendation, remove
  `gcode_preamble` from `config.py`, `model_problem` and `_preamble` in
  `app.py`, and always use `default_preamble`, which passes every check
  on the models the bridge writes to. BF2, BF3 and BF4 then have nothing
  left to act on, and BF4 needs only workstream 2's flag. If D4 keeps the
  setting instead: accept the printer's compatibility group (BF2), which
  needs a copy of the firmware's group table in `printer_sync.py` and a
  comparison for it in `firmware/compare.py`; require the
  `M862.6 P"Input shaper"` line (BF3); and say in the README that a
  failing check waits or is refused depending on the printer's settings
  (BF4).
- **Needs**: D4.
- **User and saved state**: README ("Requirements for a write") and
  `config.example.toml` lose the setting, or gain the rules. No saved
  state.
- **Tests**: `test_matching_preamble_for_other_model_writes` in
  `tests/test_writes.py` and `test_w1_custom_preamble_is_written` in
  `integration/test_configs.py` go with the setting, or flip to "no
  question". W1 and W10 change.
- **Done when**: no configuration lets the bridge upload a job that the
  printer asks about under its default settings, on a model the bridge
  writes to.
- **Overlap**: `printer_sync.py` (`model_problem`, `default_preamble`),
  shared with workstream 7; `config.py`.

### 4. Print start

- **Findings**: BF10, BF23, BF27, stage 1.
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
    print. `ToolState` needs a field for "empty when pending".
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
- **Overlap**: `accounting.py` (`print_start`, `unload`, `ToolState`).
  Collides with workstreams 5 and 8: run them one after another, or as one
  stack.

### 5. Usage on an empty tool

- **Findings**: BF24, stage 1; BF25, stage 1, as a hint only.
- **Problem**: Usage counted while a tool reports no material is held and
  charged to the next spool loaded there (C7). That's right for a load's
  feed before the type appears, and wrong for a print that runs through an
  empty tool: after the sensors are turned off (F29), after a power cut
  with the filament pulled out (F12), or on a toolchanger past the
  tools-mapping screen.
- **Approach (proposal)**: `usage()` in `accounting.py` tells the two
  apart. Usage counted with no material while a user's job is printing is
  reported lost, as an event, and `charged` moves past it; usage counted
  with no job stays held, as now. `account()` gets the job's state today
  only as `job_id`, so it needs the printer's state as well. Check F8 and
  F36 first: a filament change's reload reads `ATTENTION`, and if its feed
  counts before the type comes back, the rule has to keep that feed. The
  same event raises a condition that says the filament sensor may be off,
  which is BF25's only signal; a power cut gives it too, so the wording
  allows for both.
- **Needs**: D6, D12. Sitting D's F29 trace confirms the single-tool path
  and F12 the power-cut path.
- **User and saved state**: README, "How usage is attributed", changes the
  held-usage rule. No saved state expected.
- **Tests**: `test_a_print_through_an_empty_tool_holds_its_usage`,
  `test_an_unload_in_the_window_then_printing_on_an_empty_tool` and
  `test_a_cut_while_a_runout_waits_for_the_reload` flip: the load is
  charged its own count only. C7, P12 and T2 change. C7 gains a
  `test_c7_...` integration test for a print through an empty tool.
- **Done when**: no spool is charged for usage counted during a print on
  a tool with no material, and S1, S2 and the P2 reload charge as before.
- **Overlap**: `accounting.py` (`usage`, `account()`'s signature),
  `app.py` (`process` passes the state), `conditions.py`. Collides with
  workstream 4.

### 6. The auto-retract

- **Findings**: BF9, stage 1.
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
- **Overlap**: none.

### 7. Writes to toolchangers

- **Findings**: BF14, BF1, BF8 and BF7, stage 2.
- **Problem**: `model_problem` refuses every multi-tool model, so after
  the INDX upgrade Ross's printer gets no writes. Building the write makes
  three more findings reachable: the INDX lock question (BF1), INDX's
  presets (BF8), and an uncalibrated nozzle cleaner refusing every job
  (BF7).
- **Approach (proposal)**: `M865 L<n>` per tool with no `M865.1` (F21,
  F22), from `build_job` in `printer_sync.py`. `PrinterModel` gains what
  differs per model: `default_preamble` adds `M862.6 P"INDX lock"` on
  INDX (BF1), and `PRESETS` gains INDX's PC and FLEX preheats (BF8), with
  `firmware/compare.py` comparing the new rows. BF7 can't be read: it
  reaches the user through workstream 2's flag, and the README names the
  calibration as a cause. Read Q11 against INDX, which has no tag reader
  until the firmware adds one.
- **Needs**: D2, D10, workstream 2, and D4's answer, since a kept
  `gcode_preamble` would need per-model rules. It has to merge before the
  INDX upgrade.
- **User and saved state**: README's model table and "Writing to the
  printer". `WriteState` is already per tool: check that its saved form
  holds more than tool 0.
- **Tests**: `test_t6_writes_on_a_multi_tool_model_are_refused` in
  `integration/test_tools.py` becomes a write test (`test_t6_...`), and
  the INDX and XL cases of `test_family_story` flip. T6, W1 and W10
  change. BF1's printer side is
  `test_indx_asks_about_the_lock_under_any_setting`: the bridge's job must
  not meet it.
- **Done when**: a write lands on every tool of a Core One L INDX and a
  five-tool XL with no question, and `script/firmware-check` passes.
- **Overlap**: `printer_sync.py` (with workstream 3), `sync_printer` and
  `_uuid_tool` in `app.py` (with workstreams 1, 2 and 9). The largest
  collision: start it after workstream 2 merges.

### 8. Toolchanger swaps

- **Findings**: BF12 and BF13, stage 2.
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
- **Overlap**: none, unless D11 rejects BF13's acceptance; then it's
  `unload` in `accounting.py` and collides with workstreams 4 and 5.

### 9. MMU printers

- **Findings**: BF20, BF15, BF16, BF17, BF18, BF19, BF28 and BF29, stage 4.
- **Problem**: The bridge takes an MMU printer for a single-tool one. Its
  swaps are invisible (BF15), tool changes over-charge (BF16), a boot
  charges the wrong slot or none (BF17, BF28, BF29), and writes wait on
  the unload question (BF18) or skip UUIDs (BF19).
- **Approach (proposal)**: Under D8's recommendation, a new `info()` in
  `prusalink.py` reads `/api/v1/info`'s `mmu`, which is true while the
  MMU's setting is on, connected or not. `PrinterBridge.read()` reads it
  with the model, at each boot, and also at each poll, because `M709`
  switches the MMU with no reboot. While it's on, the bridge charges and
  writes nothing, logged once. When it goes off, each tool takes a new
  baseline, as B1 does. Check how today's `NoFilamentEndpoint` path
  resumes: without a new baseline, the count while the MMU was on would
  be charged at once. That also decides BF28: switching off counts an
  unload on slot 0, which the baseline absorbs.
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
- **Overlap**: `prusalink.py`, `read()` and `_check_model` in `app.py`.
  Collides lightly with workstreams 1, 2 and 7.

### 10. Housekeeping

- **Findings**: BF6, BF11, BF21 and BF22, stage 5 (BF11 sits in stage 1).
- **Approach (proposal)**: Remove `READY` from `IDLE_STATES` in
  `prusalink.py` (BF21): it's never matched, and a dead entry misleads
  the next reader. Mark BF6, BF11 and BF22 Accepted (D11). For BF11, once
  the F12 trace confirms the 8 mm, P12 says the resume's feed is charged
  to the active spool, as "Bridge follow-ups found here" in
  printer-behaviors.md asks.
- **Needs**: D11; the F12 trace for BF11.
- **Tests**: none flip.
- **Done when**: the four findings are fixed or Accepted.
- **Overlap**: none.

## Order and parallelism

Start what protects the Core One L and its data first. Until workstream 1
is deployed, a positive `sync_delay` on your printer covers BF26.

| When | Workstreams | Why |
| --- | --- | --- |
| Now, in parallel | 1, 3, 10 | Small, and they touch different files. 1 and 3 need D3 and D4. |
| Now, alone in `sync_printer` | 2 | Shares `sync_printer` with 1: merge 1 first, or stack them. |
| Now, one after another | 4, then 5 | Both change `accounting.py` and `account()`. Traces confirm them later. |
| After sittings A and B | 6 | Needs the F3 and F5 traces. |
| After 2 merges, before the INDX upgrade | 7 | Shares `sync_printer` with 1, 2 and 9, and `printer_sync.py` with 3. |
| Any time | 8 | Documentation, unless D11 changes. |
| After 7, or before it if D8 comes first | 9 | Shares `read()` and `sync_printer` with 1, 2 and 7. |

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
  its scenario rows. A row that becomes end to end needs an integration
  test named `test_<row>_...`, as AGENTS.md says.
- If a decision you need isn't made, stop and ask Ross: don't pick an
  answer and build on it.
- Don't change the simulator or `firmware/` to make a fix pass. If the fix
  shows the simulator is wrong, make that a separate change, with the
  firmware source or a trace as its evidence.
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
| BF11 | The lost usage needs the counters in the firmware's power-panic data. The resume's feed is charged where it belongs. |
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
- Workstream 7 makes BF1, BF7 and BF8 reachable, and extends BF26 to INDX
  and the XL: workstream 1 has to cover every model that resumes.
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
