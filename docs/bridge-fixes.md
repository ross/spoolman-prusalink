# Bridge fixes

This is the plan for fixing what [bridge-findings.md](bridge-findings.md)
records: BF1 to BF30, the places where the simulator work showed the bridge
charges the wrong amount or the wrong spool, misses a swap, or fails to
write to the printer. The findings were held until the simulator was done,
so they could be planned as a whole. The plan groups them into ten
workstreams that sessions can take up one at a time, each from a cold
start with only the repository.

Draft, 2026-11-14. The [Status](#status) table says which workstreams
have started. Every approach below is a proposal, read from the code but
not built: the session that takes a workstream checks it against the
code before building on it.

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
| D3 | How does BF26 get fixed: a positive `sync_delay` default, a guard in the bridge after a boot that interrupted a print, or wait for the F12 trace? | Decided: a positive `sync_delay` default, 30 s, and the idle clock starts again at each new `boot_id`. No guard. The code holds a write while the printer has been idle for less than `sync_delay`, and a new `PrinterBridge`, a failed read or a new boot starts that clock again at that poll, so the first idle poll after a boot only starts it, and the write runs one idle poll later. That covers a bridge that restarts in the window, and a second cut during the resume, whose window a poll can reach `poll_idle` after one in the first window. What it misses: a config that sets `sync_delay = 0`, and a window that outlasts a poll, which the F12 trace checks. What it costs: every write waits one more idle poll, up to 60 s by default. | Workstream 1. |
| D4 | Keep `gcode_preamble`, or remove it? | Answered: remove it. Its only effects found were BF2, BF3 and BF4, and the default preamble passes every check on every model the bridge writes to. Pre-release, there's no compatibility to keep. | Workstream 3, and the INDX lock line in workstream 7. |
| D5 | Amend Q4 so that a ramming or a load isn't a print's first extrusion? | Answered: yes, with a threshold. A pending job starts once the tool's counter has risen more than 23 mm, the auto-retract ramming's commanded length, past where the bridge first saw the job. A rise at two polls was rejected: the ramming counts in two moves that two polls can split. A load between the job's first sight and its first extrusion takes the starting point again, and an unload in a job a poll already saw pending starts it without the start's effects. That unload counts as a mid-print unload, with P2's runout flag and tag handling. An unload first seen with the job is a swap before the print. Q4's intent, that a cancelled print changes nothing, holds. | Nothing. |
| D6 | Amend C7: usage counted on a tool with no filament while a user's print runs is reported lost, not charged to the next spool? | Answered: yes. That filament came from no spool, so it's reported lost. The hold during a load, which S1 and S2 rely on, stays as it is. | Nothing. |
| D7 | BF9: accept and document the auto-retract's count, or correct it in the bridge? | Wait for the F3 and F5 traces (sittings A and B), then accept and document it if they show about 28.5 mm per auto-retract. A correction needs the auto-retract setting and the filament's flexibility, which the bridge can't read, and the error is about 0.09 g each time. | Workstream 6. |
| D8 | MMU3 printers, which you'll never own: fix the findings, detect the MMU and refuse it, or document it? | Detect it from `/api/v1/info`'s `mmu`, and while it's on, charge nothing and write nothing, logged once, as for a printer with no counters. BF16 alone over-charges about 0.63 g per tool change, so a multi-color print's charges are far off, and BF15 and BF17 have no fix the bridge can see. The middle option, writes off and charging on with the errors documented, is cheaper still and keeps those errors. | Workstream 9. |
| D9 | BF16's open question: if the MMU is fixed (D8), does the target charge count the slicer's 17 mm and 32 mm loads? | Moot under the recommended D8. If you choose to fix, aim at what leaves the spool for good, without the loads: the next unload returns that filament to the MMU. | Workstream 9, only if D8 is "fix". |
| D10 | Do writes to multi-tool models (BF14) cover the XL as well as INDX? | Yes. The job already writes per tool, so the XL needs only its refusal lifted and its presets compared. The README keeps the XL as untested. | Workstream 7. |
| D11 | Mark these Accepted: BF6, BF11 (once the F12 trace confirms the 8 mm), BF13, BF22? | Answered: BF6 and BF22 are Accepted. BF11 becomes Accepted once the F12 trace confirms the 8 mm. BF13 stays Open: its effect isn't settled, the INDX upgrade will show a spool join on the printer, and other users may have toolchangers too. It's decided with workstream 8. Reasons for the rest are under [Findings not planned](#findings-not-planned). | BF11's acceptance waits on the F12 trace. BF13 waits on workstream 8. |
| D12 | BF12 and BF25 have no fix the bridge can see. Document them and leave them Open? | Answered: yes, both stay Open. Workstream 5 adds a warning for BF25 and the README text for both: BF12's goes in with it rather than with workstream 8. Since then the fork's `2ef028181` serves the sensors setting, `sensors_enabled` (F47), so where the firmware reports it, workstream 5 also warns directly while the sensors are off. Neither change lets the bridge see a hand swap. BF12 is decided with workstream 8. | Nothing. |

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
| 1 | [The power-panic window](#1-the-power-panic-window) | BF26 | | 1 | Small | D3 | Merged, 9df89c8 |
| 2 | [A sync job at a question](#2-a-sync-job-at-a-question) | BF5 | BF4, BF7, BF18 | 1 | Medium | Nothing | Merged, 064d892 |
| 3 | [The custom preamble](#3-the-custom-preamble) | BF2, BF3, BF4 | | 1 | Small | D4 | Merged, 064d892 |
| 4 | [Print start](#4-print-start) | BF10, BF23, BF27 | | 1 | Medium | Sitting D confirms | Merged, a212288 |
| 5 | [Usage on an empty tool](#5-usage-on-an-empty-tool) | BF24, BF25 | | 1 | Medium | Sittings C and D confirm | BF24 merged, 90eaf2c, before workstream 4, which builds on it; BF25's hint and BF12's README text merged, 02d8f8b; BF25's sensors-off warning, where the firmware reports the setting, merged, 5e48b7e |
| 6 | [The auto-retract](#6-the-auto-retract) | BF9 | | 1 | Small | D7; sittings A and B | Not started |
| 7 | [Writes to toolchangers](#7-writes-to-toolchangers) | BF14, BF1, BF8, BF7 | BF26 | 2 | Medium | D10; workstreams 2 and 3 | Not started |
| 8 | [Toolchanger swaps](#8-toolchanger-swaps) | BF12, BF13 | | 2 | Small | BF13's decision, with an INDX | BF12's README text merged, 02d8f8b; BF13 waits on its decision |
| 9 | [MMU printers](#9-mmu-printers) | BF20, BF15 to BF19, BF28, BF29 | | 4 | Medium | D8 | Not started |
| 10 | [Housekeeping](#10-housekeeping) | BF6, BF11, BF21, BF22 | | 5 | Small | F12 trace for BF11 | BF21 merged, a9a09ea; BF6 and BF22 Accepted; BF11 waits on the F12 trace |

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
- **Approach (decided under D3, and built)**: The default `sync_delay` in
  `config.py` (`PrinterConfig` and `_printer_settings`) is `SYNC_DELAY`,
  30 s, no greater than the default `poll_idle`. `_idle_since` in
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
  - To close the gap, `read()` starts the clock again when it sees a new
    `boot_id`. It keeps the boot in its own field, `_idle_boot`, not in
    the `self._model_boot != reading.boot_id` branch, which runs again
    on every poll while the model can't be read. The reset comes before
    `_note_state`, which starts the clock at that same poll while the
    printer reads idle, so a write after a boot waits one idle poll. A
    reset after `_note_state` would make it two.
  - The wait is measured at the poll's read, not when `sync_printer`
    runs. Otherwise the time a poll spends on other printers and on
    Spoolman counts as idle time, and a short delay could pass inside
    the window.
- **Needs**: D3. The F12 trace (sitting C) confirms that one poll covers
  the window.
- **User and saved state**: README, under "Writing to the printer", and
  `config.example.toml` give the new default and say that 0 runs a write
  in the power-panic window. No saved state.
- **Tests**: most write tests expect a job at the first idle poll, so
  `tests/harness.py` and `integration/printers.py` set `sync_delay = 0`
  unless a test asks for more.
  `test_a_write_waits_out_the_window_and_the_print_resumes` in
  `tests/test_power_panic.py` runs with the default: the print resumes
  and the write runs after it. The default's assertion in
  `tests/test_app.py` changed. P12 and W2 dropped the BF26 sentence, and
  `test_w2_write_waits_out_the_power_panic_window` runs the window end to
  end. `test_a_second_cut_after_an_idle_poll_in_the_first_window`, beside
  `test_a_second_cut_during_the_resume`, covers the sequence above: a poll
  in the first window, the resume and a second cut with no poll between,
  then a poll in the second window after `sync_delay` has passed. It fails
  without the reset; a test with no poll between the two cuts doesn't.
  `test_sync_delay_starts_over_at_a_boot_and_waits_one_idle_poll` and
  `test_sync_delay_is_measured_at_the_read` in `tests/test_writes.py`
  cover the reset's place and the read-time measurement. The MMU variant
  in `tests/test_mmu.py` stays at `sync_delay = 0` until workstream 9.
- **Done when**: with the default config, no sync job uploads between a
  boot and the resume of a print, a second cut's boot included.
- **Overlap**: `config.py` (with workstream 3), `read()` in `app.py` (with
  workstream 9), and the fixtures in
  `tests/harness.py` and `integration/printers.py`, which every workstream's
  tests use.

### 2. A sync job at a question

- **Findings**: owns BF5, stage 1. Also gives BF4's, BF7's and BF18's
  outcome a flag, but workstreams 3, 7 and 9 close those findings.
- **Problem**: When the printer asked about a sync job, the bridge waited
  in `ATTENTION` with no flag and no limit. A declined job ended `IDLE`,
  the printer still differed, and the bridge uploaded again, holding the
  printer at a second question.
- **Approach**: `PrinterBridge.read()` knows the bridge's own job by
  name (`_user_job_id`) and marks it in `PrinterStatus.sync_job`, so a
  read of `ATTENTION` with the sync job is "the sync job waits at a
  question". The spools being written get the `WriteAsked` condition
  (logged once, flagged), saying the printer is asking about the bridge's
  job. `WriteState.asked` records that the latest job was seen at the
  question. If that job then ends with the printer still different, the
  spool is flagged at once with that cause (`WriteDidntTake` with
  `asked`), and nothing more is uploaded until a new spool, an unload, or
  a change to the spool in Spoolman since the last write; the same edit
  also restarts a write given up after two jobs. A change on the printer
  alone doesn't restart it. The bridge can't read why the printer asked,
  and a job refused under `abort` also waits for Abort, so a declined job
  and a refused one end the same way: the flag says to check the
  printer's screen and its check settings.
- **Needs**: Nothing. Merge it before workstream 7, which makes BF1 and
  BF7 reachable.
- **Why it still matters after workstream 3**: once `gcode_preamble` is
  gone (D4) and MMU writes are off (D8), no configuration found raises a
  question about the bridge's job on a single-tool printer. The
  workstream then guards INDX, where an uncalibrated nozzle cleaner
  refuses every job (BF7) and the lock line has to be right (BF1), and
  any check a later firmware adds.
- **User and saved state**: README, under "Writing to the printer", says
  what each flag means and what clears it. `WriteState.asked` is saved in
  a new `writes.asked` column, changed in place. A deployed bridge needs
  `ALTER TABLE writes ADD COLUMN asked INTEGER NOT NULL DEFAULT 0`, or a
  new database.
- **Tests**: `tests/test_sync_question.py` patches
  `printer_sync.default_preamble` to leave out the
  `M862.6 P"Input shaper"` line, since `gcode_preamble` is gone (D4): the
  simulator's preview then asks under the default `checks` and refuses
  under `abort` (F27, as the input-shaper case of `test_each_check` in
  `simulator/tests/test_preview.py` shows). It covers the flag while the
  job waits, a decline under `warn` and under `abort`, Print at the
  question, and the retries after an edit or an unload. The integration
  test `test_w5_declined_sync_job_is_flagged` declines a job end to end.
  BF18's pin, `test_a_sync_job_on_a_loaded_mmu_waits_on_the_unload_question`
  in `tests/test_mmu.py`, and
  `test_a_sync_job_in_the_window_on_a_connecting_mmu_waits_on_the_question`
  now assert the flag. W5 and W6 changed.
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
- **Approach**: Remove `gcode_preamble` (D4): its parsing in
  `config.py`, the `preamble` argument of `model_problem` and
  `preamble_model` in `printer_sync.py`, and `_preamble` in `app.py`, so
  `_check_model` and the `build_job` call in `sync_printer` use
  `default_preamble` alone. That passes every check on the models the
  bridge writes to, so BF2 and BF3 have nothing left to act on, and BF4
  needs a preamble that fails a check. Close all three.
- **Needs**: D4, answered: remove the setting.
- **User and saved state**: README ("Requirements for a write") and
  `config.example.toml` lose the setting. No saved state.
- **Tests**: `test_matching_preamble_for_other_model_writes` in
  `tests/test_writes.py` and `test_w1_custom_preamble_is_written` in
  `integration/test_configs.py` go with the setting. The preamble cases of `test_w10_model_the_bridge_cannot_write`
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
- **Problem**: `print_start` in `accounting.py` took a job's first
  counter rise as the print's start (Q4). A ramming at a cancel (BF10), a
  load at the preview with no poll during it (BF23), or a filament
  change's ramming before the tool's first extrusion (BF27) was that rise.
  The bridge then closed an open swap as assumed, flagged the spool, and
  staged a Spoolman change that should have confirmed the swap.
- **Approach**: Decided under D5 and built, in `account()`:
  - For BF10: a pending job starts only once the counter has risen more
    than `FIRST_EXTRUSION_MM`, 23 mm, past `pending_at`. That's the
    auto-retract ramming's commanded length (F26), and at a feed ratio of
    1 or less the counted part can't exceed it. The usage under it is
    charged as usual; only the start waits.
  - For BF23: a new saved field, `ToolState.pending_empty`, says the tool
    reported no material at the last poll of the pending job. While the
    job is pending, a tool with no material takes `pending_at` again and
    sets the field. The first poll with a material after it takes
    `pending_at` once more and clears the field, without starting. That
    also keeps an empty tool's count during a print with the sensors off
    from starting that tool's print.
  - For BF27: an unload in a job that's still pending promotes the job to
    started, without the start's effects. The unload already supersedes
    the open swap and commits any staging, so the swap it opens stays
    open, as P2 and P3 say for a started print. The promoted unload is a
    mid-print unload: it gets P2's runout flag, and its tag is handled as
    a runout's (Q10, Q11). So does an idle sensor clear while the
    tools-mapping screen shows a job, which is pending there, and a Change
    at the preview's wrong-filament question, when a poll saw the job at
    the question and none lands during the change. An unload at
    the poll that first sees a job isn't promoted: an idle unload followed
    by a print started on the emptied tool, an ordinary swap, looks the
    same. The remaining limit: a runout before any poll has seen the job
    is treated as a swap before the print. The job is pending on the
    empty tool, and with no Spoolman change the print's start closes the
    swap as assumed.
- **Needs**: The F28 trace (sitting D) confirms BF23 on the printer, and
  F26 and F5 (sittings A and B) the size of a ramming. If a trace
  disagrees, the simulator changes first, then the fix.
- **User and saved state**: README, "How usage is attributed", says what
  starts a print. One new `ToolState` field, `pending_empty`, saved in
  place and read as false when a saved state lacks it.
- **Tests**: `tests/test_print_start.py` runs each finding's "To
  reproduce" through the harness with the auto-retract on (BF22): a
  cancel in the preheat on every model that auto-retracts, checked
  against `FIRST_EXTRUSION_MM`; a cancel that rams with an open swap; a
  preview load then a declined type; a preview load then a cancel in the
  preheat, with and without a poll during the load; and a runout in the
  preheat that a poll saw pending, which records no `swap_assumed`. A
  runout no poll saw pending, and an idle unload before a print on the
  emptied tool, are each a swap before the print. The BF27 pin, now
  `test_a_change_on_a_tool_the_print_has_not_used_follows_p2` in
  `tests/test_toolchanger_prints.py`, flipped with the threshold, since
  INDX's ramming counts 16 mm, and
  `test_a_change_on_a_tool_the_print_has_not_used_follows_p3` there covers
  the reload with no Spoolman change. `tests/test_accounting.py` checks
  each rule, and `test_unload_before_a_jobs_first_extrusion_is_a_runout`
  in `tests/test_writes.py` flipped. End to end,
  `test_p8_cancel_ramming_is_not_a_print_start` and
  `test_p8_preview_load_is_not_a_print_start` in
  `integration/test_prints.py`. `Station.start_print` polls in the
  preheat and again once the first tool's counter is past the threshold.
- **Done when**: none of the three paths records `swap_assumed`, and every
  existing print-start test still passes.
- **Overlap**: `accounting.py` (`print_start`, `unload`, `ToolState`),
  `tests/test_accounting.py`, and `integration/station.py`, which both
  suites share.

### 5. Usage on an empty tool

- **Findings**: owns BF24 and BF25, stage 1. BF25 gets warnings only and
  stays Open (D12): a direct warning while the sensors are off, on firmware
  that reports the setting, and the hint on lost usage. Also writes BF12's
  README text, which workstream 8 owns (D12).
- **Problem**: Usage counted while a tool reports no material is held and
  charged to the next spool loaded there (C7). That's right for a load's
  feed before the type appears, and wrong for a print that runs through an
  empty tool: after the sensors are turned off (F29), after a power cut
  with the filament pulled out (F12), or on a toolchanger past the
  tools-mapping screen.
- **Approach**: Built for BF24. `account()` takes `printing`, which
  `PrinterBridge.process` sets for a job the bridge didn't start in
  `PRINTING` or `PAUSED` (`PRINTING_STATES` in `prusalink.py`). `BUSY` is
  left out: the firmware's status writes no job while it's busy
  (`status_renderer.cpp:36`, `printer_state.cpp:611-623`), so a resume's
  feed back there is held until the print counts on, and a second cut
  during the resume charges it, about 8 mm, to the active spool. In
  `usage()`, usage the tool counted since the last poll with no material
  while printing is reported lost, as a `LostEmpty` event, and `charged`
  moves past it; otherwise it stays held, as before. Only the tool's own
  count matters, since `printing` is the printer's: an idle empty tool on
  a toolchanger keeps its held usage through a print on another tool. `ATTENTION` holds: a filament change's reload
  counts its feed before the type comes back (F3, F36), so the rule keeps
  that feed. Usage counted between a print's last poll and its end is
  first seen at a poll with no job, so a saved flag,
  `ToolState.printed_empty`, reports the next poll's usage on the empty
  tool lost too. The flag is set by the first printing poll at which the
  tool counted with no material, and it stays through later printing
  polls with the tool empty, so a tool the print never counted on has no
  tail: a load started on it right after the print is held as a load's
  feed. A print that counts on the empty tool only after its last
  printing poll leaves that usage held for the next spool. Built for BF25: lost usage, BF25's only signal, raises a
  log-only condition, `PrintingEmpty` in `conditions.py`, at WARNING,
  which says the filament sensors may be off and allows for a power cut
  too. `process` raises it when the poll reports usage lost, and keeps it
  while it stood at the last poll and `printed_empty` is still set, so it
  stands through polls with no new usage and ends at the first poll with
  the print over or the material back. It's raised only while
  `printed_empty` is set, so the print's tail only ends one. An idle empty tool on a toolchanger loses nothing, so
  it never raises it. Then, once the fork's `2ef028181` served the
  sensors setting (F47): `FilamentReading.sensors_enabled` carries it,
  None when the firmware doesn't serve it. While it reads false,
  `PrinterBridge.read()` logs `SensorsOff`, a log-only condition at
  WARNING that's the printer's, not a tool's: it's logged through
  `log_changes()` with its own record of what's been logged, and isn't
  saved, so a restart logs it again while it stands. `PrintingEmpty`
  records the setting when it's raised and gives it in the past tense; a
  change while it stands, or a restart, doesn't change it. Each tool's `filament_present` (F48) isn't
  used: BF12 waits on workstream 8.
- **Needs**: F12 confirms the power-cut path. Sitting D's F29 trace
  confirmed the single-tool path on 2026-11-16.
- **User and saved state**: README, "How usage is attributed", changes the
  held-usage rule, explains the warning, and has BF12's text. One new
  `ToolState` field, `printed_empty`, saved in place and read as false
  when a saved state lacks it. One new condition kind, saved in the
  conditions table as the others are; older databases don't have it and
  load unchanged. For the sensors setting, `PrintingEmpty` gains
  `sensors`, saved in place and read as unknown when a saved one lacks it.
  `SensorsOff` isn't saved. The README's Requirements say the field is
  optional.
- **Tests**: `test_a_print_through_an_empty_tool_holds_its_usage`,
  `test_an_unload_in_the_window_then_printing_on_an_empty_tool` and
  `test_a_cut_while_a_runout_waits_for_the_reload` flipped, renamed for
  what they now show: the load is charged its own count only. BF24's rows
  C7, P12 and T2 changed, and C7 gained
  `test_c7_print_through_an_empty_tool_is_reported_lost`.
  `test_c1_usage_held_over_a_reboot_joins_the_provisional_charge` held its
  usage with a sensor fault while printing, which the rule now reports
  lost, so it holds it with an ambiguous location instead. BF25's rows P2,
  S2 and T4 don't change: they describe the sensors on, and with them off
  the bridge still sees no unload. The sensors-off warning changes C7 and
  T2, with `tests/test_sensors_off.py`, a toolchanger test in
  `tests/test_toolchanger_prints.py`, and `test_c7_sensors_off_warned_once`.
  C7 names the hint, and tests in
  `tests/test_empty_tool.py`, `tests/test_toolchanger_prints.py` and
  `tests/test_power_panic.py` count its log lines; the C7 integration test
  checks it's logged once. `tests/test_accounting.py` pins the flag's
  meaning, and `tests/test_toolchanger_prints.py` and
  `tests/test_empty_tool.py` show that a load right after a print on a
  tool the print never counted on is charged, not lost.
- **Done when**: no spool is charged for usage counted during a print on
  a tool with no material, and S1, S2 and the P2 reload charge as before.
- **Overlap**: `accounting.py` (`usage`, `account()`'s signature) and
  `tests/test_accounting.py`'s checking wrapper; `process` in `app.py`;
  `conditions.py`. Workstream 4 builds on it.

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
- **Needs**: D10; workstream 2, for BF7's flag; and workstream 3, which
  changes the same code. It has to merge before the INDX upgrade.
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
- **Approach (proposal)**: Documentation only. For BF12, done with
  workstream 5 (D12): README, under "How usage is attributed", tells a
  toolchanger's user to unload from the menu or to claim, since nothing
  the bridge reads shows the swap. For
  BF13, the proposal was to accept today's behavior: the join's unload
  does leave the old tool empty, and M5a's flag on a spool that ran out
  with weight left is the flag's purpose. D11 leaves BF13 Open instead:
  decide it here, with a spool join seen on an INDX, since the old
  tool's swap and flags ask for a spool in a tool the print has left.
- **Needs**: BF13's decision.
- **User and saved state**: README only.
- **Tests**: BF12's pin in `tests/test_toolchanger_prints.py` stays, and
  T8 stays Gap, with the README text named in its status. BF13's pin and T9 follow
  its decision: if BF13 is Accepted, the pin's comment changes from "a
  known fault" to the accepted behavior and T9 leaves Gap; if it's fixed,
  the pin flips with the fix.
- **Done when**: BF13 is fixed or Accepted. BF12's README text is in.
- **Overlap**: README and `tests/test_toolchanger_prints.py`'s comments,
  which workstream 4 also edits. If BF13 gets a fix, it's `unload` in
  `accounting.py`, and collides with workstreams 4 and 5.

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
- **Approach**: `READY` is out of `IDLE_STATES` in `prusalink.py` (BF21):
  it was never matched, and a dead entry misled the next reader. BF6 and
  BF22 are Accepted (D11). BF11 becomes Accepted once the F12 trace
  confirms the 8 mm. P12 already says the bridge charges each resume's
  feed back, a second cut's included. With the trace, P12 also says
  that's where the feed belongs, as "Bridge follow-ups found here" in
  printer-behaviors.md asks.
- **Needs**: the F12 trace for BF11.
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

These get no code. Each is documented, or Accepted under D11. BF13 isn't
in this list: D11 leaves it Open for workstream 8.

| Finding | Why |
| --- | --- |
| BF6 | The bridge only writes names inside the firmware's rules and preset names `firmware-check` compares. W5's flag catches the rest. |
| BF7 | The cause can't be read. Workstream 2's flag and workstream 7's README text are all the bridge can do. |
| BF11 | The lost usage needs the counters in the firmware's power-panic data. The resume's feed is charged where it belongs, and so is a second cut's, which leaves the nozzle because that cut doesn't retract. |
| BF12 | No reading shows a docked tool emptied by hand. Only a new firmware field, such as each tool's sensor state, would. The README says how to avoid it. |
| BF22 | Changing the harness's default flips many unrelated tests. Workstream 4's new tests turn the auto-retract on themselves. |
| BF25 | No reading shows a hand swap made while the sensors are off. Workstream 5 warns while the sensors are off, on firmware that reports the setting, and hints when a print loses usage on an empty tool. |

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
- A BF9 correction, if D7 chooses one, changes the counts workstream 4's
  threshold is measured against.

## Open questions

Left to the work:

- Whether one idle poll always covers the power-panic window. The F12
  trace decides (workstream 1).
- Whether `/api/v1/info` is cheap enough to read at every poll, or only
  at a boot and when the tool count changes (workstream 9).
