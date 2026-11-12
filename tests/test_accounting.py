import json
from dataclasses import fields, replace

import pytest

from spoolman_prusalink.accounting import (
    UNKNOWN_WRITE,
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

# What the printer reports for the PLA preset, which `spool()` writes.
PLA_TEMPS = {"nozzle_temperature": 215, "preheat_temperature": 170, "bed_temperature": 60}

BOOT = 111
PLA = Material("PLA", "#73737b", **PLA_TEMPS)


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
    charges, _, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(150), loc(7)),
            (BOOT, r(150), loc(7)),
            (BOOT, r(400), loc(7)),
        ]
    )
    assert charges == [Charge(7, 50, "extruded"), Charge(7, 250, "extruded")]
    assert state.charged == 400


def test_reboot_restarts_from_zero_not_unknown():
    charges, _, state = run(
        [
            (BOOT, r(1000), loc(7)),
            (BOOT, r(1200), loc(7)),
            (222, r(30), loc(7)),
        ]
    )
    assert charges == [Charge(7, 200, "extruded"), Charge(7, 30, "extruded")]
    assert (state.boot_id, state.charged) == (222, 30)


def test_resumed_job_after_reboot_is_not_a_print_start():
    # Power loss mid-print with an unconfirmed swap open, then a power-panic resume
    # that keeps the job ID.
    _, kinds, state = run(
        [
            (BOOT, r(1000), loc(7), 131),
            (BOOT, r(1100, unloads=1, at_unload=1050), loc(7), 131),
            (222, r(40), loc(7), 131),
        ]
    )
    assert EventKind.SWAP_ASSUMED not in kinds
    assert state.swap is not None and state.job_id == 131


def test_reboot_resets_unload_count():
    _, _, state = run(
        [
            (BOOT, r(100, unloads=3, at_unload=50), loc(7)),
            (222, r(10), loc(7)),
        ]
    )
    assert state.unload_count == 0


def test_reboot_and_unload_in_same_poll():
    charges, _, state = run(
        [
            (BOOT, r(5000), loc(7)),
            (222, r(300, unloads=1, at_unload=100), loc(7, 8)),
        ]
    )
    assert charges == [Charge(7, 100, "before unload"), Charge(8, 200, "extruded")]
    assert (state.unload_count, state.boot_id, state.active) == (1, 222, 8)


def test_usage_held_over_reboot_goes_to_the_active_spool_at_an_unload_in_the_same_poll():
    # Usage held while the tool reported no filament, then a reboot and an unload in one
    # poll: the held usage came before the unload, so the outgoing spool takes it.
    charges, kinds, state = run(
        [
            (BOOT, r(5000), loc(7)),
            (BOOT, r(5200, material=None), loc(7)),
            (222, r(300, unloads=1, at_unload=100), loc(7, 8)),
        ]
    )
    assert charges == [
        Charge(7, 200, "held before the printer restarted"),
        Charge(7, 100, "before unload"),
        Charge(8, 200, "extruded"),
    ]
    assert EventKind.HELD_CHARGED in kinds
    assert (state.active, state.held_mm, state.evict) == (8, 0, (7,))


def test_counter_going_backwards_resyncs():
    res = account(ToolState(BOOT, 500, 0, 7, 7), BOOT, r(400), loc(7))
    assert res.charges == []
    assert res.state.charged == 400
    assert EventKind.COUNTER_ANOMALY in [e.kind for e in res.events]


def test_bad_extruded_at_unload_is_clamped():
    res = account(ToolState(BOOT, 500, 0, 7, 7), BOOT, r(900, unloads=1, at_unload=2000), loc(7))
    assert EventKind.COUNTER_ANOMALY in [e.kind for e in res.events]
    assert sum(c.mm for c in res.charges) == 400


def test_multiple_unloads_between_polls():
    charges, _, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(900, unloads=3, at_unload=800), loc(8)),
        ]
    )
    assert charges == [Charge(7, 700, "before unload"), Charge(8, 100, "extruded")]
    assert state.unload_count == 3


# Spoolman first: staged swaps


def test_staged_swap_keeps_charging_old_spool_until_unload():
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(200), loc(7, 8)),  # spool 8 scanned into the location
            (BOOT, r(300), loc(7, 8)),
            (BOOT, r(420, material=None, unloads=1, at_unload=350), loc(7, 8)),  # unloaded
            (BOOT, r(536, unloads=1, at_unload=350), loc(7, 8)),  # spool 8 loaded
        ]
    )
    assert charges == [
        Charge(7, 100, "extruded"),
        Charge(7, 100, "extruded"),
        Charge(7, 50, "before unload"),
        Charge(8, 186, "extruded"),
    ]
    assert kinds.count(EventKind.SWAP_STAGED) == 1
    assert EventKind.SWAP_COMMITTED in kinds
    assert (state.active, state.assigned, state.evict) == (8, 8, (7,))


def test_evicted_spool_is_ignored_then_forgotten():
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(100), loc(7, 8)),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7, 8)),
            (BOOT, r(300, unloads=1, at_unload=100), loc(7, 8)),  # 7 still in the location
        ]
    )
    assert EventKind.AMBIGUOUS not in kinds
    assert (state.active, state.evict) == (8, (7,))
    _, _, state = run([(BOOT, r(300, unloads=1, at_unload=100), loc(8))], state)
    assert state.evict == ()


def test_second_swap_keeps_the_first_eviction_pending():
    # Evictions never happen (set_location keeps failing).
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(100), loc(7, 8)),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7, 8)),  # 7 -> 8
            (BOOT, r(300, unloads=1, at_unload=100), loc(7, 8, 9)),  # 9 staged
            (BOOT, r(400, unloads=2, at_unload=300), loc(7, 8, 9)),  # 8 -> 9
            (BOOT, r(500, unloads=2, at_unload=300), loc(7, 8, 9)),
        ]
    )
    assert kinds.count(EventKind.SWAP_STAGED) == 2
    assert EventKind.AMBIGUOUS not in kinds
    assert (state.active, state.assigned, state.evict) == (9, 9, (7, 8))
    assert charges[-1] == Charge(9, 100, "extruded")
    _, _, state = run([(BOOT, r(500, unloads=2, at_unload=300), loc(8, 9))], state)
    assert state.evict == (8,)


def test_old_spool_moved_out_first_then_new_staged():
    # The user moves the old spool out, then scans the new one in, before unloading.
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(150), loc()),
            (BOOT, r(200), loc(8)),
            (BOOT, r(260, unloads=1, at_unload=230), loc(8)),
        ]
    )
    assert charges == [
        Charge(7, 50, "extruded"),
        Charge(7, 50, "extruded"),
        Charge(7, 30, "before unload"),
        Charge(8, 30, "extruded"),
    ]
    assert kinds.count(EventKind.SWAP_STAGED) == 1
    assert kinds.count(EventKind.ACTIVE_UNASSIGNED) == 1
    assert (state.active, state.evict) == (8, ())


def test_spool_moved_out_with_no_replacement_is_not_staging():
    _, _, state = run([(BOOT, r(100), loc(7))])
    res = account(state, BOOT, r(150), loc())
    assert [(e.kind, e.message) for e in res.events] == [
        (
            EventKind.ACTIVE_UNASSIGNED,
            "spool 7 is no longer in the location; still charging "
            "it until the printer reports an unload",
        )
    ]
    res = account(res.state, BOOT, r(200), loc())
    assert res.events == []
    # A print starts: no spool is staged, so there's nothing to warn about.
    res = account(res.state, BOOT, r(200), loc(), 42)
    res = account(res.state, BOOT, r(250), loc(), 42)
    assert res.events == [] and res.state.notice is None
    res = account(res.state, BOOT, r(260), loc(7), 42)
    assert [(e.kind, e.message) for e in res.events] == [
        (EventKind.ACTIVE_UNASSIGNED, "spool 7 is back in the location")
    ]
    assert res.charges == [Charge(7, 10, "extruded")]


def emptied_after_staging(*steps):
    """Spool 7 active; the location goes through `steps`, then empties."""
    _, _, state = run([(BOOT, r(100), loc(7))])
    for ids in steps:
        state = account(state, BOOT, r(100), ids).state
    assert state.assigned == 8
    return account(state, BOOT, r(150), loc())


STAGED_8_LEFT = [
    (EventKind.STAGING_CANCELLED, "spool 8 is no longer staged; still charging spool 7"),
    (
        EventKind.ACTIVE_UNASSIGNED,
        "spool 7 isn't in the location either; still charging it "
        "until the printer reports an unload",
    ),
]


def test_staged_spool_leaving_an_emptied_location_is_reported():
    # Spool 7 is moved out, spool 8 in, then spool 8 out again.
    res = emptied_after_staging(loc(), loc(8))
    assert [(e.kind, e.message) for e in res.events] == STAGED_8_LEFT
    assert res.charges == [Charge(7, 50, "extruded")]


def test_both_spools_leaving_the_location_is_reported():
    res = emptied_after_staging(loc(7, 8))
    assert [(e.kind, e.message) for e in res.events] == STAGED_8_LEFT
    res = account(res.state, BOOT, r(160), loc(7))
    assert [(e.kind, e.message) for e in res.events] == [
        (EventKind.ACTIVE_UNASSIGNED, "spool 7 is back in the location")
    ]


def test_unload_after_the_location_empties_holds_usage():
    # S10: the spool stays active until the unload, then usage is held as unassigned.
    _, _, state = run([(BOOT, r(100), loc(7)), (BOOT, r(150), loc())])
    res = account(state, BOOT, r(200, unloads=1, at_unload=180), loc())
    assert [(e.kind, e.message) for e in res.events] == [
        (EventKind.UNLOAD, "1 unload(s); 30 mm before the unload goes to spool 7"),
        (
            EventKind.ACTIVE_UNASSIGNED,
            "spool 7 unloaded with no spool in the location; holding usage until one is assigned",
        ),
        (EventKind.HOLD_UNASSIGNED, "holding usage: no spool assigned"),
    ]
    assert res.state.active is None
    # S6: the next unload with nothing assigned reports the usage before it lost.
    res = account(res.state, BOOT, r(300, unloads=2, at_unload=250), loc())
    assert [(e.kind, e.message) for e in res.events] == [
        (EventKind.UNLOAD, "1 unload(s) with no spool active"),
        (EventKind.LOST_UNASSIGNED, "70 mm before unload had no spool"),
        (EventKind.HOLD_UNASSIGNED, "holding usage: no spool assigned"),
    ]
    res = account(res.state, BOOT, r(320, unloads=2, at_unload=250), loc(8))
    assert [(e.kind, e.message) for e in res.events] == [
        (EventKind.ASSIGNMENT_CHANGED, "spool 8 is active")
    ]
    assert res.charges == [Charge(8, 70, "extruded")]


def test_usage_held_over_a_reboot_is_reported_each_poll_until_assigned():
    # The hold note lasts as long as the hold, with or without new usage.
    _, _, state = run([(BOOT, r(100), loc(7)), (BOOT, r(150), loc())])
    res = account(state, BOOT, r(200, unloads=1, at_unload=180), loc())
    for reading in (r(0), r(0)):
        res = account(res.state, BOOT + 1, reading, loc())
        assert (EventKind.HOLD_UNASSIGNED, "holding usage: no spool assigned") in [
            (e.kind, e.message) for e in res.events
        ]
    res = account(res.state, BOOT + 1, r(0, material=None), loc())
    assert [e.kind for e in res.events] == [EventKind.HOLD_EMPTY]
    assert res.state.held_mm == 20


def test_spool_removed_from_location_stays_active_until_unload():
    charges, _, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(200), loc()),
            (BOOT, r(260, material=None, unloads=1, at_unload=230), loc()),
            (BOOT, r(300, material=None, unloads=1, at_unload=230), loc()),
        ]
    )
    assert charges == [Charge(7, 100, "extruded"), Charge(7, 30, "before unload")]
    assert state.active is None and state.charged == 230


def test_correction_by_uuid_switches_immediately():
    charges, kinds, state = run(
        [(BOOT, r(100), loc(7)), (BOOT, r(150), loc(7, 9)), (BOOT, r(180), loc(7, 9))],
        matches=lambda spool_id: spool_id == 9,
    )
    assert EventKind.CORRECTION in kinds
    assert charges == [Charge(9, 50, "extruded"), Charge(9, 30, "extruded")]
    assert (state.active, state.evict) == (9, (7,))


def test_provisional_charge_stays_when_a_correction_switches_spools():
    # The reloaded spool was assumed at the print start, then the printer reports the
    # staged spool's UUID: the provisional charge stays on spool 7, with no refund.
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), 42),
            (BOOT, r(300, unloads=1, at_unload=100), loc(7), 42),
            (BOOT, r(350, unloads=1, at_unload=100), loc(7, 9), 42),
        ],
        matches=lambda spool_id: spool_id == 9,
    )
    assert kinds[-2:] == [EventKind.CORRECTION, EventKind.PROVISIONAL_KEPT]
    assert charges == [
        Charge(7, 100, "extruded"),
        Charge(7, 100, "extruded"),
        Charge(9, 50, "extruded"),
    ]
    assert (state.active, state.provisional, state.evict) == (9, None, (7,))


def test_print_start_with_staged_swap_warns_and_keeps_old_spool():
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(100), loc(7, 8), None),
            (BOOT, r(400), loc(7, 8), 42),  # job seen; it starts at its first extrusion
            (BOOT, r(700), loc(7, 8), 42),
        ]
    )
    assert EventKind.STAGED_AT_PRINT in kinds
    assert charges == [Charge(7, 300, "extruded"), Charge(7, 300, "extruded")]
    assert state.active == 7


def test_job_cancelled_before_extruding_is_not_a_print_start():
    # Unload, then a print that's cancelled during preheat: the swap stays open, so a
    # Spoolman change afterwards still confirms it.
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), 42),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), 42),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(9), None),
        ]
    )
    assert EventKind.SWAP_ASSUMED not in kinds
    assert EventKind.SWAP_CONFIRMED in kinds
    assert (state.active, state.job_id, state.pending_job) == (9, None, None)


def test_spoolman_fixed_during_preheat_confirms_swap():
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), 42),  # preheating
            (BOOT, r(200, unloads=1, at_unload=100), loc(9), 42),  # user fixes Spoolman
            (BOOT, r(260, unloads=1, at_unload=100), loc(9), 42),  # purge line
        ]
    )
    assert EventKind.SWAP_CONFIRMED in kinds
    assert EventKind.SWAP_ASSUMED not in kinds
    assert (state.active, state.job_id) == (9, 42)


def test_two_spools_neither_active_is_ambiguous():
    res = account(ToolState(BOOT, 100, 0, 7, 7), BOOT, r(200), loc(8, 9))
    assert res.skipped and res.charges == []
    assert res.state == ToolState(BOOT, 100, 0, 7, 7, extruded=200)  # Held usage.


def test_resolve_assignment():
    s = ToolState(active=7, evict=(5,))
    assert resolve_assignment(s, []) is None
    assert resolve_assignment(s, [5]) is None
    assert resolve_assignment(s, [7]) == 7
    assert resolve_assignment(s, [7, 8]) == 8
    assert resolve_assignment(s, [5, 7, 8]) == 8
    assert resolve_assignment(s, [8, 9]) == [8, 9]
    assert resolve_assignment(s, [7, 8, 9]) == [7, 8, 9]


# Filament first: unconfirmed swaps


def test_unload_without_spoolman_change_opens_swap():
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(900, unloads=1, at_unload=600), loc(7)),
        ]
    )
    assert charges == [Charge(7, 500, "before unload"), Charge(7, 300, "extruded")]
    assert EventKind.SWAP_UNCONFIRMED in kinds
    assert state.swap == SwapFlag(7, 300)


def test_confirming_swap_before_print_moves_post_swap_usage():
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(900, unloads=1, at_unload=600), loc(7)),  # 300 to 7, flagged
            (BOOT, r(1000, unloads=1, at_unload=600), loc(7)),  # 100 more to 7
            (BOOT, r(1050, unloads=1, at_unload=600), loc(7, 9)),  # user scans 9 in
        ]
    )
    assert charges[-3:] == [
        Charge(9, 400, "usage since unconfirmed swap"),
        Charge(7, -400, "moved to confirmed swap spool"),
        Charge(9, 50, "extruded"),
    ]
    assert EventKind.SWAP_CONFIRMED in kinds
    assert (state.swap, state.active, state.evict) == (None, 9, (7,))


def test_print_start_closes_swap_as_same_spool():
    # Unload to clear a jam, reload the same spool, print. A later scan is a new swap.
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(220, unloads=1, at_unload=100), loc(7), None),
            (BOOT, r(5000, unloads=1, at_unload=100), loc(7), 42),
            (BOOT, r(9000, unloads=1, at_unload=100), loc(7), 42),
            (BOOT, r(9000, unloads=1, at_unload=100), loc(7, 9), None),
        ]
    )
    assert EventKind.SWAP_ASSUMED in kinds
    assert EventKind.SWAP_CONFIRMED not in kinds
    assert EventKind.SWAP_STAGED in kinds
    assert all(c.mm > 0 and c.spool_id == 7 for c in charges)
    assert (state.active, state.assigned, state.swap) == (7, 9, None)


def test_same_job_still_running_does_not_close_swap():
    # A runout mid-print: the job id doesn't change, so the window stays open.
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7), 42),
            (BOOT, r(900, unloads=1, at_unload=600), loc(7), 42),
            (BOOT, r(1200, unloads=1, at_unload=600), loc(7), 42),
        ]
    )
    assert EventKind.SWAP_ASSUMED not in kinds
    assert state.swap == SwapFlag(7, 600)


def test_next_unload_supersedes_open_swap():
    _, _, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(300, unloads=1, at_unload=200), loc(7)),
            (BOOT, r(500, unloads=2, at_unload=400), loc(7)),
        ]
    )
    assert state.swap == SwapFlag(7, 100)


def test_swap_kept_open_when_spool_removed():
    # The old spool leaves the location first: the swap stays open and usage is held.
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(900, unloads=1, at_unload=600), loc(7)),
            (BOOT, r(950, unloads=1, at_unload=600), loc()),
        ]
    )
    assert charges == [Charge(7, 500, "before unload"), Charge(7, 300, "extruded")]
    assert EventKind.SWAP_CONFIRMED not in kinds
    assert kinds[-2:] == [EventKind.ACTIVE_UNASSIGNED, EventKind.HOLD_UNASSIGNED]
    assert (state.swap, state.active, state.charged) == (SwapFlag(7, 300), None, 900)


# The old spool moved out of the location, then the new one moved in (S3 in S7's order).
TWO_STEP = [
    (BOOT, r(100), loc(7)),
    (BOOT, r(900, unloads=1, at_unload=600), loc(7)),
    (BOOT, r(950, unloads=1, at_unload=600), loc()),
]


def test_two_step_swap_moves_usage_since_unload_to_new_spool():
    charges, _, state = run(TWO_STEP)
    res = account(state, BOOT, r(1000, unloads=1, at_unload=600), loc(9))
    assert res.charges == [
        Charge(9, 300, "usage since unconfirmed swap"),
        Charge(7, -300, "moved to confirmed swap spool"),
        Charge(9, 100, "extruded"),
    ]
    totals = {}
    for c in charges + res.charges:
        totals[c.spool_id] = totals.get(c.spool_id, 0) + c.mm
    assert totals == {7: 500, 9: 400}
    confirmed = [e.message for e in res.events if e.kind == EventKind.SWAP_CONFIRMED]
    assert confirmed == ["swap confirmed: 300 mm moved from spool 7 to 9"]
    assert (res.state.swap, res.state.active, res.state.evict) == (None, 9, ())
    assert res.state.since_load == {7: 0, 9: 400}


def test_old_spool_moved_back_keeps_swap_open():
    _, _, state = run(TWO_STEP)
    charges, kinds, state = run([(BOOT, r(1000, unloads=1, at_unload=600), loc(7))], state)
    assert charges == [Charge(7, 100, "extruded")]
    assert kinds == [EventKind.ACTIVE_UNASSIGNED]
    assert (state.swap, state.active) == (SwapFlag(7, 400), 7)
    # A different spool later still takes everything since the unload.
    charges, _, _ = run([(BOOT, r(1000, unloads=1, at_unload=600), loc(7, 9))], state)
    assert charges == [
        Charge(9, 400, "usage since unconfirmed swap"),
        Charge(7, -400, "moved to confirmed swap spool"),
    ]


def test_old_spool_back_after_print_start_closes_swap_as_of_that_start():
    # Moved back mid-print: the swap closes as it would have had spool 7 never left.
    _, _, state = run([(*step, None) for step in TWO_STEP])
    charges, kinds, state = run(
        [
            (BOOT, r(950, unloads=1, at_unload=600), loc(), 42),
            (BOOT, r(1000, unloads=1, at_unload=600), loc(), 42),  # print start
            (BOOT, r(1000, unloads=1, at_unload=600), loc(7), 42),  # spool 7 back
            (BOOT, r(5000, unloads=1, at_unload=600), loc(7), 42),
            (BOOT, r(5000, unloads=1, at_unload=600), loc(7), None),
        ],
        state,
    )
    assert charges == [Charge(7, 100, "extruded"), Charge(7, 4000, "extruded")]
    assumed = [e for e in kinds if e == EventKind.SWAP_ASSUMED]
    assert len(assumed) == 1
    assert (state.swap, state.provisional, state.active) == (None, Provisional(7, 4400, 42), 7)
    # Spoolman first: spool 9 is staged, and nothing moves.
    charges, kinds, state = run([(BOOT, r(5000, unloads=1, at_unload=600), loc(7, 9))], state)
    assert charges == []
    assert EventKind.SWAP_STAGED in kinds
    assert (state.active, state.assigned) == (7, 9)


def test_print_start_with_location_emptied_keeps_swap_open():
    _, _, state = run([(*step, None) for step in TWO_STEP])
    charges, kinds, state = run(
        [
            (BOOT, r(950, unloads=1, at_unload=600), loc(), 42),
            (BOOT, r(3000, unloads=1, at_unload=600), loc(), 42),
            (BOOT, r(3000, unloads=1, at_unload=600), loc(9), None),
        ],
        state,
    )
    assert EventKind.SWAP_ASSUMED not in kinds
    assert EventKind.PROVISIONAL_KEPT not in kinds
    assert charges == [
        Charge(9, 300, "usage since unconfirmed swap"),
        Charge(7, -300, "moved to confirmed swap spool"),
        Charge(9, 2100, "extruded"),
    ]
    assert (state.swap, state.provisional, state.active) == (None, None, 9)


def test_reboot_with_location_emptied_keeps_swap_and_held_usage():
    _, _, state = run(TWO_STEP)
    charges, kinds, state = run(
        [
            (BOOT + 1, r(20), loc()),
            (BOOT + 1, r(30), loc(9)),
        ],
        state,
    )
    assert charges == [
        Charge(9, 300, "usage since unconfirmed swap"),
        Charge(7, -300, "moved to confirmed swap spool"),
        Charge(9, 50, "held before the printer restarted"),
        Charge(9, 30, "extruded"),
    ]
    assert EventKind.STAGED_AT_REBOOT not in kinds
    assert (state.swap, state.active, state.held_mm) == (None, 9, 0)


def test_unload_with_location_emptied_closes_swap_on_old_spool():
    # As S6: usage held with nothing assigned is lost at the unload. The next unload
    # closes the swap, so the usage since the first one stays on the old spool.
    _, _, state = run(TWO_STEP)
    res = account(state, BOOT, r(1200, unloads=2, at_unload=1100), loc())
    lost = [e.message for e in res.events if e.kind == EventKind.LOST_UNASSIGNED]
    assert lost == ["200 mm before unload had no spool"]
    assert res.charges == []
    assert (res.state.swap, res.state.active, res.state.charged) == (None, None, 1100)
    charges, _, _ = run([(BOOT, r(1250, unloads=2, at_unload=1100), loc(9))], res.state)
    assert charges == [Charge(9, 150, "extruded")]


def test_claim_with_location_emptied_moves_usage_since_unload():
    _, _, state = run(TWO_STEP)
    res = account(state, BOOT, r(1000, unloads=1, at_unload=600), loc(9), claim=9)
    assert res.claimed
    assert res.charges == [
        Charge(9, 300, "claimed usage since load"),
        Charge(7, -300, "moved to claimed spool"),
        Charge(9, 100, "extruded"),
    ]
    assert (res.state.swap, res.state.active) == (None, 9)


def test_measured_reboot_then_menu_unload_and_load():
    # Sequence measured on the Core One L+ (2026-11-04): reboot, menu unload, menu load
    # of the PLA preset (color and UUID dropped), 116 mm counted by the load.
    old, new = 3308742886, 3018224650
    plain = Material("PLA", **PLA_TEMPS)
    charges, _, state = run(
        [
            (old, r(43812), loc(7)),
            (new, r(0), loc(7)),  # reboot
            (new, r(0, material=None, unloads=1, at_unload=0), loc(7)),  # unloaded
            (new, r(116, material=plain, unloads=1, at_unload=0), loc(7)),  # loaded
        ]
    )
    assert charges == [Charge(7, 116, "extruded")]
    assert state.swap == SwapFlag(7, 116)

    # The user then scans the new spool into the location: the load moves to it.
    charges, _, state = run([(new, r(116, material=plain, unloads=1), loc(7, 9))], state)
    assert charges == [
        Charge(9, 116, "usage since unconfirmed swap"),
        Charge(7, -116, "moved to confirmed swap spool"),
    ]
    assert state.swap is None


def test_several_color_changes_in_one_print_are_each_an_unload():
    # P5: two M600 color changes under one job ID. The first is confirmed in Spoolman
    # during the pause; the second isn't, and stays open for the rest of the print.
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7), 42),
            (BOOT, r(500, unloads=1, at_unload=400), loc(7), 42),  # first M600
            (BOOT, r(550, unloads=1, at_unload=400), loc(8), 42),  # Spoolman updated
            (BOOT, r(900, unloads=2, at_unload=800), loc(8), 42),  # second M600
            (BOOT, r(1000, unloads=2, at_unload=800), loc(8), 42),
        ]
    )
    assert charges == [
        Charge(7, 300, "before unload"),
        Charge(7, 100, "extruded"),
        Charge(8, 100, "usage since unconfirmed swap"),
        Charge(7, -100, "moved to confirmed swap spool"),
        Charge(8, 50, "extruded"),
        Charge(8, 250, "before unload"),
        Charge(8, 100, "extruded"),
        Charge(8, 100, "extruded"),
    ]
    assert kinds.count(EventKind.UNLOAD) == 2
    assert kinds.count(EventKind.SWAP_UNCONFIRMED) == 2
    assert kinds.count(EventKind.SWAP_CONFIRMED) == 1
    # The same job carrying on isn't a print start.
    assert EventKind.SWAP_ASSUMED not in kinds
    assert EventKind.STAGED_AT_PRINT not in kinds
    assert (state.active, state.swap, state.provisional) == (8, SwapFlag(8, 200), None)
    assert (state.job_id, state.pending_job) == (42, None)


def test_runout_confirmed_after_the_print_ends_moves_usage_since_unload():
    # P3: the swap opened mid-print stays open past the end of the job.
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7), 42),
            (BOOT, r(500, unloads=1, at_unload=400), loc(7), 42),  # runout
            (BOOT, r(800, unloads=1, at_unload=400), loc(7), 42),
            (BOOT, r(800, unloads=1, at_unload=400), loc(7), None),  # print ends
        ]
    )
    assert EventKind.SWAP_ASSUMED not in kinds
    assert state.swap == SwapFlag(7, 400)
    charges, kinds, state = run([(BOOT, r(800, unloads=1, at_unload=400), loc(9), None)], state)
    assert charges == [
        Charge(9, 400, "usage since unconfirmed swap"),
        Charge(7, -400, "moved to confirmed swap spool"),
    ]
    assert EventKind.SWAP_CONFIRMED in kinds
    assert (state.swap, state.active, state.provisional) == (None, 9, None)


def test_runout_never_confirmed_is_assumed_at_the_next_print_start():
    # P4: the swap opened in job 42 closes at job 43's first extrusion.
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7), 42),
            (BOOT, r(500, unloads=1, at_unload=400), loc(7), 42),  # runout
            (BOOT, r(800, unloads=1, at_unload=400), loc(7), 42),
            (BOOT, r(800, unloads=1, at_unload=400), loc(7), None),
            (BOOT, r(800, unloads=1, at_unload=400), loc(7), 43),  # preheat
        ]
    )
    assert EventKind.SWAP_ASSUMED not in kinds
    assert state.swap == SwapFlag(7, 400)
    _, kinds, state = run([(BOOT, r(900, unloads=1, at_unload=400), loc(7), 43)], state)
    assert kinds.count(EventKind.SWAP_ASSUMED) == 1
    assert (state.swap, state.provisional) == (None, Provisional(7, 500, 43))
    assert state.job_id == 43
    # A claim still moves it.
    res = account(state, BOOT, r(950, unloads=1, at_unload=400), loc(7, 9), 43, claim=9)
    assert res.charges == [
        Charge(9, 500, "claimed usage since load"),
        Charge(7, -500, "moved to claimed spool"),
        Charge(9, 50, "extruded"),
    ]
    assert (res.state.active, res.state.provisional) == (9, None)


def test_new_job_id_with_no_idle_poll_between_is_a_print_start():
    # P9: a failed print restarted, the job ID going 42 -> 43 between two polls.
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7), 42),
            (BOOT, r(500, unloads=1, at_unload=400), loc(7), 42),  # runout
            (BOOT, r(500, unloads=1, at_unload=400), loc(7), 43),  # restarted
        ]
    )
    assert EventKind.SWAP_ASSUMED not in kinds
    assert (state.job_id, state.pending_job) == (42, 43)
    _, kinds, state = run([(BOOT, r(600, unloads=1, at_unload=400), loc(7), 43)], state)
    assert kinds == [EventKind.SWAP_ASSUMED]
    assert (state.job_id, state.pending_job) == (43, None)
    assert (state.swap, state.provisional) == (None, Provisional(7, 200, 43))


def test_spool_staged_mid_print_waits_for_the_unload():
    # P10: spool 8 scanned in while job 42 is extruding.
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(100), loc(7), 42),
            (BOOT, r(200), loc(7), 42),  # print start
            (BOOT, r(300), loc(7, 8), 42),  # spool 8 scanned in
            (BOOT, r(500), loc(7, 8), 42),
        ]
    )
    assert kinds.count(EventKind.SWAP_STAGED) == 1
    assert EventKind.STAGED_AT_PRINT not in kinds  # The print had already started.
    assert all(c.spool_id == 7 for c in charges)
    assert (state.active, state.assigned) == (7, 8)
    charges, kinds, state = run([(BOOT, r(600, unloads=1, at_unload=550), loc(7, 8), 42)], state)
    assert charges == [Charge(7, 50, "before unload"), Charge(8, 50, "extruded")]
    assert EventKind.SWAP_COMMITTED in kinds
    assert (state.active, state.assigned, state.evict) == (8, 8, (7,))


# Holding


def test_empty_tool_holds_usage_until_material_returns():
    charges, _, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(700, material=None, unloads=1, at_unload=600), loc()),
            (BOOT, r(760, material=None, unloads=1, at_unload=600), loc(8)),
            (BOOT, r(800, unloads=1, at_unload=600), loc(8)),
        ]
    )
    assert charges == [Charge(7, 500, "before unload"), Charge(8, 200, "extruded")]
    assert state.charged == 800


def test_unassigned_location_holds_usage():
    charges, _, state = run(
        [
            (BOOT, r(100), loc()),
            (BOOT, r(300), loc()),
            (BOOT, r(350), loc(4)),
        ]
    )
    assert charges == [Charge(4, 250, "extruded")]
    assert state.active == 4


def test_unload_without_spool_is_reported_lost():
    res = account(
        ToolState(BOOT, 100, 0, None, None), BOOT, r(900, unloads=1, at_unload=600), loc(5)
    )
    assert EventKind.LOST_UNASSIGNED in [e.kind for e in res.events]
    assert res.charges == [Charge(5, 300, "extruded")]


def test_unassigned_usage_held_through_reboot_goes_to_next_spool():
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc()),
            (BOOT, r(5000), loc()),
            (BOOT, r(9000), loc()),
            (BOOT + 1, r(10), loc(7)),  # Reboot, then Spoolman fixed.
        ]
    )
    assert charges == [
        Charge(7, 8900, "held before the printer restarted"),
        Charge(7, 10, "extruded"),
    ]
    assert EventKind.HELD_CHARGED in kinds
    assert (state.active, state.held_mm, state.charged) == (7, 0, 10)


def test_usage_held_through_reboot_is_kept_until_a_spool_is_active():
    _, _, state = run(
        [
            (BOOT, r(100), loc()),
            (BOOT, r(900), loc()),
            (BOOT + 1, r(10), loc()),
            (BOOT + 1, r(50), loc()),
        ]
    )
    assert (state.held_mm, state.charged) == (800, 0)
    restored = ToolState.from_json(json.loads(json.dumps(state.to_json())))
    charges, _, state = run([(BOOT + 1, r(60), loc(7))], restored)
    assert charges == [
        Charge(7, 800, "held before the printer restarted"),
        Charge(7, 60, "extruded"),
    ]
    assert state.held_mm == 0


def test_empty_tool_usage_held_through_reboot_counts_toward_open_swap():
    # Unload, reboot during the load, then Spoolman is fixed: the load moves too.
    charges, _, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(700, material=None, unloads=1, at_unload=600), loc(7)),
            (BOOT, r(760, material=None, unloads=1, at_unload=600), loc(7)),
            (BOOT + 1, r(20), loc(7)),  # Reboot, the load done.
        ]
    )
    assert charges == [
        Charge(7, 500, "before unload"),
        Charge(7, 160, "held before the printer restarted"),
        Charge(7, 20, "extruded"),
    ]
    assert state.swap == SwapFlag(7, 180)
    charges, _, _ = run([(BOOT + 1, r(20), loc(7, 9))], state)
    assert charges == [
        Charge(9, 180, "usage since unconfirmed swap"),
        Charge(7, -180, "moved to confirmed swap spool"),
    ]


def test_usage_held_through_reboot_counts_toward_provisional_at_print_start():
    # Unload, a print queued during the load, a reboot, and the print's first
    # extrusion: the held load is part of the reloaded spool's provisional charge.
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(700, material=None, unloads=1, at_unload=600), loc(7), None),
            (BOOT, r(760, material=None, unloads=1, at_unload=600), loc(7), 42),
            (BOOT + 1, r(20), loc(7), 42),
        ]
    )
    assert charges[-2:] == [
        Charge(7, 160, "held before the printer restarted"),
        Charge(7, 20, "extruded"),
    ]
    assert EventKind.SWAP_ASSUMED in kinds
    assert state.provisional == Provisional(7, 180, 42) and state.swap is None


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
    assert charges == [
        Charge(7, 500, "held before the printer restarted"),
        Charge(7, 10, "extruded"),
    ]


def test_usage_during_ambiguous_location_survives_two_reboots():
    _, _, state = run([(BOOT, r(100), loc(7)), (BOOT, r(200), loc(7))])
    kinds = []
    for boot, extruded in ((BOOT, 700), (BOOT + 1, 300), (BOOT + 1, 3000)):
        res = account(state, boot, r(extruded), loc(7, 8, 9))
        assert res.skipped and res.charges == []
        kinds += [e.kind for e in res.events]
        state = res.state
    assert kinds.count(EventKind.REBOOT) == 1
    assert (state.boot_id, state.held_mm) == (BOOT + 1, 500)
    charges, kinds, _ = run([(BOOT + 2, r(10), loc(7))], state)
    assert charges == [
        Charge(7, 3500, "held before the printer restarted"),
        Charge(7, 10, "extruded"),
    ]
    assert EventKind.LOST_UNASSIGNED not in kinds


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
    charges, kinds, state = run(
        [
            (BOOT, legacy(100), loc(7)),
            (BOOT, legacy(250), loc(7, 8)),
            (BOOT, legacy(300), loc(8)),
        ]
    )
    assert charges == [Charge(7, 150, "extruded"), Charge(8, 50, "extruded")]
    assert EventKind.SWAP_STAGED not in kinds
    assert (state.active, state.evict) == (8, ())


# Persistence


def test_state_round_trip():
    s = ToolState(
        boot_id=BOOT,
        charged=123,
        unload_count=2,
        active=7,
        assigned=8,
        evict=(5, 6),
        swap=SwapFlag(7, 40, 41),
        job_id=42,
        pending_job=43,
        pending_at=120,
        notice=Notice(7, EventKind.STAGED_AT_PRINT, "staged"),
        needs_sync=True,
        sync_attempts=2,
        written='M865 P0 F"PLA"',
        held_mm=30,
        deleted_spool=19,
        staged_notice=Notice(8, EventKind.STAGED_AT_REBOOT, "rebooted"),
        provisional=Provisional(7, 400, 42),
        since_load={7: 400, 9: -3},
        extruded=150,
        reboot_pending=True,
    )
    # Every field is set, so a field left out of to_json or from_json fails here.
    defaults = ToolState()
    assert all(getattr(s, f.name) != getattr(defaults, f.name) for f in fields(ToolState))
    assert ToolState.from_json(json.loads(json.dumps(s.to_json()))) == s
    unwritten = replace(s, written=None)
    assert ToolState.from_json(json.loads(json.dumps(unwritten.to_json()))) == unwritten


def test_state_from_before_writes_were_recorded_has_an_unknown_write():
    old = ToolState(BOOT, 10, 1, 7, 7).to_json()
    del old["written"]
    assert ToolState.from_json(old).written == UNKNOWN_WRITE


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
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(19)),
            (BOOT, r(150), loc(19, 18)),
            (BOOT, r(200), loc(19)),
        ]
    )
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
        None,
        None,
        19,
        50,
    )
    assert hold_deleted(state, 19, 30).held_mm == 80  # Another rejected charge.
    assert hold_deleted(state, 7, 30) == state  # Not this tool's spool.
    # A spool in the claim window that's neither active nor deleted is left alone too.
    windowed = replace(state, since_load={7: 40})
    assert hold_deleted(windowed, 7, 30) == windowed


def test_hold_deleted_drops_only_the_deleted_spool_from_evict():
    state = ToolState(BOOT, 100, 0, 19, 19, evict=(5, 19, 8))
    assert hold_deleted(state, 19).evict == (5, 8)


def test_deletion_drops_the_staged_notice():
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(19)),
            (BOOT, r(200), loc(19, 20)),
            (BOOT + 1, r(10), loc(19, 20)),  # Reboot with spool 20 staged.
        ]
    )
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
    charges, kinds, state = run(
        [
            (BOOT, r(300), loc()),
            (BOOT + 1, r(40), loc()),  # Reboot: held usage survives.
            (BOOT + 1, r(90, unloads=1, at_unload=60), loc()),  # Unload with nothing assigned.
        ],
        deleted(held=50),
    )
    assert charges == []
    assert EventKind.LOST_UNASSIGNED not in kinds
    assert EventKind.HOLD_DELETED in kinds
    assert (state.held_mm, state.deleted_spool, state.charged) == (50 + 100 + 40 + 50, 19, 90)


def test_held_usage_goes_to_the_next_assigned_spool():
    charges, kinds, state = run([(BOOT, r(300), loc()), (BOOT, r(350), loc(20))], deleted(held=50))
    assert charges == [
        Charge(20, 150, "held after spool 19 was deleted"),
        Charge(20, 50, "extruded"),
    ]
    assert EventKind.HELD_CHARGED in kinds
    assert (state.active, state.held_mm, state.deleted_spool) == (20, 0, None)


def test_staged_spool_waits_for_unload_then_takes_held_usage():
    charges, _, state = run([(BOOT, r(300), loc(20))], deleted(ids=(20,)))
    assert charges == [] and state.active is None and state.held_mm == 100
    charges, _, state = run([(BOOT, r(400, unloads=1, at_unload=350), loc(20))], state)
    assert charges == [
        Charge(20, 150, "held after spool 19 was deleted"),
        Charge(20, 50, "extruded"),
    ]
    assert (state.active, state.held_mm) == (20, 0)


def test_staged_spool_takes_held_usage_at_print_start():
    _, _, state = run([(BOOT, r(300), loc(20))], deleted(ids=(20,)))
    # The job appears at 320 (pending, so those 20 mm are held too) and starts at its
    # first extrusion.
    charges, _, state = run([(BOOT, r(320), loc(20), 77), (BOOT, r(340), loc(20), 77)], state)
    assert charges == [
        Charge(20, 120, "held after spool 19 was deleted"),
        Charge(20, 20, "extruded"),
    ]
    assert state.active == 20


@pytest.mark.parametrize(
    ("reports_uuid", "active", "charges", "held"),
    [
        (
            True,
            20,
            [Charge(20, 50, "held after spool 19 was deleted"), Charge(20, 100, "extruded")],
            0,
        ),
        (False, None, [], 150),
    ],
)
def test_correction_by_uuid_after_deletion(reports_uuid, active, charges, held):
    # S12 with the active spool deleted: spool 20 was already staged, so it's made
    # active only if the printer reports its UUID.
    res = account(
        deleted(ids=(20,), held=50),
        BOOT,
        r(300),
        loc(20),
        matches=lambda spool_id: reports_uuid and spool_id == 20,
    )
    assert (EventKind.CORRECTION in [e.kind for e in res.events]) == reports_uuid
    assert res.charges == charges
    assert (res.state.active, res.state.held_mm) == (active, held)
    assert res.state.deleted_spool == (None if reports_uuid else 19)


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
    charges, kinds, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(216, unloads=1, at_unload=100), loc(7), None),  # 116 mm load purge
            (BOOT, r(216, unloads=1, at_unload=100), loc(7), 42),
            (BOOT, r(300, unloads=1, at_unload=100), loc(7), 42),  # first extrusion
        ]
    )
    assert EventKind.SWAP_ASSUMED in kinds
    assert state.swap is None and state.notice is None
    assert state.provisional == Provisional(7, 200, 42)

    _, _, state = run(
        [
            (BOOT, r(500, unloads=1, at_unload=100), loc(7), 42),
            (BOOT, r(500, unloads=1, at_unload=100), loc(7), None),
            (BOOT, r(600, unloads=1, at_unload=100), loc(7), 43),  # next print, no unload
            (BOOT, r(700, unloads=1, at_unload=100), loc(7), 43),
        ],
        state,
    )
    assert state.provisional == Provisional(7, 600, 42)

    charges, kinds, state = run([(BOOT, r(750, unloads=2, at_unload=720), loc(7), None)], state)
    assert EventKind.PROVISIONAL_KEPT in kinds
    assert state.provisional is None
    assert all(c.mm > 0 and c.spool_id == 7 for c in charges)


def test_claim_on_staged_spool_moves_usage_since_unload():
    # S5: a different spool is loaded with no Spoolman change, and a print runs.
    _, _, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(500, unloads=1, at_unload=400), loc(7), None),  # 300 before unload
            (BOOT, r(500, unloads=1, at_unload=400), loc(7), 42),
            (BOOT, r(900, unloads=1, at_unload=400), loc(7), 42),
        ]
    )
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
    assert (s.active, s.assigned, s.evict) == (9, 9, (7,))
    assert (s.provisional, s.swap, s.staged_notice) == (None, None, None)
    assert s.needs_sync and s.sync_attempts == 0
    assert s.since_load == {7: 0, 9: 600}


def test_claim_moves_only_usage_since_reboot():
    # A spool staged across a reboot: the unload may have been lost with the reboot.
    charges, kinds, state = claim_run(
        [
            (BOOT, r(100), loc(7), None, None),
            (BOOT, r(300), loc(7, 9), None, None),  # 200 to 7, then 9 staged
            (222, r(40), loc(7, 9), None, None),  # reboot
            (222, r(90), loc(7, 9), None, 9),
        ]
    )
    assert EventKind.STAGED_AT_REBOOT in kinds
    assert charges[-3:] == [
        Charge(9, 40, "claimed usage since load"),
        Charge(7, -40, "moved to claimed spool"),
        Charge(9, 50, "extruded"),
    ]
    assert (state.active, state.evict, state.staged_notice) == (9, (7,), None)


def test_claim_moves_usage_from_every_spool_since_load():
    _, _, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(150, unloads=1, at_unload=100), loc(7)),  # 50 to 7 after the unload
            (BOOT, r(150, unloads=1, at_unload=100), loc(7, 8)),  # confirms: 50 moves to 8
            (BOOT, r(180, unloads=1, at_unload=100), loc(8)),
        ]
    )
    assert state.since_load == {7: 0, 8: 80}
    res = account(state, BOOT, r(180, unloads=1, at_unload=100), loc(8, 9), None, claim=9)
    assert res.charges == [
        Charge(9, 80, "claimed usage since load"),
        Charge(8, -80, "moved to claimed spool"),
    ]


def test_later_claim_moves_usage_on():
    _, _, state = claim_run(
        [
            (BOOT, r(100), loc(7), None, None),
            (BOOT, r(200), loc(7, 8), None, 8),
            (BOOT, r(260), loc(8, 9), None, 9),
        ]
    )
    assert state.active == 9
    assert state.since_load == {8: 0, 9: 160}


def test_claim_on_active_spool_confirms_and_closes_provisional():
    _, _, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), 42),
            (BOOT, r(300, unloads=1, at_unload=100), loc(7), 42),
        ]
    )
    assert state.provisional is not None
    res = account(state, BOOT, r(320, unloads=1, at_unload=100), loc(7), 42, claim=7)
    assert res.claimed
    assert res.charges == [Charge(7, 20, "extruded")]
    assert [e.kind for e in res.events] == [EventKind.CLAIM_CONFIRMED]
    assert res.state.provisional is None and res.state.active == 7


def test_claim_after_unload_moves_nothing_but_switches():
    _, _, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(200, material=None, unloads=1, at_unload=200), loc(7)),
        ]
    )
    assert state.swap == SwapFlag(7, 0)
    res = account(state, BOOT, r(200, material=None, unloads=1, at_unload=200), loc(7, 9), claim=9)
    assert res.charges == []
    assert (res.state.active, res.state.evict, res.state.swap) == (9, (7,), None)
    assert res.state.needs_sync


def test_claim_keeps_earlier_evictions():
    # Spool 5 is still awaiting eviction (Spoolman refused the move), and
    # spool 9 was evicted too before it went back in and was claimed.
    state = ToolState(BOOT, 100, 0, 7, 7, evict=(5, 9))
    res = account(state, BOOT, r(150), loc(5, 7, 9), claim=9)
    assert res.claimed
    assert (res.state.active, res.state.assigned, res.state.evict) == (9, 9, (5, 7))


def test_claim_on_first_sight_makes_the_spool_active():
    res = account(ToolState(), BOOT, r(0), loc(5), None, lambda _id: False, claim=5)
    assert res.claimed
    assert res.charges == []
    assert [e.kind for e in res.events] == [EventKind.CLAIM_CONFIRMED, EventKind.BASELINE]
    assert (res.state.active, res.state.assigned, res.state.boot_id) == (5, 5, BOOT)
    assert res.state.needs_sync


def test_claim_waits_while_location_is_ambiguous():
    res = account(ToolState(BOOT, 100, 0, 7, 7), BOOT, r(200), loc(8, 9), claim=9)
    assert res.skipped and not res.claimed


def test_staged_spool_flagged_at_print_start_and_cleared_when_loaded():
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(100), loc(7, 8), None),
            (BOOT, r(100), loc(7, 8), 42),
            (BOOT, r(200), loc(7, 8), 42),
        ]
    )
    assert EventKind.STAGED_AT_PRINT in kinds
    assert state.notice.spool_id == 7
    assert state.staged_notice.spool_id == 8
    assert state.staged_notice.kind == EventKind.STAGED_AT_PRINT
    _, _, state = run([(BOOT, r(250, unloads=1, at_unload=220), loc(7, 8), None)], state)
    assert state.active == 8 and state.staged_notice is None


def test_staged_spool_flagged_after_reboot_and_cleared_when_unstaged():
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(100), loc(7, 8)),
            (222, r(0), loc(7, 8)),
        ]
    )
    assert EventKind.STAGED_AT_REBOOT in kinds
    assert state.staged_notice.spool_id == 8
    _, _, state = run([(222, r(0), loc(7))], state)
    assert state.staged_notice is None


def test_staged_notice_for_reboot_while_ambiguous():
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (BOOT, r(100), loc(7, 8)),
            (222, r(0), loc(7, 8, 9)),  # Reboot while ambiguous.
            (222, r(0), loc(7, 8)),
        ]
    )
    assert EventKind.STAGED_AT_REBOOT in kinds
    assert state.staged_notice.spool_id == 8 and not state.reboot_pending


def test_staged_notice_for_reboot_while_ambiguous_then_another_spool():
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (222, r(0), loc(7, 8, 9)),  # Reboot while ambiguous.
            (222, r(0), loc(7, 9)),
        ]
    )
    assert EventKind.STAGED_AT_REBOOT in kinds
    assert (state.active, state.staged_notice.spool_id) == (7, 9)
    assert not state.reboot_pending


def test_staged_notice_for_reboot_while_ambiguous_with_neither_active():
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (222, r(0), loc(8, 9)),  # Reboot while ambiguous, spool 7 still loaded.
            (222, r(0), loc(8)),
        ]
    )
    assert EventKind.STAGED_AT_REBOOT in kinds
    assert (state.active, state.staged_notice.spool_id) == (7, 8)
    assert not state.reboot_pending


def test_reboot_pending_clears_without_a_staged_spool():
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7)),
            (222, r(0), loc(8, 9)),  # Reboot while ambiguous.
            (222, r(0), loc(7)),
            (222, r(0), loc(7, 8)),
        ]
    )
    assert EventKind.STAGED_AT_REBOOT not in kinds
    assert state.staged_notice is None and not state.reboot_pending


def test_reboot_without_staging_flags_nothing():
    _, kinds, state = run([(BOOT, r(100), loc(7)), (222, r(0), loc(7))])
    assert EventKind.STAGED_AT_REBOOT not in kinds
    assert state.staged_notice is None


def test_state_round_trip_with_claim_fields():
    s = ToolState(
        BOOT,
        123,
        2,
        7,
        8,
        (5,),
        None,
        42,
        staged_notice=Notice(8, EventKind.STAGED_AT_REBOOT, "staged"),
        provisional=Provisional(7, 400, 42),
        since_load={7: 400, 9: -3},
        reboot_pending=True,
    )
    assert ToolState.from_json(json.loads(json.dumps(s.to_json()))) == s


def test_state_from_before_claims_loads():
    old = {
        "boot_id": BOOT,
        "charged": 10,
        "unload_count": 1,
        "active": 7,
        "assigned": 7,
        "evict": None,
        "swap": None,
        "job_id": None,
        "notice": {"spool_id": 7, "kind": "swap_assumed", "message": "assumed"},
    }
    s = ToolState.from_json(old)
    assert (s.provisional, s.staged_notice, s.since_load) == (None, None, {})
    assert s.reboot_pending is False
    assert s.notice == Notice(7, EventKind.SWAP_ASSUMED, "assumed")


def test_state_with_one_eviction_loads():
    old = ToolState(BOOT, 10, 1, 8, 8).to_json()
    assert ToolState.from_json({**old, "evict": 7}).evict == (7,)
    assert ToolState.from_json({**old, "evict": None}).evict == ()
    assert ToolState.from_json({**old, "evict": [7, 9]}).evict == (7, 9)


def test_claim_takes_usage_held_for_a_deleted_spool():
    state = deleted(ids=(20,))
    assert state.since_load == {}  # The deleted spool's charges aren't moved.
    _, _, state = run([(BOOT, r(300), loc(20))], state)  # 20 staged: usage held
    assert (state.active, state.held_mm) == (None, 100)
    res = account(state, BOOT, r(350), loc(20), claim=20)
    assert res.claimed
    assert res.charges == [
        Charge(20, 100, "held after spool 19 was deleted"),
        Charge(20, 50, "extruded"),
    ]
    kinds = [e.kind for e in res.events]
    assert EventKind.CLAIMED in kinds and EventKind.HELD_CHARGED in kinds
    s = res.state
    assert (s.active, s.held_mm, s.deleted_spool, s.needs_sync) == (20, 0, None, True)
    assert s.since_load == {20: 150}


def test_hold_deleted_drops_the_provisional_charge():
    _, _, state = run(
        [
            (BOOT, r(100), loc(19), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(19), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(19), 42),
            (BOOT, r(300, unloads=1, at_unload=100), loc(19), 42),
        ]
    )
    assert state.provisional is not None and state.since_load == {19: 200}
    state = hold_deleted(state, 19, 30)
    assert (state.provisional, state.since_load, state.held_mm) == (None, {}, 30)


def test_staged_spool_flagged_at_every_print_start():
    # S9: the staged spool is flagged again at the next print.
    _, kinds, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(100), loc(7, 8), None),
            (BOOT, r(100), loc(7, 8), 42),
            (BOOT, r(200), loc(7, 8), 42),  # first print starts
            (BOOT, r(300), loc(7, 8), None),
            (BOOT, r(300), loc(7, 8), 43),
            (BOOT, r(400), loc(7, 8), 43),  # second print starts
        ]
    )
    assert kinds.count(EventKind.STAGED_AT_PRINT) == 2
    assert (state.active, state.assigned, state.job_id) == (7, 8, 43)
    assert state.notice.spool_id == 7
    assert state.staged_notice == Notice(
        8,
        EventKind.STAGED_AT_PRINT,
        "print started while spool 8 is staged but not loaded; charging spool 7",
    )


def test_claim_in_the_same_poll_as_an_unload_moves_only_later_usage():
    # Spool 9 is staged, and spool 7 is claimed in the poll that sees the unload.
    # Handled after the unload, the claim undoes the swap to spool 9, so the usage
    # after the unload goes to spool 7. Handled first, it would only confirm spool 7,
    # and the unload would then make spool 9 active.
    charges, kinds, state = claim_run(
        [
            (BOOT, r(100), loc(7), None, None),
            (BOOT, r(200), loc(7, 9), None, None),
            (BOOT, r(500, unloads=1, at_unload=400), loc(7, 9), None, 7),
        ]
    )
    assert charges == [
        Charge(7, 100, "extruded"),
        Charge(7, 200, "before unload"),
        Charge(7, 100, "extruded"),
    ]
    assert kinds[-2:] == [EventKind.SWAP_COMMITTED, EventKind.CLAIMED]
    assert (state.active, state.evict) == (7, (9,))
    assert state.since_load == {7: 100}


def test_claim_while_a_job_waits_for_its_first_extrusion():
    _, _, state = run(
        [
            (BOOT, r(100), loc(7), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), None),
            (BOOT, r(200, unloads=1, at_unload=100), loc(7), 42),  # preheating
        ]
    )
    assert state.pending_job == 42
    res = account(state, BOOT, r(200, unloads=1, at_unload=100), loc(7, 9), 42, claim=9)
    assert res.claimed
    assert res.charges == [
        Charge(9, 100, "claimed usage since load"),
        Charge(7, -100, "moved to claimed spool"),
    ]
    assert (res.state.active, res.state.swap, res.state.pending_job) == (9, None, 42)
    # The print start then has no swap left to assume.
    _, kinds, state = run([(BOOT, r(260, unloads=1, at_unload=100), loc(9), 42)], res.state)
    assert EventKind.SWAP_ASSUMED not in kinds
    assert (state.active, state.job_id, state.provisional) == (9, 42, None)


def test_first_claim_after_upgrade_moves_only_usage_since_then():
    old = {
        "boot_id": BOOT,
        "charged": 10,
        "unload_count": 1,
        "active": 7,
        "assigned": 7,
        "evict": None,
        "swap": None,
        "job_id": None,
    }
    _, _, state = run([(BOOT, r(60, unloads=1, at_unload=5), loc(7))], ToolState.from_json(old))
    assert state.since_load == {7: 50}
    res = account(state, BOOT, r(80, unloads=1, at_unload=5), loc(7, 9), claim=9)
    assert res.charges == [
        Charge(9, 50, "claimed usage since load"),
        Charge(7, -50, "moved to claimed spool"),
        Charge(9, 20, "extruded"),
    ]


def test_legacy_claim_window_restarts_at_assignment_change():
    _, _, state = run(
        [
            (BOOT, legacy(100), loc(7)),
            (BOOT, legacy(250), loc(7)),
            (BOOT, legacy(300), loc(8)),  # 7 -> 8: the window restarts
            (BOOT, legacy(340), loc(8)),
        ]
    )
    assert state.since_load == {8: 40}
    res = account(state, BOOT, legacy(360), loc(8, 9), claim=9)
    assert res.claimed
    assert res.charges == [
        Charge(9, 40, "claimed usage since load"),
        Charge(8, -40, "moved to claimed spool"),
        Charge(9, 20, "extruded"),
    ]
    assert (res.state.active, res.state.evict) == (9, (8,))
