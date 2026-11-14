"""What a build's print preview knows about a file's `M862` lines: which model names it
takes, and as what, and which `M862.6` features it knows and wants.

The tables are hand-copied from the firmware. `script/firmware-check` compares `GROUPS`
and each model's `group` and `checks` with the source; `UPGRADES` and `GCodeChecks.required`
are code there, which the check can't read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from simulator.model import ModelInfo

# Every name a file's `M862.3 P` can carry, with its model's compatibility group: the
# `id_str` of each `printer_model_info` entry and of each `printer_model_mmu_variant`
# entry, which takes its base model's group (`include/common/printer_model_data.hpp`). The
# MK3 and MK3S run no build but are here for their files. `from_id_str` in
# `src/common/printer_model.cpp` matches a name exactly, case and all.
# `script/firmware-check` compares it with the firmware source.
GROUPS: dict[str, str] = {
    "MK3": "mk3",
    "MK3MMU2": "mk3",
    "MK3S": "mk3",
    "MK3SMMU2S": "mk3",
    "MK3.5": "mk3_5",
    "MK3.5MMU3": "mk3_5",
    "MK3.5S": "mk3_5",
    "MK3.5SMMU3": "mk3_5",
    "MK3.9": "mk4",
    "MK3.9MMU3": "mk4",
    "MK4": "mk4",
    "MK4MMU3": "mk4",
    "MK3.9S": "mk4s",
    "MK3.9SMMU3": "mk4s",
    "MK4S": "mk4s",
    "MK4SMMU3": "mk4s",
    "MINI": "mini",
    "XL": "xl",
    "XLP": "xls",
    "iX": "ix",
    "COREONE": "coreone",
    "COREONEMMU3": "coreone",
    "COREONEOAK": "coreone",
    "COREONEOAKMMU3": "coreone",
    "COREONEL": "coreonel",
    "COREONELMMU3": "coreonel",
    "COREONEINDX": "coreone_indx",
    "COREONEL-INDX": "coreonel_indx",
}

# The group each group falls back on for a file that isn't its own, with the compatibility
# flag that step sets, or None. A group not listed has no fallback. This mirrors the
# `switch` in `gcode_compatibility_report_constexpr` in `src/common/printer_model.cpp`,
# which is code the check can't read, so compare it by hand when that function changes.
# The firmware names a model to fall back on; this names the model's group.
UPGRADES: dict[str, tuple[str, str | None]] = {
    "xls": ("xl", "xl"),
    "mk3_5": ("mk3", "mk3"),
    "mk4": ("mk3", "mk3"),
    "mk4s": ("mk4", "mk4"),
    "coreone": ("mk4s", "chamber"),
    "coreonel": ("coreone", None),  # only physically larger
}

# The `M862.6` feature names, as `gcode_info::m862_6_*` in `src/common/gcode/gcode_info.hpp`
# spells them. The firmware compares them ignoring case, after trimming.
INPUT_SHAPER = "Input Shaper"
MMU3 = "MMU3"
INDX_LOCK = "INDX lock"
HT_HOTEND = "HT Hotend"


@dataclass(frozen=True)
class GCodeChecks:
    """What one build's checks of a file's `M862` lines know."""

    # `HAS_GCODE_COMPATIBILITY`: whether an older group's file asks, in compatibility
    # mode, rather than failing the model check.
    compatibility_mode: bool
    # The `M862.6` names the build knows. Any other is an unsupported feature.
    features: frozenset[str]
    # The names a file must declare, or the preview asks. This is code in
    # `CompatibilityReport::generate_without_toolmapping` in
    # `src/feature/compatibility_checks/gcode_compatibility.cpp`, which the check can't
    # read: Input Shaper on every build but the iX's, and INDX lock on INDX builds.
    required: frozenset[str]


_INPUT_SHAPER = frozenset({INPUT_SHAPER})

# Each build's checks, shared by the models that run the build.
MK4_CHECKS = GCodeChecks(True, frozenset({INPUT_SHAPER, MMU3}), _INPUT_SHAPER)
MK3_5_CHECKS = MK4_CHECKS
# The Core One and Core One L builds.
CORE_ONE_CHECKS = GCodeChecks(True, frozenset({INPUT_SHAPER, MMU3, HT_HOTEND}), _INPUT_SHAPER)
XL_CHECKS = GCodeChecks(True, _INPUT_SHAPER, _INPUT_SHAPER)
# The MINI and XL dev kit builds.
PLAIN_CHECKS = GCodeChecks(False, _INPUT_SHAPER, _INPUT_SHAPER)
IX_CHECKS = GCodeChecks(False, _INPUT_SHAPER, frozenset())
# The two INDX builds.
INDX_CHECKS = GCodeChecks(
    False, frozenset({INPUT_SHAPER, INDX_LOCK}), frozenset({INPUT_SHAPER, INDX_LOCK})
)


class Compatibility(StrEnum):
    """How a printer takes a file's `M862.3` model name."""

    FULL = "full"  # as its own
    MODE = "compatibility mode"  # the preview asks
    INCOMPATIBLE = "incompatible"  # the model check fails


def compatibility(model: ModelInfo, name: str) -> Compatibility:
    """Return how `model` takes a file whose `M862.3` names `name`.

    As `gcode_compatibility_report_constexpr` and `GCodeInfo::parse_m862` work it out: a
    name in the model's group is its own. Otherwise the model's group falls back along
    `UPGRADES`. Reaching the name's group with no flag set is full compatibility, and with
    any flag set is compatibility mode, which a build without it takes as a wrong model. An
    unknown name is a wrong model.
    """
    target = GROUPS.get(name)
    if target is None:
        return Compatibility.INCOMPATIBLE
    group, flagged = model.group, False
    while group != target:
        if group not in UPGRADES:
            return Compatibility.INCOMPATIBLE
        group, flag = UPGRADES[group]
        flagged = flagged or flag is not None
    if not flagged:
        return Compatibility.FULL
    if model.checks.compatibility_mode:
        return Compatibility.MODE
    return Compatibility.INCOMPATIBLE
