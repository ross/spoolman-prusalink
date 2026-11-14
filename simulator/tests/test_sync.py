"""The sync job's G-code interpreter: what each M865 and M865.1 does to the printer."""

from __future__ import annotations

from dataclasses import replace

import pytest

from simulator import gcode, sync
from simulator.model import Material, SimTool
from simulator.tests.test_model import PETG, temperatures, tool0
from simulator.tests.test_model_edges import TAG, messages
from simulator.tests.util import PLA, loaded, make, make_mmu

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
    sync.apply(p, gcode.parse('M865 S"PETG" O"#ffffff" T250 L0'))
    assert tool0(p).material == Material("PETG", adhoc=False, **p.model.preset_temperatures("PETG"))


def test_preset_ignores_edits_and_adhoc_write_edits_the_slot():
    uuid = "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"
    sim, p = make()
    sync.apply(p, gcode.parse('M865 S"PETG" L0'))
    assert tool0(p).previous == PLA  # a preset write leaves the ad-hoc slot alone
    sync.apply(p, gcode.parse(f'M865.1 I0 C"{uuid}"\nM865 I0 O"#ffffff" L0'))
    assert tool0(p).material == Material("PETG", adhoc=False, **p.model.preset_temperatures("PETG"))
    sync.apply(p, gcode.parse('M865 X O"#000000" L0'))  # the slot is the base, not the preset
    assert tool0(p).material == replace(PLA, color="#000000")
    sync.apply(p, gcode.parse('M865 I0 O"#111111"'))  # an ad-hoc type takes an edit
    assert tool0(p).material.color == "#101010"  # stored in 5 bits a channel (F25)


def test_m865_writes_an_unloaded_tool():
    sim, p = make(tools=[SimTool(0)])
    sync.apply(p, gcode.parse('M865 X R N"PLA" L0'))
    assert tool0(p).material == Material("PLA", **RESET) and not tool0(p).path.loaded


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
    sync.apply(p, gcode.parse('M865 S"PETG" T250 L0'))  # a preset write ignores them
    assert temperatures(p) == (230, 170, 85)


def test_unload_of_an_empty_type_is_not_counted():
    sim, p = make(tools=[SimTool(0)])
    sync.apply(p, gcode.parse("M865 I1 L0"))  # copies "none" over nothing
    assert tool0(p).unload_count == 0
    assert messages(sim, "unload") == ["T0 M865 copied empty T1: no type stored, not counted"]


def test_m865_for_a_tool_the_model_lacks_is_dropped():
    sim, p = make("4.1.0")  # no MMU build, so one tool
    sync.apply(p, gcode.parse('M865 X R N"PLA" L4'))
    assert messages(sim) == ["M865: no tool loaded"]
    assert list(p.tools) == [0]


def test_m865_with_no_type_picked_changes_nothing():
    sim, p = make(tools=[SimTool(0)])
    before = p.to_json()
    sync.apply(p, gcode.parse('M865 L0\nM865 O"#000000" L0\nM865 S"NOSUCH" L0'))
    assert messages(sim) == ["M865 failed: Filament type invalid or not specified."] * 3
    assert p.to_json() == before


def test_m865_takes_a_name_only_on_an_ad_hoc_type():
    sim, p = make("8.1.0", [loaded(0), SimTool(1)])
    sync.apply(p, gcode.parse('M865 S"PETG" N"MINE" L0\nM865 I1 N"MINE" L0\nM865 X N"a b" L0'))
    assert messages(sim) == [
        "M865 failed: Filament is not customizable",
        "M865 failed: Filament is not customizable",
        "M865 failed: Name must contain only 'a-zA-Z0-9_-' characters",
    ]
    assert tool0(p).material == PLA
    sync.apply(p, gcode.parse('M865 I0 N"MINE"'))  # tool 0's type is ad hoc
    assert tool0(p).material == replace(PLA, name="MINE")


def test_m865_without_l_loads_nothing():
    sim, p = make()
    sync.apply(p, gcode.parse('M865 X R N"PETG"\nM865 S"PETG"'))
    assert tool0(p).material == PLA
    assert messages(sim) == ["M865: no tool loaded"] * 2


def test_a_rejected_line_is_logged():
    sim, p = make()
    sync.apply(p, gcode.parse('M865 X N"PLA L0'))
    assert messages(sim) == ['dropped: M865 X N"PLA L0 (unterminated quotes)']


def test_m865_1_reads_uuids_only_on_a_tag_build():
    sim, p = make("1.4.1", [loaded(0, replace(PLA, instance_uuid=None))])
    sync.apply(p, gcode.parse(f'M865.1 I0 C"{TAG}"'))
    assert tool0(p).material.instance_uuid is None
    sim, p = make()
    sync.apply(p, gcode.parse(f'M865.1 I0 A"{TAG}" B"{TAG}" C"{TAG}" L0'))
    m = tool0(p).material
    assert (m.material_uuid, m.package_uuid, m.instance_uuid) == (TAG, TAG, TAG)


def test_stuck_write_ignores_an_edit_of_the_slot():
    sim, p = make()
    p.set_stuck_write(0, True)
    sync.apply(p, gcode.parse('M865 I0 O"#000000"'))
    assert tool0(p).material == PLA and messages(sim)[-1] == "M865 T0 ignored: stuck write"


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
    p.model = replace(p.model, uuids=True, max_tools=2)
    sync.apply(p, gcode.parse("M865 I0 L1"))
    copy = p.tools[1].material
    assert (copy.name, copy.instance_uuid, copy.material_uuid, copy.package_uuid) == (
        "PETG",
        None,
        None,
        None,
    )


@pytest.mark.parametrize("on", [True, False])
def test_m865_writes_slot_4_on_an_mk4s_with_the_mmu_on_or_off(on):
    sim, p = make_mmu()
    if not on:
        p.set_mmu(False)
    sync.apply(p, gcode.parse('M865 S"PETG" L4\nM865 S"PETG" L5'))
    assert p.tools[4].material.name == "PETG" and 5 not in p.tools


@pytest.mark.parametrize(("code", "kept"), [("7.1.0", True), ("1.4.1", False)])
def test_mmu_uuids_land_on_slot_0_of_a_core_one_only(code, kept):
    sim, p = make_mmu(code)
    sync.apply(p, gcode.parse(f'M865 X R N"PLA" L0\nM865 X R N"PLA" L1\nM865.1 I0 C"{TAG}"'))
    sync.apply(p, gcode.parse(f'M865.1 I1 C"{TAG}"'))
    assert p.tools[0].material.instance_uuid == (TAG if kept else None)
    assert p.tools[1].material.instance_uuid is None
