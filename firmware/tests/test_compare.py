"""Rebuilding the firmware's tables per model, and comparing them with the copies."""

from dataclasses import replace

import pytest

from firmware import compare, read
from firmware.compare import (
    MISSING,
    PRESENT,
    Build,
    CleanerPurge,
    Copies,
    Difference,
    Expected,
    Firmware,
    Pause,
)
from firmware.source import Unreadable
from firmware.tests import fixtures
from simulator import tool
from simulator.model import MODELS, ModelInfo
from simulator.tool import Extruder, Lengths
from spoolman_prusalink.printer_sync import Preset, PrinterModel

NO_COUNTERS = "no counters"
MULTI_TOOL = "multi-tool"

NEXTRUDER_AUTO_RETRACT = (
    (8, 995),
    (-43, 6000),
    (-8, 3000),
    (-4, 1800),
    (20, 600),
    (-20, 470),
    (55, 1740),
    (-55, 6000),
    (20, 340),
    (-20, 210),
)
NEXTRUDER_UNLOAD = (*NEXTRUDER_AUTO_RETRACT, (-50, 2000))
NEXTRUDER_RUNOUT = ((7, 1500), (-50, 2700), (-5, 50), (-50, 1500))
NEXTRUDER = {
    "auto_retract": NEXTRUDER_AUTO_RETRACT,
    "runout": NEXTRUDER_RUNOUT,
    "unload": NEXTRUDER_UNLOAD,
}
INDX_RAMMING = (
    (8, 1500),
    (8, 1200),
    (-20, 2100),
    (-3, 60),
    (3, 120),
    (19, 1800),
    (2, 600),
    (-20, 2100),
    (-3, 120),
    (3, 120),
    (-6, 120),
    (-8, 360),
)
INDX = {
    "auto_retract": ((8, 480), (-8, 4800), (1, 1440), (-1, 1440)),
    "runout": INDX_RAMMING,
    "runout_flexible": ((-50, 600),),
    "unload": INDX_RAMMING,
    "unload_flexible": ((-50, 600),),
}
MINI_RAMMING = ((20, 1500), (-50, 2700), (-5, 50), (-50, 1500))

NEXTRUDER_PAUSE = Pause(4, 8, 105, 30, 50, 27)
CORE_ONE_PAUSE = Pause(4, 8, 105, 40, 20, 40)
INDX_PAUSE = Pause(4, 8, 55, 15, 25, 40)
INDX_PURGE = CleanerPurge(25.0, True)
# The simulator's copy of the extruder `build()` gives every build.
EXTRUDER = Extruder((380, 400), Lengths(4, 8, 105, 30, 50, 27), NEXTRUDER)

# Per build: E steps (default, calibrated), pause lengths, ramming and the cleaner's purge.
EXTRUDERS = {
    "MK4": ((380, 400), NEXTRUDER_PAUSE, NEXTRUDER, None),
    "XL": ((380, 400), NEXTRUDER_PAUSE, NEXTRUDER, None),
    "XL_DEV_KIT": (
        (380, 400),
        NEXTRUDER_PAUSE,
        {"runout": NEXTRUDER_RUNOUT, "unload": NEXTRUDER_UNLOAD},
        None,
    ),
    "iX": ((380, 400), Pause(4, 8, 110, 40, 45, 40), NEXTRUDER, CleanerPurge(0.0, False)),
    "COREONE": ((380, 400), CORE_ONE_PAUSE, NEXTRUDER, None),
    "COREONEL": ((380, 400), CORE_ONE_PAUSE, NEXTRUDER, None),
    "COREONE_INDX": ((550, 576), INDX_PAUSE, INDX, INDX_PURGE),
    "COREONEL_INDX": ((550, 576), INDX_PAUSE, INDX, INDX_PURGE),
    "MK3.5": (
        (266, 280),
        Pause(4, 8, 105, 30, 50, 21),
        {"runout": NEXTRUDER_RUNOUT, "unload": MINI_RAMMING},
        None,
    ),
    "MINI": (
        (325, 325),
        Pause(4, 8, 400, 40, 320, 50),
        {"runout": MINI_RAMMING, "unload": MINI_RAMMING},
        None,
    ),
}


def test_load_reads_every_build():
    firmware = compare.load(fixtures.FakeSource())
    assert firmware.commit == "c0ffee"
    assert firmware.models == {"1.3.0": "MK3", "1.4.0": "MK4", "2.1.0": "MINI", "7.1.0": "COREONE"}
    assert firmware.builds.keys() == {b for b in compare.MODEL_BUILDS.values() if b}
    assert firmware.defaults == (215, 170, 60)
    assert firmware.aliases == {"TPU": "FLEX"}
    assert firmware.built().keys() == {"1.4.0", "2.1.0", "7.1.0"}  # the MK3 isn't built

    mk4, mini, xl = (firmware.builds[b] for b in ("MK4", "MINI", "XL"))
    assert (mk4.counters, mk4.uuids, mk4.tools, mk4.gap_mm) == (True, False, 1, 40.0)
    assert (mini.counters, mini.tools, mini.gap_mm) == (False, 1, None)
    assert xl.tools == 5
    indx = firmware.builds["COREONEL_INDX"]
    assert (indx.tools, indx.gap_mm) == (8, 27.0)
    assert firmware.builds["COREONE"].uuids
    assert firmware.builds["COREONEL"].presets["PPS"]["chamber_min_temperature"] == 55
    assert "PPS" not in mk4.presets
    # PRINTER_IS_PRUSA_MINI is true on the MINI build only.
    assert mini.presets["PA"]["nozzle_temperature"] == 280
    assert firmware.builds["XL_DEV_KIT"].presets["PA"]["nozzle_temperature"] == 285


@pytest.mark.parametrize("name", sorted(EXTRUDERS))
def test_load_reads_every_builds_extruder(name):
    build = compare.load(fixtures.FakeSource()).builds[name]
    e_steps, pause, ramming, cleaner_purge = EXTRUDERS[name]
    assert build.e_steps == e_steps
    assert build.pause == pause
    assert build.ramming == ramming
    assert build.cleaner_purge == cleaner_purge


def test_feed_ratio():
    builds = compare.load(fixtures.FakeSource()).builds
    assert builds["MK4"].feed_ratio == 0.95
    assert builds["COREONE_INDX"].feed_ratio == 550 / 576
    assert builds["MINI"].feed_ratio == 1.0


def test_extruder_table_covers_every_build():
    assert EXTRUDERS.keys() == {b for b in compare.MODEL_BUILDS.values() if b}


@pytest.mark.parametrize(
    ("sequence", "distance"),
    [
        (NEXTRUDER_AUTO_RETRACT, 55),
        (NEXTRUDER_UNLOAD, 105),
        (INDX["auto_retract"], 8),
        ((), 0),
        (((-5, 100), (20, 100), (-3, 100)), 3),
    ],
)
def test_retracted_distance(sequence, distance):
    assert compare.retracted_distance(sequence) == distance


def test_load_fails_on_a_model_without_a_build():
    models = fixtures.MODELS_HPP.replace("{ 2, 1, 0 }", "{ 9, 9, 9 }")
    files = {**fixtures.FILES, read.MODELS_FILE: models}
    with pytest.raises(Unreadable, match=r"no build for 9\.9\.9 \(MINI\); add it to MODEL_BUILDS"):
        compare.load(fixtures.FakeSource(files))


def test_load_fails_on_a_build_printer_cant_name():
    options = fixtures.OPTIONS_CMAKE.replace('    "XL_DEV_KIT"\n    "NONE"', '    "NONE"')
    files = {**fixtures.FILES, read.OPTIONS_FILE: options}
    with pytest.raises(Unreadable, match="PRINTER_VALID_OPTS doesn't list XL_DEV_KIT"):
        compare.load(fixtures.FakeSource(files))


@pytest.mark.parametrize(
    "path",
    [
        read.TRACKER_FILE,
        read.CONFIGURATION_FILE,
        read.ADVANCED_FILE,
        read.RAMMING_CMAKE_FILE,
        read.advanced_file("MINI"),
        read.configuration_file("XL_DEV_KIT"),
        read.ramming_file("mk3_5"),
        read.CLEANER_FILE,
    ],
)
def test_load_fails_on_a_missing_file(path):
    files = dict(fixtures.FILES)
    del files[path]
    with pytest.raises(Unreadable, match=f"can't read {path}"):
        compare.load(fixtures.FakeSource(files))


def test_every_build_has_its_printer_is_macro():
    builds = {b for b in compare.MODEL_BUILDS.values() if b}
    covered = set().union(*compare.PRINTER_IS.values())
    assert builds == covered


@pytest.mark.parametrize(
    ("features", "tools"),
    [
        ({"HAS_INDX", "HAS_TOOLCHANGER"}, 8),
        ({"HAS_TOOLCHANGER"}, 5),
        ({"HAS_LOADCELL"}, 1),
    ],
)
def test_tools(features, tools):
    assert compare.tools(frozenset(features)) == tools


def test_macros():
    value = compare.macros("COREONEL_INDX", {"HAS_INDX": frozenset({"COREONEL_INDX"})})
    assert value("HAS_INDX")
    assert value("PRINTER_IS_PRUSA_COREONEL")
    assert not value("PRINTER_IS_PRUSA_COREONE")
    assert compare.macros("XL_DEV_KIT", {})("PRINTER_IS_PRUSA_XL")
    assert compare.macros("XL", {})("BOARD_IS_XLBUDDY")
    assert not compare.macros("XL", {})("BOARD_IS_DWARF")
    assert not value("BOARD_IS_XLBUDDY")
    with pytest.raises(KeyError):
        value("HAS_NEW")
    with pytest.raises(KeyError):
        value("PRINTER_IS_PRUSA_NEW")
    with pytest.raises(KeyError):
        value("BOARD_IS_NEW")


def test_current_copies():
    copies = Copies.current()
    assert copies.preset_preheat == copies.default_preheat == 170
    assert "PLA" in copies.bridge_presets and "PLA" in copies.sim_models["1.4.0"].presets
    assert copies.sim_aliases["TPU"] == "FLEX"
    assert copies.bridge_models["1.4.0"].name == "MK4"
    assert copies.sim_models["1.4.0"].name == "MK4"
    assert copies.sim_models["1.4.0"].extruder.e_steps == (380, 400)


def fw_preset(nozzle, preheat, bed, chamber=None, filtration=None, flexible=False, door=None):
    values = {
        "name": "",
        "nozzle_temperature": nozzle,
        "nozzle_preheat_temperature": preheat,
        "heatbed_temperature": bed,
        "is_flexible": flexible,
    }
    if chamber is not None:
        target, low, high = chamber
        values |= {
            "chamber_target_temperature": target,
            "chamber_min_temperature": low,
            "chamber_max_temperature": high,
        }
    if filtration is not None:
        values["requires_filtration"] = filtration
    if door is not None:
        values["requires_ht_idler_door"] = door
    return values


def build(name, features, tools, gap_mm, presets):
    """A `Build` with the fields the comparison reads, and fixed extruder numbers."""
    return Build(
        name,
        features,
        tools,
        gap_mm,
        presets,
        e_steps=(380, 400),
        pause=NEXTRUDER_PAUSE,
        ramming=NEXTRUDER,
        cleaner_purge=None,
    )


MK4_PRESETS = {"PLA": (215, 170, 60), "FLEX": (240, 210, 50)}
MINI_PRESETS = {"PLA": (215, 170, 60)}
CORE_ONE_PRESETS = {**MK4_PRESETS, "PPS": (320, 170, 105)}


def synthetic():
    """A firmware with four models, and copies that match it."""
    core = frozenset(
        {
            "HAS_FILAMENT_TRACKER",
            "HAS_ANFC",
            "HAS_CHAMBER_API",
            "HAS_CHAMBER_FILTRATION_API",
            "HAS_HT_HOTEND",
        }
    )
    builds = {
        "MK4": build(
            "MK4",
            frozenset({"HAS_FILAMENT_TRACKER"}),
            1,
            40.0,
            {"PLA": fw_preset(215, 170, 60), "FLEX": fw_preset(240, 210, 50, flexible=True)},
        ),
        "MINI": build("MINI", frozenset(), 1, None, {"PLA": fw_preset(215, 170, 60)}),
        "XL": build("XL", frozenset({"HAS_FILAMENT_TRACKER"}), 5, 40.0, {}),
        "COREONE": build(
            "COREONE",
            core,
            1,
            40.0,
            {
                "PLA": fw_preset(215, 170, 60, (20, 15, 38), False, door=False),
                "FLEX": fw_preset(240, 210, 50, (25, 15, 40), True, True, door=False),
                "PPS": fw_preset(320, 170, 105, (55, 45, 65), False, door=True),
            },
        ),
    }
    firmware = Firmware(
        commit="c0ffee",
        models={"1.4.0": "MK4", "2.1.0": "MINI", "3.1.0": "XL", "7.1.0": "COREONE"},
        builds=builds,
        defaults=(215, 170, 60),
        aliases={"TPU": "FLEX"},
    )
    copies = Copies(
        bridge_models={
            "1.4.0": PrinterModel("MK4"),
            "2.1.0": PrinterModel("MINI", NO_COUNTERS),
            "3.1.0": PrinterModel("XL", MULTI_TOOL),
            "7.1.0": PrinterModel("COREONE", uuids=True),
        },
        no_counters=NO_COUNTERS,
        multi_tool=MULTI_TOOL,
        bridge_presets={
            "PLA": Preset(215, 60, chamber=(20, 15, 38)),
            "FLEX": Preset(
                240, 50, preheat=210, chamber=(25, 15, 40), filtration=True, flexible=True
            ),
            "PPS": Preset(320, 105, chamber=(55, 45, 65)),
        },
        ht_idler_door=frozenset({"PPS"}),
        bridge_aliases={"TPU": "FLEX"},
        default_preheat=170,
        preset_preheat=170,
        sim_models={
            "1.4.0": ModelInfo("1.4.0", "MK4", EXTRUDER, MK4_PRESETS),
            "2.1.0": ModelInfo(
                "2.1.0", "MINI", EXTRUDER, MINI_PRESETS, counters=False, gap_mm=99.0
            ),
            "3.1.0": ModelInfo("3.1.0", "XL", EXTRUDER, {}, max_tools=5),
            "7.1.0": ModelInfo("7.1.0", "COREONE", EXTRUDER, CORE_ONE_PRESETS, uuids=True),
        },
        sim_flexible=frozenset({"FLEX"}),
        sim_defaults=(215, 170, 60),
        sim_aliases={"TPU": "FLEX"},
    )
    return firmware, copies


def test_matching_copies_have_no_differences():
    firmware, copies = synthetic()
    assert compare.compare(firmware, copies) == []
    lines, ok = compare.report([], ())
    assert ok
    assert lines == [f"{t}: ok" for t in compare.TABLES] + [
        "0 unexpected differences, 0 expected, 0 stale expected entries"
    ]


def test_model_differences():
    firmware, copies = synthetic()
    bridge = dict(copies.bridge_models)
    bridge["1.4.0"] = PrinterModel("MK4", MULTI_TOOL)  # it has one tool
    bridge["2.1.0"] = PrinterModel("MINI")  # it has no tracker
    bridge["3.1.0"] = PrinterModel("XL")  # it has five tools
    bridge["7.1.0"] = PrinterModel("CORE", NO_COUNTERS)  # it has the tracker and UUIDs
    bridge["9.9.9"] = PrinterModel("NEW")
    sim = dict(copies.sim_models)
    sim["1.4.0"] = replace(sim["1.4.0"], gap_mm=27.0, max_tools=2, counters=False)
    sim["2.1.0"] = ModelInfo("2.1.0", "MINI", EXTRUDER, MINI_PRESETS, uuids=True)
    del sim["7.1.0"]
    copies = replace(copies, bridge_models=bridge, sim_models=sim)
    b, s = "bridge PRINTER_MODELS", "simulator MODELS"
    assert compare.compare(firmware, copies) == [
        Difference(b, "1.4.0", None, "unsupported", "1 tool", "_MULTI_TOOL"),
        Difference(b, "2.1.0", None, "unsupported", "no filament tracker", "supported"),
        Difference(b, "3.1.0", None, "unsupported", "5 tools", "supported"),
        Difference(b, "7.1.0", None, "name", "COREONE", "CORE"),
        Difference(b, "7.1.0", None, "uuids", True, False),
        Difference(b, "7.1.0", None, "unsupported", "has the filament tracker", "_NO_COUNTERS"),
        Difference(b, "9.9.9", None, "model", MISSING, "NEW"),
        Difference(s, "1.4.0", None, "counters", True, False),
        Difference(s, "1.4.0", None, "max_tools", 1, 2),
        Difference(s, "1.4.0", None, "gap_mm", 40.0, 27.0),
        Difference(s, "2.1.0", None, "uuids", False, True),
        Difference(s, "2.1.0", None, "counters", False, True),
        Difference(s, "7.1.0", None, "model", "COREONE", MISSING),
        # The bridge's presets of a model it no longer supports aren't compared, so only
        # the door table notices the Core One's PPS. A model the simulator lacks has no
        # presets to compare.
        Difference("bridge HT_IDLER_DOOR", None, None, "materials", [], ["PPS"]),
    ]


def test_extruder_differences():
    firmware, copies = synthetic()
    mk4, xl = firmware.builds["MK4"], firmware.builds["XL"]
    builds = {
        **firmware.builds,
        "MK4": replace(mk4, features=mk4.features | {"HAS_SWITCHABLE_AUTO_RETRACT", "HAS_MMU2"}),
        "XL": replace(xl, cleaner_purge=CleanerPurge(25.0, True)),
    }
    firmware = replace(firmware, builds=builds)
    sim = dict(copies.sim_models)
    ramming = {"runout": NEXTRUDER_RUNOUT, "unload": NEXTRUDER_AUTO_RETRACT, "extra": ((1, 1),)}
    mk4_extruder = replace(
        EXTRUDER,
        e_steps=(381, 401),
        lengths=replace(EXTRUDER.lengths, purge_mm=28),
        ramming=ramming,
    )
    sim["1.4.0"] = replace(sim["1.4.0"], extruder=mk4_extruder)
    mini_extruder = replace(EXTRUDER, auto_retract_switchable=True, cleaner_purge_mm=0.0)
    sim["2.1.0"] = replace(sim["2.1.0"], mmu=True, extruder=mini_extruder)
    # The cleaner's purge matches; its auto-retract doesn't.
    sim["3.1.0"] = replace(sim["3.1.0"], extruder=replace(EXTRUDER, cleaner_purge_mm=25.0))
    sim["7.1.0"] = replace(sim["7.1.0"], extruder=replace(EXTRUDER, cleaner_auto_retract=True))
    s = "simulator MODELS"
    found = compare.compare(firmware, replace(copies, sim_models=sim))
    assert found == [
        Difference(s, "1.4.0", None, "mmu", True, False),
        Difference(s, "1.4.0", None, "e_steps default", 380, 381),
        Difference(s, "1.4.0", None, "e_steps calibrated", 400, 401),
        Difference(s, "1.4.0", None, "lengths.purge_mm", 27, 28),
        Difference(s, "1.4.0", None, "ramming.auto_retract", NEXTRUDER_AUTO_RETRACT, MISSING),
        Difference(s, "1.4.0", None, "ramming.extra", MISSING, ((1, 1),)),
        Difference(s, "1.4.0", None, "ramming.unload", NEXTRUDER_UNLOAD, NEXTRUDER_AUTO_RETRACT),
        Difference(s, "1.4.0", None, "auto_retract_switchable", True, False),
        Difference(s, "2.1.0", None, "mmu", False, True),
        Difference(s, "2.1.0", None, "auto_retract_switchable", False, True),
        Difference(s, "2.1.0", None, "cleaner_purge_mm", None, 0.0),
        Difference(s, "3.1.0", None, "cleaner_auto_retract", True, False),
        Difference(s, "7.1.0", None, "cleaner_auto_retract", False, True),
    ]
    assert str(found[3]) == "1.4.0 lengths.purge_mm: firmware 27, copy 28"
    assert str(found[5]) == "1.4.0 ramming.extra: firmware missing, copy ((1, 1),)"


def test_a_model_refused_for_another_reason_passes():
    firmware, copies = synthetic()
    bridge = dict(copies.bridge_models)
    bridge["2.1.0"] = PrinterModel("MINI", "some other reason")
    bridge["3.1.0"] = PrinterModel("XL", "some other reason")
    assert compare.compare(firmware, replace(copies, bridge_models=bridge)) == []


def test_preset_differences():
    firmware, copies = synthetic()
    copies = replace(
        copies,
        bridge_presets={
            "PLA": Preset(215, 61, preheat=171, chamber=(20, 15, 39), filtration=True),
            "FLEX": copies.bridge_presets["FLEX"],
            "ABS": Preset(255, 100, chamber=(70, 40, 75)),
        },
        ht_idler_door=frozenset({"PPS", "PPA"}),
    )
    b = "bridge PRESETS"
    assert compare.compare(firmware, copies) == [
        # The MK4 has no chamber, so only the Core One compares it.
        Difference(b, "1.4.0", "PLA", "preheat", 170, 171),
        Difference(b, "1.4.0", "PLA", "bed", 60, 61),
        Difference(b, "7.1.0", "PLA", "preheat", 170, 171),
        Difference(b, "7.1.0", "PLA", "bed", 60, 61),
        Difference(b, "7.1.0", "PLA", "chamber", (20, 15, 38), (20, 15, 39)),
        Difference(b, "7.1.0", "PLA", "filtration", False, True),
        Difference(b, "7.1.0", "PPS", "material", PRESENT, MISSING),
        Difference(b, None, "ABS", "material", MISSING, PRESENT),
        Difference("bridge HT_IDLER_DOOR", None, None, "materials", ["PPS"], ["PPA", "PPS"]),
    ]


def test_simulator_preset_differences():
    firmware, copies = synthetic()
    sim = dict(copies.sim_models)
    # The MK4's PLA runs hot, the MINI lacks its PLA, and the Core One has the MK4's table.
    sim["1.4.0"] = replace(sim["1.4.0"], presets={**MK4_PRESETS, "PLA": (216, 170, 60)})
    sim["2.1.0"] = replace(sim["2.1.0"], presets={})
    sim["7.1.0"] = replace(sim["7.1.0"], presets=MK4_PRESETS)
    # The XL's build has no presets, so anything its model has is extra.
    sim["3.1.0"] = replace(sim["3.1.0"], presets={"PPS": (320, 170, 105)})
    s = "simulator presets"
    found = compare.compare(firmware, replace(copies, sim_models=sim))
    assert found == [
        Difference(s, "1.4.0", "PLA", "nozzle", 215, 216),
        Difference(s, "2.1.0", "PLA", "material", PRESENT, MISSING),
        Difference(s, "3.1.0", "PPS", "material", MISSING, PRESENT),
        Difference(s, "7.1.0", "PPS", "material", PRESENT, MISSING),
    ]
    assert str(found[0]) == "1.4.0 PLA nozzle: firmware 215, copy 216"
    assert str(found[2]) == "3.1.0 PPS material: firmware missing, copy present"


def test_flexible_and_unset_chamber_differences():
    firmware, copies = synthetic()
    core = firmware.builds["COREONE"]
    presets = dict(core.presets)
    presets["FLEX"] = {**presets["FLEX"], "is_flexible": False, "chamber_min_temperature": None}
    builds = {**firmware.builds, "COREONE": replace(core, presets=presets)}
    firmware = replace(firmware, builds=builds)
    b, s = "bridge PRESETS", "simulator presets"
    assert compare.compare(firmware, copies) == [
        Difference(b, "7.1.0", "FLEX", "flexible", False, True),
        Difference(b, "7.1.0", "FLEX", "chamber", (25, None, 40), (25, 15, 40)),
        Difference(s, "7.1.0", "FLEX", "flexible", False, True),
    ]


def test_simulator_flexible_differences():
    firmware, copies = synthetic()
    copies = replace(copies, sim_flexible=frozenset({"PLA", "XYZ"}))
    s = "simulator presets"
    assert compare.compare(firmware, copies) == [
        Difference(s, "1.4.0", "PLA", "flexible", False, True),
        Difference(s, "1.4.0", "FLEX", "flexible", True, False),
        Difference(s, "2.1.0", "PLA", "flexible", False, True),
        Difference(s, "7.1.0", "PLA", "flexible", False, True),
        Difference(s, "7.1.0", "FLEX", "flexible", True, False),
        Difference(s, None, "XYZ", "flexible", MISSING, PRESENT),
    ]


def test_retracted_distance_agrees_with_the_simulators():
    sequences = {steps for model in MODELS.values() for steps in model.extruder.ramming.values()}
    assert sequences
    for steps in sequences:
        assert compare.retracted_distance(steps) == tool.retracted_distance(steps)
    # Nextruder's auto-retract and unload, INDX's auto-retract, as `RammingSequence::init`.
    assert tool.retracted_distance(tool.NEXTRUDER_RAMMING["auto_retract"]) == 55
    assert tool.retracted_distance(tool.NEXTRUDER_RAMMING["unload"]) == 105
    assert tool.retracted_distance(tool.INDX_RAMMING["auto_retract"]) == 8


def test_default_and_alias_differences():
    firmware, copies = synthetic()
    copies = replace(
        copies,
        default_preheat=160,
        preset_preheat=150,
        sim_defaults=(215, 170, 61),
        bridge_aliases={"TPU": "FLEX", "TPE": "FLEX"},
        sim_aliases={"TPU": "PLA"},
    )
    assert compare.compare(firmware, copies) == [
        Difference("bridge DEFAULT_PREHEAT", None, None, "DEFAULT_PREHEAT", 170, 160),
        Difference("bridge DEFAULT_PREHEAT", None, None, "Preset.preheat", 170, 150),
        Difference(
            "simulator DEFAULT_TEMPERATURES",
            None,
            None,
            "nozzle, preheat, bed",
            (215, 170, 60),
            (215, 170, 61),
        ),
        Difference("bridge PRESET_ALIASES", None, "TPE", "alias", MISSING, "FLEX"),
        Difference("simulator PRESET_ALIASES", None, "TPU", "alias", "FLEX", "PLA"),
    ]


def test_report_marks_expected_differences():
    differences = [
        Difference("bridge PRESETS", "7.1.0", "PPS", "material", PRESENT, MISSING),
        Difference("bridge PRESETS", "8.1.0", "PPS", "material", PRESENT, MISSING),
        Difference("simulator MODELS", "1.4.0", None, "max_tools", 1, 2),
    ]
    expected = (Expected("bridge PRESETS", None, "PPS", "material", "it needs the door"),)
    lines, ok = compare.report(differences, expected)
    assert not ok
    assert "bridge PRESETS:" in lines
    assert (
        "  7.1.0 PPS material: firmware present, copy missing  (expected: it needs the door)"
        in (lines)
    )
    assert "  1.4.0 max_tools: firmware 1, copy 2" in lines
    assert "simulator presets: ok" in lines
    assert lines[-1] == "1 unexpected differences, 2 expected, 0 stale expected entries"

    lines, ok = compare.report(differences[:2], expected)
    assert ok


def test_report_fails_on_a_stale_expected_difference():
    expected = (
        Expected("bridge PRESETS", None, "PPS", "material", "it needs the door"),
        Expected("simulator MODELS", "5.1.0", None, "counters", "old"),
    )
    differences = [Difference("bridge PRESETS", "7.1.0", "PPS", "material", PRESENT, MISSING)]
    lines, ok = compare.report(differences, expected)
    assert not ok
    assert (
        "stale expected difference, no longer found: simulator MODELS: 5.1.0 counters;"
        " remove it from EXPECTED" in lines
    )
    assert lines[-1] == "0 unexpected differences, 1 expected, 1 stale expected entries"


def test_a_pinned_entry_doesnt_mask_another_value():
    entry = Expected("bridge PRINTER_MODELS", "4.1.0", None, "unsupported", "why", "1 tool")
    assert entry.copy is compare.ANY
    other = Difference(
        "bridge PRINTER_MODELS", "4.1.0", None, "unsupported", "has the filament tracker", "x"
    )
    lines, ok = compare.report([other], (entry,))
    assert not ok
    assert "  4.1.0 unsupported: firmware 'has the filament tracker', copy 'x'" in lines
    assert lines[-1] == "1 unexpected differences, 0 expected, 1 stale expected entries"
    pinned = Expected("bridge PRESETS", None, "PPS", "material", "why", PRESENT, MISSING)
    assert pinned.matches(
        Difference("bridge PRESETS", "7.1.0", "PPS", "material", PRESENT, MISSING)
    )
    assert not pinned.matches(
        Difference("bridge PRESETS", None, "PPS", "material", MISSING, PRESENT)
    )


def test_expected_matching():
    entry = Expected("bridge PRESETS", "7.1.0", None, "material", "why")
    assert entry.matches(Difference("bridge PRESETS", "7.1.0", "PPA", "material", 1, 2))
    assert not entry.matches(Difference("bridge PRESETS", "8.1.0", "PPA", "material", 1, 2))
    assert not entry.matches(Difference("bridge PRESETS", "7.1.0", "PPA", "bed", 1, 2))
    assert not entry.matches(Difference("simulator MODELS", "7.1.0", "PPA", "material", 1, 2))


def test_expected_entries_have_reasons():
    assert compare.EXPECTED
    for entry in compare.EXPECTED:
        assert entry.table in compare.TABLES
        assert entry.reason
        assert compare.ANY not in (entry.firmware, entry.copy)
