"""The trace format, signature and comparison, with no printer involved.

`test_traces.py` holds the replays against recorded traces. These tests pin down what the
signature says about hand-built samples, that `compare()` notices a difference in each
field, and that a correct simulator passes against its own replays polled the way the
probe polls, at any interval and offset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from simulator import probe, trace
from simulator.model import PrintSpec, Segment
from simulator.tests.test_traces import REPLAYS, Recorder, recorder
from simulator.tests.util import make

PLA = {"name": "PLA", "color": "#73737b", "instance_uuid": "u1"}
PETG = {"name": "PETG", "color": "#1f6fb2", "instance_uuid": "u2"}


def sample(t, state, extruded, material=None, unloads=0, job=None, boot=1, **kw):
    """A reachable sample for tool 0. `filament=None` means that read failed."""
    tool = {"index": 0, "extruded": extruded, "unload_count": unloads, "extruded_at_unload": 0}
    if material:
        tool["material"] = material
    status = {"printer": {"state": state}}
    if job is not None:
        status["job"] = {"id": job}
    filament = kw.pop("filament", {"boot_id": boot, "tools": [tool]})
    return trace.Sample(t, status, filament, None, kw.pop("error", None))


def down(t):
    return trace.Sample(t, error="connection refused")


def names(steps):
    return [str(s) for s in steps]


# The signature.


def test_signature_collapses_runs_and_keeps_events():
    samples = [
        sample(0, "IDLE", 100, PLA),
        sample(1, "IDLE", 100, PLA),
        sample(2, "BUSY", 100, PLA),
        sample(3, "BUSY", 100, None, unloads=1),
        sample(4, "IDLE", 100, None, unloads=1),
        sample(5, "IDLE", 110, None, unloads=1),
        sample(6, "IDLE", 120, None, unloads=1),
        sample(7, "IDLE", 120, None, unloads=1),
        sample(8, "PRINTING", 120, None, unloads=1, job=7),
        sample(9, "PRINTING", 130, None, unloads=1, job=7),
        sample(10, "IDLE", 0, None, unloads=0, boot=2),
    ]
    steps = trace.signature(samples)
    assert names(steps) == [
        "IDLE job=none mat=tagged ext=0",
        "BUSY job=none mat=tagged ext=0",
        "BUSY job=none mat=none! unload+1 ext=0",
        "IDLE job=none mat=none ext=+",
        "PRINTING job=new mat=none ext=0",
        "PRINTING job=same mat=none ext=+",
        "IDLE job=none boot=new mat=none ext=0",
    ]
    idle = steps[3]
    assert (idle.entry, idle.interior, idle.samples, idle.moves) == ("0", "+", 4, 2)
    assert (steps[0].samples, steps[2].samples) == (2, 1)


def test_signature_sees_through_an_unreachable_gap():
    # A power cut: the job ID survives, the boot ID changes, the counters restart.
    kept = [
        sample(0, "PRINTING", 500, PLA, job=7),
        down(1),
        down(2),
        sample(3, "BUSY", 0, PLA, job=7, boot=2),
        sample(4, "BUSY", 25, PLA, job=7, boot=2),
    ]
    assert names(trace.signature(kept)) == [
        "PRINTING job=new mat=tagged ext=0",
        "unreachable job=none mat=unknown ext=0",
        "BUSY job=same boot=new mat=tagged ext=0",
        "BUSY job=same mat=tagged ext=+",
    ]
    # A new job ID, a kept boot ID, or a changed material after the gap each show.
    new_job = kept[:3] + [sample(3, "BUSY", 0, PLA, job=8, boot=2)]
    assert names(trace.signature(new_job))[2] == "BUSY job=new boot=new mat=tagged ext=0"
    same_boot = kept[:3] + [sample(3, "IDLE", 500, PLA)]
    assert names(trace.signature(same_boot))[2] == "IDLE job=none mat=tagged ext=0"
    swapped = kept[:3] + [sample(3, "IDLE", 0, PETG, boot=2)]
    assert names(trace.signature(swapped))[2] == "IDLE job=none boot=new mat=tagged! ext=0"
    # After a boot the counters restart, so what the first sample shows is counted from
    # zero: an unload counted by the idle sensor clear, or a purge already made.
    cleared = kept[:3] + [sample(3, "IDLE", 0, None, unloads=1, boot=2)]
    assert names(trace.signature(cleared))[2] == "IDLE job=none boot=new mat=none! unload+1 ext=0"
    purged = kept[:3] + [sample(3, "BUSY", 25, PLA, job=7, boot=2)]
    assert names(trace.signature(purged))[2] == "BUSY job=same boot=new mat=tagged ext=+"


def test_signature_keeps_the_last_filament_when_only_that_read_fails():
    samples = [
        sample(0, "PRINTING", 100, PLA, job=7),
        sample(1, "PRINTING", 110, PLA, job=7),
        sample(2, "PRINTING", 110, PLA, job=7, filament=None, error="/api/v1/filament: 500"),
        sample(3, "PRINTING", 130, PLA, job=7),
    ]
    assert names(trace.signature(samples)) == [
        "PRINTING job=new mat=tagged ext=0",
        "PRINTING job=same mat=tagged ext=+",
    ]


def test_signature_job_hidden_by_the_blip_is_the_same_job():
    samples = [
        sample(0, "IDLE", 100, PLA),
        sample(1, "PRINTING", 100, PLA, job=7),
        sample(2, "IDLE", 100, PLA),
        sample(3, "PRINTING", 100, PLA, job=7),
        sample(4, "PRINTING", 140, PLA, job=7),
        sample(5, "FINISHED", 140, PLA),
        sample(6, "PRINTING", 140, PLA, job=8),
    ]
    assert names(trace.signature(samples)) == [
        "IDLE job=none mat=tagged ext=0",
        "PRINTING job=new mat=tagged ext=0",
        "IDLE job=none mat=tagged ext=0",
        "PRINTING job=same mat=tagged ext=+",
        "FINISHED job=none mat=tagged ext=0",
        "PRINTING job=new mat=tagged ext=0",
    ]


# The comparison, on hand-built traces.

BASE = [
    sample(0, "IDLE", 100, PLA),
    sample(1, "BUSY", 100, PLA),
    sample(2, "BUSY", 100, None, unloads=1),
    sample(3, "IDLE", 100, None, unloads=1),
    sample(4, "BUSY", 120, None, unloads=1),
    sample(5, "BUSY", 160, PETG, unloads=1),
    sample(6, "IDLE", 160, PETG, unloads=1),
]


def test_compare_passes_an_identical_trace_and_a_sparser_one():
    full = trace.signature(BASE)
    assert trace.compare(full, full) == []
    # The probe missed the BUSY before the unload and the idle between unload and load.
    sparse = trace.signature([BASE[0], BASE[2], BASE[4], BASE[5], BASE[6]])
    assert len(sparse) < len(full)
    assert trace.compare(sparse, full) == []


@pytest.mark.parametrize(
    "index, change, field",
    [
        # An event's state may be any the printer reached before its next event, so a
        # state difference shows in a run, not at the event.
        (3, lambda: sample(3, "PAUSED", 100, None, unloads=1), "state"),
        (2, lambda: sample(2, "BUSY", 100, None, unloads=1, job=3), "job"),
        (2, lambda: sample(2, "BUSY", 100, None, unloads=1, boot=9), "boot"),
        (2, lambda: sample(2, "BUSY", 100, {"name": "PLA"}, unloads=1), "material"),
        (2, lambda: sample(2, "BUSY", 100, PLA, unloads=1), "changed"),
        (2, lambda: sample(2, "BUSY", 100, None, unloads=2), "unloads"),
    ],
)
def test_compare_reports_a_difference_in_each_shape_field(index, change, field):
    live = trace.signature(BASE)
    altered = list(BASE)
    altered[index] = change()
    sim = trace.signature(altered)
    assert trace.compare(live, sim) != [], field
    assert trace.compare(sim, live) != [], field


def test_compare_reports_movement_differences():
    def pause(moving: bool):
        e = 150
        out = [sample(0, "IDLE", 100, PLA), sample(1, "PRINTING", 100, PLA, job=7)]
        out += [sample(2 + n, "PRINTING", 100 + 10 * (n + 1), PLA, job=7) for n in range(5)]
        for n in range(6):  # six seconds paused
            e += 5 if moving else 0
            out.append(sample(7 + n, "PAUSED", e, PLA, job=7))
        out += [sample(13 + n, "PRINTING", e + 10 * (n + 1), PLA, job=7) for n in range(5)]
        out.append(sample(18, "FINISHED", e + 50, PLA))
        return trace.signature(out)

    still, moving = pause(False), pause(True)
    assert trace.compare(still, still) == [] and trace.compare(moving, moving) == []
    # A simulator that moves the counter through a pause, or one that holds it while the
    # printer moved, is wrong either way.
    assert "where the printer showed none" in trace.compare(still, moving)[0]
    assert "moved '+' within it" in trace.compare(moving, still)[0]

    # Movement in a step's first sample straddles the change before it: a FINISHED poll
    # that still carries the last second of printing is fine, while one in a step whose
    # neighbors never moved is not.
    straddle = trace.signature(
        [
            sample(0, "IDLE", 100, PLA),
            sample(1, "PRINTING", 100, PLA, job=7),
            sample(2, "PRINTING", 120, PLA, job=7),
            sample(3, "PRINTING", 140, PLA, job=7),
            sample(4, "FINISHED", 150, PLA),
            sample(5, "FINISHED", 150, PLA),
        ]
    )
    assert straddle[-1].entry == "+" and straddle[-1].interior == "0"
    fine = trace.signature(
        [sample(0, "IDLE", 100, PLA), sample(1, "PRINTING", 100, PLA, job=7)]
        + [sample(1 + n / 10, "PRINTING", 100 + 2 * n, PLA, job=7) for n in range(1, 30)]
        + [sample(4 + n / 10, "FINISHED", 158, PLA) for n in range(20)]
    )
    assert trace.compare(straddle, fine) == []
    quiet = trace.signature(
        [sample(0, "IDLE", 100, PLA), sample(1, "IDLE", 100, PLA), sample(2, "BUSY", 130, PLA)]
        + [sample(3, "BUSY", 130, PLA), sample(4, "IDLE", 130, PLA)]
    )
    never = trace.signature(
        [sample(0, "IDLE", 100, PLA), sample(1, "IDLE", 100, PLA), sample(2, "BUSY", 100, PLA)]
        + [sample(3, "BUSY", 100, PLA), sample(4, "IDLE", 100, PLA)]
    )
    assert "neither there nor just before" in trace.compare(quiet, never)[0]


def test_compare_rejects_events_the_printer_did_not_show():
    live = trace.signature(BASE)
    doubled = BASE[:3] + [
        sample(2.5, "BUSY", 100, None, unloads=2),
        sample(3, "IDLE", 100, None, unloads=2),
        sample(4, "BUSY", 120, None, unloads=2),
        sample(5, "BUSY", 160, PETG, unloads=2),
        sample(6, "IDLE", 160, PETG, unloads=2),
    ]
    problems = trace.compare(live, trace.signature(doubled))
    assert problems and "event the printer didn't show" in problems[0]

    # An extra job ID partway through, and an extra event after the printer's last step.
    extra_job = list(BASE)
    extra_job.insert(4, sample(3.5, "IDLE", 100, None, unloads=1, job=4))
    assert trace.compare(live, trace.signature(extra_job)) != []
    trailing = BASE + [sample(7, "IDLE", 160, PETG, unloads=1, boot=2)]
    problems = trace.compare(live, trace.signature(trailing))
    assert problems and "after the printer's last" in problems[0]
    # Extra non-event steps after the printer's last are fine, like extra idle time.
    longer = trace.signature(BASE + [sample(7, "IDLE", 160, PETG, unloads=1)])
    assert trace.compare(live, longer) == []


def test_compare_lets_brief_movement_hide_in_the_poll_that_crossed_a_step():
    # The load's counted feed at the end of a long ATTENTION rest: a coarse probe saw
    # rest, rest, then the type change carrying the movement. That passes. A simulator
    # that moved through the whole rest would have been seen between the live samples.
    live = trace.signature(
        [
            sample(0, "ATTENTION", 100, None, job=7),
            sample(2, "ATTENTION", 100, None, job=7),
            sample(4, "ATTENTION", 100, None, job=7),
            sample(6, "ATTENTION", 120, PETG, job=7),
        ]
    )
    rest = [sample(n / 2, "ATTENTION", 100, None, job=7) for n in range(10)]
    tail = [sample(5, "ATTENTION", 110, None, job=7), sample(5.5, "ATTENTION", 120, None, job=7)]
    change = [sample(6, "ATTENTION", 120, PETG, job=7)]
    assert trace.compare(live, trace.signature(rest + tail + change)) == []
    throughout = [sample(n / 2, "ATTENTION", 100 + 2 * n, None, job=7) for n in range(12)]
    problems = trace.compare(live, trace.signature(throughout + change))
    assert "where the printer showed none" in problems[0]
    # Movement right before a power cut is lost with the counters, so it isn't demanded.
    live = trace.signature([sample(0, "PRINTING", 100, PLA, job=7), down(2), down(4)])
    sim = trace.signature(
        [sample(n / 2, "PRINTING", 100 + 5 * n, PLA, job=7) for n in range(4)] + [down(2), down(4)]
    )
    assert trace.compare(live, sim) == []


def test_compare_places_simulator_movement_by_where_it_falls():
    gap = 1.0
    still = [sample(0, "IDLE", 100, PLA)] + [sample(t, "BUSY", 100, PLA) for t in range(1, 22)]
    still += [sample(22, "IDLE", 100, PLA), sample(23, "IDLE", 100, PLA)]
    live = trace.signature(still)

    def busy(moves: dict[float, int]) -> list[trace.Step]:
        """A BUSY run of 20 s that extrudes `mm` at each of `moves`' seconds in one tick."""
        e, out = 100, [sample(0, "IDLE", 100, PLA)]
        t = 0.1
        while t < 22.0 - 1e-9:
            e += moves.get(round(t, 1), 0)
            out.append(sample(round(t, 1), "BUSY" if t < 21.5 else "IDLE", e, PLA))
            t += 0.1
        return trace.signature(out)

    # A purge as BUSY begins shows in the live step's first sample on the printer too.
    purge = [sample(0, "IDLE", 100, PLA), sample(1, "BUSY", 140, PLA)]
    purge += [sample(t, "BUSY", 140, PLA) for t in range(2, 22)] + [sample(22, "IDLE", 140, PLA)]
    assert (
        trace.compare(trace.signature(purge), busy({0.2: 10, 0.3: 10, 0.4: 10, 0.5: 10}), gap) == []
    )
    # The same purge against a printer that never moved is caught, as is a single tick's
    # jump in the middle of the run, or at its end, and a retraction.
    assert "at the start of" in trace.compare(live, busy({0.3: 40}), gap)[0]
    assert "in the middle of" in trace.compare(live, busy({10.0: 50}), gap)[0]
    assert "at the end of" in trace.compare(live, busy({21.3: 50}), gap)[0]
    assert "moved '-' in the middle" in trace.compare(live, busy({10.0: -50}), gap)[0]
    # Movement in the last poll gap before a power cut is lost with the counters, but
    # movement earlier in the stretch is not.
    cut = [sample(0, "IDLE", 100, PLA)] + [sample(t, "IDLE", 100, PLA) for t in range(1, 30)]
    cut += [down(30), down(31)]
    live_cut = trace.signature(cut)

    def idle_then_cut(moves: dict[float, int]) -> list[trace.Step]:
        e, out, t = 100, [], 0.0
        while t < 30.0 - 1e-9:
            e += moves.get(round(t, 1), 0)
            out.append(sample(round(t, 1), "IDLE", e, PLA))
            t += 0.1
        return trace.signature(out + [down(30), down(31)])

    assert trace.compare(live_cut, idle_then_cut({29.5: 20}), gap) == []
    assert "in the middle of" in trace.compare(live_cut, idle_then_cut({15.0: 20}), gap)[0]


def test_compare_folds_consecutive_events_only():
    # Two events in consecutive simulator samples, one live sample: the idle sensor clear
    # counted at boot is the common case, and here an unload and a type restore.
    live = trace.signature([sample(0, "IDLE", 100, PLA), sample(1, "IDLE", 100, PETG, unloads=1)])
    sim = trace.signature(
        [
            sample(0, "IDLE", 100, PLA),
            sample(0.5, "IDLE", 100, None, unloads=1),
            sample(0.6, "IDLE", 100, PETG, unloads=1),
            sample(0.7, "IDLE", 100, PETG, unloads=1),
        ]
    )
    assert trace.compare(live, sim) == []
    # Runs between two events fold too while together they last less than a poll gap:
    # the sync job's scan between the job appearing and its M865 applying, for example.
    # Longer than that, the probe would have seen them, so S14's two unloads with a load
    # between them can't hide in one live sample.
    live = trace.signature([sample(0, "IDLE", 100, PLA), sample(1, "IDLE", 150, None, unloads=2)])

    def two(load_seconds: float) -> list[trace.Step]:
        n = round(load_seconds / 0.1)
        return trace.signature(
            [sample(0, "IDLE", 100, PLA), sample(0.1, "IDLE", 100, None, unloads=1)]
            + [sample(0.2 + k / 10, "BUSY", 110 + k, None, unloads=1) for k in range(n)]
            + [sample(0.2 + n / 10, "BUSY", 150, PETG, unloads=1)]
            + [sample(0.3 + (n + k) / 10, "IDLE", 150, PETG, unloads=1) for k in range(n)]
            + [sample(0.3 + 2 * n / 10, "IDLE", 150, None, unloads=2)]
        )

    assert trace.compare(live, two(0.3)) == []
    assert "event the printer didn't show" in trace.compare(live, two(3.0))[0]


def test_compare_lets_a_live_event_carry_a_later_state_without_eating_the_run():
    # The probe saw the unload once the load had begun: BUSY, where the simulator shows
    # the unload at IDLE and then a BUSY run the next live step still has to match.
    live = trace.signature(
        [
            sample(0, "IDLE", 100, PLA),
            sample(1, "BUSY", 100, None, unloads=1),
            sample(2, "BUSY", 120, None, unloads=1),
            sample(3, "BUSY", 140, None, unloads=1),
        ]
    )
    sim = trace.signature(
        [
            sample(0, "IDLE", 100, PLA),
            sample(0.5, "IDLE", 100, None, unloads=1),
            sample(0.6, "IDLE", 100, None, unloads=1),
            sample(0.7, "BUSY", 100, None, unloads=1),
            sample(1.0, "BUSY", 110, None, unloads=1),
            sample(2.0, "BUSY", 130, None, unloads=1),
            sample(3.0, "BUSY", 140, None, unloads=1),
        ]
    )
    assert trace.compare(live, sim) == []
    # But not a state from after the next event.
    later = trace.signature(
        [
            sample(0, "IDLE", 100, PLA),
            sample(0.5, "IDLE", 100, None, unloads=1),
            sample(0.6, "IDLE", 100, PETG, unloads=1),
            sample(0.7, "BUSY", 100, PETG, unloads=1),
        ]
    )
    assert trace.compare(live, later) != []


# The probe's thinning, over the replays.


def polled(rec: Recorder, interval: float, offset: int) -> list[trace.Sample]:
    """The samples a probe at `interval` would have seen, starting `offset` ticks in."""
    step = round(interval / 0.1)
    return rec.samples[offset::step]


@pytest.mark.parametrize("interval", [0.5, 1.0, 2.0])
@pytest.mark.parametrize("name", REPLAYS)
def test_a_correct_simulator_passes_its_own_replay_polled_like_the_probe(name, interval):
    rec = recorder()
    REPLAYS[name](rec)
    full = rec.steps()
    step = round(interval / 0.1)
    for offset in range(step):
        samples = polled(rec, interval, offset)
        kept = probe.thin(samples, heartbeat=10.0)
        assert trace.signature(kept) == trace.signature(samples), (name, interval, offset)
        live = trace.signature(kept)
        problems = trace.compare(live, full, interval)
        assert not problems, "\n".join(
            [f"{name} at {interval} s, offset {offset}:"]
            + problems
            + ["", "polled:", trace.describe(live), "", "full:", trace.describe(full)]
        )


def test_thinning_keeps_fewer_samples_and_every_event():
    rec = recorder()
    REPLAYS["F4-load-tag"](rec)
    samples = polled(rec, 1.0, 0)
    kept = probe.thin(samples, heartbeat=10.0)
    assert len(kept) < len(samples)
    assert [s for s in trace.signature(kept) if s.is_event()] == [
        s for s in trace.signature(samples) if s.is_event()
    ]
    # The last run's end is held back until the probe stops, then flushed.
    tail = [sample(t, "IDLE", 100, PLA) for t in (0, 1, 2)]
    assert probe.thin(tail, heartbeat=10.0) == [tail[0], tail[2]]


# The file format and the command.


def test_round_trip(tmp_path: Path):
    sim, printer = make()
    rec = Recorder(sim, printer)
    rec.advance(0.3)
    printer.set_power("off")
    rec.advance(0.3)
    printer.set_power("on")
    rec.advance(0.3)
    t = trace.Trace(trace.header("round-trip", note="test"), list(rec.samples))
    t.entries.insert(2, trace.Mark(0.15, "switched off"))
    path = tmp_path / "t.jsonl"
    trace.write(path, t)
    back = trace.read(path)
    assert back.header["name"] == "round-trip"
    assert [m.text for m in back.marks] == ["switched off"]
    assert trace.signature(back.samples) == rec.steps()
    assert [s.state for s in rec.steps()] == ["IDLE", trace.UNREACHABLE, "IDLE", "IDLE"]
    assert rec.steps()[2].boot == "new"


def test_cli_prints_signature_and_compares(tmp_path: Path, capsys):
    sim, printer = make()
    rec = Recorder(sim, printer)
    rec.advance(0.3)
    path = tmp_path / "t.jsonl"
    trace.write(path, trace.Trace(trace.header("cli"), list(rec.samples)))
    assert trace.main([str(path), str(path)]) == 0
    out = capsys.readouterr().out
    assert "IDLE job=none mat=tagged" in out and "every live step appears" in out

    printer.start_print(PrintSpec([Segment(0, 50)]))
    rec.ended()
    other = tmp_path / "u.jsonl"
    trace.write(other, trace.Trace(trace.header("cli2"), list(rec.samples)))
    assert trace.main([str(other), str(path)]) == 1  # the print never happens in `path`
