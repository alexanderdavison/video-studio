#!/bin/bash
# render_loudnorm.sh — render with EBU R128 loudness normalization baked in.
# Wraps ffmpeg so every final lands at a consistent loudness (-14 LUFS target,
# streaming standard). Use INSTEAD of a bare ffmpeg render command.
#
# Usage:
#   render_loudnorm.sh <input> <output> [extra ffmpeg args...]
# Example:
#   render_loudnorm.sh edit/base.mp4 finals/final.mp4 -vf "scale=1080:1920" -r 30
#
# Two-pass single-filter loudnorm (measured + linear) for accurate results.
set -euo pipefail

IN="$1"; OUT="$2"; shift 2

TMP=$(mktemp -d /tmp/loudnorm.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

MEASURED="$TMP/measured.json"
echo "[render_loudnorm] pass 1: measure $IN" >&2
ffmpeg -v info -i "$IN" -af loudnorm=print_format=json -f null - 2>"$TMP/pass1.log" || true
# Extract the JSON block from ffmpeg stderr
python3 - "$TMP/pass1.log" "$MEASURED" <<'PY'
import json, re, sys
log = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", log, re.S)
if not m:
    print("error: could not parse loudnorm measurement from pass 1", file=sys.stderr)
    sys.exit(1)
open(sys.argv[2], "w").write(m.group(0))
PY

INPUT_I=$(python3 -c "import json,sys; d=json.load(open('$MEASURED')); print(d['input_i'])")
INPUT_TP=$(python3 -c "import json,sys; d=json.load(open('$MEASURED')); print(d['input_tp'])")
INPUT_LRA=$(python3 -c "import json,sys; d=json.load(open('$MEASURED')); print(d['input_lra'])")
INPUT_THRESH=$(python3 -c "import json,sys; d=json.load(open('$MEASURED')); print(d['input_thresh'])")
OFFSET=$(python3 -c "import json,sys; d=json.load(open('$MEASURED')); print(d.get('target_offset','0'))")

echo "[render_loudnorm] measured: I=${INPUT_I} TP=${INPUT_TP} LRA=${INPUT_LRA} thresh=${INPUT_THRESH} offset=${OFFSET}" >&2
echo "[render_loudnorm] pass 2: render with loudnorm applied -> $OUT" >&2

ffmpeg -y -v error -i "$IN" \
  -af "loudnorm=I=-14:TP=-1.5:LRA=11:measured_I=${INPUT_I}:measured_TP=${INPUT_TP}:measured_LRA=${INPUT_LRA}:measured_thresh=${INPUT_THRESH}:offset=${OFFSET}:linear=true:print_format=summary" \
  "$@" \
  "$OUT"

echo "[render_loudnorm] done: $OUT" >&2
