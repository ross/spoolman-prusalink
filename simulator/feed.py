"""Feed systems: the rules that differ between a direct-drive printer and the toolchangers.

A model's feed system follows from its build: `ModelInfo.feed`, which `script/firmware-check`
compares with `HAS_TOOLCHANGER` and `HAS_INDX`. Each system is a stateless singleton in
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
    XL = "xl"  # HAS_TOOLCHANGER without HAS_INDX: the XL's Dwarf heads
    INDX = "indx"  # HAS_INDX: the Core One INDX and Core One L INDX


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
        caller names the tool whose path moves. Every caller picks that tool first, as the
        firmware does, so on a toolchanger it's the picked tool.
        """
        return None if p.selected_tool is None else tool

    def idle_clear_tool(self, p: SimPrinter) -> SimTool | None:
        """The tool whose sensor the idle clear reads, or None when it doesn't run.

        The clear reads the picked tool only, so with none picked it doesn't run
        (`src/feature/filament_sensor/filament_sensors_handler.cpp`). A selected index that
        isn't a configured tool has no sensor to read.
        """
        return None if p.selected_tool is None else p.tools.get(p.selected_tool)

    def pick(self, p: SimPrinter, tool: SimTool) -> None:
        """Pick `tool` up. A direct printer's one tool is always picked."""

    def rest(self, p: SimPrinter) -> None:
        """Put the picked tool where it rests after a menu load or unload. A direct printer
        and the XL leave it picked: the XL has no dock step there (`M701_2.cpp`)."""


class Direct(FeedSystem):
    """One tool fed straight from its spool: a print's segments all use tool 0."""

    selectable = False
    tool_mapping = False


class Toolchanger(FeedSystem):
    """Several tools, each with its own spool, that a print picks up in turn (XL, INDX)."""

    selectable = True
    tool_mapping = True

    def pick(self, p: SimPrinter, tool: SimTool) -> None:
        """Pick `tool` up, parking the picked one first. Neither moves counted filament."""
        p.selected_tool = tool.index
        p._log("load", f"pick T{tool.index}")


class XlToolchanger(Toolchanger):
    """The XL: a Dwarf head per tool (`toolchanger_xl.cpp`)."""


class IndxToolchanger(Toolchanger):
    """INDX: tools picked from docks onto one head (`toolchanger_indx.cpp`)."""

    def rest(self, p: SimPrinter) -> None:
        """Dock the picked tool: `tool_change(NoTool {}, dock_backoff)` after `M701`, unless
        it resumes a print, and after every `M702` (`M701_2.cpp:135-140, 194-197`). The
        dock moves no E."""
        p._log("load", f"dock T{p.selected_tool}")
        p.selected_tool = None


FEED_SYSTEMS: dict[Feed, FeedSystem] = {
    Feed.DIRECT: Direct(),
    Feed.XL: XlToolchanger(),
    Feed.INDX: IndxToolchanger(),
}
