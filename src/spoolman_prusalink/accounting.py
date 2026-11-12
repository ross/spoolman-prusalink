"""Per-tool usage accounting.

`account()` is pure: it takes the stored state for one tool, the latest printer reading,
the spools Spoolman has in the tool's location, the current print job, and any claim,
and returns the charges to make, events to report, and the new state. The caller queues
the charges for Spoolman and persists the new state.

Counter model:

- `extruded` only grows within one `boot_id`, and restarts at 0 on a new boot.
- `charged` is the counter value already attributed to a spool.

Spool model. The *active* spool is the one physically in the tool, which the bridge
charges. The *assigned* spool is the one Spoolman puts in the tool's location. They
differ while a swap is in progress:

- Spoolman first (the normal order). A new assigned spool is *staged*: the active spool
  keeps being charged until the printer reports an unload. At the unload, usage up to
  `extruded_at_unload` goes to the old spool, the new spool becomes active, and the old
  spool is marked for eviction from the location.
- Filament first. An unload with no Spoolman change opens an *unconfirmed swap*. Usage
  keeps going to the assigned spool. If Spoolman changes before the next print starts,
  the usage since the unload moves to the new spool. The swap closes, assumed to be the
  same spool, when a print starts or at the next unload. If the old spool leaves the
  location first, the swap stays open, even through a print start, and later usage is
  held: the next spool assigned confirms the swap and takes both. The old spool coming
  back leaves the swap as it would be had it never left: open, or closed as of a print
  start in between. A print starts at its first extrusion, not when its job appears, so
  preheat and bed leveling leave time to fix Spoolman, and a job cancelled before
  extruding changes nothing. The printer's reported filament can't close it: a runout
  reload keeps the old name, color and UUID whatever was loaded.
- A print start that closes an unconfirmed swap leaves the usage on the old spool as a
  *provisional* charge. Later usage on that spool adds to it until the next unload, which
  keeps it where it is, or a claim or a correction, which moves it.
- A Spoolman change with no unload, when the printer already reports the new spool's
  instance UUID, is a correction: the new spool becomes active immediately. Name and
  color can't identify a spool, because two spools of one product look the same. During
  a provisional charge, a correction also moves the usage since load, as a claim does.
- A *claim* is the user saying which spool is in the tool. Everything charged since the
  later of the tool's last unload and the printer's last reboot (`since_load`) moves to
  the claimed spool, which becomes active. Claiming the active spool confirms it.

While the tool reports no material, or no spool is active, uncharged usage is held (by
not advancing `charged`) and goes to the active spool once both are present. A reboot
restarts the counter, so the usage held then (`extruded - charged` at the last poll) moves
to `held_mm`. It's charged to the active spool as soon as there is one, or goes with the
usage before the next unload: to the active spool, or reported lost if there's none.

A deleted active spool (see `hold_deleted()`) leaves the tool with no active spool and
`deleted_spool` set. Its usage then advances `charged` and goes to `held_mm` instead, so
it survives a reboot and an unload. `held_mm` is charged to the next spool that becomes
active: by an assignment change, a swap commit, or a print start with a spool assigned.
A spool already staged when the deletion is found isn't made active on its own, since the
deleted spool's filament can still be in the tool.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace

from .events import (
    ActiveAtPrint,
    ActiveReassigned,
    ActiveUnassigned,
    Ambiguous,
    AssignmentChanged,
    Baseline,
    ClaimConfirmed,
    Claimed,
    Correction,
    CounterAnomaly,
    Event,
    EventKind,
    HeldCharged,
    HoldDeleted,
    HoldEmpty,
    HoldUnassigned,
    LostUnassigned,
    Moved,
    ProvisionalKept,
    Reboot,
    StagedAtPrint,
    StagedAtReboot,
    StagingCancelled,
    SwapAssumed,
    SwapCommitted,
    SwapConfirmed,
    SwapStaged,
    SwapUnassigned,
    SwapUnconfirmed,
    Unload,
    UnloadedUnassigned,
)
from .models import ToolReading
from .printer_sync import WrittenFilament


@dataclass(frozen=True)
class Charge:
    spool_id: int
    mm: int  # Negative to refund usage moved to another spool.
    reason: str


@dataclass(frozen=True)
class SwapFlag:
    """An unload the user hasn't confirmed by changing Spoolman."""

    spool_id: int  # The spool that was still assigned after the unload.
    mm: int = 0  # Usage charged to that spool since the unload.
    # A print that started while the location was empty, which would have closed the
    # swap: if the old spool comes back, the swap closes as of that print start.
    started_job: int | None = None


@dataclass(frozen=True)
class Provisional:
    """Usage charged to a spool on the assumption that it was reloaded after an unload.

    Recorded when a print start closes an unconfirmed swap. A claim or a correction
    resolves it, and so does the next unload, which leaves the usage where it is.
    """

    spool_id: int
    mm: int  # Charged to the spool since the unload, including later prints.
    job_id: int | None = None  # The print whose start closed the swap.


@dataclass(frozen=True)
class ToolState:
    boot_id: int | None = None
    charged: int = 0
    unload_count: int = 0
    active: int | None = None  # Spool physically in the tool, which gets charged.
    assigned: int | None = None  # Spoolman's assignment at the last poll.
    # Old spools still in the location after a swap, oldest first. More than one when an
    # eviction hasn't happened yet (Spoolman refused it) by the next swap.
    evict: tuple[int, ...] = ()
    swap: SwapFlag | None = None
    job_id: int | None = None  # Last print job that started, to detect a print start.
    # A new job that hasn't extruded yet, and the counter when it was first seen. A job
    # counts as started at its first extrusion, so one cancelled during preheat or bed
    # leveling changes nothing.
    pending_job: int | None = None
    pending_at: int = 0
    # A one-off notice for the user about the active spool (`notice.active`): a print
    # started while another spool was staged. Cleared at the next print start or unload.
    notice: StagedAtPrint | None = None
    # The printer should be written with the active spool once idle: set when the
    # active spool changes or the tool unloads (a menu load drops color and UUID).
    needs_sync: bool = False
    sync_attempts: int = 0
    # What the bridge last wrote to the printer for this tool, so an edit in Spoolman
    # can be told apart from a change on the printer. None when the bridge hasn't
    # written this tool.
    written: WrittenFilament | None = None
    # Usage held in raw extruded mm, charged to the next spool that becomes active in this
    # tool: after the active spool was deleted in Spoolman (`deleted_spool` is that
    # spool), or carried over a reboot (`deleted_spool` is None).
    held_mm: int = 0
    deleted_spool: int | None = None
    # A notice on the staged spool: a print started, or the printer rebooted, while it
    # was staged. Cleared when it stops being staged, at an unload, by a claim, or when
    # the active spool is deleted.
    # It's on `staged_notice.staged`.
    staged_notice: StagedAtPrint | StagedAtReboot | None = None
    # Usage charged to the active spool on the assumption that it was reloaded.
    provisional: Provisional | None = None
    # Millimeters charged to each spool since the later of this tool's last unload and
    # the printer's last reboot, which is when a claimed spool can have gone in: the
    # usage a claim moves. Not mutated in place.
    since_load: dict[int, int] = field(default_factory=dict)
    # The counter at the last poll, including polls skipped as ambiguous: the usage held
    # by not advancing `charged` is `extruded - charged`, which a reboot would drop.
    extruded: int = 0
    # A poll skipped as ambiguous applied a reboot: the next poll that isn't skipped
    # checks for a staged spool as if it saw the reboot itself.
    reboot_pending: bool = False

    def to_json(self) -> dict:
        return {
            "boot_id": self.boot_id,
            "charged": self.charged,
            "unload_count": self.unload_count,
            "active": self.active,
            "assigned": self.assigned,
            "evict": list(self.evict),
            "swap": None
            if self.swap is None
            else {
                "spool_id": self.swap.spool_id,
                "mm": self.swap.mm,
                "started_job": self.swap.started_job,
            },
            "job_id": self.job_id,
            "pending_job": self.pending_job,
            "pending_at": self.pending_at,
            "notice": _notice_json(self.notice),
            "needs_sync": self.needs_sync,
            "sync_attempts": self.sync_attempts,
            "written": None if self.written is None else self.written.to_json(),
            "held_mm": self.held_mm,
            "deleted_spool": self.deleted_spool,
            "staged_notice": _notice_json(self.staged_notice),
            "provisional": None
            if self.provisional is None
            else {
                "spool_id": self.provisional.spool_id,
                "mm": self.provisional.mm,
                "job_id": self.provisional.job_id,
            },
            "since_load": {str(k): v for k, v in sorted(self.since_load.items())},
            "extruded": self.extruded,
            "reboot_pending": self.reboot_pending,
        }

    @classmethod
    def from_json(cls, data: dict) -> ToolState:
        swap = data["swap"]
        provisional = data["provisional"]
        return cls(
            boot_id=data["boot_id"],
            charged=int(data["charged"]),
            unload_count=int(data["unload_count"]),
            active=data["active"],
            assigned=data["assigned"],
            evict=_evict_from_json(data["evict"]),
            swap=None
            if swap is None
            else SwapFlag(
                int(swap["spool_id"]),
                int(swap["mm"]),
                swap["started_job"],
            ),
            job_id=data["job_id"],
            pending_job=data["pending_job"],
            pending_at=int(data["pending_at"]),
            notice=_notice_from_json(data["notice"]),
            needs_sync=bool(data["needs_sync"]),
            sync_attempts=int(data["sync_attempts"]),
            written=None if data["written"] is None else WrittenFilament.from_json(data["written"]),
            held_mm=int(data["held_mm"]),
            deleted_spool=data["deleted_spool"],
            staged_notice=_notice_from_json(data["staged_notice"]),
            provisional=None
            if provisional is None
            else Provisional(
                int(provisional["spool_id"]),
                int(provisional["mm"]),
                provisional["job_id"],
            ),
            since_load={int(k): int(v) for k, v in data["since_load"].items()},
            extruded=int(data["extruded"]),
            reboot_pending=bool(data["reboot_pending"]),
        )


def _evict_from_json(data: list[int]) -> tuple[int, ...]:
    return tuple(int(i) for i in data)


# The events a notice can be, by kind.
_NOTICES: dict[str, type[StagedAtPrint | StagedAtReboot]] = {
    EventKind.STAGED_AT_PRINT: StagedAtPrint,
    EventKind.STAGED_AT_REBOOT: StagedAtReboot,
}


def _notice_json(notice: StagedAtPrint | StagedAtReboot | None) -> dict | None:
    if notice is None:
        return None
    return {"kind": notice.kind.value, "staged": notice.staged, "active": notice.active}


def _notice_from_json(data: dict | None) -> StagedAtPrint | StagedAtReboot | None:
    if data is None:
        return None
    return _NOTICES[data["kind"]](int(data["staged"]), int(data["active"]))


@dataclass
class Result:
    state: ToolState
    charges: list[Charge] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    # True when the assignment was ambiguous: nothing is charged and only the counter
    # and any reboot are recorded.
    skipped: bool = False
    claimed: bool = False  # True when the claim passed in was handled.
    # The spool that was in the tool when it unloaded during a started print: a runout,
    # a jam or tangle, or a planned color change, which all look the same.
    runout: int | None = None


class _Draft:
    """A copy of a `ToolState` that `account()` writes this poll's values to.

    It has the fields of `ToolState` and no others, so a write to a misspelled field
    raises instead of being dropped.
    """

    __slots__ = tuple(f.name for f in fields(ToolState))

    def __init__(self, state: ToolState) -> None:
        for name in self.__slots__:
            setattr(self, name, getattr(state, name))

    def freeze(self) -> ToolState:
        return ToolState(**{name: getattr(self, name) for name in self.__slots__})


# What holds for every state that `account()` and `hold_deleted()` return, given a legal
# state. Each entry says a rule and tests whether a state keeps it.
_INVARIANTS: tuple[tuple[str, Callable[[ToolState], bool]], ...] = (
    (
        "a tool the printer hasn't reported holds nothing",
        lambda s: s.boot_id is not None or s == ToolState(),
    ),
    (
        # Not `since_load`: a swap confirmed after a reboot refunds usage from before the
        # reboot, which the usage since load no longer counts (#164).
        "no counter or tally is negative",
        lambda s: (
            min(
                s.charged,
                s.unload_count,
                s.pending_at,
                s.held_mm,
                s.extruded,
                0 if s.swap is None else s.swap.mm,
                0 if s.provisional is None else s.provisional.mm,
            )
            >= 0
        ),
    ),
    (
        "a spool awaits eviction once, and it's neither the active nor the assigned spool",
        lambda s: (
            len(set(s.evict)) == len(s.evict)
            and s.active not in s.evict
            and s.assigned not in s.evict
        ),
    ),
    (
        "with no spool active, a spool is assigned only while a deleted spool's usage is held",
        lambda s: s.active is not None or s.assigned is None or s.deleted_spool is not None,
    ),
    (
        "an unconfirmed swap is on the active spool, or no spool is active",
        lambda s: s.swap is None or s.active in (None, s.swap.spool_id),
    ),
    (
        "a swap that a print start would have closed stays open only while no spool is active",
        lambda s: s.swap is None or s.swap.started_job is None or s.active is None,
    ),
    (
        "a provisional charge is on the active spool",
        lambda s: s.provisional is None or s.provisional.spool_id == s.active,
    ),
    (
        "a swap and a provisional charge aren't open together",
        lambda s: s.swap is None or s.provisional is None,
    ),
    (
        "a deleted spool's usage is held only while no spool is active",
        lambda s: s.deleted_spool is None or s.active is None,
    ),
    (
        "no swap, provisional charge, or staged notice is open while a deleted spool's "
        "usage is held",
        lambda s: (
            s.deleted_spool is None
            or (s.swap is None and s.provisional is None and s.staged_notice is None)
        ),
    ),
    (
        "a staged notice is on the assigned spool, which isn't the active one",
        lambda s: (
            s.staged_notice is None
            or (s.staged_notice.staged == s.assigned and s.assigned != s.active)
        ),
    ),
    (
        "a pending job isn't the job that last started",
        lambda s: s.pending_job is None or s.pending_job != s.job_id,
    ),
)


def violations(state: ToolState) -> list[str]:
    """Return the rules for a legal `ToolState` that `state` breaks, empty if none.

    Nothing in the bridge calls this. It writes down which combinations of fields can
    occur, and the tests check it on every state that goes into or comes out of
    `account()` and `hold_deleted()`.
    """
    return [rule for rule, holds in _INVARIANTS if not holds(state)]


def hold_deleted(state: ToolState, spool_id: int, mm: int = 0, queued: int = 0) -> ToolState:
    """Record that `spool_id` was deleted in Spoolman, holding `mm` of its usage.

    If it's the active spool, the tool is left with no active spool, and later usage is
    held for the next spool that becomes active. `mm` is a charge Spoolman rejected,
    added to the held total. The caller checks that the spool is the active or the
    already deleted one.

    An unconfirmed swap on the deleted spool is dropped, and its usage since the unload
    is held too: that filament came from whatever was loaded, which the next spool
    stands for, as a confirmed swap would move it. `queued` is the part of it still in
    the outbox, which is held charge by charge as each is rejected, so it's left out.
    """
    held = state.held_mm
    if spool_id == state.active:
        swap = state.swap
        if swap is not None and swap.spool_id == spool_id:
            held += max(swap.mm - queued, 0)
            swap = None
        notice = state.notice if state.notice is None or state.notice.active != spool_id else None
        state = replace(
            state,
            active=None,
            assigned=None if state.assigned == spool_id else state.assigned,
            swap=swap,
            notice=notice,
            deleted_spool=spool_id,
            provisional=None,  # It was on the active spool.
            # It says the deleted spool is still charged; the deleted-spool flag covers
            # the staged spool from here on.
            staged_notice=None,
        )
    if spool_id in state.since_load:
        # A claim takes the held usage instead: a rejected charge is both in the window
        # and in `held_mm`, and there's nothing left to refund.
        state = replace(
            state, since_load={k: v for k, v in state.since_load.items() if k != spool_id}
        )
    return replace(state, held_mm=held + max(mm, 0))


def resolve_assignment(
    state: ToolState | _Draft, location_ids: list[int]
) -> int | None | list[int]:
    """Return the assigned spool, None for an empty location, or the ids if ambiguous.

    Spools awaiting eviction are ignored. With two spools, one of which is active,
    the other is the staged one.
    """
    ids = [i for i in location_ids if i not in state.evict]
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    if len(ids) == 2 and state.active in ids:
        return ids[0] if ids[1] == state.active else ids[1]
    return ids


def account(
    last: ToolState,
    boot_id: int,
    reading: ToolReading,
    location_ids: list[int],
    job_id: int | None = None,
    matches: Callable[[int], bool] = lambda _spool_id: False,
    claim: int | None = None,
) -> Result:
    """Advance one tool's accounting by one poll.

    `last` is the tool's state after the previous poll. `location_ids` are the spools
    Spoolman has in the tool's location. `job_id` is the printer's current job, or None
    when no job is running; pass None for the bridge's own sync jobs.
    `matches(spool_id)` tells whether the printer reports that spool's instance UUID.
    `claim` is a spool in `location_ids` that the user says is in the tool;
    `Result.claimed` tells whether it was handled. The caller must skip the poll (not
    call this) when Spoolman is unreachable.

    Two states run through the function. `last` never changes, so `last.x` is always
    the previous poll's value. `current` starts as a copy of it, every step writes this
    poll's values to it, and at the end it's the new state. A field that no step writes
    carries over.
    """
    current = _Draft(last)
    out = Result(state=last)

    def done() -> Result:
        out.state = current.freeze()
        return out

    def event(ev: Event) -> None:
        out.events.append(ev)

    def charge(target: int, mm: int, reason: str) -> None:
        """Charge `mm` to a spool, and add it to each tally kept on that spool.

        Every charge goes through here, so no charge site updates a tally itself. A
        refund counts too: it moves usage to another spool, which the usage since load
        has to follow. `take()` and a confirmed swap close the open swap and the
        provisional charge before they refund, so those two never see one.
        """
        if not mm:
            return
        out.charges.append(Charge(target, mm, reason))
        window = current.since_load
        current.since_load = {**window, target: window.get(target, 0) + mm}
        swap, provisional = current.swap, current.provisional
        if swap is not None and swap.spool_id == target:
            current.swap = replace(swap, mm=swap.mm + mm)
        if provisional is not None and provisional.spool_id == target:
            current.provisional = replace(provisional, mm=provisional.mm + mm)

    assigned = resolve_assignment(last, location_ids)
    ambiguous = isinstance(assigned, list)

    # First sight of this tool: take the current counter as the baseline. Usage before
    # the bridge started isn't attributable.
    if last.boot_id is None and not ambiguous:
        current.boot_id = boot_id
        current.charged = current.extruded = reading.extruded
        current.unload_count = reading.unload_count
        current.active = current.assigned = assigned
        current.job_id = job_id
        if claim is not None:
            current.active, out.claimed = claim, True
            event(ClaimConfirmed(claim, first_poll=True))
        current.needs_sync = out.claimed
        event(Baseline(reading.extruded, current.active))
        return done()

    rebooted = last.boot_id is not None and boot_id != last.boot_id
    if rebooted:
        # Usage held by not advancing `charged` would be lost with the counter.
        kept = max(last.extruded - last.charged, 0)
        event(Reboot(last.boot_id, boot_id, kept))
        # Keep job_id: the firmware stores job IDs in EEPROM and never reuses them, so a
        # print resumed after a power loss keeps its ID and isn't a new print start.
        current.boot_id = boot_id
        current.charged = 0
        current.unload_count = 0
        current.pending_at = 0
        current.held_mm += kept
        # An unload just before a reboot can be missed, so a claim moves only usage
        # since the reboot.
        current.since_load = {}

    if ambiguous:
        event(Ambiguous(tuple(assigned)))
        out.skipped = True
        if last.boot_id is not None:
            # Nothing is charged, so the usage is held: keep track of it over a reboot,
            # including one while the location stays ambiguous.
            current.extruded = reading.extruded
            current.reboot_pending = last.reboot_pending or rebooted
        return done()
    current.assigned = assigned
    current.evict = tuple(i for i in current.evict if i in location_ids)

    if reading.extruded < current.charged:
        # Shouldn't happen within one boot. Resync rather than charge a negative amount.
        event(CounterAnomaly(reading.extruded, current.charged))
        current.charged = reading.extruded

    unloaded = False

    def retire(old: int | None) -> None:
        """Mark the outgoing spool for eviction if it's still in the location."""
        if (
            old is not None
            and old in location_ids
            and old != current.assigned
            and old not in current.evict
        ):
            current.evict = (*current.evict, old)

    def charge_held(target: int) -> None:
        """Charge usage held over a reboot, with no deleted spool, to the active spool."""
        held = current.held_mm
        charge(target, held, "held before the printer restarted")
        event(HeldCharged(target, held))
        current.held_mm = 0

    def take(target: int, how: str) -> tuple[Moved, str]:
        """Move the usage since load to `target` and make it active, as a claim does.

        That settles which spool went in, so an open swap or a provisional charge is
        closed. Returns what moved from which spool, and whether the usage moved is since
        the last unload or reboot, for the event.
        """
        current.swap = None
        current.provisional = None
        # Charge before refunding: Spoolman clamps used_weight at 0, so a refund applied
        # first could be truncated if the charge then fails.
        moved = {i: mm for i, mm in sorted(current.since_load.items()) if i != target and mm > 0}
        charge(target, sum(moved.values()), f"{how} usage since load")
        for i, mm in moved.items():
            charge(i, -mm, f"moved to {how} spool")
        evict = tuple(i for i in current.evict if i != target)
        old = current.active
        if old is not None and old in location_ids and old not in evict:
            evict = (*evict, old)
        current.evict = evict
        current.active = target
        resolved = resolve_assignment(current, location_ids)
        current.assigned = target if isinstance(resolved, list) else resolved
        since = "reboot" if rebooted or not current.unload_count else "unload"
        return tuple(moved.items()), since

    # 1. Unload since the last poll: usage before it goes to the active spool.
    if reading.unload_count != current.unload_count:
        at = reading.extruded_at_unload or 0
        if (
            reading.unload_count < current.unload_count
            or at < current.charged
            or at > reading.extruded
        ):
            event(
                CounterAnomaly(
                    reading.extruded,
                    current.charged,
                    unloads_from=current.unload_count,
                    unloads_to=reading.unload_count,
                    at_unload=at,
                )
            )
            at = min(max(at, current.charged), reading.extruded)
        before = at - current.charged
        event(
            Unload(
                reading.unload_count - current.unload_count,
                before,
                current.active,
                current.deleted_spool,
            )
        )
        # Usage held over a reboot is usage before the unload too.
        carried = current.held_mm if current.deleted_spool is None else 0
        if current.active is not None and carried:
            charge_held(current.active)
        if before > 0 or carried:
            if current.active is not None:
                charge(current.active, before, "before unload")
            elif current.deleted_spool is not None:
                current.held_mm += before
            else:
                event(LostUnassigned(before + carried))
                current.held_mm = 0
        if current.provisional is not None:
            event(ProvisionalKept(current.provisional.mm, current.provisional.spool_id))
            current.provisional = None
        current.charged = at
        current.unload_count = reading.unload_count
        current.since_load = {}
        current.swap = None  # A new unload supersedes any open swap.
        current.notice = None
        current.staged_notice = None
        unloaded = True
        if job_id is not None and job_id == last.job_id and current.active is not None:
            out.runout = current.active

        if current.assigned is None and current.active is not None:
            # S10: not a swap. Usage is held until a spool is assigned.
            event(UnloadedUnassigned(current.active))
            retire(current.active)
            current.active = None
        elif current.assigned != current.active:
            event(SwapCommitted(current.active, current.assigned))
            retire(current.active)
            current.active = current.assigned
        elif current.active is not None:
            current.swap = SwapFlag(current.active)
            event(SwapUnconfirmed(current.active))

    # 2. A claim: the user says which spool is in the tool. It replaces the assignment
    # handling below, which would only stage the claimed spool.
    if claim is not None:
        out.claimed = True
        if claim == current.active:
            provisional = current.provisional
            event(ClaimConfirmed(claim, None if provisional is None else provisional.mm))
        else:
            replaced, deleted = current.active, current.deleted_spool
            moved, since = take(claim, "claimed")
            event(Claimed(claim, replaced, None if replaced is not None else deleted, moved, since))
        current.swap = None
        current.provisional = None
        current.staged_notice = None

    # 3. Spoolman assigns a different spool than the active one, with no unload.
    elif unloaded:
        pass
    elif current.assigned != current.active:
        swap = current.swap
        if swap is not None and current.assigned is None:
            # The old spool left first, before the new one arrived. Keep the swap open and
            # hold usage until a spool is assigned: a new spool confirms the swap below.
            event(SwapUnassigned(current.active, swap.mm))
            current.active = None
        elif swap is not None and current.assigned == swap.spool_id:
            # The old spool is back: as if it had never left, the swap stays open, or closes
            # as of a print start while it was out.
            event(ActiveReassigned(current.assigned))
            current.active = current.assigned
            if swap.started_job is not None:
                event(SwapAssumed(swap.spool_id, swap.started_job, returned=True))
                current.provisional = Provisional(swap.spool_id, swap.mm, swap.started_job)
                current.swap = None
        elif swap is not None:
            moved = swap.mm
            current.swap = None
            if moved > 0:
                # Charge before refunding: Spoolman clamps used_weight at 0, so a refund
                # applied first could be truncated if the charge then fails.
                charge(current.assigned, moved, "usage since unconfirmed swap")
                charge(swap.spool_id, -moved, "moved to confirmed swap spool")
            event(SwapConfirmed(moved, swap.spool_id, current.assigned))
            retire(current.active)
            current.active = current.assigned
        elif (
            current.deleted_spool is not None
            and current.active is None
            and current.assigned == last.assigned
        ):
            # Already in the location when the active spool was deleted: it may not be
            # loaded yet, so keep holding unless the printer reports its UUID.
            if matches(current.assigned):
                event(Correction(current.assigned, None))
                current.active = current.assigned
        elif current.active is None:
            # Nothing was active: switch now.
            event(AssignmentChanged(current.assigned))
            current.active = current.assigned
        elif current.assigned is None:
            # Not a swap: nothing is staged, and the active spool is still loaded.
            if last.assigned == current.active:
                event(ActiveUnassigned(current.active))
            elif last.assigned is not None:
                # The staged spool left. The active one was already gone, or left with it.
                event(StagingCancelled(last.assigned, current.active))
                event(ActiveUnassigned(current.active, staged_left=True))
        elif matches(current.assigned) and current.provisional is not None:
            # The printer settles which spool went in at the unload, as a claim would:
            # the usage charged provisionally moves to it.
            old = current.active
            moved, since = take(current.assigned, "corrected")
            event(Correction(current.active, old, moved, since))
        elif matches(current.assigned):
            event(Correction(current.assigned, current.active))
            retire(current.active)
            current.active = current.assigned
        elif current.assigned != last.assigned:
            event(SwapStaged(current.assigned, current.active))

    elif (
        current.active is not None and last.assigned is None and current.assigned == current.active
    ):
        event(ActiveReassigned(current.active))
    elif (
        current.active is not None
        and last.assigned != current.active
        and current.assigned == current.active
    ):
        event(StagingCancelled(last.assigned, current.active))

    # 4. A print starting closes the confirmation window. A print starts at its first
    # extrusion: a new job is pending until the counter passes where it was first seen.
    started = False
    if job_id is None or job_id == last.job_id:
        current.pending_job = None
    elif current.pending_job != job_id:
        current.pending_job, current.pending_at = job_id, reading.extruded
    elif reading.extruded > current.pending_at:
        started = True
        current.pending_job = None
    if started:
        current.job_id = job_id
        current.notice = None
        if (
            current.deleted_spool is not None
            and current.active is None
            and current.assigned is not None
        ):
            event(ActiveAtPrint(current.assigned, current.deleted_spool))
            current.active = current.assigned
        # With the location emptied, no spool is charged to assume: the swap stays open
        # for the next spool assigned. The old spool coming back closes it as of now.
        swap = current.swap
        if swap is not None and current.active is None and swap.started_job is None:
            current.swap = replace(swap, started_job=job_id)
        elif swap is not None and current.active is not None:
            event(SwapAssumed(swap.spool_id, job_id))
            current.provisional = Provisional(swap.spool_id, swap.mm, job_id)
            current.swap = None
        if current.assigned is not None and current.assigned != current.active:
            # With a spool assigned, step 3 or the deleted-spool case above made one active.
            assert current.active is not None
            staged = StagedAtPrint(current.assigned, current.active)
            event(staged)
            current.notice = current.staged_notice = staged

    # A spool is active again after a deletion, including by a claim: it takes the held
    # usage.
    if current.deleted_spool is not None and current.active is not None:
        if current.held_mm:
            charge(
                current.active,
                current.held_mm,
                f"held after spool {current.deleted_spool} was deleted",
            )
            event(HeldCharged(current.active, current.held_mm, current.deleted_spool))
        current.deleted_spool, current.held_mm = None, 0
    elif current.held_mm and current.active is not None:
        charge_held(current.active)

    # 5. Usage since the last charge goes to the active spool.
    delta = reading.extruded - current.charged
    if delta > 0:
        if reading.material is None:
            event(HoldEmpty())
        elif current.active is None and current.deleted_spool is not None:
            event(HoldDeleted(current.deleted_spool))
            current.held_mm += delta
            current.charged = reading.extruded
        elif current.active is None:
            event(HoldUnassigned())
        else:
            charge(current.active, delta, "extruded")
            current.charged = reading.extruded
    elif current.held_mm and current.active is None and current.deleted_spool is None:
        # Usage kept over a reboot is still held, though there's no new usage this poll.
        if reading.material is None:
            event(HoldEmpty())
        else:
            event(HoldUnassigned())

    # 6. Notices that depend on the outcome.
    # After a deletion, the deleted-spool flag already covers the staged spool.
    if (
        (rebooted or last.reboot_pending)
        and current.assigned is not None
        and current.assigned != current.active
        and current.deleted_spool is None
    ):
        # `unload_count` restarts with the printer, so an unload just before the reboot
        # can be missed, leaving the staged spool loaded while the old one is charged.
        current.staged_notice = StagedAtReboot(current.assigned, current.active)
        event(current.staged_notice)
    staged = current.staged_notice
    if staged is not None and (
        staged.staged != current.assigned or current.assigned == current.active
    ):
        current.staged_notice = None

    resync = unloaded or current.active != last.active or out.claimed
    current.needs_sync = last.needs_sync or resync
    if resync:
        current.sync_attempts = 0
    current.extruded = reading.extruded
    current.reboot_pending = False
    return done()
