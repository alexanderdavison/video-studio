#!/bin/bash
# fixtures.sh — generate tiny synthetic clips for the Phase 1 regression suite.
# Uses lavfi (testsrc2 + sine) so the whole suite runs in seconds.
set -e
cd "$(dirname "$0")"
mkdir -p fixtures

# Two 2s 320x180 test clips (uniform, yuv420p) + a 4s A-spine
ffmpeg -y -f lavfi -i "testsrc2=size=320x180:rate=30" -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=2" \
  -t 2 -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 64k fixtures/seg_a.mp4 >/dev/null 2>&1
ffmpeg -y -f lavfi -i "testsrc2=size=320x180:rate=30" -f lavfi -i "sine=frequency=880:sample_rate=48000:duration=2" \
  -t 2 -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 64k fixtures/seg_b.mp4 >/dev/null 2>&1

# Truncated file (no moov) — the "worthless partial" case
head -c 20000 fixtures/seg_a.mp4 > fixtures/truncated.mp4

# Loudness fixtures: steady sine at a known level (calibrated 2026-08-26 on
# this ffmpeg: sine base amp is NOT 1.0; volume=2.6 → I=-13.5 LUFS).
ffmpeg -y -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=3" -af "volume=2.6" -c:a aac fixtures/loud_ok.m4a >/dev/null 2>&1
ffmpeg -y -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=3" -af "volume=4.0" -c:a aac fixtures/loud_bad.m4a >/dev/null 2>&1

# BT.709-tagged vs untagged encodes
ffmpeg -y -f lavfi -i "testsrc2=size=320x180:rate=30" -t 1 -c:v libx264 -pix_fmt yuv420p \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 fixtures/bt709_ok.mp4 >/dev/null 2>&1
ffmpeg -y -f lavfi -i "testsrc2=size=320x180:rate=30" -t 1 -c:v libx264 -pix_fmt yuv420p fixtures/bt709_bad.mp4 >/dev/null 2>&1

# Fatal-stderr fixture: a stderr log carrying the known fatal pattern
printf '[Parsed_hwupload_3] Impossible to convert between the formats\nError reinitializing filters\n' > fixtures/seg_a.stderr.log

echo "fixtures ready:"
ls -la fixtures/ | grep -vE "^total|^d" | awk '{print "  " $5 "  " $9}' | head -12
