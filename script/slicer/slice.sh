#!/usr/bin/env bash
# Slice a 3MF with a stock PrusaSlicer profile, to read the E moves it adds outside the
# model. It runs the flatpak PrusaSlicer on a scratch data directory that holds only the
# vendor bundle, so your own presets don't change the result.
#
# Usage: script/slicer/slice.sh [-o OUT_DIR] NAME PRINTER PRINT FILAMENT TOOLS MODEL.3mf [ARGS...]
#
#   -o OUT_DIR  Where the G-code and the scratch data directory go. Defaults to a new
#               temporary directory, which the script prints.
#   NAME        The G-code's name: OUT_DIR/NAME.gcode, in ASCII.
#   PRINTER, PRINT, FILAMENT  Preset names from the vendor bundle. FILAMENT can be a
#               '|'-separated list, one preset per tool, such as
#               "Prusament PLA @MK4S|Prusament PETG @MK4S". The last preset repeats for
#               the remaining tools.
#   TOOLS       The number of filaments, one per tool the model uses.
#   ARGS        More arguments for PrusaSlicer.
#
# Needs the flatpak `com.prusa3d.PrusaSlicer`, started once so that it has fetched its
# vendor bundle (or set PRUSASLICER_VENDOR_INI to a PrusaResearch.ini). Make a model
# with script/slicer/make3mf.py.
#
# The scratch data directory enables only the XL, Core One INDX, and 0.4 mm MMU3 models
# (MK4S MMU3, Core One MMU3, Core One L MMU3). PrusaSlicer refuses a preset whose model
# isn't enabled, or slices it as another printer and exits 0, so the script fails when the
# G-code's printer preset isn't the one requested. To slice another model, add it to the
# INI below.
set -euo pipefail

out_dir=
if [ "${1:-}" = "-o" ]; then
  out_dir=${2:?-o needs a directory}
  shift 2
fi
if [ "$#" -lt 6 ]; then
  sed -n '2,/^set -/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//' >&2
  exit 2
fi
name=$1 printer=$2 print=$3 filament=$4 tools=$5 model=$6
shift 6

[ -n "$out_dir" ] || out_dir=$(mktemp -d -t slicer.XXXXXX)
mkdir -p "$out_dir"
out_dir=$(cd "$out_dir" && pwd)
model_dir=$(cd "$(dirname "$model")" && pwd)
model=$model_dir/$(basename "$model")

app=com.prusa3d.PrusaSlicer
bundle=${PRUSASLICER_VENDOR_INI:-$HOME/.var/app/$app/config/PrusaSlicer/vendor/PrusaResearch.ini}
[ -f "$bundle" ] || {
  echo "No vendor bundle at $bundle" >&2
  exit 1
}

# The scratch data directory: the vendor bundle and the models it enables.
data=$out_dir/datadir
mkdir -p "$data/vendor"
cp "$bundle" "$data/vendor/PrusaResearch.ini"
cat > "$data/PrusaSlicer.ini" << 'INI'
[vendor:PrusaResearch]
model:XL2IS = 0.4
model:XL5IS = 0.4
model:XLP2T = 0.4
model:COREONE_INDX8T = HF0.4
model:COREONE_INDX4T = HF0.4
model:MK4SMMU3 = 0.4
model:COREONEMMU3 = 0.4
model:COREONELMMU3 = 0.4
INI
echo "bundle: $(grep -m1 '^config_version' "$bundle")" >&2

IFS='|' read -r -a fils <<< "$filament"
materials=()
for i in $(seq 0 $((tools - 1))); do
  materials+=(--material-profile "${fils[$i]:-${fils[${#fils[@]} - 1]}}")
done

timeout 300 flatpak run --filesystem="$out_dir" --filesystem="$model_dir:ro" \
  --command=prusa-slicer "$app" --datadir "$data" \
  --printer-profile "$printer" --print-profile "$print" "${materials[@]}" \
  --binary-gcode=0 "$@" --export-gcode -o "$out_dir/$name.gcode" "$model"

# The G-code's config block names the printer preset it used.
used=$(sed -n 's/^; printer_settings_id = //p' "$out_dir/$name.gcode")
if [ "$used" != "$printer" ]; then
  echo "Sliced with printer preset '$used', not '$printer'. Is its model enabled in the INI above?" >&2
  exit 1
fi
grep -m1 '^M862.3' "$out_dir/$name.gcode" >&2
echo "$out_dir/$name.gcode"
