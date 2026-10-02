#!/bin/bash
# run_proxy_tests.sh — Phase 3 visual approval proxy regression suite.
#
# Renders REAL proxy outputs from tiny synthetic manifests against the Phase 1
# media fixtures (a_cam.mp4 = 6s, short_src.mp4 = 3s) and verifies the result:
# rc 0 + ffprobe duration + video/audio streams (the proxy itself fails loudly
# otherwise). Invalid jobs MUST be refused before any render work.
#
#   ./run_proxy_tests.sh          run all checks, exit 0 = all green
#   ./run_proxy_tests.sh --list   list checks without running
#
# Exit code = number of failed checks (0 = all pass).
set -uo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
SRC="$HERE/fixtures"
MFX="$SRC/manifest"
W="$HERE/work"
mkdir -p "$W"

PROXY=/opt/video-studio/tools/proxy/render_proxy.py
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

# proxy_check <name> <expected PASS|FAIL> <expected-rc> <manifest> [extra args...]
#   expected-rc: the rc the proxy must return for the check to PASS.
proxy_check() {
  local name="$1" exp="$2" exp_rc="$3" mf="$4"; shift 4
  local rc=0 out="$W/out_${name}.mp4"
  rm -f "$out"
  "$PY" "$PROXY" "$MFX/$mf" --media-root "$SRC" --work "$W/proxy_work" \
      --out "$out" "$@" >"$W/proxy_last.log" 2>&1; rc=$?
  if [ "$exp" = "PASS" ] && [ "$rc" -eq "$exp_rc" ] && [ -f "$out" ]; then
    pass "$name"
  elif [ "$exp" = "FAIL" ] && [ "$rc" -ne 0 ]; then
    pass "$name"
  else
    fail "$name" "expected rc=$exp_rc, got rc=$rc"
    sed -n '1,4p' "$W/proxy_last.log" | sed 's/^/        /'
  fi
}

# proxy_dur_check <name> <expected-dur> <manifest> [extra args...]
#   Renders a PASS-expected job, then ffprobes the OUTPUT DURATION and asserts
#   it matches expected (+-1.5s). Used for the half-runtime proof policy.
proxy_dur_check() {
  local name="$1" exp_dur="$2" mf="$3"; shift 3
  local rc=0 dur=""
  local out="$W/out_${name}.mp4"
  rm -f "$out"
  "$PY" "$PROXY" "$MFX/$mf" --media-root "$SRC" --work "$W/proxy_work" \
      --out "$out" "$@" >"$W/proxy_last.log" 2>&1; rc=$?
  if [ "$rc" -ne 0 ]; then
    fail "$name" "expected rc 0, got rc=$rc"
    sed -n '1,4p' "$W/proxy_last.log" | sed 's/^/        /'
    return
  fi
  dur=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$out" 2>/dev/null)
  if [ -z "$dur" ]; then
    fail "$name" "ffprobe could not read output"
    return
  fi
  if awk -v d="$dur" -v e="$exp_dur" 'BEGIN{exit !(d>=e-1.5 && d<=e+1.5)}'; then
    pass "$name"
  else
    fail "$name" "duration $dur != expected $exp_dur (+-1.5s)"
  fi
}

if [ "${1:-}" = "--list" ]; then
  echo "visual proxy checks (Phase 3):"
  echo "  P01 valid standard manifest renders      -> PASS (rc 0)"
  echo "  P02 invalid manifest refused             -> FAIL (rc 1)"
  echo "  P03 cross-file B span renders (split)    -> PASS (rc 0)"
  echo "  P04 A-Cam-Only renders                   -> PASS (rc 0)"
  echo "  P05 confidence marker renders            -> PASS (rc 0)"
  echo "  P06 missing source file refused          -> FAIL (rc 1)"
  echo "  P07 render lock respected                -> FAIL (rc 2)"
  echo "  P08 default proof is HALF runtime        -> PASS (rc 0, dur=total/2)"
  echo "  P09 --full renders FULL runtime          -> PASS (rc 0, dur=total)"
  echo "  P10 --max-duration N trims to N          -> PASS (rc 0, dur=N)"
  echo "  P11 per-angle grade resolves B to curves -> PASS (lookup asserts pixels)"
  echo "  P12 pre_grades lift B pixels             -> PASS (pixel compare)"
  echo "  P13 pre_grades unknown source refused    -> FAIL (rc 1)"
  echo "  P14 auto pre-grade REFUSED for angle B (owns match) -> PASS (pixel equal)"
  echo "  P14b refusal (source + grade) recorded in log -> PASS"
  exit 0
fi

echo "== VISUAL PROXY (Phase 3) =="
proxy_check "P01 valid standard manifest renders"  PASS 0 valid_standard.yaml
proxy_check "P02 invalid manifest refused"         FAIL 1 invalid_b8_span.yaml
proxy_check "P03 cross-file B span renders"        PASS 0 valid_crossfile_span.yaml
proxy_check "P04 A-Cam-Only renders"               PASS 0 valid_acamonly.yaml
proxy_check "P05 confidence marker renders"        PASS 0 valid_confidence.yaml
proxy_check "P06 missing source file refused"      FAIL 1 invalid_missing_source.yaml

# P07: hold the render lock, then expect rc 2 (lock respected).
P07_rc=0
( flock 9; sleep 6 ) 9>/opt/video-studio/.render.lock &
LOCK_PID=$!
sleep 0.5
"$PY" "$PROXY" "$MFX/valid_standard.yaml" --media-root "$SRC" \
    --work "$W/proxy_work" --out "$W/out_P07.mp4" >"$W/proxy_last.log" 2>&1
P07_rc=$?
wait "$LOCK_PID" 2>/dev/null
if [ "$P07_rc" -eq 2 ]; then
  pass "P07 render lock respected"
else
  fail "P07 render lock respected" "expected rc=2 while lock held, got rc=$P07_rc"
  sed -n '1,4p' "$W/proxy_last.log" | sed 's/^/        /'
fi

# P08-P10: half-runtime proof policy (user directive 2026-08-27).
# valid_standard total = 2.0 + 1.5 + 2.0 = 5.5s -> default proof = 2.75s.
proxy_dur_check "P08 default proof is HALF runtime" 2.75 valid_standard.yaml
proxy_dur_check "P09 --full renders FULL runtime"   5.50 valid_standard.yaml --full
proxy_dur_check "P10 --max-duration N trims to N"   2.20 valid_standard.yaml --max-duration 2.2

# P11: per-angle grade resolution (2026-08-27). The B-cam grade is a REAL
# pixel change, not an rc/duration property — a pure lookup test is the only
# thing that catches a naming mismatch (the suite can't see identical pixels).
P11_RES=$("$PY" - "$PROXY" "$MFX/valid_standard.yaml" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("rp", sys.argv[1])
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)
reg = rp.load_registry()
a = rp.grade_for(reg, "club_dispatch_pb3_v1", "A")
b = rp.grade_for(reg, "club_dispatch_pb3_v1", "B")
ok = (a != b
      and "curves=all" in b
      and "rs=0.0:bs=0.0" in b
      and a.startswith("eq=brightness=-0.02:saturation=0.92"))
print("PASS" if ok else "FAIL a=%r b=%r" % (a, b))
PY
)
if [ "$P11_RES" = "PASS" ]; then
  pass "P11 per-angle grade resolves B to curves chain"
else
  fail "P11 per-angle grade resolves B to curves chain" "$P11_RES"
fi

# P12: pre_grades change pixels (Phase 6). The SAME job with and without a
# corrective pre-grade on the B source must render DIFFERENT B frames, and
# the pre-graded frame must be BRIGHTER (the curve lifts). Value-asserting —
# rc alone can't see identical pixels (P11 lesson).
P12_BASE="$W/out_P12_base.mp4"
P12_PRE="$W/out_P12_pre.mp4"
rm -f "$P12_BASE" "$P12_PRE"
"$PY" "$PROXY" "$MFX/valid_standard.yaml" --media-root "$SRC" --work "$W/proxy_work" \
    --out "$P12_BASE" --full >"$W/proxy_last.log" 2>&1
P12_R1=$?
"$PY" "$PROXY" "$MFX/valid_pre_grades.yaml" --media-root "$SRC" --work "$W/proxy_work" \
    --out "$P12_PRE" --full >"$W/proxy_last.log" 2>&1
P12_R2=$?
if [ "$P12_R1" -eq 0 ] && [ "$P12_R2" -eq 0 ]; then
  P12_RES=$("$PY" - "$P12_BASE" "$P12_PRE" <<'PY'
import re, subprocess, sys
base, pre = sys.argv[1], sys.argv[2]
def yavg(path, t):
    r = subprocess.run(["ffmpeg", "-ss", str(t), "-i", path, "-frames:v", "1",
                        "-vf", "signalstats,metadata=print", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"YAVG=([0-9.]+)", r.stderr)
    return float(m.group(1)) if m else None
# B cut is the 2nd segment: abs 2.0-3.5 in the full 5.5s render -> t=2.6
yb = yavg(base, 2.6)
yp = yavg(pre, 2.6)
if yb is None or yp is None:
    print("FAIL no YAVG (base=%s pre=%s)" % (yb, yp))
elif yp > yb + 1.5:
    print("PASS")
else:
    print("FAIL yb=%.2f yp=%.2f (pre not brighter)" % (yb, yp))
PY
)
  if [ "$P12_RES" = "PASS" ]; then
    pass "P12 pre_grades lift B pixels (brighter frame)"
  else
    fail "P12 pre_grades lift B pixels (brighter frame)" "$P12_RES"
  fi
else
  fail "P12 pre_grades lift B pixels (brighter frame)" "base rc=$P12_R1 pre rc=$P12_R2"
fi

# P13: pre_grades key that is not a declared source -> refused before render
proxy_check "P13 pre_grades unknown source refused" FAIL 1 invalid_pre_grades_key.yaml

# P14: the per-angle camera-match OVERRIDE (2026-08-27 decision doc point 4, enforced
# 2026-09-14). valid_dark_b declares grade club_dispatch_pb3_v1, for which the registry
# defines a DEDICATED B camera-match grade (club_dispatch_pb3_b_v1). That chain already IS
# the derived correction, so the auto pre-grade must be REFUSED for the B source —
# applying both lifts B twice (the club-dispatch-set01 blow-out: mean luma 135.5 / p95 204
# with 0.7% new clipping instead of the accepted 28.5 / 76 / 0.01%).
# So the default (auto) render and the --no-auto-pre-grade render must agree on the B cut.
# (The derivation itself is still covered by the exposure suite: E02 dark-lifts, E04
#  manifest --write.)
P14_BASE="$W/out_P14_base.mp4"
P14_AUTO="$W/out_P14_auto.mp4"
rm -f "$P14_BASE" "$P14_AUTO"
"$PY" "$PROXY" "$MFX/valid_dark_b.yaml" --media-root "$SRC" --work "$W/proxy_work" \
    --out "$P14_BASE" --full --no-auto-pre-grade >"$W/P14_base.log" 2>&1
P14_R1=$?
"$PY" "$PROXY" "$MFX/valid_dark_b.yaml" --media-root "$SRC" --work "$W/proxy_work" \
    --out "$P14_AUTO" --full >"$W/proxy_last.log" 2>&1
P14_R2=$?
if [ "$P14_R1" -eq 0 ] && [ "$P14_R2" -eq 0 ]; then
  P14_RES=$("$PY" - "$P14_BASE" "$P14_AUTO" <<'PY'
import re, subprocess, sys
base, auto = sys.argv[1], sys.argv[2]
def yavg(path, t):
    r = subprocess.run(["ffmpeg", "-ss", str(t), "-i", path, "-frames:v", "1",
                        "-vf", "signalstats,metadata=print", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"YAVG=([0-9.]+)", r.stderr)
    return float(m.group(1)) if m else None
# B cut is the 2nd segment: abs 2.0-5.0 in the full 7s render -> t=3.5
yb = yavg(base, 3.5)
ya = yavg(auto, 3.5)
if yb is None or ya is None:
    print("FAIL no YAVG (base=%s auto=%s)" % (yb, ya))
elif abs(ya - yb) <= 0.5:
    print("PASS")
else:
    print("FAIL yb=%.2f ya=%.2f (auto pre-grade WAS applied to a B source that owns its match)"
          % (yb, ya))
PY
)
  if [ "$P14_RES" = "PASS" ]; then
    pass "P14 auto pre-grade refused for the B angle that owns a match"
  else
    fail "P14 auto pre-grade refused for the B angle that owns a match" "$P14_RES"
  fi
else
  fail "P14 auto pre-grade refused for the B angle that owns a match" \
       "base rc=$P14_R1 auto rc=$P14_R2"
fi

# P14b: the refusal is auditable — the render log says which source and which grade.
if grep -q "AUTO PRE-GRADE SKIPPED" "$W/proxy_last.log" \
   && grep -q "club_dispatch_pb3_b_v1" "$W/proxy_last.log"; then
  pass "P14b refusal (source + grade) recorded in the render log"
else
  fail "P14b refusal (source + grade) recorded in the render log" \
       "$(grep -i "PRE-GRADE" "$W/proxy_last.log" | head -2)"
fi

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
{
  echo "visual proxy regression — $(date -Is)"
  printf '%s\n' "${RESULTS[@]}"
} > "$W/proxy_tests.log"

exit "$FAIL"
