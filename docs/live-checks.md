# Live checks

This is the plan for confirming the scenarios in [scenarios.md](scenarios.md)
on the Core One L+ and the production Spoolman. Unit tests already cover the
bridge's logic. A live check confirms what the tests only assume: what the
firmware reports, what Spoolman's API does, and what the sync job does to the
printer.

Most checks are grouped into sessions, so one sitting at the printer covers
several scenarios. Run the sessions in the order of the [priority
list](#priority).

## Before any session

- **Run current `main` on whale.** Whale ran `4dffd99` as of 2026-11-06.
  Checking older code confirms the wrong thing. The homelab deploy belongs to
  the `composed` session, so ask it to redeploy, and note the commit it runs.
- **Run only the production bridge.** Don't start a second bridge, even in
  dry run, because a second live bridge double-charges and a dry-run one
  muddies the logs. Every check below runs against whale.
- **Use a cheap filament.** Every check charges real usage. Where a check
  needs a spool record you can delete or archive, create a throwaway Spoolman
  spool for the same physical spool, and move its usage back to the real
  record afterwards.

## Recording a check

For each check, record:

- The date, the bridge commit and the firmware version.
- The job IDs and spool IDs involved.
- What you did, step by step, with the time of each step.
- The bridge log lines and ledger entries from those times. Read the logs with
  `ssh whale sudo /usr/local/bin/docker logs spoolman-prusalink`, or in Loki
  with the syslog tag `spoolman-prusalink`.
- Each spool's `used_weight` in Spoolman before and after, and any flags.
- What the printer shows, for checks that write to it.

The check passes when everything matches the scenario's row in
`scenarios.md`. Then change the row's status to `Seen live` with the job or
spool IDs, as the existing rows do. If it fails, open an issue with the
record, add a unit test that reproduces it, and fix it before marking the
row.

A read-only probe that logs PrusaLink's status and filament counters
(`unload_count`, `extruded`, `extruded_at_unload`, the job ID and state) every
few seconds would make the firmware checks in sessions 2 and 3 much easier to
read than 30 s polls. It only reads, so it can run alongside the bridge.
Build it before session 2.

## Priority

Priority comes from how much damage a failure does, how often the scenario
happens, and how much the unit tests had to assume.

| Rank | Scenarios | Damage if wrong | How often | Session |
| --- | --- | --- | --- | --- |
| 1 | W13, W4 | The bed moves on an unhomed printer, or a sync job collides with the user's print | Every swap after a boot; loading then printing is the usual order | 1, 2 |
| 2 | S5, S13 (claims) | A whole print charged to the wrong spool, with no live-tested way to fix it | Whenever Spoolman is forgotten | 2 |
| 3 | S7, S14 | A swap charged to the wrong spool | The usual ways to swap in Spoolman and at the printer | 1 |
| 4 | P8, P9, P11, S9 | A false print start closes a swap early | Every cancelled or paused print | 2 |
| 5 | P5, M5a, W2, P3, P4 | Usage after a color change charged to the wrong spool; missed or early writes | Every multi-color print and every runout | 3 |
| 6 | W14 | A temperature edit never reaches the printer, and nothing reports it | Whenever a spool is edited | 1 |
| 7 | M1 | Usage lost while Spoolman is down | NAS reboots and updates | 4 |
| 8 | S6, S8, S10, S11, P10 | Usage held or flagged when it shouldn't be | Spoolman mistakes | 1, 2 |
| 9 | M3, M3a, M4, M6 | Usage held for a deleted spool or renamed location | Rare | 4 |

## Session 1: between prints

No print runs. About 30 minutes and a few grams of purge.

1. **W13.** Power-cycle the printer, then swap spools so the bridge writes.
   Confirm the sync job homes before its end-of-job move, and that nothing
   hits the bed. Watch the printer the whole time, ready to stop it.
2. **S7.** In Spoolman, move the old spool out of the location, then the new
   one in, then unload and load. The new spool is charged the load.
3. **S14.** A swap at the printer takes a few minutes, so two can't fit
   between two polls while the bridge runs. S14 happens when the bridge can't
   see the printer, such as during a restart or a network outage. To set it
   up, stop the bridge with
   `ssh whale sudo /usr/local/bin/docker stop spoolman-prusalink`. With spool
   A loaded, swap to spool B and then to spool C at the printer. In Spoolman,
   move A out of the location and C in. Then start the bridge. It logs one
   unload event for 2 unloads and charges A with everything up to the second
   unload, including B's purge, because one counter reading can't be split
   between the swaps. The swap commits from A to C, and C is charged with its
   own purge after the second unload. B is charged nothing.
4. **W14.** Edit the loaded spool's color, then its temperatures, in Spoolman.
   Each one starts a sync job. Check the printer's filament menu for the new
   temperatures, because the printer doesn't report them back.
5. **S10, S6.** Move the active spool out with no replacement. It's still
   charged. Unload: usage before the unload goes to that spool, and usage
   after it is held as unassigned. Unload again: the held usage is reported
   lost.
6. **S11.** With no spool active after step 5, put two spools in the
   location. The bridge skips the tool and warns until you remove one.
7. **S8.** Stage a spool, then move it back out before unloading. The staging
   is cancelled and logged.

## Session 2: short prints with interventions

Use a small print, around 10 minutes.

1. **W4.** Swap spools and start the print within a minute. Confirm the sync
   job doesn't interfere with the print, and the write happens after it ends.
2. **P8.** Start a print and cancel it during preheat. Nothing changes in the
   ledger. Then open an unconfirmed swap, start a print, and fix Spoolman
   during preheat: the change confirms the swap.
3. **S5, S13.** Unload, load a different spool, don't change Spoolman, and
   print. After the print starts, move the right spool into the location and
   tick **Claim held usage**. The usage since the unload moves, and the box
   clears. Repeat with the claim made while the job waits for its first
   extrusion.
4. **P11.** Pause and resume a print. Nothing besides charging happens.
5. **P9.** Cancel a print partway and start it again. The second job ID is a
   new print start.
6. **S9, P10.** Stage a spool while a print runs. The old spool is still
   charged. Start a second print with it still staged: both spools are
   flagged at each print start.

## Session 3: filament changes during a print

Use a print with two or more planned color changes, or use **Tune > Change
filament** twice in one print.

1. **P5.** Each change is an unload. Usage after each change goes to the spool
   loaded for it. This also shows whether the firmware counts `M600` as an
   unload, which the tests assume.
2. **M5a.** A change on a nearly full spool flags it.
3. **W2.** Update Spoolman during a change. The write waits for the print to
   end, then runs once.
4. **P3.** In another print, change filament and update Spoolman only after
   the print ends. The update moves the usage since the change.
5. **P4.** Change filament, reload the same spool, and never update Spoolman.
   The next print start closes the swap with a provisional charge.

## Session 4: Spoolman changes

These use the throwaway spool record.

1. **M1.** During a print, stop the Spoolman container for a few minutes, then
   start it. Charges queue and catch up with no loss.
2. **M3a.** Archive the active spool. It's still charged and flagged until the
   next unload.
3. **M3.** Delete the active spool. Usage is held, and the next spool moved in
   takes it.
4. **M4.** Rename the location in Spoolman, then unload and load. The spool
   is charged until the unload, and usage after it is held. Rename the
   location back, and the held usage goes to the spool in it.
5. **M6.** Set a flag on a spool by hand. The bridge leaves it alone.

## When they happen

Don't set these up. Check the logs and ledger when they come up:

- **C2**: the printer off overnight.
- **C8**: the next firmware update.
- **W3**: a sync job refused with 409.
- **M5**: a spool going past empty in Spoolman.
- **P2, P3, P4** with a real runout at the end of a spool.
- **C3, C4, C5, C6, W5, W7, W8, W9**: as they occur.
- **S12**: the printer reports one spool's UUID while Spoolman has another
  active. A menu reload clears the UUID, so this is hard to set up on
  purpose.
- The `length_factor` scale check: weigh the spool before and after a print
  of 200 g or more.

## Not checked live

- **P12**: confirming that a power-panic resume keeps its job ID means
  cutting power mid-print. Either result is harmless (see Q5 in
  `scenarios.md`), so leave it unless a print is lost to a power cut anyway.
- **W10, M2**: they need a different printer or a broken Spoolman.
- **B3, B4, M7**: known limits or deployment problems, as `scenarios.md`
  says.
