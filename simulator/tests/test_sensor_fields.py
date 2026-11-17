"""The sensor fields of `/api/v1/filament`: `sensors_enabled`, the sensors setting (F47),
and each tool's `filament_present`, what its sensor sees, left out when it can't tell
(F48). `filament_renderer.cpp` at `2ef028181` serves both."""

from __future__ import annotations

import pytest

from simulator import prusalink
from simulator.model import JobPhase, PrintSpec, Segment, SimTool
from simulator.tests.util import PLA, loaded, make, make_mmu, run

XL, INDX = "3.1.0", "8.10.0"


def present(p) -> dict[int, bool | None]:
    """Each listed tool's `filament_present`, None where the field is left out."""
    return {t["index"]: t.get("filament_present") for t in prusalink.filament(p)["tools"]}


def sensors(p) -> bool:
    return prusalink.filament(p)["sensors_enabled"]


def test_sensors_enabled_follows_the_setting_and_off_leaves_every_reading_out():
    sim, p = make(INDX, [loaded(0), SimTool(1)])
    assert sensors(p) is True and present(p) == {0: True, 1: False}
    # Off, every sensor reads Disabled, which the renderer leaves out.
    p.set_sensors(False)
    assert sensors(p) is False and present(p) == {0: None, 1: None}
    p.set_sensors(True)
    assert sensors(p) is True and present(p) == {0: True, 1: False}


def test_the_sensor_fields_are_served_with_no_counters():
    _, p = make("2.1.0")
    p.set_firmware("no_counters")
    data = prusalink.filament(p)
    assert "boot_id" not in data and data["sensors_enabled"] is True
    assert data["tools"][0]["filament_present"] is True


def test_an_idle_loaded_tool_reads_filament_and_one_emptied_by_hand_reads_none():
    sim, p = make()
    assert present(p) == {0: True}
    p.set_power("off")
    p.unload(0)  # pulled out by hand while off
    p.set_power("on")
    # The boot's idle sensor clear counts the unload, and the sensor reads no filament.
    assert present(p) == {0: False} and p.tools[0].unload_count == 1


def test_a_hand_pull_with_the_sensors_off_counts_nothing_and_reads_nothing():
    sim, p = make()
    p.set_sensors(False)
    p.set_power("off")
    p.unload(0)
    p.set_power("on")
    run(sim, 1)
    # No clear runs and no reading shows: only the setting says the swap is hidden (BF25).
    assert p.tools[0].material == PLA and p.tools[0].unload_count == 0
    assert present(p) == {0: None} and sensors(p) is False
    p.set_sensors(True)
    run(sim, 0.1)
    assert present(p) == {0: False} and p.tools[0].unload_count == 1


def test_a_load_reads_filament_before_its_type_is_stored():
    sim, p = make(tools=[SimTool(0)])
    assert present(p) == {0: False}
    p.load(0, source="preset", name="PETG")
    run(sim, 0.5)
    # The filament is at the sensor before the feed starts, so it reads before the type.
    assert p.tools[0].material is None and present(p) == {0: True}
    sim.advance(60)
    assert p.tools[0].material.name == "PETG" and present(p) == {0: True}


def test_a_menu_unload_reads_filament_until_the_user_pulls_it():
    sim, p = make()
    p.unload(0)
    run(sim, 0.5)
    assert p.tools[0].process is not None and present(p) == {0: True}
    sim.advance(60)
    # A menu unload ends without waiting for the sensor to clear (`pause.cpp:1280-1287`): the
    # filament stays in the sensor until the user pulls it, as the F29 trace shows.
    assert p.tools[0].process is None and p.tools[0].material is None
    assert present(p) == {0: True}
    p.pull(0)
    assert present(p) == {0: False} and p.tools[0].unload_count == 1


@pytest.mark.parametrize("model", ["4.1.0", "5.1.0"])
def test_a_menu_unload_without_human_interactions_ends_with_the_sensor_clear(model):
    # With no HAS_HUMAN_INTERACTIONS, an unload waits for the sensor to clear
    # (`pause.cpp:1291-1301, 1371-1391`), so it can't end with the filament in it.
    sim, p = make(model)
    p.unload(0)
    sim.advance(120)
    assert p.tools[0].process is None and p.tools[0].material is None
    assert present(p) == {0: False}
    assert not p.tools[0].path.at_sensor


def test_a_hand_pull_with_the_sensors_on_clears_the_type():
    sim, p = make()
    p.pull(0)
    assert present(p) == {0: False}
    assert p.tools[0].material is None and p.tools[0].unload_count == 1


def test_a_hand_pull_with_the_sensors_off_counts_nothing_until_they_are_on():
    sim, p = make()
    p.set_sensors(False)
    p.pull(0)
    run(sim, 1)
    assert p.tools[0].material is not None and p.tools[0].unload_count == 0
    p.set_sensors(True)
    run(sim, 0.1)
    assert present(p) == {0: False}
    assert p.tools[0].material is None and p.tools[0].unload_count == 1


def test_a_runout_waiting_for_its_reload_reads_no_filament():
    sim, p = make()
    p.start_print(PrintSpec([Segment(0, 500)]))
    run(sim, 30)
    p.runout(0)
    assert p.job.phase is JobPhase.WAIT_LOAD and present(p) == {0: False}
    p.load(0)
    assert present(p) == {0: True}


def test_a_sensor_fault_reads_no_filament():
    sim, p = make()
    p.set_sensor_fault(0, True)
    assert present(p) == {0: False}


@pytest.mark.parametrize("model", [XL, INDX])
def test_a_docked_tool_emptied_by_hand_reads_no_filament_with_its_type_kept(model):
    """Every sensor is evaluated on every loop, docked tools' too, though the idle clear
    reads only the picked tool (BF12)."""
    sim, p = make(model, [loaded(0), loaded(1)])
    p.selected_tool = None
    p.set_power("off")
    p.unload(1)
    p.set_power("on")
    p.selected_tool = None
    run(sim, 1)
    assert p.tools[1].material == PLA and p.tools[1].unload_count == 0
    assert present(p) == {0: True, 1: False}


@pytest.mark.parametrize("model", [XL, INDX])
def test_a_toolchanger_unload_reads_filament_until_pulled_and_a_load_reads_it_again(model):
    """A menu unload leaves the filament at the tool's sensor until the user pulls it (F48)."""
    sim, p = make(model, [loaded(0), loaded(1)])
    p.unload(1)
    sim.advance(60)
    assert p.tools[1].material is None and present(p) == {0: True, 1: True}
    p.pull(1)
    assert present(p) == {0: True, 1: False}
    p.load(1, source="preset", name="PETG")
    assert present(p)[1] is True
    sim.advance(60)
    assert present(p) == {0: True, 1: True}


def test_an_indx_runout_waiting_for_its_reload_reads_no_filament():
    """A runout is the side sensor seeing the filament go."""
    sim, p = make(INDX, [loaded(0), SimTool(1)])
    p.start_print(PrintSpec([Segment(0, 500)]))
    run(sim, 5)
    p.tap_through()  # the tools-mapping screen, with two tools enabled
    run(sim, 60)
    p.runout(0)
    assert p.job.phase is JobPhase.WAIT_LOAD and present(p)[0] is False


def test_mmu_slots_share_the_extruder_sensor_and_only_the_selected_slot_reads_it():
    # Active with a slot selected: that slot reads the extruder's sensor, the others none.
    sim, p = make_mmu(slot=2)
    assert present(p) == {0: None, 1: None, 2: True, 3: None, 4: None}
    # Active with no slot selected: no slot reads it.
    sim, p = make_mmu()
    assert present(p) == dict.fromkeys(range(5))
    # While the MMU connects, slot 0 reads it, whatever is in the extruder.
    sim, p = make_mmu(slot=2, mmu_connect_seconds=5)
    p.reboot()
    assert p.selected_tool is None and present(p) == {0: True, 1: None, 2: None, 3: None, 4: None}
    # Switched off, slot 0 alone is listed, and it reads the sensor.
    sim, p = make_mmu(slot=0)
    p.set_mmu(False)
    assert present(p) == {0: True}
