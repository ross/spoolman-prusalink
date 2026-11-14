# Simulator: printer models, multi-tool, and MMU

This is the plan for making the simulator behave like each printer the
bridge can meet: every single-tool model, the toolchangers (XL and INDX),
and printers with an MMU3. It started as a close copy of one printer, the
Core One L, with the rest treated as that printer under a different name,
tool count and retraction gap.

Draft, 2026-11-13. Workstream 1 is built: each model has its profile and
its build's presets, and `script/firmware-check` compares them with the
source. The tracker counts with the model's gap and feed ratio, a load runs
the model's lengths, the ramming and auto-retract run per build, and the
tests run the bridge against each family. Workstream 2 is built: an
uploaded file goes through the preview's checks of its `M862` lines, and a
print with segments goes through its checks of the tools it uses. The MMU's
filament-inserted question belongs to Workstream 5. Workstream 3 is built:
a tool and its filament path are apart, the selected tool can be none, and
a feed system holds what differs between direct and toolchanger printers.
Workstream 4 is built: the XL and INDX have their own feed systems, a
menu load or unload picks its tool and INDX docks it after, Move E needs a
picked tool, and the picked tool rests where the firmware leaves it after a
print, a pause and a boot. A power cut leaves an idle window before the
resume. Change all, a filament change on a named tool and a spool join run
as the firmware runs them, and a toolchanger's print runs the stock slicer
profile's priming, tool-change moves and end park. Workstreams 5 and 6
aren't started. [Open questions](#open-questions) lists what the work still
has to settle.

## Goal and non-goals

The goal: for every printer family, the simulator's answers to the bridge's
reads (`/api/version`, `/api/v1/status`, `/api/v1/job`, `/api/v1/filament`,
and `/api/v1/info`, which it will need to tell an MMU printer apart) and to
its upload match what the firmware source says
that printer does, and tests run the bridge against each family.

Only the Core One L+ is at hand, and it becomes an INDX in a few months.
After that upgrade, single-tool behavior can't be traced again, and no
other model or an MMU is expected. So for most of this plan the firmware
source is the only evidence there will ever be, and the simulator is the
only place the bridge meets those printers.

Not in scope:

- **Anything the bridge can't observe.** A behavior is modeled only when it
  changes one of those reads, or whether the upload is accepted and what
  the job does. Tool mapping, per-tool temperatures, and how long a tool
  change takes are out. So is `READY`: only Prusa Connect is told it, and
  PrusaLink reports the state underneath (`status_renderer.cpp:30`).
- **Bridge changes.** Where the source shows the bridge would misbehave,
  [bridge-findings.md](bridge-findings.md) records it. Fixing it is other
  work.
- **Models with no counters beyond what exists.** The MINI, MK3.5 and
  MK3.5S report materials only, and the bridge charges nothing there. Their
  load lengths don't matter.

## Where the facts come from

Three tiers, by how well each can ever be checked:

| Tier | Printers | Evidence |
| --- | --- | --- |
| Traced | Core One L, single-tool | Traces from the printer, until the INDX upgrade. See [Before the INDX upgrade](#before-the-indx-upgrade). |
| Traced later | Core One L INDX | Source now, traces after the upgrade. |
| Source only | MK4 family, Core One, Core One Oak, iX, XL, XLP, any MMU3 | Source, with no prospect of a trace. |

The reference firmware follows the tables work. `script/firmware-check`
rebuilds the firmware's tables from upstream `master` every week and
compares them with the copies in the bridge and the simulator. Behaviors
that aren't tables are cited at `ff61540d4`, the tip of the
`prusalink-filament-endpoint` branch of Ross's fork: upstream `7d8bf6858`
plus `boot_id`, the unload counters and the endpoint. Extrusion counting
itself is upstream.

What a slicer puts in a print's G-code comes from PrusaSlicer's stock
profiles, bundle `PrusaResearch` 2.5.16. [Slicer runs](#slicer-runs) lists
the test slices that confirm those numbers.

Every firmware fact below was read from that source on 2026-11-13 and none
was seen on a printer. Each workstream reads its cited code again before
building on it, and records the behavior as a row in
[printer-behaviors.md](printer-behaviors.md) with the status "Other
hardware".

The firmware will move. Tag reader support is planned for INDX, and UUIDs
are expected to come with it. The plan keeps such facts as per-model data
the check compares, not as code paths, so a firmware change shows up as a
failing check with one table to edit. Behaviors can't be checked that way:
[Workstream 6](#workstreams) lists the assumptions to read again at each
firmware update.

## What the simulator has today

- `ModelInfo` holds a type code, a name, whether tool 0 keeps UUIDs, the
  retraction gap, the most tools, and whether the build has counters.
  `script/firmware-check` compares those with the source.
- Each model has its build's extruder profile. Every filament move counts
  through the tracker's arithmetic with the model's gap and feed ratio, and
  a menu load runs the model's slow, fast and purge lengths, with the
  purge's end per build and the high-flow doubling. Each build's ramming
  sequences run at an unload, a runout and an auto-retract, which follows
  an idle load and a print's end and stores its distance for the boot and
  the next feed. A printer's `auto_retract` switch turns it off where the
  build allows. Other lengths and times are one global `Settings`, and the
  feed ratio and load lengths there are overrides.
- Each model has its build's preset table. A menu load and a preset write
  report that build's temperatures, and neither takes a preset the build
  doesn't have, such as PPS on an MK4S.
- A tool has its counters and its stored type. Its filament path has the
  filament, the tracker's gap, the stored auto-retract distance and the
  sensor; each tool has its own path. A print is a list of (tool, mm)
  segments, and on a toolchanger the stock slicer profile's moves around
  them (see below). Picking a tool can
  trigger the idle sensor clear. A tool can be disabled.
- On the XL and INDX, a menu load or unload picks its tool first, and the
  idle sensor clear runs on it at the pick. INDX docks the tool when the
  operation ends, and the XL leaves it picked. The preview's load picks in
  its dialog, with no clear, and INDX docks it after; a reload in a print
  neither picks nor docks. Move E moves only the picked tool, and the Pick/park
  menu's park (`park_tool`) leaves none picked.
- A print on INDX, or on an XL with more than one enabled tool, runs the
  stock PrusaSlicer profile's moves outside the model (`simulator/slicer.py`,
  "What the slicer adds"), each counted on the tool picked when it runs:
  the start's priming of each used tool, the old tool's ramming and
  retracts and the new tool's purge at each change, and the last tool's
  moves before the end G-code parks it. So the firmware's end retracts and
  auto-retracts nothing. A tool's first pick in the print stands for the
  first layer. A print can override the prime and the tool-change purge.
  A stopped XL print keeps its tool, and INDX docks at a job's end and at
  a pause, whose resume picks the tool again and purges it in the nozzle
  cleaner. A boot keeps the picked tool, except an
  INDX cut during a print that used several tools, which boots with none.
  A toolchanger starts with none picked unless the config's `picked_tool`
  names one.
- Change all (`change_all`) changes the chosen tools one at a time in index
  order, `BUSY` throughout: an unload first where a type is stored, then a
  load. Each pick is inside the dialog, with no clear. INDX docks after
  each tool, and the XL ends with the last one picked. A power cut drops
  the tools not yet reached.
- Tune > Change filament (`change_filament`) runs the filament change on
  any tool during a print: on another tool it picks that tool, and the
  resume changes back with no purge. The counts land on that tool only. A
  runout and a color change on INDX follow the firmware's INDX flow, and a
  load or unload in a print reads `ATTENTION` in every phase.
- A print can carry spool joins (`PrintSpec.spool_join`). A runout on a
  tool with a join unloads it in `ATTENTION`, then picks and purges the
  join's tool in `PRINTING`, and the print goes on with that tool wherever
  the file names the old one. Joins chain, and at a chain's end the runout
  is the filament change. Snapshots and a power-panic resume keep the
  joins and the remap. A power cut during the join's unload resumes on
  the old tool until the file's next tool change.
- The model's feed system, direct, XL or INDX, holds the rules that
  differ between them. `ModelInfo.feed` names it, and
  `script/firmware-check` compares it with `HAS_TOOLCHANGER` and
  `HAS_INDX`. The printer's selected tool can be none, which
  stands for a toolchanger with no tool picked: then no filament moves or
  counts, the idle sensor clear doesn't run, and the end of a print neither
  retracts nor auto-retracts. An idle INDX after a menu load or unload has
  none, and so does an INDX or an XL with several tools after a finished
  print, and either after `park_tool`. An XL with one tool enabled isn't a toolchanger here: its
  tool stays picked, as the firmware keeps it on the head, and it prints
  as a direct printer does, with no profile moves.
- A power cut on a build with power panic boots the printer `IDLE` with no
  job for 1.5 s before the resume starts, and the idle sensor clear runs in
  that window on the picked tool, if one is. The firmware's autostart tries every
  1.5 s and gives up after 100 s (`print_utils.cpp:67-82`); the simulator
  resumes at the first try, delays the resume while a menu load or unload
  runs, and drops it when a print starts in the window. A cold bed
  reads `ATTENTION` as soon as the window ends, and a paused print reads
  `BUSY`, then `PAUSED`, after the cold bed's wait if there is one. A cut
  during a filament change dialog isn't a cut while paused: the print
  resumes on its own with the dialog gone, on the tool the cut found
  picked until the file's next tool change, and a color change in the file
  runs again.
- An uploaded file's job is counted at once. When the scan ends, the
  print preview checks the file's `M862` lines: the model by compatibility
  group, the G-code level, input shaper, the INDX lock, the MMU3 feature
  and unknown features. A failed check holds the job in `ATTENTION`, with
  its ID, until it's answered. One `checks` setting per printer ignores,
  warns or aborts; the INDX lock always warns.
- A print started from the page is counted at once, as the user pressing
  Print. The preview then checks the tools its segments use, with the
  firmware's fixed severities: a disabled tool refuses, and a tool with no
  filament warns and then waits for a load, which reads `BUSY` with no job.
  A segment can name a filament type, and a tool holding another one asks.
  On the XL and INDX these rows are one tools-mapping question, which a
  print it applies to asks with nothing wrong too, and the idle sensor
  clear runs on the picked tool while it's up. Print there asks once for
  each kind of failed row: a tool with no filament asks to turn every
  filament sensor off, then a tool holding another type asks. The print
  then counts on the empty tool with no material. A printer's `sensors`
  switch turns off the runout, the idle sensor clear and the preview's
  filament question.
- It serves `/api/version`, `/api/v1/info`, `/api/v1/status`, `/api/v1/job`
  and `/api/v1/filament`. `/api/v1/info` always reports `mmu` as false.
- Tests: the unit harness and the integration fixtures take a model and a
  tool count. `tests/test_families.py` runs one story (a print with a tool
  change, a swap, a reboot) on the Core One L, MK4S, iX, XL, Core One L INDX
  and MINI, with charges worked by hand from the firmware's numbers.
  Integration runs T1 to T7 on a two-tool INDX, T1 on a five-tool XL, and a
  few rows on an MK4, an MK4S, an MK3.9 and a MINI. The MMU families wait
  for Workstream 5.

## What's missing

Paths are in the firmware at `ff61540d4`.

### Per-model numbers

| Fact | Detail | Source |
| --- | --- | --- |
| Load lengths | Slow and fast feed, then the purge: Core One and Core One L 40, 20, 40; MK4 and XL 30, 50, 27; iX 40, 45, 40; INDX 15, 25, 25. A high-flow nozzle doubles the purge, except on INDX. | `include/marlin/Configuration_*_adv.h`, `pause.cpp:688-866`, `nozzle_cleaner.cpp:76-92` |
| The purge's end | Most builds retract 8 mm after the purge. The iX purges in 5 mm steps with no retract. INDX runs `G750 E25` once, then the auto-retract. | `pause.cpp:817-888` |
| Feed ratio | E steps over calibrated E steps: 380/400 on every build but INDX, which is 550/576. | `planner.cpp:2210-2218`, `Configuration_COREONE.h:476-484` |
| Auto-retract | After an idle load and at the end of a print, a Nextruder's sequence leaves the filament 55 mm retracted, but the tracker's gap stops at 40. The sequence first feeds back whatever the tracker says is retracted, so it starts from a gap of 0 and counts 23 mm: its 8 mm feed, which does leave the nozzle, and 15 mm that never does. When the tracker has lost the distance (the gap at its limit), only the 15 mm counts. The next forward move counts 15 mm more that never left the nozzle. INDX is exact. Off for flexible filament and when the user turns it off. | `standard_ramming_sequence_nextruder.cpp`, `ramming_sequence.cpp`, `mapi/motion.cpp`, `marlin_server.cpp:763-770, 1045-1080`, `pause.cpp:1041-1044` |
| Unload | A menu unload rams first. It counts about 23 mm on a Nextruder that isn't auto-retracted and about 19 mm on INDX, before the unload is recorded. | `pause.cpp` |
| Power-panic resume | INDX purges 25 mm (`G750 E25`). The MK4 family, MK3.5, MK3.5S, Core One, Core One L and iX feed back the 8 mm they retracted at the cut. The XL moves nothing. The MINI and the XL dev kit have no power panic (`ProjectOptions.cmake:334-345`), so a cut drops the print and they boot idle. F12 in printer-behaviors.md reads the same, the simulator follows the source per model, and the F12 trace decides. | `power_panic.cpp:110-116, 480-521` |
| Nozzle-cleaner sequences | On the iX and INDX, a file under `/usb/macros/nozzle_cleaner/` replaces a built-in sequence, the INDX purges included. The simulator assumes the built-in ones. | `gcode_loader.cpp:24-30, 105-115` |

With the gap full, a Core One L menu load counts 20 mm before the type
appears and 63 mm after: the 40 mm purge and the auto-retract's 23. That's
83 mm commanded, 78.85 mm at the 0.95 ratio. The F3 trace will confirm the
split.

### The print preview

The firmware checks a file's `M862` lines at the preview, before the job
counts, and print-after-upload goes through the same checks
(`gcode_info.cpp:260-380`, `marlin_print_preview.cpp:890-973`).

- **The job is counted before any question.** An upload skips the preview
  screen, and the job ID is taken at once. While a question waits, the
  state is `ATTENTION`, `/api/v1/status` has the `job` with its new ID, and
  `/api/v1/job` answers 200 with `PAUSED`. The upload itself answers 201.
  Declining goes to `IDLE`, not `STOPPED`, with the ID used up and the file
  kept (`wui_api.cpp:291`, `marlin_print_preview.cpp:414-425`,
  `marlin_server.cpp:1113-1136, 2300-2331`).
- **Severity is a user setting** per kind of check, picked in the printer's
  menu and kept in its config store, not compiled in: ignore, warn (the
  default on every build) or abort. Every failed check shows on one screen, and the worst
  severity decides: a warning waits for a tap, an abort can only be
  declined (`gcode_compatibility.cpp:48-153`,
  `marlin_print_preview.cpp:921-971`).
- **The model check compares groups, not names.** A Core One L takes Core
  One G-code as its own. A Core One takes Core One Oak. An MK4 takes MK3.9.
  An MMU3 name resolves to its base model whether or not an MMU is on.
  Older models' G-code is "compatibility mode", which asks. INDX builds
  take only their own name (`printer_model.cpp:140-198`).
- **INDX asks about a file with no `M862.6 P"INDX lock"`**, and no setting
  turns that off (`gcode_compatibility.cpp:92-101, 372-376`). An INDX
  whose nozzle-cleaner calibration selftest hasn't passed refuses every
  print: `nozzle_cleaner_not_calibrated` is a fixed abort
  (`gcode_compatibility.cpp:143-152, 392-396`).
- **A file sliced for an MMU3 is refused or asked about when the MMU is
  off**, under the firmware setting.
- **The per-tool checks need the file's filament-used metadata**: a tool
  with no filament or another type asks, and a disabled tool refuses.
  Those severities are fixed; only the nozzle checks follow a menu
  setting (`gcode_compatibility.cpp:155-265`). The bridge's sync
  job has no such metadata, so it raises none of them (`gcode_info.hpp:95-98`,
  `gcode_compatibility.cpp:156-205, 420-423`).
- **With an MMU and filament at the nozzle, the preview asks to unload
  first**, unless the filament sensors are off, or a single-tool file of
  the loaded type meets filament at the sensor, which starts at once. Yes
  unloads. No starts the print as it is
  (`marlin_print_preview.cpp:228-256, 699-708`).

The idle sensor clear runs while the preview opens and stops once a question
shows, except on the tools-mapping screen (`marlin_server.cpp:1448-1460`).

The simulator has the checks of an uploaded file and of a print's tools.
The MMU's question is still missing. A print raises no model question: it
has no `M862` lines.

### `/api/v1/info`

Five fields on every build: `nozzle_diameter` (tool 0), `mmu`, `serial`,
`hostname` and `min_extrusion_temp` (170). `mmu` is the MMU setting on
builds that can take one and false elsewhere (`basic_gets.cpp:229-253`).

### Toolchangers: XL and INDX

| Event | What the firmware does | Source |
| --- | --- | --- |
| Tool change in a print | No unload and no firmware E move on the XL. The INDX head-lock moves are excluded from counting. The INDX stock tool-change G-code's `G27 ... R8` makes the firmware retract the outgoing tool 8 mm in ordinary planner moves as it parks. The slicer's moves count on whichever tool is picked. | `toolchanger_indx.cpp:364-376`, `planner.cpp:2194`, `mapi/parking.cpp:262, 282-301` |
| Menu load or unload of another tool | Picks that tool first with `T<n> S1 L0 D0`, outside any dialog: it reads `IDLE`, and the idle sensor clear can run on the new tool before the operation starts. INDX docks it again afterwards; the XL leaves it picked. A preheat cancel or a failed pickup leaves an INDX tool picked. The preview's load picks inside its dialog. | `MItem_filament.cpp:41-104`, `M701_2.cpp:84-88, 112-114, 135-140, 162-166, 179-181, 194-197, 339-342`, `pause.cpp:1447-1468` |
| No tool picked | A non-service E move is a `bsod`, so callers pick first and Move E sends no E. The idle sensor clear doesn't run. | `planner.cpp:2194-2198`, `screen_menu_move.cpp:166-171, 206-211`, `filament_sensors_handler.cpp:306` |
| The picked tool at rest | INDX docks its tool after a load, an unload, a pause and a print, so an idle INDX has none picked. The pause docks in `Pausing_ParkHead`, and the resume picks the tool again and purges it in the nozzle cleaner, 25 mm then `M1705 N`, while `PAUSED`. A finished print's stock end G-code parks the tool on INDX and on an XL with several tools (`P0 S1` in `[printer:*C1_INDX_8T_common*]` and `[printer:*XLMULTI*]`, profile bundle PrusaResearch 2.5.16), so the firmware's end retract and auto-retract find no tool. An XL with one tool has its toolchanger off and keeps that tool on the head, and its profile, `*commonXL*`, has no park (`toolchanger_utils_xl.cpp:62-93`, `toolchanger_xl.cpp:236-241`). A stopped XL print keeps its tool and gets the end's retract and auto-retract; a stopped INDX print docks after its auto-retract. A sync job leaves the XL's tool as it was; INDX docks it. At boot the XL picks the Dwarf on its head. INDX restores the EEPROM's last picked tool, checked against its nozzle sensor; parking a tool while printing invalidates it unless a single-tool print froze it, so a multi-tool print cut short boots with none. | `M701_2.cpp:135-140, 194-197, 339-342`, `marlin_server.cpp:1077-1100, 2549-2563, 2662-2674, 2789-2817, 3298-3307, 3423-3434`, `P.cpp:35-48`, `toolchanger_utils_indx.cpp:50-57`, `toolchanger_indx.cpp:213-261, 926-939`, `toolchanger_utils_xl.cpp:54-58`, `toolchanger_xl.cpp:589-597` |
| Idle sensor clear | Only the picked tool, and only while not printing and with no load or unload dialog open: the XL reads the Dwarf's extruder sensor, INDX the tool's side sensor. It doesn't run on an idle INDX with none picked. The XL keeps a tool picked after a menu operation, a stop and a boot, so it runs there often. A tool emptied by hand while docked keeps its type until something picks it while idle, the menu's pick before a load or unload included. Nothing sweeps the tools at boot or at a print start. | `filament_sensors_handler.cpp:120-131, 197-212, 303-312`, `filament_sensors_handler_XL.cpp:17-61`, `filament_sensors_handler_INDX.cpp:20-23` |
| Runout with a spool join | Set per print and reset at its end. The old tool gets INDX's 2 mm unload purge and the `runout` ramming, with no park retract, and its unload counts while the status reads `ATTENTION`. The change and the purge read `PRINTING`: the XL parks, picks the new tool, purges 27 mm, never doubled, retracts 8 mm and stores it as the auto-retracted distance; INDX changes tools and purges 25 mm with the `M1705 N` auto-retract. Then the old tool's gap is restored on the new tool, whose type isn't set or copied, and the print goes on with it wherever the file names the old tool. With no next tool in the chain, the runout is a manual `M600` on the old tool. | `spool_join.cpp:71-105, 239-365`, `toolchanger_xl.cpp:505-571`, `nozzle_cleaner.cpp:87`, `M600.cpp:125-153`, `pause.cpp:1233-1288, 1820-1849`, `marlin_server.cpp:1122` |
| Runout without one | `M600` on the same tool. On INDX it isn't the single-tool flow: it parks over the wastebin, purges 2 mm before the INDX ramming, cleans the nozzle after the unload, purges 25 mm with `purge_clean` and its `M1705 N` auto-retract at the reload, then primes 4 mm, retracts 8 mm and cleans again. The reload writes the old type back, so `unload_count` +1 with the same `material`. | `filament_sensors_handler.cpp:237`, `M600.cpp:223, 282-290`, `pause.cpp:1233-1297, 1054-1096`, `mapi/parking.cpp:101-110` |
| `M600 T<n>` | Tune > Change filament sends it for a chosen tool. Picks tool `n` outside any dialog, so it reads `PRINTING`, with the resume point at `n`'s dock and `n` heated for its type. Changes `n`'s filament in the dialog (`ATTENTION`), then changes back to the print's tool with no E move; INDX reheats over the purge position. With no type found for `n`, nothing changes. | `M600.cpp:204-340`, `screen_m600.cpp` |
| Change all, on the XL and INDX | For each chosen tool in index order, inside one `BUSY` dialog: `M1600` (an unload that counts, then a load) when a type is stored, else `M701` (a load). Each picks its tool inside its dialog. INDX docks after each tool; the XL ends with the last one picked. | `multi_filament_change.cpp:151-239`, `M701_2.cpp:135-140, 240-343` |
| A load or unload during a print | Reads `ATTENTION` in every phase, `ChangingTool` included, not only while waiting. Idle, it reads `BUSY`. A preheat in a print reads `PRINTING`, not `ATTENTION`. | `printer_state.cpp:152-190, 238-246` |

The simulator has every row in the table. An INDX docks after a menu load or
unload, a pause and a job's end, so its selected tool becomes none, and the
Pick/park menu picks or parks a tool on INDX and on an XL with several
tools. The end G-code's park is part of the slicer profile ("What the slicer
adds"). The printer config's `picked_tool` sets the tool picked at start,
none by default on INDX and on an XL with several tools.

After a power cut, the idle sensor clear in the window before the resume
reads the picked tool too. On the XL that's the tool on the head. On INDX
it's the EEPROM's last picked tool, which a pickup during a print
invalidates unless a single-tool print's `G427` froze it, so a multi-tool
INDX print boots with no tool picked and gets no clear
(`toolchanger_indx.cpp:926-939`, `tool_offset_calibration.cpp:719-729`).
The simulator does the same, and its resume picks the print's tool before
anything moves.

### MMU3

An MMU3 is a runtime setting on the MK4 family, the Core One and the Core
One L (and the MK3.5, which has no counters). With it on:

- **Five tools appear**, one per slot, each with its own counters and type
  (`tool_index.cpp:84-102`). Off, the same printer shows one.
- **A slot's type means what's loaded in the slot**, not what's in the
  nozzle (`M70X_mmu.cpp:80-82`).
- **All slots share one gap**, because there's one nozzle
  (`filament_tracker.hpp:94`).
- **Almost nothing counts an unload.** Tool changes, MMU unloads, ejects,
  cuts, `M600` and spool joins leave `unload_count` alone, and the idle
  sensor clear is off. The exceptions are turning the MMU off with an empty
  nozzle, the change-all unload, and a cold pull. Swapping the spool that
  feeds a slot shows nothing (`store_definition.cpp:258-262`,
  `filament_sensors_handler.cpp:306`).
- **The selected slot decides who is credited**, and it's "none" after an
  unload and after every boot. Extrusion with no slot selected isn't
  counted (`mmu2_mk4.cpp:175, 674`, `planner.cpp:2207`).
- **A tool change credits the outgoing slot with feed that isn't its
  own.** The firmware plans about 190 mm to reach the filament sensor
  (`mmu2_mk4.cpp:55, 1528, 1590`), then up to 57 mm more: 7 mm past the
  sensor (`mmu2_mk4.cpp:1583`, `mmu2_config.h:35`) and 50 mm of feed
  (`mmu2_config.h:69`, `mmu2_mk4.cpp:524-533`), while the old slot is
  still selected (`mmu2_mk4.cpp:674`). Less the gap, that's roughly
  200 mm on the old slot per change, and more on a retry. This is
  inferred from the planner counting at plan time.
- **An idle load counts 47 mm less the gap** on the new slot. An idle
  unload counts up to 20 mm (`mmu2_config.h:70-84`,
  `Configuration_MK4_adv.h:573`).
- **States**: idle MMU operations read `BUSY`. Tool changes in a print read
  `PRINTING`. A runout's prompts and an MMU error in a print read
  `ATTENTION`; an MMU error while idle reads `BUSY`
  (`printer_state.cpp:152-176, 238-247`).
- **The tag reader is off**, so slot 0 can keep UUIDs from an earlier tag
  load but gets no new ones (`request_manager.cpp:263-266`).
- **Slots 1 to 4 keep no UUIDs.** `M865 L<n>` takes every slot and
  stores its type (`M865.cpp:194-196`, `store_definition.cpp:243-279`).
  An ad-hoc write stores the name, temperatures and color for any slot,
  and the UUIDs for slot 0 only; for another slot it skips them, behind a
  `debug_assert` that fires on a debug build (`filament.cpp:380-402`).

The simulator has none of this.

### What the slicer adds

These are the stock profiles' E moves, measured in G-code that PrusaSlicer
2.9.7 sliced with the stock profiles (vendor bundle 2.5.16). The MMU3 row
is still worked out from the profile values, until the MMU's slicer profile
brings the measured ones.

| Printer | At the start | At a tool change | At the end |
| --- | --- | --- | --- |
| MMU3 | `T<first>`, a 17 mm load, then about 53 mm of priming | On the old slot: about 21 mm of ramming, then a net 84 mm retract. `T<n>`. On the new slot: a 32 mm load, then the wipe tower's purge, at least 6 mm for PLA and 15 mm for PETG | `M702`: the firmware unloads, for every print |
| XL | 39 mm of priming (30 + 9) per tool the print uses, the first tool last. Before it, the first tool retracts 2 mm for the nozzle cleanup. After it, the first tool retracts 0.8 mm and each other tool 20 mm, which comes back at its first pick. Unused tools aren't primed | On the old tool: 4.16 mm of ramming, then a 20 mm retract (the filament's `filament_retract_length_toolchange`, not the printer's 1.2). `P0 S1 L2 D0`, `T<n> S1 L0 D0`. On the new tool: a 20 mm deretract, then the tower purge: 9.24 mm on the first layer, 7.01 mm after it | The tower rams the last tool once more (4.16 mm) and retracts 0.8 mm. Then the end G-code parks it (`P0 S1`), with no E move. A one-tool print has no tower, so no ramming |
| INDX | `G427` purges every used tool in the firmware: 25 mm (`G750 E25`), then an auto-retract. Then the slicer primes the first tool: 1.6 mm, a 5.49 mm flush (1.1 times 12 mm³), then a 0.8 mm retract | At the purge station, since the stock profile has no wipe tower. On the old tool: a 0.8 mm retract, then the firmware's 8 mm retract in `G27 R8`, and `P0`. On the new tool: on its first pick 1.6 mm, then a 4.16 mm purge, then a 0.8 mm retract. On later picks, 1.6 mm (lock), 8 mm, and 0.8 mm before the 4.16 mm purge and the 0.8 mm retract | Retracts 0.8 mm, then parks (`P0 S1`). No other E move |

The purge per tool change is the incoming filament's
`filament_minimal_purge_on_wipe_tower`: 15 mm³ for PLA on the XL, 10 mm³ on
INDX, 35 mm³ for PETG on the XL. PrusaSlicer zeroes the wiping-volume
matrix for printers without single-extruder multi-material, and the INDX
tool-change G-code uses the same setting at its purge station. On the XL
the tower's purge isn't exactly that volume: it's tower line geometry, so
the simulator keeps the measured lengths. The MMU3 profiles name the base
model in `M862.3` and add `M862.6 P "MMU3"`. The INDX profiles add
`M862.6 P"INDX lock"`.

The simulator holds these as profile data (`simulator/slicer.py`), on
`ModelInfo.slicer` for the XL and INDX models. `script/firmware-check`
doesn't compare it, since it isn't firmware. The bundle has no Core One L
INDX, so both INDX models use the Core One INDX numbers. A one-tool XL
turns its toolchanger off and keeps Dwarf 0 picked
(`toolchanger_utils_xl.cpp:62-93`), and the single-tool XL profiles
inherit `*commonXL*`, whose end G-code doesn't park, so it gets no profile.
The travel retract and its restore around the XL's tower are the model's
own moves and cancel in the tracker, so they're left out.

## Approach

### A model is a profile

`ModelInfo` grows from six facts into the model's profile, and stays
hand-copied data that `script/firmware-check` compares with the source:

- The build, tool count, gap, counters and UUIDs, as now.
- The feed ratio, the load's slow, fast and purge lengths, how the purge
  ends, the unload's ramming, the auto-retract's two artifacts, and the
  power-panic resume: whether the cut retracts, and the purge.
- The preset table for its build.
- The G-code names it accepts as its own and in compatibility mode, and the
  features a file must declare.
- Its feed system (below): direct, XL or INDX. And whether it can take an
  MMU.

`Settings` keeps times and rates. Its load lengths become overrides: unset
means the model's value. The `firmware` package gains readers for the new
tables, so each number has a check behind it.

### Tools and filament paths

`SimTool` is two things today, and an MMU pulls them apart:

- A **tool** is what `/api/v1/filament` lists: an index, the three
  counters, the stored type.
- A **path** is where filament physically is: the gap, the sensor, and
  whether filament is in it.

A single-tool printer has one of each. A toolchanger has one path per
tool. An MMU printer has five tools and one path. The printer has a
selected tool, which can be none. A move names the tool whose path moves,
and the feed system decides which tool it credits. A toolchanger's menu
load or unload picks its tool first, as the firmware's menu does. A load or
unload in progress stays on its tool.

"None selected" is two cases in the firmware. A toolchanger with no tool
picked has no physical tool: it can't move E at all, the auto-retract's
hooks don't run, and the end of a print neither retracts nor auto-retracts
(`tool_index.cpp:121-137`, `planner.cpp:2194-2197, 2224`,
`mapi/motion.cpp:88-91`). An MMU with no slot selected still has its one
physical tool: moves run and deretract but don't count, and the end of a
print still retracts and skips only the auto-retract
(`tool_index.cpp:134-151`, `auto_retract.cpp:91-94`). The simulator's none
is the first case. The MMU's feed system brings the second.

A **feed system** owns the rules that differ: which tool is selected and
when, which tool a move credits, what a tool change moves, and which tool
the idle sensor clear reads, if any. What counts an unload isn't one of
them: the firmware counts any write of "none" over a stored type
(`store_definition.cpp:258-262`). An MMU counts fewer only because its
operations don't write "none". There are four: direct, the XL and INDX
toolchangers, and MMU. The model's profile names its system, and a printer's config turns the
MMU on.

### The preview

A preview step replaces `wrong_model`. It counts the job, runs the checks
in the list above, and holds the job in `ATTENTION` on a question, visible
with its ID. The page's tap-through accepts, and declining ends in `IDLE`.

Each printer has one severity for the checks a setting governs: ignore,
warn or abort. That replaces `auto_accept_model_check`, which becomes
"ignore". The firmware has a setting per kind of check, but they share one
screen and the worst decides, so one setting shows the bridge every
outcome. The INDX lock question always asks, as in the firmware. The MMU
question is under the firmware setting
(`gcode_compatibility.cpp:106`).

A print's segment can name the filament type it was sliced for. A segment
whose tool holds another type, or none, raises the filament question at the
preview, so a mismatched print shows as it would on the printer.

Only what the API shows is modeled: the state, the job and its ID, and the
outcome. How long the preview screen stays up isn't, beyond a question
waiting until it's answered.

### Prints

A print stays a list of (tool, mm) segments. What a tool change costs comes
from the feed system and the profile, not one global purge:

- Toolchanger: the slicer's ramming and retract on the outgoing tool, then
  its deretract and purge on the incoming one. On INDX the firmware's
  `G27 R8` retracts the outgoing tool too, and a later pick feeds it back.
- MMU: the firmware's feed on the outgoing slot, then the slicer's load and
  purge on the incoming one.

The slicer's part (priming, ramming, the load after a change, the wipe
tower, the end G-code's park or unload) is profile data with the stock
profiles' values, and a print can override its prime and its tool-change
purge (`PrintSpec.prime_mm`, `toolchange_purge_mm`). The toolchangers' is
built; the MMU's waits for Workstream 5. The priming runs once, when the
print first starts feeding (`UserJob.primed`), so a pause, a power-panic
resume or a filament change doesn't run it again. Segments have no layers:
a tool's first pick in the print stands for the first layer, which on the
XL decides the tower's larger purge.

### Tests

The unit harness and the integration fixtures take a model, a tool count
and the MMU switch, instead of one fixed Core One L. One family matrix runs
the core accounting scenarios (a load, a print, a swap, a reboot) on each
of: Core One L, MK4S, XL, Core One L INDX, MK4S with an MMU, Core One with
an MMU, and a MINI.

## Workstreams

Each is a stack of small PRs. The order puts what the Core One L can still
confirm first.

1. **Profiles.** The profile fields, per-build presets, the readers and
   comparisons in `firmware/`, `Settings` overrides, the auto-retract and
   ramming counts, and `/api/v1/info`. The harness and fixtures take a
   model and tools. Acceptance: `script/firmware-check` passes with the new
   tables, the existing traces still replay, and a menu load on each build
   counts what the table says.
2. **The preview.** The job counted first, groups, the severity setting,
   the INDX lock question, and the per-tool questions for a print with
   segments. Acceptance: the bridge's default sync job on each family stops
   or runs as the source says, and a question shows the job with its ID.
3. **Tools and paths.** The split, a selected tool that can be none, and
   the feed system for direct and toolchanger printers, with no behavior
   change. Acceptance: every existing test passes.
4. **Toolchanger events.** A menu load picks the tool and INDX docks it
   again, the picked tool at rest and at boot, the idle clear on the picked
   tool only and in the window before a power-panic resume, spool join,
   `M600 T<n>`, change all, and the slicer's priming and purges. The page
   gets the actions. Acceptance: T1 to T7 still pass, and each event has a
   simulator test and a row in printer-behaviors.md.
5. **MMU.** The MMU feed system, the switch in the config and on the page,
   slot operations (preload, load to nozzle, unload, eject), tool changes,
   runout, spool join, boot, and the preview's unload question. The
   snapshot needs printer-level paths, since a flat snapshot per tool can't
   express a shared path. The `M865` slot range can't follow `max_tools`
   with an MMU on.
   Acceptance: a two-color print's counters match the source's arithmetic,
   and the family matrix runs the bridge against both MMU printers.
6. **Keeping up.** A list in printer-behaviors.md of the behaviors that
   rest on one build flag or one firmware function, to read again when
   `firmware-check` reports a change or a new firmware ships. The first
   entries: UUIDs on tool 0 only, no tag reader on INDX, the INDX lock
   question, and the MMU's selected slot at boot.

## Slicer runs

Slicing a test model in PrusaSlicer with a stock profile and reading the
G-code confirms the slicer's numbers without a printer. Each run records
the E moves before the first object move, around each `T`, and in the end
G-code, plus the `M862` lines:

1. **MK4S MMU3, two colors, PLA.** Done: two 10 x 10 x 1 mm boxes, in
   slots 0 and 1, slots 1 and 3, and slots 0, 1 and 2 in two orders, plus
   PETG, and a PLA and PETG mix. The ramming, the retract before `T`, the
   load after it, and the purge per change, with the wipe tower's purge
   volume, the minimal purge, and the wiping-volume matrix changed one at
   a time, and the layer count and per-slot heights changed.
2. **MK4S MMU3, one color.** Done: one box in slot 0 and one in slot 2.
3. **Core One MMU3, two colors, PETG.** Done: the same boxes with PETG and
   PLA, one color, and PLA on the Core One L MMU3.
4. **XL, two tools and five.** Done: two 10 x 10 x 1 mm boxes, one per tool,
   on the XL 2T and 5T and the XL+ 2T, with tools in either order, three
   tools, one tool, and the wipe tower, the ramming and the purge volume
   changed one at a time.
5. **Core One INDX, two tools.** Done: the same boxes on the 4T and 8T,
   with other tools, one tool, the wipe tower on, and a larger purge
   volume.

Runs 1 to 5 used PrusaSlicer 2.9.7 (flatpak `com.prusa3d.PrusaSlicer`)
with vendor bundle `PrusaResearch` 2.5.16. The numbers from runs 4 and 5 are
in "What the slicer adds" and `simulator/slicer.py`, and those from runs 1 to
3 arrive there with the MMU's slicer profile. The sliced G-code isn't kept in
the repository.

To repeat a run, make a model with `script/slicer/make3mf.py` and slice it
with `script/slicer/slice.sh`, which both document their arguments. Each
slice uses one printer and print preset. `slice.sh` takes one filament
preset per tool, separated by `|`, and repeats the last one for the rest, so
a mixed slice is possible. The MMU3 slices use five filaments, one per slot:

| Run | Printer preset | Print preset | Filament preset |
| --- | --- | --- | --- |
| 1 and 2, MK4S MMU3 | `Original Prusa MK4S MMU3 0.4 nozzle` | `0.20mm SPEED @MK4S 0.4` | `Prusament PLA @MK4S`, `Prusament PETG @MK4S` |
| 3, Core One MMU3 | `Prusa CORE One MMU3 0.4 nozzle` | `0.20mm SPEED @COREONE 0.4` | `Prusament PLA @COREONE`, `Prusament PETG @COREONE` |
| 3, Core One L MMU3 | `Prusa CORE One L MMU3 0.4 nozzle` | `0.20mm SPEED @COREONEL 0.4` | `Prusament PLA @COREONE` |
| 4, XL 2T and 5T | `Original Prusa XL - 2T Input Shaper 0.4 nozzle`, `Original Prusa XL - 5T Input Shaper 0.4 nozzle` | `0.20mm SPEED @XLIS 0.4` | `Prusament PLA @XLIS` |
| 4, XL+ 2T | `Prusa XL+ 2T 0.4 nozzle` | `0.20mm Balanced ★ @XL+ 0.4` | `Prusament PLA @XL+` |
| 5, INDX 4T and 8T | `Prusa CORE One INDX 4T HF0.4 nozzle`, `Prusa CORE One INDX 8T HF0.4 nozzle` | `0.20mm Balanced ★ @COREONEINDX HF0.4` | `Prusament PLA @COREONEINDX HF0.4` |

The model is one 10 x 10 mm box per tool the slice uses, side by side. At
the default height of 1 mm, that makes five layers at 0.2 mm and one tool
change per extra tool on each layer. `make3mf.py` takes another height, or
one height per box, which run 1 used for more layers and for layers that
only one slot prints. The slices that vary one setting change the wipe
tower, the XL's multi-tool ramming, the filament's
`filament_minimal_purge_on_wipe_tower`, or on the MMU3 the wipe tower's
purge volume (`multimaterial_purging`) or the wiping-volume matrix.

## Before the INDX upgrade

Once the printer is an INDX, these can't be recorded again. Sittings A to
D in printer-behaviors.md cover F1 to F13, F24, F28 and F29. The first
six below are what this plan adds; the last two are sitting D's:

- **The load's split.** F3 at `--interval 0.5`: how much counts before the
  type appears, and whether about 23 mm counts at the end of the load
  (the auto-retract).
- **The auto-retract around a print.** F5 with the counters watched during
  parking and at the next print's first move: about 23 mm (15 mm when the
  gap is already at its limit), then about 15 mm.
- **An unload that isn't auto-retracted.** F26: unload after a Move E, to
  see the ramming count.
- **A preview question.** F27: upload a file sliced for an MK4S, and one
  for a Core One: the first should ask, the second shouldn't. While it asks,
  expect `ATTENTION`, a `job` with a new ID, and `/api/v1/job` reading
  `PAUSED`. Decline one and expect `IDLE`.
- **The power-panic resume's purge.** F12 confirms the 8 mm fed back on a
  single-tool Core One L, which the simulator follows from the source. It
  also shows the idle window, and whether PrusaLink answers before the
  firmware's first resume attempt, which decides BF26.
- **Everything with a tag**: F4 and F24's tag rows. The INDX has no tag
  reader until the firmware adds one.
- **A load at the preview, then Abort.** `F28-load-then-decline`, steps 1
  and 2 of sitting D: whether BF23's print start shows on the printer.
- **A print with the sensor off.** `F29-sensors-off-print`, steps 3 to 7
  of sitting D: the single-tool path to BF24 and BF25, which the
  simulator doesn't model.

## Bridge findings

[bridge-findings.md](bridge-findings.md) lists the places where the
firmware source or the simulator shows the bridge would charge wrongly,
miss a swap, or fail to write, each by its BF ID and in a proposed order
of work. A finding from a later workstream gets an entry there.

## Open questions

Left to the work:

- The single-tool power-panic resume: the source gives a plain Core One L
  the 8 mm fed back, not a 25 mm purge, and the simulator follows the
  source. The F12 trace confirms it.
- PrusaSlicer's purge per change on an MMU3, which [slicer run](#slicer-runs)
  1 measured and the MMU's slicer profile brings. On the toolchangers it's
  `filament_minimal_purge_on_wipe_tower`.
- Whether INDX's `G1 E1.6` lock move after a pickup reaches the nozzle or
  only seats the lock. The firmware counts it as any move, and so does the
  simulator; a trace of a tool change would settle it.
- The XL tower's purge after the first layer, 7.01 mm rather than the
  6.24 mm its 15 mm³ gives, follows from the tower's line geometry, which
  depends on the tower's width and the layer height. The simulator keeps
  the stock print settings' lengths.
- Why INDX's wipe-tower purge, off by default, is 1.67 mm, below the
  10 mm³ minimum. The simulator models the default purge station only.
- Both INDX models use the Core One INDX numbers, since bundle 2.5.16 has
  no Core One L INDX profile.
- A tool first used above layer 1 gets the first layer's larger purge on the
  XL, since a segment has no layers.
- The tools-mapping screen's checks of a spool join's chain, which
  `simulator/README.md` lists.
- Change filament while a print is paused, which the firmware offers (F34).
- The iX's resume purge after a pause (F6).
- The single-tool XL profiles' start and end moves, which the slices
  didn't cover. A one-tool XL prints with no profile moves.

Only hardware could tell, and none is expected:

- How long an MMU's push to the filament sensor takes, which sets the size
  of BF16 in [bridge-findings.md](bridge-findings.md).
- The iX's assisted-insertion length.
- How the real slow feed compares with the tracker's fixed gap.
