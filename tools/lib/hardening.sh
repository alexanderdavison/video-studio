#!/bin/bash
# =============================================================================
# pipeline hardening lib — shared enforcement layer for /opt/video-studio
# Source: vault creative/decisions/2026-08-26-pipeline-hardening-directives.md
# Implements: D1 (verify_stage gate), D3 (pre-flight lint), D5 (render lockfile)
# Usage: source /opt/video-studio/tools/lib/hardening.sh  (then call functions)
# =============================================================================

HARDENING_LOCKFILE=/opt/video-studio/.render.lock
HARDENING_LINTER=/opt/video-studio/tools/bin/ffmpeg_lint.py

# ---------------------------------------------------------------------------
# D5 — render lockfile: one CPU render at a time, enforced not remembered.
# Call acquire_render_lock at the top of any render script; it refuses to
# start if a live process already holds the lock. A stale lock (dead pid) is
# taken over automatically.
# ---------------------------------------------------------------------------
acquire_render_lock() {
  # flock(1)-based (Phase 1, 2026-08-26 homelab): the earlier PID check-then-set
  # had a TOCTOU race — two processes could both pass the kill -0 test and both
  # render. flock is atomic: the lock IS the open fd, released automatically when
  # the holder dies (no manual stale-takeover needed). Lockfile content still
  # carries the holding pid for diagnostics.
  local fd
  exec {fd}>>"$HARDENING_LOCKFILE" 2>/dev/null || { echo "REFUSED: cannot open lockfile $HARDENING_LOCKFILE" >&2; return 1; }
  if ! flock -n "$fd"; then
    local holder
    holder=$(cat "$HARDENING_LOCKFILE" 2>/dev/null || echo unknown)
    echo "REFUSED: render already in progress (lock held; pid ${holder:-unknown})" >&2
    return 1
  fi
  : > "$HARDENING_LOCKFILE"
  echo $$ > "$HARDENING_LOCKFILE"
  trap 'release_render_lock' EXIT
  echo "LOCK: acquired render lock (pid $$)" >&2
  return 0
}

release_render_lock() {
  if [ -f "$HARDENING_LOCKFILE" ] && [ "$(cat "$HARDENING_LOCKFILE" 2>/dev/null)" = "$$" ]; then
    rm -f "$HARDENING_LOCKFILE"
    echo "LOCK: released render lock (pid $$)" >&2
  fi
}

# ---------------------------------------------------------------------------
# D1 — stage verification gate. Every stage script verifies its own output
# BEFORE returning control. Any FAIL halts the pipeline at that stage: the
# stage is NOT marked done and nothing downstream fires.
#
#   verify_stage <file> <expected_dur> [stderr_log]
#     file         output to verify (mp4)
#     expected_dur expected duration in seconds (±1s tolerance)
#     stderr_log   optional ffmpeg stderr log to scan for fatal patterns
#                  (default: <file without .mp4>.stderr.log; missing log = pass)
#
# Returns 0 = PASS, 1 = FAIL (with reason on stdout).
# ---------------------------------------------------------------------------
verify_stage() {
  local f="$1" expected_dur="$2" log="${3:-${1%.mp4}.stderr.log}"
  if [ ! -f "$f" ]; then
    echo "FAIL: output file missing: $f"
    return 1
  fi
  # 1. moov present = file actually finalized
  # NOTE (homelab 2026-08-26): grep -qc SIGPIPEs strings under set -o pipefail
  # (grep -q exits on first match; early moov = SIGPIPE = rc 141 = false FAIL).
  # Use grep -c with output discarded — reads to EOF, exit 0 on >=1 match.
  if ! strings "$f" | grep -c moov >/dev/null; then
    echo "FAIL: no moov atom in $f"
    return 1
  fi
  # 2. duration matches expectation within tolerance
  local dur
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null)
  if [ -z "$dur" ] || [ "$dur" = "N/A" ]; then
    echo "FAIL: ffprobe returned no duration for $f"
    return 1
  fi
  if ! awk -v d="$dur" -v e="$expected_dur" 'BEGIN{exit !(d>e-1 && d<e+1)}'; then
    echo "FAIL: duration $dur != expected $expected_dur ($f)"
    return 1
  fi
  # 3. stderr log has no known-fatal patterns (missing log = pass)
  if [ -f "$log" ] && grep -qE "Error reinitializing filters|Invalid duration|Conversion failed" "$log"; then
    echo "FAIL: fatal pattern in stderr log $log"
    return 1
  fi
  echo "PASS: $f dur=$dur expected=$expected_dur"
  return 0
}

# ---------------------------------------------------------------------------
# D3 — pre-flight linter wrapper. run_ffmpeg replaces bare `ffmpeg` in stage
# scripts: it lints the full argument list BEFORE launching, and refuses to
# run (exit 1) if the command matches a known-dead pattern. The pattern table
# lives in tools/bin/ffmpeg_lint.py and is owned by RCC — new footguns get a
# row added there, not a new lesson.
# ---------------------------------------------------------------------------
run_ffmpeg() {
  if ! python3 "$HARDENING_LINTER" "$@"; then
    echo "PREFLIGHT FAIL: ffmpeg command blocked by linter — job does not run." >&2
    return 1
  fi
  ffmpeg "$@"
}

# =============================================================================
# PHASE 1 additions (homelab, 2026-08-26) — master-scope gates beyond D1-D5.
# Source: vault creative/decisions/2026-08-26-refined-implementation-plan.md
# Adds: input hashes · source-span validation · .partial atomic publish with
# never-clobber · postflight verifier (the ONLY writer of ALLDONE).
# =============================================================================

# ---------------------------------------------------------------------------
# Input hashes — "upload must be STABLE before probing" and "never trust a
# report without a verified handle" as code. Record sha256 of every input at
# job start; verify immediately before any render command. A mid-write NFS
# read or an edited input is caught BEFORE an expensive bake.
#
#   record_input_hashes <hashfile> <files...>
#   verify_input_hashes  <hashfile> <files...>   (exit 1 + message on ANY diff)
# ---------------------------------------------------------------------------
record_input_hashes() {
  local hf="$1"; shift
  : > "$hf" || { echo "HASH FAIL: cannot write $hf" >&2; return 1; }
  local f
  for f in "$@"; do
    if [ ! -f "$f" ]; then
      echo "HASH FAIL: input missing: $f" >&2
      return 1
    fi
    sha256sum "$f" >> "$hf" || { echo "HASH FAIL: sha256sum failed on $f" >&2; return 1; }
  done
  echo "HASH: recorded $# input hashes -> $hf" >&2
  return 0
}

verify_input_hashes() {
  local hf="$1"; shift
  if [ ! -f "$hf" ]; then
    echo "HASH FAIL: no recorded hash file: $hf" >&2
    return 1
  fi
  local f rec_hash cur_hash
  for f in "$@"; do
    rec_hash=$(awk -v f="$f" '$2==f {print $1; exit}' "$hf")
    if [ -z "$rec_hash" ]; then
      echo "HASH FAIL: $f is not in the recorded hash file" >&2
      return 1
    fi
    cur_hash=$(sha256sum "$f" 2>/dev/null | awk '{print $1}')
    if [ -z "$cur_hash" ]; then
      echo "HASH FAIL: cannot read $f (missing or mid-write?)" >&2
      return 1
    fi
    if [ "$rec_hash" != "$cur_hash" ]; then
      echo "HASH FAIL: $f changed since record (recorded $rec_hash, now $cur_hash)" >&2
      return 1
    fi
  done
  echo "HASH PASS: all $# inputs match recorded hashes" >&2
  return 0
}

# ---------------------------------------------------------------------------
# Source-span validation — postmortem #8 as code. A requested span that runs
# past the source file's end silently truncates (b8: 100s requested from
# B0038 with 23.85s left -> final came out 76s short). Validate EVERY span
# against the source duration BEFORE baking.
#
#   validate_span <file> <start_sec> <dur_sec> [tolerance_sec]
# ---------------------------------------------------------------------------
validate_span() {
  local f="$1" start="$2" dur="$3" tol="${4:-0.5}"
  if [ ! -f "$f" ]; then
    echo "SPAN FAIL: source missing: $f" >&2
    return 1
  fi
  local srcdur
  srcdur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null)
  if [ -z "$srcdur" ] || [ "$srcdur" = "N/A" ]; then
    echo "SPAN FAIL: cannot probe duration of $f" >&2
    return 1
  fi
  if ! python3 - "$start" "$dur" "$srcdur" "$tol" <<'PY'
import sys
start, dur, srcdur, tol = map(float, sys.argv[1:5])
sys.exit(0 if (start >= 0 and dur > 0 and start + dur <= srcdur + tol) else 1)
PY
  then
    echo "SPAN FAIL: $f start=$start dur=$dur exceeds source duration $srcdur (tol $tol)" >&2
    return 1
  fi
  echo "SPAN PASS: $f start=$start dur=$dur within source $srcdur" >&2
  return 0
}

# ---------------------------------------------------------------------------
# Atomic publish with never-clobber — renders write to <name>.partial; publish
# verifies a moov exists (caller should have run verify_stage/postflight on the
# partial), then atomically renames. A VERIFIED final (<final>.verified) is
# NEVER overwritten without --force; an unverified leftover is parked aside.
#
#   publish_partial <partial> <final> [--force]
# ---------------------------------------------------------------------------
publish_partial() {
  local partial="$1" final="$2" force="${3:-}"
  if [ ! -f "$partial" ]; then
    echo "PUBLISH REFUSED: partial missing: $partial" >&2
    return 1
  fi
  if ! strings "$partial" | grep -c moov >/dev/null; then
    echo "PUBLISH REFUSED: $partial has no moov atom — nothing unverified gets published." >&2
    return 1
  fi
  if [ -f "$final" ]; then
    if [ -f "$final.verified" ]; then
      if [ "$force" = "--force" ]; then
        local ts
        ts=$(date +%s)
        mv -f "$final" "$final.prev.$ts"
        echo "PUBLISH: --force — parked previous verified final at $final.prev.$ts" >&2
      else
        echo "PUBLISH REFUSED: $final is a verified final — never overwrite the last verified final." >&2
        echo "Archive it deliberately (publish_partial --force) or pick a new version name." >&2
        return 1
      fi
    else
      local ts2
      ts2=$(date +%s)
      mv -f "$final" "$final.unverified.$ts2"
      echo "PUBLISH: parked unverified leftover $final at $final.unverified.$ts2" >&2
    fi
  fi
  mv -f "$partial" "$final" || { echo "PUBLISH FAIL: rename $partial -> $final" >&2; return 1; }
  sync
  echo "PUBLISH OK: $final" >&2
  return 0
}

# ---------------------------------------------------------------------------
# Postflight verifier — the ONLY writer of ALLDONE. A render script may print
# "ALLDONE" to a log, but the ALLDONE marker file that downstream automation
# keys on is created exclusively here, after the FULL check set passes. Any
# stale ALLDONE is removed on entry, so a lie can never survive a FAIL.
#
#   postflight_verify <final> <expected_dur> [--lufs -13.5] [--tp -1.5]
#                     [--lufs-tol 0.3] [--log <stderr_log>]
#
# Checks: existence · moov · faststart (moov in first 64KB) · duration ±1s ·
# >=1 video + >=1 audio stream · pix_fmt yuv420p · zero-error decode pass ·
# BT.709 primaries/transfer/matrix tags · full-program loudness (integrated
# LUFS gate, measured on the delivered file via decoded WAV) · true peak via
# decoded WAV (loudnorm JSON over-reads TP on AAC — runbook lock).
# PASS -> writes <final>.verified + ALLDONE. FAIL -> removes stale ALLDONE.
# ---------------------------------------------------------------------------
postflight_verify() {
  local f="$1" expected="$2"; shift 2
  local lufs=-13.5 tp=-1.5 lufs_tol=0.3 log=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --lufs) lufs="$2"; shift 2 ;;
      --tp) tp="$2"; shift 2 ;;
      --lufs-tol) lufs_tol="$2"; shift 2 ;;
      --log) log="$2"; shift 2 ;;
      *) echo "postflight_verify: unknown arg $1" >&2; return 2 ;;
    esac
  done
  local dir
  dir=$(dirname "$f")
  rm -f "$dir/ALLDONE"   # stale/lie marker must not survive a FAIL
  if [ ! -f "$f" ]; then
    echo "POSTFLIGHT FAIL: final missing: $f"
    return 1
  fi
  local TMP
  TMP=$(mktemp -d /tmp/postflight.XXXXXX)
  # NOTE (homelab 2026-08-26): a bare 'rm -rf "$TMP"' RETURN trap re-fires on
  # LATER function returns in the same shell, when TMP is unset -> set -u abort.
  # Guard: no-op unless TMP is still set and the dir still exists.
  trap 'if [ -n "${TMP:-}" ] && [ -d "$TMP" ]; then rm -rf "$TMP"; TMP=; fi' RETURN

  # 1. moov present
  if ! strings "$f" | grep -c moov >/dev/null; then
    echo "POSTFLIGHT FAIL: no moov atom in $f"; return 1
  fi
  # 2. faststart: moov in the first 64KB (delivery contract is +faststart)
  if ! head -c 65536 "$f" | grep -c moov >/dev/null; then
    echo "POSTFLIGHT FAIL: moov not in first 64KB — +faststart missing"; return 1
  fi
  # 3. duration ±1s
  local dur
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null)
  if [ -z "$dur" ] || [ "$dur" = "N/A" ]; then
    echo "POSTFLIGHT FAIL: no duration from ffprobe"; return 1
  fi
  if ! awk -v d="$dur" -v e="$expected" 'BEGIN{exit !(d>e-1 && d<e+1)}'; then
    echo "POSTFLIGHT FAIL: duration $dur != expected $expected"; return 1
  fi
  # 4. streams: at least one video + one audio
  local vcount acount
  vcount=$(ffprobe -v error -select_streams v -show_entries stream=codec_type -of csv=p=0 "$f" 2>/dev/null | grep -c . || true)
  acount=$(ffprobe -v error -select_streams a -show_entries stream=codec_type -of csv=p=0 "$f" 2>/dev/null | grep -c . || true)
  if [ "${vcount:-0}" -lt 1 ] || [ "${acount:-0}" -lt 1 ]; then
    echo "POSTFLIGHT FAIL: streams v=$vcount a=$acount (need >=1 each)"; return 1
  fi
  # 5. pixel format yuv420p
  local pix
  pix=$(ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt -of csv=p=0 "$f" 2>/dev/null)
  if [ "$pix" != "yuv420p" ]; then
    echo "POSTFLIGHT FAIL: pix_fmt=$pix (want yuv420p)"; return 1
  fi
  # 6. decode pass: zero error lines
  local decode_err
  decode_err=$(ffmpeg -v error -i "$f" -f null - 2>&1 | grep -cE "Error|error" || true)
  if [ "${decode_err:-1}" -ne 0 ]; then
    echo "POSTFLIGHT FAIL: decode reported $decode_err error lines"; return 1
  fi
  # 7. BT.709 tags (delivery contract)
  local cp ct cs
  cp=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_primaries -of csv=p=0 "$f" 2>/dev/null)
  ct=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_transfer -of csv=p=0 "$f" 2>/dev/null)
  cs=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_space -of csv=p=0 "$f" 2>/dev/null)
  if [ "$cp" != "bt709" ] || [ "$ct" != "bt709" ] || [ "$cs" != "bt709" ]; then
    echo "POSTFLIGHT FAIL: BT.709 tags cp=$cp ct=$ct cs=$cs (want bt709/bt709/bt709)"; return 1
  fi
  # 8. full-program loudness + true peak (measured on the delivered file;
  #    TP via decoded WAV — the AAC-over-read workaround from the runbook)
  local wav="$TMP/full.wav"
  if ! ffmpeg -y -v error -i "$f" -vn -c:a pcm_s16le "$wav" 2>"$TMP/decode.log"; then
    echo "POSTFLIGHT FAIL: cannot decode audio to WAV"; return 1
  fi
  local meas integ tp_meas
  meas=$(ffmpeg -i "$wav" -af loudnorm=print_format=json -f null - 2>&1 | python3 -c "
import json, re, sys
log = sys.stdin.read()
m = re.search(r'\{[^{}]*\"input_i\"[^{}]*\}', log, re.S)
if not m: sys.exit(1)
d = json.loads(m.group(0))
print(f\"{d.get('input_i')} {d.get('input_tp')}\")
")
  integ=$(echo "$meas" | awk '{print $1}')
  tp_meas=$(echo "$meas" | awk '{print $2}')
  if [ -z "$integ" ] || [ -z "$tp_meas" ]; then
    echo "POSTFLIGHT FAIL: could not measure loudness on $f"; return 1
  fi
  if ! awk -v i="$integ" -v t="$lufs" -v tol="$lufs_tol" 'BEGIN{exit !(i>t-tol && i<t+tol)}'; then
    echo "POSTFLIGHT FAIL: integrated $integ LUFS outside $lufs ±$lufs_tol"; return 1
  fi
  if ! awk -v p="$tp_meas" -v t="$tp" 'BEGIN{exit !(p<=t)}'; then
    echo "POSTFLIGHT FAIL: true peak $tp_meas dBTP > $tp"; return 1
  fi
  # 9. fatal patterns in supplied stderr log (optional)
  if [ -n "$log" ] && [ -f "$log" ] && grep -qE "Error reinitializing filters|Invalid duration|Conversion failed" "$log"; then
    echo "POSTFLIGHT FAIL: fatal pattern in stderr log $log"; return 1
  fi

  # PASS — verified marker + ALLDONE (the only writer)
  touch "$f.verified"
  printf 'ALLDONE %s  dur=%s  pix=%s  bt709=%s/%s/%s  LUFS=%s  TP=%s\n' \
    "$(date -Is)" "$dur" "$pix" "$cp" "$ct" "$cs" "$integ" "$tp_meas" > "$dir/ALLDONE"
  echo "POSTFLIGHT PASS: $f dur=$dur LUFS=$integ TP=$tp_meas — ALLDONE written"
  return 0
}
