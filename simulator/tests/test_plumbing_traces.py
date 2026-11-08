"""The trace format, signature and comparison, with no printer involved.

`test_traces.py` holds the replays against recorded traces. These tests pin down what the
signature says about hand-built samples, that `compare()` notices a difference in each
field, and that the probe's thinning keeps every event.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from simulator import probe, trace
from simulator.model import PrintSpec, Segment
from simulator.tests.test_traces import SETTINGS, Recorder
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
    assert names(trace.signature(samples)) == [
        "IDLE job=none mat=tagged ext=0",
        "BUSY job=none mat=tagged ext=0",
        "BUSY job=none mat=none! unload+1 ext=0",
        "IDLE job=none mat=none ext=+",
        "PRINTING job=new mat=none ext=0",
        "PRINTING job=same mat=none ext=+",
        "IDLE job=none boot=new mat=none ext=0",
    ]


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
    # An unload counted at boot, as the idle sensor clear does.
    cleared = kept[:3] + [sample(3, "IDLE", 0, None, boot=2)]
    assert names(trace.signature(cleared))[2] == "IDLE job=none boot=new mat=none! ext=0"


def test_signature_keeps_the_last_filament_when_only_that_read_fails():
    samples = [
        sample(0, "PRINTING", 100, PLA, job=7),
        sample(1, "PRINTING", 110, PLA, job=7),
        sample(2, "PRINTING", 110, PLA, job=7, filament=None, error="/api/v1/filament: HTTP 500"),
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
    "change, field",
    [
        (lambda s: sample(2, "IDLE", 100, None, unloads=1), "state"),
        (lambda s: sample(2, "BUSY", 100, None, unloads=1, job=3), "job"),
        (lambda s: sample(2, "BUSY", 100, None, unloads=1, boot=9), "boot"),
        (lambda s: sample(2, "BUSY", 100, {"name": "PLA"}, unloads=1), "material"),
        (lambda s: sample(2, "BUSY", 100, PLA, unloads=1), "changed"),
        (lambda s: sample(2, "BUSY", 100, None, unloads=2), "unloads"),
        (lambda s: sample(2, "BUSY", 130, None, unloads=1), "extruded"),
    ],
)
def test_compare_reports_a_difference_in_each_field(change, field):
    live = trace.signature(BASE)
    altered = list(BASE)
    altered[2] = change(altered[2])
    sim = trace.signature(altered)
    assert trace.compare(live, sim) != [], field
    assert trace.compare(sim, live) != [], field


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
    assert (
        trace.compare(live, trace.signature(BASE + [sample(7, "IDLE", 160, PETG, unloads=1)])) == []
    )


def test_compare_folds_events_one_poll_saw_together():
    # S14: two unloads between polls. The printer shows unload+2 once.
    live = trace.signature([sample(0, "IDLE", 100, PLA), sample(1, "IDLE", 150, None, unloads=2)])
    two = trace.signature(
        [
            sample(0, "IDLE", 100, PLA),
            sample(0.3, "IDLE", 100, None, unloads=1),
            sample(0.6, "BUSY", 150, PETG, unloads=1),
            sample(0.9, "IDLE", 150, None, unloads=2),
        ]
    )
    assert trace.compare(live, two) == []
    # Three unloads in the simulator don't fold into two.
    three = trace.signature(
        [
            sample(0, "IDLE", 100, PLA),
            sample(0.3, "IDLE", 100, None, unloads=1),
            sample(0.6, "BUSY", 150, PETG, unloads=1),
            sample(0.9, "IDLE", 150, None, unloads=2),
            sample(1.2, "BUSY", 150, PETG, unloads=2),
            sample(1.5, "IDLE", 150, None, unloads=3),
        ]
    )
    assert trace.compare(live, three) != []
    # A load whose type was set during a second of feeding: one live step, two simulator steps.
    idle = sample(0, "IDLE", 100, None)
    live = trace.signature([idle, sample(1, "BUSY", 100, None), sample(2, "BUSY", 120, PETG)])
    sim = trace.signature(
        [
            idle,
            sample(1, "BUSY", 100, None),
            sample(1.5, "BUSY", 120, None),
            sample(1.6, "BUSY", 120, PETG),
        ]
    )
    assert trace.compare(live, sim) == []


def test_thinning_keeps_every_event_and_the_same_signature():
    sim, printer = make(**SETTINGS)
    rec = Recorder(sim, printer)
    rec.advance(0.5)
    printer.unload(0)
    rec.settled()
    rec.advance(2.0)  # Actions at a real printer are seconds apart, so a 1 s poll sees the gap.
    printer.load(
        0, source="tag", name="PETG", color="#1f6fb2", uuid="7b3d2d5e-1f7a-4d8c-9a1e-2c3b4d5e6f70"
    )
    rec.settled()
    rec.advance(2.0)
    printer.upload(
        "spoolman-sync.gcode",
        'M865 X R N"ASA" O"#d9342b" L0\nM865.1 I0 C"3c2d1e0f-1a2b-4c3d-8e9f-0a1b2c3d4e5f"\n',
        print_after=True,
        overwrite=True,
    )
    rec.ended()
    rec.advance(1.0)
    full = rec.steps()
    # Whatever second the probe started on, and however sparse its polls, the kept
    # samples give the printer's events, and the full replay accepts them.
    for offset in range(10):
        polled = rec.samples[offset::10]  # one poll per simulated second
        kept = probe.thin(polled, heartbeat=10.0)
        assert len(kept) < len(polled)
        assert trace.signature(kept) == trace.signature(polled)
        assert trace.compare(trace.signature(kept), full) == [], offset
    assert any(s.changed for s in trace.signature(probe.thin(rec.samples, 10.0)))


def test_round_trip(tmp_path: Path):
    sim, printer = make(**SETTINGS)
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
    sim, printer = make(**SETTINGS)
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
