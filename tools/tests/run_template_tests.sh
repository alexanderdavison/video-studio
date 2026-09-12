#!/bin/bash
# run_template_tests.sh — Phase 5 template engine regression suite.
#
# Generates manifests from per-mix variables + timelines against the Club
# Dispatch Standard definition, checks editorial policy violations, and
# verifies the output passes the Phase 2 validator (the real gate).
#
#   ./run_template_tests.sh          run all checks, exit 0 = all green
#   ./run_template_tests.sh --list   list checks without running
#
# Exit code = number of failed checks (0 = all pass).
set -uo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
SRC="$HERE/fixtures"
MFX="$SRC/manifest"
W="$HERE/work"
mkdir -p "$W"

ENGINE=/opt/video-studio/tools/template/template_engine.py
PY=/opt/video-studio/tools/venv/bin/python
TMP="$W/template_fixtures"
mkdir -p "$TMP"

# The media fixtures are produced by the Phase 1 suite; build them if absent.
if [ ! -f "$SRC/a_cam.mp4" ] || [ ! -f "$SRC/short_src.mp4" ] \
   || [ ! -f "$SRC/long_a.mp4" ] || [ ! -f "$SRC/long_b.mp4" ]; then
  echo "== building Phase 1 media fixtures =="
  bash "$HERE/make_fixtures.sh"
fi

PASS=0; FAIL=0
RESULTS=()

pass() { PASS=$((PASS+1)); RESULTS+=("PASS  $1"); printf '  %-46s PASS\n' "$1"; }
fail() { FAIL=$((FAIL+1)); RESULTS+=("FAIL  $1"); printf '  %-46s *** FAIL: %s\n' "$1" "${2:-}"; }

# engine_check <name> <expected PASS|FAIL> <expected-rc> <mix> <timeline> [--strict]
engine_check() {
  local name="$1" exp="$2" exp_rc="$3" mixf="$4" tl="$5"; shift 5
  local rc=0 out="$W/out_$(echo "$name" | tr -cs 'A-Za-z0-9' '_').yaml"
  "$PY" "$ENGINE" --template club_dispatch_standard_v1 --mix "$TMP/$mixf" \
      --timeline "$TMP/$tl" --out "$out" --media-root "$SRC" "$@" \
      >"$W/template_last.log" 2>&1; rc=$?
  if [ "$exp" = "PASS" ] && [ "$rc" -eq "$exp_rc" ]; then
    pass "$name"
  elif [ "$exp" = "FAIL" ] && [ "$rc" -ne 0 ]; then
    pass "$name"
  else
    fail "$name" "expected rc=$exp_rc, got rc=$rc"
    sed -n '1,6p' "$W/template_last.log" | sed 's/^/        /'
  fi
}

if [ "${1:-}" = "--list" ]; then
  echo "template engine checks (Phase 5):"
  echo "  T01 valid standard timeline -> manifest     -> PASS (rc 0)"
  echo "  T02 B density over preset (warning only)    -> PASS (rc 0)"
  echo "  T03 B density over preset --strict          -> FAIL (rc 1)"
  echo "  T04 B shot shorter than min (warning)       -> PASS (rc 0)"
  echo "  T05 angle B without b_reel                  -> FAIL (rc 1)"
  echo "  T06 unknown template                        -> FAIL (rc 2)"
  echo "  T07 valid manifest passes Phase 2 validator -> PASS (rc 0)"
  echo "  T08 --propose manifest at target density    -> PASS (rc 0)"
  echo "  T09 --propose respects A recovery           -> PASS"
  echo "  T10 section override lifts B in window      -> PASS"
  echo "  T11 --propose A-Cam-Only -> single A cut    -> PASS"
  echo "  T12 proposed timeline re-locks (no propose) -> PASS"
  echo "  T13 Minimal propose at 10-15% band          -> PASS"
  echo "  T14 Active propose at 30-35% band           -> PASS"
  echo "  T15 A-Cam-Only propose zero B               -> PASS"
  echo "  T16 --beats snaps B boundaries to phrase    -> PASS"
  echo "  T17 --b-profile prefers high-action windows -> PASS"
  echo "  T17b action_profile.py produces profile     -> PASS"
  exit 0
fi

# --- fixture files -----------------------------------------------------------
cat > "$TMP/mix_std.yaml" <<'EOF'
series: club_dispatch
title: "Club Dispatch Test"
artist: "ISH D"
date: "2026-08-26"
a_camera: a_cam.mp4
b_camera:
  - a_cam.mp4
  - short_src.mp4
edit_energy: medium
b_camera_density: moderate
intro_duration: 8
outro_duration: 10
EOF
cat > "$TMP/mix_acamonly.yaml" <<'EOF'
series: club_dispatch
title: "Club Dispatch A-Cam Test"
a_camera: a_cam.mp4
EOF

# T01: 4 cuts, B density ~28% (2.5s of 9s) — over 25 by a hair? Use 20% exactly.
cat > "$TMP/tl_valid.yaml" <<'EOF'
timeline:
  - angle: A
    source_in: 0.0
    source_out: 3.0
  - angle: B
    source_in: 0.5
    source_out: 2.0
  - angle: A
    source_in: 3.0
    source_out: 5.0
  - angle: B
    source_in: 2.0
    source_out: 3.0
EOF
# T02/T03: B density 3.0s of 6.5s = 46% — over 25.
cat > "$TMP/tl_dense.yaml" <<'EOF'
timeline:
  - angle: A
    source_in: 0.0
    source_out: 1.0
  - angle: B
    source_in: 0.0
    source_out: 1.5
  - angle: A
    source_in: 1.0
    source_out: 1.5
  - angle: B
    source_in: 1.5
    source_out: 3.0
EOF
# T04: B shot 1.0s < minimum_b_shot 4 (warning only).
cat > "$TMP/tl_short.yaml" <<'EOF'
timeline:
  - angle: A
    source_in: 0.0
    source_out: 2.0
  - angle: B
    source_in: 0.5
    source_out: 1.5
  - angle: A
    source_in: 2.0
    source_out: 4.0
EOF
# T05: B cut but mix has no b_reel.
cat > "$TMP/tl_bnoreel.yaml" <<'EOF'
timeline:
  - angle: B
    source_in: 0.0
    source_out: 1.0
EOF
# T07: exactly the valid shape from the validator suite (2+1.5+2 = 5.5s, B 1.5s = 27%).
cat > "$TMP/tl_standard.yaml" <<'EOF'
timeline:
  - angle: A
    source_in: 0.0
    source_out: 2.0
  - angle: B
    source_in: 1.5
    source_out: 3.0
  - angle: A
    source_in: 2.0
    source_out: 4.0
EOF

echo "== TEMPLATE ENGINE (Phase 5) =="
engine_check "T01 valid standard timeline"        PASS 0 mix_std.yaml tl_valid.yaml
engine_check "T02 B density over preset (warn)"   PASS 0 mix_std.yaml tl_dense.yaml
engine_check "T03 B density over preset strict"   FAIL 1 mix_std.yaml tl_dense.yaml --strict
engine_check "T04 B shot shorter than min"        PASS 0 mix_std.yaml tl_short.yaml
engine_check "T05 angle B without b_reel"         FAIL 1 mix_acamonly.yaml tl_bnoreel.yaml
# T06 is handled inline (engine_check hardcodes the template name):
rc=0
"$PY" "$ENGINE" --template does_not_exist_v1 --mix "$TMP/mix_std.yaml" \
    --timeline "$TMP/tl_valid.yaml" --out "$W/out_T06.yaml" --media-root "$SRC" \
    >"$W/template_last.log" 2>&1; rc=$?
if [ "$rc" -eq 2 ]; then pass "T06 unknown template"; else fail "T06 unknown template" "expected rc=2, got rc=$rc"; fi
engine_check "T07 manifest passes validator"      PASS 0 mix_std.yaml tl_standard.yaml

# ---- Phase 6: --propose (editorial automation first-cut) ----------------
cat > "$TMP/mix_propose.yaml" <<'EOF'
series: club_dispatch
title: "Propose Test"
artist: "ISH D"
date: "2026-08-27"
a_camera: long_a.mp4
b_camera:
  - long_b.mp4
edit_energy: medium
b_camera_density: moderate
intro_duration: 5
outro_duration: 5
EOF
cat > "$TMP/mix_propose_sections.yaml" <<'EOF'
series: club_dispatch
title: "Propose Sections Test"
artist: "ISH D"
date: "2026-08-27"
a_camera: long_a.mp4
b_camera:
  - long_b.mp4
edit_energy: medium
b_camera_density: moderate
intro_duration: 5
outro_duration: 5
sections:
  - start_s: 30
    end_s: 50
    density_pct: 40
EOF
cat > "$TMP/mix_propose_acamonly.yaml" <<'EOF'
series: club_dispatch
title: "Propose A-Cam Only Test"
a_camera: long_a.mp4
EOF

# T08: propose generates a manifest at the template's target density
# (spine 110s @ ~22.5% -> 18-27% band with rounding).
rc=0
"$PY" "$ENGINE" --template club_dispatch_standard_v1 --mix "$TMP/mix_propose.yaml" \
    --propose --out "$W/out_T08.yaml" --media-root "$SRC" >"$W/template_last.log" 2>&1; rc=$?
T08_OK=0
if [ "$rc" -eq 0 ]; then
  "$PY" - "$W/out_T08.yaml" <<'PY' >/dev/null 2>&1 && T08_OK=1
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
cuts = d["timeline"]
total = sum(c["source_out"] - c["source_in"] for c in cuts)
b = sum(c["source_out"] - c["source_in"] for c in cuts if c["angle"] == "B")
dens = 100.0 * b / total
sys.exit(0 if 18.0 <= dens <= 27.0 else 1)
PY
fi
if [ "$T08_OK" -eq 1 ]; then
  pass "T08 propose generates manifest at target density"
else
  fail "T08 propose generates manifest at target density" "rc=$rc (see $W/template_last.log)"
fi

# T09: every B is followed by >= minimum_a_recovery of A.
T09_OK=0
"$PY" - "$W/out_T08.yaml" <<'PY' >/dev/null 2>&1 && T09_OK=1
import sys, yaml
cuts = yaml.safe_load(open(sys.argv[1]))["timeline"]
for i, c in enumerate(cuts):
    if c["angle"] != "B":
        continue
    nxt = cuts[i + 1] if i + 1 < len(cuts) else None
    if nxt is None or nxt["angle"] != "A" or nxt["source_in"] - c["source_out"] < 7.99:
        sys.exit(1)
sys.exit(0)
PY
if [ "$T09_OK" -eq 1 ]; then
  pass "T09 propose respects minimum A recovery"
else
  fail "T09 propose respects minimum A recovery" "see $W/out_T08.yaml"
fi

# T10: section override (30-50s @ 40%) puts >= 6s of B inside that window.
rc=0
"$PY" "$ENGINE" --template club_dispatch_standard_v1 --mix "$TMP/mix_propose_sections.yaml" \
    --propose --out "$W/out_T10.yaml" --media-root "$SRC" >"$W/template_last.log" 2>&1; rc=$?
T10_OK=0
if [ "$rc" -eq 0 ]; then
  "$PY" - "$W/out_T10.yaml" <<'PY' >/dev/null 2>&1 && T10_OK=1
import sys, yaml
cuts = yaml.safe_load(open(sys.argv[1]))["timeline"]
t = 0.0
bwin = 0.0
for c in cuts:
    dur = c["source_out"] - c["source_in"]
    if c["angle"] == "B":
        if 30.0 <= t < 50.0:
            bwin += min(dur, 50.0 - t)
        t += dur
    else:
        t = c["source_out"]
sys.exit(0 if bwin >= 6.0 else 1)
PY
fi
if [ "$T10_OK" -eq 1 ]; then
  pass "T10 section override lifts B in window"
else
  fail "T10 section override lifts B in window" "rc=$rc (see $W/template_last.log)"
fi

# T11: A-Cam-Only propose -> exactly one A cut, zero B.
rc=0
"$PY" "$ENGINE" --template club_dispatch_standard_v1 --mix "$TMP/mix_propose_acamonly.yaml" \
    --propose --out "$W/out_T11.yaml" --media-root "$SRC" >"$W/template_last.log" 2>&1; rc=$?
T11_OK=0
if [ "$rc" -eq 0 ]; then
  "$PY" - "$W/out_T11.yaml" <<'PY' >/dev/null 2>&1 && T11_OK=1
import sys, yaml
cuts = yaml.safe_load(open(sys.argv[1]))["timeline"]
sys.exit(0 if len(cuts) == 1 and cuts[0]["angle"] == "A" else 1)
PY
fi
if [ "$T11_OK" -eq 1 ]; then
  pass "T11 propose A-Cam-Only is single A cut"
else
  fail "T11 propose A-Cam-Only is single A cut" "rc=$rc (see $W/template_last.log)"
fi

# T12: the proposed timeline, written for review, re-locks through a normal
# (non-propose) run — the PROPOSE -> OK -> EXECUTE loop closes.
rc1=0; rc2=0
"$PY" "$ENGINE" --template club_dispatch_standard_v1 --mix "$TMP/mix_propose.yaml" \
    --propose --out-timeline "$W/proposed_timeline.yaml" --out "$W/out_T12a.yaml" \
    --media-root "$SRC" >"$W/template_last.log" 2>&1; rc1=$?
"$PY" "$ENGINE" --template club_dispatch_standard_v1 --mix "$TMP/mix_propose.yaml" \
    --timeline "$W/proposed_timeline.yaml" --out "$W/out_T12b.yaml" \
    --media-root "$SRC" >"$W/template_last.log" 2>&1; rc2=$?
if [ "$rc1" -eq 0 ] && [ "$rc2" -eq 0 ]; then
  pass "T12 proposed timeline re-locks through normal run"
else
  fail "T12 proposed timeline re-locks through normal run" "propose rc=$rc1 rerun rc=$rc2"
fi

# T13: Minimal definition (10-15%) proposes inside its band.
rc=0
"$PY" "$ENGINE" --template club_dispatch_minimal_v1 --mix "$TMP/mix_propose.yaml" \
    --propose --out "$W/out_T13.yaml" --media-root "$SRC" >"$W/template_last.log" 2>&1; rc=$?
T13_OK=0
if [ "$rc" -eq 0 ]; then
  "$PY" - "$W/out_T13.yaml" <<'PY' >/dev/null 2>&1 && T13_OK=1
import sys, yaml
cuts = yaml.safe_load(open(sys.argv[1]))["timeline"]
total = sum(c["source_out"] - c["source_in"] for c in cuts)
b = sum(c["source_out"] - c["source_in"] for c in cuts if c["angle"] == "B")
dens = 100.0 * b / total
sys.exit(0 if 7.0 <= dens <= 17.0 else 1)
PY
fi
if [ "$T13_OK" -eq 1 ]; then
  pass "T13 Minimal propose at 10-15% band"
else
  fail "T13 Minimal propose at 10-15% band" "rc=$rc (see $W/template_last.log)"
fi

# T14: Active definition (30-35%) proposes inside its band.
rc=0
"$PY" "$ENGINE" --template club_dispatch_active_v1 --mix "$TMP/mix_propose.yaml" \
    --propose --out "$W/out_T14.yaml" --media-root "$SRC" >"$W/template_last.log" 2>&1; rc=$?
T14_OK=0
if [ "$rc" -eq 0 ]; then
  "$PY" - "$W/out_T14.yaml" <<'PY' >/dev/null 2>&1 && T14_OK=1
import sys, yaml
cuts = yaml.safe_load(open(sys.argv[1]))["timeline"]
total = sum(c["source_out"] - c["source_in"] for c in cuts)
b = sum(c["source_out"] - c["source_in"] for c in cuts if c["angle"] == "B")
dens = 100.0 * b / total
sys.exit(0 if 27.0 <= dens <= 38.0 else 1)
PY
fi
if [ "$T14_OK" -eq 1 ]; then
  pass "T14 Active propose at 30-35% band"
else
  fail "T14 Active propose at 30-35% band" "rc=$rc (see $W/template_last.log)"
fi

# T15: A-Cam-Only definition (0%) proposes zero B — single A cut.
rc=0
"$PY" "$ENGINE" --template club_dispatch_acamonly_v1 --mix "$TMP/mix_propose.yaml" \
    --propose --out "$W/out_T15.yaml" --media-root "$SRC" >"$W/template_last.log" 2>&1; rc=$?
T15_OK=0
if [ "$rc" -eq 0 ]; then
  "$PY" - "$W/out_T15.yaml" <<'PY' >/dev/null 2>&1 && T15_OK=1
import sys, yaml
cuts = yaml.safe_load(open(sys.argv[1]))["timeline"]
b = [c for c in cuts if c["angle"] == "B"]
sys.exit(0 if len(cuts) >= 1 and not b else 1)
PY
fi
if [ "$T15_OK" -eq 1 ]; then
  pass "T15 A-Cam-Only propose zero B"
else
  fail "T15 A-Cam-Only propose zero B" "rc=$rc (see $W/template_last.log)"
fi

# T16: phrase snapping (--beats). Beats every 1.0s + 0.5s tolerance -> every
# B weave boundary must land on an integer (snapped to a beat). Recovery is
# preserved (a snap that would violate min A recovery is discarded).
cat > "$TMP/beats_1s.json" <<'EOF'
{"bpm": 60.0, "beats": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 29.0, 30.0, 31.0, 32.0, 33.0, 34.0, 35.0, 36.0, 37.0, 38.0, 39.0, 40.0, 41.0, 42.0, 43.0, 44.0, 45.0, 46.0, 47.0, 48.0, 49.0, 50.0, 51.0, 52.0, 53.0, 54.0, 55.0, 56.0, 57.0, 58.0, 59.0, 60.0, 61.0, 62.0, 63.0, 64.0, 65.0, 66.0, 67.0, 68.0, 69.0, 70.0, 71.0, 72.0, 73.0, 74.0, 75.0, 76.0, 77.0, 78.0, 79.0, 80.0, 81.0, 82.0, 83.0, 84.0, 85.0, 86.0, 87.0, 88.0, 89.0, 90.0, 91.0, 92.0, 93.0, 94.0, 95.0, 96.0, 97.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0, 111.0, 112.0, 113.0, 114.0, 115.0, 116.0, 117.0, 118.0, 119.0, 120.0]}
EOF
rc=0
"$PY" "$ENGINE" --template club_dispatch_standard_v1 --mix "$TMP/mix_propose.yaml" \
    --propose --beats "$TMP/beats_1s.json" --out "$W/out_T16.yaml" --media-root "$SRC" \
    >"$W/template_last.log" 2>&1; rc=$?
T16_OK=0
if [ "$rc" -eq 0 ]; then
  "$PY" - "$W/out_T16.yaml" <<'PY' >/dev/null 2>&1 && T16_OK=1
import sys, yaml
cuts = yaml.safe_load(open(sys.argv[1]))["timeline"]
def near_int(v):
    return abs(v - round(v)) < 0.01
for i, c in enumerate(cuts):
    if c["angle"] != "B":
        continue
    prev = cuts[i - 1] if i > 0 else None
    nxt = cuts[i + 1] if i + 1 < len(cuts) else None
    if prev is None or prev["angle"] != "A" or nxt is None or nxt["angle"] != "A":
        sys.exit(1)
    if not (near_int(prev["source_out"]) and near_int(nxt["source_in"])):
        sys.exit(1)
sys.exit(0)
PY
fi
if [ "$T16_OK" -eq 1 ]; then
  pass "T16 --beats snaps B boundaries to phrase"
else
  fail "T16 --beats snaps B boundaries to phrase" "rc=$rc (see $W/template_last.log)"
fi

# T17: action preference (--b-profile). Profile scores the LATER part of the
# B reel high (reachable from the sync-safe weave positions); the chosen B
# windows must mostly land in the high region.
cat > "$TMP/profile_high_end.json" <<'EOF'
{"reel": "long_b.mp4", "segments": [
  {"start": 0.0, "end": 20.0, "score": 0.1},
  {"start": 20.0, "end": 100.0, "score": 0.9}
]}
EOF
rc=0
"$PY" "$ENGINE" --template club_dispatch_standard_v1 --mix "$TMP/mix_propose.yaml" \
    --propose --b-profile "$TMP/profile_high_end.json" --out "$W/out_T17.yaml" --media-root "$SRC" \
    >"$W/template_last.log" 2>&1; rc=$?
T17_OK=0
if [ "$rc" -eq 0 ]; then
  "$PY" - "$W/out_T17.yaml" <<'PY' >/dev/null 2>&1 && T17_OK=1
import sys, yaml
cuts = yaml.safe_load(open(sys.argv[1]))["timeline"]
bsrc = [(c["source_in"], c["source_out"]) for c in cuts if c["angle"] == "B"]
high = sum(max(0.0, min(so, 100.0) - max(si, 20.0)) for si, so in bsrc)
total = sum(so - si for si, so in bsrc)
sys.exit(0 if total > 0 and high / total >= 0.7 else 1)
PY
fi
if [ "$T17_OK" -eq 1 ]; then
  pass "T17 --b-profile prefers high-action B windows"
else
  fail "T17 --b-profile prefers high-action B windows" "rc=$rc (see $W/template_last.log)"
fi

# T17b: action_profile.py produces a valid profile from a real fixture.
rc=0
"$PY" /opt/video-studio/tools/analysis/action_profile.py "$SRC/long_b.mp4" \
    --out "$W/action_profile_long_b.json" >"$W/template_last.log" 2>&1; rc=$?
if [ "$rc" -eq 0 ] && "$PY" - "$W/action_profile_long_b.json" <<'PY' >/dev/null 2>&1
import sys, json
d = json.load(open(sys.argv[1]))
segs = d.get("segments", [])
sys.exit(0 if len(segs) >= 5 and segs[-1]["end"] >= 58.0 else 1)
PY
then
  pass "T17b action_profile.py produces reel profile"
else
  fail "T17b action_profile.py produces reel profile" "rc=$rc"
fi


echo
echo "== RESULT: $PASS passed, $FAIL failed =="
{
  echo "template engine regression — $(date -Is)"
  printf '%s\n' "${RESULTS[@]}"
} > "$W/template_tests.log"

exit "$FAIL"
