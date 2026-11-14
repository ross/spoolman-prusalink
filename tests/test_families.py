"""The bridge on each printer family the simulator models, through one story.

Each family runs the same story on the simulator's own loads, unloads and prints, with
the model's auto-retract on: a print, a swap on tool 0 (Spoolman first, a menu unload and
a menu load), a second print, a reboot and a short third print. On a multi-tool family
tool 1 is loaded too, and each print changes tool from T0 to T1.

The expected charges are worked by hand from the firmware's numbers in
simulator/README.md. A move counts only past the tool's gap, times the feed ratio: 0.95
with a 40 mm gap on a Nextruder, 550/576 with a 27 mm gap on INDX. A Nextruder's
auto-retract feeds back what's retracted, counts 23 and leaves 55 stored; the next feed
deretracts the 55 and counts 15 that fill the gap. INDX's counts 8 and leaves 8, which
the next feed fills. A toolchanger's print adds its stock slicer profile's moves
(`simulator.slicer`), and its end G-code parks the tool before the firmware's end, which
then moves nothing but stores each tool's valid gap. The counters read whole millimetres,
so each charge is a difference of whole numbers.

The XL profile: T0 retracts 2 first; T1 primes 39 and retracts 20; T0 primes 39 and
retracts 0.8, which its first move feeds back. At the change T0 rams 4.1575 and retracts
20; T1 feeds the 20 back and purges 9.238 on its first pick. At the end T1 rams 4.1575 and
retracts 0.8. A print's own retract while a distance is stored doesn't move the filament
and keeps the distance, though the tracker counts it.

The INDX profile: `G427` purges each used tool 25 and auto-retracts it (8 counted, gap 8,
8 stored). T0 deretracts, primes 1.6 + 5.4907 and retracts 0.8, fed back. At the change T0
retracts 0.8 + 8; T1 deretracts, counts 1.6 + 4.1596 on its first pick and retracts 0.8,
fed back. At the end T1 retracts 0.8.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import pytest
from harness import (
    LOC,
    PLA,
    FakeSpoolman,
    jobs,
    ledger,
    levels,
    make,
    spool,
    spool_x,
    tool_locations,
)

from integration.station import Station
from simulator.model import MODELS, PrintSpec, Segment, SimTool

SHELF = "shelf"
SWAP_TOOL = 0
# T0, then T1 on a multi-tool family.
FIRST, SECOND = [(0, 100), (1, 50)], [(0, 10), (1, 10)]
STAGES = ("print 1", "swap", "print 2", "reboot", "print 3")
MULTI_TOOL = "multi-tool printers aren't supported yet"


class Family(NamedTuple):
    """What the story does on one family."""

    # What each stage charges each spool, a spool charged nothing left out. Spool 1
    # starts in tool 0, spool 2 in tool 1, and spool 3 replaces spool 1.
    charges: dict[str, dict[int, int]]
    # Why the bridge doesn't write spool 3 to the printer, or None when it does.
    refusal: str | None
    # Whether the model keeps UUIDs, so the write gives spool 3 one.
    uuids: bool


FAMILIES = {
    "8.1.0": Family(  # Core One L: 40 slow, 20 fast, 40 purge, an 8 mm retract.
        {
            # From gap 0, 100 printed; the end retracts 8, feeds them back and counts the
            # auto-retract's 23. 123 * 0.95 = 116.85: 1000 to 1116.85.
            "print 1": {1: 116},
            # 55 stored is fully retracted, so the unload is cold and counts nothing. The
            # load from gap 40: 20 fast past it, 40 purge; the auto-retract feeds the 8
            # back and counts 23. 83 * 0.95 = 78.85: 1195.70.
            "swap": {3: 79},
            # The deretract's 15, 100 printed, the end's 23: 138 * 0.95 = 131.1: 1326.80.
            "print 2": {3: 131},
            "reboot": {},
            # From 0 with 55 stored, so a full gap: 15 + 10 + 23 = 48 * 0.95 = 45.6.
            "print 3": {3: 45},
        },
        None,
        uuids=True,
    ),
    "1.4.1": Family(  # MK4S: 30 slow, 50 fast, 27 purge, an 8 mm retract.
        {
            "print 1": {1: 116},  # As on the Core One L: prints don't load.
            # The load from gap 40: 30 slow (gap 10), 40 of the 50 fast past it, 27 purge,
            # the auto-retract's 23: 90 * 0.95 = 85.5. 1116.85 to 1202.35.
            "swap": {3: 86},
            "print 2": {3: 131},  # 1202.35 + 131.1 = 1333.45
            "reboot": {},
            "print 3": {3: 45},
        },
        None,
        uuids=False,
    ),
    "4.1.0": Family(  # iX: 40 slow, 45 fast, 40 purge in 5 mm steps, no retract after it.
        {
            "print 1": {1: 116},
            # From gap 40: 45 fast, 40 purge, 23: 108 * 0.95 = 102.6. 1116.85 to 1219.45.
            "swap": {3: 103},
            "print 2": {3: 131},  # 1219.45 + 131.1 = 1350.55
            "reboot": {},
            "print 3": {3: 45},
        },
        "iX isn't supported: the iX isn't supported yet",
        uuids=False,
    ),
    "3.1.0": Family(  # XL: the MK4's lengths on five tools; tool 1 starts at 500.
        {
            # From gap 0. T0: -2 (gap 2), 39 of which 37 count, 100, the 4.1575 ramming:
            # 141.1575 * 0.95 = 134.10, 1134.10. T1: 39, the 20 back, the 9.238 purge, 50,
            # the end's 4.1575: 102.3955 * 0.95 = 97.28, 597.28. The end leaves T0 at gap
            # 20 and T1 at 0.8, which it stores.
            "print 1": {1: 134, 2: 97},
            # The menu picks T0, 20 mm stored: fully retracted, so the unload is cold and
            # counts nothing. Then the load as on the MK4S, 85.5: 1219.60. T0 stays picked.
            "swap": {3: 85},
            # T0, 55 stored: the -2 keeps them (gap 40), the prime deretracts them and
            # counts 15, then 39, 100 and 4.1575: 158.1575 * 0.95 = 150.25, 1369.85. T1,
            # 0.8 stored: 102.3955 * 0.95 = 97.28, 694.55.
            "print 2": {3: 150, 2: 97},
            "reboot": {},
            # T0 boots at gap 20 with 20 stored: the -2 keeps them (gap 22), the deretract
            # leaves gap 2, then 37 + 10 + 4.1575 = 51.1575 * 0.95 = 48.60. T1, 0.8 stored:
            # 39 + 9.238 + 10 + 4.1575 = 62.3955 * 0.95 = 59.28.
            "print 3": {3: 48, 2: 59},
        },
        f"XL isn't supported: {MULTI_TOOL}",
        uuids=False,
    ),
    "8.10.0": Family(  # Core One L INDX on eight tools: 15 slow, 25 fast, the cleaner's 25.
        {
            # T0: 25 + 8, 1.6 + 5.4907, 100: 140.0907 * 550/576 = 133.77, 1133.77. T1: 25 +
            # 8, 1.6 + 4.1596, 50: 88.7596 * 550/576 = 84.75, 584.75. The end leaves T0 at
            # gap 8.8 and T1 at 0.8, which it stores.
            "print 1": {1: 133, 2: 84},
            # T0's unload purges 2 after the 8.8 fed back, and rams 17: 19 * 550/576 =
            # 18.14, 1151.91. The load from gap 27: 13 fast past it, the 25 purge, the
            # cleaner's auto-retract 8: 46 * 550/576 = 43.92, 1195.83.
            "swap": {1: 18, 3: 44},
            # Each tool's stored distance fills its gap before the start's purge, so both
            # count as in print 1: T0 133.77, 1329.60; T1 84.75, 669.51.
            "print 2": {3: 134, 2: 85},
            "reboot": {},
            # T0: 33 + 7.0907 + 10 = 50.0907 * 550/576 = 47.83. T1: 33 + 5.7596 + 10 =
            # 48.7596 * 550/576 = 46.56.
            "print 3": {3: 47, 2: 46},
        },
        f"COREONEL-INDX isn't supported: {MULTI_TOOL}",
        uuids=False,
    ),
}


def segments(tools: int, plan: list[tuple[int, float]]) -> PrintSpec:
    used = plan if tools > 1 else plan[:1]
    return PrintSpec([Segment(t, mm) for t, mm in used], duration_s=2, preheat_s=0.5)


async def story(tmp_path, model: str) -> tuple[dict, FakeSpoolman, Station]:
    """Run the story on `model`; return each stage's charges by spool, the fake Spoolman
    and the station."""
    count = MODELS[model].max_tools
    tools = [SimTool.loaded_with(0, PLA, extruded=1000.0)]
    if count > 1:
        tools.append(SimTool.loaded_with(1, PLA, extruded=500.0))
    tools += [SimTool(index) for index in range(len(tools), count)]
    locations = tool_locations(LOC, count)
    spools = [spool(1, locations[0]), spool_x(3, SHELF, material="PETG", color="186bb5")]
    if count > 1:
        spools.append(spool(2, locations[1]))
    bridge, fake, station = make(tmp_path, spools, model=model, tools=tools, auto_retract=True)
    station.poll = lambda: bridge.poll()
    printer = station.printer

    async def poll():
        await bridge.poll()
        while printer.job is not None and printer.job.kind == "sync":
            station.finish_job()
            await bridge.poll()

    async def run_print(plan):
        await station.start_print(segments(count, plan))
        await station.run_until(lambda: printer.job is None, poll_every=1.0)
        await poll()

    async def swap():
        fake.spools[1]["location"], fake.spools[3]["location"] = SHELF, locations[SWAP_TOOL]
        await poll()
        await station.unload(SWAP_TOOL)
        await poll()
        await station.swap_cycle(tool=SWAP_TOOL, source="preset", name="PETG")
        await poll()

    async def reboot():
        printer.reboot()
        await poll()

    def totals():
        return {spool_id: sum(mm) for spool_id, mm in fake.used.items()}

    steps = [lambda: run_print(FIRST), swap, lambda: run_print(FIRST), reboot]
    steps.append(lambda: run_print(SECOND))
    await poll()  # The baseline.
    charged = {}
    for stage, step in zip(STAGES, steps, strict=True):
        before = totals()
        await step()
        charged[stage] = {
            spool_id: mm - before.get(spool_id, 0)
            for spool_id, mm in totals().items()
            if mm != before.get(spool_id)
        }
    await bridge.aclose()
    return charged, fake, station


@pytest.mark.parametrize("model", FAMILIES, ids=lambda code: MODELS[code].name)
async def test_family_story(tmp_path, model):
    """Each stage charges what the family's firmware counts, to the spool in the tool."""
    expected, refusal, uuids = FAMILIES[model]
    charged, fake, station = await story(tmp_path, model)
    assert charged == expected

    tools = range(MODELS[model].max_tools)
    locations = tool_locations(LOC, len(tools))
    records = ledger(tmp_path)
    # A tool change isn't an unload: the only unload, on the printer and to the bridge,
    # is the swap's.
    unloads = [e.text for e in station.sim.events if e.kind == "unload"]
    assert len(unloads) == 1 and unloads[0].startswith(f"T{SWAP_TOOL} unload:")
    swaps = ("unload", "swap_staged", "swap_committed")
    assert [(r["tool"], r["event"]) for r in records if r.get("event") in swaps] == [
        (SWAP_TOOL, "swap_staged"),
        (SWAP_TOOL, "unload"),
        (SWAP_TOOL, "swap_committed"),
    ]
    # The reboot restarts every tool's counter and keeps nothing: every poll charged.
    reboots = [r for r in records if r.get("event") == "reboot"]
    assert [(r["tool"], r["kept_mm"]) for r in reboots] == [(t, 0) for t in tools]
    # Spool 1 went to the shelf. Spool 2 stayed in tool 1 through the swap, unflagged.
    assert fake.spools[1]["location"] == SHELF
    if len(tools) > 1:
        assert fake.spools[2]["location"] == locations[1] and not fake.flag(2)

    if refusal is None:
        assert len(jobs(station)) == 1 and not fake.flag(3)
    else:
        assert jobs(station) == []
        assert f"{locations[SWAP_TOOL]}: can't write to the printer: {refusal}" in fake.flag(3)
    # Only a model that keeps UUIDs gives the written spool one, and stores it.
    assert ("instance_uuid" in fake.spools[3]["extra"]) is uuids
    assert (station.material(SWAP_TOOL)[2] is not None) is uuids


async def test_family_without_counters(tmp_path, caplog):
    """MINI: no usage counters, so the story charges nothing, and the bridge says why once."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    charged, fake, station = await story(tmp_path, "2.1.0")
    assert charged == {stage: {} for stage in STAGES}
    assert ledger(tmp_path) == []
    assert levels(caplog, "has no usage counters") == ["ERROR"]
    assert jobs(station) == [] and not fake.flag(3)
