"""Compare the printer's loaded filament with the spool Spoolman assigns."""

from __future__ import annotations

from .color import same_color
from .models import Material, Spool

# The printer's filament name holds at most 7 characters.
NAME_LIMIT = 7


def printer_name(spool: Spool) -> str | None:
    """The name the bridge would write to the printer for this spool."""
    if not spool.material:
        return None
    return spool.material[:NAME_LIMIT]


def differences(tool: int, material: Material, spool: Spool) -> list[str]:
    """Return human-readable differences, or an empty list if the tool matches."""
    diffs = []
    want_name = printer_name(spool)
    if want_name and material.name.casefold() != want_name.casefold():
        diffs.append(f"name printer={material.name!r} spoolman={want_name!r}")
    want_color = f"#{spool.color_hex.lower()}" if spool.color_hex else None
    if want_color and not same_color(material.color, want_color):
        diffs.append(f"color printer={material.color} spoolman={want_color}")
    # Only tool 0 can hold an ad-hoc instance UUID.
    if tool == 0 and spool.instance_uuid and (
        (material.instance_uuid or "").lower() != spool.instance_uuid.lower()
    ):
        diffs.append(f"uuid printer={material.instance_uuid} spoolman={spool.instance_uuid}")
    return diffs


def same_instance(material: Material | None, spool: Spool) -> bool:
    """Whether the printer reports this spool's instance UUID. Only a UUID identifies a
    spool: two spools of the same product share a name and color."""
    return bool(
        material
        and material.instance_uuid
        and spool.instance_uuid
        and material.instance_uuid.lower() == spool.instance_uuid.lower()
    )
