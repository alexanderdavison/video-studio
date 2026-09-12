#!/bin/bash
# FINAL candidate run for the duration-choice increment + full legality verification.
# No --keyframes-db: Test 2 ran with clip 3 supplied but inert (no sharpness at the time);
# clip 3 is a June file, not this tester's B card. Supplying it now would change the quality
# term and confound the increment, so quality stays on the visual_signals path exactly as
# Test 2 measured it.
set -u
V=/opt/video-studio/tools/venv/bin/python
T=/opt/video-studio/tools/analysis
AN=/opt/video-studio/projects/2026-08-25-tester/work/analysis
OUT=/opt/video-studio/tools/analysis/p1_out
C="$T/candidate_score.py --beats $AN/a_full_beats.json \
 --action-profile $AN/b_reel_profile.json \
 --signals $OUT/signals_b_300_420.json --signals-a $OUT/signals_a_300_420.json \
 --phrases $AN/a_full_phrases.json \
 --min-b 4 --max-b 14 --min-recovery 8 \
 --events 12 --min-event-gap 9 --start 300 --end 405 \
 --b-search-from 300 --b-search-to 420"

echo "=== duration-choice generation (run 1) ==="
$V $C --duration-choices --out $OUT/dur_300_420.json
echo
echo "=== determinism: run 2 ==="
$V $C --duration-choices --out $OUT/dur_300_420_run2.json
cmp -s $OUT/dur_300_420.json $OUT/dur_300_420_run2.json \
  && echo "BYTE-IDENTICAL across two runs" || echo "DIFFERS -- NOT DETERMINISTIC"
sha256sum $OUT/dur_300_420.json $OUT/dur_300_420_run2.json
echo
echo "=== control: default path (no duration choices), same inputs ==="
$V $C --out $OUT/control_no_dc.json
