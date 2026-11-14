"""The bridge and the simulator each hold a copy of the firmware's preset filament table.

The copies are separate at runtime on purpose: neither imports the other. Only tests
cross the line, and this one keeps the two in step.
"""

from dataclasses import fields

import pytest

from simulator.model import MODELS
from simulator.tool import DEFAULT_TEMPERATURES
from simulator.tool import PRESET_ALIASES as SIM_ALIASES
from spoolman_prusalink.printer_sync import (
    DEFAULT_PREHEAT,
    HT_IDLER_DOOR,
    PRESETS,
    PRINTER_MODELS,
    Preset,
)
from spoolman_prusalink.printer_sync import PRESET_ALIASES as BRIDGE_ALIASES

# The bridge holds one table for every model it writes to; the simulator holds each
# model's build's.
SUPPORTED = sorted(c for c, m in PRINTER_MODELS.items() if m.unsupported is None)


@pytest.mark.parametrize("code", SUPPORTED)
def test_temperatures_agree_with_each_supported_model(code):
    bridge = {m: (p.nozzle, p.preheat, p.bed) for m, p in PRESETS.items()}
    # The bridge leaves out the presets that need the HT idler door, which M865 can't set.
    simulator = {m: t for m, t in MODELS[code].presets.items() if m not in HT_IDLER_DOOR}
    assert bridge == simulator


def test_only_the_door_presets_are_left_out():
    left_out = {m for c in SUPPORTED for m in MODELS[c].presets} - set(PRESETS)
    assert left_out == HT_IDLER_DOOR == {"PPS", "PPA"}


def test_reset_defaults_agree():
    default_preheat = next(f.default for f in fields(Preset) if f.name == "preheat")
    assert DEFAULT_TEMPERATURES[1] == DEFAULT_PREHEAT
    assert default_preheat == DEFAULT_PREHEAT


def test_aliases_agree():
    assert BRIDGE_ALIASES == SIM_ALIASES
