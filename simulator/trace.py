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
simulator's steps, in order. The simulator may show more states, because the probe can
miss one that lasts less than its interval, but not more events: an unload, a material
change, a new boot or a new job ID is cumulative, so the printer would have shown it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    a material change or an unload is one sample long, since those are events; movement
    seen in the same sample belongs to the step before, where it happened.
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


def _tool(filament: dict | None, index: int) -> dict | None:
    if not filament:
        return None
    for t in filament.get("tools", []):
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


class Signer:
    """Turns samples into steps one at a time; `signature()` runs it over a list.

    Boot, job, material and the counters are compared with the last sample that reached
    the printer, not the sample before, so a reboot or a kept job ID is visible across an
    unreachable gap. A job is "new" when its ID differs from the last job ID seen in the
    whole trace, so a job that hides during the start blip and comes back is "same". A
    sample whose status arrived but whose filament read failed keeps the last filament
    reading: it says nothing new about the tool.
    """

    def __init__(self, tool: int = 0) -> None:
        self.tool = tool
        self.steps: list[Step] = []
        self._last: Sample | None = None  # the last reachable sample
        self._last_job: int | None = None  # the last job ID seen anywhere in the trace
        self._filament: dict | None = None  # the last filament body that arrived

    def step(self, s: Sample) -> Step:
        """Classify one sample, fold it into `steps`, and return its own step."""
        if s.status is None:
            step = Step(UNREACHABLE, "none", "same", "unknown", False, 0, "0")
            self._add(step)
            return step
        state = str(s.status.get("printer", {}).get("state", "?"))
        job_id = s.status.get("job", {}).get("id")
        if job_id is None:
            job = "none"
        else:
            job = "same" if job_id == self._last_job else "new"
            self._last_job = job_id

        filament = s.filament if s.filament is not None else self._filament
        boot_id = (filament or {}).get("boot_id")
        prev_boot = (self._filament or {}).get("boot_id")
        rebooted = boot_id is not None and prev_boot is not None and boot_id != prev_boot
        boot = "new" if rebooted else "same"

        here = _tool(filament, self.tool)
        before = _tool(self._filament, self.tool)
        if here is None:
            material, changed, unloads, extruded = "unknown", False, 0, "0"
        else:
            material = _material_kind(here.get("material"))
            changed = before is not None and _identity(here.get("material")) != _identity(
                before.get("material")
            )
            if before is None or rebooted:
                unloads, extruded = 0, "0"
            else:
                unloads = _int(here, "unload_count") - _int(before, "unload_count")
                extruded = _direction(_int(here, "extruded") - _int(before, "extruded"))
        step = Step(state, job, boot, material, changed, unloads, extruded)
        self._add(step)
        self._last, self._filament = s, filament
        return step

    def _add(self, step: Step) -> None:
        if step.is_event() and step.extruded != "0":
            # The movement happened in the interval before the event, during the run the
            # event ends, so it belongs to that run. The probe and the simulator split an
            # interval differently, and this keeps their steps the same.
            for n in range(len(self.steps) - 1, -1, -1):
                if not self.steps[n].is_event():
                    self.steps[n] = _with_extruded(
                        self.steps[n], _merge_direction(self.steps[n].extruded, step.extruded)
                    )
                    step = _with_extruded(step, "0")
                    break
        last = self.steps[-1] if self.steps else None
        if (
            last is not None
            and not last.is_event()
            and not step.is_event()
            and (last.state, last.job, last.material) == (step.state, step.job, step.material)
        ):
            self.steps[-1] = _with_extruded(last, _merge_direction(last.extruded, step.extruded))
        else:
            self.steps.append(step)


def _with_extruded(step: Step, extruded: str) -> Step:
    return Step(
        step.state, step.job, step.boot, step.material, step.changed, step.unloads, extruded
    )


def signature(samples: list[Sample], tool: int = 0) -> list[Step]:
    """Reduce samples to the steps the bridge can tell apart. See `Step` and `Signer`."""
    signer = Signer(tool)
    for s in samples:
        signer.step(s)
    return signer.steps


FOLD_WINDOW = 8  # consecutive simulator steps one live sample may cover


def fold(steps: list[Step]) -> Step:
    """What one sample would show after all of `steps` happened between two polls.

    A probe polling every second can see two events at once: a job appearing and its
    `M865` applying, or two unloads (S14). The counters are cumulative, so unloads add,
    a new boot or job stays new, a material change stays changed, and the state and the
    material are the last ones. Movement is left out: `compare()` checks it separately.
    """
    last = steps[-1]
    return Step(
        last.state,
        "new" if any(st.job == "new" for st in steps) else last.job,
        "new" if any(st.boot == "new" for st in steps) else "same",
        last.material,
        any(st.changed for st in steps),
        sum(st.unloads for st in steps),
        "0",
    )


def _same_shape(a: Step, b: Step) -> bool:
    return _with_extruded(a, "0") == _with_extruded(b, "0")


def compare(live: list[Step], sim: list[Step]) -> list[str]:
    """Problems with `sim` as a stand-in for `live`: empty when the two agree.

    Every live step must appear in the simulator's steps, in order. A live step may
    stand for several consecutive simulator steps when their `fold()` equals it, since
    one poll can cover more than one change. The simulator may have extra steps that
    aren't events, because the probe can miss a state that lasts less than its interval.
    It may not have extra events: an unload, a material change, a new boot or a new job
    ID is cumulative, so the printer would have shown it too.

    Movement is compared per run: a live run's `extruded` must equal the movement over
    the simulator steps it stands for, together with those folded into the events that
    follow it, since both sides attribute an event's movement to the run before it.
    """
    problems: list[str] = []
    ranges: list[tuple[int, int]] = []  # the simulator steps each live step stands for
    j = 0
    for i, step in enumerate(live):
        first = j
        while True:
            if j == len(sim):
                problems.append(f"live step {i} not found in the simulator's steps: {step}")
                if i + 1 < len(live):
                    problems.append(f"  (the next live step is: {live[i + 1]})")
                return problems
            end = None
            for k in range(j, min(len(sim), j + FOLD_WINDOW)):
                if _same_shape(fold(sim[j : k + 1]), step):
                    end = k
                    break
            if end is not None:
                j = end + 1
                ranges.append((first, j))
                break
            if sim[j].is_event():
                problems.append(f"simulator step {j} is an event the printer didn't show: {sim[j]}")
                problems.append(f"  (while looking for live step {i}: {step})")
                return problems
            j += 1
    for k in range(j, len(sim)):
        if sim[k].is_event():
            problems.append(f"simulator step {k} is an event after the printer's last: {sim[k]}")
            return problems

    for i, step in enumerate(live):
        if step.is_event():
            continue
        start, end = ranges[i]
        k = i + 1
        while k < len(live) and live[k].is_event():
            end = ranges[k][1]
            k += 1
        moved = "0"
        for st in sim[start:end]:
            moved = _merge_direction(moved, st.extruded)
        if moved != step.extruded:
            problems.append(
                f"live step {i} moved the counter {step.extruded!r} but the simulator moved it "
                f"{moved!r} over its steps {start} to {end - 1}: {step}"
            )
            return problems
    return problems


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
