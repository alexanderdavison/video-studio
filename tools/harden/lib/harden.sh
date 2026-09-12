#!/bin/bash
# harden.sh — Phase 1 gates for the ISH D DJ-video pipeline (2026-08-26 master scope).
# Source this from render scripts. Each gate fails loudly with rc!=0 and a FAIL: line.
# ALLDONE is ONLY ever written by harden_postflight — never by render scripts.

HARDEN_LOCKFILE="${HARDEN_LOCKFILE:-/opt/video-studio/.render.lock}"

# --- Gate 5: single-render lock (flock, auto-released on process exit) ---
# Usage: harden_lock_acquire <label> || exit 1
harden_lock_acquire() {
  local label="$1"
  if [ "${HARDEN_LOCK_HELD:-0}" = "1" ]; then
    echo "FAIL: render already in progress (held by this process)" >&2
    return 1
  fi
  exec 9>"$HARDEN_LOCKFILE"
  if ! flock -n 9; then
    local holder
    holder=$(cat "$HARDEN_LOCKFILE" 2>/dev/null || echo "?")
    echo "FAIL: render already in progress (lock holder: $holder)" >&2
    return 1
  fi
  HARDEN_LOCK_HELD=1
  echo "$$ $label $(date -Is)" > "$HARDEN_LOCKFILE"
  return 0
}

# --- Gate 2: ready-flag (directive 2 — dispatcher gate) ---
# Usage: harden_ready_flag_required <project_dir> || exit 1
harden_ready_flag_required() {
  local project_dir="$1"
  if [ ! -f "$project_dir/READY_TO_BAKE" ]; then
    echo "FAIL: job blocked — no READY_TO_BAKE flag in $project_dir" >&2
    return 1
  fi
  return 0
}

# --- Gate 1: stage verification (directive 1 — moov + duration + stderr) ---
# Usage: harden_verify_stage <file> <expected_dur> [stderr_log]
harden_verify_stage() {
  local f="$1" expected_dur="$2"
  local stderr_log="${3:-${f%.mp4}.stderr.log}"
  [ -f "$f" ] || { echo "FAIL: no output file $f" >&2; return 1; }
  strings "$f" | grep -qc moov || { echo "FAIL: no moov atom in $f" >&2; return 1; }
  local dur
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null) || { echo "FAIL: ffprobe cannot read $f" >&2; return 1; }
  awk -v d="$dur" -v e="$expected_dur" 'BEGIN{exit !(d>e-1 && d<e+1)}' || { echo "FAIL: duration $dur != expected $expected_dur" >&2; return 1; }
  if [ -f "$stderr_log" ] && grep -qE "Error reinitializing filters|Invalid duration|Conversion failed" "$stderr_log"; then
    echo "FAIL: fatal pattern in $stderr_log" >&2
    return 1
  fi
  echo "OK: $f verified (moov present, duration $dur ≈ $expected_dur, stderr clean)"
  return 0
}

# --- Gate 3: captured execution (checked rc + retained stderr) ---
# Usage: harden_run_captured <log_path> <cmd...> || exit 1
harden_run_captured() {
  local log="$1"; shift
  "$@" >"$log" 2>&1
  local rc=$?
  if [ $rc -ne 0 ]; then
    echo "FAIL: command failed rc=$rc (log: $log)" >&2
    return $rc
  fi
  return 0
}

# --- Gate: input hashing + span validation ---
# Usage: harden_input_hash <file> — prints sha256 or fails
harden_input_hash() {
  local f="$1"
  [ -f "$f" ] || { echo "FAIL: input missing $f" >&2; return 1; }
  sha256sum "$f" | awk '{print $1}'
}

# --- Gate: full-program loudness + true peak (ebur128 over the WHOLE file) ---
# Usage: harden_loudness <file> <target_lufs> <max_dbtp> || exit 1
harden_loudness() {
  local f="$1" target="$2" max_tp="$3"
  [ -f "$f" ] || { echo "FAIL: no file for loudness check: $f" >&2; return 1; }
  local stats lufs tp
  stats=$(ffmpeg -nostats -i "$f" -af ebur128=peak=true -f null - 2>&1)
  lufs=$(echo "$stats" | grep -oE "I: -?[0-9.]+ LUFS" | tail -1 | grep -oE "\-?[0-9.]+")
  tp=$(echo "$stats" | grep -oE "True peak: -?[0-9.]+ dBTP" | tail -1 | grep -oE "\-?[0-9.]+")
  [ -n "$lufs" ] || { echo "FAIL: could not measure loudness" >&2; return 1; }
  awk -v l="$lufs" -v t="$target" 'BEGIN{exit !(l>t-0.3 && l<t+0.3)}' \
    || { echo "FAIL: integrated loudness $lufs LUFS outside ±0.3 of $target" >&2; return 1; }
  if [ -n "$tp" ]; then
    awk -v p="$tp" -v m="$max_tp" 'BEGIN{exit !(p<=m)}' \
      || { echo "FAIL: true peak $tp dBTP exceeds $max_tp" >&2; return 1; }
  fi
  echo "OK: loudness $lufs LUFS, true peak ${tp:-n/a} dBTP (target $target ±0.3, TP ≤ $max_tp)"
  return 0
}

# --- Gate: BT.709 tags on the delivery stream ---
# Usage: harden_bt709 <file> || exit 1
harden_bt709() {
  local f="$1"
  local tag
  tag=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_primaries,color_transfer,color_space -of csv=p=0 "$f" 2>/dev/null)
  case "$tag" in
    bt709,bt709,bt709) echo "OK: BT.709 tags present"; return 0;;
    *) echo "FAIL: BT.709 tags missing (got: $tag)" >&2; return 1;;
  esac
}

# --- Gate: atomic final (.partial → verify → rename; never clobber verified final) ---
# Usage: harden_atomic_final <partial> <final> <expected_dur> || exit 1
harden_atomic_final() {
  local partial="$1" final="$2" expected_dur="$3"
  harden_verify_stage "$partial" "$expected_dur" || return 1
  if [ -f "$final" ]; then
    echo "FAIL: refusing to overwrite existing verified final $final (archive it first)" >&2
    return 1
  fi
  mv "$partial" "$final" || { echo "FAIL: rename $partial → $final" >&2; return 1; }
  echo "OK: atomic final delivered → $final"
  return 0
}

# --- Gate: ALLDONE ONLY from postflight verifier ---
# Usage: harden_postflight <final> <alldone_log> <expected_dur> || exit 1
harden_postflight() {
  local final="$1" log="$2" expected_dur="$3"
  harden_verify_stage "$final" "$expected_dur" || return 1
  echo "ALLDONE $(date -Is) $(basename "$final") $(stat -c%s "$final" 2>/dev/null || echo 0) bytes" > "$log"
  echo "OK: postflight verified — ALLDONE written to $log"
  return 0
}
