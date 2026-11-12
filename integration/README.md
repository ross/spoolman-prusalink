# Integration tests

These tests run the real bridge, the printer simulator and a real Spoolman
together. The bridge and the simulator run in the test process; Spoolman runs
on its own. Nothing waits on the wall clock: the simulator's time moves only
when a test ticks it, and the bridge polls only when a test calls `poll()`, so
a scenario runs the same way every time and the suite takes about a second.

They aren't part of the default `pytest` run, which collects the unit tests in
`tests/` and `simulator/tests`.

## Run them

```sh
script/integration
```

It starts a throwaway Spoolman container with Podman or Docker on a free
local port, runs the tests against it, and removes it afterwards. Arguments go
to pytest, for example `script/integration -k runout`. `SPOOLMAN_IMAGE`
overrides the image and `CONTAINER_ENGINE` picks `podman` or `docker`.

To use a Spoolman you already have, one that no bridge uses and no other run
shares (see [What they assume](#what-they-assume)), point the tests at it:

```sh
INTEGRATION_SPOOLMAN_URL=http://127.0.0.1:7912 script/integration
```

Without `INTEGRATION_SPOOLMAN_URL`, a plain `pytest integration` skips every test.
The tests never read `SPOOLMAN_URL`, the bridge's own setting, so a shell
set up for a real bridge can't point them at a real Spoolman.

### Coverage

CI reports how much of the bridge the integration tests run, in the job log
and the run summary. There's no floor, and the number doesn't count toward
the unit suite's. A miss in `app.py`, `accounting.py` or `printer_sync.py`
is bridge behavior that unit tests cover but no scenario runs end to end.
Some misses are fine: config parsing and the command line are out of scope
because the tests build `Config` directly. To see the report locally:

```sh
script/integration --cov=spoolman_prusalink --cov-fail-under=0
```

## What they assume

- **A Spoolman no other bridge uses.** Each test makes its own vendor,
  filaments and spools, and deletes them afterwards, even when it fails. One
  bridge per Spoolman is the supported setup, though: a bridge flags any
  claimed spool outside its own tool locations, so a deployed bridge or a
  second test run on the same Spoolman can flag this run's spools, and this
  run's bridges can flag theirs. Don't run the tests against a Spoolman a
  bridge uses, and don't let two runs overlap. `script/integration` without
  `INTEGRATION_SPOOLMAN_URL`, as in CI, starts a fresh Spoolman each run.
- **Unique location names.** Location names are what map a spool to a tool, so
  they're the isolation. Each test picks a prefix such as `it-3fa2c1` and uses
  `<prefix> T` for the tool and `<prefix> shelf` for spare spools. Spool
  comments carry the prefix too, so leftovers can be found after an evicted
  spool loses its location.
- **Extra fields.** The tests register the `instance_uuid` text field if it's
  missing, because spool A is created with a UUID before the bridge runs. The
  bridge creates `bridge_flag` and `bridge_claim` itself. A test that takes
  `world_fields` as `"fresh"`
  (`@pytest.mark.parametrize("world_fields", ["fresh"], indirect=True)`)
  registers nothing, so the bridge sets up every field itself. Fields are
  shared by every spool, so that world gives the bridge keys of the test's own,
  such as `it_3fa2c1_flag`, in `w.fields`, and deletes them afterwards. Spool A
  has no UUID there, in Spoolman or on the printer.
- **A Core One L.** Every scenario starts with one simulated Core One L
  (`8.1.0`): tool 0 loaded with spool A's PLA and instance UUID, 1000 mm on the
  counter, spool A in the tool's location, and spools B (PETG) and C (ASA) on
  the shelf. Each filament weighs 1000 g, so a runout always leaves plenty.
  A test that takes `world_tools` at 2
  (`@pytest.mark.parametrize("world_tools", [2], indirect=True)`) gets a
  Core One L INDX (`8.10.0`) instead, with no UUIDs on the printer and tool 1
  loaded with spool D's green PETG in `<prefix> T1`. The `World` helpers that
  act on a tool take a `tool` argument, 0 by default.
- **Another model, or other settings.** A test that takes `world_model`, such
  as `"1.4.0"` for an MK4, gets that single-tool model instead, with no UUID on
  spool A or the printer. `world_config` overrides the bridge's `Config`, such
  as `flag_field=""`, and `world_printer` overrides the printer's
  `PrinterConfig`, such as `uuids=False`. All three are set with
  `@pytest.mark.parametrize(..., indirect=True)`.
- **A second printer, when asked for.** A test that takes `world_printers`
  at 2 (`@pytest.mark.parametrize("world_printers", [2], indirect=True)`)
  also gets printer `b`, a second Core One L with its own PrusaLink server on
  the same simulation, so ticks move both. Its tool 0 is loaded with spool E's
  white PLA and UUID, and E is in `<prefix> LB`. `w.at("it")` and `w.at("b")`
  give each printer with its location, tool, counter and the bridge's state
  for the tool, and `printer_of(records, "b")` keeps one printer's ledger
  records. These are in `integration/printers.py`.
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
| S1 | `test_configs.py::test_s1_tag_uuid_conflict_is_ignored_without_uuids` | UUIDs off, and the tag's differs from the spool's in Spoolman: no conflict, no flag. |
| S1 | `test_configs.py::test_s1_tag_uuid_is_not_adopted_without_uuids` | UUIDs off, and the new spool's tag has one: nothing is stored, written or flagged. |
| S1 | `test_swaps.py::test_s1_matching_tag_writes_nothing` | Spoolman first, and the new spool's tag already matches: nothing is written. |
| S1 | `test_swaps.py::test_s1_menu_load_with_other_values_is_written` | Spoolman first, and a menu load of another type: Spoolman's spool is written. |
| S1 | `test_swaps.py::test_s1_spoolman_first_swap` | Spoolman updated before the unload: the swap commits there, the load is the new spool's. |
| S1 | `test_swaps.py::test_s1_tag_uuid_conflict_is_flagged` | The new spool has a different UUID in Spoolman: flagged, neither overwritten (#66). |
| S1 | `test_swaps.py::test_s1_tag_uuid_is_adopted` | The new spool has no UUID: it takes the tag's, and nothing is written (#66). |
| S1 | `test_swaps.py::test_s1_tag_uuid_of_an_archived_spool_is_refused` | The tag's UUID belongs to an archived spool: not adopted, logged once, not retried. |
| S1 | `test_swaps.py::test_s1_tag_with_other_values_is_left_alone` | Spoolman first, and the tag differs in name, color and temperatures: not written (Q11). |
| S1 | `test_swaps.py::test_s1_untrusted_tag_is_left_alone` | A tag the bridge didn't see load isn't adopted, written or minted over (#66, Q11). |
| S2 | `test_swaps.py::test_s2_filament_first_swap` | Filament swapped first: the charge is held, then moves once Spoolman is updated. |
| S3 | `test_swaps.py::test_s3_moved_after_the_write_back` | Spoolman updated after the old spool was written back: confirmed, then B is written. |
| S3 | `test_swaps.py::test_s3_old_spool_moved_back` | Old spool moved out and back: the swap stays open, or closes as of a print start. |
| S3 | `test_swaps.py::test_s3_spoolman_changed_during_the_load` | Filament first, old spool out mid-load, new one in mid-print: the new spool takes it all. |
| S3 | `test_swaps.py::test_s3_unload_while_the_location_is_empty` | Unload while the swap's location is empty: closed on the old spool, held usage lost. |
| S4 | `test_swaps.py::test_s4_claiming_the_reloaded_spool_confirms_it` | S4's provisional charge, then the reloaded spool claimed: confirmed, nothing moves. |
| S4 | `test_swaps.py::test_s4_same_spool_reloaded_then_print` | Same spool unloaded and reloaded, then a print: provisional charge, kept at the unload. |
| S5 | `test_configs.py::test_s5_claim_is_ignored_with_claims_off` | claim_field off: a ticked box moves nothing and the flag says to move usage by hand. |
| S5 | `test_swaps.py::test_s5_claim_after_printing_on_the_wrong_spool` | Print charged provisionally to the old spool: claiming the loaded one moves it there. |
| S5 | `test_swaps.py::test_s5_correction_after_printing_on_the_wrong_spool` | Print charged provisionally to the old spool: moving in the tagged one moves it (#107). |
| S6 | `test_swaps.py::test_s6_unload_with_nothing_assigned` | Unload with nothing assigned: the usage before it is lost, the usage after it held. |
| S7 | `test_swaps.py::test_s7_old_spool_out_new_in_then_unload` | Old spool out, new one in, then unload: the old one charged until the unload, new staged. |
| S8 | `test_swaps.py::test_s8_staged_spool_leaves_after_the_active_one` | Active spool moved out, B staged, then B moved out too: A still charged until unload. |
| S8 | `test_swaps.py::test_s8_staged_spool_moved_back_out` | Staged spool moved back out before an unload: staging cancelled, old spool charged. |
| S9 | `test_swaps.py::test_s9_staged_flags_survive_a_restart` | The print-start flags on the active and staged spools outlive a bridge restart. |
| S9 | `test_swaps.py::test_s9_staged_spool_never_unloaded` | Staged and never unloaded: print starts and reboots flag it, and a claim makes it active. |
| S10 | `test_swaps.py::test_s10_spool_moved_out_with_no_replacement` | Spool moved out with no replacement: charged until the unload, then usage is held. |
| S11 | `test_swaps.py::test_s11_ambiguous_location_at_first_start` | Ambiguous at the first poll: no state is kept, and the baseline waits until it's clear. |
| S11 | `test_swaps.py::test_s11_drift_flag_stands_while_the_tool_is_skipped` | A drift flag stays in Spoolman, like its note, while the tool is skipped as ambiguous. |
| S11 | `test_swaps.py::test_s11_drift_note_stands_while_the_tool_is_skipped` | A drift note isn't ended or repeated by polls that skip the tool as ambiguous. |
| S11 | `test_swaps.py::test_s11_two_spools_neither_active` | Two spools in the location, neither active: skipped and warned once until one leaves. |
| S12 | `test_swaps.py::test_s12_spoolman_corrected_to_the_loaded_uuid` | Spoolman changed to the spool whose UUID the printer reports: switched at once. |
| S13 | `test_swaps.py::test_s13_staged_spool_claimed` | Staged spool, no unload: a claim moves the usage since the reboot and makes it active. |
| S14 | `test_swaps.py::test_s14_several_swaps_between_polls` | Two swaps between polls: two unloads seen, the middle spool is charged nothing. |
| P1 | `test_prints.py::test_p1_normal_print` | Normal print: each poll charges the active spool the counter's change since the last. |
| P2 | `test_prints.py::test_p2_runout_reload_keeps_the_old_tag` | A runout reload shows the old spool's tag: the new spool doesn't take its UUID (#66). |
| P2 | `test_prints.py::test_p2_runout_with_spoolman_updated_during_pause` | Runout mid-print, Spoolman fixed during the pause: the rest goes to the new spool. |
| P3 | `test_prints.py::test_p3_runout_with_spoolman_updated_after_the_print` | Runout, Spoolman fixed after the print: the swap stays open, then moves the rest. |
| P4 | `test_prints.py::test_p4_runout_reloaded_with_no_spoolman_change` | Runout reloaded, Spoolman never changed: assumed at the next print, kept at the unload. |
| P5 | `test_prints.py::test_p5_planned_color_changes_are_unloads` | Two `M600` color changes: each an unload, one confirmed in Spoolman, one left open. |
| P6 | `test_prints.py::test_p6_jam_is_an_unconfirmed_swap_and_its_purge_is_charged` | Jam with an unload: an unconfirmed swap, and the reload's longer purge is charged. |
| P7 | `test_prints.py::test_p7_cancel_partway_charges_what_was_extruded` | Print cancelled partway: what was extruded is charged, and nothing else happens. |
| P8 | `test_prints.py::test_p8_cancelled_during_preheat_is_not_a_print_start` | Print cancelled in preheat: not a print start, so the open swap is still confirmed. |
| P9 | `test_prints.py::test_p9_restart_after_a_failure_is_a_new_print_start` | A print cancelled and started again under a new job ID is a second print start. |
| P10 | `test_prints.py::test_p10_spoolman_changed_mid_print_is_staged` | Spoolman changed mid-print with no unload: staged, the old spool charged until unload. |
| P11 | `test_prints.py::test_p11_pause_and_resume_changes_nothing` | Pause and resume with no filament change: one print start, and the counter carries on. |
| P12 | `test_prints.py::test_p12_power_panic_resume_keeps_the_job` | Power loss mid-print and resume: a reboot with the same job, so no new print start. |
| W1 | `test_configs.py::test_w1_custom_preamble_is_written` | A custom preamble with the model's check, or its MMU3 name, starts the job as given. |
| W1 | `test_configs.py::test_w1_write_content` | Flexible, colorless, materialless and RGBA filaments: the M865 line and no false drift. |
| W1 | `test_configs.py::test_w1_write_without_uuids` | No UUIDs to write: an M865 line with no M865.1, and the spool isn't given a UUID. |
| W1 | `test_writes.py::test_w1_uuid_mint_fails` | Storing a minted UUID fails: logged once, and B is written without a UUID. |
| W1 | `test_writes.py::test_w1_uuid_minted_once_spoolman_takes_it` | Storing a minted UUID fails, then works: B gets a UUID and the printer shows it (W1). |
| W1 | `test_writes.py::test_w1_write_after_swap` | Menu load after a Spoolman-first swap: the bridge writes name, color and UUID. |
| W2 | `test_writes.py::test_w2_write_waits_while_printing` | Write due when a print starts: nothing is written until the print ends. |
| W3 | `test_writes.py::test_w3_print_started_before_the_upload` | A print starts between the poll's read and the upload: nothing is uploaded until idle. |
| W3 | `test_writes.py::test_w3_upload_refused_busy` | Upload refused with 409 when a print starts mid-poll: the next idle poll writes. |
| W4 | `test_writes.py::test_w4_print_soon_after_loading_runs_first` | With sync_delay, a print started right after a menu load runs before the write. |
| W5 | `test_writes.py::test_w5_write_that_does_not_take_is_flagged` | Two sync jobs dropped in a print's start blip: the spool is flagged, no third job. |
| W6 | `test_writes.py::test_w6_sync_job_gone_before_its_name_is_read` | The sync job ends between the bridge's status and job reads: still not a print start. |
| W6 | `test_writes.py::test_w6_sync_job_is_not_a_print_start` | The bridge's sync job, polled while it runs, isn't taken for a print start. |
| W7 | `test_configs.py::test_w7_malformed_instance_uuid_is_flagged` | A spool whose instance UUID isn't a UUID: flagged, nothing written until it's fixed. |
| W7 | `test_configs.py::test_w7_spool_with_nothing_to_write_is_flagged` | No material and a name with no usable characters: flagged, written once it has one. |
| W7 | `test_writes.py::test_w7_non_preset_without_temperatures_is_flagged` | Material that isn't a preset, no temperatures: flagged, not written until they're set. |
| W8 | `test_writes.py::test_w8_pps_is_flagged_until_loaded_from_the_menu` | PPS isn't written but flagged; loaded from the printer's menu, the flag clears. |
| W9 | `test_configs.py::test_w9_drift_is_logged_but_not_flagged_with_flags_off` | Flags off: drift is logged and left alone, and the flag field is never written. |
| W9 | `test_configs.py::test_w9_printer_change_beside_an_edit_is_drift` | A spool edited in Spoolman and the printer changed too: drift, so nothing is written. |
| W9 | `test_configs.py::test_w9_tag_of_another_spool_is_not_drift_without_uuids` | UUIDs off, and the tag is another spool's UUID in Spoolman: not drift, not flagged. |
| W9 | `test_writes.py::test_w9_drift_is_flagged_unless_sync_drift` | Printer changed with no unload: flagged and left alone, or rewritten with sync_drift. |
| W9 | `test_writes.py::test_w9_malformed_color_is_drift` | The printer reports a color that isn't a hex color: drift, logged and cleared once. |
| W9 | `test_writes.py::test_w9_temperature_drift_is_flagged_unless_sync_drift` | A temperature changed on the printer is drift too: flagged, or rewritten. |
| W10 | `test_writes.py::test_w10_model_the_bridge_cannot_write` | Unknown, unsupported or mismatched model: logged once, spool flagged, still charged. |
| W10 | `test_writes.py::test_w10_model_unreadable_until_it_answers` | `/api/version` fails while other reads work: writes stay off, logged once each way. |
| W11 | `test_writes.py::test_w11_tag_is_not_written_back` | Unload with no Spoolman change, then a tag load: A isn't written back over it (Q11). |
| W11 | `test_writes.py::test_w11_unconfirmed_swap_writes_the_assigned_spool` | Unload with no Spoolman change, then a menu load: the printer gets A written back. |
| W12 | `test_writes.py::test_w12_staged_spool_waits_for_the_unload` | Write of B due, then C staged: nothing is written until C is unloaded and loaded. |
| W13 | `test_writes.py::test_w13_sync_job_homes_an_unhomed_printer` | Unhomed after a boot: the sync job homes with G28 O before it writes and ends. |
| W14 | `test_configs.py::test_w14_edit_that_leaves_the_spool_unwritable_is_flagged` | An edit to PPS after a write: flagged with why it can't be written, not drift. |
| W14 | `test_writes.py::test_w14_edit_after_the_uuid_is_cleared_keeps_the_printers` | UUID cleared in Spoolman, then a color edit: the rewrite keeps the printer's UUID. |
| W14 | `test_writes.py::test_w14_spoolman_edit_is_written` | A color edit in Spoolman is written to the printer; a name-only edit isn't. |
| W15 | `test_writes.py::test_w15_upload_fails` | Upload fails with a 500 or a dropped connection: logged once, retried each idle poll. |
| M1 | `test_spoolman.py::test_m1_spool_list_fails` | Spool list answers 5xx or bad JSON: polls skip, logged once, not as an outage. |
| M1 | `test_spoolman.py::test_m1_spoolman_unreachable` | Spoolman unreachable: polls skip and log once; queued charges go in order when back. |
| M2 | `test_spoolman.py::test_m2_charge_accepted_with_an_unreadable_reply` | Spoolman applies a charge but its 200 reply isn't JSON: the charge counts as sent. |
| M2 | `test_spoolman.py::test_m2_rejected_charge_is_dropped` | A charge Spoolman rejects with a 4xx is dropped with an error; later ones go through. |
| M3 | `test_spoolman.py::test_m3_404_for_a_spool_no_longer_active_is_dropped` | A 404 on a charge to a spool no longer active is dropped, not held for the next spool. |
| M3 | `test_spoolman.py::test_m3_charge_to_deleted_spool_is_held` | A charge that 404s because the spool was deleted is held for the next spool. |
| M3 | `test_spoolman.py::test_m3_delete_and_recreate` | Deleted spool re-created with the printer's UUID: a correction, charged the held usage. |
| M3 | `test_spoolman.py::test_m3_deleted_active_spool_holds_usage` | Active spool deleted: its usage is held, then charged to the next spool made active. |
| M3 | `test_spoolman.py::test_m3_deleted_during_an_unconfirmed_swap` | Active spool deleted with an unconfirmed swap open: the swap's usage is held once. |
| M3 | `test_spoolman.py::test_m3_deleted_spool_replaced_with_nothing_held` | Active spool deleted, then B moved in before any use: B is active with nothing to charge. |
| M3 | `test_spoolman.py::test_m3_lookup_of_a_missing_spool_fails` | Active spool missing from the list and its lookup fails: charged, logged once. |
| M3 | `test_spoolman.py::test_m3_staged_spool_flag_and_unload` | Active spool deleted with B staged: B's flag says why, and the unload gives B the usage. |
| M3 | `test_spoolman.py::test_m3_staged_spool_takes_held_usage_at_print_start` | Active spool deleted with another staged: a print start makes it active and charges it. |
| M3 | `test_spoolman.py::test_m3_unload_with_no_poll_after_the_extrusion` | Deleted spool found, then extrusion and an unload in one poll: the extrusion is held. |
| M3a | `test_spoolman.py::test_m3a_archived_active_spool` | Active spool archived: still charged and flagged until the next unload. |
| M4 | `test_spoolman.py::test_m4_location_renamed` | Location renamed: the spool stays active until the next unload, then usage is held. |
| M5 | `test_configs.py::test_m5_spool_with_no_weights_and_no_vendor` | No net weight and no vendor: charged and named by the filament, past empty is no flag. |
| M5 | `test_spoolman.py::test_m5_negative_weight_ends` | A negative weight ends with one line when edited, and silently when the spool unloads. |
| M5 | `test_spoolman.py::test_m5_spool_used_past_empty` | Spool used past empty: still charged, a negative weight logged once, no flag. |
| M5a | `test_configs.py::test_m5a_runout_on_a_spool_with_no_net_weight_is_not_flagged` | Runout on a spool with no net weight: unloaded as usual, no flag and no error. |
| M5a | `test_spoolman.py::test_m5a_runout_spool_archived_before_its_refund` | Runout spool archived before a refund to it: the flag follows the refund, no false edit. |
| M5a | `test_spoolman.py::test_m5a_runout_with_plenty_left_is_flagged` | Runout in a started print with over 5% left flags the spool; a weight edit clears it. |
| M5a | `test_spoolman.py::test_m5a_weight_edited_before_a_charge` | A weight edited between the poll's spool read and a charge still clears the runout flag. |
| M6 | `test_configs.py::test_m6_flag_field_off_leaves_every_note` | flag_field off: notes in the field stay on every spool, flagged or not. |
| M6 | `test_spoolman.py::test_m6_flag_write_fails` | Writing a flag fails: logged once, retried each poll, and the write logged when it works. |
| M6 | `test_spoolman.py::test_m6_flagged_spool_gone_while_its_flag_fails` | Spool deleted or archived while its flag fails: end logged once; stale flag open (#120). |
| M6 | `test_spoolman.py::test_m6_flags_by_others_left_alone` | A flag someone else wrote stays while the bridge sets and clears its own elsewhere. |
| M7 | `test_spoolman.py::test_m7_density_edit_applies_to_later_charges` | Density edited in Spoolman: later charges use the new one, earlier aren't recalculated. |
| K1 | `test_swaps.py::test_k1_claim_handled_once_when_clearing_fails` | Clearing a claim's box fails: the claim is handled once, and the failure logged once. |
| K1 | `test_swaps.py::test_k1_claim_ticked_before_the_first_start` | A claim already ticked at the first start: the spool is the baseline's, the box cleared. |
| K2 | `test_swaps.py::test_k2_two_claims_in_one_tool` | Two claims in one tool in one poll: the most recently used spool wins, once. |
| X1 | `test_printers.py::test_x1_spoolman_first_across_printers` | Spool moved LA to LB first: A charges it until its unload, B stages it until its own. |
| X2 | `test_printers.py::test_x2_filament_first_across_printers` | Unload at A, load at B, then move the spool: B's swap confirms, A's stays open and holds. |
| X3 | `test_printers.py::test_x3_spool_moved_but_never_loaded_elsewhere` | Spool moved LA to LB with no unload: A charges it, B stages it and flags it at a print. |
| X4 | `test_printers.py::test_x4_evicted_spool_moves_to_the_other_printer` | A swap on A, the old spool moved into LB before the eviction: it's left there for B. |
| X4 | `test_printers.py::test_x4_failing_eviction_ends_when_the_spool_is_moved` | An eviction Spoolman keeps refusing ends, said once, when the user moves the spool to B. |
| X4a | `test_printers.py::test_x4a_eviction_rereads_the_spool` | Spool moved into LB between the bridge's Spoolman read and its eviction: left in LB. |
| X5 | `test_printers.py::test_x5_claim_on_b_while_a_still_has_the_spool` | Spool claimed in LB while A, off, still has it active: B takes it, A lets go on return. |
| X6 | `test_printers.py::test_x6_one_spool_active_in_two_tools` | A missed an unload over a reboot and B loads the spool: logged once, flagged, both charge. |
| X7 | `test_printers.py::test_x7_two_claims_one_per_printer` | Two spools claimed in one poll, one in LA and one in LB: each claim is its tool's. |
| X8 | `test_printers.py::test_x8_failing_eviction_ends_while_the_printer_is_off` | A's eviction keeps failing, A goes off, the user moves the spool out: ended once. |
| X8 | `test_printers.py::test_x8_one_printer_off` | Printer A off: logged once, B charged, A's eviction runs, A's claim waits for it. |
| X9 | `test_printers.py::test_x9_spoolman_unreachable` | Spoolman down: printers still read; both printers' charges queue in one outbox, in order. |
| X10 | `test_printers.py::test_x10_firmware_without_the_filament_endpoint` | A's firmware lacks the filament endpoint: logged once, nothing charged, B unaffected. |
| X11 | `test_printers.py::test_x11_charge_rejected_with_404` | A 404 on a charge is held for the record's printer and tool, not the other printer's. |
| X14 | `test_printers.py::test_x14_printer_renamed` | A printer renamed: its old state kept but inert, with a warning; the new name baselines. |
| X16 | `test_printers.py::test_x16_runout_flag_clears_when_active_elsewhere` | A runout flag set on A clears when the spool becomes active in B after another spool. |
| T1 | `test_tools.py::test_t1_tool_changes_charge_each_tool` | Tool change: each tool's spool gets its own moves; the pickup purge goes to the new tool. |
| T2 | `test_tools.py::test_t2_tool_change_is_not_an_unload` | A tool change isn't an unload: no swap opens on either tool, and nothing is flagged. |
| T3 | `test_tools.py::test_t3_swap_on_one_tool_leaves_the_other` | Swap on tool 1 between prints: tool 1's spool changes, tool 0's state is untouched. |
| T4 | `test_tools.py::test_t4_idle_sensor_clear_on_a_picked_tool` | Picking a tool whose sensor sees no filament counts an unload on that tool only. |
| T5 | `test_tools.py::test_t5_tool_the_printer_does_not_report` | A configured tool the printer doesn't report: noted once, the other tool still charged. |
| T6 | `test_tools.py::test_t6_writes_on_a_multi_tool_model_are_refused` | Writes on a multi-tool model: warned at boot, and a due write flags the spool instead. |
| T7 | `test_tools.py::test_t7_reboot_restarts_every_tool` | Reboot on a multi-tool printer: each tool's counter restarts, and nothing is charged. |
| C1 | `test_counters.py::test_c1_reboot_keeps_usage_held_at_the_reboot` | Reboot between prints: usage held then is kept, and goes to the next active spool. |
| C1 | `test_counters.py::test_c1_usage_held_over_a_reboot_joins_the_open_swap` | Usage held over a reboot with a swap open: charged to the old spool, moved with it. |
| C2 | `test_counters.py::test_c2_printer_off_for_hours` | Printer off for hours: logged going, hourly and back, idle polls, nothing charged. |
| C3 | `test_counters.py::test_c3_reboot_and_unload_in_one_poll` | Reboot and an unload in the same poll: the reboot is handled first, then the unload. |
| C4 | `test_counters.py::test_c4_counter_goes_backward` | Counter goes backward within one boot: an anomaly, no charge, and later use charged. |
| C5 | `test_counters.py::test_c5_extruded_at_unload_out_of_range` | `extruded_at_unload` past the counter: clamped to it, an anomaly, the unload charged. |
| C6 | `test_counters.py::test_c6_manual_extrude_and_retract` | Extrude and retract from the menu: extrusion charged, a retraction charges nothing. |
| C7 | `test_counters.py::test_c7_tool_reports_no_filament` | Tool reports no filament after a sensor fault: usage held until filament is back. |
| C8 | `test_configs.py::test_c8_firmware_without_usage_counters` | Firmware with no usage counters: logged once, nothing charged, resumes when they're back. |
| C8 | `test_counters.py::test_c8_firmware_update` | Firmware update: a reboot, the model checked again, an unsupported version logged once. |
| C9 | `test_counters.py::test_c9_printer_answers_with_an_error` | Printer answers 401 then 500: each logged once, nothing charged, then caught up. |
| B1 | `test_bridge.py::test_b1_first_start` | First start: the counter is the baseline, and use before it isn't charged. |
| B2 | `test_bridge.py::test_b2_restart_mid_print` | Restart mid-print: one catch-up charge, with no double charge and no loss. |
| B4 | `test_bridge.py::test_b4_database_lost` | The database lost: a new baseline, use since the last poll lost, the swap forgotten. |
| B5 | `test_bridge.py::test_b5_field_setup_fails_then_recovers` | Registering the flag field fails on a fresh Spoolman: logged once, set up when it can. |
| B5 | `test_bridge.py::test_b5_field_with_the_wrong_type` | A field key taken by another type: logged once, no flag written, charging unaffected. |
| B5 | `test_bridge.py::test_b5_fields_registered_on_a_fresh_spoolman` | Fresh Spoolman: the bridge registers its flag, claim and UUID fields as it needs them. |
| B6 | `test_bridge.py::test_b6_second_bridge_on_one_database_is_refused` | A second bridge on a running bridge's database refuses to start; the first carries on. |
| B7 | `test_bridge.py::test_b7_database_that_is_not_the_bridges_is_refused` | A database that's another program's, corrupt or a newer schema: refused, left as it was. |
<!-- table:end -->

## Add a scenario

1. Find the row in `docs/scenarios.md`. It says what the bridge should do.
   The printer behaviors the scenario leans on are rows in
   `docs/printer-behaviors.md`; if one of them has no trace yet, the test
   stands on the firmware source until it does.
2. Add `test_<row id>_<short name>` to the file for its group, taking the
   `world` fixture. A row under another setup, such as a printer with no tag
   reader or a feature turned off, goes in `test_configs.py`, which takes the
   setup from `world_model`, `world_config` and `world_printer`. `world` holds the simulator (`sim`, `printer`), the bridge
   and its config, and the Spoolman helper with this test's spool IDs. Give
   the test a docstring whose first line says what it checks.
3. Act as the user would: `w.printer` for the printer (`unload`, `load`,
   `extrude`, `start_print`, `runout`, and the rest in `simulator/model.py`),
   `w.move()` for Spoolman, then `w.advance()` or `w.run_until()` to let time
   pass and `w.poll()` to run the bridge once.
4. Assert on what the bridge left behind: ledger records from `w.poll()` or
   `w.since(mark)`, which read the database's `ledger` table (filter them with
   `events()` and `charges()`), spools in Spoolman (`w.used()`, `w.flag()`,
   `w.spoolman.extra()`), and the printer (`w.material()`, `w.requests()`,
   `w.sim.events`).
5. Set the row's Status in `docs/scenarios.md` to end to end, naming the test,
   and run `python -m integration.table --write`.
   `pytest tests/test_integration_table.py` checks all three.

To make Spoolman fail, use `w.spoolman_faults`, a transport in front of the
bridge's Spoolman client (`integration/faults.py`). It passes requests through
until the test asks for a fault: `cut()` makes every request fail to connect,
`answer("PUT", "/api/v1/spool/7/use", 503)` replies 503 to the next matching
request without sending it (`times=None` keeps replying, and `containing=`
matches on the request body too). Its `hook`, an async function that gets each
request after the outage check and before the answers, can change Spoolman
between two of the bridge's requests, such as move or delete a spool, and can
return a response to use instead.
`restore()` ends all three. Its `requests` list says what each of the bridge's
requests got, as `"PUT /api/v1/spool/7/use 503"` or `"... unreachable"`. The same
module has the Spoolman edits the `world` helpers don't cover: deleting,
archiving and unarchiving a spool, writing a text field, and renaming a location.

To make the printer fail one request, set `w.before_request`, a function that gets
the method and path of each request before the simulator serves it. It can change
the printer between two of the bridge's requests, as W3 starts a print before the
upload. It can also return an aiohttp response to send instead, such as a 500 for
`/api/version` or a 204 for `/api/v1/job`, or `simulator.prusalink.DROP` to close
the connection with no answer. Returning None serves the request as usual.

Poll where a real bridge would have polled. Some behaviour depends on what two
polls in a row see: the bridge counts a print as started only when the counter
moves between two polls of the same job, for example.
