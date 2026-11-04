"""Persistent per-tool state and the would-charge ledger."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .accounting import ToolState

STATE_VERSION = 2


def load_state(path: Path) -> dict[int, ToolState]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get("version") != STATE_VERSION:
        raise ValueError(f"{path}: unsupported state version {data.get('version')!r}")
    return {int(k): ToolState.from_json(v) for k, v in data.get("tools", {}).items()}


def save_state(path: Path, tools: dict[int, ToolState]) -> None:
    """Write the state atomically, so a crash never leaves a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "tools": {str(k): v.to_json() for k, v in sorted(tools.items())},
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def append_ledger(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
