"""The MMU3's own operations while idle: the MMU menu's Load To Nozzle, Unload Filament,
Eject, Cut, Preload To MMU and Preload All (F41).

The counts follow the tracker's rules on an MK4S: a move counts what passes the gap, which
stops at 40 mm, times 0.95. With no slot selected nothing counts and the gap stays where it
was. An MMU unload from gap 0 rams +20, which counts 20, then -105, which leaves the gap
at 40. A load to the nozzle is 17 + 30 = 47 mm: 47 count from gap 0, 7 from 40.
"""

from __future__ import annotations

import json

import pytest

from simulator.mmu import MMU_RAMMING, RELIEVE_MM, change_feed, nozzle_load
from simulator.model import Busy, Invalid, Simulation, State
from simulator.tests.test_model import PETG
from simulator.tests.test_model_edges import messages
from simulator.tests.util import PLA, loaded, make, make_mmu
from simulator.tool import LoadProcess, MmuProcess, UnloadProcess

RATIO = 0.95


def check(p) -> None:
    """The MMU's invariant: filament in the extruder is a fed slot's."""
    assert not p.mmu.path.loaded or p.mmu.fed is not None


def run_checked(sim, p, seconds: float, step: float = 0.1) -> None:
    """Tick in small steps, checking the invariant after each."""
    t = 0.0
    while t < seconds - 1e-9:
        sim.tick(min(step, seconds - t))
        check(p)
        t += step


def until_idle(sim, p, limit: float = 120.0) -> None:
    t = 0.0
    while p.state is State.BUSY:
        assert t < limit, "still BUSY"
        run_checked(sim, p, 0.5)
        t += 0.5


def no_auto_retract(**kw):
    sim, p = make_mmu(**kw)
    p.auto_retract = False
    return sim, p


def test_the_change_feed_and_the_load_are_the_firmwares_lengths():
    assert change_feed() == (189.9, 7.0, 50.0)
    assert sum(change_feed()) == pytest.approx(246.9)
    assert nozzle_load() == (17.0, 30.0)
    assert sum(e for e, _ in MMU_RAMMING) == -85


def test_load_to_nozzle_after_a_boot_counts_the_whole_load():
    # A fresh boot with nothing stored starts the gap at 0: the 47 mm load counts in full.
    sim, p = no_auto_retract(types={0: PLA, 2: PETG}, mmu_connect_seconds=1)
    p.reboot()
    run_checked(sim, p, 1.5)
    assert p.mmu.active and p.mmu.path.gap == 0
    p.load_to_nozzle(2)
    assert p.state is State.BUSY and isinstance(p.tools[2].process, LoadProcess)
    phases = [s.phase for s in p.tools[2].process.steps]
    assert phases == ["FEED", "FEED", "FEED", "SELECT", "LOAD", "LOAD", "TYPE", "AUTO_RETRACT"]
    until_idle(sim, p)
    assert p.selected_tool == 2 and p.mmu.fed == 2 and p.mmu.path.loaded
    assert p.tools[2].extruded == pytest.approx(47 * RATIO)
    assert all(p.tools[i].extruded == 0 for i in (0, 1, 3, 4))
    # The slot's own type is written back: nothing counts, the type is unchanged.
    assert p.tools[2].material.name == "PETG" and p.tools[2].unload_count == 0
    assert "T2 type set to PETG #186bb5" in messages(sim, "load")


def test_the_feed_with_no_slot_selected_counts_nothing_and_freezes_the_gap():
    sim, p = no_auto_retract(types={1: PLA}, mmu_connect_seconds=0)
    p.mmu.path.gap, p.mmu.path.gap_valid = 12.0, True
    p.load_to_nozzle(1)
    # 24.69 s into the 246.9 mm feed at 10 mm/s: still no slot selected.
    run_checked(sim, p, 24.6)
    assert p.selected_tool is None and p.mmu.path.gap == 12
    assert p.tools[1].extruded == 0
    until_idle(sim, p)
    # The load fills the 12 mm gap first: 35 of its 47 count.
    assert p.tools[1].extruded == pytest.approx(35 * RATIO)


def test_load_to_nozzle_unloads_the_selected_slot_first():
    # Slot 0's ramming counts 20 on slot 0 and leaves the gap at 40. The feed counts
    # nothing, so slot 2's load fills the 40 first and counts 7 (BF29).
    sim, p = no_auto_retract(types={0: PLA, 2: PETG}, slot=0)
    p.load_to_nozzle(2)
    phases = [s.phase for s in p.tools[2].process.steps]
    assert phases[:5] == ["RAM", "RAM", "RAM", "RAM", "DESELECT"]
    until_idle(sim, p)
    assert p.tools[0].extruded == pytest.approx(20 * RATIO)
    assert p.tools[2].extruded == pytest.approx(7 * RATIO)
    assert p.selected_tool == 2 and p.mmu.fed == 2
    assert all(t.unload_count == 0 for t in p.tools.values())
    assert p.tools[0].material.name == "PLA" and p.tools[2].material.name == "PETG"


def test_load_to_nozzle_of_the_selected_slot_unloads_it_too():
    sim, p = no_auto_retract(slot=0)
    p.load_to_nozzle(0)
    until_idle(sim, p)
    assert p.tools[0].extruded == pytest.approx((20 + 7) * RATIO)
    assert p.selected_tool == 0 and p.mmu.fed == 0


def test_load_to_nozzle_auto_retracts_on_the_slot():
    # The auto-retract follows the load as on any idle load: from gap 0 its ramming counts
    # 23 mm and stores 55 (F26).
    sim, p = make_mmu()
    p.load_to_nozzle(0)
    until_idle(sim, p)
    assert p.tools[0].extruded == pytest.approx((47 + 23) * RATIO)
    assert p.mmu.path.auto_retracted == 55


def test_an_unload_when_fully_auto_retracted_relieves_with_no_feed():
    sim, p = make_mmu()
    p.load_to_nozzle(0)
    until_idle(sim, p)
    before = p.tools[0].extruded
    p.unload(0)
    process = p.tools[0].process
    assert isinstance(process, UnloadProcess) and p.state is State.BUSY
    assert [(s.phase, s.mm) for s in process.steps] == [("UNLOAD", RELIEVE_MM), ("DESELECT", 0)]
    until_idle(sim, p)
    assert p.tools[0].extruded == before
    assert p.selected_tool is None and p.mmu.fed is None and not p.mmu.path.loaded
    assert p.mmu.path.auto_retracted is None and p.mmu.path.gap == 40
    # No type is written, so nothing counts.
    assert p.tools[0].material.name == "PLA" and p.tools[0].unload_count == 0
    assert "T0 unloaded into the MMU: no type written" in messages(sim, "load")


def test_an_unload_that_isnt_auto_retracted_rams():
    sim, p = make_mmu(slot=0)
    p.unload(0)
    steps = [(s.phase, s.mm) for s in p.tools[0].process.steps]
    assert steps == [*(("RAM", e) for e, _ in MMU_RAMMING), ("DESELECT", 0)]
    until_idle(sim, p)
    assert p.tools[0].extruded == pytest.approx(20 * RATIO)
    assert p.mmu.path.gap == 40 and not p.mmu.path.loaded
    assert p.tools[0].unload_count == 0


def test_an_unload_after_a_boot_counts_nothing():
    # The boot leaves no slot selected, so slot 2's ramming counts on none.
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2, mmu_connect_seconds=1)
    p.reboot()
    run_checked(sim, p, 1.5)
    p.unload(2)
    until_idle(sim, p)
    assert all(t.extruded == 0 for t in p.tools.values())
    assert p.mmu.fed is None and not p.mmu.path.loaded and p.mmu.path.gap == 0


def test_a_load_to_nozzle_after_an_unload_counts_7():
    sim, p = no_auto_retract(slot=0)
    p.unload(0)
    until_idle(sim, p)
    p.load_to_nozzle(0)
    until_idle(sim, p)
    # 20 from the unload's ramming, then 7 of the load.
    assert p.tools[0].extruded == pytest.approx(27 * RATIO)


def test_eject_and_cut_take_time_and_change_nothing():
    sim, p = make_mmu(types={0: PLA, 3: PETG})
    for op in ("eject", "cut"):
        getattr(p, op)(3)
        process = p.tools[3].process
        assert isinstance(process, MmuProcess) and process.op == op
        assert p.state is State.BUSY
        assert p.to_json()["tools"][3]["process"] == {
            "kind": "mmu",
            "phase": op.upper(),
            "remaining_s": 3.0,
        }
        until_idle(sim, p)
        assert p.state is State.IDLE
    assert p.tools[3].material.name == "PETG" and p.tools[3].unload_count == 0
    assert all(t.extruded == 0 for t in p.tools.values())
    assert p.selected_tool is None and p.mmu.fed is None
    assert messages(sim, "mmu")[-2:] == ["T3 eject done", "T3 cut done"]


def test_preload_writes_the_new_type_after_and_counts_no_unload():
    sim, p = make_mmu(types={0: PLA, 1: PLA})
    p.change_all([0, 1], name="PETG", color="#ff0000")
    assert p.state is State.BUSY and isinstance(p.tools[0].process, MmuProcess)
    # The type is written after the preload.
    assert p.tools[0].material.name == "PLA"
    run_checked(sim, p, 3.1)
    assert p.tools[0].material.name == "PETG" and p.tools[0].material.color == "#ff0000"
    assert isinstance(p.tools[1].process, MmuProcess) and p.state is State.BUSY
    until_idle(sim, p)
    assert [p.tools[i].material.name for i in (0, 1)] == ["PETG", "PETG"]
    assert all(t.unload_count == 0 for t in p.tools.values())
    assert all(t.extruded == 0 for t in p.tools.values())


def test_preloads_unload_choice_clears_the_type_and_counts():
    sim, p = make_mmu(types={0: PLA, 1: PLA, 2: PLA})
    p.tools[0].extruded = 100.0
    # In index order: slot 0 is cleared at once, then slot 1 preloads; slot 2 is cleared
    # when slot 1's preload ends.
    p.change_all([1], name="PETG", unload=[0, 2])
    assert p.tools[0].material is None and p.tools[0].unload_count == 1
    assert p.tools[0].extruded_at_unload == 100
    assert p.tools[2].material.name == "PLA"
    until_idle(sim, p)
    assert p.tools[2].material is None and p.tools[2].unload_count == 1
    assert p.tools[1].material.name == "PETG" and p.tools[1].unload_count == 0
    assert "change all T1 to PETG; unload T0, T2" in messages(sim, "action")


def test_preloads_unload_choice_alone_takes_no_time():
    sim, p = make_mmu()
    p.change_all([], name="PLA", unload=[0, 3])
    # Slot 3 has no type, so its clear counts nothing.
    assert p.state is State.IDLE and not p.queue
    assert p.tools[0].unload_count == 1 and p.tools[3].unload_count == 0


def test_preload_all_writes_every_type_first():
    sim, p = make_mmu(types={0: PLA, 4: PETG})
    p.preload_all("PETG")
    assert all(t.material.name == "PETG" for t in p.tools.values())
    assert all(t.unload_count == 0 for t in p.tools.values())
    assert p.state is State.BUSY and [q.tool for q in p.queue] == [1, 2, 3, 4]
    until_idle(sim, p)
    assert messages(sim, "mmu")[-5:] == [f"T{i} preload done" for i in range(5)]


def test_a_power_cut_mid_preload_all_keeps_the_types():
    sim, p = make_mmu()
    p.preload_all("PETG")
    run_checked(sim, p, 4)
    p.reboot()
    check(p)
    assert p.state is State.IDLE and not p.queue
    assert all(t.process is None for t in p.tools.values())
    assert all(t.material.name == "PETG" for t in p.tools.values())


def test_a_power_cut_mid_preload_writes_no_type():
    sim, p = make_mmu(types={0: PLA, 1: PLA})
    p.change_all([0, 1], name="PETG")
    run_checked(sim, p, 1)
    p.reboot()
    assert [p.tools[i].material.name for i in (0, 1)] == ["PLA", "PLA"]
    assert p.state is State.IDLE and not p.queue


@pytest.mark.parametrize(
    ("seconds", "fed", "loaded"),
    [
        # In slot 0's ramming, 125 mm at 10 mm/s: slot 0 is still in the extruder.
        (5, 0, True),
        # In the feed, after slot 0's unload and before slot 2's switch.
        (20, None, False),
        # In the load to the nozzle, after the switch: slot 2 is in the extruder.
        (38, 2, True),
    ],
)
def test_a_power_cut_mid_load(seconds, fed, loaded):
    sim, p = no_auto_retract(types={0: PLA, 2: PETG}, slot=0)
    p.load_to_nozzle(2)
    run_checked(sim, p, seconds)
    p.reboot()
    check(p)
    assert p.selected_tool is None and p.mmu.fed == fed and p.mmu.path.loaded is loaded
    assert p.state is State.IDLE and p.tools[2].process is None
    assert [p.tools[i].material.name for i in (0, 2)] == ["PLA", "PETG"]


def test_a_snapshot_keeps_an_mmu_operation_and_its_queue():
    sim, p = make_mmu()
    p.change_all([1, 2], name="PETG", unload=[0])
    run_checked(sim, p, 1)
    saved = json.loads(json.dumps(sim.snapshot()))
    other = Simulation()
    other.restore(saved)
    q = other.printers["p"]
    assert q.tools[1].process == p.tools[1].process
    assert q.queue == p.queue and q.queue[0].material.name == "PETG"


def test_mmu_menu_guards():
    sim, p = make_mmu(types={0: PLA, 2: PETG}, slot=2, mmu_connect_seconds=1)
    with pytest.raises(Busy, match="Eject needs the extruder empty"):
        p.eject(0)
    with pytest.raises(Busy, match="Cut needs the extruder empty"):
        p.cut(0)
    with pytest.raises(Busy, match="Preload needs the extruder empty"):
        p.change_all([1], name="PLA")
    with pytest.raises(Invalid, match="T0's filament isn't in the extruder"):
        p.unload(0)
    with pytest.raises(Invalid, match="T1 has no type to load"):
        p.load_to_nozzle(1)
    p.unload(2)
    with pytest.raises(Busy, match="the MMU's menu needs an idle printer, not BUSY"):
        p.preload_all("PLA")
    until_idle(sim, p)
    with pytest.raises(Invalid, match="T1 can't both change and unload"):
        p.change_all([1, 2], name="PLA", unload=[1])
    with pytest.raises(Invalid, match="change all needs at least one tool"):
        p.change_all([], name="PLA")
    # Refused while the MMU connects.
    p.reboot()
    for call in (lambda: p.load_to_nozzle(0), lambda: p.eject(0), lambda: p.preload_all("PLA")):
        with pytest.raises(Busy, match="the MMU is connecting"):
            call()
    p.set_power("off")
    with pytest.raises(Busy, match="p is off"):
        p.cut(0)


def test_mmu_operations_need_an_mmu_switched_on():
    sim, p = make_mmu(slot=0)
    p.set_mmu(False)
    with pytest.raises(Invalid, match="the MMU is off"):
        p.load_to_nozzle(0)
    with pytest.raises(Invalid, match="p has no MMU"):
        make()[1].eject(0)
    # A toolchanger's change all has no unload choice.
    sim, xl = make("3.1.0", [loaded(0), loaded(1)])
    with pytest.raises(Invalid, match="only an MMU's change all unloads a tool alone"):
        xl.change_all([0], name="PLA", unload=[1])
