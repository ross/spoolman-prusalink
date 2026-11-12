"""The bridge's state in memory, which the database holds between runs."""

from __future__ import annotations

from dataclasses import dataclass, field

from .accounting import ToolState
from .records import Outbox


@dataclass
class Runout:
    """A runout flag, and the spool's weights in Spoolman as the bridge last knew them.

    `net` and `used` start as the weights when the flag was set, and `used` follows the
    bridge's own charges. Any other change to either is the user editing the weight,
    which answers the flag.
    """

    text: str
    net: float
    used: float


@dataclass
class PrinterState:
    tools: dict[int, ToolState] = field(default_factory=dict)
    # The flag texts this printer's tools last asked for, by spool. They stand in while
    # the printer can't be read, so an outage doesn't clear its flags in Spoolman.
    flags: dict[int, list[str]] = field(default_factory=dict)
    # By tool: the instance UUID the printer last reported while the active spool was
    # certain, that spool (None once an unload other than a runout in a print the
    # bridge saw start makes it unknown), and
    # whether the tool had a runout since. Kept so a runout reload of one spool's tag
    # isn't taken for the next spool's after a restart.
    seen_uuids: dict[int, tuple[str, int | None, bool]] = field(default_factory=dict)


@dataclass
class State:
    # By printer name. Everything else here is keyed by spool and shared by every printer.
    printers: dict[str, PrinterState] = field(default_factory=dict)
    # Charges computed but not yet accepted by Spoolman. Saving them with the tool state
    # in one transaction means a crash can't advance `charged` without the charge
    # surviving.
    outbox: Outbox = field(default_factory=Outbox)
    # Spools whose flag field the bridge has set, so it clears only its own flags.
    flagged: list[int] = field(default_factory=list)
    # Flags for spools that ran out mid-print while Spoolman showed plenty left, by
    # spool ID. Each clears when another spool becomes active and then this one does, or
    # when the user edits the spool's weight in Spoolman.
    runouts: dict[int, Runout] = field(default_factory=dict)
    # Spools whose claim the bridge has handled while the box still shows ticked, so a
    # claim is handled once even if clearing the box fails.
    claimed: list[int] = field(default_factory=list)

    def tools(self, printer: str) -> dict[int, ToolState]:
        """Return the tool states of `printer`, creating its entry."""
        return self.printers.setdefault(printer, PrinterState()).tools
