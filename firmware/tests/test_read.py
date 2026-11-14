"""The readers, on excerpts of the firmware files."""

import pytest

from firmware import compare, read
from firmware.compare import FEATURES
from firmware.source import Unreadable
from firmware.tests import fixtures


def macros(**values):
    def value(name):
        return values[name]

    return value


def test_printer_models():
    assert read.printer_models(fixtures.MODELS_HPP) == {
        "1.3.0": "MK3",
        "1.4.0": "MK4",
        "2.1.0": "MINI",
        "7.1.0": "COREONE",
    }


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("int x;", "no printer_model_info array"),
        ("printer_model_info {\n};", "no PrinterModelInfo entries"),
        (
            "printer_model_info {\n PrinterModelInfo { .version = { 1, 4, 0 }, },\n};",
            "no .id_str",
        ),
        (
            'printer_model_info {\n PrinterModelInfo { .version = { 1, 4, 0 }, .id_str = "A" },'
            ' PrinterModelInfo { .version = { 1, 4, 0 }, .id_str = "B" },\n};',
            "model 1.4.0 appears twice",
        ),
        (
            'printer_model_info {\n PrinterModelInfo { .version = { 1, 4 }, .id_str = "A" },\n};',
            "no .version",
        ),
    ],
)
def test_printer_models_fail_closed(text, problem):
    with pytest.raises(Unreadable, match=f"can't read {read.MODELS_FILE}: {problem}"):
        read.printer_models(text)


def test_features():
    found = read.features(fixtures.OPTIONS_CMAKE, FEATURES)
    assert found.keys() == set(FEATURES)
    assert found["HAS_LOADCELL"] == {
        "MK4",
        "iX",
        "XL",
        "XL_DEV_KIT",
        "COREONE",
        "COREONE_INDX",
        "COREONEL",
        "COREONEL_INDX",
    }
    assert found["HAS_FILAMENT_TRACKER"] >= {"MK4", "iX"}
    assert found["HAS_HT_HOTEND"] == {"COREONE", "COREONEL"}
    assert found["HAS_FILAMENT_HEATBREAK_PARAM"] == {"iX"}


def test_features_ignore_unneeded_ones_inside_blocks():
    # HAS_E2EE_SUPPORT is set inside if() twice, but isn't needed.
    assert read.features(fixtures.OPTIONS_CMAKE, ["HAS_ANFC"]) == {
        "HAS_ANFC": frozenset({"COREONE", "COREONEL"})
    }


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "no set_feature_for_printers call for HAS_ANFC"),
        ('if(X)\n  set_feature_for_printers(HAS_ANFC "COREONE")\nendif()', "set inside if"),
        (
            'foreach(P IN LISTS X)\nset_feature_for_printers(HAS_ANFC "A")\nendforeach()',
            "set inside foreach",
        ),
        (
            'set_feature_for_printers(HAS_ANFC "A")\n'
            'set_feature_for_printers_master_board(HAS_ANFC "B")',
            "HAS_ANFC is set more than once",
        ),
        ('set_feature_for_printers(HAS_ANFC "A")\nset(HAS_ANFC NO)', "also set by set()"),
        ("if(X)\n", r"if\(\) on line 1 isn't closed"),
        ("endif()", "closes nothing"),
        ("#[[ a bracket comment ]]", "bracket comment"),
        ("set(X [[bracket]])", "bracket argument"),
        ("set(X", "isn't closed"),
        ("set", "has no arguments"),
        ("$(X)", "unexpected '\\$'"),
        ('set(X "unterminated)', "unterminated string"),
    ],
)
def test_features_fail_closed(text, problem):
    with pytest.raises(Unreadable, match=f"can't read {read.OPTIONS_FILE}: .*{problem}"):
        read.features(text, ["HAS_ANFC"])


def test_printers():
    assert read.printers(fixtures.OPTIONS_CMAKE) == {
        "COREONE",
        "COREONE_INDX",
        "COREONEL",
        "COREONEL_INDX",
        "MINI",
        "MK4",
        "MK3.5",
        "XL",
        "iX",
        "XL_DEV_KIT",
        "NONE",
    }


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("set(X 1)", "no PRINTER_VALID_OPTS"),
        ("set(PRINTER_VALID_OPTS)", "no PRINTER_VALID_OPTS"),
        ('set(PRINTER_VALID_OPTS "A")\nset(PRINTER_VALID_OPTS "B")', "set more than once"),
        ('if(X)\nset(PRINTER_VALID_OPTS "A")\nendif()', r"set by set\(\) on line 2 inside if"),
        ('unset(PRINTER_VALID_OPTS "A")', r"set by unset\(\) on line 1$"),
    ],
)
def test_printers_fail_closed(text, problem):
    with pytest.raises(Unreadable, match=f"can't read {read.OPTIONS_FILE}: .*{problem}"):
        read.printers(text)


def test_features_tolerate_quotes_and_comments_in_arguments():
    text = 'set (MSG "a ) # in a string")\nset_feature_for_printers(HAS_ANFC # note\n "A")'
    assert read.features(text, ["HAS_ANFC"]) == {"HAS_ANFC": frozenset({"A"})}


def build_macros(build):
    """The macros for a build, as `ProjectOptions.cmake` and `printers.h` set them."""
    features = read.features(fixtures.OPTIONS_CMAKE, FEATURES)

    def value(name):
        if name == "PRINTER_IS_PRUSA_MINI":
            return build == "MINI"
        if name == "PRINTER_IS_PRUSA_COREONEL":
            return build in ("COREONEL", "COREONEL_INDX")
        return build in features[name]

    return value


def test_presets_for_a_single_tool_printer_without_a_chamber():
    presets = read.presets(fixtures.PRESETS_CPP, build_macros("MK4"))
    assert presets == {
        "PLA": {
            "name": "PLA",
            "nozzle_temperature": 215,
            "nozzle_preheat_temperature": 170,
            "heatbed_temperature": 60,
            "is_flexible": False,
        },
        "PC": {
            "name": "PC",
            "nozzle_temperature": 275,
            "nozzle_preheat_temperature": 250,
            "heatbed_temperature": 100,
            "is_flexible": False,
        },
        "PA": {
            "name": "PA",
            "nozzle_temperature": 285,
            "nozzle_preheat_temperature": 260,
            "heatbed_temperature": 100,
            "is_flexible": False,
        },
    }


def test_presets_for_a_printer_with_a_chamber_and_the_ht_hotend():
    presets = read.presets(fixtures.PRESETS_CPP, build_macros("COREONEL"))
    assert list(presets) == ["PLA", "PC", "PA", "PPS"]
    assert presets["PC"]["requires_filtration"] is True
    assert presets["PA"]["chamber_min_temperature"] is None  # the default
    assert presets["PLA"]["requires_ht_idler_door"] is False
    pps = presets["PPS"]
    chamber = [pps[f"chamber_{k}_temperature"] for k in ("min", "max", "target")]
    assert chamber == [55, 70, 60]
    assert pps["requires_ht_idler_door"] is True
    assert "is_abrasive" not in pps and "heatbreak_temperature" not in pps


def test_presets_ternaries_per_build():
    assert (
        read.presets(fixtures.PRESETS_CPP, build_macros("COREONE"))["PPS"][
            "chamber_min_temperature"
        ]
        == 45
    )
    mini = read.presets(fixtures.PRESETS_CPP, build_macros("MINI"))
    assert (mini["PA"]["nozzle_temperature"], mini["PA"]["nozzle_preheat_temperature"]) == (
        280,
        255,
    )
    assert mini["PC"]["nozzle_preheat_temperature"] == 170  # no load cell
    indx = read.presets(fixtures.PRESETS_CPP, build_macros("COREONE_INDX"))
    assert indx["PC"]["nozzle_preheat_temperature"] == 230
    assert "PPS" not in indx


def test_presets_skip_ignored_fields():
    # The iX has the heatbreak field, whose default is a macro the reader can't evaluate.
    presets = read.presets(fixtures.PRESETS_CPP, build_macros("iX"))
    assert "heatbreak_temperature" not in presets["PA"]


def presets_text(struct_extra="", entry_extra="", table_extra=""):
    return f"""
struct PresetFilamentParameters {{
    decltype(FilamentTypeParameters::name) name;
    decltype(FilamentTypeParameters::nozzle_temperature) nozzle_temperature = 200;
{struct_extra}
}};
constexpr X preset_filament_parameters_constexpr {{
    {{
        PresetFilamentType::PLA,
        PresetFilamentParameters {{
            .name = "PLA",
{entry_extra}
        }},
    }},
{table_extra}
}};
"""


def test_presets_minimal():
    assert read.presets(presets_text(), macros()) == {
        "PLA": {"name": "PLA", "nozzle_temperature": 200}
    }


@pytest.mark.parametrize(
    ("kwargs", "problem"),
    [
        ({"struct_extra": "int new_field = 1;"}, "unknown member new_field"),
        ({"struct_extra": "int name;"}, "member name appears twice"),
        ({"struct_extra": "int heatbed_temperature;"}, "PLA has no value for heatbed_temperature"),
        ({"struct_extra": "what is this"}, "can't read the member"),
        ({"entry_extra": ".is_flexible = true,"}, "PLA sets is_flexible, which isn't a member"),
        (
            {"entry_extra": ".nozzle_temperature = 1, .nozzle_temperature = 2,"},
            "sets nozzle_temperature twice",
        ),
        ({"entry_extra": "nozzle_temperature 1"}, "can't read PLA's initializer"),
        ({"entry_extra": ".nozzle_temperature = HAS_NEW() ? 1 : 2,"}, r"unknown macro HAS_NEW\(\)"),
        ({"entry_extra": "#if HAS_NEW()\n#endif"}, r"unknown macro HAS_NEW\(\)"),
        ({"entry_extra": "#ifdef X\n#endif"}, "unexpected #ifdef"),
        (
            {
                "table_extra": "{ PresetFilamentType::PLA,"
                ' PresetFilamentParameters { .name = "PLA" } },'
            },
            "repeated name",
        ),
        (
            {"table_extra": "{ PresetFilamentType::X, PresetFilamentParameters { .name = 1 } },"},
            "missing or repeated name",
        ),
        ({"table_extra": "junk"}, "unexpected text after the last preset"),
        (
            {"table_extra": "junk { PresetFilamentType::X, PresetFilamentParameters { } },"},
            "unexpected text before X",
        ),
    ],
)
def test_presets_fail_closed(kwargs, problem):
    with pytest.raises(Unreadable, match=f"can't read {read.PRESETS_FILE}: .*{problem}"):
        read.presets(presets_text(**kwargs), macros())


def test_presets_need_the_struct_table_and_entries():
    with pytest.raises(Unreadable, match="no struct PresetFilamentParameters"):
        read.presets("", macros())
    empty = "struct PresetFilamentParameters {\n};\npreset_filament_parameters_constexpr {\n};"
    with pytest.raises(Unreadable, match="no presets"):
        read.presets(empty, macros())


@pytest.mark.parametrize(
    ("expr", "value"),
    [
        ("215", 215),
        ('"PLA"', "PLA"),
        ("true", True),
        ("false", False),
        ("std::nullopt", None),
        ("275 - 25", 250),
        ("1 + 2 - 3 + 4", 4),
        ("-5", -5),
        ("+5", 5),
        ("A()", True),
        ("!A()", False),
        ("!!A()", True),
        ("A() && B()", False),
        ("A() || B()", True),
        ("B() || !A() || A()", True),
        ("A() && !B()", True),
        ("A() ? 1 : 2", 1),
        ("B() ? 1 : 2", 2),
        ("A() ? (B() ? 230 : 275 - 25) : 170", 250),
        ("B() ? (A() ? 230 : 275 - 25) : 170", 170),
        ("B() ? 1 : A() ? 2 : 3", 2),
        ("(1)", 1),
        ("A() + 1", 2),
    ],
)
def test_evaluate(expr, value):
    assert read.evaluate(expr, macros(A=True, B=False)) == value


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "1 2",
        "A",
        "A(",
        "(1",
        "1 ?",
        "1 ? 2",
        "1 ? 2 3",
        "1 +",
        '"a" + 1',
        "x == 1",
        "1 * 2",
        ")",
        "?",
    ],
)
def test_evaluate_fails_outside_the_grammar(expr):
    with pytest.raises(ValueError):
        read.evaluate(expr, macros(A=True))


def test_evaluate_fails_on_an_unknown_macro_in_either_branch():
    with pytest.raises(ValueError, match=r"unknown macro C\(\)"):
        read.evaluate("A() ? 1 : C()", macros(A=True))


def lines(text):
    return text.strip().splitlines()


def test_active_lines():
    text = """
a
#if A()
b
#elif B()
c
#else
d
#endif
#if !A()
e
#elif A() && !B()
f
#else
g
#endif
#if B()
    #if A()
h
    #else
i
    #endif
#endif
#if B()
#elif B()
#else
j
#endif
"""
    assert read.active_lines(lines(text), macros(A=True, B=False)) == ["a", "b", "f", "j"]
    assert read.active_lines(lines(text), macros(A=False, B=True)) == ["a", "c", "e", "i"]


def test_active_lines_keep_an_active_error():
    text = "#if A()\n#error one\n#else\n#error two\n#endif"
    assert read.active_lines(lines(text), macros(A=True)) == ["#error one"]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("#elif A()", "#elif without #if"),
        ("#if A()\n#else\n#elif A()\n#endif", "#elif without #if"),
        ("#else", "#else without #if"),
        ("#if A()\n#else\n#else\n#endif", "#else without #if"),
        ("#endif", "#endif without #if"),
        ("#if A()", "#if without #endif"),
        ("#define X 1", "unexpected #define"),
        ("#if B()\n#if C()\n#endif\n#endif", r"unknown macro C\(\)"),
    ],
)
def test_active_lines_fail_closed(text, problem):
    with pytest.raises(ValueError, match=problem):
        read.active_lines(lines(text), macros(A=True, B=False))


def test_reset_defaults():
    assert read.reset_defaults(fixtures.FILAMENT_HPP) == (215, 170, 60)


def test_reset_defaults_fail_closed():
    text = "struct FilamentTypeParameters {\n int16_t nozzle_temperature = 215;\n};"
    with pytest.raises(Unreadable, match="no default for nozzle_preheat_temperature"):
        read.reset_defaults(text)
    twice = text.replace("215;", "215;\n int16_t nozzle_temperature = 215;")
    with pytest.raises(Unreadable, match="default for nozzle_temperature appears 2 times"):
        read.reset_defaults(twice)
    with pytest.raises(Unreadable, match="no struct FilamentTypeParameters"):
        read.reset_defaults("")


def test_aliases():
    assert read.aliases(fixtures.DATA_UTILS_CPP) == {"TPU": "FLEX"}


def test_aliases_fail_closed():
    with pytest.raises(Unreadable, match="no abbreviation mapped onto a preset"):
        read.aliases("base_type = FilamentType::from_name(x);")
    text = fixtures.DATA_UTILS_CPP + '\nif (abbreviation.abbreviation == "TPE") { other(); }'
    with pytest.raises(Unreadable, match="an abbreviation test doesn't map onto a preset"):
        read.aliases(text)


@pytest.mark.parametrize(
    ("values", "gap"),
    [
        ({"HAS_NEXTRUDER": True, "HAS_INDX_HEAD": False}, 40.0),
        ({"HAS_NEXTRUDER": False, "HAS_INDX_HEAD": True}, 27.0),
        ({"HAS_NEXTRUDER": False, "HAS_INDX_HEAD": False}, None),
    ],
)
def test_tracker_gap(values, gap):
    assert read.tracker_gap(fixtures.TRACKER_HPP, macros(**values)) == gap


def test_tracker_gap_after_a_nested_if():
    text = "#if A()\n#if A()\n#endif\nint x;\nfloat extruder_to_nozzle_distance = 4.f;\n#endif"
    assert read.tracker_gap(text, macros(A=True)) == 4.0


def test_tracker_gap_outside_any_if():
    text = "#if X()\n#endif\nstatic constexpr float extruder_to_nozzle_distance = 12.5f;"
    assert read.tracker_gap(text, macros(X=False)) == 12.5


NESTED_CHAIN = """\
#if HAS_NEXTRUDER()
    #if HAS_INDX_HEAD()
    static constexpr float extruder_to_nozzle_distance = 27.f;
    #else
    static constexpr float extruder_to_nozzle_distance = 40.f;
    #endif
#elif HAS_INDX_HEAD()
    static constexpr float extruder_to_nozzle_distance = 30.f;
#else
    #error
#endif
"""


@pytest.mark.parametrize(
    ("values", "gap"),
    [
        ({"HAS_NEXTRUDER": True, "HAS_INDX_HEAD": False}, 40.0),
        ({"HAS_NEXTRUDER": True, "HAS_INDX_HEAD": True}, 27.0),
        ({"HAS_NEXTRUDER": False, "HAS_INDX_HEAD": True}, 30.0),
        ({"HAS_NEXTRUDER": False, "HAS_INDX_HEAD": False}, None),
    ],
)
def test_tracker_gap_in_a_nested_chain(values, gap):
    assert read.tracker_gap(NESTED_CHAIN, macros(**values)) == gap


GUARDED = """\
#if !HAS_NEXTRUDER() && !HAS_INDX_HEAD()
    #error
#endif
    static constexpr float extruder_to_nozzle_distance = 40.f;
"""


def test_tracker_gap_with_an_error_guard():
    assert read.tracker_gap(GUARDED, macros(HAS_NEXTRUDER=True, HAS_INDX_HEAD=False)) == 40.0
    with pytest.raises(
        Unreadable, match="1 active extruder_to_nozzle_distance declarations and an active #error"
    ):
        read.tracker_gap(GUARDED, macros(HAS_NEXTRUDER=False, HAS_INDX_HEAD=False))


def test_tracker_gap_skips_unit_test_only_lines():
    text = (
        "#pragma once\n#include <x.h>\n"
        "#ifndef UNITTESTS\nfloat extruder_to_nozzle_distance = 4.f;\n"
        "#endif\n#ifdef UNITTESTS\nfloat extruder_to_nozzle_distance = 9.f;\n#endif"
    )
    assert read.tracker_gap(text, macros()) == 4.0


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "no extruder_to_nozzle_distance"),
        ("#if A()\nfloat extruder_to_nozzle_distance = 4;\n", "#if without #endif"),
        (
            "#if A()\nfloat extruder_to_nozzle_distance = x;\n#endif",
            "can't read extruder_to_nozzle_distance = x",
        ),
        (
            "float extruder_to_nozzle_distance = 4.f;\n#if A()\n"
            "float extruder_to_nozzle_distance = 5.f;\n#endif",
            "2 active extruder_to_nozzle_distance declarations",
        ),
        ("bool b = extruder_to_nozzle_distance == 4;", "0 active extruder_to_nozzle_distance"),
        ("#ifdef OTHER\n#endif\nextruder_to_nozzle_distance", "unexpected #ifdef OTHER"),
    ],
)
def test_tracker_gap_fail_closed(text, problem):
    with pytest.raises(Unreadable, match=f"can't read {read.TRACKER_FILE}: {problem}"):
        read.tracker_gap(text, macros(A=True))


# Each build's `Configuration_<X>.h` and `_adv.h`, and its ramming variant.
CONFIGURATIONS = {
    "MINI": ("MINI", "mini"),
    "MK3.5": ("MK3.5", "mk3_5"),
    "MK4": ("MK4", "nextruder"),
    "XL": ("XL", "nextruder"),
    "XL_DEV_KIT": ("XL_DEV_KIT", "nextruder"),
    "iX": ("iX", "nextruder"),
    "COREONE": ("COREONE", "nextruder"),
    "COREONE_INDX": ("COREONE", "indx"),
    "COREONEL": ("COREONEL", "nextruder"),
    "COREONEL_INDX": ("COREONEL", "indx"),
}


def fixture_features():
    return read.features(fixtures.OPTIONS_CMAKE, FEATURES)


@pytest.mark.parametrize(("build", "name"), [(b, c[0]) for b, c in CONFIGURATIONS.items()])
def test_configuration(build, name):
    values = compare.macros(build, fixture_features())
    assert read.configuration(fixtures.CONFIGURATION_H, values) == name
    assert read.configuration(fixtures.CONFIGURATION_ADV_H, values, advanced=True) == name


def test_configurations_cover_every_build():
    assert CONFIGURATIONS.keys() == {b for b in compare.MODEL_BUILDS.values() if b}


def test_configuration_of_the_xl_tool_head():
    names = ("MINI", "XL_DEV_KIT", "MK4", "MK3_5", "iX", "COREONE", "COREONEL")
    values = {f"PRINTER_IS_PRUSA_{n}": False for n in names}
    values = macros(**values, PRINTER_IS_PRUSA_XL=True, BOARD_IS_XLBUDDY=False, BOARD_IS_DWARF=True)
    assert read.configuration(fixtures.CONFIGURATION_H, values) == "XL_Dwarf"
    assert read.configuration(fixtures.CONFIGURATION_ADV_H, values, advanced=True) == "XL_Dwarf"


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ('    #include "Configuration_MK4.h"', "", "0 active Configuration includes"),
        ('"config.h"\n', '"config.h"\n#include "Configuration_MINI.h"\n', "2 active"),
        ("PRINTER_IS_PRUSA_MK4()", "PRINTER_IS_PRUSA_NEW()", r"unknown macro .*_NEW\(\)"),
        ("PRINTER_IS_PRUSA_MK4()", "0", 'the build reaches #error "Unknown PRINTER_TYPE!"'),
        ("#pragma once\n", "#pragma once\nint x;\n", "unexpected 'int x;'"),
        ("#pragma once", "#pragma pack(1)", r"unexpected #pragma pack\(1\)"),
        ('"config.h"', '"other.h"', 'unexpected #include "other.h"'),
        ('"Configuration_MK4.h"', '"Configuration_MK4_adv.h"', "unexpected #include"),
        ('"Configuration_MK4.h"', "<Configuration_MK4.h>", "unexpected #include"),
        ("#pragma once", "#define X 1", "unexpected #define"),
        ("#pragma once", "#", "unexpected '#'"),
        ("#pragma once", '# "x"', """unexpected '# "x"'"""),
    ],
)
def test_configuration_fail_closed(old, new, problem):
    text = fixtures.CONFIGURATION_H
    assert old in text
    values = compare.macros("MK4", fixture_features())
    with pytest.raises(Unreadable, match=f"can't read {read.CONFIGURATION_FILE}: {problem}"):
        read.configuration(text.replace(old, new, 1), values)


def test_advanced_configuration_fail_closed():
    text = fixtures.CONFIGURATION_ADV_H.replace("Configuration_MK4_adv.h", "Configuration_MK4.h")
    values = compare.macros("MK4", fixture_features())
    match = f'can\'t read {read.ADVANCED_FILE}: unexpected #include "Configuration_MK4.h"'
    with pytest.raises(Unreadable, match=match):
        read.configuration(text, values, advanced=True)


@pytest.mark.parametrize(("build", "variant"), [(b, c[1]) for b, c in CONFIGURATIONS.items()])
def test_ramming_variant(build, variant):
    assert read.ramming_variant(fixtures.RAMMING_CMAKE, build, fixture_features()) == variant


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        # The MK4 takes the first branch, so these break a condition after it.
        ('PRINTER STREQUAL "MINI"', 'PRINTER MATCHES "MINI"', "can't read the condition"),
        ('PRINTER STREQUAL "MINI"', "HAS_INDX AND HAS_MMU2", "can't read the condition"),
        ('PRINTER STREQUAL "MINI"', '"HAS_INDX"', "can't read the condition"),
        ('PRINTER STREQUAL "MINI"', 'PRINTER STREQUAL "${X}"', "can't read the condition"),
        ('PRINTER STREQUAL "MINI"', r'PRINTER STREQUAL "MI\NI"', "can't read the condition"),
        ("elseif(HAS_INDX)", "elseif(HAS_NEW)", "unknown feature HAS_NEW on line 5"),
        ("  target_sources(firmware PRIVATE standard_ramming_sequence_nextruder.cpp)\n", "", "0 "),
        ("ramming_sequence.cpp)", "ramming_sequence.cpp standard_ramming_sequence_mini.cpp)", "2 "),
        ("sequence_mini.cpp", "sequence-mini.cpp", "can't read the ramming source .*-mini.cpp"),
        ("sequence_mini.cpp", "sequence_mini.cc", r"can't read the ramming source .*_mini\.cc"),
        ("message(FATAL_ERROR", "message(WARNING", r"unexpected message\(\) on line 12"),
        ("firmware PRIVATE ramming", "firmware PUBLIC ramming", r"unexpected target_sources"),
        ("PRIVATE standard", "PRIVATE ${SRC} standard", r"unexpected \$\{SRC\} in target_sources"),
        ("PRIVATE standard", "PRIVATE x.cpp INTERFACE standard", "unexpected INTERFACE in target"),
        ("target_sources(firmware PRIVATE ramming", "add_library(x", r"unexpected add_library"),
        ("elseif(HAS_INDX)", "if(HAS_INDX)", r"unexpected if\(\) on line 5"),
        ("else()", "else()\nelseif(HAS_INDX)", r"unexpected elseif\(\) on line 12"),
        ("else()", "else()\nelse()", r"unexpected else\(\) on line 12"),
        ("else()", "else(X)", r"unexpected else\(\) on line 11"),
        ("endif()", "endif(X)", r"unexpected endif\(\) on line 13"),
        ("endif()", "endif()\nendif()", r"unexpected endif\(\) on line 14"),
        ("target_sources(firmware", "elseif(X)\ntarget_sources(firmware", r"unexpected elseif"),
        ("endif()", "", r"if\(\) isn't closed"),
    ],
)
def test_ramming_variant_fail_closed(old, new, problem):
    text = fixtures.RAMMING_CMAKE
    assert old in text
    with pytest.raises(Unreadable, match=f"can't read {read.RAMMING_CMAKE_FILE}: {problem}"):
        read.ramming_variant(text.replace(old, new, 1), "MK4", fixture_features())


def test_ramming_variant_fails_for_a_build_without_one():
    match = r"NEW reaches message\(FATAL_ERROR\) on line 12"
    with pytest.raises(Unreadable, match=match):
        read.ramming_variant(fixtures.RAMMING_CMAKE, "NEW", fixture_features())


@pytest.mark.parametrize(
    ("text", "values", "steps"),
    [
        (fixtures.CONFIGURATION_NEXTRUDER_H, {}, (380, 400)),
        (fixtures.CONFIGURATION_MK3_5_H, {}, (266, 280)),
        (fixtures.CONFIGURATION_MINI_H, {}, (325, 325)),
        (fixtures.CONFIGURATION_CORE_ONE_H, {"HAS_INDX": False}, (380, 400)),
        (fixtures.CONFIGURATION_CORE_ONE_H, {"HAS_INDX": True}, (550, 576)),
    ],
)
def test_e_steps(text, values, steps):
    # Only the conditions around the defines are evaluated: HAS_MMU2 and the MINI's
    # `#ifdef` would fail the read.
    assert read.e_steps(text, macros(**values), "Configuration_X.h") == steps


@pytest.mark.parametrize(
    ("text", "values", "lengths"),
    [
        (fixtures.ADVANCED_NEXTRUDER_H, {"HAS_PAUSE": True}, (4, 8, 105, 30, 50, 27)),
        (fixtures.ADVANCED_MK3_5_H, {"HAS_PAUSE": True}, (4, 8, 105, 30, 50, 21)),
        (fixtures.ADVANCED_MINI_H, {"HAS_PAUSE": True}, (4, 8, 400, 40, 320, 50)),
        (fixtures.ADVANCED_IX_H, {"HAS_PAUSE": True}, (4, 8, 110, 40, 45, 40)),
        (
            fixtures.ADVANCED_CORE_ONE_H,
            {"HAS_PAUSE": True, "HAS_INDX": False},
            (4, 8, 105, 40, 20, 40),
        ),
        (
            fixtures.ADVANCED_CORE_ONE_H,
            {"HAS_PAUSE": True, "HAS_INDX": True},
            (4, 8, 55, 15, 25, 40),
        ),
    ],
)
def test_pause_lengths(text, values, lengths):
    # `EXTRUDERS > 1`, `HAS_DRIVER(TMC26X)` and HAS_NOZZLE_CLEANER aren't evaluated.
    assert read.pause_lengths(text, macros(**values), "Configuration_X_adv.h") == lengths


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("#define A 1", 1),
        ("#  define A -2 // a comment", -2),
        ("/* #define A 2 */\n#define A 1", 1),
        ("#define A \\\n    3", 3),
        ("#define AB 2\n#define A 1\n#define B(x) x", 1),
        ("#if HAS_DRIVER(TMC26X)\n#define B 1\n#endif\n#define A 1", 1),
        ("#if B()\n#define A 1\n#else\n#define A 2\n#endif", 2),
        ("#if B()\n#if X()\n#define A 1\n#endif\n#else\n#define A 2\n#endif", 2),
        ("#if B()\n#elif X()\n#if B()\n#endif\n#define A 3\n#endif", 3),
        ("#ifdef UNITTESTS\n#define A 2\n#endif\n#define A 1", 1),
    ],
)
def test_defines(text, value):
    assert read.defines(text, ["A"], macros(B=False, X=True)) == (value,)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "no #define A"),
        ("#define B 1", "no #define A"),
        ("#define A 1\n#define A 2", "A is defined 2 times for the build"),
        ("#if B()\n#define A 1\n#endif", "A is defined 0 times for the build"),
        ("#if NEW()\n#define A 1\n#endif", r"unknown macro NEW\(\)"),
        ("#if NEW()\n#else\n#define A 1\n#endif", r"unknown macro NEW\(\)"),
        ("#if X()\n#if NEW()\n#endif\n#elif NEW()\n#define A 1\n#endif", r"unknown macro NEW"),
        ("#ifdef USE_X\n#define A 1\n#endif", "unexpected #ifdef USE_X"),
        ("#if EXTRUDERS > 1\n#define A 1\n#endif", "can't evaluate"),
        ("#if X()\n#else\n#else\n#define A 1\n#endif", "#else without #if"),
        ("#define A x", "can't read #define A x"),
        ("#define A(x) 1", r"can't read #define A \(x\) 1"),
        ("#define A 1\n#undef A", "#undef A"),
        ("#else\n#define A 1", "#else without #if"),
        ("#elif X()\n#define A 1", "#elif without #if"),
        ("#endif\n#define A 1", "#endif without #if"),
        ("#if X()\n#define A 1", "#if without #endif"),
    ],
)
def test_defines_fail_closed(text, problem):
    with pytest.raises(ValueError, match=problem):
        read.defines(text, ["A"], macros(B=False, X=True))


def test_e_steps_and_pause_lengths_name_the_file():
    with pytest.raises(Unreadable, match="can't read a.h: no #define DEFAULT_AXIS_STEPS"):
        read.e_steps("", macros(), "a.h")
    with pytest.raises(Unreadable, match="can't read b.h: no #define PARK_PAUSE_PRIME_LENGTH"):
        read.pause_lengths("", macros(), "b.h")


NEXTRUDER_RUNOUT = ((7, 1500), (-50, 2700), (-5, 50), (-50, 1500))
MINI_RAMMING = ((20, 1500), (-50, 2700), (-5, 50), (-50, 1500))
RETURN_BEFORE_SWITCH = """\
    if (virtual_tool.to_physical() == 1) {
        static constexpr RammingSequenceArray seq({ { -50, 600 } });
        return seq;
    }
"""


def test_ramming_nextruder():
    found = read.ramming(fixtures.RAMMING_NEXTRUDER_CPP, macros(HAS_AUTO_RETRACT=True), "n.cpp")
    assert found.keys() == {"auto_retract", "runout", "unload"}
    assert found["auto_retract"][:2] == ((8, 995), (-43, 6000))
    assert found["unload"] == (*found["auto_retract"], (-50, 2000))
    assert found["runout"] == NEXTRUDER_RUNOUT
    without = read.ramming(fixtures.RAMMING_NEXTRUDER_CPP, macros(HAS_AUTO_RETRACT=False), "n")
    assert without == {"runout": found["runout"], "unload": found["unload"]}


def test_ramming_indx_has_flexible_variants():
    found = read.ramming(fixtures.RAMMING_INDX_CPP, macros(HAS_AUTO_RETRACT=True), "i.cpp")
    assert found["auto_retract"] == ((8, 480), (-8, 4800), (1, 1440), (-1, 1440))
    assert found["runout"] == found["unload"]
    assert found["runout"][0] == (8, 1500) and found["runout"][-1] == (-8, 360)
    assert len(found["runout"]) == 12
    assert found["runout_flexible"] == found["unload_flexible"] == ((-50, 600),)


def test_ramming_mini_and_mk3_5():
    mini = read.ramming(fixtures.RAMMING_MINI_CPP, macros(HAS_AUTO_RETRACT=False), "m.cpp")
    assert mini == {"runout": MINI_RAMMING, "unload": MINI_RAMMING}
    mk3_5 = read.ramming(fixtures.RAMMING_MK3_5_CPP, macros(HAS_AUTO_RETRACT=False), "m.cpp")
    assert mk3_5 == {"runout": NEXTRUDER_RUNOUT, "unload": MINI_RAMMING}


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ("::runout:", "::purge:", "unknown ramming sequence purge"),
        ("::unload:", "::runout:", "ramming sequence runout appears twice"),
        ("{ 7, 1500 }", "{ 7, 1500 / 60.F }", "can't read the steps of ramming sequence runout"),
        ("{ 7, 1500 }", "{ 7, 0 }", "can't read the steps of ramming sequence runout"),
        ("{ 7, 1500 }", "{ 7, -1500 }", "can't read the steps of ramming sequence runout"),
        ("{ 7, 1500 },", "{ 7, 1500 } x", "can't read the steps of ramming sequence runout"),
        ("{ -5, 50 },", "{ -5, 50 } x", "can't read the steps of ramming sequence runout"),
        ("return seq;\n    }\n\n    case", "f();\n    }\n\n    case", "can't read the case block"),
        ("    }\n    }\n", "    }\n    default: break;\n    }\n", "unexpected 'default:'"),
        ("switch (seq) {", "switch (other) {", "0 switch"),
        ("    switch", "    switch (seq) {}\n    switch", "2 switch"),
        ("    switch", RETURN_BEFORE_SWITCH + "    switch", "unexpected code before switch"),
        ("    switch", "    const bool is_flexible = f();\n    switch", "unexpected code before"),
        ("bsod_unreachable();", "return f();", "unexpected code after"),
        ("bsod_unreachable();", "bsod_unreachable();\n    f();", "unexpected code after"),
        ("standard_ramming_sequence(", "other(", "0 standard_ramming_sequence definitions"),
    ],
)
def test_ramming_fail_closed(old, new, problem):
    text = fixtures.RAMMING_MK3_5_CPP
    assert old in text
    with pytest.raises(Unreadable, match=f"can't read r.cpp: {problem}"):
        read.ramming(text.replace(old, new, 1), macros(HAS_AUTO_RETRACT=False), "r.cpp")


def test_ramming_needs_the_builds_cases():
    unload = fixtures.RAMMING_MK3_5_CPP.index("    case StandardRammingSequence::unload")
    text = fixtures.RAMMING_MK3_5_CPP[:unload] + "    }\n    bsod_unreachable();\n}\n"
    with pytest.raises(Unreadable, match="the cases are runout, not runout, unload"):
        read.ramming(text, macros(HAS_AUTO_RETRACT=False), "r.cpp")
    with pytest.raises(Unreadable, match="cases are runout, unload, not auto_retract, runout"):
        read.ramming(fixtures.RAMMING_MINI_CPP, macros(HAS_AUTO_RETRACT=True), "r.cpp")
    with pytest.raises(Unreadable, match=r"unknown macro HAS_AUTO_RETRACT\(\)"):
        read.ramming(fixtures.RAMMING_NEXTRUDER_CPP, macros(), "r.cpp")
    with pytest.raises(Unreadable, match="unbalanced braces"):
        read.ramming("standard_ramming_sequence() {", macros(), "r.cpp")


@pytest.mark.parametrize(("indx", "purge"), [(True, (25.0, True)), (False, (0.0, False))])
def test_cleaner_purge(indx, purge):
    assert read.cleaner_purge(fixtures.NOZZLE_CLEANER_CPP, macros(HAS_INDX=indx)) == purge


def purge_clean(gcode):
    return f'{{ Sequence::purge_clean, {{ .filename = "p", .default_gcode = {gcode}, }} }}'


@pytest.mark.parametrize(
    ("gcode", "purge"),
    [
        ('"G750 E5\\n" "G750 X1 E2.5 F4\\n" "M1705 N"', (7.5, True)),
        ('"M106 S0\\n"', (0.0, False)),
    ],
)
def test_cleaner_purge_sums_the_e_moves(gcode, purge):
    assert read.cleaner_purge(purge_clean(gcode), macros()) == purge


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "no Sequence::purge_clean"),
        (purge_clean('"G750 E5"') * 2, "Sequence::purge_clean appears 2 times"),
        ('{ Sequence::purge_clean, { .filename = "p", } }', "no .default_gcode"),
        (purge_clean('"G1 E25 F4"'), "can't read the E move 'G1 E25 F4'"),
        (purge_clean('"G750 E25 E3 F4"'), "can't read the E move"),
        (purge_clean('"G750 Ex F4"'), "can't read the E move"),
        (purge_clean('"M1705\\nG750 E5"'), "M1705 before the last E move"),
        (purge_clean('"M1705"'), "M1705 before the last E move"),
        (purge_clean('"G750 E5\\t"'), "an escape other than"),
        (purge_clean('"G750 E25\\n" EXTRA "M1705 N"'), "unexpected code after the .default_gcode"),
        (purge_clean('"G750 E25" .filename = "q"'), "unexpected code after the .default_gcode"),
        ("#if NEW()\n#endif", r"unknown macro NEW\(\)"),
    ],
)
def test_cleaner_purge_fail_closed(text, problem):
    with pytest.raises(Unreadable, match=f"can't read {read.CLEANER_FILE}: {problem}"):
        read.cleaner_purge(text, macros())
