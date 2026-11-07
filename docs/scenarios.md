# Bridge scenarios

This is the list of events and transitions the bridge has to handle, what it
does for each, and how sure we are of it. It covers one single-tool printer.
Multi-tool (INDX, XL) is out of scope.

Status values:

- **Seen live**: the behavior has been observed on the Core One L+.
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
| S1 | Spoolman first: move the new spool into the location, then unload and load. | Stages the new spool and keeps charging the old one. At the unload, charges usage before it to the old spool, makes the new one active, and moves the old one out of the location. The load is charged to the new spool. Writes the new spool to the printer when idle. | Tested (live test with spool 18) |
| S2 | Filament first: unload, then change Spoolman, then load. | Opens an unconfirmed swap and flags the old spool. The Spoolman change confirms it and clears the flag. Usage during the load is held until the printer reports a filament, then charged to the new spool. Writes when idle. | Seen live (19 → 14) |
| S3 | Filament first, but Spoolman changed during the load. | As S2. Usage already charged to the old spool since the unload is moved to the new one. | Seen live (14 → 19) |
| S4 | Unload and reload the same spool, with no Spoolman change, then print. | Opens an unconfirmed swap. The print start closes it as "assumed same spool", records a provisional charge (Q1) and flags the spool with the amount. The next unload keeps the usage there. | Seen live (job 126); provisional charge tested |
| S5 | Unload, load a different spool, forget Spoolman, and print. | As S4: the print is charged to the old spool provisionally, and the flag says how to move it. Moving the right spool into the location and ticking **Claim held usage** on it moves the usage since the unload, makes it active, evicts the old spool and queues a write (see Q1 and [Claims](#claims)). | Decided and implemented; tested |
| S6 | Unload with nothing assigned in Spoolman. | Usage before the unload is reported lost. Usage after it is held until a spool is assigned. | Tested |
| S7 | Old spool moved out of the location first, then the new one moved in, then unload. | The old spool stays active until the unload. The new one is staged. | Tested |
| S8 | Spool staged, then moved back out before any unload. | Staging is cancelled and logged. The active spool keeps being charged. | Tested |
| S9 | Spool staged, and the user never unloads. | It stays staged indefinitely. Each print start logs that the staged spool isn't loaded and flags both the active and the staged spool. A reboot while it's staged flags the staged spool too. Claiming the staged spool makes it active (see Q2). | Decided and implemented; tested |
| S10 | Spool moved out of the location with no replacement. | It stays active until the next unload, then usage is held as unassigned. | Tested |
| S11 | Two spools in the location, neither active. | Skips the tool and warns until one is removed. | Tested |
| S12 | Spoolman changed to a spool whose UUID the printer already reports, with no unload. | Treated as a correction: switches immediately. | Tested |
| S13 | Spoolman changed with no unload, and the printer doesn't report the new spool's UUID. | Staged until the next unload. If the user meant "I got it wrong; this spool is loaded", they claim it (see Q3). | Decided and implemented; tested |
| S14 | Several unloads and loads between two polls. | Counts them as one unload. Usage up to the last unload goes to the active spool. | Tested |

## During a print

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| P1 | Normal print. | Charges each poll's change in the counter, every 30 s. | Seen live |
| P2 | Runout mid-print, Spoolman updated during the pause. | The runout unload opens an unconfirmed swap. The Spoolman change confirms it. The reload keeps the old spool's metadata on the printer, so a write is queued for when the print ends. | Seen live (19 → 20); the write was missed before `4dffd99` |
| P3 | Runout mid-print, Spoolman updated after the print ends. | The unconfirmed swap stays open: the running job isn't a new print start. The later Spoolman change confirms it and moves the usage since the unload. | Tested |
| P4 | Runout, and the same spool type is reloaded with no Spoolman change ever. | Stays open until the next print start, which closes it as "assumed same spool", records a provisional charge (Q1) and flags the spool with the amount. The next unload keeps the usage there, or a claim moves it. | Tested |
| P5 | Planned color change (`M600` in the G-code). | Same as a runout: each change is an unload. | Implemented; untested with several changes in one print |
| P6 | Filament jam: feeding stops, `ATTENTION`, unload and reload, purge more. | Treated as an unconfirmed swap. Purge is charged. | Seen live (job 124) |
| P7 | Print cancelled partway. | Charges what was extruded. Nothing special happens. | Seen live (job 124) |
| P8 | Print starts and is cancelled before extruding, during preheat or bed leveling. | Not a print start: a print starts at its first extrusion (see Q4). An unconfirmed swap stays open, so fixing Spoolman during preheat still confirms it. | Tested |
| P9 | Print restarted after a failure (new job ID). | A new print start. | Seen live (job 126) |
| P10 | Spoolman change mid-print with no unload (scanning the next spool early). | Staged. The old spool is charged until the next unload. | Tested |
| P11 | Pause and resume with no filament change. | Nothing changes. The counter continues. | Implemented |
| P12 | Power loss mid-print, then power-panic resume. | A reboot: counters restart, and usage since the last poll (up to 30 s) is lost. The job ID is kept across reboots, so a resumed job with the same ID isn't a new print start (see Q5). | Tested; the job ID is unconfirmed on the printer |

## Printer state and the counters

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| C1 | Printer reboots or is power-cycled between prints. | New boot ID: counters restart from 0. Metadata stays on the printer. Usage held at the reboot, because the tool reported no filament, no spool was assigned or the location was ambiguous, is kept. It goes to the active spool as soon as there is one, and is logged then. An unload with no spool active first reports it lost, as in S6. | Seen live; held usage tested |
| C2 | Printer switched off for hours. | Logs once when it goes away, hourly while it's away, and once when it's back. Polls every 60 s. | Tested |
| C3 | Reboot and an unload in the same poll. | Handled in order: reboot, then unload. | Tested |
| C4 | Counter goes backward within one boot. | Logs a counter anomaly and resyncs without charging. | Tested |
| C5 | `extruded_at_unload` out of range. | Clamped, and logged as an anomaly. | Tested |
| C6 | Manual extrusion or retraction from the printer menu. | Extrusion is charged to the active spool. The counter only counts filament fed. | Implemented |
| C7 | Tool reports no filament (after an unload, or a sensor problem). | Holds usage until a filament is reported. | Tested |
| C8 | Firmware update. | A reboot (C1). The model is checked again. | Implemented |
| C9 | Printer answers with an error (wrong API key, 500). | Logs the error once, until it changes or clears. Charges nothing. | Implemented |

## Writing to the printer

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| W1 | Write after a swap, printer idle. | Uploads and runs the sync job: model checks, `G28 O`, `M865` with every parameter, `M865.1` UUID, `M117`. Gives the spool a UUID first if it has none. | Seen live |
| W2 | Write due while the printer is printing. | Waits until it's idle. | Tested |
| W3 | Printer refuses the upload (409 busy). | Waits for the next idle poll. | Tested |
| W4 | User starts a print within a minute of loading. | The write waits until that print ends, so the print runs with the menu's metadata. | Implemented |
| W5 | The printer still differs after two sync jobs. | Stops and flags the spool ("didn't take"). | Tested |
| W6 | The bridge's own sync job. | Not counted as a print start: recognized by its file name. | Tested |
| W7 | Spool's material isn't a preset and Spoolman has no temperatures. | Flags the spool and doesn't write. | Tested |
| W8 | PPS or PPA. | Flags the spool and doesn't write: `M865` can't set the HT idler-door requirement. Once the printer shows the material, loaded from its menu, the flag clears; the color and UUID the menu load leaves out aren't drift. | Tested |
| W9 | Printer differs with no swap or unload behind it (drift). | Flags it and leaves the printer alone, unless `sync_drift = true`. | Tested |
| W10 | Unsupported, unknown or mismatched model. | Logs once, flags waiting spools, and doesn't write. Charging continues. | Tested |
| W11 | Unconfirmed swap open. | Writes the assigned spool, so the printer shows what Spoolman thinks is loaded. | Tested |
| W12 | Spool staged. | Waits for the unload before writing. | Tested |
| W13 | Unhomed printer after a boot. | `G28 O` homes it before the job ends, so the end-of-job bed move is safe. | Implemented; not yet confirmed on the printer |
| W14 | Spoolman changes the spool's color, name, material, temperatures or UUID with no swap (an edit). | If the printer still matches the bridge's last write, writes the edit when idle (see Q6). Otherwise it's drift (W9). | Tested |

## Spoolman

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| M1 | Spoolman unreachable. | Skips accounting. Queued charges stay in the outbox and are retried in order. | Tested |
| M2 | Spoolman rejects a charge (4xx). | Drops it with an error, so it doesn't block later charges. | Tested |
| M3 | Active spool deleted in Spoolman. | Found when the spool drops out of Spoolman's list and a lookup returns 404, or when a charge to it returns 404. The tool is left with no active spool, and the rejected charge, any usage since an open unconfirmed swap's unload, and later usage are held, through reboots and unloads. The held usage is charged to the next spool that becomes active in the tool. A spool already staged is flagged and takes it at the next unload or print start, or when it's claimed. Logged once when found and once when charged (see Q7). | Tested |
| M3a | Active spool archived in Spoolman. | Looks moved out of the location (S10): stays active and charged until the next unload. Flagged "archived but still loaded" until then (see Q7). | Tested |
| M4 | Location renamed in Spoolman. | The location looks empty: the spool stays active until the next unload, then usage is held. There's no websocket event for renames. | Implemented |
| M5 | Spool runs past empty in Spoolman. | Keeps charging, and logs once that the spool shows a negative remaining weight. No flag: spool weights are inexact, so slightly negative is normal (see Q8). | Tested |
| M5a | Runout mid-print while Spoolman shows plenty left. | Flags the spool when it unloads during a started print with more than 5% of its net weight left. The flag clears when the user edits the spool's weight in Spoolman, or when another spool becomes active and then this one does. Reloading the same spool with no Spoolman change keeps it (see Q8). A jam or tangle looks the same. | Tested |
| M6 | Flag set by someone else on a spool. | Left alone: the bridge only clears flags it set. | Tested |
| M7 | Spoolman's filament density is wrong. | Charges use the density at the time of each charge. Earlier charges aren't recalculated. | Implemented (Spoolman behavior) |

## The bridge itself

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| B1 | First start. | Takes the current counter as a baseline. Earlier usage isn't charged. | Seen live |
| B2 | Restart mid-print. | Catches up in one charge, with no double charge and no loss. | Seen live (job 131) |
| B3 | Killed between a charge being accepted and the outbox being saved. | That charge is sent again: up to one poll's worth, double-charged. | Implemented; known limit |
| B4 | `state.json` lost. | Starts again from a baseline (B1). Usage since the last poll is lost, and swap state is forgotten. | Implemented; a lost state file is a deployment problem (see Out of scope) |
| B6 | Dry run. | Logs what it would charge and write. Changes nothing. | Tested |

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
  charged to each spool in that window (`since_load` in `state.json`), so
  usage charged to more than one spool, such as across a correction, all
  moves. State saved before claims has no record, so the first claim after
  the upgrade moves only usage charged since then. On firmware without
  `unload_count`, the window also restarts at each assignment change.
- **When.** At the next poll, even during a print or while a job waits for
  its first extrusion. The user only claims a spool once they know it's
  loaded, so there's nothing to wait for.
- **Afterwards.** The claimed spool becomes active, the old one is moved out
  of the location, any provisional charge or staging closes, the box is
  cleared, and a write to the printer is queued.
- **Claiming the active spool** confirms it and closes any provisional charge.
  Nothing moves.
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
- **Handled once.** The bridge records each handled claim in `state.json`
  until Spoolman shows the box clear, so a failed clear or dry-run mode
  doesn't handle it again on the next poll.

Possible improvement: Spoolman's websocket pushes an event for every spool
change as it happens. Listening on it would give the order of claims, and
react to Spoolman changes within seconds rather than at the next poll. It
needs a connection kept open alongside polling, and location renames still
send no events.

## Out of scope

The bridge doesn't detect or work around problems in how it's deployed or
configured. Each of these needs fixing in the deployment, not the code.

| Scenario | What happens |
| --- | --- |
| Two bridges against one printer | Every charge is doubled. |
| Two printers' bridges sharing a Spoolman location | Both charge the same spool. |
| Another tool also charging the same spools, such as an OctoPrint or Klipper plugin | Charges are doubled. |
| `state.json` lost, for example with no persistent volume | The bridge starts again from a baseline (B1) and forgets swaps in progress (B4). |
| A wrong location name in the config | The location looks empty, so usage is held as unassigned. |
| The bridge pointed at the wrong printer or Spoolman | Charges go to the wrong place. |

## Questions

- **Q1 (S5). Decided and implemented: provisional charging with a claim field.** A menu reload of the same spool looks the same as loading a different one: both drop the custom name, color and UUID. So the bridge can't tell them apart, and holding the usage would leave Spoolman short after every same-spool reload. Instead:
  1. When a print start closes an unconfirmed swap, the bridge charges the old spool as now, and records the charge as provisional: spool, millimeters and job. Later usage adds to it until it's resolved.
  2. It flags the old spool with how much was charged there provisionally, and how to move it.
  3. The user answers with a claim (see [Claims](#claims)): move the right spool into the location and tick **Claim held usage** on it. Claiming the old spool confirms the charge.
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
- **Q6 (W14). Decided and implemented: remember the last write.** The bridge stores the full `M865` line it last wrote for each tool. If the printer still matches that write and the line built from Spoolman now differs, Spoolman was edited, so the bridge writes it when idle. If the printer no longer matches the write, the printer changed, and that's drift as before. Comparing whole lines catches temperature edits too, which the printer doesn't report back. Tools in state from before this change have no stored line: the bridge rewrites each of them once when idle, even if the printer differs, which fixes the zero temperatures from the old `J`-based writes. A fresh install doesn't do this rewrite.
- **Q7 (M3). Decided and implemented: hold usage for a deleted spool.** When a tool's active spool is missing from Spoolman's list of non-archived spools, the bridge looks it up once. A 404 means it was deleted. A 404 on a queued charge to the active spool means the same. The tool is left with no active spool, and that charge, any usage since an open unconfirmed swap's unload, and later usage go to a per-tool held total in raw extruded mm (`held_mm`, with the deleted spool in `deleted_spool`). Usage held with no spool assigned survives a reboot too (C1), but an unload with no spool active reports it lost (S6). The held total for a deleted spool survives an unload as well. The bridge logs once when it finds the deletion, and flags the spool assigned to the location, if there is one. The next spool that becomes active takes the held usage, charged as "held after spool N was deleted": a spool moved into the location, or a staged spool at the next unload or print start. A typical case is deleting a spool and re-creating it in Spoolman. A claim also takes `held_mm`: the claimed spool becomes active, is charged the held usage, and the deleted state clears. The deleted spool's own charges from before the deletion aren't refunded or moved, since it no longer exists and the charges were accepted when made. That includes a provisional charge (Q1) on it: it's dropped, not held. An archived spool keeps being charged until the next unload, and is flagged "archived but still loaded". Other 4xx rejections stay as they are: logged and dropped, with the ledger as the record.
- **Q8 (M5, M5a). Decided and implemented: log a negative remaining weight; flag a runout with plenty left.**
  - A spool whose remaining weight goes below 0 gets one log line and nothing else. Spool weights are inexact, so many spools end slightly negative. Spoolman itself shows 0 rather than a negative weight, so the bridge works it out from the used weight.
  - An unload during a print, after its first extrusion, while Spoolman shows more than a threshold remaining, flags the spool. That usually means an earlier swap charged the wrong spool. The flag says how much Spoolman thinks is left and suggests checking recent swaps. A jam or tangle that trips the filament sensor, and a planned `M600` color change, look the same to the bridge, so the wording allows for both. The threshold is 5% of the spool's net weight remaining, so it scales with 2 kg and 3 kg spools. A spool with no net weight in Spoolman is never flagged.
  - The flag clears when the user edits the spool's weight in Spoolman, since a corrected weight answers it. It also clears when another spool becomes active in the tool and then this one does. Reloading the same spool keeps it. The bridge keeps the spool's net and used weights with the flag. It moves the used weight on by each of its own charges when the charge's response matches, so any other change to either weight is an edit, even one made while a charge was queued. A charge whose response is lost is sent again and applied twice, so its response doesn't match: that also reads as an edit and clears the flag early, which costs only the flag.
- **Q9 (B4). Decided: out of scope.** The bridge doesn't try to detect or prevent bad deployments. See [Out of scope](#out-of-scope).
