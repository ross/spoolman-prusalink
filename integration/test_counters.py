"""Printer state and the counters: docs/scenarios.md rows C1 to C9."""

from __future__ import annotations

import logging

import pytest

from integration.conftest import ASA, LOAD_MM, World, charges, events, logged
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


async def test_c2_printer_off_for_hours(world: World, caplog: pytest.LogCaptureFixture):
    """Printer off for hours: logged going, hourly and back, idle polls, nothing charged."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    w.bridge.printers[0]._printer_outage.clock = lambda: w.sim.now
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
    back = messages(caplog, "printer reachable again after", logging.INFO)
    assert len(back) == 1 and back[0].endswith("after 2h 30m")
    assert w.bridge.interval() == interval
    await w.poll()
    assert len(messages(caplog, "reachable again")) == 1


async def test_c3_reboot_and_unload_in_one_poll(world: World):
    """Reboot and an unload in the same poll: the reboot is handled first, then the unload."""
    w = world
    a = w.spools["A"]
    await baseline(w)
    w.printer.extrude(0, 10)
    await w.poll()

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

    # Cleared: logged once, and the next poll catches up on everything since.
    w.printer.set_fault("none")
    records = await w.poll()
    records += await w.poll()
    assert messages(caplog, "printer reads working again", logging.INFO) == [
        "printer reads working again"
    ]
    assert events(records) == []
    assert [(r["use_length"], r["reason"]) for r in charges(records)] == [(30, "extruded")]
