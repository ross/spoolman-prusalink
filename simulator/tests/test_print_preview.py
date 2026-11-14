"""The print preview's checks of a print with segments, through the model and over HTTP."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from simulator import prusalink
from simulator.model import (
    Busy,
    JobPhase,
    Material,
    PrintSpec,
    Reason,
    Segment,
    SimTool,
    Simulation,
    State,
)
from simulator.preview import Severity
from simulator.tests.util import KEY, PLA, loaded, make

AUTH = {"X-Api-Key": KEY}
PETG = Material("PETG", "#186bb5")


def empty(index: int = 0) -> SimTool:
    """A tool emptied by an unload: no filament, no type, the tracker's gap at its limit."""
    return SimTool(index, gap=40.0, gap_valid=False)


def seen(p) -> tuple[str, int | None, str | None]:
    """The state, the job ID in `/api/v1/status`, and the state `/api/v1/job` reads."""
    status, job = prusalink.status(p), prusalink.job(p)
    return str(p.state), status.get("job", {}).get("id"), job and job["state"]


def test_an_empty_tool_asks_then_waits_for_a_load_at_the_preview():
    sim, p = make(tools=[empty()])
    p.start_print(PrintSpec([Segment(0, 10, "PLA")], preheat_s=20))
    assert seen(p) == ("PRINTING", 1, "PRINTING")  # the scan
    sim.tick(0.5)
    # The "G-code incompatible" screen, with a fixed warning.
    assert str(p.job.attention) == "preview: T0 has no filament"
    assert seen(p) == ("ATTENTION", 1, "PAUSED")
    p.tap_through()  # Print
    # The filament-not-inserted screen: a load answers Yes.
    assert p.job.phase is JobPhase.WAIT_LOAD and p.job.attention.tool == 0
    assert str(p.job.attention) == "no filament in tool 0"
    assert seen(p) == ("ATTENTION", 1, "PAUSED")
    assert p.tools[0].extruded == 0
    p.load(0, source="preset", name="PLA")
    # The load's screen covers the preview and no print runs yet: BUSY, with no job.
    assert seen(p) == ("BUSY", None, None)
    assert not p.can_print() and p.actions_json()["cancel"] == "the preview's load is running"
    # The load from the empty tool's 40 mm gap: the 40 mm slow load fills the gap and
    # counts nothing, the 20 mm fast load and the 40 mm purge count, and the 8 mm retract
    # after the purge opens a gap of 8. No auto-retract, since the printer counts as
    # printing, and no prime, since it's a plain load: 60 * 0.95 = 57. That's 108 mm at
    # 10 mm/s.
    # The type matches the file's, so the print begins in the same tick: the blip, then
    # the preheat, which moves nothing.
    sim.tick(10.8)
    assert p.job.phase is JobPhase.PREHEAT and seen(p) == ("PRINTING", 1, "PRINTING")
    assert p.tools[0].extruded == pytest.approx(57)
    assert p.tools[0].material.name == "PLA" and p.tools[0].gap == pytest.approx(8)
    # 9.5 s of preheat are left. Then 1.9 s at 5 mm/s print 9.5 mm: the first 8 fill the
    # gap and 1.5 count, 57 + 1.425 = 58.425.
    sim.tick(9.5 + 1.9)
    assert p.tools[0].extruded == pytest.approx(58.425)
    # The last 0.5 mm count, 58.9. The end of the print retracts 8 mm and stores it, and
    # the auto-retract feeds it back and counts its 23 mm: 58.9 + 21.85 = 80.75.
    sim.tick(10)
    assert p.state is State.FINISHED and p.job_counter == 1
    assert p.tools[0].extruded == pytest.approx(80.75) and p.tools[0].unload_count == 0


def test_the_load_is_the_user_s_choice_and_the_type_check_follows_it():
    sim, p = make(tools=[empty()])
    p.start_print(PrintSpec([Segment(0, 10, "PLA")], preheat_s=0))
    sim.tick(0.5)
    p.tap_through()
    p.load(0, source="preset", name="PETG")
    sim.tick(10.8)
    assert str(p.job.attention) == "wrong filament: T0 holds PETG, not PLA"
    assert seen(p) == ("ATTENTION", 1, "PAUSED")
    p.tap_through()  # Ok
    sim.tick(0.3)
    assert p.state is State.PRINTING


def test_another_type_asks_and_ok_prints():
    sim, p = make()
    p.start_print(PrintSpec([Segment(0, 10, "PETG")], preheat_s=0))
    sim.tick(0.5)
    assert p.job.waits_for(Reason.WRONG_FILAMENT) and not p.job.attention.fatal
    assert seen(p) == ("ATTENTION", 1, "PAUSED")
    p.tap_through()
    assert p.job.phase is JobPhase.BLIP
    sim.tick(10)
    assert p.state is State.FINISHED and p.tools[0].material == PLA


def test_a_file_with_no_type_or_the_same_type_asks_nothing():
    for material in (None, "PLA"):
        sim, p = make()
        p.start_print(PrintSpec([Segment(0, 10, material)], preheat_s=0))
        sim.tick(0.5)
        assert p.job.phase is JobPhase.BLIP


def to_wrong_filament(sim, p):
    p.start_print(PrintSpec([Segment(0, 10, "PETG")]))
    sim.tick(0.5)


def to_incompatible(sim, p):
    p.tools[0] = empty()
    p.start_print(PrintSpec([Segment(0, 10)]))
    sim.tick(0.5)


def to_wait_for_load(sim, p):
    to_incompatible(sim, p)
    p.tap_through()


@pytest.mark.parametrize("step", [to_incompatible, to_wait_for_load, to_wrong_filament])
def test_declining_at_each_step_ends_idle_with_the_id_used(step):
    sim, p = make()
    step(sim, p)
    assert p.state is State.ATTENTION and p.job.id == 1
    extruded = p.tools[0].extruded
    p.cancel()  # Abort, or No
    assert p.job is None and seen(p) == ("IDLE", None, None) and p.job_counter == 1
    assert p.tools[0].extruded == extruded and p.tools[0].auto_retracted is None
    sim.tick(5)
    assert p.state is State.IDLE
    p.tools[0] = loaded(0)
    p.start_print(PrintSpec([Segment(0, 10)]))
    assert p.job.id == 2


def test_the_checks_setting_governs_nothing_on_a_print():
    for severity in Severity:
        sim, p = make(tools=[empty()])
        p.set_checks(severity)
        p.start_print(PrintSpec([Segment(0, 10)]))
        sim.tick(0.5)
        assert p.job.waits_for(Reason.PREVIEW) and not p.job.attention.fatal


def test_the_preview_s_load_on_an_indx_ends_with_the_cleaner_s_auto_retract():
    # One tool enabled and a one-tool print: no tool mapping, so the preview asks for a
    # load. From the full 27 mm gap the 15 slow and 25 fast count 13, then the cleaner's 25
    # mm purge and its own auto-retract's 8: 46 * 550/576, as an idle load counts.
    sim, p = make("8.10.0", [SimTool(0, gap=27.0, gap_valid=False)])
    p.start_print(PrintSpec([Segment(0, 10)], preheat_s=0))
    sim.tick(0.5)
    p.tap_through()
    p.load(0, source="preset", name="PLA")
    while p.tools[0].process is not None:
        sim.tick(0.1)
    assert p.job.phase is JobPhase.BLIP  # the type check passed: the print begins
    assert p.tools[0].extruded == pytest.approx(46 * 550 / 576)
    assert p.tools[0].auto_retracted == 8


def cut_at_the_wait(sim, p):
    pass


def cut_during_the_load(sim, p):
    p.load(0, source="preset", name="PLA")
    sim.tick(1)
    assert p.job.phase is JobPhase.PREVIEW_LOAD


@pytest.mark.parametrize("cut", [cut_at_the_wait, cut_during_the_load])
@pytest.mark.parametrize("cold_bed", [False, True])
def test_a_power_cut_at_the_preview_s_load_resumes_nothing(cut, cold_bed):
    sim, p = make(tools=[empty()])
    p.start_print(PrintSpec([Segment(0, 10)], preheat_s=0))
    sim.tick(0.5)
    p.tap_through()
    assert p.job.phase is JobPhase.WAIT_LOAD and p.job.previewing
    cut(sim, p)
    p.reboot(keep_job=True, cold_bed=cold_bed)
    assert p.job is None and seen(p) == ("IDLE", None, None) and p.job_counter == 1
    assert p.tools[0].process is None
    sim.tick(5)
    assert p.state is State.IDLE


def test_a_disabled_used_tool_can_only_be_declined():
    sim, p = make("3.1.0", [loaded(0), loaded(1, enabled=False)])
    p.start_print(PrintSpec([Segment(0, 10), Segment(1, 10)]))
    sim.tick(0.5)
    # Two tools used and one enabled: a warning on the first screen.
    assert str(p.job.attention) == "preview: the print uses more tools than are enabled"
    p.tap_through()
    assert str(p.job.attention) == "tools mapping (abort only): T1 is disabled"
    assert seen(p) == ("ATTENTION", 1, "PAUSED")
    with pytest.raises(Busy, match="offers only Abort"):
        p.tap_through()
    p.cancel()
    assert p.job is None and p.state is State.IDLE and p.job_counter == 1


@pytest.mark.parametrize("model", ["3.1.0", "8.10.0"])
def test_a_toolchanger_asks_on_its_mapping_screen_and_loads_nothing(model):
    sim, p = make(model, [loaded(0, PLA), empty(1), loaded(2, PETG)])
    p.start_print(PrintSpec([Segment(0, 10, "PLA"), Segment(1, 10, "PLA")], preheat_s=0))
    sim.tick(0.5)
    assert str(p.job.attention) == ("tools mapping: T1 has no filament; T1 holds ---, not PLA")
    assert seen(p) == ("ATTENTION", 1, "PAUSED")
    p.tap_through()  # Print: nothing loads at the preview on a toolchanger
    assert p.job.waits_for(Reason.SENSORS_OFF) and seen(p) == ("ATTENTION", 1, "PAUSED")
    p.tap_through()
    assert not p.job.previewing and p.job.phase is JobPhase.BLIP


def two_tools(model):
    sim, p = make(model, [loaded(0, PLA), loaded(1, PETG)])
    p.start_print(PrintSpec([Segment(0, 10, "PLA"), Segment(1, 10, "PETG")], preheat_s=0))
    sim.tick(0.5)
    return sim, p


@pytest.mark.parametrize("model", ["3.1.0", "8.10.0"])
def test_a_toolchanger_print_with_nothing_wrong_waits_on_the_mapping_screen(model):
    sim, p = two_tools(model)
    # The ID was taken when Print or Continue was pressed on the preview, before this
    # screen, which reads like every other question.
    assert p.job.waits_for(Reason.TOOLS_MAPPING) and not p.job.attention.fatal
    assert str(p.job.attention) == "tools mapping: nothing wrong"
    assert seen(p) == ("ATTENTION", 1, "PAUSED")
    assert p.actions_json()["tap_through"] is None
    sim.tick(100)  # nothing times out
    assert p.job.waits_for(Reason.TOOLS_MAPPING) and p.tools[0].extruded == 0
    p.tap_through()  # Print
    assert p.job.phase is JobPhase.BLIP and seen(p) == ("IDLE", None, None)
    sim.tick(0.3)
    assert seen(p) == ("PRINTING", 1, "PRINTING")
    sim.tick(100)
    assert p.state is State.FINISHED and p.job_counter == 1
    assert p.tools[0].extruded > 0 and p.tools[1].extruded > 0


@pytest.mark.parametrize("model", ["3.1.0", "8.10.0"])
def test_abort_on_the_mapping_screen_ends_idle_with_the_id_used(model):
    sim, p = two_tools(model)
    p.cancel()  # Abort
    assert p.job is None and seen(p) == ("IDLE", None, None) and p.job_counter == 1
    assert p.tools[0].extruded == 0 and p.tools[0].auto_retracted is None
    p.start_print(PrintSpec([Segment(0, 10)]))
    assert p.job.id == 2


@pytest.mark.parametrize(("enabled", "asks"), [(False, False), (True, True)])
def test_a_one_tool_print_on_an_xl_asks_when_two_tools_are_enabled(enabled, asks):
    sim, p = make("3.1.0", [loaded(0, PLA), loaded(1, PETG, enabled=enabled)])
    p.start_print(PrintSpec([Segment(0, 10, "PLA")], preheat_s=0))
    sim.tick(0.5)
    assert p.job.waits_for(Reason.TOOLS_MAPPING) is asks
    assert (p.job.phase is JobPhase.BLIP) is not asks


@pytest.mark.parametrize("model", ["8.1.0", "1.4.1", "4.1.0", "2.1.0"])
def test_a_single_tool_printer_never_shows_the_mapping_screen(model):
    sim, p = make(model)
    p.start_print(PrintSpec([Segment(0, 10)], preheat_s=0))
    sim.tick(0.5)
    assert p.job.phase is JobPhase.BLIP


def test_the_idle_sensor_clear_runs_on_the_mapping_screen():
    # `is_printing()` is false there, so the picked tool's sensor reading empty clears its
    # type, as it does while idle. On the other questions the printer counts as printing.
    sim, p = two_tools("3.1.0")
    p.set_sensor_fault(1, True)  # not the picked tool
    sim.tick(1)
    assert p.tools[1].unload_count == 0
    p.set_sensor_fault(0, True)
    sim.tick(1)
    assert p.tools[0].unload_count == 1 and p.tools[0].material is None
    sim, p = make()
    p.start_print(PrintSpec([Segment(0, 10, "PETG")]))
    sim.tick(0.5)
    p.set_sensor_fault(0, True)
    sim.tick(1)
    assert p.job.waits_for(Reason.WRONG_FILAMENT) and p.tools[0].unload_count == 0


@pytest.mark.parametrize(
    ("tools", "spec", "taps", "loading"),
    [
        ([empty()], [Segment(0, 10)], 0, False),
        ([empty()], [Segment(0, 10)], 1, False),
        ([empty()], [Segment(0, 10)], 1, True),
        ([loaded(0)], [Segment(0, 10, "PETG")], 0, False),
        ([loaded(0), empty(1)], [Segment(0, 10), Segment(1, 10)], 0, False),
    ],
)
def test_a_snapshot_keeps_a_print_waiting_at_the_preview(tools, spec, taps, loading):
    model = "3.1.0" if len(tools) > 1 else "8.1.0"
    sim, p = make(model, tools)
    p.start_print(PrintSpec(spec))
    sim.tick(0.5)
    for _ in range(taps):
        p.tap_through()
    if loading:
        p.load(0, source="preset", name="PLA")
        sim.tick(1)
    data = json.loads(json.dumps(sim.snapshot()))
    other = Simulation()
    other.restore(data)
    copy = other.printers["p"]
    assert copy.job == p.job and copy.state is p.state
    assert copy.tools == p.tools
    # The copy goes on as the original would.
    other.tick(20)
    sim.tick(20)
    assert copy.job == p.job and copy.state is p.state


@pytest.fixture
async def client():
    sim, printer = make(tools=[empty()])
    c = TestClient(TestServer(prusalink.make_app(sim, "p")))
    await c.start_server()
    yield sim, printer, c
    await c.close()


async def read(c) -> tuple[str, int | None, int, str | None]:
    """The state and job ID from `/api/v1/status`, and the status and state of
    `/api/v1/job`."""
    resp = await c.get("/api/v1/status", headers=AUTH)
    status = await resp.json()
    resp = await c.get("/api/v1/job", headers=AUTH)
    job = await resp.json() if resp.status == 200 else None
    state = status["printer"]["state"]
    return state, status.get("job", {}).get("id"), resp.status, job and job["state"]


async def test_the_mapping_screen_over_http():
    sim, p = make("8.10.0", [loaded(0, PLA), loaded(1, PETG)])
    c = TestClient(TestServer(prusalink.make_app(sim, "p")))
    await c.start_server()
    try:
        p.start_print(PrintSpec([Segment(0, 10), Segment(1, 10)], preheat_s=0))
        assert await read(c) == ("PRINTING", 1, 200, "PRINTING")  # the scan
        sim.tick(0.5)
        assert await read(c) == ("ATTENTION", 1, 200, "PAUSED")  # the tools-mapping screen
        p.tap_through()
        assert await read(c) == ("IDLE", None, 204, None)  # the start blip
        sim.tick(0.3)
        assert await read(c) == ("PRINTING", 1, 200, "PRINTING")
    finally:
        await c.close()


async def test_the_preview_of_a_print_over_http(client):
    sim, p, c = client
    p.start_print(PrintSpec([Segment(0, 10, "PETG")], preheat_s=0))
    sim.tick(0.5)
    assert await read(c) == ("ATTENTION", 1, 200, "PAUSED")  # no filament
    p.tap_through()
    assert await read(c) == ("ATTENTION", 1, 200, "PAUSED")  # waits for a load
    p.load(0, source="preset", name="PLA")
    assert await read(c) == ("BUSY", None, 204, None)  # the preview's load
    sim.tick(10.8)
    assert await read(c) == ("ATTENTION", 1, 200, "PAUSED")  # wrong filament
    p.tap_through()
    assert await read(c) == ("IDLE", None, 204, None)  # the start blip
    sim.tick(0.3)
    assert await read(c) == ("PRINTING", 1, 200, "PRINTING")
