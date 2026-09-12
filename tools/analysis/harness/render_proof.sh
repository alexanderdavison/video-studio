#!/bin/bash
# Clean render of the 4-insert duration proof.
#
# No hand-editing, no arbitrary cutoff: the renderer is invoked with --full so the
# output IS the manifest's own timeline. The manifest was constructed so that timeline
# ends in A, which is what keeps the output's final second on angle A.
#
# The audio-correct review copy re-muxes the section's real audio. This is the same
# documented step the Test 1/Test 2 proofs required, because render_proxy.py's
# build_audio_bed hardcodes -ss 0 on the A reel for section manifests. That is a
# separate ffmpeg invocation; no renderer source is touched.
set -eu
V=/opt/video-studio/tools/venv/bin/python
PROXY=/opt/video-studio/tools/proxy/render_proxy.py
D=/opt/video-studio/projects/2026-08-25-tester/work/analysis/test3_ab
B=$D/proof_basis.json
JOB=$($V -c "import json;print(json.load(open('$B'))['job'])")
Y=$($V -c "import json;print(json.load(open('$B'))['manifest'])")
OFF=$($V -c "import json;print(json.load(open('$B'))['served_audio_offset_s'])")
TOT=$($V -c "import json;print(json.load(open('$B'))['total_s'])")
A="/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4"
OUT=/mnt/media/proofs/duration_proof_4insert.mp4
REV=/mnt/media/proofs/duration_proof_4insert_review_audiosync.mp4

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
          -show_entries format=duration,size,format_name -of default=nw=1 "$F"
done
