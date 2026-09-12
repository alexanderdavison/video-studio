#!/bin/bash
# run_manifest_tests.sh — Phase 2 manifest schema + validator regression suite.
#
# Proves the validator gate with tiny synthetic manifests against REAL media
# durations (reuses the Phase 1 fixtures: a_cam.mp4 = 6s, short_src.mp4 = 3s).
# Invalid manifests MUST fail; valid ones MUST pass.
#
#   ./run_manifest_tests.sh          run all checks, exit 0 = all green
#   ./run_manifest_tests.sh --list   list checks without running
#
# Exit code = number of failed checks (0 = all pass).
set -uo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
SRC="$HERE/fixtures"
MFX="$SRC/manifest"
W="$HERE/work"
mkdir -p "$W"

# Manifest tools live in the sibling manifest/ dir; venv python has pyyaml+jsonschema.
MANIFEST_DIR="$HERE/../manifest"
VAL="$MANIFEST_DIR/validate_manifest.py"
PY=/opt/video-studio/tools/venv/bin/python

# The media fixtures are produced by the Phase 1 suite; build them if absent.
if [ ! -f "$SRC/a_cam.mp4" ] || [ ! -f "$SRC/short_src.mp4" ]; then
  echo "== building Phase 1 media fixtures =="
  bash "$HERE/make_fixtures.sh"
fi

PASS=0; FAIL=0
RESULTS=()

pass() { PASS=$((PASS+1)); RESULTS+=("PASS  $1"); printf '  %-46s PASS\n' "$1"; }
fail() { FAIL=$((FAIL+1)); RESULTS+=("FAIL  $1"); printf '  %-46s *** FAIL: %s\n' "$1" "${2:-}"; }

# run_check <name> <expected PASS|FAIL> <cmd...>
run_check() {
  local name="$1" exp="$2"; shift 2
  local rc=0
  "$@" >"$W/manifest_last.log" 2>&1; rc=$?
  if [ "$exp" = "PASS" ] && [ "$rc" -eq 0 ]; then
    pass "$name"
  elif [ "$exp" = "FAIL" ] && [ "$rc" -ne 0 ]; then
    pass "$name"
  else
    fail "$name" "expected $exp, got rc=$rc"
    sed -n '1,4p' "$W/manifest_last.log" | sed 's/^/        /'
  fi
}

if [ "${1:-}" = "--list" ]; then
  echo "manifest validator checks (Phase 2):"
  echo "  M01 valid standard manifest            -> PASS"
  echo "  M02 invalid span beyond source (b8)    -> FAIL"
  echo "  M03 unknown profile                    -> FAIL"
  echo "  M04 negative source_in                 -> FAIL"
  echo "  M05 missing source file                -> FAIL"
  echo "  M06 unknown template                   -> FAIL"
  echo "  M07 cross-file b_reel span (shape OK)  -> PASS"
  echo "  M08 cross-file span beyond reel        -> FAIL"
  echo "  M09 duplicate cut ids                  -> FAIL"
  echo "  M10 angle B without b_reel             -> FAIL"
  echo "  M11 malformed YAML                     -> FAIL"
  echo "  M12 postflight block records clean     -> PASS"
  echo "  M13 confidence low/high accepted       -> PASS"
  echo "  M14 confidence outside enum            -> FAIL"
  echo "  M15 valid pre_grades block             -> PASS"
  echo "  M16 pre_grades unknown source key      -> FAIL"
  exit 0
fi

echo "== MANIFEST VALIDATOR (Phase 2) =="
run_check "M01 valid standard manifest"            PASS "$PY" "$VAL" --media-root "$SRC" "$MFX/valid_standard.yaml"
run_check "M02 invalid span beyond source (b8)"    FAIL "$PY" "$VAL" --media-root "$SRC" "$MFX/invalid_b8_span.yaml"
run_check "M03 unknown profile"                    FAIL "$PY" "$VAL" --media-root "$SRC" "$MFX/invalid_unknown_profile.yaml"
run_check "M04 negative source_in"                 FAIL "$PY" "$VAL" --media-root "$SRC" "$MFX/invalid_negative_source_in.yaml"
run_check "M05 missing source file"                FAIL "$PY" "$VAL" --media-root "$SRC" "$MFX/invalid_missing_source.yaml"
run_check "M06 unknown template"                   FAIL "$PY" "$VAL" --media-root "$SRC" "$MFX/invalid_unknown_template.yaml"
run_check "M07 cross-file b_reel span (shape OK)"  PASS "$PY" "$VAL" --media-root "$SRC" "$MFX/valid_crossfile_span.yaml"
run_check "M08 cross-file span beyond reel"        FAIL "$PY" "$VAL" --media-root "$SRC" "$MFX/invalid_crossfile_overflow.yaml"
run_check "M09 duplicate cut ids"                  FAIL "$PY" "$VAL" --media-root "$SRC" "$MFX/invalid_duplicate_cut_id.yaml"
run_check "M10 angle B without b_reel"             FAIL "$PY" "$VAL" --media-root "$SRC" "$MFX/invalid_angle_b_no_reel.yaml"
run_check "M11 malformed YAML"                     FAIL "$PY" "$VAL" --media-root "$SRC" "$MFX/invalid_malformed.yaml"
run_check "M12 postflight block records clean"     PASS "$PY" "$VAL" --media-root "$SRC" "$MFX/valid_postflight.yaml"
run_check "M13 confidence low/high accepted"       PASS "$PY" "$VAL" --media-root "$SRC" "$MFX/valid_confidence.yaml"
run_check "M14 confidence outside enum"            FAIL "$PY" "$VAL" --media-root "$SRC" "$MFX/invalid_confidence.yaml"
run_check "M15 valid pre_grades block"             PASS "$PY" "$VAL" --media-root "$SRC" "$MFX/valid_pre_grades.yaml"
run_check "M16 pre_grades unknown source key"      FAIL "$PY" "$VAL" --media-root "$SRC" "$MFX/invalid_pre_grades_key.yaml"

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
{
  echo "manifest validator regression — $(date -Is)"
  printf '%s\n' "${RESULTS[@]}"
} > "$W/manifest_tests.log"

exit "$FAIL"
