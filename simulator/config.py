"""Simulator configuration from a TOML file, and the bridge config derived from it."""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field, fields

from simulator.model import (
    MODELS,
    Firmware,
    Invalid,
    Material,
    ModelInfo,
    Settings,
    SimPrinter,
    SimTool,
    Simulation,
    check_color,
    check_name,
    check_uuid,
)
from simulator.preview import Severity

_NAME_PATTERN = r"[A-Za-z0-9_.-]{1,64}"

_TOP_KEYS = {"host", "control_port", "data_dir", "printers"}
_PRINTER_KEYS = {
    "name",
    "model",
    "api_key",
    "port",
    "firmware",
    "checks",
    "high_flow",
    "auto_retract",
    "sensors",
    "tools",
}
_TOOL_KEYS = {"index", "enabled", "material", "extruded", "unload_count", "extruded_at_unload"}
_MATERIAL_KEYS = {"name", "color", "instance_uuid"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ToolConfig:
    index: int
    enabled: bool = True
    # {"name": str, "color": str | None, "instance_uuid": str | None}
    material: dict | None = None
    extruded: float = 0.0
    unload_count: int = 0
    extruded_at_unload: int = 0


@dataclass(frozen=True)
class PrinterConfig:
    name: str
    model: str
    api_key: str
    port: int
    # None means the model's default.
    firmware: str | None = None
    # How the print preview treats a failed check the printer's settings govern.
    checks: str = Severity.WARN.value
    high_flow: bool = False
    auto_retract: bool = True
    sensors: bool = True  # the menu switch for every filament sensor
    tools: tuple[ToolConfig, ...] = (ToolConfig(0),)


@dataclass(frozen=True)
class SimConfig:
    host: str = "127.0.0.1"
    control_port: int = 8000
    data_dir: str = "simulator/data"
    # Overrides for the fields of model.Settings, checked by name and type. A key that's
    # absent leaves the field's default, which for the load lengths is the model's.
    settings: dict[str, float | bool] = field(default_factory=dict)
    printers: tuple[PrinterConfig, ...] = ()


@dataclass(frozen=True)
class Location:
    """One tool's Spoolman location, as the bridge config names it."""

    printer: str
    tool: int
    location: str
    # Whether the printer's model has a tag reader, and whether this tool keeps tag UUIDs.
    uuids: bool
    holds_uuid: bool
    # The tool's configured material name and instance UUID, if any.
    material: str | None = None
    instance_uuid: str | None = None


def _int(value: object, name: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value!r}")
    return value


def _number(value: object, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    # A comparison, not math.isfinite, which overflows on an int too large for a float.
    if not -sys.float_info.max <= value <= sys.float_info.max:
        raise ConfigError(f"{name} must be a finite number, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value!r}")
    return float(value)


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false, got {value!r}")
    return value


def _str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string, got {value!r}")
    return value


def _port(value: object, name: str) -> int:
    port = _int(value, name)
    if not 1 <= port <= 65535:
        raise ConfigError(f"{name} must be between 1 and 65535, got {port}")
    return port


def _unknown(table: dict, allowed: set[str], prefix: str) -> None:
    for key in table:
        if key not in allowed:
            where = f"{prefix}.{key}" if prefix else key
            raise ConfigError(f"{where} isn't a known key")


def _material(value: object, prefix: str, holds_uuid: bool) -> dict:
    name = f"{prefix}.material"
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table with a name, got {value!r}")
    _unknown(value, _MATERIAL_KEYS, name)
    if "name" not in value:
        raise ConfigError(f"{name} needs a name")
    # The same checks as a load from the page, so a configured tool holds what a load could.
    out = {}
    for key, check in (("name", check_name), ("color", check_color), ("instance_uuid", check_uuid)):
        try:
            out[key] = check(value.get(key))
        except Invalid as e:
            raise ConfigError(f"{name}.{key}: {e}") from None
    if out["instance_uuid"] and not holds_uuid:
        raise ConfigError(f"{name}.instance_uuid needs a model that holds UUIDs, on tool 0")
    return out


def _tool(entry: object, prefix: str, model_code: str) -> ToolConfig:
    if not isinstance(entry, dict):
        raise ConfigError(f"{prefix} must be a table")
    _unknown(entry, _TOOL_KEYS, prefix)
    if "index" not in entry:
        raise ConfigError(f"{prefix} needs an index")
    info = MODELS[model_code]
    index = _int(entry["index"], f"{prefix}.index", 0)
    if index >= info.max_tools:
        raise ConfigError(
            f"{prefix}.index must be below {info.max_tools}, the most tools model "
            f"{model_code} has, got {index}"
        )
    enabled = _bool(entry.get("enabled", True), f"{prefix}.enabled")
    material = None
    if "material" in entry:
        material = _material(entry["material"], prefix, info.holds_uuid(index))
    extruded = _number(entry.get("extruded", 0), f"{prefix}.extruded", 0)
    unload_count = _int(entry.get("unload_count", 0), f"{prefix}.unload_count", 0)
    at_unload = _int(entry.get("extruded_at_unload", 0), f"{prefix}.extruded_at_unload", 0)
    if at_unload > extruded:
        raise ConfigError(f"{prefix}.extruded_at_unload can't exceed extruded ({extruded:g})")
    return ToolConfig(index, enabled, material, extruded, unload_count, at_unload)


def _tools(raw: object, prefix: str, model_code: str) -> tuple[ToolConfig, ...]:
    if raw is None:
        return (ToolConfig(0),)
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{prefix}.tools must be a list of [[printers.tools]] tables")
    max_tools = MODELS[model_code].max_tools
    if len(raw) > max_tools:
        raise ConfigError(
            f"{prefix}.tools has {len(raw)} entries, more than the {max_tools} that "
            f"model {model_code} has"
        )
    tools = tuple(_tool(entry, f"{prefix}.tools[{i}]", model_code) for i, entry in enumerate(raw))
    seen: set[int] = set()
    for i, tool in enumerate(tools):
        if tool.index in seen:
            raise ConfigError(f"{prefix}.tools[{i}].index {tool.index} is configured twice")
        seen.add(tool.index)
    return tools


def _printer(entry: object, position: int, control_port: int) -> PrinterConfig:
    prefix = f"printers[{position}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{prefix} must be a table")
    _unknown(entry, _PRINTER_KEYS, prefix)
    if "name" not in entry:
        raise ConfigError(f"{prefix} needs a name")
    name = _str(entry["name"], f"{prefix}.name")
    if not re.fullmatch(_NAME_PATTERN, name):
        raise ConfigError(f"{prefix}.name must match {_NAME_PATTERN}, got {name!r}")
    if "model" not in entry:
        raise ConfigError(f"{prefix} needs a model")
    model = _str(entry["model"], f"{prefix}.model")
    if model not in MODELS:
        known = ", ".join(MODELS)
        raise ConfigError(f"{prefix}.model must be one of {known}, got {model!r}")
    api_key = _str(entry.get("api_key", f"sim-{name}"), f"{prefix}.api_key")
    if not api_key:
        raise ConfigError(f"{prefix}.api_key can't be empty")
    port = _port(entry.get("port", control_port + 1 + position), f"{prefix}.port")
    firmware = None
    if "firmware" in entry:
        firmware = _str(entry["firmware"], f"{prefix}.firmware")
        if firmware not in {f.value for f in Firmware}:
            known = ", ".join(f.value for f in Firmware)
            raise ConfigError(f"{prefix}.firmware must be one of {known}, got {firmware!r}")
    auto_retract = _bool(entry.get("auto_retract", True), f"{prefix}.auto_retract")
    if not auto_retract and not MODELS[model].extruder.auto_retract_switchable:
        raise ConfigError(
            f"{prefix}.auto_retract can't be false: model {model} has no auto-retract switch"
        )
    checks = _str(entry.get("checks", Severity.WARN.value), f"{prefix}.checks")
    if checks not in {s.value for s in Severity}:
        known = ", ".join(s.value for s in Severity)
        raise ConfigError(f"{prefix}.checks must be one of {known}, got {checks!r}")
    return PrinterConfig(
        name=name,
        model=model,
        api_key=api_key,
        port=port,
        firmware=firmware,
        checks=checks,
        high_flow=_bool(entry.get("high_flow", False), f"{prefix}.high_flow"),
        auto_retract=auto_retract,
        sensors=_bool(entry.get("sensors", True), f"{prefix}.sensors"),
        tools=_tools(entry.get("tools"), prefix, model),
    )


def _settings(raw: dict) -> dict[str, float | bool]:
    """Pick the Settings overrides out of the top level, checking each one's type."""
    defaults = Settings()
    result: dict[str, float | bool] = {}
    for f in fields(Settings):
        if f.name not in raw:
            continue
        if isinstance(getattr(defaults, f.name), bool):
            result[f.name] = _bool(raw[f.name], f.name)
        else:
            result[f.name] = _number(raw[f.name], f.name)
    return result


def load(path: str | os.PathLike) -> SimConfig:
    """Load and validate the simulator config. Every error names the key that is wrong."""
    with open(path, "rb") as f:
        try:
            raw = tomllib.load(f)
        # ValueError covers TOMLDecodeError, UnicodeDecodeError, and an int too long
        # to convert, which tomllib raises bare.
        except ValueError as e:
            raise ConfigError(f"can't parse {path}: {e}") from e

    _unknown(raw, _TOP_KEYS | {f.name for f in fields(Settings)}, "")
    host = _str(raw.get("host", "127.0.0.1"), "host")
    control_port = _port(raw.get("control_port", 8000), "control_port")
    data_dir = _str(raw.get("data_dir", "simulator/data"), "data_dir")
    settings = _settings(raw)
    try:
        Settings(**settings)
    except Invalid as e:
        raise ConfigError(str(e)) from None

    entries = raw.get("printers", [])
    if not isinstance(entries, list):
        raise ConfigError("printers must be a list of [[printers]] tables")
    printers = tuple(_printer(entry, i, control_port) for i, entry in enumerate(entries))

    names: set[str] = set()
    ports = {control_port: "control_port"}
    for i, printer in enumerate(printers):
        if printer.name in names:
            raise ConfigError(f"printers[{i}].name {printer.name!r} is configured twice")
        names.add(printer.name)
        if printer.port in ports:
            other = ports[printer.port]
            raise ConfigError(f"printers[{i}].port {printer.port} is already used by {other}")
        ports[printer.port] = f"printers[{i}]"
    return SimConfig(host, control_port, data_dir, settings, printers)


def tool_location(name: str, index: int, tools: int) -> str:
    """The Spoolman location of tool `index` on a printer named `name` with `tools` tools:
    the name for one tool, else the name and the tool's number from 1, as `<name> T1`.

    The test harnesses name their tools' locations by it too.
    """
    return name if tools == 1 else f"{name} T{index + 1}"


def _location(printer: PrinterConfig, tool: ToolConfig) -> str:
    return tool_location(printer.name, tool.index, len(printer.tools))


def locations(cfg: SimConfig) -> list[Location]:
    """Every tool's location, disabled tools included: the bridge can't tell them apart."""
    return [
        Location(
            p.name,
            t.index,
            _location(p, t),
            MODELS[p.model].uuids,
            MODELS[p.model].holds_uuid(t.index),
            t.material["name"] if t.material else None,
            t.material["instance_uuid"] if t.material else None,
        )
        for p in cfg.printers
        for t in p.tools
    ]


def _quote(value: str) -> str:
    # A JSON string is a valid TOML basic string.
    return json.dumps(value)


def bridge_config(cfg: SimConfig) -> str:
    """Render the `[[printers]]` tables the bridge needs to talk to these printers."""
    lines = [
        "# Generated by the printer simulator; run `bridge-config` again after editing sim.toml.",
        "# The bridge still needs [spoolman] url and the poll_* intervals.",
        "",
    ]
    for printer in cfg.printers:
        lines += [
            "[[printers]]",
            f"name = {_quote(printer.name)}",
            f"url = {_quote(f'http://{cfg.host}:{printer.port}')}",
            f"api_key = {_quote(printer.api_key)}",
        ]
        lines.append("")
        for tool in printer.tools:
            lines += [
                "[[printers.tools]]",
                f"index = {tool.index}",
                f"location = {_quote(_location(printer, tool))}",
                "",
            ]
    return "\n".join(lines)


def build(cfg: SimConfig) -> Simulation:
    """The simulation `cfg` describes, with every printer booted and idle."""
    sim = Simulation(Settings(**cfg.settings), data_dir=cfg.data_dir)
    for pc in cfg.printers:
        model = MODELS[pc.model]
        default = Firmware.FULL if model.counters else Firmware.NO_COUNTERS
        tools = {tc.index: _sim_tool(model, tc) for tc in pc.tools}
        enabled = sorted(i for i, t in tools.items() if t.enabled)
        sim.add(
            SimPrinter(
                name=pc.name,
                port=pc.port,
                api_key=pc.api_key,
                model=model,
                firmware=Firmware(pc.firmware) if pc.firmware else default,
                checks=Severity(pc.checks),
                high_flow=pc.high_flow,
                auto_retract=pc.auto_retract,
                sensors=pc.sensors,
                tools=tools,
                selected_tool=enabled[0] if enabled else min(tools),
            )
        )
    return sim


def _sim_tool(model: ModelInfo, tc: ToolConfig) -> SimTool:
    m = tc.material
    material = None
    if m:
        color, uuid_ = m["color"], m["instance_uuid"]
        # A configured color or UUID can only come from an ad-hoc type; a bare name is a preset.
        adhoc = bool(color or uuid_)
        material = Material(m["name"], color, uuid_, adhoc, **model.preset_temperatures(m["name"]))
    return SimTool.loaded_with(
        tc.index,
        material,
        enabled=tc.enabled,
        extruded=float(tc.extruded),
        unload_count=tc.unload_count,
        extruded_at_unload=tc.extruded_at_unload,
    )
