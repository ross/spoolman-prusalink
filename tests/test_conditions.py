import json
import logging
from dataclasses import replace

import pytest

from spoolman_prusalink.conditions import (
    ArchivedLoaded,
    Condition,
    Context,
    Drift,
    HeldForDeleted,
    Lasting,
    LoadedWhileStaged,
    PrinterUnwritable,
    ProvisionalCharge,
    SpoolUnwritable,
    StagedNotLoaded,
    ToolNotReported,
    UnloadedMovedOut,
    UnloadUnconfirmed,
    UuidConflict,
    WriteDidntTake,
    WritePending,
    WriteWaitsForLoad,
    flags,
    log_changes,
    provisional,
    standing,
)
from spoolman_prusalink.drift import Difference, What
from spoolman_prusalink.events import Ambiguous, EventKind, HoldEmpty, StagedAtPrint, StagedAtReboot
from spoolman_prusalink.models import Spool

W = logging.WARNING
TAG = "44c406a5-c6f5-4440-bfba-e8060564b717"
OTHER = "9b1d3f6e-0000-4000-8000-000000000000"
DIFFS = (Difference(What.NAME, "PETG", "PLA"), Difference(What.NOZZLE, 230, 215))
DETAIL = "name printer='PETG' spoolman='PLA'; nozzle printer=230 spoolman=215"
SPOOL = Spool(
    id=7, location="Core", name="X", material="PLA", color_hex=None, diameter=1.75, density=1.24
)
CTX = Context("Core", {7: SPOOL})
CLAIMS = replace(CTX, claim_field=True)

# Every kind, with its log text and its flag text in the plain context.
KINDS = [
    (ToolNotReported(1, level=W), "printer doesn't report tool index 1", None),
    (
        Lasting(Ambiguous((7, 8)), level=W),
        "location holds spools 7, 8; skipping until it's clear which one is loaded",
        None,
    ),
    (Lasting(HoldEmpty(), level=W), "holding usage until filament is reported", None),
    (
        HeldForDeleted(7, 4),
        None,
        "usage is being held because spool 4 was deleted. It goes to this spool at the "
        "next unload or print start, or when this spool is claimed.",
    ),
    (ArchivedLoaded(7), None, "spool is archived but still loaded"),
    (
        UnloadedMovedOut(7),
        None,
        "filament was unloaded, then this spool was moved out. Usage since the unload "
        "goes to the next spool moved into Core.",
    ),
    (
        UnloadUnconfirmed(7),
        None,
        "filament was unloaded with no Spoolman change. If a different spool went in, "
        "move it into Core before the next print.",
    ),
    (
        LoadedWhileStaged(StagedAtPrint(8, 7)),
        None,
        "print started while spool 8 is staged but not loaded; charging spool 7.",
    ),
    (
        StagedNotLoaded(StagedAtReboot(7, 9)),
        None,
        "printer restarted while spool 7 is staged; an unload just before the restart "
        "can be missed, so check which spool is loaded. Still charging spool 9.",
    ),
    (
        ProvisionalCharge(7, 1000, 11),
        None,
        "a print started with no Spoolman change since the unload, so this spool was "
        "assumed reloaded and 1000 mm (≈ 3 g) is charged to it provisionally. If a "
        "different spool is loaded, move the usage in Spoolman by hand. The next unload "
        "keeps the usage here.",
    ),
    (
        UuidConflict(7, TAG, OTHER, level=W),
        f"the tag loaded has instance UUID {TAG}, but spool 7 has {OTHER} in Spoolman",
        f"the loaded tag's instance UUID is {TAG}, but this spool's is {OTHER}. If the "
        "tag is this spool's, set 'Instance UUID' to the tag's UUID or clear it. If not, "
        "move the tag's spool into Core and tick 'Claim held usage' on it.",
    ),
    (
        PrinterUnwritable(7, "MINI isn't supported", level=W),
        "can't write spool 7 to the printer: MINI isn't supported",
        "can't write to the printer: MINI isn't supported",
    ),
    (
        SpoolUnwritable(7, "spool 7 has no material or name to write", level=W),
        "can't write spool 7 to the printer: spool 7 has no material or name to write",
        "spool 7 has no material or name to write",
    ),
    (
        WritePending(7, DIFFS, level=W),
        f"printer will be set to spool 7 when idle: {DETAIL}",
        None,
    ),
    (
        WriteWaitsForLoad(7, 8, level=W),
        "printer will be set once spool 8 is loaded",
        None,
    ),
    (
        WriteDidntTake(7, 3, DIFFS, level=W),
        f"printer still differs from spool 7 after 3 sync jobs: {DETAIL}",
        f"writing this spool to the printer didn't take: {DETAIL}",
    ),
    (Drift(7, DIFFS, level=W), f"printer differs from spool 7: {DETAIL}", None),
]


def test_every_kind_is_listed():
    assert {type(c) for c, _, _ in KINDS} == set(Condition.classes.values())


@pytest.mark.parametrize("condition, log_text, flag_text", KINDS, ids=lambda v: type(v).__name__)
def test_kind_renders_and_round_trips(condition, log_text, flag_text):
    assert condition.log(CTX) == log_text
    assert condition.flag(CTX) == flag_text
    assert Condition.from_json(json.loads(json.dumps(condition.to_json()))) == condition


def test_flag_texts_that_depend_on_the_settings():
    assert Drift(7, DIFFS, level=W).flag(replace(CTX, flag_drift=True)) == (
        f"printer filament differs: {DETAIL}"
    )
    assert (
        StagedNotLoaded(StagedAtPrint(7, 9))
        .flag(CLAIMS)
        .endswith(
            "charging spool 9. If this spool is the one loaded, tick 'Claim held usage' on it."
        )
    )
    charge = ProvisionalCharge(7, 1000, 11)
    assert (
        "If a different spool is loaded, move that spool into Core and tick "
        "'Claim held usage' on it to move the usage there." in charge.flag(CLAIMS)
    )
    # The length factor scales the amount, and a spool that isn't listed has no weight.
    assert "and 1100 mm (≈ 3 g) is charged" in charge.flag(replace(CTX, length_factor=1.1))
    assert "and 1000 mm is charged" in charge.flag(Context("Core", {}))


def test_special_differences_have_their_own_wording():
    assert str(Difference(What.TAG, TAG, 8)) == f"the loaded tag is spool 8's ({TAG})"
    assert str(Difference(What.EDIT)) == "spool changed in Spoolman since the last write"
    assert str(Difference(What.UUID, TAG, None)) == f"uuid printer={TAG} spoolman=None"


def test_flags_are_grouped_by_spool_in_order_with_the_location():
    found = flags(
        [
            ToolNotReported(1, level=W),
            UnloadUnconfirmed(7),
            ArchivedLoaded(8),
            Drift(7, DIFFS, level=W),
            ArchivedLoaded(7),
            LoadedWhileStaged(StagedAtPrint(8, 7)),
            StagedNotLoaded(StagedAtPrint(8, 7)),
        ],
        CTX,
    )
    assert found == {
        7: [
            "Core: filament was unloaded with no Spoolman change. If a different spool "
            "went in, move it into Core before the next print.",
            "Core: spool is archived but still loaded",
            "Core: print started while spool 8 is staged but not loaded; charging spool 7.",
        ],
        8: [
            "Core: spool is archived but still loaded",
            "Core: print started while spool 8 is staged but not loaded; charging spool 7.",
        ],
    }


def test_provisional_amount_stands_during_a_print():
    shown = ProvisionalCharge(7, 100, 11)
    assert provisional(7, 250, 11, [shown], printing=True) is shown
    # Between prints the amount follows the charge.
    assert provisional(7, 250, 11, [shown], printing=False) == ProvisionalCharge(7, 250, 11)
    # Another spool's or another print's charge, or none yet, starts at its own amount.
    assert provisional(8, 250, 11, [shown], printing=True) == ProvisionalCharge(8, 250, 11)
    assert provisional(7, 250, 12, [shown], printing=True) == ProvisionalCharge(7, 250, 12)
    assert provisional(7, 250, 11, [ArchivedLoaded(7)], printing=True) == (
        ProvisionalCharge(7, 250, 11)
    )


def test_compared_conditions_stand_while_a_poll_cant_compare():
    drift = Drift(7, DIFFS, level=W)
    last = [UnloadUnconfirmed(7), drift, UuidConflict(8, TAG, OTHER, level=W)]
    now = [ArchivedLoaded(7)]
    assert standing(last, now, compared=True) == now
    assert standing(last, now, compared=False) == [*now, drift, last[2]]
    # One found this poll replaces the one that stood, by key.
    pending = WritePending(7, DIFFS, level=W)
    assert standing(last, [pending], compared=False) == [pending, last[2]]


def messages(caplog):
    found = [(r.levelno, r.getMessage()) for r in caplog.records]
    caplog.clear()
    return found


def test_changes_are_logged_once_at_the_conditions_level(caplog):
    caplog.set_level(logging.INFO)
    drift = Drift(7, DIFFS, level=W)
    logged = log_changes({}, [UnloadUnconfirmed(7), drift], CTX)
    assert messages(caplog) == [(W, f"[Core] printer differs from spool 7: {DETAIL}")]
    # Unchanged: nothing more.
    logged = log_changes(logged, [drift], CTX)
    assert messages(caplog) == []
    # New detail for the same key is logged as a change, not as an end and a start.
    less = replace(drift, differences=DIFFS[:1])
    logged = log_changes(logged, [less], CTX)
    assert messages(caplog) == [
        (W, "[Core] printer differs from spool 7: name printer='PETG' spoolman='PLA'")
    ]
    # So is another kind with the same key, at its own level.
    pending = WritePending(7, DIFFS[:1], level=logging.INFO)
    logged = log_changes(logged, [pending], CTX)
    assert messages(caplog) == [
        (
            logging.INFO,
            "[Core] printer will be set to spool 7 when idle: name printer='PETG' spoolman='PLA'",
        )
    ]
    # Its end is logged at the level it started at.
    assert log_changes(logged, [], CTX) == {}
    assert messages(caplog) == [
        (
            logging.INFO,
            "[Core] no longer: printer will be set to spool 7 when idle: "
            "name printer='PETG' spoolman='PLA'",
        )
    ]


def test_a_condition_with_no_spool_is_logged_by_what_its_about(caplog):
    missing = ToolNotReported(1, level=W)
    ambiguous = Lasting(Ambiguous((7, 8)), level=W)
    logged = log_changes({}, [missing, ambiguous], CTX)
    assert set(logged) == {"tool_not_reported", EventKind.AMBIGUOUS}
    caplog.clear()
    # The same event with other spools is a change. Another tool index is too.
    log_changes(logged, [ToolNotReported(2, level=W), Lasting(Ambiguous((7, 9)), level=W)], CTX)
    assert [r.getMessage() for r in caplog.records] == [
        "[Core] printer doesn't report tool index 2",
        "[Core] location holds spools 7, 9; skipping until it's clear which one is loaded",
    ]
