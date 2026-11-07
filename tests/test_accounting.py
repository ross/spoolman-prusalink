import json
from dataclasses import replace

from spoolman_prusalink.accounting import (
    Charge,
    EventKind,
    Notice,
    Provisional,
    SwapFlag,
    ToolState,
    account,
    hold_deleted,
    resolve_assignment,
)
from spoolman_prusalink.models import Material, ToolReading

BOOT = 111
PLA = Material("PLA", "#73737b")


def r(extruded, material=PLA, unloads=0, at_unload=0):
    """A reading from firmware with the unload fields."""
    return ToolReading(0, extruded, material, unloads, at_unload)


def legacy(extruded, material=PLA):
    """A reading from firmware without the unload fields."""
    return ToolReading(0, extruded, material)


def loc(*ids):
    return list(ids)


def run(steps, state=None, matches=lambda _id: False):
    """Apply (boot_id, reading, location_ids[, job_id]) steps.

    Returns all charges, all event kinds, and the final state.
    """
    state = state or ToolState()
    charges, kinds = [], []
    for step in steps:
        boot, reading, ids, *job = step
        res = account(state, boot, reading, ids, job[0] if job else None, matches)
        charges += res.charges
        kinds += [e.kind for e in res.events]
        state = res.state
    return charges, kinds, state


# Counters


def test_first_poll_sets_baseline_without_charging():
    charges, kinds, state = run([(BOOT, r(4310), loc(7))])
    assert charges == []
    assert kinds == [EventKind.BASELINE]
    assert (state.charged, state.active, state.assigned) == (4310, 7, 7)


def test_steady_printing_charges_deltas():
    charges, _, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(150), loc(7)),
        (BOOT, r(150), loc(7)),
        (BOOT, r(400), loc(7)),
    ])
    assert charges == [Charge(7, 50, "extruded"), Charge(7, 250, "extruded")]
    assert state.charged == 400


def test_reboot_restarts_from_zero_not_unknown():
    charges, _, state = run([
        (BOOT, r(1000), loc(7)),
        (BOOT, r(1200), loc(7)),
        (222, r(30), loc(7)),
    ])
    assert charges == [Charge(7, 200, "extruded"), Charge(7, 30, "extruded")]
    assert (state.boot_id, state.charged) == (222, 30)


def test_resumed_job_after_reboot_is_not_a_print_start():
    # Power loss mid-print with an unconfirmed swap open, then a power-panic resume
    # that keeps the job ID.
    _, kinds, state = run([
        (BOOT, r(1000), loc(7), 131),
        (BOOT, r(1100, unloads=1, at_unload=1050), loc(7), 131),
        (222, r(40), loc(7), 131),
    ])
    assert EventKind.SWAP_ASSUMED not in kinds
    assert state.swap is not None and state.job_id == 131


def test_reboot_resets_unload_count():
    _, _, state = run([
        (BOOT, r(100, unloads=3, at_unload=50), loc(7)),
        (222, r(10), loc(7)),
    ])
    assert state.unload_count == 0


def test_reboot_and_unload_in_same_poll():
    charges, _, state = run([
        (BOOT, r(5000), loc(7)),
        (222, r(300, unloads=1, at_unload=100), loc(7, 8)),
    ])
    assert charges == [Charge(7, 100, "before unload"), Charge(8, 200, "extruded")]
    assert (state.unload_count, state.boot_id, state.active) == (1, 222, 8)


def test_counter_going_backwards_resyncs():
    res = account(ToolState(BOOT, 500, 0, 7, 7), BOOT, r(400), loc(7))
    assert res.charges == []
    assert res.state.charged == 400
    assert EventKind.COUNTER_ANOMALY in [e.kind for e in res.events]


def test_bad_extruded_at_unload_is_clamped():
    res = account(ToolState(BOOT, 500, 0, 7, 7), BOOT, r(900, unloads=1, at_unload=2000),
                  loc(7))
    assert EventKind.COUNTER_ANOMALY in [e.kind for e in res.events]
    assert sum(c.mm for c in res.charges) == 400


def test_multiple_unloads_between_polls():
    charges, _, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(900, unloads=3, at_unload=800), loc(8)),
    ])
    assert charges == [Charge(7, 700, "before unload"), Charge(8, 100, "extruded")]
    assert state.unload_count == 3


# Spoolman first: staged swaps


def test_staged_swap_keeps_charging_old_spool_until_unload():
    charges, kinds, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(200), loc(7, 8)),  # spool 8 scanned into the location
        (BOOT, r(300), loc(7, 8)),
        (BOOT, r(420, material=None, unloads=1, at_unload=350), loc(7, 8)),  # unloaded
        (BOOT, r(536, unloads=1, at_unload=350), loc(7, 8)),  # spool 8 loaded
    ])
    assert charges == [
        Charge(7, 100, "extruded"),
        Charge(7, 100, "extruded"),
        Charge(7, 50, "before unload"),
        Charge(8, 186, "extruded"),
    ]
    assert kinds.count(EventKind.SWAP_STAGED) == 1
    assert EventKind.SWAP_COMMITTED in kinds
    assert (state.active, state.assigned, state.evict) == (8, 8, 7)


def test_evicted_spool_is_ignored_then_forgotten():
    _, kinds, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(100), loc(7, 8)),
        (BOOT, r(200, unloads=1, at_unload=100), loc(7, 8)),
        (BOOT, r(300, unloads=1, at_unload=100), loc(7, 8)),  # 7 still in the location
    ])
    assert EventKind.AMBIGUOUS not in kinds
    assert (state.active, state.evict) == (8, 7)
    _, _, state = run([(BOOT, r(300, unloads=1, at_unload=100), loc(8))], state)
    assert state.evict is None


def test_old_spool_moved_out_first_then_new_staged():
    # The user moves the old spool out, then scans the new one in, before unloading.
    charges, kinds, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(150), loc()),
        (BOOT, r(200), loc(8)),
        (BOOT, r(260, unloads=1, at_unload=230), loc(8)),
    ])
    assert charges == [
        Charge(7, 50, "extruded"),
        Charge(7, 50, "extruded"),
        Charge(7, 30, "before unload"),
        Charge(8, 30, "extruded"),
    ]
    assert kinds.count(EventKind.SWAP_STAGED) == 2
    assert (state.active, state.evict) == (8, None)


def test_spool_removed_from_location_stays_active_until_unload():
    charges, _, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(200), loc()),
        (BOOT, r(260, material=None, unloads=1, at_unload=230), loc()),
        (BOOT, r(300, material=None, unloads=1, at_unload=230), loc()),
    ])
    assert charges == [Charge(7, 100, "extruded"), Charge(7, 30, "before unload")]
    assert state.active is None and state.charged == 230


def test_correction_by_uuid_switches_immediately():
    charges, kinds, state = run(
        [(BOOT, r(100), loc(7)), (BOOT, r(150), loc(7, 9)), (BOOT, r(180), loc(7, 9))],
        matches=lambda spool_id: spool_id == 9,
    )
    assert EventKind.CORRECTION in kinds
    assert charges == [Charge(9, 50, "extruded"), Charge(9, 30, "extruded")]
    assert (state.active, state.evict) == (9, 7)


def test_print_start_with_staged_swap_warns_and_keeps_old_spool():
    charges, kinds, state = run([
        (BOOT, r(100), loc(7), None),
        (BOOT, r(100), loc(7, 8), None),
        (BOOT, r(400), loc(7, 8), 42),  # job seen; it starts at its first extrusion
        (BOOT, r(700), loc(7, 8), 42),
    ])
    assert EventKind.STAGED_AT_PRINT in kinds
    assert charges == [Charge(7, 300, "extruded"), Charge(7, 300, "extruded")]
    assert state.active == 7


def test_job_cancelled_before_extruding_is_not_a_print_start():
    # Unload, then a print that's cancelled during preheat: the swap stays open, so a
    # Spoolman change afterwards still confirms it.
    _, kinds, state = run([
        (BOOT, r(100), loc(7), None),
        (BOOT, r(200, unloads=1, at_unload=100), loc(7), None),
        (BOOT, r(200, unloads=1, at_unload=100), loc(7), 42),
        (BOOT, r(200, unloads=1, at_unload=100), loc(7), 42),
        (BOOT, r(200, unloads=1, at_unload=100), loc(7), None),
        (BOOT, r(200, unloads=1, at_unload=100), loc(9), None),
    ])
    assert EventKind.SWAP_ASSUMED not in kinds
    assert EventKind.SWAP_CONFIRMED in kinds
    assert (state.active, state.job_id, state.pending_job) == (9, None, None)


def test_spoolman_fixed_during_preheat_confirms_swap():
    _, kinds, state = run([
        (BOOT, r(100), loc(7), None),
        (BOOT, r(200, unloads=1, at_unload=100), loc(7), None),
        (BOOT, r(200, unloads=1, at_unload=100), loc(7), 42),  # preheating
        (BOOT, r(200, unloads=1, at_unload=100), loc(9), 42),  # user fixes Spoolman
        (BOOT, r(260, unloads=1, at_unload=100), loc(9), 42),  # purge line
    ])
    assert EventKind.SWAP_CONFIRMED in kinds
    assert EventKind.SWAP_ASSUMED not in kinds
    assert (state.active, state.job_id) == (9, 42)


def test_two_spools_neither_active_is_ambiguous():
    res = account(ToolState(BOOT, 100, 0, 7, 7), BOOT, r(200), loc(8, 9))
    assert res.skipped and res.charges == []
    assert res.state == ToolState(BOOT, 100, 0, 7, 7, extruded=200)  # Held usage.


def test_resolve_assignment():
    s = ToolState(active=7, evict=5)
    assert resolve_assignment(s, []) is None
    assert resolve_assignment(s, [5]) is None
    assert resolve_assignment(s, [7]) == 7
    assert resolve_assignment(s, [7, 8]) == 8
    assert resolve_assignment(s, [5, 7, 8]) == 8
    assert resolve_assignment(s, [8, 9]) == [8, 9]
    assert resolve_assignment(s, [7, 8, 9]) == [7, 8, 9]


# Filament first: unconfirmed swaps


def test_unload_without_spoolman_change_opens_swap():
    charges, kinds, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(900, unloads=1, at_unload=600), loc(7)),
    ])
    assert charges == [Charge(7, 500, "before unload"), Charge(7, 300, "extruded")]
    assert EventKind.SWAP_UNCONFIRMED in kinds
    assert state.swap == SwapFlag(7, 300)


def test_confirming_swap_before_print_moves_post_swap_usage():
    charges, kinds, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(900, unloads=1, at_unload=600), loc(7)),  # 300 to 7, flagged
        (BOOT, r(1000, unloads=1, at_unload=600), loc(7)),  # 100 more to 7
        (BOOT, r(1050, unloads=1, at_unload=600), loc(7, 9)),  # user scans 9 in
    ])
    assert charges[-3:] == [
        Charge(9, 400, "usage since unconfirmed swap"),
        Charge(7, -400, "moved to confirmed swap spool"),
        Charge(9, 50, "extruded"),
    ]
    assert EventKind.SWAP_CONFIRMED in kinds
    assert (state.swap, state.active, state.evict) == (None, 9, 7)


def test_print_start_closes_swap_as_same_spool():
    # Unload to clear a jam, reload the same spool, print. A later scan is a new swap.
    charges, kinds, state = run([
        (BOOT, r(100), loc(7), None),
        (BOOT, r(220, unloads=1, at_unload=100), loc(7), None),
        (BOOT, r(5000, unloads=1, at_unload=100), loc(7), 42),
        (BOOT, r(9000, unloads=1, at_unload=100), loc(7), 42),
        (BOOT, r(9000, unloads=1, at_unload=100), loc(7, 9), None),
    ])
    assert EventKind.SWAP_ASSUMED in kinds
    assert EventKind.SWAP_CONFIRMED not in kinds
    assert EventKind.SWAP_STAGED in kinds
    assert all(c.mm > 0 and c.spool_id == 7 for c in charges)
    assert (state.active, state.assigned, state.swap) == (7, 9, None)


def test_same_job_still_running_does_not_close_swap():
    # A runout mid-print: the job id doesn't change, so the window stays open.
    _, kinds, state = run([
        (BOOT, r(100), loc(7), 42),
        (BOOT, r(900, unloads=1, at_unload=600), loc(7), 42),
        (BOOT, r(1200, unloads=1, at_unload=600), loc(7), 42),
    ])
    assert EventKind.SWAP_ASSUMED not in kinds
    assert state.swap == SwapFlag(7, 600)


def test_next_unload_supersedes_open_swap():
    _, _, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(300, unloads=1, at_unload=200), loc(7)),
        (BOOT, r(500, unloads=2, at_unload=400), loc(7)),
    ])
    assert state.swap == SwapFlag(7, 100)


def test_swap_flag_cleared_when_spool_removed():
    charges, _, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(900, unloads=1, at_unload=600), loc(7)),
        (BOOT, r(950, unloads=1, at_unload=600), loc()),
    ])
    assert state.swap is None and state.active is None
    assert all(c.mm > 0 for c in charges)


def test_measured_reboot_then_menu_unload_and_load():
    # Sequence measured on the Core One L+ (2026-11-04): reboot, menu unload, menu load
    # of the PLA preset (color and UUID dropped), 116 mm counted by the load.
    old, new = 3308742886, 3018224650
    plain = Material("PLA")
    charges, _, state = run([
        (old, r(43812), loc(7)),
        (new, r(0), loc(7)),  # reboot
        (new, r(0, material=None, unloads=1, at_unload=0), loc(7)),  # unloaded
        (new, r(116, material=plain, unloads=1, at_unload=0), loc(7)),  # loaded
    ])
    assert charges == [Charge(7, 116, "extruded")]
    assert state.swap == SwapFlag(7, 116)

    # The user then scans the new spool into the location: the load moves to it.
    charges, _, state = run([(new, r(116, material=plain, unloads=1), loc(7, 9))], state)
    assert charges == [
        Charge(9, 116, "usage since unconfirmed swap"),
        Charge(7, -116, "moved to confirmed swap spool"),
    ]
    assert state.swap is None


# Holding


def test_empty_tool_holds_usage_until_material_returns():
    charges, _, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(700, material=None, unloads=1, at_unload=600), loc()),
        (BOOT, r(760, material=None, unloads=1, at_unload=600), loc(8)),
        (BOOT, r(800, unloads=1, at_unload=600), loc(8)),
    ])
    assert charges == [Charge(7, 500, "before unload"), Charge(8, 200, "extruded")]
    assert state.charged == 800


def test_unassigned_location_holds_usage():
    charges, _, state = run([
        (BOOT, r(100), loc()),
        (BOOT, r(300), loc()),
        (BOOT, r(350), loc(4)),
    ])
    assert charges == [Charge(4, 250, "extruded")]
    assert state.active == 4


def test_unload_without_spool_is_reported_lost():
    res = account(ToolState(BOOT, 100, 0, None, None), BOOT, r(900, unloads=1, at_unload=600),
                  loc(5))
    assert EventKind.LOST_UNASSIGNED in [e.kind for e in res.events]
    assert res.charges == [Charge(5, 300, "extruded")]


def test_unassigned_usage_held_through_reboot_goes_to_next_spool():
    charges, kinds, state = run([
        (BOOT, r(100), loc()),
        (BOOT, r(5000), loc()),
        (BOOT, r(9000), loc()),
        (BOOT + 1, r(10), loc(7)),  # Reboot, then Spoolman fixed.
    ])
    assert charges == [Charge(7, 8900, "held before the printer restarted"),
                       Charge(7, 10, "extruded")]
    assert EventKind.HELD_CHARGED in kinds
    assert (state.active, state.held_mm, state.charged) == (7, 0, 10)


def test_usage_held_through_reboot_is_kept_until_a_spool_is_active():
    _, _, state = run([
        (BOOT, r(100), loc()),
        (BOOT, r(900), loc()),
        (BOOT + 1, r(10), loc()),
        (BOOT + 1, r(50), loc()),
    ])
    assert (state.held_mm, state.charged) == (800, 0)
    restored = ToolState.from_json(json.loads(json.dumps(state.to_json())))
    charges, _, state = run([(BOOT + 1, r(60), loc(7))], restored)
    assert charges == [Charge(7, 800, "held before the printer restarted"),
                       Charge(7, 60, "extruded")]
    assert state.held_mm == 0


def test_empty_tool_usage_held_through_reboot_counts_toward_open_swap():
    # Unload, reboot during the load, then Spoolman is fixed: the load moves too.
    charges, _, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(700, material=None, unloads=1, at_unload=600), loc(7)),
        (BOOT, r(760, material=None, unloads=1, at_unload=600), loc(7)),
        (BOOT + 1, r(20), loc(7)),  # Reboot, the load done.
    ])
    assert charges == [Charge(7, 500, "before unload"),
                       Charge(7, 160, "held before the printer restarted"),
                       Charge(7, 20, "extruded")]
    assert state.swap == SwapFlag(7, 180)
    charges, _, _ = run([(BOOT + 1, r(20), loc(7, 9))], state)
    assert charges == [Charge(9, 180, "usage since unconfirmed swap"),
                       Charge(7, -180, "moved to confirmed swap spool")]


def test_usage_held_through_reboot_is_lost_at_unload_with_no_spool():
    _, _, state = run([(BOOT, r(100), loc()), (BOOT, r(900), loc())])
    res = account(state, BOOT + 1, r(50, unloads=1, at_unload=30), loc())
    lost = [e.message for e in res.events if e.kind == EventKind.LOST_UNASSIGNED]
    assert lost == ["830 mm before unload had no spool"]
    assert res.charges == [] and res.state.held_mm == 0


def test_usage_during_ambiguous_location_survives_reboot():
    _, _, state = run([(BOOT, r(100), loc(7)), (BOOT, r(200), loc(7))])
    res = account(state, BOOT, r(700), loc(7, 8, 9))
    assert res.skipped
    charges, _, _ = run([(BOOT + 1, r(10), loc(7))], res.state)
    assert charges == [Charge(7, 500, "held before the printer restarted"),
                       Charge(7, 10, "extruded")]


def test_hold_messages_stay_the_same_while_holding():
    _, _, state = run([(BOOT, r(100), loc())])
    first = account(state, BOOT, r(200), loc())
    second = account(first.state, BOOT, r(300), loc())
    assert first.events == second.events
    _, _, state = run([(BOOT, r(100), loc(7))])
    first = account(state, BOOT, r(200, material=None), loc(7))
    second = account(first.state, BOOT, r(300, material=None), loc(7))
    assert first.events == second.events


def test_state_without_last_reading_holds_nothing_over_reboot():
    old = ToolState(BOOT, 100, 0, None, None).to_json()
    del old["extruded"]
    state = ToolState.from_json(old)
    charges, _, state = run([(BOOT + 1, r(10), loc(7))], state)
    assert charges == [Charge(7, 10, "extruded")] and state.held_mm == 0


# Firmware without unload fields


def test_legacy_assignment_change_switches_after_charging_outgoing():
    charges, kinds, state = run([
        (BOOT, legacy(100), loc(7)),
        (BOOT, legacy(250), loc(7, 8)),
        (BOOT, legacy(300), loc(8)),
    ])
    assert charges == [Charge(7, 150, "extruded"), Charge(8, 50, "extruded")]
    assert EventKind.SWAP_STAGED not in kinds
    assert (state.active, state.evict) == (8, None)


# Persistence


def test_state_round_trip():
    s = ToolState(BOOT, 123, 2, 7, 8, 5, SwapFlag(7, 40), 42)
    assert ToolState.from_json(s.to_json()) == s


def test_held_usage_round_trips_and_old_state_has_none():
    s = ToolState(BOOT, 123, 2, None, None, held_mm=300, deleted_spool=19)
    assert ToolState.from_json(s.to_json()) == s
    old = s.to_json()
    del old["held_mm"], old["deleted_spool"]
    restored = ToolState.from_json(old)
    assert (restored.held_mm, restored.deleted_spool) == (0, None)


def test_restart_from_saved_state_neither_double_charges_nor_drops():
    _, _, state = run([(BOOT, r(100), loc(7)), (BOOT, r(200), loc(7))])
    restored = ToolState.from_json(state.to_json())
    charges, _, _ = run([(BOOT, r(260), loc(7))], restored)
    assert charges == [Charge(7, 60, "extruded")]


def test_unstaging_before_unload_is_logged_and_keeps_active():
    charges, kinds, state = run([
        (BOOT, r(100), loc(19)),
        (BOOT, r(150), loc(19, 18)),
        (BOOT, r(200), loc(19)),
    ])
    assert kinds.count(EventKind.STAGING_CANCELLED) == 1
    assert all(c.spool_id == 19 for c in charges)
    assert (state.active, state.assigned) == (19, 19)


# Deleted active spool (M3, Q7)


def deleted(extruded=200, ids=(), held=0):
    """State after spool 19 was active at `extruded`, then deleted with `held` mm rejected."""
    _, _, state = run([(BOOT, r(100), loc(19)), (BOOT, r(extruded), loc(19, *ids))])
    return hold_deleted(state, 19, held)


def test_hold_deleted_clears_the_active_spool():
    state = deleted(held=50)
    assert (state.active, state.assigned, state.deleted_spool, state.held_mm) == (
        None, None, 19, 50)
    assert hold_deleted(state, 19, 30).held_mm == 80  # Another rejected charge.
    assert hold_deleted(state, 7, 30) == state  # Not this tool's spool.
    # A spool in the claim window that's neither active nor deleted is left alone too.
    windowed = replace(state, since_load={7: 40})
    assert hold_deleted(windowed, 7, 30) == windowed


def test_deletion_drops_the_staged_notice():
    _, kinds, state = run([
        (BOOT, r(100), loc(19)),
        (BOOT, r(200), loc(19, 20)),
        (BOOT + 1, r(10), loc(19, 20)),  # Reboot with spool 20 staged.
    ])
    assert EventKind.STAGED_AT_REBOOT in kinds
    assert state.staged_notice is not None and state.staged_notice.spool_id == 20
    state = hold_deleted(state, 19)
    assert state.staged_notice is None
    _, kinds, state = run([(BOOT + 1, r(20), loc(20))], state)
    assert EventKind.STAGED_AT_REBOOT not in kinds and state.staged_notice is None


def test_reboot_after_deletion_has_no_staged_notice():
    _, kinds, state = run([(BOOT + 1, r(10), loc(20))], deleted(ids=(20,)))
    assert EventKind.STAGED_AT_REBOOT not in kinds
    assert state.staged_notice is None and state.active is None


def test_usage_after_deletion_is_held_through_reboot_and_unload():
    charges, kinds, state = run([
        (BOOT, r(300), loc()),
        (BOOT + 1, r(40), loc()),  # Reboot: held usage survives.
        (BOOT + 1, r(90, unloads=1, at_unload=60), loc()),  # Unload with nothing assigned.
    ], deleted(held=50))
    assert charges == []
    assert EventKind.LOST_UNASSIGNED not in kinds
    assert EventKind.HOLD_DELETED in kinds
    assert (state.held_mm, state.deleted_spool, state.charged) == (50 + 100 + 40 + 50, 19, 90)


def test_held_usage_goes_to_the_next_assigned_spool():
    charges, kinds, state = run([(BOOT, r(300), loc()), (BOOT, r(350), loc(20))],
                                deleted(held=50))
    assert charges == [Charge(20, 150, "held after spool 19 was deleted"),
                       Charge(20, 50, "extruded")]
    assert EventKind.HELD_CHARGED in kinds
    assert (state.active, state.held_mm, state.deleted_spool) == (20, 0, None)


def test_staged_spool_waits_for_unload_then_takes_held_usage():
    charges, _, state = run([(BOOT, r(300), loc(20))], deleted(ids=(20,)))
    assert charges == [] and state.active is None and state.held_mm == 100
    charges, _, state = run([(BOOT, r(400, unloads=1, at_unload=350), loc(20))], state)
    assert charges == [Charge(20, 150, "held after spool 19 was deleted"),
                       Charge(20, 50, "extruded")]
    assert (state.active, state.held_mm) == (20, 0)


def test_staged_spool_takes_held_usage_at_print_start():
    _, _, state = run([(BOOT, r(300), loc(20))], deleted(ids=(20,)))
    # The job appears at 320 (pending, so those 20 mm are held too) and starts at its
    # first extrusion.
    charges, _, state = run([(BOOT, r(320), loc(20), 77), (BOOT, r(340), loc(20), 77)], state)
    assert charges == [Charge(20, 120, "held after spool 19 was deleted"),
                       Charge(20, 20, "extruded")]
    assert state.active == 20


# Provisional charges and claims


def claim_run(steps, state=None):
    """Like run(), with (boot, reading, location_ids, job_id, claim) steps."""
    state = state or ToolState()
    charges, kinds = [], []
    for boot, reading, ids, job, claim in steps:
        res = account(state, boot, reading, ids, job, claim=claim)
        charges += res.charges
        kinds += [e.kind for e in res.events]
        state = res.state
    return charges, kinds, state


def test_print_start_records_provisional_charge_that_grows_until_unload():
    charges, kinds, state = run([
        (BOOT, r(100), loc(7), None),
        (BOOT, r(216, unloads=1, at_unload=100), loc(7), None),  # 116 mm load purge
        (BOOT, r(216, unloads=1, at_unload=100), loc(7), 42),
        (BOOT, r(300, unloads=1, at_unload=100), loc(7), 42),  # first extrusion
    ])
    assert EventKind.SWAP_ASSUMED in kinds
    assert state.swap is None and state.notice is None
    assert state.provisional == Provisional(7, 200, 42)

    _, _, state = run([
        (BOOT, r(500, unloads=1, at_unload=100), loc(7), 42),
        (BOOT, r(500, unloads=1, at_unload=100), loc(7), None),
        (BOOT, r(600, unloads=1, at_unload=100), loc(7), 43),  # next print, no unload
        (BOOT, r(700, unloads=1, at_unload=100), loc(7), 43),
    ], state)
    assert state.provisional == Provisional(7, 600, 42)

    charges, kinds, state = run([(BOOT, r(750, unloads=2, at_unload=720), loc(7), None)],
                                state)
    assert EventKind.PROVISIONAL_KEPT in kinds
    assert state.provisional is None
    assert all(c.mm > 0 and c.spool_id == 7 for c in charges)


def test_claim_on_staged_spool_moves_usage_since_unload():
    # S5: a different spool is loaded with no Spoolman change, and a print runs.
    _, _, state = run([
        (BOOT, r(100), loc(7), None),
        (BOOT, r(500, unloads=1, at_unload=400), loc(7), None),  # 300 before unload
        (BOOT, r(500, unloads=1, at_unload=400), loc(7), 42),
        (BOOT, r(900, unloads=1, at_unload=400), loc(7), 42),
    ])
    assert state.provisional == Provisional(7, 500, 42)
    assert state.since_load == {7: 500}

    # The user moves spool 9 into the location (staged) and claims it.
    _, kinds, state = run([(BOOT, r(950, unloads=1, at_unload=400), loc(7, 9), 42)], state)
    assert EventKind.SWAP_STAGED in kinds
    res = account(state, BOOT, r(1000, unloads=1, at_unload=400), loc(7, 9), 42, claim=9)
    assert res.claimed
    assert res.charges == [
        Charge(9, 550, "claimed usage since load"),
        Charge(7, -550, "moved to claimed spool"),
        Charge(9, 50, "extruded"),
    ]
    assert EventKind.CLAIMED in [e.kind for e in res.events]
    s = res.state
    assert (s.active, s.assigned, s.evict) == (9, 9, 7)
    assert (s.provisional, s.swap, s.staged_notice) == (None, None, None)
    assert s.needs_sync and s.sync_attempts == 0
    assert s.since_load == {7: 0, 9: 600}


def test_claim_moves_only_usage_since_reboot():
    # A spool staged across a reboot: the unload may have been lost with the reboot.
    charges, kinds, state = claim_run([
        (BOOT, r(100), loc(7), None, None),
        (BOOT, r(300), loc(7, 9), None, None),  # 200 to 7, then 9 staged
        (222, r(40), loc(7, 9), None, None),  # reboot
        (222, r(90), loc(7, 9), None, 9),
    ])
    assert EventKind.STAGED_AT_REBOOT in kinds
    assert charges[-3:] == [
        Charge(9, 40, "claimed usage since load"),
        Charge(7, -40, "moved to claimed spool"),
        Charge(9, 50, "extruded"),
    ]
    assert (state.active, state.evict, state.staged_notice) == (9, 7, None)


def test_claim_moves_usage_from_every_spool_since_load():
    _, _, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(150, unloads=1, at_unload=100), loc(7)),  # 50 to 7 after the unload
        (BOOT, r(150, unloads=1, at_unload=100), loc(7, 8)),  # confirms: 50 moves to 8
        (BOOT, r(180, unloads=1, at_unload=100), loc(8)),
    ])
    assert state.since_load == {7: 0, 8: 80}
    res = account(state, BOOT, r(180, unloads=1, at_unload=100), loc(8, 9), None, claim=9)
    assert res.charges == [
        Charge(9, 80, "claimed usage since load"),
        Charge(8, -80, "moved to claimed spool"),
    ]


def test_later_claim_moves_usage_on():
    _, _, state = claim_run([
        (BOOT, r(100), loc(7), None, None),
        (BOOT, r(200), loc(7, 8), None, 8),
        (BOOT, r(260), loc(8, 9), None, 9),
    ])
    assert state.active == 9
    assert state.since_load == {8: 0, 9: 160}


def test_claim_on_active_spool_confirms_and_closes_provisional():
    _, _, state = run([
        (BOOT, r(100), loc(7), None),
        (BOOT, r(200, unloads=1, at_unload=100), loc(7), None),
        (BOOT, r(200, unloads=1, at_unload=100), loc(7), 42),
        (BOOT, r(300, unloads=1, at_unload=100), loc(7), 42),
    ])
    assert state.provisional is not None
    res = account(state, BOOT, r(320, unloads=1, at_unload=100), loc(7), 42, claim=7)
    assert res.claimed
    assert res.charges == [Charge(7, 20, "extruded")]
    assert [e.kind for e in res.events] == [EventKind.CLAIM_CONFIRMED]
    assert res.state.provisional is None and res.state.active == 7


def test_claim_after_unload_moves_nothing_but_switches():
    _, _, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(200, material=None, unloads=1, at_unload=200), loc(7)),
    ])
    assert state.swap == SwapFlag(7, 0)
    res = account(state, BOOT, r(200, material=None, unloads=1, at_unload=200), loc(7, 9),
                  claim=9)
    assert res.charges == []
    assert (res.state.active, res.state.evict, res.state.swap) == (9, 7, None)
    assert res.state.needs_sync


def test_claim_waits_while_location_is_ambiguous():
    res = account(ToolState(BOOT, 100, 0, 7, 7), BOOT, r(200), loc(8, 9), claim=9)
    assert res.skipped and not res.claimed


def test_staged_spool_flagged_at_print_start_and_cleared_when_loaded():
    _, kinds, state = run([
        (BOOT, r(100), loc(7), None),
        (BOOT, r(100), loc(7, 8), None),
        (BOOT, r(100), loc(7, 8), 42),
        (BOOT, r(200), loc(7, 8), 42),
    ])
    assert EventKind.STAGED_AT_PRINT in kinds
    assert state.notice.spool_id == 7
    assert state.staged_notice.spool_id == 8
    assert state.staged_notice.kind == EventKind.STAGED_AT_PRINT
    _, _, state = run([(BOOT, r(250, unloads=1, at_unload=220), loc(7, 8), None)], state)
    assert state.active == 8 and state.staged_notice is None


def test_staged_spool_flagged_after_reboot_and_cleared_when_unstaged():
    _, kinds, state = run([
        (BOOT, r(100), loc(7)),
        (BOOT, r(100), loc(7, 8)),
        (222, r(0), loc(7, 8)),
    ])
    assert EventKind.STAGED_AT_REBOOT in kinds
    assert state.staged_notice.spool_id == 8
    _, _, state = run([(222, r(0), loc(7))], state)
    assert state.staged_notice is None


def test_reboot_without_staging_flags_nothing():
    _, kinds, state = run([(BOOT, r(100), loc(7)), (222, r(0), loc(7))])
    assert EventKind.STAGED_AT_REBOOT not in kinds
    assert state.staged_notice is None


def test_state_round_trip_with_claim_fields():
    s = ToolState(
        BOOT, 123, 2, 7, 8, 5, None, 42,
        staged_notice=Notice(8, EventKind.STAGED_AT_REBOOT, "staged"),
        provisional=Provisional(7, 400, 42),
        since_load={7: 400, 9: -3},
    )
    assert ToolState.from_json(json.loads(json.dumps(s.to_json()))) == s


def test_state_from_before_claims_loads():
    old = {"boot_id": BOOT, "charged": 10, "unload_count": 1, "active": 7, "assigned": 7,
           "evict": None, "swap": None, "job_id": None,
           "notice": {"spool_id": 7, "kind": "swap_assumed", "message": "assumed"}}
    s = ToolState.from_json(old)
    assert (s.provisional, s.staged_notice, s.since_load) == (None, None, {})
    assert s.notice == Notice(7, EventKind.SWAP_ASSUMED, "assumed")


def test_claim_takes_usage_held_for_a_deleted_spool():
    state = deleted(ids=(20,))
    assert state.since_load == {}  # The deleted spool's charges aren't moved.
    _, _, state = run([(BOOT, r(300), loc(20))], state)  # 20 staged: usage held
    assert (state.active, state.held_mm) == (None, 100)
    res = account(state, BOOT, r(350), loc(20), claim=20)
    assert res.claimed
    assert res.charges == [Charge(20, 100, "held after spool 19 was deleted"),
                           Charge(20, 50, "extruded")]
    kinds = [e.kind for e in res.events]
    assert EventKind.CLAIMED in kinds and EventKind.HELD_CHARGED in kinds
    s = res.state
    assert (s.active, s.held_mm, s.deleted_spool, s.needs_sync) == (20, 0, None, True)
    assert s.since_load == {20: 150}


def test_hold_deleted_drops_the_provisional_charge():
    _, _, state = run([
        (BOOT, r(100), loc(19), None),
        (BOOT, r(200, unloads=1, at_unload=100), loc(19), None),
        (BOOT, r(200, unloads=1, at_unload=100), loc(19), 42),
        (BOOT, r(300, unloads=1, at_unload=100), loc(19), 42),
    ])
    assert state.provisional is not None and state.since_load == {19: 200}
    state = hold_deleted(state, 19, 30)
    assert (state.provisional, state.since_load, state.held_mm) == (None, {}, 30)
