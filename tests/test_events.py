"""The typed events: one check of the wording per kind and variant, and the ledger fields.

Other tests compare events by their fields, so this is the one place that pins how each
message reads.
"""

from dataclasses import fields

import pytest

from spoolman_prusalink import events as ev
from spoolman_prusalink.events import Event, EventKind

MESSAGES = [
    (ev.Baseline(100, 7), "baseline extruded=100 spool=7"),
    (ev.Baseline(100, None), "baseline extruded=100 spool=none"),
    (
        ev.Reboot(111, 112, 0),
        "boot_id 111 -> 112; counters restart at 0 (usage since the last poll before the "
        "reboot is lost)",
    ),
    (
        ev.Reboot(111, 112, 30),
        "boot_id 111 -> 112; counters restart at 0 (usage since the last poll before the "
        "reboot is lost); 30 mm held before the restart is kept",
    ),
    (ev.Unload(1, 30, 7), "1 unload(s); 30 mm before the unload goes to spool 7"),
    (
        ev.Unload(2, 30, None, 19),
        "2 unload(s); 30 mm before the unload is held because spool 19 was deleted",
    ),
    (ev.Unload(-1, 0, None), "some unload(s) with no spool active"),
    (
        ev.Ambiguous((7, 8)),
        "location holds spools 7, 8; skipping until it's clear which one is loaded",
    ),
    (
        ev.SwapStaged(8, 7),
        "spool 8 staged; still charging spool 7 until the printer reports an unload",
    ),
    (ev.SwapCommitted(7, 8), "swap done: spool 7 -> 8"),
    (ev.SwapCommitted(None, 8), "swap done: spool 8"),
    (
        ev.SwapUnconfirmed(7),
        "filament unloaded but Spoolman still assigns spool 7; if a different spool went "
        "in, move it into this tool's location before the next print starts",
    ),
    (ev.SwapConfirmed(300, 7, 9), "swap confirmed: 300 mm moved from spool 7 to 9"),
    (
        ev.SwapAssumed(7, 42),
        "print started with no Spoolman change since the unload; assuming spool 7 was "
        "reloaded and charging it provisionally",
    ),
    (
        ev.SwapAssumed(7, 42, returned=True),
        "print 42 started with no Spoolman change since the unload; assuming spool 7 was "
        "reloaded and charging it provisionally",
    ),
    (
        ev.StagedAtPrint(8, 7),
        "print started while spool 8 is staged but not loaded; charging spool 7",
    ),
    (
        ev.StagedAtReboot(8, 7),
        "printer restarted while spool 8 is staged; an unload just before the restart can "
        "be missed, so check which spool is loaded. Still charging spool 7",
    ),
    (ev.ProvisionalKept(200, 7), "unload keeps the 200 mm charged provisionally to spool 7"),
    (
        ev.Claimed(9, 7, None, ((7, 550), (8, 10)), "unload"),
        "spool 9 claimed in place of spool 7: moved 550 mm from spool 7, 10 mm from spool 8 "
        "(usage since the last unload)",
    ),
    (
        ev.Claimed(20, None, 19, (), "reboot"),
        "spool 20 claimed in place of deleted spool 19: moved nothing (usage since the last "
        "reboot)",
    ),
    (
        ev.Claimed(5, None, None, (), "unload"),
        "spool 5 claimed with no spool active: moved nothing (usage since the last unload)",
    ),
    (ev.ClaimConfirmed(7, first_poll=True), "spool 7 claimed; nothing to move"),
    (ev.ClaimConfirmed(7), "spool 7 claimed: confirmed as loaded, nothing moves"),
    (
        ev.ClaimConfirmed(7, 200),
        "spool 7 claimed: confirmed as loaded, nothing moves; the 200 mm provisional charge stays",
    ),
    (ev.Correction(9, None), "printer already has spool 9's filament; making it active"),
    (
        ev.Correction(9, 7),
        "printer already has spool 9's filament; switching from spool 7 now",
    ),
    (
        ev.Correction(9, 7, ((7, 200),), "unload"),
        "printer already has spool 9's filament; switching from spool 7 now and moving "
        "200 mm from spool 7 (usage since the last unload)",
    ),
    (ev.StagingCancelled(8, 7), "spool 8 is no longer staged; still charging spool 7"),
    (
        ev.ActiveUnassigned(7),
        "spool 7 is no longer in the location; still charging it until the printer "
        "reports an unload",
    ),
    (
        ev.ActiveUnassigned(7, staged_left=True),
        "spool 7 isn't in the location either; still charging it until the printer "
        "reports an unload",
    ),
    (ev.ActiveReassigned(7), "spool 7 is back in the location"),
    (
        ev.UnloadedUnassigned(7),
        "spool 7 unloaded with no spool in the location; holding usage until one is assigned",
    ),
    (
        ev.SwapUnassigned(7, 40),
        "spool 7 is no longer in the location; holding usage until a spool is assigned, "
        "which also takes the 40 mm since the unload",
    ),
    (ev.AssignmentChanged(8), "spool 8 is active"),
    (
        ev.ActiveAtPrint(20, 19),
        "print started with spool 20 assigned; making it active in place of deleted spool 19",
    ),
    (ev.HoldEmpty(), "holding usage until filament is reported"),
    (ev.HoldUnassigned(), "holding usage: no spool assigned"),
    (ev.HoldDeleted(19), "holding usage since spool 19 was deleted"),
    (
        ev.HeldCharged(7, 30),
        "spool 7 is active; charged it 30 mm held before the printer restarted",
    ),
    (
        ev.HeldCharged(20, 100, 19),
        "spool 20 is active; charged it 100 mm held after spool 19 was deleted",
    ),
    (ev.LostUnassigned(70), "70 mm before unload had no spool"),
    (ev.CounterAnomaly(90, 100), "extruded 90 < charged 100 within one boot; resyncing"),
    (
        ev.CounterAnomaly(200, 100, unloads_from=2, unloads_to=1, at_unload=150),
        "unload_count 2 -> 1, extruded_at_unload=150, charged=100, extruded=200",
    ),
]


@pytest.mark.parametrize("event, message", MESSAGES, ids=lambda x: getattr(x, "kind", ""))
def test_message(event, message):
    assert event.message == message


def test_every_kind_has_one_class_and_a_message_check():
    assert set(Event.classes) == set(EventKind)
    assert all(cls.kind is kind for kind, cls in Event.classes.items())
    assert {type(e) for e, _ in MESSAGES} == set(Event.classes.values())


# Keys the bridge sets on an event's ledger record, and the ledger's own columns.
LEDGER_KEYS = {"time", "printer", "tool", "location", "event", "message"} | {
    "spool_id",
    "use_length",
    "applied",
    "failed",
}


def test_no_field_is_named_like_a_ledger_key():
    names = {f.name for cls in Event.classes.values() for f in fields(cls)}
    assert names.isdisjoint(LEDGER_KEYS)


def test_fields_are_the_dataclass_fields():
    claimed = ev.Claimed(9, 7, None, ((7, 550),), "unload")
    assert claimed.fields() == {
        "spool": 9,
        "replaced": 7,
        "deleted_spool": None,
        "moved": ((7, 550),),
        "since": "unload",
    }
    assert ev.HoldEmpty().fields() == {}


def test_the_base_class_has_no_message():
    with pytest.raises(NotImplementedError):
        _ = Event().message
