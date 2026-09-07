#!/usr/bin/env python3
"""beat_detect.py — BPM + beat times for a video/audio file using aubio.

Usage:
  beat_detect.py <input.mp4|wav> [--bpm-only] [--json out.json]

Output (default): BPM + beat times in seconds (tab-separated).
--json: also write machine-readable {bpm, beats: [...]} to the given file.

Notes:
- Extracts mono 44.1k audio on the fly with ffmpeg (no intermediate file).
- aubio tempo (default) is robust for EDM/DJ content; use --method specdiff
  for noisier crowd audio.
"""
import argparse
import json
import subprocess
import sys

import aubio
import numpy as np

FFMPEG = "ffmpeg"


def read_audio(path, samplerate=44100):
    """Stream mono audio from file via ffmpeg -> float32 numpy array."""
    cmd = [
        FFMPEG, "-v", "error", "-i", path,
        "-f", "f32le", "-ac", "1", "-ar", str(samplerate), "-"
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def detect_beats(samples, samplerate=44100, method="default", win_s=1024):
    tempo = aubio.tempo(method, win_s, win_s // 2, samplerate)
    beats = []
    hop_s = win_s // 2
    for start in range(0, len(samples) - hop_s, hop_s):
        block = samples[start:start + hop_s]
        is_beat = tempo(block)
        if is_beat:
            beats.append(tempo.get_last_s())
    bpm = tempo.get_bpm()
    return bpm, beats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--method", default="default",
                    choices=["default", "specdiff", "phase", "schmitt"])
    ap.add_argument("--json", help="write JSON output to this path")
    ap.add_argument("--bpm-only", action="store_true")
    args = ap.parse_args()

    sr = 44100
    print(f"[beat_detect] reading {args.input} ...", file=sys.stderr)
    samples = read_audio(args.input, sr)
    print(f"[beat_detect] {len(samples)/sr:.1f}s audio loaded", file=sys.stderr)

    bpm, beats = detect_beats(samples, sr, method=args.method)

    if args.bpm_only:
        print(f"{bpm:.1f}")
        return 0

    print(f"BPM: {bpm:.1f}")
    print(f"Beats: {len(beats)}")
    for t in beats:
        print(f"{t:.3f}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"bpm": bpm, "beats": beats, "input": args.input}, f)
        print(f"[beat_detect] wrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
