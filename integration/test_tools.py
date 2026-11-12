"""Multi-tool printers, on a simulated Core One L INDX: docs/scenarios.md rows T1 to T7.

No INDX is at hand, so these run against the simulator only, which follows the firmware
source (docs/printer-behaviors.md, F19 to F23).
"""

from __future__ import annotations

import logging

import pytest

from integration.conftest import (
    ASA,
    LOAD_MM,
    World,
    baseline,
    charges,
    event_fields,
    events,
    logged,
)
from simulator.model import PrintSpec, Segment, State

pytestmark = pytest.mark.parametrize("world_tools", [2], indirect=True)

MODEL_ERROR = "COREONEL-INDX isn't supported: multi-tool printers aren't supported yet"
MODEL_WARNING = f"is COREONEL-INDX (8.10.0): {MODEL_ERROR}; printer writes are off"
# Spool D's tool index, as the bridge is configured.
TOOL1 = 1


def two_tool_print() -> PrintSpec:
    """T0 200 mm, T1 100 mm, T0 100 mm: two tool changes, each with a pickup purge."""
    return PrintSpec(
        [Segment(0, 200), Segment(1, 100), Segment(0, 100)], duration_s=4, preheat_s=0.5
    )


def counted(w: World, slicer_mm: float) -> float:
    """What the printer counts for `slicer_mm` of print moves."""
    return slicer_mm * w.sim.settings.feed_ratio


async def finish(w: World) -> list[dict]:
    return await w.run_until(
        lambda: w.printer.state is State.FINISHED and w.printer.job is None, poll_every=1.0
    )


async def print_both(w: World) -> list[dict]:
    """Run `two_tool_print()` to its end and return every record it wrote."""
    mark = w.mark()
    await w.start_print(two_tool_print())
    await finish(w)
    await w.poll()
    return w.since(mark)


async def test_t1_tool_changes_charge_each_tool(world: World):
    """Tool change: each tool's spool gets its own moves; the pickup purge goes to the new tool."""
    w = world
    await baseline(w)
    start0, start1 = w.counter_at(0), w.counter_at(1)

    # One tool change, T0 to T1, so only T1 is picked up: the purge must land on D, and
    # a purge counted on the tool put down would show up as 30 mm more on A.
    mark = w.mark()
    await w.start_print(PrintSpec([Segment(0, 300), Segment(1, 100)], duration_s=4, preheat_s=0.5))
    await finish(w)
    await w.poll()
    records = w.since(mark)
    changes = [e.text for e in w.sim.events if e.text.startswith("tool change")]
    assert changes == ["tool change T0 -> T1"]

    # 300 mm of T0 moves count 285 and 100 mm of T1 count 95, at the feed ratio, and the
    # pickup purges toolchange_purge_mm on T1.
    purge = w.sim.settings.toolchange_purge_mm
    assert (counted(w, 300), counted(w, 100), purge) == (285, 95, 30)
    assert w.charged(records, "A") == 285 == w.counter_at(0) - start0
    assert w.charged(records, "D") == 95 + 30 == w.counter_at(1) - start1
    assert {r["tool"] for r in charges(records, w.spools["A"])} == {0}
    assert {r["tool"] for r in charges(records, w.spools["D"])} == {1}


async def test_t2_tool_change_is_not_an_unload(world: World):
    """A tool change isn't an unload: no swap opens on either tool, and nothing is flagged."""
    w = world
    await baseline(w)

    # Poll often, so the bridge reads the printer between the tool changes too.
    mark = w.mark()
    await w.start_print(two_tool_print())
    await w.run_until(
        lambda: w.printer.state is State.FINISHED and w.printer.job is None, poll_every=0.3
    )
    await w.poll()
    records = w.since(mark)
    assert [e.text for e in w.sim.events if e.text.startswith("tool change")] != []
    assert w.tool_at(0).unload_count == 0 and w.tool_at(1).unload_count == 0
    assert events(records) == []
    assert await w.flag("A") is None
    assert await w.flag("D") is None


async def test_t3_swap_on_one_tool_leaves_the_other(world: World):
    """Swap on tool 1 between prints: tool 1's spool changes, tool 0's state is untouched."""
    w = world
    a, c, d = w.spools["A"], w.spools["C"], w.spools["D"]
    await baseline(w)
    tool0 = w.bridge.printers[0].tools[0]

    # Spoolman first (S1), on tool 1: C moves into T1's location, D onto the shelf.
    await w.move("D", w.shelf)
    await w.move("C", w.locations[TOOL1])
    records = await w.poll()
    assert [(e["tool"], e["event"]) for e in events(records)] == [(1, "swap_staged")]

    w.printer.extrude(TOOL1, 20)
    await w.unload(TOOL1)
    records = await w.poll()
    assert [(e["tool"], e["event"]) for e in events(records)] == [
        (1, "unload"),
        (1, "swap_committed"),
    ]
    assert event_fields(records, "swap_committed") == [{"from_spool": d, "to_spool": c}]
    assert w.charged(records, "D") == 20

    # No tag reader on an INDX: the load is from the menu's preset.
    records = await w.swap_cycle(tool=TOOL1, source="preset", name=ASA[0])
    records += await w.poll()
    assert w.charged(records, "C") == LOAD_MM
    assert charges(records, a) == []

    # Tool 0 saw none of it.
    assert w.bridge.printers[0].tools[0] == tool0
    assert await w.used("A") == 0
    assert await w.spoolman.location(a) == w.tool_location
    assert await w.flag("A") is None

    # The next print on both tools charges A on tool 0 and C on tool 1.
    start0, start1 = w.counter_at(0), w.counter_at(1)
    records = await print_both(w)
    assert w.charged(records, "A") == w.counter_at(0) - start0
    assert w.charged(records, "C") == w.counter_at(1) - start1
    assert {r["spool_id"] for r in charges(records)} == {a, c}


async def test_t4_idle_sensor_clear_on_a_picked_tool(world: World):
    """Picking a tool whose sensor sees no filament counts an unload on that tool only."""
    w = world
    d = w.spools["D"]
    await baseline(w)

    # Tool 1's filament is pulled out while the printer is off, so its type stays stored.
    # The boot's idle clear runs on the selected tool, 0, which still has filament.
    w.printer.set_power("off")
    w.printer.unload(TOOL1)
    w.printer.set_power("on")
    assert w.material(TOOL1) != (None, None, None)
    records = await w.poll()
    assert [(e["tool"], e["event"]) for e in events(records)] == [(0, "reboot"), (1, "reboot")]

    w.printer.select_tool(TOOL1)
    assert w.tool_at(TOOL1).unload_count == 1 and w.tool_at(0).unload_count == 0
    records = await w.poll()
    assert [(e["tool"], e["event"]) for e in events(records)] == [
        (1, "unload"),
        (1, "swap_unconfirmed"),
    ]
    assert event_fields(records, "swap_unconfirmed") == [{"spool": d}]
    flag = await w.flag("D")
    assert flag is not None and w.locations[TOOL1] in flag
    assert await w.flag("A") is None


async def test_t5_tool_the_printer_does_not_report(world: World, caplog: pytest.LogCaptureFixture):
    """A configured tool the printer doesn't report: noted once, the other tool still charged."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    await baseline(w)
    note = f"[{w.locations[TOOL1]}] printer doesn't report tool index {TOOL1}"

    # Disabled, or its dock not calibrated: tool 1 drops out of /api/v1/filament.
    w.printer.set_tool_enabled(TOOL1, False)
    assert [t["index"] for t in w.printer.filament_json()["tools"]] == [0]
    mark = w.mark()
    for _ in range(3):
        w.printer.extrude(0, 10)
        await w.poll()
    records = w.since(mark)
    assert w.charged(records, "A") == 30
    assert charges(records, w.spools["D"]) == []
    assert events(records) == []
    assert [r.getMessage() for r in caplog.records].count(note) == 1

    # Back again: tool 1 carries on from its counter, with nothing to charge.
    w.printer.set_tool_enabled(TOOL1, True)
    records = await w.poll()
    assert events(records) == [] and charges(records) == []


async def test_t6_writes_on_a_multi_tool_model_are_refused(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Writes on a multi-tool model: warned at boot, and a due write flags the spool instead."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    c = w.spools["C"]
    await baseline(w)
    await w.poll()
    warned = [r for r in caplog.records if MODEL_WARNING in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING

    # S1 on tool 1 with a menu load: the printer shows the preset, so a write of C is due.
    await w.move("D", w.shelf)
    await w.move("C", w.locations[TOOL1])
    await w.poll()
    await w.unload(TOOL1)
    await w.poll()
    await w.swap_cycle(tool=TOOL1, source="preset", name=ASA[0])
    assert w.material(TOOL1) == (ASA[0], None, None)
    for _ in range(3):
        await w.poll()
        w.advance(1.0)

    assert await w.flag("C") == f"{w.locations[TOOL1]}: can't write to the printer: {MODEL_ERROR}"
    assert w.requests("PUT") == []
    assert w.material(TOOL1) == (ASA[0], None, None)
    assert not any(e.kind == "gcode" for e in w.sim.events)
    note = f"[{w.locations[TOOL1]}] can't write spool {c} to the printer: {MODEL_ERROR}"
    assert [r.getMessage() for r in caplog.records].count(note) == 1
    assert len([r for r in caplog.records if MODEL_WARNING in r.getMessage()]) == 1
    assert logged(caplog, logging.WARNING, note)


async def test_t7_reboot_restarts_every_tool(world: World):
    """Reboot on a multi-tool printer: each tool's counter restarts, and nothing is charged."""
    w = world
    await baseline(w)
    w.printer.extrude(0, 10)
    w.printer.extrude(TOOL1, 20)
    records = await w.poll()
    assert w.charged(records, "A") == 10 and w.charged(records, "D") == 20

    old_boot = w.printer.boot_id
    w.printer.reboot()
    assert w.counter_at(0) == 0 and w.counter_at(1) == 0
    records = await w.poll()
    reboots = events(records, "reboot")
    assert [e["tool"] for e in reboots] == [0, 1]
    # Every poll charged its usage, so nothing was held to keep.
    reboot = {"from_boot": old_boot, "to_boot": w.printer.boot_id, "kept_mm": 0}
    assert event_fields(records, "reboot") == [reboot, reboot]
    assert charges(records) == []
    assert events(records, "unload") == []

    # Each tool is charged its own counter from 0.
    records = await print_both(w)
    assert w.charged(records, "A") == w.counter_at(0)
    assert w.charged(records, "D") == w.counter_at(1)
