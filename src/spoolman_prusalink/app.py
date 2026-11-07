"""The poll loop: account usage, charge it, keep flags current, and sync the printer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import httpx

from . import drift, printer_sync
from .accounting import UNKNOWN_WRITE, WARNINGS, EventKind, ToolState, account
from .config import Config
from .models import FilamentReading, Spool
from .prusalink import BUSY_STATES, IDLE_STATES, NoFilamentEndpoint, PrinterStatus, PrusaLink
from .spoolman import Spoolman
from .state import State, append_ledger, load_state, save_state

log = logging.getLogger("spoolman_prusalink")

# Events that repeat every poll while a condition lasts. Log them only when they change.
_REPEATING = {EventKind.HOLD_EMPTY, EventKind.HOLD_UNASSIGNED, EventKind.AMBIGUOUS}

FLAG_FIELD_NAME = "Printer bridge flag"
UUID_FIELD_NAME = "Instance UUID"
# While a service stays unreachable, remind once per this many seconds.
OUTAGE_REMINDER = 3600


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


@dataclass
class Outage:
    """Log a service going unreachable and coming back, not every failed poll.

    A printer that's switched off is normal, so a connection failure logs once, then
    an hourly reminder, then a line when the service answers again.
    """

    name: str
    clock: Callable[[], float] = time.monotonic
    since: float | None = None
    _reminded: float = 0.0

    @property
    def down(self) -> bool:
        return self.since is not None

    def failed(self, error: Exception) -> None:
        now = self.clock()
        if self.since is None:
            self.since = self._reminded = now
            log.warning("%s unreachable: %s; logging again when it's back", self.name, error)
        elif now - self._reminded >= OUTAGE_REMINDER:
            self._reminded = now
            log.info("%s still unreachable after %s", self.name, _duration(now - self.since))
        else:
            log.debug("%s still unreachable: %s", self.name, error)

    def ok(self) -> None:
        if self.since is not None:
            log.info("%s reachable again after %s", self.name, _duration(self.clock() - self.since))
            self.since = None


@dataclass
class Once:
    """Log each problem once, while it lasts, keyed by what it's about.

    `fail()` logs the first time a key fails, and again only if the detail changes;
    repeats go to DEBUG. `clear()` ends the problem and optionally logs one recovery line.
    """

    _seen: dict[Hashable, str] = field(default_factory=dict)

    def fail(self, key: Hashable, detail: object, message: str, *args: object,
             level: int = logging.WARNING) -> bool:
        detail = str(detail)
        new = self._seen.get(key) != detail
        self._seen[key] = detail
        log.log(level if new else logging.DEBUG, message, *args)
        return new

    def clear(self, key: Hashable, message: str | None = None, *args: object) -> bool:
        if self._seen.pop(key, None) is None:
            return False
        if message:
            log.info(message, *args)
        return True


# A mid-print unload with more than this fraction of the net weight left in Spoolman
# gets a flag.
RUNOUT_LEFT = 0.05

# Sync jobs per swap before the bridge gives up and flags the spool.
MAX_SYNC_ATTEMPTS = 2


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Bridge:
    config: Config
    printer: PrusaLink
    spoolman: Spoolman
    state: State = field(default_factory=State)
    _last_notes: dict[int, tuple[str, ...]] = field(default_factory=dict)
    _last_state: str | None = None
    _ready_fields: set[str] = field(default_factory=set)
    _flags: dict[int, str] = field(default_factory=dict)  # Desired flags from the last poll.
    _pending_writes: list[int] = field(default_factory=list)  # Tools to write when idle.
    _job_names: dict[int, str | None] = field(default_factory=dict)
    _printer_outage: Outage = field(default_factory=lambda: Outage("printer"))
    _spoolman_outage: Outage = field(default_factory=lambda: Outage("Spoolman"))
    _problems: Once = field(default_factory=Once)
    _unsupported: bool = False  # The printer lacks the filament endpoint.
    # Why printer writes are off for this printer, or None. Checked once per boot.
    _model_error: str | None = None
    _model_boot: int | None = None
    _model: printer_sync.PrinterModel | None = None

    @classmethod
    def from_config(cls, config: Config) -> Bridge:
        return cls(
            config=config,
            printer=PrusaLink(config.printer_url, config.printer_api_key),
            spoolman=Spoolman(
                config.spoolman_url,
                uuid_field=config.uuid_field,
                flag_field=None if config.dry_run else config.flag_field,
            ),
            state=load_state(config.state_file),
        )

    @property
    def tools(self) -> dict[int, ToolState]:
        return self.state.tools

    async def aclose(self) -> None:
        await self.printer.aclose()
        await self.spoolman.aclose()

    async def poll(self) -> None:
        # Send charges left over from a failed poll or a restart before reading anything.
        if not await self.flush():
            return
        try:
            status = await self.printer.status()
            reading = await self.printer.filament()
            job_id = await self._user_job_id(status)
        except httpx.TransportError as e:
            # Connection refused, timeouts and the like: most often the printer is off.
            self._printer_outage.failed(e)
            return
        except NoFilamentEndpoint as e:
            self._printer_outage.ok()
            if not self._unsupported:
                self._unsupported = True
                log.error("%s; this firmware isn't supported, so nothing is charged. "
                          "Checking again each poll.", e)
            return
        except (httpx.HTTPError, ValueError, KeyError) as e:
            self._printer_outage.ok()  # It answered, even if badly.
            self._problems.fail("printer read", e, "printer read failed, skipping poll: %s", e)
            return
        self._printer_outage.ok()
        self._problems.clear("printer read", "printer reads working again")
        if self._unsupported:
            self._unsupported = False
            log.info("printer now reports filament; resuming")
        if self._model_boot != reading.boot_id:
            await self._check_model(reading.boot_id)
        try:
            spools = await self.spoolman.spools()
        except httpx.TransportError as e:
            self._spoolman_outage.failed(e)
            return
        except (httpx.HTTPError, ValueError, KeyError) as e:
            self._spoolman_outage.ok()  # It answered, even if badly.
            self._problems.fail("Spoolman read", e, "Spoolman read failed, skipping poll: %s", e)
            return
        self._spoolman_outage.ok()
        self._problems.clear("Spoolman read", "Spoolman reads working again")
        self._last_state = status.state
        self.process(reading, spools, status._replace(job_id=job_id))
        await self.flush()
        await self.evict(spools)
        if status.state in IDLE_STATES:
            await self.sync_printer(spools)
        if not self.config.dry_run and self.config.flag_field:
            await self.sync_flags(spools)

    async def _check_model(self, boot_id: int) -> None:
        """Look up the printer's model; if the bridge can't write to it, writes stay off."""
        try:
            printer_type = await self.printer.printer_type()
        except (httpx.HTTPError, ValueError) as e:
            error = f"can't read the printer model ({e})"
            if error != self._model_error:
                log.warning("%s; printer writes are off until it can", error)
            self._model_error = error
            return
        self._model_boot = boot_id
        self._model = printer_sync.PRINTER_MODELS.get(printer_type or "")
        self._model_error = printer_sync.model_problem(printer_type, self.config.gcode_preamble)
        name = self._model.name if self._model else "unknown"
        if self._model_error:
            log.warning("printer is %s (%s): %s; printer writes are off", name, printer_type,
                        self._model_error)
        else:
            log.info("printer is %s (%s)", name, printer_type)

    @property
    def _uuids(self) -> bool:
        """Whether to write and compare instance UUIDs: configured with a Spoolman field to
        keep them in, and the model keeps them."""
        return bool(self.config.printer_uuids and self.config.uuid_field
                    and self._model is not None and self._model.uuids)

    def _preamble(self) -> tuple[str, ...]:
        if self.config.gcode_preamble is not None:
            return self.config.gcode_preamble
        assert self._model is not None
        return printer_sync.default_preamble(self._model)

    async def _user_job_id(self, status: PrinterStatus) -> int | None:
        """Return the job id, or None for the bridge's own sync job.

        A sync job runs as a print, but it mustn't count as a print start, which would
        close an unconfirmed swap.
        """
        if status.job_id is None:
            return None
        if status.job_id not in self._job_names:
            self._job_names = {status.job_id: await self.printer.job_display_name()}
        name = self._job_names[status.job_id]
        return None if name == self.config.sync_file else status.job_id

    def process(
        self, reading: FilamentReading, spools: list[Spool], status: PrinterStatus
    ) -> list[dict]:
        """Account one poll and persist the result.

        In dry-run mode, charges go straight to the ledger. Otherwise they go to the
        outbox, saved in the same write as the new counters, for `flush()` to send.
        Returns the charge and event records.
        """
        by_id = {s.id: s for s in spools}
        now = _now()
        events: list[dict] = []
        charges: list[dict] = []
        flags: dict[int, list[str]] = {}
        dry = self.config.dry_run
        self._pending_writes = []

        for tool, location in self.config.tools.items():
            tool_reading = reading.tools.get(tool)
            if tool_reading is None:
                self._notes(tool, [f"[{location}] printer doesn't report tool index {tool}"])
                continue
            location_ids = [s.id for s in spools if s.location == location]
            material = tool_reading.material

            def matches(spool_id: int, material=material) -> bool:
                spool = by_id.get(spool_id)
                return (self._uuids and spool is not None
                        and drift.same_instance(material, spool))

            before = self.tools.get(tool, ToolState())
            result = account(
                before,
                reading.boot_id,
                tool_reading,
                location_ids,
                job_id=status.job_id,
                matches=matches,
            )
            if not result.skipped:
                self.tools[tool] = result.state
            if result.runout is not None:
                self._check_runout(location, by_id.get(result.runout))

            notes = []
            for ev in result.events:
                if ev.kind in _REPEATING:
                    notes.append(f"[{location}] {ev.message}")
                    continue
                level = logging.WARNING if ev.kind in WARNINGS else logging.INFO
                log.log(level, "[%s] %s", location, ev.message)
                events.append({"time": now, "tool": tool, "location": location,
                               "event": ev.kind.value, "message": ev.message})

            for c in result.charges:
                spool = by_id.get(c.spool_id)
                length = round(c.mm * self.config.length_factor, 1)
                grams = spool.mm_to_grams(length) if spool else None
                log.info(
                    "[%s] %s spool %d (%s): %+g mm%s (%s)",
                    location, "WOULD CHARGE" if dry else "CHARGE", c.spool_id,
                    spool.name if spool else "unknown", length,
                    f" ≈ {grams:+.2f} g" if grams is not None else "", c.reason,
                )
                charges.append({
                    "time": now, "tool": tool, "location": location, "spool_id": c.spool_id,
                    "extruded_mm": c.mm, "use_length": length,
                    "grams": None if grams is None else round(grams, 3),
                    "reason": c.reason, "boot_id": reading.boot_id,
                    "extruded": tool_reading.extruded, "dry_run": dry,
                })

            st = self.tools.get(tool, ToolState())
            if st.evict is not None and dry:
                notes.append(f"[{location}] would move spool {st.evict} out of {location!r}")
            if st.swap is not None:
                flags.setdefault(st.swap.spool_id, []).append(
                    f"{location}: filament was unloaded with no Spoolman change. If a "
                    f"different spool went in, move it into {location} before the next print."
                )
            if st.notice is not None:
                flags.setdefault(st.notice.spool_id, []).append(
                    f"{location}: {st.notice.message}."
                )
            active = by_id.get(st.active) if st.active is not None else None
            if active is not None:
                if active.id != before.active:
                    self.state.runouts.pop(active.id, None)
                self._check_remaining(active)
            if not result.skipped and material is not None and active is not None:
                diffs = drift.differences(tool, material, active, self._uuids)
                edited = self._edited(tool, st, material, active)
                if not diffs and not edited:
                    if st.needs_sync or st.sync_attempts:
                        self.tools[tool] = replace(st, needs_sync=False, sync_attempts=0)
                else:
                    self._classify_mismatch(tool, location, st, active,
                                            diffs + ([edited] if edited else []), notes, flags,
                                            edited=edited is not None)
            self._notes(tool, notes)

        for spool_id, text in self.state.runouts.items():
            flags.setdefault(spool_id, []).append(text)
        self._flags = {spool_id: " | ".join(texts) for spool_id, texts in flags.items()}
        if dry:
            append_ledger(self.config.ledger_file, events + charges)
        else:
            append_ledger(self.config.ledger_file, events)
            self.state.outbox.extend(charges)
        save_state(self.config.state_file, self.state)
        return events + charges

    def _check_runout(self, location: str, spool: Spool | None) -> None:
        """Flag a spool that unloaded mid-print with more than RUNOUT_LEFT remaining.

        That usually means an earlier swap charged the wrong spool. A planned color change
        unloads mid-print too, so the flag allows for that.
        """
        if spool is None or not spool.net_weight or spool.remaining_weight is None:
            return
        if spool.remaining_weight <= RUNOUT_LEFT * spool.net_weight:
            return
        text = (f"{location}: unloaded mid-print with {spool.remaining_weight:.0f} g left in "
                "Spoolman. If it ran out, its usage is probably on another spool: check "
                "recent swaps. Ignore this after a planned color change.")
        log.warning("[%s] spool %d unloaded mid-print with %.0f g left in Spoolman",
                    location, spool.id, spool.remaining_weight)
        self.state.runouts[spool.id] = text

    def _check_remaining(self, spool: Spool) -> None:
        """Log once when a loaded spool's remaining weight in Spoolman goes below 0."""
        key = ("negative", spool.id)
        if spool.remaining_weight is not None and spool.remaining_weight < 0:
            self._problems.fail(key, "negative",
                                "spool %d shows %.0f g remaining in Spoolman but is still "
                                "loaded; its weight is probably off", spool.id,
                                spool.remaining_weight, level=logging.INFO)
        else:
            self._problems.clear(key)

    def _edited(self, tool: int, st: ToolState, material, spool: Spool) -> str | None:
        """Return why the printer should get Spoolman's edit of `spool`, or None.

        That's when the printer still shows the bridge's last write, but the write the
        bridge would make now differs: someone changed the spool in Spoolman, not the
        printer. Comparing whole `M865` lines catches temperature edits, which the
        printer doesn't report back.
        """
        if st.written is None:
            return None
        if st.written == UNKNOWN_WRITE:
            return "rewriting once with full filament parameters"
        try:
            want = self._write_lines(tool, spool, spool.instance_uuid)
        except printer_sync.CannotSync:
            return None
        if want == st.written:
            return None
        if not printer_sync.printer_matches_write(material, st.written, self._uuids):
            return None
        return "spool changed in Spoolman since the last write"

    def _write_lines(self, tool: int, spool: Spool, uuid_value: str | None) -> str:
        uuid_value = uuid_value if tool == 0 and self._uuids else None
        return "\n".join(printer_sync.filament_lines(
            printer_sync.ToolWrite(tool, spool, uuid_value)))

    def _classify_mismatch(self, tool, location, st, active, diffs, notes, flags,
                           edited: bool = False) -> None:
        """Decide whether a printer/spool mismatch is a pending write or drift."""
        detail = "; ".join(diffs)
        expected = st.needs_sync or self.config.sync_drift or edited
        # Wait while a swap is staged: the active spool is about to change. During an
        # unconfirmed swap, write the assigned spool: the M117 then shows the user what
        # Spoolman thinks is loaded, and confirming a different spool rewrites it.
        settled = st.assigned == st.active
        if self.config.printer_sync and expected and st.sync_attempts < MAX_SYNC_ATTEMPTS:
            if settled and self._model_error:
                notes.append(f"[{location}] can't write spool {active.id} to the printer: "
                             f"{self._model_error}")
                flags.setdefault(active.id, []).append(
                    f"{location}: can't write to the printer: {self._model_error}"
                )
            elif settled:
                self._pending_writes.append(tool)
                notes.append(f"[{location}] printer will be set to spool {active.id} when "
                             f"idle: {detail}")
            else:
                notes.append(f"[{location}] printer will be set once spool {st.assigned} "
                             f"is loaded")
            return
        if self.config.printer_sync and expected:
            notes.append(f"[{location}] printer still differs from spool {active.id} after "
                         f"{st.sync_attempts} sync jobs: {detail}")
            flags.setdefault(active.id, []).append(
                f"{location}: writing this spool to the printer didn't take: {detail}"
            )
            return
        notes.append(f"[{location}] printer differs from spool {active.id}: {detail}")
        if self.config.flag_drift:
            flags.setdefault(active.id, []).append(
                f"{location}: printer filament differs: {detail}"
            )

    async def flush(self) -> bool:
        """Send queued charges in order. Returns False if any are still queued.

        A charge leaves the outbox only once Spoolman accepts it, so a crash or an
        outage delays charges instead of dropping them. A 4xx response, such as for a
        deleted spool, drops the charge with an error so it can't block the rest.
        """
        outbox = self.state.outbox
        sent = 0
        while outbox:
            rec = outbox[0]
            try:
                await self.spoolman.use_length(rec["spool_id"], rec["use_length"])
                rec = {**rec, "applied": _now()}
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code >= 500:
                    self._problems.fail(
                        "charge", code, "charge to spool %d failed (%d); %d queued, retrying",
                        rec["spool_id"], code, len(outbox))
                    return False
                log.error("Spoolman rejected a %+g mm charge to spool %d (%d %s); dropping it",
                          rec["use_length"], rec["spool_id"], code, e.response.text[:200])
                rec = {**rec, "failed": f"{code}"}
            except httpx.HTTPError as e:
                self._problems.fail(
                    "charge", e, "charge to spool %d failed (%s); %d queued, retrying",
                    rec["spool_id"], e, len(outbox))
                return False
            outbox.pop(0)
            sent += 1
            save_state(self.config.state_file, self.state)
            append_ledger(self.config.ledger_file, [rec])
        self._problems.clear("charge", "sent %d queued charge(s)", sent)
        return True

    async def evict(self, spools: list[Spool]) -> None:
        """Move each tool's outgoing spool out of the tool's location."""
        if self.config.dry_run:
            return
        where = {s.id: s.location for s in spools}
        target = self.config.eviction_location or None
        for tool, st in self.tools.items():
            location = self.config.tools.get(tool)
            if st.evict is None or where.get(st.evict) != location:
                if st.evict is not None:
                    self._problems.clear(("evict", st.evict))
                continue
            try:
                await self.spoolman.set_location(st.evict, target)
            except httpx.HTTPError as e:
                self._problems.fail(("evict", st.evict), e, "[%s] can't move spool %d out: %s",
                                    location, st.evict, e)
                continue
            self._problems.clear(("evict", st.evict))
            log.info("[%s] moved spool %d to %s", location, st.evict,
                     repr(target) if target else "no location")
            self.tools[tool] = replace(st, evict=None)
            save_state(self.config.state_file, self.state)

    async def sync_printer(self, spools: list[Spool]) -> None:
        """Write pending spools (and, if enabled, new flags) to the printer in one job."""
        if self._model_error:
            return
        by_id = {s.id: s for s in spools}
        writes: list[printer_sync.ToolWrite] = []
        for tool in self._pending_writes:
            st = self.tools[tool]
            spool = by_id.get(st.active)
            if spool is None:
                continue
            try:
                printer_sync.filament_lines(printer_sync.ToolWrite(tool, spool, None))
            except printer_sync.CannotSync as e:
                location = self.config.tools[tool]
                self._notes(tool, [f"[{location}] can't write spool {spool.id} to the printer: "
                                   f"{e}"])
                self._flags[spool.id] = " | ".join(
                    filter(None, [self._flags.get(spool.id), f"{location}: {e}"])
                )
                continue
            uuid_value = None
            if tool == 0 and self._uuids:
                uuid_value = spool.instance_uuid or await self._mint_uuid(spool)
            writes.append(printer_sync.ToolWrite(tool, spool, uuid_value))

        notices: list[str] = []
        if self.config.printer_notices:
            current = set(self._flags.values())
            notices = [t for t in self._flags.values() if t not in self.state.noticed]
            self.state.noticed = [t for t in self.state.noticed if t in current]

        if not writes and not notices:
            self._problems.clear("sync busy")
            self._problems.clear("sync failed")
            return
        gcode = printer_sync.build_job(
            self._preamble(), writes, notices, multi_tool=len(self.config.tools) > 1
        )
        if self.config.dry_run:
            log.info("WOULD RUN printer job %s:\n%s", self.config.sync_file, gcode.rstrip())
            return
        try:
            await self.printer.upload_and_print(
                self.config.sync_storage, self.config.sync_file, gcode
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                self._problems.fail("sync busy", "409",
                                    "printer busy; sync job waits for the next idle poll",
                                    level=logging.INFO)
            else:
                self._problems.fail(
                    "sync failed", f"{e.response.status_code} {e.response.text[:200]}",
                    "printer sync job failed (%d): %s", e.response.status_code,
                    e.response.text[:200])
            return
        except httpx.HTTPError as e:
            self._problems.fail("sync failed", e, "printer sync job failed: %s", e)
            return
        self._problems.clear("sync busy")
        self._problems.clear("sync failed", "printer sync job working again")
        log.info("ran printer job %s:\n%s", self.config.sync_file, gcode.rstrip())
        for w in writes:
            st = self.tools[w.tool]
            self.tools[w.tool] = replace(
                st, sync_attempts=st.sync_attempts + 1,
                written="\n".join(printer_sync.filament_lines(w)),
            )
        self.state.noticed += notices
        self._pending_writes = []
        self._last_state = "BUSY"  # The job is starting; poll again soon to verify it.
        save_state(self.config.state_file, self.state)

    async def _mint_uuid(self, spool: Spool) -> str | None:
        """Give a spool an instance UUID in Spoolman, so it can be written to the printer."""
        key = self.config.uuid_field
        assert key
        value = str(uuid.uuid4())
        if self.config.dry_run:
            log.info("would give spool %d instance UUID %s", spool.id, value)
            return value
        if not await self._ensure_field(key, UUID_FIELD_NAME):
            return None
        try:
            await self.spoolman.set_extra_text(spool.id, key, value)
        except httpx.HTTPError as e:
            self._problems.fail(("uuid", spool.id), e,
                                "can't store an instance UUID on spool %d: %s", spool.id, e)
            return None
        self._problems.clear(("uuid", spool.id))
        log.info("gave spool %d instance UUID %s", spool.id, value)
        return value

    async def _ensure_field(self, key: str, name: str) -> bool:
        if key in self._ready_fields:
            return True
        try:
            await self.spoolman.ensure_text_field(key, name)
        except (httpx.HTTPError, ValueError) as e:
            self._problems.fail(("field", key), e, "can't set up the %r spool field: %s", key, e)
            return False
        self._problems.clear(("field", key), "%r spool field is set up", key)
        self._ready_fields.add(key)
        return True

    async def sync_flags(self, spools: list[Spool]) -> None:
        """Make the flag field match the desired flags, on spools the bridge manages.

        The bridge only clears flags on spools it flagged itself (`State.flagged`), so it
        never touches a value someone else wrote.
        """
        key = self.config.flag_field
        assert key
        if not await self._ensure_field(key, FLAG_FIELD_NAME):
            return
        current = {s.id: s.flag for s in spools}
        changed = False
        for spool_id in sorted(set(self._flags) | set(self.state.flagged)):
            want = self._flags.get(spool_id)
            if spool_id not in current:
                if spool_id in self.state.flagged and want is None:
                    self.state.flagged.remove(spool_id)  # Spool deleted or archived.
                    changed = True
                continue
            if current[spool_id] != want:
                try:
                    await self.spoolman.set_extra_text(spool_id, key, want)
                except httpx.HTTPError as e:
                    self._problems.fail(("flag", spool_id), e,
                                        "can't update the flag on spool %d: %s", spool_id, e)
                    continue
                self._problems.clear(("flag", spool_id))
                log.info("%s flag on spool %d%s", "set" if want else "cleared", spool_id,
                         f": {want}" if want else "")
            if want is None:
                if spool_id in self.state.flagged:
                    self.state.flagged.remove(spool_id)
                    changed = True
            elif spool_id not in self.state.flagged:
                self.state.flagged.append(spool_id)
                changed = True
        if changed:
            save_state(self.config.state_file, self.state)

    def _notes(self, tool: int, notes: list[str]) -> None:
        key = tuple(notes)
        if self._last_notes.get(tool) != key:
            for n in notes:
                log.warning("%s", n)
        self._last_notes[tool] = key

    def interval(self) -> float:
        if self._printer_outage.down:
            return self.config.poll_idle
        if self._last_state is None or self._last_state in BUSY_STATES:
            return self.config.poll_printing
        return self.config.poll_idle

    async def run(self, stop: asyncio.Event) -> None:
        log.info(
            "starting%s: printer=%s spoolman=%s tools=%s",
            " in dry-run mode" if self.config.dry_run else "",
            self.config.printer_url, self.config.spoolman_url, self.config.tools,
        )
        if self.state.outbox:
            log.info("%d charge(s) queued from before the restart", len(self.state.outbox))
        while not stop.is_set():
            await self.poll()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval())
