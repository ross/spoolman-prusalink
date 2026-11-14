"""Watch the firmware source of the copied rules the check can't rebuild.

A few copies come from firmware that states its rule as control flow or across several
files, not as a table. For each, `WATCHED` records a fingerprint of the source text the
copy was read from, and the check fails when that text changes. A fingerprint says
nothing about whether the copy is right, only that its source changed since someone last
read it.

A region is one of four kinds, each found exactly once in its file:

- "function": a C++ function definition, by its name as the definition spells it, from
  the start of its line to its closing brace.
- "statement": a C++ statement, by the start of its first line: to its `;`, or, where a
  brace block comes first, to the block's closing brace. One with an `else` can't be read.
- "directive": a preprocessor line, by its start. A conditional branch (`#if`, `#elif`,
  `#else` and the like) runs to the next directive of the same conditional.
- "cmake": a CMake command, by its start. An `if()` runs to its `endif()`.

The fingerprint hashes the region with comments removed and each run of whitespace made
one space, so only a change to the code itself trips it.
"""

from __future__ import annotations

import functools
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from firmware import read
from firmware.source import Source, Unreadable

Kind = Literal["function", "statement", "directive", "cmake"]


@dataclass(frozen=True)
class Watched:
    name: str
    path: str  # the firmware file
    kind: Kind
    anchor: str  # the function's name, or the start of the statement, directive or command
    copy: str  # what was copied from the region, and where
    fingerprint: str
    commit: str  # the full hash of the firmware commit the region was last read at


# The upstream commit most fingerprints were read at.
UPSTREAM_7D8BF685 = "7d8bf6858b363c453c0617bceab5e66997d8fc25"

# The regions the copies come from. Each row's commit is the one its region was last read
# at, not the last one checked: a region whose text is the same at a newer commit stays as
# it is. When one changes, read the diff the check prints, fetching upstream first if the
# checkout lacks the newer commit, check the copy it names, and then record the
# fingerprint and commit the check prints. When the check says the recorded fingerprint is
# wrong, there's no diff to read: re-read the region at the recorded commit and the copy it
# names, then record the fingerprint the check found and keep the commit.
WATCHED = (
    # The upgrade paths: `UPGRADES` in simulator/compatibility.py.
    Watched(
        "the compatibility upgrades",
        "src/common/printer_model.cpp",
        "function",
        "gcode_compatibility_report_constexpr",
        "UPGRADES in simulator/compatibility.py",
        "bf8dbaf2d07d79c5",
        UPSTREAM_7D8BF685,
    ),
    # The next two guard `GCodeChecks.required` in simulator/compatibility.py. The first
    # requires the Input Shaper and INDX lock flags; the conditions sit among other checks in
    # this function, and nothing narrower can be found reliably. It also sets the "not enough
    # tools" check that `print_checks` in simulator/preview.py copies.
    Watched(
        "the required features",
        "src/feature/compatibility_checks/gcode_compatibility.cpp",
        "function",
        "CompatibilityReport::generate_without_toolmapping",
        "GCodeChecks.required in simulator/compatibility.py and print_checks in"
        " simulator/preview.py",
        "b3cf2415e667a40b",
        UPSTREAM_7D8BF685,
    ),
    # The second decides which `M862.6` name sets which flag. It parses every `M862` line, so
    # it also trips on unrelated changes: 8 commits touched it in 2026 up to 7d8bf6858.
    Watched(
        "the M862 parser",
        "src/common/gcode/gcode_info.cpp",
        "function",
        "GCodeInfo::parse_m862",
        "GCodeChecks.required in simulator/compatibility.py, and the MMU3 check that only an"
        " Active MMU passes in preview in simulator/preview.py",
        "7e4c129c699f5571",
        UPSTREAM_7D8BF685,
    ),
    # The per-tool checks of a print: the disabled tool, the missing filament and the wrong
    # type in `print_checks` and `wrong_types` in simulator/preview.py. The function runs
    # every per-tool check, nozzles and spool join among them, so it also trips on unrelated
    # changes: about 20 commits touched it in 2026 up to 7d8bf6858.
    Watched(
        "the tool checks",
        "src/feature/compatibility_checks/gcode_compatibility.cpp",
        "function",
        "CompatibilityReport::generate_toolmapping_only_noclear",
        "print_checks and wrong_types in simulator/preview.py, and the filament check an"
        " Active MMU skips in print_checks' loaded_checked and SimPrinter._loaded_checked in"
        " simulator/model.py",
        "57407f963fde6b8d",
        UPSTREAM_7D8BF685,
    ),
    # When a print goes through the tools-mapping screen: `tool_mapping` in
    # simulator/preview.py.
    Watched(
        "the tool mapping condition",
        "src/common/tools_mapping.cpp",
        "function",
        "is_tool_mapping_possible",
        "tool_mapping in simulator/preview.py and Mmu.maps_tools in simulator/feed.py",
        "136be5698acb0b96",
        UPSTREAM_7D8BF685,
    ),
    # The main boards besides the buddy boards: `OTHER_MAIN_BOARDS` in firmware/compare.py.
    Watched(
        "the main board condition",
        read.OPTIONS_FILE,
        "cmake",
        "if(BOARD IN_LIST BUDDY_BOARDS OR",
        "OTHER_MAIN_BOARDS in firmware/compare.py",
        "c6120ac728f42a92",
        UPSTREAM_7D8BF685,
    ),
    # The next six guard `BOARD_DEFINES` in firmware/compare.py: of the main boards, only
    # XLBUDDY and XL_DEV_KIT_XLB define `BOARD_IS_XLBUDDY()`, none defines
    # `BOARD_IS_DWARF()`, and both macros are 0 where no board defines them.
    Watched(
        "the XLBUDDY board's macros",
        "include/device/board.h",
        "directive",
        "#elif BOARD() == BOARD_XLBUDDY()",
        "BOARD_DEFINES in firmware/compare.py",
        "1b5878d336656cec",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the XL dev kit board's macros",
        "include/device/board.h",
        "directive",
        "#elif BOARD() == BOARD_XL_DEV_KIT_XLB()",
        "BOARD_DEFINES in firmware/compare.py",
        "8d2733394e9e16e1",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the BUDDY board's macros",
        "include/device/board.h",
        "directive",
        "#elif BOARD() == BOARD_BUDDY()",
        "BOARD_DEFINES in firmware/compare.py",
        "34ada32dca12a264",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the XBUDDY board's macros",
        "include/device/board.h",
        "directive",
        "#elif BOARD() == BOARD_XBUDDY()",
        "BOARD_DEFINES in firmware/compare.py",
        "b366077de8f45a00",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the BOARD_IS_XLBUDDY default",
        "include/device/board.h",
        "directive",
        "#ifndef BOARD_IS_XLBUDDY",
        "BOARD_DEFINES in firmware/compare.py",
        "6bacb277dac47d33",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the BOARD_IS_DWARF default",
        "include/device/board.h",
        "directive",
        "#ifndef BOARD_IS_DWARF",
        "BOARD_DEFINES in firmware/compare.py",
        "04c9261a6da4db2d",
        UPSTREAM_7D8BF685,
    ),
    # The load's purge: the high-flow doubling in `SimPrinter._load_lengths`, and the
    # override and that the minimum applies in `SimPrinter._load_steps`, in
    # simulator/model.py. The minimum's value is compared as `MINIMAL_PURGE_MM`.
    Watched(
        "the purge length",
        "src/marlin_stubs/pause/pause_settings.cpp",
        "function",
        "pause::Settings::purge_length",
        "SimPrinter._load_lengths and _load_steps in simulator/model.py",
        "eebdf064fe4b03fc",
        UPSTREAM_7D8BF685,
    ),
    # The next six guard the power cut and its resume: `Extruder.panic_retract` and
    # `panic_purge_mm` in simulator/tool.py, and `SimPrinter._reboot`, `_unpark` and
    # `_nozzle_clean` in simulator/model.py. The purge's length is compared as
    # `panic_purge_mm`. Which builds retract: the Dwarf builds don't.
    Watched(
        "the power-panic retract rule",
        "src/common/power_panic.cpp",
        "statement",
        "constexpr bool power_panic_retracts",
        "Extruder.panic_retract in simulator/tool.py",
        "8c0d0a81c1b75d9e",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the Dwarf builds",
        read.OPTIONS_FILE,
        "cmake",
        "set(PRINTERS_WITH_DWARF",
        "Extruder.panic_retract in simulator/tool.py",
        "261e96818ab33f70",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the HAS_DWARF condition",
        read.OPTIONS_FILE,
        "cmake",
        "if(${PRINTER} IN_LIST PRINTERS_WITH_DWARF",
        "Extruder.panic_retract in simulator/tool.py",
        "b27257412fa97fed",
        UPSTREAM_7D8BF685,
    ),
    # Which builds feed the cut's retract back: those that retract and don't purge.
    Watched(
        "the power-panic feed-back rule",
        "src/common/power_panic.cpp",
        "statement",
        "constexpr bool power_panic_deretracts",
        "Extruder.panic_purge_mm in simulator/tool.py and SimPrinter._unpark in simulator/model.py",
        "56342b3966db2f01",
        UPSTREAM_7D8BF685,
    ),
    # The cut's retract, which a paused print skips.
    Watched(
        "the power cut's retract",
        "src/common/power_panic.cpp",
        "statement",
        "if (power_panic_retracts &&",
        "SimPrinter._reboot in simulator/model.py",
        "86c46876b2341282",
        UPSTREAM_7D8BF685,
    ),
    # The resume: which builds purge or wipe, where the feed-back runs, and that a paused
    # print skips both. The function runs every step of the resume, so it also trips on
    # unrelated changes: about 24 commits touched it in 2026 up to 7d8bf6858.
    Watched(
        "the power-panic resume",
        "src/common/power_panic.cpp",
        "function",
        "resume_loop",
        "SimPrinter._recover, _unpark and _nozzle_clean in simulator/model.py, which have no"
        " MMU step",
        "1c614228e7ef6377",
        UPSTREAM_7D8BF685,
    ),
    # The MMU3. Which slots the printer lists: all five while the MMU's setting is on.
    Watched(
        "the listed MMU slots",
        "src/common/tool_index.cpp",
        "function",
        "VirtualToolIndex::is_enabled",
        "SimPrinter.listed in simulator/model.py",
        "014fb6a7f83f64d0",
        UPSTREAM_7D8BF685,
    ),
    # The slot an E move counts on: slot 0 until the MMU is Active, then the selected one.
    Watched(
        "the MMU's selected slot",
        "src/common/tool_index.cpp",
        "function",
        "PhysicalExtension::currently_selected_virtual_tool",
        "Mmu.credited and Mmu.head_tool in simulator/feed.py",
        "77f6c47bb051be3d",
        UPSTREAM_7D8BF685,
    ),
    # An E move with no slot selected runs and counts on no slot.
    Watched(
        "the counted E move",
        "lib/Marlin/Marlin/src/module/planner.cpp",
        "statement",
        "if(tools.virtual_tool.has_value())",
        "SimPrinter._count in simulator/model.py, which leaves an MMU print's start and its"
        " first tool change uncounted (Mmu.prime in simulator/feed.py)",
        "ecec270c18660d34",
        UPSTREAM_7D8BF685,
    ),
    # The menu switch, `M709 S1` and `S0`.
    Watched(
        "the MMU switched on",
        "src/marlin_stubs/pause/M70X_mmu.cpp",
        "function",
        "filament_gcodes::mmu_on",
        "SimPrinter.set_mmu and _start_mmu in simulator/model.py",
        "278917ea3aa4ec0f",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU switched off",
        "src/marlin_stubs/pause/M70X_mmu.cpp",
        "function",
        "filament_gcodes::mmu_off",
        "SimPrinter.set_mmu and _mmu_off in simulator/model.py",
        "1b7cb90c7e30979f",
        UPSTREAM_7D8BF685,
    ),
    # What PrusaLink reports: the flow with no slot selected, and the MMU's setting. The
    # setting's statement ends in an `#else`, so the whole `/api/v1/info` renderer is watched.
    Watched(
        "the status flow",
        "lib/WUI/nhttp/status_renderer.cpp",
        "statement",
        'JSON_FIELD_INT(" ", VirtualToolIndex::currently_selected_opt()',
        "_flow in simulator/prusalink.py",
        "041e4cccda4bc85c",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the info mmu field",
        "lib/WUI/link_content/basic_gets.cpp",
        "function",
        "get_info",
        "info in simulator/prusalink.py",
        "77e99b5297d36f30",
        UPSTREAM_7D8BF685,
    ),
    # The MMU menu's operations: Load To Nozzle, Unload Filament, Preload and Preload All.
    Watched(
        "the MMU tool change",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::ToolChangeCommonOnce",
        "_mmu_change_steps in simulator/model.py, its -80 mm with a slot selected, and"
        " change_feed and UNLOAD_FINISH_MM in simulator/mmu.py",
        "5f3fa79cacb9939c",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU tool change's retries",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::ToolChangeCommon",
        "_mmu_change_steps in simulator/model.py",
        "b8ec90350bb2a3a0",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU sensor feed",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::OnMMUProgressMsgChanged",
        "change_feed in simulator/mmu.py",
        "ba54a190af028218",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU feed past the sensor",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::OnMMUProgressMsgSame",
        "change_feed in simulator/mmu.py",
        "7e6730b54b77bff0",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU E-stall feed",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::FeedWithEStallDetection",
        "change_feed in simulator/mmu.py",
        "568ec1eba95ef338",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU unload's auto-retract check",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::UnloadObeyAutoRetracted",
        "_mmu_unload_steps in simulator/model.py",
        "9b0b7e7ca888dfbe",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU unload",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::UnloadInner",
        "_mmu_unload_steps in simulator/model.py and RELIEVE_MM in simulator/mmu.py",
        "7a00acf172574eb1",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU menu unload",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::unload",
        "SimPrinter.unload in simulator/model.py and Mmu.end_unload in simulator/feed.py",
        "bae1f87859a3ff48",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU load to the nozzle",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::load_filament_to_nozzle",
        "SimPrinter.load_to_nozzle in simulator/model.py",
        "2127d8d4ebf0be03",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU nozzle load sequence",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::execute_load_to_nozzle_sequence",
        "nozzle_load in simulator/mmu.py",
        "9a3e45aed3673e53",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU nozzle load lengths",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_config.h",
        "directive",
        "#define MMU2_LOAD_TO_NOZZLE_SEQUENCE"
        " { { MMU2_EXTRUDER_HEATBREAK_LENGTH - MMU2_FEED_DISTANCE",
        "nozzle_load in simulator/mmu.py",
        "5d9e81fdae9368a4",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the change all",
        "src/common/multi_filament_change.cpp",
        "function",
        "Config::execute",
        "SimPrinter.change_all and _next_queued in simulator/model.py",
        "cafa08a104863377",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU Preload All",
        "src/gui/screen/screen_menu_mmu_preload_all.cpp",
        "function",
        "MI_FILAMENT::click",
        "SimPrinter.preload_all in simulator/model.py",
        "553a3e68fe311176",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU menu's loaded-filament guard",
        "src/gui/screen_menu_filament_mmu.cpp",
        "function",
        "ScreenMenuFilamentMMU::windowEvent",
        "_check_mmu_unloaded in simulator/model.py",
        "38b8373e3e2c0bb9",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU Load To Nozzle item",
        "src/gui/MItem_mmu.cpp",
        "function",
        "MI_MMU_LOAD_TO_NOZZLE_I::click",
        "SimPrinter.load_to_nozzle in simulator/model.py",
        "edf1d82dd8e36773",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU Load To Nozzle item's type guard",
        "src/gui/MItem_mmu.cpp",
        "statement",
        "set_enabled(filament_type_ != FilamentType::none)",
        "_check_load_to_nozzle in simulator/model.py",
        "203c1dce15628a64",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU menu's load start",
        "src/marlin_stubs/pause/pause.cpp",
        "function",
        "Pause::mmu_load_start_process",
        "SimPrinter.load_to_nozzle in simulator/model.py, and the wait for a load into the"
        " MMU in SimPrinter._filament_change",
        "c5e0dd0a8e90e05f",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU menu's unload start",
        "src/marlin_stubs/pause/pause.cpp",
        "function",
        "Pause::mmu_unload_start_process",
        "SimPrinter.unload and the MMU's unload and eject in SimPrinter._filament_change in"
        " simulator/model.py, and Mmu.end_unload in simulator/feed.py",
        "11258212d7c70ba0",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU eject",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::eject_filament",
        "SimPrinter.eject and _mmu_operation in simulator/model.py",
        "d31efdaa24c7212a",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU cut",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::cut_filament",
        "SimPrinter.cut and _mmu_operation in simulator/model.py",
        "b84fc4701fc2b489",
        UPSTREAM_7D8BF685,
    ),
    # A boot starts an MMU whose setting is on. `MMU2::Start` itself, which `_start_mmu`
    # copies, has a stub of the same name in its file's `#else`, so no region can hold it.
    Watched(
        "the MMU started at boot",
        "src/common/appmain.cpp",
        "statement",
        "if (config_store().mmu2_enabled.get())",
        "SimPrinter.boot in simulator/model.py",
        "e782e040640e03d2",
        UPSTREAM_7D8BF685,
    ),
    # Turning the sensors off turns the MMU off, and the MMU forces the sensors on.
    Watched(
        "the sensors switched off with the MMU on",
        "src/feature/filament_sensor/filament_sensors_handler.cpp",
        "function",
        "FilamentSensors::process_enable_state_update",
        "SimPrinter.set_sensors in simulator/model.py",
        "80238e64a5268e34",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the sensors the MMU forces on",
        "src/feature/filament_sensor/filament_sensors_handler.cpp",
        "function",
        "should_enable",
        "SimPrinter.set_sensors and _mmu_off in simulator/model.py",
        "b029dfc35266ef82",
        UPSTREAM_7D8BF685,
    ),
    # The idle sensor clear is off while the setting is on. The setting's statement ends in an
    # `#else`, so its function is watched.
    Watched(
        "the sensors' MMU setting",
        "src/feature/filament_sensor/filament_sensors_handler.cpp",
        "function",
        "FilamentSensors::reconfigure_sensors_if_needed",
        "Mmu.idle_clear_tool in simulator/feed.py",
        "7700e1f5554bf978",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the idle sensor clear with an MMU",
        "src/feature/filament_sensor/filament_sensors_handler.cpp",
        "statement",
        "if (!has_mmu && no_filament_surely(LogicalFilamentSensor::closest_to_nozzle)",
        "Mmu.idle_clear_tool in simulator/feed.py",
        "4d2b4c8ab15e9e2d",
        UPSTREAM_7D8BF685,
    ),
    # A print's end skips the auto-retract while the MMU is Active, and a stop or a
    # single-tool file unloads instead. The branch has an `else`.
    Watched(
        "the end of a print with an MMU",
        "src/common/marlin_server.cpp",
        "function",
        "pre_finalize_print",
        "SimPrinter._end_of_print in simulator/model.py: the auto-retract skipped, and the"
        " unload of a stopped job or a finished single-tool file",
        "12f5dc375c32c406",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the end of a print's MMU unload",
        "src/common/marlin_server.cpp",
        "function",
        "safely_unload_filament_from_nozzle_to_mmu",
        "SimPrinter._end_of_print in simulator/model.py: no unload with the extruder empty",
        "4833bcc6699b4461",
        UPSTREAM_7D8BF685,
    ),
    # Which file is single-tool: tool 0 used, and no other tool given in the metadata at
    # all. An MMU3 profile file lists every slot, so only a file sliced without it is one.
    Watched(
        "the single-tool file",
        "src/common/gcode/gcode_info.cpp",
        "function",
        "GCodeInfo::is_singletool_gcode",
        "PrintSpec.single_tool_file and SimPrinter._check_single_tool_file in"
        " simulator/model.py, and the single-tool tests in Mmu.prime in simulator/feed.py"
        " and SimPrinter._end_of_print, and the preview's shortcut in SimPrinter._mmu_shortcut",
        "e37dbf978f89ee82",
        UPSTREAM_7D8BF685,
    ),
    # The firmware's load at the start of a single-tool file with no slot selected.
    Watched(
        "the single-tool file's start",
        "src/common/marlin_server.cpp",
        "statement",
        "if (MMU2::mmu2.Enabled() && GCodeInfo::getInstance().is_singletool_gcode()",
        "Mmu.prime in simulator/feed.py and SINGLE_TOOL_LOAD in simulator/mmu.py",
        "6ece29d270dd8ac3",
        UPSTREAM_7D8BF685,
    ),
    # Preload All writes slots 0 and 1 at once, and slot k's type when slot k - 2's preload
    # ends: the menu waits while the G-code queue holds more than one command. The queue's
    # length counts a command until it returns, and the menu reads it after each request it
    # sends, refreshed before the acknowledgement.
    Watched(
        "the G-code queue's length for a client",
        "src/common/marlin_server.cpp",
        "statement",
        "marlin_vars().gqueue = queue.length",
        "the two preloads ahead in SimPrinter.preload_all and Queued.writes in simulator/model.py",
        "b8cd11a967bc2844",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the refresh before a request's acknowledgement",
        "src/common/marlin_server.cpp",
        "function",
        "_process_server_request",
        "the two preloads ahead in SimPrinter.preload_all and Queued.writes in simulator/model.py",
        "d6e59c11bbbc65ca",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the G-code queue's advance",
        "lib/Marlin/Marlin/src/gcode/queue.cpp",
        "function",
        "GCodeQueue::advance",
        "the two preloads ahead in SimPrinter.preload_all and Queued.writes in simulator/model.py",
        "433710015bf001df",
        UPSTREAM_7D8BF685,
    ),
    # A tool is given in a file's metadata when it lists any filament used for it, zero
    # too: the single-tool rule leans on it.
    Watched(
        "a tool given in the metadata",
        "src/common/gcode/gcode_info.hpp",
        "statement",
        "inline bool given() const",
        "the single-tool rule: PrintSpec.single_tool_file in simulator/model.py and"
        " Mmu.prime in simulator/feed.py",
        "a23abdb1628d7363",
        UPSTREAM_7D8BF685,
    ),
    # A `T` while the MMU connects waits, and its feed counts on no slot, only once
    # `MMU2::Start` has handed over to the protocol. A build whose MMU talks UART, the MK4
    # family's, runs the MMU's bootloader first, and there a `T`'s request goes out at once
    # and its wait ends at the first pass. That phase isn't modeled. `MMU2::Start` itself
    # can't be watched (see "the MMU started at boot").
    Watched(
        "the builds with the MMU on UART",
        read.OPTIONS_FILE,
        "cmake",
        'if(${BOARD} STREQUAL "XBUDDY" AND HAS_MMU2)',
        "SimPrinter._mmu_change in simulator/model.py: a `T` waits while the MMU connects",
        "c0faba2142c463c6",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the builds that run the MMU's bootloader",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.h",
        "directive",
        "#elif HAS_MMU2_OVER_UART()",
        "SimPrinter._mmu_change in simulator/model.py: a `T` waits while the MMU connects",
        "07c135661af13830",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU's bootloader phase",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::mmu_loop_inner",
        "SimPrinter._mmu_change in simulator/model.py: a `T` waits while the MMU connects",
        "727d1bf0385f9c87",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU request sent at once",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/protocol_logic.cpp",
        "function",
        "ProtocolLogic::PlanGenericRequest",
        "SimPrinter._mmu_change in simulator/model.py: a `T` waits while the MMU connects",
        "d87749532e5d2649",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU request activated",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/protocol_logic.cpp",
        "function",
        "ProtocolLogic::ActivatePlannedRequest",
        "SimPrinter._mmu_change in simulator/model.py: a `T` waits while the MMU connects",
        "3e7e32a59e5138de",
        UPSTREAM_7D8BF685,
    ),
    # A print's `T` to the slot already selected changes nothing. The name has two
    # overloads, so the definition is read as a statement.
    Watched(
        "the MMU tool change in a print",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "statement",
        "bool MMU2::tool_change(uint8_t slot)",
        "SimPrinter._mmu_change in simulator/model.py",
        "045f566761af0f0a",
        UPSTREAM_7D8BF685,
    ),
    # The print preview with an MMU: the question to unload first, unless FINDA's sensor
    # reads no filament, so while the MMU connects too, and the single-tool shortcut past
    # it; then its answers. The answers sit in the preview's whole state machine, so that
    # row also trips on unrelated changes. No is `Result::Inactive`, which the server
    # starts like Print.
    Watched(
        "the MMU's preview question",
        "src/common/marlin_print_preview.cpp",
        "function",
        "PrintPreview::stateFromFilamentPresence",
        "SimPrinter._ask_mmu_unload and _mmu_shortcut in simulator/model.py: asked with the"
        " MMU on, while it connects whatever FINDA sees",
        "e6e4aab68afb170b",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU's preview answers",
        "src/common/marlin_print_preview.cpp",
        "function",
        "PrintPreview::Loop",
        "SimPrinter.tap_through, unload_first and _process_done in simulator/model.py: No"
        " prints with no mapping screen, Yes unloads and then the checks are done",
        "f19a65b3c6c48c0a",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU preview's No",
        "src/common/marlin_server.cpp",
        "statement",
        "case PrintPreview::Result::Print:",
        "SimPrinter.tap_through in simulator/model.py: No at the MMU's question prints",
        "be51a6c5eecc7226",
        UPSTREAM_7D8BF685,
    ),
    # The menu turns the MMU on only with a working extruder sensor.
    Watched(
        "the MMU menu switch",
        "src/gui/MItem_mmu.cpp",
        "function",
        "MI_MMU_ENABLE::OnChange",
        "SimPrinter._check_mmu in simulator/model.py",
        "b07a63f0acf7542e",
        UPSTREAM_7D8BF685,
    ),
    # The tag reader is off while the setting is on.
    Watched(
        "the tag radio with the MMU on",
        "src/feature/openprinttag/request_manager.cpp",
        "directive",
        "#if (HAS_MMU2() && !HAS_ANFC_FOR_MULTIPLE_TOOLS())",
        "SimPrinter._new_material in simulator/model.py",
        "d915a8eed6ae6179",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the tag reader with the MMU on",
        "src/feature/openprinttag/tool_tag_buddy.cpp",
        "function",
        "has_tool_openprinttag_reader",
        "SimPrinter._new_material in simulator/model.py",
        "ca43809fc54c58c2",
        UPSTREAM_7D8BF685,
    ),
    # The MMU's filament menu has no plain Load.
    Watched(
        "the MMU's filament menu",
        "src/gui/screen_menu_filament_mmu.hpp",
        "statement",
        "using ScreenMenuFilamentMMU__ =",
        "SimPrinter._check_load in simulator/model.py",
        "b907808f273c2844",
        UPSTREAM_7D8BF685,
    ),
    # A runout with the MMU on is FINDA's, and names the selected slot or injects nothing.
    Watched(
        "the runout",
        "src/feature/filament_sensor/filament_sensors_handler.cpp",
        "function",
        "FilamentSensors::process_events",
        "SimPrinter.runout and _check_finda_runout in simulator/model.py",
        "0435ba7d6327d7f8",
        UPSTREAM_7D8BF685,
    ),
    # `M600` with no tool named and none selected does nothing.
    Watched(
        "the M600 with no tool",
        "src/marlin_stubs/pause/M600.cpp",
        "function",
        "M600_manual",
        "the color change with no slot selected in SimPrinter._print_step in simulator/model.py",
        "23d374077fddf0c9",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "Tune's Change filament",
        "src/gui/screen/screen_m600.cpp",
        "function",
        "MI_M600::click",
        "SimPrinter.change_filament and _check_change_filament in simulator/model.py",
        "c3da1d65dc0f9887",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU filament change's reload",
        "src/marlin_stubs/pause/pause.cpp",
        "function",
        "Pause::mmu_load_process",
        "SimPrinter._mmu_reload_steps in simulator/model.py",
        "96e209d63c1c2a8a",
        UPSTREAM_7D8BF685,
    ),
    Watched(
        "the MMU spool join",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::tool_change_full",
        "SimPrinter._mmu_join in simulator/model.py",
        "221be9b97aae93e2",
        UPSTREAM_7D8BF685,
    ),
    # A `T` while the MMU connects goes ahead and waits for the MMU's answer.
    Watched(
        "the MMU's readiness wait",
        "lib/Marlin/Marlin/src/feature/prusa/MMU2/mmu2_mk4.cpp",
        "function",
        "MMU2::WaitForMMUReady",
        "SimPrinter._mmu_change in simulator/model.py: a T while the MMU connects",
        "21e2e2aaec06103b",
        UPSTREAM_7D8BF685,
    ),
    # A spool join on an MMU: no G-code tool mapped falls back to `M600`, and the MMU's
    # branch has no dialog, purge or restore.
    Watched(
        "the spool join",
        "lib/Marlin/Marlin/src/module/prusa/spool_join.cpp",
        "function",
        "SpoolJoin::do_join",
        "SimPrinter._mmu_join in simulator/model.py",
        "b92f5b6187f3791e",
        UPSTREAM_7D8BF685,
    ),
    # FINDA's sensor reads not initialized while the MMU connects.
    Watched(
        "FINDA's sensor",
        "src/feature/filament_sensor/filament_sensor_mmu.cpp",
        "function",
        "FSensorMMU::cycle",
        "SimPrinter._check_finda_runout (the Busy while the MMU connects) and"
        " SimPrinter._ask_mmu_unload (the question while it connects) in simulator/model.py",
        "ddbb10b71cb63b1f",
        UPSTREAM_7D8BF685,
    ),
    # With the MMU on, only its questions and errors read ATTENTION in a print.
    Watched(
        "the filament change's state in a print",
        "src/state/printer_state.cpp",
        "function",
        "load_unload_attention_while_printing",
        "SimPrinter._mmu_reloading in simulator/model.py: PRINTING while the MMU's reload runs",
        "50948269e679c31f",
        UPSTREAM_7D8BF685,
    ),
)


@dataclass(frozen=True)
class Change:
    watched: Watched
    now: str  # the region's fingerprint now
    commit: str  # the full hash of the commit checked

    def __str__(self) -> str:
        w = self.watched
        if self.commit == w.commit:
            return (
                f"{w.name} in {w.path} has fingerprint {self.now} at {_short(w.commit)}, its"
                f" recorded commit, not {w.fingerprint}: the recorded fingerprint is wrong; check"
                f" {w.copy}"
            )
        return (
            f"{w.name} in {w.path} changed since {_short(w.commit)}: check {w.copy} against"
            f" the diff, then record fingerprint {self.now} and commit {self.commit}\n"
            f"    in a firmware checkout, after git fetch if it lacks {_short(self.commit)}:"
            f" git diff {_short(w.commit)}..{_short(self.commit)} -- {w.path}"
        )


def _short(commit: str) -> str:
    return commit[:12]


def changes(source: Source, watched: Iterable[Watched] = WATCHED) -> list[Change]:
    """Return each watched region whose fingerprint isn't the recorded one."""
    text = functools.cache(source.read)
    found = []
    for w in watched:
        now = fingerprint(region(text(w.path), w))
        if now != w.fingerprint:
            found.append(Change(w, now, source.commit))
    return found


def fingerprint(region: str) -> str:
    """Return the first 16 hex digits of the SHA-256 of `region` with its whitespace
    runs made single spaces. `region` is already free of comments."""
    normal = " ".join(region.split())
    return hashlib.sha256(normal.encode()).hexdigest()[:16]


def region(text: str, watched: Watched) -> str:
    """Return the text of `watched`'s region in its file's `text`, without comments."""
    try:
        if watched.kind == "cmake":
            return _cmake(_strip_cmake_comments(text), watched.anchor)
        text = read._strip_comments(text)
        if watched.kind == "function":
            return _function(text, watched.anchor)
        if watched.kind == "statement":
            return _statement(text, watched.anchor)
        return _directive(text, watched.anchor)
    except ValueError as e:
        raise Unreadable(watched.path, f"{e}, for {watched.name}") from None


def _function(text: str, name: str) -> str:
    masked = _mask_literals(text)
    parts = (re.escape(p) for p in name.split("::"))
    head = re.compile(r"(?<![\w:.>~])" + r"\s*::\s*".join(parts) + r"\s*\(")
    # Between the parameters and the body only qualifiers such as `const` may stand, so a
    # call such as `if (name(x)) {` isn't taken for the definition.
    qualifiers = re.compile(r"[\w\s:&*<>,]*\{")
    found = []
    for m in head.finditer(masked):
        body = qualifiers.match(masked, _closing(masked, m.end() - 1, "(", ")"))
        if body:
            found.append((m.start(), body.end() - 1))
    if len(found) != 1:
        raise ValueError(f"{len(found)} definitions of {name}")
    start, brace = found[0]
    start = masked.rfind("\n", 0, start) + 1
    return text[start : _closing(masked, brace, "{", "}")]


def _statement(text: str, anchor: str) -> str:
    masked = _mask_literals(text)
    starts, offset = [], 0
    for line in masked.splitlines(keepends=True):
        if _starts(" ".join(line.split()), anchor):
            starts.append(offset + len(line) - len(line.lstrip()))
        offset += len(line)
    if len(starts) != 1:
        raise ValueError(f"{len(starts)} lines start {anchor}")
    depth = 0
    for i in range(starts[0], len(masked)):
        c = masked[i]
        depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(c, 0)
        if depth == 0 and c in ";{":
            end = i + 1 if c == ";" else _closing(masked, i, "{", "}")
            # An `else` belongs to the statement, and the region would leave it out. One
            # can follow preprocessor lines.
            if re.match(r"(?:\s*#(?:\\\n|[^\n])*)*\s*else(?!\w)", masked[end:]):
                raise ValueError(f"{anchor} has an else")
            return text[starts[0] : end]
    raise ValueError(f"{anchor} isn't closed")


def _closing(text: str, start: int, opening: str, closing: str) -> int:
    """Return the index just past the `closing` that matches the `opening` at `start`."""
    depth = 0
    for i in range(start, len(text)):
        depth += {opening: 1, closing: -1}.get(text[i], 0)
        if depth == 0:
            return i + 1
    raise ValueError(f"unbalanced {opening}{closing}")


# A string or character literal. A digit separator, as in `8'000`, follows a word character.
_LITERAL = re.compile(r'"(?:[^"\\\n]|\\.)*"|(?<![\w\'])\'(?:[^\'\\\n]|\\.)+\'')


def _mask_literals(text: str) -> str:
    """Blank the inside of each literal, keeping every index, so its braces don't count."""
    return _LITERAL.sub(lambda m: m.group()[0] + " " * (len(m.group()) - 2) + m.group()[0], text)


_BRANCH = re.compile(r"#\s*(if|ifdef|ifndef|elif|else|endif)\b")


def _directive(text: str, anchor: str) -> str:
    lines = text.replace("\\\n", " ").splitlines()
    starts = [i for i, line in enumerate(lines) if _starts(_directive_text(line), anchor)]
    if len(starts) != 1:
        raise ValueError(f"{len(starts)} lines start {anchor}")
    first = starts[0]
    kind = _BRANCH.match(lines[first].strip())
    if not kind or kind.group(1) == "endif":
        return lines[first]
    depth = 0
    for i in range(first + 1, len(lines)):
        m = _BRANCH.match(lines[i].strip())
        if not m:
            continue
        if m.group(1).startswith("if"):
            depth += 1
        elif depth:
            depth -= m.group(1) == "endif"
        else:
            return "\n".join(lines[first:i])
    raise ValueError(f"{anchor} isn't closed")


def _directive_text(line: str) -> str:
    return re.sub(r"^#\s*", "#", " ".join(line.split()))


def _starts(statement: str, anchor: str) -> bool:
    return re.match(re.escape(anchor) + r"(?!\w)", statement) is not None


def _cmake(text: str, anchor: str) -> str:
    commands = list(read._cmake_commands(text))
    statements = [f"{name}({' '.join(args.split())})" for name, args, _ in commands]
    starts = [i for i, s in enumerate(statements) if _starts(s, anchor)]
    if len(starts) != 1:
        raise ValueError(f"{len(starts)} commands start {anchor}")
    first = last = starts[0]
    if commands[first][0] == "if":
        depth = 0
        for last in range(first, len(commands)):
            depth += {"if": 1, "endif": -1}.get(commands[last][0], 0)
            if depth == 0:
                break
        else:
            raise ValueError(f"{anchor} isn't closed")
    _, args, line = commands[last]
    lines = text.splitlines()
    return "\n".join(lines[commands[first][2] - 1 : line + args.count("\n")])


def _strip_cmake_comments(text: str) -> str:
    return re.sub(r'("(?:[^"\\]|\\.)*")|#[^\n]*', lambda m: m.group(1) or "", text)
