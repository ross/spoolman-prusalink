"""Small excerpts of the firmware files, in the shapes the readers expect."""

from firmware import read

MODELS_HPP = """\
inline constexpr std::array printer_model_info {
    PrinterModelInfo {
        .model = PrinterModel::mk3,
        .version = { 1, 3, 0 },
        .id_str = "MK3",
    },
    PrinterModelInfo {
        .model = PrinterModel::mk4,
        .version = { 1, 4, 0 },
        .help_url = "mk4",
        .id_str = "MK4",
    },
    PrinterModelInfo {
        .model = PrinterModel::mini,
        .version = { 2, 1, 0 },
        .id_str = "MINI",
    },
    PrinterModelInfo {
        .model = PrinterModel::coreone,
        .version = { 7, 1, 0 },
        .id_str = "COREONE",
    },
};

inline constexpr std::array printer_model_mmu_variant {
    PrinterModelMMUVariant {
        .model = PrinterModel::mk4,
        .id_str = "MK4MMU3",
    },
};
"""

OPTIONS_CMAKE = """\
set(PRINTER "<invalid>" CACHE STRING "Select the printer (valid values are ${PRINTER_VALID_OPTS}).")

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

FILES = {
    read.MODELS_FILE: MODELS_HPP,
    read.OPTIONS_FILE: OPTIONS_CMAKE,
    read.PRESETS_FILE: PRESETS_CPP,
    read.FILAMENT_FILE: FILAMENT_HPP,
    read.ALIASES_FILE: DATA_UTILS_CPP,
    read.TRACKER_FILE: TRACKER_HPP,
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
