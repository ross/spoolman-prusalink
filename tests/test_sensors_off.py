"""The printer's filament sensors setting (BF25, F47): the bridge warns while it's off, on
firmware that serves `sensors_enabled`, and the printing-with-no-filament warning (C7)
says what the setting was."""

import logging

from harness import end_job, fill, levels, make, restart, spool, start_job

from simulator.prusalink import Answer, filament

OFF = "filament sensors are off, so the printer reports no runout and no unload"
HINT = "printing with no filament reported; that usage is charged to no spool."


def without_the_field(station):
    """A `before_request` hook that serves the filament without `sensors_enabled`, as
    builds of the fork before `2ef028181` do."""

    def hook(method, path):
        if path != "/api/v1/filament":
            return None
        data = filament(station.printer)
        del data["sensors_enabled"]
        return Answer(200, data)

    return hook


def messages(caplog, text):
    return [(r.levelname, r.getMessage()) for r in caplog.records if text in r.getMessage()]


async def test_the_sensors_off_warning_is_logged_once_and_ends_once(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    bridge, fake, station = make(tmp_path, [spool(19)])
    await bridge.poll()
    assert messages(caplog, OFF) == []

    station.printer.set_sensors(False)
    for _ in range(3):
        await bridge.poll()
    assert messages(caplog, OFF) == [
        (
            "WARNING",
            "printer: filament sensors are off, so the printer reports no runout and no "
            "unload: filament pulled out or swapped by hand goes unnoticed, and the old "
            "spool keeps being charged. Turn them back on in the printer's settings",
        )
    ]
    # It's the printer's, not a tool's: no tool condition, and no flag.
    assert bridge.printers[0].conditions == {}
    assert fake.flag(19) is None

    station.printer.set_sensors(True)
    await bridge.poll()
    await bridge.poll()
    assert levels(caplog, OFF) == ["WARNING", "WARNING"]
    assert levels(caplog, f"printer: no longer: {OFF}") == ["WARNING"]
    await bridge.aclose()


async def test_a_restart_while_the_sensors_are_off_logs_the_warning_again(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = make(tmp_path, [spool(19)])
    station.printer.set_sensors(False)
    await bridge.poll()
    bridge = restart(bridge)
    await bridge.poll()
    await bridge.poll()
    assert levels(caplog, OFF) == ["WARNING", "WARNING"]
    assert levels(caplog, "no longer") == []
    await bridge.aclose()


async def test_the_warning_stands_while_the_printer_cant_be_read(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = make(tmp_path, [spool(19)])
    station.printer.set_sensors(False)
    await bridge.poll()
    station.before_request = lambda method, path: Answer(500, {"message": "oops"})
    await bridge.poll()
    station.before_request = None
    await bridge.poll()
    assert levels(caplog, OFF) == ["WARNING"]
    await bridge.aclose()


async def test_firmware_without_the_setting_raises_no_warning(tmp_path, caplog):
    """Older builds of the fork don't serve `sensors_enabled`: the bridge doesn't know the
    setting, and the hint keeps the wording that allows for it."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = make(tmp_path, [spool(19)])
    station.before_request = without_the_field(station)
    station.printer.set_sensors(False)
    await bridge.poll()  # the baseline
    start_job(station)
    fill(station.tool, None)
    station.tool.extruded += 30
    await bridge.poll()
    assert levels(caplog, OFF) == []
    assert messages(caplog, HINT)[0][1].endswith(
        "If the filament sensors were turned off for this print, turn them back on after "
        "it: while they're off, the printer reports no runout and no unload, so the bridge "
        "misses spool swaps. A power cut with the filament out does this too"
    )
    await bridge.aclose()


async def test_the_warning_ends_when_the_firmware_stops_reporting_the_setting(tmp_path, caplog):
    """Firmware without the field can't say the sensors are off, so the warning ends."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, fake, station = make(tmp_path, [spool(19)])
    station.printer.set_sensors(False)
    await bridge.poll()
    station.before_request = without_the_field(station)
    await bridge.poll()
    assert levels(caplog, f"no longer: {OFF}") == ["WARNING"]
    await bridge.aclose()


async def empty_print(tmp_path, *, sensors):
    bridge, fake, station = make(tmp_path, [spool(19)])
    station.printer.set_sensors(sensors)
    await bridge.poll()  # the baseline
    start_job(station)
    fill(station.tool, None)
    station.tool.extruded += 30
    await bridge.poll()
    return bridge, station


async def test_printing_empty_with_the_sensors_on_names_a_power_cut(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, station = await empty_print(tmp_path, sensors=True)
    assert messages(caplog, HINT) == [
        (
            "WARNING",
            "[Core One L+] printing with no filament reported; that usage is charged to no "
            "spool. The filament sensors were on when that usage was lost, so a power cut "
            "probably did this: the filament was out of the tool, or a runout was waiting "
            "for its reload, when the power failed",
        )
    ]
    await bridge.aclose()


WERE_OFF = (
    "[Core One L+] printing with no filament reported; that usage is charged to no spool. "
    "The filament sensors were off when that usage was lost: while they were off, the "
    "printer reported no runout and no unload"
)


async def test_printing_empty_with_the_sensors_off_says_so(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, station = await empty_print(tmp_path, sensors=False)
    assert [text for _, text in messages(caplog, "")] == [
        f"printer: {OFF}: filament pulled out or swapped by hand goes unnoticed, and the old "
        "spool keeps being charged. Turn them back on in the printer's settings",
        WERE_OFF,
    ]
    await bridge.aclose()


async def test_a_restart_after_the_sensors_come_back_on_logs_the_hint_as_it_was(tmp_path, caplog):
    """The hint keeps the setting from when the usage was lost, through a restart, and says
    it in the past tense, so it reads right with the sensors warning gone."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, station = await empty_print(tmp_path, sensors=False)
    station.printer.set_sensors(True)
    await bridge.poll()
    bridge = restart(bridge)
    caplog.clear()
    await bridge.poll()
    assert [text for _, text in messages(caplog, "")] == [WERE_OFF]
    await bridge.aclose()


async def test_the_sensors_turned_on_mid_print_dont_rewrite_the_hint(tmp_path, caplog):
    """The hint keeps the setting it was raised with, so turning the sensors on during the
    print ends the sensors warning and logs the hint no more."""
    caplog.set_level(logging.WARNING, logger="spoolman_prusalink")
    bridge, station = await empty_print(tmp_path, sensors=False)
    for sensors in (True, False, True):
        station.printer.set_sensors(sensors)
        station.tool.extruded += 30
        await bridge.poll()
    assert levels(caplog, HINT) == ["WARNING"]
    assert levels(caplog, OFF) == ["WARNING", "WARNING", "WARNING", "WARNING"]
    end_job(station)
    await bridge.poll()
    assert levels(caplog, f"no longer: {HINT}") == ["WARNING"]
    await bridge.aclose()
