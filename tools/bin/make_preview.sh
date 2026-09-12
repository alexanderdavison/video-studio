#!/bin/bash
# =============================================================================
# make_preview.sh — Directive 4 (cheap preview) + Directive 2 flag writer.
# Source: vault creative/decisions/2026-08-26-pipeline-hardening-directives.md
#
# Any grade/crop/bug decision gets a DOWNSCALED PREVIEW first (5-10s, 960px,
# SAME filter chain) before a full bake is queued. Turns a ~1h feedback loop
# into a sub-minute one. Preview APPROVAL is what writes the READY_TO_BAKE
# flag that unblocks the conductor (Directive 2) — preview -> approve -> flag
# -> conductor unblocks -> full bake.
#
# Usage:
#   make_preview.sh render <input> <filter_chain> <project_dir> [--ts <sec>] [--dur <sec>] [--width <px>]
#   make_preview.sh approve <project_dir> [note]
#
#   render : make a cheap preview (default: representative ts = 5% of duration,
#           dur 8s, width 960). Output: <project_dir>/renders/preview_<ts>.mp4
#   approve: terminal action of preview approval — writes
#           <project_dir>/READY_TO_BAKE (fails if a stale flag exists, since
#           that would mask a NEW approval; remove it first if re-approving).
#
# Requires the hardening lib for the render lock (a preview is still a render
# and must not collide with a live bake) and the pre-flight linter.
# =============================================================================
set -uo pipefail
source /opt/video-studio/tools/lib/hardening.sh

CMD="${1:-}"
if [ -z "$CMD" ]; then
  echo "usage: make_preview.sh render <input> <filter_chain> <project_dir> [--ts sec] [--dur sec] [--width px]"
  echo "       make_preview.sh approve <project_dir> [note]"
  exit 2
fi

case "$CMD" in
  render)
    IN="${2:-}"; FILTER="${3:-}"; PROJ="${4:-}"
    TS=""; DUR="8"; WIDTH="960"
    shift 4
    while [ $# -gt 0 ]; do
      case "$1" in
        --ts) TS="$2"; shift 2 ;;
        --dur) DUR="$2"; shift 2 ;;
        --width) WIDTH="$2"; shift 2 ;;
        *) echo "unknown arg: $1"; exit 2 ;;
      esac
    done
    if [ -z "$IN" ] || [ -z "$FILTER" ] || [ -z "$PROJ" ]; then
      echo "make_preview render: need <input> <filter_chain> <project_dir>"; exit 2
    fi
    mkdir -p "$PROJ/renders"

    if ! acquire_render_lock; then
      exit 1
    fi

    # representative timestamp: explicit --ts, else 5% into the source
    if [ -z "$TS" ]; then
      SRCDUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$IN" 2>/dev/null)
      TS=$(python3 -c "print(f'{float('$SRCDUR' or 0)*0.05:.1f}')")
    fi

    OUT="$PROJ/renders/preview_${TS}.mp4"
    echo "PREVIEW: $IN @${TS}s for ${DUR}s, width ${WIDTH}, same filter chain — $(date +%H:%M:%S)"
    if ! run_ffmpeg -y -ss "$TS" -t "$DUR" -i "$IN" \
        -vf "scale=${WIDTH}:-1,${FILTER}" \
        -c:v libx264 -preset ultrafast -crf 28 \
        "$OUT" 2>"$PROJ/renders/preview_${TS}.stderr.log"; then
      echo "PREVIEW FAILED (ffmpeg or pre-flight lint)" >&2
      exit 1
    fi
    echo "PREVIEW DONE: $OUT"
    ;;

  approve)
    PROJ="${2:-}"; NOTE="${3:-}"
    if [ -z "$PROJ" ]; then
      echo "make_preview approve: need <project_dir>"; exit 2
    fi
    if [ -f "$PROJ/READY_TO_BAKE" ]; then
      echo "REFUSED: $PROJ/READY_TO_BAKE already exists — a stale flag would mask a NEW approval." >&2
      echo "Remove it first if you truly want to re-approve this bake." >&2
      exit 1
    fi
    printf 'approved %s by %s\n%s\n' "$(date -Is)" "${USER:-operator}" "${NOTE:-preview approval}" > "$PROJ/READY_TO_BAKE"
    echo "READY_TO_BAKE written: $PROJ/READY_TO_BAKE"
    ;;

  *)
    echo "unknown command: $CMD"; exit 2 ;;
esac
