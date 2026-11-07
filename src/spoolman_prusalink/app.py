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
from .accounting import (
    UNKNOWN_WRITE,
    WARNINGS,
    EventKind,
    Provisional,
    ToolState,
    account,
    hold_deleted,
)
from .config import Config, PrinterConfig
from .models import FilamentReading, Spool
from .prusalink import BUSY_STATES, IDLE_STATES, NoFilamentEndpoint, PrinterStatus, PrusaLink
from .spoolman import Spoolman
from .state import Runout, State, append_ledger, load_state, save_state

log = logging.getLogger("spoolman_prusalink")

# Events that repeat every poll while a condition lasts. Log them only when they change.
_REPEATING = {
    EventKind.HOLD_EMPTY,
    EventKind.HOLD_UNASSIGNED,
    EventKind.HOLD_DELETED,
    EventKind.AMBIGUOUS,
}

FLAG_FIELD_NAME = "Printer bridge flag"
UUID_FIELD_NAME = "Instance UUID"
CLAIM_FIELD_NAME = "Claim held usage"
# Sorts a spool that's never been used before any that has.
_NEVER = datetime.min.replace(tzinfo=UTC)
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

    def fail(
        self,
        key: Hashable,
        detail: object,
        message: str,
        *args: object,
        level: int = logging.WARNING,
    ) -> bool:
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
# Grams. Spoolman's weights are floats, so a smaller difference isn't an edit.
WEIGHT_EPSILON = 0.01

# Sync jobs per swap before the bridge gives up and flags the spool.
MAX_SYNC_ATTEMPTS = 2


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class PrinterBridge:
    """One printer's side of the bridge: read it, account its tools, and write to it.

    Everything keyed by spool rather than by tool lives on `Bridge`, which this reaches
    through `bridge`: the Spoolman client, the state file, flags, and the problem log.
    """

    config: PrinterConfig
    client: PrusaLink
    bridge: Bridge
    _last_notes: dict[int, tuple[str, ...]] = field(default_factory=dict)
    _last_state: str | None = None
    _pending_writes: list[int] = field(default_factory=list)  # Tools to write when idle.
    _job_names: dict[int, str | None] = field(default_factory=dict)
    _printer_outage: Outage = field(default_factory=lambda: Outage("printer"))
    _unsupported: bool = False  # The printer lacks the filament endpoint.
    # Why printer writes are off for this printer, or None. Checked once per boot.
    _model_error: str | None = None
    _model_boot: int | None = None
    _model: printer_sync.PrinterModel | None = None
    # Per tool: the provisional charge and the amount its flag shows. The amount is
    # updated only between prints, so the flag isn't rewritten on every poll.
    _provisional_shown: dict[int, tuple[tuple[int, int | None], int]] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def tools(self) -> dict[int, ToolState]:
        return self.bridge.state.tools

    async def read(self) -> tuple[PrinterStatus, FilamentReading] | None:
        """Read the printer's status and counters, or None if this poll can't use it.

        The status carries the user's job ID: None for the bridge's own sync job.
        """
        try:
            status = await self.client.status()
            reading = await self.client.filament()
            job_id = await self._user_job_id(status)
        except httpx.TransportError as e:
            # Connection refused, timeouts and the like: most often the printer is off.
            self._printer_outage.failed(e)
            return None
        except NoFilamentEndpoint as e:
            self._printer_outage.ok()
            if not self._unsupported:
                self._unsupported = True
                log.error(
                    "%s; this firmware isn't supported, so nothing is charged. "
                    "Checking again each poll.",
                    e,
                )
            return None
        except (httpx.HTTPError, ValueError, KeyError) as e:
            self._printer_outage.ok()  # It answered, even if badly.
            self.bridge._problems.fail(
                "printer read", e, "printer read failed, skipping poll: %s", e
            )
            return None
        self._printer_outage.ok()
        self.bridge._problems.clear("printer read", "printer reads working again")
        if self._unsupported:
            self._unsupported = False
            log.info("printer now reports filament; resuming")
        if self._model_boot != reading.boot_id:
            await self._check_model(reading.boot_id)
        return status._replace(job_id=job_id), reading

    async def check_missing(self, spools: list[Spool]) -> None:
        """Find out why an active spool is missing from Spoolman's list.

        The list leaves out archived spools. A deleted one (404) leaves its tool with no
        active spool, holding usage for the next one. An archived one keeps being charged
        until the next unload, and is flagged. Each spool is looked up once, not every poll.
        """
        listed = {s.id for s in spools}
        keep = {st.active for st in self.tools.values()} | set(self.bridge.state.flagged)
        self.bridge._archived = {
            i: s for i, s in self.bridge._archived.items() if i not in listed and i in keep
        }
        for tool, st in list(self.tools.items()):
            spool_id = st.active
            if spool_id is None or spool_id in listed or spool_id in self.bridge._archived:
                continue
            location = self.config.tools.get(tool, str(tool))
            try:
                found = await self.bridge.spoolman.spool(spool_id)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                self.bridge._problems.fail(
                    ("lookup", spool_id),
                    e,
                    "[%s] can't look up spool %d, which is missing from Spoolman's list: %s",
                    location,
                    spool_id,
                    e,
                )
                continue
            self.bridge._problems.clear(("lookup", spool_id))
            if found is None:
                self._hold_deleted(tool, spool_id)
            elif found.archived:
                self.bridge._archived[spool_id] = found
                log.warning(
                    "[%s] spool %d is archived but still loaded; charging it until the next unload",
                    location,
                    spool_id,
                )

    def _hold_deleted(self, tool: int, spool_id: int, mm: int = 0) -> bool:
        """Hold a deleted spool's usage for the tool's next spool. Returns whether it did."""
        st = self.tools.get(tool)
        if st is None or spool_id not in (st.active, st.deleted_spool):
            return False
        # Charges to this spool still queued, the rejected one included, are each held
        # as they're rejected, so they're left out of any open swap's usage.
        queued = sum(
            int(r.get("extruded_mm", 0))
            for r in self.bridge.state.outbox
            if r.get("tool") == tool
            and r.get("spool_id") == spool_id
            and int(r.get("extruded_mm", 0)) > 0
        )
        self.tools[tool] = hold_deleted(st, spool_id, mm, queued)
        location = self.config.tools.get(tool, str(tool))
        self.bridge._problems.fail(
            ("deleted", tool),
            spool_id,
            "[%s] spool %d was deleted in Spoolman; holding its usage for the next spool "
            "that becomes active in %s",
            location,
            spool_id,
            location,
        )
        # Not saved here: flush() saves with the record popped, and process() saves after.
        return True

    async def _check_model(self, boot_id: int) -> None:
        """Look up the printer's model; if the bridge can't write to it, writes stay off."""
        try:
            printer_type = await self.client.printer_type()
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
            log.warning(
                "printer is %s (%s): %s; printer writes are off",
                name,
                printer_type,
                self._model_error,
            )
        else:
            log.info("printer is %s (%s)", name, printer_type)

    @property
    def _uuids(self) -> bool:
        """Whether to write and compare instance UUIDs: configured with a Spoolman field to
        keep them in, and the model keeps them."""
        return bool(
            self.config.uuids
            and self.bridge.config.uuid_field
            and self._model is not None
            and self._model.uuids
        )

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
            self._job_names = {status.job_id: await self.client.job_display_name()}
        name = self._job_names[status.job_id]
        return None if name == self.config.sync_file else status.job_id

    def process(
        self,
        reading: FilamentReading,
        spools: list[Spool],
        status: PrinterStatus,
        claims: dict[int, tuple[int, list[int]]],
        flags: dict[int, list[str]],
    ) -> list[dict]:
        """Account one poll and persist the result.

        In dry-run mode, charges go straight to the ledger and the state isn't saved.
        Otherwise they go to the outbox, saved in the same write as the new counters, for
        `flush()` to send. Returns the charge and event records.

        `claims` is this printer's share of `Bridge._claims()`, by tool. Flags for spools
        are added to `flags`, which the bridge completes and writes after every printer.
        """
        by_id = {s.id: s for s in spools}
        now = _now()
        events: list[dict] = []
        charges: list[dict] = []
        dry = self.bridge.config.dry_run
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
                return self._uuids and spool is not None and drift.same_instance(material, spool)

            before = self.tools.get(tool, ToolState())
            claim, others = claims.get(tool, (None, []))
            result = account(
                before,
                reading.boot_id,
                tool_reading,
                location_ids,
                job_id=status.job_id,
                matches=matches,
                claim=claim,
            )
            # A skipped tool's state changes only to record the counter and any reboot.
            if not result.skipped or tool in self.tools:
                self.tools[tool] = result.state
            if result.runout is not None:
                self._check_runout(location, by_id.get(result.runout))
            if result.claimed:
                self._claim_handled(location, claim, others)

            notes = []
            for ev in result.events:
                if ev.kind in _REPEATING:
                    notes.append(f"[{location}] {ev.message}")
                    continue
                level = logging.WARNING if ev.kind in WARNINGS else logging.INFO
                log.log(level, "[%s] %s", location, ev.message)
                events.append(
                    {
                        "time": now,
                        "tool": tool,
                        "location": location,
                        "event": ev.kind.value,
                        "message": ev.message,
                        "dry_run": dry,
                    }
                )

            for c in result.charges:
                spool = by_id.get(c.spool_id)
                length = round(c.mm * self.bridge.config.length_factor, 1)
                grams = spool.mm_to_grams(length) if spool else None
                log.info(
                    "[%s] %s spool %d (%s): %+g mm%s (%s)",
                    location,
                    "WOULD CHARGE" if dry else "CHARGE",
                    c.spool_id,
                    spool.name if spool else "unknown",
                    length,
                    f" ≈ {grams:+.2f} g" if grams is not None else "",
                    c.reason,
                )
                charges.append(
                    {
                        "time": now,
                        "tool": tool,
                        "location": location,
                        "spool_id": c.spool_id,
                        "extruded_mm": c.mm,
                        "use_length": length,
                        "grams": None if grams is None else round(grams, 3),
                        "reason": c.reason,
                        "boot_id": reading.boot_id,
                        "extruded": tool_reading.extruded,
                        "dry_run": dry,
                    }
                )

            st = self.tools.get(tool, ToolState())
            if st.deleted_spool is None:
                self.bridge._problems.clear(("deleted", tool))
            elif st.assigned is not None:
                flags.setdefault(st.assigned, []).append(
                    f"{location}: usage is being held because spool {st.deleted_spool} was "
                    f"deleted. It goes to this spool at the next unload or print start, "
                    f"or when this spool is claimed."
                )
            if st.active in self.bridge._archived:
                flags.setdefault(st.active, []).append(
                    f"{location}: spool is archived but still loaded"
                )
            if dry:
                notes += [
                    f"[{location}] would move spool {i} out of {location!r}" for i in st.evict
                ]
            if st.swap is not None and st.active is None:
                flags.setdefault(st.swap.spool_id, []).append(
                    f"{location}: filament was unloaded, then this spool was moved out. "
                    f"Usage since the unload goes to the next spool moved into {location}."
                )
            elif st.swap is not None:
                flags.setdefault(st.swap.spool_id, []).append(
                    f"{location}: filament was unloaded with no Spoolman change. If a "
                    f"different spool went in, move it into {location} before the next print."
                )
            if st.notice is not None:
                flags.setdefault(st.notice.spool_id, []).append(f"{location}: {st.notice.message}.")
            if st.staged_notice is not None:
                hint = (
                    f" If this spool is the one loaded, tick '{CLAIM_FIELD_NAME}' on it."
                    if self.bridge.config.claim_field
                    else ""
                )
                flags.setdefault(st.staged_notice.spool_id, []).append(
                    f"{location}: {st.staged_notice.message}.{hint}"
                )
            if st.provisional is None:
                self._provisional_shown.pop(tool, None)
            else:
                flags.setdefault(st.provisional.spool_id, []).append(
                    self._provisional_flag(
                        tool, location, st.provisional, by_id.get(st.provisional.spool_id), status
                    )
                )
            active = by_id.get(st.active) if st.active is not None else None
            if active is not None:
                if active.id != before.active:
                    self.bridge.state.runouts.pop(active.id, None)
                self._check_remaining(active)
            if not result.skipped and material is not None and active is not None:
                if printer_sync.loaded_from_menu(material.name, active):
                    diffs, edited = [], None  # As the "can't write" flag asked.
                else:
                    diffs = drift.differences(tool, material, active, self._uuids)
                    # With the location emptied, the spool is leaving and won't be
                    # rewritten, so only a real printer difference is worth reporting.
                    edited = (
                        self._edited(tool, st, material, active)
                        if st.assigned is not None
                        else None
                    )
                if not diffs and not edited:
                    if st.needs_sync or st.sync_attempts:
                        self.tools[tool] = replace(st, needs_sync=False, sync_attempts=0)
                else:
                    self._classify_mismatch(
                        tool,
                        location,
                        st,
                        active,
                        diffs + ([edited] if edited else []),
                        notes,
                        flags,
                        edited=edited is not None,
                    )
            self._notes(tool, notes)

        if dry:
            append_ledger(self.bridge.config.ledger_file, events + charges)
        else:
            append_ledger(self.bridge.config.ledger_file, events)
            self.bridge.state.outbox.extend(charges)
        self.bridge._save()
        return events + charges

    def _check_runout(self, location: str, spool: Spool | None) -> None:
        """Flag a spool that unloaded mid-print with more than RUNOUT_LEFT remaining.

        That usually means an earlier swap charged the wrong spool. A jam or tangle that
        trips the filament sensor, and a planned color change, unload mid-print too, so the
        flag allows for them.
        """
        if spool is None or not spool.net_weight or spool.remaining_weight is None:
            return
        if spool.remaining_weight <= RUNOUT_LEFT * spool.net_weight:
            return
        text = (
            f"{location}: unloaded mid-print with {spool.remaining_weight:.0f} g left in "
            "Spoolman. If it ran out, its usage is probably on another spool: check "
            "recent swaps, then correct this spool's weight, which clears this. The "
            "filament may also have jammed or tangled. Ignore this after a planned "
            "color change."
        )
        log.warning(
            "[%s] spool %d unloaded mid-print with %.0f g left in Spoolman",
            location,
            spool.id,
            spool.remaining_weight,
        )
        self.bridge.state.runouts[spool.id] = Runout(text, spool.net_weight, spool.used_weight)

    def _check_remaining(self, spool: Spool) -> None:
        """Log once when a loaded spool's remaining weight in Spoolman goes below 0."""
        key = ("negative", spool.id)
        if spool.remaining_weight is not None and spool.remaining_weight < 0:
            self.bridge._problems.fail(
                key,
                "negative",
                "spool %d shows %.0f g remaining in Spoolman but is still "
                "loaded; its weight is probably off",
                spool.id,
                spool.remaining_weight,
                level=logging.INFO,
            )
        else:
            self.bridge._problems.clear(key)

    def _claim_handled(self, location: str, claim: int, others: list[int]) -> None:
        """Record a handled claim and the claims it beat, so their boxes are cleared."""
        if others:
            log.warning(
                "[%s] several spools claimed at once: chose spool %d, the most "
                "recently used, and clearing the claims on spools %s",
                location,
                claim,
                ", ".join(str(i) for i in others),
            )
        for spool_id in [claim, *others]:
            if spool_id not in self.bridge.state.claimed:
                self.bridge.state.claimed.append(spool_id)
            if self.bridge.config.dry_run:
                log.info("[%s] would clear the claim on spool %d", location, spool_id)

    def _provisional_flag(
        self, tool: int, location: str, p: Provisional, spool: Spool | None, status: PrinterStatus
    ) -> str:
        key = (p.spool_id, p.job_id)
        shown = self._provisional_shown.get(tool)
        if shown is None or shown[0] != key or status.job_id is None:
            shown = self._provisional_shown[tool] = (key, p.mm)
        length = round(shown[1] * self.bridge.config.length_factor, 1)
        grams = spool.mm_to_grams(length) if spool else None
        amount = f"{length:g} mm" + (f" (≈ {grams:.0f} g)" if grams is not None else "")
        how = (
            f"move that spool into {location} and tick '{CLAIM_FIELD_NAME}' on it to "
            "move the usage there"
            if self.bridge.config.claim_field
            else "move the usage in Spoolman by hand"
        )
        return (
            f"{location}: a print started with no Spoolman change since the unload, "
            f"so this spool was assumed reloaded and {amount} is charged to it "
            f"provisionally. If a different spool is loaded, {how}. The next unload "
            "keeps the usage here."
        )

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
            # The edit can't be written, but it's still an edit, not drift:
            # _classify_mismatch flags why.
            want = None
        if want == st.written:
            return None
        if not printer_sync.printer_matches_write(material, st.written, self._uuids):
            return None
        return "spool changed in Spoolman since the last write"

    def _write_lines(self, tool: int, spool: Spool, uuid_value: str | None) -> str:
        uuid_value = uuid_value if tool == 0 and self._uuids else None
        return "\n".join(
            printer_sync.filament_lines(printer_sync.ToolWrite(tool, spool, uuid_value))
        )

    def _cannot_write(self, tool: int, spool: Spool) -> str | None:
        """Return why `spool` can't be written to the printer, or None if it can."""
        try:
            self._write_lines(tool, spool, spool.instance_uuid)
        except printer_sync.CannotSync as e:
            return str(e)
        return None

    def _classify_mismatch(
        self, tool, location, st, active, diffs, notes, flags, edited: bool = False
    ) -> None:
        """Decide whether a printer/spool mismatch is a pending write or drift."""
        detail = "; ".join(diffs)
        # With the location emptied, the active spool is still loaded but is leaving:
        # don't write it, report the mismatch as drift.
        expected = st.assigned is not None and (
            st.needs_sync or self.bridge.config.sync_drift or edited
        )
        # Wait while a swap is staged: the active spool is about to change. During an
        # unconfirmed swap, write the assigned spool: the M117 then shows the user what
        # Spoolman thinks is loaded, and confirming a different spool rewrites it.
        settled = st.assigned == st.active
        if self.bridge.config.printer_sync and expected and st.sync_attempts < MAX_SYNC_ATTEMPTS:
            if settled and self._model_error:
                notes.append(
                    f"[{location}] can't write spool {active.id} to the printer: "
                    f"{self._model_error}"
                )
                flags.setdefault(active.id, []).append(
                    f"{location}: can't write to the printer: {self._model_error}"
                )
            elif settled and (problem := self._cannot_write(tool, active)):
                # Noted here, not in sync_printer, so it's logged once rather than
                # alternating with the "will be set" note on every idle poll.
                notes.append(
                    f"[{location}] can't write spool {active.id} to the printer: {problem}"
                )
                flags.setdefault(active.id, []).append(f"{location}: {problem}")
            elif settled:
                self._pending_writes.append(tool)
                # After a sync job, the printer can take a poll to show it. Don't warn
                # about that wait: a retry logs its job, and giving up logs below.
                if not st.sync_attempts or edited:
                    notes.append(
                        f"[{location}] printer will be set to spool {active.id} when idle: {detail}"
                    )
            else:
                notes.append(f"[{location}] printer will be set once spool {st.assigned} is loaded")
            return
        if self.bridge.config.printer_sync and expected:
            notes.append(
                f"[{location}] printer still differs from spool {active.id} after "
                f"{st.sync_attempts} sync jobs: {detail}"
            )
            flags.setdefault(active.id, []).append(
                f"{location}: writing this spool to the printer didn't take: {detail}"
            )
            return
        notes.append(f"[{location}] printer differs from spool {active.id}: {detail}")
        if self.bridge.config.flag_drift:
            flags.setdefault(active.id, []).append(
                f"{location}: printer filament differs: {detail}"
            )

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
            uuid_value = None
            if tool == 0 and self._uuids:
                uuid_value = spool.instance_uuid or await self.bridge._mint_uuid(spool)
            writes.append(printer_sync.ToolWrite(tool, spool, uuid_value))

        notices: list[str] = []
        if self.bridge.config.printer_notices:
            current = set(self.bridge._flags.values())
            notices = [t for t in self.bridge._flags.values() if t not in self.bridge.state.noticed]
            self.bridge.state.noticed = [t for t in self.bridge.state.noticed if t in current]

        if not writes and not notices:
            self.bridge._problems.clear("dry-run job")
            self.bridge._problems.clear("sync busy")
            self.bridge._problems.clear("sync failed")
            return
        gcode, shown = printer_sync.build_job(
            self._preamble(), writes, notices, multi_tool=len(self.config.tools) > 1
        )
        if self.bridge.config.dry_run:
            # Nothing is recorded as written, so the same job is due every idle poll.
            self.bridge._problems.fail(
                "dry-run job",
                gcode,
                "WOULD RUN printer job %s:\n%s",
                self.config.sync_file,
                gcode.rstrip(),
                level=logging.INFO,
            )
            return
        try:
            await self.client.upload_and_print(
                self.config.sync_storage, self.config.sync_file, gcode
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                self.bridge._problems.fail(
                    "sync busy",
                    "409",
                    "printer busy; sync job waits for the next idle poll",
                    level=logging.INFO,
                )
            else:
                self.bridge._problems.fail(
                    "sync failed",
                    f"{e.response.status_code} {e.response.text[:200]}",
                    "printer sync job failed (%d): %s",
                    e.response.status_code,
                    e.response.text[:200],
                )
            return
        except httpx.HTTPError as e:
            self.bridge._problems.fail("sync failed", e, "printer sync job failed: %s", e)
            return
        self.bridge._problems.clear("sync busy")
        self.bridge._problems.clear("sync failed", "printer sync job working again")
        log.info("ran printer job %s:\n%s", self.config.sync_file, gcode.rstrip())
        for w in writes:
            st = self.tools[w.tool]
            # Expected until a poll sees the printer match, so an edit write that
            # hasn't run yet isn't drift and one that didn't take is retried (W5).
            self.tools[w.tool] = replace(
                st,
                needs_sync=True,
                sync_attempts=st.sync_attempts + 1,
                written="\n".join(printer_sync.filament_lines(w)),
            )
        self.bridge.state.noticed += shown
        self._pending_writes = []
        self._last_state = "BUSY"  # The job is starting; poll again soon to verify it.
        self.bridge._save()

    def _notes(self, tool: int, notes: list[str]) -> None:
        key = tuple(notes)
        if self._last_notes.get(tool) != key:
            for n in notes:
                log.warning("%s", n)
        self._last_notes[tool] = key


@dataclass
class Bridge:
    """The poll loop and everything keyed by spool: charges, flags, claims, and evictions."""

    config: Config
    spoolman: Spoolman
    clients: dict[str, PrusaLink]  # By printer name.
    state: State = field(default_factory=State)
    printers: list[PrinterBridge] = field(init=False, default_factory=list)
    _ready_fields: set[str] = field(default_factory=set)
    _flags: dict[int, str] = field(default_factory=dict)  # Desired flags from the last poll.
    _spoolman_outage: Outage = field(default_factory=lambda: Outage("Spoolman"))
    _problems: Once = field(default_factory=Once)
    # Active spools missing from Spoolman's list that turned out to be archived, looked
    # up once each. Kept while active or flagged, so their flag can be set and cleared.
    _archived: dict[int, Spool] = field(default_factory=dict)
    # Claimed spools outside every tool's location, logged once until that changes.
    _unplaced: set[int] = field(default_factory=set)
    # Dry run: the UUID each spool would be given, reused so the job stays the same.
    _dry_uuids: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.printers = [PrinterBridge(p, self.clients[p.name], self) for p in self.config.printers]

    @classmethod
    def from_config(cls, config: Config) -> Bridge:
        return cls(
            config=config,
            clients={p.name: PrusaLink(p.url, p.api_key) for p in config.printers},
            spoolman=Spoolman(
                config.spoolman_url,
                uuid_field=config.uuid_field,
                flag_field=None if config.dry_run else config.flag_field,
                claim_field=config.claim_field,
            ),
            state=load_state(config.state_file),
        )

    def _save(self) -> None:
        """Save the state, except in dry run.

        Dry run keeps its accounting in memory only, as if the bridge were stopped: the
        next live run starts from the last live state and charges everything since, as
        after an outage.
        """
        if not self.config.dry_run:
            save_state(self.config.state_file, self.state)

    async def aclose(self) -> None:
        for client in self.clients.values():
            await client.aclose()
        await self.spoolman.aclose()

    async def poll(self) -> None:
        # Send charges left over from a failed poll or a restart before reading anything.
        if not await self.flush():
            return
        printer = self.printers[0]  # One printer until the loop serves several.
        read = await printer.read()
        if read is None:
            return
        status, reading = read
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
        printer._last_state = status.state
        await printer.check_missing(spools)
        flags: dict[int, list[str]] = {}
        claims = self._claims(spools, flags)
        printer.process(reading, spools, status, claims.get(printer.name, {}), flags)
        self._finish_flags(flags, spools)
        await self.flush()
        await self.evict(spools)
        if not self.config.dry_run and self.config.claim_field:
            await self.sync_claims(spools)
        if status.state in IDLE_STATES:
            await printer.sync_printer(spools)
        if not self.config.dry_run and self.config.flag_field:
            await self.sync_flags(spools)

    def _claims(
        self, spools: list[Spool], flags: dict[int, list[str]]
    ) -> dict[str, dict[int, tuple[int, list[int]]]]:
        """Return each tool's claim to handle this poll, and the other claims for it.

        Keyed by printer name, then tool index.

        A claim is handled once: the bridge then clears the box, and until Spoolman
        shows it clear, the spool stays in `State.claimed`. A claimed spool must be in a
        tool's location, which says which tool it's in. Spoolman doesn't record when a
        box was ticked, so of several claims for one tool the most recently used spool
        wins, with never-used spools oldest and ties going to the highest ID.
        """
        if not self.config.claim_field:
            return {}
        ticked = {s.id: s for s in spools if s.claim}
        handled = [i for i in self.state.claimed if i in ticked]
        if handled != self.state.claimed:
            self.state.claimed = handled  # Boxes now clear in Spoolman.
        tools = {
            location: (printer.name, tool)
            for printer in self.printers
            for tool, location in printer.config.tools.items()
        }
        per_tool: dict[tuple[str, int], list[Spool]] = {}
        unplaced: set[int] = set()
        for spool in ticked.values():
            if spool.id in handled:
                continue
            tool = tools.get(spool.location or "")
            if tool is None:
                unplaced.add(spool.id)
                flags.setdefault(spool.id, []).append(
                    f"'{CLAIM_FIELD_NAME}' is ticked, but this spool isn't in a printer "
                    "tool's location. Move it into the location of the tool it's loaded in."
                )
                self._problems.fail(
                    ("claim", spool.id),
                    spool.location,
                    "spool %d is claimed but isn't in a tool's location (it's in %s); "
                    "waiting until it is",
                    spool.id,
                    repr(spool.location or "no location"),
                )
                continue
            per_tool.setdefault(tool, []).append(spool)
        for spool_id in self._unplaced - unplaced:
            self._problems.clear(("claim", spool_id))
        self._unplaced = unplaced
        chosen: dict[str, dict[int, tuple[int, list[int]]]] = {}
        for (name, tool), candidates in per_tool.items():
            candidates.sort(key=lambda s: (s.last_used or _NEVER, s.id), reverse=True)
            winner, *others = candidates
            chosen.setdefault(name, {})[tool] = (winner.id, [s.id for s in others])
        return chosen

    def _finish_flags(self, flags: dict[int, list[str]], spools: list[Spool]) -> None:
        """Add the Spoolman-wide flags to the printers' and keep the result for `sync_flags()`."""
        by_id = {s.id: s for s in spools}
        if self._check_runout_edits(by_id):
            self._save()
        for spool_id, runout in self.state.runouts.items():
            flags.setdefault(spool_id, []).append(runout.text)
        self._flags = {spool_id: " | ".join(texts) for spool_id, texts in flags.items()}

    def _follow_runout_charge(self, rec: dict, used: float | None) -> None:
        """Move a runout flag's used weight on by a charge Spoolman accepted.

        Only when the response is the stored weight plus this charge: anything else
        includes a change made in Spoolman since the last read, and leaving the stored
        weight alone lets the next read see it as an edit.
        """
        runout = self.state.runouts.get(rec["spool_id"])
        if runout is None or used is None:
            return
        grams = rec.get("grams")
        if runout.used is None or grams is None:
            runout.used = used
            return
        # Spoolman clamps a refund at 0.
        if abs(used - max(runout.used + grams, 0.0)) <= WEIGHT_EPSILON:
            runout.used = used

    def _check_runout_edits(self, by_id: dict[int, Spool]) -> bool:
        """Clear runout flags on spools whose weight the user edited in Spoolman.

        A corrected weight answers the flag. A charge whose response was lost also looks
        like an edit, which clears the flag early; that's rare and costs only the flag.
        Returns whether any flag was cleared.
        """
        cleared = False
        for spool_id, runout in list(self.state.runouts.items()):
            spool = by_id.get(spool_id)
            if spool is None:
                continue
            if runout.net is None or runout.used is None:
                runout.net, runout.used = spool.net_weight, spool.used_weight
                continue
            if (
                spool.net_weight is not None
                and spool.used_weight is not None
                and (
                    abs(spool.net_weight - runout.net) > WEIGHT_EPSILON
                    or abs(spool.used_weight - runout.used) > WEIGHT_EPSILON
                )
            ):
                log.info(
                    "spool %d's weight was edited in Spoolman; clearing its runout flag", spool_id
                )
                del self.state.runouts[spool_id]
                cleared = True
        return cleared

    async def sync_claims(self, spools: list[Spool]) -> None:
        """Clear the claim box on spools whose claim has been handled."""
        key = self.config.claim_field
        assert key
        if not await self._ensure_field(key, CLAIM_FIELD_NAME, "boolean"):
            return
        ticked = {s.id for s in spools if s.claim}
        changed = False
        for spool_id in list(self.state.claimed):
            if spool_id not in ticked:
                continue
            try:
                await self.spoolman.set_extra(spool_id, key, False)
            except httpx.HTTPError as e:
                self._problems.fail(
                    ("unclaim", spool_id), e, "can't clear the claim on spool %d: %s", spool_id, e
                )
                continue
            self._problems.clear(("unclaim", spool_id))
            log.info("cleared the claim on spool %d", spool_id)
            self.state.claimed.remove(spool_id)
            changed = True
        if changed:
            self._save()

    async def flush(self) -> bool:
        """Send queued charges in order. Returns False if any are still queued.

        A charge leaves the outbox only once Spoolman accepts it, so a crash or an
        outage delays charges instead of dropping them. A 404 for the tool's active (or
        already deleted) spool means it was deleted: the charge is held for the tool's
        next spool. Any other 4xx drops the charge with an error so it can't block the rest.

        Dry run sends nothing: charges queued by a live run stay queued until it's off.
        """
        outbox = self.state.outbox
        if self.config.dry_run and outbox:
            self._problems.fail(
                "dry-run outbox",
                len(outbox),
                "%d queued charge(s) held until dry run is off",
                len(outbox),
            )
            return True
        self._problems.clear("dry-run outbox")
        sent = 0
        while outbox:
            rec = outbox[0]
            try:
                used = await self.spoolman.use_length(rec["spool_id"], rec["use_length"])
                rec = {**rec, "applied": _now()}
                self._follow_runout_charge(rec, used)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code >= 500:
                    self._problems.fail(
                        "charge",
                        code,
                        "charge to spool %d failed (%d); %d queued, retrying",
                        rec["spool_id"],
                        code,
                        len(outbox),
                    )
                    return False
                mm = int(rec.get("extruded_mm", 0))
                if (
                    code == 404
                    and mm > 0
                    and self.printers[0]._hold_deleted(rec.get("tool", -1), rec["spool_id"], mm)
                ):
                    rec = {**rec, "failed": f"{code}", "held": True}
                else:
                    log.error(
                        "Spoolman rejected a %+g mm charge to spool %d (%d %s); dropping it",
                        rec["use_length"],
                        rec["spool_id"],
                        code,
                        e.response.text[:200],
                    )
                    rec = {**rec, "failed": f"{code}"}
            except httpx.HTTPError as e:
                self._problems.fail(
                    "charge",
                    e,
                    "charge to spool %d failed (%s); %d queued, retrying",
                    rec["spool_id"],
                    e,
                    len(outbox),
                )
                return False
            outbox.pop(0)
            sent += 1
            self._save()
            append_ledger(self.config.ledger_file, [rec])
        self._problems.clear("charge", "sent %d queued charge(s)", sent)
        return True

    async def evict(self, spools: list[Spool]) -> None:
        """Move each tool's outgoing spools out of the tool's location."""
        if self.config.dry_run:
            return
        where = {s.id: s.location for s in spools}
        target = self.config.eviction_location or None
        for printer in self.printers:
            for tool, st in list(printer.tools.items()):
                location = printer.config.tools.get(tool)
                for spool_id in st.evict:
                    if where.get(spool_id) != location:
                        self._problems.clear(("evict", spool_id))
                        continue
                    try:
                        await self.spoolman.set_location(spool_id, target)
                    except httpx.HTTPError as e:
                        self._problems.fail(
                            ("evict", spool_id),
                            e,
                            "[%s] can't move spool %d out: %s",
                            location,
                            spool_id,
                            e,
                        )
                        continue
                    self._problems.clear(("evict", spool_id))
                    log.info(
                        "[%s] moved spool %d to %s",
                        location,
                        spool_id,
                        repr(target) if target else "no location",
                    )
                    st = replace(st, evict=tuple(i for i in st.evict if i != spool_id))
                    printer.tools[tool] = st
                    self._save()

    async def _mint_uuid(self, spool: Spool) -> str | None:
        """Give a spool an instance UUID in Spoolman, so it can be written to the printer."""
        key = self.config.uuid_field
        assert key
        if self.config.dry_run:
            if spool.id not in self._dry_uuids:
                self._dry_uuids[spool.id] = str(uuid.uuid4())
                log.info(
                    "would give spool %d instance UUID %s", spool.id, self._dry_uuids[spool.id]
                )
            return self._dry_uuids[spool.id]
        value = str(uuid.uuid4())
        if not await self._ensure_field(key, UUID_FIELD_NAME):
            return None
        try:
            await self.spoolman.set_extra_text(spool.id, key, value)
        except httpx.HTTPError as e:
            self._problems.fail(
                ("uuid", spool.id), e, "can't store an instance UUID on spool %d: %s", spool.id, e
            )
            return None
        self._problems.clear(("uuid", spool.id))
        log.info("gave spool %d instance UUID %s", spool.id, value)
        return value

    async def _ensure_field(self, key: str, name: str, field_type: str = "text") -> bool:
        if key in self._ready_fields:
            return True
        try:
            await self.spoolman.ensure_field(key, name, field_type)
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
        current = {s.id: s.flag for s in [*spools, *self._archived.values()]}
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
                    self._problems.fail(
                        ("flag", spool_id), e, "can't update the flag on spool %d: %s", spool_id, e
                    )
                    continue
                self._problems.clear(("flag", spool_id))
                if spool_id in self._archived:
                    self._archived[spool_id] = replace(self._archived[spool_id], flag=want)
                log.info(
                    "%s flag on spool %d%s",
                    "set" if want else "cleared",
                    spool_id,
                    f": {want}" if want else "",
                )
            if want is None:
                if spool_id in self.state.flagged:
                    self.state.flagged.remove(spool_id)
                    changed = True
            elif spool_id not in self.state.flagged:
                self.state.flagged.append(spool_id)
                changed = True
        if changed:
            self._save()

    def interval(self) -> float:
        printer = self.printers[0]
        if printer._printer_outage.down:
            return self.config.poll_idle
        if printer._last_state is None or printer._last_state in BUSY_STATES:
            return self.config.poll_printing
        return self.config.poll_idle

    async def run(self, stop: asyncio.Event) -> None:
        log.info(
            "starting%s: printer=%s spoolman=%s tools=%s",
            " in dry-run mode" if self.config.dry_run else "",
            self.printers[0].config.url,
            self.config.spoolman_url,
            self.printers[0].config.tools,
        )
        if self.state.outbox:
            log.info("%d charge(s) queued from before the restart", len(self.state.outbox))
        while not stop.is_set():
            await self.poll()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval())
