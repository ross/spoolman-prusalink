"""The bridge's extra spool fields in Spoolman: its UUID, flag, and claim fields."""

from __future__ import annotations

import logging

from .errors import Rejected, ServiceError, record_failure
from .spoolman import Spoolman

log = logging.getLogger("spoolman_prusalink")


class SpoolFields:
    """Sets up each extra spool field the bridge uses, once, before its first use."""

    def __init__(self, spoolman: Spoolman) -> None:
        self._spoolman = spoolman
        self._ready: set[str] = set()
        # Keys whose last setup failure was `Rejected`: it needs someone to fix it.
        self._unusable: set[str] = set()
        # What's failing now, by key, with the last error logged for each.
        self._failing: dict[str, str] = {}

    async def ensure(self, key: str, name: str, field_type: str = "text") -> bool:
        """Set up the field `key` if it isn't yet. Returns whether it's ready."""
        if key in self._ready:
            return True
        try:
            await self._spoolman.ensure_field(key, name, field_type)
        except ServiceError as e:
            # A refusal, such as a key of another type, a proxy refusing the fields API or
            # a Spoolman without it, lasts until someone fixes it. Anything else may pass.
            if isinstance(e, Rejected):
                self._unusable.add(key)
            else:
                self._unusable.discard(key)  # Passing, whatever failed before.
            if record_failure(self._failing, key, e):
                log.warning("can't set up the %r spool field: %s", key, e)
            else:
                log.debug("can't set up the %r spool field: %s", key, e)
            return False
        if self._failing.pop(key, None) is not None:
            log.info("%r spool field is set up", key)
        self._unusable.discard(key)
        self._ready.add(key)
        return True

    def unusable(self, key: str) -> bool:
        """Whether the field's setup was refused, so it stays unusable until someone
        fixes it in Spoolman. False once `ensure` passes or fails some other way."""
        return key in self._unusable
