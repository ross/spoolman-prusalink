"""The bridge against a printer with an MMU3: the MMU findings in docs/bridge-findings.md."""

from harness import LOC, jobs, ledger, make, menu, spool_x, tool_locations

from simulator.model import JobPhase, PrintSpec, Segment
from simulator.prusalink import filament, info
from simulator.tool import SimTool

MK4S = "1.4.1"
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
    # A one-color print on slot 0 counts 35 + 15 + 53 + 20 + 19.3 = 142.3 mm
    # (simulator/tests/test_mmu_prints.py), 135 at 0.95, all of it charged to spool 1.
    assert totals(fake) == {1: 135}
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
    # the old slot, and the wipe tower's stamping 15 mm more past the 40 mm gap. The bridge
    # charges each slot's spool what its counter says: slot 0 counts 657.6536 mm, slot 1
    # 467.3836 (simulator/tests/test_mmu_prints.py), so 624 and 444 at 0.95, where the
    # model, the start and the purges come to 70 + 40 + 61.95 + 3 x 20.9518 on slot 0 and
    # 86.63 + 40 + 20.9518 on slot 1.
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


async def resume_after_cut(station, spec, fed, connect=None):
    """Print `spec` to `fed` mm, polling, then cut the power and run the window, the
    recovery and the unpark. `connect` sets how long the MMU connects after the boot."""
    p = station.printer
    await station.start_print(spec)
    await run(station, lambda: p.job.fed_mm >= fed)
    await station.poll()  # the bridge charges what the print counted so far
    if connect is not None:
        station.sim.settings.mmu_connect_seconds = connect
    p.reboot(keep_job=True)
    await station.poll()  # in the window
    await run(station, lambda: p.job is not None and p.job.phase is JobPhase.PRINTING)
    await station.poll()


async def test_an_mmu_print_resumed_after_a_cut_is_charged_nothing_until_a_tool_change(
    tmp_path,
):
    # BF17: the boot leaves no slot selected. The MMU connects in 0.2 s here, before the
    # resume, so its 8 mm feed back and the rest of slot 0's segment count on no slot. The
    # change to slot 1 runs from none: slot 0's tower unload and the firmware's feed count
    # nothing, and slot 1 counts from its first pick on. Spool 1 is charged nothing after
    # the cut, though slot 0 printed 70 more mm.
    bridge, fake, station, _ = mmu(tmp_path, [(0, "PLA"), (1, "PLA")], t0="PLA", t1="PLA")
    p = station.printer
    await bridge.poll()
    spec = PrintSpec([Segment(0, 100), Segment(1, 100)], duration_s=20)
    await resume_after_cut(station, spec, 30)
    # Before the cut, slot 0 counted the start's 17 + 53 and 30 mm of its segment: 95.
    assert totals(fake) == {1: 95}
    assert p.mmu.active and p.selected_tool is None and p.job.segment == 0
    await run(station, lambda: p.job.segment == 1)
    await station.poll()
    # Slot 0 printed its last 70 mm and changed to slot 1; slot 1's first pick counted.
    assert totals(fake) == {1: 95, 2: 82}
    await run(station, lambda: p.job is None)
    await station.poll()
    # Slot 1: 32 + 54.63, its 100 mm and its tower unload at the end, 35.9518: 222.5818
    # mm counted, 211 charged.
    assert totals(fake) == {1: 95, 2: 211}
    assert "tool change none -> T1" in [e.text for e in station.sim.events if e.kind == "job"]
    await bridge.aclose()


async def test_the_resume_feed_back_lands_on_slot_0_while_the_mmu_connects(tmp_path):
    # BF17: a print on slot 2, resumed while the MMU still connects. The resume feeds 8 mm
    # of slot 2's filament back, which counts 7.6 on slot 0, and the MMU is Active before
    # the print goes on, so nothing counts after that. The bridge charges spool 1, which
    # never moved, and spool 3 nothing from the cut to the print's end.
    bridge, fake, station, _ = mmu(tmp_path, [(0, "PLA"), (2, "PLA")], t0="PLA", t2="PLA")
    p = station.printer
    await bridge.poll()
    spec = PrintSpec([Segment(2, 200)], duration_s=20)
    # 3.5 s after the boot the 8 mm feeds back, and 4.5 s after it the print goes on.
    await resume_after_cut(station, spec, 30, connect=4)
    # Before the cut, slot 2 counted the start's 17 + 53 and 30 mm of the print: 95.
    assert totals(fake) == {3: 95, 1: 7} and station.counter_at(0) == 7
    await run(station, lambda: p.job is None)
    await station.poll()
    assert totals(fake) == {3: 95, 1: 7}
    await bridge.aclose()
