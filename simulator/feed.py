"""Feed systems: the rules that differ between a direct-drive printer and a toolchanger.

A model's feed system follows from its build. Each system is a stateless singleton in
`FEED_SYSTEMS`, so snapshots and `asdict` never see it. `model` holds the state the rules
act on, and `SimPrinter.feed` picks the printer's system.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from simulator.model import SimPrinter
    from simulator.tool import SimTool


class Feed(StrEnum):
    DIRECT = "direct"
    TOOLCHANGER = "toolchanger"


class FeedSystem:
    selectable: ClassVar[bool]  # the user can pick a tool (select_tool)
    tool_mapping: ClassVar[bool]  # the preview's tools-mapping screen applies (HAS_TOOL_MAPPING)

    def tool_change(self, p: SimPrinter, old: SimTool, new: SimTool) -> None:
        """A print's change from `old` to `new`: pick `new` and count what the change moves."""
        p.selected_tool = new.index
        p._log("job", f"tool change T{old.index} -> T{new.index}")
        # Nothing checks for filament at a pickup, and a runout needs the sensor to go from
        # filament to none, so an empty tool prints on and counts what it would have fed
        # (`check_runout` in `src/feature/filament_sensor/filament_sensors_handler.cpp`,
        # `check_for_events` in `filament_sensor.cpp`).
        p._track(new, p.settings.toolchange_purge_mm)


class Direct(FeedSystem):
    """One tool fed straight from its spool: a print's segments all use tool 0."""

    selectable = False
    tool_mapping = False


class Toolchanger(FeedSystem):
    """Several tools, each with its own spool, that a print picks up in turn (XL, INDX)."""

    selectable = True
    tool_mapping = True


FEED_SYSTEMS: dict[Feed, FeedSystem] = {Feed.DIRECT: Direct(), Feed.TOOLCHANGER: Toolchanger()}
