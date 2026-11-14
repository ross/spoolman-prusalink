"""An MMU3 as a unit a printer has: the switch, the listing, the selected slot and what a
move counts on (F38, F39)."""

from __future__ import annotations

import pytest

from simulator import prusalink
from simulator.feed import FEED_SYSTEMS, MMU, Feed
from simulator.mmu import MMU_SLOTS
from simulator.model import MODELS, Busy, Invalid, Simulation, State
from simulator.tests.test_model import PETG, one_segment, run_sync
from simulator.tests.test_model_edges import messages
from simulator.tests.util import PLA, make, make_mmu, run, sliced
from simulator.tool import retracted_distance

CORE_ONE = "7.1.0"


def listed(p) -> list[int]:
    return [t["index"] for t in prusalink.filament(p)["tools"]]


def test_only_models_with_an_mmu3_profile_take_one():
    takes = {code for code, m in MODELS.items() if m.takes_mmu}
    assert takes == {"1.3.9", "1.3.10", "1.4.0", "1.4.1", "7.1.0", "8.1.0"}
    # A model that takes one has a build with HAS_MMU2.
    assert all(MODELS[code].mmu for code in takes)


@pytest.mark.parametrize(("code", "tools"), [("1.4.1", 5), ("1.3.5", 5), ("4.1.0", 1)])
def test_m865_takes_every_slot_on_an_mmu_build(code, tools):
    assert MODELS[code].m865_tools == tools


def test_every_slot_shares_the_units_one_path():
    sim, p = make_mmu(slot=2)
    assert sorted(p.tools) == list(range(MMU_SLOTS))
    assert all(t.path is p.mmu.path for t in p.tools.values())
    assert p.feed is MMU and p.selected_tool == 2 and p.mmu.fed == 2
    assert p.mmu.path.loaded and p.mmu.active


def test_turning_the_mmu_off_with_an_empty_extruder_clears_slot_0_only():
    sim, p = make_mmu(types={0: PLA, 2: PETG, 4: PLA})
    p.set_mmu(False)
    assert p.tools[0].material is None and p.tools[0].unload_count == 1
    assert p.tools[2].material.name == "PETG" and p.tools[2].unload_count == 0
    assert p.tools[4].material.name == "PLA"
    assert not p.mmu.on and p.feed is FEED_SYSTEMS[Feed.DIRECT] and p.selected_tool == 0
    assert listed(p) == [0]
    assert prusalink.info(p)["mmu"] is False
    assert messages(sim, "unload") == ["T0 MMU off: unload_count 1 at 0"]


def test_turning_the_mmu_off_with_filament_in_the_extruder_keeps_slot_0():
    # Slot 2's filament is in the nozzle, and slot 0 keeps its type: inferred to charge
    # slot 0 for slot 2's filament from here (BF28).
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2)
    p.set_mmu(False)
    assert p.tools[0].material.name == "PLA" and p.tools[0].unload_count == 0
    p.extrude(0, 10)
    assert p.tools[0].extruded == pytest.approx(10 * p.feed_ratio)
    assert p.tools[2].extruded == 0


def test_a_faulty_extruder_sensor_reads_empty_at_the_switch_off():
    sim, p = make_mmu(slot=0)
    p.set_sensor_fault(0, True)
    p.set_mmu(False)
    assert p.tools[0].unload_count == 1


def test_an_mmu_switched_off_lists_slot_0_and_takes_writes_to_every_slot():
    sim, p = make_mmu()
    p.set_mmu(False)
    run_sync(sim, p, sliced(p, 'M865 N"PETG" L4\n'))
    assert p.tools[4].material.name == "PETG"
    assert listed(p) == [0]
    with pytest.raises(Invalid, match="T4 is an MMU slot, and the MMU is off"):
        p.set_sensor_fault(4, True)
    sim.advance(5)
    p.set_mmu(True)
    assert listed(p) == [0, 1, 2, 3, 4]
    assert prusalink.filament(p)["tools"][4]["material"]["name"] == "PETG"


def test_turning_the_mmu_on_starts_it_connecting_with_no_slot():
    sim, p = make_mmu(slot=1, mmu_connect_seconds=4)
    p.set_mmu(False)
    p.set_mmu(True)
    assert p.selected_tool is None and p.mmu.connecting == 4 and not p.mmu.active
    # Listed, and reported on, by the setting while it connects.
    assert listed(p) == [0, 1, 2, 3, 4] and prusalink.info(p)["mmu"] is True
    run(sim, 3.9)
    assert not p.mmu.active
    run(sim, 0.2)
    assert p.mmu.active and p.mmu.connecting == 0
    assert messages(sim, "mmu") == ["MMU connecting for 4 s", "MMU active"]
    # The filament stays where it was.
    assert p.mmu.fed == 1 and p.mmu.path.loaded


def test_an_mmu_with_no_connecting_window_is_active_at_once():
    sim, p = make_mmu(mmu_connect_seconds=0)
    p.set_mmu(False)
    p.set_mmu(True)
    assert p.mmu.active
    assert messages(sim, "mmu") == ["MMU connecting for 0 s", "MMU active"]


def test_a_boot_leaves_no_slot_selected_and_connects():
    sim, p = make_mmu(types={0: PLA, 3: PETG}, slot=3, mmu_connect_seconds=2)
    p.reboot()
    assert p.selected_tool is None and not p.mmu.active
    assert p.mmu.fed == 3 and p.mmu.path.loaded  # nothing reads the MMU's slot back
    assert p.tools[0].material.name == "PLA" and p.tools[3].material.name == "PETG"
    assert all(t.unload_count == 0 for t in p.tools.values())  # no idle clear


def test_a_boot_with_the_mmu_off_leaves_slot_0_picked():
    sim, p = make_mmu(slot=0)
    p.set_mmu(False)
    p.reboot()
    assert p.selected_tool == 0 and p.mmu.connecting == 0


def test_moves_count_on_slot_0_while_the_mmu_connects():
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2, mmu_connect_seconds=2)
    p.reboot()
    p.extrude(2, 10)  # slot 2's filament moves, slot 0 is charged (BF17)
    assert p.tools[0].extruded == pytest.approx(10 * p.feed_ratio)
    assert p.tools[2].extruded == 0
    assert prusalink.status(p)["printer"]["flow"] == 100


def test_moves_with_no_slot_selected_count_nothing_and_freeze_the_gap():
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2, mmu_connect_seconds=0)
    p.retract(2, 15)
    assert p.mmu.path.gap == 15
    p.reboot()
    assert p.mmu.active and p.selected_tool is None
    p.retract(2, 10)
    p.extrude(2, 50)
    assert all(t.extruded == 0 for t in p.tools.values())
    # The tracker saw none of it: the gap is what the boot restored.
    assert p.mmu.path.gap == 0 and not p.mmu.path.gap_valid
    assert prusalink.status(p)["printer"]["flow"] == 0


def test_moves_count_on_the_selected_slot():
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2)
    p.retract(2, 5)
    p.extrude(2, 15)
    assert p.tools[2].extruded == pytest.approx(10 * p.feed_ratio)
    assert p.tools[0].extruded == 0
    assert prusalink.status(p)["printer"]["flow"] == 100


def test_a_sync_job_on_an_active_mmu_skips_the_auto_retract():
    # The job's end retracts 8 mm on the selected slot and stores it, and skips the
    # auto-retract while the MMU is Active (`pre_finalize_print`).
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2)
    run_sync(sim, p, sliced(p, ""))
    sim.advance(5)
    assert p.job is None and p.state is State.FINISHED
    assert p.mmu.path.auto_retracted == 8 and p.mmu.path.gap == 8
    assert all(t.extruded == 0 for t in p.tools.values())


def test_a_sync_job_while_the_mmu_connects_auto_retracts_on_slot_0():
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2, mmu_connect_seconds=60)
    p.reboot()
    run_sync(sim, p, sliced(p, ""))
    sim.advance(5)
    assert p.job is None and not p.mmu.active
    # The ramming's feed counts on slot 0, and the sequence's distance is stored.
    assert p.tools[0].extruded > 0 and p.tools[2].extruded == 0
    ramming = p.model.extruder.ramming["auto_retract"]
    assert p.mmu.path.auto_retracted == retracted_distance(ramming)


def test_a_sync_job_with_nothing_selected_moves_nothing_counted():
    sim, p = make_mmu()
    run_sync(sim, p, sliced(p, 'M865 N"PETG" L3\n'))
    sim.advance(5)
    assert p.tools[3].material.name == "PETG"
    assert all(t.extruded == 0 for t in p.tools.values())
    assert p.mmu.path.gap == 0


def test_a_snapshot_keeps_one_shared_path():
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2, mmu_connect_seconds=3)
    p.retract(2, 5)
    p.reboot()
    saved = sim.snapshot()
    assert "gap" not in saved["printers"][0]["tools"][0]
    other = Simulation()
    other.restore(saved)
    q = other.printers["p"]
    assert all(t.path is q.mmu.path for t in q.tools.values())
    assert q.mmu == p.mmu and q.selected_tool is None
    q.mmu.path.gap = 7
    assert q.tools[4].path.gap == 7


def test_the_page_shows_the_mmu_and_which_slot_is_in_the_extruder():
    sim, p = make_mmu(slot=2, mmu_connect_seconds=3)
    view = p.to_json()
    assert view["mmu"] == {"on": True, "active": True, "connecting": 0.0, "fed": 2}
    assert [t["loaded"] for t in view["tools"]] == [False, False, True, False, False]
    p.set_mmu(False)
    view = p.to_json()
    assert [t["listed"] for t in view["tools"]] == [True, False, False, False, False]
    assert view["tools"][3]["actions"]["load"] == "T3 is an MMU slot, and the MMU is off"
    assert make()[1].to_json()["mmu"] is None


def test_with_the_mmu_off_the_page_shows_tool_0_loaded_from_its_path():
    # Off, the printer feeds slot 0 as a single-tool one, so the fed slot doesn't decide.
    sim, p = make_mmu(types={})
    p.set_mmu(False)
    p.load(0, source="preset", name="PLA")
    sim.advance(60)
    assert p.mmu.path.loaded and p.mmu.fed is None
    tool = p.to_json()["tools"][0]
    assert tool["loaded"] is True and tool["actions"]["unload"] is None

    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2)
    p.set_mmu(False)
    assert [t["loaded"] for t in p.to_json()["tools"]] == [True, False, False, False, False]


def test_the_tag_reader_is_off_while_the_mmu_is_on():
    sim, p = make_mmu(CORE_ONE)
    assert p.to_json()["load_sources"] == ["preset"]
    with pytest.raises(Invalid, match="the tag reader is off while the MMU is on"):
        p.load(0, source="tag", name="PLA")
    p.set_mmu(False)
    assert p.to_json()["load_sources"] == ["tag", "preset"]


def test_turning_the_sensors_off_turns_the_mmu_off():
    sim, p = make_mmu()
    p.set_sensors(False)
    assert not p.sensors and not p.mmu.on and p.tools[0].unload_count == 1
    with pytest.raises(Busy, match="the MMU needs the filament sensors on"):
        p.set_mmu(True)


def test_the_mmu_switches_only_on_an_idle_printer():
    sim, p = make_mmu()
    run_sync(sim, p, sliced(p, ""))
    with pytest.raises(Busy, match="can't switch the MMU while PRINTING"):
        p.set_mmu(False)
    with pytest.raises(Busy, match="can't switch the MMU while PRINTING"):
        p.set_sensors(False)
    assert p.sensors and p.mmu.on
    p.set_power("off")
    with pytest.raises(Busy, match="p is off"):
        p.set_mmu(False)
    with pytest.raises(Invalid, match="p has no MMU"):
        make()[1].set_mmu(True)


def test_mmu_guards():
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2)
    with pytest.raises(Invalid, match="T0's filament isn't in the extruder"):
        p.extrude(0, 1)
    with pytest.raises(Invalid, match="an MMU selects a slot only by loading it"):
        p.select_tool(1)
    with pytest.raises(Invalid, match="an MMU slot can't be disabled"):
        p.set_tool_enabled(3, False)
    with pytest.raises(Invalid, match="the MMU's filament menu has no plain load"):
        p.load(1, source="preset", name="PLA")
    # An upload still runs.
    run_sync(sim, p, sliced(p, ""))
    assert p.job is not None


def test_an_mmu_switched_off_prints_as_a_single_tool_printer():
    sim, p = make_mmu(slot=0)
    p.set_mmu(False)
    p.start_print(one_segment(10))
    assert p.job is not None


def test_a_plain_mmu_build_stores_slot_writes_but_lists_tool_0_alone():
    # An MK4 with no MMU attached: `M865 I2` lands in slot 2's config, but the printer has
    # slot 0 alone while the MMU setting is off, so slot 2 can't be enabled or listed.
    sim, p = make("1.4.0")
    run_sync(sim, p, sliced(p, 'M865 I2 N"PETG"\n'))
    assert p.tools[2].material.name == "PETG" and not p.tools[2].enabled
    reason = "T2 is an MMU slot, and the MMU is off"
    assert p.to_json()["tools"][1]["actions"]["set_tool_enabled"] == reason
    with pytest.raises(Invalid, match=reason):
        p.set_tool_enabled(2, True)
    assert listed(p) == [0]


def test_switching_an_mmu_off_again_with_the_sensors_off_clears_slot_0():
    # `M709 S0` runs `mmu_off` with the MMU already off, and the switched-off sensors,
    # no longer forced on by the MMU, never surely see filament.
    sim, p = make_mmu(types={0: PETG, 2: PLA}, slot=2)
    p.set_mmu(False)
    p.set_sensors(False)
    assert p.tools[0].material.name == "PETG" and p.tools[0].unload_count == 0
    p.set_mmu(False)
    assert p.tools[0].material is None and p.tools[0].unload_count == 1
    assert p.to_json()["actions"]["set_mmu"] == "the MMU needs the filament sensors on"


def test_turning_the_sensors_off_with_filament_in_the_extruder_keeps_slot_0():
    # The MMU forced the sensors on, so they still read when `mmu_off` looks.
    sim, p = make_mmu(types={0: PETG, 2: PLA}, slot=2)
    p.set_sensors(False)
    assert p.tools[0].material.name == "PETG" and p.tools[0].unload_count == 0
