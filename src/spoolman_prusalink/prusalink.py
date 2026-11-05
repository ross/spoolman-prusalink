"""Read-only PrusaLink client."""

from __future__ import annotations

from typing import NamedTuple

import httpx

from .models import FilamentReading, Material, ToolReading

# Printer states in which a job file can start (used by phase 3 writes).
IDLE_STATES = frozenset({"IDLE", "FINISHED", "READY", "STOPPED"})
BUSY_STATES = frozenset({"PRINTING", "PAUSED", "BUSY", "ATTENTION"})


class PrinterStatus(NamedTuple):
    state: str
    job_id: int | None


def parse_filament(data: dict) -> FilamentReading:
    tools: dict[int, ToolReading] = {}
    for raw in data.get("tools", []):
        mat = raw.get("material")
        material = (
            None
            if not mat
            else Material(
                name=mat.get("name", ""),
                color=mat.get("color"),
                instance_uuid=mat.get("instance_uuid"),
            )
        )
        index = int(raw["index"])
        tools[index] = ToolReading(
            index=index,
            extruded=int(raw["extruded"]),
            material=material,
            unload_count=raw.get("unload_count"),
            extruded_at_unload=raw.get("extruded_at_unload"),
        )
    return FilamentReading(boot_id=int(data["boot_id"]), tools=tools)


class NoFilamentEndpoint(Exception):
    """The printer's firmware has no `/api/v1/filament`."""


class PrusaLink:
    def __init__(self, url: str, api_key: str, *, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"X-Api-Key": api_key},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def filament(self) -> FilamentReading:
        resp = await self._client.get("/api/v1/filament")
        if resp.status_code == 404:
            raise NoFilamentEndpoint("printer firmware has no /api/v1/filament")
        resp.raise_for_status()
        return parse_filament(resp.json())

    async def printer_type(self) -> str | None:
        """Return the printer type code from `GET /api/version`, such as `8.1.0`."""
        resp = await self._client.get("/api/version")
        resp.raise_for_status()
        value = resp.json().get("printer")
        return None if value is None else str(value)

    async def job_display_name(self) -> str | None:
        """Return the current job's long file name, or None when no job is running."""
        resp = await self._client.get("/api/v1/job")
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        file = resp.json().get("file") or {}
        return file.get("display_name") or file.get("name")

    async def upload_and_print(self, storage: str, name: str, gcode: str) -> None:
        """Upload a G-code file, overwriting any old copy, and start it as a print.

        The printer refuses (409) unless it's idle or finished.
        """
        resp = await self._client.put(
            f"/api/v1/files/{storage}/{name}",
            content=gcode.encode(),
            headers={"Print-After-Upload": "?1", "Overwrite": "?1",
                     "Content-Type": "text/x.gcode"},
        )
        resp.raise_for_status()

    async def status(self) -> PrinterStatus:
        """Return the printer state and the current job's id from `GET /api/v1/status`."""
        resp = await self._client.get("/api/v1/status")
        resp.raise_for_status()
        data = resp.json()
        state = str(data.get("printer", {}).get("state", "UNKNOWN")).upper()
        job = data.get("job") or {}
        job_id = job.get("id")
        return PrinterStatus(state, None if job_id is None else int(job_id))
