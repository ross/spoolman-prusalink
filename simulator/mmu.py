"""The MMU3: the firmware's constants for it and the unit's state.

An MMU3 feeds five slots, one at a time, into a printer's one extruder. The firmware treats
each slot as a tool: it stores a type and counts per slot, and keeps one filament path, one
gap and one auto-retract distance for the extruder they share (`tool_index.hpp`,
`filament_tracker.hpp`). `script/firmware-check` compares `MMU_SLOTS` with the MMU builds'
`EXTRUDERS`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from simulator.tool import EPSILON, FilamentPath

# `VirtualToolIndex::count` on an MMU build: `EXTRUDERS` is 6, "5 + NoTool"
# (`include/marlin/Configuration_MK4.h:143-147`, `src/common/tool_index.hpp:29-43`).
MMU_SLOTS = 5


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
