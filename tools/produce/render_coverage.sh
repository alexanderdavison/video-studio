#!/bin/bash
# render_coverage.sh — review proof for the COVERAGE-REVISED 9-minute program (Prompt 1 smoke test).
#
# Same renderer, same template, same audio handling as the first-pass proof. Only the manifest and the
# output name differ: the program timeline now carries one promoted insert whose territory was split
# out of the first pass's long A run.
set -eu
V=/opt/video-studio/tools/venv/bin/python
PROXY=/opt/video-studio/tools/proxy/render_proxy.py
D=/opt/video-studio/projects/2026-08-25-tester/work/analysis/test789_coverage
B=$D/revised_basis.json
JOB=$($V -c "import json;print(json.load(open('$B'))['job'])")
Y=$D/test789_coverage_reviewproof.yaml
OFF=$($V -c "import json;print(json.load(open('$B'))['served_audio_offset_s'])")
TOT=$($V -c "import json;print(json.load(open('$B'))['total_s'])")
A="/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4"
OUT=/mnt/media/proofs/test789_coverage_reviewproof.mp4
REV=/mnt/media/proofs/test789_coverage_reviewproof_review_audiosync.mp4

echo "job            : $JOB"
echo "manifest       : $Y"
echo "manifest total : ${TOT}s (--full: no cutoff applied)"
echo "audio offset   : ${OFF}s into the A reel"
$V "$PROXY" "$Y" --media-root /mnt/media/raw --work "$D/work" --out "$OUT" --full --json
ffmpeg -hide_banner -nostdin -v error -i "$OUT" -ss "$OFF" -i "$A" \
       -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 192k -shortest \
       -movflags +faststart -y "$REV"
echo "--- ffprobe ---"
for F in "$OUT" "$REV"; do
  echo "$F"
  ffprobe -v error -show_entries format=duration,size -of default=nw=1 "$F"
done
sha256sum "$OUT" "$REV"
echo "COVERAGE REVISED PROOF RENDER DONE"
