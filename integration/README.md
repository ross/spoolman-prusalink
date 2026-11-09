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
  A test that takes `world_tools` at 2
  (`@pytest.mark.parametrize("world_tools", [2], indirect=True)`) gets a
  Core One L INDX (`8.10.0`) instead, with no UUIDs on the printer and tool 1
  loaded with spool D's green PETG in `<prefix> T1`. The `World` helpers that
  act on a tool take a `tool` argument, 0 by default.
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
| W2 | `test_writes.py::test_w2_write_waits_while_printing` | Write due when a print starts: nothing is written until the print ends. |
| W3 | `test_writes.py::test_w3_upload_refused_busy` | Upload refused with 409 when a print starts mid-poll: the next idle poll writes. |
| W4 | `test_writes.py::test_w4_print_soon_after_loading_runs_first` | With sync_delay, a print started right after a menu load runs before the write. |
| W5 | `test_writes.py::test_w5_write_that_does_not_take_is_flagged` | Two sync jobs dropped in a print's start blip: the spool is flagged, no third job. |
| W6 | `test_writes.py::test_w6_sync_job_is_not_a_print_start` | The bridge's sync job, polled while it runs, isn't taken for a print start. |
| W9 | `test_writes.py::test_w9_drift_is_flagged_unless_sync_drift` | Printer changed with no unload: flagged and left alone, or rewritten with sync_drift. |
| W11 | `test_writes.py::test_w11_unconfirmed_swap_writes_the_assigned_spool` | Unload with no Spoolman change, then a menu load: the printer gets A written back. |
| W12 | `test_writes.py::test_w12_staged_spool_waits_for_the_unload` | Write of B due, then C staged: nothing is written until C is unloaded and loaded. |
| W14 | `test_writes.py::test_w14_spoolman_edit_is_written` | A color edit in Spoolman is written to the printer; a name-only edit isn't. |
| M1 | `test_spoolman.py::test_m1_spoolman_unreachable` | Spoolman unreachable: polls skip and log once; queued charges go in order when back. |
| M2 | `test_spoolman.py::test_m2_rejected_charge_is_dropped` | A charge Spoolman rejects with a 4xx is dropped with an error; later ones go through. |
| M3 | `test_spoolman.py::test_m3_charge_to_deleted_spool_is_held` | A charge that 404s because the spool was deleted is held for the next spool. |
| M3 | `test_spoolman.py::test_m3_deleted_active_spool_holds_usage` | Active spool deleted: its usage is held, then charged to the next spool made active. |
| M3a | `test_spoolman.py::test_m3a_archived_active_spool` | Active spool archived: still charged and flagged until the next unload. |
| M4 | `test_spoolman.py::test_m4_location_renamed` | Location renamed: the spool stays active until the next unload, then usage is held. |
| M6 | `test_spoolman.py::test_m6_flags_by_others_left_alone` | A flag someone else wrote stays while the bridge sets and clears its own elsewhere. |
| T1 | `test_tools.py::test_t1_tool_changes_charge_each_tool` | Tool change: each tool's spool gets its own moves; the pickup purge goes to the new tool. |
| T2 | `test_tools.py::test_t2_tool_change_is_not_an_unload` | A tool change isn't an unload: no swap opens on either tool, and nothing is flagged. |
| T3 | `test_tools.py::test_t3_swap_on_one_tool_leaves_the_other` | Swap on tool 1 between prints: tool 1's spool changes, tool 0's state is untouched. |
| T4 | `test_tools.py::test_t4_idle_sensor_clear_on_a_picked_tool` | Picking a tool whose sensor sees no filament counts an unload on that tool only. |
| T5 | `test_tools.py::test_t5_tool_the_printer_does_not_report` | A configured tool the printer doesn't report: noted once, the other tool still charged. |
| T6 | `test_tools.py::test_t6_writes_on_a_multi_tool_model_are_refused` | Writes on a multi-tool model: warned at boot, and a due write flags the spool instead. |
| T7 | `test_tools.py::test_t7_reboot_restarts_every_tool` | Reboot on a multi-tool printer: each tool's counter restarts, and nothing is charged. |
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

To make Spoolman fail, use `w.spoolman_faults`, a transport in front of the
bridge's Spoolman client (`integration/faults.py`). It passes requests through
until the test asks for a fault: `cut()` makes every request fail to connect,
`answer("PUT", "/api/v1/spool/7/use", 503)` replies 503 to the next matching
request without sending it (`times=None` keeps replying), and `restore()` ends
both. Its `requests` list says what each of the bridge's requests got. The same
module has the Spoolman edits the `world` helpers don't cover: deleting and
archiving a spool, writing a text field, and renaming a location.

Poll where a real bridge would have polled. Some behaviour depends on what two
polls in a row see: the bridge counts a print as started only when the counter
moves between two polls of the same job, for example.
