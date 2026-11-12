"""What `accounting.account()` reports about a tool, as typed records.

Each kind of event is a frozen dataclass whose fields are the facts it reports, such as
the spools and millimeters involved. Its message is rendered from them. The bridge logs
the message and writes the event to the ledger with both its fields and its message.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum, StrEnum, auto
from typing import ClassVar


class Report(Enum):
    """How the bridge reports an event, by its kind."""

    # Something that happened this poll: logged once and recorded in the ledger.
    INFO = auto()
    # The same, for an event that needs the user's attention.
    WARNING = auto()
    # A condition that `account()` repeats every poll while it lasts: logged when it
    # starts, changes, and ends, and not recorded.
    LASTING = auto()
    # Not reported. It's only emitted on polls with usage, so the bridge reports the
    # condition from the tool state instead.
    STATE = auto()


class EventKind(StrEnum):
    """What an event is about, and how it's reported: `Report.INFO` unless it says."""

    report: Report

    def __new__(cls, value: str, report: Report = Report.INFO) -> EventKind:
        kind = str.__new__(cls, value)
        kind._value_ = value
        kind.report = report
        return kind

    BASELINE = "baseline"
    REBOOT = "reboot"
    UNLOAD = "unload"
    AMBIGUOUS = "ambiguous", Report.LASTING
    SWAP_STAGED = "swap_staged"
    SWAP_COMMITTED = "swap_committed"
    SWAP_UNCONFIRMED = "swap_unconfirmed", Report.WARNING
    SWAP_CONFIRMED = "swap_confirmed"
    SWAP_ASSUMED = "swap_assumed", Report.WARNING
    STAGED_AT_PRINT = "staged_at_print", Report.WARNING
    STAGED_AT_REBOOT = "staged_at_reboot", Report.WARNING
    PROVISIONAL_KEPT = "provisional_kept"
    CLAIMED = "claimed"
    CLAIM_CONFIRMED = "claim_confirmed"
    CORRECTION = "correction"
    STAGING_CANCELLED = "staging_cancelled"
    # The active spool left the location with no unload: it's still charged.
    ACTIVE_UNASSIGNED = "active_unassigned"
    # The active spool is in the location again.
    ACTIVE_REASSIGNED = "active_reassigned"
    # The tool unloaded with nothing in the location: usage is held.
    UNLOADED_UNASSIGNED = "unloaded_unassigned"
    # The spool of an unconfirmed swap left the location: usage is held.
    SWAP_UNASSIGNED = "swap_unassigned"
    # A spool was assigned while none was active, and became active.
    ASSIGNMENT_CHANGED = "assignment_changed"
    # A print started with a deleted spool's usage held: the assigned spool became active.
    ACTIVE_AT_PRINT = "active_at_print"
    HOLD_EMPTY = "hold_empty", Report.LASTING
    HOLD_UNASSIGNED = "hold_unassigned", Report.LASTING
    # The bridge logs when a hold for a deleted spool starts, moves to another spool, and
    # ends, from `ToolState.deleted_spool`.
    HOLD_DELETED = "hold_deleted", Report.STATE
    HELD_CHARGED = "held_charged"
    LOST_UNASSIGNED = "lost_unassigned", Report.WARNING
    COUNTER_ANOMALY = "counter_anomaly", Report.WARNING


# Usage moved from other spools by a claim or a correction: (spool, mm) pairs.
Moved = tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Event:
    """An event. Each subclass is one kind and names it: `class X(Event, kind=...)`."""

    kind: ClassVar[EventKind]
    # The class for each kind, filled in as the subclasses are defined.
    classes: ClassVar[dict[EventKind, type[Event]]] = {}

    def __init_subclass__(cls, kind: EventKind, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        assert kind not in Event.classes, kind
        cls.kind = kind
        Event.classes[kind] = cls

    @property
    def message(self) -> str:
        raise NotImplementedError

    def fields(self) -> dict[str, object]:
        """The event's facts by field name, as the ledger stores them beside its message."""
        return asdict(self)


def _moved(moved: Moved, since: str) -> str:
    sources = ", ".join(f"{mm} mm from spool {spool}" for spool, mm in moved)
    return f"{sources or 'nothing'} (usage since the last {since})"


@dataclass(frozen=True)
class Baseline(Event, kind=EventKind.BASELINE):
    extruded: int
    spool: int | None

    @property
    def message(self) -> str:
        spool = "none" if self.spool is None else self.spool
        return f"baseline extruded={self.extruded} spool={spool}"


@dataclass(frozen=True)
class Reboot(Event, kind=EventKind.REBOOT):
    from_boot: int
    to_boot: int
    kept_mm: int  # Usage held by not advancing `charged`, moved to `held_mm`.

    @property
    def message(self) -> str:
        kept = f"; {self.kept_mm} mm held before the restart is kept" if self.kept_mm else ""
        return (
            f"boot_id {self.from_boot} -> {self.to_boot}; counters restart at 0 "
            f"(usage since the last poll before the reboot is lost){kept}"
        )


@dataclass(frozen=True)
class Unload(Event, kind=EventKind.UNLOAD):
    unloads: int  # How far the unload count moved; not positive after an anomaly.
    mm: int  # Usage before the unload.
    spool: int | None  # The active spool, which is charged it.
    deleted_spool: int | None = None  # With no spool active: the usage is held for it.

    @property
    def message(self) -> str:
        unloads = f"{self.unloads if self.unloads > 0 else 'some'} unload(s)"
        if self.spool is not None:
            return f"{unloads}; {self.mm} mm before the unload goes to spool {self.spool}"
        if self.deleted_spool is not None:
            return (
                f"{unloads}; {self.mm} mm before the unload is held because spool "
                f"{self.deleted_spool} was deleted"
            )
        # The LOST_UNASSIGNED warning reports the usage, so don't repeat it here.
        return f"{unloads} with no spool active"


@dataclass(frozen=True)
class Ambiguous(Event, kind=EventKind.AMBIGUOUS):
    spools: tuple[int, ...]

    @property
    def message(self) -> str:
        ids = ", ".join(str(i) for i in self.spools)
        return f"location holds spools {ids}; skipping until it's clear which one is loaded"


@dataclass(frozen=True)
class SwapStaged(Event, kind=EventKind.SWAP_STAGED):
    staged: int
    active: int

    @property
    def message(self) -> str:
        return (
            f"spool {self.staged} staged; still charging spool {self.active} until the "
            "printer reports an unload"
        )


@dataclass(frozen=True)
class SwapCommitted(Event, kind=EventKind.SWAP_COMMITTED):
    from_spool: int | None
    to_spool: int

    @property
    def message(self) -> str:
        if self.from_spool is None:
            return f"swap done: spool {self.to_spool}"
        return f"swap done: spool {self.from_spool} -> {self.to_spool}"


@dataclass(frozen=True)
class SwapUnconfirmed(Event, kind=EventKind.SWAP_UNCONFIRMED):
    spool: int

    @property
    def message(self) -> str:
        return (
            f"filament unloaded but Spoolman still assigns spool {self.spool}; if a "
            "different spool went in, move it into this tool's location before the next "
            "print starts"
        )


@dataclass(frozen=True)
class SwapConfirmed(Event, kind=EventKind.SWAP_CONFIRMED):
    mm: int
    from_spool: int
    to_spool: int

    @property
    def message(self) -> str:
        return f"swap confirmed: {self.mm} mm moved from spool {self.from_spool} to {self.to_spool}"


@dataclass(frozen=True)
class SwapAssumed(Event, kind=EventKind.SWAP_ASSUMED):
    spool: int
    job: int | None
    # The spool came back to the location after the print started while it was out.
    returned: bool = False

    @property
    def message(self) -> str:
        started = f"print {self.job} started" if self.returned else "print started"
        return (
            f"{started} with no Spoolman change since the unload; assuming spool "
            f"{self.spool} was reloaded and charging it provisionally"
        )


@dataclass(frozen=True)
class StagedAtPrint(Event, kind=EventKind.STAGED_AT_PRINT):
    staged: int
    active: int

    @property
    def message(self) -> str:
        return (
            f"print started while spool {self.staged} is staged but not loaded; "
            f"charging spool {self.active}"
        )


@dataclass(frozen=True)
class StagedAtReboot(Event, kind=EventKind.STAGED_AT_REBOOT):
    staged: int
    active: int

    @property
    def message(self) -> str:
        return (
            f"printer restarted while spool {self.staged} is staged; an unload just "
            "before the restart can be missed, so check which spool is loaded. Still "
            f"charging spool {self.active}"
        )


@dataclass(frozen=True)
class ProvisionalKept(Event, kind=EventKind.PROVISIONAL_KEPT):
    mm: int
    spool: int

    @property
    def message(self) -> str:
        return f"unload keeps the {self.mm} mm charged provisionally to spool {self.spool}"


@dataclass(frozen=True)
class Claimed(Event, kind=EventKind.CLAIMED):
    spool: int
    replaced: int | None  # The spool that was active.
    deleted_spool: int | None  # With no spool active, the deleted one it replaces.
    moved: Moved
    since: str  # "unload" or "reboot": the start of the usage moved.

    @property
    def message(self) -> str:
        if self.replaced is not None:
            replaced = f"in place of spool {self.replaced}"
        elif self.deleted_spool is not None:
            replaced = f"in place of deleted spool {self.deleted_spool}"
        else:
            replaced = "with no spool active"
        return f"spool {self.spool} claimed {replaced}: moved {_moved(self.moved, self.since)}"


@dataclass(frozen=True)
class ClaimConfirmed(Event, kind=EventKind.CLAIM_CONFIRMED):
    spool: int
    provisional_mm: int | None = None  # A provisional charge on the spool, which stays.
    first_poll: bool = False  # The claim came on the tool's first poll.

    @property
    def message(self) -> str:
        if self.first_poll:
            return f"spool {self.spool} claimed; nothing to move"
        stays = (
            f"; the {self.provisional_mm} mm provisional charge stays"
            if self.provisional_mm is not None
            else ""
        )
        return f"spool {self.spool} claimed: confirmed as loaded, nothing moves{stays}"


@dataclass(frozen=True)
class Correction(Event, kind=EventKind.CORRECTION):
    spool: int
    from_spool: int | None  # None when no spool was active.
    # Usage since load moved to `spool`, when a provisional charge was open.
    moved: Moved | None = None
    since: str | None = None

    @property
    def message(self) -> str:
        has = f"printer already has spool {self.spool}'s filament"
        if self.from_spool is None:
            return f"{has}; making it active"
        if self.moved is not None:
            assert self.since is not None
            return (
                f"{has}; switching from spool {self.from_spool} now and moving "
                f"{_moved(self.moved, self.since)}"
            )
        return f"{has}; switching from spool {self.from_spool} now"


@dataclass(frozen=True)
class StagingCancelled(Event, kind=EventKind.STAGING_CANCELLED):
    staged: int
    active: int

    @property
    def message(self) -> str:
        return f"spool {self.staged} is no longer staged; still charging spool {self.active}"


@dataclass(frozen=True)
class ActiveUnassigned(Event, kind=EventKind.ACTIVE_UNASSIGNED):
    spool: int
    staged_left: bool = False  # The staged spool left the location with it.

    @property
    def message(self) -> str:
        gone = (
            "isn't in the location either" if self.staged_left else "is no longer in the location"
        )
        return f"spool {self.spool} {gone}; still charging it until the printer reports an unload"


@dataclass(frozen=True)
class ActiveReassigned(Event, kind=EventKind.ACTIVE_REASSIGNED):
    spool: int

    @property
    def message(self) -> str:
        return f"spool {self.spool} is back in the location"


@dataclass(frozen=True)
class UnloadedUnassigned(Event, kind=EventKind.UNLOADED_UNASSIGNED):
    spool: int

    @property
    def message(self) -> str:
        return (
            f"spool {self.spool} unloaded with no spool in the location; holding usage "
            "until one is assigned"
        )


@dataclass(frozen=True)
class SwapUnassigned(Event, kind=EventKind.SWAP_UNASSIGNED):
    spool: int
    mm: int  # Usage since the unload, which the next spool assigned takes.

    @property
    def message(self) -> str:
        return (
            f"spool {self.spool} is no longer in the location; holding usage until a "
            f"spool is assigned, which also takes the {self.mm} mm since the unload"
        )


@dataclass(frozen=True)
class AssignmentChanged(Event, kind=EventKind.ASSIGNMENT_CHANGED):
    spool: int

    @property
    def message(self) -> str:
        return f"spool {self.spool} is active"


@dataclass(frozen=True)
class ActiveAtPrint(Event, kind=EventKind.ACTIVE_AT_PRINT):
    spool: int
    deleted_spool: int

    @property
    def message(self) -> str:
        return (
            f"print started with spool {self.spool} assigned; making it active in place of "
            f"deleted spool {self.deleted_spool}"
        )


@dataclass(frozen=True)
class HoldEmpty(Event, kind=EventKind.HOLD_EMPTY):
    @property
    def message(self) -> str:
        return "holding usage until filament is reported"


@dataclass(frozen=True)
class HoldUnassigned(Event, kind=EventKind.HOLD_UNASSIGNED):
    @property
    def message(self) -> str:
        return "holding usage: no spool assigned"


@dataclass(frozen=True)
class HoldDeleted(Event, kind=EventKind.HOLD_DELETED):
    deleted_spool: int

    @property
    def message(self) -> str:
        return f"holding usage since spool {self.deleted_spool} was deleted"


@dataclass(frozen=True)
class HeldCharged(Event, kind=EventKind.HELD_CHARGED):
    spool: int
    mm: int
    # The deleted spool the usage was held after, or None for usage held over a reboot.
    deleted_spool: int | None = None

    @property
    def message(self) -> str:
        held = (
            "before the printer restarted"
            if self.deleted_spool is None
            else f"after spool {self.deleted_spool} was deleted"
        )
        return f"spool {self.spool} is active; charged it {self.mm} mm held {held}"


@dataclass(frozen=True)
class LostUnassigned(Event, kind=EventKind.LOST_UNASSIGNED):
    mm: int

    @property
    def message(self) -> str:
        return f"{self.mm} mm before unload had no spool"


@dataclass(frozen=True)
class CounterAnomaly(Event, kind=EventKind.COUNTER_ANOMALY):
    extruded: int
    charged: int
    # For an unload count that went backwards, or an unload outside the usage since the
    # last poll. None when the counter itself went backwards.
    unloads_from: int | None = None
    unloads_to: int | None = None
    at_unload: int | None = None

    @property
    def message(self) -> str:
        if self.unloads_from is None:
            return f"extruded {self.extruded} < charged {self.charged} within one boot; resyncing"
        return (
            f"unload_count {self.unloads_from} -> {self.unloads_to}, "
            f"extruded_at_unload={self.at_unload}, charged={self.charged}, "
            f"extruded={self.extruded}"
        )
