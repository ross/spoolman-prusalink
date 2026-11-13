"""The readers, on excerpts of the firmware files."""

import pytest

from firmware import read
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
