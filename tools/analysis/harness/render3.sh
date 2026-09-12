#!/bin/bash
# Render the lightweight 90 s duration proof, then produce the audio-correct review copy.
# The proxy's build_audio_bed hardcodes -ss 0 on the A reel (known pre-existing defect for
# section manifests), so the review copy re-muxes the real section audio, exactly as the
# Test 2 proofs did.
set -u
V=/opt/video-studio/tools/venv/bin/python
PROXY=/opt/video-studio/tools/proxy/render_proxy.py
D=/opt/video-studio/projects/2026-08-25-tester/work/analysis/test3_ab
Y=$D/test3_duration_demo_section300_600.yaml
A="/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4"
O=/mnt/media/proofs/test3_duration_demo_300_390.mp4
L=/root/render3.log
: > "$L"

echo "START $(date -Is)" >> "$L"
echo "=== proxy render (first 90 s of the manifest) ===" >> "$L"
$V "$PROXY" "$Y" --media-root /mnt/media/raw --work "$D/work" \
   --out "$O" --max-duration 90 --json >> "$L" 2>&1
RC=$?
echo "render rc=$RC $(date -Is)" >> "$L"

if [ ! -s "$O" ]; then echo "FATAL: no output" >> "$L"; echo ALLDONE >> "$L"; exit 1; fi

R=/mnt/media/proofs/test3_duration_demo_300_390_review_audiosync.mp4
ffmpeg -hide_banner -nostdin -v error -i "$O" -ss 300 -i "$A" \
       -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 192k -shortest \
       -movflags +faststart -y "$R" >> "$L" 2>&1
echo "audiosync review rc=$? -> $R" >> "$L"

echo "=== verify ===" >> "$L"
for F in "$O" "$R"; do
  printf "%s  " "$(basename "$F")" >> "$L"
  ffprobe -v error -show_entries stream=codec_type,width,height,r_frame_rate,nb_frames \
          -show_entries format=duration,size,bit_rate -of csv=p=0 "$F" | tr '\n' ' ' >> "$L"
  echo >> "$L"
done
# count the cuts actually present in the rendered 90 s window
echo "=== cuts inside the proof window ===" >> "$L"
$V - >> "$L" 2>&1 <<'PY'
import json, yaml
man = yaml.safe_load(open("/opt/video-studio/projects/2026-08-25-tester/work/analysis/test3_ab/test3_duration_demo_section300_600.yaml"))
t, n = 0.0, 0
for c in man["timeline"]:
    d = c["source_out"] - c["source_in"]
    if t < 90.0:
        print("  %s %-3s timeline %.3f-%.3f  %6.3fs  %s" % (
            c["id"], c["angle"], t, min(t + d, 90.0), min(d, 90.0 - t),
            "B INSERT" if c["angle"] == "B" else ""))
    t += d
    if c["angle"] == "B" and t <= 90.0:
        n += 1
print("full B inserts inside the first 90 s:", n)
PY
echo "ALLDONE $(date -Is)" >> "$L"
tail -30 "$L"
