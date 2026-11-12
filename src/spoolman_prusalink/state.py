"""The bridge's state in memory, which the database holds between runs."""

from __future__ import annotations

from dataclasses import dataclass, field

from .accounting import ToolState
from .conditions import Condition
from .printer_sync import WriteState
from .records import Outbox
from .tags import SeenTag


@dataclass
class Runout:
    """A runout flag: a spool that unloaded mid-print while Spoolman showed plenty left.

    `location` is the tool's, and `remaining` is the grams Spoolman showed then.

    `net` and `used` start as the weights when the flag was set, and `used` follows the
    bridge's own charges. Any other change to either is the user editing the weight,
    which answers the flag.
    """

    location: str
    remaining: float
    net: float
    used: float

    @property
    def flag(self) -> str:
        return (
            f"{self.location}: unloaded mid-print with {self.remaining:.0f} g left in "
            "Spoolman. If it ran out, its usage is probably on another spool: check "
            "recent swaps, then correct this spool's weight, which clears this. The "
            "filament may also have jammed or tangled. Ignore this after a planned "
            "color change."
        )


@dataclass
class PrinterState:
    tools: dict[int, ToolState] = field(default_factory=dict)
    # By tool: its conditions as of the last poll that read the printer. Their flags
    # stand while the printer can't be read, so an outage doesn't clear them in Spoolman.
    conditions: dict[int, list[Condition]] = field(default_factory=dict)
    # By tool: what the tool's instance UUID was last seen with. Kept so a runout reload
    # of one spool's tag isn't taken for the next spool's after a restart.
    seen_tags: dict[int, SeenTag] = field(default_factory=dict)
    # By tool: where the bridge is with writing the tool's active spool to the printer.
    writes: dict[int, WriteState] = field(default_factory=dict)


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
