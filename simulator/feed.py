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

    def credited(self, p: SimPrinter, tool: SimTool) -> SimTool | None:
        """The tool a move on `tool`'s path counts on; None when the move can't be made.

        A toolchanger with no tool picked can't move E at all (`bsod("E move without tool")`
        in `Planner::buffer_segment`, `lib/Marlin/Marlin/src/module/planner.cpp`). The
        caller names the tool whose path moves, so a menu load on a toolchanger's unpicked
        tool counts on that tool.
        """
        return None if p.selected_tool is None else tool

    def idle_clear_tool(self, p: SimPrinter) -> SimTool | None:
        """The tool whose sensor the idle clear reads, or None when it doesn't run.

        The clear reads the picked tool only, so with none picked it doesn't run
        (`src/feature/filament_sensor/filament_sensors_handler.cpp`). A selected index that
        isn't a configured tool has no sensor to read.
        """
        return None if p.selected_tool is None else p.tools.get(p.selected_tool)


class Direct(FeedSystem):
    """One tool fed straight from its spool: a print's segments all use tool 0."""

    selectable = False
    tool_mapping = False


class Toolchanger(FeedSystem):
    """Several tools, each with its own spool, that a print picks up in turn (XL, INDX)."""

    selectable = True
    tool_mapping = True


FEED_SYSTEMS: dict[Feed, FeedSystem] = {Feed.DIRECT: Direct(), Feed.TOOLCHANGER: Toolchanger()}
