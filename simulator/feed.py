"""Feed systems: the rules that differ between a direct-drive printer and the toolchangers.

A model's feed system follows from its build: `ModelInfo.feed`, which `script/firmware-check`
compares with `HAS_TOOLCHANGER` and `HAS_INDX`. Each system is a stateless singleton in
`FEED_SYSTEMS`, so snapshots and `asdict` never see it. The MMU's system, `MMU`, follows
from the MMU's setting instead, so it stands outside that table. `model` holds the state the
rules act on, and `SimPrinter.feed` picks the printer's system.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from simulator.mmu import SINGLE_TOOL_LOAD

if TYPE_CHECKING:
    from simulator.model import SimPrinter, UserJob
    from simulator.slicer import MmuSlicer, Slicer
    from simulator.tool import SimTool


class Feed(StrEnum):
    DIRECT = "direct"
    XL = "xl"  # HAS_TOOLCHANGER without HAS_INDX: the XL's Dwarf heads
    INDX = "indx"  # HAS_INDX: the Core One INDX and Core One L INDX


class FeedSystem:
    selectable: ClassVar[bool]  # the user can pick a tool (select_tool)
    docks: ClassVar[bool] = False  # whether the picked tool goes back to a dock (INDX)
    # Whether an E move runs with no tool credited: it then counts nothing and leaves the
    # gap where it is. Elsewhere it can't run (`credited`).
    moves_without_tool: ClassVar[bool] = False

    def maps_tools(self, p: SimPrinter) -> bool:
        """Whether the preview's tools-mapping screen applies (`HAS_TOOL_MAPPING`). A direct
        printer has no tool mapping."""
        return False

    def profile(self, p: SimPrinter) -> Slicer | None:
        """The multi-tool slicer profile `p` prints with. A direct printer has none: its
        single-tool profiles add no moves the simulator models."""
        return None

    def head_tool(self, p: SimPrinter) -> SimTool | None:
        """The tool whose path the head's own moves run on: the picked tool, or None on a
        toolchanger with none picked."""
        return None if p.selected_tool is None else p.tools[p.selected_tool]

    def select_for_print(self, p: SimPrinter, tool: SimTool) -> None:
        """Make `tool` the picked tool as a print starts its preheat and each time it starts
        feeding: the start G-code's tool, or the one a resume found picked."""
        p.selected_tool = tool.index

    def prime(self, p: SimPrinter, job: UserJob, slicer: Slicer | None) -> None:
        """The start G-code's moves outside the model, run once as the print first feeds.
        `slicer` is the print's profile with its overrides. A direct printer has none."""

    def end_moves(self, p: SimPrinter, job: UserJob, slicer: Slicer | None) -> None:
        """A finished print's end G-code moves outside the model, before the firmware's own
        end. `slicer` is the print's profile with its overrides. A direct printer has none."""

    def end_unload(self, p: SimPrinter, t: SimTool) -> None:
        """End a menu unload of `t` once its moves have run: the filament leaves the path and
        the type clears. The picked tool then goes to rest, unless a change all goes on with
        a load of `t`: `M702` docks after every unload on INDX (`M701_2.cpp:194-197`), and
        `M1600` docks only after its load, so a change all's unload keeps the tool for the
        load that follows."""
        p._physical_unload(t, "unload")
        if not (p.queue and p.queue[0].tool == t.index):
            self.rest(p)

    def tool_change(
        self, p: SimPrinter, old: SimTool, new: SimTool, slicer: Slicer, first: bool
    ) -> None:
        """A print's change from `old` to `new`: the profile's moves on `old` before and
        during its park, the pick of `new`, then the profile's moves on `new`, those of its
        first pick in the print when `first` is set.

        The pick and the park move no counted filament (`_pick`). Nothing checks for
        filament at a pickup, and a runout needs the sensor to go from filament to none, so
        an empty tool prints on and counts what it would have fed (`check_runout` in
        `src/feature/filament_sensor/filament_sensors_handler.cpp`, `check_for_events` in
        `filament_sensor.cpp`).
        """
        p._print_moves(old, slicer.park_out)
        p.selected_tool = new.index
        p._log("job", f"tool change T{old.index} -> T{new.index}")
        p._print_moves(new, (slicer.first_pick if first else slicer.later_pick).moves())

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
        """Put the picked tool where it rests after a menu load or unload, a pause, and a
        print's end. A direct printer and the XL leave it picked: the XL has no dock step
        (`HAS_INDX` only, `M701_2.cpp`, `marlin_server.cpp`)."""


class Direct(FeedSystem):
    """One tool fed straight from its spool: a print's segments all use tool 0."""

    selectable = False


class Toolchanger(FeedSystem):
    """Several tools, each with its own spool, that a print picks up in turn (XL, INDX)."""

    selectable = True

    def maps_tools(self, p: SimPrinter) -> bool:
        return True

    def prime(self, p: SimPrinter, job: UserJob, slicer: Slicer | None) -> None:
        """The profile's start moves (`Slicer`), when the printer has a profile.

        `G427` picks each used tool in ascending order and purges it in the nozzle cleaner
        (`run` and `prepare_tool` in
        `src/feature/tool_offset_calibration/tool_offset_calibration.cpp:186-233, 663-850`).
        """
        if slicer is None:
            return
        # The start G-code's tools go through the tool mapping too.
        tools = {p._seg_tool(job, i) for i in range(len(job.spec.segments))}
        first = p.tools[p._seg_tool(job, 0)]
        used = [p.tools[i] for i in sorted(tools)]
        p._pick(first)
        p._print_moves(first, slicer.cleanup)
        if slicer.calibrates:
            for t in used:
                p._pick(t)
                p._purge_clean(t)
        if slicer.prime_others is not None:
            for t in used:
                if t is not first:
                    p._pick(t)
                    p._print_moves(t, slicer.prime_others.moves())
        p._pick(first)
        p._print_moves(first, slicer.prime.moves())
        p._log("job", f"start G-code primed {', '.join(f'T{t.index}' for t in used)}")

    def end_moves(self, p: SimPrinter, job: UserJob, slicer: Slicer | None) -> None:
        """The profile's last moves on the head's tool, the wipe tower's when the print used
        several tools, then the end G-code's park (`P0 S1`, `src/marlin_stubs/P.cpp:35-48`),
        when the printer has a profile."""
        if slicer is None:
            return
        t = p._head_tool()
        if len({s.tool for s in job.spec.segments}) > 1:
            p._print_moves(t, slicer.tower_end)
        p._print_moves(t, slicer.end)
        p._log("job", f"end G-code parks T{t.index}")
        p.selected_tool = None

    def profile(self, p: SimPrinter) -> Slicer | None:
        """On a toolchanger only. An XL with one tool isn't one (`SimPrinter.toolchanger`),
        and its single-tool profiles inherit `*commonXL*`, whose end G-code doesn't park, so
        it prints as a direct printer does."""
        return p.model.slicer if p.toolchanger else None

    def pick(self, p: SimPrinter, tool: SimTool) -> None:
        """Pick `tool` up, parking the picked one first. Neither moves counted filament."""
        p.selected_tool = tool.index
        p._log("load", f"pick T{tool.index}")


class XlToolchanger(Toolchanger):
    """The XL: a Dwarf head per tool (`toolchanger_xl.cpp`)."""


class IndxToolchanger(Toolchanger):
    """INDX: tools picked from docks onto one head (`toolchanger_indx.cpp`)."""

    docks = True

    def rest(self, p: SimPrinter) -> None:
        """Dock the picked tool, if one is picked: `tool_change(NoTool {}, dock_backoff)`
        after `M701`, unless it resumes a print, and after every `M702`
        (`M701_2.cpp:135-140, 194-197`); `tool_change(NoTool {})` at a pause, in
        `Pausing_ParkHead`, and at a print's end, in `pre_finalize_print`
        (`marlin_server.cpp:2557-2563, 1095-1100`). The dock moves no E."""
        if p.selected_tool is None:
            return
        p._log("load", f"dock T{p.selected_tool}")
        p.selected_tool = None


class Mmu(FeedSystem):
    """An MMU3 switched on: five slots fed one at a time into the one extruder, which share
    its path (`simulator.mmu`). Only a load or a print's tool change selects a slot, and
    every unload, eject and cut, a boot and `M709 S1` leave none selected
    (`mmu2_mk4.cpp:155, 175, 674, 869-871, 913, 1013`)."""

    selectable = False
    moves_without_tool = True

    def maps_tools(self, p: SimPrinter) -> bool:
        """The tools-mapping screen applies while the MMU is Active, not while it connects
        (`is_tool_mapping_possible`, `src/common/tools_mapping.cpp:26-36`)."""
        return p.mmu.active

    def head_tool(self, p: SimPrinter) -> SimTool | None:
        """The selected slot, else slot 0: the one physical tool is always there, and every
        slot's path is the shared one, so only `credited` decides what a move counts on."""
        return p.tools[0 if p.selected_tool is None else p.selected_tool]

    def select_for_print(self, p: SimPrinter, tool: SimTool) -> None:
        """Nothing: only a `T`, which loads the slot, selects one."""

    def credited(self, p: SimPrinter, tool: SimTool) -> SimTool | None:
        """Slot 0 while the MMU isn't Active, else the selected slot or none, whichever slot
        `tool` is: the slots share one path (`currently_selected_virtual_tool`,
        `src/common/tool_index.cpp:140-158`). With none, the planner runs the move and
        counts nothing (`planner.cpp:2206-2219`)."""
        if not p.mmu.active:
            return p.tools[0]
        return None if p.selected_tool is None else p.tools[p.selected_tool]

    def idle_clear_tool(self, p: SimPrinter) -> SimTool | None:
        """None: the clear is off while the MMU is switched on, connecting too (`has_mmu`,
        `filament_sensors_handler.cpp:161-182, 303-312`)."""
        return None

    def end_unload(self, p: SimPrinter, t: SimTool) -> None:
        """End the MMU menu's Unload Filament. Its last step left no slot selected, and it
        writes no type, so nothing counts: the MMU's unload never reaches the write of none
        in `unload_process` (`pause.cpp:969-973, 1263-1278`)."""
        p._log("load", f"T{t.index} unloaded into the MMU: no type written")

    def profile(self, p: SimPrinter) -> MmuSlicer | None:
        """The model's stock MMU3 profile."""
        return p.model.mmu_slicer

    def prime(self, p: SimPrinter, job: UserJob, slicer: MmuSlicer | None) -> None:
        """The print's start, before its first segment.

        A file that uses tool 0 alone, by its filament use, gets the firmware's own load
        first when the MMU is Active and no slot is selected: a `T0` through the tool
        mapping, then 35 mm
        (`PrintInit`, `marlin_server.cpp:2492-2518`). A single-tool file stops there, and
        a one-color profile file for slot 0 goes on with its start, whose `T0` then finds
        the slot selected. The profile's start retracts with no slot selected yet, so
        uncounted, sends `T<first>`, whose feed counts on no slot either, and loads and
        purges the first slot (`MmuSlicer`). Its second `T<first>` after the start changes
        nothing: that it's a no-op in the firmware is unverified.
        """
        first = p.tools[p._seg_tool(job, 0)]
        single = all(s.tool == 0 for s in job.spec.segments)
        if p.mmu.active and p.selected_tool is None and single:
            p._log("job", "a file on tool 0 alone: the firmware loads T0")
            p._mmu_change(first)
            p._print_moves(first, SINGLE_TOOL_LOAD)
        if slicer is None:
            return
        p._print_moves(p._head_tool(), slicer.cleanup)
        p._mmu_change(first)
        p._print_moves(first, (*slicer.start_load, *slicer.purge_line))
        p._mmu_change(first)
        p._log("job", f"start G-code primed T{first.index}")

    def tool_change(
        self, p: SimPrinter, old: SimTool, new: SimTool, slicer: MmuSlicer, first: bool
    ) -> None:
        """A print's change from `old` to `new`: the wipe tower's unload of the old slot's
        filament, the firmware's change, then the new slot's load and purge, those of its
        first pick in the print when `first` is set (`MmuSlicer`, `SimPrinter._mmu_change`).

        Each segment's filament is the type the file names for its tool. The firmware's
        change pushes the new slot's filament, but it counts on the old slot.
        """
        segments, i = p.job.spec.segments, p.job.segment
        p._print_moves(old, slicer.filament(segments[i - 1].material).park_out())
        p._mmu_change(new)
        p._print_moves(new, slicer.filament(segments[i].material).pick(first))

    def end_moves(self, p: SimPrinter, job: UserJob, slicer: MmuSlicer | None) -> None:
        """A finished print's end, when it was sliced with the profile: where it used several
        slots, the wipe tower's last unload with no `T`, then the print's last retract, then
        the end G-code's `M702`, which unloads the slot into the MMU on the selected slot
        (`M701_2.cpp`, `MMU2::unload`)."""
        if slicer is None:
            return
        t = p._head_tool()
        filament = slicer.filament(job.spec.segments[-1].material)
        if len({s.tool for s in job.spec.segments}) > 1:
            p._print_moves(t, filament.park_out())
        p._print_moves(t, (-filament.retract,))
        p._log("job", "end G-code M702: unload into the MMU")
        p._run_steps(t, p._mmu_unload_steps())


# Outside `FEED_SYSTEMS`: a model's build decides its feed system, and an MMU is switched
# on and off at run time (`SimPrinter.feed`).
MMU = Mmu()


FEED_SYSTEMS: dict[Feed, FeedSystem] = {
    Feed.DIRECT: Direct(),
    Feed.XL: XlToolchanger(),
    Feed.INDX: IndxToolchanger(),
}
