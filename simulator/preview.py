"""The print preview's checks of an uploaded file's `M862` lines.

The firmware reads the lines in `GCodeInfo::parse_m862` (`src/common/gcode/gcode_info.cpp`)
and turns what failed into a report in `CompatibilityReport::generate_without_toolmapping`
(`src/feature/compatibility_checks/gcode_compatibility.cpp`). Every failed check is a row on
one screen, and the worst severity among them decides it
(`src/feature/compatibility_checks/compatibility_checks_common.cpp`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from simulator import gcode
from simulator.compatibility import INDX_LOCK, INPUT_SHAPER, MMU3, Compatibility
from simulator.compatibility import compatibility as model_compatibility

if TYPE_CHECKING:
    from simulator.model import ModelInfo

# The G-code level the firmware takes (`gcode_level` in `src/common/gcode/gcode_info.hpp`).
GCODE_LEVEL = 2


class Severity(StrEnum):
    """What a failed check does: `HWCheckSeverity` in `include/common/hw_check.hpp`."""

    IGNORE = "ignore"  # no question for it
    WARN = "warn"  # the screen offers Print and Abort; the firmware's default
    ABORT = "abort"  # the screen offers only Abort


@dataclass(frozen=True)
class Failed:
    """One row of the preview's screen."""

    text: str
    fatal: bool


def preview(model: ModelInfo, text: str, severity: Severity) -> list[Failed]:
    """Return the rows of the screen the preview shows for a file, or no rows when it
    shows none.

    `severity` stands for every setting that governs a check: the model, compatibility
    mode, G-code level, input shaper and firmware settings, the last of which covers an
    MMU3 file with the MMU off and a feature the build doesn't know. The INDX lock is a
    fixed warning. A check under `ignore` asks nothing, but it is still a row when another
    check brings up the screen, as `frame_compat_checks.cpp` lists every failed check.
    """
    found = _failed(model, gcode.scan(text), severity)
    if all(s is Severity.IGNORE for _, s in found):
        return []
    return [Failed(t, s is Severity.ABORT) for t, s in found]


def _declares(checks: list[gcode.Check], feature: str) -> bool:
    return any(isinstance(c, gcode.FeatureCheck) and _same(c.name, feature) for c in checks)


def _same(a: str, b: str) -> bool:
    return a.upper() == b.upper()


def _failed(
    model: ModelInfo, checks: list[gcode.Check], severity: Severity
) -> list[tuple[str, Severity]]:
    """The failed checks' texts with their severities, in the report's order: the order of
    `GeneralCheck` in `src/feature/compatibility_checks/gcode_compatibility.hpp`, which
    `visit_set_bits` walks."""
    wrong, older, level = None, None, 0
    for c in checks:
        if isinstance(c, gcode.ModelCheck):
            match model_compatibility(model, c.name):
                case Compatibility.INCOMPATIBLE:
                    wrong = wrong or c
                case Compatibility.MODE:
                    older = older or c
        elif isinstance(c, gcode.LevelCheck):
            level = max(level, c.level)
    out = []
    if wrong is not None:
        out.append(f"for printer {wrong.name}" if wrong.name else "M862.3 names no printer")
    if older is not None:
        out.append(f"compatibility mode for {older.name}")
    if level > GCODE_LEVEL:
        out.append(f"G-code level {level}, above {GCODE_LEVEL}")
    if INPUT_SHAPER in model.checks.required and not _declares(checks, INPUT_SHAPER):
        out.append("not sliced with input shaper")
    found = [(t, severity) for t in out]
    if INDX_LOCK in model.checks.required and not _declares(checks, INDX_LOCK):
        found.append(("not sliced for the INDX lock", Severity.WARN))
    features = [c.name for c in checks if isinstance(c, gcode.FeatureCheck)]
    known = model.checks.features
    # The MMU is never on in the simulator, so a file sliced for one always fails.
    if MMU3 in known and any(_same(f, MMU3) for f in features):
        found.append(("sliced for an MMU3, which is off", severity))
    # A file sliced for the HT Hotend fails only when the hotend isn't one, which is
    # printer state the simulator doesn't hold, so it's taken as satisfied.
    unknown = [f for f in features if not any(_same(f, k) for k in known)]
    if unknown:
        found.append(("unsupported features: " + ", ".join(repr(f) for f in unknown), severity))
    return found


# The name a tool with no filament type reports, which no file's type matches
# (`src/common/filament.cpp`).
NO_TYPE = "---"


@dataclass(frozen=True)
class ToolView:
    """What the preview reads of one tool: whether it's enabled, whether its sensor sees
    filament, and the name of its stored type, or None for none."""

    enabled: bool
    loaded: bool
    material: str | None


@dataclass(frozen=True)
class PrintChecks:
    """What the preview asks about a print, in order.

    `rows` is the "G-code incompatible" screen. On a printer with tool mapping the per-tool
    rows go to `mapping`, the tools-mapping screen, instead. On one without, `empty` names
    the used tools with no filament, which the filament-not-inserted screen asks to load.
    The type check comes after that load, so `wrong_types` runs then.
    """

    rows: list[Failed]
    mapping: list[Failed]
    empty: list[int]


def tool_mapping(max_tools: int, uses: dict[int, str | None], tools: dict[int, ToolView]) -> bool:
    """Whether the print goes through the tools-mapping screen
    (`is_tool_mapping_possible` in `src/common/tools_mapping.cpp`).

    The toolchanger builds have tool mapping, and so do MMU builds with the MMU on, which
    the simulator never is. It applies when the print uses more than one tool, or when more
    than one tool is enabled.
    """
    enabled = sum(t.enabled for t in tools.values())
    return max_tools > 1 and (len(uses) > 1 or (enabled > 1 and len(uses) > 0))


def print_checks(
    max_tools: int, uses: dict[int, str | None], tools: dict[int, ToolView]
) -> PrintChecks:
    """Return the preview's questions about a print that uses the tools in `uses`, each with
    the filament type its file names or None.

    `generate_without_toolmapping` and `generate_toolmapping_only_noclear` in
    `src/feature/compatibility_checks/gcode_compatibility.cpp`, as
    `stateFromPrinterCheck` and `stateFromFilamentPresence` in
    `src/common/marlin_print_preview.cpp` use them. A print has no `M862` lines, so only
    these checks apply, and no setting governs any of them: more used tools than enabled
    ones warns, a disabled used tool aborts, and a used tool with no filament warns. The
    type check is left out of the first screen.
    """
    rows = []
    if len(uses) > sum(t.enabled for t in tools.values()):
        rows.append(Failed("the print uses more tools than are enabled", False))
    mapped = tool_mapping(max_tools, uses, tools)
    per_tool = []
    for index in sorted(uses):
        t = tools[index]
        if not t.enabled:  # the firmware checks nothing else on a disabled tool
            per_tool.append(Failed(f"T{index} is disabled", True))
            continue
        if not t.loaded:
            per_tool.append(Failed(f"T{index} has no filament", False))
        if mapped:
            per_tool += wrong_types({index: uses[index]}, tools)
    if mapped:
        return PrintChecks(rows, per_tool, [])
    empty = [i for i in sorted(uses) if tools[i].enabled and not tools[i].loaded]
    return PrintChecks(rows + per_tool, [], empty)


def wrong_types(uses: dict[int, str | None], tools: dict[int, ToolView]) -> list[Failed]:
    """The used, enabled tools whose stored type isn't the one the file names: an exact,
    case-sensitive comparison of names. A tool the file names no type for, or `---`, isn't
    checked."""
    out = []
    for index in sorted(uses):
        want, t = uses[index], tools[index]
        if want in (None, "", NO_TYPE) or not t.enabled:
            continue
        have = t.material or NO_TYPE
        if want != have:
            out.append(Failed(f"T{index} holds {have}, not {want}", False))
    return out
