"""Whose tag a tool shows: the rules for trusting a tag's instance UUID.

A tag's instance UUID is the spool's identity (Q10), but the printer can report one that
isn't the active spool's: a runout reload keeps the old filament's type, tag UUIDs
included, and the bridge can start with the wrong spool assigned. So the bridge keeps a
`SeenTag` per tool and trusts a UUID only when all of these hold:

- The active spool is certain: no swap open or staged, and no provisional charge.
- The bridge saw the UUID appear: the tool showed another UUID, or none, before.
- The printer didn't report it with another spool before a runout.
- No other spool in Spoolman has it.

Everything here is pure, as `accounting.account()` is: each function takes the record
and returns the new one. The caller keeps it, saves it, and acts on the decision.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from enum import Enum

from .models import Material, Spool


@dataclass(frozen=True)
class SeenTag:
    """What the bridge knows about the instance UUID a tool reports.

    The database saves `uuid`, `spool_id` and `runout`, and only once there's a UUID.
    `appeared` and `gap` are what the bridge watched, so a restart loses them: a tag
    already loaded when the bridge starts isn't trusted, but a UUID that differs from
    the saved one counts as having appeared.
    """

    # The UUID the tool last reported while its active spool was certain, in lowercase.
    uuid: str | None = None
    # The spool that was active then. None once an unload that wasn't a runout made it
    # unknown: the spool recorded may have been wrong.
    spool_id: int | None = None
    # Whether the bridge saw `uuid` appear.
    appeared: bool = False
    # Whether the tool had a runout since. A runout reload keeps the old filament's
    # UUID, so with another spool active the UUID is a leftover.
    runout: bool = False
    # Whether the tool has read empty or without a UUID since `uuid` was recorded: a
    # UUID shown next appeared while the bridge watched.
    gap: bool = False


class Decision(Enum):
    """What to do with the instance UUID a tool reports."""

    NONE = "none"
    # A trusted tag on a spool with no UUID: store the tag's UUID on the spool.
    ADOPT = "adopt"
    # The spool has no UUID and the printer shows the one the bridge last wrote to the
    # tool: write it back instead of a new one, without storing it.
    KEEP = "keep"
    # A trusted tag whose UUID differs from the spool's in Spoolman.
    CONFLICT = "conflict"


def without_uuid(seen: SeenTag) -> SeenTag:
    """Record that the tool read empty or without an instance UUID."""
    return seen if seen.gap else replace(seen, gap=True)


def unloaded(seen: SeenTag, runout: bool) -> SeenTag:
    """Record an unload since the tool's UUID was last seen.

    `runout` is whether it was a runout in a print the bridge saw start. Only M600,
    during a print, reloads the type from before the unload, so only then can the UUID
    be a leftover. After any other unload a tag shown next was loaded from the tag, but
    the spool recorded may have been wrong. A runout in a print the bridge didn't see
    start looks the same. Either way the spool becomes unknown, and the same UUID shown
    next is neither a leftover nor trusted.
    """
    if seen.uuid is None:
        return seen
    if runout:
        if seen.spool_id is None or seen.runout:
            return seen
        return replace(seen, runout=True)
    return replace(seen, spool_id=None, appeared=False, runout=False)


def leftover(seen: SeenTag, uuid: str, active: Spool) -> bool:
    """Whether `uuid` is the one the tool showed with another spool before a runout.

    Then it's the spool before's, not the active spool's. A claim or a correction (S12)
    changes the active spool with no unload, and then the UUID is the newly active
    spool's: it was recorded with the wrong one. The active spool's own UUID in Spoolman
    is never a leftover.
    """
    uuid = uuid.lower()
    if (active.instance_uuid or "").lower() == uuid:
        return False
    return (
        seen.uuid == uuid
        and seen.spool_id is not None
        and seen.spool_id != active.id
        and seen.runout
    )


def loaded_tag(seen: SeenTag, material: Material, active: Spool) -> bool:
    """Whether the tool's type came from the tag of the spool now loaded (Q11).

    The bridge never writes such a tool: its `M865 X R` would clear the tag link and
    stop the printer's own usage tracking on the tag until the next load from it. A
    runout leftover isn't the loaded spool's tag, so it's written over as any other
    type is. With no instance UUID, a leftover can't be told apart, so the tag counts
    as the loaded spool's.
    """
    if not material.from_tag:
        return False
    uuid = material.instance_uuid
    return not (uuid and leftover(seen, uuid, active))


def decide(
    seen: SeenTag,
    material: Material,
    active: Spool,
    *,
    certain: bool,
    spools: Iterable[Spool],
    uuids: bool,
    refused: Collection[tuple[int, str]] = (),
    written_uuid: str | None = None,
) -> tuple[SeenTag, Decision]:
    """Decide what to do with the instance UUID the tool reports, and record it.

    Call it for the tag reader's tool only, on a poll that accounted the tool.

    - `certain`: whether `active` is certainly the spool in the tool.
    - `spools`: Spoolman's spools. The archived ones aren't listed, so the caller checks
      those before it acts on `ADOPT` or `KEEP`.
    - `uuids`: whether the bridge writes and compares instance UUIDs. The record is
      kept either way, so a runout leftover is found with UUIDs off, but the decision
      is then always `NONE`.
    - `refused`: the (spool, UUID) pairs already turned down because an archived spool
      has the UUID.
    - `written_uuid`: the UUID in the bridge's last write to the tool, in lowercase. The
      write clears a tag's markers, so a UUID the bridge wrote may be a tag's.
    """
    uuid = (material.instance_uuid or "").lower()
    if not uuid:
        return seen, Decision.NONE
    if leftover(seen, uuid, active):
        return seen, Decision.NONE  # The spool before's, until the printer shows another.
    if any(s.id != active.id and (s.instance_uuid or "").lower() == uuid for s in spools):
        return seen, Decision.NONE  # Another spool's: a correction (S12) or drift.
    if certain and (seen.uuid != uuid or seen.spool_id != active.id or seen.gap):
        # The same UUID with no known spool follows an unload that wasn't a runout: it
        # didn't appear, whatever the tool read since. The same UUID with another spool
        # follows a claim or a correction, and appeared only if the tool read without
        # it since.
        unknown = seen.uuid == uuid and seen.spool_id is None
        changed = seen.uuid is not None and seen.uuid != uuid
        seen = SeenTag(uuid, active.id, appeared=not unknown and (changed or seen.gap))
    elif certain and seen.runout:
        # A runout and a reload, and the same spool is certain again: still its UUID.
        seen = replace(seen, runout=False)
    if not uuids:
        return seen, Decision.NONE
    # While the active spool isn't certain, such as after a reload with no Spoolman
    # change, a tag isn't trusted: it may be another spool's.
    trusted = certain and material.from_tag and seen.appeared
    have = (active.instance_uuid or "").lower()
    if not have:
        if (active.id, uuid) in refused:
            return seen, Decision.NONE
        if trusted:
            return seen, Decision.ADOPT
        if uuid == written_uuid:
            return seen, Decision.KEEP
        return seen, Decision.NONE
    if have != uuid and trusted:
        return seen, Decision.CONFLICT
    return seen, Decision.NONE
