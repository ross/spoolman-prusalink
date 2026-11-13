"""The bridge and the simulator each hold a copy of the firmware's preset filament table.

The copies are separate at runtime on purpose: neither imports the other. Only tests
cross the line, and this one keeps the two in step.
"""

from dataclasses import fields

from simulator.tool import DEFAULT_TEMPERATURES, PRESET_TEMPERATURES
from spoolman_prusalink.printer_sync import DEFAULT_PREHEAT, HT_IDLER_DOOR, PRESETS, Preset


def test_temperatures_agree():
    shared = PRESETS.keys() & PRESET_TEMPERATURES.keys()
    bridge = {m: (PRESETS[m].nozzle, PRESETS[m].preheat, PRESETS[m].bed) for m in shared}
    simulator = {m: PRESET_TEMPERATURES[m] for m in shared}
    assert bridge == simulator


def test_same_materials():
    # The bridge leaves out the presets that need the HT idler door, which M865 can't set.
    assert set(PRESET_TEMPERATURES) - set(PRESETS) == {"PPS", "PPA"}
    assert set(PRESET_TEMPERATURES) - set(PRESETS) == HT_IDLER_DOOR
    assert set(PRESETS) - set(PRESET_TEMPERATURES) == set()


def test_reset_defaults_agree():
    default_preheat = next(f.default for f in fields(Preset) if f.name == "preheat")
    assert DEFAULT_TEMPERATURES[1] == DEFAULT_PREHEAT
    assert default_preheat == DEFAULT_PREHEAT
