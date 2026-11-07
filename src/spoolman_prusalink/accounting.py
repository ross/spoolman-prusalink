"""Per-tool usage accounting.

`account()` is pure: it takes the stored state for one tool, the latest printer reading,
the spools Spoolman has in the tool's location, and the current print job, and returns
the charges to make, events to report, and the new state. The caller decides whether to
apply charges (phase 1 only logs them) and persists the new state.

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
  same spool, when a print starts or at the next unload. A print starts at its first
  extrusion, not when its job appears, so preheat and bed leveling leave time to fix
  Spoolman, and a job cancelled before extruding changes nothing. The printer's reported filament
  can't close it: a runout reload keeps the old name, color and UUID whatever was loaded.
- A Spoolman change with no unload, when the printer already reports the new spool's
  instance UUID, is a correction: the new spool becomes active immediately. Name and
  color can't identify a spool, because two spools of one product look the same.
- Firmware without `unload_count` can't stage: an assignment change switches immediately,
  after charging usage so far to the outgoing spool.

While the tool reports no material, or no spool is active, uncharged usage is held (by
not advancing `charged`) and goes to the active spool once both are present.

A deleted active spool (see `hold_deleted()`) leaves the tool with no active spool and
`deleted_spool` set. Its usage then advances `charged` and goes to `held_mm` instead, so
it survives a reboot and an unload. `held_mm` is charged to the next spool that becomes
active: by an assignment change, a swap commit, or a print start with a spool assigned.
A spool already staged when the deletion is found isn't made active on its own, since the
deleted spool's filament can still be in the tool.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .models import ToolReading


class EventKind(StrEnum):
    BASELINE = "baseline"
    REBOOT = "reboot"
    UNLOAD = "unload"
    AMBIGUOUS = "ambiguous"
    SWAP_STAGED = "swap_staged"
    SWAP_COMMITTED = "swap_committed"
    SWAP_UNCONFIRMED = "swap_unconfirmed"
    SWAP_CONFIRMED = "swap_confirmed"
    SWAP_ASSUMED = "swap_assumed"
    STAGED_AT_PRINT = "staged_at_print"
    CORRECTION = "correction"
    STAGING_CANCELLED = "staging_cancelled"
    ASSIGNMENT_CHANGED = "assignment_changed"
    HOLD_EMPTY = "hold_empty"
    HOLD_UNASSIGNED = "hold_unassigned"
    HOLD_DELETED = "hold_deleted"
    HELD_CHARGED = "held_charged"
    LOST_UNASSIGNED = "lost_unassigned"
    COUNTER_ANOMALY = "counter_anomaly"


# Events that need the user's attention.
WARNINGS = frozenset({
    EventKind.AMBIGUOUS, EventKind.SWAP_UNCONFIRMED, EventKind.SWAP_ASSUMED,
    EventKind.STAGED_AT_PRINT, EventKind.LOST_UNASSIGNED, EventKind.COUNTER_ANOMALY,
})


@dataclass(frozen=True)
class Event:
    kind: EventKind
    message: str


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


@dataclass(frozen=True)
class Notice:
    spool_id: int
    kind: EventKind
    message: str


# `ToolState.written` for state saved before the bridge recorded its writes. Those writes
# used `J` to supply temperatures, which the firmware ignores, so they're rewritten once.
UNKNOWN_WRITE = ""


@dataclass(frozen=True)
class ToolState:
    boot_id: int | None = None
    charged: int = 0
    unload_count: int = 0
    active: int | None = None  # Spool physically in the tool, which gets charged.
    assigned: int | None = None  # Spoolman's assignment at the last poll.
    evict: int | None = None  # Old spool still in the location after a swap.
    swap: SwapFlag | None = None
    job_id: int | None = None  # Last print job that started, to detect a print start.
    # A new job that hasn't extruded yet, and the counter when it was first seen. A job
    # counts as started at its first extrusion, so one cancelled during preheat or bed
    # leveling changes nothing.
    pending_job: int | None = None
    pending_at: int = 0
    # A one-off notice for the user about a spool, such as a swap assumed at print
    # start. Cleared at the next print start or unload.
    notice: Notice | None = None
    # The printer should be written with the active spool once idle: set when the
    # active spool changes or the tool unloads (a menu load drops color and UUID).
    needs_sync: bool = False
    sync_attempts: int = 0
    # The `M865` lines last written to the printer for this tool, so an edit in Spoolman
    # can be told apart from a change on the printer. None when the bridge hasn't
    # written this tool. UNKNOWN_WRITE for state saved before this was recorded.
    written: str | None = None
    # Usage held after the active spool was deleted in Spoolman, in raw extruded mm, and
    # the deleted spool. Charged to the next spool that becomes active in this tool.
    held_mm: int = 0
    deleted_spool: int | None = None

    def to_json(self) -> dict:
        return {
            "boot_id": self.boot_id,
            "charged": self.charged,
            "unload_count": self.unload_count,
            "active": self.active,
            "assigned": self.assigned,
            "evict": self.evict,
            "swap": None
            if self.swap is None
            else {"spool_id": self.swap.spool_id, "mm": self.swap.mm},
            "job_id": self.job_id,
            "pending_job": self.pending_job,
            "pending_at": self.pending_at,
            "notice": None
            if self.notice is None
            else {"spool_id": self.notice.spool_id, "kind": self.notice.kind.value,
                  "message": self.notice.message},
            "needs_sync": self.needs_sync,
            "sync_attempts": self.sync_attempts,
            "written": self.written,
            "held_mm": self.held_mm,
            "deleted_spool": self.deleted_spool,
        }

    @classmethod
    def from_json(cls, data: dict) -> ToolState:
        swap = data.get("swap")
        notice = data.get("notice")
        return cls(
            boot_id=data.get("boot_id"),
            charged=int(data.get("charged", 0)),
            unload_count=int(data.get("unload_count", 0)),
            active=data.get("active"),
            assigned=data.get("assigned"),
            evict=data.get("evict"),
            swap=None if swap is None else SwapFlag(int(swap["spool_id"]), int(swap["mm"])),
            job_id=data.get("job_id"),
            pending_job=data.get("pending_job"),
            pending_at=int(data.get("pending_at", 0)),
            notice=None if notice is None else Notice(
                int(notice["spool_id"]), EventKind(notice["kind"]), notice["message"]
            ),
            needs_sync=bool(data.get("needs_sync", False)),
            sync_attempts=int(data.get("sync_attempts", 0)),
            written=data.get("written", UNKNOWN_WRITE),
            held_mm=int(data.get("held_mm", 0)),
            deleted_spool=data.get("deleted_spool"),
        )


@dataclass
class Result:
    state: ToolState
    charges: list[Charge] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    skipped: bool = False  # True when the assignment was ambiguous and nothing changed.
    # The spool that was in the tool when it unloaded during a started print: a runout,
    # or a planned color change, which looks the same.
    runout: int | None = None


def hold_deleted(state: ToolState, spool_id: int, mm: int = 0, queued: int = 0) -> ToolState:
    """Record that `spool_id` was deleted in Spoolman, holding `mm` of its usage.

    If it's the active spool, the tool is left with no active spool, and later usage is
    held for the next spool that becomes active. `mm` is a charge Spoolman rejected,
    added to the held total. Returns the state unchanged if the spool is neither active
    nor already held for.

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
        notice = (state.notice if state.notice is None or state.notice.spool_id != spool_id
                  else None)
        state = replace(
            state,
            active=None,
            assigned=None if state.assigned == spool_id else state.assigned,
            evict=None if state.evict == spool_id else state.evict,
            swap=swap,
            notice=notice,
            deleted_spool=spool_id,
        )
    elif spool_id != state.deleted_spool:
        return state
    return replace(state, held_mm=held + max(mm, 0))


def resolve_assignment(state: ToolState, location_ids: list[int]) -> int | None | list[int]:
    """Return the assigned spool, None for an empty location, or the ids if ambiguous.

    The spool awaiting eviction is ignored. With two spools, one of which is active,
    the other is the staged one.
    """
    ids = [i for i in location_ids if i != state.evict]
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    if len(ids) == 2 and state.active in ids:
        return ids[0] if ids[1] == state.active else ids[1]
    return ids


def account(
    state: ToolState,
    boot_id: int,
    reading: ToolReading,
    location_ids: list[int],
    job_id: int | None = None,
    matches: Callable[[int], bool] = lambda _spool_id: False,
) -> Result:
    """Advance one tool's accounting by one poll.

    `location_ids` are the spools Spoolman has in the tool's location. `job_id` is the
    printer's current job, or None when no job is running; pass None for the bridge's own
    sync jobs. `matches(spool_id)` tells whether the printer reports that spool's
    instance UUID. The caller must skip the poll (not call this) when Spoolman is unreachable.
    """
    out = Result(state=state)

    def event(kind: EventKind, message: str) -> None:
        out.events.append(Event(kind, message))

    def charge(target: int, mm: int, reason: str) -> None:
        if mm:
            out.charges.append(Charge(target, mm, reason))

    assigned = resolve_assignment(state, location_ids)
    if isinstance(assigned, list):
        ids = ", ".join(str(i) for i in assigned)
        event(EventKind.AMBIGUOUS, f"location holds spools {ids}; skipping until it's clear "
              "which one is loaded")
        out.skipped = True
        return out
    evict = state.evict if state.evict in location_ids else None

    # First sight of this tool: take the current counter as the baseline. Usage before
    # the bridge started isn't attributable.
    if state.boot_id is None:
        out.state = ToolState(
            boot_id=boot_id,
            charged=reading.extruded,
            unload_count=reading.unload_count or 0,
            active=assigned,
            assigned=assigned,
            job_id=job_id,
        )
        event(EventKind.BASELINE, f"baseline extruded={reading.extruded} spool={assigned}")
        return out

    s = replace(state, evict=evict)
    if boot_id != s.boot_id:
        event(
            EventKind.REBOOT,
            f"boot_id {s.boot_id} -> {boot_id}; counters restart at 0 "
            "(usage since the last poll before the reboot is lost)",
        )
        # Keep job_id: the firmware stores job IDs in EEPROM and never reuses them, so a
        # print resumed after a power loss keeps its ID and isn't a new print start.
        s = replace(s, boot_id=boot_id, charged=0, unload_count=0, pending_at=0)

    if reading.extruded < s.charged:
        # Shouldn't happen within one boot. Resync rather than charge a negative amount.
        event(
            EventKind.COUNTER_ANOMALY,
            f"extruded {reading.extruded} < charged {s.charged} within one boot; resyncing",
        )
        s = replace(s, charged=reading.extruded)

    legacy = reading.unload_count is None
    active = s.active
    swap = s.swap
    notice = s.notice
    unloaded = False
    deleted = s.deleted_spool
    held = s.held_mm

    def retire(old: int | None) -> int | None:
        """Mark the outgoing spool for eviction if it's still in the location."""
        return old if old is not None and old in location_ids and old != assigned else evict

    # 1. Unload since the last poll: usage before it goes to the active spool.
    if not legacy and reading.unload_count != s.unload_count:
        at = reading.extruded_at_unload or 0
        if reading.unload_count < s.unload_count or at < s.charged or at > reading.extruded:
            event(
                EventKind.COUNTER_ANOMALY,
                f"unload_count {s.unload_count} -> {reading.unload_count}, "
                f"extruded_at_unload={at}, charged={s.charged}, extruded={reading.extruded}",
            )
            at = min(max(at, s.charged), reading.extruded)
        before = at - s.charged
        n = reading.unload_count - s.unload_count
        event(EventKind.UNLOAD, f"{n if n > 0 else 'some'} unload(s); {before} mm before "
              f"the unload goes to spool {active}")
        if before > 0:
            if active is not None:
                charge(active, before, "before unload")
            elif deleted is not None:
                held += before
            else:
                event(EventKind.LOST_UNASSIGNED, f"{before} mm before unload had no spool")
        s = replace(s, charged=at, unload_count=reading.unload_count)
        swap = None  # A new unload supersedes any open swap.
        notice = None
        unloaded = True
        if job_id is not None and job_id == s.job_id and active is not None:
            out.runout = active

        if assigned != active:
            event(EventKind.SWAP_COMMITTED, f"swap done: spool {active} -> {assigned}")
            evict = retire(active)
            active = assigned
        elif active is not None:
            swap = SwapFlag(active)
            event(
                EventKind.SWAP_UNCONFIRMED,
                f"filament unloaded but Spoolman still assigns spool {active}; if a "
                "different spool went in, move it into this tool's location before the "
                "next print starts",
            )

    # 2. Spoolman assigns a different spool than the active one, with no unload.
    elif assigned != active:
        if swap is not None:
            moved = swap.mm if assigned is not None else 0
            if moved > 0:
                # Charge before refunding: Spoolman clamps used_weight at 0, so a refund
                # applied first could be truncated if the charge then fails.
                charge(assigned, moved, "usage since unconfirmed swap")
                charge(swap.spool_id, -moved, "moved to confirmed swap spool")
            event(EventKind.SWAP_CONFIRMED,
                  f"swap confirmed: {moved} mm moved from spool {swap.spool_id} to {assigned}")
            swap = None
            evict = retire(active)
            active = assigned
        elif deleted is not None and active is None and assigned == s.assigned:
            # Already in the location when the active spool was deleted: it may not be
            # loaded yet, so keep holding unless the printer reports its UUID.
            if matches(assigned):
                event(EventKind.CORRECTION, f"printer already has spool {assigned}'s "
                      "filament; making it active")
                active = assigned
        elif legacy or active is None:
            # No unload signal to wait for, or nothing was active: switch now, charging
            # usage so far to the outgoing spool.
            delta = reading.extruded - s.charged
            if legacy and active is not None and delta > 0 and reading.material is not None:
                charge(active, delta, "extruded")
                s = replace(s, charged=reading.extruded)
            event(EventKind.ASSIGNMENT_CHANGED, f"spool {active} -> {assigned}")
            evict = retire(active)
            active = assigned
        elif assigned is not None and matches(assigned):
            event(EventKind.CORRECTION, f"printer already has spool {assigned}'s filament; "
                  f"switching from spool {active} now")
            evict = retire(active)
            active = assigned
        elif assigned != s.assigned:
            event(EventKind.SWAP_STAGED, f"spool {assigned} staged; still charging spool "
                  f"{active} until the printer reports an unload")

    elif not legacy and s.assigned != active and assigned == active:
        event(EventKind.STAGING_CANCELLED, f"spool {s.assigned} is no longer staged; "
              f"still charging spool {active}")

    # 3. A print starting closes the confirmation window. A print starts at its first
    # extrusion: a new job is pending until the counter passes where it was first seen.
    started = False
    pending_job, pending_at = s.pending_job, s.pending_at
    if job_id is None or job_id == s.job_id:
        pending_job = None
    elif pending_job != job_id:
        pending_job, pending_at = job_id, reading.extruded
    elif reading.extruded > pending_at:
        started = True
        pending_job = None
    if started:
        notice = None
        if deleted is not None and active is None and assigned is not None:
            event(EventKind.ASSIGNMENT_CHANGED, f"print started with spool {assigned} "
                  f"assigned; making it active in place of deleted spool {deleted}")
            active = assigned
        if swap is not None:
            msg = (f"print started with no Spoolman change since the unload; assuming "
                   f"spool {swap.spool_id} was reloaded")
            event(EventKind.SWAP_ASSUMED, msg)
            notice = Notice(swap.spool_id, EventKind.SWAP_ASSUMED, msg)
            swap = None
        if not legacy and assigned != active:
            msg = (f"print started while spool {assigned} is staged but not loaded; "
                   f"charging spool {active}")
            event(EventKind.STAGED_AT_PRINT, msg)
            if active is not None:
                notice = Notice(active, EventKind.STAGED_AT_PRINT, msg)

    # A spool is active again after a deletion: it takes the held usage.
    if deleted is not None and active is not None:
        if held:
            charge(active, held, f"held after spool {deleted} was deleted")
            event(EventKind.HELD_CHARGED, f"spool {active} is active; charged it {held} mm "
                  f"held after spool {deleted} was deleted")
        deleted, held = None, 0

    # 4. Usage since the last charge goes to the active spool.
    delta = reading.extruded - s.charged
    if delta > 0:
        if reading.material is None:
            event(EventKind.HOLD_EMPTY, f"holding {delta} mm until filament is reported")
        elif active is None and deleted is not None:
            event(EventKind.HOLD_DELETED, f"holding usage since spool {deleted} was deleted")
            held += delta
            s = replace(s, charged=reading.extruded)
        elif active is None:
            event(EventKind.HOLD_UNASSIGNED, f"holding {delta} mm: no spool assigned")
        else:
            charge(active, delta, "extruded")
            s = replace(s, charged=reading.extruded)
            if swap is not None and swap.spool_id == active:
                swap = replace(swap, mm=swap.mm + delta)

    resync = unloaded or active != s.active
    out.state = replace(
        s,
        needs_sync=s.needs_sync or resync,
        sync_attempts=0 if resync else s.sync_attempts,
        active=active,
        assigned=assigned,
        evict=evict,
        swap=swap,
        job_id=job_id if started else s.job_id,
        pending_job=pending_job,
        pending_at=pending_at,
        notice=notice,
        held_mm=held,
        deleted_spool=deleted,
    )
    return out
