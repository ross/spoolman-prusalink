"""The sync job's G-code interpreter: what each M865 and M865.1 does to the printer."""

from __future__ import annotations

from dataclasses import replace

from simulator import gcode, sync
from simulator.model import Material, SimTool, preset_temperatures
from simulator.tests.test_model import PETG, temperatures, tool0
from simulator.tests.test_model_edges import TAG, messages
from simulator.tests.util import PLA, loaded, make

# What M865 R resets the temperatures to.
RESET = {"nozzle": 215, "preheat": 170, "bed": 60}


def test_m865_reset_clears_uuid_and_preset_write_ignores_parameters():
    sim, p = make()
    sync.apply(p, gcode.parse('M865 X R N"ASA" O"#de3129" L0'))
    assert tool0(p).material == Material("ASA", "#de3129", **RESET)
    sync.apply(p, gcode.parse('M865.1 I0 C"8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"'))
    assert tool0(p).material.instance_uuid
    sync.apply(p, gcode.parse('M865 X O"#000000" L0'))  # ad-hoc edit keeps the UUID
    assert tool0(p).material.instance_uuid and tool0(p).material.color == "#000000"
    sync.apply(p, gcode.parse('M865 N"PETG" O"#ffffff" T250 L0'))
    assert tool0(p).material == Material("PETG", adhoc=False, **preset_temperatures("PETG"))


def test_preset_ignores_edits_and_adhoc_write_edits_the_slot():
    uuid = "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"
    sim, p = make()
    sync.apply(p, gcode.parse('M865 N"PETG" L0'))
    assert tool0(p).previous == PLA  # a preset write leaves the ad-hoc slot alone
    sync.apply(p, gcode.parse(f'M865.1 I0 C"{uuid}"\nM865 O"#ffffff" L0'))
    assert tool0(p).material == Material("PETG", adhoc=False, **preset_temperatures("PETG"))
    assert "ignored on preset" in sim.events[-1].text
    sync.apply(p, gcode.parse('M865 X O"#000000" L0'))  # the slot is the base, not the preset
    assert tool0(p).material == replace(PLA, color="#000000")
    sync.apply(p, gcode.parse('M865 O"#111111" L0'))  # an ad-hoc type takes an edit
    assert tool0(p).material.color == "#101010"  # stored in 5 bits a channel (F25)


def test_m865_writes_an_unloaded_tool():
    sim, p = make(tools=[SimTool(0)])
    sync.apply(p, gcode.parse('M865 X R N"PLA" L0'))
    assert tool0(p).material == Material("PLA", **RESET) and not tool0(p).loaded


def test_m865_1_only_on_tool_0_of_uuids_model():
    uuid = "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"
    sim, p = make("8.10.0", [loaded(0, PETG), loaded(1, PETG)])
    sync.apply(p, gcode.parse(f'M865.1 I0 C"{uuid}"\nM865.1 I1 C"{uuid}"'))
    assert p.tools[0].material.instance_uuid is None
    assert p.tools[1].material.instance_uuid is None
    sim, p = make(tools=[SimTool(0)])
    sync.apply(p, gcode.parse(f'M865.1 I0 C"{uuid}"'))
    assert tool0(p).material is None  # nothing stored to attach it to


def test_m865_on_other_tool_and_copy_from_empty_tool():
    sim, p = make("8.10.0", [loaded(0, PETG), loaded(1, PETG), SimTool(2)])
    sync.apply(p, gcode.parse('M865 X R N"PLA" O"#73737b" L1'))
    assert p.tools[1].material == Material("PLA", "#73737b", **RESET)
    assert p.selected_tool == 0
    sync.apply(p, gcode.parse("M865 I2 L1"))  # copies "none": an unload of T1
    assert p.tools[1].material is None and p.tools[1].unload_count == 1
    sync.apply(p, gcode.parse("M865 I0 L2"))
    assert p.tools[2].material == PETG


def test_m865_temperatures():
    sim, p = make()
    # R resets to the firmware defaults, not the PC preset's preheat.
    sync.apply(p, gcode.parse('M865 X R N"PC" T260 B90 L0'))
    assert temperatures(p) == (260, 170, 90)
    sync.apply(p, gcode.parse("M865 X P200 L0"))  # an ad-hoc edit keeps the rest
    assert temperatures(p) == (260, 200, 90)
    sync.apply(p, gcode.parse('M865 N"PETG" T250 L0'))  # a preset write ignores them
    assert temperatures(p) == (230, 170, 85)


def test_unload_of_an_empty_type_is_not_counted():
    sim, p = make(tools=[SimTool(0)])
    sync.apply(p, gcode.parse("M865 I1 L0"))  # copies "none" over nothing
    assert tool0(p).unload_count == 0
    assert messages(sim, "unload") == ["T0 M865 copied empty T1: no type stored, not counted"]


def test_m865_for_a_tool_the_model_lacks_is_dropped():
    sim, p = make()
    sync.apply(p, gcode.parse('M865 X R N"PLA" L4'))
    assert messages(sim) == ["M865 L4 dropped: no such tool"]
    assert list(p.tools) == [0]


def test_m865_with_nothing_to_write_changes_nothing():
    sim, p = make(tools=[SimTool(0)])
    before = p.to_json()
    sync.apply(p, gcode.parse("M865 L0"))
    assert messages(sim) == ["M865 L0: nothing to write"]
    sync.apply(p, gcode.parse('M865 O"#000000" L0'))  # an edit needs a stored type
    assert messages(sim)[-1] == "M865 L0: nothing to write"
    assert p.to_json() == before


def test_m865_copy_onto_tool_0_of_a_tag_model_keeps_the_uuids():
    sim, p = make("8.1.0", [SimTool(0)])
    sync.apply(p, gcode.parse('M865 X R N"PETG" O"#186bb5" L0'))
    sync.apply(p, gcode.parse(f'M865.1 I0 C"{TAG}"'))
    uuids = tool0(p).material.instance_uuid
    assert uuids == TAG
    sync.apply(p, gcode.parse("M865 I0 L0"))
    assert tool0(p).material.instance_uuid == TAG
    assert "copied from T0" in messages(sim)[-1]


def test_m865_copy_onto_another_tool_drops_the_uuids():
    tagged = Material("PETG", "#186bb5", TAG, True, material_uuid=TAG, package_uuid=TAG)
    sim, p = make("1.4.1", [loaded(0, tagged)])
    p.tools[1] = SimTool(1, enabled=False)
    # Only tool 0 holds UUIDs.
    p.model = type(p.model)("x", "X", p.model.extruder, uuids=True, max_tools=2)
    sync.apply(p, gcode.parse("M865 I0 L1"))
    copy = p.tools[1].material
    assert (copy.name, copy.instance_uuid, copy.material_uuid, copy.package_uuid) == (
        "PETG",
        None,
        None,
        None,
    )
