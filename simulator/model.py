"""The simulated printers: tools, counters, jobs, and the timed processes that change them.

The model has no clock. `Simulation.tick(dt)` is the only place time passes, so tests drive
it directly and the server calls it with scaled real time.
"""

from __future__ import annotations

import math
import random
import re
import sys
import uuid as uuidlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from simulator import gcode, sync
from simulator.compatibility import (
    CORE_ONE_CHECKS,
    INDX_CHECKS,
    IX_CHECKS,
    MK3_5_CHECKS,
    MK4_CHECKS,
    PLAIN_CHECKS,
    XL_CHECKS,
    GCodeChecks,
)
from simulator.preview import (
    Failed,
    Severity,
    ToolView,
    preview,
    print_checks,
    tool_mapping,
    wrong_types,
)
from simulator.tool import (
    AUTO_RETRACT_STEP,
    CORE_ONE_EXTRUDER,
    CORE_ONE_PRESETS,
    DEFAULT_TEMPERATURES,
    DERETRACT_STEP,
    INDX_EXTRUDER,
    INDX_PRESETS,
    IX_EXTRUDER,
    LOAD_CELL_PRESETS,
    MARKERS,
    MINI_EXTRUDER,
    MINI_PRESETS,
    MK3_5_EXTRUDER,
    MK3_5_PRESETS,
    MK4_EXTRUDER,
    PRESET_ALIASES,
    RETRACTED_STEP,
    TYPE_STEP,
    XL_DEV_KIT_EXTRUDER,
    XL_EXTRUDER,
    Extruder,
    LoadProcess,
    LoadStep,
    Material,
    Ramming,
    SimTool,
    Temperatures,
    UnloadProcess,
    describe,
    is_flexible,
    retracted_distance,
)

EVENT_LIMIT = 2000
SNAPSHOT_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
EPSILON = 1e-9
# The shortest purge a load runs, whatever the override: `minimal_purge` in
# `src/marlin_stubs/include/pause_settings.hpp`. `script/firmware-check` compares it with
# the firmware source.
MINIMAL_PURGE_MM = 1.0
# The iX's nozzle cleaner purges in steps of this until it has purged the purge length
# (`nozzle_cleaner_purge_sequence` in `src/marlin_stubs/pause/pause.cpp`).
IX_PURGE_STEP_MM = 5.0
UUID_TOOL = 0  # the one tool that keeps tag UUIDs, on a model that has them


class State(StrEnum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    STOPPED = "STOPPED"
    ATTENTION = "ATTENTION"


class Firmware(StrEnum):
    FULL = "full"
    NO_COUNTERS = "no_counters"
    NO_ENDPOINT = "no_endpoint"


class Power(StrEnum):
    ON = "on"
    OFF = "off"
    HUNG = "hung"


class Fault(StrEnum):
    NONE = "none"
    BAD_KEY = "bad_key"
    ERROR = "error"


class JobPhase(StrEnum):
    SCANNING = "SCANNING"
    BLIP = "BLIP"
    PREHEAT = "PREHEAT"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    WAIT_LOAD = "WAIT_LOAD"  # ATTENTION until the user reloads a tool
    HOLD = "HOLD"  # ATTENTION until the user taps through or clears a jam
    RUN = "RUN"  # a sync job applying its G-code
    PARKING = "PARKING"
    RECOVER = "RECOVER"  # BUSY after a power-panic boot, before the resume
    AWAIT_RESUME = "AWAIT_RESUME"  # ATTENTION: the bed went cold during the outage
    UNPARK = "UNPARK"  # PAUSED briefly while the head unparks after a power-panic resume
    PREVIEW_LOAD = "PREVIEW_LOAD"  # BUSY with no job: the preview's filament-not-inserted load


IDLE_STATES = (State.IDLE, State.FINISHED, State.STOPPED)
# The state each job phase reads as. Without a job the state is BUSY while a tool loads or
# unloads, else the idle screen the printer stores.
PHASE_STATES = {
    JobPhase.SCANNING: State.PRINTING,
    JobPhase.BLIP: State.IDLE,  # the firmware's PrintInit reads IDLE for a moment
    JobPhase.PREHEAT: State.PRINTING,
    JobPhase.PRINTING: State.PRINTING,
    JobPhase.PAUSED: State.PAUSED,
    JobPhase.WAIT_LOAD: State.ATTENTION,
    JobPhase.HOLD: State.ATTENTION,
    JobPhase.RUN: State.PRINTING,
    JobPhase.PARKING: State.PRINTING,
    JobPhase.RECOVER: State.BUSY,
    JobPhase.AWAIT_RESUME: State.ATTENTION,
    JobPhase.UNPARK: State.PAUSED,
    # The load's own screen covers the preview, and with no print running yet it reads BUSY
    # (`get_state` in `src/state/printer_state.cpp`), which has no job.
    JobPhase.PREVIEW_LOAD: State.BUSY,
}
TIMED_PHASES = (
    JobPhase.SCANNING,
    JobPhase.BLIP,
    JobPhase.PREHEAT,
    JobPhase.RUN,
    JobPhase.PARKING,
    JobPhase.RECOVER,
    JobPhase.UNPARK,
)


class Reason(StrEnum):
    """Why a job waits for the user."""

    PREVIEW = "preview"  # the print preview asks about a file's failed checks
    TOOLS_MAPPING = "tools mapping"  # a toolchanger's preview asks about the tools a print uses
    WRONG_FILAMENT = "wrong filament"  # the preview asks about a tool holding another type
    # Print on the tools-mapping screen asks about each kind of failed check once: a used
    # tool empty asks to turn the sensors off, then a used tool of another type asks too.
    SENSORS_OFF = "filament sensors off"
    MAPPING_TYPE = "wrong filament type"
    PARSE_ERROR = "parse error"  # a sync job's G-code can't run
    JAM = "jam"
    RUNOUT = "runout"
    COLOR_CHANGE = "color change"
    NO_FILAMENT = "no filament"  # the preview's filament-not-inserted screen
    COLD_BED = "cold bed"  # the bed went cold during a power panic


# The reasons each waiting phase can have. A jam waits in HOLD when it's cleared in place,
# and in WAIT_LOAD when it unloads the tool.
PHASE_REASONS = {
    JobPhase.HOLD: frozenset(
        {
            Reason.PREVIEW,
            Reason.TOOLS_MAPPING,
            Reason.WRONG_FILAMENT,
            Reason.SENSORS_OFF,
            Reason.MAPPING_TYPE,
            Reason.PARSE_ERROR,
            Reason.JAM,
        }
    ),
    JobPhase.WAIT_LOAD: frozenset(
        {Reason.RUNOUT, Reason.JAM, Reason.COLOR_CHANGE, Reason.NO_FILAMENT}
    ),
    JobPhase.AWAIT_RESUME: frozenset({Reason.COLD_BED}),
}
# The questions of the firmware's tools-mapping phase, while it isn't printing.
MAPPING_QUESTIONS = frozenset({Reason.TOOLS_MAPPING, Reason.SENSORS_OFF, Reason.MAPPING_TYPE})
# The questions the print preview asks with Abort and Print or Ok, which `tap_through` and
# `cancel` answer.
PREVIEW_QUESTIONS = MAPPING_QUESTIONS | {Reason.PREVIEW, Reason.WRONG_FILAMENT}


@dataclass(frozen=True)
class Attention:
    """What the screen asks the user, with the tool it names, or the text of a parse error
    or of the preview's failed checks. A fatal preview question offers only Abort. The
    tools-mapping screen can have no failed checks, and then no text."""

    reason: Reason
    tool: int | None = None
    detail: str | None = None
    fatal: bool = False

    def __str__(self) -> str:
        match self.reason:
            case reason if reason in PREVIEW_QUESTIONS:
                detail = self.detail or "nothing wrong"
                return f"{self.reason}{' (abort only)' if self.fatal else ''}: {detail}"
            case Reason.PARSE_ERROR:
                return f"parse error: {self.detail}"
            case Reason.NO_FILAMENT:
                return f"no filament in tool {self.tool}"
            case Reason.COLD_BED:
                return "bed cold after the power panic"
            case _:
                return f"{self.reason} tool {self.tool}"


@dataclass(frozen=True)
class ModelInfo:
    code: str
    name: str
    extruder: Extruder
    # The build's preset filaments. Left out of the hash so a `ModelInfo` stays hashable.
    presets: dict[str, Temperatures] = field(hash=False)
    uuids: bool = False
    gap_mm: float = 40.0
    max_tools: int = 1
    counters: bool = True
    mmu: bool = False  # whether the build can drive an MMU3: HAS_MMU2
    # Its `compatibility_group` in `printer_model_data.hpp`. The MK3.5, MK4 and XL builds
    # run several models and check a file against the one the printer is set to
    # (`PrinterModelInfo::current`), so each model has its own entry's group.
    group: str = field(kw_only=True)
    checks: GCodeChecks = field(kw_only=True)  # its build's

    def holds_uuid(self, tool: int) -> bool:
        """Whether `tool` keeps tag UUIDs: tool 0 of a model with a tag reader (F21).

        `M865.1 I0` writes the instance UUID there, and only the Core One (and Oak) and
        Core One L build `M865.1` and the tag reader.
        """
        return self.uuids and tool == UUID_TOOL

    def preset_temperatures(self, name: str) -> dict[str, int]:
        """Return the temperatures a material named `name` starts from, as `Material`
        keywords.

        A preset's name matches exactly, as `FilamentType::from_name` does. A name in
        `PRESET_ALIASES`, such as TPU, starts from its preset. Anything else gets the
        defaults, a preset only other builds have included.
        """
        values = self.presets.get(PRESET_ALIASES.get(name, name), DEFAULT_TEMPERATURES)
        return dict(zip(("nozzle", "preheat", "bed"), values, strict=True))


def _models(*infos: ModelInfo) -> dict[str, ModelInfo]:
    return {m.code: m for m in infos}


# The printer models, from the firmware: `script/firmware-check` compares them with its source.
MODELS = _models(
    ModelInfo(
        "1.3.5",
        "MK3.5",
        MK3_5_EXTRUDER,
        MK3_5_PRESETS,
        counters=False,
        mmu=True,
        group="mk3_5",
        checks=MK3_5_CHECKS,
    ),
    ModelInfo(
        "1.3.6",
        "MK3.5S",
        MK3_5_EXTRUDER,
        MK3_5_PRESETS,
        counters=False,
        mmu=True,
        group="mk3_5",
        checks=MK3_5_CHECKS,
    ),
    ModelInfo(
        "1.3.9", "MK3.9", MK4_EXTRUDER, LOAD_CELL_PRESETS, mmu=True, group="mk4", checks=MK4_CHECKS
    ),
    ModelInfo(
        "1.3.10",
        "MK3.9S",
        MK4_EXTRUDER,
        LOAD_CELL_PRESETS,
        mmu=True,
        group="mk4s",
        checks=MK4_CHECKS,
    ),
    ModelInfo(
        "1.4.0", "MK4", MK4_EXTRUDER, LOAD_CELL_PRESETS, mmu=True, group="mk4", checks=MK4_CHECKS
    ),
    ModelInfo(
        "1.4.1", "MK4S", MK4_EXTRUDER, LOAD_CELL_PRESETS, mmu=True, group="mk4s", checks=MK4_CHECKS
    ),
    ModelInfo(
        "2.1.0",
        "MINI",
        MINI_EXTRUDER,
        MINI_PRESETS,
        counters=False,
        group="mini",
        checks=PLAIN_CHECKS,
    ),
    ModelInfo(
        "3.1.0", "XL", XL_EXTRUDER, LOAD_CELL_PRESETS, max_tools=5, group="xl", checks=XL_CHECKS
    ),
    ModelInfo(
        "3.1.1", "XLP", XL_EXTRUDER, LOAD_CELL_PRESETS, max_tools=5, group="xls", checks=XL_CHECKS
    ),
    ModelInfo(
        "5.1.0",
        "XL",
        XL_DEV_KIT_EXTRUDER,
        LOAD_CELL_PRESETS,
        max_tools=5,
        counters=False,
        group="xl",
        checks=PLAIN_CHECKS,
    ),
    ModelInfo("4.1.0", "iX", IX_EXTRUDER, LOAD_CELL_PRESETS, group="ix", checks=IX_CHECKS),
    ModelInfo(
        "7.1.0",
        "COREONE",
        CORE_ONE_EXTRUDER,
        CORE_ONE_PRESETS,
        uuids=True,
        mmu=True,
        group="coreone",
        checks=CORE_ONE_CHECKS,
    ),
    ModelInfo(
        "7.2.0",
        "COREONEOAK",
        CORE_ONE_EXTRUDER,
        CORE_ONE_PRESETS,
        uuids=True,
        mmu=True,
        group="coreone",
        checks=CORE_ONE_CHECKS,
    ),
    ModelInfo(
        "8.1.0",
        "COREONEL",
        CORE_ONE_EXTRUDER,
        CORE_ONE_PRESETS,
        uuids=True,
        mmu=True,
        group="coreonel",
        checks=CORE_ONE_CHECKS,
    ),
    ModelInfo(
        "7.10.0",
        "COREONEINDX",
        INDX_EXTRUDER,
        INDX_PRESETS,
        gap_mm=27.0,
        max_tools=8,
        group="coreone_indx",
        checks=INDX_CHECKS,
    ),
    ModelInfo(
        "8.10.0",
        "COREONEL-INDX",
        INDX_EXTRUDER,
        INDX_PRESETS,
        gap_mm=27.0,
        max_tools=8,
        group="coreonel_indx",
        checks=INDX_CHECKS,
    ),
)


# The load's lengths a setting can override, in the order `_load_lengths` returns them.
LOAD_LENGTHS = ("slow_load_mm", "fast_load_mm", "purge_mm")
POSITIVE_SETTINGS = frozenset({"time_scale", "load_rate_mm_s", "print_rate_mm_s"})


@dataclass
class Settings:
    """The simulation's settings. A field that defaults to None overrides a value of the
    printer's model, and None means the model's."""

    time_scale: float = 1.0
    feed_ratio: float | None = None
    print_rate_mm_s: float = 5.0
    load_rate_mm_s: float = 10.0
    unload_seconds: float = 3.0
    slow_load_mm: float | None = None
    fast_load_mm: float | None = None
    # The whole purge, not doubled for a high-flow nozzle. At least 1 mm, in 5 mm steps on
    # the iX, and INDX ignores it: its cleaner purges a fixed length.
    purge_mm: float | None = None
    toolchange_purge_mm: float = 30.0
    jam_purge_mm: float = 60.0
    preheat_seconds: float = 5.0
    scan_seconds: float = 0.5
    blip_seconds: float = 0.3
    park_seconds: float = 1.0
    sync_job_seconds: float = 3.0
    recover_seconds: float = 2.0  # BUSY after a power-panic boot
    unpark_seconds: float = 1.0  # PAUSED while the head unparks on a power-panic resume
    start_blip: bool = True

    def __post_init__(self) -> None:
        """Check every field, so no way of building a `Settings` can hold a bad value."""
        for f in fields(self):
            key = f.name
            value = getattr(self, key)
            if isinstance(f.default, bool):
                if not isinstance(value, bool):
                    raise Invalid(f"{key} must be true or false")
            elif value is None and f.default is None:
                pass
            elif (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not 0 <= value <= sys.float_info.max
            ):
                raise Invalid(f"{key} must be a number of at least 0")
            elif key in POSITIVE_SETTINGS and value <= 0:
                raise Invalid(f"{key} must be greater than 0")

    def update(self, values: dict[str, Any]) -> None:
        """Apply a subset of fields. A bad one raises and changes nothing."""
        known = {f.name for f in fields(self)}
        for key in values:
            if key not in known:
                raise Invalid(f"unknown setting {key!r}")
        candidate = replace(self, **values)
        for key in known:
            setattr(self, key, getattr(candidate, key))


@dataclass
class Segment:
    tool: int
    slicer_mm: float
    # The filament type the file names for this tool (`filament_type` in its metadata), or
    # None when it names none, which skips the preview's type check.
    material: str | None = None


@dataclass
class PrintSpec:
    segments: list[Segment]
    duration_s: float | None = None
    preheat_s: float | None = None
    color_changes_mm: list[float] = field(default_factory=list)
    name: str = "part.bgcode"

    @property
    def total_mm(self) -> float:
        return sum(s.slicer_mm for s in self.segments)


@dataclass(kw_only=True)
class SimJob:
    """What every job has. A job is a `UserJob` or a `SyncJob`."""

    kind: ClassVar[str]
    id: int = 0  # set when the job is counted, as it starts
    name: str
    phase: JobPhase = JobPhase.SCANNING
    attention: Attention | None = None  # set exactly while the phase waits for the user
    remaining: float = 0.0  # seconds left in a timed phase
    time_printing: float = 0.0
    end_state: State = State.FINISHED

    def enter(self, phase: JobPhase, attention: Attention | None = None) -> None:
        """Move to `phase`. A waiting phase needs a reason that fits it; no other phase has one."""
        allowed = PHASE_REASONS.get(phase, frozenset())
        if (attention is None) != (not allowed) or (attention and attention.reason not in allowed):
            raise ValueError(f"{phase} can't wait for {attention!r}")
        self.phase, self.attention = phase, attention

    def waits_for(self, reason: Reason) -> bool:
        return self.attention is not None and self.attention.reason is reason

    @property
    def wait_tool(self) -> int | None:
        """The tool WAIT_LOAD waits for a load into."""
        return self.attention.tool if self.phase is JobPhase.WAIT_LOAD else None

    def progress(self) -> int:
        return 100 if self.phase is JobPhase.PARKING else 0


@dataclass(kw_only=True)
class UserJob(SimJob):
    """A print started from the page: it feeds filament through its segments."""

    kind: ClassVar[str] = "user"
    spec: PrintSpec
    fed_mm: float = 0.0
    segment: int = 0
    pending_color_changes: list[float] = field(default_factory=list)
    paused_from: JobPhase | None = None  # where resume goes back to
    cold_bed: bool = False  # a power-panic resume waits for the user when the bed went cold
    clean_at: float | None = None  # RECOVER time left when the nozzle clean purges
    restore_gap: float | None = None  # the tool's gap when a filament change began
    previewing: bool = True  # until the preview lets the print begin
    # The tools-mapping screen and the questions its Print asks, set when the screen opens.
    mapping: list[Attention] = field(default_factory=list)

    def progress(self) -> int:
        return int(100 * self.fed_mm / self.spec.total_mm)


@dataclass(kw_only=True)
class SyncJob(SimJob):
    """An uploaded G-code file: it applies its commands, then runs for a while and parks."""

    kind: ClassVar[str] = "sync"
    gcode: str
    run_seconds: float = 0.0  # the length of the RUN phase

    def progress(self) -> int:
        if self.phase is JobPhase.RUN and self.run_seconds > 0:
            # A sync job has no moves to measure, so the page sees time pass instead.
            return int(100 * (self.run_seconds - self.remaining) / self.run_seconds)
        return super().progress()


class Busy(Exception):
    """The action isn't allowed in the printer's current state."""


class Invalid(Exception):
    """The action's parameters are wrong: an unknown or disabled tool, a bad name."""


@dataclass
class Event:
    t: float
    printer: str | None
    kind: str
    text: str
    data: dict | None = None


def new_boot_id() -> int:
    return random.getrandbits(32)


def _positive(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise Invalid(f"{what} must be a positive number, got {value!r}")
    return float(value)


def check_name(name: Any) -> str:
    if not isinstance(name, str) or not gcode.NAME_RE.fullmatch(name):
        raise Invalid(f"filament name {name!r} must be 1 to 7 of A-Z a-z 0-9 _ -")
    return name


def _check_temperatures(temps: Any) -> tuple[int, int, int]:
    ok = isinstance(temps, list | tuple) and len(temps) == 3
    if not ok or not all(isinstance(v, int) and 0 <= v <= 500 for v in temps):
        raise Invalid(f"temperatures must be [nozzle, preheat, bed] in °C, got {temps!r}")
    return tuple(temps)


def check_color(color: Any) -> str | None:
    if color is None or color == "":
        return None
    if not isinstance(color, str) or not gcode.COLOR_RE.fullmatch(color):
        raise Invalid(f"color {color!r} must look like #rrggbb")
    return color.lower()


def check_file_type(value: Any) -> str | None:
    """A segment's filament type, as a file's `filament_type` entry can hold it: the
    firmware cuts each to 7 characters (`gcode_info.cpp`)."""
    if value is not None and not (isinstance(value, str) and 0 < len(value) <= 7):
        raise Invalid(f"a segment's material must be 1 to 7 characters, got {value!r}")
    return value


def check_uuid(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        return str(uuidlib.UUID(str(value)))
    except ValueError:
        raise Invalid(f"{value!r} isn't a UUID") from None


@dataclass
class SimPrinter:
    name: str
    port: int
    api_key: str
    model: ModelInfo
    firmware: Firmware = Firmware.FULL
    power: Power = Power.ON
    fault: Fault = Fault.NONE
    boot_id: int = field(default_factory=new_boot_id)
    # The screen with no job and no load running: IDLE, or FINISHED or STOPPED until dismissed.
    screen: State = State.IDLE
    job: UserJob | SyncJob | None = None
    job_counter: int = 0
    selected_tool: int = 0
    message: str | None = None
    homed: bool = False
    # How the print preview treats a failed check that a setting governs: the firmware has
    # one setting per kind of check, but they share one screen and the worst decides.
    checks: Severity = Severity.WARN
    # The menu's switch for every filament sensor at once. Off, no sensor reads, so no
    # runout, no idle sensor clear, and no filament check at the preview.
    sensors: bool = True
    high_flow: bool = False  # a high-flow nozzle, which doubles the load's purge
    # The user's auto-retract switch. It has effect only on a model whose extruder has
    # one; INDX always auto-retracts, and a build without the sequence never does.
    auto_retract: bool = True
    tools: dict[int, SimTool] = field(default_factory=lambda: {0: SimTool(0)})
    files: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Not fields, so asdict() and snapshots leave them out.
        self._sim: Simulation | None = None
        self._shown = self.state  # the state last logged

    @property
    def state(self) -> State:
        """The state PrusaLink reports, which follows from the job phase or the idle screen."""
        if self.job is not None:
            return PHASE_STATES[self.job.phase]
        if any(t.process is not None for t in self.tools.values()):
            return State.BUSY
        return self.screen

    # Plumbing.

    @property
    def settings(self) -> Settings:
        return self._sim.settings if self._sim else Settings()

    @property
    def feed_ratio(self) -> float:
        """How much an E move feeds per mm commanded: the setting, else the model's."""
        ratio = self.settings.feed_ratio
        return self.model.extruder.feed_ratio if ratio is None else ratio

    def _track(self, tool: SimTool, mm: float) -> None:
        """Move `tool`'s filament by `mm` commanded, signed, as the firmware's planner does.

        A feed first feeds back an auto-retracted distance, and a retract forgets it: the
        auto-retract hook in `Planner::buffer_segment`
        (`lib/Marlin/Marlin/src/module/planner.cpp`). Every move the simulator makes goes
        through here.
        """
        if mm > 0:
            self._deretract(tool)
        elif mm < 0:
            tool.auto_retracted = None
        self._count(tool, mm)

    def _count(self, tool: SimTool, mm: float) -> None:
        """Count a move as the firmware's filament tracker does.

        A retract grows the gap up to the model's limit, a feed fills it first, and only what
        goes past the gap counts, times the feed ratio. The gap becomes valid when it reaches
        0 and invalid when it reaches the limit (`track_extruder_move` in
        `src/feature/filament_tracker/filament_tracker.cpp`).
        """
        limit = self.model.gap_mm
        unclamped = tool.gap - mm
        tool.gap = min(max(unclamped, 0.0), limit)
        # A move split into ticks can stop a rounding error short of either end.
        if tool.gap <= EPSILON:
            tool.gap, tool.gap_valid = 0.0, True
        elif tool.gap >= limit - EPSILON:
            tool.gap, tool.gap_valid = limit, False
        tool.extruded += max(-unclamped, 0.0) * self.feed_ratio

    def _tracked(self, tool: SimTool) -> float:
        """The tracker's gap when it's valid, else 0 (`get_retracted_distance`)."""
        return tool.gap if tool.gap_valid else 0.0

    def _deretract(self, tool: SimTool) -> None:
        """Feed back an auto-retracted distance (`maybe_deretract_to_nozzle`).

        On a Nextruder that's 55 mm into a gap that stops at 40, so 15 mm count that never
        left the nozzle.
        """
        stored = tool.auto_retracted
        if stored:
            tool.auto_retracted = 0.0
            self._count(tool, stored)
            self._log("load", f"T{tool.index} deretracted {stored:g} mm")

    def _fully_retracted(self, tool: SimTool) -> bool:
        """`AutoRetract::is_fully_retracted`: the stored distance reaches the threshold."""
        stored = tool.auto_retracted
        return stored is not None and stored >= self.model.extruder.full_retract_mm

    def _retract_to(self, tool: SimTool, target: float) -> None:
        """Retract until the filament is `target` mm back, unless it already is
        (`mapi::retract_to` in `src/common/mapi/motion.cpp`)."""
        stored = tool.auto_retracted or 0.0
        if stored >= target:
            return
        # A stored distance counts as retracted; the retract then forgets it.
        current = stored or self._tracked(tool)
        if target - current > EPSILON:
            self._track(tool, current - target)

    def _restore_to(self, tool: SimTool, target: float) -> None:
        """Move the filament to `target` mm back, feeding or retracting
        (`mapi::restore_retracted_distance` in `src/common/mapi/motion.cpp`)."""
        stored = tool.auto_retracted or 0.0
        current = stored or self._tracked(tool)
        # The move takes a stored distance over, so a feed doesn't deretract it again.
        tool.auto_retracted = None if stored else tool.auto_retracted
        if abs(target - current) > EPSILON:
            self._track(tool, current - target)

    @staticmethod
    def _flexible(tool: SimTool) -> bool:
        """Whether the type `FilamentType::for_tool_heuristic` finds is flexible: the stored
        type, else the type the last unload left. The firmware checks the print's G-code
        filament name between the two, which isn't modeled."""
        return is_flexible(tool.material or tool.kept_type)

    def _sequence(self, case: str, tool: SimTool) -> Ramming:
        """The ramming sequence for `case`, or its flexible variant for flexible filament."""
        ramming = self.model.extruder.ramming
        flexible = f"{case}_flexible"
        return ramming[flexible if self._flexible(tool) and flexible in ramming else case]

    @staticmethod
    def _ram_steps(sequence: Ramming) -> list[LoadStep]:
        """A ramming sequence as `RammingSequence::execute` runs it: one that starts with a
        feed first feeds back what's retracted (`mapi::fully_deretract`)."""
        first = [LoadStep(DERETRACT_STEP)] if sequence[0][0] > 0 else []
        return first + [LoadStep("RAM", e) for e, _feedrate in sequence]

    def _auto_retract_steps(self, tool: SimTool) -> list[LoadStep]:
        """The auto-retract's steps, or none where the firmware skips it.

        `AutoRetract::maybe_retract_from_nozzle` skips a tool that's fully retracted, a
        printer with the switch off, and flexible filament. It feeds back what's stored,
        runs the sequence, and stores the distance the sequence retracts.
        """
        ex = self.model.extruder
        sequence = ex.ramming.get("auto_retract")
        if (
            sequence is None
            or self._fully_retracted(tool)
            or (ex.auto_retract_switchable and not self.auto_retract)
            or self._flexible(tool)
        ):
            return []
        self._deretract(tool)
        stored = LoadStep(RETRACTED_STEP, retracted_distance(sequence))
        return [*self._ram_steps(sequence), stored]

    def _unload_steps(self, tool: SimTool, case: str) -> list[LoadStep]:
        """The moves of an unload with the `case` ramming (`src/marlin_stubs/pause/pause.cpp`).

        INDX purges 2 mm first. A Nextruder that's fully auto-retracted unloads cold, with
        no ramming. The unload then retracts its length less what the ramming retracted.
        """
        ex = self.model.extruder
        unload = ex.lengths.unload_mm
        if ex.auto_retracts and ex.cold_unload and self._fully_retracted(tool):
            return [LoadStep("UNLOAD", -max(unload - (tool.auto_retracted or 0.0), 0.0))]
        sequence = self._sequence(case, tool)
        return [
            LoadStep("PURGE", ex.unload_purge_mm),
            *self._ram_steps(sequence),
            LoadStep("UNLOAD", -max(unload - retracted_distance(sequence), 0)),
        ]

    def _settle(self, tool: SimTool, steps: list[LoadStep], material: Material | None) -> None:
        """Act on the markers at the head of `steps` and drop finished moves, so the head is
        a move with something left, or `steps` is empty."""
        while steps:
            step = steps[0]
            if step.phase not in MARKERS:
                if abs(step.mm) > EPSILON:
                    return
                steps.pop(0)
                continue
            steps.pop(0)
            if step.phase == TYPE_STEP:
                tool.store(material if material is not None else tool.kept())
                self._log("load", f"T{tool.index} type set to {describe(tool.material)}")
            elif step.phase == AUTO_RETRACT_STEP:
                steps[:0] = self._auto_retract_steps(tool)
            elif step.phase == DERETRACT_STEP:
                self._deretract(tool)
                self._track(tool, self._tracked(tool))
            else:  # RETRACTED_STEP
                tool.auto_retracted = step.mm
                self._log(
                    "load",
                    f"T{tool.index} auto-retracted {step.mm:g} mm: extruded {int(tool.extruded)}",
                )

    def _move(self, tool: SimTool, steps: list[LoadStep], most: float) -> float:
        """Run the head move for up to `most` mm; return the mm moved."""
        step = steps[0]
        moved = min(abs(step.mm), most)
        move = moved if step.mm > 0 else -moved
        self._track(tool, move)
        step.mm -= move
        return moved

    def _run_steps(self, tool: SimTool, steps: list[LoadStep]) -> None:
        """Run `steps` at once, for moves the simulator doesn't time."""
        self._settle(tool, steps, None)
        while steps:
            self._move(tool, steps, abs(steps[0].mm))
            self._settle(tool, steps, None)

    def _load_lengths(self) -> tuple[float, float, float]:
        """The slow load, fast load and purge a load runs with no override set.

        A high-flow nozzle doubles the purge (`pause_settings.cpp`), except where the
        nozzle cleaner's G-code purges a fixed length instead (INDX).
        """
        ex = self.model.extruder
        lengths = ex.lengths
        # The iX's cleaner purges 0 of its own: it purges the build's length in steps.
        purge = ex.cleaner_purge_mm or lengths.purge_mm * (2 if self.high_flow else 1)
        return lengths.slow_load_mm, lengths.fast_load_mm, purge

    def _load_steps(
        self, purge: float | None, extra: float, printing: bool, *, auto_retract: bool = True
    ) -> list[LoadStep]:
        """The moves of a load (`src/marlin_stubs/pause/pause.cpp`), with the overrides.

        `purge` is the load's own purge override, and `extra` is added to the purge. As
        `Settings::purge_length` in `src/marlin_stubs/pause/pause_settings.cpp` does, an
        override replaces the build's length, high-flow doubling included, and no purge is
        shorter than 1 mm. INDX's cleaner G-code purges its fixed length whatever the
        override, and the iX purges in 5 mm steps until it reaches the length.

        The type is written before the purge. Without a nozzle cleaner the purge ends with a
        retract; the iX purges with no retract, and the INDX cleaner's purge ends with an
        auto-retract (`M1705`), in a print too. A load outside a print then auto-retracts
        (`load_finalize_process`). A reload during a print primes and retracts again
        instead, and the resume puts the tool back at the gap it had when the change began.
        The preview's load does neither.
        """
        s, lengths = self.settings, self.model.extruder.lengths
        slow, fast, model_purge = self._load_lengths()
        slow = slow if s.slow_load_mm is None else s.slow_load_mm
        fast = fast if s.fast_load_mm is None else s.fast_load_mm
        ex = self.model.extruder
        if ex.cleaner_purge_mm:
            purge = ex.cleaner_purge_mm
        else:
            if purge is None:
                purge = model_purge if s.purge_mm is None else s.purge_mm
            purge = max(purge, MINIMAL_PURGE_MM)
        purge += extra
        if ex.cleaner_purge_mm == 0:  # the iX
            purge = math.ceil(purge / IX_PURGE_STEP_MM - EPSILON) * IX_PURGE_STEP_MM
        steps = [
            LoadStep("SLOW", slow),
            LoadStep("FAST", fast),
            LoadStep(TYPE_STEP),
            LoadStep("PURGE", purge),
        ]
        if ex.cleaner_purge_mm is None:
            steps.append(LoadStep("RETRACT", -lengths.retract_mm))
        if ex.cleaner_auto_retract:
            steps.append(LoadStep(AUTO_RETRACT_STEP))
        if printing:
            steps += [LoadStep("PRIME", lengths.prime_mm), LoadStep("RETRACT", -lengths.retract_mm)]
        elif auto_retract:
            steps.append(LoadStep(AUTO_RETRACT_STEP))
        return steps

    def _log(self, kind: str, text: str, data: dict | None = None) -> None:
        if self._sim:
            self._sim.log(self.name, kind, text, data)

    def _show_state(self) -> None:
        """Log the state when it changed. Called wherever a phase, the screen or a load changes."""
        state = self.state
        if state is not self._shown:
            self._log("state", f"{self._shown} -> {state}")
            self._shown = state

    def _tool(self, index: int, *, enabled: bool = True) -> SimTool:
        tool = self.tools.get(index)
        if tool is None:
            raise Invalid(f"{self.name} has no tool {index!r}")
        if enabled and not tool.enabled:
            raise Invalid(f"tool {index} is disabled")
        return tool

    def _need_power(self) -> None:
        if self.power is Power.OFF:
            raise Busy(f"{self.name} is off")

    def _count_unload(self, tool: SimTool, why: str) -> None:
        # The firmware counts a write of "none" over a stored type, nothing else.
        if tool.material is None:
            self._log("unload", f"T{tool.index} {why}: no type stored, not counted")
            return
        tool.unload_count += 1
        tool.extruded_at_unload = int(tool.extruded)
        tool.clear_type()
        self._log(
            "unload",
            f"T{tool.index} {why}: unload_count {tool.unload_count} at {tool.extruded_at_unload}",
        )

    def _physical_unload(self, tool: SimTool, why: str) -> None:
        self._count_unload(tool, why)
        tool.loaded = False
        tool.gap, tool.gap_valid, tool.auto_retracted = self.model.gap_mm, False, None

    def _idle_sensor_clear(self, tool: SimTool, *, sensor_fault_counts: bool) -> None:
        if not self.sensors:
            return
        empty = not tool.loaded or (sensor_fault_counts and tool.sensor_fault)
        if tool.material is not None and empty:
            self._count_unload(tool, "idle sensor clear")

    # Job state rules.

    def job_visible(self) -> bool:
        job = self.job
        return job is not None and job.phase not in (JobPhase.BLIP, JobPhase.PREVIEW_LOAD)

    def can_print(self) -> bool:
        # The firmware's rule is state-only: IDLE, READY, STOPPED or FINISHED. A print asked
        # for during the PrintInit blip, which reads IDLE, passes that rule and is then
        # dropped; `print_file` handles it. A running load or unload reads BUSY, so it
        # refuses too.
        return self.state in IDLE_STATES and self.job is None and self.power is not Power.OFF

    def upload(self, name: str, body: str, *, print_after: bool, overwrite: bool) -> None:
        """Store a file and maybe print it. The file stays even when the print is refused."""
        if name in self.files and not overwrite:
            raise Busy("File already exists")
        if self.job is not None and self.job.name == name:
            raise Busy("File is busy")
        self.files[name] = body
        self._log("job", f"stored {name} ({len(body)} bytes)")
        if print_after:
            self.print_file(name)

    def print_file(self, name: str) -> None:
        """Print a stored file. The job is counted at once, and the preview checks the file
        when the scan ends."""
        if self.job is not None and self.job.phase is JobPhase.BLIP:
            # The firmware's state check passes during PrintInit, but print_start ignores
            # the request there, so the upload succeeds and the print that's starting goes
            # on (`marlin_server.cpp:1527-1550` at 2db93db5).
            self._log("job", f"{name}: asked for during the start blip; the firmware drops it")
            return
        if not self.can_print():
            raise Busy("Can't print right now")
        self._start_job(SyncJob(name=name, gcode=self.files[name]))

    # Actions on tools.

    def load(
        self,
        tool: int,
        *,
        source: str | None = None,
        name: str | None = None,
        color: str | None = None,
        uuid: str | None = None,
        purge_mm: float | None = None,
        temperatures: list[int] | None = None,
    ) -> None:
        """Load filament into a tool: slow and fast feed, set the type, purge.

        `source` None keeps the previous type and is only for the reload a runout waits
        for. While the printer is off the filament is pushed in by hand: the type is set
        at once and nothing is counted. `temperatures` is a tag's nozzle, preheat and bed
        temperatures; without it a tag load takes the preset's.
        """
        t = self._tool(tool)
        material = self._new_material(t, source, name, color, uuid, temperatures)
        self._check_load(t, keep=source is None)
        what = f"{source} {describe(material)}" if source else f"keep {describe(t.kept())}"
        if self.power is Power.OFF:
            self._hand_load(t, material)
            return
        job = self.job
        waiting = job is not None and job.wait_tool == tool
        self._log("action", f"load T{tool} {what}")
        if purge_mm is not None:
            purge_mm = _positive(purge_mm, "purge_mm")
        extra = self.settings.jam_purge_mm if waiting and job.waits_for(Reason.JAM) else 0.0
        if waiting and isinstance(job, UserJob) and job.previewing:
            # The preview's Yes runs `M701` (`queue_filament_gcodes` in
            # `src/common/marlin_print_preview.cpp`): a plain load, with no prime, and no
            # auto-retract because `is_printing()` is already true there
            # (`load_finalize_process` in `src/marlin_stubs/pause/pause.cpp`).
            steps = self._load_steps(purge_mm, extra, False, auto_retract=False)
            job.enter(JobPhase.PREVIEW_LOAD)
        else:
            steps = self._load_steps(purge_mm, extra, waiting)
        t.process = LoadProcess(steps, material)
        self._next_load_step(t, t.process)  # a load with no slow or fast feed types at once
        if job is None:
            self.screen = State.IDLE  # using the menu dismisses a finished or stopped screen
        self._show_state()

    def _new_material(
        self, t: SimTool, source: str | None, name: Any, color: Any, uuid: Any, temps: Any
    ) -> Material | None:
        if source is None:
            if temps is not None:
                raise Invalid("only a tag load takes temperatures")
            return None
        if source not in ("tag", "preset"):
            raise Invalid(f"source must be tag or preset, got {source!r}")
        name, color = check_name(name), check_color(color)
        if source == "preset":
            if uuid or temps is not None:
                raise Invalid("a preset load has no UUID or temperatures")
            # The menu lists only the build's presets (`all_filament_types` in
            # `src/common/filament_list.hpp`), so PPS can't be picked on an MK4S.
            if name not in self.model.presets:
                raise Invalid(f"{self.model.name} has no {name} preset")
            temps = self.model.preset_temperatures(name)
            # Picking a color makes the preset ad-hoc, which replaces the ad-hoc slot.
            return Material(name, color, adhoc=color is not None, **temps)
        if not self.model.uuids:
            raise Invalid(f"{self.model.name} has no tag reader")
        value = check_uuid(uuid)
        tag_temps = self.model.preset_temperatures(name)
        if temps is not None:
            tag_temps = dict(zip(tag_temps, _check_temperatures(temps), strict=True))
        if not self.model.holds_uuid(t.index):
            return Material(name, color, **tag_temps)
        # A real tag carries its product's UUIDs; derive stable ones from the name and color.
        return Material(
            name,
            color,
            value,
            material_uuid=str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"material/{name}")),
            package_uuid=str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"package/{name}/{color}")),
            **tag_temps,
        )

    def _hand_load(self, t: SimTool, material: Material | None) -> None:
        # Filament goes in even when there's no type to restore.
        t.store(t.kept() if material is None else material)
        t.loaded, t.gap = True, 0.0
        self._log("load", f"T{t.index} loaded by hand while off: {describe(t.material)}")

    def unload(self, tool: int) -> None:
        """Unload a tool. While the printer is off the type stays stored until the next boot."""
        t = self._tool(tool)
        self._check_unload(t)
        if self.power is Power.OFF:
            self._log("action", f"unload T{tool} by hand while off")
            t.loaded, t.gap = False, self.model.gap_mm
            return
        self._log("action", f"unload T{tool}")
        steps = self._unload_steps(t, "unload")
        self._settle(t, steps, None)
        t.process = UnloadProcess(steps, self.settings.unload_seconds)
        self.screen = State.IDLE  # using the menu dismisses a finished or stopped screen
        self._show_state()

    def _manual_move(self, tool: int, mm: float) -> tuple[SimTool, float]:
        t = self._tool(tool)
        mm = _positive(mm, "mm")
        self._check_move(t)
        return t, mm

    def extrude(self, tool: int, mm: float) -> None:
        t, mm = self._manual_move(tool, mm)
        self._track(t, mm)
        self._log("action", f"extrude T{tool} {mm:g} mm: extruded {int(t.extruded)}")

    def retract(self, tool: int, mm: float) -> None:
        t, mm = self._manual_move(tool, mm)
        self._track(t, -mm)
        self._log("action", f"retract T{tool} {mm:g} mm: gap {t.gap:g}")

    def select_tool(self, tool: int) -> None:
        t = self._tool(tool)
        self._check_select(t)
        self.selected_tool = tool
        self._log("action", f"select T{tool}")
        if self.power is not Power.OFF:
            self._idle_sensor_clear(t, sensor_fault_counts=True)

    # Actions on prints.

    def start_print(self, spec: PrintSpec) -> None:
        self._check_start_print()
        if not spec.segments:
            raise Invalid("a print needs at least one segment")
        types: dict[int, str | None] = {}
        for s in spec.segments:
            # A disabled tool is the preview's to refuse, after the job is counted.
            self._tool(s.tool, enabled=False)
            _positive(s.slicer_mm, "slicer_mm")
            check_file_type(s.material)
            if types.setdefault(s.tool, s.material) != s.material:
                raise Invalid(f"a file names one filament type per tool, not two for T{s.tool}")
        if spec.duration_s is not None:
            _positive(spec.duration_s, "duration_s")
        if spec.preheat_s is not None and spec.preheat_s < 0:
            raise Invalid("preheat_s can't be negative")
        total = spec.total_mm
        changes = sorted(x for x in spec.color_changes_mm if 0 < x < total)
        self._start_job(UserJob(name=spec.name, spec=spec, pending_color_changes=changes))

    def _start_job(self, job: UserJob | SyncJob) -> None:
        """Count the job and scan the file. A print from a file is counted before the preview
        asks anything (`init` in `src/common/marlin_print_preview.cpp`)."""
        self.homed = True  # a print starts by homing
        self.job_counter += 1
        job.id = self.job_counter
        job.enter(JobPhase.SCANNING)
        job.remaining = self.settings.scan_seconds
        self.job = job
        self._log(
            "job",
            f"job {job.id} ({job.kind}) {job.name} started",
            {"started": job.id, "kind": job.kind},
        )
        self._show_state()

    def pause(self) -> None:
        self._check_pause()
        job = self.job
        self._log("action", "pause")
        job.paused_from = job.phase
        job.enter(JobPhase.PAUSED)
        self._show_state()

    def resume(self) -> None:
        self._check_resume()
        job = self.job
        if job.phase is JobPhase.AWAIT_RESUME:
            self._log("action", "resume after the power panic")
            job.cold_bed = False
            self._recover(job, clean=True)
            return
        self._log("action", "resume")
        if job.restore_gap is not None:
            # The unpark's feed and `restore_retracted_distance` put the filament back at
            # the gap it had when the change began (`Pause::filament_change` in
            # `src/marlin_stubs/pause/pause.cpp`).
            self._restore_to(self.tools[self.selected_tool], job.restore_gap)
            job.restore_gap = None
        if job.paused_from is JobPhase.PREHEAT:
            job.enter(JobPhase.PREHEAT)
            self._show_state()
        else:
            self._begin_feeding(job)
        job.paused_from = None

    def cancel(self) -> None:
        self._check_cancel()
        job = self.job
        if isinstance(job, UserJob) and job.attention in job.mapping[1:]:
            self._log("action", "cancel: back to the tools-mapping screen")
            self._wait(job, job.mapping[0])
            return
        if self._at_preview(job) or job.phase is JobPhase.BLIP:
            # Abort or No at the preview, or a stop before the print begins, which
            # `print_abort` sends to `Aborting_Preview`: `cleanup_print` in `marlin_server.cpp`
            # goes to IDLE with nothing parked or moved, and the job's ID stays used.
            self._log("action", f"cancel job {job.id}: abort at the preview")
            self.job, self.screen = None, State.IDLE
            self._show_state()
            return
        self._log("action", f"cancel job {job.id}")
        for t in self.tools.values():
            self._abort_process(t)
        self._park(job, State.STOPPED)

    @staticmethod
    def _at_preview(job: UserJob | SyncJob) -> bool:
        """Whether the job is still in the preview: scanned, or waiting on one of its
        questions."""
        if job.phase is JobPhase.SCANNING or any(job.waits_for(r) for r in PREVIEW_QUESTIONS):
            return True
        return isinstance(job, UserJob) and job.previewing and job.phase is JobPhase.WAIT_LOAD

    def _abort_process(self, t: SimTool) -> None:
        """Stop a load or unload where it stands.

        The type is set before the purge, so a load stopped before then leaves the tool
        empty with no type, and one stopped after keeps its type and counts as loaded. A
        stopped unload leaves the filament in. This is a simplification of the firmware's
        abort paths.
        """
        p = t.process
        if p is None:
            return
        t.process = None
        if isinstance(p, LoadProcess) and p.typed:
            t.loaded = True
        self._log("load", f"T{t.index} {p.kind} stopped by the cancel")

    def dismiss(self) -> None:
        self._check_dismiss()
        self._log("action", "dismiss")
        self.screen = State.IDLE
        self._show_state()

    def home(self) -> None:
        """Home from the menu. Only the flag changes: homing moves no filament."""
        self._check_home()
        self.homed = True
        self._log("action", "home")

    def runout(self, tool: int) -> None:
        t = self._tool(tool)
        self._check_sensor_runout(t)
        self._log("action", f"runout T{tool}")
        self._runout(t, Reason.RUNOUT)

    def jam(self, tool: int, unload: bool = True) -> None:
        """A jam either unloads like a runout (and purges more on reload) or is cleared in place."""
        t = self._tool(tool)
        self._check_runout(t)
        self._log("action", f"jam T{tool}" + ("" if unload else " without unloading"))
        if unload:
            self._runout(t, Reason.JAM)
            return
        job = self.job
        job.paused_from = job.phase
        job.enter(JobPhase.HOLD, Attention(Reason.JAM, tool))
        self._show_state()

    def _runout(self, t: SimTool, reason: Reason) -> None:
        job = self.job
        # Only a print in progress runs out, so the phase is PRINTING or PREHEAT here.
        job.paused_from = job.phase
        # The filament change records the tracker's distance, parks with a retract, then
        # rams and unloads (`Pause::filament_change` and `park_nozzle_and_notify` in
        # `src/marlin_stubs/pause/pause.cpp`).
        job.restore_gap = self._tracked(t)
        self._retract_to(t, self.model.extruder.lengths.retract_mm)
        self._run_steps(t, self._unload_steps(t, "runout"))
        self._physical_unload(t, str(reason))
        job.enter(JobPhase.WAIT_LOAD, Attention(reason, t.index))
        self._show_state()

    def clear_jam(self) -> None:
        self._check_clear_jam()
        job = self.job
        self._log("action", "clear jam")
        job.enter(JobPhase.PAUSED)
        self._show_state()

    def tap_through(self) -> None:
        """Answer the screen's question: Print or Ok at the preview, or abort on a parse
        error."""
        self._check_tap_through()
        job = self.job
        self._log("action", f"tap through: {job.attention}")
        if isinstance(job, UserJob) and job.waits_for(Reason.PREVIEW):
            self._after_incompatible(job)
        elif isinstance(job, UserJob) and job.attention in job.mapping:
            if job.waits_for(Reason.SENSORS_OFF):
                self.set_sensors(False)  # it stays off after the print
            following = job.mapping[job.mapping.index(job.attention) + 1 :]
            if following:
                self._wait(job, following[0])
            else:
                self._begin(job)
        elif any(job.waits_for(r) for r in PREVIEW_QUESTIONS):
            self._begin(job)
        else:
            self._park(job, State.STOPPED)

    # Power and faults.

    def boot(self, *, idle_clear: bool = True) -> None:
        """Start the firmware: counters restart, types and the job counter survive.

        The idle sensor clear runs at boot unless a print is resuming: the firmware skips it
        while printing, and a power-panic resume counts as printing.
        """
        self.boot_id = new_boot_id()
        for t in self.tools.values():
            t.extruded, t.unload_count, t.extruded_at_unload = 0.0, 0, 0
            t.process = None
            self._boot_tracker(t)
        self.homed, self.message = False, None
        self._log("boot", f"boot id {self.boot_id}")
        selected = self.tools.get(self.selected_tool)
        if idle_clear and selected is not None:
            self._idle_sensor_clear(selected, sensor_fault_counts=False)

    def _boot_tracker(self, t: SimTool) -> None:
        """The tracker starts at gap 0 and invalid; a stored auto-retract distance then sets
        it, valid below the gap's limit (`marlin_server::init` and
        `FilamentTracker::assume_retracted_distance`). The stored distance survives."""
        stored = t.auto_retracted
        if stored is None:
            t.gap, t.gap_valid = 0.0, False
            return
        t.gap = min(max(stored, 0.0), self.model.gap_mm)
        t.gap_valid = t.gap != self.model.gap_mm

    def reboot(self, *, keep_job: bool = False, cold_bed: bool = False) -> None:
        """Reboot. With keep_job a print in progress resumes with the same ID (power panic).

        A print that wasn't paused retracts at the cut, except on the XL. The resume is BUSY
        while the firmware recovers: INDX purges in its nozzle cleaner halfway through, and
        every other model that retracted feeds the retract back at the end. Then PAUSED
        while the head unparks, then PRINTING. With `cold_bed` it waits in ATTENTION for
        `resume()` before recovering. A print that was paused comes back paused, with no
        filament moved.
        """
        self._need_power()
        self._log("action", "power loss and resume" if keep_job else "reboot")
        self._reboot(keep_job, cold_bed)

    def _reboot(self, keep_job: bool, cold_bed: bool = False) -> None:
        job = self.job
        resumable = (JobPhase.PRINTING, JobPhase.PAUSED, JobPhase.PREHEAT, JobPhase.WAIT_LOAD)
        # A cut during the preview has nothing to resume: the firmware saves power-panic data
        # only once the print has begun (`power_panic::prepare` in
        # `src/common/power_panic.cpp`).
        keep = (
            keep_job and isinstance(job, UserJob) and not job.previewing and job.phase in resumable
        )
        was_paused = keep and job.phase in (JobPhase.PAUSED, JobPhase.WAIT_LOAD)
        if keep and not was_paused and self.model.extruder.panic_retract:
            # The cut retracts through the planner, which forgets a stored auto-retract
            # distance (`panic_loop` in `src/common/power_panic.cpp`). The counters restart
            # at the boot.
            self._track(self._job_tool(job), -self.model.extruder.lengths.retract_mm)
        self.boot(idle_clear=not keep)
        if not keep:
            self.job, self.screen = None, State.IDLE
            self._show_state()
            return
        job.restore_gap = None  # a power loss ends the filament change before its restore
        if was_paused:
            job.paused_from = JobPhase.PRINTING
            job.enter(JobPhase.PAUSED)
            self._show_state()
            return
        job.paused_from = None
        job.cold_bed = cold_bed
        self._recover(job, clean=not cold_bed)

    def _recover(self, job: UserJob, *, clean: bool) -> None:
        """BUSY while the firmware heats and homes. With `clean`, a model whose resume
        purges does so halfway through."""
        job.enter(JobPhase.RECOVER)
        job.remaining = self.settings.recover_seconds
        purges = clean and self.model.extruder.panic_purge_mm is not None
        job.clean_at = self.settings.recover_seconds / 2 if purges else None
        self._show_state()

    def _unpark(self, job: UserJob) -> None:
        """End the recovery. A model that retracted at the cut and doesn't purge feeds the
        retract back, filament loaded or not (`ResumeState::Unpark` in
        `src/common/power_panic.cpp`)."""
        ex = self.model.extruder
        if ex.panic_retract and ex.panic_purge_mm is None:
            self._track(self._job_tool(job), ex.lengths.retract_mm)
            self._log("job", f"fed back {ex.lengths.retract_mm:g} mm")
        job.enter(JobPhase.UNPARK)
        job.remaining = self.settings.unpark_seconds
        self._show_state()

    def _nozzle_clean(self, job: UserJob) -> None:
        """INDX's power-panic resume purges in the nozzle cleaner, filament loaded or not,
        and doesn't auto-retract after (`G12 S21`, `ResumeState::WaitForHeaters` in
        `src/common/power_panic.cpp`)."""
        purge = self.model.extruder.panic_purge_mm
        self._track(self._job_tool(job), purge)
        self._log("job", f"nozzle clean purged {purge:g} mm")

    def _job_tool(self, job: UserJob) -> SimTool:
        """The tool the job's current segment prints with."""
        return self.tools[job.spec.segments[job.segment].tool]

    def set_power(self, power: Power, *, resume: bool = False, cold_bed: bool = False) -> None:
        """Off freezes the printer; leaving off boots it, which drops any job unless `resume`."""
        power = Power(power)
        old, self.power = self.power, power
        self._log("action", f"power {power}")
        if old is Power.OFF and power is not Power.OFF:
            self._reboot(resume, cold_bed)

    def set_fault(self, fault: Fault) -> None:
        self.fault = Fault(fault)
        self._log("action", f"fault {self.fault}")

    def set_firmware(self, firmware: Firmware) -> None:
        self.firmware = Firmware(firmware)
        self._log("action", f"firmware {self.firmware}")

    def set_sensor_fault(self, tool: int, value: bool) -> None:
        self._tool(tool).sensor_fault = value
        self._log("action", f"T{tool} sensor fault {'on' if value else 'off'}")

    def set_stuck_write(self, tool: int, value: bool) -> None:
        self._tool(tool).stuck_write = value
        self._log("action", f"T{tool} stuck write {'on' if value else 'off'}")

    def set_tool_enabled(self, tool: int, value: bool) -> None:
        t = self._tool(tool, enabled=False)
        if not value:
            self._check_disable(t)
        t.enabled = value
        self._log("action", f"T{tool} {'enabled' if value else 'disabled'}")

    def set_sensors(self, value: bool) -> None:
        self.sensors = value
        self._log("action", f"filament sensors {'on' if value else 'off'}")

    def set_checks(self, value: Severity) -> None:
        self.checks = Severity(value)
        self._log("action", f"preview checks {self.checks}")

    def counter_step_back(self, tool: int, mm: float) -> None:
        t = self._tool(tool)
        t.extruded = max(0.0, t.extruded - _positive(mm, "mm"))
        self._log("action", f"T{tool} extruded stepped back to {int(t.extruded)}")

    def set_extruded_at_unload(self, tool: int, value: int) -> None:
        self._tool(tool).extruded_at_unload = value
        self._log("action", f"T{tool} extruded_at_unload set to {value}")

    # Guards. Each raises Busy or Invalid when its action can't run now. The actions call
    # them, and `actions_json` calls them to tell the page what it can do, so the rules
    # are written once. A printer that's off takes no input except by hand: filament pushed
    # in or pulled out, and a tool picked, enabled or disabled.

    def _check_load(self, t: SimTool, *, keep: bool) -> None:
        waiting = self.job is not None and self.job.wait_tool == t.index
        if keep and not waiting:
            raise Invalid("a load needs a source, tag or preset, outside a runout reload")
        if t.process is not None:
            raise Busy("a load or unload is already running")
        if self.power is Power.OFF:
            return  # pushed in by hand
        if self.state is State.BUSY:
            raise Busy("a load or unload is already running")
        if self.job is not None and not waiting:
            raise Busy("can't load during a print; run the tool out first")

    def _check_unload(self, t: SimTool) -> None:
        if not t.loaded:
            raise Invalid(f"T{t.index} has no filament")
        if t.process is not None:
            raise Busy("a load or unload is already running")
        if self.power is Power.OFF:
            return  # pulled out by hand
        if self.job is not None:
            raise Busy("can't unload during a print; use runout")
        if self.state not in IDLE_STATES:
            raise Busy(f"can't unload while {self.state}")

    def _check_move(self, t: SimTool) -> None:
        self._need_power()
        if self.job is not None or self.state not in IDLE_STATES:
            raise Busy(f"manual moves need an idle printer, not {self.state}")
        if not t.loaded:
            raise Invalid(f"T{t.index} has no filament")

    def _check_select(self, t: SimTool) -> None:
        if self.model.max_tools == 1:
            raise Invalid(f"{self.model.name} has one tool")
        if self.job is not None or self.state is State.BUSY:
            raise Busy(f"can't pick a tool while {self.state}")

    def _check_runout(self, t: SimTool) -> None:
        self._need_power()
        job = self.job
        if not (isinstance(job, UserJob) and job.phase in (JobPhase.PRINTING, JobPhase.PREHEAT)):
            raise Busy("a runout or jam needs a print in progress")
        # Only the picked tool feeds, so only its sensor can see a runout or a jam.
        if t.index != self.selected_tool:
            raise Invalid(f"T{t.index} isn't the selected tool")
        if not t.loaded:
            raise Busy(f"T{t.index} has no filament")

    def _check_sensor_runout(self, t: SimTool) -> None:
        # A runout is the sensor seeing the filament go. A jam is found another way.
        self._check_runout(t)
        if not self.sensors:
            raise Busy("the filament sensors are off")

    def _check_disable(self, t: SimTool) -> None:
        job = self.job
        in_print = isinstance(job, UserJob) and t.index in {s.tool for s in job.spec.segments}
        if t.index == self.selected_tool or in_print or t.process is not None:
            raise Invalid(f"T{t.index} is in use")

    def _check_start_print(self) -> None:
        self._need_power()
        if not self.can_print():
            raise Busy(f"can't print while {self.state}")

    def _check_pause(self) -> None:
        self._need_power()
        if self.job is None or self.job.phase not in (JobPhase.PRINTING, JobPhase.PREHEAT):
            raise Busy("nothing to pause")

    def _check_resume(self) -> None:
        self._need_power()
        if self.job is None or self.job.phase not in (JobPhase.PAUSED, JobPhase.AWAIT_RESUME):
            raise Busy("nothing to resume")

    def _check_cancel(self) -> None:
        self._need_power()
        if self.job is None or self.job.phase is JobPhase.PARKING:
            raise Busy("nothing to cancel")
        if self.job.phase is JobPhase.PREVIEW_LOAD:
            raise Busy("the preview's load is running")

    def _check_dismiss(self) -> None:
        self._need_power()
        if self.job is not None or self.state not in (State.FINISHED, State.STOPPED):
            raise Busy(f"nothing to dismiss in {self.state}")

    def _check_home(self) -> None:
        self._need_power()
        if self.job is not None or self.state not in IDLE_STATES:
            raise Busy(f"can't home in {self.state}")

    def _check_clear_jam(self) -> None:
        self._need_power()
        job = self.job
        if job is None or job.phase is not JobPhase.HOLD or not job.waits_for(Reason.JAM):
            raise Busy("no jam to clear")

    def _check_tap_through(self) -> None:
        self._need_power()
        job = self.job
        if job is None or job.phase is not JobPhase.HOLD or job.waits_for(Reason.JAM):
            raise Busy("nothing to tap through")
        if job.attention.fatal:
            raise Busy("the preview offers only Abort; cancel answers it")

    def actions_json(self) -> dict[str, str | None]:
        """Each printer action, with why it can't run now, or None when it can."""
        checks: dict[str, Callable[[], None]] = {
            "start_print": self._check_start_print,
            "pause": self._check_pause,
            "resume": self._check_resume,
            "cancel": self._check_cancel,
            "dismiss": self._check_dismiss,
            "home": self._check_home,
            "reboot": self._need_power,
            "clear_jam": self._check_clear_jam,
            "tap_through": self._check_tap_through,
        }
        return {name: _refusal(check) for name, check in checks.items()}

    def _tool_actions_json(self, t: SimTool) -> dict[str, str | None]:
        """Each tool action, with why it can't run now, or None when it can."""
        checks: dict[str, Callable[[], None]] = {
            "load": lambda: self._check_load(t, keep=False),
            "keep": lambda: self._check_load(t, keep=True),
            "unload": lambda: self._check_unload(t),
            "extrude": lambda: self._check_move(t),
            "retract": lambda: self._check_move(t),
            "runout": lambda: self._check_sensor_runout(t),
            "jam": lambda: self._check_runout(t),
            "select_tool": lambda: self._check_select(t),
            # The fault switches take any enabled tool, in any state.
            "set_sensor_fault": lambda: None,
            "set_stuck_write": lambda: None,
        }
        if not t.enabled:
            refused = {name: f"tool {t.index} is disabled" for name in checks}
            return {**refused, "set_tool_enabled": None}  # enabling is always allowed
        out = {name: _refusal(check) for name, check in checks.items()}
        return {**out, "set_tool_enabled": _refusal(lambda: self._check_disable(t))}

    # Time.

    def tick(self, dt: float) -> bool:
        """Advance this printer by dt; return whether anything was running."""
        if self.power is Power.OFF or dt <= 0:
            return False
        active = False
        for t in self.tools.values():
            if t.process is not None:
                active = True
                self._tick_process(t, dt)
        if self.job is not None:
            active = True
            self._tick_job(dt)
        self._idle_clear_selected()
        return active

    def _idle_clear_selected(self) -> None:
        # The firmware clears the type whenever it isn't printing and the picked tool's sensor
        # reads empty, so a type written to an empty tool shows only until the job ends.
        # `is_printing()` is false on the tools-mapping screen too (`marlin_server.cpp`).
        t = self.tools.get(self.selected_tool)
        job = self.job
        idle = job is None and self.state in IDLE_STATES
        mapping = job is not None and any(job.waits_for(r) for r in MAPPING_QUESTIONS)
        if (idle or mapping) and t is not None and t.process is None:
            self._idle_sensor_clear(t, sensor_fault_counts=True)

    def _tick_process(self, t: SimTool, dt: float) -> None:
        rate = self.settings.load_rate_mm_s
        while t.process is not None and dt > EPSILON:
            p = t.process
            if isinstance(p, UnloadProcess):
                self._unload_tick(t, p, dt)
                return
            dt -= self._move(t, p.steps, rate * dt) / rate
            self._next_load_step(t, p)

    def _unload_tick(self, t: SimTool, p: UnloadProcess, dt: float) -> None:
        """Move the unload's share of its steps for dt; record the unload when time is up."""
        left = sum(abs(st.mm) for st in p.steps if st.phase not in MARKERS)
        most = left * min(dt / p.remaining, 1.0) if p.remaining > EPSILON else left
        p.remaining = max(p.remaining - dt, 0.0)
        while p.steps and most > EPSILON:
            most -= self._move(t, p.steps, most)
            self._settle(t, p.steps, None)
        if p.remaining > EPSILON:
            return
        self._run_steps(t, p.steps)  # what rounding left
        t.process = None
        self._physical_unload(t, "unload")
        self._process_done()

    def _next_load_step(self, t: SimTool, p: LoadProcess) -> None:
        """Act on the markers that come up, and end the load after its last step."""
        self._settle(t, p.steps, p.material)
        if not p.steps:
            t.loaded, t.process = True, None
            self._log("load", f"T{t.index} loaded: extruded {int(t.extruded)}")
            self._process_done()

    def _process_done(self) -> None:
        job = self.job
        if job is not None and job.phase is JobPhase.WAIT_LOAD:
            job.enter(JobPhase.PAUSED)
        elif job is not None and job.phase is JobPhase.PREVIEW_LOAD:
            # The load's result isn't checked and no sensor is read again: the type check
            # follows (`filament_not_inserted_load` in `marlin_print_preview.cpp`).
            self._check_types(job)
        self._show_state()

    def _tick_job(self, dt: float) -> None:
        while dt > EPSILON and self.job is not None:
            job = self.job
            if job.phase is JobPhase.PRINTING:
                dt = self._print_step(job, dt)
            elif job.phase in TIMED_PHASES:
                used = min(job.remaining, dt)
                job.remaining -= used
                dt -= used
                if job.phase is not JobPhase.BLIP:
                    job.time_printing += used
                if (
                    job.phase is JobPhase.RECOVER
                    and job.clean_at is not None
                    and job.remaining <= job.clean_at + EPSILON
                ):
                    job.clean_at = None
                    self._nozzle_clean(job)
                if job.remaining <= EPSILON:
                    self._end_phase(job)
            else:
                return  # paused or waiting for the user

    def _end_phase(self, job: UserJob | SyncJob) -> None:
        if job.phase is JobPhase.SCANNING:
            if isinstance(job, UserJob):
                self._ask_print(job)
            elif not self._ask(job):
                self._begin(job)
        elif job.phase is JobPhase.BLIP:
            self._after_blip(job)
        elif job.phase is JobPhase.PREHEAT:
            self._begin_feeding(job)
        elif job.phase is JobPhase.RUN:
            self._park(job, State.FINISHED)
        elif job.phase is JobPhase.RECOVER:
            if job.cold_bed:
                job.enter(JobPhase.AWAIT_RESUME, Attention(Reason.COLD_BED))
                self._show_state()
            else:
                self._unpark(job)
        elif job.phase is JobPhase.UNPARK:
            self._begin_feeding(job)
        else:  # PARKING, the last of the timed phases
            self.job, self.screen = None, job.end_state
            self._log("job", f"job {job.id} {job.end_state.lower()}")
            self._show_state()

    def _begin(self, job: UserJob | SyncJob) -> None:
        """Start the job the preview let through: the blip, then the job itself."""
        if isinstance(job, UserJob):
            job.previewing, job.mapping = False, []
        if self.settings.start_blip:
            job.enter(JobPhase.BLIP)
            job.remaining = self.settings.blip_seconds
            self._show_state()
        else:
            self._after_blip(job)

    def _ask(self, job: SyncJob) -> bool:
        """Check the file as the preview does; return whether it asks the user."""
        rows = preview(self.model, job.gcode, self.checks)
        if not rows:
            return False
        fatal = any(r.fatal for r in rows)
        detail = "; ".join(r.text for r in rows)
        job.enter(JobPhase.HOLD, Attention(Reason.PREVIEW, detail=detail, fatal=fatal))
        self._log("job", f"job {job.id} waits: {job.attention}")
        self._show_state()
        return True

    # The preview of a print with segments: the chain of `PrintPreview::Loop` in
    # `src/common/marlin_print_preview.cpp`, each question its own wait.

    def _uses(self, job: UserJob) -> dict[int, str | None]:
        return {s.tool: s.material for s in job.spec.segments}

    def _tool_views(self) -> dict[int, ToolView]:
        # A sensor that's off reads neither filament nor none, so no check fails on it.
        seen = {i: t.loaded or not self.sensors for i, t in self.tools.items()}
        return {
            i: ToolView(t.enabled, seen[i], t.material.name if t.material else None)
            for i, t in self.tools.items()
        }

    def _preview_question(self, job: UserJob, reason: Reason, rows: list[Failed]) -> None:
        self._wait(job, _question(reason, rows))

    def _wait(self, job: UserJob, attention: Attention) -> None:
        job.enter(JobPhase.HOLD, attention)
        self._log("job", f"job {job.id} waits: {job.attention}")
        self._show_state()

    def _ask_print(self, job: UserJob) -> None:
        """The "G-code incompatible" screen (`stateFromPrinterCheck`), then the rest."""
        rows = print_checks(self.model.max_tools, self._uses(job), self._tool_views()).rows
        if rows:
            self._preview_question(job, Reason.PREVIEW, rows)
        else:
            self._after_incompatible(job)

    def _after_incompatible(self, job: UserJob) -> None:
        """The tools-mapping screen on a toolchanger, else the filament-not-inserted screen
        (`stateFromFilamentPresence` and `checks_done`).

        A print started at the printer shows the tools-mapping screen whenever tool mapping
        applies, with nothing wrong too: only a remote start that is fully compatible skips
        it (`checks_done` in `src/common/marlin_print_preview.cpp`).
        """
        checks = print_checks(self.model.max_tools, self._uses(job), self._tool_views())
        if checks.mapped:
            job.mapping = self._mapping_report(job, checks.mapping)
            self._wait(job, job.mapping[0])
        elif checks.empty:
            job.enter(JobPhase.WAIT_LOAD, Attention(Reason.NO_FILAMENT, checks.empty[0]))
            self._log("job", f"job {job.id} waits: {job.attention}")
            self._show_state()
        else:
            self._check_types(job)

    def _mapping_report(self, job: UserJob, rows: list[Failed]) -> list[Attention]:
        """The tools-mapping screen, then the questions its Print asks.

        Print confirms every failed check, each kind once for all the tools, in the order of
        `VirtualToolCheck` in `src/feature/compatibility_checks/gcode_compatibility.hpp`: a
        used tool with no filament asks to turn the sensors off, then a used tool holding
        another type asks Cancel or Ignore. Cancel at either goes back to the screen, with the
        sensors as they are. The answers come from the report the screen made when it
        opened, which only a remap renews, so turning the sensors off doesn't clear its
        filament row. A disabled used tool is fatal: Print shows only an error whose Cancel
        returns to the screen, and with no remap modeled only Abort leaves it.
        (`on_button_click` and `update_mapping` in
        `src/gui/screen/print/frame_tool_mapping.cpp`, `gui_confirm_all_incompatibilities`
        in `compatibility_checks_common.hpp`, `visit_failed_checks` and
        `gui_confirm_incompatibility` in `gcode_compatibility.cpp`.)
        """
        uses, views = self._uses(job), self._tool_views()
        empty = [i for i in sorted(uses) if not views[i].loaded]
        asks = [
            (Reason.SENSORS_OFF, [Failed(f"T{i} has no filament", False) for i in empty]),
            (Reason.MAPPING_TYPE, wrong_types(uses, views)),
        ]
        screen = _question(Reason.TOOLS_MAPPING, rows)
        return [screen, *(_question(reason, rows) for reason, rows in asks if rows)]

    def _check_types(self, job: UserJob) -> None:
        """The wrong-filament screen (`stateFromFilamentType`), which a printer with tool
        mapping leaves to its tools-mapping screen."""
        uses, views = self._uses(job), self._tool_views()
        rows = [] if tool_mapping(self.model.max_tools, uses, views) else wrong_types(uses, views)
        if rows:
            self._preview_question(job, Reason.WRONG_FILAMENT, rows)
        else:
            self._begin(job)

    def _after_blip(self, job: UserJob | SyncJob) -> None:
        if isinstance(job, SyncJob):
            self._run_sync(job)
            return
        spec = job.spec
        job.enter(JobPhase.PREHEAT)
        job.remaining = self.settings.preheat_seconds if spec.preheat_s is None else spec.preheat_s
        self.selected_tool = spec.segments[0].tool
        self._show_state()

    def _begin_feeding(self, job: UserJob) -> None:
        self.selected_tool = job.spec.segments[job.segment].tool
        job.enter(JobPhase.PRINTING)
        self._show_state()

    def _park(self, job: UserJob | SyncJob, end: State) -> None:
        self._end_of_print()
        job.enter(JobPhase.PARKING)
        job.remaining, job.end_state = self.settings.park_seconds, end
        if isinstance(job, UserJob):
            job.paused_from = None
        self._show_state()

    def _end_of_print(self) -> None:
        """What a finished or stopped job does to the filament before it parks, a sync job
        too (`marlin_server.cpp`).

        It retracts the picked tool to 8 mm (`retract_and_lift`). Then `pre_finalize_print`
        stores each tool's valid tracker gap as its auto-retracted distance and
        auto-retracts the picked tool, which INDX skips: its 8 mm already counts as fully
        retracted.
        """
        ex = self.model.extruder
        t = self.tools[self.selected_tool]
        self._retract_to(t, ex.lengths.retract_mm)
        if not ex.auto_retracts:
            return
        for tool in self.tools.values():
            if tool.gap_valid:
                tool.auto_retracted = tool.gap
        self._run_steps(t, [LoadStep(AUTO_RETRACT_STEP)])

    def _print_step(self, job: UserJob, dt: float) -> float:
        """Feed for dt, stopping at each segment end and color change. Return the time left."""
        spec = job.spec
        total = spec.total_mm
        rate = total / spec.duration_s if spec.duration_s else self.settings.print_rate_mm_s
        ends = [sum(s.slicer_mm for s in spec.segments[: i + 1]) for i in range(len(spec.segments))]
        while dt > EPSILON and job.phase is JobPhase.PRINTING:
            tool = self.tools[spec.segments[job.segment].tool]
            change = job.pending_color_changes[0] if job.pending_color_changes else None
            target = ends[job.segment] if change is None else min(change, ends[job.segment])
            need = max(0.0, target - job.fed_mm)
            if rate * dt < need:
                self._track(tool, rate * dt)
                job.fed_mm += rate * dt
                job.time_printing += dt
                return 0.0
            self._track(tool, need)
            job.fed_mm = target
            dt -= need / rate
            job.time_printing += need / rate
            if change is not None and change <= target:
                job.pending_color_changes.pop(0)
                self._log("job", f"color change at {change:g} mm")
                self._runout(tool, Reason.COLOR_CHANGE)
                return 0.0
            if job.segment == len(spec.segments) - 1:
                self._park(job, State.FINISHED)
                return dt
            self._next_segment(job)
        return 0.0 if job.phase is not JobPhase.PRINTING else dt

    def _next_segment(self, job: UserJob) -> None:
        segments = job.spec.segments
        old = segments[job.segment].tool
        job.segment += 1
        new = segments[job.segment].tool
        if new == old:
            return
        self.selected_tool = new
        self._log("job", f"tool change T{old} -> T{new}")
        # Nothing checks for filament at a pickup, and a runout needs the sensor to go from
        # filament to none, so an empty tool prints on and counts what it would have fed
        # (`check_runout` in `src/feature/filament_sensor/filament_sensors_handler.cpp`,
        # `check_for_events` in `filament_sensor.cpp`).
        self._track(self.tools[new], self.settings.toolchange_purge_mm)

    # The sync job.

    def _run_sync(self, job: SyncJob) -> None:
        try:
            commands = gcode.parse(job.gcode)
        except gcode.GcodeError as e:
            self._hold(job, str(e))
            return
        problem = sync.tool_problem(self, commands)
        if problem:
            self._hold(job, problem)
            return
        self._run_commands(job, commands)

    def _hold(self, job: SyncJob, problem: str) -> None:
        job.enter(JobPhase.HOLD, Attention(Reason.PARSE_ERROR, detail=problem))
        self._log("job", f"job {job.id} stopped: {job.attention}")
        self._show_state()

    def _run_commands(self, job: SyncJob, commands: list[gcode.Command]) -> None:
        sync.apply(self, commands)
        job.enter(JobPhase.RUN)
        job.remaining = job.run_seconds = self.settings.sync_job_seconds
        self._show_state()

    # The page's view.

    def to_json(self) -> dict:
        m = self.model
        job = None
        if self.job is not None:
            j = self.job
            a = j.attention
            user = isinstance(j, UserJob)
            job = {
                "id": j.id,
                "kind": j.kind,
                "name": j.name,
                "phase": str(j.phase),
                "progress": j.progress(),
                "fed_mm": j.fed_mm if user else 0.0,
                "total_mm": j.spec.total_mm if user else 0.0,
                "attention": None
                if a is None
                else {"reason": str(a.reason), "tool": a.tool, "text": str(a), "fatal": a.fatal},
                "time_printing": j.time_printing,
                "segment": j.segment if user else 0,
            }
        return {
            "name": self.name,
            "port": self.port,
            "api_key": self.api_key,
            "model": {
                "code": m.code,
                "name": m.name,
                "uuids": m.uuids,
                "gap_mm": m.gap_mm,
                "max_tools": m.max_tools,
                # What an unset override falls back to on this printer.
                "feed_ratio": m.extruder.feed_ratio,
                "auto_retracts": m.extruder.auto_retracts,
                "auto_retract_switchable": m.extruder.auto_retract_switchable,
                **dict(zip(LOAD_LENGTHS, self._load_lengths(), strict=True)),
            },
            "high_flow": self.high_flow,
            "auto_retract": self.auto_retract,
            "firmware": str(self.firmware),
            "power": str(self.power),
            "fault": str(self.fault),
            "boot_id": self.boot_id,
            "state": str(self.state),
            "selected_tool": self.selected_tool,
            "message": self.message,
            "homed": self.homed,
            "job": job,
            "job_counter": self.job_counter,
            "checks": str(self.checks),
            "sensors": self.sensors,
            "actions": self.actions_json(),
            "load_sources": ["tag", "preset"] if m.uuids else ["preset"],
            "tools": [
                {**_tool_json(t), "actions": self._tool_actions_json(t)}
                for t in sorted(self.tools.values(), key=lambda t: t.index)
            ],
        }


def _setting_kind(default: Any) -> str:
    if isinstance(default, bool):
        return "bool"
    return "number" if default is not None else "optional"  # a number, or null for the model's


def _question(reason: Reason, rows: list[Failed]) -> Attention:
    detail = "; ".join(r.text for r in rows) or None
    return Attention(reason, detail=detail, fatal=any(r.fatal for r in rows))


def _refusal(check: Callable[[], None]) -> str | None:
    try:
        check()
    except (Busy, Invalid) as e:
        return str(e)
    return None


def _tool_json(t: SimTool) -> dict:
    out = asdict(t)
    p = t.process
    if p is None:
        out["process"] = None
    elif isinstance(p, LoadProcess):
        out["process"] = {"kind": "load", "phase": p.phase, "remaining_mm": p.remaining}
    else:
        out["process"] = {"kind": "unload", "phase": p.phase, "remaining_s": p.remaining}
    return out


# Snapshots: asdict() out, explicit reconstruction in.


def _material_from(d: dict | None) -> Material | None:
    return None if d is None else Material(**d)


def _process_from(d: dict | None) -> LoadProcess | UnloadProcess | None:
    if d is None:
        return None
    d = dict(d)
    kind = d.pop("kind")
    d["steps"] = [LoadStep(**st) for st in d["steps"]]
    if kind == "unload":
        return UnloadProcess(**d)
    d["material"] = _material_from(d["material"])
    return LoadProcess(**d)


def _tool_from(d: dict) -> SimTool:
    d = dict(d)
    d["material"] = _material_from(d["material"])
    d["previous"] = _material_from(d["previous"])
    d["kept_type"] = _material_from(d["kept_type"])
    d["process"] = _process_from(d["process"])
    return SimTool(**d)


def _attention_from(a: dict) -> Attention:
    return Attention(Reason(a["reason"]), a["tool"], a["detail"], a["fatal"])


def _job_from(d: dict | None) -> UserJob | SyncJob | None:
    if d is None:
        return None
    d = dict(d)
    kind = d.pop("kind")
    d["phase"] = JobPhase(d["phase"])
    d["end_state"] = State(d["end_state"])
    if (a := d["attention"]) is not None:
        d["attention"] = _attention_from(a)
    if kind == "sync":
        return SyncJob(**d)
    d["mapping"] = [_attention_from(a) for a in d["mapping"]]
    if d["paused_from"] is not None:
        d["paused_from"] = JobPhase(d["paused_from"])
    spec = dict(d["spec"])
    spec["segments"] = [Segment(**s) for s in spec["segments"]]
    d["spec"] = PrintSpec(**spec)
    return UserJob(**d)


def _printer_dict(p: SimPrinter) -> dict:
    d = asdict(p)
    d["model"] = p.model.code
    d["tools"] = [d["tools"][i] for i in sorted(d["tools"])]
    if p.job is not None:
        d["job"]["kind"] = p.job.kind
    return d


def _printer_from(d: dict) -> SimPrinter:
    d = dict(d)
    d["model"] = MODELS[d["model"]]
    d["firmware"] = Firmware(d["firmware"])
    d["power"] = Power(d["power"])
    d["fault"] = Fault(d["fault"])
    d["screen"] = State(d["screen"])
    d["checks"] = Severity(d["checks"])
    d["tools"] = {t["index"]: _tool_from(t) for t in d["tools"]}
    d["job"] = _job_from(d["job"])
    return SimPrinter(**d)


class Simulation:
    def __init__(
        self,
        settings: Settings | None = None,
        printers: list[SimPrinter] | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.printers: dict[str, SimPrinter] = {}
        self.events: list[Event] = []
        self.now = 0.0
        self.data_dir = Path(data_dir) if data_dir else None
        self.listeners: list[Callable[[Event], None]] = []  # called with each event
        self.watchers: list[Callable[[], None]] = []  # called when a tick changed something
        for p in printers or []:
            self.add(p)

    def add(self, printer: SimPrinter) -> None:
        printer._sim = self
        self.printers[printer.name] = printer

    def printer(self, name: str) -> SimPrinter:
        return self.printers[name]

    def log(self, printer: str | None, kind: str, text: str, data: dict | None = None) -> Event:
        event = Event(round(self.now, 3), printer, kind, text, data)
        self.events.append(event)
        del self.events[:-EVENT_LIMIT]
        for listener in self.listeners:
            listener(event)
        return event

    def tick(self, dt: float) -> None:
        if dt <= 0:
            return
        self.now += dt
        active = [p.tick(dt) for p in self.printers.values()]
        if any(active):
            for watcher in self.watchers:
                watcher()

    def advance(self, seconds: float, step: float = 0.1) -> None:
        """Tick through `seconds` in the clock task's steps, so phases end in order."""
        done = 0.0
        while done < seconds - 1e-9:
            dt = min(step, seconds - done)
            self.tick(dt)
            done += dt

    def snapshot(self) -> dict:
        return {
            "now": self.now,
            "settings": asdict(self.settings),
            "printers": [_printer_dict(p) for p in self.printers.values()],
        }

    def restore(self, data: dict) -> None:
        """Replace the model with a snapshot of the same printers (names and ports).

        Every printer gets a fresh boot_id, as if it rebooted. The snapshot's counters can be
        lower than the ones the bridge last read, and a counter that goes backwards under the
        same boot_id is something the bridge treats as impossible. The job counter keeps the
        higher of the current and the snapshot's, so the next job's ID is new to the bridge.
        """
        try:
            printers = [_printer_from(d) for d in data["printers"]]
            settings = Settings(**data["settings"])
            now = float(data["now"])
        except (KeyError, TypeError, ValueError, Invalid) as e:
            raise Invalid(f"bad snapshot: {e}") from None
        if self.printers:
            ours = {(p.name, p.port) for p in self.printers.values()}
            if {(p.name, p.port) for p in printers} != ours:
                raise Invalid("the snapshot is for other printers or ports")
        counters = {p.name: p.job_counter for p in self.printers.values()}
        self.settings, self.now, self.printers = settings, now, {}
        for p in printers:
            p.boot_id = new_boot_id()
            p.job_counter = max(p.job_counter, counters.get(p.name, 0))
            self.add(p)
        self.log(None, "action", "restored a snapshot")

    def snapshot_names(self) -> list[str]:
        if self.data_dir is None or not self.data_dir.is_dir():
            return []
        return sorted(p.stem for p in self.data_dir.glob("*.json") if SNAPSHOT_NAME.match(p.stem))

    def to_json(self, *, events: bool = True) -> dict:
        out: dict[str, Any] = {
            "now": self.now,
            "settings": asdict(self.settings),
            "printers": [p.to_json() for p in self.printers.values()],
            "snapshots": self.snapshot_names(),
            # What the page's forms offer, so it copies none of the model's lists.
            "setting_fields": [
                {"name": f.name, "kind": _setting_kind(f.default)} for f in fields(Settings)
            ],
            "options": {
                "power": [str(v) for v in Power],
                "fault": [str(v) for v in Fault],
                "firmware": [str(v) for v in Firmware],
                "checks": [str(v) for v in Severity],
            },
        }
        if events:
            out["events"] = [asdict(e) for e in self.events[-200:]]
        return out
