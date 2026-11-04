# spoolman-prusalink

A sidecar service that charges filament usage from a Prusa printer to the
spools [Spoolman](https://github.com/Donkie/Spoolman) assigns to its tools.
It reads the per-tool `extruded` counter from PrusaLink's
`GET /api/v1/filament` endpoint and maps each tool to a Spoolman location.

This version is phase 1, which is read-only. It logs and records the usage it
would charge, but writes nothing to Spoolman or the printer. A config with
`dry_run = false` is rejected.

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
   printer extrudes, look for lines like this one:

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

## Files

The `/data` volume holds two files:

- `state.json` stores each tool's counter baseline. Keep it across restarts:
  without it, the bridge takes a new baseline and drops the usage since the
  last poll.
- `ledger.jsonl` stores one JSON object per line for every charge the bridge
  would make, and every event, such as a reboot or swap. Compare it against
  the slicer's `filament used [mm]` to validate the counts.
