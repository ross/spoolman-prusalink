"""Starting the simulation from a configuration file, which is how `python -m simulator` does."""

from __future__ import annotations

from pathlib import Path

from simulator import config, prusalink
from simulator.model import Firmware, Material, Simulation
from simulator.preview import Severity
from simulator.tests.test_config import printer, tool, write
from simulator.tests.test_model import shown

EXAMPLE = Path(__file__).parent.parent / "sim.example.toml"


def sim_from(tmp_path, text: str) -> Simulation:
    return config.build(config.load(write(tmp_path, text)))


def test_example_builds():
    sim = config.build(config.load(EXAMPLE))
    assert list(sim.printers) == ["core-one-l", "mk4s", "indx"]
    assert [p.port for p in sim.printers.values()] == [8001, 8002, 8003]
    assert sim.data_dir is not None and sim.data_dir.name == "data"
    assert sim.printers["core-one-l"].tools[0].extruded == 4310


def test_high_flow_reaches_the_printer(tmp_path):
    sim = sim_from(tmp_path, printer("high_flow = true") + printer(name="q"))
    assert sim.printers["p"].high_flow and not sim.printers["q"].high_flow


def test_tool_with_color_and_uuid_is_adhoc(tmp_path):
    text = printer(
        tool(
            "index = 0\nextruded = 4310.5\nunload_count = 2\nextruded_at_unload = 4000\n"
            'material = { name = "PETG", color = "#186BB5", '
            'instance_uuid = "7B3D2D5E-1F7A-4D8C-9A1E-2C3B4D5E6F70" }'
        )
    )
    p = sim_from(tmp_path, text).printers["p"]
    t = p.tools[0]
    assert (t.loaded, t.enabled, t.extruded, t.unload_count) == (True, True, 4310.5, 2)
    assert t.extruded_at_unload == 4000
    # A color or UUID can only come from an ad-hoc type, which also fills the ad-hoc slot.
    assert t.material == Material(
        "PETG",
        "#186bb5",
        "7b3d2d5e-1f7a-4d8c-9a1e-2c3b4d5e6f70",
        True,
        **p.model.preset_temperatures("PETG"),
    )
    assert t.previous == t.material and t.previous is not t.material
    assert (p.api_key, p.port, p.model.code) == ("sim-p", 8001, "8.1.0")


def test_bare_name_is_a_preset(tmp_path):
    p = sim_from(tmp_path, printer(tool('index = 0\nmaterial = { name = "ASA" }'))).printers["p"]
    t = p.tools[0]
    assert t.material == Material("ASA", None, None, False, **p.model.preset_temperatures("ASA"))
    assert t.loaded and t.previous is None
    assert shown(p) == {"name": "ASA"}


def test_tool_without_material_is_empty(tmp_path):
    t = sim_from(tmp_path, printer()).printers["p"].tools[0]
    assert (t.loaded, t.material, t.previous, t.extruded) == (False, None, None, 0.0)


def test_firmware_defaults_by_model(tmp_path):
    full = sim_from(tmp_path, printer(model="8.1.0")).printers["p"]
    assert full.firmware is Firmware.FULL
    assert "boot_id" in prusalink.filament(full)
    old = sim_from(tmp_path, printer(model="1.3.5")).printers["p"]
    assert old.firmware is Firmware.NO_COUNTERS
    assert "boot_id" not in prusalink.filament(old)
    forced = sim_from(tmp_path, printer('firmware = "no_endpoint"')).printers["p"]
    assert forced.firmware is Firmware.NO_ENDPOINT
    assert prusalink.filament(forced) is None


def test_selected_tool_is_the_first_enabled(tmp_path):
    tools = tool("index = 0\nenabled = false") + tool("index = 2") + tool("index = 1")
    p = sim_from(tmp_path, printer(tools, model="3.1.0")).printers["p"]
    assert p.selected_tool == 1
    assert p.tools[0].enabled is False and p.tools[1].enabled is True
    none = tool("index = 3\nenabled = false") + tool("index = 1\nenabled = false")
    p = sim_from(tmp_path, printer(none, model="3.1.0")).printers["p"]
    assert p.selected_tool == 1


def test_settings_and_per_printer_options(tmp_path):
    text = "unload_seconds = 1.5\n" + printer('checks = "abort"\nport = 9100')
    sim = sim_from(tmp_path, text)
    assert sim.settings.unload_seconds == 1.5
    assert sim.printers["p"].checks is Severity.ABORT
    assert sim.printers["p"].port == 9100
