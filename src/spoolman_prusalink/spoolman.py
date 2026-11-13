"""Spoolman client: reads spools, charges usage, moves spools and sets extra fields."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from .errors import Rejected, check, parsing, send
from .models import Spool


class FieldTypeError(Rejected):
    """A spool extra field's key exists in Spoolman with another type."""


def _extra_str(extra: dict | None, key: str | None) -> str | None:
    """Decode a text extra field. Spoolman stores extra values JSON-encoded."""
    if not key or not extra or key not in extra:
        return None
    try:
        value = json.loads(extra[key])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, str) and value else None


def _extra_true(extra: dict | None, key: str | None) -> bool:
    """Whether a boolean extra field is ticked. Spoolman stores it as JSON `true`."""
    if not key or not extra or key not in extra:
        return False
    try:
        return json.loads(extra[key]) is True
    except (TypeError, ValueError):
        return False


def _extra_int(extra: dict | None, key: str | None) -> int:
    """Decode an integer extra field. Missing or invalid values read as 0."""
    if not key or not extra or key not in extra:
        return 0
    try:
        value = json.loads(extra[key])
    except (TypeError, ValueError):
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _timestamp(value: object) -> datetime | None:
    """Parse a Spoolman timestamp such as `2026-11-07T01:47:16Z`, read as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_spool(
    data: dict,
    uuid_field: str | None = None,
    flag_field: str | None = None,
    claim_field: str | None = None,
    jobs_field: str | None = None,
) -> Spool:
    filament = data.get("filament") or {}
    vendor = (filament.get("vendor") or {}).get("name")
    name = filament.get("name") or filament.get("material") or f"spool {data['id']}"
    if vendor:
        name = f"{vendor} {name}"
    # Spoolman clamps its own remaining_weight at 0, so work it out from used_weight,
    # which it doesn't. The net weight is the spool's initial weight, else the filament's.
    net = data.get("initial_weight")
    if net is None:
        net = filament.get("weight")
    used = data.get("used_weight")
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
        remaining_weight=None if net is None or used is None else net - used,
        net_weight=net,
        used_weight=used,
        archived=bool(data.get("archived", False)),
        claim=_extra_true(data.get("extra"), claim_field),
        last_used=_timestamp(data.get("last_used")),
        jobs_used_on=_extra_int(data.get("extra"), jobs_field),
    )


class Spoolman:
    def __init__(
        self,
        url: str,
        *,
        uuid_field: str | None = None,
        flag_field: str | None = None,
        claim_field: str | None = None,
        jobs_field: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._uuid_field = uuid_field
        self._flag_field = flag_field
        self._claim_field = claim_field
        self._jobs_field = jobs_field
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"), timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def spools(self, archived: bool = False) -> list[Spool]:
        """Return every non-archived spool, or with `archived`, every spool.

        The bridge reads the full list rather than filtering by location: Spoolman's
        location filter matches partial text, and a location rename sends no websocket
        events, so a full read is the only reliable reconcile.
        """
        params = {"allow_archived": "true"} if archived else None
        resp = await send(self._client, "GET", "/api/v1/spool", params=params)
        check(resp)
        with parsing("Spoolman's spool list"):
            return [self._parse(s) for s in resp.json()]

    async def spool(self, spool_id: int) -> Spool | None:
        """Return one spool, archived or not, or None if it doesn't exist (404)."""
        resp = await send(self._client, "GET", f"/api/v1/spool/{spool_id}")
        if resp.status_code == 404:
            return None
        check(resp)
        with parsing(f"spool {spool_id}"):
            return self._parse(resp.json())

    def _parse(self, data: dict) -> Spool:
        return parse_spool(
            data, self._uuid_field, self._flag_field, self._claim_field, self._jobs_field
        )

    async def use_length(self, spool_id: int, mm: float) -> float | None:
        """Charge (or, with a negative value, refund) filament length.

        Returns the spool's used weight after the charge, if Spoolman reports one.
        """
        # A generous timeout: a charge Spoolman applies after a timeout is sent again.
        resp = await send(
            self._client,
            "PUT",
            f"/api/v1/spool/{spool_id}/use",
            json={"use_length": mm},
            timeout=30.0,
        )
        check(resp)
        try:
            used = resp.json().get("used_weight")
        except (ValueError, AttributeError):
            return None
        return float(used) if isinstance(used, int | float) else None

    async def ensure_field(self, key: str, name: str, field_type: str = "text") -> None:
        """Register a spool extra field of `field_type`, unless it already exists."""
        resp = await send(self._client, "GET", "/api/v1/field/spool")
        check(resp)
        with parsing("Spoolman's spool fields"):
            types = {f.get("key"): f.get("field_type") for f in resp.json()}
        if key in types:
            if types[key] != field_type:
                raise FieldTypeError(
                    f"spool extra field {key!r} exists but isn't a {field_type} field"
                )
            return
        resp = await send(
            self._client,
            "POST",
            f"/api/v1/field/spool/{key}",
            json={"name": name, "field_type": field_type},
        )
        check(resp)

    async def set_location(self, spool_id: int, location: str | None) -> None:
        """Move a spool. None (or "") leaves it with no location."""
        resp = await send(
            self._client, "PATCH", f"/api/v1/spool/{spool_id}", json={"location": location or None}
        )
        check(resp)

    async def set_extra(self, spool_id: int, key: str, value: object) -> None:
        """Set an extra field to a JSON value, or delete it with None."""
        resp = await send(
            self._client,
            "PATCH",
            f"/api/v1/spool/{spool_id}",
            json={"extra": {key: None if value is None else json.dumps(value)}},
        )
        check(resp)
