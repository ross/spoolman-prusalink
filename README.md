# spoolman-prusalink

A sidecar service that charges filament usage from a Prusa printer to the
spools [Spoolman](https://github.com/Donkie/Spoolman) assigns to its tools.
It reads the per-tool `extruded` counter from PrusaLink's
`GET /api/v1/filament` endpoint and maps each tool to a Spoolman location.

The bridge starts in dry-run mode, which logs and records what it would do
but writes nothing. With `dry_run = false`, it charges usage to Spoolman,
writes flags such as an unconfirmed swap to a text extra field on the spool,
moves the old spool out of the tool's location when a swap takes effect, and
writes the loaded spool's name, color and UUID to the printer.

## Requirements

- A printer running firmware with `GET /api/v1/filament`, which is on the
  [`prusalink-filament-endpoint` branch](https://github.com/ross/Prusa-Firmware-Buddy/tree/prusalink-filament-endpoint).
  Stock firmware doesn't have it.
- The printer's PrusaLink API key.
- Spoolman, reachable from the bridge over HTTP.

## Run it

1. Copy the example config and edit the URLs and tool locations:

   ```sh
   cp config.example.toml config.toml
   ```

   The location must match the spool's Spoolman location exactly, including
   case. The bridge doesn't use Spoolman's location filter, because that
   filter matches partial text.

1. Build and run the container:

   ```sh
   docker build -t spoolman-prusalink .
   docker run --rm -e PRUSALINK_API_KEY=API_KEY \
     -v "$PWD/config.toml:/config/config.toml:ro" \
     -v "$PWD/bridge-data:/data" \
     spoolman-prusalink
   ```

   Replace `API_KEY` with the printer's key. `compose.example.yaml` runs the
   bridge next to Spoolman.

1. To verify, look for a `baseline` line for each tool in the log. After the
   printer extrudes, look for lines like this one (`CHARGE` instead of
   `WOULD CHARGE` when `dry_run = false`):

   ```text
   [Core One L+] WOULD CHARGE spool 7 (Prusament Galaxy Black): +1000 mm ≈ +2.98 g (extruded)
   ```

To poll once and exit, for example while testing the config, run the command
with `once` in place of the default `run`.

### Run from source

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
PRUSALINK_API_KEY=API_KEY .venv/bin/spoolman-prusalink -c config.toml once
.venv/bin/pytest
```

## How usage is attributed

Usage is charged to the spool physically in the tool. Spoolman decides which
spool that is, and the printer's unload signal decides when a change takes
effect.

**Change Spoolman first, then swap the filament.** Scan or move the new spool
into the tool's location while the old filament is still loaded. The bridge
stages the new spool and keeps charging the old one. When the printer reports
an unload, usage up to the unload goes to the old spool, and everything after
it, including the load and purge, goes to the new one. The old spool can stay
in the location until then. The bridge ignores it after the unload and logs
that it should be moved out.

If you swap the filament first, the bridge flags an unconfirmed swap and keeps
charging the assigned spool. Move the new spool into the location before the
next print starts, and the usage since the unload moves to it. If a print
starts first, the bridge assumes you reloaded the same spool, for example
after clearing a jam, and records that assumption in the ledger.

Other rules:

- The first poll of each tool records the current counter as a baseline.
  Usage from before the bridge started isn't charged.
- When `boot_id` changes, the printer has rebooted and its counters restart
  at 0. Usage between the last poll and the reboot is lost, so a shorter poll
  interval loses less.
- While the tool reports no filament, or no spool is active, usage is held
  and charged once both are present.
- If the location holds two spools and neither is the loaded one, or more
  than two, the bridge skips the tool and logs a warning until it clears.
- A Spoolman change with no unload switches immediately only if the printer
  already reports the new spool's instance UUID, which fixes a wrong
  assignment. Name and color aren't enough, because two spools of the same
  product look the same.
- If a print starts while a swap is staged, the bridge warns and keeps
  charging the old spool.
- Firmware without `unload_count` can't stage swaps. A Spoolman change then
  switches at the next poll, after charging usage so far to the outgoing
  spool.

The bridge also logs when the printer's filament name, color, or instance UUID
differs from the loaded spool. A menu load replaces the color and UUID with
the preset's, so expect this after every menu load. Colors are compared after
the printer's rounding to 5 bits per channel. The UUID is read from the spool
extra field named by `uuid_field`, `instance_uuid` by default, which must be a
text field.

## Charging and flags

With `dry_run = false`, each charge is saved to `state.json` before it's sent
to Spoolman, in the same write as the counters it came from, and removed once
Spoolman accepts it. If Spoolman is unreachable or returns a server error,
charges stay queued and are retried in order on the next poll, including
after a restart. If Spoolman rejects a charge with a client error, for
example because the spool was deleted, the bridge logs an error, records it
in the ledger with `failed`, and drops it.

A charge that Spoolman applied but whose response was lost is sent again, so
it can be counted twice. Every other failure delays charges rather than
losing or repeating them.

Flags go to the `flag_field` extra field (`bridge_flag` by default), which
the bridge creates on first use:

- An unconfirmed swap, on the spool still assigned, until Spoolman changes,
  a print starts, or the next unload.
- A swap assumed at print start, or a print started with a swap staged, until
  the next print start or unload.
- With `flag_drift = true`, a mismatch between the printer's filament and the
  loaded spool. A menu load always causes one, because it drops the color and
  UUID.

The bridge only changes the field on spools it flagged itself, and doesn't
rewrite a flag that hasn't changed.

## Writing to the printer

PrusaLink has no write endpoint for filament, so the bridge uploads a small
G-code job, `spoolman-sync.gcode` on the USB drive, and runs it as a print.
The printer only accepts it while idle or finished, and each run appears in
the printer's job history. The job starts with the printer's compatibility
checks (`gcode_preamble`), sets the filament with `M865` without moving any,
sets the instance UUID with `M865.1` on tool 0, and ends with an `M117`
message naming the spool.

The bridge writes a tool once the printer is idle, after a swap takes effect
or the tool unloads. A menu load replaces the filament with a preset, which
drops the color and UUID, so a write is expected then. It waits while a swap
is unconfirmed or staged, and while the tool is empty. If the printer still
differs after two jobs, it stops and flags the spool.

A mismatch with no swap or unload behind it is drift. The bridge flags it and
leaves the printer alone, unless `sync_drift = true`.

Requirements for a write:

- The spool's material must be a printer preset (PLA, PETG, ASA, PC, PVB,
  ABS, HIPS, PP, FLEX, PA, PPS, PPA, PVA, BVOH; TPU and TPE map to FLEX), or
  its filament must have extruder and bed temperatures in Spoolman. The job
  resets the filament's parameters, so it has to supply temperatures.
  Otherwise the bridge flags the spool and doesn't write.
- The printer's name is the material, cut to the 7 characters from
  `a-zA-Z0-9_-` the firmware accepts, so `PLA+` is written as `PLA`.
- With `uuid_field` set and `uuids = true`, a spool with no instance UUID gets
  a new one in Spoolman before it's written.

The bridge's own job doesn't count as a print start: it's recognized by
`sync_file`.

## Files

The `/data` volume holds two files:

- `state.json` stores each tool's counters and swap state, the queue of
  charges not yet accepted by Spoolman, and the spools the bridge has
  flagged. Keep it across restarts: without it, the bridge takes a new
  baseline, drops queued charges, and loses track of open swaps. Run only one
  bridge per state file and printer.
- `ledger.jsonl` stores one JSON object per line for every charge, and every
  event, such as a reboot or swap. A charge has `dry_run`, and when it was
  sent, `applied` (the time Spoolman accepted it) or `failed` (the HTTP
  status that rejected it). Compare it against
  the slicer's `filament used [mm]` to validate the counts.
