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
from simulator.feed import FEED_SYSTEMS, MMU, Feed, FeedSystem
from simulator.mmu import MMU_SLOTS, MmuUnit
from simulator.preview import (
    Failed,
    Severity,
    ToolView,
    preview,
    print_checks,
    tool_mapping,
    wrong_types,
)
from simulator.slicer import INDX_SLICER, XL_SLICER, Moves, Slicer
from simulator.tool import (
    AUTO_RETRACT_STEP,
    CORE_ONE_EXTRUDER,
    CORE_ONE_PRESETS,
    DEFAULT_TEMPERATURES,
    DERETRACT_STEP,
    EPSILON,
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
    FilamentPath,
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
    RECOVER = "RECOVER"  # BUSY while a power-panic resume heats and homes
    AWAIT_RESUME = "AWAIT_RESUME"  # ATTENTION: the bed went cold during the outage
    UNPARK = "UNPARK"  # PAUSED briefly while the head unparks after a power-panic resume
    PREVIEW_LOAD = "PREVIEW_LOAD"  # BUSY with no job: the preview's filament-not-inserted load
    JOIN = "JOIN"  # ATTENTION while a spool join unloads the old tool, then back to PRINTING


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
    # The join's unload runs in a `Load_unload` dialog while printing, which reads ATTENTION
    # (`get_state` in `src/state/printer_state.cpp:238-243`).
    JobPhase.JOIN: State.ATTENTION,
}
TIMED_PHASES = (
    JobPhase.SCANNING,
    JobPhase.BLIP,
    JobPhase.PREHEAT,
    JobPhase.RUN,
    JobPhase.PARKING,
    JobPhase.RECOVER,
    JobPhase.UNPARK,
    JobPhase.JOIN,
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
    # Whether the build resumes a print after a power cut: HAS_POWER_PANIC in
    # `ProjectOptions.cmake`. Without it, a cut loses the print.
    power_panic: bool = True
    # Its `compatibility_group` in `printer_model_data.hpp`. The MK3.5, MK4 and XL builds
    # run several models and check a file against the one the printer is set to
    # (`PrinterModelInfo::current`), so each model has its own entry's group.
    group: str = field(kw_only=True)
    checks: GCodeChecks = field(kw_only=True)  # its build's
    # Its feed system: XL on HAS_TOOLCHANGER builds, INDX on HAS_INDX ones, else direct.
    feed: Feed = field(default=Feed.DIRECT, kw_only=True)
    # The stock PrusaSlicer profile's moves outside the model on a toolchanger, which a
    # print can override; None on a direct printer. Not firmware, so `script/firmware-check`
    # doesn't compare it.
    slicer: Slicer | None = field(default=None, kw_only=True)
    # Whether a printer of this model can have an MMU3 attached (`SimPrinter.mmu`): the
    # models with a stock MMU3 slicer profile. Of the other HAS_MMU2 builds' models, the
    # MK3.5's have no usage counters, and the firmware says "MMU is not expected on Oak"
    # (`include/common/printer_model_data.hpp:227-233`). Not firmware, so
    # `script/firmware-check` doesn't compare it.
    takes_mmu: bool = field(default=False, kw_only=True)

    @property
    def m865_tools(self) -> int:
        """How many tools `M865 I` and `L` take: every slot on an MMU build, the MMU on or
        off, else the build's tools (`VirtualToolIndex::count`, `M865.cpp:64, 194-196`)."""
        return MMU_SLOTS if self.mmu else self.max_tools

    def first_tool(self, enabled: int) -> int | None:
        """The tool picked when the config names none, on a printer with `enabled` tools
        enabled: none on a toolchanger, whose tools wait docked or parked, and tool 0
        elsewhere. An XL with one tool isn't one: its toolchanger is off and the tool stays
        on the head (`autodetect_toolchanger_enabled` in
        `src/module/prusa/toolchanger_utils_xl.cpp`)."""
        changes = self.feed is Feed.INDX or (self.feed is Feed.XL and enabled > 1)
        return None if changes else 0

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
        "1.3.9",
        "MK3.9",
        MK4_EXTRUDER,
        LOAD_CELL_PRESETS,
        mmu=True,
        group="mk4",
        checks=MK4_CHECKS,
        takes_mmu=True,
    ),
    ModelInfo(
        "1.3.10",
        "MK3.9S",
        MK4_EXTRUDER,
        LOAD_CELL_PRESETS,
        mmu=True,
        group="mk4s",
        checks=MK4_CHECKS,
        takes_mmu=True,
    ),
    ModelInfo(
        "1.4.0",
        "MK4",
        MK4_EXTRUDER,
        LOAD_CELL_PRESETS,
        mmu=True,
        group="mk4",
        checks=MK4_CHECKS,
        takes_mmu=True,
    ),
    ModelInfo(
        "1.4.1",
        "MK4S",
        MK4_EXTRUDER,
        LOAD_CELL_PRESETS,
        mmu=True,
        group="mk4s",
        checks=MK4_CHECKS,
        takes_mmu=True,
    ),
    ModelInfo(
        "2.1.0",
        "MINI",
        MINI_EXTRUDER,
        MINI_PRESETS,
        counters=False,
        power_panic=False,
        group="mini",
        checks=PLAIN_CHECKS,
    ),
    ModelInfo(
        "3.1.0",
        "XL",
        XL_EXTRUDER,
        LOAD_CELL_PRESETS,
        max_tools=5,
        group="xl",
        checks=XL_CHECKS,
        feed=Feed.XL,
        slicer=XL_SLICER,
    ),
    ModelInfo(
        "3.1.1",
        "XLP",
        XL_EXTRUDER,
        LOAD_CELL_PRESETS,
        max_tools=5,
        group="xls",
        checks=XL_CHECKS,
        feed=Feed.XL,
        slicer=XL_SLICER,
    ),
    ModelInfo(
        "5.1.0",
        "XL",
        XL_DEV_KIT_EXTRUDER,
        LOAD_CELL_PRESETS,
        max_tools=5,
        counters=False,
        power_panic=False,
        group="xl",
        checks=PLAIN_CHECKS,
        feed=Feed.XL,
        slicer=XL_SLICER,
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
        takes_mmu=True,
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
        takes_mmu=True,
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
        feed=Feed.INDX,
        slicer=INDX_SLICER,
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
        feed=Feed.INDX,
        slicer=INDX_SLICER,
    ),
)


# The load's lengths a setting can override, in the order `_load_lengths` returns them.
LOAD_LENGTHS = ("slow_load_mm", "fast_load_mm", "purge_mm")
POSITIVE_SETTINGS = frozenset({"time_scale", "load_rate_mm_s", "print_rate_mm_s"})


PANIC_RETRY_SECONDS = 1.5  # `rescan_delay` in `src/common/print_utils.cpp`


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
    jam_purge_mm: float = 60.0
    preheat_seconds: float = 5.0
    scan_seconds: float = 0.5
    blip_seconds: float = 0.3
    park_seconds: float = 1.0
    sync_job_seconds: float = 3.0
    # IDLE with no job after a power-panic boot, until the autostart resumes the print: it
    # tries every 1.5 s (`rescan_delay` in `print_utils_loop`, `src/common/print_utils.cpp`).
    panic_idle_seconds: float = PANIC_RETRY_SECONDS
    recover_seconds: float = 2.0  # BUSY while a power-panic resume heats and homes
    unpark_seconds: float = 1.0  # PAUSED while the head unparks on a power-panic resume
    # How long an MMU takes to become Active after a boot or `M709 S1`, inferred from the
    # "~10s" in `filament_sensors_handler.cpp:179`. Until then moves count on slot 0.
    mmu_connect_seconds: float = 10.0
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
        # The autostart's first try comes `rescan_delay` after the boot, never sooner
        # (`print_utils_loop` in `src/common/print_utils.cpp`).
        if self.panic_idle_seconds < PANIC_RETRY_SECONDS:
            raise Invalid(f"panic_idle_seconds must be at least {PANIC_RETRY_SECONDS:g}")

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
class Join:
    """A spool join: when `tool` runs out, the print goes on with `to`."""

    tool: int
    to: int


@dataclass
class PrintSpec:
    segments: list[Segment]
    duration_s: float | None = None
    preheat_s: float | None = None
    color_changes_mm: list[float] = field(default_factory=list)
    name: str = "part.bgcode"
    # A toolchanger print's own prime and tool-change purge, in place of the stock
    # profile's (`Slicer.overridden`); None keeps the profile's.
    prime_mm: float | None = None
    toolchange_purge_mm: float | None = None
    # The print's spool joins, set at the tools-mapping screen, by Connect or by `M864` and
    # reset when the print ends (`marlin_server.cpp:1122`), so a print carries them.
    spool_join: list[Join] = field(default_factory=list)

    @property
    def total_mm(self) -> float:
        return sum(s.slicer_mm for s in self.segments)


@dataclass
class Queued:
    """One operation a change all has still to run: the unload of the tool's old filament,
    or the load of `material`."""

    tool: int
    kind: str  # "unload" or "load"
    material: Material | None = None


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
    clean_at: float | None = None  # RECOVER time left when the nozzle clean purges
    # The tool of a filament change and its gap when the change began, which the resume
    # restores. A spool join keeps the join's tool and the old tool's gap here, which the
    # join restores on the new tool at its end.
    restore: tuple[int, float] | None = None
    # Whether the pause went through the firmware's pause states, whose resume unparks and,
    # on INDX, purges (`unpark_prime`), and which a power cut keeps (`was_paused`). A jam
    # cleared in place didn't: it's a filament change dialog, which parks nothing
    # (`Pause::filament_change`).
    parked: bool = False
    previewing: bool = True  # until the preview lets the print begin
    # Whether the start G-code has run its moves, so a resume doesn't run them again.
    primed: bool = False
    # The tool mapping a spool join changed: a G-code tool to the tool that prints it now.
    # A G-code tool not listed prints on its own tool.
    mapped: dict[int, int] = field(default_factory=dict)
    # The tool a power-panic resume prints the current segment on when the cut found another
    # tool picked than the segment's: during a filament change on another tool, or a spool
    # join's unload. The resume issues no tool change, so that tool prints until the file's
    # next one, which ends this (`power_panic.cpp:380-395, 1049-1055`).
    resumed_on: int | None = None
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
    # The print a power cut stopped, held with no job until the autostart resumes it
    # `resume_in` seconds from now. Its phase says how it comes back: RECOVER, PAUSED, or
    # AWAIT_RESUME for a cold bed.
    resuming: UserJob | None = None
    resume_in: float = 0.0
    job_counter: int = 0
    # The picked tool, or None for a toolchanger with no tool picked: then no filament moves
    # and the idle sensor clear doesn't run (`SimPrinter.feed`). INDX docks its tool after a
    # menu load or unload, and `park_tool` parks it on either toolchanger.
    selected_tool: int | None = 0
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
    # What a change all has still to run after the running load or unload, in order.
    queue: list[Queued] = field(default_factory=list)
    # An MMU3 attached, on a model that takes one. Then the printer has a tool per slot,
    # each always enabled, and every slot's path is the unit's one path. A configured
    # tool's loaded filament is only its slot's stored type: the unit says what's in the
    # path, and `selected_tool` is the slot selected.
    mmu: MmuUnit | None = None

    def __post_init__(self) -> None:
        # Not fields, so asdict() and snapshots leave them out.
        self._sim: Simulation | None = None
        if self.mmu is not None:
            for i in range(MMU_SLOTS):
                tool = self.tools.setdefault(i, SimTool(i))
                tool.path = self.mmu.path
        self._shown = self.state  # the state last logged

    @property
    def state(self) -> State:
        """The state PrusaLink reports, which follows from the job phase or the idle screen."""
        if self.job is not None:
            return PHASE_STATES[self.job.phase]
        if any(t.process is not None for t in self.tools.values()):
            return State.BUSY
        return self.screen

    @property
    def toolchanger(self) -> bool:
        """Whether the printer changes tools. An XL with one tool enabled doesn't: the
        firmware turns its toolchanger off, keeps that tool on the head and refuses a
        change to any other, and its stock profile's end G-code has no park
        (`autodetect_toolchanger_enabled` in `toolchanger_utils_xl.cpp`, `tool_change` in
        `toolchanger_xl.cpp`). INDX always docks."""
        enabled = sum(self.listed(t) for t in self.tools.values())
        return self.feed.selectable and (self.feed.docks or enabled > 1)

    @property
    def feed(self) -> FeedSystem:
        """The rules of the printer's feed system: the MMU's while one is switched on, else
        the model's."""
        return MMU if self.mmu and self.mmu.on else FEED_SYSTEMS[self.model.feed]

    def listed(self, t: SimTool) -> bool:
        """Whether `/api/v1/filament` lists `t`: an enabled tool whose slot is on."""
        return t.enabled and self._slot_on(t.index)

    def _slot_on(self, index: int) -> bool:
        """Whether the build has tool `index` now (`VirtualToolIndex::is_enabled`,
        `src/common/tool_index.cpp:84-109`). An MMU build has every slot while the MMU is
        switched on, connecting too, and slot 0 alone while it's off or there's no MMU, though
        `M865` still writes the other slots."""
        return index == 0 or not self.model.mmu or (self.mmu is not None and self.mmu.on)

    def _head_tool(self) -> SimTool | None:
        """The tool whose path the head's own moves run on, as the feed system says."""
        return self.feed.head_tool(self)

    # Plumbing.

    @property
    def settings(self) -> Settings:
        return self._sim.settings if self._sim else Settings()

    @property
    def feed_ratio(self) -> float:
        """How much an E move feeds per mm commanded: the setting, else the model's."""
        ratio = self.settings.feed_ratio
        return self.model.extruder.feed_ratio if ratio is None else ratio

    def _track(self, tool: SimTool, mm: float, *, printing: bool = False) -> None:
        """Move `tool`'s filament by `mm` commanded, signed, as the firmware's planner does.

        A feed first feeds back an auto-retracted distance, and a retract forgets it: the
        auto-retract hook in `Planner::buffer_segment`
        (`lib/Marlin/Marlin/src/module/planner.cpp:2223-2240`). A `printing` move, a print
        file's `G0` or `G1`, that retracts while a distance is stored doesn't move the
        filament and keeps the distance, though the tracker has counted it. Every move the
        simulator makes goes through here.

        Every caller picks `tool` first, as the firmware does: a toolchanger's E move with
        no tool picked is a `bsod("E move without tool")` in the same function.
        """
        if mm > 0:
            self._deretract(tool)
        elif mm < 0 and not (printing and tool.path.auto_retracted):
            tool.path.auto_retracted = None
        self._count(tool, mm)

    def _print_moves(self, tool: SimTool, moves: Moves) -> None:
        """Run a slicer profile's moves on `tool`, the picked tool, as the print's own."""
        for mm in moves:
            self._track(tool, mm, printing=True)

    def _count(self, tool: SimTool, mm: float) -> None:
        """Count a move as the firmware's filament tracker does: only what goes past the
        model's gap counts, times the feed ratio (`FilamentPath.move`). The feed system says
        which tool it counts on.

        On an MMU with no slot credited the move runs, but the tracker sees none of it, so
        the gap stays where it was (`if(tools.virtual_tool.has_value())` in
        `Planner::buffer_segment`, `planner.cpp:2206-2219`).
        """
        credited = self.feed.credited(self, tool)
        if credited is None:
            # On a toolchanger every caller picks the tool first: the planner stops on this.
            assert self.feed.moves_without_tool, "E move without tool"
            return
        credited.extruded += tool.path.move(mm, self.model.gap_mm) * self.feed_ratio

    def _deretract(self, tool: SimTool) -> None:
        """Feed back an auto-retracted distance (`maybe_deretract_to_nozzle`).

        On a Nextruder that's 55 mm into a gap that stops at 40, so 15 mm count that never
        left the nozzle.
        """
        stored = tool.path.auto_retracted
        if stored:
            tool.path.auto_retracted = 0.0
            self._count(tool, stored)
            self._log("load", f"T{tool.index} deretracted {stored:g} mm")

    def _retract_to(self, tool: SimTool, target: float) -> None:
        """Retract until the filament is `target` mm back, unless it already is
        (`mapi::retract_to` in `src/common/mapi/motion.cpp`)."""
        stored = tool.path.auto_retracted or 0.0
        if stored >= target:
            return
        # A stored distance counts as retracted; the retract then forgets it.
        current = stored or tool.path.tracked()
        if target - current > EPSILON:
            self._track(tool, current - target)

    def _restore_to(self, tool: SimTool, target: float) -> None:
        """Move the filament to `target` mm back, feeding or retracting
        (`mapi::restore_retracted_distance` in `src/common/mapi/motion.cpp`)."""
        stored = tool.path.auto_retracted or 0.0
        current = stored or tool.path.tracked()
        # The move takes a stored distance over, so a feed doesn't deretract it again.
        tool.path.auto_retracted = None if stored else tool.path.auto_retracted
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
            or tool.path.fully_retracted(ex.full_retract_mm)
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
        if ex.auto_retracts and ex.cold_unload and tool.path.fully_retracted(ex.full_retract_mm):
            return [LoadStep("UNLOAD", -max(unload - (tool.path.auto_retracted or 0.0), 0.0))]
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
                self._track(tool, tool.path.tracked())
            else:  # RETRACTED_STEP
                tool.path.auto_retracted = step.mm
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
        if not self._slot_on(index):
            raise Invalid(f"T{index} is an MMU slot, and the MMU is off")
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
        tool.path.empty(self.model.gap_mm)

    def _idle_sensor_clear(self, tool: SimTool, *, sensor_fault_counts: bool) -> None:
        if not self.sensors:
            return
        empty = not tool.path.loaded or (sensor_fault_counts and tool.path.sensor_fault)
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
            # The preview's Yes runs `M701 T<n>` (`queue_filament_gcodes` in
            # `src/common/marlin_print_preview.cpp`): a plain load, with no prime, and no
            # auto-retract because `is_printing()` is already true there
            # (`load_finalize_process` in `src/marlin_stubs/pause/pause.cpp`). It picks
            # the tool inside its dialog (`Pause::tool_change`, `pause.cpp:1447-1468`), so
            # no idle sensor clear runs at the pick.
            self._pick(t)
            steps = self._load_steps(purge_mm, extra, False, auto_retract=False)
            job.enter(JobPhase.PREVIEW_LOAD)
        else:
            if not waiting:
                self._menu_pick(t)
            steps = self._load_steps(purge_mm, extra, waiting)
        self._start_load(t, steps, material)
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
        # The radio is off while the MMU is switched on (`request_manager.cpp:262-269`,
        # `tool_tag_buddy.cpp:11-18`).
        if self.feed is MMU:
            raise Invalid("the tag reader is off while the MMU is on")
        value = check_uuid(uuid)
        tag_temps = self.model.preset_temperatures(name)
        if temps is not None:
            tag_temps = dict(zip(tag_temps, _check_temperatures(temps), strict=True))
        # Only tool 0 can read a tag: the models with a reader are MMU builds, whose other
        # slots are on only while the MMU is, and then the radio is off.
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
        t.path.loaded, t.path.gap = True, 0.0
        self._log("load", f"T{t.index} loaded by hand while off: {describe(t.material)}")

    def unload(self, tool: int) -> None:
        """Unload a tool. While the printer is off the type stays stored until the next boot."""
        t = self._tool(tool)
        self._check_unload(t)
        if self.power is Power.OFF:
            self._log("action", f"unload T{tool} by hand while off")
            t.path.loaded, t.path.gap = False, self.model.gap_mm
            return
        self._log("action", f"unload T{tool}")
        self._menu_pick(t)
        self._start_unload(t)
        self.screen = State.IDLE  # using the menu dismisses a finished or stopped screen
        self._show_state()

    def _start_load(self, t: SimTool, steps: list[LoadStep], material: Material | None) -> None:
        t.process = LoadProcess(steps, material)
        self._next_load_step(t, t.process)  # a load with no slow or fast feed types at once

    def _start_unload(self, t: SimTool) -> None:
        steps = self._unload_steps(t, "unload")
        self._settle(t, steps, None)
        t.process = UnloadProcess(steps, self.settings.unload_seconds)

    def change_all(self, tools: list[int], *, name: str, color: str | None = None) -> None:
        """Change the filament in the chosen tools to the preset `name`: Change all in the
        Filament menu, which sends `M9934` (`Config::execute` in
        `src/common/multi_filament_change.cpp`). The other tools keep theirs.

        One dialog runs the whole change, so the printer reads BUSY throughout. The tools
        change one at a time in index order. A tool with a stored type changes with
        `M1600`, whose unload counts; a tool with none only loads, with `M701`. The stored
        type decides, not the sensor, and the load needs no filament check. Each operation
        picks its tool inside its dialog (`Pause::tool_change`), so no idle sensor clear
        runs at the pick. INDX docks each tool after its load, and the XL ends with the
        last tool picked.
        """
        self._check_change_all()
        chosen = sorted({self._tool(i).index for i in tools})
        if not chosen:
            raise Invalid("change all needs at least one tool")
        queue: list[Queued] = []
        for i in chosen:
            t = self.tools[i]
            material = self._new_material(t, "preset", name, color, None, None)
            if t.material is not None:
                queue.append(Queued(i, "unload"))
            queue.append(Queued(i, "load", material))
        listed = ", ".join(f"T{i}" for i in chosen)
        self._log("action", f"change all {listed} to {describe(material)}")
        self.queue = queue
        self.screen = State.IDLE  # using the menu dismisses a finished or stopped screen
        self._next_queued()
        self._show_state()

    def _next_queued(self) -> None:
        """Start the change all's next operation, in the step the last one ended, so the
        printer never reads IDLE between them."""
        q = self.queue.pop(0)
        t = self.tools[q.tool]
        self._pick(t)
        if q.kind == "unload":
            self._start_unload(t)
        else:
            self._start_load(t, self._load_steps(None, 0.0, False), q.material)

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
        self._log("action", f"retract T{tool} {mm:g} mm: gap {t.path.gap:g}")

    def select_tool(self, tool: int) -> None:
        t = self._tool(tool)
        self._check_select()
        self._log("action", f"select T{tool}")
        self._pick(t)
        cleared = self.feed.idle_clear_tool(self)
        if self.power is not Power.OFF and cleared is not None:
            self._idle_sensor_clear(cleared, sensor_fault_counts=True)

    def park_tool(self) -> None:
        """Park the picked tool from the Pick/park menu (`P0 S1 L0` in
        `src/gui/screen/screen_tool_pick_park.cpp`), leaving none picked."""
        self._check_park()
        self._log("action", f"park T{self.selected_tool}")
        self.selected_tool = None

    def _pick(self, t: SimTool) -> None:
        """Pick `t` up unless it's picked already. A pick moves no counted filament: the
        XL's moves no E, and INDX's head-lock moves are service moves, which the planner
        never counts (`toolchanger_xl.cpp`, `toolchanger_indx.cpp`, `planner.cpp:2194-2198`)."""
        if self.selected_tool != t.index:
            self.feed.pick(self, t)

    def _menu_pick(self, t: SimTool) -> None:
        """The pick before a menu load or unload: `T<n> S1 L0 D0`, sent before `M701` or
        `M702` with no dialog open (`show_tool_selector_dialog` in
        `src/gui/MItem_filament.cpp`). So the idle sensor clear runs on the newly picked
        tool before the operation starts, as it does after `select_tool`. On a tool that's
        already picked nothing changes."""
        if self.selected_tool == t.index:
            return
        self._pick(t)
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
        for key in ("prime_mm", "toolchange_purge_mm"):
            value = getattr(spec, key)
            if value is None:
                continue
            if self.profile is None:
                raise Invalid(f"{key} needs a multi-tool slicer profile, which this printer lacks")
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                raise Invalid(f"{key} must be a number of at least 0, got {value!r}")
        self._check_joins(spec)
        total = spec.total_mm
        changes = sorted(x for x in spec.color_changes_mm if 0 < x < total)
        self._start_job(UserJob(name=spec.name, spec=spec, pending_color_changes=changes))

    def _check_joins(self, spec: PrintSpec) -> None:
        """Refuse spool joins the firmware wouldn't set (`SpoolJoin::add_join` in
        `lib/Marlin/Marlin/src/module/prusa/spool_join.cpp:71-105`).

        A join needs a toolchanger that changes tools and two enabled tools. A tool is
        joined to once, and no chain loops. A second join from one tool is refused here,
        which is the simulator's simplification: the firmware appends it to the end of
        that tool's chain (`:76-79`). The tools-mapping screen takes a
        join's tool off any G-code tool first (`disconnect_virtual_tool` in
        `src/gui/screen/print/frame_tool_mapping.cpp`), and the simulator maps each G-code
        tool to its own tool, so a join's tool can't be one the print uses.
        """
        joins = spec.spool_join
        if joins and not self.toolchanger:
            raise Invalid(f"a spool join needs a toolchanger, which {self.name} isn't")
        used = {s.tool for s in spec.segments}
        nexts: dict[int, int] = {}
        for j in joins:
            old, new = self._tool(j.tool), self._tool(j.to)
            if old is new:
                raise Invalid(f"T{old.index} can't join itself")
            if new.index in used:
                raise Invalid(f"T{new.index} prints in this file, so it can't be joined to")
            if old.index in nexts:
                raise Invalid(f"T{old.index} joins on twice")
            if new.index in nexts.values():
                raise Invalid(f"T{new.index} is joined to twice")
            nexts[old.index] = new.index
        for start in nexts:
            tool = nexts[start]
            while tool in nexts:
                if tool == start:
                    raise Invalid(f"the joins from T{start} loop back to it")
                tool = nexts[tool]

    def _start_job(self, job: UserJob | SyncJob) -> None:
        """Count the job and scan the file. A print from a file is counted before the preview
        asks anything (`init` in `src/common/marlin_print_preview.cpp`)."""
        if self.resuming is not None:
            # The printer is no longer idle, so the autostart gives the resume up
            # (`print_utils_loop` in `src/common/print_utils.cpp`).
            self._log("job", f"job {self.resuming.id}: resume dropped by the new job")
            self.resuming = None
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
        job.parked = True
        if self._primes_at_resume():
            # The pause retracts to 8 mm (`retract_and_lift` in `Pausing_WaitIdle`), and the
            # resume never feeds it back: its purge refills the gap instead.
            t = self._job_tool(job)
            self._retract_to(t, self.model.extruder.lengths.retract_mm)
        # INDX docks the tool once the head is parked; the XL keeps it.
        self.feed.rest(self)
        job.enter(JobPhase.PAUSED)
        self._show_state()

    def resume(self) -> None:
        self._check_resume()
        job = self.job
        if job.phase is JobPhase.AWAIT_RESUME:
            self._log("action", "resume after the power panic")
            self._recover(job)
            return
        self._log("action", "resume")
        if job.restore is not None:
            # The unpark's feed and `restore_retracted_distance` put the filament back at
            # the gap it had when the change began (`Pause::filament_change` in
            # `src/marlin_stubs/pause/pause.cpp`).
            tool, gap = job.restore
            self._restore_to(self.tools[tool], gap)
            job.restore = None
            # A color change in the file ends here, with its `M600`. Until then a power-panic
            # resume runs it again: the cut saves the file position of the command running,
            # which is the `M600` itself (`stop_and_save` in `crash_recovery.cpp`,
            # `get_current_sdpos` in `queue.h`).
            if job.pending_color_changes and job.pending_color_changes[0] <= job.fed_mm:
                job.pending_color_changes.pop(0)
            # A change on another tool changes back to the print's tool after the dialog,
            # with no E move and no purge (`M600_execute`, `M600.cpp:317-339`).
            self._pick(self._job_tool(job))
        elif job.parked:
            self._unpark_prime(job)
        job.parked = False
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

    def _primes_at_resume(self) -> bool:
        """Whether resuming a pause purges in the nozzle cleaner: INDX's `purge_clean`.
        The iX's cleaner G-code isn't modeled here."""
        return bool(self.model.extruder.cleaner_purge_mm)

    def _unpark_prime(self, job: UserJob) -> None:
        """Resume from a pause: INDX picks the tool it docked at the pause, then purges it
        in the nozzle cleaner, still `PAUSED` (`resuming_begin` and `unpark_prime` in
        `src/common/marlin_server.cpp:3298-3307, 2662-2674, 3423-3434`). The unpark then
        feeds nothing back: the purge syncs E to the position the pause saved."""
        t = self._job_tool(job)
        self._pick(t)
        if self._primes_at_resume():
            self._purge_clean(t)
            purge = self.model.extruder.cleaner_purge_mm
            self._log("job", f"T{t.index} purged {purge:g} mm at the resume")

    def _purge_clean(self, t: SimTool) -> None:
        """The nozzle cleaner's `purge_clean` on the picked tool: `G750 E25`, then the
        `M1705 N` auto-retract (`src/feature/nozzle_cleaner/nozzle_cleaner.cpp:76-94`,
        `src/marlin_stubs/M1705.cpp:28-46`)."""
        purge = self.model.extruder.cleaner_purge_mm
        self._run_steps(t, [LoadStep("PURGE", purge), LoadStep(AUTO_RETRACT_STEP)])

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
            t.path.loaded = True
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
        # The runout sends `M600 A`, which joins when the print has a join from the tool
        # and is otherwise a filament change on it (`M600.cpp:125-148`). A jam sends
        # `M1601`, which never joins.
        job = self.job
        join = next((j for j in job.spec.spool_join if j.tool == t.index), None)
        if join is None:
            self._filament_change(t, Reason.RUNOUT)
        else:
            self._spool_join(job, t, self.tools[join.to])

    def _spool_join(self, job: UserJob, old: SimTool, new: SimTool) -> None:
        """Start a spool join from the picked tool `old` to `new` (`SpoolJoin::do_join` in
        `lib/Marlin/Marlin/src/module/prusa/spool_join.cpp:239-365`).

        It maps the G-code tool that prints on `old` to `new` for the rest of the print,
        then unloads `old` in a dialog that reads ATTENTION: no park retract, INDX's 2 mm
        unload purge, the `runout` ramming and the rest of the unload, and the unload
        counts (`unload_spool_join` in `pause.cpp`). No nozzle clean or question follows.
        The JOIN phase stands for the dialog; `_finish_join` changes tools at its end. The
        old tool's gap is kept for the new one.
        """
        g = job.spec.segments[job.segment].tool
        job.mapped[g] = new.index
        job.restore = (new.index, old.path.tracked())
        self._log("job", f"spool join T{old.index} -> T{new.index}")
        self._run_steps(old, self._unload_steps(old, "runout"))
        self._physical_unload(old, "spool join")
        job.enter(JobPhase.JOIN)
        job.remaining = self.settings.unload_seconds
        self._show_state()

    def _finish_join(self, job: UserJob) -> None:
        """End a spool join: change to the new tool and purge it, outside any dialog, so it
        reads PRINTING (`spool_join.cpp:311-355`).

        The XL parks the old tool, picks the new one and runs `purge_tool`: the build's
        purge length, never doubled, then where the build auto-retracts a retract of 8 mm
        stored as the auto-retracted distance (`toolchanger_xl.cpp:505-571`). INDX changes
        tools and runs the nozzle cleaner's `purge_clean`. Then the move puts the old tool's
        gap back on the new tool (`mapi::restore_retracted_distance`). The new tool's type
        isn't touched. A join during the preheat goes straight to printing: the join has
        waited for the new tool to heat.
        """
        tool, gap = job.restore
        job.restore = None
        new = self.tools[tool]
        self._pick(new)
        ex = self.model.extruder
        if ex.cleaner_purge_mm:
            self._purge_clean(new)
        else:
            steps = [LoadStep("PURGE", ex.lengths.purge_mm)]
            if ex.auto_retracts:
                retract = ex.lengths.retract_mm
                steps += [LoadStep("RETRACT", -retract), LoadStep(RETRACTED_STEP, retract)]
            self._run_steps(new, steps)
        self._restore_to(new, gap)
        self._log("job", f"T{new.index} purged after the spool join")
        self._begin_feeding(job)

    def jam(self, tool: int, unload: bool = True) -> None:
        """A jam either unloads like a runout (and purges more on reload) or is cleared in place."""
        t = self._tool(tool)
        self._check_runout(t)
        self._log("action", f"jam T{tool}" + ("" if unload else " without unloading"))
        if unload:
            self._filament_change(t, Reason.JAM)
            return
        job = self.job
        job.paused_from = job.phase
        job.enter(JobPhase.HOLD, Attention(Reason.JAM, tool))
        self._show_state()

    def change_filament(self, tool: int) -> None:
        """Change the filament in `tool` during a print: Tune > Change filament, which on a
        toolchanger asks for the tool and sends `M600 P T<n>`, and on a direct printer names
        the picked tool (`src/gui/screen/screen_m600.cpp`).

        On the picked tool it's the filament change a runout runs. On another tool, `M600`
        picks that tool with no dialog open, so it reads `PRINTING`, runs the same change
        on it, and changes back to the print's tool when the change ends, with no E move
        (`M600_execute` in `src/marlin_stubs/pause/M600.cpp:204-340`). The simulator
        changes back at the resume. Picking the other tool heats it for the type
        `_type_to_load` finds; with none found its target is 0, so `Pause::filament_change`
        returns at once as too cold, and `M600` changes back with nothing changed.
        """
        t = self._tool(tool)
        self._check_change_filament()
        job = self.job
        self._log("action", f"change filament T{tool}")
        if t.index != self.selected_tool:
            if self._type_to_load(t, job) is None:
                self._log("job", f"T{tool} has no type to heat for: nothing changes")
                return
            self._pick(t)
        self._filament_change(t, Reason.COLOR_CHANGE)

    def _type_to_load(self, t: SimTool, job: UserJob) -> Material | None:
        """The type a filament change on `t` loads back, read before its unload:
        `FilamentType::for_tool_heuristic` (`M600.cpp:282-290`,
        `src/common/filament_tools.cpp:15-45`). The stored type; else the type the print's
        file names for the tool, when it's a preset of the build; else the type the last
        unload left, or the ad-hoc slot. The file's tools go through the tool mapping, so
        after a spool join the file's type belongs to the join's tool."""
        if t.material is not None:
            return t.material
        named = {job.mapped.get(s.tool, s.tool): s.material for s in job.spec.segments}.get(t.index)
        if named in self.model.presets:
            return self._new_material(t, "preset", named, None, None, None)
        return t.kept()

    def _filament_change(self, t: SimTool, reason: Reason) -> None:
        """The filament change on the picked tool `t` that every `M600` runs: a runout, a
        jam that unloads, a color change in the file, and Tune > Change filament."""
        job = self.job
        # Only a print in progress changes filament, so the phase is PRINTING or PREHEAT.
        job.paused_from = job.phase
        loads = self._type_to_load(t, job)
        # The filament change records the tracker's distance, parks with a retract, then
        # rams and unloads (`Pause::filament_change` and `park_nozzle_and_notify` in
        # `src/marlin_stubs/pause/pause.cpp`).
        job.restore = (t.index, t.path.tracked())
        self._retract_to(t, self.model.extruder.lengths.retract_mm)
        self._run_steps(t, self._unload_steps(t, "runout"))
        self._physical_unload(t, str(reason))
        if loads is not None:
            t.kept_type = loads  # what the reload writes (`filament::set_type_to_load`)
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

    def boot(self) -> None:
        """Start the firmware: counters restart, types and the job counter survive.

        The idle sensor clear runs at boot, a power-panic boot too: the print it resumes
        isn't running yet, so the printer isn't printing (`print_utils_loop` in
        `src/common/print_utils.cpp`).
        """
        self.boot_id = new_boot_id()
        for t in self.tools.values():
            t.extruded, t.unload_count, t.extruded_at_unload = 0.0, 0, 0
            if isinstance(t.process, LoadProcess):
                t.path.loaded = True  # a load cut short leaves its filament in
            t.process = None
            t.path.boot(self.model.gap_mm)
        self.queue = []  # a change all doesn't survive the cut
        self.homed, self.message = False, None
        self._log("boot", f"boot id {self.boot_id}")
        if self.mmu is not None and self.mmu.on:
            # `app_setup` starts an MMU that's switched on (`appmain.cpp:186-190`). Its
            # filament stays where it was.
            self._start_mmu()
        enabled = [t.index for t in self.tools.values() if t.enabled]
        if self.feed.selectable and not self.toolchanger and enabled:
            # An XL that finds one Dwarf turns its toolchanger off and picks that tool
            # (`autodetect_active_tool` in `toolchanger_utils_xl.cpp`).
            self.selected_tool = enabled[0]
        selected = self.feed.idle_clear_tool(self)
        if selected is not None:
            self._idle_sensor_clear(selected, sensor_fault_counts=False)

    def reboot(self, *, keep_job: bool = False, cold_bed: bool = False) -> None:
        """Reboot. With keep_job a print in progress resumes with the same ID (power panic).

        A print that wasn't paused retracts at the cut, except on the XL. The printer boots
        IDLE with no job, and the print resumes `panic_idle_seconds` later. The resume is
        BUSY while the firmware recovers: INDX purges in its nozzle cleaner halfway through,
        and every other model that retracted feeds the retract back at the end. Then PAUSED
        while the head unparks, then PRINTING. With `cold_bed` it waits in ATTENTION for
        `resume()` before recovering. A print that was paused comes back paused after the
        BUSY, with no filament moved. A filament change dialog isn't a pause: a cut during
        one resumes the print on its own, on the tool the cut found picked, and a color
        change in the file runs again. A model without power panic loses the print and
        boots idle, as a plain reboot does.
        """
        self._need_power()
        if not keep_job:
            self._log("action", "reboot")
        elif self.model.power_panic:
            self._log("action", "power loss and resume")
        else:
            self._log("action", "power loss, print lost: no power panic")
        self._reboot(keep_job, cold_bed)

    def _reboot(self, keep_job: bool, cold_bed: bool = False) -> None:
        job, held = self.job, self.resuming
        # HOLD past the preview is a jam's question, a filament change dialog.
        resumable = (
            JobPhase.PRINTING,
            JobPhase.PAUSED,
            JobPhase.PREHEAT,
            JobPhase.WAIT_LOAD,
            JobPhase.HOLD,
            JobPhase.JOIN,
        )
        # A cut during the preview has nothing to resume: the firmware saves power-panic data
        # only once the print has begun (`power_panic::prepare` in
        # `src/common/power_panic.cpp`). A build without power panic resumes nothing.
        keep = (
            keep_job
            and self.model.power_panic
            and isinstance(job, UserJob)
            and not job.previewing
            and job.phase in resumable
        )
        if keep:
            held = job
            # The firmware's `was_paused` is its pause state, which only `pause()` enters
            # (`printer_paused` in `marlin_server.cpp`, `ac_fault_isr` in
            # `power_panic.cpp`). A filament change dialog, a spool join's unload or a jam's
            # question runs inside a G-code while the print state stays printing or
            # resuming, so the print resumes on its own and the dialog is gone
            # (`Pause::filament_change` in `pause.cpp`).
            was_paused = job.parked
            if not was_paused:
                # The resume picks the tool the cut found picked and issues no tool change
                # (`power_panic.cpp:380-395, 1049-1055`; the XL's Dwarf stays on the head).
                picked = self.selected_tool
                if picked != self._job_tool(job).index:
                    job.resumed_on = picked
                if self.model.extruder.panic_retract:
                    # The cut retracts the head's tool through the planner, which forgets a
                    # stored auto-retract distance (`panic_loop` in `power_panic.cpp`). The
                    # counters restart at the boot. A print in progress always has a tool
                    # picked.
                    self._track(self._head_tool(), -self.model.extruder.lengths.retract_mm)
            job.restore = None  # a power loss ends the filament change before its restore
            job.paused_from = JobPhase.PRINTING if was_paused else None
            if was_paused:
                job.enter(JobPhase.PAUSED)
        elif held is not None and not keep_job:
            self._log("job", f"job {held.id}: resume dropped by the reboot")
            held = None
        self._forget_pick(job)
        # A cut while the resume waits finds the printer idle, so it saves nothing new and
        # the saved print waits again (`ac_fault_isr` in `src/common/power_panic.cpp`).
        self.job, self.resuming, self.screen = None, None, State.IDLE
        self.boot()
        if held is not None:
            # The bed decides at each boot, for a paused print too (`auto_recover` in
            # `resume_print`).
            if cold_bed:
                held.enter(JobPhase.AWAIT_RESUME, Attention(Reason.COLD_BED))
            elif held.paused_from is None:
                held.enter(JobPhase.RECOVER)
            else:
                held.enter(JobPhase.PAUSED)
            self.resuming, self.resume_in = held, self.settings.panic_idle_seconds
            self._log("job", f"job {held.id} resumes in {self.resume_in:g} s")
        self._show_state()

    def _forget_pick(self, job: UserJob | SyncJob | None) -> None:
        """Leave none picked after a cut during a print on INDX that uses several tools.

        INDX boots with the last picked tool its EEPROM holds, and parking a tool while
        printing marks that invalid, unless a one-tool print froze it at its start
        (`restore_last_picked_tool` in `toolchanger_utils_indx.cpp:50-57`,
        `persist_last_picked_tool` in `toolchanger_indx.cpp:926-939`). That this follows
        from the print's tools is inferred. Every other boot keeps the picked tool: the XL's is the
        Dwarf on its head (`toolchanger_xl.cpp`).
        """
        printing = isinstance(job, UserJob) and not job.previewing
        if self.feed.docks and printing and len({s.tool for s in job.spec.segments}) > 1:
            self._log("boot", "no tool restored: the print used several tools")
            self.selected_tool = None

    def _tick_resume(self, dt: float) -> float:
        """Count down to the power-panic resume; return the time left once it starts.

        The firmware's autostart tries every 1.5 s and resumes once the USB drive, the
        temperatures and the screen are ready, which the simulator takes as at the first
        try (`print_utils_loop` in `src/common/print_utils.cpp`). A menu load or unload
        still running then makes it try again 1.5 s later: the firmware doesn't check for
        one, and the resume's own moves wait for the queue to empty.
        """
        used = min(self.resume_in, dt)
        self.resume_in -= used
        if self.resume_in > EPSILON:
            return 0.0
        if self.state is not State.IDLE:
            self.resume_in = self.settings.panic_idle_seconds
            self._log("job", f"resume waits {self.resume_in:g} s: the printer is {self.state}")
            return 0.0
        job, self.resuming = self.resuming, None
        self.job = job
        self._log("job", f"job {job.id} resumes after the power panic")
        if job.phase is JobPhase.AWAIT_RESUME:
            # A cold bed waits for the user at once (`powerpanic_resume` in
            # `src/common/marlin_server.cpp`).
            self._show_state()
        else:
            self._recover(job)
        return dt - used

    def _recover(self, job: UserJob) -> None:
        """BUSY while the firmware heats and homes. A model whose resume purges does so
        halfway through, unless the print was paused: that resume only parks the head
        (`ResumeState::ParkForPause` in `src/common/power_panic.cpp`)."""
        # The resume sets the tool the print had picked before anything moves
        # (`power_panic.cpp:380-395`).
        self._pick(self._job_tool(job))
        job.enter(JobPhase.RECOVER)
        job.remaining = self.settings.recover_seconds
        purges = job.paused_from is None and self.model.extruder.panic_purge_mm is not None
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
        """The tool the job's current segment prints with: the one a power-panic resume
        found picked, else the segment's own."""
        if job.resumed_on is not None:
            return self.tools[job.resumed_on]
        return self.tools[self._seg_tool(job, job.segment)]

    @staticmethod
    def _seg_tool(job: UserJob, index: int) -> int:
        """The tool segment `index` prints on: its G-code tool, unless a spool join has
        mapped that to another (`tool_mapper.set_mapping`, `spool_join.cpp:258-265`)."""
        tool = job.spec.segments[index].tool
        return job.mapped.get(tool, tool)

    def set_power(self, power: Power, *, resume: bool = False, cold_bed: bool = False) -> None:
        """Off freezes the printer; leaving off boots it, which drops any job unless `resume`
        on a model with power panic."""
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
        self._tool(tool).path.sensor_fault = value
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
        """The menu's switch for every filament sensor. Turning them off with the MMU on
        turns the MMU off too: the firmware queues `M709 S0`
        (`filament_sensors_handler.cpp:328-332`), so it needs what `set_mmu` needs. The
        sensors the MMU forces on still read when `mmu_off` looks (`should_enable`,
        `:391-398`), which is inferred from the order of the calls."""
        mmu_off = not value and self.feed is MMU
        if mmu_off:
            self._check_mmu(False)
        self.sensors = value
        self._log("action", f"filament sensors {'on' if value else 'off'}")
        if mmu_off:
            self._log("mmu", "MMU off: the filament sensors are off")
            self._mmu_off()
            self._show_state()

    def set_mmu(self, value: bool) -> None:
        """Switch the MMU on or off: `M709 S1` or `S0`, which the menu's switch sends
        (`filament_gcodes::mmu_on` and `mmu_off`, `M70X_mmu.cpp:54-92`).

        On, the MMU starts: no slot selected, connecting. The tag link each slot's ad-hoc
        type keeps on a Core One isn't modeled. Off, slot 0's type is cleared, which counts
        an unload, unless the extruder sensor surely sees filament: always slot 0,
        whichever slot was loaded. The other slots keep their types and stop being listed,
        and the printer feeds slot 0 as a single-tool one.
        """
        self._check_mmu(value)
        self._log("action", f"MMU {'on' if value else 'off'}")
        if value:
            self.mmu.on = True
            self._start_mmu()
        else:
            self._mmu_off()
        self.screen = State.IDLE  # using the menu dismisses a finished or stopped screen
        self._show_state()

    def _start_mmu(self) -> None:
        """`MMU2::Start`: no slot selected until a load, and slot 0 credited until the MMU
        is Active (`mmu2_mk4.cpp:169-191`)."""
        self.selected_tool = None
        self.mmu.connecting = self.settings.mmu_connect_seconds
        self._log("mmu", f"MMU connecting for {self.mmu.connecting:g} s")
        self._tick_mmu(0.0)

    def _tick_mmu(self, dt: float) -> None:
        """Count the MMU's connecting window down by `dt`; log once when it's Active."""
        mmu = self.mmu
        mmu.connecting = max(mmu.connecting - dt, 0.0)
        if mmu.connecting <= EPSILON:
            mmu.connecting = 0.0
            self._log("mmu", "MMU active")

    def _mmu_off(self) -> None:
        """`mmu_off`: the setting goes off, then slot 0 is cleared unless the extruder
        sensor surely sees filament. `M709 S0` runs it with the MMU already off too
        (`M707-9_parse.cpp:91-93`). A disabled sensor is never sure
        (`filament_sensors_handler.hpp:80-84`), and the sensors are disabled when they're
        switched off, unless the MMU was on and forced them on (`should_enable`,
        `filament_sensors_handler.cpp:391-402`)."""
        path, was_on = self.mmu.path, self.mmu.on
        self.mmu.on, self.mmu.connecting = False, 0.0
        if not path.loaded or path.sensor_fault or not (self.sensors or was_on):
            self._count_unload(self.tools[0], "MMU off")
        self.selected_tool = 0

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
        # The MMU's filament menu has no plain Load (`screen_menu_filament_mmu.hpp:11-24`).
        if self.feed is MMU and not keep:
            raise Invalid("the MMU's filament menu has no plain load")
        if t.process is not None:
            raise Busy("a load or unload is already running")
        if self.power is Power.OFF:
            return  # pushed in by hand
        if self.state is State.BUSY:
            raise Busy("a load or unload is already running")
        if self.job is not None and not waiting:
            raise Busy("can't load during a print; run the tool out first")

    def _check_unload(self, t: SimTool) -> None:
        if self.feed is MMU:
            raise Invalid("the MMU's Unload Filament arrives in a later PR of this stack")
        if not t.path.loaded:
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
        # Move E moves only the picked tool, and with none picked it shows "No tool" and
        # sends no E (`ScreenMenuMove::loop` and `plan_moves` in
        # `src/gui/screen_menu_move.cpp`). An MMU's one extruder is always there, and moves
        # whichever slot's filament is in it.
        if self.feed is MMU:
            if t.index != self.mmu.fed:
                raise Invalid(f"T{t.index}'s filament isn't in the extruder")
        elif self.selected_tool is None:
            raise Busy("no tool picked")
        elif t.index != self.selected_tool:
            raise Invalid(f"T{t.index} isn't the picked tool")
        if not t.path.loaded:
            raise Invalid(f"T{t.index} has no filament")

    def _check_select(self) -> None:
        if self.feed is MMU:
            raise Invalid("an MMU selects a slot only by loading it")
        if not self.toolchanger:
            raise Invalid(f"{self.model.name} has one tool")
        if self.job is not None or self.state is State.BUSY:
            raise Busy(f"can't pick a tool while {self.state}")

    def _check_park(self) -> None:
        self._check_select()
        if self.selected_tool is None:
            raise Busy("no tool picked")

    def _check_runout(self, t: SimTool) -> None:
        self._need_power()
        job = self.job
        if not (isinstance(job, UserJob) and job.phase in (JobPhase.PRINTING, JobPhase.PREHEAT)):
            raise Busy("a runout or jam needs a print in progress")
        # Only the picked tool feeds, so only its sensor can see a runout or a jam.
        if t.index != self.selected_tool:
            raise Invalid(f"T{t.index} isn't the selected tool")
        if not t.path.loaded:
            raise Busy(f"T{t.index} has no filament")

    def _check_change_filament(self) -> None:
        self._need_power()
        job = self.job
        # Tune > Change filament is enabled once the print has printed something
        # (`max_printed_z > 0` in `MI_M600::Loop`, `src/gui/screen/screen_m600.cpp`), so
        # not during the preheat. The firmware offers it while paused too
        # (`screen_printing.cpp`), which the simulator doesn't model.
        if not (isinstance(job, UserJob) and job.phase is JobPhase.PRINTING):
            raise Busy("a filament change needs a print in progress")

    def _check_sensor_runout(self, t: SimTool) -> None:
        # A runout is the sensor seeing the filament go. A jam is found another way.
        self._check_runout(t)
        if not self.sensors:
            raise Busy("the filament sensors are off")

    def _check_disable(self, t: SimTool) -> None:
        # An MMU build lists its slots by the MMU's setting alone (`listed`).
        if self.mmu is not None:
            raise Invalid("an MMU slot can't be disabled")
        job = self.job or self.resuming
        in_print = isinstance(job, UserJob) and t.index in {
            *(s.tool for s in job.spec.segments),
            *(j.to for j in job.spec.spool_join),
            job.resumed_on,
        }
        queued = any(q.tool == t.index for q in self.queue)
        if t.index == self.selected_tool or in_print or t.process is not None or queued:
            raise Invalid(f"T{t.index} is in use")

    def _check_mmu(self, value: bool) -> None:
        """Switching the MMU needs one attached and an idle printer. The menu turns it on
        only with a working extruder sensor (`MItem_mmu.cpp:176-184`); `M709 S1` doesn't
        check, but with the sensors off the firmware turns it straight off again
        (`filament_sensors_handler.cpp:328-332`)."""
        if self.mmu is None:
            raise Invalid(f"{self.name} has no MMU")
        self._need_power()
        if self.job is not None or self.state not in IDLE_STATES:
            raise Busy(f"can't switch the MMU while {self.state}")
        if value and not self.sensors:
            raise Busy("the MMU needs the filament sensors on")

    def _check_change_all(self) -> None:
        # Change all is in the Filament menu on a toolchanger only
        # (`src/gui/screen_menu_filament.hpp`), and the menu needs an idle printer.
        if not self.toolchanger:
            raise Invalid(f"{self.model.name} has one tool")
        self._need_power()
        if self.job is not None or self.state not in IDLE_STATES:
            raise Busy(f"can't change all while {self.state}")

    def _check_start_print(self) -> None:
        self._need_power()
        if not self.can_print():
            raise Busy(f"can't print while {self.state}")
        # Temporary: the MMU's print rules come with its slicer profile.
        if self.feed is MMU:
            raise Invalid("MMU prints arrive in a later PR of this stack")

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
            "park_tool": self._check_park,
            "change_all": self._check_change_all,
            "reboot": self._need_power,
            "clear_jam": self._check_clear_jam,
            "tap_through": self._check_tap_through,
            "set_mmu": lambda: self._check_mmu(not (self.mmu and self.mmu.on)),
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
            "change_filament": self._check_change_filament,
            "select_tool": self._check_select,
            # The fault switches take any enabled tool, in any state.
            "set_sensor_fault": lambda: None,
            "set_stuck_write": lambda: None,
        }
        if not self._slot_on(t.index):
            unlisted = _refusal(lambda: self._tool(t.index, enabled=False))
            return {name: unlisted for name in [*checks, "set_tool_enabled"]}
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
        if self.mmu is not None and self.mmu.connecting > 0:
            active = True
            self._tick_mmu(dt)
        for t in self.tools.values():
            if t.process is not None:
                active = True
                self._tick_process(t, dt)
        if self.resuming is not None:
            active = True
            dt = self._tick_resume(dt)
        if self.job is not None:
            active = True
            self._tick_job(dt)
        self._idle_clear_selected()
        return active

    def _idle_clear_selected(self) -> None:
        # The firmware clears the type whenever it isn't printing and the picked tool's sensor
        # reads empty, so a type written to an empty tool shows only until the job ends.
        # `is_printing()` is false on the tools-mapping screen too (`marlin_server.cpp`).
        t = self.feed.idle_clear_tool(self)
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
        self.feed.end_unload(self, t)
        self._process_done()

    def _next_load_step(self, t: SimTool, p: LoadProcess) -> None:
        """Act on the markers that come up, and end the load after its last step."""
        self._settle(t, p.steps, p.material)
        if not p.steps:
            t.path.loaded, t.process = True, None
            self._log("load", f"T{t.index} loaded: extruded {int(t.extruded)}")
            # `M701` docks on INDX unless it resumes a print, so a menu load and the
            # preview's load do and a reload in a print doesn't (`M701_2.cpp:135-140`).
            job = self.job
            if job is None or job.phase is JobPhase.PREVIEW_LOAD:
                self.feed.rest(self)
            self._process_done()

    def _process_done(self) -> None:
        job = self.job
        if job is not None and job.phase is JobPhase.WAIT_LOAD:
            job.enter(JobPhase.PAUSED)
        elif job is not None and job.phase is JobPhase.PREVIEW_LOAD:
            # The load's result isn't checked and no sensor is read again: the type check
            # follows (`filament_not_inserted_load` in `marlin_print_preview.cpp`).
            self._check_types(job)
        elif self.queue:
            self._next_queued()
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
            if job.paused_from is not None:
                job.enter(JobPhase.PAUSED)  # `powerpanic_finish_pause`
                self._show_state()
            else:
                self._unpark(job)
        elif job.phase is JobPhase.UNPARK:
            self._begin_feeding(job)
        elif job.phase is JobPhase.JOIN:
            self._finish_join(job)
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
        seen = {i: t.path.loaded or not self.sensors for i, t in self.tools.items()}
        return {
            i: ToolView(self.listed(t), seen[i], t.material.name if t.material else None)
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
        rows = print_checks(self.feed.maps_tools(self), self._uses(job), self._tool_views()).rows
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
        checks = print_checks(self.feed.maps_tools(self), self._uses(job), self._tool_views())
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
        mapped = tool_mapping(self.feed.maps_tools(self), uses, views)
        rows = [] if mapped else wrong_types(uses, views)
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
        self.feed.select_for_print(self, self.tools[self._seg_tool(job, 0)])
        self._show_state()

    def _begin_feeding(self, job: UserJob) -> None:
        if not job.primed:
            job.primed = True
            job.resumed_on = None  # the start G-code's tool changes end it
            self.feed.prime(self, job, self._slicer(job))
        self.feed.select_for_print(self, self._job_tool(job))
        job.enter(JobPhase.PRINTING)
        self._show_state()

    @property
    def profile(self) -> Slicer | None:
        """The multi-tool slicer profile the printer prints with, as its feed system says."""
        return self.feed.profile(self)

    def _slicer(self, job: UserJob) -> Slicer | None:
        """The print's slicer profile, with the print's overrides."""
        slicer, spec = self.profile, job.spec
        return slicer and slicer.overridden(spec.prime_mm, spec.toolchange_purge_mm)

    def _park(self, job: UserJob | SyncJob, end: State) -> None:
        """End a job: a finished print's end G-code runs the feed system's last moves, then
        the firmware's own end runs, and INDX docks what's still picked
        (`pre_finalize_print`). A stopped print and a sync job run no end G-code."""
        if isinstance(job, UserJob) and end is State.FINISHED:
            self.feed.end_moves(self, job, self._slicer(job))
        self._end_of_print()
        self.feed.rest(self)
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

        On a toolchanger with no tool picked, the retract and the auto-retract do nothing
        (`move_to_retracted_distance` in `src/common/mapi/motion.cpp`,
        `AutoRetract::maybe_retract_from_nozzle` in `src/feature/auto_retract/auto_retract.cpp`),
        and the valid gaps are still stored: that loop goes over every hotend.

        With an MMU Active the auto-retract is skipped, whatever the slot: the slicer
        unloads (`pre_finalize_print`, `marlin_server.cpp:1067-1083`). While the MMU
        connects it runs, on slot 0 (`Mmu.credited`).
        """
        ex = self.model.extruder
        t = self._head_tool()
        if t is not None:
            self._retract_to(t, ex.lengths.retract_mm)
        if not ex.auto_retracts:
            return
        for tool in self.tools.values():
            if tool.path.gap_valid:
                tool.path.auto_retracted = tool.path.gap
        if t is not None and not (self.mmu and self.mmu.active):
            self._run_steps(t, [LoadStep(AUTO_RETRACT_STEP)])

    def _print_step(self, job: UserJob, dt: float) -> float:
        """Feed for dt, stopping at each segment end and color change. Return the time left."""
        spec = job.spec
        total = spec.total_mm
        rate = total / spec.duration_s if spec.duration_s else self.settings.print_rate_mm_s
        ends = [sum(s.slicer_mm for s in spec.segments[: i + 1]) for i in range(len(spec.segments))]
        while dt > EPSILON and job.phase is JobPhase.PRINTING:
            tool = self._job_tool(job)
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
                # It stays pending until the change ends (`resume`).
                self._log("job", f"color change at {change:g} mm")
                self._filament_change(tool, Reason.COLOR_CHANGE)
                return 0.0
            if job.segment == len(spec.segments) - 1:
                self._park(job, State.FINISHED)
                return dt
            self._next_segment(job)
        return 0.0 if job.phase is not JobPhase.PRINTING else dt

    def _next_segment(self, job: UserJob) -> None:
        """Go on to the next segment, through the file's tool change when its G-code tool
        differs. The change's moves run when it picks the tool already picked too, as after
        a resume on another tool: they're the file's."""
        segments = job.spec.segments
        old = self._job_tool(job)
        job.segment += 1
        tool = segments[job.segment].tool
        if tool == segments[job.segment - 1].tool:
            return
        job.resumed_on = None
        new = self.tools[self._seg_tool(job, job.segment)]
        # A G-code tool's first pick in the print stands for the first layer's tool changes.
        first = all(s.tool != tool for s in segments[: job.segment])
        self.feed.tool_change(self, old, new, self._slicer(job), first)

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
            "toolchanger": self.toolchanger,
            "message": self.message,
            "homed": self.homed,
            "job": job,
            "job_counter": self.job_counter,
            "checks": str(self.checks),
            "sensors": self.sensors,
            "actions": self.actions_json(),
            "load_sources": ["tag", "preset"] if m.uuids and self.feed is not MMU else ["preset"],
            "mmu": None if self.mmu is None else self._mmu_json(),
            "tools": [
                {**self._tool_json(t), "actions": self._tool_actions_json(t)}
                for t in sorted(self.tools.values(), key=lambda t: t.index)
            ],
        }

    def _mmu_json(self) -> dict:
        mmu = self.mmu
        return {"on": mmu.on, "active": mmu.active, "connecting": mmu.connecting, "fed": mmu.fed}

    def _tool_json(self, t: SimTool) -> dict:
        """The tool as the page shows it. On an MMU every slot shares the extruder's path,
        so a slot reads loaded only when its filament is the one in it. With the MMU off,
        the printer feeds slot 0 as a single-tool one, and slot 0 owns the path."""
        out = {**_tool_json(t), "listed": self.listed(t)}
        if self.mmu is not None:
            owner = self.mmu.fed if self.mmu.on else 0
            out["loaded"] = t.path.loaded and t.index == owner
        return out


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


def _tool_dict(t: SimTool) -> dict:
    """`asdict(t)` with the path's fields at the tool's level, as the page and snapshots
    have them."""
    out = asdict(t)
    out.update(out.pop("path"))
    return out


def _tool_json(t: SimTool) -> dict:
    out = _tool_dict(t)
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
    path = {f.name: d.pop(f.name) for f in fields(FilamentPath) if f.name in d}
    d["path"] = FilamentPath(**path)
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
    if d["restore"] is not None:
        d["restore"] = tuple(d["restore"])
    # JSON keeps a dict's keys as strings.
    d["mapped"] = {int(k): v for k, v in d["mapped"].items()}
    spec = dict(d["spec"])
    spec["segments"] = [Segment(**s) for s in spec["segments"]]
    spec["spool_join"] = [Join(**j) for j in spec["spool_join"]]
    d["spec"] = PrintSpec(**spec)
    return UserJob(**d)


def _printer_dict(p: SimPrinter) -> dict:
    d = asdict(p)
    d["model"] = p.model.code
    d["tools"] = [_tool_dict(p.tools[i]) for i in sorted(p.tools)]
    if p.mmu is not None:
        # The slots share the unit's path, which `d["mmu"]` holds once.
        path = {f.name for f in fields(FilamentPath)}
        d["tools"] = [{k: v for k, v in t.items() if k not in path} for t in d["tools"]]
    if p.job is not None:
        d["job"]["kind"] = p.job.kind
    if p.resuming is not None:
        d["resuming"]["kind"] = p.resuming.kind
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
    d["resuming"] = _job_from(d["resuming"])
    d["queue"] = [Queued(**{**q, "material": _material_from(q["material"])}) for q in d["queue"]]
    if (mmu := d["mmu"]) is not None:
        # `SimPrinter.__post_init__` points every slot at this one path again.
        d["mmu"] = MmuUnit(**{**mmu, "path": FilamentPath(**mmu["path"])})
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
