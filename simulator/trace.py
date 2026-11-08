"""Printer traces: what PrusaLink reported over time, and how to compare two of them.

A trace is a JSON Lines file. The first line is a header, then each line is a sample
(the raw `/api/v1/status`, `/api/v1/filament` and `/api/v1/job` bodies at one moment) or a
mark (a note typed by whoever ran the probe). `simulator/probe.py` records one from a real
printer; `simulator/tests/test_traces.py` records one from the simulator and compares the
two with `compare()`.

Two traces never match sample by sample: a real printer is sampled every second or so
and a simulated one every tick, and the numbers differ. `signature()` reduces a trace to
the sequence of things the bridge can tell apart: the printer state, whether a job is
present and whether it's new, a new boot, the kind of material reported, unloads counted,
and whether the counter moved. Runs of samples that say the same thing collapse into one
step. `compare()` then checks that every step the real printer showed appears in the
simulator's steps, in order. The simulator may show more steps, because the probe can miss
a state that lasts less than its interval.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FORMAT = 1
UNREACHABLE = "unreachable"


@dataclass
class Sample:
    """One look at the printer. `None` bodies mean the request failed; `error` says how."""

    t: float
    status: dict | None = None
    filament: dict | None = None
    job: dict | None = None
    error: str | None = None

    def to_json(self) -> dict:
        out: dict[str, Any] = {"t": round(self.t, 2), "status": self.status}
        out["filament"] = self.filament
        out["job"] = self.job
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class Mark:
    t: float
    text: str


@dataclass
class Trace:
    header: dict
    entries: list[Sample | Mark] = field(default_factory=list)

    @property
    def samples(self) -> list[Sample]:
        return [e for e in self.entries if isinstance(e, Sample)]

    @property
    def marks(self) -> list[Mark]:
        return [e for e in self.entries if isinstance(e, Mark)]


def header(name: str, **info: Any) -> dict:
    return {"trace": FORMAT, "name": name, **info}


def dumps(entry: Sample | Mark | dict) -> str:
    if isinstance(entry, Sample):
        data = entry.to_json()
    elif isinstance(entry, Mark):
        data = {"t": round(entry.t, 2), "mark": entry.text}
    else:
        data = entry
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def write(path: Path, trace: Trace) -> None:
    lines = [dumps(trace.header)] + [dumps(e) for e in trace.entries]
    path.write_text("\n".join(lines) + "\n")


def read(path: Path) -> Trace:
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"{path}: empty trace")
    head = json.loads(lines[0])
    if head.get("trace") != FORMAT:
        raise ValueError(f"{path}: not a trace header: {lines[0][:80]}")
    trace = Trace(head)
    for n, line in enumerate(lines[1:], start=2):
        data = json.loads(line)
        if "mark" in data:
            trace.entries.append(Mark(float(data["t"]), str(data["mark"])))
        elif "t" in data:
            trace.entries.append(
                Sample(
                    float(data["t"]),
                    data.get("status"),
                    data.get("filament"),
                    data.get("job"),
                    data.get("error"),
                )
            )
        else:
            raise ValueError(f"{path}:{n}: neither a sample nor a mark")
    return trace


# The signature.


@dataclass(frozen=True)
class Step:
    """A stretch of a trace during which the printer told the bridge the same things.

    `job` is "none", "same" (the job seen before) or "new" (an ID not seen in the previous
    sample). `boot` is "same" or "new". `material` is "none", "name", "color" (name and
    color) or "tagged" (name, color and UUID); `changed` says the identity differs from the
    sample before. `unloads` is the change in `unload_count`. `extruded` is "+", "-", "±"
    or "0" for how the counter moved over the stretch. A step with a new job, a new boot,
    a material change or an unload is one sample long, since those are events.
    """

    state: str
    job: str
    boot: str
    material: str
    changed: bool
    unloads: int
    extruded: str

    def is_event(self) -> bool:
        return self.job == "new" or self.boot == "new" or self.changed or self.unloads != 0

    def __str__(self) -> str:
        parts = [self.state, f"job={self.job}"]
        if self.boot == "new":
            parts.append("boot=new")
        parts.append(f"mat={self.material}" + ("!" if self.changed else ""))
        if self.unloads:
            parts.append(f"unload+{self.unloads}" if self.unloads > 0 else f"unload{self.unloads}")
        parts.append(f"ext={self.extruded}")
        return " ".join(parts)


def _material_kind(material: dict | None) -> str:
    if not material or not material.get("name"):
        return "none"
    if material.get("instance_uuid"):
        return "tagged"
    if material.get("color"):
        return "color"
    return "name"


def _identity(material: dict | None) -> tuple | None:
    if not material or not material.get("name"):
        return None
    return (material.get("name"), material.get("color"), material.get("instance_uuid"))


def _tool(sample: Sample, index: int) -> dict | None:
    if not sample.filament:
        return None
    for t in sample.filament.get("tools", []):
        if t.get("index") == index:
            return t
    return None


def _int(tool: dict, key: str) -> int:
    return int(tool.get(key) or 0)


def _direction(delta: int) -> str:
    return "+" if delta > 0 else "-" if delta < 0 else "0"


def _merge_direction(a: str, b: str) -> str:
    if a == "0":
        return b
    if b == "0" or a == b:
        return a
    return "±"


def signature(samples: list[Sample], tool: int = 0) -> list[Step]:
    """Reduce samples to the steps the bridge can tell apart. See `Step`."""
    steps: list[Step] = []
    prev: Sample | None = None
    for s in samples:
        reachable = s.status is not None and s.error is None
        state = str(s.status.get("printer", {}).get("state", "?")) if reachable else UNREACHABLE
        job_id = (s.status or {}).get("job", {}).get("id") if reachable else None
        prev_job = (prev.status or {}).get("job", {}).get("id") if prev is not None else None
        if job_id is None:
            job = "none"
        elif prev is not None and job_id == prev_job:
            job = "same"
        else:
            job = "new"

        boot_id = (s.filament or {}).get("boot_id")
        prev_boot = (prev.filament or {}).get("boot_id") if prev is not None else None
        rebooted = boot_id is not None and prev_boot is not None and boot_id != prev_boot
        boot = "new" if rebooted else "same"

        here, before = _tool(s, tool), _tool(prev, tool) if prev is not None else None
        if not reachable or here is None:
            material, changed, unloads, extruded = "unknown", False, 0, "0"
        else:
            material = _material_kind(here.get("material"))
            changed = before is not None and _identity(here.get("material")) != _identity(
                before.get("material")
            )
            if before is None or boot == "new":
                unloads, extruded = 0, "0"
            else:
                unloads = _int(here, "unload_count") - _int(before, "unload_count")
                extruded = _direction(_int(here, "extruded") - _int(before, "extruded"))
        step = Step(state, job, boot, material, changed, unloads, extruded)
        last = steps[-1] if steps else None
        if (
            last is not None
            and not last.is_event()
            and not step.is_event()
            and (last.state, last.job, last.material) == (step.state, step.job, step.material)
        ):
            steps[-1] = Step(
                last.state,
                last.job,
                last.boot,
                last.material,
                False,
                0,
                _merge_direction(last.extruded, step.extruded),
            )
        else:
            steps.append(step)
        prev = s
    return steps


def compare(live: list[Step], sim: list[Step]) -> list[str]:
    """Problems with `sim` as a stand-in for `live`: empty when every live step appears in order.

    The first live step is where the probe happened to start, so its `extruded` isn't
    compared. A live step that only differs by `extruded` from the simulator's next step is
    still reported, because a counter that moves on one and not the other is a real gap.
    """
    problems: list[str] = []
    j = 0
    for i, step in enumerate(live):
        want = step
        if i == 0:
            want = Step(
                step.state, step.job, step.boot, step.material, step.changed, step.unloads, "*"
            )
        while j < len(sim) and not _matches(want, sim[j]):
            j += 1
        if j == len(sim):
            problems.append(f"live step {i} not found in the simulator's steps: {step}")
            if i + 1 < len(live):
                problems.append(f"  (the next live step is: {live[i + 1]})")
            break
        j += 1
    return problems


def _matches(want: Step, have: Step) -> bool:
    if want.extruded == "*":
        return asdict(want) | {"extruded": have.extruded} == asdict(have)
    return want == have


def describe(steps: list[Step]) -> str:
    return "\n".join(f"{n:3d}  {s}" for n, s in enumerate(steps))


def main(argv: list[str] | None = None) -> int:
    """`python -m simulator.trace FILE` prints a trace's signature; two files compares them."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("live", type=Path, help="a trace recorded with simulator.probe")
    parser.add_argument("sim", type=Path, nargs="?", help="a trace to check against the first")
    parser.add_argument("--tool", type=int, default=0)
    args = parser.parse_args(argv)
    live = read(args.live)
    print(f"# {args.live}: {live.header.get('name')}")
    live_steps = signature(live.samples, args.tool)
    print(describe(live_steps))
    for m in live.marks:
        print(f"     mark {m.t:8.1f}s: {m.text}")
    if args.sim is None:
        return 0
    sim_steps = signature(read(args.sim).samples, args.tool)
    print(f"\n# {args.sim}")
    print(describe(sim_steps))
    problems = compare(live_steps, sim_steps)
    print()
    print("\n".join(problems) if problems else "every live step appears in the second trace")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
