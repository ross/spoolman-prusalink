"""Printer state and the counters: docs/scenarios.md rows C1 to C9."""

from __future__ import annotations

import logging

import pytest

from integration.conftest import (
    ASA,
    LOAD_MM,
    PLA,
    World,
    charges,
    count,
    events,
    logged,
    print_spec,
)
from simulator.model import State


async def baseline(w: World) -> None:
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    assert charges(records) == []


def messages(caplog: pytest.LogCaptureFixture, text: str, level: int | None = None) -> list[str]:
    """The bridge's log lines that contain `text`, at `level` if given."""
    return [
        r.getMessage()
        for r in caplog.records
        if text in r.getMessage() and (level is None or r.levelno == level)
    ]


async def test_c1_reboot_keeps_usage_held_at_the_reboot(world: World):
    """Reboot between prints: usage held then is kept, and goes to the next active spool."""
    w = world
    b = w.spools["B"]
    await baseline(w)
    # Leave the tool with no active spool: A moves out, then the printer unloads it (S10).
    await w.move("A", w.shelf)
    await w.unload()
    records = await w.poll()
    assert [e["event"] for e in events(records)][-1] == "active_unassigned"

    # A load with no spool assigned is held, by not charging it.
    await w.swap_cycle(source="preset", name=ASA[0])
    records = await w.poll()
    assert charges(records) == []

    # The reboot restarts the counter and keeps the type; the held load is kept.
    w.printer.reboot()
    assert w.counter == 0 and w.material() == (ASA[0], None, None)
    records = await w.poll()
    reboots = events(records, "reboot")
    assert len(reboots) == 1
    assert reboots[0]["message"].endswith(f"; {LOAD_MM} mm held before the restart is kept")
    assert charges(records) == []

    # An unload with no spool active first: the held load is reported lost, as in S6.
    await w.unload()
    records = await w.poll()
    assert [e["message"] for e in events(records, "lost_unassigned")] == [
        f"{LOAD_MM} mm before unload had no spool"
    ]
    assert charges(records) == []

    # Held again through another reboot, then B is assigned: B takes it, logged then.
    await w.swap_cycle(source="preset", name=ASA[0])
    await w.poll()
    w.printer.reboot()
    records = await w.poll()
    assert len(events(records, "reboot")) == 1 and charges(records) == []
    await w.move("B", w.tool_location)
    w.printer.extrude(0, 10)
    records = await w.poll()
    assert [e["message"] for e in events(records, "held_charged")] == [
        f"spool {b} is active; charged it {LOAD_MM} mm held before the printer restarted"
    ]
    assert [(r["use_length"], r["reason"]) for r in charges(records)] == [
        (LOAD_MM, "held before the printer restarted"),
        (10, "extruded"),
    ]
    assert w.charged(w.since(0), "A") == 0


async def test_c1_usage_held_over_a_reboot_joins_the_open_swap(world: World):
    """Usage held over a reboot with a swap open: charged to the old spool, moved with it."""
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    # A sensor fault reads as an unload, and the tool shows no filament: the swap opens
    # on A, and the next 20 mm is held.
    w.printer.set_sensor_fault(0, True)
    w.advance(0.1)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert [e["event"] for e in events(records)] == ["unload", "swap_unconfirmed"]
    assert charges(records) == []

    # A reboot with the fault still on keeps the 20 mm, and A, still active, takes it.
    w.printer.reboot()
    assert w.material() == (None, None, None)
    records = await w.poll()
    reboots = events(records, "reboot")
    assert len(reboots) == 1 and reboots[0]["message"].endswith(
        "; 20 mm held before the restart is kept"
    )
    assert [e["message"] for e in events(records, "held_charged")] == [
        f"spool {a} is active; charged it 20 mm held before the printer restarted"
    ]
    assert [(r["spool_id"], r["use_length"]) for r in charges(records)] == [(a, 20.0)]
    tool = w.bridge.printers[0].tools[0]
    assert tool.swap is not None and tool.swap.spool_id == a and tool.swap.mm == 20
    assert tool.held_mm == 0

    # Another reboot with the fault fixed, then B moved in: confirming the swap moves
    # the 20 mm to B with the rest of the usage since the unload.
    w.printer.set_sensor_fault(0, False)
    w.printer.reboot()
    records = await w.poll()
    assert len(events(records, "reboot")) == 1 and charges(records) == []
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert [e["message"] for e in events(records, "swap_confirmed")] == [
        f"swap confirmed: 20 mm moved from spool {a} to {b}"
    ]
    assert [(r["spool_id"], r["use_length"]) for r in charges(records)] == [(b, 20.0), (a, -20.0)]
    assert w.charged(w.since(0), "A") == 0


async def test_c1_usage_held_over_a_reboot_joins_the_provisional_charge(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Usage held over a reboot with a provisional charge open: it joins that charge."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    # S4: A unloaded and reloaded with no Spoolman change, then a print charges it
    # provisionally.
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    unloaded = w.mark()
    at_unload = w.tool.extruded_at_unload
    await w.swap_cycle(poll_every=0.3, source="tag", name=PLA[0], color=PLA[1], uuid=w.uuids["A"])
    await w.poll()
    await w.run_until(lambda: w.printer.job is None)
    records = await w.start_print(print_spec())
    assert len(events(records, "swap_assumed")) == 1
    tool = w.bridge.printers[0].tools[0]
    assert tool.provisional is not None and tool.provisional.spool_id == a

    # The sensor fails mid-print: the tool reports no filament, which isn't an unload
    # while printing, so the usage is held. Then the printer reboots, dropping the print.
    # The load above held its usage for a poll too, so count from here.
    hold = f"[{w.tool_location}] holding usage until filament is reported"
    hold_ended = f"[{w.tool_location}] no longer: holding usage until filament is reported"
    holds, ends = count(caplog, logging.WARNING, hold), count(caplog, logging.WARNING, hold_ended)
    w.printer.set_sensor_fault(0, True)
    w.advance(0.5)
    records = await w.poll()
    assert charges(records) == [] and events(records, "unload") == []
    w.advance(0.5)
    records = await w.poll()
    assert charges(records) == []
    tool = w.bridge.printers[0].tools[0]
    held = w.counter - tool.charged
    assert held > 0
    before_reboot = w.counter
    assert count(caplog, logging.WARNING, hold) == holds + 1

    w.printer.set_sensor_fault(0, False)
    w.printer.reboot()
    records = await w.poll()
    reboots = events(records, "reboot")
    assert len(reboots) == 1
    assert reboots[0]["message"].endswith(f"; {held} mm held before the restart is kept")
    assert [e["message"] for e in events(records, "held_charged")] == [
        f"spool {a} is active; charged it {held} mm held before the printer restarted"
    ]
    assert [(r["spool_id"], r["use_length"]) for r in charges(records)] == [(a, held)]
    tool = w.bridge.printers[0].tools[0]
    provisional = before_reboot - at_unload
    assert tool.provisional is not None and tool.provisional.spool_id == a
    assert tool.provisional.mm == provisional
    assert tool.held_mm == 0
    flag = await w.flag("A")
    assert flag is not None and f" {provisional} mm (≈ " in flag
    assert count(caplog, logging.WARNING, hold) == holds + 1
    assert count(caplog, logging.WARNING, hold_ended) == ends + 1

    # The next unload keeps all of it on A, held part included; B was never involved.
    w.printer.extrude(0, 10)
    await w.unload()
    records = await w.poll()
    assert [e["message"] for e in events(records, "provisional_kept")] == [
        f"unload keeps the {provisional + 10} mm charged provisionally to spool {a}"
    ]
    since_unload = w.since(unloaded)
    assert all(c["spool_id"] == a and c["use_length"] > 0 for c in charges(since_unload))
    assert w.charged(since_unload, "A") == provisional + 10
    assert w.charged(w.since(0), "B") == 0
    assert w.bridge.printers[0].tools[0].provisional is None
    assert b not in w.bridge.state.runouts


async def test_c2_printer_off_for_hours(world: World, caplog: pytest.LogCaptureFixture):
    """Printer off for hours: logged going, hourly and back, idle polls, nothing charged."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    # The outage clock runs on simulated time, offset so that it starts large, as a
    # monotonic clock does: a reminder measured from zero would show up here too.
    w.bridge.printers[0]._printer_reach.clock = lambda: w.sim.now + 1e6
    await baseline(w)
    w.printer.extrude(0, 10)
    records = await w.poll()
    assert w.charged(records, "A") == 10

    # Some use the bridge never sees, then the power goes for two and a half hours.
    w.printer.extrude(0, 20)
    w.printer.set_power("off")
    interval = w.bridge.config.poll_idle
    assert interval == 60
    mark = w.mark()
    for _ in range(int(2.5 * 3600 / interval)):
        await w.bridge.poll()
        assert w.bridge.interval() == interval
        w.advance(interval)
    assert w.since(mark) == []
    assert len(messages(caplog, "printer unreachable:", logging.WARNING)) == 1
    # A reminder at the first poll an hour or more after the last line.
    reminders = messages(caplog, "printer still unreachable after", logging.INFO)
    assert [m.split("after ")[1][:3] for m in reminders] == ["1h ", "2h "]

    # Back on: a reboot, so the unseen 20 mm is lost and the counter starts again.
    w.printer.set_power("on")
    w.printer.extrude(0, 30)
    records = await w.poll()
    assert len(events(records, "reboot")) == 1
    assert w.charged(records, "A") == 30
    back = messages(caplog, "printer reachable again after", logging.WARNING)
    assert len(back) == 1 and back[0].endswith("after 2h 30m")
    assert w.bridge.interval() == interval
    await w.poll()
    assert len(messages(caplog, "reachable again")) == 1


async def test_c3_reboot_and_unload_in_one_poll(world: World):
    """Reboot and an unload in the same poll: the reboot is handled first, then the unload."""
    w = world
    a = w.spools["A"]
    await baseline(w)
    # An unload and a reload first, so the printer's unload_count is 1 before the reboot
    # and 0 after it: the reboot has to reset the bridge's count for the unload to show.
    await w.unload()
    await w.swap_cycle(source="tag", name=PLA[0], color=PLA[1], uuid=w.uuids["A"])
    w.printer.extrude(0, 10)
    await w.poll()
    assert w.tool.unload_count == 1

    w.printer.reboot()
    w.printer.extrude(0, 15)
    await w.unload()
    records = await w.poll()
    assert [e["event"] for e in events(records)] == ["reboot", "unload", "swap_unconfirmed"]
    unload = events(records, "unload")[0]
    assert unload["message"] == f"1 unload(s); 15 mm before the unload goes to spool {a}"
    assert [(r["use_length"], r["reason"]) for r in charges(records)] == [(15, "before unload")]


async def test_c4_counter_goes_backward(world: World):
    """Counter goes backward within one boot: an anomaly, no charge, and later use charged."""
    w = world
    await baseline(w)
    w.printer.extrude(0, 50)
    await w.poll()

    w.printer.counter_step_back(0, 30)
    records = await w.poll()
    anomalies = events(records, "counter_anomaly")
    assert [e["message"] for e in anomalies] == [
        f"extruded {w.counter} < charged {w.counter + 30} within one boot; resyncing"
    ]
    assert charges(records) == []

    w.printer.extrude(0, 40)
    records = await w.poll()
    assert events(records) == []
    assert w.charged(records, "A") == 40


async def test_c5_extruded_at_unload_out_of_range(world: World):
    """`extruded_at_unload` past the counter: clamped to it, an anomaly, the unload charged."""
    w = world
    await baseline(w)
    w.printer.extrude(0, 50)
    await w.poll()

    w.printer.extrude(0, 30)
    await w.unload()
    w.printer.set_extruded_at_unload(0, w.counter + 500)
    records = await w.poll()
    assert [e["event"] for e in events(records)] == [
        "counter_anomaly",
        "unload",
        "swap_unconfirmed",
    ]
    anomaly = events(records, "counter_anomaly")[0]["message"]
    assert f"extruded_at_unload={w.counter + 500}" in anomaly
    # Clamped to the counter: everything since the last poll is before the unload.
    assert [(r["use_length"], r["reason"]) for r in charges(records)] == [(30, "before unload")]


async def test_c6_manual_extrude_and_retract(world: World):
    """Extrude and retract from the menu: extrusion charged, a retraction charges nothing."""
    w = world
    await baseline(w)
    w.printer.extrude(0, 25)
    records = await w.poll()
    assert [(r["use_length"], r["reason"]) for r in charges(records)] == [(25, "extruded")]

    counter = w.counter
    w.printer.retract(0, 10)
    assert w.counter == counter
    records = await w.poll()
    assert events(records) == [] and charges(records) == []

    # Feeding refills the retraction first: only the 5 mm past it is new filament.
    w.printer.extrude(0, 15)
    records = await w.poll()
    assert [(r["use_length"], r["reason"]) for r in charges(records)] == [(5, "extruded")]
    assert all(r["use_length"] > 0 for r in charges(w.since(0)))


async def test_c7_tool_reports_no_filament(world: World):
    """Tool reports no filament after a sensor fault: usage held until filament is back."""
    w = world
    a = w.spools["A"]
    await baseline(w)

    # The sensor reads empty, so the idle clear counts an unload with the filament in.
    w.printer.set_sensor_fault(0, True)
    w.advance(0.1)
    assert w.tool.loaded and w.material() == (None, None, None)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert [e["event"] for e in events(records)] == ["unload", "swap_unconfirmed"]
    assert charges(records) == []
    w.printer.extrude(0, 15)
    records = await w.poll()
    assert charges(records) == []

    # The sensor is fixed and the filament reloaded: the held 35 mm goes to A with it.
    w.printer.set_sensor_fault(0, False)
    w.advance(0.1)
    assert w.printer.job is None
    w.printer.unload(0)
    await w.run_until(lambda: w.tool.process is None)
    records = await w.swap_cycle(source="tag", name="PLA", color="#73737b", uuid=w.uuids["A"])
    records += await w.poll()
    assert [(r["spool_id"], r["use_length"]) for r in charges(records)] == [(a, 35 + LOAD_MM)]
    assert w.printer.state is State.IDLE


async def test_c8_firmware_update(world: World, caplog: pytest.LogCaptureFixture):
    """Firmware update: a reboot, the model checked again, an unsupported version logged once."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    model = "printer is COREONEL (8.1.0)"
    await baseline(w)
    assert messages(caplog, model, logging.INFO) == [model]

    w.printer.reboot()
    w.printer.extrude(0, 10)
    records = await w.poll()
    assert len(events(records, "reboot")) == 1
    assert w.charged(records, "A") == 10
    assert messages(caplog, model, logging.INFO) == [model, model]

    # Updated to firmware without the filament endpoint: logged once, nothing charged.
    w.printer.set_firmware("no_endpoint")
    w.printer.reboot()
    w.printer.extrude(0, 20)
    mark = w.mark()
    for _ in range(3):
        await w.poll()
    assert w.since(mark) == []
    unsupported = "this firmware isn't supported, so nothing is charged"
    assert len(messages(caplog, unsupported, logging.ERROR)) == 1

    # Updated again, with it: resumes after a reboot, and the model is checked again.
    w.printer.set_firmware("full")
    w.printer.reboot()
    w.printer.extrude(0, 5)
    records = await w.poll()
    assert messages(caplog, "printer now reports filament; resuming") != []
    assert len(events(records, "reboot")) == 1
    assert w.charged(records, "A") == 5
    assert messages(caplog, model, logging.INFO) == [model, model, model]


async def test_c9_printer_answers_with_an_error(world: World, caplog: pytest.LogCaptureFixture):
    """Printer answers 401 then 500: each logged once, nothing charged, then caught up."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    await baseline(w)
    failed = "printer read failed, skipping poll"

    w.printer.set_fault("bad_key")
    w.printer.extrude(0, 20)
    mark = w.mark()
    for _ in range(3):
        await w.poll()
    assert len(messages(caplog, failed, logging.WARNING)) == 1
    assert "401 Unauthorized" in messages(caplog, failed)[0]

    # A different error is logged again, once.
    w.printer.set_fault("error")
    w.printer.extrude(0, 10)
    for _ in range(3):
        await w.poll()
    assert len(messages(caplog, failed, logging.WARNING)) == 2
    assert "500 Internal Server Error" in messages(caplog, failed)[1]
    assert w.since(mark) == []
    assert not logged(caplog, logging.WARNING, "unreachable")

    # Cleared: logged once, at the failure's level, and the next poll catches up on
    # everything since.
    w.printer.set_fault("none")
    records = await w.poll()
    records += await w.poll()
    back = messages(caplog, "printer reads working again", logging.WARNING)
    assert [m.split(" after ")[0] for m in back] == ["printer reads working again"]
    assert events(records) == []
    assert [(r["use_length"], r["reason"]) for r in charges(records)] == [(30, "extruded")]
