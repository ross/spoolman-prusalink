"""What's wrong with a tool right now, as typed records.

A condition lasts from the poll that first finds it to the poll that no longer does.
Each kind is a frozen dataclass whose fields are the facts, and its log line and its
Spoolman flag are both rendered from them, so a wording fix reaches conditions that are
already saved. `log_changes()` logs each condition when it starts, changes, and ends,
and `flags()` collects the flag texts.
"""

from __future__ import annotations

import logging
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, fields
from typing import ClassVar

from .drift import Difference, describe
from .events import Event, EventKind, StagedAtPrint, StagedAtReboot
from .models import Spool

log = logging.getLogger("spoolman_prusalink")

UUID_FIELD_NAME = "Instance UUID"
CLAIM_FIELD_NAME = "Claim held usage"


@dataclass(frozen=True)
class Context:
    """What rendering a tool's conditions needs beyond their own fields."""

    location: str
    spools: Mapping[int, Spool]  # By ID, to show an amount in grams.
    length_factor: float = 1.0
    claim_field: bool = False  # Whether spools have the claim box.
    flag_drift: bool = False  # Whether drift the bridge leaves alone is flagged.


@dataclass(frozen=True)
class Condition:
    """A condition. Each subclass is one kind: `class X(Condition, name="x")`.

    A kind with `compared` is about how the printer compares with the active spool. A
    poll that can't compare them leaves it standing: see `standing()`.

    A kind that can be flagged has the spool the flag goes on as `spool`.
    """

    name: ClassVar[str]
    compared: ClassVar[bool]
    # The class for each name, filled in as the subclasses are defined.
    classes: ClassVar[dict[str, type[Condition]]] = {}

    def __init_subclass__(cls, name: str | None = None, compared: bool = False, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if name is None:
            return  # A base for other kinds, not a kind.
        assert name not in Condition.classes, name
        cls.name = name
        cls.compared = compared
        Condition.classes[name] = cls

    @property
    def key(self) -> Hashable:
        """What the condition is about. One with the same key and other fields is the
        same condition with new detail, and it ends when its key goes away."""
        return (self.name, self.spool)

    def log(self, ctx: Context) -> str | None:
        """Return the log text, without the location, or None if it isn't logged."""
        return None

    def flag(self, ctx: Context) -> str | None:
        """Return the flag text, without the location, or None if it isn't flagged."""
        return None

    def to_json(self) -> dict:
        data: dict = {"kind": self.name}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "differences":
                value = [d.to_json() for d in value]
            elif f.name == "event":
                value = {"kind": value.kind.value, **value.fields()}
            data[f.name] = value
        return data

    @staticmethod
    def from_json(data: dict) -> Condition:
        values = dict(data)
        cls = Condition.classes[values.pop("kind")]
        if "differences" in values:
            values["differences"] = tuple(Difference.from_json(d) for d in values["differences"])
        if "event" in values:
            event = dict(values["event"])
            kind = EventKind(event.pop("kind"))
            # JSON has no tuples, and an event's sequences are all tuples.
            event = {k: tuple(v) if isinstance(v, list) else v for k, v in event.items()}
            values["event"] = Event.classes[kind](**event)
        return cls(**values)


@dataclass(frozen=True, kw_only=True)
class Logged(Condition):
    """A condition that's logged. The code that creates it gives the level."""

    level: int


# Log only.


@dataclass(frozen=True)
class ToolNotReported(Logged, name="tool_not_reported"):
    tool: int

    @property
    def key(self) -> Hashable:
        return self.name

    def log(self, ctx: Context) -> str:
        return f"printer doesn't report tool index {self.tool}"


@dataclass(frozen=True)
class Lasting(Logged, name="lasting"):
    """An event that `account()` repeats every poll while it lasts (`Report.LASTING`)."""

    event: Event

    @property
    def key(self) -> Hashable:
        return self.event.kind

    def log(self, ctx: Context) -> str:
        return self.event.message


# From the tool's saved state. Flag only: the event that led to each was logged once.


@dataclass(frozen=True)
class HeldForDeleted(Condition, name="held_for_deleted"):
    spool: int  # The assigned spool, which gets the usage.
    deleted_spool: int

    def flag(self, ctx: Context) -> str:
        return (
            f"usage is being held because spool {self.deleted_spool} was "
            "deleted. It goes to this spool at the next unload or print start, "
            "or when this spool is claimed."
        )


@dataclass(frozen=True)
class ArchivedLoaded(Condition, name="archived_loaded"):
    spool: int

    def flag(self, ctx: Context) -> str:
        return "spool is archived but still loaded"


@dataclass(frozen=True)
class UnloadedMovedOut(Condition, name="unloaded_moved_out"):
    spool: int

    def flag(self, ctx: Context) -> str:
        return (
            "filament was unloaded, then this spool was moved out. "
            f"Usage since the unload goes to the next spool moved into {ctx.location}."
        )


@dataclass(frozen=True)
class UnloadUnconfirmed(Condition, name="unload_unconfirmed"):
    spool: int

    def flag(self, ctx: Context) -> str:
        return (
            "filament was unloaded with no Spoolman change. If a "
            f"different spool went in, move it into {ctx.location} before the next print."
        )


@dataclass(frozen=True)
class LoadedWhileStaged(Condition, name="loaded_while_staged"):
    """The notice on the active spool (`ToolState.notice`)."""

    event: StagedAtPrint

    @property
    def spool(self) -> int:
        return self.event.active

    def flag(self, ctx: Context) -> str:
        return f"{self.event.message}."


@dataclass(frozen=True)
class StagedNotLoaded(Condition, name="staged_not_loaded"):
    """The notice on the staged spool (`ToolState.staged_notice`)."""

    event: StagedAtPrint | StagedAtReboot

    @property
    def spool(self) -> int:
        return self.event.staged

    def flag(self, ctx: Context) -> str:
        hint = (
            f" If this spool is the one loaded, tick '{CLAIM_FIELD_NAME}' on it."
            if ctx.claim_field
            else ""
        )
        return f"{self.event.message}.{hint}"


@dataclass(frozen=True)
class ProvisionalCharge(Condition, name="provisional_charge"):
    spool: int
    # The amount the flag shows. It grows during a print, and `provisional()` keeps it
    # as it was until the print ends, so the flag isn't rewritten on every poll.
    mm: int
    job_id: int | None

    def flag(self, ctx: Context) -> str:
        length = round(self.mm * ctx.length_factor, 1)
        spool = ctx.spools.get(self.spool)
        grams = spool.mm_to_grams(length) if spool else None
        amount = f"{length:g} mm" + (f" (≈ {grams:.0f} g)" if grams is not None else "")
        how = (
            f"move that spool into {ctx.location} and tick '{CLAIM_FIELD_NAME}' on it to "
            "move the usage there"
            if ctx.claim_field
            else "move the usage in Spoolman by hand"
        )
        return (
            "a print started with no Spoolman change since the unload, "
            f"so this spool was assumed reloaded and {amount} is charged to it "
            f"provisionally. If a different spool is loaded, {how}. The next unload "
            "keeps the usage here."
        )


def provisional(
    spool: int, mm: int, job_id: int | None, last: Iterable[Condition], *, printing: bool
) -> ProvisionalCharge:
    """Return the condition for a provisional charge of `mm` so far.

    During a print it's the one in `last`, the tool's conditions at the last poll, if
    that's for the same charge: its amount stands until the print ends.
    """
    if printing:
        for c in last:
            if isinstance(c, ProvisionalCharge) and (c.spool, c.job_id) == (spool, job_id):
                return c
    return ProvisionalCharge(spool, mm, job_id)


# From comparing the printer with the active spool.


@dataclass(frozen=True)
class UuidConflict(Logged, name="uuid_conflict", compared=True):
    """The loaded tag's instance UUID differs from the spool's in Spoolman."""

    spool: int
    tag_uuid: str
    spool_uuid: str

    @property
    def key(self) -> Hashable:
        return ("uuid", self.spool)

    def log(self, ctx: Context) -> str:
        return (
            f"the tag loaded has instance UUID {self.tag_uuid}, but "
            f"spool {self.spool} has {self.spool_uuid} in Spoolman"
        )

    def flag(self, ctx: Context) -> str:
        return (
            f"the loaded tag's instance UUID is {self.tag_uuid}, but "
            f"this spool's is {self.spool_uuid}. If the tag is this spool's, set "
            f"'{UUID_FIELD_NAME}' to the tag's UUID or clear it. If not, move the tag's "
            f"spool into {ctx.location} and tick '{CLAIM_FIELD_NAME}' on it."
        )


@dataclass(frozen=True)
class Mismatch(Logged):
    """The printer's filament differs from the active spool. The kinds are what the
    bridge does about it, and they share a key: one replaces another without ending."""

    spool: int

    @property
    def key(self) -> Hashable:
        return ("printer", self.spool)


@dataclass(frozen=True)
class PrinterUnwritable(Mismatch, name="printer_unwritable", compared=True):
    reason: str  # Why the bridge can't write to this printer at all.

    def log(self, ctx: Context) -> str:
        return f"can't write spool {self.spool} to the printer: {self.reason}"

    def flag(self, ctx: Context) -> str:
        return f"can't write to the printer: {self.reason}"


@dataclass(frozen=True)
class SpoolUnwritable(Mismatch, name="spool_unwritable", compared=True):
    reason: str  # What the spool lacks, from `CannotSync`.

    def log(self, ctx: Context) -> str:
        return f"can't write spool {self.spool} to the printer: {self.reason}"

    def flag(self, ctx: Context) -> str:
        return self.reason


@dataclass(frozen=True)
class WritePending(Mismatch, name="write_pending", compared=True):
    differences: tuple[Difference, ...]

    def log(self, ctx: Context) -> str:
        return f"printer will be set to spool {self.spool} when idle: {describe(self.differences)}"


@dataclass(frozen=True)
class WriteWaitsForLoad(Mismatch, name="write_waits_for_load", compared=True):
    """A swap is staged: the write waits for the assigned spool to be loaded."""

    assigned: int

    def log(self, ctx: Context) -> str:
        return f"printer will be set once spool {self.assigned} is loaded"


@dataclass(frozen=True)
class WriteDidntTake(Mismatch, name="write_didnt_take", compared=True):
    attempts: int
    differences: tuple[Difference, ...]

    def log(self, ctx: Context) -> str:
        return (
            f"printer still differs from spool {self.spool} after "
            f"{self.attempts} sync jobs: {describe(self.differences)}"
        )

    def flag(self, ctx: Context) -> str:
        return f"writing this spool to the printer didn't take: {describe(self.differences)}"


@dataclass(frozen=True)
class Drift(Mismatch, name="drift", compared=True):
    """A difference the bridge leaves on the printer."""

    differences: tuple[Difference, ...]

    def log(self, ctx: Context) -> str:
        return f"printer differs from spool {self.spool}: {describe(self.differences)}"

    def flag(self, ctx: Context) -> str | None:
        if not ctx.flag_drift:
            return None
        return f"printer filament differs: {describe(self.differences)}"


def standing(
    last: Iterable[Condition], now: Iterable[Condition], *, compared: bool
) -> list[Condition]:
    """Return a tool's conditions after a poll that found `now`.

    Without `compared`, the poll didn't compare the printer with the active spool, so
    the conditions in `last` about that stand as they were.
    """
    found = list(now)
    if not compared:
        keys = {c.key for c in found}
        found += [c for c in last if c.compared and c.key not in keys]
    return found


# What's been logged for a tool: each condition's level and text, by key.
LoggedTexts = dict[Hashable, tuple[int, str]]


def log_changes(last: LoggedTexts, conditions: Iterable[Condition], ctx: Context) -> LoggedTexts:
    """Log each condition that's new or changed since `last`, and each that's gone.

    Each is logged at its own level, and its end at the level it started at. Returns
    what to pass as `last` at the next poll.
    """
    now: LoggedTexts = {}
    for c in conditions:
        text = c.log(ctx)
        if text is not None:
            now[c.key] = (c.level, text)
    for key, (level, text) in last.items():
        if key not in now:
            log.log(level, "[%s] no longer: %s", ctx.location, text)
    for key, (level, text) in now.items():
        if key not in last or last[key][1] != text:
            log.log(level, "[%s] %s", ctx.location, text)
    return now


def flags(conditions: Iterable[Condition], ctx: Context) -> dict[int, list[str]]:
    """Return the conditions' flag texts by spool, in order."""
    found: dict[int, list[str]] = {}
    for c in conditions:
        text = c.flag(ctx)
        if text is not None:
            found.setdefault(c.spool, []).append(f"{ctx.location}: {text}")
    return found
