#!/bin/bash
# Test 4 review proof render. No hand-editing, no arbitrary cutoff: --full means the output
# IS the manifest timeline, which was built to end in A.
#
# The audio-correct review copy re-muxes the section's real audio in a separate ffmpeg call,
# because render_proxy.py's build_audio_bed hardcodes -ss 0 for section manifests. No
# renderer source is touched.
set -eu
V=/opt/video-studio/tools/venv/bin/python
PROXY=/opt/video-studio/tools/proxy/render_proxy.py
D=/opt/video-studio/projects/2026-08-25-tester/work/analysis/test4_ab
B=$D/proof_basis.json
JOB=$($V -c "import json;print(json.load(open('$B'))['job'])")
Y=$($V -c "import json;print(json.load(open('$B'))['manifest'])")
OFF=$($V -c "import json;print(json.load(open('$B'))['served_audio_offset_s'])")
TOT=$($V -c "import json;print(json.load(open('$B'))['total_s'])")
A="/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4"
OUT=/mnt/media/proofs/test4_matchedab_reviewproof.mp4
REV=/mnt/media/proofs/test4_matchedab_reviewproof_review_audiosync.mp4

echo "job            : $JOB"
echo "manifest       : $Y"
echo "manifest total : ${TOT}s   (renderer gets --full: no cutoff applied)"
echo "audio offset   : ${OFF}s into the A reel"

$V "$PROXY" "$Y" --media-root /mnt/media/raw --work "$D/work" --out "$OUT" --full --json

ffmpeg -hide_banner -nostdin -v error -i "$OUT" -ss "$OFF" -i "$A" \
       -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 192k -shortest \
       -movflags +faststart -y "$REV"

echo "--- ffprobe ---"
for F in "$OUT" "$REV"; do
  echo "$F"
  ffprobe -v error -show_entries stream=index,codec_type,codec_name,width,height,r_frame_rate,nb_frames \
          -show_entries format=duration,size -of default=nw=1 "$F"
done
