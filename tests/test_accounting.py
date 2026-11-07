from spoolman_prusalink.accounting import (
    Charge,
    EventKind,
    SwapFlag,
    ToolState,
    account,
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
    assert res.state == ToolState(BOOT, 100, 0, 7, 7)


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
