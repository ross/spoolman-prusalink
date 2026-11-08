"""Record a trace from a real printer, for `simulator/tests/test_traces.py` to replay.

    python -m simulator.probe --url http://192.168.1.22 --key-env PRUSALINK_API_KEY \\
        --out simulator/tests/traces/F2-unload.jsonl --note "menu unload, spool 20"

It only reads, so it can run beside the production bridge. It polls `/api/v1/status`,
`/api/v1/filament` and `/api/v1/job` every `--interval` seconds and writes a sample when
something the bridge could notice has changed (the state, the job, the boot ID, the
material, the unload counters, or whether the counter is moving), and otherwise every
`--heartbeat` seconds. Each line you type on stdin while it runs is saved as a mark with
its time, so note what you did at the printer as you do it. Stop it with Ctrl-C or EOF.

Progress goes to stderr: one line per sample written, so you can watch the printer's
state and counters change as you work.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from simulator import trace

ENDPOINTS = ("status", "filament", "job")


def _get(client: httpx.Client, path: str) -> tuple[dict | None, str | None]:
    """A body, or an error string; 204 and 404 are bodies of None with no error."""
    try:
        resp = client.get(path)
    except httpx.HTTPError as e:
        return None, f"{path}: {type(e).__name__}: {e}"
    if resp.status_code in (204, 404):
        return None, None
    if resp.status_code >= 400:
        return None, f"{path}: HTTP {resp.status_code}"
    try:
        return resp.json(), None
    except ValueError:
        return None, f"{path}: not JSON"


def sample(client: httpx.Client, t: float) -> trace.Sample:
    status, err = _get(client, "/api/v1/status")
    if err is not None:
        return trace.Sample(t, error=err)
    filament, err_f = _get(client, "/api/v1/filament")
    job, err_j = _get(client, "/api/v1/job")
    error = "; ".join(e for e in (err_f, err_j) if e) or None
    return trace.Sample(t, status, filament, job, error)


def _projection(s: trace.Sample, prev: trace.Sample | None, tool: int) -> tuple:
    """What a change in means a sample is worth keeping. See the module docstring."""
    steps = trace.signature([prev, s] if prev is not None else [s], tool)
    step = steps[-1]
    return (step.state, step.job != "none", step.boot, step.material, step.unloads, step.extruded)


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def _read_marks(marks: list[tuple[float, str]], lock: threading.Lock, started: float) -> None:
    for line in sys.stdin:
        text = line.strip()
        if text:
            with lock:
                marks.append((time.monotonic() - started, text))


def record(args: argparse.Namespace) -> int:
    key = args.key or (os.environ.get(args.key_env) if args.key_env else None)
    if not key:
        print("an API key is needed: --key or --key-env", file=sys.stderr)
        return 2
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not args.force:
        print(f"{out} exists; pass --force to overwrite it", file=sys.stderr)
        return 2

    client = httpx.Client(base_url=args.url.rstrip("/"), headers={"X-Api-Key": key}, timeout=5.0)
    version, err = _get(client, "/api/version")
    head = trace.header(
        out.stem,
        url=args.url,
        started=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        interval=args.interval,
        heartbeat=args.heartbeat,
        tool=args.tool,
        version=version,
        bridge_commit=_git_commit(),
        note=args.note,
    )
    if err:
        print(f"warning: {err}", file=sys.stderr)

    marks: list[tuple[float, str]] = []
    lock = threading.Lock()
    started = time.monotonic()
    threading.Thread(target=_read_marks, args=(marks, lock, started), daemon=True).start()

    written = 0
    prev: trace.Sample | None = None
    last_written_t = -1e9
    last_projection: tuple | None = None
    print(
        f"recording {out}; type a note and press Enter to mark it; Ctrl-C to stop",
        file=sys.stderr,
    )
    with out.open("w") as f:
        f.write(trace.dumps(head) + "\n")
        try:
            while True:
                t = time.monotonic() - started
                s = sample(client, t)
                with lock:
                    pending, marks[:] = list(marks), []
                for mt, text in pending:
                    f.write(trace.dumps(trace.Mark(mt, text)) + "\n")
                    print(f"{mt:8.1f}s  mark: {text}", file=sys.stderr)
                projection = _projection(s, prev, args.tool)
                keep = (
                    prev is None
                    or projection != last_projection
                    or t - last_written_t >= args.heartbeat
                )
                if keep:
                    f.write(trace.dumps(s) + "\n")
                    f.flush()
                    written += 1
                    last_written_t, last_projection = t, projection
                    print(f"{t:8.1f}s  {_summary(s, args.tool)}", file=sys.stderr)
                prev = s
                time.sleep(max(0.0, args.interval - ((time.monotonic() - started) - t)))
        except KeyboardInterrupt:
            pass
    print(f"wrote {written} samples to {out}", file=sys.stderr)
    return 0


def _summary(s: trace.Sample, tool: int) -> str:
    if s.status is None:
        return f"unreachable ({s.error})"
    state = s.status.get("printer", {}).get("state")
    job = s.status.get("job", {}).get("id")
    text = f"{state:<10} job={job}"
    entry = next((t for t in (s.filament or {}).get("tools", []) if t.get("index") == tool), None)
    if entry is not None:
        m = entry.get("material") or {}
        text += (
            f" extruded={entry.get('extruded')} unloads={entry.get('unload_count')}"
            f" at_unload={entry.get('extruded_at_unload')}"
            f" material={m.get('name')}/{m.get('color')}"
            f"/{'uuid' if m.get('instance_uuid') else '-'}"
        )
    if s.error:
        text += f"  ({s.error})"
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record a trace of what PrusaLink reports.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--url", required=True, help="the printer, such as http://192.168.1.22")
    parser.add_argument("--key", help="the PrusaLink API key")
    parser.add_argument("--key-env", help="the environment variable holding the API key")
    parser.add_argument("--out", type=Path, required=True, help="the trace file to write")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between polls")
    parser.add_argument(
        "--heartbeat", type=float, default=10.0, help="seconds between samples when nothing changes"
    )
    parser.add_argument("--tool", type=int, default=0, help="the tool index to watch for changes")
    parser.add_argument("--note", default="", help="what this trace records, for the header")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    return record(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
