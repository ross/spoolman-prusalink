"""Flags on spools in Spoolman: keeping the flag field current, and spools in two tools."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from .archive import ArchivedSpools
from .errors import NotFound, ServiceError, record_failure
from .fields import SpoolFields
from .models import Spool
from .spoolman import Spoolman
from .state import State

log = logging.getLogger("spoolman_prusalink")

FLAG_FIELD_NAME = "Printer bridge flag"

# Flag texts by spool. A spool's texts are joined into its one flag.
Flags = dict[int, list[str]]


class SharedSpools:
    """Flags each spool that's active in more than one tool at once, and logs it once."""

    def __init__(self) -> None:
        # Spools active in two tools last poll, with the tool locations each was logged in.
        self._shared: dict[int, list[str]] = {}

    def check(self, where: Mapping[int, list[str]]) -> Flags:
        """Return a flag for each spool that's active in more than one tool at once.

        It happens when a tool misses an unload, which a reboot can cause, and the spool is
        then loaded elsewhere. Each tool keeps charging it until its own unload or a claim.

        `where` is the locations of the tools each spool is active in.
        """
        flags: Flags = {}
        shared = {spool_id for spool_id, locations in where.items() if len(locations) > 1}
        for spool_id in sorted(self._shared.keys() - shared):
            log.warning("spool %d is no longer active in more than one tool", spool_id)
        previous, self._shared = self._shared, {i: where[i] for i in shared}
        for spool_id in sorted(shared):
            locations = where[spool_id]
            names = " and ".join(locations)
            flags.setdefault(spool_id, []).append(
                f"active in {names} at once. One of them missed an unload: unload and reload "
                "there, or claim the spool that's loaded."
            )
            if previous.get(spool_id) != locations:
                log.warning(
                    "spool %d is active in %s at once; one of them missed an unload",
                    spool_id,
                    names,
                )
        return flags


class FlagSync:
    """Keeps the flag field `key` matching the flags the bridge wants on each spool.

    The bridge's own flags are listed in `State.flagged`, which this owns.
    """

    def __init__(
        self,
        spoolman: Spoolman,
        fields: SpoolFields,
        archived: ArchivedSpools,
        state: State,
        key: str | None,
    ) -> None:
        self._spoolman = spoolman
        self._fields = fields
        self._archived = archived
        self._state = state
        self._key = key
        # What's failing now, by spool, with the last error logged for each.
        self._failing: dict[int, str] = {}

    def _deleted(self, spool_id: int) -> bool:
        """Forget a deleted spool's flag, and end a failing update on it.

        Returns whether `State.flagged` changed.
        """
        if self._failing.pop(spool_id, None) is not None:
            log.info("spool %d was deleted; its flag no longer needs updating", spool_id)
        if spool_id not in self._state.flagged:
            return False
        self._state.flagged.remove(spool_id)
        return True

    async def sync(self, spools: list[Spool], flags: Mapping[int, str]) -> bool:
        """Make the flag field match `flags`, each spool's text, on spools the bridge manages.

        The field is the bridge's own: on a spool it flags, whatever the field held is
        replaced, and clearing the flag empties it. On spools it has no flag for and
        didn't flag itself (`State.flagged`), the field is left as it is.

        Returns whether `State.flagged` changed, which the caller then saves.
        """
        key = self._key
        assert key
        if not await self._fields.ensure(key, FLAG_FIELD_NAME):
            return False
        current = {s.id: s.flag for s in [*spools, *self._archived]}
        changed = False
        for spool_id in sorted(set(flags) | set(self._state.flagged)):
            want = flags.get(spool_id)
            if spool_id not in current and spool_id in self._state.flagged:
                # A spool the bridge flagged left the list. An archived one is looked up
                # once and kept, like an archived active spool, so its flag is kept current
                # and a stale one is cleared.
                try:
                    found = await self._spoolman.spool(spool_id)
                except ServiceError as e:
                    if record_failure(self._failing, spool_id, e):
                        log.warning("can't look up flagged spool %d: %s", spool_id, e)
                    else:
                        log.debug("can't look up flagged spool %d: %s", spool_id, e)
                    continue
                if found is None:
                    changed |= self._deleted(spool_id)
                    continue
                if found.archived:
                    self._archived.add(found)
                # Otherwise it was unarchived since the list was read; next poll lists it.
                current[spool_id] = found.flag
            if spool_id not in current:
                continue  # Not listed, and the bridge never flagged it: leave it alone.
            if current[spool_id] != want:
                try:
                    await self._spoolman.set_extra(spool_id, key, want)
                except NotFound:
                    # Deleted since it was listed or cached. An active spool that was
                    # cached as archived is looked up again, as deleted.
                    self._archived.drop(spool_id)
                    changed |= self._deleted(spool_id)
                    continue
                except ServiceError as e:
                    if record_failure(self._failing, spool_id, e):
                        log.warning("can't update the flag on spool %d: %s", spool_id, e)
                    else:
                        log.debug("can't update the flag on spool %d: %s", spool_id, e)
                    continue
                self._archived.set_flag(spool_id, want)
                self._failing.pop(spool_id, None)
                log.info(
                    "%s flag on spool %d%s",
                    "set" if want else "cleared",
                    spool_id,
                    f": {want}" if want else "",
                )
            else:
                # Someone else made it match, so a failing update is no longer needed.
                if self._failing.pop(spool_id, None) is not None:
                    log.info("flag on spool %d is up to date", spool_id)
            if want is None:
                # Only a spool the bridge flagged earlier has no desired flag.
                self._state.flagged.remove(spool_id)
                changed = True
            elif spool_id not in self._state.flagged:
                self._state.flagged.append(spool_id)
                changed = True
        return changed
