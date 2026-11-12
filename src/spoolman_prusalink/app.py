"""The poll loop: account usage, charge it, keep flags current, and sync the printer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import NamedTuple

import httpx  # For the transport types `from_config` takes; errors come from .errors.

from . import conditions, drift, printer_sync, tags
from .accounting import ToolState, account, hold_deleted
from .archive import ArchivedSpools
from .conditions import (
    CLAIM_FIELD_NAME,
    UUID_FIELD_NAME,
    ArchivedLoaded,
    Condition,
    Drift,
    HeldForDeleted,
    Lasting,
    LoadedWhileStaged,
    LoggedTexts,
    PrinterUnwritable,
    SpoolUnwritable,
    StagedNotLoaded,
    ToolNotReported,
    UnloadedMovedOut,
    UnloadUnconfirmed,
    UuidConflict,
    WriteDidntTake,
    WritePending,
    WriteWaitsForLoad,
)
from .config import Config, PrinterConfig
from .db import Database
from .errors import Busy, NotFound, Rejected, ServiceError, Unreachable
from .events import Report, Unload
from .models import FilamentReading, Spool
from .prusalink import BUSY_STATES, IDLE_STATES, NoFilamentEndpoint, PrinterStatus, PrusaLink
from .records import ChargeRecord, EventRecord
from .spoolman import Spoolman
from .state import PrinterState, Runout, State
from .tags import Decision, SeenTag

log = logging.getLogger("spoolman_prusalink")

FLAG_FIELD_NAME = "Printer bridge flag"
# Sorts a spool that's never been used before any that has.
_NEVER = datetime.min.replace(tzinfo=UTC)
# While a service stays in trouble, remind once per this many seconds.
REACH_REMINDER = 3600


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


class Trouble(Enum):
    """What a failed read means for the log."""

    STARTED = "started"
    # Unreachable became a bad answer, or the reverse, or a bad answer is a different error.
    CHANGED = "changed"
    REMINDER = "reminder"
    REPEAT = "repeat"


class Recovery(NamedTuple):
    unreachable: bool  # The kind of trouble that ended.
    lasted: float


@dataclass
class Reachability:
    """Track whether a service can be read, and for how long it hasn't been.

    The trouble is "unreachable" (a transport error) or a "bad answer" (the service
    replied, but badly). A bad answer with a different error is a change too, since a
    wrong key and a server error need different fixes. A transport error's text isn't:
    refusals and timeouts come and go while a printer is off. A change starts the clock
    again, so a reminder or recovery line gives the length of the current trouble.

    This class holds state only. `failed()` and `ok()` report what changed, and the
    caller logs it. `clock` reads the bridge's clock.
    """

    name: str
    clock: Callable[[], float]
    since: float | None = None
    unreachable: bool = False
    error: str = ""  # The bad answer's error; unused while unreachable.
    _reminded: float = 0.0

    @property
    def down(self) -> bool:
        """Whether the service is unreachable. A bad answer still counts as up."""
        return self.since is not None and self.unreachable

    def failed(self, unreachable: bool, error: str = "") -> Trouble:
        now = self.clock()
        error = "" if unreachable else error
        if self.since is None:
            self.since = self._reminded = now
            self.unreachable, self.error = unreachable, error
            return Trouble.STARTED
        if (unreachable, error) != (self.unreachable, self.error):
            self.since = self._reminded = now  # Time and reminders measure this trouble.
            self.unreachable, self.error = unreachable, error
            return Trouble.CHANGED
        if now - self._reminded >= REACH_REMINDER:
            self._reminded = now
            return Trouble.REMINDER
        return Trouble.REPEAT

    def ok(self) -> Recovery | None:
        if self.since is None:
            return None
        recovery = Recovery(self.unreachable, self.clock() - self.since)
        self.since = None
        return recovery

    def lasting(self) -> str:
        assert self.since is not None
        return _duration(self.clock() - self.since)


def _log_failed(reach: Reachability, error: ServiceError, *, unreachable: bool) -> None:
    """Record a failed read and log what it changed."""
    trouble = reach.failed(unreachable, error.summary)
    if trouble in (Trouble.STARTED, Trouble.CHANGED):
        if unreachable:
            log.warning("%s unreachable: %s; logging again when it's back", reach.name, error)
        else:
            log.warning("%s read failed, skipping poll: %s", reach.name, error)
    elif trouble is Trouble.REMINDER:
        what = "unreachable" if unreachable else "giving bad answers"
        log.info("%s still %s after %s", reach.name, what, reach.lasting())
    else:
        if unreachable:
            log.debug("%s still unreachable: %s", reach.name, error)
        else:
            log.debug("%s read failed, skipping poll: %s", reach.name, error)


def _log_ok(reach: Reachability) -> None:
    """Record a good read and log the end of any trouble."""
    recovery = reach.ok()
    if recovery is None:
        return
    if recovery.unreachable:
        log.warning("%s reachable again after %s", reach.name, _duration(recovery.lasted))
    else:
        log.warning("%s reads working again after %s", reach.name, _duration(recovery.lasted))


def _record(failing: dict, key: Hashable, error: ServiceError) -> bool:
    """Note `error` as what's failing at `key`. Returns whether it's new or changed."""
    text = error.summary
    new = failing.get(key) != text
    failing[key] = text
    return new


# A mid-print unload with more than this fraction of the net weight left in Spoolman
# gets a flag.
RUNOUT_LEFT = 0.05
# Grams. Spoolman's weights are floats, so a smaller difference isn't an edit.
WEIGHT_EPSILON = 0.01

# Sync jobs per swap before the bridge gives up and flags the spool.
MAX_SYNC_ATTEMPTS = 2


def _outbox_outcome(sent: int, held: int, dropped: int) -> str:
    """Word what a flush did with the queued charges, e.g. "sent 2 queued charge(s)"."""
    parts = [f"sent {sent} queued charge(s)"]
    if held:
        parts.append(f"held {held} for the next spool")
    if dropped:
        parts.append(f"dropped {dropped}")
    return "; ".join(parts)


# Flag texts by spool. A spool's texts are joined into its one flag.
Flags = dict[int, list[str]]


def _add_flags(flags: Flags, more: Flags) -> None:
    for spool_id, texts in more.items():
        flags.setdefault(spool_id, []).extend(texts)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class _Adoption(NamedTuple):
    """A UUID the printer shows for a spool that has none in Spoolman."""

    spool_id: int
    uuid: str
    # Whether to store it on the spool (adopt) or only keep it on the printer.
    store: bool


@dataclass
class Plan:
    """What `process()` found to do for a printer once the poll's charges are sent."""

    writes: list[int] = field(default_factory=list)  # Tools to write when idle.
    # Per tool: the UUID the printer shows for the active spool, which has none.
    adoptions: dict[int, _Adoption] = field(default_factory=dict)


@dataclass
class PrinterBridge:
    """One printer's side of the bridge: read it, account its tools, and write to it.

    Everything keyed by spool rather than by tool lives on `Bridge`, which this reaches
    through `bridge`: the Spoolman client, the saved state, flags, and the problem log.
    """

    config: PrinterConfig
    client: PrusaLink
    bridge: Bridge
    # How log lines name this printer: "printer" alone when it's the only one.
    label: str = "printer"
    _printer_reach: Reachability = field(init=False)
    # What's been logged about each tool's conditions. It starts empty, so the conditions
    # standing at a restart are logged again.
    _logged: dict[int, LoggedTexts] = field(default_factory=dict)
    # Spools that can't be looked up, with the last error logged for each.
    _lookup_failing: dict[int, str] = field(default_factory=dict)
    # The sync job: waiting out the idle delay, the busy state or "409" it's waiting on,
    # and the detail of the failure that's last been logged.
    _sync_delaying: bool = False
    _sync_busy: str | None = None
    _sync_failed: str | None = None
    _last_state: str | None = None
    # A sync job is starting: poll again soon to verify it, whatever state was last read.
    _poll_soon: bool = False
    _job_names: dict[int, str | None] = field(default_factory=dict)
    _unsupported: bool = False  # The printer lacks the filament endpoint.
    # Why printer writes are off for this printer, or None. Checked once per boot.
    _model_error: str | None = None
    _model_boot: int | None = None
    _model: printer_sync.PrinterModel | None = None
    # Per tool: the deleted spool a hold was last logged for. Starts empty, so a hold
    # restored after a restart is logged once.
    _hold_logged: dict[int, int] = field(default_factory=dict)
    # When the printer was first seen idle since it was last busy or unreachable.
    _idle_since: float | None = None

    def __post_init__(self) -> None:
        # Read through the bridge, so a clock set on it later reaches this one too.
        self._printer_reach = Reachability(self.label, lambda: self.bridge.clock())

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def tools(self) -> dict[int, ToolState]:
        return self.bridge.state.tools(self.name)

    @property
    def writes(self) -> dict[int, printer_sync.WriteState]:
        """Where each tool's write to the printer stands, which the database keeps."""
        return self.bridge.state.printers.setdefault(self.name, PrinterState()).writes

    @property
    def busy(self) -> bool:
        """Whether to poll at the printing rate: the printer is busy, hasn't been read
        yet, or is starting a sync job."""
        return self._poll_soon or self._last_state is None or self._last_state in BUSY_STATES

    @property
    def conditions(self) -> dict[int, list[Condition]]:
        """Each tool's conditions as of its last poll, which the database keeps."""
        return self.bridge.state.printers.setdefault(self.name, PrinterState()).conditions

    @property
    def _seen_tags(self) -> dict[int, SeenTag]:
        """What each tool's instance UUID was last seen with, which the database keeps."""
        return self.bridge.state.printers.setdefault(self.name, PrinterState()).seen_tags

    async def read(self) -> tuple[PrinterStatus, FilamentReading] | None:
        """Read the printer's status and counters, or None if this poll can't use it.

        The status carries the user's job ID: None for the bridge's own sync job.
        """
        try:
            status = await self.client.status()
            self._last_state = status.state  # Known even when the counters can't be read.
            self._poll_soon = False
            reading = await self.client.filament()
            job_id = await self._user_job_id(status)
        except Unreachable as e:
            # Connection refused, timeouts and the like: most often the printer is off.
            _log_failed(self._printer_reach, e, unreachable=True)
            self._idle_since = None
            return None
        except NoFilamentEndpoint as e:
            _log_ok(self._printer_reach)
            self._idle_since = None
            if not self._unsupported:
                self._unsupported = True
                log.error(
                    "%s: %s; this firmware isn't supported, so nothing is charged. "
                    "Checking again each poll.",
                    self.label,
                    e,
                )
            return None
        except ServiceError as e:
            _log_failed(self._printer_reach, e, unreachable=False)
            self._idle_since = None
            return None
        _log_ok(self._printer_reach)
        self._note_state(status.state)
        if self._unsupported:
            self._unsupported = False
            log.error("%s now reports filament; resuming", self.label)
        if self._model_boot != reading.boot_id:
            await self._check_model(reading.boot_id)
        return status._replace(job_id=job_id), reading

    def _note_state(self, state: str) -> None:
        """Track how long the printer has been idle, for `sync_delay`."""
        if state not in IDLE_STATES:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = self.bridge.clock()

    async def check_missing(self, spools: list[Spool]) -> None:
        """Find out why an active spool is missing from Spoolman's list.

        The list leaves out archived spools. A deleted one (404) leaves its tool with no
        active spool, holding usage for the next one. An archived one keeps being charged
        until the next unload, and is flagged. An archived spool is remembered, so it's
        looked up once, not every poll. A spool that's found but neither archived nor
        listed was unarchived after the list was read; it's charged without its filament
        data this poll, and the next poll's list has it, so nothing is remembered.
        """
        listed = {s.id for s in spools}
        missing = {
            st.active
            for st in self.tools.values()
            if st.active is not None
            and st.active not in listed
            and st.active not in self.bridge.archived
        }
        # A spool that's listed again, or unloaded, no longer needs looking up.
        for spool_id in list(self._lookup_failing):
            if spool_id not in missing:
                del self._lookup_failing[spool_id]
                log.info("%s: spool %d no longer needs looking up", self.label, spool_id)
        held = False
        for tool, st in list(self.tools.items()):
            spool_id = st.active
            if spool_id not in missing:
                continue
            location = self.config.tools.get(tool, str(tool))
            try:
                found = await self.bridge.spoolman.spool(spool_id)
            except ServiceError as e:
                if _record(self._lookup_failing, spool_id, e):
                    log.warning(
                        "[%s] can't look up spool %d, which is missing from Spoolman's list: %s",
                        location,
                        spool_id,
                        e,
                    )
                else:
                    log.debug(
                        "[%s] still can't look up spool %d, which is missing from Spoolman's "
                        "list: %s",
                        location,
                        spool_id,
                        e,
                    )
                continue
            if self._lookup_failing.pop(spool_id, None) is not None:
                log.info("[%s] can look up spool %d again", location, spool_id)
            if found is None:
                held = self._hold_deleted(tool, spool_id) or held
            elif found.archived:
                self.bridge.archived.add(found)
                log.warning(
                    "[%s] spool %d is archived but still loaded; charging it until the next unload",
                    location,
                    spool_id,
                )
        if held:
            self.bridge._save()

    def _hold_deleted(self, tool: int, spool_id: int, mm: int = 0) -> bool:
        """Hold a deleted spool's usage for the tool's next spool. Returns whether it did,
        which the caller then saves."""
        st = self.tools.get(tool)
        if st is None or spool_id not in (st.active, st.deleted_spool):
            return False
        # Charges to this spool still queued, the rejected one included, are each held
        # as they're rejected, so they're left out of any open swap's usage.
        queued = sum(
            r.extruded_mm
            for r in self.bridge.state.outbox
            if (r.printer, r.tool, r.spool_id) == (self.name, tool, spool_id) and r.extruded_mm > 0
        )
        self.tools[tool] = hold_deleted(st, spool_id, mm, queued)
        self._note_hold(tool)
        return True  # The caller saves.

    def _note_hold(self, tool: int) -> None:
        """Log when a tool's hold for a deleted spool starts, moves to another spool, or ends."""
        st = self.tools.get(tool)
        held = st.deleted_spool if st is not None else None
        logged = self._hold_logged.get(tool)
        if held == logged:
            return
        location = self.config.tools.get(tool, str(tool))
        if held is None:
            del self._hold_logged[tool]
            log.warning("[%s] no longer holding usage for a deleted spool", location)
            return
        self._hold_logged[tool] = held
        log.warning(
            "[%s] spool %d was deleted in Spoolman; holding its usage for the next spool "
            "that becomes active in %s",
            location,
            held,
            location,
        )

    async def _check_model(self, boot_id: int) -> None:
        """Look up the printer's model; if the bridge can't write to it, writes stay off."""
        try:
            printer_type = await self.client.printer_type()
        except ServiceError as e:
            # The summary, not the body: this is a flag's text too. The log has the body.
            error = f"can't read the printer model ({e.summary})"
            if error != self._model_error:
                log.warning(
                    "%s: can't read the printer model (%s); printer writes are off until it can",
                    self.label,
                    e,
                )
            self._model_error = error
            return
        self._model_boot = boot_id
        self._model = printer_sync.PRINTER_MODELS.get(printer_type or "")
        # Writes were off and said so: the line saying they're back on is its recovery.
        was_off = self._model_error is not None
        self._model_error = printer_sync.model_problem(printer_type, self.config.gcode_preamble)
        name = self._model.name if self._model else "unknown"
        if self._model_error:
            log.warning(
                "%s is %s (%s): %s; printer writes are off",
                self.label,
                name,
                printer_type,
                self._model_error,
            )
        else:
            level = logging.WARNING if was_off else logging.INFO
            log.log(level, "%s is %s (%s)", self.label, name, printer_type)

    @property
    def _tag_reader(self) -> bool:
        """Whether the model reads tags and reports their UUIDs on tool 0."""
        return self._model is not None and self._model.uuids

    def _tag_tool(self, tool: int) -> bool:
        """Whether this tool can report a tag's UUID: the tag reader's tool."""
        return self._tag_reader and tool == printer_sync.UUID_TOOL

    def _uuid_tool(self, tool: int) -> bool:
        """Whether to write and store this tool's instance UUID."""
        return self._uuids and tool == printer_sync.UUID_TOOL

    @property
    def _uuids(self) -> bool:
        """Whether to write and compare instance UUIDs: configured with a Spoolman field to
        keep them in, and the model keeps them."""
        return bool(self.bridge.config.uuid_field and self._tag_reader)

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
    ) -> Plan:
        """Account one poll and persist the result.

        Charges go to the outbox for `flush()` to send, saved in one transaction with the
        events, the new counters, and each tool's conditions. Returns what's left to do:
        the UUIDs to adopt and the tools to write.

        `claims` is this printer's share of `Bridge._claims()`, by tool.
        """
        by_id = {s.id: s for s in spools}
        now = _now()
        events: list[EventRecord] = []
        charges: list[ChargeRecord] = []
        plan = Plan()

        for tool, location in self.config.tools.items():
            tool_reading = reading.tools.get(tool)
            if tool_reading is None:
                found: list[Condition] = [ToolNotReported(tool, level=logging.WARNING)]
                if tool in self.tools:
                    found += self._state_conditions(tool, self.tools[tool], status)
                self._report(tool, location, found, by_id, compared=False)
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
            if result.resync:
                self.writes[tool] = self._write(tool).resync()
            if not result.skipped or tool in self.tools:
                self.tools[tool] = result.state
            if result.runout is not None:
                self._check_runout(location, by_id.get(result.runout))
            if result.claimed:
                self._claim_handled(location, claim, others)
            seen = self._seen_tags.get(tool, SeenTag())
            if any(isinstance(ev, Unload) for ev in result.events):
                # Only a runout in a print the bridge saw start is reported as one.
                seen = tags.unloaded(seen, runout=result.runout is not None)

            lasting: list[Condition] = []
            for ev in result.events:
                report = ev.kind.report
                if report is Report.STATE:
                    continue
                if report is Report.LASTING:
                    # Logged only when it starts, changes, and ends.
                    lasting.append(Lasting(ev, level=logging.WARNING))
                    continue
                level = logging.WARNING if report is Report.WARNING else logging.INFO
                log.log(level, "[%s] %s", location, ev.message)
                events.append(EventRecord(now, self.name, tool, location, ev))

            for c in result.charges:
                # An archived spool is still charged until the next unload, but isn't listed.
                spool = by_id.get(c.spool_id) or self.bridge.archived.get(c.spool_id)
                length = round(c.mm * self.config.length_factor, 1)
                grams = spool.mm_to_grams(length) if spool else None
                log.info(
                    "[%s] %s spool %d (%s): %+g mm%s (%s)",
                    location,
                    "CHARGE",
                    c.spool_id,
                    spool.name if spool else "unknown",
                    length,
                    f" ≈ {grams:+.2f} g" if grams is not None else "",
                    c.reason,
                )
                charges.append(
                    ChargeRecord(
                        time=now,
                        printer=self.name,
                        tool=tool,
                        location=location,
                        spool_id=c.spool_id,
                        extruded_mm=c.mm,
                        use_length=length,
                        grams=None if grams is None else round(grams, 3),
                        reason=c.reason,
                        boot_id=reading.boot_id,
                        extruded=tool_reading.extruded,
                    )
                )

            st = self.tools.get(tool, ToolState())
            # Also logs a hold restored from the database after a restart, since
            # nothing is logged for it yet.
            self._note_hold(tool)
            found = self._state_conditions(tool, st, status) + lasting
            active = by_id.get(st.active) if st.active is not None else None
            if active is not None and active.id != before.active:
                self.bridge.state.runouts.pop(active.id, None)
            decision = Decision.NONE
            comparable = not result.skipped and material is not None and active is not None
            if tool == printer_sync.UUID_TOOL and (material is None or not material.instance_uuid):
                seen = tags.without_uuid(seen)
            elif comparable and self._tag_tool(tool):
                seen, decision = tags.decide(
                    seen,
                    material,
                    active,
                    certain=st.assigned == st.active and st.swap is None and st.provisional is None,
                    spools=spools,
                    uuids=self._uuids,
                    refused=self.bridge._refused_uuids,
                    written_uuid=written.uuid if (written := self._write(tool).written) else None,
                )
            if seen != SeenTag():
                self._seen_tags[tool] = seen
            if decision is Decision.CONFLICT:
                found.append(
                    UuidConflict(
                        active.id,
                        material.instance_uuid,
                        active.instance_uuid,
                        level=logging.WARNING,
                    )
                )
                self._no_write(tool)
            elif comparable:
                if decision is not Decision.NONE:
                    # Compare as if Spoolman already had the printer's UUID: adopting it
                    # makes the tool match, and a write keeps it either way.
                    shown = material.instance_uuid.lower()
                    plan.adoptions[tool] = _Adoption(active.id, shown, decision is Decision.ADOPT)
                    active = replace(active, instance_uuid=shown)
                tagged = self._tag_tool(tool) and tags.loaded_tag(seen, material, active)
                if tagged:
                    # Never written, so only whose tag it is matters (Q11).
                    diffs, edited = (
                        drift.tag_differences(material, active, spools, self._uuids),
                        None,
                    )
                elif printer_sync.loaded_from_menu(material.name, active):
                    diffs, edited = [], None  # As the "can't write" flag asked.
                else:
                    diffs = drift.differences(tool, material, active, self._uuids)
                    # With the location emptied, the spool is leaving and won't be
                    # rewritten, so only a real printer difference is worth reporting.
                    edited = (
                        self._edited(tool, material, active) if st.assigned is not None else None
                    )
                if tagged:
                    self._no_write(tool)
                    if diffs:
                        found.append(Drift(active.id, tuple(diffs), level=logging.WARNING))
                elif not diffs and not edited:
                    self._no_write(tool)
                else:
                    mismatch, write = self._classify_mismatch(
                        tool,
                        st,
                        active,
                        (*diffs, *([edited] if edited else [])),
                        edited=edited is not None,
                    )
                    if mismatch is not None:
                        found.append(mismatch)
                    if write:
                        plan.writes.append(tool)
            self._report(tool, location, found, by_id, compared=not result.skipped)

        for rec in charges:
            self.bridge.state.outbox.push(rec)
        self.bridge._save(events)
        return plan

    def _state_conditions(self, tool: int, st: ToolState, status: PrinterStatus) -> list[Condition]:
        """Return the conditions a tool's saved state is in."""
        found: list[Condition] = []
        if st.deleted_spool is not None and st.assigned is not None:
            found.append(HeldForDeleted(st.assigned, st.deleted_spool))
        if st.active in self.bridge.archived:
            found.append(ArchivedLoaded(st.active))
        if st.swap is not None and st.active is None:
            found.append(UnloadedMovedOut(st.swap.spool_id))
        elif st.swap is not None:
            found.append(UnloadUnconfirmed(st.swap.spool_id))
        if st.notice is not None:
            found.append(LoadedWhileStaged(st.notice))
        if st.staged_notice is not None:
            found.append(StagedNotLoaded(st.staged_notice))
        if st.provisional is not None:
            p = st.provisional
            found.append(
                conditions.provisional(
                    p.spool_id,
                    p.mm,
                    p.job_id,
                    self.conditions.get(tool, ()),
                    printing=status.job_id is not None,
                )
            )
        return found

    def _context(self, location: str, by_id: Mapping[int, Spool]) -> conditions.Context:
        return conditions.Context(
            location,
            by_id,
            length_factor=self.config.length_factor,
            claim_field=bool(self.bridge.config.claim_field),
            flag_drift=self.config.flag_drift,
        )

    def _report(
        self,
        tool: int,
        location: str,
        found: list[Condition],
        by_id: Mapping[int, Spool],
        *,
        compared: bool,
    ) -> None:
        """Keep the conditions a poll found for a tool, and log what changed.

        Without `compared`, the poll didn't compare the printer with the active spool,
        so the conditions about that stand as they were.
        """
        standing = conditions.standing(self.conditions.get(tool, ()), found, compared=compared)
        self._logged[tool] = conditions.log_changes(
            self._logged.get(tool, {}), standing, self._context(location, by_id)
        )
        if standing:
            self.conditions[tool] = standing
        else:
            self.conditions.pop(tool, None)

    def flags(self, by_id: Mapping[int, Spool]) -> dict[int, list[str]]:
        """Return the flag texts of the tools' conditions, by spool.

        They're the conditions as of the last poll that read the printer, so an outage
        doesn't clear its flags in Spoolman.
        """
        found: dict[int, list[str]] = {}
        for tool, location in self.config.tools.items():
            ctx = self._context(location, by_id)
            for spool_id, texts in conditions.flags(self.conditions.get(tool, ()), ctx).items():
                found.setdefault(spool_id, []).extend(texts)
        return found

    def _write(self, tool: int) -> printer_sync.WriteState:
        return self.writes.get(tool, printer_sync.WriteState())

    def _no_write(self, tool: int) -> None:
        """Drop any write still expected for a tool, so none fires later."""
        write = self._write(tool)
        if write.due or write.attempts:
            self.writes[tool] = write.settled()

    async def adopt_uuids(self, plan: Plan) -> Plan:
        """Store the UUIDs of tags `process()` found on spools that have none.

        Returns the plan that's left: the adoptions that worked, and the writes that
        don't wait on one that didn't.

        Adopted UUIDs are tags', and a tool loaded from a tag is never written (Q11). A
        kept UUID is checked against archived spools only: one an archived spool turns
        out to have is dropped, so a write this poll gives the spool its own UUID. On
        any other failure the tool's write waits for the next poll.
        """
        left = Plan(list(plan.writes))
        for tool, adopt in plan.adoptions.items():
            if await self.bridge._adopt_uuid(adopt.spool_id, adopt.uuid, store=adopt.store):
                left.adoptions[tool] = adopt
            elif (adopt.spool_id, adopt.uuid) not in self.bridge._refused_uuids:
                left.writes = [t for t in left.writes if t != tool]
        return left

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
        log.warning(
            "[%s] spool %d unloaded mid-print with %.0f g left in Spoolman",
            location,
            spool.id,
            spool.remaining_weight,
        )
        self.bridge.state.runouts[spool.id] = Runout(
            location, spool.remaining_weight, spool.net_weight, spool.used_weight
        )

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
        # _claims() leaves spools already in `claimed` out of the candidates.
        for spool_id in [claim, *others]:
            self.bridge.state.claimed.append(spool_id)

    def _edited(self, tool: int, material, spool: Spool) -> drift.Difference | None:
        """Return why the printer should get Spoolman's edit of `spool`, or None.

        That's when the printer still shows the bridge's last write, but the write the
        bridge would make now differs: someone changed the spool in Spoolman, not the
        printer.
        """
        written = self._write(tool).written
        if written is None:
            return None
        try:
            want = self._filament(tool, spool, spool.instance_uuid)
        except printer_sync.CannotSync:
            # The edit can't be written, but it's still an edit, not drift:
            # _classify_mismatch flags why.
            want = None
        if want == written:
            return None
        if not printer_sync.printer_matches_write(material, written, self._uuids):
            return None
        return drift.Difference(drift.What.EDIT)

    def _filament(
        self, tool: int, spool: Spool, uuid_value: str | None
    ) -> printer_sync.WrittenFilament:
        """Return what writing `spool` to `tool` would set, or raise CannotSync."""
        uuid_value = uuid_value if self._uuid_tool(tool) else None
        return printer_sync.filament_for(printer_sync.ToolWrite(tool, spool, uuid_value))

    def _cannot_write(self, tool: int, spool: Spool) -> str | None:
        """Return why `spool` can't be written to the printer, or None if it can."""
        try:
            self._filament(tool, spool, spool.instance_uuid)
        except printer_sync.CannotSync as e:
            return str(e)
        return None

    def _classify_mismatch(
        self,
        tool: int,
        st: ToolState,
        active: Spool,
        diffs: tuple[drift.Difference, ...],
        edited: bool = False,
    ) -> tuple[Condition | None, bool]:
        """Decide whether a printer/spool mismatch is a pending write or drift.

        Returns the condition to report, if any, and whether to write the tool.
        """
        # With the location emptied, the active spool is still loaded but is leaving:
        # don't write it, report the mismatch as drift.
        write = self._write(tool)
        expected = st.assigned is not None and (write.due or self.config.sync_drift or edited)
        # Wait while a swap is staged: the active spool is about to change. During an
        # unconfirmed swap, write the assigned spool: the printer then shows what Spoolman
        # thinks is loaded, and confirming a different spool rewrites it.
        settled = st.assigned == st.active
        if self.config.printer_sync and expected and write.attempts < MAX_SYNC_ATTEMPTS:
            if settled and self._model_error:
                return PrinterUnwritable(active.id, self._model_error, level=logging.WARNING), False
            if settled and (problem := self._cannot_write(tool, active)):
                # Found here, not in sync_printer, so it's logged once rather than
                # alternating with the "will be set" line on every idle poll.
                return SpoolUnwritable(active.id, problem, level=logging.WARNING), False
            if not settled:
                return WriteWaitsForLoad(active.id, st.assigned, level=logging.WARNING), False
            # After a sync job, the printer can take a poll to show it. Don't warn
            # about that wait: a retry logs its job, and giving up logs below.
            if not write.attempts or edited:
                return WritePending(active.id, diffs, level=logging.WARNING), True
            return None, True
        if self.config.printer_sync and expected:
            return (
                WriteDidntTake(active.id, write.attempts, diffs, level=logging.WARNING),
                False,
            )
        return Drift(active.id, diffs, level=logging.WARNING), False

    async def sync_printer(self, spools: list[Spool], plan: Plan) -> None:
        """Write the plan's tools to the printer in one job."""
        if self._model_error:
            return
        by_id = {s.id: s for s in spools}
        writes: list[printer_sync.ToolWrite] = []
        minting = False  # A tool's write waits for its spool's UUID to be stored.
        for tool in plan.writes:
            st = self.tools[tool]
            spool = by_id.get(st.active)
            if spool is None:
                continue
            uuid_value = None
            if self._uuid_tool(tool):
                adopted = plan.adoptions.get(tool)
                if spool.instance_uuid:
                    uuid_value = spool.instance_uuid
                elif adopted is not None and adopted.spool_id == spool.id:
                    uuid_value = adopted.uuid
                else:
                    uuid_value = await self.bridge._mint_uuid(spool)
                    if uuid_value is None and self.bridge.config.uuid_field not in (
                        self.bridge._unusable_fields
                    ):
                        # The spool gets its UUID first (W1): written without one, the
                        # printer would match the write and no later poll would mint
                        # again. The write stays due, and the next poll retries the mint.
                        # Only a UUID field that's unusable until someone fixes it doesn't
                        # hold the write: the spool is written without a UUID.
                        minting = True
                        continue
            writes.append(printer_sync.ToolWrite(tool, spool, uuid_value))

        if not writes and minting:
            return  # The job is still needed: leave its waits and failures as they are.
        if not writes:
            # A wait for the delay ends silently. A job that was failing or waiting on a
            # busy printer says it's no longer needed.
            if self._sync_failed is not None:
                log.warning("%s sync job no longer needed", self.label)
            elif self._sync_busy is not None:
                log.info("%s sync job no longer needed", self.label)
            self._sync_delaying = False
            self._sync_busy = self._sync_failed = None
            return
        delay = self.config.sync_delay
        idle_for = 0.0 if self._idle_since is None else self.bridge.clock() - self._idle_since
        if idle_for < delay:
            # The printer may have only just become idle, with someone at it about to
            # start a print. Give them time before taking it over.
            if not self._sync_delaying:
                log.info(
                    "%s sync job waits until the printer has been idle for %gs",
                    self.label,
                    delay,
                )
            self._sync_delaying = True
            return
        self._sync_delaying = False
        gcode = printer_sync.build_job(self._preamble(), writes)
        try:
            # The state this poll acted on was read a few requests ago. Check it again
            # right before taking over the printer, in case a print started since.
            state = (await self.client.status()).state
            self._note_state(state)
            if state not in IDLE_STATES:
                if self._sync_busy != state:
                    log.info(
                        "%s became %s; sync job waits for the next idle poll", self.label, state
                    )
                self._sync_busy = state
                return
            await self.client.upload_and_print(
                self.config.sync_storage, self.config.sync_file, gcode
            )
        except Busy:
            if self._sync_busy != "409":
                log.info("%s busy; sync job waits for the next idle poll", self.label)
            self._sync_busy = "409"
            return
        except ServiceError as e:
            if self._sync_failed != e.summary:
                log.warning("%s sync job failed: %s", self.label, e)
            self._sync_failed = e.summary
            return
        if self._sync_failed is not None:
            log.warning("%s sync job working again", self.label)
        # A wait on a busy printer ends with the job; the line below says it ran.
        self._sync_busy = self._sync_failed = None
        log.info("ran %s job %s:\n%s", self.label, self.config.sync_file, gcode.rstrip())
        for w in writes:
            self.writes[w.tool] = self._write(w.tool).wrote(printer_sync.filament_for(w))
        self._poll_soon = True
        self.bridge._save()


@dataclass
class Bridge:
    """The poll loop and everything keyed by spool: charges, flags, claims, and evictions."""

    config: Config
    spoolman: Spoolman
    clients: dict[str, PrusaLink]  # By printer name.
    # Where the state and the ledger are saved. Closed by `aclose`.
    db: Database
    state: State = field(default_factory=State)
    printers: list[PrinterBridge] = field(init=False, default_factory=list)
    _ready_fields: set[str] = field(default_factory=set)
    # Field keys whose last setup failure was `Rejected`: it needs someone to fix it.
    _unusable_fields: set[str] = field(default_factory=set)
    _spoolman_reach: Reachability = field(init=False)
    # What's failing now, by spool, with the last error logged for each.
    _unclaim_failing: dict[int, str] = field(default_factory=dict)
    _evict_failing: dict[int, str] = field(default_factory=dict)
    _uuid_failing: dict[int, str] = field(default_factory=dict)
    _flag_failing: dict[int, str] = field(default_factory=dict)
    _field_failing: dict[str, str] = field(default_factory=dict)
    # The status or error code of a charge that's stuck in the outbox, if one is.
    _charge_failing: str | None = None
    # Active or flagged spools missing from Spoolman's list that turned out to be archived.
    archived: ArchivedSpools = field(default_factory=ArchivedSpools)
    # Claimed spools outside every tool's location, with the location each was last
    # logged in.
    _unplaced: dict[int, str | None] = field(default_factory=dict)
    # Spools active in two tools last poll, with the tool locations each was logged in.
    _shared: dict[int, list[str]] = field(default_factory=dict)
    # Loaded spools that showed a negative remaining weight last poll.
    _negative: set[int] = field(default_factory=set)
    # Tag UUIDs not adopted because an archived spool has them, so the archived spools
    # aren't read again every poll.
    _refused_uuids: set[tuple[int, str]] = field(default_factory=set)
    # UUIDs kept on the printer for a spool that no archived spool has, checked once.
    _unowned_uuids: set[tuple[int, str]] = field(default_factory=set)
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        # Read through `self`, so a clock set later reaches it too.
        self._spoolman_reach = Reachability("Spoolman", lambda: self.clock())
        several = len(self.config.printers) > 1
        self.printers = [
            PrinterBridge(
                p, self.clients[p.name], self, label=f"printer {p.name}" if several else "printer"
            )
            for p in self.config.printers
        ]
        for name in sorted(self.state.printers.keys() - {p.name for p in self.printers}):
            # Kept, not dropped: it can hold usage for a deleted spool or spools to evict.
            log.warning(
                "the database has a printer %r that isn't configured; keeping its state",
                name,
            )

    def _printer(self, name: str) -> PrinterBridge | None:
        return next((p for p in self.printers if p.name == name), None)

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        clock: Callable[[], float] = time.monotonic,
        spoolman_transport: httpx.AsyncBaseTransport | None = None,
        printer_transports: Mapping[str, httpx.AsyncBaseTransport] | None = None,
    ) -> Bridge:
        """A bridge on the configured printers, Spoolman and database.

        It locks the database until `aclose`, and creates it if it's missing. `clock`
        times the idle wait and every outage. The transports, for Spoolman and by
        printer name, replace the network; the clients close them.
        """
        transports = printer_transports or {}
        db = Database.open(config.db_file)
        return cls(
            config=config,
            clients={
                p.name: PrusaLink(p.url, p.api_key, transport=transports.get(p.name))
                for p in config.printers
            },
            spoolman=Spoolman(
                config.spoolman_url,
                uuid_field=config.uuid_field,
                flag_field=config.flag_field,
                claim_field=config.claim_field,
                transport=spoolman_transport,
            ),
            state=db.load(),
            db=db,
            clock=clock,
        )

    def _save(self, ledger: Sequence[ChargeRecord | EventRecord] = ()) -> None:
        """Save the state and append the `ledger` records, in one transaction."""
        self.db.save(self.state, ledger)

    async def aclose(self) -> None:
        try:
            for client in self.clients.values():
                await client.aclose()
            await self.spoolman.aclose()
        finally:
            self.db.close()

    async def poll(self) -> None:
        """Poll every printer once, then reconcile Spoolman.

        A printer that can't be read is skipped for this poll, and the others carry on.
        When no printer answers, nothing is read from Spoolman either.
        """
        # Send charges left over from a failed poll or a restart before reading anything.
        if not await self.flush():
            for p in self.printers:
                p._idle_since = None  # The printer wasn't read; restart the idle wait.
            return
        reads: dict[str, tuple[PrinterStatus, FilamentReading]] = {}
        for printer in self.printers:
            read = await printer.read()
            if read is not None:
                reads[printer.name] = read
        if not reads:
            return
        try:
            spools = await self.spoolman.spools()
        except Unreachable as e:
            _log_failed(self._spoolman_reach, e, unreachable=True)
            return
        except ServiceError as e:
            _log_failed(self._spoolman_reach, e, unreachable=False)
            return
        _log_ok(self._spoolman_reach)
        claims, flags = self._claims(spools)
        by_id = {s.id: s for s in spools}
        self.archived.prune(
            set(by_id),
            {st.active for p in self.printers for st in p.tools.values()} | set(self.state.flagged),
        )
        plans: dict[str, Plan] = {}
        for printer in self.printers:
            if printer.name in reads:
                status, reading = reads[printer.name]
                await printer.check_missing(spools)
                plans[printer.name] = printer.process(
                    reading, spools, status, claims.get(printer.name, {})
                )
            # An unread printer's conditions stand until it answers again.
            _add_flags(flags, printer.flags(by_id))
        _add_flags(flags, self._check_shared())
        self._check_negative(spools)
        _add_flags(flags, self._runout_flags(spools))
        await self.flush()
        for printer in self.printers:
            if printer.name in plans:
                plans[printer.name] = await printer.adopt_uuids(plans[printer.name])
        await self.evict(spools)
        if self.config.claim_field:
            await self.sync_claims()
        for printer in self.printers:
            if printer.name in plans and reads[printer.name][0].state in IDLE_STATES:
                await printer.sync_printer(spools, plans[printer.name])
        if self.config.flag_field:
            await self.sync_flags(
                spools, {spool_id: " | ".join(texts) for spool_id, texts in flags.items()}
            )

    def _check_negative(self, spools: list[Spool]) -> None:
        """Log a loaded spool whose remaining weight in Spoolman goes below 0, and its recovery.

        A spool that leaves the set because it's unloaded isn't logged: the unload is.
        """
        loaded = {st.active for p in self.printers for st in p.tools.values()}
        negative = {
            s.id: s.remaining_weight
            for s in spools
            if s.id in loaded and s.remaining_weight is not None and s.remaining_weight < 0
        }
        for spool_id in sorted(negative.keys() - self._negative):
            log.info(
                "spool %d shows %.0f g remaining in Spoolman but is still "
                "loaded; its weight is probably off",
                spool_id,
                negative[spool_id],
            )
        listed = {s.id for s in spools}
        for spool_id in sorted(self._negative - negative.keys()):
            if spool_id in loaded and spool_id in listed:
                log.info(
                    "spool %d no longer shows a negative weight remaining in Spoolman", spool_id
                )
        # A loaded spool the list misses (archived, say) stays, so it logs no second start.
        kept = {i for i in self._negative if i in loaded and i not in listed}
        self._negative = set(negative) | kept

    def _check_shared(self) -> Flags:
        """Return a flag for each spool that's active in more than one tool at once.

        It happens when a tool misses an unload, which a reboot can cause, and the spool is
        then loaded elsewhere. Each tool keeps charging it until its own unload or a claim.
        """
        flags: Flags = {}
        where: dict[int, list[str]] = {}
        for printer in self.printers:
            for tool, st in printer.tools.items():
                if st.active is not None:
                    where.setdefault(st.active, []).append(
                        printer.config.tools.get(tool, str(tool))
                    )
        shared = {spool_id for spool_id, locations in where.items() if len(locations) > 1}
        for spool_id in sorted(self._shared.keys() - shared):
            log.warning("spool %d is no longer active in more than one tool", spool_id)
        previous, self._shared = self._shared, {i: where[i] for i in shared}
        for spool_id in sorted(shared):
            locations = where[spool_id]
            names = " and ".join(locations)
            flags.setdefault(spool_id, []).append(
                f"active in {names} at once. One of them missed an unload: unload and reload "
                "there, or claim the spool that's loaded."
            )
            if previous.get(spool_id) != locations:
                log.warning(
                    "spool %d is active in %s at once; one of them missed an unload",
                    spool_id,
                    names,
                )
        return flags

    def _claims(
        self, spools: list[Spool]
    ) -> tuple[dict[str, dict[int, tuple[int, list[int]]]], Flags]:
        """Return each tool's claim to handle this poll, and the other claims for it.

        Keyed by printer name, then tool index. Also returns the flags for claims that
        can't be handled.

        A claim is handled once: the bridge then clears the box, and until Spoolman
        shows it clear, the spool stays in `State.claimed`. A claimed spool must be in a
        tool's location, which says which tool it's in. Spoolman doesn't record when a
        box was ticked, so of several claims for one tool the most recently used spool
        wins, with never-used spools oldest and ties going to the highest ID.
        """
        flags: Flags = {}
        if not self.config.claim_field:
            return {}, flags
        ticked = {s.id: s for s in spools if s.claim}
        handled = [i for i in self.state.claimed if i in ticked]
        if handled != self.state.claimed:
            for spool_id in set(self.state.claimed) - set(handled):
                if self._unclaim_failing.pop(spool_id, None) is not None:
                    log.info("spool %d no longer needs its claim cleared", spool_id)
            self.state.claimed = handled  # Boxes now clear in Spoolman.
            self._save()
        tools = {
            location: (printer.name, tool)
            for printer in self.printers
            for tool, location in printer.config.tools.items()
        }
        per_tool: dict[tuple[str, int], list[Spool]] = {}
        unplaced: dict[int, str | None] = {}
        for spool in ticked.values():
            if spool.id in handled:
                continue
            tool = tools.get(spool.location or "")
            if tool is None:
                unplaced[spool.id] = spool.location
                flags.setdefault(spool.id, []).append(
                    f"'{CLAIM_FIELD_NAME}' is ticked, but this spool isn't in a printer "
                    "tool's location. Move it into the location of the tool it's loaded in."
                )
                if spool.id not in self._unplaced or self._unplaced[spool.id] != spool.location:
                    log.warning(
                        "spool %d is claimed but isn't in a tool's location (it's in %s); "
                        "waiting until it is",
                        spool.id,
                        repr(spool.location or "no location"),
                    )
                continue
            per_tool.setdefault(tool, []).append(spool)
        for spool_id in sorted(self._unplaced.keys() - unplaced.keys()):
            log.warning("claimed spool %d is no longer waiting on a location", spool_id)
        self._unplaced = unplaced
        chosen: dict[str, dict[int, tuple[int, list[int]]]] = {}
        for (name, tool), candidates in per_tool.items():
            candidates.sort(key=lambda s: (s.last_used or _NEVER, s.id), reverse=True)
            winner, *others = candidates
            chosen.setdefault(name, {})[tool] = (winner.id, [s.id for s in others])
        return chosen, flags

    def _runout_flags(self, spools: list[Spool]) -> Flags:
        """Return the runout flags that stand, dropping the ones a weight edit answers."""
        if self._check_runout_edits({s.id: s for s in spools}):
            self._save()
        return {spool_id: [runout.flag] for spool_id, runout in self.state.runouts.items()}

    def _follow_runout_charge(self, rec: ChargeRecord, used: float | None) -> None:
        """Move a runout flag's used weight on by a charge Spoolman accepted.

        Only when the response is the stored weight plus this charge: anything else
        includes a change made in Spoolman since the last read, and leaving the stored
        weight alone lets the next read see it as an edit. When the charge's weight
        isn't known, the response is taken as the bridge's own charge.
        """
        runout = self.state.runouts.get(rec.spool_id)
        if runout is None or used is None:
            return
        grams = rec.grams
        if grams is None:
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

    async def sync_claims(self) -> None:
        """Clear the claim box on spools whose claim has been handled.

        Every spool in `State.claimed` is ticked in this poll's list: `_claims()` drops
        the rest, and claims handled since were ticked.
        """
        key = self.config.claim_field
        assert key
        if not await self._ensure_field(key, CLAIM_FIELD_NAME, "boolean"):
            return
        changed = False
        for spool_id in list(self.state.claimed):
            try:
                await self.spoolman.set_extra(spool_id, key, False)
            except ServiceError as e:
                if _record(self._unclaim_failing, spool_id, e):
                    log.warning("can't clear the claim on spool %d: %s", spool_id, e)
                else:
                    log.debug("still can't clear the claim on spool %d: %s", spool_id, e)
                continue
            self._unclaim_failing.pop(spool_id, None)
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
        next spool. Any other 4xx except a timeout (408) or a rate limit (429) drops the
        charge with an error so it can't block the rest.
        """
        outbox = self.state.outbox
        sent = held = dropped = 0
        while outbox:
            rec = outbox.front()
            try:
                used = await self.spoolman.use_length(rec.spool_id, rec.use_length)
                answered = replace(rec, applied=_now())
                self._follow_runout_charge(answered, used)
                sent += 1
            except Rejected as e:
                printer = self._printer(rec.printer)
                if (
                    isinstance(e, NotFound)
                    and rec.extruded_mm > 0
                    and printer is not None
                    and printer._hold_deleted(rec.tool, rec.spool_id, rec.extruded_mm)
                ):
                    answered = replace(rec, failed=f"{e.status}", held=True)
                    held += 1
                else:
                    log.error(
                        "Spoolman rejected a %+g mm charge to spool %d (%s); dropping it",
                        rec.use_length,
                        rec.spool_id,
                        e,
                    )
                    answered = replace(rec, failed=f"{e.status}")
                    dropped += 1
            except ServiceError as e:
                self._charge_stuck(rec.spool_id, e, len(outbox))
                return False
            outbox.pop()
            self._save([answered])  # One transaction, so the record can't be lost.
        if self._charge_failing is not None:
            self._charge_failing = None
            log.warning("%s", _outbox_outcome(sent, held, dropped))
        return True

    def _charge_stuck(self, spool_id: int, error: ServiceError, queued: int) -> None:
        """Log a charge that's stuck, once per distinct error."""
        detail = error.summary
        if self._charge_failing != detail:
            log.warning(
                "charge to spool %d failed (%s); %d queued, retrying", spool_id, error, queued
            )
        else:
            log.debug(
                "charge to spool %d still failing (%s); %d queued, retrying",
                spool_id,
                error,
                queued,
            )
        self._charge_failing = detail

    def _evict_gone(self, location: str | None, spool_id: int) -> None:
        """End a failing eviction of a spool that's left the location some other way."""
        if self._evict_failing.pop(spool_id, None) is not None:
            log.info("[%s] spool %d is out of the location now", location, spool_id)

    async def evict(self, spools: list[Spool]) -> None:
        """Move each tool's outgoing spools out of the tool's location."""
        # An eviction dropped elsewhere, such as when the spool was moved out by hand,
        # ends any problem moving it.
        pending = {i for p in self.printers for st in p.tools.values() for i in st.evict}
        for spool_id in list(self._evict_failing):
            if spool_id not in pending:
                del self._evict_failing[spool_id]
                log.info("spool %d no longer needs moving out", spool_id)
        where = {s.id: s.location for s in spools}
        target = self.config.eviction_location or None
        for printer in self.printers:
            for tool, st in list(printer.tools.items()):
                location = printer.config.tools.get(tool)
                for spool_id in st.evict:
                    if where.get(spool_id) != location:
                        self._evict_gone(location, spool_id)
                        continue
                    try:
                        # Read it again first: the list is from the start of the poll, and
                        # the user may have moved the spool since, such as into another
                        # printer's location. Moving it then would undo that.
                        fresh = await self.spoolman.spool(spool_id)
                        if fresh is None or fresh.location != location:
                            self._evict_gone(location, spool_id)
                            continue
                        await self.spoolman.set_location(spool_id, target)
                    except ServiceError as e:
                        if _record(self._evict_failing, spool_id, e):
                            log.warning("[%s] can't move spool %d out: %s", location, spool_id, e)
                        else:
                            log.debug(
                                "[%s] still can't move spool %d out: %s", location, spool_id, e
                            )
                        continue
                    self._evict_failing.pop(spool_id, None)
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
        value = str(uuid.uuid4())
        if not await self._ensure_field(key, UUID_FIELD_NAME):
            return None
        try:
            await self.spoolman.set_extra(spool.id, key, value)
        except ServiceError as e:
            self._uuid_trouble(spool.id, e, "can't store an instance UUID on spool %d: %s")
            return None
        self._uuid_failing.pop(spool.id, None)
        log.info("gave spool %d instance UUID %s", spool.id, value)
        return value

    async def _adopt_uuid(self, spool_id: int, value: str, store: bool = True) -> bool:
        """Store a tag's instance UUID on a spool that has none. Returns whether it did.

        Spoolman's list leaves out archived spools, so they're checked here: a runout
        reload can show an archived spool's tag. A spool that has the UUID already keeps
        it, and this one doesn't take it. With `store` False, only that check is made,
        once, for a UUID kept on the printer without storing it.
        """
        key = self.config.uuid_field
        assert key
        if not store and (spool_id, value) in self._unowned_uuids:
            return True
        try:
            every = await self.spoolman.spools(archived=True)
        except ServiceError as e:
            self._uuid_trouble(spool_id, e, "can't check spool %d's tag UUID: %s")
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
            self._refused_uuids.add((spool_id, value))
            # Logged once: `tags.decide` doesn't propose a refused pair again.
            self._uuid_failing.pop(spool_id, None)
            log.info(
                "spool %d can't take instance UUID %s from the printer: archived spool %d has it",
                spool_id,
                value,
                owner,
            )
            return False
        if not store:
            self._unowned_uuids.add((spool_id, value))
            if self._uuid_failing.pop(spool_id, None) is not None:
                log.info("checked spool %d's tag UUID %s: no other spool has it", spool_id, value)
            return True
        if not await self._ensure_field(key, UUID_FIELD_NAME):
            return False
        try:
            await self.spoolman.set_extra(spool_id, key, value)
        except ServiceError as e:
            self._uuid_trouble(spool_id, e, "can't store an instance UUID on spool %d: %s")
            return False
        self._uuid_failing.pop(spool_id, None)
        log.info("gave spool %d its tag's instance UUID %s", spool_id, value)
        return True

    def _uuid_trouble(self, spool_id: int, error: Exception, message: str) -> None:
        """Log a failed UUID store or check at WARNING, or at DEBUG if it repeats."""
        if _record(self._uuid_failing, spool_id, error):
            log.warning(message, spool_id, error)
        else:
            log.debug(message, spool_id, error)

    async def _ensure_field(self, key: str, name: str, field_type: str = "text") -> bool:
        if key in self._ready_fields:
            return True
        try:
            await self.spoolman.ensure_field(key, name, field_type)
        except ServiceError as e:
            # A refusal, such as a key of another type, a proxy refusing the fields API or
            # a Spoolman without it, lasts until someone fixes it. Anything else may pass.
            if isinstance(e, Rejected):
                self._unusable_fields.add(key)
            else:
                self._unusable_fields.discard(key)  # Passing, whatever failed before.
            if _record(self._field_failing, key, e):
                log.warning("can't set up the %r spool field: %s", key, e)
            else:
                log.debug("can't set up the %r spool field: %s", key, e)
            return False
        if self._field_failing.pop(key, None) is not None:
            log.info("%r spool field is set up", key)
        self._unusable_fields.discard(key)
        self._ready_fields.add(key)
        return True

    def _flag_spool_deleted(self, spool_id: int) -> bool:
        """Forget a deleted spool's flag, and end a failing update on it.

        Returns whether `State.flagged` changed.
        """
        if self._flag_failing.pop(spool_id, None) is not None:
            log.info("spool %d was deleted; its flag no longer needs updating", spool_id)
        if spool_id not in self.state.flagged:
            return False
        self.state.flagged.remove(spool_id)
        return True

    async def sync_flags(self, spools: list[Spool], flags: Mapping[int, str]) -> None:
        """Make the flag field match `flags`, each spool's text, on spools the bridge manages.

        The field is the bridge's own: on a spool it flags, whatever the field held is
        replaced, and clearing the flag empties it. On spools it has no flag for and
        didn't flag itself (`State.flagged`), the field is left as it is.
        """
        key = self.config.flag_field
        assert key
        if not await self._ensure_field(key, FLAG_FIELD_NAME):
            return
        current = {s.id: s.flag for s in [*spools, *self.archived]}
        changed = False
        for spool_id in sorted(set(flags) | set(self.state.flagged)):
            want = flags.get(spool_id)
            if spool_id not in current and spool_id in self.state.flagged:
                # A spool the bridge flagged left the list. An archived one is looked up
                # once and kept, like an archived active spool, so its flag is kept current
                # and a stale one is cleared.
                try:
                    found = await self.spoolman.spool(spool_id)
                except ServiceError as e:
                    if _record(self._flag_failing, spool_id, e):
                        log.warning("can't look up flagged spool %d: %s", spool_id, e)
                    else:
                        log.debug("can't look up flagged spool %d: %s", spool_id, e)
                    continue
                if found is None:
                    changed |= self._flag_spool_deleted(spool_id)
                    continue
                if found.archived:
                    self.archived.add(found)
                # Otherwise it was unarchived since the list was read; next poll lists it.
                current[spool_id] = found.flag
            if spool_id not in current:
                continue  # Not listed, and the bridge never flagged it: leave it alone.
            if current[spool_id] != want:
                try:
                    await self.spoolman.set_extra(spool_id, key, want)
                except NotFound:
                    # Deleted since it was listed or cached. An active spool that was
                    # cached as archived is looked up again, as deleted.
                    self.archived.drop(spool_id)
                    changed |= self._flag_spool_deleted(spool_id)
                    continue
                except ServiceError as e:
                    if _record(self._flag_failing, spool_id, e):
                        log.warning("can't update the flag on spool %d: %s", spool_id, e)
                    else:
                        log.debug("can't update the flag on spool %d: %s", spool_id, e)
                    continue
                self.archived.set_flag(spool_id, want)
                self._flag_failing.pop(spool_id, None)
                log.info(
                    "%s flag on spool %d%s",
                    "set" if want else "cleared",
                    spool_id,
                    f": {want}" if want else "",
                )
            else:
                # Someone else made it match, so a failing update is no longer needed.
                if self._flag_failing.pop(spool_id, None) is not None:
                    log.info("flag on spool %d is up to date", spool_id)
            if want is None:
                # Only a spool the bridge flagged earlier has no desired flag.
                self.state.flagged.remove(spool_id)
                changed = True
            elif spool_id not in self.state.flagged:
                self.state.flagged.append(spool_id)
                changed = True
        if changed:
            self._save()

    def interval(self) -> float:
        """Poll at the printing rate while any reachable printer is busy or not yet read."""
        up = [p for p in self.printers if not p._printer_reach.down]
        if any(p.busy for p in up):
            return self.config.poll_printing
        return self.config.poll_idle

    async def run(self, stop: asyncio.Event) -> None:
        log.info(
            "starting: %s spoolman=%s %s",
            " ".join(f"{p.label}={p.config.url}" for p in self.printers),
            self.config.spoolman_url,
            " ".join(
                f"tools={p.config.tools}"
                if len(self.printers) == 1
                else f"{p.label} tools={p.config.tools}"
                for p in self.printers
            ),
        )
        if self.state.outbox:
            log.info("%d charge(s) queued from before the restart", len(self.state.outbox))
        while not stop.is_set():
            await self.poll()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval())
