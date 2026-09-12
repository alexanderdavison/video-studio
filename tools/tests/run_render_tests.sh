#!/bin/bash
# run_render_tests.sh — Phase 4 deterministic final renderer regression suite.
#
# Renders REAL 1080p30 finals from tiny synthetic manifests against the Phase 1
# media fixtures (a_cam.mp4 = 6s, short_src.mp4 = 3s) through the FULL locked
# chain: uniform x264 segments -> concat -> two-pass loudnorm -> alimiter ->
# AAC 320k -> graphics overlay -> BT.709 -> postflight (Phase 1 gates) ->
# atomic publish. Invalid jobs MUST be refused before any render work.
#
#   ./run_render_tests.sh          run all checks, exit 0 = all green
#   ./run_render_tests.sh --list   list checks without running
#
# Exit code = number of failed checks (0 = all pass).
set -uo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
SRC="$HERE/fixtures"
MFX="$SRC/manifest"
W="$HERE/work"
mkdir -p "$W"

RENDER=/opt/video-studio/tools/render/render_final.py
PY=/opt/video-studio/tools/venv/bin/python
GFX="$SRC/gfx"

# The media fixtures are produced by the Phase 1 suite; build them if absent.
if [ ! -f "$SRC/a_cam.mp4" ] || [ ! -f "$SRC/short_src.mp4" ]; then
  echo "== building Phase 1 media fixtures =="
  bash "$HERE/make_fixtures.sh"
fi

# Phase 4 needs a graphics overlay fixture (cd_bug_v9 profile resolution).
if [ ! -f "$GFX/cd_bug_v9/overlay.png" ]; then
  mkdir -p "$GFX/cd_bug_v9"
  ffmpeg -y -hide_banner -loglevel error -f lavfi -i \
    "color=c=0xDD2222@0.9:size=152x152,format=rgba" -frames:v 1 "$GFX/cd_bug_v9/overlay.png"
fi

PASS=0; FAIL=0
RESULTS=()

pass() { PASS=$((PASS+1)); RESULTS+=("PASS  $1"); printf '  %-46s PASS\n' "$1"; }
fail() { FAIL=$((FAIL+1)); RESULTS+=("FAIL  $1"); printf '  %-46s *** FAIL: %s\n' "$1" "${2:-}"; }

# render_check <name> <expected PASS|FAIL> <expected-rc> <manifest> [extra args...]
#   Copies the manifest to work/ first so postflight recording never mutates
#   the shared fixtures. expected-rc: the rc the renderer must return.
#   Output stem is the SANITIZED name (spaces/arrows -> _), so R08 can target
#   the same file R01 produced.
render_check() {
  local name="$1" exp="$2" exp_rc="$3" mf="$4"; shift 4
  local stem out jobman rc=0
  stem=$(printf '%s' "$name" | tr -cs 'A-Za-z0-9' '_')
  out="$W/final_${stem}.mp4"
  jobman="$W/man_${stem}.yaml"
  cp "$MFX/$mf" "$jobman"
  rm -f "$out" "$out.verified" "$W/ALLDONE"
  "$PY" "$RENDER" "$jobman" --media-root "$SRC" --work "$W/render_work" \
      --graphics-root "$GFX" --out "$out" "$@" >"$W/render_last.log" 2>&1; rc=$?
  if [ "$exp" = "PASS" ] && [ "$rc" -eq "$exp_rc" ] && [ -f "$out" ]; then
    pass "$name"
  elif [ "$exp" = "FAIL" ] && [ "$rc" -ne 0 ]; then
    pass "$name"
  else
    fail "$name" "expected rc=$exp_rc, got rc=$rc"
    sed -n '1,6p' "$W/render_last.log" | sed 's/^/        /'
  fi
}

R01_STEM=$(printf '%s' "R01 valid standard manifest -> 1080p final" | tr -cs 'A-Za-z0-9' '_')
R01_OUT="$W/final_${R01_STEM}.mp4"

if [ "${1:-}" = "--list" ]; then
  echo "final renderer checks (Phase 4):"
  echo "  R01 valid standard manifest -> 1080p final   -> PASS (rc 0)"
  echo "  R02 invalid manifest refused                 -> FAIL (rc 1)"
  echo "  R03 cross-file B span renders                -> PASS (rc 0)"
  echo "  R04 A-Cam-Only renders                       -> PASS (rc 0)"
  echo "  R05 confidence marker renders                -> PASS (rc 0)"
  echo "  R06 missing source file refused              -> FAIL (rc 1)"
  echo "  R07 render lock respected                    -> FAIL (rc 2)"
  echo "  R08 verified final never clobbered           -> FAIL (rc 2)"
  echo "  R09 pre-graded manifest renders               -> PASS (rc 0)"
  echo "  R09b postflight records pre_grades_applied   -> PASS"
  echo "  R10 auto-pre-graded manifest renders          -> PASS (rc 0)"
  echo "  R10b postflight records auto pre-grades      -> PASS"
  exit 0
fi

echo "== FINAL RENDERER (Phase 4) =="
render_check "R01 valid standard manifest -> 1080p final" PASS 0 valid_standard.yaml
render_check "R02 invalid manifest refused"               FAIL 1 invalid_b8_span.yaml
render_check "R03 cross-file B span renders"              PASS 0 valid_crossfile_span.yaml
render_check "R04 A-Cam-Only renders"                     PASS 0 valid_acamonly.yaml
render_check "R05 confidence marker renders"              PASS 0 valid_confidence.yaml
render_check "R06 missing source file refused"            FAIL 1 invalid_missing_source.yaml

# R07: hold the render lock, then expect rc 2 (lock respected).
R07_rc=0
( flock 9; sleep 6 ) 9>/opt/video-studio/.render.lock &
LOCK_PID=$!
sleep 0.5
cp "$MFX/valid_standard.yaml" "$W/man_R07.yaml"
"$PY" "$RENDER" "$W/man_R07.yaml" --media-root "$SRC" --work "$W/render_work" \
    --graphics-root "$GFX" --out "$W/final_R07.mp4" >"$W/render_last.log" 2>&1
R07_rc=$?
wait "$LOCK_PID" 2>/dev/null
if [ "$R07_rc" -eq 2 ]; then
  pass "R07 render lock respected"
else
  fail "R07 render lock respected" "expected rc=2 while lock held, got rc=$R07_rc"
  sed -n '1,4p' "$W/render_last.log" | sed 's/^/        /'
fi

# R08: verified final is never clobbered. R01 already produced its final
# + .verified (postflight writes it). Rendering the same job again WITHOUT
# --force must refuse at publish (rc 2) and leave the verified final intact.
R08_rc=0
cp "$MFX/valid_standard.yaml" "$W/man_R08.yaml"
"$PY" "$RENDER" "$W/man_R08.yaml" --media-root "$SRC" --work "$W/render_work" \
    --graphics-root "$GFX" --out "$R01_OUT" >"$W/render_last.log" 2>&1
R08_rc=$?
if [ "$R08_rc" -eq 2 ] && [ -f "$R01_OUT.verified" ]; then
  pass "R08 verified final never clobbered"
else
  fail "R08 verified final never clobbered" "expected rc=2 + .verified intact, got rc=$R08_rc"
  sed -n '1,6p' "$W/render_last.log" | sed 's/^/        /'
fi

# R09: pre_grades render through the FULL locked chain + QC record (Phase 6)
render_check "R09 pre-graded manifest renders" PASS 0 valid_pre_grades.yaml
R09_MAN="$W/man_R09_pre_graded_manifest_renders.yaml"
if grep -q "pre_grades_applied" "$R09_MAN" && grep -q "a_cam.mp4" "$R09_MAN"; then
  pass "R09b postflight records pre_grades_applied"
else
  fail "R09b postflight records pre_grades_applied" "$(grep -A6 postflight "$R09_MAN" | head -8)"
fi

# R10: --auto-pre-grade derives the curve at render time through the FULL
# locked chain + QC record (Phase 6). valid_dark_b has no pre_grades — the
# renderer's default auto mode must derive one for the dark B source.
render_check "R10 auto-pre-graded manifest renders" PASS 0 valid_dark_b.yaml
R10_MAN="$W/man_R10_auto_pre_graded_manifest_renders.yaml"
if grep -q "pre_grades_applied" "$R10_MAN" && grep -q "dark_b.mp4" "$R10_MAN"; then
  pass "R10b postflight records auto pre_grades_applied"
else
  fail "R10b postflight records auto pre_grades_applied" "$(grep -A6 postflight "$R10_MAN" | head -8)"
fi

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
{
  echo "final renderer regression — $(date -Is)"
  printf '%s\n' "${RESULTS[@]}"
} > "$W/render_tests.log"

exit "$FAIL"
