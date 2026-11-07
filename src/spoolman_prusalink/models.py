"""Data types shared by the printer client, the Spoolman client and the accounting."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Material:
    """Filament the printer reports as loaded in a tool."""

    name: str
    color: str | None = None
    instance_uuid: str | None = None


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

    def mm_to_grams(self, mm: float) -> float | None:
        """Convert a filament length to weight, as Spoolman does for `use_length`."""
        if not self.diameter or not self.density:
            return None
        radius_cm = self.diameter / 20
        length_cm = mm / 10
        return 3.141592653589793 * radius_cm * radius_cm * length_cm * self.density
