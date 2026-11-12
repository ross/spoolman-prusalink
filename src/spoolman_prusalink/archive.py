"""Archived spools the bridge still needs: Spoolman's spool list leaves them out."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

from .models import Spool


class ArchivedSpools:
    """Archived spools that are still loaded or flagged, looked up once each.

    An archived spool stays loaded until the next unload and keeps being charged, and a
    flag the bridge set on it is kept current. Neither is in Spoolman's list, so each is
    looked up once and kept here while it's still needed.
    """

    def __init__(self) -> None:
        self._spools: dict[int, Spool] = {}

    def __contains__(self, spool_id: object) -> bool:
        return spool_id in self._spools

    def __len__(self) -> int:
        return len(self._spools)

    def __iter__(self) -> Iterator[Spool]:
        return iter(list(self._spools.values()))

    def get(self, spool_id: int) -> Spool | None:
        return self._spools.get(spool_id)

    def add(self, spool: Spool) -> None:
        """Keep an archived spool that was looked up."""
        self._spools[spool.id] = spool

    def drop(self, spool_id: int) -> None:
        """Forget a spool, such as one that turned out to be deleted."""
        self._spools.pop(spool_id, None)

    def set_flag(self, spool_id: int, flag: str | None) -> None:
        """Note the flag the bridge set on a kept spool, so it isn't set again."""
        if spool_id in self._spools:
            self._spools[spool_id] = replace(self._spools[spool_id], flag=flag)

    def prune(self, listed: set[int], needed: set[int]) -> None:
        """Forget the spools that are listed again or no longer `needed`.

        A listed spool was unarchived. `needed` is every spool that's active in a tool
        or flagged by the bridge.
        """
        self._spools = {i: s for i, s in self._spools.items() if i not in listed and i in needed}
