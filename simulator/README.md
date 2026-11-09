# Printer simulator

A stand-in for one or more Prusa printers, for developing and testing the
bridge without hardware. It serves the parts of PrusaLink the bridge calls,
one listener per printer, keeps a physical model of each tool's counters and
filament, and gives you a page with buttons for what a user or the filament
does: load, unload, print, run out, jam, reboot, switch off. It runs against
a real Spoolman. The plan behind it is [docs/plans.md](../docs/plans.md).

The bridge never imports this package, and the Docker image doesn't ship it.

## Run it

```
pip install -e '.[sim]'
python -m simulator -c simulator/sim.example.toml run
```

The page is at `http://127.0.0.1:8000`. The printers listen on 8001, 8002,
and so on, in config order. Each has its own API key, shown on its card.

To point the bridge at the simulated printers:

```
python -m simulator -c simulator/sim.example.toml seed --spoolman http://127.0.0.1:7912
python -m simulator -c simulator/sim.example.toml bridge-config > bridge.toml
```

`seed` creates a vendor, a few filaments, and two spools per tool in Spoolman:
one in the tool's location and a spare on the `shelf`. It never deletes or
moves anything, and running it again changes nothing. `bridge-config` prints
the bridge's `[[printers]]` list with the same locations. Add `[spoolman]`
and `[bridge]` tables to it, with short poll intervals, and run the bridge:

```toml
[spoolman]
url = "http://127.0.0.1:7912"

[bridge]
db_file = "/tmp/sim-bridge.db"
poll_printing = 2
poll_idle = 3
dry_run = false
```

```
PYTHONPATH=src python -m spoolman_prusalink -c bridge.toml run
```

## Config

See [sim.example.toml](sim.example.toml), which shows every setting with its
default. A printer has a `model` (a PrusaLink type code such as `8.1.0`), an
`api_key`, a `firmware` mode, and `[[printers.tools]]` entries that can start
loaded with a material and counters. The top-level settings are timing and
lengths: the time scale, the feed ratio, how fast loads and prints move
filament, the gap, feed and purge lengths of a load, the preheat and park
times, and whether the start blip and the model check are on.

## The page and the control API

Every button posts to `POST /api/printers/<name>/<action>` with a JSON body,
so a script can do the same:

```
curl -X POST -H 'Content-Type: application/json' \
  -d '{"tool": 0, "source": "preset", "name": "PETG"}' \
  http://127.0.0.1:8000/api/printers/core-one-l/load
```

Actions: `load`, `unload`, `extrude`, `retract`, `select_tool`,
`start_print`, `pause`, `resume`, `cancel`, `dismiss`, `runout`, `jam`,
`clear_jam`, `reboot`, `set_power`, `set_fault`, `set_firmware`,
`set_sensor_fault`, `set_stuck_write`, `set_tool_enabled`,
`set_auto_accept`, `counter_step_back`, `set_extruded_at_unload`,
`tap_through`. Parameter names are the keyword arguments of the methods with
those names in `model.py`. An action the printer can't do now answers 409
with the reason; bad parameters answer 400.

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
- A load counts in three phases: the retraction gap is refilled uncounted,
  then feed counts with no material reported, then the material is set and
  the purge counts under it.
- `extruded` is the slicer length times the feed ratio, 0.95 by default.
  Retracts grow a gap, and the next feed fills it before counting.
- Every print start shows `PRINTING`, then a brief `IDLE` with no job, then
  `PRINTING`. `FINISHED` and `STOPPED` persist until dismissed, and the job
  disappears from `/api/v1/status` and `/api/v1/job` as soon as a print ends.
- The sync job is accepted only when a print could start, runs as a job
  with the uploaded file's name, applies `M865`, `M865.1` and `M117`, and
  stops in `ATTENTION` on an `M862.3` for another model until you tap
  through.
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
  it waits in `ATTENTION` for `resume` before the clean.
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
  more.

## Tests

```
pytest simulator/tests
```

They're part of the default `pytest` run, with the bridge's own suite on `tests/`.

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
