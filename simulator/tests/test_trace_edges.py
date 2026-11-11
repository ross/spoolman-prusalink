"""Malformed trace files, odd samples and the problems `compare()` reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simulator import trace
from simulator.tests.test_plumbing_traces import BASE, PETG, PLA, down, names, sample


def header_line() -> str:
    return json.dumps({"trace": trace.FORMAT, "name": "x"})


def read_text(tmp_path: Path, text: str) -> trace.Trace:
    path = tmp_path / "t.jsonl"
    path.write_text(text)
    return trace.read(path)


# Reading.


def test_read_refuses_an_empty_file(tmp_path):
    with pytest.raises(ValueError, match="empty trace"):
        read_text(tmp_path, "\n  \n")


def test_read_refuses_a_file_without_a_header(tmp_path):
    with pytest.raises(ValueError, match="not a trace header"):
        read_text(tmp_path, '{"t": 0.0}\n')
    with pytest.raises(ValueError, match="not a trace header"):
        read_text(tmp_path, json.dumps({"trace": trace.FORMAT + 1}) + "\n")


def test_read_names_the_line_that_is_neither_sample_nor_mark(tmp_path):
    text = header_line() + '\n{"t": 0.0, "status": null}\n{"what": 1}\n'
    with pytest.raises(ValueError, match=r"t\.jsonl:3: neither a sample nor a mark"):
        read_text(tmp_path, text)


def test_read_skips_blank_lines_and_keeps_marks_apart(tmp_path):
    text = header_line() + '\n\n{"t": 1.5, "mark": "cut"}\n{"t": 2, "error": "boom"}\n'
    t = read_text(tmp_path, text)
    assert [(m.t, m.text) for m in t.marks] == [(1.5, "cut")]
    assert [(s.t, s.error) for s in t.samples] == [(2.0, "boom")]


# The signature of samples the printer's API can produce.


def test_signature_tells_a_color_only_material_from_a_name_only_one():
    samples = [
        sample(0, "IDLE", 0, {"name": "PLA", "color": "#73737b"}),
        sample(1, "IDLE", 0, {"name": "PLA"}),
        sample(2, "IDLE", 0, {"color": "#73737b"}),
    ]
    assert names(trace.signature(samples)) == [
        "IDLE job=none mat=color ext=0",
        "IDLE job=none mat=name! ext=0",
        "IDLE job=none mat=none! ext=0",
    ]


def test_signature_of_a_tool_the_filament_endpoint_does_not_list():
    empty = {"boot_id": 1, "tools": []}
    samples = [sample(0, "IDLE", 0, PLA), sample(1, "IDLE", 0, PLA, filament=empty)]
    assert names(trace.signature(samples)) == [
        "IDLE job=none mat=tagged ext=0",
        "IDLE job=none mat=unknown ext=0",
    ]
    # Asking for another tool than the samples carry sees none of them.
    assert names(trace.signature(samples, tool=1)) == ["IDLE job=none mat=unknown ext=0"]
    # A filament body with no tools at all, as when the endpoint answers with an empty object.
    bare = [sample(0, "IDLE", 0, filament={})]
    assert names(trace.signature(bare)) == ["IDLE job=none mat=unknown ext=0"]


def test_signature_shows_a_counter_that_moved_both_ways_within_a_run():
    samples = [
        sample(0, "PRINTING", 100, PLA, job=1),
        sample(1, "PRINTING", 120, PLA, job=1),
        sample(2, "PRINTING", 90, PLA, job=1),
    ]
    steps = trace.signature(samples)
    assert names(steps) == [
        "PRINTING job=new mat=tagged ext=0",
        "PRINTING job=same mat=tagged ext=±",
    ]
    assert steps[1].samples == 2


def test_a_poll_that_moved_both_ways_stands_for_any_movement():
    both = trace.signature(
        [
            sample(0, "PRINTING", 100, PLA, job=1),
            sample(1, "PRINTING", 130, PLA, job=1),
            sample(2, "PRINTING", 90, PLA, job=1),
        ]
    )
    up = trace.signature(
        [
            sample(0, "PRINTING", 100, PLA, job=1),
            sample(1, "PRINTING", 130, PLA, job=1),
            sample(2, "PRINTING", 140, PLA, job=1),
        ]
    )
    assert trace.compare(both, both) == []
    # A simulator that only ever moves up can't account for the live counter stepping back.
    assert trace.compare(both, up) != []


# What compare() says about a simulator that doesn't match.


def test_compare_names_a_live_state_the_simulator_never_reaches():
    live = trace.signature(
        [sample(0, "IDLE", 100), sample(1, "PAUSED", 100), sample(2, "IDLE", 100)]
    )
    sim = trace.signature([sample(0, "IDLE", 100), sample(1, "IDLE", 100)])
    problems = trace.compare(live, sim)
    assert problems[0].startswith("live step 1 not found in the simulator's steps: PAUSED")
    assert problems[1] == f"  (the next live step is: {live[2]})"
    # With nothing after the missing step, only the first line is reported.
    problems = trace.compare(live[:2], sim)
    assert len(problems) == 1 and "live step 1 not found" in problems[0]


def test_compare_names_a_live_event_the_simulator_never_reaches():
    live = trace.signature([sample(0, "IDLE", 100, PLA), sample(1, "BUSY", 100, None, unloads=1)])
    sim = trace.signature([sample(0, "IDLE", 100, PLA), sample(1, "BUSY", 100, PLA)])
    problems = trace.compare(live, sim)
    assert problems and "not found in the simulator's steps" in problems[0]


def test_compare_names_a_simulator_event_the_printer_did_not_show():
    live = trace.signature([sample(0, "IDLE", 100, PLA), sample(1, "BUSY", 100, PLA)])
    sim = trace.signature([sample(0, "IDLE", 100, PLA), sample(1, "BUSY", 100, None, unloads=1)])
    problems = trace.compare(live, sim)
    assert problems[0].startswith("simulator step 1 is an event the printer didn't show")
    assert "unload+1" in problems[0]
    assert problems[1].startswith("  (while looking for live step 1: BUSY")


def test_compare_does_not_fold_more_events_than_the_window_allows():
    def unload(n: int) -> trace.Step:
        return trace.Step("BUSY", "none", "same", "none", False, n, "0", t_first=n, t_last=n)

    idle = trace.Step("IDLE", "none", "same", "tagged", False, 0, "0")
    # Six consecutive unloads, each an event the printer would have shown.
    sim = [idle] + [unload(1)] * 6
    live = [idle, unload(6)]
    problems = trace.compare(live, sim)
    assert problems and "event the printer didn't show" in problems[0]
    # Within the window one live event can stand for them all.
    assert trace.compare([idle, unload(3)], [idle] + [unload(1)] * 3) == []


def test_compare_reports_a_run_the_probe_would_have_seen():
    # The simulator spends 3 s in PAUSED between two states the probe saw, with a poll every
    # second; the printer would have shown it.
    live = trace.signature(
        [sample(0, "PRINTING", 100, PLA, job=1), sample(1, "PRINTING", 100, PLA, job=1)]
    )
    assert live  # a baseline the rest builds on
    seen = [sample(i * 0.1, "PRINTING", 100, PLA, job=1) for i in range(5)]
    seen += [sample(0.5 + i * 0.1, "PAUSED", 100, PLA, job=1) for i in range(30)]
    seen += [sample(3.5 + i * 0.1, "PRINTING", 100, PLA, job=1) for i in range(5)]
    polled = [sample(0, "PRINTING", 100, PLA, job=1), sample(1, "PRINTING", 100, PLA, job=1)]
    problems = trace.compare(trace.signature(polled), trace.signature(seen), gap=1.0)
    assert problems


def test_compare_of_unreachable_samples_matches_unreachable_steps():
    live = trace.signature([sample(0, "IDLE", 1, PLA), down(1), sample(2, "IDLE", 1, PLA)])
    assert [s.state for s in live] == ["IDLE", trace.UNREACHABLE, "IDLE"]
    assert trace.compare(live, live) == []
    assert trace.compare(live, trace.signature(BASE)) != []
    assert PETG["name"] == "PETG"


def test_compare_places_a_poll_that_saw_the_counter_step_back():
    live = trace.signature([sample(0, "IDLE", 100, PLA), sample(1, "IDLE", 90, PLA)])
    fine = [sample(i * 0.25, "IDLE", 100 if i < 3 else 90, PLA) for i in range(5)]
    assert trace.compare(live, trace.signature(fine), gap=1.0) == []
    flat = [sample(i * 0.25, "IDLE", 100, PLA) for i in range(5)]
    assert trace.compare(live, trace.signature(flat), gap=1.0) != []


def test_cli_prints_marks(tmp_path, capsys):
    t = trace.Trace(trace.header("marks"), [sample(0, "IDLE", 0, PLA)])
    t.entries.append(trace.Mark(1.5, "switched off"))
    path = tmp_path / "t.jsonl"
    trace.write(path, t)
    assert trace.main([str(path)]) == 0
    assert "mark      1.5s: switched off" in capsys.readouterr().out
