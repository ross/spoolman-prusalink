"""The poll loop. Phase 1: compute and log charges, never write."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from . import drift
from .accounting import WARNINGS, EventKind, ToolState, account
from .config import Config
from .models import FilamentReading, Spool
from .prusalink import BUSY_STATES, PrinterStatus, PrusaLink
from .spoolman import Spoolman
from .state import append_ledger, load_state, save_state

log = logging.getLogger("spoolman_prusalink")

# Events that repeat every poll while a condition lasts. Log them only when they change.
_REPEATING = {EventKind.HOLD_EMPTY, EventKind.HOLD_UNASSIGNED, EventKind.AMBIGUOUS}


@dataclass
class Bridge:
    config: Config
    printer: PrusaLink
    spoolman: Spoolman
    tools: dict[int, ToolState] = field(default_factory=dict)
    _last_notes: dict[int, tuple[str, ...]] = field(default_factory=dict)
    _last_state: str | None = None

    @classmethod
    def from_config(cls, config: Config) -> Bridge:
        return cls(
            config=config,
            printer=PrusaLink(config.printer_url, config.printer_api_key),
            spoolman=Spoolman(config.spoolman_url, uuid_field=config.uuid_field),
            tools=load_state(config.state_file),
        )

    async def aclose(self) -> None:
        await self.printer.aclose()
        await self.spoolman.aclose()

    async def poll(self) -> None:
        try:
            status = await self.printer.status()
            reading = await self.printer.filament()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("printer read failed, skipping poll: %s", e)
            return
        try:
            spools = await self.spoolman.spools()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("Spoolman read failed, skipping poll: %s", e)
            return
        self._last_state = status.state
        self.process(reading, spools, status)

    def process(
        self, reading: FilamentReading, spools: list[Spool], status: PrinterStatus
    ) -> list[dict]:
        """Account one poll. Returns the ledger records it wrote."""
        by_id = {s.id: s for s in spools}
        now = datetime.now(UTC).isoformat(timespec="seconds")
        records: list[dict] = []

        for tool, location in self.config.tools.items():
            tool_reading = reading.tools.get(tool)
            if tool_reading is None:
                self._notes(tool, [f"[{location}] printer doesn't report tool index {tool}"])
                continue
            location_ids = [s.id for s in spools if s.location == location]
            material = tool_reading.material

            def matches(spool_id: int, material=material) -> bool:
                spool = by_id.get(spool_id)
                return spool is not None and drift.same_instance(material, spool)

            result = account(
                self.tools.get(tool, ToolState()),
                reading.boot_id,
                tool_reading,
                location_ids,
                job_id=status.job_id,
                matches=matches,
            )
            if not result.skipped:
                self.tools[tool] = result.state

            notes = []
            for ev in result.events:
                if ev.kind in _REPEATING:
                    notes.append(f"[{location}] {ev.message}")
                    continue
                level = logging.WARNING if ev.kind in WARNINGS else logging.INFO
                log.log(level, "[%s] %s", location, ev.message)
                records.append({"time": now, "tool": tool, "location": location,
                                "event": ev.kind.value, "message": ev.message})

            for c in result.charges:
                spool = by_id.get(c.spool_id)
                length = round(c.mm * self.config.length_factor, 1)
                grams = spool.mm_to_grams(length) if spool else None
                log.info(
                    "[%s] WOULD CHARGE spool %d (%s): %+g mm%s (%s)",
                    location, c.spool_id, spool.name if spool else "unknown", length,
                    f" ≈ {grams:+.2f} g" if grams is not None else "", c.reason,
                )
                records.append({
                    "time": now, "tool": tool, "location": location, "spool_id": c.spool_id,
                    "extruded_mm": c.mm, "use_length": length,
                    "grams": None if grams is None else round(grams, 3),
                    "reason": c.reason, "boot_id": reading.boot_id,
                    "extruded": tool_reading.extruded, "dry_run": True,
                })

            st = result.state
            if st.evict is not None:
                notes.append(f"[{location}] spool {st.evict} left the tool but is still in "
                             f"{location!r}; phase 3 moves it out")
            active = by_id.get(st.active) if st.active is not None else None
            if not result.skipped and material is not None and active is not None:
                diffs = drift.differences(tool, material, active)
                if diffs:
                    notes.append(f"[{location}] printer differs from spool {active.id}: "
                                 + "; ".join(diffs))
            self._notes(tool, notes)

        append_ledger(self.config.ledger_file, records)
        save_state(self.config.state_file, self.tools)
        return records

    def _notes(self, tool: int, notes: list[str]) -> None:
        key = tuple(notes)
        if self._last_notes.get(tool) != key:
            for n in notes:
                log.warning("%s", n)
        self._last_notes[tool] = key

    def interval(self) -> float:
        if self._last_state is None or self._last_state in BUSY_STATES:
            return self.config.poll_printing
        return self.config.poll_idle

    async def run(self, stop: asyncio.Event) -> None:
        log.info(
            "starting in dry-run mode: printer=%s spoolman=%s tools=%s",
            self.config.printer_url, self.config.spoolman_url, self.config.tools,
        )
        while not stop.is_set():
            await self.poll()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval())
