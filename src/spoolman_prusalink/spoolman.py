"""Spoolman client. Phase 1 only reads."""

from __future__ import annotations

import json

import httpx

from .models import Spool


def _extra_str(extra: dict | None, key: str | None) -> str | None:
    """Decode a text extra field. Spoolman stores extra values JSON-encoded."""
    if not key or not extra or key not in extra:
        return None
    try:
        value = json.loads(extra[key])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, str) and value else None


def parse_spool(data: dict, uuid_field: str | None = None) -> Spool:
    filament = data.get("filament") or {}
    vendor = (filament.get("vendor") or {}).get("name")
    name = filament.get("name") or filament.get("material") or f"spool {data['id']}"
    if vendor:
        name = f"{vendor} {name}"
    return Spool(
        id=int(data["id"]),
        location=data.get("location") or None,
        name=name,
        material=filament.get("material"),
        color_hex=filament.get("color_hex"),
        diameter=filament.get("diameter"),
        density=filament.get("density"),
        instance_uuid=_extra_str(data.get("extra"), uuid_field),
    )


class Spoolman:
    def __init__(self, url: str, *, uuid_field: str | None = None, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._uuid_field = uuid_field
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"), timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def spools(self) -> list[Spool]:
        """Return every non-archived spool.

        The bridge reads the full list rather than filtering by location: Spoolman's
        location filter matches partial text, and a location rename sends no websocket
        events, so a full read is the only reliable reconcile.
        """
        resp = await self._client.get("/api/v1/spool")
        resp.raise_for_status()
        return [parse_spool(s, self._uuid_field) for s in resp.json()]

    async def use_length(self, spool_id: int, mm: float) -> None:
        """Charge (or, with a negative value, refund) filament length. Not used in phase 1."""
        resp = await self._client.put(
            f"/api/v1/spool/{spool_id}/use", json={"use_length": mm}
        )
        resp.raise_for_status()
