#!/bin/bash
# preview.sh — Directive 4: cheap preview before any full bake.
# Turns an hour-long grade/crop/bug feedback loop into a sub-minute one.
#
# Usage:
#   PREVIEW_TS=182.4 PREVIEW_SECONDS=8 preview.sh <input> <filter_chain> <out.mp4>
# Example:
#   PREVIEW_TS=182.4 preview.sh a_full_pb3.mp4 "eq=brightness=0.06:saturation=1.12" prev_$(date +%s).mp4
set -e
TS="${PREVIEW_TS:-0}"
SECS="${PREVIEW_SECONDS:-8}"
IN="$1"
FILTER="$2"
OUT="$3"
[ -n "$IN" ] && [ -n "$FILTER" ] && [ -n "$OUT" ] || { echo "usage: preview.sh <input> <filter_chain> <out.mp4>" >&2; exit 2; }
ffmpeg -y -ss "$TS" -t "$SECS" -i "$IN" \
  -vf "scale=960:-1,${FILTER}" \
  -c:v libx264 -preset ultrafast -crf 28 \
  "$OUT"
echo "OK: preview → $OUT (ts=$TS, ${SECS}s, 960px)"
