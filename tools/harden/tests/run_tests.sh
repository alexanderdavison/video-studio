#!/bin/bash
# run_tests.sh — Phase 1 regression suite (2026-08-26 master scope, Phase 1).
# Each gate ships with its regression fixture. Exit 0 only if ALL pass.
set -u
cd "$(dirname "$0")"
source ../lib/harden.sh
LINT="python3 ../lint/ffmpeg_lint.py"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1"; }
expect_rc() { # expect_rc <desc> <want_rc> <got_rc>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want rc=$2 got rc=$3)"; fi
}

echo "=== generating fixtures ==="
bash fixtures.sh >/dev/null 2>&1 || { echo "FIXTURE GEN FAILED"; exit 2; }

echo "=== Gate 3: pre-flight linter ==="
# T01 stream_loop + shortest
echo 'ffmpeg -stream_loop -1 -i loop.mp4 -c:v libx264 -shortest out.mp4' | $LINT >/dev/null 2>&1
expect_rc "T01 linter flags -stream_loop -1 + -shortest" 1 $?
# T02 vaapi + multi-input
echo 'ffmpeg -i a.mp4 -i b.mp4 -filter_complex "concat=n=2:v=1:a=1" -c:v h264_vaapi out.mp4' | $LINT >/dev/null 2>&1
expect_rc "T02 linter flags h264_vaapi + multi-segment concat" 1 $?
# T03 copy + pix_fmt
echo 'ffmpeg -i a.mp4 -c:v copy -pix_fmt yuv420p out.mp4' | $LINT >/dev/null 2>&1
expect_rc "T03 linter flags -c:v copy + -pix_fmt" 1 $?
# T04 stream label mismatch
echo 'ffmpeg -i a.mp4 -filter_complex "[2:v]scale=100:100[o]" -map "[o]" out.mp4' | $LINT >/dev/null 2>&1
expect_rc "T04 linter flags [2:v] with 1 input" 1 $?
# T05 concat without uniform flags
echo 'ffmpeg -f concat -safe 0 -i list.txt -c copy out.mp4' | $LINT >/dev/null 2>&1
expect_rc "T05 linter flags concat without settb=AVTB" 1 $?
# T06 clean command
echo 'ffmpeg -i a.mp4 -vf "fps=30000/1001,settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p" -c:v libx264 out.mp4' | $LINT >/dev/null 2>&1
expect_rc "T06 linter passes clean command" 0 $?

echo "=== Gate 1: stage verification ==="
# T07 good file (seg_b has no stderr log)
{ harden_verify_stage fixtures/seg_b.mp4 2; } >/dev/null 2>&1
expect_rc "T07 verify_stage passes good file (moov+duration)" 0 $?
# T08 truncated file
{ harden_verify_stage fixtures/truncated.mp4 2; } >/dev/null 2>&1
expect_rc "T08 verify_stage fails truncated file (no moov)" 1 $?
# T09 fatal stderr pattern (seg_a.stderr.log carries "Error reinitializing filters")
{ harden_verify_stage fixtures/seg_a.mp4 2; } >/dev/null 2>&1
expect_rc "T09 verify_stage fails on fatal stderr pattern" 1 $?

echo "=== Gate 5: render lock ==="
# T10 second render refused
HARDEN_LOCKFILE=/tmp/harden-test.lock
rm -f "$HARDEN_LOCKFILE"
harden_lock_acquire test-one >/dev/null 2>&1
{ harden_lock_acquire test-two; } >/dev/null 2>&1
expect_rc "T10 second concurrent render refused" 1 $?
exec 9>&-; rm -f "$HARDEN_LOCKFILE"

echo "=== Gate: atomic final + ALLDONE-only-from-postflight ==="
# T11 atomic final: verified partial → rename; then refuse overwrite
rm -f /tmp/harden-partial.mp4 /tmp/harden-final.mp4
cp fixtures/seg_a.mp4 /tmp/harden-partial.mp4
{ harden_atomic_final /tmp/harden-partial.mp4 /tmp/harden-final.mp4 2; } >/dev/null 2>&1
expect_rc "T11a atomic_final verifies + renames .partial" 0 $?
cp fixtures/seg_b.mp4 /tmp/harden-partial.mp4
{ harden_atomic_final /tmp/harden-partial.mp4 /tmp/harden-final.mp4 2; } >/dev/null 2>&1
expect_rc "T11b atomic_final refuses to clobber verified final" 1 $?
# T12 postflight: verified final → ALLDONE; truncated final → no ALLDONE
rm -f /tmp/alldone.log
{ harden_postflight /tmp/harden-final.mp4 /tmp/alldone.log 2; } >/dev/null 2>&1
[ -f /tmp/alldone.log ] && grep -q ALLDONE /tmp/alldone.log
expect_rc "T12a postflight writes ALLDONE after verify" 0 $?
rm -f /tmp/alldone.log
{ harden_postflight fixtures/truncated.mp4 /tmp/alldone.log 2; } >/dev/null 2>&1
[ ! -f /tmp/alldone.log ]
expect_rc "T12b postflight refuses ALLDONE on unverified final" 0 $?

echo "=== Gate: full-program loudness + TP ==="
# T13 loudness ok (≈ -13.5) vs bad (≈ -3.9)
{ harden_loudness fixtures/loud_ok.m4a -13.5 -1.5; } >/dev/null 2>&1
expect_rc "T13a loudness gate passes ≈-13.5 LUFS target" 0 $?
{ harden_loudness fixtures/loud_bad.m4a -13.5 -1.5; } >/dev/null 2>&1
expect_rc "T13b loudness gate fails too-loud program" 1 $?

echo "=== Gate: BT.709 tags ==="
{ harden_bt709 fixtures/bt709_ok.mp4; } >/dev/null 2>&1
expect_rc "T14a BT.709 tag check passes tagged encode" 0 $?
{ harden_bt709 fixtures/bt709_bad.mp4; } >/dev/null 2>&1
expect_rc "T14b BT.709 tag check fails untagged encode" 1 $?

echo "=== Gate 2: ready-flag ==="
# T15 blocked without flag, advances with flag
rm -rf /tmp/harden-proj && mkdir -p /tmp/harden-proj
{ harden_ready_flag_required /tmp/harden-proj; } >/dev/null 2>&1
expect_rc "T15a ready-flag blocks proposed job without flag" 1 $?
touch /tmp/harden-proj/READY_TO_BAKE
{ harden_ready_flag_required /tmp/harden-proj; } >/dev/null 2>&1
expect_rc "T15b ready-flag advances with READY_TO_BAKE" 0 $?

echo
echo "=== SUMMARY: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
