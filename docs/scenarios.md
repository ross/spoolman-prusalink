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
  [printer-behaviors.md](printer-behaviors.md). Named after the "Seen live"
  or "Tested" status where both apply.
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
| S1 | Spoolman first: move the new spool into the location, then unload and load. | Stages the new spool and keeps charging the old one. At the unload, charges usage before it to the old spool, makes the new one active, and moves the old one out of the location. The load is charged to the new spool. Writes the new spool to the printer when idle. | Tested (live test with spool 18); end to end (`test_s1_spoolman_first_swap`) |
| S2 | Filament first: unload, then change Spoolman, then load. | Opens an unconfirmed swap and flags the old spool. The Spoolman change confirms it and clears the flag. Usage during the load is held until the printer reports a filament, then charged to the new spool. Writes when idle. | Seen live (19 → 14); end to end (`test_s2_filament_first_swap`) |
| S3 | Filament first, but Spoolman changed during the load. | As S2. Usage already charged to the old spool since the unload is moved to the new one. If the old spool is moved out of the location before the new one is moved in, the swap stays open while the location is empty, even through a print start, and later usage is held. The next spool moved in confirms the swap and takes all the usage since the unload. Moving the old spool back in leaves the swap as if the spool never left: open, or, if a print started while it was out, closed as of that print start with a provisional charge (S4). An unload while the location is empty closes the swap with its usage on the old spool, and the usage held since the location emptied is reported lost (S6). | Seen live (14 → 19); the two-step change tested |
| S4 | Unload and reload the same spool, with no Spoolman change, then print. | Opens an unconfirmed swap. The print start closes it as "assumed same spool", records a provisional charge (Q1) and flags the spool with the amount. The next unload keeps the usage there. | Seen live (job 126); provisional charge tested |
| S5 | Unload, load a different spool, forget Spoolman, and print. | As S4: the print is charged to the old spool provisionally, and the flag says how to move it. Moving the right spool into the location and ticking **Claim held usage** on it moves the usage since the unload, makes it active, evicts the old spool and queues a write (see Q1 and [Claims](#claims)). | Decided and implemented; tested; end to end (`test_s5_claim_after_printing_on_the_wrong_spool`) |
| S6 | Unload with nothing assigned in Spoolman. | Usage before the unload is reported lost. Usage after it is held until a spool is assigned. | Seen live (20 → 2 → 7, with spool 2's purge reported lost) |
| S7 | Old spool moved out of the location first, then the new one moved in, then unload. | The old spool stays active until the unload. The new one is staged. If the filament was swapped first, see S3. | Seen live (7 → 14) |
| S8 | Spool staged, then moved back out before any unload. | Staging is cancelled and logged. The active spool keeps being charged. | Seen live (spool 2 staged over spool 7) |
| S9 | Spool staged, and the user never unloads. | It stays staged indefinitely. Each print start logs that the staged spool isn't loaded and flags both the active and the staged spool. A reboot while it's staged flags the staged spool too. Claiming the staged spool makes it active (see Q2). | Decided and implemented; tested; end to end (`test_s9_staged_spool_never_unloaded`) |
| S10 | Spool moved out of the location with no replacement. | It stays active until the next unload, then usage is held as unassigned. During an unconfirmed swap, usage is held at once and the swap stays open (S3). | Seen live (spool 20); the unconfirmed-swap case tested |
| S11 | Two spools in the location, neither active. | Skips the tool and warns until one is removed. | Seen live (spools 2 and 7) |
| S12 | Spoolman changed to a spool whose UUID the printer already reports, with no unload. | Treated as a correction: switches immediately. | Tested |
| S13 | Spoolman changed with no unload, and the printer doesn't report the new spool's UUID. | Staged until the next unload. If the user meant "I got it wrong; this spool is loaded", they claim it (see Q3). | Decided and implemented; tested; end to end (`test_s13_staged_spool_claimed`) |
| S14 | Several unloads and loads between two polls. | Counts them as one unload. Usage up to the last unload goes to the active spool. | Seen live (14 → 7 → 20, with the bridge stopped during the swaps); end to end (`test_s14_several_swaps_between_polls`) |

## During a print

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| P1 | Normal print. | Charges each poll's change in the counter, every 30 s. | Seen live |
| P2 | Runout mid-print, Spoolman updated during the pause. | The runout unload opens an unconfirmed swap. The Spoolman change confirms it. The reload keeps the old spool's metadata on the printer, so a write is queued for when the print ends. | Seen live (19 → 20); the write was missed before `4dffd99`; end to end (`test_p2_runout_with_spoolman_updated_during_pause`) |
| P3 | Runout mid-print, Spoolman updated after the print ends. | The unconfirmed swap stays open: the running job isn't a new print start. The later Spoolman change confirms it and moves the usage since the unload. | Tested |
| P4 | Runout, and the same spool type is reloaded with no Spoolman change ever. | Stays open until the next print start, which closes it as "assumed same spool", records a provisional charge (Q1) and flags the spool with the amount. The next unload keeps the usage there, or a claim moves it. | Tested |
| P5 | Planned color change (`M600` in the G-code). | Same as a runout: each change is an unload. | Tested |
| P6 | Filament jam: feeding stops, `ATTENTION`, unload and reload, purge more. | Treated as an unconfirmed swap. Purge is charged. | Seen live (job 124) |
| P7 | Print cancelled partway. | Charges what was extruded. Nothing special happens. | Seen live (job 124) |
| P8 | Print starts and is cancelled before extruding, during preheat or bed leveling. | Not a print start: a print starts at its first extrusion (see Q4). An unconfirmed swap stays open, so fixing Spoolman during preheat still confirms it. | Tested; end to end (`test_p8_cancelled_during_preheat_is_not_a_print_start`) |
| P9 | Print restarted after a failure (new job ID). | A new print start. | Seen live (job 126); end to end (`test_p9_restart_after_a_failure_is_a_new_print_start`) |
| P10 | Spoolman change mid-print with no unload (scanning the next spool early). | Staged. The old spool is charged until the next unload. | Tested; end to end (`test_p10_spoolman_changed_mid_print_is_staged`) |
| P11 | Pause and resume with no filament change. | Nothing changes. The counter continues. | Implemented; end to end (`test_p11_pause_and_resume_changes_nothing`) |
| P12 | Power loss mid-print, then power-panic resume. | A reboot: counters restart, and usage since the last poll (up to 30 s) is lost. The job ID is kept across reboots, so a resumed job with the same ID isn't a new print start (see Q5). | Tested; the job ID is unconfirmed on the printer; end to end (`test_p12_power_panic_resume_keeps_the_job`) |

## Printer state and the counters

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| C1 | Printer reboots or is power-cycled between prints. | New boot ID: counters restart from 0. Metadata stays on the printer. Usage held at the reboot, because the tool reported no filament, no spool was assigned or the location was ambiguous, is kept. It goes to the active spool as soon as there is one, and is logged then. An unload with no spool active first reports it lost, as in S6. | Seen live; held usage tested; end to end (`test_c1_reboot_keeps_usage_held_at_the_reboot`) |
| C2 | Printer switched off for hours. | Logs once when it goes away, hourly while it's away, and once when it's back. Polls every 60 s. | Tested; end to end (`test_c2_printer_off_for_hours`) |
| C3 | Reboot and an unload in the same poll. | Handled in order: reboot, then unload. | Tested; end to end (`test_c3_reboot_and_unload_in_one_poll`) |
| C4 | Counter goes backward within one boot. | Logs a counter anomaly and resyncs without charging. | Tested; end to end (`test_c4_counter_goes_backward`) |
| C5 | `extruded_at_unload` out of range. | Clamped, and logged as an anomaly. | Tested; end to end (`test_c5_extruded_at_unload_out_of_range`) |
| C6 | Manual extrusion or retraction from the printer menu. | Extrusion is charged to the active spool. The counter only counts filament fed. | Implemented; end to end (`test_c6_manual_extrude_and_retract`) |
| C7 | Tool reports no filament (after an unload, or a sensor problem). | Holds usage until a filament is reported. | Tested; end to end (`test_c7_tool_reports_no_filament`) |
| C8 | Firmware update. | A reboot (C1). The model is checked again. | Implemented; end to end (`test_c8_firmware_update`) |
| C9 | Printer answers with an error (wrong API key, 500). | Logs the error once, until it changes or clears. Charges nothing. | Tested; end to end (`test_c9_printer_answers_with_an_error`) |

## Writing to the printer

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| W1 | Write after a swap, printer idle. | Uploads and runs the sync job: model checks, `G28 O`, `M865` with every parameter, `M865.1` UUID. Gives the spool a UUID first if it has none. | Seen live; end to end (`test_w1_write_after_swap`) |
| W2 | Write due while the printer is printing. | Waits until it's idle. | Tested; end to end (`test_w2_write_waits_while_printing`) |
| W3 | Printer refuses the upload (409 busy). | Waits for the next idle poll. | Tested; end to end (`test_w3_upload_refused_busy`) |
| W4 | User starts a print within a minute of loading. | The write waits until that print ends, so the print runs with the menu's metadata. | Tested; end to end (`test_w4_print_soon_after_loading_runs_first`) |
| W5 | The printer still differs after two sync jobs. | Stops and flags the spool ("didn't take"). | Tested; end to end (`test_w5_write_that_does_not_take_is_flagged`) |
| W6 | The bridge's own sync job. | Not counted as a print start: recognized by its file name. | Tested; end to end (`test_w6_sync_job_is_not_a_print_start`) |
| W7 | Spool's material isn't a preset and Spoolman has no temperatures. | Flags the spool and doesn't write. | Tested |
| W8 | PPS or PPA. | Flags the spool and doesn't write: `M865` can't set the HT idler-door requirement. Once the printer shows the material, loaded from its menu, the flag clears; the color and UUID the menu load leaves out aren't drift. | Tested |
| W9 | Printer differs with no swap or unload behind it (drift). | Flags it and leaves the printer alone, unless `sync_drift = true`. | Tested; end to end (`test_w9_drift_is_flagged_unless_sync_drift`) |
| W10 | Unsupported, unknown or mismatched model. | Logs once, flags waiting spools, and doesn't write. Charging continues. | Tested |
| W11 | Unconfirmed swap open. | Writes the assigned spool, so the printer shows what Spoolman thinks is loaded. | Tested; end to end (`test_w11_unconfirmed_swap_writes_the_assigned_spool`) |
| W12 | Spool staged. | Waits for the unload before writing. | Tested; end to end (`test_w12_staged_spool_waits_for_the_unload`) |
| W13 | Unhomed printer after a boot. | `G28 O` homes it before the job ends, so the end-of-job bed move is safe. | Seen live (2026-11-07: a sync job right after a cold boot homed, then lowered the bed safely) |
| W14 | Spoolman changes the spool's color, name, material, temperatures or UUID with no swap (an edit). | If the printer still matches the bridge's last write, writes the edit when idle (see Q6). Otherwise it's drift (W9). A name-only edit isn't written when the filament has a material: the printer's filament name is then the material, so the spool's name isn't on the printer. With no material, the printer's filament name is the first 7 letters, digits, `_` or `-` of the vendor and name, so a name edit is written only if it changes them. | Seen live (spool 20: a color edit, then two quick temperature edits, each written); end to end (`test_w14_spoolman_edit_is_written`) |

## Spoolman

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| M1 | Spoolman unreachable. | Skips accounting. Queued charges stay in the outbox and are retried in order. | Tested; end to end (`test_m1_spoolman_unreachable`) |
| M2 | Spoolman rejects a charge (4xx). | Drops it with an error, so it doesn't block later charges. | Tested; end to end (`test_m2_rejected_charge_is_dropped`) |
| M3 | Active spool deleted in Spoolman. | Found when the spool drops out of Spoolman's list and a lookup returns 404, or when a charge to it returns 404. The tool is left with no active spool, and the rejected charge, any usage since an open unconfirmed swap's unload, and later usage are held, through reboots and unloads. The held usage is charged to the next spool that becomes active in the tool. A spool already staged is flagged and takes it at the next unload or print start, or when it's claimed. Logged once when found and once when charged (see Q7). | Tested; end to end (`test_m3_deleted_active_spool_holds_usage`); a 404 on a charge end to end (`test_m3_charge_to_deleted_spool_is_held`) |
| M3a | Active spool archived in Spoolman. | Looks moved out of the location (S10): stays active and charged until the next unload. Flagged "archived but still loaded" until then (see Q7). | Tested; end to end (`test_m3a_archived_active_spool`) |
| M4 | Location renamed in Spoolman. | The location looks empty: the spool stays active until the next unload, then usage is held. There's no websocket event for renames. | Implemented; end to end (`test_m4_location_renamed`) |
| M5 | Spool runs past empty in Spoolman. | Keeps charging, and logs once that the spool shows a negative remaining weight. No flag: spool weights are inexact, so slightly negative is normal (see Q8). | Tested |
| M5a | Runout mid-print while Spoolman shows plenty left. | Flags the spool when it unloads during a started print with more than 5% of its net weight left. The flag clears when the user edits the spool's weight in Spoolman, or when another spool becomes active and then this one does. Reloading the same spool with no Spoolman change keeps it (see Q8). A jam or tangle looks the same. | Tested |
| M6 | Flag set by someone else on a spool. | Left alone: the bridge only clears flags it set. A spool the bridge flags itself loses the other text: the bridge's flag replaces it, and clearing that leaves the field empty. | Tested; end to end (`test_m6_flags_by_others_left_alone`) |
| M7 | Spoolman's filament density is wrong. | Charges use the density at the time of each charge. Earlier charges aren't recalculated. | Implemented (Spoolman behavior) |

## The bridge itself

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| B1 | First start. | Takes the current counter as a baseline. Earlier usage isn't charged. | Seen live; end to end (`test_b1_first_start`) |
| B2 | Restart mid-print. | Catches up in one charge, with no double charge and no loss. | Seen live (job 131); end to end (`test_b2_restart_mid_print`) |
| B3 | Killed between a charge being accepted and the outbox being saved. | That charge is sent again: up to one poll's worth, double-charged. | Implemented; known limit |
| B4 | `state.json` lost. | Starts again from a baseline (B1). Usage since the last poll is lost, and swap state is forgotten. | Implemented; a lost state file is a deployment problem (see Out of scope); end to end (`test_b4_state_file_lost`) |
| B6 | Dry run. | Logs what it would charge and write, and records the charges in the ledger. Changes nothing in Spoolman, on the printer or in `state.json`. The next live run charges everything since the last live run, as after an outage (B2), so several spool changes during the dry run count as one unload (S14). Dry-run ledger records, events included, carry `dry_run`. The next run charges the same usage again from the last live state, with its own events and charges, which can differ from the dry run's. | Tested; end to end (`test_b6_dry_run_then_live`) |

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
  until Spoolman shows the box clear, so a failed clear doesn't handle it
  again on the next poll. Dry run records it only in memory, so the next live
  run handles it again.

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

| # | Scenario | What the bridge does | Status |
| --- | --- | --- | --- |
| X1 | Spoolman first: move the spool from `LA` to `LB`, unload at A, load at B. | A: S10. The spool stays active until A's unload, then A holds usage as unassigned. B: S1. The spool is staged until B's unload, then active and charged the load. A's unload and B's load in one poll interval are handled independently, in either order. | Tested; end to end (`test_x1_spoolman_first_across_printers`) |
| X2 | Filament first: unload at A, load at B, then move the spool `LA` to `LB`. | A: S2 then S3. An unconfirmed swap opens and the spool is flagged; the move empties `LA`, so the swap stays open and A holds later usage for the next spool moved in. B: S2. B's unconfirmed swap on its old spool is confirmed by the move, and usage since B's unload moves to the spool. The spool carries A's "moved out" flag while active in B, until something is moved into `LA`. | Tested; end to end (`test_x2_filament_first_across_printers`) |
| X3 | Spool moved `LA` to `LB` in Spoolman while still loaded in A, and never loaded in B. | A: S10, still charged. B: S9, staged and flagged at each B print start. Moving it back to `LA` is S8 on B and resumes A's assignment. | Tested; end to end (`test_x3_spool_moved_but_never_loaded_elsewhere`) |
| X4 | Swap on A evicts the old spool; the user moves it into `LB` and loads it in B. | A: the eviction finds the spool already out of `LA` and clears it. B: S1 or S2 depending on order. A's flag on the spool, if any, clears when A's state no longer mentions it. | Tested; end to end (`test_x4_evicted_spool_moves_to_the_other_printer`) |
| X4a | The user moves the evicted spool into `LB` between the bridge's Spoolman read and its eviction. | `evict()` re-reads the spool and sees it in `LB`, so it doesn't move it. Without the re-read, the bridge would move it to the eviction location, undoing the user's move. The re-read narrows the window to the instant between it and the write; a move in that instant is still undone. | Tested; end to end (`test_x4a_eviction_rereads_the_spool`) |
| X5 | A spool in `LB` is claimed while A still has it active, because A hasn't seen its unload yet (A is off). | B: a claim as in S5, from B's `since_load`. A: when it polls again, a reboot or unload at A ends the spool's time there; usage before it is charged to the spool, which is right, since the filament was in A. | Tested; end to end (`test_x5_claim_on_b_while_a_still_has_the_spool`) |
| X6 | One spool active in two tools: A missed an unload across a reboot (Q2), and the spool is now loaded and active in B. | Logged once naming both locations, and flagged on the spool. A keeps charging it until A's next unload, print-start flag (S9), or a claim on A's staged spool. B charges it normally. | Tested; end to end (`test_x6_one_spool_active_in_two_tools`) |
| X7 | Two spools claimed in one poll, one in `LA` and one in `LB`. | Two claims for two tools: each is handled on its own. The tie-break in Claims applies only within one tool. | Tested; end to end (`test_x7_two_claims_one_per_printer`) |
| X8 | Printer A is off. | A's outage is logged once, hourly, and when it ends (C2). B is polled, charged, and written as normal. Evictions, runout flags, and the shared-spool check still run every tick, including A's pending evictions. A's own tool flags stay in Spoolman as they were when A was last read. Claims on A's tools wait for A. The interval follows B's state. With every printer off, the tick ends after the printer reads, as with one printer. | Tested; end to end (`test_x8_one_printer_off`) |
| X9 | Spoolman is unreachable. | Every printer skips accounting (M1). While the outbox is empty, printer reads still happen, so a printer outage that starts meanwhile is logged on time; with charges queued, the tick stops at the failed flush, as with one printer. Charges from both printers wait in one outbox and are sent in order. | Tested; end to end (`test_x9_spoolman_unreachable`) |
| X10 | A's firmware lacks the filament endpoint (C9, MINI). | A logs once and charges nothing. B is unaffected, including the poll rate: A's status still reads, so A counts as idle or busy by it. | Tested; end to end (`test_x10_firmware_without_the_filament_endpoint`) |
| X11 | A charge from A is rejected with 404 (M3). | Held for A's tool, found by the record's `printer` and `tool`. A spool deleted while active in A and staged in B holds A's usage for A's next spool only. | Tested; end to end (`test_x11_charge_rejected_with_404`) |
| X12 | Removed: printer notices, which showed flags on the printer screen with `M117`. | The bridge sends no notices. Flags are in Spoolman only. | Removed |
| X13 | A version 2 `state.json` with two printers configured. | The bridge refuses to start and says the state file is from one printer. Configure that printer alone under its final name, with dry run off, and let one poll complete with the printer and Spoolman reachable, which rewrites the file as version 3. Then add the second. A dry-run start saves nothing, and a different name later is X14. | Tested; end to end (`test_x13_version_2_state_with_two_printers`) |
| X14 | A printer is renamed in the config. | The old name's state stays in the file, with a warning at each start, but is inert: its pending evictions never run, and a queued charge of its that Spoolman rejects with 404 is dropped, not held. The new name starts from a baseline (B1). | Tested; end to end (`test_x14_printer_renamed`) |
| X15 | Two printers configured with one location, URL, or name. | Config error at start. | Tested (`test_config_rejects_bad_printer_lists`) |
| X16 | A runout flag (M5a) set on A, then the spool becomes active in B after another spool was. | The flag clears, as it does within one tool: the rule is "another spool, then this one" in the tool where it becomes active. A weight edit clears it too. | Tested; end to end (`test_x16_runout_flag_clears_when_active_elsewhere`) |

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
| T1 | A print with tool changes. | Charges each tool's spool the tool's own extrusion. The purge after a pickup counts on the new tool, so it's charged to that tool's spool (F20). | Simulator only; end to end (`test_t1_tool_changes_charge_each_tool`) |
| T2 | A tool change mid-print. | Not an unload: `unload_count` holds on both tools, so no swap opens on either and nothing is flagged. | Simulator only; end to end (`test_t2_tool_change_is_not_an_unload`) |
| T3 | A swap on one tool between prints, Spoolman first (S1). | S1 on that tool alone: the swap commits there and the new spool is charged the load. The other tool's state and spool aren't touched, and the next print charges each tool's spool. A swap can't happen on one tool while another prints: the printer refuses an unload during a print. | Simulator only; end to end (`test_t3_swap_on_one_tool_leaves_the_other`) |
| T4 | Picking a tool whose sensor sees no filament while it has a type, as after filament is pulled out with the printer off (F23). | The firmware counts an unload on that tool only, so the bridge opens an unconfirmed swap there and flags its spool (S2, C7). The other tools aren't touched. | Simulator only; end to end (`test_t4_idle_sensor_clear_on_a_picked_tool`) |
| T5 | A configured tool the printer doesn't report: disabled, or its dock not calibrated (F19). | Logs `printer doesn't report tool index N` once and skips that tool. The other tools are charged as normal. When the tool comes back, it carries on from its counter. | Simulator only; end to end (`test_t5_tool_the_printer_does_not_report`) |
| T6 | A write due on a multi-tool printer. | Writes are off for every multi-tool model: the bridge logs the model once per boot ("multi-tool printers aren't supported yet; printer writes are off"), and when a write would be due on a tool, flags the spool "can't write to the printer" and uploads nothing. Charging is unaffected (W10). This is the current behavior; writing per tool with `M865 L<n>` and no `M865.1` (F21, F22) is a pending decision. | Simulator only; end to end (`test_t6_writes_on_a_multi_tool_model_are_refused`) |
| T7 | A multi-tool printer reboots between prints. | A reboot (C1) on every tool: each tool logs it and its counter restarts at 0, nothing is charged for the restart, and each tool's spool is then charged from 0. | Simulator only; end to end (`test_t7_reboot_restarts_every_tool`) |
| T8 | One location mapped to two tools of one printer. | Config error at start (X15 covers two printers). | Tested (`test_config_rejects_bad_printer_lists`) |

## Out of scope

The bridge doesn't detect or work around problems in how it's deployed or
configured. Each of these needs fixing in the deployment, not the code.

| Scenario | What happens |
| --- | --- |
| Two bridges against one printer | Every charge is doubled. |
| Two bridges whose printers share a Spoolman location | Both charge the same spool. Within one bridge, a location mapped to tools on two printers is a config error. |
| Another tool also charging the same spools, such as an OctoPrint or Klipper plugin | Charges are doubled. |
| `state.json` lost, for example with no persistent volume | The bridge starts again from a baseline (B1) and forgets swaps in progress (B4). |
| A wrong location name in the config | The location looks empty, so usage is held as unassigned. |
| The bridge pointed at the wrong printer or Spoolman | Charges go to the wrong place. |
| A sync job uploaded while the user is on the print preview screen | The preview reads `IDLE` with no job, so the bridge sees an idle printer, and the firmware replaces the pending print with the sync job (F15 in [printer-behaviors.md](printer-behaviors.md)). The user starts the print again afterwards. `sync_delay` narrows the window; the API can't show the preview, so nothing closes it. Accepted 2026-11-08. |

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
- **Q6 (W14). Decided and implemented: remember the last write.** The bridge stores the full `M865` line it last wrote for each tool. If the printer still matches that write and the line built from Spoolman now differs, Spoolman was edited, so the bridge writes it when idle. If the printer no longer matches the write, the printer changed, and that's drift as before. Comparing whole lines catches temperature edits too, which the printer doesn't report back. Tools in state from before this change have no stored line: the bridge rewrites each of them once when idle, even if the printer differs, which fixes the zero temperatures from the old `J`-based writes. A fresh install doesn't do this rewrite. A color, material or UUID edit, or a name edit on a filament with no material, counts as applied only when a poll sees the printer match (see W5). The printer doesn't report temperatures, so a temperature-only edit counts as applied once its job uploads: if the user cancels the job before its `M865` runs, the edit is lost, and nothing retries or flags it. Detecting a cancelled sync job is out of scope.
- **Q7 (M3). Decided and implemented: hold usage for a deleted spool.** When a tool's active spool is missing from Spoolman's list of non-archived spools, the bridge looks it up once. A 404 means it was deleted. A 404 on a queued charge to the active spool means the same. The tool is left with no active spool, and that charge, any usage since an open unconfirmed swap's unload, and later usage go to a per-tool held total in raw extruded mm (`held_mm`, with the deleted spool in `deleted_spool`). Usage held with no spool assigned survives a reboot too (C1), but an unload with no spool active reports it lost (S6). The held total for a deleted spool survives an unload as well. The bridge logs once when it finds the deletion, and flags the spool assigned to the location, if there is one. The next spool that becomes active takes the held usage, charged as "held after spool N was deleted": a spool moved into the location, or a staged spool at the next unload or print start. A typical case is deleting a spool and re-creating it in Spoolman. A claim also takes `held_mm`: the claimed spool becomes active, is charged the held usage, and the deleted state clears. The deleted spool's own charges from before the deletion aren't refunded or moved, since it no longer exists and the charges were accepted when made. That includes a provisional charge (Q1) on it: it's dropped, not held. An archived spool keeps being charged until the next unload, and is flagged "archived but still loaded". Other 4xx rejections stay as they are: logged and dropped, with the ledger as the record.
- **Q8 (M5, M5a). Decided and implemented: log a negative remaining weight; flag a runout with plenty left.**
  - A spool whose remaining weight goes below 0 gets one log line and nothing else. Spool weights are inexact, so many spools end slightly negative. Spoolman itself shows 0 rather than a negative weight, so the bridge works it out from the used weight.
  - An unload during a print, after its first extrusion, while Spoolman shows more than a threshold remaining, flags the spool. That usually means an earlier swap charged the wrong spool. The flag says how much Spoolman thinks is left and suggests checking recent swaps. A jam or tangle that trips the filament sensor, and a planned `M600` color change, look the same to the bridge, so the wording allows for both. The threshold is 5% of the spool's net weight remaining, so it scales with 2 kg and 3 kg spools. A spool with no net weight in Spoolman is never flagged.
  - The flag clears when the user edits the spool's weight in Spoolman, since a corrected weight answers it. It also clears when another spool becomes active in the tool and then this one does. Reloading the same spool keeps it. The bridge keeps the spool's net and used weights with the flag. It moves the used weight on by each of its own charges when the charge's response matches, so any other change to either weight is an edit, even one made while a charge was queued. A charge whose response is lost is sent again and applied twice, so its response doesn't match: that also reads as an edit and clears the flag early, which costs only the flag.
- **Q9 (B4). Decided: out of scope.** The bridge doesn't try to detect or prevent bad deployments. See [Out of scope](#out-of-scope).
