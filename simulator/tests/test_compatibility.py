"""How each model takes a file's `M862.3` name, as `printer_model.cpp` works it out."""

from dataclasses import replace

import pytest

from simulator.compatibility import (
    GROUPS,
    INDX_LOCK,
    INPUT_SHAPER,
    UPGRADES,
    Compatibility,
    compatibility,
)
from simulator.model import MODELS

# A name of each group, an MMU variant of most, and two names no model has.
NAMES = (
    "MK3",
    "MK3SMMU2S",
    "MK3.5",
    "MK3.5SMMU3",
    "MK3.9",
    "MK4MMU3",
    "MK4S",
    "MK3.9SMMU3",
    "MINI",
    "XL",
    "XLP",
    "iX",
    "COREONE",
    "COREONEOAKMMU3",
    "COREONEL",
    "COREONELMMU3",
    "COREONEINDX",
    "COREONEL-INDX",
    "mk4",  # the match is case-sensitive
    "MK5",
)
MK3 = {"MK3", "MK3SMMU2S"}
MK4 = {"MK3.9", "MK4MMU3"}
MK4S = {"MK4S", "MK3.9SMMU3"}
CORE_ONE = {"COREONE", "COREONEOAKMMU3"}
# Per type code: the names it takes as its own, and those it takes in compatibility mode.
# Every other name in NAMES fails the model check.
TAKES = {
    "1.3.5": ({"MK3.5", "MK3.5SMMU3"}, MK3),
    "1.3.6": ({"MK3.5", "MK3.5SMMU3"}, MK3),
    "1.3.9": (MK4, MK3),
    "1.4.0": (MK4, MK3),
    "1.3.10": (MK4S, MK4 | MK3),
    "1.4.1": (MK4S, MK4 | MK3),
    "2.1.0": ({"MINI"}, set()),
    "3.1.0": ({"XL"}, set()),
    "3.1.1": ({"XLP"}, {"XL"}),
    "5.1.0": ({"XL"}, set()),
    "4.1.0": ({"iX"}, set()),
    "7.1.0": (CORE_ONE, MK4S | MK4 | MK3),
    "7.2.0": (CORE_ONE, MK4S | MK4 | MK3),
    # The Core One L steps down to the Core One without a flag.
    "8.1.0": ({"COREONEL", "COREONELMMU3"} | CORE_ONE, MK4S | MK4 | MK3),
    "7.10.0": ({"COREONEINDX"}, set()),
    "8.10.0": ({"COREONEL-INDX"}, set()),
}


def expected(code: str, name: str) -> Compatibility:
    full, mode = TAKES[code]
    if name in full:
        return Compatibility.FULL
    if name in mode:
        return Compatibility.MODE
    return Compatibility.INCOMPATIBLE


def test_the_table_covers_every_model():
    assert TAKES.keys() == MODELS.keys()


@pytest.mark.parametrize("code", sorted(TAKES))
@pytest.mark.parametrize("name", NAMES)
def test_compatibility(code, name):
    assert compatibility(MODELS[code], name) == expected(code, name)


def test_every_name_has_a_group():
    assert set(NAMES) - GROUPS.keys() == {"mk4", "MK5"}
    for model in MODELS.values():
        assert GROUPS[model.name] == model.group


def test_a_build_without_the_mode_fails_the_model_check():
    # No real build has an upgrade path without `HAS_GCODE_COMPATIBILITY`, which a
    # static_assert in `printer_model.cpp` enforces, so this model is made up.
    mk4s = MODELS["1.4.1"]
    without = replace(mk4s, checks=replace(mk4s.checks, compatibility_mode=False))
    assert compatibility(without, "MK4S") == Compatibility.FULL
    assert compatibility(without, "MK4") == Compatibility.INCOMPATIBLE
    assert compatibility(without, "MK3") == Compatibility.INCOMPATIBLE


def test_every_model_with_an_upgrade_path_has_the_mode():
    # The XL build has the mode too, for the XLP, which shares it.
    with_mode = {m.code for m in MODELS.values() if m.checks.compatibility_mode}
    with_path = {m.code for m in MODELS.values() if m.group in UPGRADES}
    assert with_mode - with_path == {"3.1.0"}
    assert with_path <= with_mode


def test_required_features():
    for model in MODELS.values():
        checks = model.checks
        assert checks.required <= checks.features
        assert (INPUT_SHAPER in checks.required) == (model.name != "iX")
        assert (INDX_LOCK in checks.required) == (model.max_tools == 8)
