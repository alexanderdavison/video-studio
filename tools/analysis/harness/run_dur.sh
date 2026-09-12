#!/bin/bash
# Regression + new-run driver. Runs candidate_score.py twice per mode.
set -u
V=/opt/video-studio/tools/venv/bin/python
T=/opt/video-studio/tools/analysis
AN=/opt/video-studio/projects/2026-08-25-tester/work/analysis
OUT=/opt/video-studio/tools/analysis/p1_out
DB=/opt/video-studio/studio.db

COMMON="--beats $AN/a_full_beats.json \
 --action-profile $AN/b_reel_profile.json \
 --signals $OUT/signals_b_300_420.json --signals-a $OUT/signals_a_300_420.json \
 --phrases $AN/a_full_phrases.json \
 --min-b 4 --max-b 14 --min-recovery 8 \
 --events 12 --min-event-gap 9 --start 300 --end 405 \
 --b-search-from 300 --b-search-to 420 \
 --keyframes-db $DB --clip-id 3"

echo "=== REGRESSION: default path (no --duration-choices) ==="
$V $T/candidate_score.py $COMMON --out $OUT/regr_default_1.json
$V $T/candidate_score.py $COMMON --out $OUT/regr_default_2.json
if cmp -s $OUT/regr_default_1.json $OUT/regr_default_2.json; then
  echo "determinism (default): byte-identical across two runs"
else
  echo "determinism (default): DIFFERS"; fi
# the pre-change artifact for the same inputs
if [ -f $OUT/candidates_300_420.json ]; then
  $V - <<'PY'
import json
a = json.load(open("/opt/video-studio/tools/analysis/p1_out/candidates_300_420.json"))
b = json.load(open("/opt/video-studio/tools/analysis/p1_out/regr_default_1.json"))
def strip(d):
    d = json.loads(json.dumps(d))
    d.pop("duration_choice_rules", None)
    d["policy"].pop("duration_choices", None)
    for e in d["events"]:
        e.pop("duration_choices", None)
    return d
print("pre-change artifact vs new default path identical:", strip(a) == strip(b))
PY
fi
echo
echo "=== NEW: --duration-choices ==="
$V $T/candidate_score.py $COMMON --duration-choices --out $OUT/dur_300_420.json
$V $T/candidate_score.py $COMMON --duration-choices --out $OUT/dur_300_420_run2.json
if cmp -s $OUT/dur_300_420.json $OUT/dur_300_420_run2.json; then
  echo "determinism (duration choices): byte-identical across two runs"
else
  echo "determinism (duration choices): DIFFERS"; fi
