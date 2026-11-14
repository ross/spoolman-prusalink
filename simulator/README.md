# Printer simulator

A stand-in for one or more Prusa printers, for developing and testing the
bridge without hardware. It serves the parts of PrusaLink the bridge calls,
and `/api/v1/info`, which the bridge doesn't call yet, on one listener per
printer. It keeps a physical model of each tool's counters and filament, and
gives you a page with buttons for what a user or the filament does: load,
unload, print, run out, jam, reboot, switch off. It runs against
a real Spoolman. The plan behind it is [docs/plans.md](../docs/plans.md).

The bridge never imports this package, and the Docker image doesn't ship it.

## Run it

To run the simulator, a Spoolman, and the bridge together:

```sh
script/demo
```

It starts the container `spoolman-demo` on `127.0.0.1:7912` with Podman or
Docker, unless `DEMO_SPOOLMAN_URL` names a Spoolman to use. Then it seeds Spoolman,
writes the bridge's config to `simulator/data/bridge.toml`, and runs the
simulator and the bridge until you press Ctrl-C. Spoolman's data lives on the
volume `spoolman-demo-data`, so it carries over between runs; the container
keeps running afterwards. `SIM_CONFIG` picks another simulator config. See
the comments in `script/demo` for the rest.

The rest of this section does the same steps by hand. To run only the
simulator:

```sh
script/bootstrap
.venv/bin/python -m simulator -c simulator/sim.example.toml run
```

The page is at `http://127.0.0.1:8000`. The printers listen on 8001, 8002,
and so on, in config order. Each has its own API key, shown on its card.

To point the bridge at the simulated printers:

```sh
.venv/bin/python -m simulator -c simulator/sim.example.toml seed --spoolman http://127.0.0.1:7912
.venv/bin/python -m simulator -c simulator/sim.example.toml bridge-config > bridge.toml
```

`seed` creates a vendor, a few filaments, and two spools per tool in Spoolman:
one in the tool's location and a spare on the `shelf`. A tool's spool gets the
tool's configured material and instance UUID, so the demo starts in agreement, as
with `sim.example.toml`. That holds when the material and color match one of
`seed`'s filaments other than Nylon-X, whose temperatures differ from the
simulator's defaults. A material `seed` has no filament for gets one from a fixed
cycle instead. On tool 0 of a model that holds UUIDs, a tool with no material
gets a new UUID. It never deletes or moves anything, and running it again
changes nothing, so a Spoolman seeded earlier keeps its old spools and UUIDs.
`bridge-config` prints the bridge's `[[printers]]` list with the same locations. Add `[spoolman]`
and `[bridge]` tables to it, with short poll intervals, and run the bridge:

```toml
[spoolman]
url = "http://127.0.0.1:7912"

[bridge]
db_file = "/tmp/sim-bridge.db"
poll_printing = 2
poll_idle = 3
```

```sh
.venv/bin/spoolman-prusalink -c bridge.toml run
```

## Config

See [sim.example.toml](sim.example.toml), which shows every setting with its
default. A printer has a `model` (a PrusaLink type code such as `8.1.0`), an
`api_key`, a `firmware` mode, `checks` for the print preview, `high_flow`
for a high-flow nozzle, `sensors` for the menu's switch of every filament
sensor, and `[[printers.tools]]` entries that can start
loaded with a material and counters. `checks` is `ignore`, `warn` (the
default) or `abort`, and it stands for every setting in the printer's menu
that governs a check of a file's `M862` lines. The top-level settings are
timing and lengths: the time scale, how fast loads and prints move filament,
the purges of a tool change, a jam and a power-panic resume, the preheat and
park times, and whether the start blip is on. The feed ratio and a load's slow, fast and purge
lengths are overrides: left out, each printer uses its model's value, which
the page shows on the printer's card. On the page or through
`/api/settings`, an empty value or `null` unsets an override. The purge
override follows the firmware's `purge_length`: it replaces the model's
length, a high-flow nozzle doesn't double it, and a purge is never shorter
than 1 mm. The iX purges in 5 mm steps, so it rounds the length up to the
next multiple of 5, and INDX ignores the override: its nozzle cleaner purges
25 mm.

## The page and the control API

Every button posts to `POST /api/printers/<name>/<action>` with a JSON body,
so a script can do the same:

```
curl -X POST -H 'Content-Type: application/json' \
  -d '{"tool": 0, "source": "preset", "name": "PETG"}' \
  http://127.0.0.1:8000/api/printers/core-one-l/load
```

Actions: `load`, `unload`, `extrude`, `retract`, `select_tool`,
`start_print`, `pause`, `resume`, `cancel`, `dismiss`, `home`, `runout`, `jam`,
`clear_jam`, `reboot`, `set_power`, `set_fault`, `set_firmware`,
`set_sensor_fault`, `set_stuck_write`, `set_tool_enabled`,
`set_checks`, `set_sensors`, `counter_step_back`,
`set_extruded_at_unload`, `tap_through`. Parameter names are the keyword arguments of the methods with
those names in `model.py`, and their type hints say what each takes: JSON
`true` or `false` for a bool, an integer such as `3` (not `3.0`) for an int,
any finite number for a float, and a listed value for an enum. An action the
printer can't do now answers 409 with the reason; bad parameters answer 400
and name the parameter.

`start_print` takes the fields of `PrintSpec`: `segments`, each
`{"tool": T, "slicer_mm": MM}` with an optional `"material"`, the filament
type the file names for that tool, and the optional `duration_s`,
`preheat_s`, `color_changes_mm` and `name`. The page's start form takes a
segment per line as `tool:mm` or `tool:mm:TYPE`.

The state lists each printer's and each tool's actions under `actions`, each
with null when the printer would take it now or the reason it would refuse
it, with 409 or 400, so a script can check before it asks.

`GET /api/state` is everything the page shows, `GET /api/events` streams it
as server-sent events, `POST /api/settings` changes a setting, and
`POST /api/snapshot/<name>` and `POST /api/restore/<name>` save and load the
whole model under `simulator/data/`. `POST /api/advance` with
`{"seconds": 30}` jumps simulated time forward, so a script can step through
a scenario without waiting.

The event log at the bottom of the page shows every action, state change,
and request each printer received, with the body of every uploaded job.

## What it copies from the firmware

[docs/printer-behaviors.md](../docs/printer-behaviors.md) is the list of
printer behaviors the simulator has to get right, each with its firmware
source, the trace recorded from the printer where there is one, and the
replay in `simulator/tests/test_traces.py` that checks the simulator against
that trace. The points that matter for the bridge, in short:

- An unload bumps `unload_count` and `extruded_at_unload` and hides the
  material, but keeps it: a runout or `M600` reload restores the old name,
  color and UUID. A menu load replaces them with a preset, dropping color
  and UUID.
- Each model has its build's presets. PPS and PPA exist only on the Core One
  and Core One L. The PC and FLEX preheats differ on INDX, the MINI and the
  MK3.5, and the PA temperatures on the MINI. A menu load of a preset the
  build lacks answers 400, and an `M865` preset write of one stores nothing.
- Every filament move counts as the firmware's filament tracker counts it.
  A retract grows the tool's gap up to the model's limit, 40 mm or 27 mm on
  INDX. A feed fills the gap first, and what goes past it counts times the
  model's feed ratio: 380/400 steps, or 550/576 on INDX. This covers prints,
  loads, purges and manual moves.
- A menu load feeds the model's slow and fast lengths with no material
  reported, sets the material, then purges. The purge ends with an 8 mm
  retract, except on the iX and INDX, whose nozzle cleaners end it. A
  high-flow nozzle doubles the purge, except on INDX, whose cleaner purges a
  fixed 25 mm. An idle load then auto-retracts. A reload during a print
  primes 4 mm and retracts 8 mm instead, and the resume puts the tool back at
  the gap it had when the change began. From a full gap, a Core One L load
  counts 20 mm before the material shows and 63 mm after, times the feed
  ratio: 40 mm of purge and 23 mm of auto-retract ramming.
- An auto-retract runs the model's `auto_retract` ramming sequence and stores
  how far it left the filament retracted: 55 mm on a Nextruder, 8 mm on INDX.
  It runs at the end of an idle load, after the INDX cleaner purge, and at a
  print's end, and it skips a tool already retracted that far, flexible
  filament, and a printer with `auto_retract = false`. The next forward move
  feeds the stored distance back first. At boot, the stored distance sets
  the tool's gap; with nothing stored, the gap starts at 0 and the tracker
  reports no retracted distance until a feed reaches the nozzle. A Nextruder print's end counts 23
  mm; an INDX print's end counts nothing, because the 8 mm park retract
  already counts as retracted. The MINI, MK3.5 and the dev kit don't
  auto-retract.
- An unload or runout rams with the model's `unload` or `runout` sequence,
  then retracts what's left of the unload length. INDX purges 2 mm first.
  A menu unload of a Nextruder tool that auto-retracted skips the ramming
  and counts nothing. An unload of a tool that fed since its auto-retract
  counts 23 mm times the feed ratio; a Nextruder runout counts 7 mm.
- A print started from the page stands for the user pressing Print on the
  file's preview: the job is counted at once and reads `PRINTING` while the
  file is scanned. Then the preview checks the tools the print's segments
  use, and each question holds the job in `ATTENTION`, with its ID in
  `/api/v1/status` and `PAUSED` in `/api/v1/job`. On a printer without
  tool mapping, the first screen lists more used tools than enabled ones
  (a warning), a disabled used tool (Abort only) and a used tool with no
  filament (a warning). Then, for an empty tool, the job waits for a
  `load` of that tool, which answers Yes. The load reads `BUSY` with no
  job. It neither primes nor runs an idle load's final auto-retract, but
  the INDX cleaner's purge still ends with its own. A power cut while the
  preview waits leaves nothing to resume. Then a used tool whose
  stored type isn't the segment's `material`, compared exactly, asks
  again. On the XL and INDX, a print that uses two tools, or any print with
  two tools enabled, asks one tools-mapping question with every per-tool
  row, the type included, and asks it with nothing wrong too. While that
  question is up the printer isn't printing, so the idle sensor clear runs
  on the picked tool. Nothing loads there. Print then asks once for each
  kind of failed row, across all the tools: a used tool empty asks to turn
  the filament sensors off, which `tap_through` does, and then a used tool
  holding another type asks too, an empty one included when its segment
  names a type. `tap_through` answers Ignore, and `cancel` goes back to the
  tools-mapping screen with the sensors as they are. The screen keeps the
  rows it showed when it opened, so Print asks the same questions again. A
  disabled used tool leaves Abort the only way off the screen.
  `tap_through` answers Print or Ok, and `cancel` answers Abort or No,
  which ends in `IDLE` with no job and the ID used up. `checks` governs
  none of these: the firmware's severities are fixed.
- A print never waits for a tool with no filament. A runout needs the sensor
  to see filament go, and nothing checks for filament when a tool is
  picked, so a print that reaches an empty tool prints on through it: the
  tool's `extruded` counts the purge and the moves with no `material`.
  `sensors` is the menu's switch for every filament sensor. Off, there's no
  runout, no idle sensor clear and no filament question at the preview, and
  it stays off until `set_sensors` turns it on.
- Every print start shows `PRINTING`, then a brief `IDLE` with no job, then
  `PRINTING`. `FINISHED` and `STOPPED` persist until dismissed, and the job
  disappears from `/api/v1/status` and `/api/v1/job` as soon as a print ends.
- The sync job is accepted only when a print could start, runs as a job
  with the uploaded file's name, and applies `M865`, `M865.1` and `M117`.
  The job is counted at once and reads `PRINTING` while the file is
  scanned. Then the print preview checks the `M862` lines in the file's
  first 200 G-code lines: the model by compatibility group (`M862.3`), the
  G-code level (`M862.5`), and the features (`M862.6`): input shaper on
  every build but the iX, the INDX lock on INDX, an MMU3 file with the MMU
  off, and a feature the build doesn't know. A failed check that `checks`
  doesn't ignore holds the job in `ATTENTION`, with its ID in
  `/api/v1/status` and `PAUSED` in `/api/v1/job`, and one screen lists
  every failed check. Under `warn`, `tap_through` answers Print: the start
  blip, then the job runs. Under `abort`, only `cancel` answers, as Abort.
  Abort ends in `IDLE` with no job, the ID used up and the file kept, and
  nothing parks or moves. The INDX lock is a warning whatever `checks` is.
  A `cancel` during the scan or the start blip ends the same way, for a
  print started from the page too. A line with no `P`, such as the query
  form `M862.3 Q`, checks nothing.
- INDX and XL printers are several tools with their own counters. A tool
  change isn't an unload; the incoming tool gets a purge. Neither stores
  UUIDs, so `M865.1` does nothing there. Disabled tools don't appear in
  `/api/v1/filament`.
- MINI, MK3.5 and MK3.5S report the material with no counters. The
  `no_endpoint` firmware mode answers 404, like stock firmware.

- A power-panic resume (`reboot` with `keep_job`, or `set_power` on with
  `resume`) keeps the job ID, restarts the counters, skips the idle sensor
  clear, and goes through `BUSY`, during which a nozzle clean purges
  `panic_purge_mm`, then a short `PAUSED`, then `PRINTING`. With `cold_bed`
  it waits in `ATTENTION` for `resume` before the clean. The page's
  **Power-loss resume** sends `cold_bed` from its **cold bed** checkbox, and
  **Resume** continues that wait.
- An upload during the start blip is accepted and dropped: the firmware's
  state check passes while it reads `IDLE`, but the print start ignores it.
  The user's print goes on and the uploaded file never runs.

Choices made where the firmware was silent or the plan left it open:

- While a printer is off, `load` and `unload` change the filament by hand
  with no counters. At the next boot, a tool with a stored type and no
  filament counts one unload, as the firmware's idle sensor clear does on
  every printer, single-tool included.
- A `jam` with `unload` off goes to `ATTENTION` without counting anything and
  `clear_jam` resumes it. With `unload` on, it's a runout whose reload purges
  `jam_purge_mm` more, on top of the 1 mm minimum and before the iX's
  rounding. The firmware has no extra jam purge: it stands for the user
  picking Purge more.
- The iX's assisted insertion before the slow load isn't modeled: the
  firmware source doesn't give its length. The iX purges its length in one
  move, not in 5 mm steps with a clean between.
- A pause and resume with no filament change moves no filament. The
  firmware retracts at the pause and feeds the same length back at the
  unpark, which counts nothing unless the gap was near its limit.
- `fill()` and a tool built loaded start with gap 0, the tracker valid and
  nothing stored, on every model. A real printer that just loaded would
  have auto-retracted.
- Ramming, unload and auto-retract moves run at the load rate, not at the
  sequence's feedrates.
- The menu unload starts with a cold nozzle, so it gets no 8 mm park
  retract. On INDX it counts 19 mm: the 2 mm purge and 17 mm of ramming.
  With a hot nozzle the firmware's park would make it 17 mm.
- An INDX tool has no extruder filament sensor, so the firmware sends a jam
  to `manual_unload` in `pause.cpp`, with no ramming and no unload counted. The simulator rams and counts it as a runout.
- The `M865` flexible flag isn't modeled: only the preset names in
  `FLEXIBLE_PRESETS` count as flexible.
- The nozzle cleaning at a print's start isn't modeled. Before it,
  `prepare_for_nozzle_cleaning` (`probe.cpp`) calls
  `ensure_retracted_no_ramming` when the stored distance is known and under
  5 mm and the filament isn't flexible, and `G29` calls it with an 8 mm
  purge when a cleaning fails on a printer with a nozzle cleaner; it
  retracts to the full distance (20 mm on a Nextruder, 8 mm on INDX) with
  no ramming, whatever the switch says, and stores that distance for the next feed to feed back.
- Temperatures, `G12`, the mesh bed leveling retract and the MMU don't move
  filament in the simulator.
- A power loss during a filament change drops the gap to restore: the
  resume after the reload doesn't move.
- One `checks` setting stands for the firmware's settings per kind of check
  (model, compatibility mode, G-code level, input shaper and firmware). The
  checks share one screen and the worst severity decides it, so one setting
  shows the bridge every outcome.
- The MMU is always off, so a file with `M862.6 P"MMU3"` fails on a build
  that knows the MMU3. A file that declares the `HT Hotend` is taken as
  satisfied: whether the hotend is high-temperature is printer state the
  simulator doesn't hold.
- A print started from the page isn't a sliced file and has no `M862`
  lines, so the preview checks only its tools: no model, level or feature
  check. The simulator judges only the files the bridge uploads that way.
- The preview screen before Print isn't modeled: `start_print` is the user
  pressing Print, or Continue on the XL. The bed preheat while the
  tools-mapping screen is up isn't modeled: the simulator reports no
  temperatures.
- A tool is used when a segment names it; the firmware reads the file's
  `filament used [g]`. The filament sensor is the tool's `loaded` flag, so a
  sensor fault doesn't raise the filament question.
- The preview's load is the user's choice of what to load. The firmware
  loads the file's type when it knows the name and asks otherwise. The
  wrong-filament screen's Change, which runs a filament change, and the
  filament-not-inserted screen's "disable the filament sensor" answer
  aren't modeled. Neither is the
  firmware's mapping of a print with one tool to the only enabled tool on a
  toolchanger. The load can't be cancelled.
- A print's end auto-retracts the picked tool even when it has no
  filament, as the simulator does for a loaded one. The source doesn't
  say otherwise, but a trace hasn't shown it.
- The simulator has no spool join. The firmware refuses to turn the
  sensors off when a print joins spools.
- Stuck-filament detection isn't modeled. Load-cell printers have it, on by
  default except on INDX. The MINI and MK3.5 don't have it.
- The preview doesn't model an uncalibrated INDX nozzle cleaner, which
  refuses every print, the unfinished-selftest screen, the
  new-firmware screen (`M115 U`, which continues by itself after 30 s),
  `M862.4` firmware versions, `M862.1` nozzle checks, `M862.2` numeric model
  codes, the wastebin, file errors, answering through Connect, or
  `DELETE /api/v1/job`.

## Tests

```sh
script/test simulator/tests
```

They're part of `script/test`, with the bridge's own suite on `tests/`.

`simulator/tests/test_traces.py` replays each behavior in
`docs/printer-behaviors.md` and compares the simulator with the trace
recorded from the printer under `simulator/tests/traces/`. A behavior with
no trace yet is skipped. To record one:

```
python -m simulator.probe --url http://<printer> --key-env PRUSALINK_API_KEY \
    --out simulator/tests/traces/F2-unload.jsonl --note "menu unload"
```

The probe only reads, so it can run beside a live bridge. It writes a sample
whenever the state, job, boot ID, material or counters change, and a line
typed on stdin becomes a timed mark. `python -m simulator.trace FILE` prints
a trace's steps, and with two files compares them the way the test does.
