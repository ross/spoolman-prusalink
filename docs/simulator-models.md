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
filament-inserted question belongs to Workstream 5. Workstreams 3 to 6
aren't started. [Open questions](#open-questions)
lists what the work still has to settle.

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
- A tool has its own counters, material, gap and sensor. A print is a list
  of (tool, mm) segments with a fixed purge at each tool change. Picking a
  tool can trigger the idle sensor clear. A tool can be disabled.
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
  then counts on the empty tool with no material. A printer's `sensors` switch turns off the
  runout, the idle sensor clear and the preview's filament question.
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
| Power-panic resume | INDX purges 25 mm (`G750 E25`). The MK4 family, Core One, Core One L and iX feed back the 8 mm they retracted at the cut. The XL moves nothing. F12 in printer-behaviors.md reads the same, the simulator follows the source per model, and the F12 trace decides. | `power_panic.cpp:110-116, 480-521` |
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
| Tool change in a print | No unload and no firmware E move on the XL. The INDX head-lock moves are excluded from counting. The slicer's ramming counts on whichever tool is picked. | `toolchanger_indx.cpp:365-376`, `planner.cpp:2194` |
| Menu load or unload of another tool | Picks that tool first. INDX docks it again afterwards; the XL leaves it picked. | `M701_2.cpp:136-139` |
| No tool picked | Extrusion isn't counted and the idle sensor clear doesn't run. | `planner.cpp:2207` |
| The picked tool at rest | INDX docks its tool after a load, an unload, a pause and a print, so an idle INDX has none picked. At boot it has one only if a tool is physically on the head. An XL has whichever tool is physically on the head, which after a print depends on the end G-code; the stock profile parks it. | `M701_2.cpp:135-140, 194-197, 339-342`, `pause.cpp:1395-1402`, `marlin_server.cpp:1098, 2561`, `toolchanger_indx.cpp:222-262`, `toolchanger_xl.cpp:582-597` |
| Idle sensor clear | Only the picked tool. So it never runs on an idle INDX, and rarely on an XL. A tool emptied by hand while docked keeps its type. Nothing sweeps the tools at boot or at a print start. | `filament_sensors_handler.cpp:294-312` |
| Runout with a spool join | Counts an unload on the old tool, and the print goes on with the join's tool. The new tool's type isn't set or copied. Its purge counts on it: 27 mm on the XL, 25 mm on INDX. | `spool_join.cpp:258-342`, `toolchanger_xl.cpp:505-549`, `nozzle_cleaner.cpp:87` |
| Runout without one | `M600` on the same tool, as on a single-tool printer. | `filament_sensors_handler.cpp:237` |
| `M600 T<n>` | Picks tool `n`, changes its filament, and goes back. | `M600.cpp:223-241, 319` |
| Change all, from the XL menu | For each enabled tool: an unload, the new type, a purge. | `multi_filament_change.cpp:186-233` |
| A load or unload during a print | Reads `ATTENTION` in every phase, not only while waiting. Idle, it reads `BUSY`. | `printer_state.cpp:178-190, 238-243` |

The simulator has the first row, and an idle sensor clear that follows the
picked tool. It always has a tool picked, so T4 (the idle clear on a picked
tool) describes something an INDX does only with a tool left on the head.

One correction applies to every build. After a power cut, the firmware sits
in `IDLE` for at least 1.5 s before the resume starts, and the idle sensor
clear runs in that window. The simulator skips it for the whole resume
(`print_utils.cpp:69-82`).

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

These are the stock profiles' E moves, which the firmware runs as any other
print move. The tool-change numbers are worked out from the profile values
and PrusaSlicer's wipe tower code, not from sliced G-code.

| Printer | At the start | At a tool change | At the end |
| --- | --- | --- | --- |
| MMU3 | `T<first>`, a 17 mm load, then about 53 mm of priming | On the old slot: about 21 mm of ramming, then a net 84 mm retract. `T<n>`. On the new slot: a 32 mm load, then the wipe tower's purge, at least 6 mm for PLA and 15 mm for PETG | `M702`: the firmware unloads, for every print |
| XL | About 19 mm of priming per tool the print uses, 39 mm for a filament with multi-tool ramming | A 1.2 mm retract, about 4 mm of ramming, the wipe tower's purge | Parks the tool. No E move |
| INDX | About 5 mm on the first tool | About 4 mm at the purge station or on the wipe tower | Parks the tool. No E move |

The purge per color change isn't in the profiles: it's PrusaSlicer's
built-in wiping volume. The MMU3 profiles name the base model in `M862.3`
and add `M862.6 P "MMU3"`. The INDX profiles add `M862.6 P"INDX lock"`.

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
- Its feed system (below), and whether it can take an MMU.

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
selected tool, which can be none, and feeding credits the selected tool
through its path's gap. With none selected, nothing counts.

A **feed system** owns the rules that differ: which tool is selected and
when, what a tool change moves, what counts an unload, and whether the idle
sensor clear runs. There are three: direct, toolchanger and MMU. The model's
profile names its system, and a printer's config turns the MMU on.

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

- Toolchanger: the slicer's purge on the incoming tool.
- MMU: the firmware's feed on the outgoing slot, then the slicer's load and
  purge on the incoming one.

The slicer's part (priming, ramming, the load after a change, the wipe
tower, the end G-code's unload) is profile data with the stock profiles'
values, and a print can override it.

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
   runout, spool join, boot, and the preview's unload question.
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

1. **MK4S MMU3, two colors, PLA.** The ramming, the retract before `T`, the
   load after it, and the purge per change with the default wiping volume.
2. **MK4S MMU3, one color.** Whether the start and end differ from run 1.
3. **Core One MMU3, two colors, PETG.** The same, for the other build and
   a filament with more cooling moves.
4. **XL, two tools and five.** Priming per tool, and the purge per change.
5. **Core One INDX, two tools.** The purge station against the wipe tower,
   and whether the retract before a tool change is emitted.

Runs 1 and 3 go in before Workstream 5, runs 4 and 5 before Workstream 4.

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
  single-tool Core One L, which the simulator follows from the source, and
  shows whether the idle clear can run before the resume.
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
- PrusaSlicer's default wiping volume, from [slicer run](#slicer-runs) 1.
- Whether an INDX profile's retract before a tool change is emitted, from
  slicer run 5.

Only hardware could tell, and none is expected:

- How long an MMU's push to the filament sensor takes, which sets the size
  of BF16 in [bridge-findings.md](bridge-findings.md).
- The iX's assisted-insertion length.
- How the real slow feed compares with the tracker's fixed gap.
