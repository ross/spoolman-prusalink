"""Configuration from a TOML file, with secrets from the environment."""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# The name of the printer in the single-printer config form, which has no name of its own.
DEFAULT_PRINTER_NAME = "default"
_NAME_PATTERN = r"[A-Za-z0-9_.-]{1,64}"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PrinterConfig:
    """One printer: how to reach it, its tools, and how to write to it."""

    # Keys the printer's state and appears in ledger records, so it must stay stable.
    name: str
    url: str
    api_key: str
    # Tool index -> exact Spoolman location name.
    tools: dict[int, str]
    # Compatibility checks for the sync job; None uses the printer model's default.
    gcode_preamble: tuple[str, ...] | None = None
    # Where the sync job is uploaded, and its file name (overwritten each time).
    sync_storage: str = "usb"
    sync_file: str = "spoolman-sync.gcode"
    # Write the instance UUID on tool 0, minting one in Spoolman if the spool has none.
    # False on printers without NFC support, such as INDX.
    uuids: bool = True


@dataclass(frozen=True)
class Config:
    spoolman_url: str
    printers: tuple[PrinterConfig, ...]
    db_file: Path = Path("/data/bridge.db")
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
    # Spool extra field (type boolean) the user ticks to claim a spool is in a tool;
    # None disables claims.
    claim_field: str | None = "bridge_claim"
    # Whether a printer/spool mismatch (drift) is written as a flag.
    flag_drift: bool = True
    # Write the spool to the printer after a swap or unload, once the printer is idle.
    printer_sync: bool = True
    # Seconds the printer must stay idle before the sync job runs; 0 runs it at once.
    sync_delay: float = 0.0
    # Also overwrite drift (a mismatch with no swap or unload to explain it).
    sync_drift: bool = False


def _env_bool(env: dict[str, str], key: str) -> bool | None:
    value = env.get(key)
    if value is None or value == "":
        return None
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be true or false, got {value!r}")


def _bool(table: dict, key: str, default: bool, name: str) -> bool:
    """Return a TOML boolean. A string such as "false" is an error, not true."""
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false, got {value!r}")
    return value


def _str(table: dict, key: str, default: str, name: str) -> str:
    """Return a TOML string. Anything else is an error, not str() of it."""
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string, got {value!r}")
    return value


def _number(table: dict, key: str, default: float, name: str) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be a number, not nan or inf, got {value!r}")
    return float(value)


def _interval(table: dict, key: str, default: float, name: str) -> float:
    value = table.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigError(f"{name} must be a number greater than 0, got {value!r}")
    return float(value)


def _delay(table: dict, key: str, default: float, name: str) -> float:
    value = table.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ConfigError(f"{name} must be a number of seconds, 0 or more, got {value!r}")
    return float(value)


def _table(raw: dict, key: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _tools(entries: object, what: str, prefix: str = "") -> dict[int, str]:
    """Parse `[[tools]]` entries: each maps a tool index to a Spoolman location.

    `prefix` names the printer in errors when the entries belong to a listed printer.
    """
    tools: dict[int, str] = {}
    where = f"{prefix}: " if prefix else ""
    if not isinstance(entries, list):
        raise ConfigError(f"{where}tools must be {what} entries")
    for entry in entries:
        if not isinstance(entry, dict) or "index" not in entry or "location" not in entry:
            raise ConfigError(f"{where}each {what} entry needs an index and a location")
        index = entry["index"]
        location = _str(entry, "location", "", f"{where}tool location")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ConfigError(f"{where}tool index must be an integer, got {index!r}")
        if index in tools:
            raise ConfigError(f"{where}tool {index} is configured twice")
        if location in tools.values():
            raise ConfigError(f"{where}location {location!r} is mapped to more than one tool")
        tools[index] = location
    return tools


def _url_key(url: str, prefix: str) -> str:
    """Normalize a printer URL for comparing two entries: scheme and host case, the default
    port, and a trailing slash don't make a different printer."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port  # Also raises for a port that isn't a number or is out of range.
    except ValueError as e:
        raise ConfigError(f"{prefix}.url isn't a valid url: {e}") from e
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if ":" in host:  # IPv6 keeps its brackets, so the port stays apart from the address.
        host = f"[{host}]"
    if port is None or (scheme, port) in (("http", 80), ("https", 443)):
        netloc = host
    else:
        netloc = f"{host}:{port}"
    return f"{scheme}://{netloc}{parts.path.rstrip('/')}"


def _printer_settings(table: dict, prefix: str) -> dict:
    """Parse the settings every printer form shares: the sync job and UUIDs."""
    sync_file = _str(table, "sync_file", "spoolman-sync.gcode", f"{prefix}.sync_file")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.gcode", sync_file):
        raise ConfigError(f"{prefix}.sync_file must be a plain .gcode file name, got {sync_file!r}")
    preamble = table.get("gcode_preamble")
    if preamble is not None and (
        not isinstance(preamble, list) or not all(isinstance(x, str) for x in preamble)
    ):
        raise ConfigError(f"{prefix}.gcode_preamble must be a list of strings, got {preamble!r}")
    return {
        "gcode_preamble": None if preamble is None else tuple(preamble),
        "sync_storage": _str(table, "sync_storage", "usb", f"{prefix}.sync_storage"),
        "sync_file": sync_file,
        "uuids": _bool(table, "uuids", True, f"{prefix}.uuids"),
    }


def _name(table: dict, prefix: str, default: str) -> str:
    name = _str(table, "name", default, f"{prefix}.name")
    if not re.fullmatch(_NAME_PATTERN, name):
        raise ConfigError(f"{prefix}.name must match {_NAME_PATTERN}, got {name!r}")
    return name


def _api_key_from_file(table: dict, prefix: str, env: dict[str, str]) -> str:
    """Return the key from `api_key_env`, else `api_key`, else ""."""
    key = _str(table, "api_key", "", f"{prefix}.api_key")
    key_env = _str(table, "api_key_env", "", f"{prefix}.api_key_env")
    if key and key_env:
        raise ConfigError(f"set {prefix}.api_key or {prefix}.api_key_env, not both")
    if key_env:
        if not env.get(key_env):
            raise ConfigError(f"{prefix}.api_key_env names {key_env}, which isn't set")
        return env[key_env]
    return key


def _single_identity(env: dict[str, str], file_api_key: str, file_url: str) -> tuple[str, str]:
    """Return the `[printer]` form's API key and URL, with the environment on top."""
    api_key = env.get("PRUSALINK_API_KEY") or file_api_key
    if not api_key:
        raise ConfigError("set PRUSALINK_API_KEY or printer.api_key")
    url = env.get("PRUSALINK_URL") or file_url
    if not url:
        raise ConfigError("set printer.url or PRUSALINK_URL")
    return api_key, url


def _single_printer(raw: dict, env: dict[str, str], api_key: str, url: str) -> PrinterConfig:
    """Parse the rest of the `[printer]` and `[[tools]]` form."""
    printer = _table(raw, "printer")
    tools = _tools(raw.get("tools", []), "[[tools]]")
    if not tools and env.get("SPOOLMAN_PRUSALINK_LOCATION"):
        tools[0] = env["SPOOLMAN_PRUSALINK_LOCATION"]
    if not tools:
        raise ConfigError("set SPOOLMAN_PRUSALINK_LOCATION or configure [[tools]] entries")
    return PrinterConfig(
        name=_name(printer, "printer", DEFAULT_PRINTER_NAME),
        url=url,
        api_key=api_key,
        tools=tools,
        **_printer_settings(printer, "printer"),
    )


def _listed_printer(entry: object, index: int, env: dict[str, str]) -> PrinterConfig:
    """Parse one `[[printers]]` entry. Nothing in the environment applies to it."""
    prefix = f"printers[{index}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{prefix} must be a table")
    if not entry.get("name"):
        raise ConfigError(f"{prefix} needs a name")
    name = _name(entry, prefix, "")
    prefix = f"printers[{name}]"
    url = _str(entry, "url", "", f"{prefix}.url")
    if not url:
        raise ConfigError(f"{prefix} needs a url")
    api_key = _api_key_from_file(entry, prefix, env)
    if not api_key:
        raise ConfigError(f"set {prefix}.api_key or {prefix}.api_key_env")
    tools = _tools(entry.get("tools", []), "[[printers.tools]]", prefix)
    if not tools:
        raise ConfigError(f"{prefix} needs [[printers.tools]] entries")
    return PrinterConfig(
        name=name, url=url, api_key=api_key, tools=tools, **_printer_settings(entry, prefix)
    )


# Environment variables that configure the single-printer form only.
_SINGLE_PRINTER_VARS = ("PRUSALINK_URL", "PRUSALINK_API_KEY", "SPOOLMAN_PRUSALINK_LOCATION")


def _listed_printers(raw: dict, env: dict[str, str]) -> tuple[PrinterConfig, ...]:
    """Parse the `[[printers]]` list."""
    entries = raw["printers"]
    if "printer" in raw or "tools" in raw:
        raise ConfigError("use [printer] and [[tools]] or [[printers]], not both")
    for var in _SINGLE_PRINTER_VARS:
        if env.get(var):
            raise ConfigError(
                f"{var} configures a single [printer]; with [[printers]], set each "
                "printer's url, api_key and tools in the file"
            )
    if not isinstance(entries, list):
        raise ConfigError("printers must be a list of [[printers]] tables")
    if not entries:
        raise ConfigError("[[printers]] needs at least one entry")
    printers = tuple(_listed_printer(entry, i, env) for i, entry in enumerate(entries))
    seen_locations: dict[str, str] = {}
    seen_urls: dict[str, str] = {}
    for printer in printers:
        if sum(p.name == printer.name for p in printers) > 1:
            raise ConfigError(f"printer {printer.name!r} is configured twice")
        url = _url_key(printer.url, f"printers[{printer.name}]")
        if url in seen_urls:
            raise ConfigError(
                f"printers {seen_urls[url]!r} and {printer.name!r} have the same url; "
                "the printer's usage would be counted twice"
            )
        seen_urls[url] = printer.name
        for location in printer.tools.values():
            if location in seen_locations:
                raise ConfigError(
                    f"location {location!r} is mapped to a tool on both "
                    f"{seen_locations[location]!r} and {printer.name!r}"
                )
            seen_locations[location] = printer.name
    return printers


def load(path: str | os.PathLike | None, env: dict[str, str] | None = None) -> Config:
    """Load the config from a TOML file, or from the environment alone if `path` is None.

    A file configures one printer with `[printer]` and `[[tools]]`, or several with
    `[[printers]]`. The environment overrides the one-printer form for the basic settings:
    PRUSALINK_URL, PRUSALINK_API_KEY, SPOOLMAN_URL, SPOOLMAN_PRUSALINK_LOCATION (the
    location of a single-tool printer, used when the file has no [[tools]]) and
    SPOOLMAN_PRUSALINK_DRY_RUN. With `[[printers]]`, the printer variables are an error.
    """
    env = dict(os.environ) if env is None else env
    raw: dict = {}
    if path is not None:
        with open(path, "rb") as f:
            try:
                raw = tomllib.load(f)
            except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
                raise ConfigError(f"can't parse {path}: {e}") from e

    # The one-printer form's checks run in the order they always have: the printer's
    # values before Spoolman's, and types before missing values.
    single = raw.get("printers") is None
    printer = _table(raw, "printer") if single else {}
    spoolman = _table(raw, "spoolman")
    bridge = _table(raw, "bridge")
    file_api_key = file_url = ""
    if single:
        file_api_key = _api_key_from_file(printer, "printer", env)
        file_url = _str(printer, "url", "", "printer.url")
    # Check the file's values even when the environment overrides them.
    file_spoolman_url = _str(spoolman, "url", "", "spoolman.url")
    identity = _single_identity(env, file_api_key, file_url) if single else None
    spoolman_url = env.get("SPOOLMAN_URL") or file_spoolman_url
    if not spoolman_url:
        raise ConfigError("set spoolman.url or SPOOLMAN_URL")
    if identity is not None:
        printers: tuple[PrinterConfig, ...] = (_single_printer(raw, env, *identity),)
    else:
        printers = _listed_printers(raw, env)

    dry_run = _env_bool(env, "SPOOLMAN_PRUSALINK_DRY_RUN")
    if dry_run is None:
        dry_run = _bool(bridge, "dry_run", True, "bridge.dry_run")

    length_factor = _number(bridge, "length_factor", 1.0, "bridge.length_factor")
    if not 0 < length_factor <= 2:
        raise ConfigError(f"length_factor must be in (0, 2], got {length_factor}")

    uuid_field = _str(bridge, "uuid_field", "instance_uuid", "bridge.uuid_field") or None
    flag_field = _str(bridge, "flag_field", "bridge_flag", "bridge.flag_field") or None
    claim_field = _str(bridge, "claim_field", "bridge_claim", "bridge.claim_field") or None
    fields = (("uuid_field", uuid_field), ("flag_field", flag_field), ("claim_field", claim_field))
    for name, key in fields:
        if key is not None and not re.fullmatch(r"[a-z0-9_]{1,64}", key):
            raise ConfigError(f"{name} must match [a-z0-9_]{{1,64}}, got {key!r}")
    keys = [key for _, key in fields if key is not None]
    if len(keys) != len(set(keys)):
        raise ConfigError("uuid_field, flag_field and claim_field must differ")
    return Config(
        spoolman_url=spoolman_url,
        printers=printers,
        db_file=Path(_str(bridge, "db_file", "/data/bridge.db", "bridge.db_file")),
        ledger_file=Path(_str(bridge, "ledger_file", "/data/ledger.jsonl", "bridge.ledger_file")),
        poll_printing=_interval(bridge, "poll_printing", 30, "bridge.poll_printing"),
        poll_idle=_interval(bridge, "poll_idle", 60, "bridge.poll_idle"),
        uuid_field=uuid_field,
        eviction_location=_str(bridge, "eviction_location", "", "bridge.eviction_location"),
        dry_run=dry_run,
        length_factor=length_factor,
        flag_field=flag_field,
        claim_field=claim_field,
        flag_drift=_bool(bridge, "flag_drift", True, "bridge.flag_drift"),
        printer_sync=_bool(bridge, "printer_sync", True, "bridge.printer_sync"),
        sync_delay=_delay(bridge, "sync_delay", 0, "bridge.sync_delay"),
        sync_drift=_bool(bridge, "sync_drift", False, "bridge.sync_drift"),
    )
