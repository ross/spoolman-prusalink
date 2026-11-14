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
sensor, `picked_tool` for the tool picked at start, and `[[printers.tools]]`
entries that can start loaded with a material and counters. `picked_tool`
names an enabled tool; left out, a direct printer has tool 0 picked and a
toolchanger has none. `mmu = true` gives a model that takes an MMU3, the
MK3.9, MK3.9S, MK4, MK4S, Core One and Core One L, an MMU that starts on and
connected. Its tools are then the MMU's slots 0 to 4, each entry's material
is that slot's stored type, and `picked_tool` names the slot whose filament
is in the extruder; left out, no slot is loaded or selected. An MMU needs
`sensors` on, and its slots can't be disabled. `checks` is `ignore`, `warn` (the
default) or `abort`, and it stands for every setting in the printer's menu
that governs a check of a file's `M862` lines. The top-level settings are
timing and lengths: the time scale, how fast loads and prints move filament,
the extra purge of a jam, the preheat and
park times, the power-panic resume's wait and phases, how long an MMU takes
to connect (`mmu_connect_seconds`), and whether the start blip is on. The feed ratio and a load's slow, fast and purge
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

Actions: `load`, `unload`, `load_to_nozzle`, `eject`, `cut`, `extrude`, `retract`,
`select_tool`, `park_tool`, `change_all`, `preload_all`, `start_print`, `pause`, `resume`, `cancel`, `dismiss`, `home`, `runout`, `jam`,
`change_filament`, `clear_jam`, `reboot`, `set_power`, `set_fault`, `set_firmware`,
`set_sensor_fault`, `set_stuck_write`, `set_tool_enabled`,
`set_checks`, `set_sensors`, `set_mmu`, `counter_step_back`,
`set_extruded_at_unload`, `tap_through`, `unload_first`. Parameter names are the keyword arguments of the methods with
those names in `model.py`, and their type hints say what each takes: JSON
`true` or `false` for a bool, an integer such as `3` (not `3.0`) for an int,
any finite number for a float, and a listed value for an enum. An action the
printer can't do now answers 409 with the reason; bad parameters answer 400
and name the parameter.

`start_print` takes the fields of `PrintSpec`: `segments`, each
`{"tool": T, "slicer_mm": MM}` with an optional `"material"`, the filament
type the file names for that tool, and the optional `duration_s`,
`preheat_s`, `color_changes_mm` and `name`. On a printer with a multi-tool
slicer profile, `prime_mm` and `toolchange_purge_mm` replace the profile's
prime and tool-change purge; elsewhere they answer 400. On an MMU the prime
is the purge line. `single_tool_file`, on an MMU only, is a file that
prints on tool 0 alone and has no MMU profile, as one sliced for the
printer without the MMU; it takes neither override. Only such a file is
single-tool to the firmware: a one-color MMU3 profile file lists all five
slots, so it isn't, even on slot 0. On a toolchanger,
`spool_join` is a list of `{"tool": A, "to": B}`: when tool A runs out, the
print goes on with tool B. Each tool joins on once and is joined to once,
no chain loops, and B can't be a tool the segments use. The page's start
form takes a segment per line as `tool:mm` or `tool:mm:TYPE`, the two
overrides on a toolchanger or with the MMU on, the joins as `A>B, C>D` on a
toolchanger, and a single-tool file box.

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
  Each tool has a filament path, which holds its gap, its stored
  auto-retract distance and its sensor. A retract grows the path's gap up to
  the model's limit, 40 mm or 27 mm on INDX. A feed fills the gap first, and
  what goes past it counts times the model's feed ratio: 380/400 steps, or
  550/576 on INDX. This covers prints, loads, purges and manual moves.
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
  the path's gap; with nothing stored, the gap starts at 0 and the tracker
  reports no retracted distance until a feed reaches the nozzle. A
  single-tool Nextruder print's end counts 23 mm. A toolchanger's finished
  print counts only its slicer profile's last moves at its end, because its
  end G-code parks the tool first. A stopped XL print counts the 23 mm on
  its picked tool, and a
  stopped INDX print counts nothing, because the 8 mm park retract already
  counts as retracted. The MINI, MK3.5 and the dev kit don't auto-retract.
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
- An MMU3's slots share one filament path, sensor and gap (F38, F39).
  `set_mmu` is the menu's switch, which a print or any operation blocks.
  With it on, `/api/v1/info` reports `mmu` as true and `/api/v1/filament`
  lists the five slots; with it off, slot 0 alone. Turning it off clears
  slot 0's type, an unload, unless the extruder's sensor sees filament.
  Turning the sensors off turns it off too. Turning it on, or a boot with it
  on, leaves no slot selected while the MMU connects for
  `mmu_connect_seconds`, and E moves count on slot 0 meanwhile. Once it's
  connected, moves count on the selected slot; with none selected they
  count nothing, the gap stays where it was, and `/api/v1/status` reads
  `flow` 0. The idle sensor clear and the tag reader are off while the MMU
  is on, and a print's end with it connected doesn't auto-retract. An MMU
  printer refuses the menu's plain load.
- A print on an MMU runs the stock MMU3 profile's moves and the firmware's
  tool changes (F40, F42, F46). It's refused while the MMU connects. The
  start retracts 2 mm with no slot selected, then `T<first>` feeds the slot
  with no slot credited, and the 17 mm load and the purge line count on it.
  At each change the old slot rams, parks and cools, the firmware retracts
  80 mm and feeds 189.9, 7 and 50 mm, all on the old slot, and the new slot
  loads 32 mm and purges. A `T` to the selected slot does nothing. A
  multi-color print's end rams and parks the last slot, and every print
  then runs `M702`, which unloads into the MMU. A single-tool file gets the
  firmware's `T0` and 35 mm when no slot is selected, and a finished one
  unloads into the MMU. A stopped print or sync job unloads too, while it
  reads `PRINTING`; a finished sync job leaves the filament.
- A `runout` on an MMU is FINDA's, so it takes the slot whose filament is
  fed (F44). On the selected slot it's the MMU's `M600`, as are
  `change_filament` and a color change: the park retracts 8 mm, the MMU's
  ramming counts on the slot, the unit ejects it, and the printer waits in
  `ATTENTION` for the filament to be loaded into the MMU. No type is
  written, so no unload counts. `load` with no source then reloads the same
  slot: the feed counts nothing, the load to the nozzle counts 7 mm past
  the 40 mm gap and the prime 4, and the print waits `PAUSED` for the
  resume, which feeds the 8 mm back. With a spool join from the slot, the
  join unloads the slot into the MMU, changes to the join's slot from no
  slot and loads it to the nozzle, while it reads `PRINTING`, and the print
  goes on with the join's slot; the joins' rules are the toolchangers'.
  With no slot selected, as after a power-panic resume, a runout injects
  nothing and `change_filament` and a color change do nothing. The
  firmware leaves its `m600_sent` set after such a runout, which may block
  later runouts; that isn't modeled. `change_filament` names the selected
  slot only. A reload takes no `purge_mm`, and `jam` is refused: the MMU's
  jam isn't read.
- The preview on an MMU (F43). With the MMU connected, no used slot is
  checked for filament, and a print always gets the tools-mapping screen.
  When FINDA sees filament, a slot fed past the selector, the preview first
  asks to unload it, holding the job in `ATTENTION` with its ID: a print
  from the page and a sync job alike. While the MMU connects, FINDA's
  sensor isn't read yet, so the question asks whatever FINDA sees; only an
  upload's sync job can start then. A `single_tool_file` of slot 0's type
  with filament at the extruder's sensor skips the question and the
  mapping screen and prints at once. `tap_through` answers No, which prints
  with the filament in place and no mapping screen. `unload_first` answers
  Yes: the MMU unloads the filament over `unload_seconds`, counting on the
  selected slot or on none, reading `BUSY` with no job, and `cancel` waits
  for it; then the mapping screen or the job. `cancel` at the question
  ends in `IDLE` as at any preview question. A power cut during the unload
  drops the job and leaves the filament in.
- The MMU menu's operations need the MMU on and connected and the printer
  idle, and each reads `BUSY` until it's done (F41).
  `load_to_nozzle` is Load To Nozzle: it unloads the selected slot, feeds
  the new slot to the extruder with no slot selected, selects it, and loads
  17 and then 30 mm, which count on the slot, then writes back the slot's
  own type. It refuses while the extruder holds filament and no slot is
  selected, as after a boot: unload that filament first. `unload` takes the
  filament in the extruder, the fed slot's, back into the MMU, whether or not
  a slot is selected: it rams, or only relieves the pressure when the
  filament is fully auto-retracted, and leaves no slot selected. It writes no type, so no
  unload counts. `eject`, `cut` and each preload take `unload_seconds` and
  move nothing the bridge counts. `eject`, `cut` and `change_all` refuse while
  the extruder holds filament. `preload_all` preloads every slot in turn with
  the preset `name`: it stores the type on slots 0 and 1 at once and on each
  later slot when the preload two before it ends, as the firmware's menu
  waits on the G-code queue. A power cut keeps the types stored so far and
  drops the preloads left.
- Every print start shows `PRINTING`, then a brief `IDLE` with no job, then
  `PRINTING`. `FINISHED` and `STOPPED` persist until dismissed, and the job
  disappears from `/api/v1/status` and `/api/v1/job` as soon as a print ends.
- The sync job is accepted only when a print could start, runs as a job
  with the uploaded file's name, and applies `M865`, `M865.1` and `M117`.
  The job is counted at once and reads `PRINTING` while the file is
  scanned. Then the print preview checks the `M862` lines in the file's
  first 200 G-code lines: the model by compatibility group (`M862.3`), the
  G-code level (`M862.5`), and the features (`M862.6`): input shaper on
  every build but the iX, the INDX lock on INDX, an MMU3 file unless the
  MMU is on and connected, and a feature the build doesn't know. A failed check that `checks`
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
  change isn't an unload. Neither stores UUIDs, so `M865.1` does nothing
  there. Disabled tools don't appear in `/api/v1/filament`.
- A print on INDX, or on an XL with more than one enabled tool, adds the
  stock PrusaSlicer profile's moves outside the model (`slicer.py`), each
  counted on the picked tool through the tracker. They're profile data from
  PrusaSlicer 2.9.7 with the PrusaResearch vendor bundle 2.5.16, not
  firmware, so `script/firmware-check` doesn't compare them. The bundle has
  no Core One L INDX, so both INDX models use the Core One INDX numbers.
  - XL: the first tool retracts 2 mm; each other used tool, in ascending
    order, primes 39 mm and retracts 20; then the first tool primes 39 mm
    and retracts 0.8, which its first move feeds back. At a tool change
    the old tool rams 4.1575 mm and retracts 20; the new tool feeds the 20
    back and purges 9.238 mm on its first pick in the print, which stands
    for the first layer, and 7.0076 mm on later ones. At the end the last
    tool rams 4.1575 mm when the print uses several tools, then retracts
    0.8.
  - INDX: `G427` purges every used tool, in ascending order, in the nozzle
    cleaner: 25 mm, then the auto-retract. The first tool then primes 1.6
    + 5.4907 mm and retracts 0.8, fed back. At a tool change the old tool
    retracts 0.8 mm, then 8 in its park (`G27 R8`). The new tool's first
    pick moves 1.6 mm, purges 4.1596 and retracts 0.8, fed back; a later
    pick first moves 1.6 (the lock move), 8 and 0.8 back. At the end the
    last tool retracts 0.8.

  Unused tools aren't picked. A print's own retract while an auto-retract
  distance is stored doesn't move the filament and keeps the distance,
  though the tracker counts it. The priming runs once, when the print
  first starts feeding, so a pause, a power-panic resume or a filament
  change doesn't run it again.
- A model's feed system, direct, XL or INDX, holds the rules that differ
  between them (`feed.py`). The XL and INDX feed systems are the two
  toolchangers, and the rest of the models are direct. `ModelInfo.feed` names
  the system, and `script/firmware-check` compares it with the build's
  `HAS_TOOLCHANGER` and `HAS_INDX`. The feed system says whether `select_tool` can pick a
  tool, whether the preview's tools-mapping screen applies, what a
  print's tool change does, which tool a move counts on, which tool's
  sensor the idle sensor clear reads, and where the picked tool rests after
  a menu load or unload, a pause and a print's end.
- On a toolchanger, a menu `load` or `unload` picks its tool first, as the
  printer's menu does. The pick reads `IDLE` and opens no dialog, so the
  idle sensor clear runs on the newly picked tool before the operation
  starts: a stored type over an empty sensor counts an unload. INDX docks
  the tool when the operation ends, and the XL leaves it picked. A load
  the preview waits for picks its tool inside the dialog, with no clear,
  and INDX docks it after too. A reload in a print neither picks nor docks.
  `park_tool` parks the picked tool, as the Pick/park menu does.
- `change_all` is the Filament menu's Change all on a toolchanger, and its
  Preload To MMU on an MMU. It takes
  `tools`, a list of enabled tools, and the preset `name` with an optional
  `color`, the same for every chosen tool; the other tools keep their
  filament. The tools change one at a time in index order, and the printer
  reads `BUSY` from the first to the last. A tool with a stored type
  unloads first, which counts the unload, then loads; a tool with none only
  loads. The stored type decides, not the sensor. Each tool is picked
  inside the dialog, so no idle sensor clear runs at the pick. INDX docks
  each tool after its load, and the XL ends with the last tool picked. A
  reboot or power cut drops what's left, and a power-panic resume waits
  until the last tool is done. On an MMU, each chosen slot preloads and
  then stores the new type, with no unload counted, and `unload` lists the
  slots whose type the dialog clears, which counts an unload. A power cut
  mid-preload writes no type. The page's **Change all** opens the form.
- `change_filament` is the Tune menu's Change filament, `M600 T<n>`, on any
  enabled tool once a print is printing. On the picked tool it's the
  filament change a runout runs. On another tool it picks that tool, runs
  the same change there, and the `resume` after the reload changes back to
  the print's tool with no purge, so the counts land on that tool only.
  The pick moves nothing and takes no time. The reload writes the type
  the tool had, else the type the file names for it, else the type its
  last unload left; with none of them, the firmware can't heat the tool,
  and nothing changes. A power cut during the change resumes on that tool,
  which prints until the file's next tool change. The page's **Change**
  button sends it.
- A load or unload during a print reads `ATTENTION` in every phase, the
  reload's load included; with no print, `BUSY`.
- A `runout` on a tool with a spool join unloads it at once, which counts
  the unload, and reads `ATTENTION` for `unload_seconds`. Then the printer
  picks the join's tool, purges it as the firmware's join does, puts the
  old tool's gap back on it and reads `PRINTING`. The join's tool keeps its
  type, and the print goes on with it wherever the file names the old tool,
  through later tool changes too. With no join from the tool, the runout is
  the filament change. A jam never joins, and the join can't be paused. A
  power cut during the unload resumes on the old tool, empty, without the
  join's change or purge, until the file's next tool change.
- `extrude` and `retract` move only the picked tool, as the menu's Move E
  does. With no tool picked they answer 409, and on another tool 400.
- The selected tool can be none, which stands for a toolchanger with no
  tool picked: a toolchanger at start unless `picked_tool` names a tool,
  either toolchanger after a finished print or `park_tool`, an INDX after a
  menu load or unload, a stopped print and a pause, and an INDX booted
  after a cut during a print that used several tools. Then no filament
  moves or counts, the idle sensor clear doesn't run, and a print's end
  neither retracts nor auto-retracts. The page shows the tool as "none".
  An XL with one tool enabled isn't a toolchanger in any of this: its
  toolchanger is off in the firmware, so the tool stays picked, a print
  runs no profile moves and its end doesn't park, and `select_tool` and
  `park_tool` are refused.
- Where the picked tool rests. A finished print with a multi-tool slicer
  profile ends with the stock profiles' `P0 S1`, which parks the tool
  before the firmware's end retract and auto-retract, so those find no
  tool. A stopped
  XL print and a sync job keep the tool, and INDX docks whatever is picked
  at a job's end. INDX docks the tool at a pause, after an 8 mm retract,
  and the resume picks it again and purges it in the nozzle cleaner: 25 mm,
  then the auto-retract, before the job reads `PRINTING`. The XL keeps its
  tool through a pause. A boot keeps the picked tool, except on INDX after
  a cut during a print that used several tools: its EEPROM then holds no
  tool.
- MINI, MK3.5 and MK3.5S report the material with no counters. The
  `no_endpoint` firmware mode answers 404, like stock firmware.

- A power-panic resume (`reboot` with `keep_job`, or `set_power` on with
  `resume`) keeps the job ID and restarts the counters. The printer boots
  `IDLE` with no job for `panic_idle_seconds`, as the firmware does until
  its autostart resumes the print, and the idle sensor clear runs then on
  the picked tool, if one is. The resume picks the print's tool before
  anything moves, then goes through `BUSY`, then a short
  `PAUSED`, then `PRINTING`. A menu load or unload still running when the
  wait ends delays the resume by another `panic_idle_seconds`, until the
  printer is idle. A print started in the wait, an upload too, drops the
  resume, and the event log says "resume dropped". A plain reboot drops it
  too, and another power cut leaves it waiting.
  A print that wasn't paused retracts 8 mm at the cut on every model but
  the XL. INDX purges 25 mm in its nozzle cleaner halfway through `BUSY`,
  with no auto-retract after. Every other model that retracted feeds the
  8 mm back at the end of `BUSY`. Both count whether or not the tool holds
  filament. A print that was paused comes back `PAUSED` after the `BUSY`,
  with no filament moved. Only `pause` counts as paused. A cut during a
  filament change dialog, a runout's, a jam's, a color change's, Tune >
  Change filament's or a spool join's unload, resumes on its own with the
  dialog gone. The print goes on through what the cut left in the tool,
  an empty tool included, with no runout. A load cut short leaves its
  filament in, which is inferred: nothing in the cut moves the filament
  back. A color change in the file runs again after the resume. The
  resume picks the tool the cut found picked, which prints until the
  file's next tool change, or until the start G-code's when the print
  hadn't primed yet. The MINI and the XL dev kit build no power panic,
  so there a cut drops the job and the printer boots idle, as a plain
  reboot does. With `cold_bed`, the resume waits in `ATTENTION` for
  `resume` before recovering, for a print that was paused too. The page's **Power-loss resume** sends
  `cold_bed` from its **cold bed** checkbox, and **Resume** continues that
  wait.
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
- A pause and resume with no filament change moves no filament, except on
  INDX. The firmware retracts at the pause and feeds the same length back
  at the unpark, which counts nothing unless the gap was near its limit.
  The iX's resume runs its nozzle cleaner's `purge_clean` too, which isn't
  modeled.
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
- Temperatures, `G12` other than INDX's power-panic purge, the mesh bed
  leveling retract and the MMU don't move filament in the simulator.
- A power cut during a runout's or a jam's dialog retracts 8 mm, as any
  cut in a print does. The firmware skips that retract when the runout or
  jam interrupted a move, which runs it through the crash recovery
  (`was_crashed`). The boot leaves the tracker at gap 0 and untrusted
  either way, so the resume's feed back counts the same.
- The power-panic resume starts at the autostart's first try, 1.5 s after
  the boot: the firmware tries every 1.5 s until the USB drive, the
  temperatures and the screen are ready, and gives up after 100 s, which the
  simulator never reaches. The firmware doesn't wait for a menu load or
  unload: it resumes and its moves queue behind the operation. The simulator
  waits for the printer to be idle instead, trying again every 1.5 s. A print
  started in the wait drops the resume, as the firmware's autostart does
  when its next try finds the printer printing and not yet ready. When that
  try finds everything ready, the firmware starts the resume over the new
  print: its check that the printer is idle is a debug assertion
  (`resume_print` in `power_panic.cpp`). The simulator doesn't model that.
- One `checks` setting stands for the firmware's settings per kind of check
  (model, compatibility mode, G-code level, input shaper and firmware). The
  checks share one screen and the worst severity decides it, so one setting
  shows the bridge every outcome.
- A file that declares the `HT Hotend` is taken as
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
  `filament used [g]`. The filament sensor is the path's `loaded` flag, so a
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
- What the preview's Yes unloads while the MMU connects, or once it's
  connected with FINDA clear after a question asked while it connected,
  isn't in the source: `SelectorSlot()` assumes the MMU is up
  (`marlin_print_preview.cpp:709-711`). `unload_first` is refused then;
  No and Cancel work.
- The tools-mapping screen doesn't check a spool join's tools. The
  firmware checks each tool in a join's chain for filament and type, and
  refuses a join whose earlier tool has no working sensor, so the sensors
  can't be turned off for a print that joins
  (`gcode_compatibility.cpp:520-549`).
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
