#!/bin/bash
# make_fixtures.sh — generate TINY synthetic fixtures reproducing the failure
# classes from the 2026-08-26 tester postmortem (13 messups). All clips are a
# few seconds; the regression suite never touches full-length footage.
#
# Fixtures produced in ./fixtures:
#   a_cam.mp4     6s 640x360 yuv420p 30fps + 1kHz tone   (general source)
#   short_src.mp4 3s same                                   (span-overflow: b8 shape)
#   nonunif_a/b.mp4 2s yuv420p vs yuv444p + nonunif.txt     (mixed-format concat)
#   unif_a/b.mp4  2s both yuv420p + unif.txt                (uniform concat)
#   hot.mp4       4s LOUD audio (~-6.5 LUFS), bt709-tagged  (loudness FAIL fixture)
#   good.mp4      4s two-pass loudnorm -13.5, bt709-tagged  (full postflight PASS)
#   untagged.mp4  4s loudnorm -13.5, NO bt709 tags          (BT.709 FAIL fixture)
set -euo pipefail
cd "$(dirname "$0")"
FX="$(pwd)/fixtures"
mkdir -p "$FX"

ENC="-c:v libx264 -preset ultrafast -crf 30"

echo "== fixture: a_cam.mp4 (6s)"
ffmpeg -y -v error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=6" \
  -f lavfi -i "sine=frequency=1000:duration=6" \
  $ENC -pix_fmt yuv420p -c:a aac -b:a 96k -ar 48000 -movflags +faststart "$FX/a_cam.mp4"

echo "== fixture: short_src.mp4 (3s)"
ffmpeg -y -v error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=3" \
  -f lavfi -i "sine=frequency=440:duration=3" \
  $ENC -pix_fmt yuv420p -c:a aac -b:a 96k -ar 48000 -movflags +faststart "$FX/short_src.mp4"

echo "== fixture: dark_b.mp4 (10s, crushed black — B-cam LOG stand-in)"
ffmpeg -y -v error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=10" \
  -f lavfi -i "sine=frequency=330:duration=10" \
  -vf "eq=brightness=-0.55,format=yuv420p" \
  $ENC -pix_fmt yuv420p -c:a aac -b:a 96k -ar 48000 -movflags +faststart "$FX/dark_b.mp4"

echo "== fixture: long_a.mp4 (120s) + long_b.mp4 (120s) for propose tests"
ffmpeg -y -v error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=120" \
  -f lavfi -i "sine=frequency=880:duration=120" \
  $ENC -pix_fmt yuv420p -c:a aac -b:a 96k -ar 48000 -movflags +faststart "$FX/long_a.mp4"
ffmpeg -y -v error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=120" \
  -f lavfi -i "sine=frequency=660:duration=120" \
  $ENC -pix_fmt yuv420p -c:a aac -b:a 96k -ar 48000 -movflags +faststart "$FX/long_b.mp4"

echo "== fixture: non-uniform pair (yuv420p + yuv444p)"
ffmpeg -y -v error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=2" \
  $ENC -pix_fmt yuv420p "$FX/nonunif_a.mp4"
ffmpeg -y -v error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=2" \
  $ENC -pix_fmt yuv444p "$FX/nonunif_b.mp4"
printf "file 'nonunif_a.mp4'\nfile 'nonunif_b.mp4'\n" > "$FX/nonunif.txt"

echo "== fixture: uniform pair (yuv420p + yuv420p)"
ffmpeg -y -v error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=2" \
  $ENC -pix_fmt yuv420p "$FX/unif_a.mp4"
ffmpeg -y -v error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=2" \
  $ENC -pix_fmt yuv420p "$FX/unif_b.mp4"
printf "file 'unif_a.mp4'\nfile 'unif_b.mp4'\n" > "$FX/unif.txt"

echo "== fixture: hot.mp4 (loud, bt709-tagged)"
ffmpeg -y -v error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=4" \
  -f lavfi -i "sine=frequency=1000:duration=4" \
  -af "volume=0.9" \
  -vf "format=yuv420p" \
  $ENC -pix_fmt yuv420p -c:a aac -b:a 96k -ar 48000 -movflags +faststart \
  -bsf:v h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1 \
  "$FX/hot.mp4"

echo "== fixture: good.mp4 (two-pass loudnorm -13.5, bt709-tagged)"
MEAS=$(ffmpeg -i "$FX/hot.mp4" -af loudnorm=print_format=json -f null - 2>&1 | python3 -c "
import json, re, sys
log = sys.stdin.read()
m = re.search(r'\{[^{}]*\"input_i\"[^{}]*\}', log, re.S)
if not m:
    print('ERR no loudnorm json'); sys.exit(1)
d = json.loads(m.group(0))
print(f\"{d['input_i']} {d['input_tp']} {d['input_lra']} {d['input_thresh']}\")
")
if [ "$MEAS" = "ERR no loudnorm json" ]; then echo "fixture FAIL: loudnorm measure"; exit 1; fi
read MI MT ML MTH <<< "$MEAS"
echo "   hot measures: I=$MI TP=$MT LRA=$ML thresh=$MTH"
ffmpeg -y -v error -i "$FX/hot.mp4" \
  -vf "format=yuv420p" \
  -af "loudnorm=I=-13.5:TP=-3.0:LRA=6.4:measured_I=${MI}:measured_TP=${MT}:measured_LRA=${ML}:measured_thresh=${MTH}:linear=true:print_format=summary" \
  $ENC -pix_fmt yuv420p -c:a aac -b:a 96k -ar 48000 -movflags +faststart \
  -bsf:v h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1 \
  "$FX/good.mp4"

echo "== fixture: untagged.mp4 (loudnorm -13.5, NO bt709)"
# NOTE: must be built from RAW lavfi, not by re-encoding hot.mp4 — ffmpeg
# propagates hot's bt709 color tags to the output, which would make this
# fixture pass the BT.709 check. Raw testsrc2 carries unknown/unknown/unknown.
ffmpeg -y -v error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=4" \
  -f lavfi -i "sine=frequency=1000:duration=4" \
  -af "volume=0.9,loudnorm=I=-13.5:TP=-3.0:LRA=6.4:measured_I=${MI}:measured_TP=${MT}:measured_LRA=${ML}:measured_thresh=${MTH}:linear=true:print_format=summary" \
  $ENC -pix_fmt yuv420p -c:a aac -b:a 96k -ar 48000 -movflags +faststart "$FX/untagged.mp4"

echo "== verify fixtures =="
for f in a_cam short_src dark_b long_a long_b nonunif_a nonunif_b unif_a unif_b hot good untagged; do
  d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$FX/$f.mp4")
  pix=$(ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt -of csv=p=0 "$FX/$f.mp4")
  echo "   $f.mp4 dur=$d pix=$pix"
done
echo "FIXTURES_OK"
