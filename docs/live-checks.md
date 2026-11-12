# Live checks

What still has to be watched on the Core One L+ and the production Spoolman,
and why. Most of the old sessions in this document are gone: the scenarios
in [scenarios.md](scenarios.md) are compositions of what the printer reports
and what the bridge does with it, and only the printer's part needs hardware.
That part is now [printer-behaviors.md](printer-behaviors.md), which records
each printer behavior as a trace and checks the simulator against it. Once a
scenario's behaviors are traced, its end-to-end test in `integration/` is
the record, and it runs on every CI turn.

This document shrinks as traces land, and goes away when the integration
tests cover every scenario row.

## Sessions done

- **Session 1, 2026-11-07**: W13, S7, S14, W14, S10, S6, S11 and S8, between
  prints. All passed; the scenario rows carry the spool IDs.
- Earlier one-off checks, before the sessions existed: S1 to S4, P1, P2, P6,
  P7, P9, C1, W1, B1 and B2.

No trace was recorded during these. The behaviors they exercised are
recorded again as traces, as printer-behaviors.md says, because a trace is
what the simulator is checked against and a log read by eye isn't.

## What is still live

1. **Traces.** The capture plan in printer-behaviors.md: one idle session,
   one short-print session, and one power cut. That document has the steps.
2. **Power-panic resume (P12).** Part of the power cut in the capture plan.
   The trace shows whether the job ID survives (Q5 in scenarios.md) and what
   the counters and state do on the way back.
3. **The `length_factor` scale check.** Weigh the spool before and after a
   print of 200 g or more and compare with Spoolman's used weight. The first
   attempt was inconclusive on a small print. Nothing in the simulator can
   stand in for this: it's the firmware's counter against a scale.
4. **A second printer.** Only when there is one with this firmware. For the
   foreseeable future that's the INDX upgrade, which also brings multi-tool
   behavior the simulator copies from source only. Cross-printer rows X1 to
   X16 and the multi-tool rows get end-to-end tests meanwhile.

## Rules for anything live

- **Run current `main` on whale**, and note the commit. The homelab deploy
  belongs to the `composed` session; ask it to redeploy.
- **Run only the production bridge.** A second bridge double-charges
  and muddies the logs. The probe in `simulator/probe.py` only
  reads, so it can run beside the bridge.
- **Use a cheap filament**, and a throwaway Spoolman spool where a check
  needs a record you can delete or archive. Move the usage back afterwards.
- **Record**: the date, the bridge commit, the firmware version (the probe
  puts `/api/version` in the trace header), the job and spool IDs, the steps
  with their times, and the bridge log lines from those times
  (`ssh whale sudo /usr/local/bin/docker logs spoolman-prusalink`, or Loki
  with the syslog tag `spoolman-prusalink`).

## When they happen

Don't set these up. Run the probe if there's time, and check the logs and
ledger afterwards:

- **C2**: the printer off overnight.
- **C8**: the next firmware update. A trace of the first boot afterwards is
  worth having.
- **P2** with a real runout at the end of a spool, and **P6**, a jam. Both
  were seen live once. The filament change behavior they share is traced
  from a Tune menu change instead (F8 in printer-behaviors.md).
- **M5**: a spool going past empty in Spoolman.
- **S12**: the printer reports one spool's UUID while Spoolman has another
  active. A menu reload clears the UUID, so it's hard to set up on purpose.

## Not checked live

- **W10, M2**: they need a different printer or a broken Spoolman. The
  simulator has both.
- **M1, M3, M3a, M4, M6, S5, S13, S9, P8, P10, P11, W2 to W9, W11, W12**: bridge
  and Spoolman logic over printer behaviors that are traced or seen. They
  go straight to end-to-end tests, as scenarios.md tracks.
- **X1 to X16**: no second printer; end-to-end tests with two simulated
  printers.
- **B3, B4, M7**: known limits or deployment problems, as scenarios.md says.
