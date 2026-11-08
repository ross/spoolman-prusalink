"""Persistent per-tool state, the charge outbox, and the ledger."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .accounting import ToolState

# Version 3 nests tool states under the printer's name and stamps outbox records with it.
STATE_VERSION = 3


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
    printer: str | None = None  # Whose tool it ran out in; None in state from before names.

    def to_json(self) -> dict:
        return {"text": self.text, "net": self.net, "used": self.used, "printer": self.printer}

    @classmethod
    def from_json(cls, data: dict | str) -> Runout:
        if isinstance(data, str):  # Before the weights were kept.
            return cls(data)
        return cls(str(data["text"]), data.get("net"), data.get("used"), data.get("printer"))


@dataclass
class PrinterState:
    tools: dict[int, ToolState] = field(default_factory=dict)
    # The flag texts this printer's tools last asked for, by spool. They stand in while
    # the printer can't be read, so an outage doesn't clear its flags in Spoolman.
    flags: dict[int, list[str]] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "tools": {str(k): v.to_json() for k, v in sorted(self.tools.items())},
            "flags": {str(k): list(v) for k, v in sorted(self.flags.items())},
        }

    @classmethod
    def from_json(cls, data: dict) -> PrinterState:
        return cls(
            tools={int(k): ToolState.from_json(v) for k, v in data.get("tools", {}).items()},
            flags={int(k): [str(t) for t in v] for k, v in data.get("flags", {}).items()},
        )


@dataclass
class State:
    # By printer name. Everything else here is keyed by spool and shared by every printer.
    printers: dict[str, PrinterState] = field(default_factory=dict)
    # Charges computed but not yet accepted by Spoolman, oldest first. Each is a ledger
    # record with `printer`, `tool`, `spool_id` and `use_length`. Persisting them with the
    # tool state in one atomic write means a crash can't advance `charged` without the
    # charge surviving.
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

    def tools(self, printer: str) -> dict[int, ToolState]:
        """Return the tool states of `printer`, creating its entry."""
        return self.printers.setdefault(printer, PrinterState()).tools


def load_state(path: Path, printers: Sequence[str]) -> State:
    """Load the state file. `printers` are the configured printer names.

    A version 2 file is from a bridge with one printer and no printer names. It loads
    only when one printer is configured, and its tools and outbox become that printer's.
    """
    if not path.exists():
        return State()
    data = json.loads(path.read_text())
    version = data.get("version")
    if version == 2:
        if len(printers) != 1:
            raise ValueError(
                f"{path} is from a single-printer bridge (state version {version}); with "
                f"{len(printers)} printers configured, the bridge can't tell which one it "
                "belongs to. Configure that printer alone and poll once with dry run off and "
                "the printer and Spoolman reachable, which saves the file as version "
                f"{STATE_VERSION}. Then add the others."
            )
        data = _migrate_v2(data, printers[0])
    elif version != STATE_VERSION:
        raise ValueError(f"{path}: unsupported state version {version!r}")
    return State(
        printers={k: PrinterState.from_json(v) for k, v in data.get("printers", {}).items()},
        outbox=list(data.get("outbox", [])),
        flagged=[int(i) for i in data.get("flagged", [])],
        noticed=list(data.get("noticed", [])),
        runouts={int(k): Runout.from_json(v) for k, v in data.get("runouts", {}).items()},
        claimed=[int(i) for i in data.get("claimed", [])],
    )


def _migrate_v2(data: dict, printer: str) -> dict:
    """Nest a version 2 file's tools under `printer` and stamp its outbox and runouts."""
    runouts = {
        k: {**(v if isinstance(v, dict) else {"text": v}), "printer": printer}
        for k, v in data.get("runouts", {}).items()
    }
    return {
        **data,
        "version": STATE_VERSION,
        "printers": {printer: {"tools": data.get("tools", {})}},
        "outbox": [{**r, "printer": printer} for r in data.get("outbox", [])],
        "runouts": runouts,
    }


def save_state(path: Path, state: State) -> bool:
    """Write the state atomically, so a crash never leaves a partial file.

    Skip the write when the file already holds this state, since the bridge saves after
    every poll and most idle polls change nothing. Return whether it wrote.
    """
    payload = {
        "version": STATE_VERSION,
        "printers": {k: v.to_json() for k, v in sorted(state.printers.items())},
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
