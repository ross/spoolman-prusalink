"""Configuration from a TOML file, with secrets from the environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    printer_url: str
    printer_api_key: str
    spoolman_url: str
    # Tool index -> exact Spoolman location name.
    tools: dict[int, str]
    state_file: Path = Path("/data/state.json")
    ledger_file: Path = Path("/data/ledger.jsonl")
    poll_printing: float = 30.0
    poll_idle: float = 60.0
    # Spool extra field that holds the OpenPrintTag instance UUID; None disables it.
    uuid_field: str | None = "instance_uuid"
    # Where an evicted spool goes; "" means no location. Used from phase 3.
    eviction_location: str = ""
    dry_run: bool = True
    # Multiplier applied to the printer's extruded length before charging.
    length_factor: float = 1.0


def load(path: str | os.PathLike, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    printer = raw.get("printer", {})
    spoolman = raw.get("spoolman", {})
    bridge = raw.get("bridge", {})

    api_key = env.get("PRUSALINK_API_KEY") or printer.get("api_key")
    if not api_key:
        raise ConfigError("set PRUSALINK_API_KEY or printer.api_key")
    printer_url = env.get("PRUSALINK_URL") or printer.get("url")
    if not printer_url:
        raise ConfigError("set printer.url or PRUSALINK_URL")
    spoolman_url = env.get("SPOOLMAN_URL") or spoolman.get("url")
    if not spoolman_url:
        raise ConfigError("set spoolman.url or SPOOLMAN_URL")

    tools: dict[int, str] = {}
    for entry in raw.get("tools", []):
        index, location = int(entry["index"]), str(entry["location"])
        if index in tools:
            raise ConfigError(f"tool {index} is configured twice")
        if location in tools.values():
            raise ConfigError(f"location {location!r} is mapped to more than one tool")
        tools[index] = location
    if not tools:
        raise ConfigError("configure at least one [[tools]] entry")

    dry_run = bool(bridge.get("dry_run", True))
    if not dry_run:
        raise ConfigError("only dry_run = true is supported in this version (phase 1)")

    length_factor = float(bridge.get("length_factor", 1.0))
    if not 0 < length_factor <= 2:
        raise ConfigError(f"length_factor must be in (0, 2], got {length_factor}")

    uuid_field = bridge.get("uuid_field", "instance_uuid") or None
    return Config(
        printer_url=printer_url,
        printer_api_key=api_key,
        spoolman_url=spoolman_url,
        tools=tools,
        state_file=Path(bridge.get("state_file", "/data/state.json")),
        ledger_file=Path(bridge.get("ledger_file", "/data/ledger.jsonl")),
        poll_printing=float(bridge.get("poll_printing", 30)),
        poll_idle=float(bridge.get("poll_idle", 60)),
        uuid_field=uuid_field,
        eviction_location=str(bridge.get("eviction_location", "")),
        dry_run=dry_run,
        length_factor=length_factor,
    )
