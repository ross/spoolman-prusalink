# Bridge findings

Places where the firmware source or the simulator shows the bridge would
charge the wrong amount or the wrong spool, miss a spool swap, or fail to
write to the printer. They turned up while the simulator was made to follow
each printer family ([simulator-models.md](simulator-models.md)). This list
tracks them in a proposed order of work, so they can be taken one stage at
a time.

## Reading an entry

Each finding has an ID from BF1 to BF25, for "bridge finding". The single
letters are taken by the scenario rows in [scenarios.md](scenarios.md) and
the printer behaviors (F) in [printer-behaviors.md](printer-behaviors.md).
An ID stays with its finding when the finding moves stage or is fixed.

- **Printers**: the models or families it affects.
- **What happens**: what the printer does, with the firmware file, and what
  the bridge does with it today. Firmware paths are at `ff61540d4`, as in
  simulator-models.md.
- **Effect**: what's charged wrong, missed or not written.
- **Evidence**: read in the firmware source, inferred from the source,
  shown in the simulator (with the test), or to be confirmed. Facts about
  the bridge itself name the bridge's file.
- **Scenario rows**: the rows in scenarios.md it touches.
- **To reproduce**: how to show it in the simulator today, or what keeps
  it out of reach, such as a workstream in simulator-models.md not yet
  built.
- **Status**: Open until it's fixed, or Accepted when the owner decides to
  live with it.

## Priorities

A proposed order, for the owner to change. Each finding sits in the
earliest stage that has a printer it affects. Within a stage, findings
that change what's charged come first, then those that change what's
written.

1. **The Core One L, in use today**, and anything that affects every
   printer.
2. **The INDX upgrade.** The Core One L+ becomes a Core One L INDX in a few
   months. After that, single-tool behavior can't be traced again, so the
   traces listed in simulator-models.md under "Before the INDX upgrade"
   have to be taken before it: F3, F5, F12, F26, F27, F28 and F29, which
   stage 1 depends on, and F4 and F24's tag rows, which no finding does.
3. **Other multi-tool printers (XL).** No finding is the XL's alone: BF9,
   BF12, BF13, BF14, BF24 and BF25 cover it and sit in stages 1 and 2.
4. **MMU3.**
5. **Minor or harmless**, whatever the printer.

| Stage | Finding | Title |
| --- | --- | --- |
| 1 | BF9 | The auto-retract charges filament that wasn't used |
| 1 | BF10 | A print cancelled in preheat can look started |
| 1 | BF11 | A power-panic resume loses usage and counts a purge |
| 1 | BF23 | A print declined after the preview's load can count as started |
| 1 | BF24 | Usage counted on an empty tool lands on the next spool |
| 1 | BF25 | Printing past an empty tool leaves the filament sensors off |
| 1 | BF3 | A custom preamble without the input-shaper check makes every sync job wait |
| 1 | BF2 | The bridge refuses a preamble the printer accepts |
| 1 | BF4 | The printer's check settings change what a failed check does |
| 1 | BF5 | A declined or refused sync job uses up a job ID and never finishes |
| 2 | BF12 | Hand swaps on a toolchanger can be missed |
| 2 | BF13 | A spool join counts an unload in the middle of a print |
| 2 | BF14 | Writes are refused on multi-tool models |
| 2 | BF1 | INDX: every sync job waits for a tap |
| 2 | BF8 | INDX presets differ from the bridge's table |
| 2 | BF7 | An INDX with an uncalibrated nozzle cleaner refuses every print |
| 3 | | None of its own |
| 4 | BF16 | MMU: each tool change over-charges the outgoing slot |
| 4 | BF15 | MMU: swaps are invisible |
| 4 | BF17 | MMU: nothing is counted after a boot until a tool change or load |
| 4 | BF20 | MMU printers are on the supported list as single-tool models |
| 4 | BF18 | MMU: the sync job can wait on a question and unload the filament |
| 4 | BF19 | MMU: writes reach slot 0 only |
| 5 | BF6 | A failed `M865` is silent |
| 5 | BF21 | `READY` in `IDLE_STATES` never arrives |
| 5 | BF22 | The unit harness runs with the auto-retract off |

## Writing to the printer

### BF1. INDX: every sync job waits for a tap

- **Printers**: Core One INDX, Core One L INDX.
- **What happens**: An INDX asks about any file with no
  `M862.6 P"INDX lock"`, and no setting turns the question off
  (`gcode_compatibility.cpp:92-101, 372-376`). `default_preamble` in
  `printer_sync.py` has no such line, so the job would wait in `ATTENTION`
  until someone taps through. `model_problem` refuses writes on every
  multi-tool model (BF14), so the bridge never uploads that job today.
- **Effect**: No write lands without someone at the printer. An unanswered
  or declined job then goes the way of BF5.
- **Evidence**: Read in the firmware source.
- **Scenario rows**: W1, T6.
- **To reproduce**: The printer's side, today:
  `test_indx_asks_about_the_lock_under_any_setting` in
  `simulator/tests/test_model.py` uploads a job with no lock line to an
  INDX, and it asks under every `checks` setting. The bridge's side can't
  be reached: its sync job never gets to the question until the per-tool
  write (BF14) is built.
- **Status**: Open.

### BF2. The bridge refuses a preamble the printer accepts

- **Printers**: Every model the bridge writes to, with a custom
  `gcode_preamble`.
- **What happens**: The firmware's model check compares groups, not names:
  a Core One L takes Core One G-code as its own, a Core One takes Core One
  Oak, and an MK4 takes MK3.9. Older models' G-code is "compatibility
  mode", which asks. INDX builds take only their own name
  (`printer_model.cpp:124-198`). `model_problem` accepts only the exact
  name or its MMU3 variant.
- **Effect**: A custom preamble for another model in the group is refused,
  and waiting spools are flagged (W10), though the printer would run the
  job. The default preamble names the model exactly, so it isn't affected.
- **Evidence**: Read in the firmware source. The printer's side is shown
  in the simulator by
  `test_the_preview_takes_a_group_and_asks_in_compatibility_mode` in
  `simulator/tests/test_model.py`: a Core One L runs a Core One job with
  no question.
- **Scenario rows**: W1, W10.
- **To reproduce**: Give a Core One L a `gcode_preamble` whose `M862.3`
  names `COREONE`. `model_problem` refuses it, though the simulator's
  preview takes the job. No bridge test runs that pairing.
- **Status**: Open.

### BF3. A custom preamble without the input-shaper check makes every sync job wait

- **Printers**: Every model the bridge writes to: MK3.9, MK3.9S, MK4, MK4S,
  Core One, Core One Oak, Core One L.
- **What happens**: `model_problem` checks a custom `gcode_preamble` for
  its `M862.3` line only. Every build but the iX requires
  `M862.6 P"Input shaper"`, and under the default setting asks about a
  file without it (F27: the check in
  `src/feature/compatibility_checks/gcode_compatibility.cpp`, the parse in
  `src/common/gcode/gcode_info.cpp`). So every sync job from such a
  preamble waits in `ATTENTION` for a tap. README.md, under "Requirements
  for a write", lists the three lines the default job starts with, but
  says only that a custom preamble's `M862.3` must name the printer's
  model. The `gcode_preamble` comment in `config.example.toml` says only
  that too. Its example has the input-shaper line, and neither text says
  the line is needed.
- **Effect**: No write lands without someone at the printer, and an
  unanswered or declined job goes the way of BF5. Under `abort` (BF4) the
  job is refused.
- **Evidence**: Read in the firmware source; shown in the simulator by
  `test_matching_preamble_for_other_model_writes` in `tests/test_writes.py`
  and `test_w1_custom_preamble_is_written` in
  `integration/test_configs.py`, which assert the question and tap
  through it. The default preamble asks nothing on any model the bridge
  writes to
  (`test_the_bridge_default_job_asks_nothing_on_the_models_it_writes_to`
  in `simulator/tests/test_model.py`).
- **Scenario rows**: W1, W10.
- **To reproduce**: Those tests, today.
- **Status**: Open.

### BF4. The printer's check settings change what a failed check does

- **Printers**: All.
- **What happens**: The severity of each kind of check is a user setting
  in the printer's menu: ignore, warn (the default on every build) or
  abort. Every failed check shows on one screen, and the worst severity
  decides. A warning waits for a tap; an abort can only be declined
  (`gcode_compatibility.cpp:48-150`,
  `marlin_print_preview.cpp:921-971`). README.md, under "Requirements for
  a write", describes only the warning. The INDX lock question (BF1)
  always asks. The checks of a print's tools have fixed severities, and
  the sync job, which names no filament used, raises none of them (F28).
- **Effect**: Set to abort, a job that fails a check is refused instead of
  held, and goes the way of BF5. Set to ignore, it runs. The default
  preamble passes every check on a supported model, so this matters only
  with BF2, BF3 or another preamble that fails a check.
- **Evidence**: Read in the firmware source. The simulator models the
  three outcomes for an uploaded file (F27).
- **Scenario rows**: W1, W5, W10.
- **To reproduce**: The simulator's per-printer `checks` setting
  (`ignore`, `warn` or `abort`, or the `set_checks` action) stands for the
  menu's settings. `test_checks_set_to_ignore_ask_nothing` and
  `test_a_fatal_question_offers_only_abort` in
  `simulator/tests/test_model.py` show ignore and abort on an upload. A
  bridge with a preamble that fails a check (BF3) meets the same
  outcomes. No bridge test sets `checks`.
- **Status**: Open.

### BF5. A declined or refused sync job uses up a job ID and never finishes

- **Printers**: All the bridge writes to; INDX once BF14 is built.
- **What happens**: An upload skips the preview screen, so the job ID is
  taken at once. While a question waits, the state is `ATTENTION`,
  `/api/v1/status` has the job with its new ID, and `/api/v1/job` answers
  `PAUSED`. Declining goes to `IDLE`, not `STOPPED` or `FINISHED`, with
  the ID used up (`wui_api.cpp:291`, `marlin_print_preview.cpp:414-425`,
  `marlin_server.cpp:1113-1136, 2300-2316`). The bridge waits while the
  printer reads `ATTENTION` and knows the job by its file name (W6). After
  a declined job the printer still differs, so the bridge uploads once
  more. After two jobs (`MAX_SYNC_ATTEMPTS` in `app.py`) it flags the
  spool "didn't take" (W5).
- **Effect**: Each attempt holds the printer at a question until someone
  answers, and the spool is flagged after the second. No charge changes.
- **Evidence**: Read in the firmware source. The simulator models the
  printer's side (F27; `test_the_preview_counts_the_job_then_asks` and
  `test_abort_at_the_preview_ends_idle_with_the_id_used` in
  `simulator/tests/test_model.py`). The bridge's side is inferred from
  `app.py`: no test declines a sync job.
- **Scenario rows**: W2, W5, W6.
- **To reproduce**: Raise the question as
  `test_w1_custom_preamble_is_written` does, with a preamble that has no
  input-shaper line, then answer it with `cancel` instead of
  `tap_through`, polling between. With `checks` set to `abort`, `cancel`
  is the only answer.
- **Status**: Open.

### BF6. A failed `M865` is silent

- **Printers**: All the bridge writes to.
- **What happens**: An `M865` the firmware can't take prints a serial
  error, stores nothing, and the job still ends `FINISHED`. A filament name
  outside 1 to 7 of `a-zA-Z0-9_-` does it (`filament_common.cpp:27-42`),
  and so does a preset name the build lacks (`FilamentType::from_name` in
  `src/common/filament_common.cpp`). `printer_name` in `printer_sync.py`
  keeps the bridge's names inside that rule.
- **Effect**: A late flag only. The printer still differs, so the bridge
  flags the spool "didn't take" after the second job (W5).
- **Evidence**: Read in the firmware source. The simulator models the
  preset case (`simulator/sync.py`,
  `simulator/tests/test_model_presets.py`).
- **Scenario rows**: W1, W5.
- **To reproduce**: The simulator's preset-write failure, today. No bridge
  test drives a failing `M865`.
- **Status**: Open.

### BF7. An INDX with an uncalibrated nozzle cleaner refuses every print

- **Printers**: Core One INDX, Core One L INDX.
- **What happens**: An INDX whose nozzle cleaner isn't calibrated refuses
  every print, the sync job included
  (`gcode_compatibility.cpp:92-101, 372-376`). Unreachable today, as BF1,
  until the per-tool write (BF14).
- **Effect**: Every write fails and goes the way of BF5. The "didn't take"
  flag lists the differences, not the cause.
- **Evidence**: Read in the firmware source.
- **Scenario rows**: W5, T6.
- **To reproduce**: Can't be shown. The simulator's preview doesn't model
  an uncalibrated nozzle cleaner (`simulator/README.md`), and the bridge
  uploads nothing to an INDX until the per-tool write (BF14).
- **Status**: Open.

### BF8. INDX presets differ from the bridge's table

- **Printers**: Core One INDX, Core One L INDX. The MINI and MK3.5 differ
  too, but the bridge charges and writes nothing there. On the models the
  bridge writes to, the table matches apart from PPS and PPA, which it
  leaves out on purpose (W8, `EXPECTED` in `firmware/compare.py`).
- **What happens**: `PRESETS` in `printer_sync.py` is one table for every
  model. On INDX the firmware's PC preheat is 230 and its FLEX preheat 170,
  against the table's 250 and 210 (`src/common/filament_presets.cpp`).
- **Effect**: Once the bridge writes to an INDX, a PC or FLEX spool is
  written with the wrong preheat. A menu load of either preset that
  otherwise matches the spool differs in preheat, so it gets a sync job,
  or with printer sync off a drift flag ([plans.md](plans.md)).
- **Evidence**: Read in the firmware source. `script/firmware-check`
  compares the simulator's per-build tables with it.
- **Scenario rows**: W1, W9, W14, T6.
- **To reproduce**: The printer's side, today: an INDX menu load of PC in
  the simulator reports a 230 preheat. The write needs BF14.
- **Status**: Open.

## Charging

### BF9. The auto-retract charges filament that wasn't used

- **Printers**: Every Nextruder: the MK3.9 and MK4 families, Core One,
  Core One Oak, Core One L, iX and XL. INDX counts its auto-retract
  exactly.
- **What happens**: After an idle load and at the end of every print, the
  sync job included, a Nextruder runs its auto-retract ramming. It first
  feeds back what the tracker says is retracted, then counts 23 mm: an
  8 mm feed that leaves the nozzle and 15 mm that never does. The next
  forward move counts 15 mm more that never leaves the nozzle (F26;
  `standard_ramming_sequence_nextruder.cpp`, `ramming_sequence.cpp`,
  `mapi/motion.cpp`, `marlin_server.cpp:763-770, 1045-1080`). It's off for
  flexible filament and when the user turns it off. The bridge charges what
  the counter says.
- **Effect**: About 30 mm commanded, 28.5 mm counted at the 0.95 feed
  ratio, for each auto-retract: half at the retract, half at the next
  feed. That's about 0.09 g of 1.75 mm PLA. In `tests/test_families.py` a
  10 mm print on the Core One L charges 45 mm.
- **Evidence**: Read in the firmware source; shown in the simulator by
  `test_family_story` in `tests/test_families.py`. Not traced: F3 and F5
  on the Core One L will show it.
- **Scenario rows**: P1, S1, S2.
- **To reproduce**: `script/test tests/test_families.py`.
- **Status**: Open.

### BF10. A print cancelled in preheat can look started

- **Printers**: Every Nextruder, as BF9.
- **What happens**: A cancelled print's end runs the auto-retract too. On a
  tool that isn't already auto-retracted, such as one fed with Move E or
  Purge filament since its last load or print, the ramming counts about
  22 mm (23 mm commanded at 0.95) while the job is still visible.
  `accounting.print_start` takes the first counter rise in a new job as
  the print's start (Q4), so it takes the ramming as one. A cancel during
  the scan or the start blip moves nothing and ends in `IDLE` (F27), so
  this takes a cancel later in the preheat.
- **Effect**: A print cancelled before it extruded counts as started. An
  open unconfirmed swap closes as "assumed same spool" with a provisional
  charge (S4, Q1), and a staged spool is flagged (S9, Q2). A Spoolman fix
  made during the preheat no longer confirms the swap, against what P8
  promises.
- **Evidence**: Inferred from the source and `accounting.py`. No test
  covers it: the P8 test's tool is already auto-retracted.
- **Scenario rows**: P8, Q4, S4, Q1, S9, Q2.
- **To reproduce**: The simulator has what it needs today:
  `test_p8_cancelled_during_preheat_is_not_a_print_start` in
  `integration/test_prints.py`, with the tool fed (`extrude`) between the
  load and the print. The test already cancels after the blip.
- **Status**: Open.

### BF11. A power-panic resume loses usage and counts a purge

- **Printers**: All with counters. The purge differs by build.
- **What happens**: The counters restart at boot and aren't in the
  power-panic data, so usage since the bridge's last poll is lost. The
  resume then moves filament, and two readings of the source disagree on
  how much. F12 reads a 25 mm purge on the Core One L
  (`power_panic.cpp:484-488`, `nozzle_cleaner.cpp:96-106`).
  simulator-models.md reads 25 mm on INDX only: the MK4 family, Core One,
  Core One L and iX feed back the 8 mm retracted at the cut, and the XL
  moves nothing (`power_panic.cpp:110-116, 480-521`). The bridge charges
  whatever counts to the spool active after the boot.
- **Effect**: Up to one poll interval of printing, 30 s by default, is
  never charged. The purge is charged to the active spool, which is right
  if it leaves the nozzle; its size is unsettled.
- **Evidence**: Read in the firmware source, with two readings that
  disagree. The F12 trace on the Core One L decides, and has to be taken
  before the INDX upgrade.
- **Scenario rows**: P12, Q5, C1.
- **To reproduce**: `test_p12_power_panic_resume_keeps_the_job` in
  `integration/test_prints.py` shows the loss and a 25 mm purge today. The
  purge per build needs workstream 4.
- **Status**: Open. A candidate for Accepted: printer-behaviors.md, under
  "Bridge follow-ups found here", calls both parts fine.

### BF23. A print declined after the preview's load can count as started

- **Printers**: Single-tool models with counters. A toolchanger's
  tools-mapping screen loads nothing.
- **What happens**: A print started on a tool with no filament asks for a
  load at the preview (F28). The load counts while the printer reads
  `BUSY` with no job: 57 mm on a Core One L, (20 + 40) × 0.95. A file
  that names another type than the one loaded then asks again, and the
  user can decline there. `accounting.print_start` takes a new job as
  started when a later poll of the same job sees the counter past where
  the first poll saw it. With one poll at the preview's first question,
  the next at the wrong-filament question and none during the load, the
  load's count reads as the print's first extrusion. The load takes about
  11 s at the simulator's load rate, and `poll_printing` defaults to 30 s,
  so a poll usually misses it.
- **Effect**: A job that never printed counts as started. An open
  unconfirmed swap closes as "assumed same spool" with a provisional
  charge (S4, Q1), and a staged spool is flagged (S9, Q2), as in BF10.
- **Evidence**: Reproduced through the unit harness, with no test kept. A
  poll during the load avoids it: the bridge sees no job then, and takes
  the job's counter afresh when the job comes back.
- **Scenario rows**: P8, Q4, S4, Q1, S9, Q2.
- **To reproduce**: Unload the tool's spool. Start a print whose segment
  names another type on the empty tool, and poll at the "no filament"
  question. `tap_through`, then `load` the tool with a preset of a
  different type from the segment's, with no poll during the load. Poll
  at the wrong-filament question, then `cancel`. The ledger shows
  `swap_assumed` for the job. On the Core One L, the live check
  `F28-load-then-decline` confirms it with the production bridge: sitting
  D in printer-behaviors.md, under "Capture plan".
- **Status**: Open.

### BF24. Usage counted on an empty tool lands on the next spool

- **Printers**: Single-tool models with counters, when the user turns the
  filament sensors off at the filament-not-inserted screen instead of
  loading (F29). Core One INDX, Core One L INDX and XL, at the
  tools-mapping screen.
- **What happens**: On the tools-mapping screen, Print with a used tool
  empty first asks to turn the filament sensors off, and only that lets
  the print go on. When the file names a type for the empty tool, a type
  question follows, which Ignore answers (F28, F29). A single-tool printer's
  filament-not-inserted screen offers the same, and the print goes on
  alike (F29). The print then runs through the empty tool, and the tracker
  counts every move on that tool with no material, a toolchanger's pickup
  purge included. The bridge holds that usage because the tool reports no
  material (C7). When filament is next loaded in that tool, the bridge
  charges the held usage, with the load's own count, to the spool
  reported there.
- **Effect**: The new spool is charged for filament that came from no
  spool: 76 mm in the test, on top of its load.
- **Evidence**: On a toolchanger, shown in the simulator by
  `test_a_print_through_an_empty_tool_holds_its_usage` in
  `tests/test_toolchanger_prints.py`, on an INDX, which pins the charge:
  the held 76 mm plus the load's 70. On a single-tool
  printer, read in the firmware source (F29): the simulator doesn't model
  that path.
- **Scenario rows**: C7, T2.
- **To reproduce**: That test, today, on a toolchanger. On the Core One L,
  the live check `F29-sensors-off-print` confirms it with the production
  bridge: sitting D in printer-behaviors.md, under "Capture plan".
- **Status**: Open.

### BF25. Printing past an empty tool leaves the filament sensors off

- **Printers**: As BF24.
- **What happens**: The sensors setting that BF24's answer turns off is
  persistent (F29). Until the user turns it on again, the printer detects
  no runout and runs no idle sensor clear, so filament pulled out by hand
  counts no unload. Only a menu unload counts. The bridge's swap detection
  rests on those unloads. Nothing the simulator serves says the sensors
  are off, and the bridge has no reading for it that this work found.
- **Effect**: A hand swap opens no swap, as in BF12, so the old spool is
  charged for the new one until a claim moves it. A spool that runs out
  isn't noticed, and the print goes on through the empty tool, which is
  BF24 again.
- **Evidence**: Read in the firmware source (F29). The printer's side is
  shown in the simulator on a toolchanger, by
  `test_with_the_sensors_off_nothing_reads_them` in
  `simulator/tests/test_model.py`. The simulator doesn't model the
  single-tool path to the setting. The bridge's side is inferred.
- **Scenario rows**: P2, S2, T4.
- **To reproduce**: On the Core One L, the live check
  `F29-sensors-off-print` shows the printer's side: sitting D in
  printer-behaviors.md, under "Capture plan". In the simulator, the
  printer's side with that test. With a bridge, turn the sensors off with
  the simulator's `sensors` setting or `set_sensors`, then empty a tool by
  hand and pick it as `test_t4_idle_sensor_clear_on_a_picked_tool` in
  `integration/test_tools.py` does, and poll: no unload counts, where T4
  counts one. No bridge test covers it.
- **Status**: Open.

## Multi-tool

### BF12. Hand swaps on a toolchanger can be missed

- **Printers**: Core One INDX, Core One L INDX, XL.
- **What happens**: The idle sensor clear runs only on the picked tool
  (`filament_sensors_handler.cpp:294-312`). An INDX docks its tool after a
  load, an unload, a pause and a print, so an idle INDX has none picked
  (`marlin_server.cpp:1098, 2561`, `toolchanger_indx.cpp:222-262`). An XL
  has whichever tool is on the head. A docked tool emptied by hand keeps
  its type and counts no unload, and nothing sweeps the tools at boot or at
  a print start. On an idle INDX only a menu unload counts. At a print
  start the tools-mapping screen shows a hand-emptied used tool as having
  no filament, but that counts no unload either, and the idle sensor clear
  it allows runs on the picked tool only (F28). Printing past it is BF24
  and BF25.
- **Effect**: The bridge sees no unload, so a hand swap opens no swap. A
  spool moved into the tool's location in Spoolman stays staged (S13, S9),
  and the old spool is charged for the new one until a claim moves it.
- **Evidence**: Read in the firmware source; the bridge's side is
  inferred.
- **Scenario rows**: T3, T4, S9, S13.
- **To reproduce**: Needs workstream 4. The simulator always has a tool
  picked today, so T4 shows only the picked-tool case.
- **Status**: Open.

### BF13. A spool join counts an unload in the middle of a print

- **Printers**: Core One INDX, Core One L INDX, XL.
- **What happens**: A runout with a spool join counts an unload on the old
  tool, and the print goes on with the join's tool. The new tool's type
  isn't set or copied, and its purge counts on it: 27 mm on the XL, 25 mm
  on INDX (`spool_join.cpp:258-342`, `toolchanger_xl.cpp:505-549`). No
  scenario row covers a join. The bridge takes the old tool's unload as a
  runout during a started print: it opens an unconfirmed swap there (P3)
  and may raise the runout flag (M5a). The rest of the print is charged to
  the spool in the join tool's location.
- **Effect**: Not settled. The charges follow the filament when each tool's
  location holds its spool. The old tool's swap and flags follow rules
  written for a reload of the same tool.
- **Evidence**: Read in the firmware source; the bridge's side is
  inferred.
- **Scenario rows**: P2, P3, P4, M5a, T2.
- **To reproduce**: Needs workstream 4.
- **Status**: Open.

### BF14. Writes are refused on multi-tool models

- **Printers**: Core One INDX, Core One L INDX, XL, XLP.
- **What happens**: `model_problem` refuses every multi-tool type in
  `PRINTER_MODELS` ("multi-tool printers aren't supported yet"). A due
  write flags the spool "can't write to the printer" and uploads nothing.
  The per-tool write isn't built: `M865 L<n>` per tool (F22), with no
  `M865.1`, since these models keep no UUIDs (F21). The tools-mapping
  screen won't stand in its way: the sync job names no filament used, so
  the screen doesn't apply (F28), and on INDX only the lock question asks
  (BF1).
- **Effect**: After the INDX upgrade the owner's printer gets no writes. A
  menu load keeps the preset's name and temperatures, and each write that
  would be due leaves a flag. Charging isn't affected. Building the write
  makes BF1, BF7 and BF8 reachable.
- **Evidence**: Shown in the simulator by
  `test_t6_writes_on_a_multi_tool_model_are_refused` in
  `integration/test_tools.py`, and by the XL and INDX cases of
  `test_family_story`.
- **Scenario rows**: T6, W10.
- **To reproduce**: Those tests, today.
- **Status**: Open.

## MMU3

Each MMU finding applies to the MK4 family, the Core One and the Core One
L with an MMU3 turned on in the printer's settings. None can be shown
before workstream 5, and the scenarios have no MMU rows yet.

### BF15. MMU: swaps are invisible

- **What happens**: Tool changes, MMU unloads, ejects, cuts, `M600` and
  spool joins leave `unload_count` alone, and the idle sensor clear is off.
  Only turning the MMU off with an empty nozzle, the change-all unload and
  a cold pull count one (`store_definition.cpp:258-262`,
  `filament_sensors_handler.cpp:306`). A slot's type is what's loaded in
  the slot (`M70X_mmu.cpp:80-82`), so swapping the spool that feeds a slot
  shows nothing.
- **Effect**: The bridge never sees a slot's spool change. A spool moved
  into the slot's location stays staged (S13, S9), and the old spool is
  charged for the new one until a claim moves it.
- **Evidence**: Read in the firmware source; the bridge's side is
  inferred.
- **Scenario rows**: S1, S2, S9, S13.
- **To reproduce**: Needs workstream 5.
- **Status**: Open.

### BF16. MMU: each tool change over-charges the outgoing slot

- **What happens**: At a tool change the firmware plans about 190 mm to
  reach the filament sensor, then up to 57 mm more, while the old slot is
  still selected. The planner counts at plan time, so the old slot is
  credited with it (`mmu2_mk4.cpp:524-701`).
- **Effect**: Roughly 200 mm, less the gap, charged to the outgoing slot's
  spool per change, and more on a retry. That's about 0.6 g of 1.75 mm PLA
  per change.
- **Evidence**: Inferred from the source. The size depends on how long the
  push to the sensor takes, which only hardware could tell.
- **Scenario rows**: None yet. T1 and T2 are the multi-tool equivalents.
- **To reproduce**: Needs workstream 5.
- **Status**: Open.

### BF17. MMU: nothing is counted after a boot until a tool change or load

- **What happens**: The selected slot decides which tool is credited, and
  it's "none" after an unload and after every boot. Extrusion with no slot
  selected isn't counted (`mmu2_mk4.cpp:175, 674`, `planner.cpp:2207`).
  A power-panic resume is a boot too.
- **Effect**: Usage between a boot and the next tool change or load is
  never charged. A resumed single-color print is charged nothing from the
  resume on.
- **Evidence**: Read in the firmware source.
- **Scenario rows**: C1, P12.
- **To reproduce**: Needs workstream 5.
- **Status**: Open.

### BF18. MMU: the sync job can wait on a question and unload the filament

- **What happens**: With an MMU and filament at the nozzle, the preview
  asks to unload first, whatever the settings. Yes unloads; no starts the
  job as it is (`marlin_print_preview.cpp:228-256, 699-704`). The stock
  MMU3 end G-code unloads after every print, so this needs a printer left
  loaded by hand.
- **Effect**: The sync job waits in `ATTENTION` and goes the way of BF5.
  Answering yes unloads the filament for a job that moves none.
- **Evidence**: Read in the firmware source.
- **Scenario rows**: W1, W5.
- **To reproduce**: Needs workstream 5, which adds the preview's unload
  question.
- **Status**: Open.

### BF19. MMU: writes reach slot 0 only

- **What happens**: A sync job's write lands only on slot 0. An ad-hoc
  write to slots 1 to 4 does nothing on release firmware
  (`filament.cpp:396-402`). The tag reader is off with an MMU on, so slot
  0 can keep UUIDs from an earlier tag load but gets no new ones
  (`request_manager.cpp:263-266`). The bridge writes `M865 L<n>` for each
  configured tool, and `M865.1` on tool 0 of a model that keeps UUIDs.
- **Effect**: A write to slots 1 to 4 never takes, so the bridge flags the
  spool "didn't take" after two jobs (W5). Slot 0 is written, UUID
  included, but never reports a new tag.
- **Evidence**: Read in the firmware source; the bridge's side is
  inferred.
- **Scenario rows**: W1, W5, Q10, Q11.
- **To reproduce**: Needs workstream 5.
- **Status**: Open.

### BF20. MMU printers are on the supported list as single-tool models

- **What happens**: With an MMU on, `/api/v1/filament` lists five tools,
  one per slot (`tool_index.cpp:84-102`), and `/api/v1/info` reports
  `mmu` as true (`basic_gets.cpp:229-253`). The bridge doesn't read
  `/api/v1/info`. `PRINTER_MODELS` in `printer_sync.py` lists these types
  as single-tool and writable, so `model_problem` lets writes through.
- **Effect**: The bridge writes to an MMU printer as to a single-tool one,
  which leads to BF18 and BF19, and nothing tells the user an MMU isn't
  supported. Charging per slot follows the config's tool map.
- **Evidence**: Read in the firmware source and `printer_sync.py`.
- **Scenario rows**: W10, T6.
- **To reproduce**: Needs workstream 5. The simulator's `/api/v1/info`
  always reports `mmu` as false today.
- **Status**: Open.

## Minor

### BF21. `READY` in `IDLE_STATES` never arrives

- **Printers**: All.
- **What happens**: Only Prusa Connect is told `READY`. PrusaLink reports
  the state underneath (`status_renderer.cpp:30`). `IDLE_STATES` in
  `prusalink.py` lists it.
- **Effect**: None. The entry is never matched.
- **Evidence**: Read in the firmware source.
- **Scenario rows**: None directly. W2 and W3 rely on `IDLE_STATES`.
- **To reproduce**: Nothing to show: the simulator never reports `READY`.
- **Status**: Open. A candidate for Accepted.

### BF22. The unit harness runs with the auto-retract off

- **Printers**: Every Nextruder, in tests only.
- **What happens**: `add_station` and `make` in `tests/harness.py` default
  to `auto_retract=False`, because most unit tests move the counter by
  hand. `tests/test_families.py` and `tests/test_toolchanger_prints.py`
  turn it on, so the unit suite sees the ram after a load and at a
  print's end in the family story on each family (BF9), and runs INDX
  prints, whose end doesn't ram. The rest of the unit suite runs with it
  off, so no unit test sees the ram at a cancel (BF10). The integration
  suite runs the simulator's default, with the auto-retract on.
- **Effect**: No charge changes. A unit test can pass on behavior the ram
  would change.
- **Evidence**: Read in `tests/harness.py` and those two test files.
- **Scenario rows**: P8.
- **To reproduce**: `tests/harness.py`, today.
- **Status**: Open. A candidate for Accepted.
