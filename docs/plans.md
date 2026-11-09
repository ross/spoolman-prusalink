# Printer simulator

This is the plan for a simulator that stands in for one or more Prusa
printers, so the bridge can be developed and tested for several printers and
for multi-tool printers without the hardware. It fakes the parts of PrusaLink
the bridge uses, keeps a physical model of each printer's tools and counters,
and serves a web page with buttons for what a user or the filament does:
load, unload, print, run out, jam, reboot, switch off.

It lives in `simulator/` and is separate from the bridge: the bridge never
imports it, and the Docker image doesn't ship it. It's a development tool,
so it has no review or test requirement. A few tests on its G-code parser
and state machine are cheap and suggested below.

The firmware facts in this plan come from the firmware source
(`prusalink-filament-endpoint` branch), read by the firmware session on
2026-11-08, not from hardware, except where a row says "Seen live". Paths
in parentheses are in the firmware repo.

## Status

Workstreams 1 to 5 are built, under `simulator/`, with
[simulator/README.md](../simulator/README.md) as the user guide. The
decisions the build made where this plan was silent are listed in that
README under "Choices made".

Workstream 6 took a different shape from the sketch below: instead of a
scenario file format and snapshots, `integration/` holds pytest tests that
run the bridge, the simulator and a real Spoolman in lockstep, one scenario
per test, with no wall-clock timing. The simulator gained `POST
/api/advance` for scripts that want the same control from outside. See
[integration/README.md](../integration/README.md).

The "Firmware behavior to copy" table below was the first list of what the
simulator copies. [printer-behaviors.md](printer-behaviors.md) has replaced
it as the record: one row per behavior, with its source, its trace from the
printer, and the replay test that checks the simulator against it. The table
here is kept as the plan it was.

## Goal and non-goals

The goal is to reproduce every printer-side event in
[scenarios.md](scenarios.md) on demand, for any number of printers and any
number of tools per printer, against a real Spoolman, so that:

- The X scenarios (several printers) run end to end instead of only as unit
  tests with fake printers.
- The multi-tool work (the per-tool write job, INDX and XL accounting) can
  be developed and exercised before any multi-tool printer exists.
- Live-check sessions can be rehearsed before they cost filament.

Not in scope:

- Anything PrusaLink offers that the bridge doesn't call. No file browser,
  camera, thumbnails, transfers, job control endpoints, or the PrusaLink web
  UI.
- Simulating Spoolman. The real Spoolman container (`ghcr.io/donkie/spoolman`
  on port 7912, see the README) is the other half of every test. A seed
  script that creates spools and locations for the simulated tools is in
  scope.
- Printing real sliced files. A simulated print is described by numbers
  (filament length, duration, color changes, tool sequence), not by G-code.
  The sync job the bridge uploads is the only G-code the simulator parses.
- Physical realism beyond what the bridge can observe: no temperatures,
  motion, or bed leveling. Only counters, metadata, states and job IDs.

## What the bridge sees

The bridge talks to a printer through `src/spoolman_prusalink/prusalink.py`
and nothing else. The simulator has to get these right, including the
failure shapes the bridge handles.

| Endpoint | What the bridge uses | What the firmware does | Failure shapes the bridge handles |
| --- | --- | --- | --- |
| `GET /api/v1/status` | `printer.state`, `job.id`. Idle states for a write are `IDLE`, `FINISHED`, `READY`, `STOPPED`; busy are `PRINTING`, `PAUSED`, `BUSY`, `ATTENTION`. The state also sets the poll interval. | Emits `IDLE`, `BUSY`, `PRINTING`, `PAUSED`, `FINISHED`, `STOPPED`, `ATTENTION`. Never `READY`. `job` is present only while a job runs: it disappears the moment a print finishes or stops. | Connection refused or timeout (printer off, C2). Non-2xx (C9). |
| `GET /api/v1/filament` | `boot_id`, and per tool `index`, `extruded`, `unload_count`, `extruded_at_unload`, `material{name,color,instance_uuid}`, or no `material` when empty. | One entry per enabled tool, in index order, with gaps for disabled tools. UUIDs only on Core One and Core One L, and only on tool 0. | 404 (stock firmware, X10). 200 without `boot_id` or `extruded` (MINI, MK3.5). Both read as `NoFilamentEndpoint`. |
| `GET /api/version` | `printer`, the type code, read once per boot for the model check (W10). | `type.version.subversion`, such as `8.1.0`. | Non-2xx: the model check is retried next boot. |
| `GET /api/v1/job` | `file.display_name` or `file.name`, read once per new job ID, to recognize the sync job (W6). 204 when no job. | 204 whenever there's no job, including right after `FINISHED` or `STOPPED`. `state` is `PRINTING`, or `PAUSED` for both paused and attention. | None special. |
| `PUT /api/v1/files/{storage}/{name}` | Headers `Print-After-Upload: ?1`, `Overwrite: ?1`, text body. Starts the sync job. | 201 with the file info, no job ID. 409 when the file exists without `Overwrite`, when the file is being printed, or when the state isn't `IDLE`, `READY`, `STOPPED` or `FINISHED` ("Can't print right now"; the file stays). | 409 (W3). Other 4xx/5xx logged once. |
| `X-Api-Key` header | Sent on every request. | 401 without it. | 401 is C9. |

What the bridge infers from these, which the model below must make true:

- A reboot is a new `boot_id`. Counters restart at 0; metadata and the job
  counter survive (C1, P12).
- An unload is `unload_count` going up, with `extruded_at_unload` saying
  where the counter stood. Several unloads between two polls show as one
  jump (S14).
- A print starts at the first poll where `extruded` grows under a new user
  job ID (Q4). Job IDs are a persisted counter, incremented at each print
  start, never reused, and the sync job gets one too.
- The sync job is recognized only by its file name in `/api/v1/job`, so
  the job must be visible there while it runs.
- A tool with no `material` holds usage (C7).

## The physical model

One `Simulation` holds a list of printers. Each printer has:

- `name`, `port`, `api_key`, `model`: a type code from `PRINTER_MODELS` in
  `printer_sync.py`. The model decides the `M862.3` name, whether UUIDs are
  stored (Core One and Core One L only), the retraction gap (27 mm on INDX,
  40 mm on the Nextruder and XL), and the tool limit (8 on INDX, 5 on XL, 1
  elsewhere).
- `firmware`: `full` (counters and unload fields), `no_counters` (MINI and
  MK3.5: material only), `no_endpoint` (404). Switchable at run time, so a
  "firmware update" can turn counters on.
- `power`: on, off (the listener is closed, so connections are refused), or
  `hung` (connections accepted, no reply, for timeouts).
- `fault`: none, `bad_key` (401 on every request), `error` (500), switchable
  at run time (C9).
- `boot_id`, `state`, `job` (ID, file name, display name, progress), the
  `M117` message last shown, the selected tool, and the job-ID counter, which
  persists across reboots like the firmware's config store.
- `tools`: one `SimTool` per index. A tool can be disabled, in which case it
  has no entry in `/api/v1/filament`, which gives the index gaps a real XL
  or INDX can show.

Each tool has:

- `extruded` (mm, integer in the API, float inside), `unload_count`,
  `extruded_at_unload`. All reset to 0 at boot.
- `material`: what the API reports, or none. Name, color, instance UUID.
- `previous`: the metadata kept in the ad-hoc slot after an unload. The
  firmware clears nothing on unload; it only stops reporting the type. A
  runout or `M600` reload restores it, a menu load replaces it.
- `loaded`: whether filament is physically present, separate from
  `material`, so a sensor fault can report no material with filament in.
- `gap`: retracted filament, 0 up to the model's clamp. Retraction grows it
  without changing `extruded`; the next feed fills it before counting.
- The temperatures the last `M865` set. The nozzle, preheat and bed
  temperatures belong to the material and are reported by the filament
  endpoint; the chamber, filtration and flexible values are shown on the
  page only.

`extruded` is the commanded E times a feed ratio, about 0.95 on a stock
Nextruder, which is why the printer runs about 5% under slicer estimates.
The simulator takes print lengths as slicer millimeters and applies
`feed_ratio` (default 0.95) so the `length_factor` setting can be exercised.

Time runs in real seconds scaled by `time_scale`, so a 300 g print can take
30 seconds. Extrusion during a print advances continuously, so a poll in
the middle sees a partial value, as on a real printer.

### Firmware behavior to copy

| Behavior | What the simulator does | Source |
| --- | --- | --- |
| Reboot | New random 32-bit `boot_id`, every tool's three counters to 0, `material` and `previous` kept, job counter kept, state `IDLE`. | `filament_tracker.cpp:11`; seen live (C1) |
| Unload | `unload_count += 1`, `extruded_at_unload = extruded`, `material` moves to `previous`, `loaded = false`. Covers the menu, `M702`, `M600`, runout, stuck-filament recovery, pulling filament out during a load, and the idle sensor clear below. | `store_definition.cpp:258`, `pause.cpp:1278`; seen live (S1, S2) |
| Load | Three phases. Feed: the first `gap` mm fill the gap and don't count, the rest counts with no `material` reported. Then the material is set. Then the purge counts under the new material. A menu load on a Core One L measured 116 mm in total. | `pause.cpp:797`; seen live (S2, the held load usage) |
| Load from the menu, with a preset | `material` becomes the preset's name with no color and no UUID; `previous` is replaced. "Ask for color" gives the preset's name plus the chosen color and clears the UUID. A plain load, `M701`, and autoload all go through this menu. Any preset, including PPS and PPA (W8). | Follow-up A; README (W8) |
| Load from a tag (ANFC) | Sets name, color and UUID. Core One and Core One L only. | Follow-up A; seen live (S1) |
| Runout, `M600`, jam | During a print: state `ATTENTION` while the dialog waits, `PAUSED` once handled until resumed, then `PRINTING`. The unload counts as above. The reload restores `previous` as `material`, so the printer keeps showing the old spool. A jam adds an extra purge after the reload. A jam cleared without unloading counts nothing. | `printer_state.cpp:238`, `M600.cpp:282`; seen live (P2, P6) |
| Print start | Job ID incremented at once. `PRINTING` during the file scan, a brief `IDLE` with no `job` object, then `PRINTING`. A configurable preheat delay before the first extrusion, so P8 is possible. | `marlin_server.cpp:2301`, `printer_state.cpp:379` |
| Pause and resume | `PAUSED` then `PRINTING`; the counter holds, then continues. | P11 |
| Cancel | `PRINTING` while parking, then `STOPPED`. `job` disappears. | Seen live (P7) |
| Finish | `PRINTING` while parking, then `FINISHED`. `job` disappears. `FINISHED` and `STOPPED` persist with no timeout until dismissed on the screen, and a new print can start without dismissing. | Answer 5; seen live |
| Sync job | 409 unless the state allows a print. Assigns a job ID, shows `PRINTING` (with the start blip) for the scan and park time, a few seconds, with `display_name` equal to the file name, applies the G-code, then `FINISHED`. No extrusion. | Answers 5 to 7; seen live (W1, W6) |
| Model check | An `M862.3` naming another model (not this one or its MMU3 variant) stops the job in `ATTENTION` until tapped through on the page, or at once with the auto-accept toggle. | README (W10) |
| Power-panic resume | Reboot as above, then the same job ID continues in `PRINTING`. | Decided (P12); the kept ID is from source |
| Manual extrude and retract | Retract grows `gap` up to the clamp and changes nothing else. Extrude fills `gap` first, then counts. | Answer 2; C6 |
| Idle sensor clear | Outside a print, selecting a tool whose sensor sees no filament while it has a `material` counts an unload. Multi-tool only in practice. | `filament_sensors_handler.cpp:306` |

### Multi-tool

INDX and XL are both several toolheads, not several feeds into one nozzle.
On INDX one carriage picks one of up to eight docked nozzle tools, each with
its own filament path and sensor. An API tool is one docked tool, enabled
when its dock is calibrated. The XL has up to five heads, enabled per dwarf.

- Every tool has its own counters and metadata. Only the selected tool's
  `extruded` grows. A simulated print lists segments of (tool, slicer mm).
- A tool change is not an unload, and the state stays `PRINTING` through it.
  Slicer ramming or retract before `T<n>` counts on the old tool (retract
  only grows its gap); the purge after pickup counts on the new tool. The
  change itself moves no filament. The simulator applies a `toolchange_purge_mm`
  to the incoming tool at each change.
- No UUIDs. INDX and XL builds have no ANFC, so `material` is `{name,
  color?}` and `M865.1` is a no-op for UUIDs. The UUID correction (S12) and
  UUID drift can't happen there, and the per-tool write job can't carry a
  UUID. The simulator enforces this per model.
- `M865 ... L<n>` sets tool `n`'s type with no motion or tool change.
  `I<n>` selects which tool's filament to read or modify. `M865 I<empty tool>
  L<n>` copies "none" and counts as an unload of `n`.
- Type codes and model names, already in `PRINTER_MODELS`: XL `3.1.0` and
  `5.1.0` as `XL`, XL+ `3.1.1` as `XLP` (not in the bridge's table yet),
  Core One INDX `7.10.0` as `COREONEINDX`, Core One L INDX `8.10.0` as
  `COREONEL-INDX`.
- Nothing else in PrusaLink says how many tools there are or which is
  selected. `/api/v1/filament` is the only multi-tool signal.

## Actions

Every action is a `POST` on the simulator's own API, and the page is a thin
client over it, so a shell script or a test can drive the same actions.
Actions work while the printer is powered off: they change the physical
model, and the API shows the result when power returns. That's how to make
the bridge miss an unload (Q2, X6).

| Action | Parameters | Effect | Scenarios |
| --- | --- | --- | --- |
| Load | tool; source: tag (name, color, UUID) or preset (name, optional color); feed and purge mm | The three-phase load | S1 to S14, W8 |
| Unload | tool | Counts an unload, keeps `previous` | Swaps, C7 |
| Extrude / retract | tool, mm | Gap-aware manual moves | C6 |
| Select tool | tool | Multi-tool; may trigger the idle sensor clear | Multi-tool |
| Start print | segments of (tool, slicer mm); duration; preheat seconds; color changes (`M600`) at given mm | New job ID, start blip, extrusion at rate | P1 to P11, Q4 |
| Pause / resume / cancel | | State changes | P7, P8, P11 |
| Dismiss | | `FINISHED` or `STOPPED` to `IDLE` | W2, poll interval |
| Runout now | tool | `ATTENTION`, unload, waits for a load, then `PAUSED` | P2 to P4, M5a |
| Jam now | tool | As runout, plus extra purge on reload; or clear without unloading | P6 |
| Reboot | | New boot ID | C1, C3, C8 |
| Power off / on / hang | | Listener closed, open, or silent | C2, X8 |
| Power loss and resume | | Reboot that keeps the job | P12 |
| Fault | none, bad key, 500 | | C9 |
| Firmware | full, no counters, no endpoint | | X10, no-`unload_count` paths |
| Sensor | report no material while loaded, and back | `material` omitted | C7 |
| Counter anomaly | step `extruded` back; set `extruded_at_unload` out of range | | C4, C5 |
| Tap through | | Clears a model-check `ATTENTION` | W10 |
| Stuck write | tool, on or off | The tool ignores `M865`, so a write doesn't take | W5 |
| Enable / disable tool | tool | Index gaps in `/api/v1/filament` | Multi-tool |
| Time scale | factor | | |
| Snapshot / restore | name | Saves and loads the whole model as JSON | Repeatable scenarios |

## The sync job parser

The bridge's job is plain text. The simulator parses line by line and
ignores what it doesn't know, logging each ignored line, so a new G-code the
bridge starts sending is visible at once. Lines it honors:

- `M862.3 P"<model>"`: the model check. `M862.5` and `M862.6` are accepted.
- `G28 O`: no effect; the page notes "homed".
- `M865 X R N"<name>" T<n> B<n> P<n> C<n> D<n> E<n> F1 G1 O"#rrggbb" L<tool>`:
  writes tool `L`'s ad-hoc slot: name (1 to 7 characters from
  `a-zA-Z0-9_-`, else the job fails), color, and temperatures: the
  filament endpoint reports nozzle, preheat and bed, and the rest are for
  display. `R`
  clears everything first, including the UUID, as the firmware does.
  Parameters on a preset type are ignored silently, as in the firmware; the
  bridge always writes with `X`, so this only matters for hand-written jobs.
- `M865.1 I<tool> C"<uuid>"`: sets the UUID on tool 0 of a model with UUIDs.
  Dropped elsewhere.
- `M117 <text>`: stored as the last message and shown on the page.

A parse error fails the job in `ATTENTION` with the offending line.

## Architecture

- **Language and dependencies.** Python 3.11+, like the bridge, with
  `aiohttp` as the one dependency: it serves the PrusaLink clones, the page,
  and server-sent events from one asyncio loop. It's an optional extra,
  `sim`, in `pyproject.toml`, so `pip install -e '.[sim]'` gets it and the
  image is unchanged (the `Dockerfile` copies only `src`). Ruff runs over
  the whole repo in CI, so the simulator follows the same style. Pytest's
  `testpaths` stays `tests`; the simulator's tests live in `simulator/tests`
  and run on demand with `pytest simulator/tests`.
- **Layout.**
  ```
  simulator/
    __main__.py      # python -m simulator -c simulator/sim.example.toml
    config.py        # the TOML: printers, tools, ports, time scale
    model.py         # Simulation, SimPrinter, SimTool, the state machine
    prusalink.py     # the PrusaLink clone, one aiohttp app per printer
    gcode.py         # the sync-job parser
    control.py       # the simulator's own API and SSE stream
    static/index.html, app.js, app.css   # the page, no build step
    seed.py          # creates spools and locations in Spoolman
    sim.example.toml
    tests/
  ```
- **Ports.** The page and control API on 8000; printers on 8001, 8002, ...
  in config order, or an explicit `port` per printer. Everything binds to
  `127.0.0.1` unless `--host` says otherwise, for a bridge running in a
  container with `--network host`.
- **Config.** One TOML file. Per printer: `name`, `model`, `api_key`,
  `firmware`, and `[[tools]]` with an optional initial material, counters
  and `enabled`, so a run can start with filament loaded and usage on the
  clock (B1). Global: `time_scale`, `print_rate_mm_s`, `feed_ratio`,
  `sync_job_seconds`, `preheat_seconds`, `load_feed_mm`, `purge_mm`,
  `toolchange_purge_mm`, `start_blip_seconds`, `auto_accept_model_check`,
  `host`.
- **Bridge config output.** `python -m simulator bridge-config` prints a
  `[[printers]]` block for the bridge with every URL and key, and one
  location per tool named `<printer> T<n+1>` (or `<printer>` for one tool),
  matching what `config.example.toml` recommends. The seed script uses the
  same names, so the three agree without hand editing.
- **The page.** One card per printer: state, job, boot ID, last message,
  selected tool, and a row per tool with counters, material and the gap.
  Buttons per the actions table, with small forms for the ones that take
  parameters. Below the cards, an event log: every action, every state
  change, and every request each printer received with its path, status
  and, for uploads, the full G-code body. That log shows what the bridge is
  doing without reading two sets of logs. Vanilla JS, SSE for updates,
  nothing to build.
- **Snapshots.** `POST /api/snapshot/{name}` and `/api/restore/{name}`
  write JSON under a `--data` directory. Not needed on day one, but it's
  what makes a scripted scenario repeatable.
- **Seeding Spoolman.** `python -m simulator seed --spoolman URL` creates a
  vendor, a few filaments (PLA, PETG, ASA, a non-preset material with and
  one without temperatures, PPS), and two spools per tool, one in the
  tool's location and one spare, with `instance_uuid` set on some. It's
  idempotent on spool names and never deletes.

## Scenario coverage

What each group of scenarios needs from the simulator, to check the build
against.

| Scenarios | Needs |
| --- | --- |
| S1 to S14 | Load (tag and preset), unload, reboot while the bridge is stopped, two swaps between polls |
| P1 to P12 | Print with preheat, pause, resume, cancel, runout and jam mid-print, `M600` at a given mm, power-loss resume with the same job ID |
| C1 to C9 | Reboot, power off and on for hours (time scale), counter step back, `extruded_at_unload` out of range, sensor fault, firmware switch, bad key and 500 |
| W1 to W14 | Upload accepted when idle, 409 when not, the job visible with its file name, `M865`, `M865.1` and `M117` applied, a wrong model stopping in `ATTENTION`, the stuck-write toggle |
| M1 to M7 | Nothing from the simulator; stop or break Spoolman |
| B1 to B6 | Initial counters in config, restart the bridge mid-print |
| X1 to X16 | Two or more printers, the actions above on each, power off one |
| Multi-tool | Tools per printer, per-tool actions, prints with a tool sequence, `M865 L<n>`, disabled tools, no UUIDs on INDX and XL |

The live-check sessions in `live-checks.md` are a good first script: each
step maps to one or two actions.

## Workstreams

1. **Model and PrusaLink clone.** `model.py`, `prusalink.py`, `config.py`,
   `__main__.py`, one printer and one tool. Acceptance: the bridge's `once`
   command against it in dry run logs a baseline; a load, unload and reboot
   driven with `curl` on the control API appear in the bridge's log as the
   right events.
2. **The page.** Cards, buttons and the event log over SSE. Acceptance: S1
   and S2 run from the page against the real Spoolman and the bridge, and
   both sides show what `scenarios.md` says.
3. **Prints and faults.** Start, pause, resume, cancel, dismiss, runout,
   jam, `M600`, power loss, power off, hang, faults, firmware modes, counter
   anomalies, the model check, the stuck write. Acceptance: sessions 1 to 4
   of `live-checks.md` run from the page with no printer.
4. **Several printers.** Ports, bridge-config output, the seed script, per
   printer power. Acceptance: X1, X2, X4, X8 and X10 run from the page.
5. **Multi-tool.** Per-tool model, tool segments in prints, tool-change
   purge, idle sensor clear, disabled tools, `M865 L<n>`, no UUIDs on INDX
   and XL. Acceptance: the bridge's current behavior (charge per tool,
   refuse to write) shows on an eight-tool INDX and a five-tool XL. This is
   the test bed for the multi-tool work, which gets its own plan.
6. **Optional: scripted scenarios.** A `scenarios/*.toml` of timed actions
   and a runner, plus snapshots, so a scenario is one command. With that, an
   end-to-end suite over the simulator, a throwaway Spoolman container and
   the bridge becomes possible, which is where "Tested" in `scenarios.md`
   can mean more than unit tests.

## Decisions

- **Poll intervals, not time scale, make polls frequent.** The bridge's
  intervals accept any positive number, so test runs set `poll_printing = 2`
  and `poll_idle = 3`. The time scale only shortens prints and outages.
- **The start blip is on by default.** A real print start shows `IDLE` with
  no job for a moment. The bridge should survive a poll landing there, so
  the simulator reproduces it, with a config switch to turn it off when it
  gets in the way.
- **The model check stops by default.** Faithful, with an auto-accept
  toggle in config and on the page for when it's not what's being tested.
- **Counters report the floor** of a float, so a slow print still shows the
  counter moving between polls.
- **Load lengths are configurable, not fixed.** The measured 116 mm is the
  default total for a Core One L menu load, split as 40 mm of gap, 36 mm of
  counted feed and 40 mm of purge, but each part is a setting, because the
  high-flow nozzle doubles the purge and other models differ.

## Open points for the bridge, found while planning

These aren't simulator work, but the simulator makes each one testable.

- `XLP` (`3.1.1`) is missing from `PRINTER_MODELS`.
- The start blip: a poll that lands on the brief `IDLE` with no `job` during
  a print start sees an idle printer with no job and could run a sync job
  into the start of a user print. The upload would fail with 409 once the
  print is under way, but if it lands first, the user's print is refused.
  Worth a scenario once the simulator can produce the blip.
- `FINISHED` persists, so a printer that finished a print days ago still
  reads as idle, which is what the bridge wants. But `/api/v1/job` returns
  204 then, and the bridge's job-name cache keeps the last ID; nothing
  depends on it today.
- On INDX and XL there are no UUIDs, so S12 and UUID drift can't happen, and
  the per-tool write has name, color and temperatures only. Drift and sync
  comparisons need a per-model "has UUIDs" rule, which `PrinterModel.uuids`
  already is.
- To investigate before the per-tool write: some firmware presets differ by
  model. On INDX the PC preheat is 230 and the FLEX preheat 170
  (`src/common/filament_presets.cpp` in the firmware), while
  `printer_sync.PRESETS` holds the single-tool values, 250 and 210. The
  bridge would write the wrong preheat, and a menu load of those presets
  that otherwise matches would still get a sync job. With printer sync off,
  every such menu load reports preheat drift. Check every preset value per
  model, and the simulator's `PRESET_TEMPERATURES`, which has the same
  values.

## Running it

```
pip install -e '.[sim]'
python -m simulator -c simulator/sim.example.toml
python -m simulator seed --spoolman http://127.0.0.1:7912
python -m simulator bridge-config > /tmp/bridge.toml   # then set spoolman.url, dry_run, poll_*
PYTHONPATH=src python -m spoolman_prusalink -c /tmp/bridge.toml run
```

The page is at `http://127.0.0.1:8000`. Spoolman runs as in the README's
compose example.
