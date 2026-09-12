#!/bin/bash
# run_regression.sh — Phase 1 hardening regression suite.
#
# Maps the 13 postmortem messups (2026-08-26 tester) to enforced gates and
# proves each gate with a tiny synthetic fixture: broken commands MUST fail,
# clean commands MUST pass. Runs on the studio or any host with ffmpeg.
#
#   ./run_regression.sh          run all checks, exit 0 = all green
#   ./run_regression.sh --list   list checks without running
#
# Exit code = number of failed checks (0 = all pass).
set -uo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
HARDENING_LIB="$HERE/../lib/hardening.sh"
LINT="$HERE/../bin/ffmpeg_lint.py"
FX="$HERE/fixtures"
W="$HERE/work"
mkdir -p "$W"

source "$HARDENING_LIB"
# point the lock at a TEST lockfile — never the production render lock
TEST_LOCK="$W/render.lock"
HARDENING_LOCKFILE="$TEST_LOCK"

PASS=0; FAIL=0
RESULTS=()

pass() { PASS=$((PASS+1)); RESULTS+=("PASS  $1"); printf '  %-44s PASS\n' "$1"; }
fail() { FAIL=$((FAIL+1)); RESULTS+=("FAIL  $1"); printf '  %-44s *** FAIL: %s\n' "$1" "${2:-}"; }

# run_check <name> <expected PASS|FAIL> <cmd...>
run_check() {
  local name="$1" exp="$2"; shift 2
  local rc=0
  "$@" >"$W/last.log" 2>&1; rc=$?
  if [ "$exp" = "PASS" ] && [ "$rc" -eq 0 ]; then
    pass "$name"
  elif [ "$exp" = "FAIL" ] && [ "$rc" -ne 0 ]; then
    pass "$name"
  else
    fail "$name" "expected $exp, got rc=$rc"
    sed -n '1,5p' "$W/last.log" | sed 's/^/        /'
  fi
}

echo "== LINTER (D3) — dead patterns must be blocked =="
run_check "L01 stream_loop+-shortest (#1)"            FAIL python3 "$LINT" -stream_loop -1 -i x.mp4 -c copy -shortest out.mp4
run_check "L02 vaapi+concat mixed (#2)"               FAIL python3 "$LINT" -vaapi_device /dev/dri/renderD128 -i a.mp4 -i b.mp4 -f concat -safe 0 -i "$FX/nonunif.txt" -c:v h264_vaapi out.mp4
run_check "L03 stream label mismatch (#12)"           FAIL python3 "$LINT" -i a.mp4 -filter_complex "[2:v]scale=640:360[v]" -map "[v]" out.mp4
run_check "L04 -c copy + -pix_fmt"                    FAIL python3 "$LINT" -i a.mp4 -c:v copy -pix_fmt yuv420p out.mp4
run_check "L05 concat -c copy non-uniform"            FAIL python3 "$LINT" -f concat -safe 0 -i "$FX/nonunif.txt" -c copy out.mp4
run_check "L05b concat -c copy uniform"               PASS python3 "$LINT" -f concat -safe 0 -i "$FX/unif.txt" -c copy out.mp4
run_check "L06 scale_vaapi unsupported"               FAIL python3 "$LINT" -i a.mp4 -vf "scale_vaapi=w=1920:h=1080" -c:v h264_vaapi out.mp4
run_check "L07 alimiter=limit=0.95 (locked chain)"    FAIL python3 "$LINT" -i a.mp4 -filter_complex "[1:a]loudnorm=I=-13.5,alimiter=limit=0.95:level=false[a]" -map "[a]" out.mp4
run_check "L08 AAC 192k on delivery mux"              FAIL python3 "$LINT" -i a.mp4 -c:v libx264 -c:a aac -b:a 192k -movflags +faststart out.mp4
run_check "L08b AAC 320k on delivery mux"             PASS python3 "$LINT" -i a.mp4 -c:v libx264 -c:a aac -b:a 320k -movflags +faststart out.mp4
run_check "L09 clean command"                         PASS python3 "$LINT" -i a.mp4 -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 320k out.mp4

echo "== STAGE VERIFY (D1) =="
run_check "S01 verify good duration"                  PASS verify_stage "$FX/good.mp4" 4
run_check "S02 verify duration mismatch (76s-short #8)" FAIL verify_stage "$FX/good.mp4" 99
cp /dev/null "$W/garbage.bin"
head -c 65536 /dev/urandom >> "$W/garbage.bin"
run_check "S03 verify no-moov garbage (#7/#13)"       FAIL verify_stage "$W/garbage.bin" 4
printf 'Conversion failed: something\n' > "$W/bad.stderr.log"
run_check "S04 verify fatal stderr pattern (#6)"      FAIL verify_stage "$FX/good.mp4" 4 "$W/bad.stderr.log"

echo "== SPAN VALIDATION (b8 truncation shape #8) =="
run_check "SP01 span fits"                            PASS validate_span "$FX/a_cam.mp4" 0 3
run_check "SP02 span fits exactly"                    PASS validate_span "$FX/short_src.mp4" 0 3
run_check "SP03 span overflows source"                FAIL validate_span "$FX/short_src.mp4" 1 3
run_check "SP04 b8 shape: 100s from 3s src"           FAIL validate_span "$FX/short_src.mp4" 2.5 100

echo "== INPUT HASHES (stable-upload + verified-handle) =="
cp "$FX/a_cam.mp4" "$W/hash_src.mp4"
run_check "H01 record+verify unchanged"               PASS bash -c "source '$HARDENING_LIB'; record_input_hashes '$W/hashes.txt' '$W/hash_src.mp4'; verify_input_hashes '$W/hashes.txt' '$W/hash_src.mp4'"
printf 'X' >> "$W/hash_src.mp4"
run_check "H02 verify detects modified input"         FAIL bash -c "source '$HARDENING_LIB'; verify_input_hashes '$W/hashes.txt' '$W/hash_src.mp4'"
run_check "H03 verify without record file"            FAIL bash -c "source '$HARDENING_LIB'; verify_input_hashes '$W/none.txt' '$W/hash_src.mp4'"

echo "== RENDER LOCK (D5b flock) =="
( source "$HARDENING_LIB"; HARDENING_LOCKFILE="$TEST_LOCK"; acquire_render_lock; sleep 1.2 ) &
BG=$!
sleep 0.3
if acquire_render_lock; then
  fail "LK01 flock refuses concurrent render" "second acquire succeeded while bg held lock"
  release_render_lock
else
  pass "LK01 flock refuses concurrent render"
fi
wait "$BG" 2>/dev/null || true
if acquire_render_lock; then
  pass "LK02 acquire after holder exit"
  release_render_lock
else
  fail "LK02 acquire after holder exit" "lock not released after holder died"
fi
echo "99999" > "$TEST_LOCK"   # stale pid content, no live flock holder
if acquire_render_lock; then
  pass "LK03 stale lockfile (dead pid) taken over"
  release_render_lock
else
  fail "LK03 stale lockfile taken over" "refused despite no live holder"
fi

echo "== ATOMIC PUBLISH (never-clobber) =="
head -c 131072 /dev/urandom > "$W/bad.partial"
run_check "PB01 publish unverified (no moov)"         FAIL publish_partial "$W/bad.partial" "$W/final.mp4"
# verified final exists -> refuse
cp "$FX/good.mp4" "$W/verified_final.mp4"
touch "$W/verified_final.mp4.verified"
cp "$FX/good.mp4" "$W/new.partial"
run_check "PB02 publish over verified final"          FAIL publish_partial "$W/new.partial" "$W/verified_final.mp4"
run_check "PB03 publish --force parks old"            PASS publish_partial "$W/new.partial" "$W/verified_final.mp4" --force
ls "$W"/verified_final.mp4.prev.* >/dev/null 2>&1 && pass "PB03b old verified parked as .prev" || fail "PB03b" "no .prev parked"
# unverified leftover -> parked, publish proceeds
cp "$FX/good.mp4" "$W/leftover.mp4"
cp "$FX/good.mp4" "$W/leftover.partial"
run_check "PB04 publish over unverified leftover"     PASS publish_partial "$W/leftover.partial" "$W/leftover.mp4"
ls "$W"/leftover.mp4.unverified.* >/dev/null 2>&1 && pass "PB04b leftover parked as .unverified" || fail "PB04b" "no .unverified parked"

echo "== POSTFLIGHT (full-program loudness, BT.709, ALLDONE discipline) =="
cp "$FX/good.mp4" "$W/pf_good.mp4"
run_check "PF01 postflight PASS good"                 PASS postflight_verify "$W/pf_good.mp4" 4
if [ -f "$W/ALLDONE" ] && [ -f "$W/pf_good.mp4.verified" ]; then
  pass "PF01b ALLDONE + .verified written"
  grep -q "LUFS" "$W/ALLDONE" && pass "PF01c ALLDONE carries postflight stamp" || fail "PF01c" "ALLDONE missing stamp"
else
  fail "PF01b" "ALLDONE/.verified missing after PASS"
fi
cp "$FX/untagged.mp4" "$W/pf_untagged.mp4"
run_check "PF02 postflight FAIL no bt709"             FAIL postflight_verify "$W/pf_untagged.mp4" 4
cp "$FX/hot.mp4" "$W/pf_hot.mp4"
run_check "PF03 postflight FAIL loudness"             FAIL postflight_verify "$W/pf_hot.mp4" 4
run_check "PF04 postflight FAIL wrong duration"       FAIL postflight_verify "$W/pf_good.mp4" 99
# ALLDONE discipline: a lie marker must not survive a FAIL
cp "$FX/untagged.mp4" "$W/pf_lie.mp4"
printf 'ALLDONE %s\n' "$(date -Is)" > "$W/ALLDONE"
run_check "PF05 stale ALLDONE cleared on FAIL"        FAIL postflight_verify "$W/pf_lie.mp4" 4
if [ -f "$W/ALLDONE" ]; then
  fail "PF05b" "stale ALLDONE survived a postflight FAIL"
else
  pass "PF05b stale ALLDONE removed by FAIL"
fi

echo
echo "=============================================="
echo "REGRESSION SUMMARY: $PASS passed, $FAIL failed"
echo "=============================================="
exit "$FAIL"
