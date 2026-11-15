# Bridge findings

Places where the firmware source or the simulator shows the bridge would
charge the wrong amount or the wrong spool, miss a spool swap, or fail to
write to the printer. They turned up while the simulator was made to follow
each printer family ([simulator-models.md](simulator-models.md)). This list
tracks them in a proposed order of work, so they can be taken one stage at
a time. [bridge-fixes.md](bridge-fixes.md) plans the fixes as workstreams,
with the decisions they wait on. This list stays the record of each
finding: a fix sets its Status here.

## Reading an entry

Each finding has an ID from BF1 to BF29, for "bridge finding". The single
letters are taken by the scenario rows in [scenarios.md](scenarios.md) and
the printer behaviors (F) in [printer-behaviors.md](printer-behaviors.md).
An ID stays with its finding when the finding moves stage or is fixed.

- **Printers**: the models or families it affects.
- **What happens**: what the printer does, with the firmware file, and what
  the bridge does with it today. Firmware paths are at `ff61540d4`, as in
  simulator-models.md.
- **Effect**: what's charged wrong, missed or not written.
- **Evidence**: one or more of these labels. Read in the firmware source.
  Inferred from the source. Shown in the simulator, with the test that
  shows it. Reproduced, no test kept: a run through the unit harness or
  the integration suite showed it, and no kept test repeats it. To be
  confirmed: the claim is still open. Facts about the bridge itself name
  the bridge's file.
- **Scenario rows**: the rows in scenarios.md it touches.
- **To reproduce**: how to show it in the simulator today, or what keeps
  it out of reach, such as a workstream in simulator-models.md not yet
  built.
- **Status**: Open until it's fixed, or Accepted when the owner decides to
  live with it.

## Priorities

A proposed order, for the owner to change. Each finding sits in the
earliest stage that has a printer it affects, except a minor or harmless
one, which sits in stage 5 whatever the printer. Within a stage, findings
that change what's charged come first, then those that change what's
written.

1. **The Core One L, in use today**, and anything that affects every
   printer.
2. **The INDX upgrade.** The Core One L+ becomes a Core One L INDX in a few
   months. After that, single-tool behavior can't be traced again, so the
   traces listed in simulator-models.md under "Before the INDX upgrade"
   have to be taken before it: F3, F5, F12, F26, F27, F28 and F29, which
   stage 1 depends on, and F4 and F24's tag rows, which no finding does.
3. **Other multi-tool printers (XL).** No finding is the XL's alone:
   every finding whose Printers line includes the XL sits in stages 1
   and 2.
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
| 1 | BF27 | A filament change before a tool's first extrusion in a print looks like its print start |
| 1 | BF3 | A custom preamble without the input-shaper check makes every sync job wait |
| 1 | BF2 | The bridge refuses a preamble the printer accepts |
| 1 | BF4 | The printer's check settings change what a failed check does |
| 1 | BF5 | A declined or refused sync job uses up a job ID and never finishes |
| 1 | BF26 | A sync job in the power-panic window drops the resume |
| 2 | BF12 | Hand swaps on a toolchanger can be missed |
| 2 | BF13 | A spool join counts an unload in the middle of a print |
| 2 | BF14 | Writes are refused on multi-tool models |
| 2 | BF1 | INDX: every sync job waits for a tap |
| 2 | BF8 | INDX presets differ from the bridge's table |
| 2 | BF7 | An INDX with an uncalibrated nozzle cleaner refuses every print |
| 3 | | None of its own |
| 4 | BF16 | MMU: each tool change over-charges the outgoing slot |
| 4 | BF29 | MMU: a load or print start after an unload in the same boot counts up to 40 mm less |
| 4 | BF15 | MMU: swaps are invisible |
| 4 | BF28 | MMU: turning the MMU off clears slot 0, whichever slot was loaded |
| 4 | BF17 | MMU: after a boot, slot 0 is charged while the MMU connects, then nothing until a load or tool change |
| 4 | BF20 | MMU printers are on the supported list as single-tool models |
| 4 | BF18 | MMU: the sync job can wait on the unload question and leave the filament |
| 4 | BF19 | MMU: slots 1 to 4 keep no UUIDs |
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
  be reached: its sync job never gets to the question while writes to
  multi-tool models are refused (BF14).
- **Status**: Open.

### BF2. The bridge refuses a preamble the printer accepts

- **Printers**: Every model the bridge writes to, with a custom
  `gcode_preamble`, until workstream 3 removed the setting.
- **What happens**: The firmware's model check compares groups, not names:
  a Core One L takes Core One G-code as its own, a Core One takes Core One
  Oak, and an MK4 takes MK3.9. Older models' G-code is "compatibility
  mode", which asks. INDX builds take only their own name
  (`printer_model.cpp:140-198`). Until workstream 3, `model_problem`
  accepted only the exact name or its MMU3 variant.
- **Effect**: A custom preamble for another model in the group was
  refused, and waiting spools were flagged (W10), though the printer would
  have run the job. The default preamble names the model exactly, so it
  wasn't affected.
- **Evidence**: Read in the firmware source. The printer's side is shown
  in the simulator by
  `test_the_preview_takes_a_group_and_asks_in_compatibility_mode` in
  `simulator/tests/test_model.py`: a Core One L runs a Core One job with
  no question.
- **Scenario rows**: W1, W10.
- **To reproduce**: No longer reachable: the bridge has no setting for the
  preamble.
- **Status**: Fixed by workstream 3: `gcode_preamble` is gone (D4), and
  every job starts with `default_preamble`, which names the model itself.

### BF3. A custom preamble without the input-shaper check makes every sync job wait

- **Printers**: Every model the bridge writes to: MK3.9, MK3.9S, MK4, MK4S,
  Core One, Core One Oak, Core One L.
- **What happens**: Every build but the iX requires
  `M862.6 P"Input shaper"`, and under the default setting asks about a
  file without it (F27: the check in
  `src/feature/compatibility_checks/gcode_compatibility.cpp`, the parse in
  `src/common/gcode/gcode_info.cpp`). Until workstream 3, `model_problem`
  checked a custom `gcode_preamble` for its `M862.3` line only, so every
  sync job from a preamble without the input-shaper line waited in
  `ATTENTION` for a tap. README.md, under "Requirements for a write", and
  the `gcode_preamble` comment in `config.example.toml` said only that a
  custom preamble's `M862.3` must name the printer's model, and neither
  said the input-shaper line was needed.
- **Effect**: No write landed without someone at the printer, and an
  unanswered or declined job went the way of BF5. Under `abort` (BF4) the
  job was refused.
- **Evidence**: Read in the firmware source. Until workstream 3, shown in
  the simulator by `test_matching_preamble_for_other_model_writes` in
  `tests/test_writes.py` and `test_w1_custom_preamble_is_written` in
  `integration/test_configs.py`, which asserted the question and tapped
  through it. The default preamble asks nothing on any model the bridge
  writes to
  (`test_the_bridge_default_job_asks_nothing_on_the_models_it_writes_to`
  in `simulator/tests/test_model.py`).
- **Scenario rows**: W1, W10.
- **To reproduce**: No longer reachable: the tests went with the setting.
- **Status**: Fixed by workstream 3: `gcode_preamble` is gone (D4), and
  every job starts with `default_preamble`, which has the input-shaper
  line. The tests named under Evidence went with the setting.

### BF4. The printer's check settings change what a failed check does

- **Printers**: All.
- **What happens**: The severity of each kind of check is a user setting
  in the printer's menu: ignore, warn (the default on every build) or
  abort. Every failed check shows on one screen, and the worst severity
  decides. A warning waits for a tap; an abort can only be declined
  (`gcode_compatibility.cpp:48-153`,
  `marlin_print_preview.cpp:921-971`). Until workstream 3, README.md,
  under "Requirements for a write", described only the warning. The INDX
  lock question (BF1)
  always asks. Of the checks of a print's tools, the nozzle checks follow
  a setting and the rest have fixed severities
  (`gcode_compatibility.cpp:155-265`). The sync job, which names no
  filament used, raises none of them (F28).
- **Effect**: Set to abort, a job that fails a check is refused instead of
  held, and goes the way of BF5. Set to ignore, it runs. The default
  preamble passes every check on a supported model, so this mattered only
  with BF2, BF3 or another custom preamble that failed a check, until
  workstream 3 removed the setting.
- **Evidence**: Read in the firmware source. The simulator models the
  three outcomes for an uploaded file (F27).
- **Scenario rows**: W1, W5, W10.
- **To reproduce**: The simulator's per-printer `checks` setting
  (`ignore`, `warn` or `abort`, or the `set_checks` action) stands for the
  menu's settings. `test_checks_set_to_ignore_ask_nothing` and
  `test_a_fatal_question_offers_only_abort` in
  `simulator/tests/test_model.py` show ignore and abort on an upload.
  Until workstream 3, a bridge with a preamble that failed a check (BF3)
  met the same outcomes. No bridge test sets `checks`.
- **Status**: Fixed by workstream 3: `gcode_preamble` is gone (D4), and
  the default preamble passes every check on every model the bridge writes
  to, so the job raises no question under any of the printer's check
  settings. Workstream 2 flags a job that still meets one, such as the
  INDX lock question (BF1) once writes to INDX are on.

### BF5. A declined or refused sync job uses up a job ID and never finishes

- **Printers**: All the bridge writes to; INDX once BF14 is built.
- **What happens**: An upload skips the preview screen, so the job ID is
  taken at once. While a question waits, the state is `ATTENTION`,
  `/api/v1/status` has the job with its new ID, and `/api/v1/job` answers
  `PAUSED`. Declining goes to `IDLE`, not `STOPPED` or `FINISHED`, with
  the ID used up (`wui_api.cpp:291`, `marlin_print_preview.cpp:414-425`,
  `marlin_server.cpp:1113-1136, 2300-2331`). Until workstream 2, the
  bridge waited while the printer read `ATTENTION`, knowing the job by its
  file name (W6). After a declined job the printer still differed, so the
  bridge uploaded once more, and after two jobs (`MAX_SYNC_ATTEMPTS` in
  `app.py`) it flagged the spool "didn't take" (W5).
- **Effect**: Each attempt held the printer at a question until someone
  answered, and the spool was flagged only after the second. No charge
  changed.
- **Evidence**: Read in the firmware source. The simulator models the
  printer's side (F27; `test_the_preview_counts_the_job_then_asks` and
  `test_abort_at_the_preview_ends_idle_with_the_id_used` in
  `simulator/tests/test_model.py`). The bridge's side is shown by
  `tests/test_sync_question.py` and `test_w5_declined_sync_job_is_flagged`
  in `integration/test_writes.py`, which decline a sync job.
- **Scenario rows**: W2, W5, W6.
- **To reproduce**: `tests/test_sync_question.py` and
  `test_w5_declined_sync_job_is_flagged` patch
  `printer_sync.default_preamble` to leave out the input-shaper line, so
  the question is raised, then answer it with `cancel` instead of
  `tap_through`, polling between. With `checks` set to `abort`, `cancel`
  is the only answer. They now show the fix.
- **Status**: Fixed by workstream 2. `PrinterStatus.sync_job` tells the
  bridge's job at a question apart from no job. While it waits, the spool
  is flagged (`WriteAsked`). If it's then gone with the printer still
  different, the spool is flagged at once with that cause
  (`WriteDidntTake` with `asked`), and no second job is uploaded until a
  new spool, an unload or an edit in Spoolman. A job declined between two
  polls, with no poll seeing the question, is still retried once. Shown by
  `tests/test_sync_question.py` and `test_w5_declined_sync_job_is_flagged`
  in `integration/test_writes.py`.

### BF6. A failed `M865` is silent

- **Printers**: All the bridge writes to.
- **What happens**: The job ends `FINISHED` whether or not the printer
  took its `M865`. A name that's empty, `---` or has a character outside
  `a-zA-Z0-9_-` prints a serial error and stores nothing
  (`M865.cpp:133-138`, `filament_common.cpp:27-42`), and so does a preset
  name the build lacks (`M865.cpp:73-78`, `FilamentType::from_name` in
  `src/common/filament_common.cpp`). A name longer than 7 characters is
  dropped without an error: the parser stops at the 7-character buffer
  and skips `N` (`M865.cpp:132-133`, `gcode_parser.cpp:153-155`), and its
  error report does nothing because `M865`'s parser has no error callback
  (`M865.cpp:57`, `gcode_parser_helper.cpp:21-24`). The rest of the line
  is stored. The bridge's line has `R`, which resets the name, so the tool
  loads a type with an empty name (`M865.cpp:83-85`); without `R`, the
  type keeps its old name. `printer_name` in
  `printer_sync.py` keeps the bridge's names inside these rules, and cuts
  them to 7 characters (`NAME_LIMIT`).
- **Effect**: A late flag only. The printer still differs, so the bridge
  flags the spool "didn't take" after the second job (W5). The bridge
  can't write a name longer than 7 characters, so the silent drop doesn't
  reach it.
- **Evidence**: Read in the firmware source. The simulator runs each
  case and ends the job `FINISHED` (`simulator/sync.py`,
  `simulator/tests/test_model.py`, `simulator/tests/test_sync.py`).
- **Scenario rows**: W1, W5.
- **To reproduce**: A sync job in the simulator with a refused name, such
  as `N"P L A"`, or a preset the build lacks. No bridge test drives a
  failing `M865`.
- **Status**: Accepted (D11 in bridge-fixes.md). Nothing the bridge reads
  says an `M865` failed. The bridge writes only names inside the
  firmware's rules and preset names that `script/firmware-check`
  compares, and W5's flag catches the rest.

### BF7. An INDX with an uncalibrated nozzle cleaner refuses every print

- **Printers**: Core One INDX, Core One L INDX.
- **What happens**: An INDX whose nozzle-cleaner calibration selftest
  hasn't passed refuses every print, the sync job included: the check
  `nozzle_cleaner_not_calibrated` is an abort whatever the settings
  (`gcode_compatibility.cpp:143-152, 392-396`). Unreachable today, as BF1,
  while writes to multi-tool models are refused (BF14).
- **Effect**: Every write fails and goes the way of BF5. The "didn't take"
  flag lists the differences, not the cause.
- **Evidence**: Read in the firmware source.
- **Scenario rows**: W5, T6.
- **To reproduce**: Can't be shown. The simulator's preview doesn't model
  an uncalibrated nozzle cleaner (`simulator/README.md`), and the bridge
  uploads nothing to an INDX while it refuses multi-tool writes (BF14).
- **Status**: Open; workstream 7 owns it. Workstream 2 flags the outcome:
  the refused job is flagged while it waits at the question and once it's
  gone, after one job, telling the user to check the printer's screen and
  its check settings. The flag still can't name the cause.

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

### BF26. A sync job in the power-panic window drops the resume

- **Printers**: Every model with power panic that the bridge writes to:
  MK3.9, MK3.9S, MK4, MK4S, Core One, Core One Oak, Core One L. INDX and
  the XL once BF14 is built.
- **What happens**: After a power cut the printer boots and reads `IDLE`
  with no job for at least 1.5 s, until the firmware's autostart resumes
  the print (F12; `print_utils.cpp:67-82`, `printer_state.cpp:376-384`).
  A write that waited for the print (W2), such as a Spoolman edit of the
  loaded spool (W14), is still due. Until workstream 1, `sync_delay` was
  0 by default, and the bridge's first poll after the boot saw an idle
  printer, read the status again right before the upload, found `IDLE`
  again, and uploaded its sync job (`sync_printer` in `app.py`). The job
  starts a print. At its next try the autostart finds the printer
  printing and gives the resume up, unless the USB drive, the
  temperatures and the screen are all ready by then. Then it resumes over
  the running job: its check that the printer is idle is a debug assertion
  (`print_utils.cpp:75-79`, `power_panic.cpp:269-271`). A positive
  `sync_delay` held the job at the first idle poll after the boot, but a
  second cut during the resume could put the next poll in a second window
  with the delay already passed. Since workstream 1 the idle clock starts
  over at each new `boot_id`, so the first idle poll after any boot only
  starts it. With an MMU3, the sync job waits
  at the preview's question to unload the MMU's filament first and reads
  `ATTENTION` until someone answers (F43, BF18): while the MMU still
  connects after the boot, whatever FINDA sees, and once it's Active
  because the cut's slot is still at FINDA. If No runs the job while the
  MMU still connects, its end isn't the MMU's: it auto-retracts, which
  counts on slot 0 (F45).
- **Effect**: The user's print was lost: it never resumed, and the printer
  ended the sync job `FINISHED`. If the autostart resumed anyway, the two
  jobs collided in a way the source doesn't define. This now happens only
  with `sync_delay = 0`.
- **Evidence**: Read in the firmware source. Shown in the simulator on an
  MMU by `test_a_sync_job_in_the_window_on_a_connecting_mmu_waits_on_the_question`
  in `tests/test_mmu.py` and the window tests in
  `simulator/tests/test_mmu_power_panic.py`. To be
  confirmed on the printer: whether PrusaLink answers before the
  autostart's first try. The F12 trace shows the window, and has to be
  taken before the INDX upgrade.
- **Scenario rows**: P12, W2, W14.
- **To reproduce**: Set `sync_delay = 0`, as the MMU test does.
- **Status**: Fixed (workstream 1). `sync_delay` is 30 s by default, and the
  idle clock starts over at each new `boot_id`, so the first idle poll after
  a boot, a second cut's included, only starts it.
  `test_a_write_waits_out_the_window_and_the_print_resumes` and
  `test_a_second_cut_after_an_idle_poll_in_the_first_window` in
  `tests/test_power_panic.py` show the print resume, and
  `test_w2_write_waits_out_the_power_panic_window` runs it end to end.
  `sync_delay = 0` still runs a write in the window, and the MMU test stays
  pinned at 0 until workstream 9.

## Charging

### BF9. The auto-retract charges filament that wasn't used

- **Printers**: Every Nextruder: the MK3.9 and MK4 families, Core One,
  Core One Oak, Core One L, iX and XL. INDX counts its auto-retract
  exactly. With an MMU3, Load To Nozzle auto-retracts on the slot it
  loaded (F41).
- **What happens**: After an idle load and at the end of every print, the
  sync job included, a Nextruder runs its auto-retract ramming. It first
  feeds back what the tracker says is retracted, then counts 23 mm: an
  8 mm feed that leaves the nozzle and 15 mm that never does. The next
  forward move counts 15 mm more that never leaves the nozzle (F26;
  `standard_ramming_sequence_nextruder.cpp`, `ramming_sequence.cpp`,
  `mapi/motion.cpp`, `marlin_server.cpp:763-770, 1045-1080`,
  `pause.cpp:1041-1044`). It's off for
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
  Purge filament since its last load or print, or one loaded at this
  print's preview (BF23, F28), the ramming counts about 22 mm (23 mm
  commanded at 0.95) while the job is still visible.
  `accounting.print_start` takes the first counter rise in a new job as
  the print's start (Q4), so it takes the ramming as one. A cancel during
  the scan or the start blip moves nothing and ends in `IDLE` (F27), so
  this takes a cancel later in the preheat.
- **Effect**: A print cancelled before it extruded counts as started. An
  open unconfirmed swap closes as "assumed same spool" with a provisional
  charge (S4, Q1), and a staged spool is flagged (S9, Q2). A Spoolman fix
  made after the cancel's ramming no longer confirms the swap, against
  what P8 promises. A fix the bridge polls during the preheat, before the
  cancel, still confirms it.
- **Evidence**: Inferred from the source and `accounting.py`. Reproduced,
  no test kept: the bridge records `swap_assumed`, a provisional 22 mm
  charge and `swap_staged`. Through the unit harness, a tool loaded at
  the preview gives `swap_assumed` and the 22 mm charge too, with or
  without polls during the load (BF23). The P8 test doesn't show it: its
  tool is already auto-retracted, it fixes Spoolman before the cancel,
  and its `finish()` polls every 1 s while the job parks for 0.2 s.
- **Scenario rows**: P8, Q4, S4, Q1, S9, Q2.
- **To reproduce**: Start from
  `test_p8_cancelled_during_preheat_is_not_a_print_start` in
  `integration/test_prints.py`. Open an unconfirmed swap as it does, then
  feed the tool (`extrude`) after the load. Start the print, `cancel` in
  the preheat, and poll every 0.1 s while the job parks. Fix Spoolman only
  after the cancel. Expect `swap_assumed` for the job. BF23's cancel
  path, with polls during the load and the auto-retract on, shows the
  same after a load at the preview.
- **Status**: Open.

### BF11. A power-panic resume loses usage and counts a purge

- **Printers**: All with counters. The purge differs by build.
- **What happens**: The counters restart at boot and aren't in the
  power-panic data, so usage since the bridge's last poll is lost. The
  resume then moves filament. INDX purges 25 mm with `G12 S21`
  (`power_panic.cpp:480-488`, `nozzle_cleaner.cpp:96-109`). The MK4
  family, MK3.5, MK3.5S, Core One, Core One L and iX feed back the 8 mm
  retracted at the cut, and the XL moves nothing
  (`power_panic.cpp:110-116, 518-521`). The MINI and the XL dev kit have
  no power panic and never resume (`ProjectOptions.cmake:334-345`). The
  purge sits under `HAS_NOZZLE_CLEANER`, which only the iX and the INDX
  builds set (`ProjectOptions.cmake:755-756`), and the iX's plain `G12`
  wipes without a purge (`power_panic.cpp:489-491`,
  `nozzle_cleaner.cpp:157-190`). The bridge charges whatever counts to
  the spool active after the boot. When the filament was pulled out while
  the power was off, the idle sensor clear in the window before the resume
  counts an unload first (F12), and the purge or feed counts on the empty
  tool: that's BF24.
- **Effect**: Up to one poll interval of printing, 30 s by default, is
  never charged. The purge or feed is charged to the active spool, which
  is right if it leaves the nozzle; on a plain Core One L the source says
  8 mm.
- **Evidence**: Read in the firmware source. The size on the Core One L
  is to be confirmed: the F12 trace decides, and has to be taken before
  the INDX upgrade. The simulator follows the source per model.
- **Scenario rows**: P12, Q5, C1.
- **To reproduce**: `test_p12_power_panic_resume_keeps_the_job` in
  `integration/test_prints.py` shows the loss and the 8 mm fed back on a
  Core One L, 7.6 mm counted. `simulator/tests/test_model_power_panic.py`
  shows the amount per model. With the filament pulled out while off,
  `test_an_unload_in_the_window_then_the_resumed_print_is_lost` in
  `tests/test_power_panic.py`. A second cut during the resume charges a
  second feed back, which leaves the nozzle, since that cut doesn't
  retract: `test_a_second_cut_during_the_resume` there.
- **Status**: Open. Under D11 in bridge-fixes.md it becomes Accepted once
  the F12 trace confirms the 8 mm on the Core One L. The lost usage needs
  counters the firmware doesn't keep across a power cut, and the feed
  back leaves the nozzle, so its charge is right. printer-behaviors.md,
  under "Bridge follow-ups found here", calls both parts fine.

### BF23. A print declined after the preview's load can count as started

- **Printers**: Single-tool models with counters. A toolchanger's
  tools-mapping screen loads nothing.
- **What happens**: A print started on a tool with no filament asks for a
  load at the preview (F28). Yes queues `M701 S"<the file's type>" T<n>
  W2`, and the load preheats for and stores the file's type, so the type
  check after it passes (`marlin_print_preview.cpp:307-309, 326-335`,
  `M701_2.cpp:66-98`). Only when the file's type is a name the build
  doesn't know does the load ask for a type. No pick can match that name,
  so the wrong-filament question follows, and the user can decline there.
  The load counts while the printer reads `BUSY` with no job: 57 mm on a
  Core One L, (20 + 40) × 0.95. `accounting.print_start` takes a new job
  as started when a later poll of the same job sees the counter past
  where the first poll saw it. With one poll at the preview's first
  question and none during the load, the next poll of the job takes the
  load's count as the print's first extrusion: at the wrong-filament
  question, or in the preheat once the print goes on. A cancel in that
  preheat then ends a job already taken as started. That's BF10's outcome
  even with no ramming, as with auto-retract off. With auto-retract on,
  the preview's load leaves the tool not retracted (F28), so the cancel
  rams too, and that alone counts the job as started through BF10.
- **Effect**: A job that never printed counts as started. An open
  unconfirmed swap closes as "assumed same spool" with a provisional
  charge (S4, Q1), and a staged spool is flagged (S9, Q2), as in BF10. It
  takes a print started on an empty tool, no poll during the load, and
  then a cancel in the preheat or a declined file whose type the build
  doesn't know. The simulator's load is 108 mm at 10 mm/s, about 11 s. A
  Core One L moves 40 mm at 6 mm/s, 20 mm at 25 mm/s and a 40 mm purge at
  3 mm/s, then retracts 8 mm: about 21 s
  (`standard_feedrates_nextruder.cpp:12-18`). It also reads `BUSY` while
  it heats to the load temperature and waits for the filament to go in,
  so a 30 s `poll_printing` lands in the load far more often than in the
  simulator. That poll avoids this finding's load path, but with
  auto-retract on, a cancel in that preheat still counts as started
  through BF10. The live check will tell.
- **Evidence**: The preview's load is read in the firmware source.
  Reproduced through the unit harness, no test kept, for both paths: a
  decline at the wrong-filament question and a cancel in the preheat each
  give `swap_assumed`. The simulator reaches the wrong-filament question
  for any type, because its preview load is the user's free choice of
  preset (`simulator/README.md`). A poll during the load avoids the load
  path: the bridge sees no job then, and takes the job's counter afresh
  when the job comes back. With polls every 0.2 s during the load, the
  cancel path still gives `swap_assumed` and a provisional 22 mm charge
  with the auto-retract on (BF10), and nothing with it off. The printer's
  timing is to be confirmed.
- **Scenario rows**: P8, Q4, S4, Q1, S9, Q2.
- **To reproduce**: Unload the tool's spool and poll, to open an
  unconfirmed swap. Start a print on the empty tool, and poll at the "no
  filament" question. `tap_through`, then `load` the tool, with no poll
  during the load. For the decline, load another type than the segment
  names, poll at the wrong-filament question, then `cancel`. For the
  cancel, load the segment's type, poll in the preheat, then `cancel`.
  Either way the ledger shows `swap_assumed` for the job. To see the
  cancel path through BF10, poll during the load with the auto-retract
  on, which the unit harness leaves off (BF22). On the Core One L, the
  live check `F28-load-then-decline` confirms it with the production
  bridge: sitting D in printer-behaviors.md, under "Capture plan".
- **Status**: Open.

### BF24. Usage counted on an empty tool lands on the next spool

- **Printers**: Single-tool models with counters, when the user turns the
  filament sensors off at the filament-not-inserted screen instead of
  loading (F29). Core One INDX, Core One L INDX and XL, at the
  tools-mapping screen. Every model with power panic and counters, after a
  power cut with the filament pulled out while off, or during a filament
  change.
- **What happens**: On the tools-mapping screen, Print with a used tool
  empty first asks to turn the filament sensors off, and only that lets
  the print go on. When the file names a type for the empty tool, a type
  question follows, which Ignore answers (F28, F29). A single-tool printer's
  filament-not-inserted screen offers the same, and the print goes on
  alike (F29). The print then runs through the empty tool, and the tracker
  counts every move on that tool with no material, a toolchanger's
  priming and purges included. A power-panic resume gets there a second
  way: when the filament was pulled out while the power was off, the idle sensor clear
  in the window before the resume counts the unload and clears the type,
  and the resumed print goes on through the empty tool, since a runout
  needs the sensor to see the filament go (F12, F29). The resume's feed
  back or purge counts there too (BF11). A power cut during a filament
  change gets there a third way: the dialog isn't a pause, so a cut while
  a runout waits for its reload resumes the print on its own, through the
  tool the runout emptied (F12). A cut during Tune > Change filament on
  another tool, or during a spool join's unload, does the same on that
  tool until the file's next tool change (F34, F35). Until workstream 5, the bridge
  held that usage because the tool reported no material (C7). When
  filament was next loaded in that tool, the bridge charged the held
  usage, with the load's own count, to the spool reported there. An MMU's runout writes no type,
  so a cut while it waits holds nothing: slot 0 keeps its type, and the
  resumed print counts on slot 0 while the MMU connects and on no slot
  after (F44, BF17).
- **Effect**: Until workstream 5, the new spool was charged for filament
  that came from no spool: in the test, the held 84 mm on top of the
  load's own count.
- **Evidence**: On a toolchanger, shown in the simulator on an INDX by
  the test now named
  `test_a_print_through_an_empty_tool_reports_its_usage_lost` in
  `tests/test_toolchanger_prints.py`, which pinned the charge before the
  fix: the held 84 mm plus the load's 70. On a single-tool
  printer, read in the firmware source (F29): the simulator doesn't model
  that path. After a power cut, shown in the simulator on a Core One L by
  the test now named
  `test_an_unload_in_the_window_then_the_resumed_print_is_lost` in
  `tests/test_power_panic.py`: the bridge records the unload and
  `swap_unconfirmed` in the window, and before the fix held the usage of
  the resumed print and charged it with the next load's count to the spool
  reported then. After a cut while a runout waits for its reload, on a
  Core One L, by the test now named
  `test_a_cut_while_a_runout_waits_loses_the_resumed_print` in the same
  file: the swap the runout opened stays open, and before the fix the
  bridge held the resumed print's usage and charged it with the next
  load's count to the spool reported then. On an MMU, by
  `test_a_cut_while_an_mmu_runout_waits_charges_nothing_after` in
  `tests/test_mmu.py`: no unload is recorded, no usage is held, and spool 1
  is charged only what slot 0 counted before the runout.
- **Scenario rows**: C7, T2, P12.
- **To reproduce**: Those tests, before the fix, on a toolchanger and
  after a power cut. On the Core One L,
  the live check `F29-sensors-off-print` confirms the printer's side with
  the production bridge: sitting D in printer-behaviors.md, under "Capture
  plan".
- **Status**: Fixed (D6 in bridge-fixes.md). Usage counted on a tool with
  no material while a user's print runs, in `PRINTING` or `PAUSED` with a
  job that isn't the sync job, is reported lost (`lost_empty`) and
  charged to no spool, and so is the print's last stretch, first seen at
  the poll after it ends. The tests above now charge the next load its own
  count only, and `test_c7_print_through_an_empty_tool_is_reported_lost`
  in `integration/test_counters.py` shows it end to end. A load's feed,
  and a filament change's reload in `ATTENTION`, are still held for the
  spool loaded (C7), and so is usage in `BUSY`, which the printer never
  reports with a job. A power-panic resume's feed back on an empty tool
  counts in `BUSY`, so it's held, and the next `PRINTING` poll that sees
  the tool count reports it lost with the print's usage. A remaining
  limit: if a second cut lands during the resume, the reboot keeps that
  held feed back, about 8 mm, and charges it to the active spool. The F29
  trace still confirms the single-tool path.

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

### BF27. A filament change before a tool's first extrusion in a print looks like its print start

- **Printers**: Core One INDX and Core One L INDX always, through Tune >
  Change filament on a tool the print hasn't used yet. The XL the same
  way, and every single-tool model with counters through a runout during
  a print's preheat, but only when the tool isn't fully auto-retracted.
- **What happens**: Tune > Change filament on a toolchanger asks for a
  tool and sends `M600 P T<n>`, which picks that tool, changes its
  filament and changes back (F34, `src/marlin_stubs/pause/M600.cpp:204-340`,
  `src/gui/screen/screen_m600.cpp`). The change counts an unload on the
  tool, and its ramming counts too. `accounting.print_start` takes a
  tool's first counter rise in a new job as that tool's print start (Q4).
  On a tool that hasn't extruded in the running job, the ramming is that
  rise, so the bridge sees the unload and the tool's print start in the
  same poll: it records `unload` and `swap_unconfirmed`, then at once
  `swap_assumed` with a provisional charge, and flags the spool as assumed
  reloaded. On a tool the print has already extruded with, the swap stays
  open as P2 and P3 say. A runout during a print's preheat, before any
  extrusion, takes the same path on a single-tool printer. It needs the
  unload to count something. A Nextruder that is fully auto-retracted, as
  it is after a menu load or a finished print, unloads cold with no
  ramming (`can_cold_unload`, `pause.cpp:1249-1255`), so nothing counts
  and the swap stays open as P2 says. A Nextruder rams when auto-retract
  is off, with flexible filament, and on an XL tool that an earlier print
  used but didn't end on. INDX never unloads cold, so it always rams.
- **Effect**: A Spoolman change made while the printer waits for the
  reload, which P2 promises confirms the swap, is staged instead (S13),
  and the reload and the rest of the print go to the old spool until a
  claim moves them. With no Spoolman change, the old spool carries an
  "assumed reloaded" flag where P3 would leave the swap open.
- **Evidence**: Shown in the simulator on an INDX by
  `test_a_change_on_a_tool_the_print_has_not_used_looks_like_its_print_start`
  in `tests/test_toolchanger_prints.py`. Reproduced, no test kept: the
  same change on a tool the print used earlier follows P2, and a runout in
  the preheat of a Core One L print gives `unload`, `swap_unconfirmed` and
  `swap_assumed` in one poll when the tool isn't auto-retracted, and
  nothing more than P2 when it is. That a real runout can come before the
  print's first extrusion is inferred: the start G-code's heat waits come
  before its purge line.
- **Scenario rows**: T10, P2, P3, P8, Q4, S13.
- **To reproduce**: That test, today. For the single-tool path, start a
  print with a preheat on a Core One L through the unit harness, with the
  tool not auto-retracted (the harness's starting state, or `auto_retract`
  off), poll during the preheat, `runout(0)`, and poll.
- **Status**: Open.

## Multi-tool

### BF12. Hand swaps on a toolchanger can be missed

- **Printers**: Core One INDX, Core One L INDX, XL.
- **What happens**: The idle sensor clear runs only on the picked tool
  (`filament_sensors_handler.cpp:294-312`). An INDX docks its tool after a
  load, an unload, a pause and a print, so an idle INDX has none picked
  (`M701_2.cpp:135-140, 194-197, 339-342`,
  `marlin_server.cpp:1098, 2561`). A finished print's end G-code parks the
  tool on an XL with several tools too (`P0 S1` in the stock profile), and
  that XL otherwise
  has whichever tool is on the head (F32). A docked or parked tool emptied
  by hand keeps its type and counts no unload, and nothing sweeps the
  tools at boot or at a print start. On an idle INDX only a menu load,
  unload or Pick/park of that tool counts it: each picks the tool first,
  with no dialog open, and the clear then counts the unload (F23, F30). At
  a print start the tools-mapping screen shows a hand-emptied used tool as
  having no filament, but that counts no unload either, and the idle
  sensor clear it allows runs on the picked tool only (F28). Printing past
  it is BF24 and BF25.
- **Effect**: The bridge sees no unload, so a hand swap opens no swap. A
  spool moved into the tool's location in Spoolman stays staged (S13, S9),
  and the old spool is charged for the new one until a claim moves it, or
  until the user next loads, unloads or picks that tool from the menu. The
  menu's pick runs the idle clear first, which counts the unload, so a menu
  load commits the staged swap before the load counts. A print's preview
  load picks inside its dialog and counts no unload.
- **Evidence**: Read in the firmware source. Shown in the simulator:
  `test_a_tool_emptied_by_hand_while_docked_counts_no_unload` in
  `tests/test_toolchanger_prints.py`.
- **Scenario rows**: T3, T4, T8, S9, S13.
- **To reproduce**: `test_a_tool_emptied_by_hand_while_docked_counts_no_unload`:
  an INDX finishes a print, so no tool is picked; T1 is emptied by hand
  while off and its spool swapped in Spoolman. The bridge sees no unload
  and the new spool stays staged. A menu load of T1 then counts the unload
  and commits the swap. T4 shows the picked-tool case.
- **Status**: Open.

### BF13. A spool join counts an unload in the middle of a print

- **Printers**: Core One INDX, Core One L INDX, XL.
- **What happens**: A runout with a spool join counts an unload on the old
  tool mid-print, while the job reads `ATTENTION`, and the print goes on
  with the join's tool (F35). The join's tool's type isn't set or copied,
  and its purge counts on it: 27 mm and an 8 mm retract on the XL, the
  nozzle cleaner's 25 mm and its auto-retract on INDX
  (`spool_join.cpp:239-365`, `toolchanger_xl.cpp:505-571`,
  `nozzle_cleaner.cpp:87`). The bridge takes the old tool's unload as a
  runout during a started print: it records `unload` and
  `swap_unconfirmed` there and flags the old spool (`unload` in
  `accounting.py`), and with more than 5% of its net weight left in
  Spoolman flags it as run out mid-print too (M5a; `_check_runout` and
  `RUNOUT_LEFT` in `app.py`). The swap stays open after the print (P3). The join's tool's first
  count in the job is its purge, which the bridge takes as that tool's
  print start (`print_start` in `accounting.py`), and the rest of the print is charged to the spool in its
  location, with no event.
- **Effect**: Not settled. The charges follow the filament when each tool's
  location holds its spool. The old tool's swap and flags follow rules
  written for a reload of the same tool: they ask for a spool to be moved
  into a tool the print has left empty.
- **Evidence**: Read in the firmware source. Shown in the simulator on an
  INDX by `test_a_spool_join_counts_an_unload_on_the_old_tool_mid_print`
  in `tests/test_toolchanger_prints.py`.
- **Scenario rows**: T9, P2, P3, P4, M5a, T2.
- **To reproduce**: That test, today.
- **Status**: Open. D11 in bridge-fixes.md leaves it open: its effect
  isn't settled, and it's decided with workstream 8, once a spool join
  can be seen on an INDX.

### BF14. Writes are refused on multi-tool models

- **Printers**: Core One INDX, Core One L INDX, XL, XLP.
- **What happens**: `model_problem` refuses every multi-tool type in
  `PRINTER_MODELS` ("multi-tool printers aren't supported yet"). A due
  write flags the spool "can't write to the printer" and uploads nothing.
  The job itself already writes per tool: `M865 L<n>` for each tool due
  (F22), and `M865.1` only on tool 0 of a model that keeps UUIDs, which
  these don't (F21). Only the refusal stops it. The tools-mapping
  screen won't stand in its way: the sync job names no filament used, so
  the screen doesn't apply (F28), and on INDX only the lock question asks
  (BF1).
- **Effect**: After the INDX upgrade the owner's printer gets no writes. A
  menu load keeps the preset's name and temperatures, and each write that
  would be due leaves a flag. Charging isn't affected. Lifting the refusal
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
L with an MMU3 turned on in the printer's settings. The simulator models
the MMU's switch, its selected slot, its idle operations, its prints, the
preview's unload question, its runouts, `M600` and spool joins, and power
panic (F38 to F46). The MMU's scenario rows are T12 to T17.

### BF15. MMU: swaps are invisible

- **What happens**: An unload counts only where a slot's type is written
  to none (`store_definition.cpp:258-263`). A tool change, Unload
  Filament, Eject, Cut, `M600` and a spool join write no type. Load To
  Nozzle writes back the slot's own type. Preload To MMU writes the new
  type over the old one after its preload, with no none between
  (`multi_filament_change.cpp:151-239`, F41). The idle sensor clear is off
  by the MMU's setting, even while it connects
  (`filament_sensors_handler.cpp:161-182, 303-312`). What does count: `M709
  S0` with an empty extruder sensor, on slot 0 only (BF28), Preload's
  unload choice, a cold pull on slot 0, a filament-stuck unload on the
  selected slot, and `M865 I` with an empty type.
- **Effect**: The usual swap, Eject and then Preload with the new type,
  shows the printer only a type change. A spool moved into the slot's
  location stays staged (S13, S9), and the old spool is charged for the
  new one, with a drift flag for the new type, until a claim moves it. A
  swap at a runout shows the printer nothing at all: the `M600` ejects the
  slot and reloads it with its type kept (F44), so the new spool stays
  staged and the old one is charged, with no flag.
- **Evidence**: Read in the firmware source; shown in the simulator by
  `test_an_mmu_slot_swap_by_eject_and_preload_counts_no_unload` and
  `test_an_mmu_runout_swap_counts_no_unload` in `tests/test_mmu.py`.
- **Scenario rows**: S1, S2, S9, S13, T13.
- **To reproduce**: That test: with spool 3 moved into slot 0's location,
  slot 0 is ejected and preloaded with PETG. No unload is recorded, spool 3
  stays staged, and the next print's 103 mm goes to spool 1, which is
  flagged for the printer's PETG. The second test swaps spool 3 in while a
  runout waits for its reload: the print's 125 mm goes to spool 1, with no
  flag.
- **Status**: Open.

### BF16. MMU: each tool change over-charges the outgoing slot

- **What happens**: At a tool change in a print the firmware retracts
  80 mm and feeds up to 50 mm (`ToolChangeCommonOnce`, `mmu2_mk4.cpp:568-679`,
  through `FeedWithEStallDetection`, `:492-546`), and plans
  the 189.9 mm move to the filament sensor and 7 mm past it as the MMU
  reports its progress (`maximumAllowedExtruderMove`, `:55`, planned at
  `:1528` in `OnMMUProgressMsgChanged` and `:1590` in
  `OnMMUProgressMsgSame`; `ExtraLoadDistance() + 2`, `:1583`;
  `mmu2_config.h:35, 44, 69`). All of it runs while the old slot is still
  selected (`mmu2_mk4.cpp:681-702`). The planner counts each move at plan
  time, less whatever is left of the gap, which is at most 40 mm (F39),
  so the old slot is credited 193 to 207 mm, 206.9 with the nominal 50
  (F40). The feed leaves the gap at 0, so the new slot's 32 mm slicer
  load then counts in full, though most of it fills the path to the
  nozzle. The slicer's wipe-tower stamping on the old slot also counts
  15 mm of each stamping past the gap: one stamping per change for PLA,
  two for PETG (F46). The first change of a print, from no slot, credits
  nothing, and a failed load's retries add nothing.
- **Effect**: The outgoing slot's spool is charged 221.9 x 0.95 = 210.8
  mm more per change for PLA and 236.9 x 0.95 = 225.1 mm for PETG, about
  0.63 g.
- **Evidence**: Read in the firmware source and the stock slicer profile;
  shown in the simulator by
  `test_an_mmu_tool_change_charges_the_outgoing_slot_the_firmware_feed` in
  `tests/test_mmu.py`, and through a real Spoolman by
  `integration/test_tools.py::test_t12_two_color_mmu_print`. The feed's last move stops at the sensor, between
  36 and 50 mm, which only hardware could tell.
- **Scenario rows**: T12.
- **To reproduce**: That test: a print on slots 0 and 1, 20 mm each, in the
  order 0, 1, 0, 1, makes three changes, and each slot rams out twice:
  slot 0 at both changes to slot 1, and slot 1 at its change to slot 0
  and at the end. In commanded mm, slot 0 counts 657.65 and slot 1
  467.38, and the counters read 624 and 444 at 0.95, which the bridge
  charges. The start, the model, the loads, the purges and the rammings
  come to 213.85 mm on slot 0 (70 + 40 + 32 + 29.95 + 2 x 20.95) and
  230.48 mm on slot 1 (2 x 32 + 54.63 + 29.95 + 40 + 2 x 20.95), 203 and
  219 at 0.95. The rest is the firmware's feed and the stampings: 2 x
  (206.9 + 15) = 443.8 mm on slot 0, and 206.9 + 2 x 15 = 236.9 mm on
  slot 1, whose last stamping comes at the end with no feed after it.
- **Open question**: Whether the charge a fix aims for counts the
  slicer's 17 mm and 32 mm loads. The printer counts them, but most of
  that filament fills the path to the nozzle and comes back at the next
  unload.
- **Status**: Open.

### BF17. MMU: after a boot, slot 0 is charged while the MMU connects, then nothing until a load or tool change

- **What happens**: The planner counts each move on the selected virtual
  tool. That's slot 0 while the MMU's setting is on but the MMU isn't
  Active, as while it connects after a boot or `M709 S1`, and then the
  selected slot, or none (`tool_index.cpp:140-158`,
  `planner.cpp:2194-2220`). Every boot, `M709 S1`, unload, eject and cut
  leaves no slot selected, and only a tool change or a load selects one
  (`mmu2_mk4.cpp:155, 175, 674, 869-871, 913, 1013`). A power-panic resume
  is a boot, and the power-panic data holds nothing about the slot (F45).
  The resume's 8 mm feed back counts on slot 0 while the MMU still
  connects, else on none. The resumed print counts on
  slot 0 until the MMU is Active, whichever slot prints, then on none. Its
  next tool change runs from no slot: the old slot's wipe-tower unload,
  before the `T`, counts on slot 0 or on none, the firmware's feed counts
  on none, and the new slot counts from its load on.
- **Effect**: Usage from a boot to the next load or tool change is charged
  to slot 0's spool while the MMU connects, whichever slot printed, and
  isn't charged at all after. A resumed one-color print is charged nothing
  after its first seconds. In the simulator, with the MMU connecting for
  10 s and the test's print at 5 mm/s, a print cut in slot 1's segment
  counts 35.5 mm commanded, 33.7 counted, on slot 0 after the boot, and
  nothing on slot 1 to the print's end. That's the 8 mm feed back, 3.5 s
  after the boot in the simulator's timing (its 1.5 s window and 2 s
  recovery), and the 27.5 mm slot 1 prints from the print's restart, 4.5 s
  after the boot, until the MMU is Active.
- **Evidence**: Read in the firmware source; shown in the simulator by
  `test_an_mmu_print_resumed_after_a_cut_is_charged_nothing_until_a_tool_change`
  and `test_the_resume_feed_back_lands_on_slot_0_while_the_mmu_connects`
  in `tests/test_mmu.py`, and by
  `simulator/tests/test_mmu_power_panic.py`. How long the MMU takes to
  connect is inferred, about 10 s from a comment at
  `filament_sensors_handler.cpp:179`, and it decides how much lands on
  slot 0.
- **Scenario rows**: C1, P12, T15.
- **To reproduce**: Those tests. A print on slots 0 and 1, cut 30 mm into
  slot 0's 100 mm segment and resumed with the MMU Active, charges spool 1
  nothing after the cut, and spool 2 211 mm from slot 1's load on. A print
  on slot 2 resumed while the MMU connects charges spool 1, in slot 0,
  7 mm for the feed back, and spool 3 nothing after the cut.
- **Status**: Open.

### BF18. MMU: the sync job can wait on the unload question and leave the filament

- **What happens**: With the MMU Active and FINDA seeing filament, any
  slot's filament past the selector, the preview asks to unload it first
  (F43). That's most often after Load To Nozzle: a stock MMU3 print ends
  with `M702`, which clears FINDA. While the MMU connects after a boot, it
  asks whatever FINDA sees (BF26). The shortcut for a single-tool file
  doesn't apply, since the sync job's metadata names no tool
  (`gcode_info.cpp:611-625`). The job waits in `ATTENTION` with its ID.
  Yes unloads into the MMU, reading `BUSY`, then turns the heaters off and
  runs the job. No runs it with the filament in place
  (`marlin_print_preview.cpp:228-257, 698-728`). A finished sync job
  neither unloads nor auto-retracts with the MMU Active, so the next job
  asks again; a stopped one unloads while reading `PRINTING` (F42,
  `marlin_server.cpp:1067-1075`). The bridge uploads once, and while the
  printer reads `ATTENTION` it flags the spool (W5).
- **Effect**: Each write after Load To Nozzle holds the printer at a
  question until someone answers, with no time-out. The bridge never
  uploads again, and the flag tells the user to answer the question.
  Answering Yes unloads the filament for a job that moves none. A stop at
  the question is flagged after one job (BF5).
- **Evidence**: Read in the firmware source; shown in the simulator by
  `test_a_sync_job_on_a_loaded_mmu_waits_on_the_unload_question` in
  `tests/test_mmu.py`.
- **Scenario rows**: T16, W1, W5.
- **To reproduce**: That test: after Load To Nozzle on slot 0, a color
  write uploads one job, which waits at the question through 20 minutes
  of polls with no second upload, with the spool flagged. No then runs
  it, the color lands, and slot 0's filament stays in the extruder.
- **Status**: Open; workstream 9 owns it. Workstream 2 flags the outcome:
  the job waiting at the unload question flags its spool, which
  `test_a_sync_job_on_a_loaded_mmu_waits_on_the_unload_question` now
  asserts, and a job stopped there is flagged after one job.

### BF19. MMU: slots 1 to 4 keep no UUIDs

- **What happens**: `M865` takes `L` from 0 to 4 on an MMU build, and
  stores the type for that slot (`M865.cpp:64, 194-196`,
  `store_definition.cpp:243-279`). An ad-hoc write stores the name,
  temperatures and color for any slot, but the UUIDs only for slot 0. Any
  ad-hoc write to slot 1 or higher, not only one with UUIDs, skips them
  behind a `debug_assert` that fires on a debug build
  (`filament.cpp:251-275, 379-403`). Only the Core One and Core One L keep
  UUIDs at all; the MK4 has no tag reader. While the MMU is on, the tag
  radio is off and no slot has a tag reader, so slot 0 keeps UUIDs from an
  earlier tag load but gets no new ones
  (`request_manager.cpp:262-269`, `tool_tag_buddy.cpp:11-18`). `M709 S1`
  clears every slot's tag link (F38). The bridge writes `M865 L<n>` for
  each configured tool, and mints and writes `M865.1` on tool 0 alone of a
  model that keeps UUIDs.
- **Effect**: None today. Every slot's type and color land, and tool 0's
  UUID lands on slot 0, so the printer matches every spool after one job
  and nothing is flagged. Slots 1 to 4 keep no UUID, so a spool there can't
  be identified by one (Q10), and slot 0 never reports a new tag. What the
  bridge makes of UUIDs slot 0 kept from an earlier tag load is to be
  confirmed.
- **Evidence**: Read in the firmware source; shown in the simulator by
  `test_mmu_writes_land_on_every_slot_and_uuids_on_slot_0` in
  `tests/test_mmu.py`, on a Core One.
- **Scenario rows**: W1, W5, Q10, Q11.
- **To reproduce**: That test: five spools with five colors on a Core One
  with its MMU on. One job writes `L0` to `L4` and `M865.1 I0` with the
  UUID the bridge minted for slot 0's spool, and no other spool gets one.
  After it, slot 0 holds that UUID, slots 1 to 4 none, and the next poll
  uploads nothing.
- **Status**: Open.

### BF20. MMU printers are on the supported list as single-tool models

- **Printers**: MK3.9, MK3.9S, MK4, MK4S, Core One and Core One L with an
  MMU3.
- **What happens**: `/api/v1/filament` lists the five slots as tools 0 to 4
  while the MMU's setting is on, connected or not, and tool 0 alone while
  it's off (`tool_index.cpp:84-109`). `/api/v1/info` reports `mmu` as the
  setting, true while the MMU connects too (`basic_gets.cpp:229-247`). The
  bridge doesn't read `/api/v1/info`. `PRINTER_MODELS` in `printer_sync.py`
  lists these types as single-tool and writable, so `model_problem` lets
  writes through, and the job's `M865 L1` to `L4` land on the slots
  (`M865.cpp:64, 194-196`).
- **Effect**: The bridge writes to an MMU printer as to a single-tool one,
  which leads to BF18 and BF19, and nothing tells the user an MMU isn't
  supported. Charging per slot follows the config's tool map.
- **Evidence**: Read in the firmware source and `printer_sync.py`, and
  shown in the simulator.
- **Scenario rows**: W10, T6.
- **To reproduce**:
  `test_the_bridge_writes_to_an_mmu_printer_as_a_single_tool_model` in
  `tests/test_mmu.py`: an MK4S with its MMU on reads `mmu` as true and lists
  five tools. A drift write uploads one job with `L0` to `L4`, which runs and
  sets every slot's type.
- **Status**: Open.

### BF28. MMU: turning the MMU off clears slot 0, whichever slot was loaded

- **What happens**: `M709 S0` writes none to slot 0 unless the extruder's
  sensor surely sees filament (`M70X_mmu.cpp:77-92`, F38). With an empty
  nozzle that's an unload on slot 0, whichever slot was used last. With
  another slot's filament in the nozzle, slot 0 keeps its type, and the
  printer, now a single-tool one, counts every move on slot 0
  (`tool_index.cpp:140-158`).
- **Effect**: With an empty nozzle the bridge records an unload on slot
  0's spool, which never left the MMU, and flags it. With slot 2 loaded,
  the bridge charges slot 2's filament to slot 0's spool, with no flag
  while slot 0's type matches it.
- **Evidence**: Read in the firmware source; shown in the simulator by
  `test_turning_the_mmu_off_with_an_empty_nozzle_unloads_slot_0` and
  `test_turning_the_mmu_off_with_slot_2_loaded_charges_slot_0` in
  `tests/test_mmu.py`. That the loaded slot's filament is then charged to
  slot 0 is inferred: nothing reads the slot back.
- **Scenario rows**: T14.
- **To reproduce**: Those tests. With an empty nozzle, the ledger shows
  an unload and an unconfirmed swap on slot 0's spool. With slot 2 loaded,
  a 100 mm move after the switch-off charges 95 mm to slot 0's spool.
- **Status**: Open.

### BF29. MMU: a load or print start after an unload in the same boot counts up to 40 mm less

- **What happens**: A tool change's feed runs with no slot selected, so it
  counts nothing and leaves the tracker's gap where it was (F39). After
  any MMU unload the gap is 40 mm, so the load that follows fills it
  first: Load To Nozzle counts 7 mm of its 47, where the first load after
  a boot, from a gap of 0, counts all 47 (F41,
  `mmu2_mk4.cpp:568-679, 961-987`).
- **Effect**: About 38 mm, 0.11 g of 1.75 mm PLA, isn't charged to the
  loaded slot's spool, and how much depends on what ran earlier in the
  boot. A print's start after an unload, including the unload at the end
  of an earlier print, counts 30 mm of its 70 the same way.
- **Evidence**: Read in the firmware source; shown in the simulator by
  `test_a_load_to_nozzle_after_an_unload_counts_7` and
  `test_a_second_mmu_print_in_one_boot_counts_less_at_its_start` in
  `tests/test_mmu.py`.
- **Scenario rows**: T17.
- **To reproduce**: Those tests. Load To Nozzle, Unload Filament and Load
  To Nozzle again on slot 0 charge 44, 19 and 7 mm. The same two-color
  print twice in one boot charges slot 0's spool 624 mm, then 587.
- **Status**: Open.

## Minor

### BF21. `READY` in `IDLE_STATES` never arrives

- **Printers**: All.
- **What happens**: Only Prusa Connect is told `READY`. PrusaLink reports
  the state underneath (`status_renderer.cpp:30`). `IDLE_STATES` in
  `prusalink.py` listed it until workstream 10.
- **Effect**: None. The entry is never matched.
- **Evidence**: Read in the firmware source.
- **Scenario rows**: None directly. W2 and W3 rely on `IDLE_STATES`.
- **To reproduce**: Nothing to show: the simulator never reports `READY`.
- **Status**: Fixed. `IDLE_STATES` no longer lists `READY` (workstream 10).

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
- **Status**: Accepted (D11 in bridge-fixes.md). Changing the harness's
  default would move many unrelated tests. A test that needs the ram
  turns the auto-retract on itself.
