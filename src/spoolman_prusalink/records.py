"""The records the bridge keeps: charges queued for Spoolman and the ledger's entries."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, dataclass

from .events import Event


@dataclass(frozen=True)
class ChargeRecord:
    """A charge to a spool: queued in the outbox, then written to the ledger once Spoolman
    answers it."""

    time: str  # When the bridge computed it, ISO 8601 in UTC.
    printer: str
    tool: int
    location: str
    spool_id: int
    extruded_mm: int  # Raw extruded millimeters; negative to refund usage moved away.
    use_length: float  # What Spoolman is sent: `extruded_mm` times the length factor.
    grams: float | None  # The weight, when the spool's density and diameter are known.
    reason: str
    boot_id: int
    extruded: int  # The tool's counter when it was computed.
    # How Spoolman answered, set when the charge leaves the outbox.
    applied: str | None = None  # When Spoolman accepted it.
    failed: str | None = None  # The HTTP status it was rejected with.
    held: bool = False  # Rejected for a deleted spool, and held for the tool's next spool.

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> ChargeRecord:
        return cls(**data)


@dataclass(frozen=True)
class EventRecord:
    """An event, with where and when it happened, for the ledger."""

    time: str
    printer: str
    tool: int
    location: str
    event: Event


class Outbox:
    """Charges computed but not yet accepted by Spoolman, oldest first.

    Charges join at the back with `push` and leave from the front with `pop`. The outbox
    knows the database row of each saved charge, and what changed since the last save,
    so `Database.save` writes only that.
    """

    def __init__(self, saved: list[tuple[int, ChargeRecord]] | None = None) -> None:
        self._saved = list(saved or [])  # (row id, charge), oldest first.
        self._new: list[ChargeRecord] = []  # Pushed since the last save, oldest first.
        self._popped: list[int] = []  # Rows of saved charges popped since the last save.

    def __len__(self) -> int:
        return len(self._saved) + len(self._new)

    def __eq__(self, other: object) -> bool:
        """Outboxes are equal when they hold the same charges in order, saved or not."""
        return isinstance(other, Outbox) and list(self) == list(other)

    def __repr__(self) -> str:
        return f"Outbox({list(self)!r})"

    def __iter__(self) -> Iterator[ChargeRecord]:
        yield from (rec for _, rec in self._saved)
        yield from self._new

    def push(self, rec: ChargeRecord) -> None:
        self._new.append(rec)

    def queued_mm(self, printer: str, tool: int, spool_id: int) -> int:
        """Return the usage a tool's queued charges to a spool hold, refunds left out."""
        return sum(
            r.extruded_mm
            for r in self
            if (r.printer, r.tool, r.spool_id) == (printer, tool, spool_id) and r.extruded_mm > 0
        )

    def front(self) -> ChargeRecord:
        return self._saved[0][1] if self._saved else self._new[0]

    def pop(self) -> ChargeRecord:
        """Remove the oldest charge and return it."""
        if self._saved:
            row, rec = self._saved.pop(0)
            self._popped.append(row)
            return rec
        return self._new.pop(0)

    def changes(self) -> tuple[list[int], list[ChargeRecord]]:
        """The rows to delete and the charges to insert, oldest first, since the last save."""
        return list(self._popped), list(self._new)

    def saved(self, rows: list[int]) -> None:
        """Record a save of `changes()`: `rows` are the new charges' rows, in order."""
        assert len(rows) == len(self._new)
        self._saved += zip(rows, self._new, strict=True)
        self._new = []
        self._popped = []
