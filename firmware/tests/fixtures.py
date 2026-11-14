"""Small excerpts of the firmware files, in the shapes the readers expect."""

from firmware import read

MODELS_HPP = """\
inline constexpr std::array printer_model_info {
    PrinterModelInfo {
        .model = PrinterModel::mk3,
        .compatibility_group = PrinterModelCompatibilityGroup::mk3,
        .version = { 1, 3, 0 },
        .help_url = nullptr,
        .usb_pid = 0,
        .gcode_check_code = 300,
        .id_str = "MK3",
    },
    PrinterModelInfo {
        .model = PrinterModel::mk4,
        .compatibility_group = PrinterModelCompatibilityGroup::mk4,
        .version = { 1, 4, 0 },
        .help_url = "mk4",
        .usb_pid = 13,
        .gcode_check_code = 130,
        .id_str = "MK4",
    },
    PrinterModelInfo {
        .model = PrinterModel::mini,
        .compatibility_group = PrinterModelCompatibilityGroup::mini,
        .version = { 2, 1, 0 },
        .help_url = "mini",
        .usb_pid = 12,
        .gcode_check_code = 120,
        .id_str = "MINI",
    },
    PrinterModelInfo {
        .model = PrinterModel::coreone,
        .compatibility_group = PrinterModelCompatibilityGroup::coreone,
        .version = { 7, 1, 0 },
        .help_url = "core-one",
        .usb_pid = 31,
        .gcode_check_code = 310,
        .id_str = "COREONE",
    },
};

inline constexpr std::array printer_model_mmu_variant {
    PrinterModelMMUVariant {
        .model = PrinterModel::mk3,
        .gcode_check_code = 20300,
        .id_str = "MK3MMU2",
    },
    PrinterModelMMUVariant {
        .model = PrinterModel::mk4,
        .gcode_check_code = 30130,
        .id_str = "MK4MMU3",
    },
    // Oak shares PRINTER=COREONE so it inherits HAS_MMU2. This entry exists only
    // to satisfy the static_assert in printer_model.cpp — MMU is not expected on Oak.
    PrinterModelMMUVariant {
        .model = PrinterModel::coreone,
        .gcode_check_code = 30310,
        .id_str = "COREONEMMU3",
    },
};

constexpr const PrinterModelInfo &PrinterModelInfo::get_constexpr(PrinterModel model) {
    return printer_model_info[std::to_underlying(model)];
}
"""

GCODE_INFO_HPP = """\
#pragma once

#include <option/has_indx.h>
#include <option/has_mmu2.h>

// these strings are meant NOT to be translated
namespace gcode_info {
inline constexpr const char *time = "estimated printing time (normal mode)";
#if EXTRUDERS > 1
inline constexpr const char *filament_wipe_tower_g = "total filament used for wipe tower [g]";
#endif
inline constexpr const char *m862 = "M862";

// M862.6 feature tokens, spelled as the slicer emits them in P"...". Shared with the
// M862.6 Q response, which must answer in the same spelling it accepts.
inline constexpr const char *m862_6_input_shaper = "Input Shaper";
#if HAS_MMU2()
inline constexpr const char *m862_6_mmu = "MMU3";
#endif
#if HAS_INDX()
inline constexpr const char *m862_6_indx_lock = "INDX lock";
#endif
#if HAS_HT_HOTEND()
inline constexpr const char *m862_6_ht_hotend = "HT Hotend";
#endif

inline constexpr const char *m115 = "M115";
#if EXTRUDERS > 1
inline constexpr const char *total_toolchanges = "total toolchanges";
#endif
}; // namespace gcode_info
"""

OPTIONS_CMAKE = """\
set(PRINTER_VALID_OPTS
    "COREONE"
    "COREONE_INDX"
    "COREONEL"
    "COREONEL_INDX"
    "MINI"
    "MK4"
    "MK3.5"
    "XL"
    "iX"
    "XL_DEV_KIT"
    "NONE"
    )
set(PRINTER "<invalid>" CACHE STRING "Select the printer (valid values are ${PRINTER_VALID_OPTS}).")
set(BUDDY_BOARDS "BUDDY" "XBUDDY" "XLBUDDY")

# Set BOARD_IS_MASTER_BOARD - means main board of entire printer, non-main board are puppies
if(BOARD IN_LIST BUDDY_BOARDS OR BOARD STREQUAL "XL_DEV_KIT_XLB")
  set(BOARD_IS_MASTER_BOARD true)
else()
  set(BOARD_IS_MASTER_BOARD false)
endif()
define_boolean_option(BOARD_IS_MASTER_BOARD ${BOARD_IS_MASTER_BOARD})

set(PRINTERS_WITH_DWARF "XL" "XL_DEV_KIT")
if(${PRINTER} IN_LIST PRINTERS_WITH_DWARF AND BOARD_IS_MASTER_BOARD)
  set(HAS_DWARF YES)
else()
  set(HAS_DWARF NO)
endif()
define_boolean_option(HAS_DWARF ${HAS_DWARF})

if(BOARD IN_LIST BUDDY_BOARDS)
  set(RTT_METRICS_ENABLED
      ${DEBUG}
      CACHE BOOL "Enable metrics over rtt"
      )
else()
  # Puppies have no rtt_metrics implementation
  set(RTT_METRICS_ENABLED
      OFF
      CACHE BOOL "Enable metrics over rtt" FORCE
      )
endif()

function(set_feature_for_printers FEATURE_NAME)
  if(DEFINED ${FEATURE_NAME})
    set(FEATURE_VALUE ${${FEATURE_NAME}})
  endif()
endfunction()

set_feature_for_printers_master_board(
  HAS_LOADCELL
  "MK4"
  "iX"
  "XL"
  "XL_DEV_KIT" # a comment inside the call
  "COREONE"
  "COREONE_INDX"
  "COREONEL"
  "COREONEL_INDX"
  )
set_feature_for_printers_master_board(
  HAS_NEXTRUDER "MK4" "iX" "XL" "XL_DEV_KIT" "COREONE" "COREONEL"
  )
set_feature_for_printers_master_board(HAS_FILAMENT_HEATBREAK_PARAM "iX")
set_feature_for_printers_master_board(
  HAS_TOOLCHANGER "XL" "XL_DEV_KIT" "COREONE_INDX" "COREONEL_INDX"
  )
set_feature_for_printers_master_board(
  HAS_CHAMBER_API "XL" "COREONE" "COREONE_INDX" "COREONEL" "COREONEL_INDX"
  )
set_feature_for_printers_master_board(
  HAS_CHAMBER_FILTRATION_API "COREONE" "COREONE_INDX" "COREONEL" "COREONEL_INDX" "XL"
  )
set_feature_for_printers(HAS_HT_HOTEND "COREONE" "COREONEL")
set_feature_for_printers(
  HAS_FILAMENT_TRACKER
  "COREONE"
  "COREONE_INDX"
  "COREONEL"
  "COREONEL_INDX"
  "MK4"
  "iX"
  "XL"
  ) # TODO: a comment after the call
if(DEVELOPMENT_ITEMS_ENABLED)
  set_feature_for_printers_master_board(HAS_E2EE_SUPPORT "MINI" "UNITTESTS")
else()
  set_feature_for_printers_master_board(HAS_E2EE_SUPPORT "UNITTESTS")
endif()
set_feature_for_printers(HAS_ANFC "COREONE" "COREONEL")
set_feature_for_printers(HAS_INDX "COREONE_INDX" "COREONEL_INDX")
set_feature_for_printers(HAS_INDX_HEAD "COREONE_INDX" "COREONEL_INDX")
if(HAS_TOOLCHANGER AND NOT HAS_INDX)
  set(HAS_LOADCELL_HX717 "YES")
endif()
set_feature_for_printers_master_board(
  HAS_PAUSE
  "MINI"
  "MK4"
  "MK3.5"
  "iX"
  "XL"
  "XL_DEV_KIT"
  "COREONE"
  "COREONE_INDX"
  "COREONEL"
  "COREONEL_INDX"
  )
# POWER_PANIC requires SELFTEST and CRASH_DETECTION to work
set_feature_for_printers_master_board(
  HAS_POWER_PANIC
  "MK4"
  "MK3.5"
  "iX"
  "XL"
  "COREONE"
  "COREONE_INDX"
  "COREONEL"
  "COREONEL_INDX"
  )
set_feature_for_printers(HAS_MMU2 "MK4" "MK3.5" "COREONE" "COREONEL")
set_feature_for_printers(HAS_GCODE_COMPATIBILITY "MK3.5" "MK4" "COREONE" "COREONEL" "XL")
set_feature_for_printers(
  HAS_AUTO_RETRACT
  "COREONE"
  "COREONE_INDX"
  "COREONEL"
  "COREONEL_INDX"
  "MK4"
  "iX"
  "XL"
  )
set_feature_for_printers(HAS_SWITCHABLE_AUTO_RETRACT "COREONE" "COREONEL" "MK4" "iX" "XL")
set_feature_for_printers(HAS_NOZZLE_CLEANER "iX" "COREONE_INDX" "COREONEL_INDX")
set_feature_for_printers(HAS_NOZZLE_CLEANER_LITE "XL" "COREONE" "COREONEL")
if(HAS_TOOLCHANGER OR HAS_MMU2)
  define_boolean_option(HAS_TOOL_MAPPING yes)
endif()
"""

PRESETS_CPP = """\
#include "filament.hpp"

namespace {

struct PresetFilamentParameters {
    decltype(FilamentTypeParameters::name) name;
    decltype(FilamentTypeParameters::nozzle_temperature) nozzle_temperature;
    decltype(FilamentTypeParameters::nozzle_preheat_temperature) nozzle_preheat_temperature = 170;
    decltype(FilamentTypeParameters::heatbed_temperature) heatbed_temperature;
#if HAS_FILAMENT_HEATBREAK_PARAM()
    uint8_t heatbreak_temperature = DEFAULT_HEATBREAK_TEMPERATURE;
#endif
#if HAS_CHAMBER_API()
    std::optional<uint8_t> chamber_min_temperature = std::nullopt;
    std::optional<uint8_t> chamber_max_temperature = std::nullopt;
    std::optional<uint8_t> chamber_target_temperature = std::nullopt;
#endif
#if HAS_CHAMBER_FILTRATION_API()
    decltype(FilamentTypeParameters::requires_filtration) requires_filtration = false;
#endif
    bool is_abrasive : 1 = false;
    bool is_flexible : 1 = false;
#if HAS_HT_HOTEND()
    bool requires_ht_idler_door : 1 = false;
#endif
};

// These temperatures correspond to slicer defaults for MBL.
constexpr PresetFilamentParametersT<PresetFilamentParameters> preset_filament_parameters_constexpr {
    {
        PresetFilamentType::PLA,
        PresetFilamentParameters {
            .name = "PLA",
            .nozzle_temperature = 215,
            .heatbed_temperature = 60,
#if HAS_FILAMENT_HEATBREAK_PARAM()
            .heatbreak_temperature = 45,
#endif
#if HAS_CHAMBER_API()
            .chamber_min_temperature = 15,
            .chamber_max_temperature = 38,
            .chamber_target_temperature = 20,
#endif
        },
    },
        {
            PresetFilamentType::PC,
            PresetFilamentParameters {
                .name = "PC",
                .nozzle_temperature = 275,
                .nozzle_preheat_temperature = HAS_LOADCELL() ? (HAS_INDX() ? 230 : 275 - 25) : 170,
                .heatbed_temperature = 100,
#if HAS_CHAMBER_FILTRATION_API()
                .requires_filtration = true,
#endif
            },
        },
        {
            PresetFilamentType::PA,
            PresetFilamentParameters {
                .name = "PA",
                // MINI has slightly lower max nozzle temperature
                .nozzle_temperature = PRINTER_IS_PRUSA_MINI() ? 280 : 285,
                .nozzle_preheat_temperature = PRINTER_IS_PRUSA_MINI() ? 280 - 25 : 285 - 25,
                .heatbed_temperature = 100,
                .is_flexible = false,
            },
        },
#if HAS_HT_HOTEND()
        {
            PresetFilamentType::PPS,
            PresetFilamentParameters {
                .name = "PPS",
                .nozzle_temperature = 320,
                .heatbed_temperature = 105,
    #if HAS_CHAMBER_API()
                .chamber_min_temperature = PRINTER_IS_PRUSA_COREONEL() ? 55 : 45,
                .chamber_max_temperature = PRINTER_IS_PRUSA_COREONEL() ? 70 : 65,
                .chamber_target_temperature = PRINTER_IS_PRUSA_COREONEL() ? 60 : 55,
    #endif
                .is_abrasive = true,
                .requires_ht_idler_door = true,
            },
        },
#endif
};

} // namespace

FilamentTypeParameters preset_filament_parameters(PresetFilamentType t) {
    return {};
}
"""

FILAMENT_HPP = """\
struct FilamentTypeParameters {

public:
    /// Name of the filament (zero terminated).
    Name name;

    OptionalColor color = std::nullopt;

    /// Nozzle temperature for the filament, in degrees Celsius
    int16_t nozzle_temperature = 215;

    int16_t nozzle_preheat_temperature = 170;

    int16_t heatbed_temperature = 60;

#if HAS_CHAMBER_API()
    std::optional<uint8_t> chamber_min_temperature = std::nullopt;
#endif
};
"""

DATA_UTILS_CPP = """\
FilamentParametersInfo::FilamentParametersInfo(const RequestRef &req, const TagUID &tag_uid) {
    {
        auto base_type = FilamentType::from_name(abbreviation.abbreviation);

        // Necessary compatibility workaround. OPT uses actual chemistries for abbreviations,
        // while the Buddy is stuck with the "FLEX" preset name for TPU
        if (base_type == FilamentType::none && abbreviation.abbreviation == "TPU") {
            base_type = PresetFilamentType::FLEX;
        }
    }
}
"""

TRACKER_HPP = """\
#pragma once

#include <option/has_nextruder.h>

class FilamentTracker : Uncopyable {
public:
#if HAS_NEXTRUDER()
    static constexpr float extruder_to_nozzle_distance = 40.f; // mm
#elif HAS_INDX_HEAD()
    static constexpr float extruder_to_nozzle_distance = 27.f; // mm
#else
    #error
#endif

#ifndef UNITTESTS
    void unrelated();
#endif
};
"""

# The configuration excerpts. A backslash at the end of a line here only wraps a long
# firmware line; one the firmware has is written `\\`.

# Configuration_MK4.h. It stands in for the iX's, the XL's and the dev kit's, whose E
# steps lines are the same.
CONFIGURATION_NEXTRUDER_H = """\
// This defines the number of extruders
// :[1, 2, 3, 4, 5, 6]
#if HAS_MMU2()
#define EXTRUDERS 6 // 5 + NoTool
#else
#define EXTRUDERS 1
#endif

/**
 * Default Axis Steps Per Unit (steps/mm)
 * Override with M92
 *                                      X, Y, Z, E0 [, E1[, E2[, E3[, E4[, E5]]]]]
 */
#define DEFAULT_AXIS_STEPS_PER_UNIT_X 100
#define DEFAULT_AXIS_STEPS_PER_UNIT_Y 100
#define DEFAULT_AXIS_STEPS_PER_UNIT_Z 400
// Steps/mm at which the extruder feeds exactly the commanded length. The default is deliberately
// lower ("normalised flow", formerly M221 S95 in the slicer start G-code); filament usage tracking
// and the Move E menu compensate for it. Re-measure both when the extruder changes.
#define CALIBRATED_AXIS_STEPS_PER_UNIT_E0 400
#define DEFAULT_AXIS_STEPS_PER_UNIT_E0 380
"""

CONFIGURATION_MK3_5_H = """\
// This defines the number of extruders
// :[1, 2, 3, 4, 5, 6]
#include <option/has_mmu2.h>
#if HAS_MMU2()
#define EXTRUDERS 6 // 5 + NoTool
#else
#define EXTRUDERS 1
#endif

// Steps/mm at which the extruder feeds exactly the commanded length. The default is deliberately
// lower ("normalised flow", formerly M221 S95 in the slicer start G-code); filament usage tracking
// and the Move E menu compensate for it. Re-measure both when the extruder changes.
#define CALIBRATED_AXIS_STEPS_PER_UNIT_E0 280
#define DEFAULT_AXIS_STEPS_PER_UNIT_E0 266
"""

CONFIGURATION_MINI_H = """\
#define CONFIGURATION_H_VERSION 020000
#define USE_PRUSA_EEPROM_AS_SOURCE_OF_DEFAULT_VALUES

#ifdef USE_PRUSA_EEPROM_AS_SOURCE_OF_DEFAULT_VALUES
    #include "config_store/store_c_api.h"
#endif

#define DEFAULT_AXIS_STEPS_PER_UNIT_Z 400
// Steps/mm at which the extruder feeds exactly the commanded length.
// MINI G-codes apply the "normalised flow" themselves (M221 S95), so the default is not reduced.
#define CALIBRATED_AXIS_STEPS_PER_UNIT_E0 325
#define DEFAULT_AXIS_STEPS_PER_UNIT_E0 325 //E0 280 295
"""

# Configuration_COREONE.h. The Core One L's reads the same.
CONFIGURATION_CORE_ONE_H = """\
#if HAS_MMU2()
#define EXTRUDERS 6 // 5 + NoTool
#elif HAS_INDX()
#define EXTRUDERS 9 // 8 + NoTool
#else
#define EXTRUDERS 1
#endif

// Steps/mm at which the extruder feeds exactly the commanded length. The default is deliberately
// lower ("normalised flow", formerly M221 S95 in the slicer start G-code); filament usage tracking
// and the Move E menu compensate for it. Re-measure both when the extruder changes.
#if HAS_INDX()
#define CALIBRATED_AXIS_STEPS_PER_UNIT_E0 576
#define DEFAULT_AXIS_STEPS_PER_UNIT_E0 550
// Extruder service moves feed exactly their commanded length
static constexpr float EXTRUDER_SERVICE_MOVE_E_FACTOR = \
static_cast<float>(CALIBRATED_AXIS_STEPS_PER_UNIT_E0) / DEFAULT_AXIS_STEPS_PER_UNIT_E0;
#else
#define CALIBRATED_AXIS_STEPS_PER_UNIT_E0 400
#define DEFAULT_AXIS_STEPS_PER_UNIT_E0 380
#endif
"""

# Configuration_MK4_adv.h. It stands in for the XL's and the dev kit's, whose pause
# lengths are the same; they lack the MMU2 ramming macro.
ADVANCED_NEXTRUDER_H = """\
#if EXTRUDERS > 1
    // Z raise distance for tool-change, as needed for some extruders
    #define TOOLCHANGE_ZRAISE 2 // (mm)
#endif

/**
 * Advanced Pause
 * Experimental feature for filament change support and for parking the nozzle when paused.
 * Adds the GCode M600 for initiating filament change.
 *
 * Requires an LCD display.
 */
#if HAS_PAUSE()
    #define FILAMENT_MMU2_RAMMING_SEQUENCE \\
        { \\
            { 20, 1500 / 60.F}, \\
            { -50, 2700 / 60.F}, \\
            { -5, 50 / 60.F}, \\
            { -50, 1500 / 60.F}, \\
        }

    #define PARK_PAUSE_PRIME_LENGTH 4 // (mm) Length of a priming "poop".

    #define STANDARD_RETRACT_LENGTH 8 // (mm) Initial retract.
        // This retract is done immediately, before parking the nozzle.

    #define FILAMENT_CHANGE_UNLOAD_ACCEL 1100 \
// (mm/s^2) Lower acceleration may allow a faster feedrate.
    #define FILAMENT_CHANGE_UNLOAD_LENGTH 105 // (mm) The length of filament for a complete unload.
        //   For Bowden, the full length of the tube and nozzle.
        //   For direct drive, the full length of the nozzle.
        //   Set to 0 for manual unloading.

    #define FILAMENT_CHANGE_SLOW_LOAD_LENGTH 30 \
// (mm) Slow length, to allow time to insert material. //60
        // 0 to disable start loading and skip to fast load only

    #define FILAMENT_CHANGE_FAST_LOAD_ACCEL 100 \
// (mm/s^2) Lower acceleration may allow a faster feedrate.  //200
    #define FILAMENT_CHANGE_FAST_LOAD_LENGTH 50 \
// (mm) Load length of filament, from extruder gear to nozzle. //75
        //   For Bowden, the full length of the tube and nozzle.
        //   For direct drive, the full length of the nozzle.

    #define ADVANCED_PAUSE_PURGE_LENGTH 27 // (mm) Length to extrude after loading. //50
#endif

// @section tmc

/**
 * TMC26X Stepper Driver options
 *
 * The TMC26XStepper library is required for this stepper driver.
 * https://github.com/trinamic/TMC26XStepper
 */
#if HAS_DRIVER(TMC26X)

    #if AXIS_DRIVER_TYPE_X(TMC26X)
        #define X_MAX_CURRENT 1000 // (mA)
    #endif
#endif
"""

ADVANCED_MK3_5_H = """\
#if HAS_PAUSE()
    #define FILAMENT_MMU2_RAMMING_SEQUENCE \\
        { \\
            { 20, 1500 / 60.F}, \\
            { -50, 2700 / 60.F}, \\
            { -5, 50 / 60.F}, \\
            { -50, 1500 / 60.F}, \\
        }

    #define PARK_PAUSE_PRIME_LENGTH 4 // (mm) Length of a priming "poop".

    #define STANDARD_RETRACT_LENGTH 8 // (mm) Initial retract.
        // This retract is done immediately, before parking the nozzle.
    #define FILAMENT_CHANGE_UNLOAD_ACCEL 1100 \
// (mm/s^2) Lower acceleration may allow a faster feedrate.
    #define FILAMENT_CHANGE_UNLOAD_LENGTH 105 // (mm) The length of filament for a complete unload.

    #define FILAMENT_CHANGE_SLOW_LOAD_LENGTH 30 \
// (mm) Slow length, to allow time to insert material. //60
    #define FILAMENT_CHANGE_FAST_LOAD_LENGTH 50 \
// (mm) Load length of filament, from extruder gear to nozzle. //75

    #define ADVANCED_PAUSE_PURGE_LENGTH 21 // (mm) Length to extrude after loading. //50
#endif
"""

ADVANCED_MINI_H = """\
#if HAS_PAUSE()
    #define PARK_PAUSE_PRIME_LENGTH 4 // (mm) Length of a priming "poop".

    #define STANDARD_RETRACT_LENGTH 8 // (mm) Initial retract.
// This retract is done immediately, before parking the nozzle.

    #define FILAMENT_CHANGE_UNLOAD_ACCEL 1250 \
// (mm/s^2) Lower acceleration may allow a faster feedrate.
    #define FILAMENT_CHANGE_UNLOAD_LENGTH 400 // (mm) The length of filament for a complete unload.
//   For Bowden, the full length of the tube and nozzle.
//   For direct drive, the full length of the nozzle.
//   Set to 0 for manual unloading.

    #define FILAMENT_CHANGE_SLOW_LOAD_LENGTH 40 \
// (mm) Slow length, to allow time to insert material.
// 0 to disable start loading and skip to fast load only

    #define FILAMENT_CHANGE_FAST_LOAD_ACCEL 625 \
// (mm/s^2) Lower acceleration may allow a faster feedrate.
    #define FILAMENT_CHANGE_FAST_LOAD_LENGTH 320 \
// (mm) Load length of filament, from extruder gear to nozzle.
//   For Bowden, the full length of the tube and nozzle.
//   For direct drive, the full length of the nozzle.

    #define ADVANCED_PAUSE_PURGE_LENGTH 50 // (mm) Length to extrude after loading.
#endif

// @section tmc

/**
 * TMC26X Stepper Driver options
 */
#if HAS_DRIVER(TMC26X)

    #if AXIS_DRIVER_TYPE_X(TMC26X)
        #define X_MAX_CURRENT 1000 // (mA)
    #endif
#endif
"""

ADVANCED_IX_H = """\
#if HAS_PAUSE()
    #define PARK_PAUSE_PRIME_LENGTH 4 // (mm) Length of a priming "poop".

    /**
     * (mm) Initial retract.
     * This retract is done immediately, before parking the nozzle.
     */
    #define STANDARD_RETRACT_LENGTH 8
    #define FILAMENT_CHANGE_UNLOAD_ACCEL 25 \
// (mm/s^2) Lower acceleration may allow a faster feedrate.
    /**
     * (mm) The length of filament for a complete unload.
     * For Bowden, the full length of the tube and nozzle.
     * For direct drive, the full length of the nozzle.
     * Set to 0 for manual unloading.
     */
    #define FILAMENT_CHANGE_UNLOAD_LENGTH 110
    /**
     * (mm) Slow length, to allow time to insert material.
     * 0 to disable start loading and skip to fast load only
     */
    #define FILAMENT_CHANGE_SLOW_LOAD_LENGTH 40
    #define FILAMENT_CHANGE_FAST_LOAD_ACCEL 25 \
// (mm/s^2) Lower acceleration may allow a faster feedrate.
    /**
     * (mm) Load length of filament, from extruder gear to nozzle.
     * For Bowden, the full length of the tube and nozzle.
     * For direct drive, the full length of the nozzle.
     */
    #define FILAMENT_CHANGE_FAST_LOAD_LENGTH 45

    #define ADVANCED_PAUSE_PURGE_LENGTH 40 // (mm) Length to extrude after loading.
#endif
"""

# Configuration_COREONE_adv.h. The Core One L's reads the same.
ADVANCED_CORE_ONE_H = """\
#include <option/has_nozzle_cleaner.h>
#if HAS_NOZZLE_CLEANER()
    #define AVOID_NOZZLE_CLEANER_Y_FIRST true
#endif

/**
 * Universal tool change settings.
 * Applies to all types of extruders except where explicitly noted.
 */
#if EXTRUDERS > 1
    // Z raise distance for tool-change, as needed for some extruders
    #define TOOLCHANGE_ZRAISE 2 // (mm)
#endif

#if HAS_PAUSE()
    #define FILAMENT_MMU2_RAMMING_SEQUENCE \\
        { \\
            { 20, 1500 / 60.F}, \\
            { -50, 2700 / 60.F}, \\
            { -5, 50 / 60.F}, \\
            { -50, 1500 / 60.F}, \\
        }

    #define PARK_PAUSE_PRIME_LENGTH 4 // (mm) Length of a priming "poop".

    /**
     * (mm) Initial retract.
     * This retract is done immediately, before parking the nozzle.
     */
    #define STANDARD_RETRACT_LENGTH 8
#if HAS_INDX()
    #define FILAMENT_CHANGE_UNLOAD_LENGTH 55 \
// (mm) Total lenght for filament unload (For Bowden: full length of the tube + nozzle).
    #define FILAMENT_CHANGE_SLOW_LOAD_LENGTH 15 \
// (mm) length of slow inser, set to 0 to skip slow load.
    #define FILAMENT_CHANGE_FAST_LOAD_LENGTH 25 \
// (mm) from extruder gear to nozzle (For Bowden: full length of the tube + nozzle).
#else
    #define FILAMENT_CHANGE_UNLOAD_LENGTH 105 \
// (mm) Total lenght for filament unload (For Bowden: full length of the tube + nozzle).
    #define FILAMENT_CHANGE_SLOW_LOAD_LENGTH 40 \
// (mm) length of slow inser, set to 0 to skip slow load.
    #define FILAMENT_CHANGE_FAST_LOAD_LENGTH 20 \
// (mm) from extruder gear to nozzle (For Bowden: full length of the tube + nozzle).
#endif

    #define FILAMENT_CHANGE_UNLOAD_ACCEL 25 \
// (mm/s^2) Lower acceleration may allow a faster feedrate.
    #define FILAMENT_CHANGE_FAST_LOAD_ACCEL 25 \
// (mm/s^2) Lower acceleration may allow a faster feedrate.

    #define ADVANCED_PAUSE_PURGE_LENGTH 40 // (mm) Length to extrude after loading.
#endif
"""

RAMMING_NEXTRUDER_CPP = """\
#include "standard_ramming_sequence.hpp"

#include <bsod/bsod.h>

#include <option/has_auto_retract.h>
#if HAS_AUTO_RETRACT()
    #include <feature/auto_retract/auto_retract.hpp>
#endif

using namespace buddy;

const RammingSequence &buddy::standard_ramming_sequence(\
StandardRammingSequence seq, VirtualToolIndex virtual_tool) {
    // TODO different sequence for HF nozzle
    [[maybe_unused]] const bool is_high_flow_nozzle = \
config_store().get_nozzle_is_high_flow(virtual_tool.to_physical());

    switch (seq) {

#if HAS_AUTO_RETRACT()
    case StandardRammingSequence::auto_retract: {
        static constexpr RammingSequenceArray seq({
            { 8, 995 },
            { -43, 6000 },
            { -8, 3000 },
            { -4, 1800 },
            { 20, 600 },
            { -20, 470 },
            { 55, 1740 },
            { -55, 6000 },
            { 20, 340 },
            { -20, 210 },
        });
        static_assert(seq.retracted_distance() >= AutoRetract::full_retract_distance);
        return seq;
    }
#endif

    case StandardRammingSequence::runout: {
        // Shorter - we might have last strand of filament left and standard ramming sequence \
might get it out of the gears.
        // Also, we don't need a nice tip for re-insertion. The remaining filament is very \
short and will be thrown out.
        static constexpr RammingSequenceArray seq({
            { 7, 1500 },
            { -50, 2700 },
            { -5, 50 },
            { -50, 1500 },
        });
        return seq;
    }

    case StandardRammingSequence::unload: {
        // Same as auto-retract sequence, just the last push is longer so that the filament \
gets out of gears
        static constexpr RammingSequenceArray seq({
            { 8, 995 },
            { -43, 6000 },
            { -8, 3000 },
            { -4, 1800 },
            { 20, 600 },
            { -20, 470 },
            { 55, 1740 },
            { -55, 6000 },
            { 20, 340 },
            { -20, 210 },
            { -50, 2000 },
        });
        return seq;
    }
    }

    bsod_unreachable();
}
"""

RAMMING_INDX_CPP = """\
#include "standard_ramming_sequence.hpp"

#include <bsod/bsod.h>
#include <config_store/store_instance.hpp>

#include <option/has_auto_retract.h>
#if HAS_AUTO_RETRACT()
    #include <feature/auto_retract/auto_retract.hpp>
#endif

using namespace buddy;

const RammingSequence &buddy::standard_ramming_sequence(\
StandardRammingSequence seq, VirtualToolIndex virtual_tool) {
    const bool is_flexible = \
FilamentType::for_tool_heuristic(virtual_tool).parameters().is_flexible;

    switch (seq) {

#if HAS_AUTO_RETRACT()
    case StandardRammingSequence::auto_retract: {
        // auto_retract is never called for flexible filaments
        // (filtered in auto_retract.cpp), so no differentiation needed here
        static constexpr RammingSequenceArray seq({
            { 8, 480 },
            { -8, 4800 },
            { 1, 1440 },
            { -1, 1440 },
        });
        static_assert(seq.retracted_distance() >= AutoRetract::full_retract_distance);
        return seq;
    }
#endif

    case StandardRammingSequence::runout: {
        if (is_flexible) {
            static constexpr RammingSequenceArray seq({
                { -50, 600 },
            });
            return seq;
        }
        static constexpr RammingSequenceArray seq({
            { 8, 1500 }, // first ramming
            { 8, 1200 },
            { -20, 2100 },
            { -3, 60 }, // cooling moves
            { 3, 120 },
            { 19, 1800 }, // second ramming
            { 2, 600 },
            { -20, 2100 },
            { -3, 120 }, // cooling moves
            { 3, 120 },
            { -6, 120 }, // tip cooling before retraction
            { -8, 360 },
        });
        return seq;
    }

    case StandardRammingSequence::unload: {
        if (is_flexible) {
            static constexpr RammingSequenceArray seq({
                { -50, 600 },
            });
            return seq;
        }
        static constexpr RammingSequenceArray seq({
            { 8, 1500 }, // first ramming
            { 8, 1200 },
            { -20, 2100 },
            { -3, 60 }, // cooling moves
            { 3, 120 },
            { 19, 1800 }, // second ramming
            { 2, 600 },
            { -20, 2100 },
            { -3, 120 }, // cooling moves
            { 3, 120 },
            { -6, 120 }, // tip cooling before retraction
            { -8, 360 },
        });
        return seq;
    }
    }

    bsod_unreachable();
}
"""

RAMMING_MINI_CPP = """\
#include "standard_ramming_sequence.hpp"

#include <bsod/bsod.h>

using namespace buddy;

const RammingSequence &buddy::standard_ramming_sequence(\
StandardRammingSequence seq, [[maybe_unused]] VirtualToolIndex virtual_tool) {
    switch (seq) {

    case StandardRammingSequence::runout:
    case StandardRammingSequence::unload: {
        static constexpr RammingSequenceArray seq({
            { 20, 1500 },
            { -50, 2700 },
            { -5, 50 },
            { -50, 1500 },
        });
        return seq;
    }
    }

    bsod_unreachable();
}
"""

RAMMING_MK3_5_CPP = """\
#include "standard_ramming_sequence.hpp"

#include <bsod/bsod.h>

using namespace buddy;

const RammingSequence &buddy::standard_ramming_sequence(\
StandardRammingSequence seq, [[maybe_unused]] VirtualToolIndex virtual_tool) {
    switch (seq) {

    case StandardRammingSequence::runout: {
        static constexpr buddy::RammingSequenceArray seq({
            { 7, 1500 },
            { -50, 2700 },
            { -5, 50 },
            { -50, 1500 },
        });
        return seq;
    }

    case StandardRammingSequence::unload: {
        static constexpr RammingSequenceArray seq({
            { 20, 1500 },
            { -50, 2700 },
            { -5, 50 },
            { -50, 1500 },
        });
        return seq;
    }
    }

    bsod_unreachable();
}
"""

# The entries are re-indented and power_panic_purge is cut short; `\\n` is the firmware's
# `\n`.
NOZZLE_CLEANER_CPP = """\
#include "nozzle_cleaner.hpp"
#include <option/has_indx.h>

#if HAS_INDX()
// The config folds the park point into self-contained constants (SanityCheck.h expands it \
in every TU);
// keep them tracking the wastebin geometry.
static_assert(X_NOZZLE_PARK_POINT == X_WASTEBIN_POINT);
#endif

namespace nozzle_cleaner {

constexpr ConstexprString directory { "nozzle_cleaner" };

static constexpr EnumArray<Sequence, GCodeFile, static_cast<int>(Sequence::_cnt)> sequences {
#if HAS_INDX()
        { Sequence::quick_clean, {
            .filename = "quick_clean",
            .directory = directory,
            .default_gcode = "G1 F21000\\n"
                             "G750 Y98.5 X0\\n"
                             "G750 Y118.5 X-1.0\\n"
                             "G750 Y98.5 X-2.0",
        } },
        { Sequence::purge_clean, {
            .filename = "purge_clean",
            .directory = directory,
            .default_gcode = "G750 Y86 F21000 A\\n" // Eject poop and move back to purge position
                             "G750 Y93 F21000 A\\n"
                             "G750 Y83 F21000 A\\n"
                             "G750 Y93 F21000 A\\n"
                             "G750 Y76 F21000 A\\n"
                             "G750 Y93 F21000 A\\n"
                             "G750 Y87 F21000\\n"
                             "M906 P1\\n" // Increase E current for purge
                             "G750 E25 F4 L\\n" \
// L: G750 adjusts this E feedrate for the loaded filament
                             "M400\\n" // planner.synchronize()
                             "M1705 N\\n" // Autoretract sequence
                             "M400\\n"
                             "M906 P0\\n" // Restore E current
                             "G750 Y98.5 F21000\\n"
                             "G750 Y91.5 F21000",
        } },
        // Like purge_clean, but without the retract at the end.
        { Sequence::power_panic_purge, {
            .filename = "power_panic_purge",
            .directory = directory,
            .default_gcode = "M906 P1\\n" // Increase E current for purge
                             "G750 E25 F4 L\\n" \
// L: G750 adjusts this E feedrate for the loaded filament
                             "M400\\n" // planner.synchronize()
                             "M906 P0\\n" // Restore E current
                             "G750 X0 Y96.5 F18000",
        } },
#else
        { Sequence::purge_clean, {
            .filename = "purge_clean", .directory = directory,
            .default_gcode = "M106 S200\\n" // fan on
                             "G4 S4\\n" // Wait for 4 seconds
                             "G750 X267.4 Y284.75 F50 A\\n"
                             "G750 X253.4 Y284.75 F50 A\\n"
                             "G750 X267.4 Y284.75 F50 A\\n"
                             "G750 X253.4 Y284.75 F50 A\\n"
                             "G750 X253.4 Y305.0 F50\\n"
                             "M106 S0\\n", // fan off
        } },
#endif
};

} // namespace nozzle_cleaner
"""

# Cut down to the namespace with the two constants, all of `resume_loop`, and the cut's
# retract in a stub of `panic_loop`.
POWER_PANIC_CPP = """\
#include "power_panic.hpp"

namespace {

constexpr uint16_t chamber_temp_off = 0xffff;

constexpr bool power_panic_retracts =
    // Dwarf gets turned off during the shutdown loop, so no retraction can happen
    !HAS_DWARF();

constexpr bool power_panic_deretracts = power_panic_retracts
    // Skipped where G12 S21 already pressurizes the nozzle (see WaitForHeaters).
    && !(HAS_NOZZLE_CLEANER() && (PRINTER_IS_PRUSA_COREONE() || PRINTER_IS_PRUSA_COREONEL()))
    && true;


namespace power_panic {

void resume_loop() {
    switch (resume_state) {
    case ResumeState::Setup:
        // Set bed temperature to prevent bed from cooling down
        thermalManager.setTargetBed(state_buf.planner.target_bed);
        break;

    case ResumeState::Resume: {
        // setup the paused state
        // This applies for PowerPanic from paused AND from printing too
        // because printing after power up starts from pause
        marlin_server::resume_state_t resume;
        resume.pos = state_buf.crash.crash_native_position;
        resume.fan_speed = state_buf.planner.fan_speed;
        resume.print_speed = state_buf.planner.print_speed;
        resume.nozzle_temp = state_buf.planner.target_nozzle;
#if HAS_MOTOR_CURRENT_PROFILES()
        buddy::set_active_motor_current_profile(static_cast<buddy::StandardMotorCurrentProfile>(state_buf.planner.current_profile));
#endif
#if HAS_INDX()
        resume.active_tool = state_buf.planner.active_tool;
        if (state_buf.toolchanger.phase != PrusaToolChanger::ToolchangePhase::none) {
            // During a toolchange planner.active_tool is pre-toolchange; use the target tool.
            resume.active_tool = state_buf.toolchanger.tool_nr;
        }
#endif
        marlin_server::set_resume_data(&resume);

        // Set sdpos
        //  in case powerpanic happens before sdpos propagates from resume data to media where crash_s would get it
        crash_s.sdpos = state_buf.crash.sdpos;

        // set bed temperatures
        thermalManager.setTargetBed(state_buf.planner.target_bed);
#if ENABLED(PREVENT_COLD_EXTRUSION)
        thermalManager.extrude_min_temp = state_buf.planner.extrude_min_temp;
        thermalManager.allow_cold_extrude = state_buf.planner.allow_cold_extrude;
#endif

        gcode.compatibility = state_buf.planner.compatibility;

        marlin_debug_flags = state_buf.planner.marlin_debug_flags;

        // planner settings
        planner.apply_settings(state_buf.planner.settings);
        planner.refresh_acceleration_rates();
#if !HAS_CLASSIC_JERK
        planner.junction_deviation_mm = state_buf.planner.junction_deviation_mm;
#endif

#if HAS_INDX()
        // active_extruder is not restored from EEPROM (the cache is invalidated
        // mid-print). Apply the saved pre-toolchange tool so subsequent E moves
        // are valid, including its tool offset. Prefer the one physically held
        // if toolchange is in progress.
        const uint8_t effective_active_tool
            = state_buf.toolchanger.phase == PrusaToolChanger::ToolchangePhase::after_lock
            ? state_buf.toolchanger.tool_nr
            : state_buf.planner.active_tool;
        if (effective_active_tool != PrusaToolChanger::MARLIN_NO_TOOL_PICKED) {
            const PhysicalToolIndex tool = PhysicalToolIndex::from_raw(effective_active_tool);
            prusa_toolchanger.set_active_extruder(tool);
            hotend_currently_applied_offset = hotend_offset[tool];
        } else {
            prusa_toolchanger.set_active_extruder(NoTool {});
            hotend_currently_applied_offset = xyz_pos_t {};
        }
#endif
        // initial planner state (order is relevant!)
        debug_assert(!planner.leveling_active);
        current_position[Z_AXIS] = state_buf.planner.z_position;
        planner.set_position_mm(current_position);
        axes_home_level[Z_AXIS] = state_buf.crash.axes_home_level[Z_AXIS];
        planner.max_printed_z = state_buf.planner.max_printed_z;

        // canceled objects
#if HAS_CANCEL_OBJECT()
        buddy::cancel_object().set_state(state_buf.cancel_object);
#endif
#if HAS_TOOL_MAPPING()
        tool_mapper.deserialize(state_buf.tool_mapping);
#endif
#if HAS_SPOOL_JOIN()
        spool_join.deserialize(state_buf.spool_join);
#endif
#if HAS_CHAMBER_API()
        if (state_buf.chamber_target_temp == chamber_temp_off) {
            buddy::chamber().set_target_temperature(std::nullopt);
        } else {
            buddy::chamber().set_target_temperature(state_buf.chamber_target_temp);
        }
#endif
#if HAS_TEMP_HEATBREAK_CONTROL
        for (auto tool : PhysicalToolIndex::all()) {
            thermalManager.setTargetHeatbreak(state_buf.heatbreak_temperatures[tool], tool);
        }

#endif

#if HAS_TOOLCHANGER() && HAS_TOOL_CRASH_RECOVERY()
        if (prusa_toolchanger.is_pos_in_toolchange_area(state_buf.crash.crash_machine_position.xy())) {
            prusa_toolchanger.set_return_data({
                PhysicalToolIndex::from_raw_notool(state_buf.toolchanger.tool_nr),
                state_buf.toolchanger.return_type,
                state_buf.toolchanger.return_pos,
            });
            resume_state = ResumeState::Finish; // Do not reheat, do not unpark
            break; // Skip lift and rehome
            // Will continue with toolcrash recovery
        }
#endif
#if HAS_INDX()
        if (state_buf.toolchanger.phase != PrusaToolChanger::ToolchangePhase::none) {
            // Skip XY re-home. Already included in the toolchange recovery.
            resume_state = ResumeState::Finish;
            break;
        }
#endif

        if (state_buf.crash.recover_flags & Crash_s::RECOVER_AXIS_STATE) {
            // lift and rehome
            if (state_buf.crash.axes_home_level.is_homed(X_AXIS, AxisHomeLevel::imprecise) || state_buf.crash.axes_home_level.is_homed(Y_AXIS, AxisHomeLevel::imprecise)) {
                float z_dist = current_position[Z_AXIS] - state_buf.crash.crash_native_position[Z_AXIS];
                float z_lift = z_dist < Z_HOMING_HEIGHT ? Z_HOMING_HEIGHT - z_dist : 0;
                char cmd_buf[24];
                snprintf(cmd_buf, sizeof(cmd_buf), "G28 X Y D R%f", (double)z_lift);
                marlin_server::enqueue_gcode(cmd_buf);
            }
        }

        if (state_buf.planner.was_paused) {
            resume_state = ResumeState::ParkForPause;
        } else {
            for (auto tool : PhysicalToolIndex::all()) {
                thermalManager.setTargetHotend(state_buf.planner.target_nozzle[tool], tool);
            }
            // setTargetBed is already called higher up in this function

            resume_state = ResumeState::WaitForHeaters;
        }
        break;
    }

    case ResumeState::WaitForHeaters: {
        buddy::safety_timer().reset_restore_nonblocking();

        if (!Temperature::are_all_temperatures_reached(Temperature::RequireCooling::current_tool_only)) {
            break;
        }

#if HAS_NOZZLE_CLEANER()
        if (PhysicalToolIndex::currently_selected_opt().has_value()) {
            // Keep the printer list below in sync with the unretract guard in
            // ResumeState::Unpark — G12 S21 pressurizes the nozzle on its own.
    #if PRINTER_IS_PRUSA_COREONE() || PRINTER_IS_PRUSA_COREONEL()
            static_assert(!power_panic_deretracts);
            marlin_server::enqueue_gcode("G12 S90"); // enter cleaner
            marlin_server::enqueue_gcode("G12 S21"); // purge (no retract) and brush wipe
            marlin_server::enqueue_gcode("G12 S91"); // exit cleaner
    #else
            marlin_server::enqueue_gcode("G12"); // clean nozzle on the brush
    #endif
        }
#endif
        resume_state = ResumeState::Unpark;
        break;
    }

    case ResumeState::Unpark:
        if (marlin_server::is_processing()) {
            break;
        }

        // forget the XYZ resume position if requested
        if (!(state_buf.crash.recover_flags & Crash_s::RECOVER_XY_POSITION)) {
            LOOP_XY(i) {
                state_buf.crash.crash_native_position[i] = current_position[i];
            }
        }
        if (!(state_buf.crash.recover_flags & Crash_s::RECOVER_Z_POSITION)) {
            state_buf.crash.crash_native_position[Z_AXIS] = current_position[Z_AXIS];
        }

        // unpark only if the position was known
        if (state_buf.crash.axes_home_level.is_homed({ X_AXIS, Y_AXIS }, AxisHomeLevel::imprecise)) {
            plan_park_move_to_xyz(state_buf.crash.crash_native_position.xyz(), NOZZLE_PARK_XY_FEEDRATE, NOZZLE_PARK_Z_FEEDRATE, Segmented::yes);
        }

        // Unretract paired with the retract in PPState::Prepared.
        if (power_panic_deretracts && PhysicalToolIndex::currently_selected_opt().has_value()) {
            mapi::extruder_move(STANDARD_RETRACT_LENGTH, buddy::standard_feedrates::current_extruder(buddy::standard_feedrates::Extruder::deretract));
        }

        resume_state = ResumeState::Finish;
        break;

    case ResumeState::ParkForPause:
        if (marlin_server::is_processing()) {
            break;
        }

        // return to the parking position
        plan_park_move_to_xyz(state_buf.crash.start_current_position.xyz(),
            NOZZLE_PARK_XY_FEEDRATE, NOZZLE_PARK_Z_FEEDRATE, Segmented::yes);
        resume_state = ResumeState::Finish;
        break;

    case ResumeState::Finish:
        if (marlin_server::is_processing()) {
            break;
        }

        // original planner state
        planner.flow_percentage = state_buf.planner.flow_percentage;
        gcode.axis_relative = state_buf.planner.axis_relative;

        // IS/PA
        LOOP_XYZ(i) {
            if (state_buf.planner.axis_config[i].frequency == 0.f) {
                input_shaper::set_axis_config((AxisEnum)i, std::nullopt);
            } else {
                input_shaper::set_axis_config((AxisEnum)i, state_buf.planner.axis_config[i]);
            }
        }

        if (state_buf.planner.original_y.frequency == 0.f) {
            input_shaper::set_config_for_m74(Y_AXIS, std::nullopt);
        } else {
            input_shaper::set_config_for_m74(Y_AXIS, state_buf.planner.original_y);
        }

        if (state_buf.planner.axis_y_weight_adjust.frequency_delta != 0.f) {
            input_shaper::set_axis_y_weight_adjust(std::nullopt);
        } else {
            input_shaper::set_axis_y_weight_adjust(state_buf.planner.axis_y_weight_adjust);
        }

        pressure_advance::set_axis_e_config(state_buf.planner.axis_e_config);

        // restore crash state
        {
            const auto &d = state_buf.crash;

            crash_s.start_current_position = d.start_current_position;
            crash_s.crash_native_position = d.crash_native_position;
            crash_s.crash_machine_position = d.crash_machine_position;
            crash_s.segments_finished = d.segments_finished;
            crash_s.leveling_active = d.leveling_active;
            crash_s.recover_flags = d.recover_flags;
            crash_s.fr_mm_s = d.fr_mm_s;
            crash_s.counters.restore_data(d.counters);
        }

        atomic_finish();

        // Re-prepare for the next power panic now that power is stable, to
        // avoid expensive erase.
        //
        // For the non-resumed print, it is done in MBL/G29.
        prepare();

        log_info(PowerPanic, "resuming complete");

        resume_state = ResumeState::Error;
        break;

    case ResumeState::Error:
        // fail if marlin_server::powerpanic_finish_xxx didn't reset the server loop state
        bsod("resume loop not reset");
    }
}

void panic_loop() {
    switch (power_panic_state) {
    case PPState::Triggered:
        if (power_panic_retracts && PhysicalToolIndex::currently_selected_opt().has_value() && !runtime_state.nested_fault && !state_buf.planner.was_paused && !state_buf.planner.was_crashed) {
            // retract if we were printing
            mapi::extruder_move(-STANDARD_RETRACT_LENGTH, buddy::standard_feedrates::current_extruder(buddy::standard_feedrates::Extruder::retract));
            planner.start_moving();
        }
        break;
    }
}

} // namespace power_panic
"""  # noqa: E501 (verbatim firmware lines)

CONFIGURATION_H = """\
//! @file
#pragma once

#include "config.h"

#if PRINTER_IS_PRUSA_MINI()
    #include "Configuration_MINI.h"
#elif PRINTER_IS_PRUSA_XL_DEV_KIT()
    #include "Configuration_XL_DEV_KIT.h"
#elif PRINTER_IS_PRUSA_XL() && BOARD_IS_XLBUDDY()
    #include "Configuration_XL.h"
#elif PRINTER_IS_PRUSA_XL() && BOARD_IS_DWARF()
    #include "Configuration_XL_Dwarf.h"
#elif PRINTER_IS_PRUSA_MK4()
    #include "Configuration_MK4.h"
#elif PRINTER_IS_PRUSA_MK3_5()
    #include "Configuration_MK3.5.h"
#elif PRINTER_IS_PRUSA_iX()
    #include "Configuration_iX.h"
#elif PRINTER_IS_PRUSA_COREONE()
    #include "Configuration_COREONE.h"
#elif PRINTER_IS_PRUSA_COREONEL()
    #include "Configuration_COREONEL.h"
#else
    #error "Unknown PRINTER_TYPE!"
#endif
"""

CONFIGURATION_ADV_H = """\
//! @file
#pragma once

#include <device/board.h>
#include "config.h"

#if PRINTER_IS_PRUSA_MINI()
    #include "Configuration_MINI_adv.h"
#elif PRINTER_IS_PRUSA_XL_DEV_KIT()
    #include "Configuration_XL_DEV_KIT_adv.h"
#elif PRINTER_IS_PRUSA_XL() && BOARD_IS_XLBUDDY()
    #include "Configuration_XL_adv.h"
#elif PRINTER_IS_PRUSA_XL() && BOARD_IS_DWARF()
    #include "Configuration_XL_Dwarf_adv.h"
#elif PRINTER_IS_PRUSA_MK4()
    #include "Configuration_MK4_adv.h"
#elif PRINTER_IS_PRUSA_MK3_5()
    #include "Configuration_MK3.5_adv.h"
#elif PRINTER_IS_PRUSA_iX()
    #include "Configuration_iX_adv.h"
#elif PRINTER_IS_PRUSA_COREONE()
    #include "Configuration_COREONE_adv.h"
#elif PRINTER_IS_PRUSA_COREONEL()
    #include "Configuration_COREONEL_adv.h"
#else
    #error "Unknown PRINTER_TYPE!"
#endif
"""

RAMMING_CMAKE = """\
target_sources(firmware PRIVATE ramming_sequence.cpp)

if(HAS_NEXTRUDER)
  target_sources(firmware PRIVATE standard_ramming_sequence_nextruder.cpp)
elseif(HAS_INDX)
  target_sources(firmware PRIVATE standard_ramming_sequence_indx.cpp)
elseif(PRINTER STREQUAL "MINI")
  target_sources(firmware PRIVATE standard_ramming_sequence_mini.cpp)
elseif(PRINTER STREQUAL "MK3.5")
  target_sources(firmware PRIVATE standard_ramming_sequence_mk3_5.cpp)
else()
  message(FATAL_ERROR "Missing ramming sequence setup for \\"${PRINTER}\\".")
endif()
"""

AUTO_RETRACT_HPP = """\
/// @file
#pragma once

#include "tool_index.hpp"
#include <optional>
#include <bitset>
#include <inplace_function.hpp>
#include <option/has_indx.h>
#include <option/has_wastebin.h>
#include <option/has_nextruder.h>

namespace buddy {

namespace auto_retract_detail {
    using ProgressCallback = stdext::inplace_function<void(float progress_0_100)>;

    struct RetractFromNozzleParams {
        /// Callback for reporting progress of the retraction, called with values from 0 to \
100. Optional.
        ProgressCallback progress_callback = nullptr;
#if HAS_WASTEBIN()
        /// Whether to park over the wastebin for the ramming sequence
        bool park_over_wastebin = true;
#endif
    };
} // namespace auto_retract_detail

/// Class for managing automatic retraction after print or load, so that the printer keeps \
the nozzle empty for MBL and non-printing to prevent oozing.
/// Only to be managed from the marlin thread
class AutoRetract {
    friend AutoRetract &auto_retract();

public:
    using ToolVariant = std::variant<PhysicalToolIndex, NoTool>;

#if HAS_NEXTRUDER()
    /// Minimum retract distance for the filament to be considered auto-retracted. \
Auto-retracted filaments can be unloaded without heating.
    static constexpr float full_retract_distance = 20.f; // mm
    static constexpr bool supports_cold_unload = true;

#elif HAS_INDX_HEAD()
    /// Retraction distance for a standard auto retract sequence
    static constexpr float full_retract_distance = 8.f; // mm

    /// In parked tools, the only thing holding the filament is the nozzle
    /// Thus we cannot auto-retract to the level that would allow cold unload
    static constexpr bool supports_cold_unload = false;
#else
    #error
#endif

    /// @returns whether the specified \\param tool is retracted (some amount > 0.0f) and is \
a known value -> will deretract on positive Z move
    bool will_deretract(ToolVariant tool = PhysicalToolIndex::currently_selected()) const;

    /// @returns true if the filament retracted from the \\param tool's nozzle for at least \
full_retract_distance
    bool is_fully_retracted(ToolVariant tool = PhysicalToolIndex::currently_selected()) const;

    /// @returns true if the filament is retracted enough for a cold unload (unloading \
without heating up the nozzle)
    bool can_cold_unload(PhysicalToolIndex physical_tool) const;
};

} // namespace buddy
"""

# Three pieces of the file, the last cut out of the middle of a switch.
PAUSE_CPP = """\
void Pause::unload_wait_temp_process([[maybe_unused]] Response response) {
    if (!ensureSafeTemperatureNotifyProgress()) {
        return;
    }

#if HAS_INDX()
    set(LoadState::unload_purge);
#else
    set(LoadState::ram_sequence);
#endif
}

#if HAS_INDX()
void Pause::unload_purge_process([[maybe_unused]] Response response) {
    setPhase(PhasesLoadUnload::Purging_unstoppable);

    static constexpr float unload_purge_length = 2.0f; // mm
    std::ignore = do_e_move_notify_progress_hotextrude(
        unload_purge_length, standard_feedrates::extruder(standard_feedrates::Extruder::\
advanced_pause_purge, FilamentType::for_tool_heuristic(settings.virtual_tool())), \
StopConditions::UserStopped);

    set(LoadState::ram_sequence);
}
#endif

    case LoadType::unload:
    case LoadType::unload_confirm:
#if HAS_SPOOL_JOIN() && HAS_TOOLCHANGER()
    case LoadType::unload_spool_join:
#endif
    case LoadType::filament_stuck: {
        constexpr static ProgressMapperWorkflowArray workflow { \
std::to_array<WorkflowStep>({
            { LoadState::unload_wait_temp, 3 },
#if HAS_INDX()
                { LoadState::unload_purge, 1 },
#endif
                { LoadState::ram_sequence, 2 },
                { LoadState::unload, 1 },
        }) };
        result = &workflow;
        break;
    }

void Pause::mmu_load_start_process([[maybe_unused]] Response response) {
    if (load_type == LoadType::load) {
        if (!MMU2::mmu2.load_filament_to_nozzle(settings.virtual_tool().to_raw())) {
            // TODO tell user that he has already loaded filament if he really wants to continue
            // TODO check fsensor .. how should I behave if filament is not detected ???
            // some error?
            set(LoadState::load_finalize);
            return;
        }
        mark_filament_as_loaded();

        set(LoadState::color_correct_ask);
    } else if (load_type == LoadType::filament_change) {
        setPhase(PhasesLoadUnload::LoadFilamentIntoMMU);
        set(LoadState::mmu_load_ask);
    }

    return;
}

void Pause::mmu_unload_start_process([[maybe_unused]] Response response) {
    if (load_type == LoadType::unload) {
        MMU2::mmu2.unload();
        set(LoadState::_finished);

    } else if (load_type == LoadType::filament_change) {
        const auto virtual_tool = VirtualToolIndex::currently_selected_opt();

        // No filament loaded in MMU, we can't continue, as we don't know what slot to load
        if (!virtual_tool.has_value()) {
            set(LoadState::unload_finish_or_change);
            return;
        }

        settings.virtual_tool_ = *virtual_tool;

        MMU2::mmu2.unload();
        MMU2::mmu2.eject_filament(virtual_tool->to_raw());
        set(LoadState::unload_finish_or_change);
    }

    return;
}

void Pause::mmu_load_process([[maybe_unused]] Response response) {
    MMU2::mmu2.load_filament(settings.virtual_tool().to_raw());
    MMU2::mmu2.load_filament_to_nozzle(settings.virtual_tool().to_raw());

    set(LoadState::color_correct_ask);
}
"""

PRINTER_MODEL_CPP = """\
constexpr PrinterGCodeCompatibilityReport gcode_compatibility_report_constexpr(const PrinterModelInfo &fw_printer, const PrinterModelInfo &gcode_printer) {
    using CompatGroup = PrinterModelCompatibilityGroup;

    PrinterGCodeCompatibilityReport result;

    if (gcode_printer.compatibility_group == fw_printer.compatibility_group) {
        result.is_compatible = true;
        return result;
    }

    // Recursively calls gcode_compatibility_report. is_compatible ends up being true if there is an upgrade path
    const auto upgrade_from = [&](PrinterModel model) {
        result = gcode_compatibility_report_constexpr(PrinterModelInfo::get_constexpr(model), gcode_printer);
    };

    switch (fw_printer.compatibility_group) {

    case CompatGroup::mini:
    case CompatGroup::xl:
    case CompatGroup::ix:
    case CompatGroup::mk3:
    case CompatGroup::coreone_indx: // INDX_TODO: Implement back compatibility
    case CompatGroup::coreonel_indx: // INDX_TODO: Implement gcode compatibility
        // No backwards compatibility
        break;

    case CompatGroup::xls:
        upgrade_from(PrinterModel::xl);
        result.xl_compatibility_mode = true;
        break;

    case CompatGroup::mk3_5:
    case CompatGroup::mk4:
        upgrade_from(PrinterModel::mk3);
        result.mk3_compatibility_mode = true;
        break;

    case CompatGroup::mk4s:
        upgrade_from(PrinterModel::mk4);
        result.mk4_compatibility_mode = true;
        break;

    case CompatGroup::coreone:
        upgrade_from(PrinterModel::mk4s);
        result.chamber_compatibility_mode = true;
        break;
    case CompatGroup::coreonel:
        upgrade_from(PrinterModel::coreone);
        // No special handling needed, we are just physically larger
        break;
    }

    // Clear all compatibility modes if we're not compatible
    if (!result.is_compatible) {
        result = {};
    }

    return result;
}

PrinterGCodeCompatibilityReport PrinterModelInfo::gcode_compatibility_report(const PrinterModelInfo &gcode_target_printer) const {
    return gcode_compatibility_report_constexpr(*this, gcode_target_printer);
}

// If printer HAS_EXTENDED_PRINTER_TYPE (i.e. XL -> XLP) it must have also HAS_GCODE_COMPATIBILITY
// otherwise the gcode sliced for master printer would generate warning message on the extended printer and vice versa.
#if HAS_EXTENDED_PRINTER_TYPE()
static_assert(HAS_GCODE_COMPATIBILITY());
#else
// In case HAS_EXTENDED_PRINTER_TYPE() is not defined, HAS_GCODE_COMPATIBILITY() should coincide with the printer being able to run MK3 gcode
// Only in case of legacy printers, for new printers the HAS_EXTENDED_PRINTER_TYPE should be used, no need to keep MK3 gcode compatibility for new printers.
static_assert(HAS_GCODE_COMPATIBILITY() == gcode_compatibility_report_constexpr(firmware_base_constexpr, PrinterModelInfo::get_constexpr(PrinterModel::mk3)).is_compatible);
#endif
"""  # noqa: E501 (verbatim firmware lines)

GCODE_COMPATIBILITY_CPP = """\
void CompatibilityReport::generate_without_toolmapping(const GCodeInfo &gcode_info) {
    *this = {};

    failed_general_checks |= gcode_info.info().failed_gcode_checks;

    if (!gcode_info.info().sliced_with_input_shaper_ && !PRINTER_IS_PRUSA_iX()) {
        failed_general_checks.set(GeneralCheck::input_shaper);
    }

#if HAS_INDX()
    if (!gcode_info.info().sliced_with_indx_lock_) {
        failed_general_checks.set(GeneralCheck::indx_lock);
    }
#endif

#if HAS_HT_HOTEND()
    // hotend_type is read from EEPROM, so test against the safe default rather than for
    // high_temp: an unknown or future-FW value then warns instead of silently letting an
    // HT-sliced g-code run on a hotend that cannot take its temperatures.
    static_assert(PhysicalToolIndex::count == 1, "HT hotend is a single-tool printer feature");
    if (gcode_info.info().sliced_for_ht_hotend_ && config_store().hotend_type.get(0) != HotendType::high_temp) {
        failed_general_checks.set(GeneralCheck::ht_hotend);
    }
#endif

    if (gcode_info.UsedExtrudersCount() > get_num_of_enabled_tools()) {
        failed_general_checks.set(GeneralCheck::not_enough_tools);
    }

#if HAS_INDX()
    if (config_store().selftest_result_nozzle_cleaner_calibration.get() != TestResult::passed) {
        failed_general_checks.set(GeneralCheck::nozzle_cleaner_not_calibrated);
    }
#endif
}

void CompatibilityReport::generate_full(const ToolMappingArgs &args) {
    generate_without_toolmapping(args.gcode_info);
    generate_toolmapping_only_noclear(args);
}

void CompatibilityReport::generate_toolmapping_only(const ToolMappingArgs &args) {
    *this = {};
    generate_toolmapping_only_noclear(args);
}

void CompatibilityReport::generate_toolmapping_only_noclear([[maybe_unused]] const ToolMappingArgs &args) {
#if HAS_MMU2()
    const bool mmu_enabled = MMU2::mmu2.Enabled();
#else
    const bool mmu_enabled = false;
#endif

    // Do NOT skip disabled GCodeTools - these need to fail on GCodeToolCheck::tool_assigned if they are used
    for (GcodeToolIndex gcode_tool : GcodeToolIndex::all()) {
        auto &gcode_tool_fails = failed_gcode_tool_checks[gcode_tool];

        const auto &extruder_info = args.gcode_info.get_extruder_info(gcode_tool);
        if (!extruder_info.used()) {
            continue;
        }

#if HAS_TOOL_MAPPING()
        const auto base_virtual_tool_opt = gcode_tool.to_virtual(args.tool_mapper);
#else
        const auto base_virtual_tool_opt = gcode_tool.to_virtual();
#endif

        if (!std::holds_alternative<VirtualToolIndex>(base_virtual_tool_opt)) {
            gcode_tool_fails.set(GCodeToolCheck::tool_assigned);
            continue;
        }
        const VirtualToolIndex base_virtual_tool = std::get<VirtualToolIndex>(base_virtual_tool_opt);

#if HAS_MMU2()
        // Make sure that MMU gcode is sliced with the correct nozzle.
        // Slicing with a non-HF nozzle while HF nozzle is installed results in unsufficient purging.
        // Slicing for a HF nozzle without having it leads to extruder skipping.
        // Note: Always checking first bit in the config store, since nozzle_is_high_flow is set per toolhead and MMU always uses first one.
        if (extruder_info.requires_high_flow_nozzle == Tristate::yes
            && !config_store().get_nozzle_is_high_flow(0)
            && !args.gcode_info.is_singletool_gcode()
            && mmu_enabled) {
            failed_virtual_tool_checks[base_virtual_tool].set(VirtualToolCheck::nozzle_not_high_flow);
        }
#endif

#if HAS_ANFC()
        /// Sums remaining filament across the whole spool join chain
        /// Is set to NAN if any of the spooljoined tools does not have an OPT assigned
        float remaining_filament_g = 0;
#endif

        const auto virtual_tool_check = [&](VirtualToolIndex virtual_tool, [[maybe_unused]] std::optional<VirtualToolIndex> previous_virtual_tool) {
            auto &virtual_tool_fails = failed_virtual_tool_checks[virtual_tool];

            const PhysicalToolIndex physical_tool = virtual_tool.to_physical();

            if (!physical_tool.is_enabled()) {
                virtual_tool_fails.set(VirtualToolCheck::correct_tool);
                return;
            }

#if HAS_TOOLCHANGER()
            if (prusa_toolchanger.is_toolchanger_enabled() && !prusa_toolchanger.is_tool_info_valid(physical_tool)) {
                virtual_tool_fails.set(VirtualToolCheck::dock_position_calibrated);
            }
#endif

            const FilamentType loaded_filament_type = config_store().get_filament_type(virtual_tool);
            const FilamentTypeParameters loaded_filament_params = loaded_filament_type.parameters();

            // Check filament type and hotend compatibility
            // Don't report errors if the gcode did not provide the filament type
            if (const auto &fn = extruder_info.filament_name; !fn.empty() && fn != "---") {
                if (fn != loaded_filament_params.name) {
                    virtual_tool_fails.set(VirtualToolCheck::filament_type);
                }

                const auto gcode_filament = FilamentType::from_name(std::string_view(fn.data()));
                if (gcode_filament != FilamentType::none) {
                    const filament_compatibility::CompatibilityReportGenerateArgs args {
                        .filament = gcode_filament.parameters(),
                        .tools = virtual_tool,
                        .assume_filament_already_inserted = true,
                    };
                    filament_check_reports[virtual_tool].generate_noclear(args);
                }
            }

            if (auto dia = extruder_info.nozzle_diameter; dia.has_value() && std::abs(*dia - config_store().get_nozzle_diameter(physical_tool)) > 0.001f) {
                virtual_tool_fails.set(VirtualToolCheck::nozzle_diameter);
            }
            if (extruder_info.requires_hardened_nozzle == Tristate::yes && !config_store().get_nozzle_is_hardened(physical_tool)
                // These errors are +- duplicit, prevent showing the both
                && !filament_check_reports[virtual_tool].failed_tool_checks.test(buddy::filament_compatibility::ToolCheck::abrasive)) {
                virtual_tool_fails.set(VirtualToolCheck::nozzle_hardened);
            }
            if (extruder_info.requires_high_flow_nozzle == Tristate::yes && !config_store().get_nozzle_is_high_flow(physical_tool)) {
                virtual_tool_fails.set(VirtualToolCheck::nozzle_high_flow);
            }

            // With MMU, the filaments are intentionally unloaded at the start of the print
            if (!mmu_enabled) {
                if (auto fs = GetExtruderFSensor(physical_tool); fs && fs->get_state() == FilamentSensorState::NoFilament) {
                    virtual_tool_fails.set(VirtualToolCheck::filament_loaded);
                }
                if (auto fs = GetSideFSensor(physical_tool); !mmu_enabled && fs && fs->get_state() == FilamentSensorState::NoFilament) {
                    virtual_tool_fails.set(VirtualToolCheck::filament_loaded);
                }
            }

#if HAS_ANFC()
            if (auto v = buddy::openprinttag::filament_usage_tracker().filament_report(virtual_tool)) {
                remaining_filament_g += v->remaining_weight_g;
            } else {
                // At least one of the spools does not have an OPT
                // We cannot rely on the remaining filament calculations
                remaining_filament_g = NAN;
            }
#endif

#if HAS_INDX()
// TODO VirtualToolCheck::filament_calibrated
#endif

#if HAS_SPOOL_JOIN()
            // Spooljoin related checks
            if (previous_virtual_tool.has_value()) {
                does_spool_join = true;

                const auto previous_physical_tool = previous_virtual_tool->to_physical();
                if (!is_fsensor_working_state(GetExtruderFSensor(previous_physical_tool)) && !is_fsensor_working_state(GetSideFSensor(previous_physical_tool))) {
                    virtual_tool_fails.set(VirtualToolCheck::can_spool_join);
                }
            }
#endif
        };

#if HAS_SPOOL_JOIN()
        {
            std::optional<VirtualToolIndex> previous_tool;
            for (std::optional<VirtualToolIndex> tool = base_virtual_tool; tool.has_value();) {
                virtual_tool_check(*tool, previous_tool);
                previous_tool = tool;
                tool = args.spool_join.get_spool_2(*tool);
            }
        }
#else
        virtual_tool_check(base_virtual_tool, std::nullopt);
#endif

#if HAS_ANFC()
        // Note: Comparing NAN to anything is always FALSE, so if either of the values is unknown, we do not fail this check
        // We want to fail this check only when we are certain that we don't have enough filament
        if (extruder_info.filament_used_g.value_or(NAN) > remaining_filament_g) {
            gcode_tool_fails.set(GCodeToolCheck::enough_filament);
        }
#endif
    }
}
"""  # noqa: E501 (verbatim firmware lines)

TOOLS_MAPPING_CPP = """\
namespace tools_mapping {
bool is_tool_mapping_possible() {
#if HAS_TOOL_MAPPING()
    #if HAS_MMU2()
    if (!MMU2::mmu2.Enabled()) {
        return false;
    }
    #endif
    return GCodeInfo::getInstance().UsedExtrudersCount() > 1 || (get_num_of_enabled_tools() > 1 && GCodeInfo::getInstance().UsedExtrudersCount() > 0);
#endif
    return false;
}
"""  # noqa: E501 (verbatim firmware lines)

PAUSE_SETTINGS_CPP = """\
void Settings::SetResumeNozzleTemperature(int16_t temperature) {
    resume_nozzle_temperature = temperature;
}

float pause::Settings::purge_length() const {
    float r;

    if (purge_length_.has_value()) {
        r = *purge_length_;

    } else {
        // Double the purge length for HF nozzles
        r = ADVANCED_PAUSE_PURGE_LENGTH * (config_store().get_nozzle_is_high_flow(physical_tool()) ? 2 : 1);
    }

    return std::max<float>(std::abs(r), minimal_purge);
}
"""  # noqa: E501 (verbatim firmware lines)

GCODE_INFO_CPP = """\
void GCodeInfo::parse_m862(GcodeBuffer::String cmd) {
    using Check = buddy::gcode_compatibility::GeneralCheck;

    {
        // format is M862.x, so remove dot
        char dot = cmd.pop_front();
        if (dot != '.') {
            return;
        }
    }

    char subcode = cmd.pop_front();
    cmd.skip_ws();

    const auto check_compatibility = [&](const PrinterModelInfo *gcode_printer) {
        // Unknown gcode printer, sayonara!
        if (!gcode_printer) {
            info_.failed_gcode_checks.set(Check::printer_model);
            return;
        }
        const PrinterGCodeCompatibilityReport compatibility = PrinterModelInfo::current().gcode_compatibility_report(*gcode_printer);

        // If there isn't full compatibility of the gcode, report wrong printer model
        if (compatibility != PrinterGCodeCompatibilityReport { .is_compatible = true }) {
#if HAS_GCODE_COMPATIBILITY()
            if (compatibility.is_compatible) {
                info_.failed_gcode_checks.set(Check::gcode_compatibility_mode);
            } else
#endif
            {
                info_.failed_gcode_checks.set(Check::printer_model);
            }
        }
    };

    // Parse parameters
    [[maybe_unused]] uint8_t tool = 0; // Default is first tool
    std::optional<float> p_diameter;
    std::optional<bool> requires_hardened_nozzle, requires_high_flow_nozzle;
    while (!cmd.is_empty()) {
        const char letter = cmd.pop_front();
        if (letter == 'T') {
            tool = cmd.get_uint(); // Check particular tool (only for M862.1)
        } else if (letter == 'P') {
            switch (subcode) {

            case '1':
                if (auto val = cmd.get_float(); std::isfinite(val)) {
                    p_diameter = val; // Only store value in case Tx comes later
                }
                break;

            case '3': {
                const auto gcode_printer_str = cmd.get_string();
                check_compatibility(PrinterModelInfo::from_id_str(std::string_view(gcode_printer_str.begin, gcode_printer_str.end)));
                break;
            }

            case '4':
                // Parse M862.4 for minimal required firmware version
                if (!is_up_to_date(cmd.c_str())) {
                    info_.failed_gcode_checks.set(Check::minimum_fw_version);
                }
                break;

            case '2':
                check_compatibility(PrinterModelInfo::from_gcode_check_code(cmd.get_uint()));
                break;

            case '5':
                if (cmd.get_uint() > gcode_level) {
                    info_.failed_gcode_checks.set(Check::gcode_level);
                }
                break;

            case '6': {
                auto compare = [](GcodeBuffer::String &a, const char *b) {
                    for (char *c = a.begin;; ++c, ++b) {
                        if (c == a.end || *b == '\\0') {
                            return c == a.end && *b == '\\0';
                        }
                        if (toupper(*c) != toupper(*b)) {
                            return false;
                        }
                    }
                    return *b == '\\0';
                };
                auto feature = cmd.get_string();
                feature.trim();

#if HAS_MMU2()
                if (compare(feature, gcode_info::m862_6_mmu)) {
                    if (!MMU2::mmu2.Enabled()) {
                        info_.failed_gcode_checks.set(Check::mmu);
                    }
                    break;
                }
#endif
                if (compare(feature, gcode_info::m862_6_input_shaper)) {
                    info_.sliced_with_input_shaper_ = true;
                    break;
                }
#if HAS_INDX()
                if (compare(feature, gcode_info::m862_6_indx_lock)) {
                    info_.sliced_with_indx_lock_ = true;
                    break;
                }
#endif
#if HAS_HT_HOTEND()
                if (compare(feature, gcode_info::m862_6_ht_hotend)) {
                    info_.sliced_for_ht_hotend_ = true;
                    break;
                }
#endif

                log_error(Buddy, "Unsupported feature: %s", feature.c_str());
                info_.failed_gcode_checks.set(Check::unsupported_features);
                break;
            }
            }
        } else if (letter == 'A') {
            switch (subcode) {

            case '1':
                requires_hardened_nozzle = cmd.get_uint();
                break;

            default:
                break;
            }
        } else if (letter == 'F') {
            switch (subcode) {

            case '1':
                requires_high_flow_nozzle = cmd.get_uint();
                break;

            default:
                break;
            }
        }
        cmd.skip_nws();
        cmd.skip_ws();
    }

    const auto visit_tool = [&](const auto &visitor) {
#if HAS_MMU2()
        // MMU-equipped printers have only one nozzle diameter for all tools/slots
        // Makes the pre-print screen hide the nozzle sizes, which is both good and bad at the same time
        // -> "?.??" is gone, but no actual diameter is shown anymore - that can be tweaked further on the visualization side.
        // Here, we must set the correct nozzle diameter for all tools if specified.
        for (int8_t e = 0; e < EXTRUDERS; e++) {
            visitor(per_extruder_info[e]);
        }
#else
        if (tool < per_extruder_info.size()) {
            visitor(per_extruder_info[tool]);
        }
#endif
    };

    if (p_diameter.has_value()) {
        visit_tool([&](auto &info) { info.nozzle_diameter = *p_diameter; });
    }
    if (requires_hardened_nozzle.has_value()) {
        visit_tool([&](auto &info) { info.requires_hardened_nozzle = *requires_hardened_nozzle; });
    }
    if (requires_high_flow_nozzle.has_value()) {
        visit_tool([&](auto &info) { info.requires_high_flow_nozzle = *requires_high_flow_nozzle; });
    }
}

bool GCodeInfo::is_singletool_gcode() const {
    // Tool 0 needs to be given in comments and used
    if (!per_extruder_info[0].used()) {
        return false;
    }

    // Other tools need to not be given in comments at all
    for (uint8_t e = 1; e < std::size(per_extruder_info); e++) {
        if (per_extruder_info[e].given()) {
            return false;
        }
    }

    return true;
}
"""  # noqa: E501 (verbatim firmware lines)


PAUSE_SETTINGS_HPP = """\
class Pause; // forward declaration, so Settings does not think Pause is member of pause namespace

namespace pause {

class Settings {
public:
    Settings();
    static constexpr const float minimal_purge = 1;

"""  # noqa: E501 (verbatim firmware lines)

BOARD_H = """\
    #error Please define the BOARD macro
#elif BOARD() == BOARD_BUDDY()
    #define BOARD_IS_BUDDY() 1
#elif BOARD() == BOARD_XBUDDY()
    #define BOARD_IS_XBUDDY() 1
#elif BOARD() == BOARD_XLBUDDY()
    #define BOARD_IS_XLBUDDY() 1
#elif BOARD() == BOARD_DWARF()
    #define BOARD_IS_DWARF() 1
#elif BOARD() == BOARD_MODULARBED()
    #define BOARD_IS_MODULARBED() 1
#elif BOARD() == BOARD_XL_DEV_KIT_XLB()
    #define BOARD_IS_XLBUDDY()        1 // todo: remove, for now xl dev two  boards enabled
    #define BOARD_IS_XL_DEV_KIT_XLB() 1
#elif BOARD() == BOARD_XBUDDY_EXTENSION()
    #define BOARD_IS_XBUDDY_EXTENSION() 1
#elif BOARD() == BOARD_ANFC()
    #define BOARD_IS_ANFC() 1
#elif BOARD() == BOARD_TOOL_OFFSET_SENSOR()
    #define BOARD_IS_TOOL_OFFSET_SENSOR() 1
#elif BOARD() == BOARD_HTCONTROLLER()
    #define BOARD_IS_HTCONTROLLER() 1
#else
    #error BOARD is something weird
#endif

#ifndef BOARD_IS_BUDDY
    #define BOARD_IS_BUDDY() 0
#endif

#ifndef BOARD_IS_XBUDDY
    #define BOARD_IS_XBUDDY() 0
#endif

#ifndef BOARD_IS_XLBUDDY
    #define BOARD_IS_XLBUDDY() 0
#endif

#ifndef BOARD_IS_XL_DEV_KIT_XLB
    #define BOARD_IS_XL_DEV_KIT_XLB() 0
#endif

#ifndef BOARD_IS_DWARF
    #define BOARD_IS_DWARF() 0
#endif
"""

# An excerpt of `utils/presets/presets.json`: one image or more per build, with tool
# head, bed, extension, NFC and INDX head images.
PRESETS_JSON = """\
{
    "$schema": "./presets_schema.json",
    "presets": [
        {
            "name": "coreone",
            "description": "Original Prusa CORE One",
            "cacheVariables": {
                "PRINTER": "COREONE",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "XBUDDY",
                "XBUDDY_EXTENSION_VARIANT": "STANDARD"
            }
        },
        {
            "name": "coreone_indx",
            "description": "Original Prusa CORE One with INDX",
            "cacheVariables": {
                "PRINTER": "COREONE_INDX",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "XBUDDY",
                "XBUDDY_EXTENSION_VARIANT": "STANDARD"
            }
        },
        {
            "name": "coreonel",
            "description": "Original Prusa CORE One L",
            "cacheVariables": {
                "PRINTER": "COREONEL",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "XBUDDY",
                "XBUDDY_EXTENSION_VARIANT": "STANDARD"
            }
        },
        {
            "name": "coreonel_indx",
            "description": "Original Prusa CORE One L",
            "cacheVariables": {
                "PRINTER": "COREONEL_INDX",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "XBUDDY",
                "XBUDDY_EXTENSION_VARIANT": "STANDARD"
            }
        },
        {
            "name": "mini",
            "description": "Original Prusa MINI",
            "cacheVariables": {
                "PRINTER": "MINI",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "BUDDY"
            }
        },
        {
            "name": "mini-en-cs",
            "description": "Original Prusa MINI (EN & CS translation)",
            "cacheVariables": {
                "PRINTER": "MINI",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "BUDDY",
                "TRANSLATIONS_LIST": "cs"
            }
        },
        {
            "name": "mk4",
            "description": "Original Prusa MK4 with xBuddy 0.2.7c or newer fw compatible, 400-step motors and planetary gearbox",
            "cacheVariables": {
                "PRINTER": "MK4",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "XBUDDY"
            }
        },
        {
            "name": "mk3.5",
            "description": "Original Prusa MK3.5 with xBuddy 0.2.7c or newer fw compatible, 200-step motors and MK3 extruder",
            "cacheVariables": {
                "PRINTER": "MK3.5",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "XBUDDY"
            }
        },
        {
            "name": "ix",
            "description": "Original Prusa iX",
            "cacheVariables": {
                "PRINTER": "iX",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "XBUDDY",
                "XBUDDY_EXTENSION_VARIANT": "iX"
            }
        },
        {
            "name": "xl",
            "description": "Original Prusa XL",
            "cacheVariables": {
                "PRINTER": "XL",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "XLBUDDY"
            }
        },
        {
            "name": "xl_dev_kit",
            "description": "Original Prusa xl_dev_kit",
            "cacheVariables": {
                "PRINTER": "XL_DEV_KIT",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "XL_DEV_KIT_XLB"
            }
        },
        {
            "name": "xl-dwarf",
            "description": "Original Prusa XL's Dwarf (the extruder puppy)",
            "cacheVariables": {
                "PRINTER": "XL",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "DWARF"
            }
        },
        {
            "name": "xl-modularbed",
            "description": "Original Prusa XL's Modular Bed",
            "cacheVariables": {
                "PRINTER": "XL",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "MODULARBED"
            }
        },
        {
            "name": "ix-modularbed",
            "description": "Original Prusa iX's Modular Bed",
            "cacheVariables": {
                "PRINTER": "iX",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "MODULARBED"
            }
        },
        {
            "name": "coreone_indx-xbuddy_extension",
            "description": "Original Prusa CORE One INDX Extension Board",
            "cacheVariables": {
                "PRINTER": "COREONE_INDX",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "XBUDDY_EXTENSION"
            }
        },
        {
            "name": "anfc",
            "description": "Firmware for NFC addon (dev-kit)",
            "cacheVariables": {
                "PRINTER": "NONE",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "ANFC",
                "CAN_BUS_TYPE": "PUB6",
                "NFC_BOARD_HAS_LEFT_RIGHT_DETECTION_PIN": "YES",
                "NFC_BOARD_HAS_FILAMENT_SENSORS": "YES",
                "HAS_CYPHAL_LOGGING": "YES"
            }
        },
        {
            "name": "coreone_indx-anfc",
            "description": "Firmware for NFC addon for CoreOne printer with INDX",
            "cacheVariables": {
                "PRINTER": "COREONE_INDX",
                "SIGNATURE_OAK": "OFF",
                "BOARD": "ANFC",
                "CAN_BUS_TYPE": "PUB6",
                "NFC_ST25R3919B_ENABLED_ANTENNAS": "ANTENNA_2"
            }
        },
        {
            "name": "coreone_indx-indx_head",
            "description": "Firmware for INDX head",
            "cacheVariables": {
                "PRINTER": "COREONE_INDX",
                "BOARD": "INDX_HEAD"
            }
        }
    ]
}
"""  # noqa: E501 (verbatim firmware lines)

# The two MMU functions in src/common/tool_index.cpp.
TOOL_INDEX_CPP = """\
template <>
bool VirtualToolIndex::is_enabled() const {
    [[maybe_unused]] const auto &self = static_cast<const VirtualToolIndex &>(*this);

#if HAS_TOOLCHANGER()
    return self.to_physical().is_enabled();

#elif HAS_MMU2()
    static_assert(PhysicalToolIndex::count == 1);
    if (config_store().mmu2_enabled.get()) {
        // MMU has five slots, the virtual tool count should match
        static_assert(VirtualToolIndex::count == 5);

        // MMU is enabled - we have access to all virtual tools and can toolchange between them
        return true;

    } else {
        // Without the MMU, we only have one virtual tool available (there's nothing that would do the toolchange)
        return self.to_raw() == 0;
    }
#else
    // Single nozzle, single slot - nothing to get disabled
    static_assert(PhysicalToolIndex::count == 1);
    static_assert(VirtualToolIndex::count == 1);
    return true;
#endif
}

std::variant<VirtualToolIndex, NoTool> PhysicalExtension::currently_selected_virtual_tool() const {
    const auto &self = static_cast<const PhysicalToolIndex &>(*this);

#if HAS_MMU2()
    static_assert(PhysicalToolIndex::count == 1);
    if (MMU2::mmu2.Enabled()) {
        const auto e = MMU2::mmu2.get_current_tool();
        if (e == MMU2::FILAMENT_UNKNOWN) {
            return NoTool {};
        } else {
            return VirtualToolIndex::from_raw(e);
        }
    }
#else
    static_assert(PhysicalToolIndex::count == VirtualToolIndex::count);
#endif

    return VirtualToolIndex::from_raw(self.to_raw());
}
"""  # noqa: E501 (verbatim firmware lines)

# The MMU's switch in src/marlin_stubs/pause/M70X_mmu.cpp.
M70X_MMU_CPP = """\
/**
 * @brief turn mmu on
 *
 */
void filament_gcodes::mmu_on() {
    config_store().mmu2_enabled.set(true);

#if HAS_ANFC() && !HAS_ANFC_FOR_MULTIPLE_TOOLS()
    // Remove link to a NFC Tag
    for (const VirtualToolIndex tool : VirtualToolIndex::all()) {
        FilamentType { AdHocFilamentType { tool } }.modify_parameters([](FilamentTypeParameters &p) {
            p.openprinttag_uid_hash = buddy::openprinttag::ToolTag::no_tag_hash;
        });
    }
#endif

    MMU2::mmu2.Start();

    // To ensure SideFS (MMU's FS) is properly set up in FS handler
    // logical_sensor_states_ has to update with logical filament sensors already reconfigured and enabled
    FSensors_instance().request_enable_state_update();
}

/**
 * @brief turn mmu off
 *
 */
void filament_gcodes::mmu_off() {
    config_store().mmu2_enabled.set(false);

    // MMU uses config_store.filament_type to store what's preloaded in the MMU
    // When we turn the MMU off, it should now actually store what's in the nozzle.
    // If there is nothing in the nozzle, clear the setting.
    // BFW-5199
    if (!FSensors_instance().has_filament_surely(LogicalFilamentSensor::extruder)) {
        config_store().set_filament_type(VirtualToolIndex::from_raw(0), {});
    }

    MMU2::mmu2.Stop();

    // Update logical FS states
    FSensors_instance().request_enable_state_update();
}
"""  # noqa: E501 (verbatim firmware lines)

# The E move's count in `Planner::buffer_segment`, without the function around it.
PLANNER_CPP = """\
    /// Extruder movement without a tool picked doesn't make any sense
    if(!tools.physical_tool.has_value()) {
      bsod("E move without tool");
    }

    [[maybe_unused]] const float delta_e_mm = (xyze.e - position_float.e) * get_move_e_factor(tools, hints.move);

#if HAS_INDX()
    buddy::indx_tool_lock_hack().track_extruder_move(delta_e_mm, Badge<Planner>{});
#endif

#if HAS_FILAMENT_TRACKER()
    if(tools.virtual_tool.has_value()) {
      // Note: This is not >>ideal<<, because although the moves get planned, they might get discarded through (gcode_exceptions/quick_stop)
      // Most notably, this will track some extra filament usage if user intterupts purging
      // Tying this directly to the immediate motor positions might be better, but one would also need to also handle the origin resets
      // Live steps/mm against the stock extruder's calibration, so G-codes changing steps (M92) or flow are tracked right.
      // A custom extruder with user-set steps/mm would need its own calibrated value (e.g. an experimental
      // menu item stored in EEPROM next to the steps/mm); not supported.
      static constexpr float inv_calibrated_steps_per_mm = 1.0f / CALIBRATED_AXIS_STEPS_PER_UNIT_E0;
      buddy::filament_tracker().track_extruder_move(*tools.virtual_tool, {
        .commanded_mm = delta_e_mm,
        .feed_ratio = settings.axis_steps_per_mm[E_AXIS_N(tools.extruder)] * inv_calibrated_steps_per_mm,
      });
    }
#endif
"""  # noqa: E501 (verbatim firmware lines)

# Part of the status renderer's printer object.
STATUS_RENDERER_CPP = """\
            // XYZE, mm
            JSON_FIELD_FFIXED("axis_z", marlin_vars().logical_curr_pos[2], 1) JSON_COMMA;
            if (!marlin_client::is_printing()) {
                JSON_FIELD_FFIXED("axis_x", marlin_vars().logical_curr_pos[0], 1) JSON_COMMA;
                JSON_FIELD_FFIXED("axis_y", marlin_vars().logical_curr_pos[1], 1) JSON_COMMA;
            }
            JSON_FIELD_INT("flow", VirtualToolIndex::currently_selected_opt().transform([] (auto tool) { return marlin_vars().virtual_tools[tool].flow_factor.get(); }).value_or(0)) JSON_COMMA;
            JSON_FIELD_INT("speed", marlin_vars().print_speed) JSON_COMMA;
            JSON_FIELD_INT("fan_hotend", marlin_vars().active_hotend().heatbreak_fan_rpm) JSON_COMMA;
            JSON_FIELD_INT("fan_print", marlin_vars().active_hotend().print_fan_rpm);
"""  # noqa: E501 (verbatim firmware lines)

# `get_info`, the `/api/v1/info` renderer.
BASIC_GETS_CPP = """\
JsonResult get_info(size_t resume_point, JsonOutput &output) {
    char hostname[HOSTNAME_LEN + 1];
    netdev_get_hostname(netdev_get_active_id(), hostname, sizeof hostname);
    float nozzle_diameter = config_store().get_nozzle_diameter(PhysicalToolIndex::from_raw(0));
    auto mmu2_enabled =
#if HAS_MMU2()
        config_store().mmu2_enabled.get();
#else
        false;
#endif
    serial_nr_t serial {};
    otp_get_serial_nr(serial);

    // Keep the indentation of the JSON in here!
    // clang-format off
    JSON_START;
    JSON_OBJ_START;
        JSON_FIELD_FFIXED("nozzle_diameter", static_cast<double>(nozzle_diameter), 2) JSON_COMMA;
        JSON_FIELD_BOOL("mmu", mmu2_enabled) JSON_COMMA;
        JSON_FIELD_STR("serial", serial.begin()) JSON_COMMA;
        JSON_FIELD_STR("hostname", hostname) JSON_COMMA;
        JSON_FIELD_INT("min_extrusion_temp", EXTRUDE_MINTEMP);
    JSON_OBJ_END;
    JSON_END;
    // clang-format on
}
"""

# `mmu2_config.h`.
MMU2_CONFIG_H = """\
#pragma once
#include <stdint.h>
#ifndef UNITTEST
    #include <option/has_loadcell.h>
#else
    // @@TODO find a better way of separating load cell and MMU for unit tests
    // Beware - unit tests have been developed for Try-Load, therefore we pretend LoadCell is not present
    #define HAS_LOADCELL() 0
#endif

#if HAS_LOADCELL()
// we may want to return to try-load sequences on MK4 if E-motor stall turns out to be unreliable
// #define USE_TRY_LOAD
#else
    #define USE_TRY_LOAD
#endif

#define DEFAULT_SAFETYTIMER_TIME_MINS 30

///////////////////////////////////////////

// MMU Error pause position
#define MMU_ERR_X_PAUSE_POS  125
#define MMU_ERR_Y_PAUSE_POS  0
#define MMU_ERR_Z_PAUSE_LIFT 20

// As discussed with our PrusaSlicer profile specialist
// - ToolChange shall not try to push filament into the very tip of the nozzle
// to have some space for additional G-code to tune the extruded filament length
// in the profile
// Beware - this value is used to initialize the MMU logic layer - it will be sent to the MMU upon line up (written into its 8bit register 0x0b)
// However - in the G-code we can get a request to set the extra load distance at runtime to something else (M708 A0xb Xsomething).
// The printer intercepts such a call and sets its extra load distance to match the new value as well.
// The same applies to Pulley slow feed rate (register 0x14)
static constexpr uint8_t MMU2_TOOL_CHANGE_LOAD_LENGTH = 5; // mm
static constexpr float MMU2_LOAD_TO_NOZZLE_FEED_RATE = 20.0F; // mm/s

static constexpr float MMU2_UNLOAD_TO_FINDA_FEED_RATE = 120.0F; // mm/s

// The first the MMU does is initialise its axis. Meanwhile the E-motor will unload 20mm of filament in approx. 1 second.
static constexpr float MMU2_RETRY_UNLOAD_TO_FINDA_LENGTH = 20.0f; // mm
static constexpr float MMU2_RETRY_UNLOAD_TO_FINDA_FEED_RATE = 20.0f; // mm/s

static constexpr float MMU2_RETRY_UNLOAD_FINISH_LENGTH = -80.0f; // mm

static constexpr float MMU2_EXTRUDER_NOZZLE_LENGTH = 20.F;
#ifdef USE_TRY_LOAD
    #ifndef UNITTEST
        #if not PRINTER_IS_PRUSA_MK3_5()
            #error "Beware - this part has been tuned for MK35 (copied from MK3S), it probably won't work for MK4 anymore"
        #endif
    #endif
static constexpr float MMU2_FILAMENT_SENSOR_POSITION = 0; // mm
static constexpr float MMU2_EXTRUDER_PTFE_LENGTH = 42.3f; // mm
static constexpr float MMU2_EXTRUDER_HEATBREAK_LENGTH = 17.7f; // mm
static constexpr float MMU2_VERIFY_LOAD_TO_NOZZLE_TWEAK = -15.F; // mm used to shorten/lenghten (negative number -> shorten) the distange of verify load to nozzle
static constexpr float MMU2_CHECK_FILAMENT_PRESENCE_EXTRUSION_LENGTH = MMU2_EXTRUDER_PTFE_LENGTH + MMU2_EXTRUDER_HEATBREAK_LENGTH + MMU2_VERIFY_LOAD_TO_NOZZLE_TWEAK + MMU2_FILAMENT_SENSOR_POSITION;

static constexpr float MMU2_VERIFY_LOAD_TO_NOZZLE_FEED_RATE = 50.F;

    #define MMU2_LOAD_TO_NOZZLE_SEQUENCE                                       \\
        {                                                                      \\
            { MMU2_EXTRUDER_HEATBREAK_LENGTH, MMU2_LOAD_TO_NOZZLE_FEED_RATE }, \\
            { MMU2_EXTRUDER_NOZZLE_LENGTH + 40, 5.F }                          \\
        }
#else
static constexpr float MMU2_EXTRUDER_HEATBREAK_LENGTH = 67.F;
// ram the filament as deep as possible while checking for any obstacles
static constexpr float MMU2_FEED_DISTANCE = 50.F; // 50mm

// Obviously, we want to go as fast as possible, but...
// Beware - the speed has been carefully picked from a broad range to make sure it always detects something.
// The main issue is the fact, that the loadcell's sampling rate is close to the frequency of force spikes generated by the skipping motor.
// To overcome this, a few tricks have been applied:
// - lower detection threshold - may also allow the loadcell to "see" when the filament hits the main plate plastic part (when the forces are much lower)
// - pick a specific speed which is a bit different than the sampling rate
static constexpr float MMU2_FEED_RATE = 31.F; // 31mm/s

    #define MMU2_LOAD_TO_NOZZLE_SEQUENCE                                             \\
        {                                                                            \\
            { MMU2_EXTRUDER_HEATBREAK_LENGTH - MMU2_FEED_DISTANCE, MMU2_FEED_RATE }, \\
            { MMU2_EXTRUDER_NOZZLE_LENGTH + 10, 5.F }                                \\
        }
#endif

static constexpr uint8_t MMU2_NO_TOOL = 99;

typedef float feedRate_t;

struct E_Step {
    float extrude; ///< extrude distance in mm
    feedRate_t feedRate; ///< feed rate in mm/s
};

static constexpr E_Step ramming_sequence[] = FILAMENT_MMU2_RAMMING_SEQUENCE;
static constexpr E_Step load_to_nozzle_sequence[] = MMU2_LOAD_TO_NOZZLE_SEQUENCE;
"""  # noqa: E501 (verbatim firmware lines)

# `mmu2_mk4.h`, excerpts.
MMU2_MK4_H = """\
    /// Default nominal unload fsensor trigger distance.
    /// As this largely depends on the slicer profile, a register for setting a custom value is provided along with it.
    static constexpr float nominalEMotorFSOff = 14.0F;
    /// The "register" allowing user tweaking of the nominal trigger distance. Set a custom value through M708 A0x81 Xsomething.
    float nominalEMotorFSOffReg = nominalEMotorFSOff;
    /// A record of the last E-motor position when fsensor turned off while unloading
    float unloadEPosOnFSOff = nominalEMotorFSOff;
"""  # noqa: E501 (verbatim firmware lines)

# `mmu2_mk4.cpp`, excerpts.
MMU2_MK4_CPP = """\
namespace {

static constexpr float maximumAllowedExtruderMove = 189.9f; // we cannot plan anything longer than 190mm, stay on the safe side

bool MMU2::FeedWithEStallDetection() {
    // Obviously, we want to go as fast as possible, but...
    // Beware - the speed has been carefully picked from a broad range to make sure it always detects something.
    // The main issue is the fact, that the loadcell's sampling rate is close to the frequency of force spikes generated by the skipping motor.
    // To overcome this, a few tricks have been applied:
    // - lower detection threshold - may also allow the loadcell to "see" when the filament hits the main plate plastic part (when the forces are much lower)
    // - pick a specific speed which is a bit different than the sampling rate
    static constexpr float feedRate = MMU2_FEED_RATE;

    // ram the filament as deep as possible while checking for any obstacles

    #ifndef UNITTEST
    // get the best out of the HX717
    Loadcell::HighPrecisionEnabler enableHighPrecision(loadcell, /*enable*/ true);
    #endif

    // save state of EStall detection flags including the detection threshold
    EStallDetectionStateLatch esdsl;

    // block invocation of M1601 in Marlin::idle()
    BlockEStallDetection blocker;

    auto &emsd = EMotorStallDetector::Instance();

    // activate the detector
    emsd.SetEnabled();
    // whatever happened before is not interesting
    emsd.ClearDetected();
    // lower the detection threshold to overcome the sampling rate limitation - see explanation above
    emsd.SetDetectionThreshold(500'000.F);

    // plan the move and tweak by the amount of presumably remaining molten filament in the melt zone
    float remainingFilament = std::clamp(unloadEPosOnFSOff, 0.F, nominalEMotorFSOffReg); // anything below nominal distance will get subtracted from the load
    float compensatedFeedDistance = MMU2_FEED_DISTANCE - nominalEMotorFSOffReg + remainingFilament;

    { // dump the compensated feed distance into the syslog for verification
        char msg[32];
        snprintf(msg, sizeof(msg), "CompensatedFeedDistance=%2.1f", (double)compensatedFeedDistance);
        LogEchoEvent_P(msg);
    }

    extruder_move(compensatedFeedDistance, feedRate);
    while (planner_any_moves()) {
        if (emsd.DetectedRaw()) {
            planner_abort_queued_moves(); // stop instantly
            // @@TODO save the position where it tripped to allow retraction of the same amount
            return false;
        }
        safe_delay_keep_alive(0);
        if (planner_draining()) {
            return false; // power panic or a similar issue happened, bail out fast
        }
    }
    return CheckFailLoadToExtr(true);
}

bool MMU2::ToolChangeCommonOnce(uint8_t slot) {
    static_assert(MAX_RETRIES > 1); // need >1 retries to do the cut in the last attempt
    std::optional<float> firstUnloadEPosOnFSOff;
    for (uint8_t retries = MAX_RETRIES; retries; --retries) {
        for (;;) {
            Disable_E0(); // it may seem counterintuitive to disable the E-motor, but it gets enabled in the planner whenever the E-motor is to move
            tool_change_extruder = slot;

            allowPrematureFinish = true;

            // The idea here is to activate the MMU earlier than the printer is actually ready to unload the filament,
            // because engaging of the MMU's Idler takes ~1 second.
            // Thefore the MMU prepares the Idler into a close intermediate position and waits for printer's fsensor to turn off.
            // Once the FS is off, the MMU engages the Idler fully (which is much faster than engaging from the parking position),
            // pulls the filament out and pushes a new one in.
            // If the FS doesn't turn off within 4 seconds, the MMU reports an error.
            // Therefore we issue the MMU command ToolChange at this spot - even before the filament is freed from the drive gear.
            // Also, this is the only reason why MMU FW 3.0.3 is incompatible with 3.0.4 - it's an important change of the unload process.
            // But - we save a significant amount of time per toolchange -> shorter print times.
            logic.ToolChange(slot);

            // The gcode is expected to do ramming which ends with the filament
            // tip still in extruder gears. Rotate the extruder some more to
            // get the filament (including a potential string) completely out
            // after we've blocked the runout.
            if (extruder != MMU2_NO_TOOL) {
                extruder_move(MMU2_RETRY_UNLOAD_FINISH_LENGTH, logic.PulleySlowFeedRate());

                // Monitor the fsensor - detect the current E-motor stepper position when fsensor turns off.
                // That switch has some ideal (or expected) distance,
                // but in case some of the filament remains in the melt zone the distance gets shorter.
                // The main problem here is the fact, that we need to call the whole marlin infrastructure and there is no simple place to hook into.
                // Therefore I had to copy planner.synchronize(), which is a nasty hack.
                UnloadDistanceDetector udd;
                planner_synchronize_hook(udd);

                // Save just the very first unload attempt - that's the "amount" of filament left in the melt zone.
                // Repeated unload and reload attempts are not interesting for the compensation later.
                if (!firstUnloadEPosOnFSOff.has_value()) {
                    firstUnloadEPosOnFSOff = udd.FSTriggerDistance();
                }
#ifndef UNITTEST
                // Record as a metric each attempt
                metric_record_float(&metric_unloadDistanceFSOff, unloadEPosOnFSOff);
#endif
            }
            if (manage_response(true, true)) {
                allowPrematureFinish = false;
                if (planner_draining()) {
                    // Power panic. Give up fast.
                    return false;
                } else {
                    break;
                }
            }
            // otherwise: failed to perform the command - unload first and then let it run again
            IncrementMMUFails();

            // just in case we stood in an error screen for too long and the hotend got cold
            ResumeHotendTemp();
            // if the extruder has been parked, it will get unparked once the ToolChange command finishes OK
            // - so no ResumeUnpark() at this spot
            UnloadInner(PreUnloadPolicy::RelieveFilament);
            // if we run out of retries, we must do something ... may be raise an error screen and allow the user to do something
            // but honestly - if the MMU restarts during every toolchange,
            // something else is seriously broken and stopping a print is probably our best option.
        }
        if (VerifyFilamentEnteredPTFE()) {
            // make sure we don't assert on not having a value (shouldn't be possible)
            unloadEPosOnFSOff = firstUnloadEPosOnFSOff.value_or(nominalEMotorFSOffReg);
            return true; // success
        } else { // Prepare a retry attempt
            loadFilamentStarted = false;

            while (!manage_response(true, true)) {
                // Must wait for the MMU to finish its operation
                // - the PrematureFinish-exit from manage_response brought a nasty edge case
                // - but since this is "just" error recovery, it can wait a bit longer
            }

            UnloadInner(PreUnloadPolicy::ExtraRelieveHotFilament);
            if (retries == 2 && cutter_enabled()) {
                CutFilamentInner(slot); // try cutting filament tip at the last attempt
            }
        }
    }
    return false; // couldn't accomplish the task
}

void MMU2::ToolChangeCommon(uint8_t slot) {
    UpdateCurrentFilamentType(slot);
    SaveResumePos();
    while (!ToolChangeCommonOnce(slot)) { // while not successfully fed into extruder's PTFE tube
        if (planner_draining()) {
            return; // power panic happening, pretend the G-code finished ok
        }
        // failed autoretry, report an error by forcing a "printer" error into the MMU infrastructure - it is a hack to leverage existing code
        // @@TODO theoretically logic layer may not need to be spoiled with the printer error - may be just the manage_response needs it...
        logic.SetPrinterError(ErrorCode::LOAD_TO_EXTRUDER_FAILED);
        // We only have to wait for the user to fix the issue and press "Retry".
        // Please see CheckUserInput() for details how we "leave" manage_response.
        // If manage_response returns false at this spot (MMU operation interrupted aka MMU reset)
        // we can safely continue because the MMU is not doing an operation now.
        static_cast<void>(manage_response(true, true)); // yes, I'd like to silence [[nodiscard]] warning at this spot by casting to void
    }

    extruder = slot; // filament change is finished
    // @@TODO SpoolJoin::spooljoin.setSlot(slot);

    ++toolchange_counter;
    IncrementMMUChanges();
}

void MMU2::UnloadObeyAutoRetracted() {
    CommandInProgressGuard cipg(CommandInProgress::UnloadFilament, commandInProgressManager);
    SaveResumePos();
    if (!marlin_is_retracted()) {
        // if not already auto-retracted, do the ramming ourself
        WaitForHotendTargetTempBeep();
        UnloadInner(PreUnloadPolicy::Ramming);
    } else {
        // otherwise it's not even necessary to wait for temperature, the filament can be pulled out of the nube directly
        // Allow cold extrusion since Tx only loads to the gears not nozzle
        mapi::ColdExtrudeGuard cold_extrude_guard;
        UnloadInner(PreUnloadPolicy::RelieveFilament);
    }
}

void MMU2::UnloadInner(PreUnloadPolicy preUnloadPolicy) {
    UpdateCurrentFilamentType(extruder); // this is probably the only place where we need to rely on the "extruder" variable matching the currently unloaded slot
    FSensorBlockRunout blockRunout;
    BlockEStallDetection blockEStallDetection;

    switch (preUnloadPolicy) {
    case PreUnloadPolicy::Ramming:
        filament_ramming();
        break;
    case PreUnloadPolicy::ExtraRelieveHotFilament:
#ifdef USE_TRY_LOAD
        // try-load printers' ExtraRelieveHotFilament move was always the same as RelieveFilament
#else
        // BFW-7473: Assume the filament somehow made it into the melt zone (we have no reliable way of knowing),
        // but the E-motor skipped during the move.
        // Therefore, the relieve move must be extra slow in order to allow the filament tip to cool down
        // and not get wound up on the drive gear -> hence the slow feedrate
        extruder_move(-60.F, 10.F);
#endif
        [[fallthrough]]; // The rest of the relieve move will be done in the following RelieveFilament branch of this switch at a faster pace
    case PreUnloadPolicy::RelieveFilament:
        extruder_move(
#ifdef USE_TRY_LOAD
            // try-loads are symmetrical
            -40.F,
#else
            // But E-stall detection is not symmetrical - it needs to retract way more (because the filament may still be somewhere in the nozzle)
            // Theoretically, we should be able to retract the same distance as the failed load (when the E-motor skipped) + some extra margin
            -120.F,
#endif
            60.F);
        planner_synchronize();
        break;
    case PreUnloadPolicy::Nothing:
        break;
    };

    // we assume the printer managed to relieve filament tip from the gears,
    // so repeating that part in case of an MMU restart is not necessary
    for (;;) {
        Disable_E0();
        logic.UnloadFilament();
        if (manage_response(false, true)) {
            break;
        }
        IncrementMMUFails();
    }
    MakeSound(Confirm);

    // no active tool
    extruder = MMU2_NO_TOOL;
    tool_change_extruder = MMU2_NO_TOOL;
}

bool MMU2::unload() {
    if (!WaitForMMUReady()) {
        return false;
    }

    UnloadObeyAutoRetracted();

    ScreenUpdateEnable();
    marlin_finalize_unload();
    return true;
}

bool MMU2::load_filament_to_nozzle(uint8_t slot) {
    if (!WaitForMMUReady()) {
        return false;
    }

    FullScreenMsgLoad(slot);
    {
        // used for MMU-menu operation "Load to Nozzle"
        CommandInProgressGuard cipg(ExtendedCommandInProgress::LoadToNozzle, commandInProgressManager);

        if (extruder != MMU2_NO_TOOL) { // we already have some filament loaded - free it + shape its tip properly
            UnloadObeyAutoRetracted();
        }
        WaitForHotendTargetTempBeep(); // heat up to target temp of the new filament
        {
            FSensorBlockRunout blockRunout;
            BlockEStallDetection blockEStallDetection;
            ToolChangeCommon(slot);

            // Finish loading to the nozzle with finely tuned steps.
            execute_load_to_nozzle_sequence();
        }
        MakeSound(Confirm);
    }
    ScreenUpdateEnable();
    return true;
}

void MMU2::execute_load_to_nozzle_sequence() {
    planner_synchronize();
    // Compensate for configurable Extra Loading Distance
    // @@TODO 8bit needs this: planner_set_current_position_E(planner_get_current_position_E() - (logic.ExtraLoadDistance() - MMU2_FILAMENT_SENSOR_POSITION));
    execute_extruder_sequence(load_to_nozzle_sequence, sizeof(load_to_nozzle_sequence) / sizeof(load_to_nozzle_sequence[0]), ExtendedProgressCode::LoadingToNozzle);
}

bool MMU2::OnMMUProgressMsgChanged(ProgressCode pc) {
    ReportProgress(pc);
    switch (pc) {
    case ProgressCode::UnloadingToFinda:
        if ((CommandInProgress)logic.CommandInProgress() == CommandInProgress::UnloadFilament
            || ((CommandInProgress)logic.CommandInProgress() == CommandInProgress::ToolChange)) {
            // If MK3S sent U0 command, ramming sequence takes care of releasing the filament.
            // If Toolchange is done while printing, PrusaSlicer takes care of releasing the filament
            // If printing is not in progress, ToolChange will issue a U0 command.
            break;
        } else {
            // We're likely recovering from an MMU error
            planner_synchronize();
            unloadFilamentStarted = true;
            HelpUnloadToFinda();
        }
        break;
    case ProgressCode::FeedingToFSensor:
        // prepare for the movement of the E-motor
        // make sure we start at the same position every time - better for debugging.
        // GCode is not affected, it does it's own G92 E0 after the MMU toolchange
        marlin_resetE();
        extruder_move(maximumAllowedExtruderMove, logic.PulleySlowFeedRate()); // start the extuder
        loadFilamentStarted = true;
        break;
    case ProgressCode::DisengagingIdler: // only happens after the fsensor triggered correctly -> safe to kill the E-moves
        if (loadFilamentStarted) {
            loadFilamentStarted = false;
            // make sure we kill the moves in case we lost some packets on the communication
            // which should have been processed in OnMMUProgressMsgSame
            planner_abort_queued_moves();
            return allowPrematureFinish; // exit manage_response prematurely
        }
        break;
    default:
        // do nothing yet
        break;
    }
    return false;
}

void __attribute__((noinline)) MMU2::HelpUnloadToFinda() {
    extruder_move(-MMU2_RETRY_UNLOAD_TO_FINDA_LENGTH, MMU2_RETRY_UNLOAD_TO_FINDA_FEED_RATE);
}

bool MMU2::OnMMUProgressMsgSame(ProgressCode pc) {
    switch (pc) {
    case ProgressCode::UnloadingToFinda:
        if (unloadFilamentStarted && !planner_any_moves()) { // Only plan a move if there is no move ongoing
            switch (WhereIsFilament()) {
            case FilamentState::AT_FSENSOR:
            case FilamentState::IN_NOZZLE:
            case FilamentState::UNAVAILABLE: // actually Unavailable makes sense as well to start the E-move to release the filament from the gears
                HelpUnloadToFinda();
                break;
            default:
                unloadFilamentStarted = false;
            }
        }
        break;
    case ProgressCode::FeedingToFSensor:
        if (loadFilamentStarted) {
            switch (WhereIsFilament()) {
            case FilamentState::AT_FSENSOR:
                // fsensor triggered, finish FeedingToExtruder state
                loadFilamentStarted = false;
                planner_abort_queued_moves();
                {
#if PRINTER_IS_PRUSA_MK3_5()
                    // on the MK3.5 due to fsensor filtering delay (compared to MK3S),
                    // we are getting 0.175mm of extra loaded filament per 1mm/s speed increase of slow loading speed.
                    // i.e. for 20mm/s we get roughly 4mm of extra loaded filament
                    float loadingSpeedCompensation = 0.175f * logic.PulleySlowFeedRate();
#else
                    // no change for MK4 and MK3.9, fsensor is different there
                    static constexpr float loadingSpeedCompensation = 0.F;
#endif
                    extruder_move(logic.ExtraLoadDistance() + 2 - loadingSpeedCompensation, logic.PulleySlowFeedRate());
                }
                return allowPrematureFinish; // let the ExtraLoadDistance move finish and then bail out prematurely
                break;
            case FilamentState::NOT_PRESENT:
                // fsensor not triggered, continue moving extruder
                if (!planner_any_moves()) { // Only plan a move if there is no move ongoing
                    extruder_move(maximumAllowedExtruderMove, logic.PulleySlowFeedRate()); // start the extruder
                }
                break;
            case FilamentState::UNAVAILABLE:
                // @@TODO houston, we have a problem, fsensor unavailable, the MMU cannot continue doing anything without it
                // log an error and wait for the worst
                break;
            default:
                // Abort here?
                break;
            }
        }
        break;
    default:
        // do nothing yet
        break;
    }
    return false;
}

bool MMU2::cut_filament(uint8_t slot, bool enableFullScreenMsg /*= true*/) {
    if (!WaitForMMUReady()) {
        return false;
    }

    UpdateCurrentFilamentType(slot);
    if (enableFullScreenMsg) {
        FullScreenMsgCut(slot);
    }
    {
        if (FindaDetectsFilament()) {
            unload();
        }

        CommandInProgressGuard cipg(CommandInProgress::CutFilament, commandInProgressManager);
        CutFilamentInner(slot);
        extruder = MMU2_NO_TOOL;
        tool_change_extruder = MMU2_NO_TOOL;
        MakeSound(SoundType::Confirm);
    }
    ScreenUpdateEnable();
    return true;
}

bool MMU2::eject_filament(uint8_t slot, bool enableFullScreenMsg /* = true */) {
    if (!WaitForMMUReady()) {
        return false;
    }

    UpdateCurrentFilamentType(slot);
    if (enableFullScreenMsg) {
        FullScreenMsgEject(slot);
    }
    {
        if (FindaDetectsFilament()) {
            unload();
        }

        CommandInProgressGuard cipg(CommandInProgress::EjectFilament, commandInProgressManager);
        SaveResumePos();
        for (;;) {
            Disable_E0();
            logic.EjectFilament(slot);
            if (manage_response(false, true)) {
                break;
            }
            IncrementMMUFails();
        }
        extruder = MMU2_NO_TOOL;
        tool_change_extruder = MMU2_NO_TOOL;
        MakeSound(Confirm);
    }
    ScreenUpdateEnable();
    return true;
}

bool MMU2::tool_change(uint8_t slot) {
    if (!WaitForMMUReady()) {
        return false;
    }

    if (slot != extruder) {
        if (/*FindaDetectsFilament()*/
            /*!IS_SD_PRINTING && !usb_timer.running()*/
            !marlin_printingIsActive()) {
            // If Tcodes are used manually through the serial
            // we need to unload manually as well -- but only if FINDA detects filament
            unload();
        }

        CommandInProgressGuard cipg(CommandInProgress::ToolChange, commandInProgressManager);
        FSensorBlockRunout blockRunout;
        BlockEStallDetection blockEStallDetection;
        planner_synchronize();
        ToolChangeCommon(slot);
    }
    return true;
}

bool MMU2::tool_change_full(uint8_t slot) {
    if (!WaitForMMUReady()) {
        return false;
    }

    if (slot != extruder) {
        planner_synchronize();

        pos3d resume_pos = planner_current_position();

        if (all_axes_homed()) {
            nozzle_park();
        }

        unload();

        CommandInProgressGuard cipg(CommandInProgress::ToolChange, commandInProgressManager);
        FSensorBlockRunout blockRunout;
        BlockEStallDetection blockEStallDetection;
        planner_synchronize();
        ToolChangeCommon(slot);

        execute_load_to_nozzle_sequence();

        if (all_axes_homed()) {
            // Unpark the nozzle
            motion_do_blocking_move_to_xy(resume_pos.xyz[0], resume_pos.xyz[1], feedRate_t(NOZZLE_PARK_XY_FEEDRATE));
            motion_do_blocking_move_to_z(resume_pos.xyz[2], feedRate_t(NOZZLE_PARK_Z_FEEDRATE));
        }
    }

    return true;
}

bool MMU2::WaitForMMUReady() {
    switch (State()) {
    case xState::Stopped:
        return false;
    case xState::Connecting:
    case xState::Bootloader:
        // shall we wait until the MMU reconnects?
        // fire-up a fsm_dlg and show "MMU not responding"?
    default:
        return true;
    }
}
"""  # noqa: E501 (verbatim firmware lines)

# `multi_filament_change.cpp`, excerpts.
MULTI_FILAMENT_CHANGE_CPP = """\
void Config::execute() const {
    // If we have nothing to do, we can exit right now
    if (std::ranges::all_of(items, [](const auto &i) { return i.action == Action::keep; })) {
        return;
    }

    marlin_server::FSM_Holder fsm { PhaseWait::print_status_message };

    StrongIndexArray<FilamentType, VirtualToolIndex::count, VirtualToolIndex, VirtualToolIndex::to_raw_static> old_filaments;
    for (auto tool : VirtualToolIndex::all()) {
        old_filaments[tool] = config_store().get_filament_type(tool);
    }

#if HAS_TOOLCHANGER()
    // Start preheating all nozzles
    for (auto tool : VirtualToolIndex::all().skip_all_disabled()) {
        const auto &item = items[tool];
        if (item.action == Action::keep) {
            continue;
        }

        const uint16_t temperature = max(FilamentType { item.new_filament }.parameters().nozzle_temperature, old_filaments[tool].parameters().nozzle_temperature);
        thermalManager.setTargetHotend(temperature, tool.to_physical());
    }
#endif

    // Lift Z to prevent unparking and parking of each tool
    // without a pre-raise, every iteration would do its own Z movement (raise + lower)
    GcodeSuite::process_subcommands_now_P("G27 P0 Z40");

    /* MMU2 Reimplementation

        MMU should be in unloaded state right now (as every start of print)
        For all selected tools (in this case tool == MMU's filament slot) change filament
        This means MMU will go through selected tools and load to the slot (NOT load to extruder)
    */

    // Carry out the changes
    for (auto tool : VirtualToolIndex::all().skip_all_disabled()) {
        const auto &item = items[tool];
        switch (item.action) {

        case Action::keep:
            break;

        case Action::unload: {
#if HAS_MMU2()
            config_store().set_filament_type(tool, FilamentType::none);
#else
            filament_gcodes::M702_unload(
                std::nullopt,
                Z_AXIS_LOAD_POS,
                RetAndCool_t::Return,
                tool,
                false);
#endif
            break;
        }

        case Action::change: {
#if HAS_MMU2()
            // preload the MMU slot
            filament_gcodes::mmu_load(tool.to_raw());

            config_store().set_filament_type(tool, filament::apply_load_color(FilamentType { item.new_filament }, tool, item.color));
#else
            const FilamentType new_filament { item.new_filament };
            if (old_filaments[tool] != FilamentType::none) {
                filament_gcodes::M1600_change_filament(
                    new_filament,
                    tool,
                    RetAndCool_t::Return,
                    filament_gcodes::AskFilament_t::Never,
                    item.color);
            } else {
                filament_gcodes::M701_load(filament_gcodes::M701LoadArgs {
                    .filament_to_be_loaded = new_filament,
                    .z_min_pos = Z_AXIS_LOAD_POS,
                    .op_preheat = RetAndCool_t::Return,
                    .virtual_tool = tool,
                    .color_to_be_loaded = item.color,
                });
            }
#endif
            break;
        }
        }
    }
}
"""  # noqa: E501 (verbatim firmware lines)

# `screen_menu_mmu_preload_all.cpp`, excerpts.
MMU_PRELOAD_ALL_CPP = """\
void MI_FILAMENT::click(IWindowMenu &) {
    // Set filament type for all MMU slots and issue preload commands
    for (auto slot : VirtualToolIndex::all()) {
        config_store().set_filament_type(slot, filament_type);
        marlin_client::gcode_printf("M704 P%d", slot.to_raw());

        // Wait while there are more than one command in queue to prevent race conditions
        while (marlin_vars().gqueue > 1) {
            gui::TickLoop();
            DialogHandler::Access().Loop();
            gui_loop();
        }
    }

    // Wait for all preload operations to finish
    while (marlin_vars().is_processing.get()) {
        gui::TickLoop();
        DialogHandler::Access().Loop();
        gui_loop();
    }

    Screens::Access()->close_screen();
}
"""

# `screen_menu_filament_mmu.cpp`, excerpts.
FILAMENT_MMU_CPP = """\
void ScreenMenuFilamentMMU::windowEvent(window_t *sender, GUI_event_t event, void *param) {
    if (event == GUI_event_t::LOOP) {
        bool filament_in_nozzle = FSensors_instance().WhereIsFilament() == MMU2::FilamentState::AT_FSENSOR;

        // Some operations are not available when filament is loaded all the
        // way to the nozzle (the MMU can't move then).
        //
        // Update the status ‒ both because it can change from a submenu and in
        // theory by a gcode sent remotely from eg. Connect.
        Item<MI_MMU_CUT_FILAMENT>().set_enabled(!filament_in_nozzle);
        Item<MI_MMU_EJECT_FILAMENT>().set_enabled(!filament_in_nozzle);
        Item<MI_MMU_PRELOAD>().set_enabled(!filament_in_nozzle);
    }

    ScreenMenu::windowEvent(sender, event, param);
}
"""  # noqa: E501 (verbatim firmware lines)

# `MItem_mmu.cpp`, excerpts.
MITEM_MMU_CPP = """\
MI_MMU_LOAD_TO_NOZZLE_I::MI_MMU_LOAD_TO_NOZZLE_I(VirtualToolIndex tool)
    : MI_MMU_ITEM_WITH_SLOT(tool.to_raw(), N_("Load Filament %i")) {
    filament_type_ = FilamentType::for_tool(tool);

    StringBuilder info_sb { info_buffer_ };
    filament_type_.build_name_with_info(info_sb);

    extension_ = MenuItemColorSwatchExtension {
        .text = string_view_utf8::MakeRAM(info_buffer_.data()),
        .color = filament_type_.parameters().color,
        .show_null_color = false,
    };
    extension_width = extension_.compute_width();

    set_enabled(filament_type_ != FilamentType::none);
}

void MI_MMU_LOAD_TO_NOZZLE_I::printExtension(Rect16 extension_rect, Color color_text, Color color_back, ropfn raster_op) const {
    (void)color_text;
    extension_.draw(*this, extension_rect, color_back, raster_op);
}

void MI_MMU_LOAD_TO_NOZZLE_I::click(IWindowMenu &) {
    ArrayStringBuilder<MAX_CMD_SIZE> gcode;
    gcode.append_string("M701 S\\"");
    filament_type_.build_encoded_gcode_param(gcode);
    gcode.append_printf("\\" W2 P%i", slot_i_);
    gui_try_gcode_with_msg(gcode.str());
}

void MI_MMU_ENABLE::OnChange([[maybe_unused]] size_t old_index) {
    if (value() && !config_store().is_mmu_rework.get() && !set_mmu_rework(true)) {
        set_value(false);
        return;

    } else if (value() && !FSensors_instance().is_working(LogicalFilamentSensor::extruder)) {
        MsgBoxWarning(_("Can't enable MMU: calibrate and enable the printer's filament sensor first."), Responses_Ok);
        set_value(false);
        return;
    }

    marlin_client::gcode(value() ? "M709 S1" : "M709 S0");
}
"""  # noqa: E501 (verbatim firmware lines)  # noqa: E501 (verbatim firmware lines)

# The MMU's start in `app_setup`.
APPMAIN_CPP = """\
static void app_setup(void) {
    metric_record_event(&metric_app_start);

#if HAS_MMU2()
    if (config_store().mmu2_enabled.get()) {
        MMU2::mmu2.Start();
    }
#endif
}
"""

# The filament sensors' MMU setting, idle clear, switch-off and forced sensors.
FILAMENT_SENSORS_HANDLER_CPP = """\
void FilamentSensors::reconfigure_sensors_if_needed(bool force) {
    const auto new_tool = stdext::get_optional<PhysicalToolIndex>(PhysicalToolIndex::currently_selected());
    const auto new_tool_index = new_tool ? new_tool->to_raw() : PhysicalToolIndex::count;

    if (!force && new_tool_index == tool_index) {
        return;
    }

    tool_index = new_tool_index;

    has_mmu =
#if HAS_MMU2()
        config_store().mmu2_enabled.get(); // there is a slight semantic difference:
    // - mmu2_enabled in eeprom describes the "intent" to enable MMU on the machine (this flag is true even when the MMU is not communicating)
    // - mmu2.Enabled() is true when the MMU is operational (i.e. powered up and initial communication handshake accomplished)
    // at this spot of filament runout logic, using the "intent" is better because:
    // - you cannot start a print with MMU in the "connecting" stage
    // - when communication drops out during print, an error screen occurrs and must be resolved to continue -> runout are completely irrelevant at that moment
    // - enabling MMU in the eeprom ("intent") is an atomic operation, while it takes ~10s for the MMU to become operational
#else
        false;
#endif

    extruder_fs_independent =
#if PRINTER_IS_PRUSA_iX()
        // iX always has the "MMU rework"
        false;
#elif HAS_MMU2()
        !config_store().is_mmu_rework.get();
#else
            true;
#endif

    using LFS = LogicalFilamentSensor;
    auto &ls = logical_sensors_;

    const auto extruder_fs = new_tool ? GetExtruderFSensor(*new_tool) : nullptr;
    const auto side_fs = new_tool ? GetSideFSensor(*new_tool) : nullptr;

    const bool side_fs_enabled = side_fs && side_fs->is_enabled();

    ls[LFS::extruder] = extruder_fs;
    ls[LFS::side] = side_fs;
    ls[LFS::primary_runout] = side_fs_enabled ? side_fs : extruder_fs;
    ls[LFS::closest_to_nozzle] = extruder_fs ?: side_fs;
    ls[LFS::closest_to_nozzle_independent] = (extruder_fs && extruder_fs_independent) ? extruder_fs : side_fs;
}

void FilamentSensors::process_events() {
    if (isEvLocked()) {
        return;
    }

    const auto check_runout = [&](LogicalFilamentSensor s) {
        auto sen = sensor(s);
        if (sen == nullptr) {
            return false;
        }

        const auto event = sen->last_event();
        if (m600_sent || event != IFSensor::Event::filament_removed) {
            return false;
        }

#if HAS_CRASH_DETECTION()
        if (crash_s.get_state() != Crash_s::PRINTING) {
            // Only allow runouts in print and outside of crash recovery
            return false;
        }
#endif

        m600_sent = true;

        const auto tool = VirtualToolIndex::currently_selected_opt();

        if (tool.has_value()) {
            marlin_server::gcode_interrupt(GCodeLiteral { .gcode = "M600 A P T%.0f", .parameter = static_cast<float>(tool->to_raw()) });
            log_info(FSensor, "Injected runout for T: %u", tool->to_raw());
        } else {
            return false;
        }

        return true;
    };

    const auto check_autoload = [&]() {
        const auto is_insert_event = [&](LogicalFilamentSensor s) {
            const auto si = sensor(s);
            return si && si->last_event() == IFSensor::Event::filament_inserted;
        };

        if (!(
                (
                    // Trigger autoload from extruder FS only if the filament is not missing in the side FS
                    (is_insert_event(LogicalFilamentSensor::extruder) && !no_filament_surely(LogicalFilamentSensor::side)
                        // On the MMU rework, the "FS autoload" configuration item is hidden (BFW-4290)
                        // So there's no way to enable it when it has been disabled in prior.
                        // Keep the option enabled for power users, in the spirit of the OG task assignment
                        && (config_store().fs_autoload_enabled.get() || !extruder_fs_independent))

                    // Trigger autoload from side FS only if there is no filament in the extruder
                    || (is_insert_event(LogicalFilamentSensor::side) && !has_filament_surely(LogicalFilamentSensor::extruder) && config_store().fs_autoload_enabled.get())
                    //
                    )
                && !has_mmu
                && !autoload_sent
                && !marlin_server::is_processing()
                && !isAutoloadLocked())
            //
        ) {
            return false;
        }

        autoload_sent = true;

#if HAS_ANFC()
        const auto opt_uid_hash = buddy::openprinttag::autoload_auto_assign_tool_tag_hash.load();
        const auto opt_tag = VirtualToolIndex::currently_selected_opt().and_then(buddy::openprinttag::ToolTag::for_tool_ephemeral);
        if (opt_tag.has_value() && opt_tag->uid_hash() == opt_uid_hash) {
            // Make sure we don't lose any data when casting to float
            static_assert(std::numeric_limits<float>::digits >= std::numeric_limits<decltype(opt_uid_hash)>::digits);
            marlin_server::inject(GCodeLiteral { .gcode = "M1701 G%.0f", .parameter = (float)opt_uid_hash });
        } else
#endif
        {
            marlin_server::inject(GCodeLiteral { .gcode = "M1701" });
        }

        log_info(FSensor, "Injected autoload");

        return true;
    };

    if (marlin_server::is_printing()) {
        if (check_runout(LogicalFilamentSensor::primary_runout)) {
            return;
        }

        // With an MMU, don't check for runout on the extruder sensor, it would be too late for anything
        if (!has_mmu && check_runout(LogicalFilamentSensor::extruder)) {
            return;
        }
    } else {
        // During MMU standard operation, there is no filament loaded to the nozzle when not printing.
        // So it's not a good idea to reset what filament types we have stored.
        if (!has_mmu && no_filament_surely(LogicalFilamentSensor::closest_to_nozzle) && tool_index < PhysicalToolIndex::count) {
            const auto physical_tool = PhysicalToolIndex::from_raw(tool_index);
            const auto virtual_tool = stdext::get_optional<VirtualToolIndex>(physical_tool.currently_selected_virtual_tool());
            if (virtual_tool.has_value()) {
                config_store().set_filament_type(*virtual_tool, FilamentType::none);
            }
        }

        if (check_autoload()) {
            return;
        }
    }
}

void FilamentSensors::process_enable_state_update() {
    enable_state_update_processing = true;
    enable_state_update_pending = false;

    for_all_sensors([](IFSensor &s, PhysicalToolIndex, bool) {
        s.set_enabled(should_enable(s.id()));
    });

#if HAS_MMU2()
    if (config_store().mmu2_enabled.get() && !config_store().fsensor_enabled.get()) {
        // MMU requires enabled filament sensor to work, turn it off if the fsensors got disabled
        marlin_server::enqueue_gcode("M709 S0");
    }
#endif

    enable_state_update_processing = false;
}

bool should_enable(FilamentSensorID id) {
#if HAS_MMU2()
    /// MMU override: when MMU is active, both extruder and side sensors
    /// are required regardless of per-sensor enable bits.
    if (config_store().mmu2_enabled.get()) {
        return true;
    }
#endif

    if (!config_store().fsensor_enabled.get()) {
        return false;
    }

    if (id.position == FilamentSensorID::Position::side) {
        return config_store().fsensor_side_enabled_bits.get() & (1 << id.index);
    } else {
        return config_store().fsensor_extruder_enabled_bits.get() & (1 << id.index);
    }
}
"""  # noqa: E501 (verbatim firmware lines)

# `marlin_server.cpp`, excerpts: `pre_finalize_print`, its unload, the single-tool start, and
# the preview's Print and Inactive results.
MARLIN_SERVER_CPP = """\
static bool pre_finalize_print([[maybe_unused]] bool finished) {
#if HAS_ANFC()
    buddy::openprinttag::filament_usage_tracker().flush({
        .tools = AllTools {},
        .warn_on_failure = true,
    });
#endif

#if HAS_AUTO_RETRACT()
    // During multi tool printing, slicer handles retraction/ramming and keeps FW in the dark
    // FilamentTracker keeps track of retracted distances on each hotend
    // This overwrites retracted distances in persistent storage with temporary ones from FilamentTracker
    for (auto tool : PhysicalToolIndex::all()) {
        const auto dist = buddy::filament_tracker().get_retracted_distance(tool);
        // Do not save filament_tracker value before it was validated
        if (dist.has_value()) {
            // update only used hotends
            buddy::auto_retract().set_retracted_distance(tool, dist);
        }
    }
#endif

#if HAS_MMU2()
    if (MMU2::mmu2.Enabled()) {
        // Unloading from nozzle is handled by Slicer, do not use auto_retract (frequent filament changes cause filament_tracker cannot properly hold valid value)
        // When we are running single-filament gcode with MMU, we should unload current filament.
        if (!finished || GCodeInfo::getInstance().is_singletool_gcode()) {
            safely_unload_filament_from_nozzle_to_mmu();
        }
    } else
#endif

#if HAS_AUTO_RETRACT()
        if (true) {
        buddy::auto_retract().maybe_retract_from_nozzle();
    } else
#endif
    {
    }

#if HAS_NOZZLE_CLEANER()
    // Skip nozzle cleaning if no tool is picked (e.g. tool already parked during pause on INDX)
    if (!std::holds_alternative<NoTool>(PhysicalToolIndex::currently_selected())) {
        if (!nozzle_cleaner::load_and_execute(nozzle_cleaner::Sequence::clean)) {
            return false;
        }
        mapi::park(mapi::get_parking_position(mapi::ParkPosition::park).without_z_move());
    }
#endif

#if HAS_INDX()
    // Park the tool to its dock and persist the state to eeprom.
    // Done here (still with axes homed) so tool_change's ensure_safe_move() doesn't trigger another G28.
    tool_change(NoTool {}, tool_return_t::no_return);
    prusa_toolchanger.persist_last_picked_tool(PhysicalToolIndex::currently_selected(), true);
#endif

#if HAS_CHAMBER_VENTS()
    buddy::chamber().close_vents_after_print();
#endif

    disable_e_steppers();
    disable_xy_steppers();

    return true;
}

void safely_unload_filament_from_nozzle_to_mmu() {
    if (MMU2::WhereIsFilament() == MMU2::FilamentState::NOT_PRESENT) {
        return; // no filament loaded, nothing to do
    }
    const uint16_t original_temp = thermalManager.degTargetHotend(active_extruder);
    enqueue_gcode("M702 W2");
    enqueue_gcode_printf("M104 S%" PRIu16, original_temp);
}

#if HAS_MMU2()
            if (MMU2::mmu2.Enabled() && GCodeInfo::getInstance().is_singletool_gcode() && MMU2::mmu2.get_current_tool() == MMU2::FILAMENT_UNKNOWN) {
                // POC: Handle singletool G-code which doesn't have T commands in it
                // In case we don't have other filament loaded!
                // Unfortunately we don't have the nozzle heated, an ugly workaround is to enqueue an M109 :(

                const uint16_t preheat_temp = GCodeInfo::getInstance().get_hotend_preheat_temp().value_or(215U);
                enqueue_gcode_printf("M109 S%" PRIu16, preheat_temp); // speculatively, use PLA temp for MMU prints, anything else is highly unprobable at this stage
                enqueue_gcode("T0"); // tool change T0 (can be remapped to anything)
                enqueue_gcode("G92 E0"); // reset extruder position to 0

                bool is_relative = gcode.axis_is_relative(AxisEnum::E_AXIS);

                enqueue_gcode("M82"); // set E to absolute positions
    #if HAS_NEXTRUDER()
                enqueue_gcode("G1 E25 F1860"); // push filament into the nozzle - load distance from fsensor into nozzle tuned (hardcoded) for now
                enqueue_gcode("G1 E35 F300"); // slowly push another 10mm (absolute E)
    #elif PRINTER_IS_PRUSA_MK3_5()
                enqueue_gcode("G1 E50 F1860"); // push filament into the nozzle - load distance from fsensor into nozzle tuned (hardcoded) for now
                enqueue_gcode("G1 E62 F300"); // slowly push another 12mm (absolute E)
    #else
        #error
    #endif
                if (is_relative) {
                    enqueue_gcode("M83"); // set E back to relative positions
                }

                // In case of need, we can perform a custom purge line from the other end of the heatbed
                // It would require homing the axes first, moving to [maxx-10, -4] and slowly purging while moving towards the origin
            }
#endif

        case PrintPreview::Result::Print:
        case PrintPreview::Result::Inactive:
            did_not_start_print = false;
            server.print_state = State::PrintInit;
            break;
"""  # noqa: E501 (verbatim firmware lines)

# The tag radio's MMU condition.
REQUEST_MANAGER_CPP = """\
    static_assert(!HAS_INDX(), "Revisit all HAS_ANFC_FOR_MULTIPLE_TOOLS block (especially code below) when implementing NFC for 'multi-tool' printers.");
#if (HAS_MMU2() && !HAS_ANFC_FOR_MULTIPLE_TOOLS())
    // Turn NFC antenna off if MMU is active
    // !!This code should be temporary!!
    const bool want_radio = !config_store().mmu2_enabled.get();
#else
    const bool want_radio = true;
#endif
"""  # noqa: E501 (verbatim firmware lines)

# Whether a tool has a tag reader.
TOOL_TAG_BUDDY_CPP = """\
bool has_tool_openprinttag_reader([[maybe_unused]] VirtualToolIndex tool) {

#if HAS_MMU2() && !HAS_ANFC_FOR_MULTIPLE_TOOLS()
    return !config_store().mmu2_enabled.get();
#else
    return true;
#endif
}
"""

# The MMU's filament menu.
SCREEN_MENU_FILAMENT_MMU_HPP = """\
using ScreenMenuFilamentMMU__ = ScreenMenu<GuiDefaults::MenuFooter, MI_RETURN,
    MI_LOADED_FILAMENT,
    MI_MMU_PRELOAD,
    MI_MMU_PRELOAD_ALL,
    MI_MMU_LOAD_TO_NOZZLE,
    MI_MMU_UNLOAD_FILAMENT,
    MI_MMU_EJECT_FILAMENT,
    MI_MMU_CUT_FILAMENT,
    // MI_PURGE, doing purges in MMU mode is not only unsupported but dangerous and doesn't play well with the preferred workflow of the MMU-enabled machine
    // debug items, but the functionality will be used by the error screens - hidden in release builds
    MI_MMU_SW_RESET,
    MI_MMU_HW_RESET,
    MI_MMU_POWER_CYCLE,
    MI_FILAMENT_MANAGEMENT>;
"""  # noqa: E501 (verbatim firmware lines)

M600_CPP = """\
void M600_manual(const GCodeParser2 &p) {
    const std::optional<VirtualToolIndex> virtual_tool = stdext::get_optional<VirtualToolIndex>(PrusaGcodeSuite::get_target_virtual_from_command_p(p));
    if (!virtual_tool.has_value()) {
        return;
    }
    VirtualToolIndex target_tool = *virtual_tool;

    XYZval<float, LogicalPosTag> logical_park_point { NAN, NAN, NAN };

    // Lift Z axis
    p.store_option_if_present('Z', logical_park_point.z);

    // Move XY axes to filament change position or given position
    p.store_option_if_present('X', logical_park_point.x);
    p.store_option_if_present('Y', logical_park_point.y);

    const auto park_point = logical_park_point.asNative();

    auto park_position = mapi::get_parking_position(mapi::ParkPosition::filament_change);
    if (!std::isnan(park_point.x)) {
        park_position.x = park_point.x;
    }
    if (!std::isnan(park_point.y)) {
        park_position.y = park_point.y;
    }
    if (!std::isnan(park_point.z)) {
        park_position.z = park_point.z;
    }

    const xyze_float_t no_return = { { { NAN, NAN, NAN, current_position.e } } };

    M600_execute(park_position,
        target_tool,
        p.option<bool>('N') ? no_return : current_position,
        p.option<float>('U'),
        p.option<float>('L'),
        p.option<float>('E').transform(fabsf),
        p.option<Color>('C'),
        p.option<FilamentType>('S'),
        false);
}
"""  # noqa: E501 (verbatim firmware lines)

SCREEN_M600_CPP = """\
void MI_M600::click([[maybe_unused]] IWindowMenu &window_menu) {
#if HAS_TOOLCHANGER()
    if (prusa_toolchanger.is_toolchanger_enabled()) {
        const auto tool = select_tool_dialog({
            .allow_return = true,
        });
        if (tool.has_value() && inject(*tool)) {
            Screens::Access()->close_screen();
        }
        return;
    }
#endif
    match(
        VirtualToolIndex::currently_selected(),
        [](VirtualToolIndex virtual_tool) { inject(virtual_tool); },
        [](NoTool) { debug_assert(false); /* theoretically reachable edge case - do nothing */ });
}
"""

# `marlin_print_preview.cpp`, excerpts: the filament check before a print and the preview's
# state machine, with the MMU's unload question and its answers.
MARLIN_PRINT_PREVIEW_CPP = """\
IPrintPreview::State PrintPreview::stateFromFilamentPresence() const {

#if HAS_MMU2()
    if (FSensors_instance().HasMMU()) {
        if (!config_store().fsensor_enabled.get()) {
            return State::checks_done;
        }

        buddy::gcode_compatibility::CompatibilityReport report;
        report.generate_toolmapping_only({});

        // with MMU, its only possible to check that filament is properly unloaded, no check of filaments presence in each "tool"
        if (FSensors_instance().MMUReadyToPrint()) {
            return State::checks_done;
        } else if (GCodeInfo::getInstance().is_singletool_gcode()
            && FSensors_instance().WhereIsFilament() == MMU2::FilamentState::AT_FSENSOR
            // only allow this shortcut print start if filament type is matching
            && !report.failed(buddy::gcode_compatibility::VirtualToolCheck::filament_type) //
        ) {
            // beware: State::checks_done shows the toolmapping screen.
            // We don't want that in MMU mode printing a single material gcode with filament already loaded in the nozzle.
            // In such a case we want to avoid toolmapping and start the print right away instead -> set State::done and not checks_done
            return State::done;
        } else {
            // Request removal of the inserted filament, esp. when:
            // - starting a multi-material print
            // - singlematerial print's material type not matching the inserted filament
            return State::mmu_filament_inserted_wait_user;
        }
    } else
#endif
    {
        if (!config_store().fsensor_enabled.get()) {
            return stateFromFilamentType();
        }
        if (tools_mapping::is_tool_mapping_possible()) {
            return stateFromFilamentType(); // filament loaded/type checks are handled by the tools_mapping screen
        }

        // no MMU, do regular check of filament presence in each tool
        for (auto virtual_tool : VirtualToolIndex::all()) {
            if (check_extruder_need_filament_load_tools_mapping(virtual_tool)) {
                return State::filament_not_inserted_wait_user;
            }
        }
        return stateFromFilamentType();
    }
}

PrintPreview::Result PrintPreview::Loop() {
    if (GetState() == State::inactive) {
        return Result::Inactive;
    }

    const uint32_t curr_ms = ticks_ms();
    if (ticks_diff(curr_ms, last_run) < max_run_period_ms) {
        return stateToResult();
    }
    last_run = curr_ms;
    const Response response = GetResponse();

    auto &gcode_info = GCodeInfo::getInstance();

    switch (GetState()) {

    case State::inactive: // cannot be, but have it defined to enumerate all states
        return Result::Inactive;

    case State::init:
        gcode_info_scan::start_scan();

        // Reset print progress to 0. Need to be at this point because Connect is already starting to snitch the info.
        oProgressData.mInit();

        ChangeState(State::loading);
        if (skip_if_able > marlin_server::PreviewSkipIfAble::no) {
            // if skip print confirmation was requested, mark the print as started immediately.
            // If not, it will be started later when user clicks print
            return Result::MarkStarted;
        }
        break;

    case State::download_wait: {
        if (response == Response::Quit) {
            gcode_info_scan::cancel_scan();
            ChangeState(State::inactive);
            return Result::Abort;
        }

        if (gcode_info.has_error()) {
            ChangeState(State::file_error_wait_user);
            break;

        } else if (!gcode_info.can_be_printed()) {
            // Wait till we have downloaded enough
            break;
        }

        const auto prefetch_ready = marlin_server::media_prefetch.check_ready_to_start_print();
        if (prefetch_ready == MediaPrefetchManager::ReadyToStartPrintResult::needs_fetching) {
            // Make sure we have the prefetch buffer full before start the print
            // If we got into the "downloading" phase, do the prefetch checking here, because we're waiting for the file to download more
            marlin_server::media_prefetch.issue_fetch();
            break;

        } else if (prefetch_ready == MediaPrefetchManager::ReadyToStartPrintResult::error) {
            // This is a bit hacky way, but the error reporting is done through gcode_info, so we gotta put the error there.
            gcode_info.set_error(N_("The file is corrupt."));
            ChangeState(State::file_error_wait_user);
            break;
        }

        ChangeState(State::loading);
        break;
    }

    case State::loading: {
        if (gcode_info_scan::scan_start_result() == gcode_info_scan::ScanStartResult::not_started) {
            // Wait for the gcode scan to start
            break;
        }

        if (gcode_info.has_error()) {
            ChangeState(State::file_error_wait_user);
            break;

#if HAS_E2EE_SUPPORT()
        } else if (gcode_info.has_identity_info()) {
            ChangeState(State::untrusted_identity);
            break;
#endif
        } else if (!gcode_info.can_be_printed()) {
            // The file is not fully downloaded, wait till we have downloaded enough for printing
            ChangeState(State::download_wait);
            break;

        } else if (!gcode_info.is_loaded()) {
            // Wait for the gcode info to fully load
            break;
        }
        const auto prefetch_ready = marlin_server::media_prefetch.check_ready_to_start_print();
        if (prefetch_ready == MediaPrefetchManager::ReadyToStartPrintResult::needs_fetching) {
            // Make sure we have the prefetch buffer full before start the print
            // If we got into the "downloading" phase, do the prefetch checking here, because we're waiting for the file to download more
            marlin_server::media_prefetch.issue_fetch();
            break;

        } else if (prefetch_ready == MediaPrefetchManager::ReadyToStartPrintResult::error) {
            // This is a bit hacky way, but the error reporting is done through gcode_info, so we gotta put the error there.
            gcode_info.set_error(N_("The file is corrupt."));
            ChangeState(State::file_error_wait_user);
            break;
        }

#if HAS_SELFTEST()
        // We're ready to print now
        ChangeState((skip_if_able > marlin_server::PreviewSkipIfAble::no) ? stateFromSelftestCheck() : State::preview_wait_user);
#else
        // #error dead code found by automatic analyses (see BFW-5461)
        ChangeState(State::preview_wait_user);
#endif
        break;
    }

    case State::preview_wait_user:
        switch (response) {

        case Response::Continue: // no difference in state machine, some checks will not be run if tools_mapping is possible
        case Response::Print:
            ChangeState(stateFromSelftestCheck());
            if (skip_if_able == marlin_server::PreviewSkipIfAble::no) {
                // If print wasn't maked as started immediately, mark it now
                return Result::MarkStarted;
            }
            break;

        case Response::Back:
            ChangeState(State::inactive);
            return Result::Abort;

        default:
            break;
        }

        // Periodically kindly ask the prefetch thread to check if the file is still valid and update GCodeInfo::has_error
        if (ticks_diff(curr_ms, last_still_valid_check_ms) > 1000 && !still_valid_check_job.is_active()) {
            last_still_valid_check_ms = curr_ms;

            still_valid_check_job.issue([](AsyncJobExecutionControl &) {
                GCodeInfo::getInstance().check_still_valid();
            });
        }

        // Still check for file validity - file could be downloaded enough for the info to show,
        // but the transfer might fail and we want to prevent the user from start the print then
        if (gcode_info.has_error()) {
            ChangeState(State::file_error_wait_user);
            break;
        }
        break;

    case State::unfinished_selftest_wait_user:
        switch (response) {

        case Response::Ignore:
            ChangeState(stateFromUpdateCheck());
            break;

        case Response::Calibrate:
            marlin_server::request_calibrations_screen();
            // There is currently no way to keep the FSM open while GUI screen
            // such as `Calibrations & Tests` is being shown.
            // Let's just close the FSM and be done with it.
            ChangeState(State::inactive);
            return Result::Abort;

        case Response::_none:
            break;

        default:
            bsod_unreachable();
        }
        break;

    case State::new_firmware_available_wait_user:
        switch (response) {

        case Response::Continue:
            ChangeState(stateFromPrinterCheck());
            break;

        default:
            // TODO this should be handled more generally with a possibility to set timeout for specific state, but this should work for now and is MUCH easier
            if (ticks_ms() >= new_firmware_open_ms + new_firmware_timeout_ms) {
                ChangeState(stateFromPrinterCheck());
            }
            break;
        }
        break;

#if HAS_TOOL_MAPPING()
    case State::tools_mapping_wait_user:
        switch (response) {

        case Response::Abort:
            ChangeState(State::inactive);
            tools_mapping_cleanup();
            return Result::Abort;

        case Response::Print:
            tools_mapping_cleanup(true);
            ChangeState(State::done);
            break;

        default:
            break;
        }
        break;
#endif

    case State::gcode_invalid_wait_user:
    case State::gcode_invalid_wait_user_abort:
        switch (response) {

        case Response::Print:
            // Resume the check chain right after the printer check that sent us here.
#if HAS_WASTEBIN_FILL_TRACKING()
            ChangeState(stateFromWastebinCheck());
#else
            ChangeState(stateFromFilamentPresence());
#endif
            break;

        case Response::Abort:
            ChangeState(State::inactive);
            return Result::Abort;

        default:
            break;
        }
        break;

#if HAS_WASTEBIN_FILL_TRACKING()
    case State::wastebin_overfill_wait_user:
        switch (response) {

        case Response::Print:
            // Print anyway and keep mid-print monitoring on.
            ChangeState(stateFromFilamentPresence());
            break;

        case Response::Ignore:
            // Print anyway and stop monitoring for this print (e.g. remote user who will empty later).
            WastebinWatcher::instance().set_ignored_for_print(true);
            ChangeState(stateFromFilamentPresence());
            break;

        case Response::Empty:
            // Outside a print M1986 leaves the head parked; the start gcode homes anyway.
            marlin_server::enqueue_gcode("M1986");
            ChangeState(State::wastebin_emptying);
            break;

        case Response::_none:
            break;

        default:
            bsod_unreachable();
        }
        break;

    case State::wastebin_emptying:
        // M1986 runs as a gcode; wait for the gcode queue to drain before continuing.
        if (marlin_server::is_processing()) {
            break;
        }
        ChangeState(stateFromFilamentPresence());
        break;
#endif

    case State::filament_not_inserted_wait_user:
        switch (response) {

        case Response::FS_disable:
            FSensors_instance().set_enabled_global(false);
            ChangeState(State::checks_done);
            break;

        case Response::No:
            ChangeState(State::inactive);
            return Result::Abort;

        case Response::Yes:
            if (!queue_filament_gcodes(QueueFilamentGCodesMode::load)) {
                // The function changed the FSM state, reassert
                ChangeState(State::filament_not_inserted_wait_user);
                break;
            }
            ChangeState(State::filament_not_inserted_load);
            break;

        default:
            break;
        }
        break;

    case State::filament_not_inserted_load:
        if (!filament_gcodes::InProgress::Active()) {
            ChangeState(stateFromFilamentType());
        }
        break;

#if HAS_MMU2()
    case State::mmu_filament_inserted_wait_user:
        switch (response) {

        case Response::No:
            ChangeState(State::inactive);
            return Result::Inactive;

        case Response::Yes: {

            ChangeState(State::mmu_filament_inserted_unload);
            // If filament is really loaded, MMU knows exactly the physical slot index.
            // Silently assume, that the MMU is up and running -> therefore it's Set_Get_Selector_Slot register has already been read upon comm start
            const uint8_t physical_slot_index = MMU2::mmu2.SelectorSlot();
            // M702 translates given tool index through toolmapping -> need to hack around it (invert it).
            // The tool_mapper should either return the same index in case of a freshly booted printer (or no mapping changes)
            // or a valid mapping from the last print (which is also correct in this case).
            const uint8_t gcode_slot_index = tool_mapper.to_gcode(physical_slot_index);
            marlin_server::enqueue_gcode_printf("M702 W0 T%" PRIu8, gcode_slot_index); // unload, no return or cooldown
        } break;

        default:
            break;
        }
        break;

    case State::mmu_filament_inserted_unload:
        if (!filament_gcodes::InProgress::Active()) {
            ChangeState(State::checks_done);
        }
        break;
#endif

    case State::wrong_filament_wait_user: // change / ignore / abort
        switch (response) {

        case Response::Change:
            if (!queue_filament_gcodes(QueueFilamentGCodesMode::change)) {
                // The function changed the FSM state, reassert
                ChangeState(State::wrong_filament_wait_user);
                break;
            }
            ChangeState(State::wrong_filament_change);
            break;

        case Response::Ok:
            ChangeState(State::checks_done);
            break;

        case Response::Abort:
            ChangeState(State::inactive);
            return Result::Abort;

        default:
            break;
        }
        break;

    case State::wrong_filament_change:
        if (!filament_gcodes::InProgress::Active()) {
            PreheatStatus::Result res = PreheatStatus::ConsumeResult();
            if (res == PreheatStatus::Result::Aborted || res == PreheatStatus::Result::DidNotFinish) {
                ChangeState(State::wrong_filament_wait_user); // Return back to wrong filament type dialog
            } else {
                ChangeState(State::checks_done);
            }
        }
        break;

#if HAS_E2EE_SUPPORT()
    case State::untrusted_identity:
        if (response == Response::Abort) {
            ChangeState(State::inactive);
            return Result::Abort;
        } else if (response == Response::Yes) {
            const auto &info = gcode_info.get_identity_info();
            if (info.one_time) {
                e2ee::save_identity_key_temporary(info);
            } else {
                e2ee::save_identity_key(info);
            }
            ChangeState(State::preview_wait_user);
            break;
        } else if (response == Response::No) {
            e2ee::save_identity_key_temporary(gcode_info.get_identity_info());
            ChangeState(State::preview_wait_user);
            break;
        }
        break;
#endif

    case State::file_error_wait_user:
        // Only one possible response -> abort
        if (response == Response::Abort) {
            ChangeState(State::inactive);
            return Result::Abort;
        }
        break;

    case State::checks_done:
#if PRINTER_IS_PRUSA_iX()
        // We've removed reset_bounding_rect at the end of the print for the iX (in marlin_server.cpp::finalize_print).
        // So now, just to make sure, we reset the bounding rect at the start if we don't see it being set in the gcode.
        // BFW-5085
        if (!GCodeInfo::getInstance().get_bed_preheat_area().has_value()) {
            PrintArea::reset_bounding_rect();
        }
#endif

        if (tools_mapping::is_tool_mapping_possible()) {
#if HAS_SPOOL_JOIN() && HAS_TOOL_MAPPING()
            buddy::gcode_compatibility::CompatibilityReport compatibility;
            compatibility.generate_toolmapping_only({});

            // we can skip tools mapping if there is not warning/error in global tools mapping
            if ((skip_if_able >= marlin_server::PreviewSkipIfAble::tool_mapping) && compatibility.compatibility_level() == buddy::compatibility_checks::CompatibilityLevel::fully_compatible) {
                ChangeState(State::done);
            } else {
                ChangeState(State::tools_mapping_wait_user);
            }

            // start preheating bed to save time in absorbing heat
            if (GCodeInfo::getInstance().get_bed_preheat_temp().has_value()) {
                if (GCodeInfo::getInstance().get_bed_preheat_area().has_value()) {
                    PrintArea::set_bounding_rect(GCodeInfo::getInstance().get_bed_preheat_area().value());
                }

                marlin_server::set_target_bed(GCodeInfo::getInstance().get_bed_preheat_temp().value());
            }
            break;
#endif
        }
        // else go to print, checks are done
        ChangeState(State::done);
        break;
    case State::done:
        ChangeState(State::inactive);
        return Result::Print;
    }
    return stateToResult();
}
"""  # noqa: E501 (verbatim firmware lines)

FILES = {
    read.MODELS_FILE: MODELS_HPP,
    read.GCODE_INFO_FILE: GCODE_INFO_HPP,
    read.OPTIONS_FILE: OPTIONS_CMAKE,
    read.PRESETS_FILE: PRESETS_CPP,
    read.FILAMENT_FILE: FILAMENT_HPP,
    read.ALIASES_FILE: DATA_UTILS_CPP,
    read.TRACKER_FILE: TRACKER_HPP,
    read.CONFIGURATION_FILE: CONFIGURATION_H,
    read.ADVANCED_FILE: CONFIGURATION_ADV_H,
    read.RAMMING_CMAKE_FILE: RAMMING_CMAKE,
    **{read.configuration_file(n): CONFIGURATION_NEXTRUDER_H for n in ("MK4", "iX", "XL")},
    read.configuration_file("XL_DEV_KIT"): CONFIGURATION_NEXTRUDER_H,
    read.configuration_file("MK3.5"): CONFIGURATION_MK3_5_H,
    read.configuration_file("MINI"): CONFIGURATION_MINI_H,
    read.configuration_file("COREONE"): CONFIGURATION_CORE_ONE_H,
    read.configuration_file("COREONEL"): CONFIGURATION_CORE_ONE_H,
    **{read.advanced_file(n): ADVANCED_NEXTRUDER_H for n in ("MK4", "XL", "XL_DEV_KIT")},
    read.advanced_file("MK3.5"): ADVANCED_MK3_5_H,
    read.advanced_file("MINI"): ADVANCED_MINI_H,
    read.advanced_file("iX"): ADVANCED_IX_H,
    read.advanced_file("COREONE"): ADVANCED_CORE_ONE_H,
    read.advanced_file("COREONEL"): ADVANCED_CORE_ONE_H,
    read.ramming_file("nextruder"): RAMMING_NEXTRUDER_CPP,
    read.ramming_file("indx"): RAMMING_INDX_CPP,
    read.ramming_file("mini"): RAMMING_MINI_CPP,
    read.ramming_file("mk3_5"): RAMMING_MK3_5_CPP,
    read.CLEANER_FILE: NOZZLE_CLEANER_CPP,
    read.AUTO_RETRACT_FILE: AUTO_RETRACT_HPP,
    read.PAUSE_FILE: PAUSE_CPP,
    "src/common/power_panic.cpp": POWER_PANIC_CPP,
    "src/common/printer_model.cpp": PRINTER_MODEL_CPP,
    "src/feature/compatibility_checks/gcode_compatibility.cpp": GCODE_COMPATIBILITY_CPP,
    "src/marlin_stubs/pause/pause_settings.cpp": PAUSE_SETTINGS_CPP,
    "include/device/board.h": BOARD_H,
    "src/common/gcode/gcode_info.cpp": GCODE_INFO_CPP,
    "src/common/tools_mapping.cpp": TOOLS_MAPPING_CPP,
    "src/common/tool_index.cpp": TOOL_INDEX_CPP,
    "src/marlin_stubs/pause/M70X_mmu.cpp": M70X_MMU_CPP,
    "lib/Marlin/Marlin/src/module/planner.cpp": PLANNER_CPP,
    "lib/WUI/nhttp/status_renderer.cpp": STATUS_RENDERER_CPP,
    "lib/WUI/link_content/basic_gets.cpp": BASIC_GETS_CPP,
    read.MMU_CONFIG_FILE: MMU2_CONFIG_H,
    read.MMU_FILE: MMU2_MK4_CPP,
    read.MMU_HEADER_FILE: MMU2_MK4_H,
    "src/common/multi_filament_change.cpp": MULTI_FILAMENT_CHANGE_CPP,
    "src/gui/screen/screen_menu_mmu_preload_all.cpp": MMU_PRELOAD_ALL_CPP,
    "src/gui/screen_menu_filament_mmu.cpp": FILAMENT_MMU_CPP,
    "src/common/appmain.cpp": APPMAIN_CPP,
    "src/feature/filament_sensor/filament_sensors_handler.cpp": FILAMENT_SENSORS_HANDLER_CPP,
    "src/common/marlin_server.cpp": MARLIN_SERVER_CPP,
    "src/common/marlin_print_preview.cpp": MARLIN_PRINT_PREVIEW_CPP,
    "src/gui/MItem_mmu.cpp": MITEM_MMU_CPP,
    "src/feature/openprinttag/request_manager.cpp": REQUEST_MANAGER_CPP,
    "src/feature/openprinttag/tool_tag_buddy.cpp": TOOL_TAG_BUDDY_CPP,
    "src/gui/screen_menu_filament_mmu.hpp": SCREEN_MENU_FILAMENT_MMU_HPP,
    "src/marlin_stubs/pause/M600.cpp": M600_CPP,
    "src/gui/screen/screen_m600.cpp": SCREEN_M600_CPP,
    read.IMAGES_FILE: PRESETS_JSON,
    read.PAUSE_SETTINGS_FILE: PAUSE_SETTINGS_HPP,
}


class FakeSource:
    """A `Source` over in-memory files."""

    def __init__(self, files: dict[str, str] | None = None, commit: str = "c0ffee") -> None:
        self.files = FILES if files is None else files
        self.commit = commit

    def read(self, path: str) -> str:
        if path not in self.files:
            raise read.Unreadable(path, "no such file")
        return self.files[path]
