"""The MMU3: the firmware's constants for it and the unit's state.

An MMU3 feeds five slots, one at a time, into a printer's one extruder. The firmware treats
each slot as a tool: it stores a type and counts per slot, and keeps one filament path, one
gap and one auto-retract distance for the extruder they share (`tool_index.hpp`,
`filament_tracker.hpp`).

The lengths are the load-cell builds', the MK4's, Core One's and Core One L's, which share
them: those builds feed with E-stall detection, not the MK3.5's try-load
(`lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_config.h:11-16`). `script/firmware-check`
compares `MMU_SLOTS` with the MMU builds' `EXTRUDERS`, and the lengths and `MMU_RAMMING`
with those builds' source.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from simulator.tool import EPSILON, FilamentPath, Ramming

# `VirtualToolIndex::count` on an MMU build: `EXTRUDERS` is 6, "5 + NoTool"
# (`include/marlin/Configuration_MK4.h:143-147`, `src/common/tool_index.hpp:29-43`).
MMU_SLOTS = 5

# In `mmu2_config.h`: the retract a tool change makes first when a slot is selected
# (`MMU2_RETRY_UNLOAD_FINISH_LENGTH`), the feed past the extruder's sensor before the
# firmware's +2 (`MMU2_TOOL_CHANGE_LOAD_LENGTH`, the MMU's initial `ExtraLoadDistance`), the
# E-stall feed (`MMU2_FEED_DISTANCE`), and the two lengths the load to the nozzle runs from:
# `MMU2_EXTRUDER_HEATBREAK_LENGTH` less the feed, then `MMU2_EXTRUDER_NOZZLE_LENGTH` + 10
# (`MMU2_LOAD_TO_NOZZLE_SEQUENCE`).
UNLOAD_FINISH_MM = -80.0
EXTRA_LOAD_MM = 5.0
FEED_MM = 50.0
HEATBREAK_MM = 67.0
NOZZLE_MM = 20.0
# The longest move the firmware plans, which it plans to push the filament to the extruder's
# sensor (`maximumAllowedExtruderMove`, `mmu2_mk4.cpp:55`).
MAX_MOVE_MM = 189.9
# `nominalEMotorFSOff` (`mmu2_mk4.h:439-443`): the E-stall feed is 50 less this, plus the
# distance at which the last change's sensor turned off, clamped to it. That distance
# starts at this value and only hardware can tell it after a change, so the feed is
# always 50.
FS_OFF_MM = 14.0
# The unload's retract when the filament is fully auto-retracted, in place of the ramming:
# a literal in `MMU2::UnloadInner` (`mmu2_mk4.cpp:840-851`).
RELIEVE_MM = -120.0
# `FILAMENT_MMU2_RAMMING_SEQUENCE` (`Configuration_MK4_adv.h:573-579`,
# `Configuration_COREONE_adv.h` and `Configuration_COREONEL_adv.h:606-612`), in mm and
# mm/min: the ramming an MMU unload runs when the filament isn't fully auto-retracted.
MMU_RAMMING: Ramming = ((20, 1500), (-50, 2700), (-5, 50), (-50, 1500))


def change_feed() -> tuple[float, ...]:
    """The firmware's moves that push a slot's filament into the extruder at a tool change:
    the sensor feed, the feed past the sensor, and the E-stall feed
    (`OnMMUProgressMsgChanged` and `OnMMUProgressMsgSame`, `mmu2_mk4.cpp:1523-1530,
    1566-1585`; `FeedWithEStallDetection`, `:523-533`). They count on the slot selected
    when they're planned: the old one, not the slot they feed.

    One sensor feed is planned; a second, when the first ends before the sensor sees the
    filament, isn't modeled.
    """
    stall = FEED_MM - FS_OFF_MM + min(max(FS_OFF_MM, 0.0), FS_OFF_MM)
    return (MAX_MOVE_MM, EXTRA_LOAD_MM + 2, stall)


def nozzle_load() -> tuple[float, ...]:
    """The load to the nozzle after a tool change: `MMU2_LOAD_TO_NOZZLE_SEQUENCE`, 17 mm then
    30 (`mmu2_config.h:79-83`, `execute_load_to_nozzle_sequence` in `mmu2_mk4.cpp:1388-1393`)."""
    return (HEATBREAK_MM - FEED_MM, NOZZLE_MM + 10)


@dataclass
class MmuUnit:
    """An MMU3 attached to a printer.

    The selected slot isn't here: it's `SimPrinter.selected_tool`, the firmware's
    `MMU2::extruder`, None for `MMU2_NO_TOOL`. `path.loaded` is the extruder's filament
    sensor, and its invariant is that `fed` names a slot whenever it's set.
    """

    # The `mmu2_enabled` setting, which `M709` switches and `/api/v1/info` reports
    # (`M70X_mmu.cpp:54-92`, `basic_gets.cpp:229-247`).
    on: bool = True
    # Seconds left until the MMU is Active after a boot or `M709 S1` (`MMU2::Start`); 0 is
    # Active. How long it takes is inferred (`filament_sensors_handler.cpp:179`).
    connecting: float = 0.0
    # The slot whose filament is past the selector, what FINDA sees, or None.
    fed: int | None = None
    # The one filament path every slot's `SimTool.path` points to.
    path: FilamentPath = field(default_factory=FilamentPath)

    @classmethod
    def loaded_with(cls, slot: int) -> MmuUnit:
        """An MMU as a finished load of `slot` to the nozzle leaves it, Active. The printer
        selects the slot too (`SimPrinter.selected_tool`): a load is the only way a slot gets
        selected outside a print."""
        unit = cls(fed=slot)
        unit.path.fill(True)
        return unit

    @property
    def active(self) -> bool:
        """`MMU2::Enabled()`: switched on and done connecting (`mmu2_mk4.h:64`)."""
        return self.on and self.connecting <= EPSILON
