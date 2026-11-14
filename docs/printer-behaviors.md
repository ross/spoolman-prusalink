# Printer behaviors

What the printer reports through PrusaLink when something happens to it,
where each behavior comes from in the firmware, and how the simulator is
checked against the printer. This is the live-check list now: every scenario
in [scenarios.md](scenarios.md) is a composition of these behaviors and the
bridge's own logic, and only the behaviors need hardware. Once a behavior is
traced here, the scenarios that lean on it are end-to-end tests in
`integration/`, run on every CI turn against a simulator that is known to
match the printer.

The bridge sees a printer through four reads and one write: `GET
/api/v1/status`, `GET /api/v1/filament`, `GET /api/v1/job`, `GET
/api/version`, and `PUT /api/v1/files/{storage}/{name}`. A behavior here is
what those show.

## The firmware under test

The printer is a Core One L+ running the `prusalink-filament-endpoint`
branch of Ross's fork of Prusa-Firmware-Buddy, at commit `2db93db5`
(2026-11-03), checked out at `~/other/Prusa-Firmware-Buddy`. The branch
starts from the commit that set the version to 9.0.0, before any 9.0.0
change, so it behaves as 8.0.0. The `/api/v1/filament` endpoint and its
counters are the branch's addition; stock firmware has neither.

Paths and line numbers below are in that checkout at that commit. The probe
records `GET /api/version` and `GET /api/v1/info` in every trace header, so
a trace says which firmware and printer it saw. On 2026-11-08 the printer
reported firmware `9.0.0+17074.LOCAL`, printer type `8.1.0` (Core One L),
hostname `prusa-core-one-l`, a 0.4 mm nozzle and no MMU.

## Status values

- **Traced**: a trace recorded from the printer is under
  `simulator/tests/traces/`, and `simulator/tests/test_traces.py` replays the
  behavior in the simulator and checks it against that trace. The row names
  the trace and the date.
- **Seen live**: watched on the printer and recorded by eye in a scenario
  row or a log, before traces existed. The simulator may match it; nothing
  checks that until it's traced.
- **From source**: read from the firmware, not yet seen on the printer.
- **Other hardware**: a printer we don't have. The simulator copies the
  source.

## Behaviors

Each row says what the probe should see for tool 0. "Counted" means
`extruded` grows; a counter that doesn't move is "held". The replay in
`test_traces.py` has the same ID.

| ID | Behavior | What PrusaLink shows | Firmware | Simulator | Status |
| --- | --- | --- | --- | --- | --- |
| F1 | Reboot or power cycle, idle, filament loaded | Unreachable, then `IDLE` with a new `boot_id`; `extruded`, `unload_count` and `extruded_at_unload` at 0; the same material. | `boot_id` is a random number drawn once at boot (`src/feature/filament_tracker/filament_tracker.cpp:10-11`). The counters are RAM, zero at start (`filament_tracker.hpp:147-164, 187-196`). The type and its parameters persist in the config store (`src/configuration_store/store_definition.hpp:498-508, 530-548`). | `boot()` | Seen live (C1). Trace `F1-reboot` to record. |
| F2 | Unload from the menu | `unload_count` up by one, `extruded_at_unload` equal to `extruded`, `material` gone. `extruded` unchanged after an idle load's auto-retract; otherwise the ramming counts first, about 23 mm on a Core One L (F26). `BUSY` or `IDLE` while it runs: the trace will say. | One hook counts every unload: setting the type to none while a type is set (`store_definition.cpp:258-262`, `filament_tracker.cpp:69-76`). The menu, `M702`, `M600` and a runout all reach it through `unload_process` (`src/common/pause.cpp:444-453, 1278`). | `unload()` | Seen live (S1, S2). Trace `F2-unload`. |
| F3 | Load from the menu with a preset | `extruded` grows with no `material` (the feed past the gap, about 20 mm of the 60 mm fed), then `material` appears with a name and the preset's temperatures, and no color or UUID, then `extruded` grows about 40 mm more (the purge), then about 23 mm more (the auto-retract, F26). | Feed 40 mm slow and 20 mm fast (`include/marlin/Configuration_COREONEL_adv.h:626-627`); the first 40 mm refill the retraction gap and aren't counted (`filament_tracker.cpp:22-27, 41-48`). The type is set at the start of the purge (`pause.cpp:793-802`), which is 40 mm (`adv.h:633`), then 8 mm retract into the gap (`pause.cpp:831-832`) and the auto-retract (`load_finalize_process` in `pause.cpp`). A preset load points at the preset and reports no UUID. | `load(source="preset")` | Seen live (S2 held load usage; Q1 UUID drop). Trace `F3-load-preset`. |
| F4 | Load from a spool with a tag | As F3, with `material` carrying name, color, `instance_uuid` and the tag's temperatures. | The tag sets the name, hash, UUIDs and color (`src/common/data_utils.cpp:238-265`). The nozzle and bed temperatures are the middle of the tag's min and max, and the preheat is the tag's own (`src/feature/openprinttag/data_utils.cpp:267-301`). UUIDs are kept only on Core One and Core One L, tool 0 (`ProjectOptions.cmake:814`, `store_definition.hpp:547-549`). | `load(source="tag")` | Seen live (S1). Trace `F4-load-tag`. |
| F5 | A print from start to finish, then dismissed | `job` with a new ID and `PRINTING` while the file is scanned; a brief `IDLE` with no `job`; `PRINTING` with `extruded` held (preheat); `PRINTING` counted; `PRINTING` while parking; `FINISHED` with no `job`, until dismissed to `IDLE`. | The ID is reserved and incremented at the preview (`src/common/marlin_print_preview.cpp:420-424`, `marlin_server.cpp:2309-2311`), persisted and never reused (`:1393`). The scan reads `PRINTING` (`lib/WUI/.../printer_state.cpp:395`), `PrintInit` reads `IDLE` with no job (`:379, 611-622`), finishing reads `PRINTING` with the job (`:387-394`), `FINISHED` persists until exit (`marlin_server.cpp:1684-1690`). A new print can start from `FINISHED` without dismissing (`:1530-1535`). | `start_print()`, `dismiss()` | Seen live (P1); the blip from source. Trace `F5-print`. |
| F6 | Pause and resume, no filament change | `PAUSED` with `extruded` held, then `PRINTING`; no net movement across the pause. | The pause retracts 8 mm and the resume feeds the same 8 mm back, which refills the gap; no purge (`marlin_server.cpp:2550-2552, 3348-3349, 3465-3467, 2621-2690`). | `pause()`, `resume()` | From source (P11 has no test yet). Trace `F6-pause-resume`. |
| F7 | Cancel partway | `PRINTING` while parking, then `STOPPED` with no `job`. | `Aborting_*` reads `PRINTING`, `Aborted` reads `STOPPED` and the job object goes (`printer_state.cpp:387-394, 425-436, 611-622`). | `cancel()` | Seen live (P7). Trace `F7-cancel`. |
| F8 | Filament change during a print: Tune > Change filament, a runout, or a jam with an unload | `unload_count` up by one at the unload; `ATTENTION` while the screen waits, `PAUSED` once handled; the reload brings back the same `material` as before (name, color, UUID); the purge is counted; then `PRINTING`. | A runout injects `M600` (`src/common/filament_sensors_handler.cpp:237`), so every change is the `M600` path: unload, count, reload (`pause.cpp:1291, 1363-1365`). The reload takes the previous type (`src/marlin_stubs/M600.cpp:284-285`, `filament_tools.cpp:39-42`). | `runout()`, `jam()`, `load()` with no source | Seen live (P2 runout, P6 jam). Trace `F8-change-filament` from the Tune menu. |
| F9 | Planned color change, `M600` in the G-code | As F8, at the point the file asks for it. | Same path as F8. | `PrintSpec(color_changes_mm=...)` | From source (P5 end to end). Trace `F9-m600`. |
| F10 | The bridge's sync job | Upload accepted when `IDLE`, `FINISHED` or `STOPPED` (409 "Can't print right now" otherwise, with the file kept); a `job` with its own ID and the file's name in `/api/v1/job`; `PRINTING` with the F5 blip; `M865 R` clears the slot including UUIDs, then sets name, color and temperatures; `M865.1` sets the UUID; `FINISHED` with no `job`. `extruded` held throughout. | Accepted states (`printer_state.cpp:592-608`); 409 after the file is saved (`lib/WUI/nhttp/gcode_upload.cpp:286-295`). `M865 R` resets every parameter (`src/marlin_stubs/M865.cpp:83-85`); parameters on a preset are ignored but `L` still loads it, and `N` on a preset is an error (`:133-137, 189-191`). `M865.1` is built for Core One and Core One L only (`:145-171`). Names are 1 to 7 of `a-zA-Z0-9_-` (`filament_common.cpp:27-42`). | `upload()`, `gcode.py` and `sync.py` | Seen live (W1, W6, W13, W14). Traced as `F10b-sync-job-rewrite`, retaken 2026-11-09 on firmware 9.0.0+17075 with temperatures: the job written by hand with the loaded spool's own values, from the FINISHED screen; `PRINTING` for 16 s with no `IDLE` blip at 1 s polls, no material event (temperatures included), `FINISHED` with no `job`. Traced 2026-11-09 as `F10c-sync-job-rewrite-idle`, the same job from `IDLE` after a boot with Z not homed: the same steps, but `PRINTING` for 105 s, most of it `G28 O` homing. The write that changes the slot is still to trace as `F10-sync-job` in sitting A. |
| F11 | Manual moves from the menu: Move E, retract, Purge filament | Move E forward is counted once past the gap; a retract never lowers `extruded`; Purge filament is counted and leaves `material` as it was, with no unload. | Move E goes through the planner (`src/gui/screen_menu_move.cpp:201-212`), where every positive move past the gap counts and the gap is clamped at 40 mm (`filament_tracker.cpp:22-27`). Purge filament is `M701 L0 W2` (`MItem_filament.cpp:168`, `M701_2.cpp:123`): a purge that re-sets the chosen type without an unload. | `extrude()`, `retract()` | From source (C6 has no test). Trace `F11-manual-extrude`. |
| F12 | Power cut during a print, bed still warm | Unreachable; then a new `boot_id`, counters at 0, the same `job` ID; `BUSY`, during which about 25 mm is counted (the nozzle clean); briefly `PAUSED`; then `PRINTING`. The trace settles where the 25 mm shows. | No new ID: the resume skips the preview (`marlin_server.cpp:2081-2094`) and loads the saved ID (`:1393`). Warm bed resumes on its own (`src/common/power_panic.cpp:294-315`): `PowerPanic_Resume` reads `BUSY`, the unpark reads `PAUSED` (`marlin_server.cpp:2105`, `printer_state.cpp:399-424`). XY are homed, Z restored (`power_panic.cpp:400, 450-456`). Core One and Core One L run `G12 S21`, which includes `G750 E25` through the planner (`power_panic.cpp:484-488`, `nozzle_cleaner.cpp:96-106`, `G750.cpp:40`). The counters aren't in the power-panic data. Resume needs the USB drive present and not the reset button (`print_utils.cpp:45-50`). | `set_power("on", resume=True)` | From source (P12). Trace `F12-power-panic`. |
| F13 | Power cut during a print, bed gone cold | As F12, but `ATTENTION` after the first `BUSY` until the user taps resume; the nozzle clean comes after that. The threshold and whether the job's `state` in `/api/v1/job` reads `PAUSED` throughout are for the trace. | `PowerPanic_AwaitingResume` with the `HeatbedColdAfterPP` warning (`marlin_server.cpp:2089-2094, 2053-2055`) reads `ATTENTION` (`printer_state.cpp:343`). A print that was paused when power failed comes back `PAUSED` (`marlin_server.cpp:2108-2116`). | `set_power("on", resume=True, cold_bed=True)` | From source. Trace `F13-power-panic-cold`, optional. |
| F14 | Idle sensor clear | Outside a print, when the tool's sensor sees no filament while a type is stored: `material` goes and `unload_count` goes up by one. At boot, `extruded_at_unload` is 0. | Runs on every sensor step when not printing and the sensor surely sees nothing (`filament_sensors_handler.cpp:303-312`, `marlin_server.cpp:959`). No multi-tool guard: it applies to tool 0 of a single-tool printer. Skipped while a load or unload dialog is open, while printing, paused or resuming from a power panic (`marlin_server.cpp:1448-1460`), and when the sensor is disabled or uncalibrated (`filament_sensors_handler.hpp:88-89`). | `_idle_sensor_clear()` | From source. Not traced: it needs filament pulled from a cold extruder. See the note below. |
| F15 | Upload while a print is starting | During the scan (`PRINTING`): 409. During the `IDLE` blip: 2xx, and nothing happens; the user's print goes on and the file never runs. While the preview screen is open before a user confirms a print (`IDLE`, no `job`): the upload replaces the pending start. | The state check passes in the blip but `print_start` ignores it from `PrintInit` (`marlin_server.cpp:1527-1550`); in the preview a new request replaces the pending one (`:1538-1539`). | The blip drop. The preview's screen before a user presses Print isn't modeled; its checks of an upload (F27) and of a print's tools (F28) are. | From source. Not traced on purpose: see the bridge follow-ups. |
| F16 | Other unload paths | Stuck filament counts an unload only if the user picks Unload. Pulling filament out during a load counts only after the type has been set (the purge). The color-wrong retry or eject doesn't count. Clearing a type that's already hidden counts nothing. | `pause.cpp:1213-1223, 1881-1888, 994-1012`; `store_definition.cpp:258-272`. Also counted: cold pull (`M1702.cpp:394`), XL multi-change (`multi_filament_change.cpp:199-205`), `M865 L` with a type that resolves to none (`M865.cpp:194-195`). | Not modeled beyond F2 and F8. | From source. Check when they happen. |
| F17 | Stock firmware, MINI, MK3.5 | 404 on `/api/v1/filament`, or 200 with `material` and no counters. | Stock firmware has no endpoint. | `firmware` mode | Other hardware. |
| F18 | API faults | 401 without the key; 500 on an internal error; connection refused while off; a hang on a stuck network. | nhttp. | `set_fault()`, `set_power()` | Seen live (401 and off, C1); the rest from source. |
| F24 | The filament type across an unload | After an unload while idle, the next load sets a fresh type: a menu preset with no `material_uuid`, `package_uuid` or `tag` object, or a tag read with the tag's values and markers. Only a filament change during a print (a runout, a jam, or Tune > Change filament) reloads the type it had before the unload, tag markers included. | M600 reloads the tool's current type, or `for_tool_heuristic`'s guess, which falls back to the previously loaded type (`src/marlin_stubs/pause/M600.cpp:282-290`, `src/common/filament_tools.cpp:15-45`). An idle load takes the type from a preheat pick or a tag read (`M701_load`, `src/marlin_stubs/pause/M701_2.cpp:66-98`). An idle Change filament unloads, then asks for a new type (`M1600_change_filament`, `M701_2.cpp:240-323`). A menu unload and the idle sensor clear (F14) set the type to none. | `load()`: a load needs a source outside a runout reload | From source. The bridge relies on it: a runout leftover needs an unload during a print (Q11 in scenarios.md). Sitting A, steps 6 and 7; sitting B, step 2. |
| F25 | A color written to the type | `color` reads back with each channel cut to 5 bits and widened again: `O"#73767d"` reads `#73737b`. A color that is already 5-bit, such as `#73737b`, reads back as written. | `M865` keeps the full color (`src/marlin_stubs/M865.cpp:119-120`), and the rounding happens when the type is stored: an ad-hoc type's color is kept as `Color555` (`src/common/filament.cpp:341-343, 392`, `filament_eeprom.hpp:99-109`). `from_rgb` keeps each channel's top 5 bits and `to_color` widens them as `(v << 3) \| (v >> 2)` (`src/common/utils/color555.hpp:23-54`). The picked color of a menu load (`filament_to_load.cpp:13-29`) and a tag's color are stored the same way. Only an ad-hoc type has a color. `/api/v1/filament` reports what is stored (`lib/WUI/nhttp/filament_renderer.cpp:46-48`). | `SimTool.store()` | From source. `tests/test_color_drift.py` pins the pair above as a printer readback. |
| F26 | Auto-retract and ramming | After an idle load and at a print's end, `extruded` grows about 23 mm on a Core One L and nothing more until the next forward move, which counts about 15 mm more than it feeds. A menu unload right after that counts nothing before the unload; one after a manual feed counts about 23 mm first. A runout during a print counts about 7 mm before the unload. A reboot keeps the stored distance, so the same holds after one. | The auto-retract runs the `auto_retract` ramming sequence and stores how far it left the filament retracted, 55 mm on a Nextruder (`src/feature/auto_retract/auto_retract.cpp`, `src/feature/ramming/ramming_sequence.cpp`). A sequence that starts with a feed first feeds back what's retracted (`fully_deretract` in `src/common/mapi/motion.cpp`), so the tracker's gap is 0 when it starts; its feeds past the gap count 23 mm. It's skipped when already retracted 20 mm or more, for flexible filament, and when the user turns it off. Every forward move feeds the stored distance back first (`planner.cpp`). It follows an idle load (`load_finalize_process` in `src/marlin_stubs/pause/pause.cpp`) and the end of every print, the sync job included (`pre_finalize_print` in `src/common/marlin_server.cpp`). An unload rams with the `unload` sequence unless already auto-retracted, a runout with the `runout` one (`ram_sequence_process` in `pause.cpp`). At boot, a stored distance sets the tracker's gap; with none stored, the gap starts at 0 and reports no retracted distance until a feed reaches the nozzle (`marlin_server.cpp`). Setting the type to none forgets the stored distance (`store_definition.cpp`). INDX stores 8 mm, skips the print's end, and unloads with a 2 mm purge and no cold path. | `_auto_retract_steps()`, `_unload_steps()`, `boot()`, `SimPrinter.auto_retract` | From source. The F10b and F10c traces start auto-retracted. |
| F27 | The print preview's checks of an uploaded file | The upload answers 201, and the `job` with its new ID reads `PRINTING` during the scan. When a check fails under a severity that shows, `ATTENTION` with the same `job` in `/api/v1/status`, and `/api/v1/job` 200 with `PAUSED`, until the question is answered; nothing times out. Print: the F5 blip, then the job runs. Abort: `IDLE` with no `job`, the ID used up, the file kept, nothing parked or moved. A stop during the scan or the start blip ends the same way. Another upload meanwhile: 409, the file kept. | The ID is taken before the scan (`init` in `src/common/marlin_print_preview.cpp:414-426`, `marlin_server.cpp:2300-2316`). The checks read the first 200 G-code lines, not counting comments and `M486` (`src/common/gcode/gcode_info.cpp:134-144, 432-478`, `gcode_info.hpp:76-79`): `M862.3` by compatibility group, an unquoted name failing; `M862.5` above level 2; `M862.6` matched ignoring case, an unknown or unquoted name failing (`gcode_info.cpp:260-380`, `src/common/gcode/gcode_buffer.cpp:18-34`). Input shaper is required on every build but the iX, and the INDX lock on INDX as a fixed warning (`src/feature/compatibility_checks/gcode_compatibility.cpp:48-153, 368-376`). Each kind has a menu setting, ignore, warn (the default) or abort (`include/common/hw_check.hpp:12-29`), and the worst failed check picks a screen with Abort and Print or Abort alone (`marlin_print_preview.cpp:921-973`, `compatibility_checks_common.cpp:13-27`, `print_preview_phases.hpp:62-63`). The question reads `ATTENTION` with the job (`src/state/printer_state.cpp:228-234, 611-623`). Print goes to `PrintInit` (`marlin_server.cpp:2343-2358`); Abort runs `cleanup_print` to `Idle` (`:2326-2331, 1113-1137`). A stop in the scan or `PrintInit` goes to `Aborting_Preview` and the same `cleanup_print`, with no `pre_finalize_print` (`:1647-1681, 2828-2841`). An `M862` line with no `P` sets no check (`gcode_info.cpp:299-377`). | `SimPrinter.checks`, `preview.py`, `tap_through()`, `cancel()` | From source. Trace `F27-preview` to record: upload a file sliced for another model. |
| F28 | The print preview's checks of a print's tools | A print started at the printer reads `IDLE` with no `job` while its preview is open; Print takes the new ID, and the job reads `PRINTING`. Each question then reads `ATTENTION` with the `job` and `/api/v1/job` `PAUSED`. Without tool mapping (single-tool printers, the MMU off): a screen with more used tools than enabled ones and a used tool with no filament as warnings, and a disabled used tool as Abort only; then, for an empty used tool, Yes loads it, which reads `BUSY` with no `job` and doesn't auto-retract or prime; then a used tool holding another type than the file names asks Change, Ok or Abort. On the XL and INDX the per-tool rows, the type included, show on the tools-mapping screen (Abort or Print), which a print started at the printer always shows when the print uses two tools or two are enabled. No, Abort: `IDLE` with no `job`, the ID used up. No setting governs these rows. | The ID is taken when Print is pressed on the preview (`src/common/marlin_print_preview.cpp:510-528`, `marlin_server.cpp:2300-2316`). A tool is used when the file's `filament used [g]` is above 0.0003 (`src/common/gcode/gcode_info.hpp:95-99`). The rows and their fixed severities: `not_enough_tools`, `correct_tool`, `filament_loaded`, `filament_type` (`src/feature/compatibility_checks/gcode_compatibility.cpp:133-210, 388-513`); the type compares the file's `filament_type` entry exactly with the loaded type's name, `---` for none, and skips an empty or `---` entry. Without tool mapping the first screen leaves the type out (`marlin_print_preview.cpp:921-973`), the presence check follows (`:228-275`), Yes queues `M701 S"<type>" T<n> W2` (`:285-359, 666-696`), and the type check follows the load (`:361-374, 692-696`). The load's screen reads `BUSY` with no `Printing` screen (`src/state/printer_state.cpp:238-247, 611-623`), and `is_printing()` is true, so the load doesn't auto-retract (`src/marlin_stubs/pause/pause.cpp:1029-1070`). Tool mapping is on toolchanger builds and MMU builds with the MMU on (`ProjectOptions.cmake:534-536, 592, 863-869`), when the print uses two tools or two are enabled (`src/common/tools_mapping.cpp:26-36`); a start at the screen always shows it (`marlin_print_preview.cpp:797-832`). | `Segment.material`, `preview.py`, `tap_through()`, `cancel()`, `load()` | From source. The simulator asks the tools-mapping question only when a used tool has a problem. |

### Multi-tool printers

INDX and XL are several toolheads, each with its own filament path, sensor
and counters. None is at hand: these rows are from the source and the plan
(`plans.md`, "Multi-tool"), and get traced when the Core One L+ becomes an
INDX. They're tracked here all the same, because the bridge's accounting
and state already handle several tools per printer on the strength of
these readings, and the integration tests for them shouldn't wait for the
hardware. When the INDX arrives, every row below is a trace to record, and
the single-tool rows above are recorded again on it: a tool change in a
print (F20) and the per-tool idle clear (F23) are where the firmware's
behavior is least covered by what the Core One L+ can show.

| ID | Behavior | What PrusaLink shows | Firmware | Simulator | Status |
| --- | --- | --- | --- | --- | --- |
| F19 | Several tools | One `tools` entry per enabled tool, in index order, with gaps for tools whose dock isn't calibrated. Each has its own `extruded`, `unload_count`, `extruded_at_unload` and `material`. Nothing else in PrusaLink says how many tools there are or which is selected. | `filament_renderer.cpp`; the tracker is per tool. | `SimTool`, `set_tool_enabled()` | Other hardware. |
| F20 | Tool change during a print | Not an unload: `unload_count` holds on both tools and the state stays `PRINTING`. Ramming or retract before `T<n>` counts on the old tool (a retract only grows its gap); the purge after pickup counts on the new one. | Planner-time counting per active tool (`planner.cpp:2206-2219`); INDX service moves excluded (`:2194`). | `Segment(tool, mm)`, `toolchange_purge_mm` | Other hardware. |
| F21 | No tags | `material` has a name and maybe a color, never `instance_uuid`. `M865.1` does nothing. | `M865.1` is built with `HAS_ANFC`, Core One and Core One L only (`ProjectOptions.cmake:814`). | `ModelInfo.uuids` | Other hardware. The bridge doesn't write `M865.1` there: it writes UUIDs only on models that keep them. |
| F22 | `M865 L<n>` on another tool | Sets tool `n`'s type with no motion. `M865` with an empty tool's type copied into `n` counts as an unload of `n`. | `M865.cpp:194-195`. | `sync.py` honors `L` | Other hardware. |
| F23 | Idle sensor clear on a picked tool | Picking a tool whose sensor sees no filament while it has a type counts an unload on it (F14 per tool). | `filament_sensors_handler.cpp:303-312`. | `select_tool()` | Other hardware. |

### Notes from the source

- **F14 changes a premise.** Q2 and X6 in scenarios.md assume an unload can
  be missed across a reboot because `unload_count` restarts. The firmware
  counts one at boot when the sensor sees no filament and a type is stored,
  so that case is covered. A missed unload still happens when the sensor is
  disabled, when a different spool was pushed in while the printer was off
  (the sensor sees filament, so the type stays), or when the reboot is a
  power-panic resume. The bridge's handling stays, with a narrower reason.
- **F3 and F11 share the gap.** The retraction gap is clamped at 40 mm, so a
  retract longer than that followed by a feed over-counts by the excess.
  Only a manual move can do it.
- **F12 costs the usage since the last poll**, up to one poll interval, and
  then counts the 25 mm nozzle clean to whichever spool is active after the
  boot. The material is restored from the config store, so it's charged, not
  held. The simulator counts the purge halfway through its `BUSY` phase;
  where it falls in the firmware's resume sequence, and whether the
  firmware's `time_printing` grows during the recovery, the trace will say.
- **F10's `M865 R`** wipes the UUID too, so a sync job that doesn't carry
  `M865.1` leaves the tool with no UUID. The bridge always writes both.
- **F10 ends F4's tag tracking.** `M865 X` edits the pending ad-hoc slot
  (`M865.cpp:67-68`). `R` resets its parameters to the defaults, which
  clears the tag hash and the material and package UUIDs (`M865.cpp:83-85`,
  `filament.cpp:41-42`). `L0` saves that to the tool
  (`store_definition.cpp:243-256`, `filament.cpp:394`). The printer's usage
  tracker then stops reading and writing the tag, and usage not yet flushed
  to it is lost (`filament_usage_tracker.cpp:127-139`). A reboot doesn't
  restore it, since the stored hash stays cleared
  (`tool_tag_buddy.cpp:52-54`). Neither does a runout reload, which keeps
  the tool's current type (`M600.cpp:282-290`). Only a load from the tag
  does. The `tag` object leaves `/api/v1/filament`
  (`filament_renderer.cpp:66-67`), and so do the material and package
  UUIDs, which it renders from the reset parameters
  (`filament_renderer.cpp:59-60`). G-code can't set the hash
  (`M865.cpp:183`). So the bridge never writes a tool loaded from a tag
  (Q11 in scenarios.md). From source at `ff61540d4`, not yet seen on the
  printer.

## Recording a trace

The probe is `simulator/probe.py`. It only reads, so the production bridge
can keep running. Every rule in [live-checks.md](live-checks.md) under
"Rules for anything live" applies: current `main` on whale, one bridge,
cheap filament.

1. **Start the probe** with the row's trace name:

   ```
   PRUSALINK_API_KEY=... python -m simulator.probe --url http://<printer> \
       --key-env PRUSALINK_API_KEY \
       --out simulator/tests/traces/F2-unload.jsonl --note "menu unload, spool 20"
   ```

   It prints one line per sample it keeps. Start it while the printer is
   idle, or before a print starts: the first sample has nothing to be
   compared with, so a trace that begins in the middle of something can't
   show how it got there. A trace may begin later than the replay does, as
   long as it begins at rest, for longer than the polling interval: the
   replay tests assume it. A trace is reduced to steps, each either an
   event (a new job, a new boot, a material change, temperatures included,
   or an unload, always one sample) or a run of samples with the same state, job presence and kind
   of material. The comparison requires that every step the printer showed
   appears in the simulator's steps, in order. The simulator may show runs
   the probe missed, but no event the printer didn't, and none after the
   printer's last step. One poll can see several changes at once, such as an
   unload and a material change, so a live event may stand for up to four
   consecutive simulator events, with runs between them that together last
   less than one poll; longer runs never combine. A run the printer showed
   may also pass over a simulator event whose poll the probe missed, such
   as a job's first sighting before the start blip, as long as a later live
   event accounts for it. Whether `extruded` moved
   is checked separately for each step, over the simulator steps from where
   it matched to where the next one did: the simulator states the probe
   skipped belong to the step before them. Each of the printer's polls is
   placed at a simulator sample in that stretch, in order, so that the
   movement between two consecutive polls is exactly what the simulator
   shows between their places: a poll that saw no movement means the
   simulator was still there, and a poll that saw movement means the
   simulator moved the same way somewhere in between. An event's poll can
   come up to a poll after the event and carry movement from what follows,
   so it may sit anywhere up to the end of the next step's stretch. A poll
   that brought no counter (the printer unreachable, or the filament read
   failed) takes no place of its own: the next poll that saw the counter is
   measured from the last that did. Before a reboot the counters restart,
   so there the blind poll accepts anything, which is how movement in the
   last poll before a power cut is lost; without time, movement earlier in
   a still run before the cut can hide the same way, a known limit. The
   simulator may not move before the first poll or after the last, since a
   recording starts and ends at rest. Durations enter in one place:
   simulator states the probe never sampled must together fit within one
   poll, or the printer would have shown them. Otherwise a replay may run
   its states faster or slower than the printer did, as long as each
   stretch has at least as many simulator samples (ten a second) as the
   trace kept polls there: the probe keeps a still sample every heartbeat,
   so a long wait on the printer needs a few seconds in the replay.
2. **Mark each step** by typing what you're about to do and pressing Enter,
   then do it at the printer: `unload from the menu`, `pulled the plug`.
   Marks carry their time and show up in the review. Stop the probe with
   Ctrl-C, or `kill` it when it runs in the background, where a shell
   ignores Ctrl-C for it; either way the last poll is written. Don't edit
   a trace while the probe still writes it.
3. **Let it settle.** When the printer is back to `IDLE` (or `FINISHED`,
   for a print) and a heartbeat has passed, stop the probe with Ctrl-C.
4. **Review the steps:**

   ```
   python -m simulator.trace simulator/tests/traces/F2-unload.jsonl
   ```

   Each step is the state, whether a job is present and new, a new boot, the
   kind of material reported (`none`, `name`, `color`, `tagged`, with `!` when
   it changed), unloads counted, and whether `extruded` moved. Compare it
   with the row's "What PrusaLink shows". If the printer did something the
   row doesn't say, that's a finding: update the row, and open an issue if
   the bridge should care.
5. **Replay it:**

   ```
   pytest simulator/tests/test_traces.py -k f2
   ```

   Passing means the simulator reproduces every step the printer showed and
   adds no event of its own. A failure prints both step lists. The printer
   is right: when the trace and a reading of the firmware source disagree,
   the simulator follows the trace. The source is then re-read to explain
   the difference, but the two may never agree: the printer may run a
   different firmware version from the one read, or a setting or a detail
   of the model may change the path taken. The trace's header records what
   the printer said it was (`firmware`, and the `/api/version` and
   `/api/v1/info` bodies: model, hostname, serial, nozzle), so a
   disagreement can be tied to a printer and a version. So the fix is
   usually in `simulator/model.py`, with the row updated to say what the
   printer did; sometimes it's the replay's choreography in the test.
6. **Record it.** Set the row's status to Traced with the date, and commit
   the trace with the model or test change. If the printer did anything
   the row's firmware column didn't predict, add a line to
   [Disagreements](#disagreements-between-the-source-and-the-printer) with
   the firmware version from the trace's header, even when the difference
   turned out to be harmless or unexplained.

### Disagreements between the source and the printer

Each line is a place where a reading of the firmware source said one thing
and a trace showed another. They're worth revisiting: a wrong reading may
hide a second one nearby, a firmware update may change the behavior, and
the bridge may be leaning on the reading somewhere else. Keep the line
after the simulator is fixed. "Why" is the explanation found in the
source, a version or setting mismatch, or "unexplained".

| Date | Row | Firmware | The source said | The printer showed | Why | What changed |
| --- | --- | --- | --- | --- | --- | --- |
| | | | | | | |

Redo a spoiled trace with `--force`. Use `--interval 0.5` for the power
cuts, where the states move fast once the printer is back. Each trace is a
few kilobytes: the probe writes a sample only when something the bridge
could notice changes, and a heartbeat every 10 s otherwise.

## Capture plan

Three sittings, cheap filament, and the production bridge running throughout
so F10 happens on its own. Rows already Seen live are recorded again,
because a trace is what the simulator is checked against and the old
observations are notes.

**Sitting A, between prints.** About 30 minutes and a few grams of purge.

1. **F1.** Filament loaded, idle. Start the probe, switch the printer off
   at the switch, wait ten seconds, switch it on, wait for the home screen
   and two heartbeats.
2. **F2.** Unload from the menu.
3. **F3.** Load a preset from the menu, without a tag. The bridge will then
   write the assigned spool (W11 or W1) once idle: that is F10, so keep the
   probe running under the F10 name after F3 settles, or start it again
   before editing the loaded spool's nozzle temperature in Spoolman and let
   the bridge's write come.
4. **F11.** With filament loaded: Move E forward 20 mm, retract 10 mm, Move E
   forward 20 mm, then Purge filament.
5. **F4.** If a tagged spool is at hand: unload, then load it from its tag.
6. **F24, a menu load.** With the tagged spool loaded from its tag, unload
   from the menu, then load a preset from the menu. Check that
   `/api/v1/filament` shows the preset with no `material_uuid`,
   `package_uuid` or `tag` object.
7. **F24, a tag load.** Unload from the menu again, then load from the tag.
   Check that the tag's name, color, temperatures, UUIDs and `tag` object
   come back fresh.

**Sitting B, prints.** Three short prints of about ten minutes.

1. **F5.** A clean print, probe running from before the start until after
   you dismiss `FINISHED`. Nothing else happens during it.
2. During the second print, one row at a time, starting and stopping the
   probe for each: **F6** pause, wait half a minute, resume; **F8** Tune >
   Change filament, reload the same spool, resume; **F7** cancel. Do F8
   with the tagged spool loaded from its tag: for **F24**, check that the
   reload keeps the type it had before, `material_uuid`, `package_uuid`
   and `instance_uuid` included.
3. **F9.** A print sliced with one color change (or the Tune change twice,
   if slicing is a bother). Reload the same spool at the change.

**Sitting C, power cuts.** One print, well past its first layer.

1. **F12.** Probe at `--interval 0.5`. Pull the plug, count ten seconds,
   plug it back in. The USB drive must stay in. Expect the printer to resume
   on its own; let the print run a minute more.
2. **F13.** Optional. Pull the plug and leave it for several minutes, until
   the bed is cold, then plug it in. Expect the printer to ask before it
   resumes.

**When they happen.** F14 (pull filament from a cold extruder while off,
then boot), F16 (a stuck-filament dialog, a pull during a load), the next
firmware update (F1 after it), a real runout and a jam (F8). Run the probe if
there's time.

## Bridge follow-ups found here

Things the source says that the bridge or its tests should take up. None is
a live check.

1. **F15, the preview. Accepted (2026-11-08).** A user who picks a file on
   the screen sits in the preview, which reads `IDLE` with no `job`. A sync
   job uploaded then replaces the user's pending print, and the user starts
   it again once the sync job has finished. `sync_delay` narrows the window
   and nothing closes it: the API doesn't show the preview. Listed under
   "Out of scope" in scenarios.md. The simulator doesn't model the preview.
2. **F15, the blip.** An upload during the blip succeeds and runs nothing.
   The bridge then expects the job by name (W6) and never sees it. W5's
   "didn't take" catches it after a second attempt. Add an end-to-end test
   for it; the simulator drops the upload now.
3. **F14.** Narrow Q2's reason in scenarios.md to the three cases above.
4. **F12.** The nozzle clean's 25 mm is charged to the active spool after
   the boot; the usage between the last poll and the cut is lost. Both are
   fine. Say so in P12 once traced.

## From here

With the behaviors traced, the integration suite becomes the record and
scenario rows move to End to end. The order, by the damage a wrong answer
does and how often it happens, as the old live-check ranks had it:

1. **Claims and print starts**: S5, S13, P8, P9, P11, S9, P10, and P12 with
   the power-panic resume the simulator has now. Done: all eight are End to
   end.
2. **Writes**: W2 to W5, W9, W11, W12, W14, and the blip drop above.
   Done: all eight are End to end, and W5's test is the blip drop, twice.
   One thing those tests turned up to trace: after the bridge uploads a
   sync job it polls again soon, and a poll that lands in the sync job's
   own start blip sees `IDLE` with no job, so the bridge uploads a second
   time. The simulator answers that upload with a 409 and the bridge waits;
   the F10b and F10c traces at 1 s polls showed no blip at all for a sync job, so
   this may never happen on the printer. Record what a real overwrite
   upload does there, with F10.
3. **Multi-tool**: an INDX print with tool changes, a swap on one tool
   between prints while the other keeps its spool, and the per-tool write. Checked by hand once
   (three segments: tool 0 charged 505 = 475 + 30 purge, tool 1 charged
   315) with no test. INDX and XL store no UUIDs, so `M865.1` does nothing
   there, and the bridge doesn't write it on those models. Done:
   scenarios.md has a "Multi-tool" section, T1 to T7, written from F19 to
   F23, all End to end against the simulator. The per-tool
   write (T6) stays refused until the bridge supports multi-tool printers;
   it's `M865 L<n>` per tool with no `M865.1`, and gets its tests then.
4. **Spoolman faults**: M1, M2 and X9 need a Spoolman that fails on demand.
   Done: the fault transport in `integration/faults.py` covers M1, M2 and
   X9 and holds back a charge for one M3 test; Spoolman edits in the same
   file cover M3, M3a, M4 and M6.
5. **Several printers**: X1 to X16 with a two-printer `world`. Done: all
   of them.
6. **`sync_delay` above 0**: W4. Done: W4's test runs it end to end, and no
   other row depends on it.
7. **Counters and boots**: C1 to C9, B1 to B4. Done, except B3, a kill
   between a charge and the save, which nothing outside the bridge can
   produce; it stays a known limit.
