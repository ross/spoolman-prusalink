"""The simulated printers: tools, counters, jobs, and the timed processes that change them.

The model has no clock. `Simulation.tick(dt)` is the only place time passes, so tests drive
it directly and the server calls it with scaled real time.
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
import uuid as uuidlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from simulator import gcode

if TYPE_CHECKING:
    from simulator.config import SimConfig

EVENT_LIMIT = 2000
QUEUE_LIMIT = 1000  # items per SSE client before the oldest are dropped
SNAPSHOT_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
EPSILON = 1e-9


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
    UNPARK = "UNPARK"  # PAUSED briefly while the head unparks, then the nozzle clean purges


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

    MODEL_CHECK = "model check"  # the print preview asks about a file for another model
    PARSE_ERROR = "parse error"  # a sync job's G-code can't run
    JAM = "jam"
    RUNOUT = "runout"
    COLOR_CHANGE = "color change"
    NO_FILAMENT = "no filament"  # a print reached a tool that has none
    COLD_BED = "cold bed"  # the bed went cold during a power panic


# The reasons each waiting phase can have. A jam waits in HOLD when it's cleared in place,
# and in WAIT_LOAD when it unloads the tool.
PHASE_REASONS = {
    JobPhase.HOLD: frozenset({Reason.MODEL_CHECK, Reason.PARSE_ERROR, Reason.JAM}),
    JobPhase.WAIT_LOAD: frozenset(
        {Reason.RUNOUT, Reason.JAM, Reason.COLOR_CHANGE, Reason.NO_FILAMENT}
    ),
    JobPhase.AWAIT_RESUME: frozenset({Reason.COLD_BED}),
}


@dataclass(frozen=True)
class Attention:
    """What the screen asks the user, with the tool it names or a parse error's text."""

    reason: Reason
    tool: int | None = None
    detail: str | None = None

    def __str__(self) -> str:
        match self.reason:
            case Reason.MODEL_CHECK:
                return "model check"
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
    uuids: bool = False
    gap_mm: float = 40.0
    max_tools: int = 1
    counters: bool = True


def _models(*infos: ModelInfo) -> dict[str, ModelInfo]:
    return {m.code: m for m in infos}


MODELS = _models(
    ModelInfo("1.3.5", "MK3.5", counters=False),
    ModelInfo("1.3.6", "MK3.5S", counters=False),
    ModelInfo("1.3.9", "MK3.9"),
    ModelInfo("1.3.10", "MK3.9S"),
    ModelInfo("1.4.0", "MK4"),
    ModelInfo("1.4.1", "MK4S"),
    ModelInfo("2.1.0", "MINI", counters=False),
    ModelInfo("3.1.0", "XL", max_tools=5),
    ModelInfo("3.1.1", "XLP", max_tools=5),
    ModelInfo("5.1.0", "XL", max_tools=5),
    ModelInfo("4.1.0", "iX"),
    ModelInfo("7.1.0", "COREONE", uuids=True),
    ModelInfo("8.1.0", "COREONEL", uuids=True),
    ModelInfo("7.10.0", "COREONEINDX", gap_mm=27.0, max_tools=8),
    ModelInfo("8.10.0", "COREONEL-INDX", gap_mm=27.0, max_tools=8),
)


# Nozzle, preheat and bed temperatures of the firmware's preset filaments
# (`src/common/filament_presets.cpp`), with the preheat for printers with a load cell.
PRESET_TEMPERATURES = {
    "PLA": (215, 170, 60),
    "PETG": (230, 170, 85),
    "ASA": (260, 170, 100),
    "PC": (275, 250, 100),
    "PVB": (215, 170, 75),
    "ABS": (255, 170, 100),
    "HIPS": (220, 170, 100),
    "PP": (240, 170, 100),
    "FLEX": (240, 210, 50),
    "PA": (285, 260, 100),
    "PPS": (320, 170, 105),
    "PPA": (320, 170, 105),
    "PVA": (210, 160, 60),
    "BVOH": (210, 160, 60),
}
# What `M865 R` resets them to (`FilamentTypeParameters` in `filament.hpp`).
DEFAULT_TEMPERATURES = (215, 170, 60)


@dataclass
class Material:
    name: str
    color: str | None = None
    instance_uuid: str | None = None
    # A preset has no color or UUID and ignores edits; tag loads, loads with a picked color,
    # and M865 X writes are ad-hoc. The page shows the flag; PrusaLink doesn't.
    adhoc: bool = True
    # Set when the material is built, as the firmware does: from the preset, the tag or
    # M865. None, as in a snapshot from before temperatures, reads as the default.
    nozzle: int | None = None
    preheat: int | None = None
    bed: int | None = None
    # Set by a tag load only. M865 X R starts afresh without them; edits and runout
    # reloads keep them.
    material_uuid: str | None = None
    package_uuid: str | None = None

    def temperatures(self) -> tuple[int, int, int]:
        """Return the nozzle, preheat and bed temperatures the printer reports."""
        set_ = (self.nozzle, self.preheat, self.bed)
        return tuple(d if v is None else v for v, d in zip(set_, DEFAULT_TEMPERATURES, strict=True))


def preset_temperatures(name: str) -> dict[str, int]:
    """Return the temperatures a material named `name` starts from, as `Material` keywords.

    A preset's name matches exactly, as `FilamentType::from_name` does. A tag named TPU
    starts from FLEX (`openprinttag/data_utils.cpp:217-219`). Anything else gets the defaults.
    """
    key = "FLEX" if name == "TPU" else name
    values = PRESET_TEMPERATURES.get(key, DEFAULT_TEMPERATURES)
    return dict(zip(("nozzle", "preheat", "bed"), values, strict=True))


POSITIVE_SETTINGS = frozenset({"time_scale", "load_rate_mm_s", "print_rate_mm_s"})


@dataclass
class Settings:
    time_scale: float = 1.0
    feed_ratio: float = 0.95
    print_rate_mm_s: float = 5.0
    load_rate_mm_s: float = 10.0
    unload_seconds: float = 3.0
    load_feed_mm: float = 36.0
    purge_mm: float = 40.0
    toolchange_purge_mm: float = 30.0
    jam_purge_mm: float = 60.0
    preheat_seconds: float = 5.0
    scan_seconds: float = 0.5
    blip_seconds: float = 0.3
    park_seconds: float = 1.0
    sync_job_seconds: float = 3.0
    recover_seconds: float = 2.0  # BUSY after a power-panic boot
    unpark_seconds: float = 1.0  # PAUSED while the head unparks on a power-panic resume
    panic_purge_mm: float = 25.0  # the nozzle clean's purge on a power-panic resume
    auto_accept_model_check: bool = False
    start_blip: bool = True

    def __post_init__(self) -> None:
        """Check every field, so no way of building a `Settings` can hold a bad value."""
        for f in fields(self):
            key = f.name
            value = getattr(self, key)
            if isinstance(f.default, bool):
                if not isinstance(value, bool):
                    raise Invalid(f"{key} must be true or false")
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
class LoadProcess:
    phase: str  # FEED_GAP (not counted), FEED (counted, old material), PURGE (new material)
    remaining: float  # mm left in this phase
    feed_mm: float
    purge_mm: float
    material: Material | None  # set at the purge; None restores the type the unload left
    kind: str = "load"


@dataclass
class UnloadProcess:
    remaining: float  # seconds
    kind: str = "unload"


@dataclass
class SimTool:
    index: int
    enabled: bool = True
    loaded: bool = False
    material: Material | None = None
    previous: Material | None = None  # the ad-hoc slot: the last ad-hoc material written
    kept_type: Material | None = None  # the type the last unload left, for a runout reload
    extruded: float = 0.0
    unload_count: int = 0
    extruded_at_unload: int = 0
    gap: float = 0.0
    sensor_fault: bool = False
    stuck_write: bool = False
    temps: dict[str, int] = field(default_factory=dict)
    process: LoadProcess | UnloadProcess | None = None


@dataclass
class Segment:
    tool: int
    slicer_mm: float


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

    @classmethod
    def from_dict(cls, data: dict) -> PrintSpec:
        try:
            segments = [Segment(int(s["tool"]), float(s["slicer_mm"])) for s in data["segments"]]
            return cls(
                segments=segments,
                duration_s=_opt_float(data.get("duration_s")),
                preheat_s=_opt_float(data.get("preheat_s")),
                color_changes_mm=[float(x) for x in data.get("color_changes_mm") or []],
                name=str(data.get("name") or "part.bgcode"),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise Invalid(f"bad print: {e}") from None


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


@dataclass(kw_only=True)
class SimJob:
    """What every job has. A job is a `UserJob` or a `SyncJob`."""

    kind: ClassVar[str]
    id: int | None = None  # None while the preview asks its question: the job isn't counted yet
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
    toolchange_purge: bool = False  # a tool change into an empty tool purges after the reload
    cold_bed: bool = False  # a power-panic resume waits for the user when the bed went cold
    clean_at: float | None = None  # RECOVER time left when the nozzle clean purges

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


def _check_name(name: Any) -> str:
    if not isinstance(name, str) or not gcode.NAME_RE.match(name):
        raise Invalid(f"filament name {name!r} must be 1 to 7 of A-Z a-z 0-9 _ -")
    return name


def _check_temperatures(temps: Any) -> tuple[int, int, int]:
    ok = isinstance(temps, list | tuple) and len(temps) == 3
    if not ok or not all(isinstance(v, int) and 0 <= v <= 500 for v in temps):
        raise Invalid(f"temperatures must be [nozzle, preheat, bed] in °C, got {temps!r}")
    return tuple(temps)


def _check_color(color: Any) -> str | None:
    if color is None or color == "":
        return None
    if not isinstance(color, str) or not gcode.COLOR_RE.match(color):
        raise Invalid(f"color {color!r} must look like #rrggbb")
    return color.lower()


def _check_uuid(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        return str(uuidlib.UUID(str(value)))
    except ValueError:
        raise Invalid(f"{value!r} isn't a UUID") from None


def _describe(m: Material | None) -> str:
    if m is None:
        return "nothing"
    extra = "".join(f" {x}" for x in (m.color, m.instance_uuid) if x)
    return m.name + extra


def _feed(tool: SimTool, mm: float) -> None:
    """Feed filament: refill the retraction gap first, count only what goes past it."""
    fill = min(tool.gap, mm)
    tool.gap -= fill
    tool.extruded += mm - fill


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
    auto_accept_model_check: bool | None = None
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

    def _log(self, kind: str, text: str, data: dict | None = None) -> None:
        if self._sim:
            self._sim.log(self.name, kind, text, data)

    def _show_state(self) -> None:
        """Log the state when it changed. Called wherever a phase, the screen or a load changes."""
        state = self.state
        if state is not self._shown:
            self._log("state", f"{self._shown} -> {state}")
            self._shown = state

    def _tool(self, index: Any, *, enabled: bool = True) -> SimTool:
        tool = self.tools.get(index) if isinstance(index, int) else None
        if tool is None:
            raise Invalid(f"{self.name} has no tool {index!r}")
        if enabled and not tool.enabled:
            raise Invalid(f"tool {index} is disabled")
        return tool

    def _need_power(self) -> None:
        if self.power is Power.OFF:
            raise Busy(f"{self.name} is off")

    def _set_material(self, tool: SimTool, material: Material | None) -> None:
        """Write the stored type. An ad-hoc type also fills the ad-hoc slot (`previous`)."""
        tool.material = material
        if material is not None and material.adhoc:
            tool.previous = replace(material)

    def _kept(self, tool: SimTool) -> Material | None:
        """The type a load without a source restores: the one the unload left, else the slot."""
        kept = tool.kept_type or tool.previous
        return None if kept is None else replace(kept)

    def _count_unload(self, tool: SimTool, why: str) -> None:
        # The firmware counts a write of "none" over a stored type, nothing else.
        if tool.material is None:
            self._log("unload", f"T{tool.index} {why}: no type stored, not counted")
            return
        tool.unload_count += 1
        tool.extruded_at_unload = int(tool.extruded)
        tool.kept_type, tool.material = tool.material, None
        self._log(
            "unload",
            f"T{tool.index} {why}: unload_count {tool.unload_count} at {tool.extruded_at_unload}",
        )

    def _physical_unload(self, tool: SimTool, why: str) -> None:
        self._count_unload(tool, why)
        tool.loaded = False
        tool.gap = self.model.gap_mm

    def _idle_sensor_clear(self, tool: SimTool, *, sensor_fault_counts: bool) -> None:
        empty = not tool.loaded or (sensor_fault_counts and tool.sensor_fault)
        if tool.material is not None and empty:
            self._count_unload(tool, "idle sensor clear")

    # PrusaLink views.

    def job_visible(self) -> bool:
        job = self.job
        return job is not None and job.id is not None and job.phase is not JobPhase.BLIP

    def can_print(self) -> bool:
        # The firmware's rule is state-only: IDLE, READY, STOPPED or FINISHED. A print asked
        # for during the PrintInit blip, which reads IDLE, passes that rule and is then
        # dropped; `print_file` handles it. A running load or unload reads BUSY, so it
        # refuses too.
        return self.state in IDLE_STATES and self.job is None and self.power is not Power.OFF

    def status_json(self) -> dict:
        printer: dict[str, Any] = {
            "state": str(self.state),
            "temp_bed": 0.0,
            "target_bed": 0.0,
            "temp_nozzle": 0.0,
            "target_nozzle": 0.0,
            "axis_z": 0.0,
            "flow": 100,
            "speed": 100,
            "fan_hotend": 0,
            "fan_print": 0,
        }
        if self.state is not State.PRINTING:
            printer.update(axis_x=0.0, axis_y=0.0)
        out: dict[str, Any] = {
            "printer": printer,
            "storage": {"path": "/usb/", "name": "usb", "read_only": False},
        }
        if self.job_visible():
            job = self.job
            out["job"] = {
                "id": job.id,
                "progress": job.progress(),
                "time_printing": int(job.time_printing),
            }
        return out

    def job_json(self) -> dict | None:
        if not self.job_visible():
            return None
        job = self.job
        paused = self.state in (State.PAUSED, State.ATTENTION)
        path = f"/usb/{job.name}"
        return {
            "id": job.id,
            "state": "PAUSED" if paused else "PRINTING",
            "progress": job.progress(),
            "time_printing": int(job.time_printing),
            "file": {
                "name": job.name,
                "display_name": job.name,
                "path": path,
                "refs": {"download": path},
            },
        }

    def _material_json(self, tool: SimTool) -> dict | None:
        m = tool.material
        if m is None or tool.sensor_fault:
            return None
        nozzle, preheat, bed = m.temperatures()
        out: dict[str, Any] = {
            "name": m.name,
            "nozzle_temperature": nozzle,
            "preheat_temperature": preheat,
            "bed_temperature": bed,
        }
        if m.color:
            out["color"] = m.color
        if self.model.uuids and tool.index == 0:
            for key in ("material_uuid", "package_uuid", "instance_uuid"):
                if value := getattr(m, key):
                    out[key] = value
        return out

    def filament_json(self) -> dict | None:
        if self.firmware is Firmware.NO_ENDPOINT:
            return None
        counters = self.firmware is Firmware.FULL
        tools = []
        for tool in sorted(self.tools.values(), key=lambda t: t.index):
            if not tool.enabled:
                continue
            entry: dict[str, Any] = {"index": tool.index}
            if counters:
                entry.update(
                    extruded=int(tool.extruded),
                    unload_count=tool.unload_count,
                    extruded_at_unload=tool.extruded_at_unload,
                )
            material = self._material_json(tool)
            if material:
                entry["material"] = material
            tools.append(entry)
        return {"boot_id": self.boot_id, "tools": tools} if counters else {"tools": tools}

    def version_json(self) -> dict:
        return {
            "api": "2.0.0",
            "server": "2.1.2",
            "text": "PrusaLink",
            "firmware": "6.4.0-sim",
            "hostname": self.name,
            "printer": self.model.code,
            "nozzle_diameter": 0.4,
            "capabilities": {"upload-by-put": True},
        }

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
        """Print a stored file. A wrong model asks first, before the firmware counts the job."""
        if self.job is not None and self.job.phase is JobPhase.BLIP:
            # The firmware's state check passes during PrintInit, but print_start ignores
            # the request there, so the upload succeeds and the print that's starting goes
            # on (`marlin_server.cpp:1527-1550` at 2db93db5).
            self._log("job", f"{name}: asked for during the start blip; the firmware drops it")
            return
        if not self.can_print():
            raise Busy("Can't print right now")
        body = self.files[name]
        wrong = self._wrong_model(body)
        if wrong and not self._auto_accept():
            job = SyncJob(name=name, gcode=body)
            job.enter(JobPhase.HOLD, Attention(Reason.MODEL_CHECK))
            self.job = job
            self._log("job", f"{name}: the preview asks about model {wrong}")
            self._show_state()
            return
        if wrong:
            self._log("gcode", f"model check for {wrong} accepted automatically")
        self._start_job(SyncJob(name=name, gcode=body))

    # Actions on tools.

    def load(
        self,
        tool: int,
        *,
        source: str | None = None,
        name: str | None = None,
        color: str | None = None,
        uuid: str | None = None,
        feed_mm: float | None = None,
        purge_mm: float | None = None,
        temperatures: Any = None,
    ) -> None:
        """Load filament into a tool: feed over the gap, counted feed, set the type, purge.

        `source` None keeps the previous type and is only for the reload a runout waits
        for. While the printer is off the filament is pushed in by hand: the type is set
        at once and nothing is counted. `temperatures` is a tag's nozzle, preheat and bed
        temperatures; without it a tag load takes the preset's.
        """
        t = self._tool(tool)
        material = self._new_material(t, source, name, color, uuid, temperatures)
        job = self.job
        waiting = job is not None and job.wait_tool == tool
        if source is None and not waiting:
            raise Invalid("a load needs a source, tag or preset, outside a runout reload")
        kept = t.kept_type or t.previous
        what = f"{source} {_describe(material)}" if source else f"keep {_describe(kept)}"
        if self.power is Power.OFF:
            self._hand_load(t, material)
            return
        if t.process is not None or self.state is State.BUSY:
            raise Busy("a load or unload is already running")
        if job is not None and not waiting:
            raise Busy("can't load during a print; run the tool out first")
        self._log("action", f"load T{tool} {what}")
        feed = self.settings.load_feed_mm if feed_mm is None else _positive(feed_mm, "feed_mm")
        purge = self.settings.purge_mm if purge_mm is None else _positive(purge_mm, "purge_mm")
        if waiting and job.waits_for(Reason.JAM):
            purge += self.settings.jam_purge_mm
        t.process = LoadProcess("FEED_GAP", t.gap, feed, purge, material)
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
        name, color = _check_name(name), _check_color(color)
        if source == "preset":
            if uuid or temps is not None:
                raise Invalid("a preset load has no UUID or temperatures")
            # Picking a color makes the preset ad-hoc, which replaces the ad-hoc slot.
            return Material(name, color, adhoc=color is not None, **preset_temperatures(name))
        if not self.model.uuids:
            raise Invalid(f"{self.model.name} has no tag reader")
        value = _check_uuid(uuid)
        tag_temps = preset_temperatures(name)
        if temps is not None:
            tag_temps = dict(zip(tag_temps, _check_temperatures(temps), strict=True))
        if t.index != 0:
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
        if t.process is not None:
            raise Busy("a load or unload is already running")
        if material is None:
            material = self._kept(t)
        self._set_material(t, material)
        t.loaded, t.gap = True, 0.0
        self._log("load", f"T{t.index} loaded by hand while off: {_describe(material)}")

    def unload(self, tool: int) -> None:
        """Unload a tool. While the printer is off the type stays stored until the next boot."""
        t = self._tool(tool)
        if not t.loaded:
            raise Invalid(f"T{tool} has no filament")
        if self.power is Power.OFF:
            if t.process is not None:
                raise Busy("a load or unload is already running")
            self._log("action", f"unload T{tool} by hand while off")
            t.loaded, t.gap = False, self.model.gap_mm
            return
        if self.job is not None:
            raise Busy("can't unload during a print; use runout")
        if t.process is not None or self.state not in IDLE_STATES:
            raise Busy(f"can't unload while {self.state}")
        self._log("action", f"unload T{tool}")
        t.process = UnloadProcess(self.settings.unload_seconds)
        self.screen = State.IDLE  # using the menu dismisses a finished or stopped screen
        self._show_state()

    def _manual_move(self, tool: int, mm: Any) -> tuple[SimTool, float]:
        t = self._tool(tool)
        mm = _positive(mm, "mm")
        if self.power is Power.OFF or self.job is not None or self.state not in IDLE_STATES:
            raise Busy(f"manual moves need an idle printer, not {self.state}")
        if not t.loaded:
            raise Invalid(f"T{tool} has no filament")
        return t, mm

    def extrude(self, tool: int, mm: float) -> None:
        t, mm = self._manual_move(tool, mm)
        _feed(t, mm)
        self._log("action", f"extrude T{tool} {mm:g} mm: extruded {int(t.extruded)}")

    def retract(self, tool: int, mm: float) -> None:
        t, mm = self._manual_move(tool, mm)
        t.gap = min(t.gap + mm, self.model.gap_mm)
        self._log("action", f"retract T{tool} {mm:g} mm: gap {t.gap:g}")

    def select_tool(self, tool: int) -> None:
        if self.model.max_tools == 1:
            raise Invalid(f"{self.model.name} has one tool")
        t = self._tool(tool)
        if self.job is not None or self.state is State.BUSY:
            raise Busy(f"can't pick a tool while {self.state}")
        self.selected_tool = tool
        self._log("action", f"select T{tool}")
        if self.power is not Power.OFF:
            self._idle_sensor_clear(t, sensor_fault_counts=True)

    # Actions on prints.

    def start_print(self, spec: PrintSpec) -> None:
        self._need_power()
        if not self.can_print():
            raise Busy(f"can't print while {self.state}")
        if not spec.segments:
            raise Invalid("a print needs at least one segment")
        for s in spec.segments:
            self._tool(s.tool)
            _positive(s.slicer_mm, "slicer_mm")
        if spec.duration_s is not None:
            _positive(spec.duration_s, "duration_s")
        if spec.preheat_s is not None and spec.preheat_s < 0:
            raise Invalid("preheat_s can't be negative")
        total = spec.total_mm
        changes = sorted(x for x in spec.color_changes_mm if 0 < x < total)
        self._start_job(UserJob(name=spec.name, spec=spec, pending_color_changes=changes))

    def _start_job(self, job: UserJob | SyncJob) -> None:
        """Count the job and scan the file: the firmware does this once the preview is confirmed."""
        self.homed = True  # a print starts by homing
        self.job_counter += 1
        job.id = self.job_counter
        job.enter(JobPhase.SCANNING)
        job.remaining = self.settings.scan_seconds
        self.job = job
        self._log("job", f"job {job.id} ({job.kind}) {job.name} started")
        self._show_state()

    def pause(self) -> None:
        job = self.job
        if job is None or job.phase not in (JobPhase.PRINTING, JobPhase.PREHEAT):
            raise Busy("nothing to pause")
        self._log("action", "pause")
        job.paused_from = job.phase
        job.enter(JobPhase.PAUSED)
        self._show_state()

    def resume(self) -> None:
        job = self.job
        if job is not None and job.phase is JobPhase.AWAIT_RESUME:
            self._log("action", "resume after the power panic")
            job.cold_bed = False
            self._recover(job, clean=True)
            return
        if job is None or job.phase is not JobPhase.PAUSED or self.state is not State.PAUSED:
            raise Busy("nothing to resume")
        self._log("action", "resume")
        if job.paused_from is JobPhase.PREHEAT:
            job.enter(JobPhase.PREHEAT)
            self._show_state()
        else:
            self._begin_feeding(job)
        job.paused_from = None

    def cancel(self) -> None:
        job = self.job
        if job is None or job.phase is JobPhase.PARKING:
            raise Busy("nothing to cancel")
        if job.id is None:
            # Declining the preview: no job was counted, so there's nothing to park.
            self._log("action", f"cancel {job.name} at the preview")
            self.job, self.screen = None, State.IDLE
            self._show_state()
            return
        self._log("action", f"cancel job {job.id}")
        for t in self.tools.values():
            self._abort_process(t)
        self._park(job, State.STOPPED)

    def _abort_process(self, t: SimTool) -> None:
        """Stop a load or unload where it stands.

        The type is set at the start of the purge, so a load stopped before then leaves the
        tool empty with no type, and one stopped in the purge keeps its type and counts as
        loaded. A stopped unload leaves the filament in. This is a simplification of the
        firmware's abort paths.
        """
        p = t.process
        if p is None:
            return
        t.process = None
        if isinstance(p, LoadProcess) and p.phase == "PURGE":
            t.loaded = True
        self._log("load", f"T{t.index} {p.kind} stopped by the cancel")

    def dismiss(self) -> None:
        if self.job is not None or self.state not in (State.FINISHED, State.STOPPED):
            raise Busy(f"nothing to dismiss in {self.state}")
        self._log("action", "dismiss")
        self.screen = State.IDLE
        self._show_state()

    def home(self) -> None:
        """Home from the menu. Only the flag changes: homing moves no filament."""
        self._need_power()
        if self.job is not None or self.state not in (State.IDLE, State.FINISHED, State.STOPPED):
            raise Busy(f"can't home in {self.state}")
        self.homed = True
        self._log("action", "home")

    def _check_runout(self, tool: int) -> SimTool:
        t = self._tool(tool)
        job = self.job
        printing = isinstance(job, UserJob) and job.phase in (JobPhase.PRINTING, JobPhase.PREHEAT)
        if self.power is Power.OFF or not printing:
            raise Busy("a runout or jam needs a print in progress")
        # Only the picked tool feeds, so only its sensor can see a runout or a jam.
        if t.index != self.selected_tool:
            raise Invalid(f"T{tool} isn't the selected tool")
        if not t.loaded:
            raise Busy(f"T{tool} has no filament")
        return t

    def runout(self, tool: int) -> None:
        t = self._check_runout(tool)
        self._log("action", f"runout T{tool}")
        self._runout(t, Reason.RUNOUT)

    def jam(self, tool: int, unload: bool = True) -> None:
        """A jam either unloads like a runout (and purges more on reload) or is cleared in place."""
        t = self._check_runout(tool)
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
        self._physical_unload(t, str(reason))
        job.enter(JobPhase.WAIT_LOAD, Attention(reason, t.index))
        self._show_state()

    def clear_jam(self) -> None:
        job = self.job
        if job is None or job.phase is not JobPhase.HOLD or not job.waits_for(Reason.JAM):
            raise Busy("no jam to clear")
        self._log("action", "clear jam")
        job.enter(JobPhase.PAUSED)
        self._show_state()

    def tap_through(self) -> None:
        """Answer the screen's question: accept the model check, or abort on a parse error."""
        job = self.job
        if job is None or job.phase is not JobPhase.HOLD or not isinstance(job, SyncJob):
            raise Busy("nothing to tap through")
        self._log("action", f"tap through: {job.attention}")
        if job.waits_for(Reason.MODEL_CHECK):
            self._start_job(job)
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
            t.extruded, t.unload_count, t.extruded_at_unload, t.gap = 0.0, 0, 0, 0.0
            t.process = None
        self.homed, self.message = False, None
        self._log("boot", f"boot id {self.boot_id}")
        selected = self.tools.get(self.selected_tool)
        if idle_clear and selected is not None:
            self._idle_sensor_clear(selected, sensor_fault_counts=False)

    def reboot(self, *, keep_job: bool = False, cold_bed: bool = False) -> None:
        """Reboot. With keep_job a print in progress resumes with the same ID (power panic).

        The resume is BUSY while the firmware recovers, during which the nozzle clean
        purges `panic_purge_mm`, then PAUSED while the head unparks, then PRINTING. With
        `cold_bed` it waits in ATTENTION for `resume()` before recovering. A print that was
        paused comes back paused.
        """
        self._need_power()
        self._log("action", "power loss and resume" if keep_job else "reboot")
        self._reboot(keep_job, cold_bed)

    def _reboot(self, keep_job: bool, cold_bed: bool = False) -> None:
        job = self.job
        resumable = (JobPhase.PRINTING, JobPhase.PAUSED, JobPhase.PREHEAT, JobPhase.WAIT_LOAD)
        keep = keep_job and isinstance(job, UserJob) and job.phase in resumable
        was_paused = keep and job.phase in (JobPhase.PAUSED, JobPhase.WAIT_LOAD)
        self.boot(idle_clear=not keep)
        if not keep:
            self.job, self.screen = None, State.IDLE
            self._show_state()
            return
        if was_paused:
            job.paused_from = JobPhase.PRINTING
            job.enter(JobPhase.PAUSED)
            self._show_state()
            return
        job.paused_from = None
        job.cold_bed = cold_bed
        self._recover(job, clean=not cold_bed)

    def _recover(self, job: UserJob, *, clean: bool) -> None:
        """BUSY while the firmware heats and homes; the nozzle clean purges halfway through."""
        job.enter(JobPhase.RECOVER)
        job.remaining = self.settings.recover_seconds
        job.clean_at = self.settings.recover_seconds / 2 if clean else None
        self._show_state()

    def _unpark(self, job: UserJob) -> None:
        job.enter(JobPhase.UNPARK)
        job.remaining = self.settings.unpark_seconds
        self._show_state()

    def _nozzle_clean(self, job: UserJob) -> None:
        """Core One and Core One L clean the nozzle on a power-panic resume, which purges."""
        tool = self.tools[job.spec.segments[job.segment].tool]
        if tool.loaded:
            _feed(tool, self.settings.panic_purge_mm)
            self._log("job", f"nozzle clean purged {self.settings.panic_purge_mm:g} mm")

    def set_power(self, power: str, *, resume: bool = False, cold_bed: bool = False) -> None:
        """Off freezes the printer; leaving off boots it, which drops any job unless `resume`."""
        power = _enum(Power, power)
        old, self.power = self.power, power
        self._log("action", f"power {power}")
        if old is Power.OFF and power is not Power.OFF:
            self._reboot(resume, cold_bed)

    def set_fault(self, fault: str) -> None:
        self.fault = _enum(Fault, fault)
        self._log("action", f"fault {self.fault}")

    def set_firmware(self, firmware: str) -> None:
        self.firmware = _enum(Firmware, firmware)
        self._log("action", f"firmware {self.firmware}")

    def set_sensor_fault(self, tool: int, value: bool) -> None:
        self._tool(tool).sensor_fault = bool(value)
        self._log("action", f"T{tool} sensor fault {'on' if value else 'off'}")

    def set_stuck_write(self, tool: int, value: bool) -> None:
        self._tool(tool).stuck_write = bool(value)
        self._log("action", f"T{tool} stuck write {'on' if value else 'off'}")

    def set_tool_enabled(self, tool: int, value: bool) -> None:
        t = self._tool(tool, enabled=False)
        if not value:
            in_print = (
                self.job and self.job.spec and tool in {s.tool for s in self.job.spec.segments}
            )
            if tool == self.selected_tool or in_print or t.process is not None:
                raise Invalid(f"T{tool} is in use")
        t.enabled = bool(value)
        self._log("action", f"T{tool} {'enabled' if value else 'disabled'}")

    def set_auto_accept(self, value: bool | None) -> None:
        self.auto_accept_model_check = None if value is None else bool(value)
        self._log("action", f"auto-accept model check {value}")

    def counter_step_back(self, tool: int, mm: float) -> None:
        t = self._tool(tool)
        t.extruded = max(0.0, t.extruded - _positive(mm, "mm"))
        self._log("action", f"T{tool} extruded stepped back to {int(t.extruded)}")

    def set_extruded_at_unload(self, tool: int, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise Invalid("value must be an integer")
        self._tool(tool).extruded_at_unload = value
        self._log("action", f"T{tool} extruded_at_unload set to {value}")

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
        t = self.tools.get(self.selected_tool)
        if self.job is None and self.state in IDLE_STATES and t is not None and t.process is None:
            self._idle_sensor_clear(t, sensor_fault_counts=True)

    def _tick_process(self, t: SimTool, dt: float) -> None:
        rate = self.settings.load_rate_mm_s
        while t.process is not None and dt > EPSILON:
            p = t.process
            if isinstance(p, UnloadProcess):
                if p.remaining > dt:
                    p.remaining -= dt
                    return
                t.process = None
                self._physical_unload(t, "unload")
                self._process_done()
                return
            step = min(p.remaining, rate * dt)
            if p.phase == "FEED_GAP":
                t.gap = max(0.0, t.gap - step)
            else:
                t.extruded += step
            p.remaining -= step
            dt -= step / rate
            if p.remaining <= EPSILON:
                self._next_load_phase(t, p)

    def _next_load_phase(self, t: SimTool, p: LoadProcess) -> None:
        if p.phase == "FEED_GAP":
            p.phase, p.remaining, t.gap = "FEED", p.feed_mm, 0.0
        elif p.phase == "FEED":
            material = p.material if p.material is not None else self._kept(t)
            self._set_material(t, material)
            self._log("load", f"T{t.index} type set to {_describe(material)}")
            p.phase, p.remaining = "PURGE", p.purge_mm
        else:
            t.loaded, t.process = True, None
            self._log("load", f"T{t.index} loaded: extruded {int(t.extruded)}")
            self._process_done()

    def _process_done(self) -> None:
        job = self.job
        if job is not None and job.phase is JobPhase.WAIT_LOAD:
            job.enter(JobPhase.PAUSED)
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
        s = self.settings
        if job.phase is JobPhase.SCANNING and s.start_blip:
            job.enter(JobPhase.BLIP)
            job.remaining = s.blip_seconds
            self._show_state()
        elif job.phase in (JobPhase.SCANNING, JobPhase.BLIP):
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
        tool = job.spec.segments[job.segment].tool
        self.selected_tool = tool
        if self.tools[tool].loaded:
            if job.toolchange_purge:
                job.toolchange_purge = False
                _feed(self.tools[tool], self.settings.toolchange_purge_mm)
            job.enter(JobPhase.PRINTING)
            self._show_state()
        else:
            self._wait_for_filament(job, tool)

    def _wait_for_filament(self, job: UserJob, tool: int) -> None:
        job.paused_from = JobPhase.PRINTING
        job.enter(JobPhase.WAIT_LOAD, Attention(Reason.NO_FILAMENT, tool))
        self._show_state()

    def _park(self, job: UserJob | SyncJob, end: State) -> None:
        job.enter(JobPhase.PARKING)
        job.remaining, job.end_state = self.settings.park_seconds, end
        if isinstance(job, UserJob):
            job.paused_from = None
        self._show_state()

    def _print_step(self, job: UserJob, dt: float) -> float:
        """Feed for dt, stopping at each segment end and color change. Return the time left."""
        spec = job.spec
        total = spec.total_mm
        rate = total / spec.duration_s if spec.duration_s else self.settings.print_rate_mm_s
        ends = [sum(s.slicer_mm for s in spec.segments[: i + 1]) for i in range(len(spec.segments))]
        ratio = self.settings.feed_ratio
        while dt > EPSILON and job.phase is JobPhase.PRINTING:
            tool = self.tools[spec.segments[job.segment].tool]
            change = job.pending_color_changes[0] if job.pending_color_changes else None
            target = ends[job.segment] if change is None else min(change, ends[job.segment])
            need = max(0.0, target - job.fed_mm)
            if rate * dt < need:
                _feed(tool, rate * dt * ratio)
                job.fed_mm += rate * dt
                job.time_printing += dt
                return 0.0
            _feed(tool, need * ratio)
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
        tool = self.tools[new]
        if not tool.loaded:
            job.toolchange_purge = True
            self._wait_for_filament(job, new)
            return
        _feed(tool, self.settings.toolchange_purge_mm)

    # The sync job.

    def _auto_accept(self) -> bool:
        if self.auto_accept_model_check is not None:
            return self.auto_accept_model_check
        return self.settings.auto_accept_model_check

    def _run_sync(self, job: SyncJob) -> None:
        try:
            commands = gcode.parse(job.gcode)
        except gcode.GcodeError as e:
            self._hold(job, str(e))
            return
        problem = self._tool_problem(commands)
        if problem:
            self._hold(job, problem)
            return
        self._run_commands(job, commands)

    def _wrong_model(self, body: str) -> str | None:
        """The model a file's M862.3 names when it isn't this one. A parse error is left to the
        job, which stops on it."""
        try:
            commands = gcode.parse(body)
        except gcode.GcodeError:
            return None
        names = (self.model.name, f"{self.model.name}MMU3")
        for c in commands:
            if isinstance(c, gcode.ModelCheck) and c.name not in names:
                return c.name
        return None

    def _tool_problem(self, commands: list[gcode.Command]) -> str | None:
        # The firmware takes any index the model can have, enabled or not.
        last = self.model.max_tools - 1
        for c in commands:
            if not isinstance(c, gcode.SetFilament):
                continue
            for index in (c.tool, c.copy_from):
                if index is not None and not 0 <= index <= last:
                    return f"M865 names tool {index}; {self.model.name} has tools 0 to {last}"
        return None

    def _hold(self, job: SyncJob, problem: str) -> None:
        job.enter(JobPhase.HOLD, Attention(Reason.PARSE_ERROR, detail=problem))
        self._log("job", f"job {job.id} stopped: {job.attention}")
        self._show_state()

    def _run_commands(self, job: SyncJob, commands: list[gcode.Command]) -> None:
        self.apply(commands)
        job.enter(JobPhase.RUN)
        job.remaining = job.run_seconds = self.settings.sync_job_seconds
        self._show_state()

    def apply(self, commands: list[gcode.Command]) -> None:
        for c in commands:
            if isinstance(c, gcode.SetFilament):
                self._apply_filament(c)
            elif isinstance(c, gcode.SetUuid):
                self._apply_uuid(c)
            elif isinstance(c, gcode.Message):
                self.message = c.text
                self._log("gcode", f"M117 {c.text}")
            elif isinstance(c, gcode.Home):
                self.homed = True
                self._log("gcode", "G28: homed")
            elif isinstance(c, gcode.ModelCheck):
                self._log("gcode", f"M862.3 model check {c.name}")
            else:
                self._log("gcode", f"ignored: {c.line}")

    def _apply_filament(self, c: gcode.SetFilament) -> None:
        if not 0 <= c.tool < self.model.max_tools:
            self._log("gcode", f"M865 L{c.tool} dropped: no such tool")
            return
        # The config store has a slot for every tool the model can have, so a write to a
        # disabled or unconfigured tool lands; a disabled tool just isn't reported.
        t = self.tools.get(c.tool)
        if t is None:
            t = self.tools[c.tool] = SimTool(c.tool, enabled=False)
        if t.stuck_write:
            self._log("gcode", f"M865 L{c.tool} ignored: stuck write")
            return
        if c.copy_from is not None:
            self._copy_filament(t, self.tools.get(c.copy_from) or SimTool(c.copy_from))
            return
        if c.reset or c.adhoc:
            # An ad-hoc write edits the ad-hoc slot; R starts it afresh.
            old = t.previous
            if c.reset or old is None:
                # R starts from the firmware defaults, not a preset's values.
                nozzle, preheat, bed = DEFAULT_TEMPERATURES
                name = c.name or (old.name if old else "Custom")
                material = Material(name, nozzle=nozzle, preheat=preheat, bed=bed)
                temps: dict[str, int] = {}
            else:
                material, temps = replace(old, adhoc=True), dict(t.temps)
            material.name = c.name or material.name
            material.color = c.color or material.color
            _apply_temperatures(material, c.temps)
            temps.update(c.temps)
            t.temps = temps
        elif c.name:
            # A preset write: the firmware ignores every parameter but the type.
            material = Material(c.name, adhoc=False, **preset_temperatures(c.name))
            if c.color or c.temps:
                self._log("gcode", f"M865 L{c.tool}: parameters ignored on preset {c.name}")
        elif (c.color or c.temps) and t.material is not None:
            # An edit of the stored type, which only an ad-hoc type takes.
            if not t.material.adhoc:
                self._log("gcode", f"M865 L{c.tool} ignored on preset {t.material.name}")
                return
            material = replace(t.material, color=c.color or t.material.color)
            _apply_temperatures(material, c.temps)
            t.temps = {**t.temps, **c.temps}
        else:
            self._log("gcode", f"M865 L{c.tool}: nothing to write")
            return
        self._set_material(t, material)
        self._log("gcode", f"M865 T{c.tool} set to {_describe(material)}")

    def _copy_filament(self, t: SimTool, source: SimTool) -> None:
        if source.material is None:
            self._count_unload(t, f"M865 copied empty T{source.index}")
            return
        material = replace(source.material)
        if not (self.model.uuids and t.index == 0):
            material.instance_uuid = material.material_uuid = material.package_uuid = None
        self._set_material(t, material)
        self._log("gcode", f"M865 T{t.index} copied from T{source.index}: {_describe(material)}")

    def _apply_uuid(self, c: gcode.SetUuid) -> None:
        t = self.tools.get(c.tool)
        usable = self.model.uuids and c.tool == 0 and t is not None and t.material is not None
        if not usable or t.stuck_write:
            self._log("gcode", f"M865.1 I{c.tool} dropped")
            return
        if not t.material.adhoc:
            self._log("gcode", f"M865.1 I0 ignored on preset {t.material.name}")
            return
        t.material.instance_uuid = c.uuid
        t.previous = replace(t.material)
        self._log("gcode", f"M865.1 T0 instance UUID {c.uuid}")

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
                else {"reason": str(a.reason), "tool": a.tool, "text": str(a)},
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
            },
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
            "auto_accept_model_check": self.auto_accept_model_check,
            "tools": [_tool_json(t) for t in sorted(self.tools.values(), key=lambda t: t.index)],
        }


def _tool_json(t: SimTool) -> dict:
    out = asdict(t)
    p = t.process
    if p is None:
        out["process"] = None
    elif isinstance(p, LoadProcess):
        out["process"] = {"kind": "load", "phase": p.phase, "remaining_mm": p.remaining}
    else:
        out["process"] = {"kind": "unload", "phase": "UNLOAD", "remaining_s": p.remaining}
    return out


def _enum(cls: type, value: Any) -> Any:
    try:
        return cls(value)
    except ValueError:
        known = ", ".join(v.value for v in cls)
        raise Invalid(f"{value!r} isn't one of {known}") from None


def _apply_temperatures(material: Material, temps: dict[str, int]) -> None:
    for key in ("nozzle", "preheat", "bed"):
        if key in temps:
            setattr(material, key, temps[key])


# Snapshots: asdict() out, explicit reconstruction in.


def _material_from(d: dict | None) -> Material | None:
    return None if d is None else Material(**d)


def _process_from(d: dict | None) -> LoadProcess | UnloadProcess | None:
    if d is None:
        return None
    d = dict(d)
    kind = d.pop("kind")
    d.pop("resume_state", None)  # older snapshots
    if kind == "unload":
        return UnloadProcess(**d)
    d["material"] = _material_from(d["material"])
    return LoadProcess(**d)


def _tool_from(d: dict) -> SimTool:
    d = dict(d)
    d["material"] = _material_from(d["material"])
    d["previous"] = _material_from(d["previous"])
    d["kept_type"] = _material_from(d.get("kept_type"))
    d["process"] = _process_from(d["process"])
    return SimTool(**d)


def _job_from(d: dict | None) -> UserJob | SyncJob | None:
    if d is None:
        return None
    d = dict(d)
    kind = d.pop("kind")
    d["phase"] = JobPhase(d["phase"])
    d["end_state"] = State(d["end_state"])
    if (a := d["attention"]) is not None:
        d["attention"] = Attention(Reason(a["reason"]), a["tool"], a["detail"])
    if kind == "sync":
        return SyncJob(**d)
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
        self.listeners: list[Callable[[Event], None]] = []
        self._queues: list[asyncio.Queue] = []
        for p in printers or []:
            self.add(p)

    @classmethod
    def from_config(cls, cfg: SimConfig) -> Simulation:
        sim = cls(Settings(**cfg.settings), data_dir=cfg.data_dir)
        for pc in cfg.printers:
            model = MODELS[pc.model]
            default = Firmware.FULL if model.counters else Firmware.NO_COUNTERS
            tools = {tc.index: _tool_from_config(tc) for tc in pc.tools}
            enabled = sorted(i for i, t in tools.items() if t.enabled)
            sim.add(
                SimPrinter(
                    name=pc.name,
                    port=pc.port,
                    api_key=pc.api_key,
                    model=model,
                    firmware=Firmware(pc.firmware) if pc.firmware else default,
                    auto_accept_model_check=pc.auto_accept_model_check,
                    tools=tools,
                    selected_tool=enabled[0] if enabled else min(tools),
                )
            )
        return sim

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
        self.push(("log", event))
        self.changed()
        return event

    def subscribe(self) -> asyncio.Queue:
        """A queue of ("log", Event) and ("state", None) items for one SSE client.

        It's bounded so a client that stops reading costs a fixed amount of memory: `push`
        drops the oldest item to make room.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._queues:
            self._queues.remove(queue)

    def push(self, item: tuple) -> None:
        for queue in self._queues:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)

    def changed(self) -> None:
        self.push(("state", None))

    def tick(self, dt: float) -> None:
        if dt <= 0:
            return
        self.now += dt
        active = [p.tick(dt) for p in self.printers.values()]
        if any(active):
            self.changed()

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
        }
        if events:
            out["events"] = [asdict(e) for e in self.events[-200:]]
        return out


def _tool_from_config(tc: Any) -> SimTool:
    m = tc.material
    material = None
    if m:
        color, uuid = m.get("color"), m.get("instance_uuid")
        # A configured color or UUID can only come from an ad-hoc type; a bare name is a preset.
        adhoc = bool(color or uuid)
        material = Material(
            m["name"],
            color.lower() if color else None,
            uuid,
            adhoc,
            **preset_temperatures(m["name"]),
        )
    return SimTool(
        index=tc.index,
        enabled=tc.enabled,
        loaded=material is not None,
        material=material,
        previous=replace(material) if material and material.adhoc else None,
        extruded=float(tc.extruded),
        unload_count=tc.unload_count,
        extruded_at_unload=tc.extruded_at_unload,
    )
