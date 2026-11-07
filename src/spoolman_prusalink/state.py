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
class Runout:
    """A runout flag, and the spool's weights in Spoolman as the bridge last knew them.

    `net` and `used` start as the weights when the flag was set, and `used` follows the
    bridge's own charges. Any other change to either is the user editing the weight,
    which answers the flag. None means not known yet, as for state from older versions.
    """

    text: str
    net: float | None = None
    used: float | None = None

    def to_json(self) -> dict:
        return {"text": self.text, "net": self.net, "used": self.used}

    @classmethod
    def from_json(cls, data: dict | str) -> Runout:
        if isinstance(data, str):  # Before the weights were kept.
            return cls(data)
        return cls(str(data["text"]), data.get("net"), data.get("used"))


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
    # Flags for spools that ran out mid-print while Spoolman showed plenty left, by
    # spool ID. Each clears when another spool becomes active and then this one does, or
    # when the user edits the spool's weight in Spoolman.
    runouts: dict[int, Runout] = field(default_factory=dict)
    # Spools whose claim the bridge has handled while the box still shows ticked, so a
    # claim is handled once even if clearing the box fails, or in dry-run mode.
    claimed: list[int] = field(default_factory=list)


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
        runouts={int(k): Runout.from_json(v) for k, v in data.get("runouts", {}).items()},
        claimed=[int(i) for i in data.get("claimed", [])],
    )


def save_state(path: Path, state: State) -> bool:
    """Write the state atomically, so a crash never leaves a partial file.

    Skip the write when the file already holds this state, since the bridge saves after
    every poll and most idle polls change nothing. Return whether it wrote.
    """
    payload = {
        "version": STATE_VERSION,
        "tools": {str(k): v.to_json() for k, v in sorted(state.tools.items())},
        "outbox": state.outbox,
        "flagged": state.flagged,
        "noticed": state.noticed,
        "runouts": {str(k): v.to_json() for k, v in sorted(state.runouts.items())},
        "claimed": state.claimed,
    }
    text = json.dumps(payload, indent=2)
    try:
        if path.read_text() == text:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def append_ledger(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
