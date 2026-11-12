"""Compare the printer's loaded filament with the spool Spoolman assigns."""

from __future__ import annotations

from dataclasses import astuple, dataclass
from enum import StrEnum

from . import printer_sync
from .color import same_color
from .models import Material, Spool


class What(StrEnum):
    """What a difference is about."""

    NAME = "name"
    COLOR = "color"
    NOZZLE = "nozzle"
    PREHEAT = "preheat"
    BED = "bed"
    UUID = "uuid"
    # The loaded tag's UUID is another spool's: `printer` is the UUID and `spoolman`
    # is that spool's ID.
    TAG = "tag"
    # The spool changed in Spoolman since the bridge last wrote it. It has no values.
    EDIT = "edit"


@dataclass(frozen=True)
class Difference:
    """One way a tool differs from its spool: what, and the value on each side."""

    what: What
    printer: str | int | None = None
    spoolman: str | int | None = None

    def __str__(self) -> str:
        if self.what is What.TAG:
            return f"the loaded tag is spool {self.spoolman}'s ({self.printer})"
        if self.what is What.EDIT:
            return "spool changed in Spoolman since the last write"
        if self.what is What.NAME:
            return f"name printer={self.printer!r} spoolman={self.spoolman!r}"
        return f"{self.what} printer={self.printer} spoolman={self.spoolman}"

    def to_json(self) -> list:
        return [self.what.value, self.printer, self.spoolman]

    @classmethod
    def from_json(cls, data: list) -> Difference:
        what, printer, spoolman = data
        return cls(What(what), printer, spoolman)


def describe(differences: tuple[Difference, ...] | list[Difference]) -> str:
    """Return the differences as one line of text."""
    return "; ".join(str(d) for d in differences)


def differences(
    tool: int, material: Material, spool: Spool, uuids: bool = True
) -> list[Difference]:
    """Return how the tool differs from the spool, or an empty list if it matches.

    `uuids` is False on printers that can't hold an instance UUID, such as INDX.
    """
    diffs = []
    want_name = printer_sync.printer_name(spool)
    if want_name and material.name.casefold() != want_name.casefold():
        diffs.append(Difference(What.NAME, material.name, want_name))
    want_color = f"#{spool.color_hex.lower()}" if spool.color_hex else None
    if want_color and not same_color(material.color, want_color):
        diffs.append(Difference(What.COLOR, material.color, want_color))
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
        labels = (What.NOZZLE, What.PREHEAT, What.BED)
        for label, have, want in zip(labels, have_temps, astuple(want_temps), strict=True):
            if have != want:
                diffs.append(Difference(label, have, want))
    # Only tool 0 can hold an ad-hoc instance UUID. A UUID on the printer that the spool
    # doesn't have counts too: a runout reload keeps the old spool's UUID, and a spool of
    # the same product otherwise matches on name and color.
    have = (material.instance_uuid or "").lower()
    want = (spool.instance_uuid or "").lower()
    if uuids and tool == printer_sync.UUID_TOOL and (want or have) and have != want:
        diffs.append(Difference(What.UUID, material.instance_uuid, spool.instance_uuid))
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
