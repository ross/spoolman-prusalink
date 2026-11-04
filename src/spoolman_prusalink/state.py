"""Persistent per-tool state, the charge outbox, and the ledger."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .accounting import ToolState

STATE_VERSION = 2


@dataclass
class State:
    tools: dict[int, ToolState] = field(default_factory=dict)
    # Charges computed but not yet accepted by Spoolman, oldest first. Each is a ledger
    # record with `spool_id` and `use_length`. Persisting them with the tool state in one
    # atomic write means a crash can't advance `charged` without the charge surviving.
    outbox: list[dict] = field(default_factory=list)
    # Spools whose flag field the bridge has set, so it clears only its own flags.
    flagged: list[int] = field(default_factory=list)
    # Flag texts already shown on the printer with M117, so each is shown once.
    noticed: list[str] = field(default_factory=list)


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    data = json.loads(path.read_text())
    if data.get("version") != STATE_VERSION:
        raise ValueError(f"{path}: unsupported state version {data.get('version')!r}")
    return State(
        tools={int(k): ToolState.from_json(v) for k, v in data.get("tools", {}).items()},
        outbox=list(data.get("outbox", [])),
        flagged=[int(i) for i in data.get("flagged", [])],
        noticed=list(data.get("noticed", [])),
    )


def save_state(path: Path, state: State) -> None:
    """Write the state atomically, so a crash never leaves a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "tools": {str(k): v.to_json() for k, v in sorted(state.tools.items())},
        "outbox": state.outbox,
        "flagged": state.flagged,
        "noticed": state.noticed,
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
