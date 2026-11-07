# spoolman-prusalink

A sidecar service that charges filament usage from a Prusa printer to the
spools [Spoolman](https://github.com/Donkie/Spoolman) assigns to its tools.
It reads the per-tool `extruded` counter from PrusaLink's
`GET /api/v1/filament` endpoint and maps each tool to a Spoolman location.

The bridge starts in dry-run mode, which logs what it would do and records
what it would charge in the ledger, but changes nothing in Spoolman, on the
printer or in `state.json`. With `dry_run = false`, it charges usage to
Spoolman, writes flags such as an unconfirmed swap to a text extra field on
the spool, moves the old spool out of the tool's location when a swap takes
effect, and writes the loaded spool's name, color and UUID to the printer.

Dry run is like a stopped bridge that shows what it would do. When it next
runs live, the bridge starts from the state its last live run saved and
charges everything printed since then, including during the dry run, as it
would after an outage. Turning dry run on and off loses no usage, except
that, as after an outage, a printer restart in between drops the usage
before the restart.

## Requirements

- A Prusa MK4, MK4S, MK3.9, MK3.9S, Core One or Core One L running firmware
  with `GET /api/v1/filament`, which is on the
  [`prusalink-filament-endpoint` branch](https://github.com/ross/Prusa-Firmware-Buddy/tree/prusalink-filament-endpoint).
  Stock firmware doesn't have the endpoint. Only the Core One L has been run
  with the bridge.

  | Model | Charging | Printer writes | Instance UUIDs |
  | --- | --- | --- | --- |
  | Core One, Core One L | Yes | Yes | Yes |
  | MK4, MK4S, MK3.9, MK3.9S | Yes | Yes | No: the firmware drops them |
  | MINI, MK3.5, MK3.5S | No: the endpoint has no usage counters | No | No |
  | XL, iX, Core One (L) INDX | Untested | No: multi-tool isn't supported yet | No |

  On firmware without the endpoint or its counters, the bridge logs an error
  once and charges nothing, then resumes on its own if they appear.
- The printer's PrusaLink API key.
- Spoolman, reachable from the bridge over HTTP.

## Run it

The image needs only environment variables for a single-tool printer:

```sh
docker build -t spoolman-prusalink .
docker run -d --name spoolman-prusalink --restart unless-stopped \
  -e PRUSALINK_URL=http://PRINTER_HOST \
  -e PRUSALINK_API_KEY=API_KEY \
  -e SPOOLMAN_URL=http://SPOOLMAN_HOST:7912 \
  -e "SPOOLMAN_PRUSALINK_LOCATION=Core One L+" \
  -e SPOOLMAN_PRUSALINK_DRY_RUN=false \
  -v spoolman-prusalink-data:/data \
  spoolman-prusalink
```

Replace `PRINTER_HOST`, `API_KEY` and `SPOOLMAN_HOST`. The location must match
the spool's Spoolman location exactly, including case. The bridge doesn't use
Spoolman's location filter, because that filter matches partial text.

| Variable | Meaning |
|---|---|
| `PRUSALINK_URL` | The printer's PrusaLink URL. |
| `PRUSALINK_API_KEY` | The printer's PrusaLink API key. |
| `SPOOLMAN_URL` | Spoolman's base URL. |
| `SPOOLMAN_PRUSALINK_LOCATION` | The Spoolman location that stands for the printer's single tool. |
| `SPOOLMAN_PRUSALINK_DRY_RUN` | `true` (the default) logs what the bridge would do and changes nothing. |
| `SPOOLMAN_PRUSALINK_CONFIG` | Optional path to a TOML config file. `/config/config.toml` is read if it exists. |
| `LOG_LEVEL` | `INFO` by default. `DEBUG` also logs every HTTP request. |

Keep `/data` on a persistent volume: it holds the state file, which the
bridge needs to avoid dropping or repeating charges across restarts. The
container runs as UID 1000, so a bind-mounted directory must be writable by
that user. Run only one bridge per printer: two would charge the same usage
twice.

For multi-tool printers and the other settings, mount a config file based on
`config.example.toml` at `/config/config.toml`. `PRUSALINK_URL`,
`PRUSALINK_API_KEY`, `SPOOLMAN_URL` and `SPOOLMAN_PRUSALINK_DRY_RUN` override
the file. `SPOOLMAN_PRUSALINK_LOCATION` applies only when the file has no
`[[tools]]` entries, because the file's tools take precedence.
`compose.example.yaml` runs the bridge next to Spoolman.

To verify, look for a `baseline` line for each tool in the log. After the
printer extrudes, look for lines like this one (`WOULD CHARGE` in dry-run
mode):

```text
[Core One L+] CHARGE spool 7 (Prusament Galaxy Black): +1000 mm ≈ +2.98 g (extruded)
```

To poll once and exit, for example while testing the config, run the command
with `once` in place of the default `run`.

### Run from source

The example config is set up for the container. After copying it, edit
`config.toml`: point `[printer] url` and `[spoolman] url` at your printer and
Spoolman, and set `state_file` and `ledger_file` to a directory you can write
to, such as `./data`.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config.example.toml config.toml
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
in the location until then. After the unload, the bridge ignores it. With
`dry_run = false`, it also moves the old spool to `eviction_location`, which
is no location by default. In dry-run mode, it only logs that it would move
the spool. Set `eviction_location` in the config file.

If you swap the filament first, the bridge flags an unconfirmed swap and keeps
charging the assigned spool. Move the new spool into the location before the
next print starts, and the usage since the unload moves to it. If a print
starts first, the bridge assumes you reloaded the same spool, for example
after clearing a jam. It charges that spool provisionally and flags it with
the amount. The next unload keeps the usage there. If a different spool was
loaded, claim it (see [Claims](#claims)).

You can also move the old spool out of the location before moving the new
one in. While the location is empty, the bridge holds usage, even during a
print, and the next spool you move in takes everything since the unload.

Other rules:

- The first poll of each tool records the current counter as a baseline.
  Usage from before the bridge started isn't charged.
- When `boot_id` changes, the printer has rebooted and its counters restart
  at 0. Usage between the last poll and the reboot is lost, so a shorter poll
  interval loses less.
- While the tool reports no filament, no spool is active, or the location is
  ambiguous, usage is held. It's charged once the tool reports filament, a
  spool is active, and the location is clear. Usage held when the printer
  reboots is kept, and charged as soon as a spool is active, even before the
  tool reports filament. An unload with no spool active first
  reports it lost.
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

### Claims

A claim tells the bridge which spool is in a tool when it can't know. To
claim a spool, move it into the tool's location, then tick **Claim held
usage** on it in Spoolman. Everything charged to that tool since the later of
its last unload and the printer's last reboot moves to the claimed spool. The
claimed spool becomes active, the old spool is moved out of the location, the
box is cleared, and the spool is written to the printer when it's idle.
Claiming the spool that's already active confirms it, and nothing moves.

- A ticked spool that isn't in a tool's location is flagged and left alone
  until it is.
- If several spools are claimed for one tool between two polls, the one with
  the most recent `last_used` wins, and the other boxes are cleared.
  Spoolman doesn't record when a box was ticked.
- If the location holds two spools and neither is active, the claim waits
  until the location is resolved.
- The bridge flags a staged spool when a print starts, or when the printer
  reboots, while it's staged: the unload can be lost across a reboot. Claim
  the staged spool if it's the one loaded.

## Charging and flags

With `dry_run = false`, each charge is saved to `state.json` before it's sent
to Spoolman, in the same write as the counters it came from, and removed once
Spoolman accepts it. If Spoolman is unreachable or returns a server error,
charges stay queued and are retried in order on the next poll, including
after a restart. If Spoolman rejects a charge with a client error (4xx), the
bridge logs an error, records it in the ledger with `failed`, and drops it.
The exception is a 404 on a charge to the tool's active spool, or to a spool
already found deleted, described next.

If the active spool is deleted in Spoolman, found either from Spoolman's
spool list or from a 404 on a charge, the tool is left with no active spool.
The rejected charge, any usage since an unconfirmed swap's unload, and later
usage are held, and charged to the next spool that becomes active in the
tool, with the reason "held after spool N was deleted". Charges accepted
before the deletion, including a provisional one, stay where they are. An
archived active spool keeps being charged until the next unload.

A charge that Spoolman applied but whose response was lost is sent again, so
it can be counted twice. Every other failure delays charges rather than
losing or repeating them.

Flags go to the `flag_field` extra field (`bridge_flag` by default), which
the bridge creates on first use:

- An unconfirmed swap, on the spool still assigned, until Spoolman changes,
  a print starts, or the next unload. If that spool is moved out of the
  location, the flag says so and lasts until a spool is moved in or the next
  unload.
- A provisional charge, on the spool assumed reloaded at print start, until
  the next unload or a claim. The amount shown updates between prints.
- A print started with a swap staged, on both spools. The active spool's flag
  lasts until the next print start or unload. The staged spool's lasts until
  it stops being staged. A reboot while a spool is staged also flags it.
- A claimed spool that isn't in a tool's location.
- An unload during a print while Spoolman shows more than 5% of the spool's
  net weight left. That usually means an earlier swap charged the wrong
  spool, though a jam or tangle, or a planned color change, looks the same.
  Correcting the spool's weight in Spoolman clears the flag, as does another
  spool becoming active and then this one. Reloading the same spool keeps it. A loaded spool going below 0 g remaining is only logged, once:
  spool weights are inexact.
- Usage held because the active spool was deleted, on the spool staged in
  the location, until it becomes active at the next unload or print start.
- An archived spool that's still loaded, until the next unload.
- With `flag_drift = true`, a mismatch between the printer's filament and the
  loaded spool. A menu load always causes one, because it drops the color and
  UUID.

The bridge only changes the field on spools it flagged itself, and doesn't
rewrite a flag that hasn't changed.

Claims use the `claim_field` extra field (`bridge_claim` by default), a
boolean field the bridge creates on first use and shows as **Claim held
usage**. The bridge clears the box after it handles a claim. Set
`claim_field = ""` to turn claims off. In dry-run mode the bridge handles each
claim once but doesn't clear the box, so the next live run handles it again.

## Writing to the printer

PrusaLink has no write endpoint for filament, so the bridge uploads a small
G-code job, `spoolman-sync.gcode` on the USB drive, and runs it as a print.
The printer only accepts it while idle or finished, and each run appears in
the printer's job history. The job starts with the compatibility checks for
the printer's model, then `G28 O` to home only if the position isn't
known: every job ends by lowering the bed, and with Z unhomed (as after a
boot) the printer would drive the bed into its stop. It then sets the
filament with `M865` without moving any,
sets the instance UUID with `M865.1` on tool 0, and ends with an `M117`
message naming the spool. Don't count on seeing that message: the printer
shows an `M117` for 5 seconds, the finished-job screen covers it, and later
messages push it out of the eight-entry **Tune** > **Messages** history. The
spool's flag in Spoolman is the reliable record.

The bridge writes a tool once the printer is idle, after a swap takes effect
or the tool unloads. A menu load replaces the filament with a preset, which
drops the color and UUID, so a write is expected then. It waits while a swap
is staged and while the tool is empty. During an unconfirmed swap it writes
the spool Spoolman still assigns, so the printer shows what Spoolman
thinks is loaded; confirming a different spool rewrites the printer. If the printer still
differs after two jobs, it stops and flags the spool.

The bridge remembers the `M865` line it last wrote for each tool. If the
printer still shows that write but the spool has changed in Spoolman (color,
name, material, temperatures or UUID), the bridge writes the change when
idle. Temperature edits are caught this way even though the printer doesn't
report temperatures back.

Any other mismatch, with no swap, unload or Spoolman edit behind it, is drift:
something changed on the printer. The bridge flags it and leaves the printer
alone, unless `sync_drift = true`.

After upgrading from a version that didn't record its writes, the bridge
rewrites each tool once when idle. Those versions relied on `J` for
temperatures, which the firmware ignores, so they left the nozzle and bed
temperatures at 0.

Requirements for a write:

- The bridge must support the printer's model. At startup and after each
  printer reboot, it reads the model from `GET /api/version`. On a model
  that's unsupported or unknown, it doesn't write to the printer and flags
  any spool waiting to be written. Charging isn't affected.
- The job's compatibility checks come from the model: `M862.3 P"<model>"`,
  `M862.5 P2` and `M862.6 P"Input shaper"`. If you set `gcode_preamble`, its
  `M862.3` must name the printer's model exactly (or its MMU3 variant), or
  the bridge doesn't write. A job for the wrong model would stop on the
  printer's compatibility warning until someone tapped through it.
- The spool's material must be a printer preset (PLA, PETG, ASA, PC, PVB,
  ABS, HIPS, PP, FLEX, PA, PVA, BVOH; TPU and TPE map to FLEX), or its
  filament must have extruder and bed temperatures in Spoolman. `M865 X R`
  resets every filament parameter, so the job writes them all: the preset's
  nozzle, preheat, bed and chamber temperatures and its filtration and
  flexible settings, with Spoolman's extruder and bed temperatures taking
  priority. PPS and PPA need the HT idler-door setting, which `M865` can't
  set, so load them from the printer's menu. Otherwise the bridge flags the
  spool and doesn't write.
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
