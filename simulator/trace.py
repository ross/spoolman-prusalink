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
    and how many of them moved. `dirs` is each sample's movement in order and `blind`
    whether each sample came without the counter (the printer unreachable, or the
    filament read failed), for `compare()`'s poll-by-poll check. `t_first` and `t_last`
    are the times of the first and last samples.
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
    dirs: tuple[str, ...] = field(default=("0",), compare=False)
    blind: tuple[bool, ...] = field(default=(False,), compare=False)
    t_first: float = field(default=0.0, compare=False)
    t_last: float = field(default=0.0, compare=False)

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
            step = Step(
                UNREACHABLE,
                "none",
                "same",
                "unknown",
                False,
                0,
                "0",
                blind=(True,),
                t_first=s.t,
                t_last=s.t,
            )
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
            state,
            job,
            boot,
            material,
            changed,
            unloads,
            moved,
            entry=moved,
            moves=int(moved != "0"),
            dirs=(moved,),
            blind=(s.filament is None or here is None,),
            t_first=s.t,
            t_last=s.t,
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
                dirs=last.dirs + step.dirs,
                blind=last.blind + step.blind,
                t_last=step.t_last,
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
SLACK = 1.5  # a poll gap may run this much over the interval before it's a missed sample


def poll_gap(steps: list[Step]) -> float:
    """The typical spacing between consecutive samples: the polling interval.

    The median of the spacings between steps and within them, so one short or long gap
    doesn't set it. The probe's header carries the interval itself; this is for traces
    without one.
    """
    gaps = []
    for a, b in zip(steps, steps[1:], strict=False):
        gaps.append(b.t_first - a.t_last)
    for st in steps:
        if st.samples > 1:
            gaps.append((st.t_last - st.t_first) / (st.samples - 1))
    gaps = sorted(g for g in gaps if g > 0)
    return gaps[len(gaps) // 2] if gaps else 1.0


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


def _match_event(
    step: Step, sim: list[Step], j: int, pending: list[int], window: float, tick: float
) -> int | None:
    """Where the simulator continues after live event `step` folds `pending` and `sim[j:]`.

    `pending` are simulator events that live runs passed over: the probe missed their
    poll, and their information (a new job, an unload) comes out in this sample. The
    fold may take them alone, or with the events from `j` on; runs between the folded
    events are allowed while together they last less than `window`, one poll gap. The
    live sample's state may be any state the printer reached after the last folded event
    and before the next, since the sample can come later within the same poll. Returns
    the index after the last simulator event consumed, or None if nothing fits.
    """
    folded = [sim[p] for p in pending]
    last = pending[-1] if pending else None

    def fits() -> bool:
        return (
            len(folded) <= FOLD_WINDOW
            and fold(folded).shape()[1:] == step.shape()[1:]
            and step.state in _states_after(sim, last)
        )

    if folded and fits():
        return j
    skipped = 0.0
    for k in range(j, len(sim)):
        if sim[k].is_event():
            folded.append(sim[k])
            last = k
            if len(folded) > FOLD_WINDOW:
                return None
            if fits():
                return k + 1
        else:
            skipped += sim[k].t_last - sim[k].t_first + tick
            if skipped >= window:
                return None
    return None


def compare(live: list[Step], sim: list[Step], gap: float | None = None) -> list[str]:
    """Problems with `sim` as a stand-in for `live`: empty when the two agree.

    `gap` is the live trace's polling interval, taken from its samples when not given.

    Shape first: every live step must appear in the simulator's steps, in order. The
    simulator may have extra steps that aren't events, because the probe can miss a state
    that lasts less than its interval. It may not have extra events: an unload, a material
    change, a new boot or a new job ID is cumulative, so the printer would have shown it
    too. A live event may stand for consecutive simulator events, with runs between them
    that together last less than a poll gap, and its state may be any the printer reached
    before the next event.

    Then movement, poll by poll: the printer's samples must be a coarsening of the
    simulator's. Each live sample is placed at a simulator sample within the stretch its
    step matched (from that step's match to the next step's), in order, so that the
    counter's movement between consecutive live samples is exactly the movement the
    simulator shows between their places: a poll that saw no movement means the
    simulator was still over that stretch, and a poll that saw movement means the
    simulator moved the same way somewhere in it. A poll that brought no counter (the
    printer unreachable, or the filament read failed) accepts any movement, so movement
    before a power cut can hide behind it: a limit of a rule without time. The simulator
    may not move before the first live sample or after the last, since a recording
    begins and ends at rest. Durations take part in one place only: simulator runs the
    probe never sampled must together fit within one poll, or the printer would have
    shown them. Otherwise a replay may run its states faster or slower than the printer.
    """
    problems: list[str] = []
    window = (gap if gap is not None else poll_gap(live)) * SLACK
    tick = poll_gap(sim)
    matched: list[int] = []  # where in the simulator each live step matched
    pending: list[int] = []  # simulator events whose poll the probe missed, so far
    j = 0
    for i, step in enumerate(live):
        while True:
            if step.is_event():
                k = _match_event(step, sim, j, pending, window, tick) if j < len(sim) else None
                if k is not None:
                    matched.append(j)
                    pending = []
                    j = k
                    break
                if j == len(sim):
                    problems.append(f"live step {i} not found in the simulator's steps: {step}")
                    return problems
                if sim[j].is_event():
                    problems.append(
                        f"simulator step {j} is an event the printer didn't show: {sim[j]}"
                    )
                    problems.append(f"  (while looking for live step {i}: {step})")
                    return problems
                j += 1  # a state the probe missed
            else:
                if j == len(sim):
                    problems.append(f"live step {i} not found in the simulator's steps: {step}")
                    if i + 1 < len(live):
                        problems.append(f"  (the next live step is: {live[i + 1]})")
                    return problems
                if sim[j].is_event():
                    # The probe missed this event's poll, such as a job's first sighting
                    # before the start blip; a later live event has to account for it.
                    pending.append(j)
                elif sim[j].shape() == step.shape():
                    matched.append(j)
                    j += 1
                    break
                j += 1
    for k in pending + list(range(j, len(sim))):
        if sim[k].is_event():
            problems.append(f"simulator step {k} is an event the printer didn't show: {sim[k]}")
            return problems
    # A live step stands for the simulator steps from its match up to the next live
    # step's match: the states the probe missed in between happened on its watch. Two
    # live steps can match the same simulator step, so a span is at least that step.
    spans = [(m, max(n, m + 1)) for m, n in zip(matched, matched[1:] + [len(sim)], strict=True)]

    problems = _missed_runs(live, sim, spans, window, tick)
    return problems or _coarsening(live, sim, spans)


def _missed_runs(
    live: list[Step], sim: list[Step], spans: list[tuple[int, int]], window: float, tick: float
) -> list[str]:
    """Simulator runs the probe never sampled must together fit between two polls.

    Within a live step's stretch, a run whose state, job or material differs from the
    live step's was never sampled by it (the step's samples all share those). Such runs,
    taken consecutively, must last less than one poll gap, or the printer would have
    shown them.
    """
    for i, step in enumerate(live):
        a, b = spans[i]
        missed, first = 0.0, None
        for j in range(a, b):
            st = sim[j]
            sampled = st.is_event() or (
                st.state == step.state
                and (step.is_event() or (st.job, st.material) == (step.job, step.material))
            )
            if sampled:
                missed, first = 0.0, None
                continue
            first = j if first is None else first
            missed += st.t_last - st.t_first + tick
            if missed >= window:
                return [
                    f"simulator steps {first} to {j} last {missed:.1f} s, longer than a poll, "
                    f"yet the printer never showed them within live step {i}: {step}"
                ]
    return []


def _coarsening(live: list[Step], sim: list[Step], spans: list[tuple[int, int]]) -> list[str]:
    """Problems placing the live samples among the simulator's; see `compare()`.

    Dynamic programming over (live sample, simulator sample): a live sample may sit at a
    simulator sample within its step's stretch if some placement of the sample before it
    leaves exactly its movement in between. Prefix counts of moving simulator samples
    make each step a range query.
    """
    dirs = [d for st in sim for d in st.dirs]
    n = len(dirs)
    plus = [0] * (n + 1)  # plus[i]: moving-up samples among the first i
    minus = [0] * (n + 1)
    for i, d in enumerate(dirs):
        plus[i + 1] = plus[i] + (d in "+±")
        minus[i + 1] = minus[i] + (d in "-±")
    # Where the last sample that moved up, down, or at all sits at or before i.
    last_plus, last_minus, last_move = [-1] * n, [-1] * n, [-1] * n
    for i, d in enumerate(dirs):
        last_plus[i] = i if d in "+±" else (last_plus[i - 1] if i else -1)
        last_minus[i] = i if d in "-±" else (last_minus[i - 1] if i else -1)
        last_move[i] = i if d != "0" else (last_move[i - 1] if i else -1)
    starts = [0]
    for st in sim:
        starts.append(starts[-1] + st.samples)

    def bounds(d: str, blind: bool, i: int) -> tuple[int, int]:
        """The places (inclusive, as positions; -1 is before the first) the previous live
        sample may sit so that the simulator's movement over (prev, i] is exactly `d`."""
        if blind:
            return -1, i - 1
        if d == "0":
            return last_move[i], i - 1
        if d == "+":
            return last_minus[i], last_plus[i] - 1
        if d == "-":
            return last_plus[i], last_minus[i] - 1
        return -1, min(last_plus[i], last_minus[i]) - 1

    flat = [(i, k) for i, st in enumerate(live) for k in range(st.samples)]
    # reach[p + 1] says whether the previous live sample may sit at position p (-1..n-1).
    reach = [False] * (n + 1)
    reach[0] = True
    for f, (i, k) in enumerate(flat):
        step = live[i]
        d, blind = step.dirs[k], step.blind[k]
        lo, hi = starts[spans[i][0]], starts[spans[i][1]]
        # The trace's last poll sits at the stretch's end: a recording ends at rest, so
        # the simulator may not move after it.
        if f + 1 == len(flat):
            lo = max(lo, hi - 1)
        prefix = [0] * (n + 2)
        for p in range(n + 1):
            prefix[p + 1] = prefix[p] + reach[p]
        nxt = [False] * (n + 1)
        for pos in range(lo, hi):
            a, b = bounds(d, blind, pos)
            a, b = max(a, -1), min(b, pos - 1)
            if a <= b and prefix[b + 2] - prefix[a + 1] > 0:
                nxt[pos + 1] = True
        if not any(nxt):
            saw = {"0": "no movement", "+": "movement up", "-": "movement down"}.get(
                d, "movement both ways"
            )
            which = "first" if k == 0 else f"{k + 1}th" if k > 2 else ("2nd", "3rd")[k - 1]
            return [
                f"live step {i}'s {which} sample saw {saw} since the poll before, and no "
                f"place in the simulator's steps {spans[i][0]} to {spans[i][1] - 1} fits "
                f"that after where the earlier samples fit: {step}"
            ]
        reach = nxt
    return []


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
    problems = compare(live_steps, sim_steps, live.header.get("interval"))
    print()
    print("\n".join(problems) if problems else "every live step appears in the second trace")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
