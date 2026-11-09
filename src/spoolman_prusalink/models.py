"""Data types shared by the printer client, the Spoolman client and the accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Material:
    """Filament the printer reports as loaded in a tool."""

    name: str
    color: str | None = None
    instance_uuid: str | None = None
    nozzle_temperature: int = field(kw_only=True)
    preheat_temperature: int = field(kw_only=True)
    bed_temperature: int = field(kw_only=True)
    # Set only by a load from an NFC tag. The bridge's own write (`M865 X R`) clears them,
    # and a runout reload keeps them from the filament before.
    material_uuid: str | None = None
    package_uuid: str | None = None

    @property
    def from_tag(self) -> bool:
        """Whether the type came from a tag rather than a menu pick or the bridge's write."""
        return bool(self.material_uuid or self.package_uuid)


@dataclass(frozen=True)
class ToolReading:
    """One tool's entry from `GET /api/v1/filament`."""

    index: int
    extruded: int
    material: Material | None = None
    # Pending firmware fields. None means the firmware doesn't report them.
    unload_count: int | None = None
    extruded_at_unload: int | None = None


@dataclass(frozen=True)
class FilamentReading:
    """A full `GET /api/v1/filament` response."""

    boot_id: int
    tools: dict[int, ToolReading] = field(default_factory=dict)


@dataclass(frozen=True)
class Spool:
    """The subset of a Spoolman spool the bridge uses."""

    id: int
    location: str | None
    name: str
    material: str | None
    color_hex: str | None
    diameter: float | None
    density: float | None
    instance_uuid: str | None = None
    flag: str | None = None
    extruder_temp: float | None = None
    bed_temp: float | None = None
    remaining_weight: float | None = None  # Grams, per Spoolman; None without a weight.
    net_weight: float | None = None  # Grams of filament when full: the spool's or filament's.
    used_weight: float | None = None  # Grams used, per Spoolman.
    archived: bool = False
    claim: bool = False  # The claim box is ticked.
    last_used: datetime | None = None

    def mm_to_grams(self, mm: float) -> float | None:
        """Convert a filament length to weight, as Spoolman does for `use_length`."""
        if not self.diameter or not self.density:
            return None
        radius_cm = self.diameter / 20
        length_cm = mm / 10
        return 3.141592653589793 * radius_cm * radius_cm * length_cm * self.density
