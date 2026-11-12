"""Compare the printer's loaded filament with the spool Spoolman assigns."""

from __future__ import annotations

from dataclasses import astuple

from . import printer_sync
from .color import same_color
from .models import Material, Spool


def differences(tool: int, material: Material, spool: Spool, uuids: bool = True) -> list[str]:
    """Return human-readable differences, or an empty list if the tool matches.

    `uuids` is False on printers that can't hold an instance UUID, such as INDX.
    """
    diffs = []
    want_name = printer_sync.printer_name(spool)
    if want_name and material.name.casefold() != want_name.casefold():
        diffs.append(f"name printer={material.name!r} spoolman={want_name!r}")
    want_color = f"#{spool.color_hex.lower()}" if spool.color_hex else None
    if want_color and not same_color(material.color, want_color):
        diffs.append(f"color printer={material.color} spoolman={want_color}")
    try:
        want_temps = printer_sync.temperatures(spool)
    except printer_sync.CannotSync:
        want_temps = None  # Flagged as unwritable instead.
    if want_temps is not None:
        have_temps = (
            material.nozzle_temperature,
            material.preheat_temperature,
            material.bed_temperature,
        )
        labels = ("nozzle", "preheat", "bed")
        for label, have, want in zip(labels, have_temps, astuple(want_temps), strict=True):
            if have != want:
                diffs.append(f"{label} printer={have} spoolman={want}")
    # Only tool 0 can hold an ad-hoc instance UUID. A UUID on the printer that the spool
    # doesn't have counts too: a runout reload keeps the old spool's UUID, and a spool of
    # the same product otherwise matches on name and color.
    have = (material.instance_uuid or "").lower()
    want = (spool.instance_uuid or "").lower()
    if uuids and tool == printer_sync.UUID_TOOL and (want or have) and have != want:
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
