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


def parse_spool(
    data: dict, uuid_field: str | None = None, flag_field: str | None = None
) -> Spool:
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
        flag=_extra_str(data.get("extra"), flag_field),
        extruder_temp=filament.get("settings_extruder_temp"),
        bed_temp=filament.get("settings_bed_temp"),
        remaining_weight=data.get("remaining_weight"),
        net_weight=data.get("initial_weight") or filament.get("weight"),
    )


class Spoolman:
    def __init__(self, url: str, *, uuid_field: str | None = None,
                 flag_field: str | None = None, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._uuid_field = uuid_field
        self._flag_field = flag_field
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
        return [parse_spool(s, self._uuid_field, self._flag_field) for s in resp.json()]

    async def use_length(self, spool_id: int, mm: float) -> None:
        """Charge (or, with a negative value, refund) filament length."""
        # A generous timeout: a charge Spoolman applies after a timeout is sent again.
        resp = await self._client.put(
            f"/api/v1/spool/{spool_id}/use", json={"use_length": mm}, timeout=30.0
        )
        resp.raise_for_status()

    async def ensure_text_field(self, key: str, name: str) -> None:
        """Register a spool extra field of type text, unless it already exists."""
        resp = await self._client.get("/api/v1/field/spool")
        resp.raise_for_status()
        for f in resp.json():
            if f.get("key") == key:
                if f.get("field_type") != "text":
                    raise ValueError(f"spool extra field {key!r} exists but isn't a text field")
                return
        resp = await self._client.post(
            f"/api/v1/field/spool/{key}", json={"name": name, "field_type": "text"}
        )
        resp.raise_for_status()

    async def set_location(self, spool_id: int, location: str | None) -> None:
        """Move a spool. None (or "") leaves it with no location."""
        resp = await self._client.patch(
            f"/api/v1/spool/{spool_id}", json={"location": location or None}
        )
        resp.raise_for_status()

    async def set_extra_text(self, spool_id: int, key: str, value: str | None) -> None:
        """Set a text extra field, or delete it with None. Other extra fields are kept."""
        resp = await self._client.patch(
            f"/api/v1/spool/{spool_id}",
            json={"extra": {key: None if value is None else json.dumps(value)}},
        )
        resp.raise_for_status()
