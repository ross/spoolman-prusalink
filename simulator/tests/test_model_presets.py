"""Each model's preset filaments: its build's table, from the menu and from the sync job."""

from __future__ import annotations

import pytest

from simulator import gcode, sync
from simulator.model import Invalid, Material, SimTool
from simulator.tests.test_model import temperatures, tool0
from simulator.tests.util import make

# Nozzle, preheat and bed of PC, FLEX and PA in each build, from
# `src/common/filament_presets.cpp` at 7d8bf6858.
BUILD_PRESETS = {
    "8.1.0": {"PC": (275, 250, 100), "FLEX": (240, 210, 50), "PA": (285, 260, 100)},
    "8.10.0": {"PC": (275, 230, 100), "FLEX": (240, 170, 50), "PA": (285, 260, 100)},
    "1.4.1": {"PC": (275, 250, 100), "FLEX": (240, 210, 50), "PA": (285, 260, 100)},
    "2.1.0": {"PC": (275, 170, 100), "FLEX": (240, 170, 50), "PA": (280, 255, 100)},
}
CASES = [(code, name, t) for code, table in BUILD_PRESETS.items() for name, t in table.items()]


@pytest.mark.parametrize(("code", "name", "temps"), CASES)
def test_a_menu_load_reports_the_builds_preset(code, name, temps):
    sim, p = make(code, [SimTool(0)])
    p.load(0, source="preset", name=name)
    sim.tick(300.0)
    assert tool0(p).loaded and temperatures(p) == temps


@pytest.mark.parametrize(("code", "name", "temps"), CASES)
def test_a_preset_write_reports_the_builds_preset(code, name, temps):
    sim, p = make(code)
    sync.apply(p, gcode.parse(f'M865 N"{name}" L0'))
    assert tool0(p).material.name == name and temperatures(p) == temps


def test_a_preset_the_build_lacks():
    sim, p = make("1.4.1")
    p.unload(0)
    sim.tick(30.0)
    # The menu doesn't list PPS on an MK4S.
    with pytest.raises(Invalid, match="MK4S has no PPS preset"):
        p.load(0, source="preset", name="PPS")
    p.load(0, source="preset", name="PLA")
    sim.tick(300.0)
    # `M865 S"PPS"` doesn't parse there: it prints an error and stores nothing.
    sync.apply(p, gcode.parse('M865 N"PPS" L0'))
    assert tool0(p).material == Material("PLA", adhoc=False, nozzle=215, preheat=170, bed=60)
    assert sim.events[-1].text == "M865 L0 failed: MK4S has no PPS preset"
    # An ad-hoc type can take the name, starting from the reset defaults.
    sync.apply(p, gcode.parse('M865 X R N"PPS" T320 B105 L0'))
    assert tool0(p).material == Material("PPS", nozzle=320, preheat=170, bed=105)
