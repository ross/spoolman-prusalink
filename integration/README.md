# Integration tests

These tests run the real bridge, the printer simulator and a real Spoolman
together. The bridge and the simulator run in the test process; Spoolman runs
on its own. Nothing waits on the wall clock: the simulator's time moves only
when a test ticks it, and the bridge polls only when a test calls `poll()`, so
a scenario runs the same way every time and the suite takes about a second.

They aren't part of the default `pytest` run, which collects the unit tests in
`tests/` and `simulator/tests`.

## Run them

Start a Spoolman, with Podman or Docker:

```
podman run -d --name spoolman-it -p 127.0.0.1:7912:8000 ghcr.io/donkie/spoolman:0.27.1
```

Then point the tests at it:

```
SPOOLMAN_URL=http://127.0.0.1:7912 pytest integration
```

Without `SPOOLMAN_URL`, every test is skipped. The `dev` extra
(`pip install -e '.[dev]'`) has everything they need.

## What they assume

- **A Spoolman you can share.** Each test makes its own vendor, filaments and
  spools, and deletes them afterwards, even when it fails. It never touches
  records it didn't make, so a Spoolman that a bridge is already using is
  fine.
- **Unique location names.** Location names are what map a spool to a tool, so
  they're the isolation. Each test picks a prefix such as `it-3fa2c1` and uses
  `<prefix> T` for the tool and `<prefix> shelf` for spare spools. Spool
  comments carry the prefix too, so leftovers can be found after an evicted
  spool loses its location.
- **Extra fields.** The tests register the `instance_uuid` text field if it's
  missing, because spool A is created with a UUID before the bridge runs. The
  bridge creates `bridge_flag` and `bridge_claim` itself.
- **A Core One L.** Every scenario starts with one simulated Core One L
  (`8.1.0`): tool 0 loaded with spool A's PLA and instance UUID, 1000 mm on the
  counter, spool A in the tool's location, and spools B (PETG) and C (ASA) on
  the shelf. Each filament weighs 1000 g, so a runout always leaves plenty.
- **Short phases.** The simulator's settings in `conftest.py` make unloads,
  loads, prints and sync jobs take a second or so. A load counts 36 mm of feed
  and 40 mm of purge.

## The scenarios

Each test is named after its row in `docs/scenarios.md`. The table below is
generated from the tests: the row comes from the name and the description from
the first line of the docstring. Update it with
`python -m integration.table --write`. A unit test checks that the table is
current and that the scenario rows name their tests.

<!-- table:begin -->
| Row | Test | What it checks |
| --- | --- | --- |
| S1 | `test_swaps.py::test_s1_spoolman_first_swap` | Spoolman updated before the unload: the swap commits there, the load is the new spool's. |
| S2 | `test_swaps.py::test_s2_filament_first_swap` | Filament swapped first: the charge is held, then moves once Spoolman is updated. |
| S5 | `test_swaps.py::test_s5_claim_after_printing_on_the_wrong_spool` | Print charged provisionally to the old spool: claiming the loaded one moves it there. |
| S9 | `test_swaps.py::test_s9_staged_spool_never_unloaded` | Staged and never unloaded: print starts and reboots flag it, and a claim makes it active. |
| S13 | `test_swaps.py::test_s13_staged_spool_claimed` | Staged spool, no unload: a claim moves the usage since the reboot and makes it active. |
| S14 | `test_swaps.py::test_s14_several_swaps_between_polls` | Two swaps between polls: two unloads seen, the middle spool is charged nothing. |
| P2 | `test_prints.py::test_p2_runout_with_spoolman_updated_during_pause` | Runout mid-print, Spoolman fixed during the pause: the rest goes to the new spool. |
| P8 | `test_prints.py::test_p8_cancelled_during_preheat_is_not_a_print_start` | Print cancelled in preheat: not a print start, so the open swap is still confirmed. |
| P9 | `test_prints.py::test_p9_restart_after_a_failure_is_a_new_print_start` | A print cancelled and started again under a new job ID is a second print start. |
| P10 | `test_prints.py::test_p10_spoolman_changed_mid_print_is_staged` | Spoolman changed mid-print with no unload: staged, the old spool charged until unload. |
| P11 | `test_prints.py::test_p11_pause_and_resume_changes_nothing` | Pause and resume with no filament change: one print start, and the counter carries on. |
| P12 | `test_prints.py::test_p12_power_panic_resume_keeps_the_job` | Power loss mid-print and resume: a reboot with the same job, so no new print start. |
| W1 | `test_writes.py::test_w1_write_after_swap` | Menu load after a Spoolman-first swap: the bridge writes name, color and UUID. |
<!-- table:end -->

## Add a scenario

1. Find the row in `docs/scenarios.md`. It says what the bridge should do.
   The printer behaviors the scenario leans on are rows in
   `docs/printer-behaviors.md`; if one of them has no trace yet, the test
   stands on the firmware source until it does.
2. Add `test_<row id>_<short name>` to the file for its group, taking the
   `world` fixture. `world` holds the simulator (`sim`, `printer`), the bridge
   and its config, and the Spoolman helper with this test's spool IDs. Give
   the test a docstring whose first line says what it checks.
3. Act as the user would: `w.printer` for the printer (`unload`, `load`,
   `extrude`, `start_print`, `runout`, and the rest in `simulator/model.py`),
   `w.move()` for Spoolman, then `w.advance()` or `w.run_until()` to let time
   pass and `w.poll()` to run the bridge once.
4. Assert on what the bridge left behind: ledger records from `w.poll()` or
   `w.since(mark)` (filter them with `events()` and `charges()`), spools in
   Spoolman (`w.used()`, `w.flag()`, `w.spoolman.extra()`), and the printer
   (`w.material()`, `w.requests()`, `w.sim.events`).
5. Set the row's Status in `docs/scenarios.md` to end to end, naming the test,
   and run `python -m integration.table --write`.
   `pytest tests/test_integration_table.py` checks all three.

Poll where a real bridge would have polled. Some behaviour depends on what two
polls in a row see: the bridge counts a print as started only when the counter
moves between two polls of the same job, for example.
