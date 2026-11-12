# Bridge scenarios

This is the list of events and transitions the bridge has to handle, what it
does for each, and how sure we are of it. It covers one single-tool printer,
and [Several printers](#several-printers) covers what changes when one bridge
serves two of them. [Multi-tool](#multi-tool) covers a printer with several
tools (INDX, XL), so far against the simulator only.

Status values:

- **End to end**: a test in `integration/` runs the bridge, the simulator and
  a real Spoolman through it, in lockstep. This is the status every row is
  heading for: it runs on every CI turn, and the simulator's printer
  behavior is checked against the printer itself by
  [printer-behaviors.md](printer-behaviors.md). It goes after "Seen live" or
  "Tested" where both apply, and the cell says what the tests cover. The
  table in [integration/README.md](../integration/README.md#the-scenarios)
  maps each row to its tests.
- **Seen live**: the behavior has been observed on the Core One L+. Kept as
  history on rows that have it. New live work goes into the printer
  behaviors, not into scenario rows.
- **Tested**: a unit test covers it, but it hasn't been seen live.
- **Implemented**: code handles it, with no test.
- **Decided**: the behavior is settled. Unless the row also says
  "implemented", the code doesn't do it yet.
- **Gap**: the bridge does something wrong or unhelpful.
- **Question**: the behavior needs a decision.

Terms: the *active* spool is the one the bridge charges, which it believes is
in the tool. The *assigned* spool is the one in the tool's Spoolman location.
An *unconfirmed swap* is an unload with no Spoolman change yet.

## Logging

Decided: the bridge logs a problem once, when it starts, and stays quiet
about it until something changes: the problem ends, or its details change.
It never logs the same problem on every poll. Long outages get an hourly
reminder at most (C2).

Implemented in `208d30d` for read failures, retried charges, evictions,
flag and UUID writes, field setup and refused sync jobs. A deleted or
archived active spool (M3, M3a) is logged once when the bridge finds it.

## Swaps between prints

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| S1 | Spoolman first: move the new spool into the location, then unload and load. | Stages the new spool and keeps charging the old one. At the unload, charges usage before it to the old spool, makes the new one active, and moves the old one out of the location. The load is charged to the new spool. Usage during the load is held until the printer reports a material, as in S2, even though the new spool is already active. Writes the new spool to the printer when idle if it was loaded from the menu. A spool loaded from its tag isn't written, even when the tag's name, color or temperatures differ from Spoolman's (Q11). A tag whose instance UUID the spool doesn't have yet gives the spool that UUID (Q10). A spool with a different UUID in Spoolman is flagged. With UUIDs off (`uuid_field = ""`), a tag's UUID is neither stored nor compared: no adoption, no conflict. | Tested (live test with spool 18); end to end, including a tag that already matches; a tag with another name, color and temperatures left alone; a menu load of another type written; a tag's UUID adopted, or not adopted with UUIDs off; a conflicting UUID; a tag the bridge didn't see load, neither adopted nor written; and a tag whose UUID an archived spool has, refused once. The load held until a material is reported was seen on the first live run of the suite; no test pins its timing |
| S2 | Filament first: unload, then change Spoolman, then load. | Opens an unconfirmed swap and flags the old spool. The Spoolman change confirms it and clears the flag. Usage during the load is held until the printer reports a filament, then charged to the new spool. Writes when idle. | Seen live (19 → 14); end to end |
| S3 | Filament first, but Spoolman changed during the load. | As S2. Usage already charged to the old spool since the unload is moved to the new one. If the old spool is moved out of the location before the new one is moved in, the swap stays open while the location is empty, even through a print start, and later usage is held. The next spool moved in confirms the swap and takes all the usage since the unload. Moving the old spool back in leaves the swap as if the spool never left: open, or, if a print started while it was out, closed as of that print start with a provisional charge (S4). An unload while the location is empty closes the swap with its usage on the old spool, and the usage held since the location emptied is reported lost (S6). While the swap is open, the bridge writes the still-assigned old spool over a menu load once the load finishes and the printer is idle (W11); a Spoolman move made after that still confirms the swap and rewrites the printer. A new spool loaded from its tag isn't written over (Q11). | Seen live (14 → 19); end to end, including the two-step change; the old spool moved back; an unload while the location is empty, with the new spool's tag left alone; a move after the old spool is written back |
| S4 | Unload and reload the same spool, with no Spoolman change, then print. | Opens an unconfirmed swap. The print start closes it as "assumed same spool", records a provisional charge (Q1) and flags the spool with the amount. The next unload keeps the usage there. | Seen live (job 126); the provisional charge is tested; end to end, including the reloaded spool claimed, which confirms it |
| S5 | Unload, load a different spool, forget Spoolman, and print. | As S4: the print is charged to the old spool provisionally, and the flag says how to move it. Moving the right spool into the location and ticking **Claim held usage** on it moves the usage since the unload, makes it active, evicts the old spool and queues a write, unless the spool was loaded from its tag (see Q1, Q11 and [Claims](#claims)). Moving the right spool in without the tick does the same when the printer reports that spool's UUID, as for a spool loaded from its tag: the move is a correction (S12), and it settles which spool was loaded. The old spool's flag clears either way. With claims off (`claim_field = ""`), nothing is claimed: a ticked box moves nothing and isn't cleared, a ticked spool outside every location isn't flagged, and the flag says to move the usage in Spoolman by hand. | Decided and implemented; tested; end to end, including a correction instead of a claim; with `claim_field = ""`, a ticked box ignored |
| S6 | Unload with nothing assigned in Spoolman. | Usage before the unload is reported lost. Usage after it is held until a spool is assigned. | Seen live (20 → 2 → 7, with spool 2's purge reported lost); end to end |
| S7 | Old spool moved out of the location first, then the new one moved in, then unload. | The old spool stays active until the unload. The new one is staged. If the filament was swapped first, see S3. | Seen live (7 → 14); end to end |
| S8 | Spool staged, then moved back out before any unload. | Staging is cancelled and logged. The active spool keeps being charged. | Seen live (spool 2 staged over spool 7); end to end, including the staged spool moved out after the active one had left |
| S9 | Spool staged, and the user never unloads. | It stays staged indefinitely. Each print start logs that the staged spool isn't loaded and flags both the active and the staged spool. A reboot while it's staged flags the staged spool too. Claiming the staged spool makes it active (see Q2). | Decided and implemented; tested; end to end, including its flags outliving a bridge restart |
| S10 | Spool moved out of the location with no replacement. | It stays active until the next unload, then usage is held as unassigned. During an unconfirmed swap, usage is held at once and the swap stays open (S3). | Seen live (spool 20); the unconfirmed-swap case is tested; end to end |
| S11 | Two spools in the location, neither active. | Skips the tool and warns until one is removed. At the first start, nothing is recorded until the location is clear, and the baseline is taken then (B1). A note about how the printer compares with the spool stands, unlogged, while the tool is skipped, and so does its flag in Spoolman. A bridge restart while the tool is skipped clears that flag until the tool is compared again. | Seen live (spools 2 and 7); end to end, including a location that's ambiguous at the first start, with the baseline taken once it's clear; a drift note standing while the tool is skipped; its drift flag standing too; reboots while the tool is skipped, the usage held once and charged once it's clear |
| S12 | Spoolman changed to a spool whose UUID the printer already reports, with no unload. | Treated as a correction: switches immediately. The spool's UUID can be one the bridge wrote, or a tag's that the user entered in Spoolman or the bridge adopted (Q10). During a provisional charge (S4, S5), the correction also acts as a claim: the usage since the unload moves from the old spool to the new one, and the old spool is evicted and its flag cleared (see [Claims](#claims)). Otherwise nothing moves: the usage before the correction stays on the old spool. | Tested; end to end |
| S13 | Spoolman changed with no unload, and the printer doesn't report the new spool's UUID. | Staged until the next unload. If the user meant "I got it wrong; this spool is loaded", they claim it (see Q3). | Decided and implemented; tested; end to end |
| S14 | Several unloads and loads between two polls. | Counts them as one unload. Usage up to the last unload goes to the active spool. | Seen live (14 → 7 → 20, with the bridge stopped during the swaps); end to end |

## During a print

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| P1 | Normal print. | Charges each poll's change in the counter, every 30 s. | Seen live; end to end |
| P2 | Runout mid-print, Spoolman updated during the pause. | The runout unload opens an unconfirmed swap. The Spoolman change confirms it. The reload keeps the old spool's metadata on the printer, so a write is queued for when the print ends. If the old spool was loaded from a tag, the reload keeps that tag's data. It isn't the new spool's, so the bridge writes the new spool over it (Q11), with a UUID of its own: the new spool doesn't take the tag's, even with the old spool archived (Q10). | Seen live (19 → 20); the write was missed before `4dffd99`; end to end, including the old spool's tag kept |
| P3 | Runout mid-print, Spoolman updated after the print ends. | The unconfirmed swap stays open: the running job isn't a new print start. The later Spoolman change confirms it and moves the usage since the unload. | Tested; end to end |
| P4 | Runout, and the same spool type is reloaded with no Spoolman change ever. | Stays open until the next print start, which closes it as "assumed same spool", records a provisional charge (Q1) and flags the spool with the amount. The next unload keeps the usage there, or a claim moves it. | Tested; end to end |
| P5 | Planned color change (`M600` in the G-code). | Same as a runout: each change is an unload. | Tested; end to end |
| P6 | Filament jam: feeding stops, `ATTENTION`, unload and reload, purge more. | Treated as an unconfirmed swap. Purge is charged. The reload after a jam purges more than a normal load, and all of it is charged. | Seen live (job 124); end to end |
| P7 | Print cancelled partway. | Charges what was extruded. Nothing special happens. | Seen live (job 124); end to end |
| P8 | Print starts and is cancelled before extruding, during preheat or bed leveling. | Not a print start: a print starts at its first extrusion (see Q4). An unconfirmed swap stays open, so fixing Spoolman during preheat still confirms it. | Tested; end to end |
| P9 | Print restarted after a failure (new job ID). | A new print start. | Seen live (job 126); end to end |
| P10 | Spoolman change mid-print with no unload (scanning the next spool early). | Staged. The old spool is charged until the next unload. | Tested; end to end |
| P11 | Pause and resume with no filament change. | Nothing changes. The counter continues. | Implemented; end to end |
| P12 | Power loss mid-print, then power-panic resume. | A reboot: counters restart, and usage since the last poll (up to 30 s) is lost. The job ID is kept across reboots, so a resumed job with the same ID isn't a new print start (see Q5). | Tested; the job ID is unconfirmed on the printer; end to end |

## Printer state and the counters

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| C1 | Printer reboots or is power-cycled between prints. | New boot ID: counters restart from 0. Metadata stays on the printer. Usage held at the reboot, because the tool reported no filament, no spool was assigned or the location was ambiguous, is kept. It goes to the active spool as soon as there is one, and is logged then. An unload with no spool active first reports it lost, as in S6. | Seen live; held usage is tested; end to end, including usage held over a reboot with an unconfirmed swap open, charged to the old spool and moved with the swap; usage held over a reboot with a provisional charge open, joining it |
| C2 | Printer switched off for hours. | Logs once when it goes away, hourly while it's away, and once when it's back. Polls every 60 s. | Tested; end to end |
| C3 | Reboot and an unload in the same poll. | Handled in order: reboot, then unload. | Tested; end to end |
| C4 | Counter goes backward within one boot. | Logs a counter anomaly and resyncs without charging. | Tested; end to end |
| C5 | `extruded_at_unload` out of range. | Clamped, and logged as an anomaly. | Tested; end to end |
| C6 | Manual extrusion or retraction from the printer menu. | Extrusion is charged to the active spool. The counter only counts filament fed. | Implemented; end to end |
| C7 | Tool reports no filament (after an unload, or a sensor problem). | Holds usage until a filament is reported. | Tested; end to end |
| C8 | Firmware update. | A reboot (C1). The model is checked again. Firmware that reports filament with no usage counters is treated like firmware without the endpoint: logged once, nothing charged, and resumed from a baseline when counters are back. | Implemented; end to end, including firmware with no usage counters (MK3.5, MINI) |
| C9 | Printer answers with an error (wrong API key, 500). | Logs the error once, again if the error changes, and as an hourly INFO reminder while it lasts. One warning on recovery gives how long it lasted. Charges nothing. | Tested; end to end |

## Writing to the printer

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| W1 | Write after a swap, printer idle. | Uploads and runs the sync job: model checks, `G28 O`, `M865` with every parameter, `M865.1` UUID. Gives the spool a UUID first if it has none. If Spoolman can't store the UUID, or can't set up its field for a reason that may pass (a server error, a transport error, a timeout or rate limit (408, 429), or a body that can't be read), that tool's write waits and the next poll mints again, so the spool has its UUID before the printer is written. If the field can't be set up until someone fixes it (its key exists with another type, or the fields API answers with a 4xx), the spool is written without a UUID (B5). A tool loaded from a tag isn't written; its spool takes the tag's UUID instead (Q10, Q11). Models with no tag reader (MK4, MK3.9) and bridges with UUIDs off get the `M865` line only: no `M865.1`, and the spool keeps whatever UUID it has. A flexible material (`TPU`, `FLEX`) adds `G1`, a filament with no color writes no `O`, an 8-digit `RRGGBBAA` color writes its first six digits, and a filament with no material is named after its vendor and name. A custom `gcode_preamble` starts the job as given, if its `M862.3` check names the model or its MMU3 variant, whatever lines come before it. | Seen live; end to end, including on a model with no tag reader (MK4, MK3.9), or with UUIDs off, no `M865.1` and no UUID given; a flexible, colorless, materialless or RGBA filament; a custom preamble; storing the minted UUID fails, logged once and nothing written; then Spoolman takes it, the spool and the printer get the UUID and one INFO line says so |
| W2 | Write due while the printer is printing. | Waits until it's idle. | Tested; end to end |
| W3 | Printer refuses the upload (409 busy), or a print starts between the poll's read and the upload. | Waits for the next idle poll. The bridge reads the printer's state again right before uploading, so a print that started since gets no upload; that wait is logged once at INFO, and ends with the job's own line. | Tested; end to end, including a print started before the upload |
| W4 | User starts a print within a minute of loading. | The write waits until that print ends, so the print runs with the menu's metadata. | Tested; end to end |
| W5 | The printer still differs after two sync jobs. | Stops and flags the spool ("didn't take"). | Tested; end to end |
| W6 | The bridge's own sync job. | Not counted as a print start: recognized by its file name. A job that's gone when the bridge asks for its name (`/api/v1/job` answers 204) never extrudes, so it isn't a print start either. | Tested; end to end, including a sync job that ends before its name is read |
| W7 | Spool's material isn't a preset and Spoolman has no temperatures. | Flags the spool and doesn't write. The same for a spool with no material and a name with no usable characters (there's nothing to name the filament), and for an instance UUID in Spoolman that isn't a UUID, which the bridge leaves as it is until the user fixes or clears it. | Tested; end to end, including a spool with no material and no usable name; an instance UUID in Spoolman that isn't a UUID |
| W8 | PPS or PPA. | Flags the spool and doesn't write: `M865` can't set the HT idler-door requirement. Once the printer shows the material, loaded from its menu, the flag clears; the color and UUID the menu load leaves out aren't drift. | Tested; end to end |
| W9 | Printer differs with no swap or unload behind it (drift). | Flags it and leaves the printer alone, unless `sync_drift = true`. On a tool loaded from a tag, only a UUID that isn't the spool's counts: another spool's, named in the flag, or one that differs from the spool's. It's never written, even with `sync_drift` (Q11). With `flag_drift = false` it's only logged, and with `flag_field = ""` nothing is flagged at all. A spool edited in Spoolman while the printer was also changed is drift, not an edit (W14): the printer must still show the last write for an edit to be written. With UUIDs off, no UUID counts as drift. A color the printer reports that isn't a hex color never matches, so it's drift too. | Tested; end to end, including a temperature changed on the printer; with `flag_drift = false` or `flag_field = ""`, logged with no flag written; a name, color, UUID or temperature changed on the printer beside an edit in Spoolman; a color that isn't a hex color |
| W10 | Unsupported, unknown or mismatched model. | Logs once, flags waiting spools, and doesn't write. Charging continues. A `gcode_preamble` with no `M862.3` model check, or one for another model, counts as mismatched. Unsupported means a model the bridge knows but won't write to: the XL and other multi-tool models. MINI and MK3.5 report no usage counters, so the bridge reads nothing from them, charges nothing, and has nothing to flag (X10). A model that can't be read, because `/api/version` fails, keeps writes off the same way until a read works: it's read again at each poll, logged once, and logged once at WARNING when it's read. | Tested; end to end, including a `gcode_preamble` with no `M862.3` check; a model that can't be read |
| W11 | Unconfirmed swap open. | Writes the assigned spool over a menu load, so the printer shows what Spoolman thinks is loaded. A spool loaded from its tag isn't written over (Q11). | Tested; end to end, including a tag left alone |
| W12 | Spool staged. | Waits for the unload before writing. | Tested; end to end |
| W13 | Unhomed printer after a boot. | `G28 O` homes it before the job ends, so the end-of-job bed move is safe. | Seen live (2026-11-07: a sync job right after a cold boot homed, then lowered the bed safely); end to end |
| W14 | Spoolman changes the spool's color, name, material, temperatures or UUID with no swap (an edit). | If the printer still matches the bridge's last write, writes the edit when idle (see Q6). Otherwise it's drift (W9). A name-only edit isn't written when the filament has a material: the printer's filament name is then the material, so the spool's name isn't on the printer. With no material, the printer's filament name is the first 7 letters, digits, `_` or `-` of the vendor and name, so a name edit is written only if it changes them. A tool loaded from a tag isn't written, so an edit isn't either (Q11). An edit that leaves the spool unwritable, such as to PPS, is flagged with why it can't be written. It isn't drift. | Seen live (spool 20: a color edit, then two quick temperature edits, each written); end to end, including an edit after the spool's UUID was cleared, written with the printer's UUID; an edit that leaves the spool unwritable, flagged with why and not drift |
| W15 | The upload fails another way: an error other than 409, or the connection drops. | Logs a warning once and keeps the write due, trying again at each idle poll. Logs once at WARNING when a job runs again. | Tested; end to end |

## Spoolman

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| M1 | Spoolman unreachable. | Skips accounting. Queued charges stay in the outbox and are retried in order. | Tested; end to end, including the spool list answering 5xx or bad JSON, logged once and not as an outage |
| M2 | Spoolman rejects a charge (4xx). | Drops it with an error, so it doesn't block later charges. | Tested; end to end, including a charge Spoolman applies with a reply that isn't JSON, kept |
| M3 | Active spool deleted in Spoolman. | Found when the spool drops out of Spoolman's list and a lookup returns 404, or when a charge to it returns 404. The tool is left with no active spool, and the rejected charge, any usage since an open unconfirmed swap's unload, and later usage are held, through reboots and unloads. The held usage is charged to the next spool that becomes active in the tool. A spool already staged is flagged and takes it at the next unload or print start, or when it's claimed. Logged once when found and once when charged (see Q7). | Tested; end to end, including a 404 on a charge; a 404 for a spool no longer active, dropped; the active spool deleted with an unconfirmed swap open; a staged spool taking held usage at a print start; an unload with no poll after the extrusion; delete and re-create, a correction; a staged spool's held-usage flag and the unload that gives it the usage; a staged spool claimed, taking the held usage; a spool moved in before any usage was held; a missing spool whose lookup fails, logged once until it's looked up or listed again |
| M3a | Active spool archived in Spoolman. | Looks moved out of the location (S10): stays active and charged until the next unload. Flagged "archived but still loaded" until then (see Q7). | Tested; end to end |
| M4 | Location renamed in Spoolman. | The location looks empty: the spool stays active until the next unload, then usage is held. There's no websocket event for renames. | Implemented; end to end |
| M5 | Spool runs past empty in Spoolman. | Keeps charging, and logs once at INFO that the spool shows a negative remaining weight, and once when it no longer does while still loaded. Unloading it ends the line silently. No flag: spool weights are inexact, so slightly negative is normal (see Q8). A spool with no net weight has no remaining weight to go negative, so it's charged and never logged. | Tested; end to end, including a spool with no net weight and a filament with no vendor; the line ending at a weight edit, or silently at an unload |
| M5a | Runout mid-print while Spoolman shows plenty left. | Flags the spool when it unloads during a started print, meaning two polls have seen the counter move, with more than 5% of its net weight left. The flag clears when the user edits the spool's weight in Spoolman, or when another spool becomes active and then this one does. Reloading the same spool with no Spoolman change keeps it (see Q8), and also raises the unconfirmed-swap flag from the unload. A jam or tangle looks the same. | Tested; end to end, including a spool with no net weight; a weight edited just before a charge; a refund to the spool after it's archived; a refund bigger than the used weight, which Spoolman stops at 0, not taken for an edit |
| M6 | Text someone else put in the bridge's flag field. | The field is the bridge's. On spools the bridge has no flag for, the text is left as it is; on a spool the bridge flags, its flag replaces the text, and clearing the flag empties the field. Decided 2026-11-08: the bridge owns the field, so anything else written there may be overwritten. A spool the bridge flagged that leaves Spoolman's list is looked up. An archived one keeps its flag current, so a stale flag is cleared; Spoolman accepts writes to archived spools. A deleted one is forgotten. Decided in #120. With `flag_field = ""` the bridge writes nothing to the field, so every note stays. | Tested; end to end, including with `flag_field = ""`, every note left alone; a flag write that fails, logged once. A spool whose flag can't be cleared is deleted or archived: the failure's end is logged once at INFO, and an archived spool's stale flag is retried until it's cleared |
| M7 | Spoolman's filament density is wrong. | Charges use the density at the time of each charge. Earlier charges aren't recalculated. | Implemented (Spoolman behavior); end to end |

## The bridge itself

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| B1 | First start. | Takes the current counter as a baseline. Earlier usage isn't charged. | Seen live; end to end |
| B2 | Restart mid-print. | Catches up in one charge, with no double charge and no loss. | Seen live (job 131); end to end |
| B3 | Killed between a charge being accepted and the outbox being saved. | That charge is sent again: up to one poll's worth, double-charged. | Implemented; known limit |
| B4 | The database lost. | Starts again from a baseline (B1). Usage since the last poll is lost, and swap state is forgotten. | Implemented; a lost database is a deployment problem (see Out of scope); end to end |
| B5 | First start on a Spoolman without the bridge's extra fields. | Registers each field when it first needs it: the flag (text) and claim (boolean) fields at the first poll, the instance UUID field (text) when it first stores a UUID. A key that already exists with another type is logged once and left alone: nothing is written to that field, and charging carries on. The instance UUID field follows the latest failure to set it up. While it's one that lasts until someone fixes it, a key of another type or a 4xx from the fields API, spools are written to the printer without a UUID. While it's one that may pass, such as a 500, a 408 or a 429, a write that needs a UUID waits until the field is set up and the UUID stored (W1). Either is logged once. | Tested; end to end, including a key with the wrong type, and field setup failing, then recovering. Unit tests cover the instance UUID field: a key with the wrong type, spools written without a UUID; its setup failing, then working, the write held until the UUID is stored; a 403 from the fields API, spools written without a UUID; a key of another type, then fixed with the field read failing, the write held |
| B6 | A second bridge started on a running bridge's database. | Refuses to start, naming the lock file beside the database (Q9). The running bridge carries on. | Tested; end to end |
| B7 | The database file isn't a bridge database: another program's, corrupt, or a schema version this bridge doesn't know. | Refuses to start, naming the file, and leaves it as it was. | Tested; end to end |

## Claims

A claim is how the user tells the bridge which spool is in a tool when the
bridge can't know. It answers Q1, Q2 and Q3. Decided and implemented.

- **The field.** A boolean extra field on spools, **Claim held usage**,
  registered by the bridge like the flag field. Its key is `claim_field`,
  `bridge_claim` by default; `""` turns claims off. The bridge clears the box
  by writing `false`.
- **What it means.** "This spool is in this tool now, and has been since the
  tool was last loaded."
- **How to claim.** Move the spool into the tool's location, then tick the
  box. It works the same for one tool or several: the location says which
  tool. Moving a spool in while another is active stages it, which is
  harmless until the claim.
- **What moves.** Everything charged to that tool since the later of its
  last unload and the printer's last reboot, which is when the claimed spool
  can have gone in. The bridge charges the claimed spool before refunding the
  old one, as for a confirmed swap. It keeps a per-tool record of what it
  charged to each spool in that window (`since_load` in the database), so
  usage charged to more than one spool, such as across a correction, all
  moves.
- **When.** At the next poll, even during a print or while a job waits for
  its first extrusion. The user only claims a spool once they know it's
  loaded, so there's nothing to wait for.
- **Afterwards.** The claimed spool becomes active, the old one is moved out
  of the location, any provisional charge or staging closes, the box is
  cleared, and a write to the printer is queued.
- **Claiming the active spool** confirms it and closes any provisional charge.
  Nothing moves.
- **A correction during a provisional charge** (S12) is handled as a claim
  on the spool whose UUID the printer reports: the same usage moves, and the
  same things happen afterwards, though there's no box to clear. The
  printer's UUID answers the question the provisional charge asks.
- **Several claims for one tool in the same poll interval.** Spoolman doesn't
  record when a field changed, so the bridge can't tell which box was ticked
  first. It picks the spool with the most recent `last_used`, treating none
  as oldest and breaking ties by the highest spool ID. It processes that
  claim, clears the other boxes, and logs one line naming the spool it chose
  and the ones it cleared. The active spool usually has the most recent
  `last_used`, since the bridge charges it, so a tick on it alongside another
  spool confirms it. Claims in different poll intervals are handled one at a
  time, in order, so a later claim moves the usage on.
- **A claim with nothing to move**, such as right after an unload, still
  makes the spool active, evicts the old one and queues a write.
- **A ticked spool that isn't in any tool's location** is flagged and left
  alone until it is.
- **A claim while the location is ambiguous** (S11) waits until the location
  is resolved. The claim isn't used to resolve it.
- **A claim in the same poll as an unload** is handled after the unload, so
  only usage after the unload moves.
- **Handled once.** The bridge records each handled claim in the database
  until Spoolman shows the box clear, so a failed clear doesn't handle it
  again on the next poll.

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| K1 | The bridge can't clear a claim's box. | Handles the claim once: the usage moves at the first poll, and later polls don't move it again while the box stays ticked. The failure is logged once, then at DEBUG while it lasts. When the box is cleared some other way, such as by hand, the bridge stops trying and logs that once. | Tested; end to end, including a claim already ticked at the first start, confirmed with the baseline and cleared |
| K2 | Two spools in one tool's location claimed in the same poll interval. | Handles the claim on the most recently used spool, clears the other box, and logs one warning naming both. | Tested; end to end |

Possible improvement: Spoolman's websocket pushes an event for every spool
change as it happens. Listening on it would give the order of claims, and
react to Spoolman changes within seconds rather than at the next poll. It
needs a connection kept open alongside polling, and location renames still
send no events.

## Several printers

One bridge can serve several printers against one Spoolman. Printer A has
location `LA`; printer B has location `LB`. Each row says what happens from
both sides. Most are the single-printer rows above seen from two printers at
once, and the row names which. Everything here is tested with two fake
printers against one fake Spoolman, and most rows end to end with two
simulated printers against a real Spoolman; none of it has been seen live,
because there's no second printer yet (see "A second printer" in `live-checks.md`).
X12, X13 and X15 aren't used.

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| X1 | Spoolman first: move the spool from `LA` to `LB`, unload at A, load at B. | A: S10. The spool stays active until A's unload, then A holds usage as unassigned. B: S1. The spool is staged until B's unload, then active and charged the load. A's unload and B's load in one poll interval are handled independently, in either order. | Tested; end to end |
| X2 | Filament first: unload at A, load at B, then move the spool `LA` to `LB`. | A: S2 then S3. An unconfirmed swap opens and the spool is flagged; the move empties `LA`, so the swap stays open and A holds later usage for the next spool moved in. B: S2. B's unconfirmed swap on its old spool is confirmed by the move, and usage since B's unload moves to the spool. The spool carries A's "moved out" flag while active in B, until something is moved into `LA`. | Tested; end to end |
| X3 | Spool moved `LA` to `LB` in Spoolman while still loaded in A, and never loaded in B. | A: S10, still charged. B: S9, staged and flagged at each B print start. Moving it back to `LA` is S8 on B and resumes A's assignment. | Tested; end to end |
| X4 | Swap on A evicts the old spool; the user moves it into `LB` and loads it in B. | A: the eviction finds the spool already out of `LA` and clears it. B: S1 or S2 depending on order. A's flag on the spool, if any, clears when A's state no longer mentions it. | Tested; end to end, including an eviction Spoolman keeps refusing, ended when the spool is moved into `LB` |
| X4a | The user moves the evicted spool into `LB` between the bridge's Spoolman read and its eviction. | `evict()` re-reads the spool and sees it in `LB`, so it doesn't move it. Without the re-read, the bridge would move it to the eviction location, undoing the user's move. The re-read narrows the window to the instant between it and the write; a move in that instant is still undone. | Tested; end to end |
| X5 | A spool in `LB` is claimed while A still has it active, because A hasn't seen its unload yet (A is off). | B: a claim as in S5, from B's `since_load`. A: when it polls again, a reboot or unload at A ends the spool's time there; usage before it is charged to the spool, which is right, since the filament was in A. | Tested; end to end |
| X6 | One spool active in two tools: A missed an unload across a reboot (Q2), and the spool is now loaded and active in B. | Logged once naming both locations, and flagged on the spool. A keeps charging it until A's next unload, print-start flag (S9), or a claim on A's staged spool. B charges it normally. | Tested; end to end |
| X7 | Two spools claimed in one poll, one in `LA` and one in `LB`. | Two claims for two tools: each is handled on its own. The tie-break in Claims applies only within one tool. | Tested; end to end |
| X8 | Printer A is off. | A's outage is logged once, hourly, and when it ends (C2). B is polled, charged, and written as normal. Evictions, runout flags, and the shared-spool check still run every tick, including A's pending evictions. A's own tool flags stay in Spoolman as they were when A was last read. Claims on A's tools wait for A. The interval follows B's state. With every printer off, the tick ends after the printer reads, as with one printer. | Tested; end to end, including A's failing eviction ended while A is off |
| X9 | Spoolman is unreachable. | Every printer skips accounting (M1). While the outbox is empty, printer reads still happen, so a printer outage that starts meanwhile is logged on time; with charges queued, the tick stops at the failed flush, as with one printer. Charges from both printers wait in one outbox and are sent in order. | Tested; end to end |
| X10 | A's firmware lacks the filament endpoint (C9, MINI). | A logs once and charges nothing. B is unaffected, including the poll rate: A's status still reads, so A counts as idle or busy by it. | Tested; end to end |
| X11 | A charge from A is rejected with 404 (M3). | Held for A's tool, found by the record's `printer` and `tool`. A spool deleted while active in A and staged in B holds A's usage for A's next spool only. | Tested; end to end |
| X14 | A printer is renamed in the config. | The old name's state stays in the database, with a warning at each start, but is inert: its pending evictions never run, and a queued charge of its that Spoolman rejects with 404 is dropped, not held. The new name starts from a baseline (B1). | Tested; end to end |
| X16 | A runout flag (M5a) set on A, then the spool becomes active in B after another spool was. | The flag clears, as it does within one tool: the rule is "another spool, then this one" in the tool where it becomes active. A weight edit clears it too. | Tested; end to end |

## Multi-tool

A multi-tool printer, such as an INDX or an XL, has several tools, each with
its own filament path, sensor and counters. One location per tool is mapped
in the config, and the bridge keeps each tool's state and charges apart, so
every row above applies to each tool on its own, except the printer writes,
which are off on these models (T6), and the rows that need an instance UUID,
such as S12, since these models keep none (F21). These rows come from
[printer-behaviors.md](printer-behaviors.md) F19 to F23. No multi-tool
printer is at hand, so they run end to end against the simulator only, which
follows the firmware source; each gets traced when the INDX arrives.

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| T1 | A print with tool changes. | Charges each tool's spool the tool's own extrusion. The purge after a pickup counts on the new tool, so it's charged to that tool's spool (F20). | Simulator only; end to end |
| T2 | A tool change mid-print. | Not an unload: `unload_count` holds on both tools, so no swap opens on either and nothing is flagged. | Simulator only; end to end |
| T3 | A swap on one tool between prints, Spoolman first (S1). | S1 on that tool alone: the swap commits there and the new spool is charged the load. The other tool's state and spool aren't touched, and the next print charges each tool's spool. A swap can't happen on one tool while another prints: the printer refuses an unload during a print. | Simulator only; end to end |
| T4 | Picking a tool whose sensor sees no filament while it has a type, as after filament is pulled out with the printer off (F23). | The firmware counts an unload on that tool only, so the bridge opens an unconfirmed swap there and flags its spool (S2, C7). The other tools aren't touched. | Simulator only; end to end |
| T5 | A configured tool the printer doesn't report: disabled, or its dock not calibrated (F19). | Logs `printer doesn't report tool index N` once and skips that tool. The other tools are charged as normal. The skipped tool's flags in Spoolman stand. A bridge restart while the tool is skipped clears the flags from comparing the printer with the spool until the tool is compared again; the flags from its saved state survive. When the tool comes back, it carries on from its counter. | Simulator only; end to end |
| T6 | A write due on a multi-tool printer. | Writes are off for every multi-tool model: the bridge logs the model once per boot ("multi-tool printers aren't supported yet; printer writes are off"), and when a write would be due on a tool, flags the spool "can't write to the printer" and uploads nothing. Charging is unaffected (W10). Writes to multi-tool printers come with multi-tool printer support, when the bridge adds it: `M865 L<n>` per tool (F22) and no `M865.1`, since these models keep no UUIDs (F21). Not yet. | Simulator only; end to end |
| T7 | A multi-tool printer reboots between prints. | A reboot (C1) on every tool: each tool logs it and its counter restarts at 0, nothing is charged for the restart, and each tool's spool is then charged from 0. | Simulator only; end to end |

## Out of scope

The bridge doesn't detect or work around problems in how it's deployed or
configured. Each of these needs fixing in the deployment, not the code.

| Scenario | What happens |
| --- | --- |
| Two bridges against one printer, each with its own database | Every charge is doubled. Two bridges on one database can't run: the second refuses to start, naming the lock file beside the database. |
| Two bridges whose printers share a Spoolman location | Both charge the same spool. Within one bridge, a location mapped to tools on two printers is a config error. |
| Another tool also charging the same spools, such as an OctoPrint or Klipper plugin | Charges are doubled. |
| The database lost, for example with no persistent volume | The bridge starts again from a baseline (B1) and forgets swaps in progress (B4). |
| A wrong location name in the config | The location looks empty, so usage is held as unassigned. |
| The bridge pointed at the wrong printer or Spoolman | Charges go to the wrong place. |
| A sync job uploaded while the user is on the print preview screen | The preview reads `IDLE` with no job, so the bridge sees an idle printer, and the firmware replaces the pending print with the sync job (F15 in [printer-behaviors.md](printer-behaviors.md)). The user starts the print again afterwards. `sync_delay` narrows the window; the API can't show the preview, so nothing closes it. Accepted 2026-11-08. |

## Questions

- **Q1 (S5). Decided and implemented: provisional charging with a claim field.** A menu reload of the same spool looks the same as loading a different one: both drop the custom name, color and UUID. So the bridge can't tell them apart, and holding the usage would leave Spoolman short after every same-spool reload. Instead:
  1. When a print start closes an unconfirmed swap, the bridge charges the old spool as now, and records the charge as provisional: spool, millimeters and job. Later usage adds to it until it's resolved.
  2. It flags the old spool with how much was charged there provisionally, and how to move it.
  3. The user answers with a claim (see [Claims](#claims)): move the right spool into the location and tick **Claim held usage** on it. Claiming the old spool confirms the charge. If the printer reports the moved spool's UUID, the move alone is a correction (S12), and it moves the usage as a claim would.
  4. The next unload also closes the provisional charge, leaving it where it is.

  The provisional charge replaces the old "assumed same spool" notice, so it isn't cleared by the next print start. The flag shows the amount as of the print start and updates it only while no print is running, so the flag isn't rewritten on every poll. A reboot keeps the provisional charge, but a claim moves only usage since the reboot (see [Claims](#claims)), which can be less than the flag shows.

  We rejected attributing the usage to the next spool moved into Spoolman: with the Spoolman-first order, that's usually the next swap, not a correction.
- **Q2 (S9). Decided and implemented: staging doesn't expire.** A spool can be staged on purpose for a long time, for example when the current spool is low, and expiring it would quietly undo that. Changes:
  - At each print start, flag the staged spool as well as the active one: it's staged but not loaded, and the active spool is still being charged.
  - When the printer reboots while a spool is staged, flag the staged spool. `unload_count` resets on a reboot, so an unload just before it can be missed, leaving the staged spool in the tool while the old one is charged.
  - Claiming the staged spool (see [Claims](#claims)) makes it active. The usage moved starts at the reboot, which is where the missed unload happened.
  - Moving the staged spool back out still cancels it (S8).
  - The staged spool's flag clears when it stops being staged: an unload commits it, it's moved out, or a claim.
- **Q3 (S13). Decided and implemented: claims.** The user claims the spool that's loaded (see [Claims](#claims)). We rejected treating a Spoolman change as a correction whenever the printer reports no UUID: a reload of the same spool also clears the UUID, so it would give false corrections.
- **Q4 (P8). Decided and implemented: a print starts at its first extrusion.** A new job ID is only noted as pending. The first poll in that job where the counter increases is the print start, and all the start effects happen then: closing an unconfirmed swap, the provisional charge (Q1) and the staging flags (Q2). A job that ends without extruding changes nothing. This also gives the user preheat and bed leveling time to fix Spoolman, with the change treated as a confirmation. The first poll's extrusion is charged before the swap closes, to the same spool the swap would assume.
- **Q5 (P12). Decided and implemented: keep the saved job ID across reboots.** The firmware stores job IDs in EEPROM and assigns a new one only when a print starts through the print preview (`marlin_server.cpp:2307` in the firmware), so IDs aren't reused and a power-panic resume should keep its ID. That's from the source, not a test. With the ID kept, a resumed job is the same print carrying on. If a resume does get a new ID, it's a print start under Q4, which is harmless. Usage between the last poll and the power cut is lost: saving it would need the firmware to put the counter in its power-panic data. Confirming the job ID needs a test with the power pulled mid-print.
- **Q6 (W14). Decided and implemented: remember the last write.** The bridge stores the full `M865` line it last wrote for each tool. If the printer still matches that write and the line built from Spoolman now differs, Spoolman was edited, so the bridge writes it when idle. If the printer no longer matches the write, the printer changed, and that's drift as before. An edit counts as applied only when a poll sees the printer match, temperatures included (see W5). A tool loaded from a tag isn't written, so an edit to its spool isn't either (Q11).
- **Q7 (M3). Decided and implemented: hold usage for a deleted spool.** When a tool's active spool is missing from Spoolman's list of non-archived spools, the bridge looks it up once. A 404 means it was deleted. A 404 on a queued charge to the active spool means the same. The tool is left with no active spool, and that charge, any usage since an open unconfirmed swap's unload, and later usage go to a per-tool held total in raw extruded mm (`held_mm`, with the deleted spool in `deleted_spool`). Usage held with no spool assigned survives a reboot too (C1), but an unload with no spool active reports it lost (S6). The held total for a deleted spool survives an unload as well. The bridge logs once when it finds the deletion, and flags the spool assigned to the location, if there is one. The next spool that becomes active takes the held usage, charged as "held after spool N was deleted": a spool moved into the location, or a staged spool at the next unload or print start. A typical case is deleting a spool and re-creating it in Spoolman. A claim also takes `held_mm`: the claimed spool becomes active, is charged the held usage, and the deleted state clears. The deleted spool's own charges from before the deletion aren't refunded or moved, since it no longer exists and the charges were accepted when made. That includes a provisional charge (Q1) on it: it's dropped, not held. An archived spool keeps being charged until the next unload, and is flagged "archived but still loaded". Other 4xx rejections stay as they are: logged and dropped, with the ledger as the record.
- **Q8 (M5, M5a). Decided and implemented: log a negative remaining weight; flag a runout with plenty left.**
  - A spool whose remaining weight goes below 0 gets one log line, and another if it recovers while still loaded, and nothing else. Spool weights are inexact, so many spools end slightly negative. Spoolman itself shows 0 rather than a negative weight, so the bridge works it out from the used weight.
  - An unload during a print, after its first extrusion, while Spoolman shows more than a threshold remaining, flags the spool. That usually means an earlier swap charged the wrong spool. The flag says how much Spoolman thinks is left and suggests checking recent swaps. A jam or tangle that trips the filament sensor, and a planned `M600` color change, look the same to the bridge, so the wording allows for both. The threshold is 5% of the spool's net weight remaining, so it scales with 2 kg and 3 kg spools. A spool with no net weight in Spoolman is never flagged.
  - The flag clears when the user edits the spool's weight in Spoolman, since a corrected weight answers it. It also clears when another spool becomes active in the tool and then this one does. Reloading the same spool keeps it. The bridge keeps the spool's net and used weights with the flag. It moves the used weight on by each of its own charges when the charge's response matches, so any other change to either weight is an edit, even one made while a charge was queued. A charge whose response is lost is sent again and applied twice, so its response doesn't match: that also reads as an edit and clears the flag early, which costs only the flag.
- **Q9 (B4). Decided: out of scope, except a second bridge on one database.** The bridge doesn't try to detect or prevent bad deployments. The exception is a lock beside the database: a second bridge on the same database refuses to start. See [Out of scope](#out-of-scope).
- **Q10 (S1, W1). Decided and implemented: a tag's instance UUID is the spool's.** Users can enter the tag's UUID in Spoolman when they add a spool. If they don't, the bridge stores it on the spool when it trusts the tag, instead of minting one, so later loads of the tag match (#66). A load from a tag is told apart by the material and package UUIDs, which only a tag sets: the bridge's `M865 X R` clears them. The printer can report a tag that isn't the active spool's, because a runout reload keeps the old filament's type. So the UUID is adopted only when all of these hold:
  - The active spool is certain: assigned and active agree, with no unconfirmed swap and no provisional charge.
  - The bridge saw the UUID appear: the printer showed an empty tool, a type with no UUID, or another UUID before. A tag already loaded when the bridge first starts isn't trusted, because the spool assigned may not be the tag's (as in S12).
  - The UUID isn't a runout leftover: the printer didn't report it with another spool and then unload during a print the bridge saw start (Q11). A UUID recorded with another spool and followed by a claim or correction, with no unload, is the new spool's, but it didn't appear, so it isn't adopted until a load the bridge watches. Any other unload makes the spool the UUID was seen with unknown, and the same UUID shown next is neither written over nor adopted. After an idle unload, that UUID is the loaded spool's tag, but the spool recorded may have been wrong. After a runout in a print the bridge didn't see start, it's still the old spool's. A different UUID after that unload appears as usual. The UUID and spool last seen are saved in the database, so this holds across a restart, and a UUID that differs from the saved one after a restart counts as having appeared. With nothing saved, as at the first start, a tag already loaded isn't trusted.
  - No other spool in Spoolman has it, archived spools included.

  A tag that fails only the first two checks isn't adopted, and the bridge stores nothing. The tag stays on the printer, since a tool loaded from a tag isn't written (Q11). That covers a reload of the same spool with no Spoolman change, which is an unconfirmed swap. The tag is adopted at the first load the bridge watches with the spool certain, such as an S1 swap. If storing the UUID fails, the next poll tries again. The bridge still keeps a UUID that its last write to a tool carried: when a write is due for a spool with no UUID, it writes that UUID back instead of minting one.

  A spool with a different UUID in Spoolman is a conflict. The bridge flags it until the user sets or clears the spool's UUID. Overwriting either one could lose the tag's identity or hide a wrong spool. A tag whose UUID another spool in Spoolman has is drift on the active spool, and the flag names the spool whose tag it is (W9). The printer isn't written in either case (Q11).
- **Q11 (S1, S3, W11, W14). Decided and implemented: a tagged tool isn't written.** A tool whose type came from an NFC tag keeps it: the bridge writes no `M865` or `M865.1` to it, whatever Spoolman says about name, color or temperatures (#69). This supersedes the rule that Spoolman wins over the tag for those fields. Untagged tools are written as before. The reason is the printer's own tag tracking. The sync job's `M865 X R ... L0` resets the tool's parameters, tag link included, and the printer then stops reading and writing usage on the tag until the spool is loaded from its tag again (see [printer-behaviors.md](printer-behaviors.md), F10). Charges don't need the tool's filament data: they come from the extrusion counters. The tag's instance UUID stays on the printer, so Q10 and S12 still work.
  - A tool is loaded from a tag when it's tool 0 on a model that keeps UUIDs, and the printer reports the material or package UUID. A tag with no instance UUID counts too. This doesn't depend on the UUID settings: with `uuid_field = ""`, a tagged tool still isn't written, and nothing about it is drift.
  - This holds in every state: settled, staged, during an unconfirmed swap (W11), and with a provisional charge. An edit in Spoolman isn't written either (W14), and a write still expected from before the load is dropped.
  - Name, color and temperature differences aren't reported: the bridge won't act on them. A UUID that isn't the spool's is still drift (W9), and a trusted tag with a different UUID in Spoolman is still a conflict (Q10). Neither is written.
  - The exception is a runout leftover. A runout reload keeps the old spool's type, tag data included, while a different spool may be loaded. When the printer shows the instance UUID the bridge last saw with another spool, and the tool has unloaded during a print the bridge saw start since, the tag isn't the loaded spool's. The bridge writes over it as it would over a menu load, and the new spool gets a UUID of its own when UUIDs are on (P2). Only M600 (a runout, a jam, or Tune > Change filament) keeps the type across an unload, and it runs during a print. Every unload outside a print ends with a fresh type or none, so a tag shown after one was loaded from a tag. Such an unload makes the spool the UUID was seen with unknown: the same UUID shown next is left alone and isn't adopted (Q10). So is a runout in any other print, such as one that started while the bridge was down: the old spool's tag stays on the tool. The new spool is flagged only when the old spool has the tag's UUID in Spoolman (W9). A claim or a correction (S12) changes the active spool with no unload, so the tag is then the newly active spool's: the bridge recorded it with the wrong spool. A tag whose UUID the active spool has in Spoolman is never a leftover. The bridge records the UUID seen whenever the model reports UUIDs, whatever the UUID settings, and saves it with the spool and the runout in the database. A leftover tag with no instance UUID can't be told apart, so it's left alone.
