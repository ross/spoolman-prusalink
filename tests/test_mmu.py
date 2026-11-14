"""The bridge against a printer with an MMU3: the MMU findings in docs/bridge-findings.md."""

from harness import LOC, jobs, make, menu, spool_x, tool_locations

from simulator.prusalink import filament, info

MK4S = "1.4.1"
COLORS = ("ff0000", "00ff00", "0000ff", "ffff00", "00ffff")


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
