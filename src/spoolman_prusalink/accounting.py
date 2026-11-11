"""Per-tool usage accounting.

`account()` is pure: it takes the stored state for one tool, the latest printer reading,
the spools Spoolman has in the tool's location, the current print job, and any claim,
and returns the charges to make, events to report, and the new state. The caller queues
the charges for Spoolman, or only logs them in dry run, and persists the new state.

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
  keeps it where it is, or a claim.
- A Spoolman change with no unload, when the printer already reports the new spool's
  instance UUID, is a correction: the new spool becomes active immediately. Name and
  color can't identify a spool, because two spools of one product look the same.
- A *claim* is the user saying which spool is in the tool. Everything charged since the
  later of the tool's last unload and the printer's last reboot (`since_load`) moves to
  the claimed spool, which becomes active. Claiming the active spool confirms it.
- Firmware without `unload_count` can't stage: an assignment change switches immediately,
  after charging usage so far to the outgoing spool.

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
    STAGED_AT_REBOOT = "staged_at_reboot"
    PROVISIONAL_KEPT = "provisional_kept"
    CLAIMED = "claimed"
    CLAIM_CONFIRMED = "claim_confirmed"
    CORRECTION = "correction"
    STAGING_CANCELLED = "staging_cancelled"
    ACTIVE_UNASSIGNED = "active_unassigned"
    ASSIGNMENT_CHANGED = "assignment_changed"
    HOLD_EMPTY = "hold_empty"
    HOLD_UNASSIGNED = "hold_unassigned"
    HOLD_DELETED = "hold_deleted"
    HELD_CHARGED = "held_charged"
    LOST_UNASSIGNED = "lost_unassigned"
    COUNTER_ANOMALY = "counter_anomaly"


# Events that need the user's attention.
WARNINGS = frozenset(
    {
        EventKind.AMBIGUOUS,
        EventKind.SWAP_UNCONFIRMED,
        EventKind.SWAP_ASSUMED,
        EventKind.STAGED_AT_PRINT,
        EventKind.STAGED_AT_REBOOT,
        EventKind.LOST_UNASSIGNED,
        EventKind.COUNTER_ANOMALY,
    }
)


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
    # A print that started while the location was empty, which would have closed the
    # swap: if the old spool comes back, the swap closes as of that print start.
    started_job: int | None = None


@dataclass(frozen=True)
class Provisional:
    """Usage charged to a spool on the assumption that it was reloaded after an unload.

    Recorded when a print start closes an unconfirmed swap. A claim resolves it, and so
    does the next unload, which leaves the usage where it is.
    """

    spool_id: int
    mm: int  # Charged to the spool since the unload, including later prints.
    job_id: int | None = None  # The print whose start closed the swap.


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
    # Old spools still in the location after a swap, oldest first. More than one when an
    # eviction hasn't happened yet (dry run, or Spoolman refused it) by the next swap.
    evict: tuple[int, ...] = ()
    swap: SwapFlag | None = None
    job_id: int | None = None  # Last print job that started, to detect a print start.
    # A new job that hasn't extruded yet, and the counter when it was first seen. A job
    # counts as started at its first extrusion, so one cancelled during preheat or bed
    # leveling changes nothing.
    pending_job: int | None = None
    pending_at: int = 0
    # A one-off notice for the user about a spool, such as a print started while another
    # spool is staged. Cleared at the next print start or unload.
    notice: Notice | None = None
    # The printer should be written with the active spool once idle: set when the
    # active spool changes or the tool unloads (a menu load drops color and UUID).
    needs_sync: bool = False
    sync_attempts: int = 0
    # The `M865` lines last written to the printer for this tool, so an edit in Spoolman
    # can be told apart from a change on the printer. None when the bridge hasn't
    # written this tool. UNKNOWN_WRITE for state saved before this was recorded.
    written: str | None = None
    # Usage held in raw extruded mm, charged to the next spool that becomes active in this
    # tool: after the active spool was deleted in Spoolman (`deleted_spool` is that
    # spool), or carried over a reboot (`deleted_spool` is None).
    held_mm: int = 0
    deleted_spool: int | None = None
    # A notice on the staged spool: a print started, or the printer rebooted, while it
    # was staged. Cleared when it stops being staged, at an unload, by a claim, or when
    # the active spool is deleted.
    staged_notice: Notice | None = None
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
            "written": self.written,
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
        swap = data.get("swap")
        provisional = data.get("provisional")
        return cls(
            boot_id=data.get("boot_id"),
            charged=int(data.get("charged", 0)),
            unload_count=int(data.get("unload_count", 0)),
            active=data.get("active"),
            assigned=data.get("assigned"),
            evict=_evict_from_json(data.get("evict")),
            swap=None
            if swap is None
            else SwapFlag(
                int(swap["spool_id"]),
                int(swap["mm"]),
                swap.get("started_job"),
            ),
            job_id=data.get("job_id"),
            pending_job=data.get("pending_job"),
            pending_at=int(data.get("pending_at", 0)),
            notice=_notice_from_json(data.get("notice")),
            needs_sync=bool(data.get("needs_sync", False)),
            sync_attempts=int(data.get("sync_attempts", 0)),
            written=data.get("written", UNKNOWN_WRITE),
            held_mm=int(data.get("held_mm", 0)),
            deleted_spool=data.get("deleted_spool"),
            staged_notice=_notice_from_json(data.get("staged_notice")),
            provisional=None
            if provisional is None
            else Provisional(
                int(provisional["spool_id"]),
                int(provisional["mm"]),
                provisional.get("job_id"),
            ),
            # State from before claims has no record: a claim then moves only usage
            # charged since the upgrade.
            since_load={int(k): int(v) for k, v in (data.get("since_load") or {}).items()},
            # State from before this was recorded holds nothing over a reboot.
            extruded=int(data.get("extruded", data.get("charged", 0))),
            reboot_pending=bool(data.get("reboot_pending", False)),
        )


def _evict_from_json(data: list[int] | int | None) -> tuple[int, ...]:
    # State from before several evictions were kept stores one spool or None.
    if data is None:
        return ()
    if isinstance(data, int):
        return (data,)
    return tuple(int(i) for i in data)


def _notice_json(notice: Notice | None) -> dict | None:
    if notice is None:
        return None
    return {"spool_id": notice.spool_id, "kind": notice.kind.value, "message": notice.message}


def _notice_from_json(data: dict | None) -> Notice | None:
    if data is None:
        return None
    return Notice(int(data["spool_id"]), EventKind(data["kind"]), data["message"])


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
        notice = state.notice if state.notice is None or state.notice.spool_id != spool_id else None
        state = replace(
            state,
            active=None,
            assigned=None if state.assigned == spool_id else state.assigned,
            evict=tuple(i for i in state.evict if i != spool_id),
            swap=swap,
            notice=notice,
            deleted_spool=spool_id,
            provisional=None,  # It was on the active spool.
            # It says the deleted spool is still charged; the deleted-spool flag covers
            # the staged spool from here on.
            staged_notice=None,
        )
    elif spool_id != state.deleted_spool:
        return state
    if spool_id in state.since_load:
        # A claim takes the held usage instead: a rejected charge is both in the window
        # and in `held_mm`, and there's nothing left to refund.
        state = replace(
            state, since_load={k: v for k, v in state.since_load.items() if k != spool_id}
        )
    return replace(state, held_mm=held + max(mm, 0))


def resolve_assignment(state: ToolState, location_ids: list[int]) -> int | None | list[int]:
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
    state: ToolState,
    boot_id: int,
    reading: ToolReading,
    location_ids: list[int],
    job_id: int | None = None,
    matches: Callable[[int], bool] = lambda _spool_id: False,
    claim: int | None = None,
) -> Result:
    """Advance one tool's accounting by one poll.

    `location_ids` are the spools Spoolman has in the tool's location. `job_id` is the
    printer's current job, or None when no job is running; pass None for the bridge's own
    sync jobs. `matches(spool_id)` tells whether the printer reports that spool's
    instance UUID. `claim` is a spool in `location_ids` that the user says is in the tool;
    `Result.claimed` tells whether it was handled. The caller must skip the poll (not
    call this) when Spoolman is unreachable.
    """
    out = Result(state=state)
    # Usage per spool since the tool was last loaded. Every charge counts, so a refund
    # that moves usage to another spool moves it here too.
    window = dict(state.since_load)

    def event(kind: EventKind, message: str) -> None:
        out.events.append(Event(kind, message))

    def charge(target: int, mm: int, reason: str) -> None:
        if mm:
            out.charges.append(Charge(target, mm, reason))
            window[target] = window.get(target, 0) + mm

    assigned = resolve_assignment(state, location_ids)
    ambiguous = isinstance(assigned, list)

    # First sight of this tool: take the current counter as the baseline. Usage before
    # the bridge started isn't attributable.
    if state.boot_id is None and not ambiguous:
        active = assigned
        if claim is not None:
            active, out.claimed = claim, True
            event(EventKind.CLAIM_CONFIRMED, f"spool {claim} claimed; nothing to move")
        out.state = ToolState(
            boot_id=boot_id,
            charged=reading.extruded,
            unload_count=reading.unload_count or 0,
            active=active,
            assigned=assigned,
            job_id=job_id,
            needs_sync=out.claimed,
            extruded=reading.extruded,
        )
        event(EventKind.BASELINE, f"baseline extruded={reading.extruded} spool={active}")
        return out

    s = state
    rebooted = s.boot_id is not None and boot_id != s.boot_id
    if rebooted:
        # Usage held by not advancing `charged` would be lost with the counter.
        kept = max(s.extruded - s.charged, 0)
        event(
            EventKind.REBOOT,
            f"boot_id {s.boot_id} -> {boot_id}; counters restart at 0 "
            "(usage since the last poll before the reboot is lost)"
            + (f"; {kept} mm held before the restart is kept" if kept else ""),
        )
        # Keep job_id: the firmware stores job IDs in EEPROM and never reuses them, so a
        # print resumed after a power loss keeps its ID and isn't a new print start.
        s = replace(
            s, boot_id=boot_id, charged=0, unload_count=0, pending_at=0, held_mm=s.held_mm + kept
        )
        # An unload just before a reboot can be missed, so a claim moves only usage
        # since the reboot.
        window.clear()

    if ambiguous:
        ids = ", ".join(str(i) for i in assigned)
        event(
            EventKind.AMBIGUOUS,
            f"location holds spools {ids}; skipping until it's clear which one is loaded",
        )
        out.skipped = True
        if s.boot_id is not None:
            # Nothing is charged, so the usage is held: keep track of it over a reboot,
            # including one while the location stays ambiguous.
            out.state = replace(
                s,
                extruded=reading.extruded,
                since_load=window,
                reboot_pending=s.reboot_pending or rebooted,
            )
        return out
    evict = tuple(i for i in s.evict if i in location_ids)
    s = replace(s, evict=evict)

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
    staged_notice = s.staged_notice
    provisional = s.provisional
    unloaded = False
    deleted = s.deleted_spool
    held = s.held_mm

    def retire(old: int | None) -> tuple[int, ...]:
        """Mark the outgoing spool for eviction if it's still in the location."""
        if old is None or old not in location_ids or old == assigned or old in evict:
            return evict
        return (*evict, old)

    def charge_held(target: int) -> None:
        """Charge usage held over a reboot, with no deleted spool, to the active spool."""
        nonlocal held, swap, provisional
        charge(target, held, "held before the printer restarted")
        event(
            EventKind.HELD_CHARGED,
            f"spool {target} is active; charged it {held} mm held before the printer restarted",
        )
        # It's usage from before this poll, like usage held by not advancing `charged`.
        if swap is not None and swap.spool_id == target:
            swap = replace(swap, mm=swap.mm + held)
        if provisional is not None and provisional.spool_id == target:
            provisional = replace(provisional, mm=provisional.mm + held)
        held = 0

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
        unloads = f"{n if n > 0 else 'some'} unload(s)"
        if active is not None:
            event(
                EventKind.UNLOAD, f"{unloads}; {before} mm before the unload goes to spool {active}"
            )
        elif deleted is not None:
            event(
                EventKind.UNLOAD,
                f"{unloads}; {before} mm before the unload is held because spool "
                f"{deleted} was deleted",
            )
        else:
            # The LOST_UNASSIGNED warning below reports the usage, so don't repeat it here.
            event(EventKind.UNLOAD, f"{unloads} with no spool active")
        # Usage held over a reboot is usage before the unload too.
        carried = held if deleted is None else 0
        if active is not None and carried:
            charge_held(active)
        if before > 0 or carried:
            if active is not None:
                charge(active, before, "before unload")
                if provisional is not None and provisional.spool_id == active:
                    provisional = replace(provisional, mm=provisional.mm + before)
            elif deleted is not None:
                held += before
            else:
                event(
                    EventKind.LOST_UNASSIGNED, f"{before + carried} mm before unload had no spool"
                )
                held = 0
        if provisional is not None:
            event(
                EventKind.PROVISIONAL_KEPT,
                f"unload keeps the {provisional.mm} mm charged provisionally to spool "
                f"{provisional.spool_id}",
            )
            provisional = None
        s = replace(s, charged=at, unload_count=reading.unload_count)
        window.clear()
        swap = None  # A new unload supersedes any open swap.
        notice = None
        staged_notice = None
        unloaded = True
        if job_id is not None and job_id == s.job_id and active is not None:
            out.runout = active

        if assigned is None and active is not None:
            # S10: not a swap. Usage is held until a spool is assigned.
            event(
                EventKind.ACTIVE_UNASSIGNED,
                f"spool {active} unloaded with no spool in "
                "the location; holding usage until one is assigned",
            )
            evict = retire(active)
            active = None
        elif assigned != active:
            done = f"spool {active} -> {assigned}" if active is not None else f"spool {assigned}"
            event(EventKind.SWAP_COMMITTED, f"swap done: {done}")
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

    # 2. A claim: the user says which spool is in the tool. It replaces the assignment
    # handling below, which would only stage the claimed spool.
    if claim is not None:
        out.claimed = True
        if claim == active:
            closed = (
                f"; the {provisional.mm} mm provisional charge stays"
                if provisional is not None
                else ""
            )
            event(
                EventKind.CLAIM_CONFIRMED,
                f"spool {claim} claimed: confirmed as loaded, nothing moves{closed}",
            )
        else:
            # Charge before refunding: Spoolman clamps used_weight at 0, so a refund
            # applied first could be truncated if the charge then fails.
            moved = {i: mm for i, mm in sorted(window.items()) if i != claim and mm > 0}
            total = sum(moved.values())
            charge(claim, total, "claimed usage since load")
            for i, mm in moved.items():
                charge(i, -mm, "moved to claimed spool")
            since = "reboot" if rebooted or not s.unload_count else "unload"
            sources = ", ".join(f"{mm} mm from spool {i}" for i, mm in moved.items())
            event(
                EventKind.CLAIMED,
                f"spool {claim} claimed in place of spool {active}: moved "
                f"{sources or 'nothing'} (usage since the last {since})",
            )
            evict = tuple(i for i in evict if i != claim)
            if active is not None and active in location_ids and active not in evict:
                evict = (*evict, active)
            active = claim
            resolved = resolve_assignment(replace(s, active=claim, evict=evict), location_ids)
            assigned = claim if isinstance(resolved, list) else resolved
        swap = None
        provisional = None
        staged_notice = None

    # 3. Spoolman assigns a different spool than the active one, with no unload.
    elif unloaded:
        pass
    elif assigned != active:
        if swap is not None and assigned is None:
            # The old spool left first, before the new one arrived. Keep the swap open and
            # hold usage until a spool is assigned: a new spool confirms the swap below.
            event(
                EventKind.ACTIVE_UNASSIGNED,
                f"spool {active} is no longer in the "
                "location; holding usage until a spool is assigned, which also takes "
                f"the {swap.mm} mm since the unload",
            )
            active = None
        elif swap is not None and assigned == swap.spool_id:
            # The old spool is back: as if it had never left, the swap stays open, or closes
            # as of a print start while it was out.
            event(EventKind.ACTIVE_UNASSIGNED, f"spool {assigned} is back in the location")
            active = assigned
            if swap.started_job is not None:
                event(
                    EventKind.SWAP_ASSUMED,
                    f"print {swap.started_job} started with no Spoolman change since the "
                    f"unload; assuming spool {swap.spool_id} was reloaded and charging it "
                    "provisionally",
                )
                provisional = Provisional(swap.spool_id, swap.mm, swap.started_job)
                swap = None
        elif swap is not None:
            moved = swap.mm
            if moved > 0:
                # Charge before refunding: Spoolman clamps used_weight at 0, so a refund
                # applied first could be truncated if the charge then fails.
                charge(assigned, moved, "usage since unconfirmed swap")
                charge(swap.spool_id, -moved, "moved to confirmed swap spool")
            event(
                EventKind.SWAP_CONFIRMED,
                f"swap confirmed: {moved} mm moved from spool {swap.spool_id} to {assigned}",
            )
            swap = None
            evict = retire(active)
            active = assigned
        elif deleted is not None and active is None and assigned == s.assigned:
            # Already in the location when the active spool was deleted: it may not be
            # loaded yet, so keep holding unless the printer reports its UUID.
            if matches(assigned):
                event(
                    EventKind.CORRECTION,
                    f"printer already has spool {assigned}'s filament; making it active",
                )
                active = assigned
        elif legacy or active is None:
            # No unload signal to wait for, or nothing was active: switch now, charging
            # usage so far to the outgoing spool.
            delta = reading.extruded - s.charged
            if legacy and active is not None and delta > 0 and reading.material is not None:
                charge(active, delta, "extruded")
                s = replace(s, charged=reading.extruded)
            if legacy:
                window.clear()  # The switch is the best guess at when the spool went in.
            event(
                EventKind.ASSIGNMENT_CHANGED,
                f"spool {active} -> {assigned}"
                if active is not None
                else f"spool {assigned} is active",
            )
            evict = retire(active)
            active = assigned
        elif assigned is None:
            # Not a swap: nothing is staged, and the active spool is still loaded.
            if s.assigned == active:
                event(
                    EventKind.ACTIVE_UNASSIGNED,
                    f"spool {active} is no longer in the "
                    "location; still charging it until the printer reports an unload",
                )
            elif s.assigned is not None:
                # The staged spool left. The active one was already gone, or left with it.
                event(
                    EventKind.STAGING_CANCELLED,
                    f"spool {s.assigned} is no longer staged; still charging spool {active}",
                )
                event(
                    EventKind.ACTIVE_UNASSIGNED,
                    f"spool {active} isn't in the location "
                    "either; still charging it until the printer reports an unload",
                )
        elif matches(assigned):
            event(
                EventKind.CORRECTION,
                f"printer already has spool {assigned}'s filament; "
                f"switching from spool {active} now",
            )
            evict = retire(active)
            active = assigned
        elif assigned != s.assigned:
            event(
                EventKind.SWAP_STAGED,
                f"spool {assigned} staged; still charging spool "
                f"{active} until the printer reports an unload",
            )

    elif not legacy and active is not None and s.assigned is None and assigned == active:
        event(EventKind.ACTIVE_UNASSIGNED, f"spool {active} is back in the location")
    elif not legacy and active is not None and s.assigned != active and assigned == active:
        event(
            EventKind.STAGING_CANCELLED,
            f"spool {s.assigned} is no longer staged; still charging spool {active}",
        )

    # 4. A print starting closes the confirmation window. A print starts at its first
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
            event(
                EventKind.ASSIGNMENT_CHANGED,
                f"print started with spool {assigned} "
                f"assigned; making it active in place of deleted spool {deleted}",
            )
            active = assigned
        # With the location emptied, no spool is charged to assume: the swap stays open
        # for the next spool assigned. The old spool coming back closes it as of now.
        if swap is not None and active is None and swap.started_job is None:
            swap = replace(swap, started_job=job_id)
        elif swap is not None and active is not None:
            event(
                EventKind.SWAP_ASSUMED,
                f"print started with no Spoolman change since the unload; assuming "
                f"spool {swap.spool_id} was reloaded and charging it provisionally",
            )
            provisional = Provisional(swap.spool_id, swap.mm, job_id)
            swap = None
        if not legacy and assigned is not None and assigned != active:
            msg = (
                f"print started while spool {assigned} is staged but not loaded; "
                f"charging spool {active}"
            )
            event(EventKind.STAGED_AT_PRINT, msg)
            # With a spool assigned, step 3 or the deleted-spool case above made one active.
            assert active is not None
            notice = Notice(active, EventKind.STAGED_AT_PRINT, msg)
            staged_notice = Notice(assigned, EventKind.STAGED_AT_PRINT, msg)

    # A spool is active again after a deletion, including by a claim: it takes the held
    # usage.
    if deleted is not None and active is not None:
        if held:
            charge(active, held, f"held after spool {deleted} was deleted")
            event(
                EventKind.HELD_CHARGED,
                f"spool {active} is active; charged it {held} mm "
                f"held after spool {deleted} was deleted",
            )
        deleted, held = None, 0
    elif held and active is not None:
        charge_held(active)

    # 5. Usage since the last charge goes to the active spool.
    delta = reading.extruded - s.charged
    if delta > 0:
        if reading.material is None:
            event(EventKind.HOLD_EMPTY, "holding usage until filament is reported")
        elif active is None and deleted is not None:
            event(EventKind.HOLD_DELETED, f"holding usage since spool {deleted} was deleted")
            held += delta
            s = replace(s, charged=reading.extruded)
        elif active is None:
            event(EventKind.HOLD_UNASSIGNED, "holding usage: no spool assigned")
        else:
            charge(active, delta, "extruded")
            s = replace(s, charged=reading.extruded)
            if swap is not None and swap.spool_id == active:
                swap = replace(swap, mm=swap.mm + delta)
            if provisional is not None and provisional.spool_id == active:
                provisional = replace(provisional, mm=provisional.mm + delta)
    elif held and active is None and deleted is None:
        # Usage kept over a reboot is still held, though there's no new usage this poll.
        if reading.material is None:
            event(EventKind.HOLD_EMPTY, "holding usage until filament is reported")
        else:
            event(EventKind.HOLD_UNASSIGNED, "holding usage: no spool assigned")

    # 6. Notices that depend on the outcome.
    # After a deletion, the deleted-spool flag already covers the staged spool.
    if (
        (rebooted or s.reboot_pending)
        and not legacy
        and assigned is not None
        and assigned != active
        and deleted is None
    ):
        # `unload_count` restarts with the printer, so an unload just before the reboot
        # can be missed, leaving the staged spool loaded while the old one is charged.
        msg = (
            f"printer restarted while spool {assigned} is staged; an unload just before "
            f"the restart can be missed, so check which spool is loaded. Still "
            f"charging spool {active}"
        )
        event(EventKind.STAGED_AT_REBOOT, msg)
        staged_notice = Notice(assigned, EventKind.STAGED_AT_REBOOT, msg)
    if staged_notice is not None and (staged_notice.spool_id != assigned or assigned == active):
        staged_notice = None
    if provisional is not None and provisional.spool_id != active:
        event(
            EventKind.PROVISIONAL_KEPT,
            f"spool {provisional.spool_id} is no longer active; its {provisional.mm} mm "
            "provisional charge stays",
        )
        provisional = None

    resync = unloaded or active != s.active or out.claimed
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
        staged_notice=staged_notice,
        provisional=provisional,
        since_load=window,
        extruded=reading.extruded,
        reboot_pending=False,
    )
    return out
