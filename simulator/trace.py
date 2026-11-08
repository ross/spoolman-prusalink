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
from dataclasses import dataclass, field, replace
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

    `job` is "none", "same" (a job ID seen before) or "new" (an ID not seen before in the
    trace). `boot` is "same" or "new". `material` is "none", "name", "color" (name and
    color) or "tagged" (name, color and UUID); `changed` says the identity differs from the
    sample before. `unloads` is the change in `unload_count`. A step with a new job, a new
    boot, a material change or an unload is one sample long, since those are events.

    `extruded` is "+", "-", "±" or "0" for how the counter moved over the step's samples.
    The fields that don't take part in equality describe that movement more closely, for
    `compare()`: `entry` is the first sample's movement, which happened in the interval
    before it and so may belong to the step before; `interior` is the movement over the
    other samples, which is this step's for sure; `samples` and `moves` count the samples
    and how many of them moved.
    """

    state: str
    job: str
    boot: str
    material: str
    changed: bool
    unloads: int
    extruded: str
    entry: str = field(default="0", compare=False)
    interior: str = field(default="0", compare=False)
    samples: int = field(default=1, compare=False)
    moves: int = field(default=0, compare=False)

    def is_event(self) -> bool:
        return self.job == "new" or self.boot == "new" or self.changed or self.unloads != 0

    def shape(self) -> tuple:
        """Everything but the movement."""
        return (self.state, self.job, self.boot, self.material, self.changed, self.unloads)

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


def _has(direction: str, d: str) -> bool:
    """Whether movement `direction` includes `d` ("+" or "-")."""
    return direction == d or direction == "±"


class Signer:
    """Turns samples into steps one at a time; `signature()` runs it over a list.

    Boot, job, material and the counters are compared with the last sample that reached
    the printer, not the sample before, so a reboot or a kept job ID is visible across an
    unreachable gap. A job is "new" when its ID differs from the last job ID seen in the
    whole trace, so a job that hides during the start blip and comes back is "same". A
    sample whose status arrived but whose filament read failed keeps the last filament
    reading: it says nothing new about the tool. After a reboot the counters restart, so
    the first sample's unloads and movement are counted from zero.
    """

    def __init__(self, tool: int = 0) -> None:
        self.tool = tool
        self.steps: list[Step] = []
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
            material, changed, unloads, moved = "unknown", False, 0, "0"
        else:
            material = _material_kind(here.get("material"))
            changed = before is not None and _identity(here.get("material")) != _identity(
                before.get("material")
            )
            if before is None:
                unloads, moved = 0, "0"
            elif rebooted:
                unloads, moved = _int(here, "unload_count"), _direction(_int(here, "extruded"))
            else:
                unloads = _int(here, "unload_count") - _int(before, "unload_count")
                moved = _direction(_int(here, "extruded") - _int(before, "extruded"))
        step = Step(
            state, job, boot, material, changed, unloads, moved, moved, "0", 1, int(moved != "0")
        )
        self._add(step)
        self._filament = filament
        return step

    def _add(self, step: Step) -> None:
        last = self.steps[-1] if self.steps else None
        if (
            last is not None
            and not last.is_event()
            and not step.is_event()
            and (last.state, last.job, last.material) == (step.state, step.job, step.material)
        ):
            self.steps[-1] = replace(
                last,
                extruded=_merge_direction(last.extruded, step.extruded),
                interior=_merge_direction(last.interior, step.extruded),
                samples=last.samples + 1,
                moves=last.moves + step.moves,
            )
        else:
            self.steps.append(step)


def signature(samples: list[Sample], tool: int = 0) -> list[Step]:
    """Reduce samples to the steps the bridge can tell apart. See `Step` and `Signer`."""
    signer = Signer(tool)
    for s in samples:
        signer.step(s)
    return signer.steps


FOLD_WINDOW = 4  # consecutive simulator events one live sample may cover
SUSTAINED = 2  # moving simulator samples that a live step watching the run must have seen


def fold(steps: list[Step]) -> Step:
    """What one sample shows after consecutive events happened between two polls.

    A boot and the unload the idle sensor clear counts at it come in one sample, for
    example. The counters are cumulative, so unloads add, a new boot or job stays new, a
    material change stays changed, and the state and the material are the last ones.
    Movement is left out: `compare()` checks it separately.
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


def _states_after(sim: list[Step], k: int) -> set[str]:
    """The states the printer passes through from event `k` until its next event."""
    states = {sim[k].state}
    for st in sim[k + 1 :]:
        if st.is_event():
            break
        states.add(st.state)
    return states


def _match_event(step: Step, sim: list[Step], j: int) -> int | None:
    """The index of the last simulator event that `step` folds `sim[j:]` up to, or None.

    The live sample's state may be any state the printer reached before its next event,
    since the sample can come after the event within the same poll.
    """
    for k in range(j, min(len(sim), j + FOLD_WINDOW)):
        if not sim[k].is_event():
            break
        f = fold(sim[j : k + 1])
        if f.shape()[1:] == step.shape()[1:] and step.state in _states_after(sim, k):
            return k
    return None


def compare(live: list[Step], sim: list[Step]) -> list[str]:
    """Problems with `sim` as a stand-in for `live`: empty when the two agree.

    Shape first: every live step must appear in the simulator's steps, in order. The
    simulator may have extra steps that aren't events, because the probe can miss a state
    that lasts less than its interval. It may not have extra events: an unload, a material
    change, a new boot or a new job ID is cumulative, so the printer would have shown it
    too. A live event may stand for consecutive simulator events, and its state may be any
    the printer reached before the next event.

    Then movement, per live step, against the simulator steps from where it matched to
    where the next live step matched. Movement inside a live step is the step's own, so
    the simulator must show it there. A step's first sample straddles the change before
    it, so that movement may be the neighbor's. And steady movement in the simulator, over
    several samples, must have been seen by the live step that watched that stretch:
    between its samples, or in the poll that crossed into the next step when the
    movement filled less than half the stretch, unless that poll found the printer
    unreachable.
    """
    problems: list[str] = []
    matched: list[int] = []  # where in the simulator each live step matched
    j = 0
    for i, step in enumerate(live):
        while True:
            if j == len(sim):
                problems.append(f"live step {i} not found in the simulator's steps: {step}")
                if i + 1 < len(live):
                    problems.append(f"  (the next live step is: {live[i + 1]})")
                return problems
            if step.is_event():
                if sim[j].is_event():
                    k = _match_event(step, sim, j)
                    if k is None:
                        problems.append(
                            f"simulator step {j} is an event the printer didn't show: {sim[j]}"
                        )
                        problems.append(f"  (while looking for live step {i}: {step})")
                        return problems
                    matched.append(j)
                    j = k + 1
                    break
                j += 1  # a state the probe missed
            else:
                if sim[j].is_event():
                    problems.append(
                        f"simulator step {j} is an event the printer didn't show: {sim[j]}"
                    )
                    problems.append(f"  (while looking for live step {i}: {step})")
                    return problems
                if sim[j].shape() == step.shape():
                    matched.append(j)
                    j += 1
                    break
                j += 1
    for k in range(j, len(sim)):
        if sim[k].is_event():
            problems.append(f"simulator step {k} is an event after the printer's last: {sim[k]}")
            return problems
    # A live step stands for the simulator steps from its match up to the next live
    # step's match: the states the probe missed in between happened on its watch.
    spans = [(m, n) for m, n in zip(matched, matched[1:] + [len(sim)], strict=True)]

    def span_movement(i: int) -> tuple[str, int, int]:
        """The span's merged direction, its moving samples and its samples in all."""
        if not 0 <= i < len(spans):
            return "0", 0, 0
        moved, moves, samples = "0", 0, 0
        for st in sim[spans[i][0] : spans[i][1]]:
            moved = _merge_direction(moved, st.extruded)
            moves += st.moves
            samples += st.samples
        return moved, moves, samples

    for i, step in enumerate(live):
        here, moves, span_samples = span_movement(i)
        before, _, _ = span_movement(i - 1)
        after, _, _ = span_movement(i + 1)
        for d in "+-":
            if _has(step.interior, d) and not _has(here, d):
                problems.append(
                    f"live step {i} moved {d!r} within it, but the simulator didn't over its "
                    f"steps {spans[i][0]} to {spans[i][1] - 1}: {step}"
                )
                return problems
            nearby = _merge_direction(before, here)
            if step.is_event():
                nearby = _merge_direction(nearby, after)
            if _has(step.entry, d) and not _has(nearby, d):
                problems.append(
                    f"live step {i} began with movement {d!r} that the simulator shows "
                    f"neither there nor just before: {step}"
                )
                return problems
            if _has(here, d) and moves >= SUSTAINED:
                # Movement on this step's watch shows up between the step's own samples.
                # Movement over less than half the span may instead have fallen in the one
                # poll that crossed into the next step, and shows up in its entry. It can't
                # show up at all when that poll found the printer unreachable: the counters
                # restart with it.
                seen = step.interior if step.samples > 1 else step.entry
                brief = step.samples == 1 or moves * 2 < span_samples
                following = live[i + 1] if i + 1 < len(live) else None
                if following is not None:
                    if following.state == UNREACHABLE:
                        continue
                    if brief:
                        seen = _merge_direction(seen, following.entry)
                if not _has(seen, d):
                    problems.append(
                        f"the simulator moved {d!r} for {moves} samples over its steps "
                        f"{spans[i][0]} to {spans[i][1] - 1}, and live step {i} saw none: {step}"
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
