"""The bridge against a printer with an MMU3: the MMU findings in docs/bridge-findings.md."""

import json

from harness import LOC, jobs, ledger, make, menu, poll_during_job, spool_x, tool_locations

from simulator.model import PrintSpec, Reason, Segment
from simulator.prusalink import filament, info, status
from simulator.tool import SimTool

MK4S = "1.4.1"
CORE_ONE = "7.1.0"
COLORS = ("ff0000", "00ff00", "0000ff", "ffff00", "00ffff")
SHELF = "shelf"
SWAPS = ("swap_staged", "unload", "swap_committed", "swap_unconfirmed", "swap_confirmed")
# The acceptance print of simulator/tests/test_mmu_prints.py: slots 0 and 1, twice each.
TWO_COLOR = PrintSpec([Segment(t, 20) for t in (0, 1, 0, 1)], duration_s=8)


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
    # location first, stays staged, and the next print is charged to spool 1, which the
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
    await print_through(station, PrintSpec([Segment(0, 20)], duration_s=2))
    assert swaps(tmp_path, mark) == [(0, "swap_staged")]
    # A one-color print on slot 0 counts 17 + 53 + 20 + 19.3 = 109.3 mm
    # (simulator/tests/test_mmu_prints.py), 103 at 0.95, all of it charged to spool 1.
    assert totals(fake) == {1: 103}
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


async def print_through(station, spec):
    await station.start_print(spec)
    await run(station, lambda: station.printer.job is None)
    await station.poll()


async def test_an_mmu_tool_change_charges_the_outgoing_slot_the_firmware_feed(tmp_path):
    # BF16: each change counts the firmware's feed, 206.9 mm of the new slot's filament, on
    # the old slot, and the wipe tower's stamping 15 mm more past the gap. The bridge
    # charges each slot's spool what its counter says. In commanded mm, slot 0 counts
    # 657.6536 and slot 1 467.3836 (simulator/tests/test_mmu_prints.py), and the counters
    # read 624 and 444 at 0.95. Each slot rams out twice, so the start, the model, the
    # loads, the purges and the rammings come to 70 + 40 + 32 + 29.95 + 2 x 20.9518 =
    # 213.8536 on slot 0 and 32 + 54.63 + 40 + 32 + 29.95 + 2 x 20.9518 = 230.4836 on
    # slot 1; the rest is 2 x (206.9 + 15) on slot 0 and 206.9 + 2 x 15 on slot 1.
    bridge, fake, station, _ = mmu(tmp_path, [(0, "PLA"), (1, "PLA")], t0="PLA", t1="PLA")
    p = station.printer
    await bridge.poll()
    mark = len(ledger(tmp_path))
    await print_through(station, TWO_COLOR)
    assert [station.counter_at(i) for i in range(5)] == [624, 444, 0, 0, 0]
    assert totals(fake) == {1: 624, 2: 444}
    assert swaps(tmp_path, mark) == []
    assert p.selected_tool is None and all(t.unload_count == 0 for t in p.tools.values())
    assert fake.flag(1) is None and fake.flag(2) is None
    await bridge.aclose()


async def test_a_second_mmu_print_in_one_boot_counts_less_at_its_start(tmp_path):
    # BF29: the first print's end left the gap at 40, and the second print's first change,
    # with no slot selected, leaves it there. The start's 17 + 53 then count 30 on slot 0,
    # not 70: the bridge charges spool 1 38 mm less, and spool 2 the same again.
    bridge, fake, station, _ = mmu(tmp_path, [(0, "PLA"), (1, "PLA")], t0="PLA", t1="PLA")
    await bridge.poll()
    await print_through(station, TWO_COLOR)
    first = totals(fake)
    await print_through(station, TWO_COLOR)
    second = {spool: total - first[spool] for spool, total in totals(fake).items()}
    assert first == {1: 624, 2: 444}
    # 1275.3072 and 934.7672 mm counted: 1211 and 888.
    assert second == {1: 1211 - 624, 2: 888 - 444}
    await bridge.aclose()


async def test_a_sync_job_on_a_loaded_mmu_waits_on_the_unload_question(tmp_path):
    # BF18: after Load To Nozzle, FINDA sees slot 0's filament, so the preview asks whether
    # to unload it first, and the bridge's job waits in ATTENTION with its ID. The bridge
    # uploads once and then only waits: no retry, no time-out and no flag, for as long as
    # nobody answers. No runs the job with the filament in place, and its end leaves it
    # there, so the next job would ask again.
    locations = tool_locations(LOC, 5)
    bridge, fake, station = make(
        tmp_path,
        [spool_x(10, locations[0], color="00ff00")],
        model=MK4S,
        tools=5,
        mmu=True,
        sync_drift=True,
    )
    p = station.printer
    p.load_to_nozzle(0)
    await station.run_until(lambda: p.tools[0].process is None, limit=120)
    await bridge.poll()
    assert len(jobs(station)) == 1
    station.advance(1)
    assert p.job.waits_for(Reason.MMU_LOADED) and p.job.id == 1
    assert status(p)["printer"]["state"] == "ATTENTION" and status(p)["job"]["id"] == 1
    for _ in range(40):  # 20 minutes
        station.advance(30)
        await poll_during_job(bridge)
    assert len(jobs(station)) == 1 and p.job.waits_for(Reason.MMU_LOADED)
    assert fake.flag(10) is None
    p.tap_through()
    station.finish_job()
    await bridge.poll()
    assert len(jobs(station)) == 1 and station.material(0)[1] == "#00ff00"
    assert fake.flag(10) is None
    assert p.mmu.fed == 0 and p.mmu.path.loaded and p.selected_tool == 0
    await bridge.aclose()


async def test_mmu_writes_land_on_every_slot_and_uuids_on_slot_0(tmp_path):
    # BF19, on a Core One, which keeps tag UUIDs: the bridge mints and writes an instance
    # UUID for tool 0's spool alone, `M865.1 I0`, and a type and color for every slot. Each
    # lands, slots 1 to 4 keep no UUID, and the printer then matches every spool, so one
    # job does it and nothing is flagged. A write to slots 1 to 4 that carried UUIDs would
    # lose them, but the bridge sends none.
    locations = tool_locations(LOC, 5)
    spools = [spool_x(10 + i, locations[i], color=c) for i, c in enumerate(COLORS)]
    bridge, fake, station = make(
        tmp_path,
        spools,
        model=CORE_ONE,
        tools=5,
        mmu=True,
        sync_drift=True,
        uuid_field="instance_uuid",
    )
    p = station.printer
    for i in range(1, 5):
        station.tool_at(i).store(menu("PLA"))
    await bridge.poll()
    (gcode,) = jobs(station)
    minted = json.loads(fake.spools[10]["extra"]["instance_uuid"])
    assert [line for line in gcode.splitlines() if line.startswith("M865.1")] == [
        f'M865.1 I0 C"{minted}"'
    ]
    assert all(fake.spools[10 + i]["extra"] == {} for i in range(1, 5))
    station.finish_job()
    await bridge.poll()
    assert len(jobs(station)) == 1
    assert [station.material(i)[1] for i in range(5)] == [f"#{c}" for c in COLORS]
    assert [p.tools[i].material.instance_uuid for i in range(5)] == [minted] + [None] * 4
    assert all(fake.flag(10 + i) is None for i in range(5))
    await bridge.aclose()
