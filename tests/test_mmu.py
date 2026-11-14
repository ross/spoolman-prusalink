"""The bridge against a printer with an MMU3: the MMU findings in docs/bridge-findings.md."""

from harness import LOC, jobs, ledger, make, menu, spool_x, tool_locations

from simulator.prusalink import filament, info
from simulator.tool import SimTool

MK4S = "1.4.1"
COLORS = ("ff0000", "00ff00", "0000ff", "ffff00", "00ffff")
SHELF = "shelf"
SWAPS = ("swap_staged", "unload", "swap_committed", "swap_unconfirmed", "swap_confirmed")


def mmu(tmp_path, spools, **types):
    """A bridge on an MK4S with its MMU on and Active, nothing in the extruder, and slot
    N's type `types["tN"]`, red to match `spool_x`. Spool N + 1 sits in slot N's
    location for each of `spools`, a list of (slot, material)."""
    locations = tool_locations(LOC, 5)
    tools = [SimTool(i) for i in range(5)]
    for name, material in types.items():
        tools[int(name[1:])].store(menu(material, "#ff0000"))
    made = [spool_x(slot + 1, locations[slot], material=m) for slot, m in spools]
    bridge, fake, station = make(tmp_path, made, model=MK4S, tools=tools, mmu=True)
    station.poll = lambda: bridge.poll()
    return bridge, fake, station, locations


async def run(station, done):
    await station.run_until(done, poll_every=1.0, limit=120)


def swaps(tmp_path, mark):
    return [(r["tool"], r["event"]) for r in ledger(tmp_path)[mark:] if r.get("event") in SWAPS]


def totals(fake):
    return {spool_id: sum(mm) for spool_id, mm in fake.used.items()}


async def test_the_bridge_writes_to_an_mmu_printer_as_a_single_tool_model(tmp_path):
    # BF20: the printer says it has an MMU and lists a tool per slot. The bridge doesn't
    # read `info`, takes the MK4S for a supported single-tool model, and writes each
    # slot's spool to its slot in one job, which runs: the MMU is Active with nothing
    # loaded, so the preview asks nothing, and its end neither moves nor counts filament.
    locations = tool_locations(LOC, 5)
    spools = [spool_x(10 + i, locations[i], color=c) for i, c in enumerate(COLORS)]
    bridge, fake, station = make(tmp_path, spools, model=MK4S, tools=5, mmu=True, sync_drift=True)
    p = station.printer
    for i in range(1, 5):
        station.tool_at(i).store(menu("PLA"))
    assert info(p)["mmu"] is True
    assert [t["index"] for t in filament(p)["tools"]] == [0, 1, 2, 3, 4]

    await bridge.poll()
    (gcode,) = jobs(station)
    assert gcode.splitlines()[0] == 'M862.3 P "MK4S" ; printer model check'
    for i, color in enumerate(COLORS):
        assert f'O"#{color}" L{i}' in gcode
    station.finish_job()
    await bridge.poll()
    assert len(jobs(station)) == 1
    assert [station.material(i)[1] for i in range(5)] == [f"#{c}" for c in COLORS]
    assert [station.counter_at(i) for i in range(5)] == [1000, 0, 0, 0, 0]
    assert all(t.unload_count == 0 for t in p.tools.values())
    await bridge.aclose()


async def test_an_mmu_slot_swap_by_eject_and_preload_counts_no_unload(tmp_path):
    # BF15: Eject writes no type, and Preload writes the new one over the old with no
    # unload between, so the printer never reports the swap. Spool 3, moved into slot 0's
    # location first, stays staged, and the next load is charged to spool 1, which the
    # bridge flags for the printer's new type.
    bridge, fake, station, locations = mmu(tmp_path, [(0, "PLA")], t0="PLA")
    p = station.printer
    fake.spools[3] = spool_x(3, SHELF, material="PETG")
    await bridge.poll()
    mark = len(ledger(tmp_path))
    fake.spools[1]["location"], fake.spools[3]["location"] = SHELF, locations[0]
    await bridge.poll()
    p.eject(0)
    await run(station, lambda: p.tools[0].process is None)
    p.change_all([0], name="PETG")
    await run(station, lambda: not p.queue and p.tools[0].process is None)
    assert station.material(0)[0] == "PETG" and p.tools[0].unload_count == 0
    p.load_to_nozzle(0)
    await run(station, lambda: p.tools[0].process is None)
    await bridge.poll()
    assert swaps(tmp_path, mark) == [(0, "swap_staged")]
    # The load counts 47 * 0.95 = 44.65 from a fresh boot's empty gap.
    assert totals(fake) == {1: 44}
    assert "name printer='PETG' spoolman='PLA'" in fake.flag(1)
    await bridge.aclose()


async def test_turning_the_mmu_off_with_an_empty_nozzle_unloads_slot_0(tmp_path):
    # BF28: `M709 S0` with nothing in the extruder writes none to slot 0 alone. The bridge
    # records an unload on slot 0's spool, which never left the MMU, and flags it, while
    # slot 2 keeps its type.
    bridge, fake, station, _ = mmu(tmp_path, [(0, "PLA"), (2, "PETG")], t0="PLA", t2="PETG")
    await bridge.poll()
    mark = len(ledger(tmp_path))
    station.printer.set_mmu(False)
    await bridge.poll()
    assert station.material(0)[0] is None and station.material(2)[0] == "PETG"
    assert swaps(tmp_path, mark) == [(0, "unload"), (0, "swap_unconfirmed")]
    assert "filament was unloaded with no Spoolman change" in fake.flag(1)
    assert fake.flag(3) is None
    await bridge.aclose()


async def test_turning_the_mmu_off_with_slot_2_loaded_charges_slot_0(tmp_path):
    # BF28: with slot 2's filament in the extruder, slot 0 keeps its type. The printer is
    # then a single-tool one, so a move counts on slot 0 and the bridge charges spool 1
    # for spool 3's filament, with no flag: slot 0's type still matches spool 1.
    bridge, fake, station, _ = mmu(tmp_path, [(0, "PLA"), (2, "PETG")], t0="PLA", t2="PETG")
    p = station.printer
    await bridge.poll()
    p.load_to_nozzle(2)
    await run(station, lambda: p.tools[2].process is None)
    await bridge.poll()
    mark = len(ledger(tmp_path))
    p.set_mmu(False)
    p.extrude(0, 100)
    await run(station, lambda: p.tools[0].process is None)
    await bridge.poll()
    assert swaps(tmp_path, mark) == []
    # Slot 2's load counts 44.65 on spool 3; the 100 mm move counts 95 on spool 1.
    assert totals(fake) == {3: 44, 1: 95}
    assert fake.flag(1) is None and fake.flag(3) is None
    await bridge.aclose()


async def test_a_load_to_nozzle_after_an_unload_counts_7(tmp_path):
    # BF29: the unload's ramming counts 20 and leaves the gap at 40, and the next tool
    # change's feed, with no slot selected, counts nothing and leaves it there. The load
    # then fills the 40 first and counts 7 of its 47 mm, where the first load of the boot
    # counted all 47.
    bridge, fake, station, _ = mmu(tmp_path, [(0, "PLA")], t0="PLA")
    p = station.printer
    await bridge.poll()
    for op in (p.load_to_nozzle, p.unload, p.load_to_nozzle):
        op(0)
        await run(station, lambda: p.tools[0].process is None)
        await bridge.poll()
    assert fake.used == {1: [44.0, 19.0, 7.0]}
    assert p.tools[0].unload_count == 0
    await bridge.aclose()
