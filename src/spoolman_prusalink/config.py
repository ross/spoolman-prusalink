"""Configuration from a TOML file, with secrets from the environment."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .printer_sync import COREONEL_PREAMBLE


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
    # Spool extra field (type text) the bridge writes flags to; None disables flags.
    flag_field: str | None = "bridge_flag"
    # Whether a printer/spool mismatch (drift) is written as a flag.
    flag_drift: bool = True
    # Compatibility-check lines at the start of every job file.
    gcode_preamble: tuple[str, ...] = COREONEL_PREAMBLE
    # Where the sync job is uploaded, and its file name (overwritten each time).
    sync_storage: str = "usb"
    sync_file: str = "spoolman-sync.gcode"
    # Write the spool to the printer after a swap or unload, once the printer is idle.
    printer_sync: bool = True
    # Also overwrite drift (a mismatch with no swap or unload to explain it).
    sync_drift: bool = False
    # Write the instance UUID on tool 0, minting one in Spoolman if the spool has none.
    # False on printers without NFC support, such as INDX.
    printer_uuids: bool = True
    # Show new flags on the printer screen with M117 through a job, when idle.
    printer_notices: bool = False


def _env_bool(env: dict[str, str], key: str) -> bool | None:
    value = env.get(key)
    if value is None or value == "":
        return None
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be true or false, got {value!r}")


def load(path: str | os.PathLike | None, env: dict[str, str] | None = None) -> Config:
    """Load the config from a TOML file, or from the environment alone if `path` is None.

    The environment overrides the file for the basic settings: PRUSALINK_URL,
    PRUSALINK_API_KEY, SPOOLMAN_URL, SPOOLMAN_PRUSALINK_LOCATION (the location of a
    single-tool printer, used when the file has no [[tools]]) and
    SPOOLMAN_PRUSALINK_DRY_RUN.
    """
    env = dict(os.environ) if env is None else env
    raw: dict = {}
    if path is not None:
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
    if not tools and env.get("SPOOLMAN_PRUSALINK_LOCATION"):
        tools[0] = env["SPOOLMAN_PRUSALINK_LOCATION"]
    if not tools:
        raise ConfigError("set SPOOLMAN_PRUSALINK_LOCATION or configure [[tools]] entries")

    dry_run = _env_bool(env, "SPOOLMAN_PRUSALINK_DRY_RUN")
    if dry_run is None:
        dry_run = bool(bridge.get("dry_run", True))

    length_factor = float(bridge.get("length_factor", 1.0))
    if not 0 < length_factor <= 2:
        raise ConfigError(f"length_factor must be in (0, 2], got {length_factor}")

    uuid_field = bridge.get("uuid_field", "instance_uuid") or None
    flag_field = bridge.get("flag_field", "bridge_flag") or None
    for name, key in (("uuid_field", uuid_field), ("flag_field", flag_field)):
        if key is not None and not re.fullmatch(r"[a-z0-9_]{1,64}", key):
            raise ConfigError(f"{name} must match [a-z0-9_]{{1,64}}, got {key!r}")
    sync_file = str(printer.get("sync_file", "spoolman-sync.gcode"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.gcode", sync_file):
        raise ConfigError(f"printer.sync_file must be a plain .gcode file name, got {sync_file!r}")
    if uuid_field is not None and uuid_field == flag_field:
        raise ConfigError("uuid_field and flag_field must differ")
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
        flag_field=flag_field,
        flag_drift=bool(bridge.get("flag_drift", True)),
        gcode_preamble=tuple(printer.get("gcode_preamble", COREONEL_PREAMBLE)),
        sync_storage=str(printer.get("sync_storage", "usb")),
        sync_file=sync_file,
        printer_sync=bool(bridge.get("printer_sync", True)),
        sync_drift=bool(bridge.get("sync_drift", False)),
        printer_uuids=bool(printer.get("uuids", True)),
        printer_notices=bool(bridge.get("printer_notices", False)),
    )
