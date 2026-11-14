"""The print preview with an MMU3 (F43): the MMU3 feature check, the filament check left out,
and the question to unload the MMU's filament first.

The question asks when the MMU is Active and FINDA sees filament, a slot's filament past the
selector (`mmu.fed`), and always while the MMU connects, when FINDA's sensor reads not
initialized. A single-tool file, one whose metadata names tool 0 alone, skips it when the
extruder's sensor sees filament and slot 0 holds the file's type. No (`tap_through`) prints
with the filament where it is; Yes (`unload_first`) unloads it into the MMU, reading BUSY
with the job hidden, then goes on to the tools-mapping screen or the print. What Yes unloads
while the MMU connects isn't read, so it's refused then.
"""

from __future__ import annotations

import json

import pytest

from simulator import prusalink
from simulator.model import (
    Busy,
    JobPhase,
    Power,
    PrintSpec,
    Reason,
    Segment,
    Simulation,
    State,
)
from simulator.preview import Severity
from simulator.tests.test_model import PETG, run_sync
from simulator.tests.test_model_edges import messages
from simulator.tests.util import PLA, make_mmu, sliced

RATIO = 0.95
MMU3_FILE = 'M862.6 P"MMU3"\n'


def loaded(**settings):
    """An MK4S with its MMU Active and slot 2 loaded to the nozzle and selected, as Load To
    Nozzle leaves it but with nothing auto-retracted: slots 0 and 2 typed."""
    return make_mmu(types={0: PLA, 2: PETG}, slot=2, **settings)


def booted():
    """`loaded()` after a boot: slot 2's filament still in the extruder, FINDA sees it, and
    no slot is selected."""
    sim, p = loaded(mmu_connect_seconds=0)
    p.reboot()
    assert p.mmu.active and p.mmu.fed == 2 and p.selected_tool is None
    return sim, p


def sync(sim, p, body: str = "") -> None:
    """Upload a sync job and tick past its scan."""
    run_sync(sim, p, sliced(p, body))


def unload(sim, p) -> None:
    """Answer Yes and tick until the unload into the MMU ends."""
    p.unload_first()
    while any(t.process for t in p.tools.values()):
        sim.tick(0.1)


def counts(p) -> dict[int, float]:
    return {i: t.extruded / RATIO for i, t in p.tools.items()}


def view(p) -> tuple[State, int | None]:
    """The state and the job ID `/api/v1/status` shows."""
    status = prusalink.status(p)
    return State(status["printer"]["state"]), status.get("job", {}).get("id")


def asked(p) -> bool:
    return p.job is not None and p.job.waits_for(Reason.MMU_LOADED)


def test_a_sync_job_on_a_loaded_mmu_waits_on_the_question():
    sim, p = loaded()
    sync(sim, p)
    assert asked(p) and p.job.attention.tool == 2
    assert view(p) == (State.ATTENTION, 1)
    assert prusalink.job(p)["state"] == "PAUSED"
    assert str(p.job.attention) == "filament from slot 2 in the MMU: unload it first?"
    # It waits until it's answered.
    sim.advance(60)
    assert asked(p) and p.mmu.fed == 2 and all(t.extruded == 0 for t in p.tools.values())


def test_no_finda_filament_no_question():
    sim, p = make_mmu()
    sync(sim, p)
    assert p.job.phase is JobPhase.RUN


def test_the_extruder_s_sensor_alone_asks_nothing():
    # Filament pushed in by hand while the MMU was off came through no slot: FINDA sees
    # none, so the question keys on it, not on the extruder's sensor.
    sim, p = make_mmu(mmu_connect_seconds=0)
    p.set_mmu(False)
    p.set_power(Power.OFF)
    p.load(0, source="preset", name="PLA")
    p.set_power(Power.ON)
    p.set_mmu(True)
    assert p.mmu.active and p.mmu.path.loaded and p.mmu.fed is None
    sync(sim, p)
    assert p.job.phase is JobPhase.RUN


def connecting(fed: bool):
    """`loaded()` after a boot whose MMU connects for 60 s; `fed` leaves slot 2's filament
    in, else the extruder is empty."""
    sim, p = loaded(mmu_connect_seconds=60) if fed else make_mmu(mmu_connect_seconds=60)
    p.reboot()
    assert p.mmu.on and not p.mmu.active and p.mmu.fed == (2 if fed else None)
    return sim, p


@pytest.mark.parametrize(
    ("fed", "text"),
    [
        (True, "filament from slot 2 in the MMU: unload it first?"),
        (False, "filament in the MMU: unload it first?"),
    ],
)
def test_the_question_asks_while_the_mmu_connects_whatever_finda_sees(fed, text):
    sim, p = connecting(fed)
    sync(sim, p)
    assert asked(p) and str(p.job.attention) == text
    assert view(p) == (State.ATTENTION, 1)
    with pytest.raises(Busy, match="what Yes unloads while the MMU connects isn't read"):
        p.unload_first()
    assert asked(p)


def test_yes_once_connected_unloads_what_finda_sees():
    sim, p = connecting(True)
    sync(sim, p)
    sim.advance(60)
    assert p.mmu.active and asked(p)
    unload(sim, p)
    assert p.mmu.fed is None and not p.mmu.path.loaded and p.job.phase is JobPhase.BLIP


def test_yes_once_connected_with_finda_clear_is_refused():
    sim, p = connecting(False)
    sync(sim, p)
    sim.advance(60)
    assert p.mmu.active and asked(p)
    with pytest.raises(Busy, match="FINDA sees no filament"):
        p.unload_first()
    p.tap_through()
    assert p.job.phase is JobPhase.BLIP


def test_no_question_with_the_mmu_off():
    # Turning the filament sensors off turns the MMU off too, so this is also the sensors.
    sim, p = loaded()
    p.set_sensors(False)
    assert not p.mmu.on
    sync(sim, p)
    assert p.job.phase is JobPhase.RUN


def test_no_runs_the_sync_job_with_the_filament_in_place():
    sim, p = loaded()
    sync(sim, p)
    p.tap_through()
    assert view(p) == (State.IDLE, None)  # the start blip
    sim.tick(0.5)
    assert p.job.phase is JobPhase.RUN and view(p) == (State.PRINTING, 1)
    sim.advance(5)
    assert p.state is State.FINISHED and p.mmu.fed == 2 and p.mmu.path.loaded


def test_yes_unloads_reading_busy_then_runs_the_sync_job():
    sim, p = loaded()
    sync(sim, p)
    p.unload_first()
    assert p.job.phase is JobPhase.PREVIEW_UNLOAD
    assert view(p) == (State.BUSY, None) and prusalink.job(p) is None
    assert p.tools[2].process is not None
    with pytest.raises(Busy, match="the preview's unload is running"):
        p.cancel()
    while p.tools[2].process:
        sim.tick(0.1)
    # The MMU's ramming, +20 -50 -5 -50, counts 20 on the selected slot from gap 0, and the
    # MMU unloads: no slot selected, nothing in the extruder, no type written.
    assert counts(p) == pytest.approx({0: 0, 1: 0, 2: 20, 3: 0, 4: 0})
    assert p.selected_tool is None and p.mmu.fed is None and not p.mmu.path.loaded
    assert p.tools[2].unload_count == 0 and p.tools[2].material == PETG
    assert p.job.phase is JobPhase.BLIP
    sim.tick(0.5)
    assert p.job.phase is JobPhase.RUN and view(p) == (State.PRINTING, 1)
    sim.advance(5)
    assert p.state is State.FINISHED and p.job_counter == 1


def test_yes_after_a_boot_counts_nothing():
    # No slot is selected, so the unload's moves count on none.
    sim, p = booted()
    sync(sim, p)
    unload(sim, p)
    assert all(t.extruded == 0 for t in p.tools.values())
    assert p.mmu.fed is None and not p.mmu.path.loaded and p.job.phase is JobPhase.BLIP


def test_yes_on_filament_fully_auto_retracted_pulls_it_out_cold():
    # Load To Nozzle auto-retracts, so the unload is the 120 mm relieve, which counts
    # nothing.
    sim, p = make_mmu(types={2: PLA})
    p.load_to_nozzle(2)
    sim.advance(60)
    before = counts(p)[2]
    sync(sim, p)
    unload(sim, p)
    assert counts(p)[2] == before and not p.mmu.path.loaded


def test_cancel_at_the_question_ends_idle_with_the_filament_in_place():
    sim, p = loaded()
    sync(sim, p)
    p.cancel()
    assert p.job is None and p.state is State.IDLE and p.job_counter == 1
    assert p.mmu.fed == 2 and p.mmu.path.loaded and p.selected_tool == 2
    assert all(t.extruded == 0 for t in p.tools.values())


def test_tap_through_on_a_failed_check_then_asks_to_unload():
    # The file's checks come first, then the filament.
    sim, p = loaded()
    p.upload("x.gcode", 'M862.3 P "MINI"\n', print_after=True, overwrite=False)
    sim.tick(0.6)
    assert p.job.waits_for(Reason.PREVIEW)
    p.tap_through()
    assert asked(p)


def test_a_power_cut_during_the_unload_drops_the_job():
    sim, p = loaded()
    sync(sim, p)
    p.unload_first()
    sim.tick(1)
    assert p.tools[2].extruded > 0
    p.set_power(Power.OFF)
    p.set_power(Power.ON, resume=True)
    # Nothing resumes from the preview, and the MMU's unload never finished: the filament
    # is where the cut left it, FINDA still sees it, and no slot is selected after the boot.
    assert p.job is None and p.resuming is None and p.state is State.IDLE
    assert p.mmu.fed == 2 and p.mmu.path.loaded and p.selected_tool is None
    assert p.job_counter == 1 and p.tools[2].process is None


def test_a_snapshot_keeps_the_preview_s_unload():
    sim, p = loaded()
    sync(sim, p)
    p.unload_first()
    sim.tick(1)
    other = Simulation()
    other.restore(json.loads(json.dumps(sim.snapshot())))
    copy = other.printers["p"]
    assert copy.job == p.job and copy.job.phase is JobPhase.PREVIEW_UNLOAD
    assert copy.tools == p.tools and copy.state is State.BUSY and not copy.job_visible()
    other.tick(2.5)
    sim.tick(2.5)
    assert copy.job == p.job and copy.job.phase is JobPhase.RUN
    assert copy.tools == p.tools and copy.mmu == p.mmu


# A user print.


def spec(*segments: tuple, **kw) -> PrintSpec:
    return PrintSpec([Segment(*s) for s in segments], preheat_s=0, duration_s=2, **kw)


def single(material: str | None) -> PrintSpec:
    """A single-tool file: sliced without the MMU3 profile, on tool 0."""
    return spec((0, 10, material), single_tool_file=True)


TWO = ((0, 10), (2, 10))


def test_a_print_on_a_loaded_mmu_asks_before_the_mapping_screen():
    sim, p = loaded()
    p.start_print(spec(*TWO))
    sim.tick(0.5)
    assert asked(p) and view(p) == (State.ATTENTION, 1)


def test_no_prints_at_once_with_no_mapping_screen():
    sim, p = loaded()
    p.start_print(spec(*TWO))
    sim.tick(0.5)
    p.tap_through()
    sim.tick(0.4)
    assert p.job.phase is JobPhase.PRINTING and p.job.mapping == []
    # The start's `T0` changes from slot 2, which is selected.
    assert "tool change T2 -> T0" in messages(sim, "job")


def test_yes_unloads_then_shows_the_mapping_screen():
    sim, p = loaded()
    p.start_print(spec(*TWO))
    sim.tick(0.5)
    p.unload_first()
    assert view(p) == (State.BUSY, None)
    sim.tick(3)
    # The used slots are empty now, and with the MMU Active the preview doesn't check for
    # filament: the screen has nothing wrong, and its Print asks nothing more.
    assert p.job.waits_for(Reason.TOOLS_MAPPING) and p.job.attention.detail is None
    assert len(p.job.mapping) == 1 and view(p) == (State.ATTENTION, 1)
    p.tap_through()
    sim.tick(0.4)
    assert p.job.phase is JobPhase.PRINTING
    assert "tool change none -> T0" in messages(sim, "job")


@pytest.mark.parametrize("material", ["PLA", None])
def test_the_shortcut_prints_a_single_tool_file_of_the_loaded_type_at_once(material):
    # The file names slot 0's type, or none, and the extruder's sensor sees filament: no
    # question and no mapping screen. Slot 0's type decides, though slot 2's is loaded.
    sim, p = loaded()
    p.start_print(single(material))
    sim.tick(0.5)
    assert p.job.attention is None and p.job.phase is JobPhase.BLIP
    assert "job 1: T0's type is loaded, so the print starts at once" in messages(sim, "job")


@pytest.mark.parametrize(
    "print_spec",
    [
        single("PETG"),  # another type than slot 0's
        # A one-color file from the MMU3 profile lists five tools, so it isn't single-tool.
        spec((0, 10, "PLA")),
        spec((2, 10, "PETG")),
        spec(*TWO),
    ],
)
def test_the_shortcut_needs_a_single_tool_file_of_slot_0_s_type(print_spec):
    sim, p = loaded()
    p.start_print(print_spec)
    sim.tick(0.5)
    assert asked(p)


def test_the_shortcut_reads_the_extruder_s_sensor_not_finda():
    # FINDA sees slot 2's filament, but it hasn't reached the extruder's sensor.
    sim, p = make_mmu(types={0: PLA})
    p.mmu.fed = 2
    p.start_print(single("PLA"))
    sim.tick(0.5)
    assert asked(p)


def test_an_mmu_that_isn_t_active_checks_filament_as_a_single_tool_printer():
    # With the MMU off, slot 0 is the printer's one tool, and the preview checks it.
    sim, p = make_mmu()
    p.set_mmu(False)
    p.start_print(spec((0, 10)))
    sim.tick(0.5)
    assert p.job.waits_for(Reason.PREVIEW) and p.job.attention.detail == "T0 has no filament"


# The MMU3 feature check on an uploaded file.


@pytest.mark.parametrize("severity", list(Severity))
@pytest.mark.parametrize("mmu", ["active", "connecting", "off"])
def test_a_file_sliced_for_an_mmu3_needs_it_active(mmu, severity):
    sim, p = make_mmu(mmu_connect_seconds=60)
    if mmu == "active":
        p.mmu.connecting = 0.0
    elif mmu == "off":
        p.set_mmu(False)
    else:
        p.reboot()
    p.set_checks(severity)
    sync(sim, p, MMU3_FILE)
    if mmu == "active" or (mmu == "off" and severity is Severity.IGNORE):
        assert p.job.phase is JobPhase.RUN
        return
    if severity is Severity.IGNORE:
        # The check passes, and the MMU's question asks while it connects.
        assert asked(p)
        return
    assert p.job.waits_for(Reason.PREVIEW)
    assert p.job.attention.detail == "sliced for an MMU3, which isn't active"
    assert p.job.attention.fatal is (severity is Severity.ABORT)
