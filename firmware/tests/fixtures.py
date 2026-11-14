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
    "src/common/printer_model.cpp": PRINTER_MODEL_CPP,
    "src/feature/compatibility_checks/gcode_compatibility.cpp": GCODE_COMPATIBILITY_CPP,
    "src/marlin_stubs/pause/pause_settings.cpp": PAUSE_SETTINGS_CPP,
    "include/device/board.h": BOARD_H,
    "src/common/gcode/gcode_info.cpp": GCODE_INFO_CPP,
    "src/common/tools_mapping.cpp": TOOLS_MAPPING_CPP,
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
