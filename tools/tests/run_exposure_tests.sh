#!/bin/bash
# run_exposure_tests.sh — Phase 6 exposure analyzer + pre-grade regression suite.
#
# Verifies the RELATIVE classification (A-cam = reference, other sources
# normalized toward IT — decision doc 2026-08-27-auto-exposure-analyzer.md)
# and the manifest pre-grade integration:
#   E01 same source vs itself          -> normal, no corrective curve
#   E02 dark source vs bright ref      -> underexposed + lighten curve
#   E03 bright source vs dark ref      -> overexposed + darken curve
#   E04 manifest --write               -> pre_grades only for off-profile sources
#   E04b patched manifest still passes the Phase 2 validator
#
#   ./run_exposure_tests.sh          run all checks, exit 0 = all green
#   ./run_exposure_tests.sh --list   list checks without running
#
# Exit code = number of failed checks (0 = all pass).
set -uo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
SRC="$HERE/fixtures"
W="$HERE/work"
mkdir -p "$W"

ANALYZER=/opt/video-studio/tools/exposure/exposure_analyze.py
PY=/opt/video-studio/tools/venv/bin/python
VAL=/opt/video-studio/tools/manifest/validate_manifest.py

if [ ! -f "$SRC/a_cam.mp4" ] || [ ! -f "$SRC/dark_b.mp4" ]; then
  echo "== building fixtures =="
  bash "$HERE/make_fixtures.sh"
fi

PASS=0; FAIL=0
RESULTS=()

pass() { PASS=$((PASS+1)); RESULTS+=("PASS  $1"); printf '  %-46s PASS\n' "$1"; }
fail() { FAIL=$((FAIL+1)); RESULTS+=("FAIL  $1"); printf '  %-46s *** FAIL: %s\n' "$1" "${2:-}"; }

if [ "${1:-}" = "--list" ]; then
  echo "exposure analyzer checks (Phase 6):"
  echo "  E01 same source vs itself            -> normal, no curve"
  echo "  E02 dark vs bright reference         -> underexposed + lighten"
  echo "  E03 bright vs dark reference         -> overexposed + darken"
  echo "  E04 manifest --write pre_grades      -> only off-profile sources"
  echo "  E04b pre-graded manifest validates   -> PASS"
  exit 0
fi

echo "== EXPOSURE ANALYZER (Phase 6) =="

E01=$("$PY" "$ANALYZER" "$SRC/a_cam.mp4" --reference "$SRC/a_cam.mp4" --json 2>"$W/exposure_last.log")
if echo "$E01" | python3 -c "
import json, sys
d = json.load(sys.stdin)
sys.exit(0 if d.get('label') == 'normal' and not d.get('corrective') else 1)
" 2>/dev/null; then
  pass "E01 same source vs itself is normal (no curve)"
else
  fail "E01 same source vs itself is normal (no curve)" "$(echo "$E01" | head -c 300)"
fi

E02=$("$PY" "$ANALYZER" "$SRC/dark_b.mp4" --reference "$SRC/a_cam.mp4" --json 2>"$W/exposure_last.log")
if echo "$E02" | python3 -c "
import json, sys
d = json.load(sys.stdin)
sys.exit(0 if d.get('label') == 'underexposed' and d.get('corrective') and 'curves=all' in d['corrective'] else 1)
" 2>/dev/null; then
  pass "E02 dark source vs bright reference lifts"
else
  fail "E02 dark source vs bright reference lifts" "$(echo "$E02" | head -c 300)"
fi

E03=$("$PY" "$ANALYZER" "$SRC/a_cam.mp4" --reference "$SRC/dark_b.mp4" --json 2>"$W/exposure_last.log")
if echo "$E03" | python3 -c "
import json, sys
d = json.load(sys.stdin)
sys.exit(0 if d.get('label') == 'overexposed' and d.get('corrective') else 1)
" 2>/dev/null; then
  pass "E03 bright source vs dark reference darkens"
else
  fail "E03 bright source vs dark reference darkens" "$(echo "$E03" | head -c 300)"
fi

cat > "$W/exposure_job.yaml" <<'EOF'
manifest_version: 1
job_id: exposure_e04
template: club_dispatch_standard_v1
sources:
  a_reel: a_cam.mp4
  b_reel:
    - dark_b.mp4
timeline:
  - id: cut_001
    angle: A
    source_in: 0.0
    source_out: 4.0
  - id: cut_002
    angle: B
    source_in: 0.0
    source_out: 4.0
profiles:
  grade: club_dispatch_pb3_v1
  crop: cd_a_pushin_v1
  graphics: cd_bug_v9
  audio: cd_youtube_v1
EOF
cp "$W/exposure_job.yaml" "$W/exposure_job_write.yaml"
"$PY" "$ANALYZER" --manifest "$W/exposure_job_write.yaml" --media-root "$SRC" --write >"$W/exposure_manifest.log" 2>&1
E04_RC=$?
if [ "$E04_RC" -eq 0 ] && "$PY" -c "
import sys, yaml
d = yaml.safe_load(open('$W/exposure_job_write.yaml'))
pg = d.get('pre_grades') or {}
sys.exit(0 if ('dark_b.mp4' in pg and 'a_cam.mp4' not in pg and pg['dark_b.mp4'].startswith('curves=all')) else 1)
" 2>/dev/null; then
  pass "E04 manifest --write adds pre_grades for dark source only"
else
  fail "E04 manifest --write adds pre_grades for dark source only" "rc=$E04_RC; see $W/exposure_manifest.log"
fi

"$PY" "$VAL" --media-root "$SRC" "$W/exposure_job_write.yaml" >"$W/exposure_last.log" 2>&1
if [ $? -eq 0 ]; then
  pass "E04b pre-graded manifest still validates"
else
  fail "E04b pre-graded manifest still validates" "$(sed -n '1,3p' "$W/exposure_last.log")"
fi

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
{
  echo "exposure analyzer regression — $(date -Is)"
  printf '%s\n' "${RESULTS[@]}"
} > "$W/exposure_tests.log"

exit "$FAIL"
