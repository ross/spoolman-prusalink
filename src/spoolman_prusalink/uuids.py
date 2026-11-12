"""Instance UUIDs in Spoolman: minting them, and adopting the UUIDs tags show."""

from __future__ import annotations

import logging
import uuid
from typing import NamedTuple

from .conditions import UUID_FIELD_NAME
from .errors import ServiceError, record_failure
from .fields import SpoolFields
from .models import Spool
from .spoolman import Spoolman

log = logging.getLogger("spoolman_prusalink")


class Minted(NamedTuple):
    """What minting a spool's UUID came to."""

    # The UUID now stored on the spool, or None if none could be.
    uuid: str | None
    # With no UUID: whether the write waits for a later mint. It doesn't when the UUID
    # field is unusable until someone fixes it; the spool is then written without one.
    wait: bool = False


class UuidRegistry:
    """Stores spools' instance UUIDs in the Spoolman field `key`.

    Remembers the tag UUIDs it turned down because an archived spool has them, and the
    UUIDs it checked can be kept on the printer, so the archived spools aren't read again
    every poll.
    """

    def __init__(self, spoolman: Spoolman, fields: SpoolFields, key: str) -> None:
        self._spoolman = spoolman
        self._fields = fields
        self._key = key
        self._refused: set[tuple[int, str]] = set()
        self._unowned: set[tuple[int, str]] = set()
        # What's failing now, by spool, with the last error logged for each.
        self._failing: dict[int, str] = {}

    @property
    def refused(self) -> frozenset[tuple[int, str]]:
        """The (spool, UUID) pairs turned down because an archived spool has the UUID."""
        return frozenset(self._refused)

    def checked(self, spool_id: int, value: str) -> bool:
        """Whether `value` was checked and no archived spool has it, so it can be kept on
        the printer for `spool_id`."""
        return (spool_id, value) in self._unowned

    async def mint(self, spool: Spool) -> Minted:
        """Give a spool a new instance UUID in Spoolman, so it can be written to the printer."""
        assert self._key
        value = str(uuid.uuid4())
        if not await self._fields.ensure(self._key, UUID_FIELD_NAME):
            return Minted(None, wait=not self._fields.unusable(self._key))
        try:
            await self._spoolman.set_extra(spool.id, self._key, value)
        except ServiceError as e:
            self._trouble(spool.id, e, "can't store an instance UUID on spool %d: %s")
            return Minted(None, wait=True)
        self._failing.pop(spool.id, None)
        log.info("gave spool %d instance UUID %s", spool.id, value)
        return Minted(value)

    async def adopt(self, spool_id: int, value: str, store: bool = True) -> bool:
        """Store a tag's instance UUID on a spool that has none. Returns whether it did.

        Spoolman's list leaves out archived spools, so they're checked here: a runout
        reload can show an archived spool's tag. A spool that has the UUID already keeps
        it, and this one doesn't take it. With `store` False, only that check is made,
        once, for a UUID kept on the printer without storing it.
        """
        assert self._key
        if not store and self.checked(spool_id, value):
            return True
        try:
            every = await self._spoolman.spools(archived=True)
        except ServiceError as e:
            self._trouble(spool_id, e, "can't check spool %d's tag UUID: %s")
            return False
        owner = next(
            (
                s.id
                for s in every
                if s.id != spool_id and (s.instance_uuid or "").lower() == value.lower()
            ),
            None,
        )
        if owner is not None:
            self._refused.add((spool_id, value))
            # Logged once: `tags.decide` doesn't propose a refused pair again.
            self._failing.pop(spool_id, None)
            log.info(
                "spool %d can't take instance UUID %s from the printer: archived spool %d has it",
                spool_id,
                value,
                owner,
            )
            return False
        if not store:
            self._unowned.add((spool_id, value))
            if self._failing.pop(spool_id, None) is not None:
                log.info("checked spool %d's tag UUID %s: no other spool has it", spool_id, value)
            return True
        if not await self._fields.ensure(self._key, UUID_FIELD_NAME):
            return False
        try:
            await self._spoolman.set_extra(spool_id, self._key, value)
        except ServiceError as e:
            self._trouble(spool_id, e, "can't store an instance UUID on spool %d: %s")
            return False
        self._failing.pop(spool_id, None)
        log.info("gave spool %d its tag's instance UUID %s", spool_id, value)
        return True

    def _trouble(self, spool_id: int, error: ServiceError, message: str) -> None:
        """Log a failed UUID store or check at WARNING, or at DEBUG if it repeats."""
        if record_failure(self._failing, spool_id, error):
            log.warning(message, spool_id, error)
        else:
            log.debug(message, spool_id, error)
