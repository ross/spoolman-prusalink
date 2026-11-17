"""The chamber vents at a print's start and end."""

from __future__ import annotations

import pytest

from simulator import prusalink
from simulator.model import Busy, JobPhase, Material, PrintSpec, Segment, SimTool, State
from simulator.tests.util import loaded, make, run

PETG = Material("PETG", "#186bb5")
ASA = Material("ASA", "#000000")
SYNC = """M862.3 P "COREONEL"
M862.5 P2
M862.6 P"Input shaper"
G28 O
M865 X R N"PETG" L0
"""


def started(model="8.1.0", material=PETG, file_type=None, **settings):
    """A print on tool 0 holding `material`, past its scan, with a 2 s vent move."""
    sim, p = make(model, [loaded(0, material)], vents_seconds=2.0, **settings)
    p.start_print(PrintSpec([Segment(0, 100, file_type)], preheat_s=1.0))
    sim.tick(0.5)  # the scan
    return sim, p


def test_the_automatic_move_reads_as_the_preview_before_it():
    sim, p = started()
    # The preview asked nothing, so it reads IDLE with no job, and with no blip first.
    assert p.job.phase is JobPhase.VENTS and p.state is State.IDLE and p.vents_open
    assert prusalink.job(p) is None and "job" not in prusalink.status(p)
    before = p.job.time_printing
    sim.tick(1.0)
    assert p.job.time_printing == pytest.approx(before + 1.0)  # the job's timer runs
    sim.tick(1.0)
    assert p.job.phase is JobPhase.PREHEAT and p.state is State.PRINTING


def test_after_a_question_the_move_reads_attention_with_the_job():
    sim, p = make(tools=[SimTool(0)], vents_seconds=2.0)  # empty: the preview asks
    p.start_print(PrintSpec([Segment(0, 100, "PETG")]))
    sim.tick(0.5)
    assert p.state is State.ATTENTION
    p.tap_through()  # Print at the warning
    p.tap_through()  # Disable FS
    assert p.job.phase is JobPhase.VENTS and p.state is State.ATTENTION
    assert not p.sensors and prusalink.job(p)["state"] == "PAUSED"
    # The tool has no type, so the file's opens the vents.
    assert p.vents_open


def test_the_vents_close_after_the_print_and_open_again_for_the_next():
    sim, p = started()
    run(sim, 30.0)
    assert p.job is None and p.vents_open is False
    p.dismiss()
    p.start_print(PrintSpec([Segment(0, 10)]))
    sim.tick(0.5)
    assert p.job.phase is JobPhase.VENTS


def test_vents_already_in_place_dont_move():
    sim, p = make(tools=[loaded(0, PETG)], vents_seconds=2.0)
    p.vents_open = True
    p.start_print(PrintSpec([Segment(0, 10)]))
    sim.tick(0.5)
    assert p.job.phase is JobPhase.BLIP


def test_a_closing_type_outranks_an_opening_one():
    sim, p = make("8.10.0", [loaded(0, PETG), loaded(1, ASA)], vents_seconds=2.0, start_blip=False)
    p.start_print(PrintSpec([Segment(0, 10), Segment(1, 10)]))
    sim.tick(0.5)
    p.tap_through()  # the tools-mapping screen
    assert p.job.phase is JobPhase.VENTS and p.vents_open is False
    assert p.state is State.ATTENTION


@pytest.mark.parametrize(
    "model, material", [("8.1.0", Material("XYZ")), ("1.4.0", PETG), ("7.1.0", PETG)]
)
def test_no_move_without_a_target_vents_or_automatic_control(model, material):
    sim, p = started(model, material)
    assert p.job.phase is JobPhase.BLIP
    # Manual control records the state it asked for; the warning isn't modeled.
    assert p.vents_open is (True if model == "7.1.0" else None)


def test_a_move_that_takes_no_time_still_moves_them():
    sim, p = make(tools=[loaded(0, PETG)])
    p.start_print(PrintSpec([Segment(0, 10)]))
    sim.tick(0.5)
    assert p.job.phase is JobPhase.BLIP and p.vents_open


def test_a_sync_job_moves_no_vents():
    sim, p = make(tools=[loaded(0, PETG)], vents_seconds=2.0)
    p.upload("sync.gcode", SYNC, print_after=True, overwrite=True)
    run(sim, 10.0)
    assert p.job is None and p.vents_open is None


def test_an_upload_during_the_move_is_dropped_or_refused_as_the_state_says():
    sim, p = started()
    p.upload("sync.gcode", SYNC, print_after=True, overwrite=True)  # IDLE: no Busy
    assert p.job.kind == "user" and p.job.phase is JobPhase.VENTS
    sim, p = make(tools=[SimTool(0)], vents_seconds=2.0)
    p.start_print(PrintSpec([Segment(0, 100, "PETG")]))
    sim.tick(0.5)
    p.tap_through()
    p.tap_through()
    with pytest.raises(Busy):  # ATTENTION refuses
        p.upload("sync.gcode", SYNC, print_after=True, overwrite=True)


def test_a_boot_forgets_where_the_vents_are():
    sim, p = started()
    p.set_power("off")
    p.set_power("on")
    assert p.vents_open is None
