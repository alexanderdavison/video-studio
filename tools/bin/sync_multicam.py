#!/usr/bin/env python3
"""sync_multicam.py — align multiple video clips by audio cross-correlation.

Finds the time offset (in seconds) between a reference clip and each other
clip so they can be cut together on the same timeline/beat.

Usage:
  sync_multicam.py <ref.mp4> <clip2.mp4> [clip3.mp4 ...] [--json out.json]

Output:
  ref: 0.000s
  clip2: +12.345s   (clip2's audio matches ref at ref_time + 12.345)

Positive offset = clip starts LATER than ref (its t=0 is ref t=+offset).
Negative offset = clip starts EARLIER than ref.

Method: extract mono 8 kHz audio via ffmpeg, FFT cross-correlation, find the
lag with max normalized correlation within the search window, refine to ~1 ms
by parabolic interpolation. Confidence is the peak correlation (1.0 = perfect).
"""
import argparse
import json
import subprocess
import sys

import numpy as np
from scipy import fft, signal

FFMPEG = "ffmpeg"
SR = 8000  # downsampled sample rate for correlation


def read_audio(path):
    cmd = [
        FFMPEG, "-v", "error", "-i", path,
        "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def align(ref, clip, max_shift_s=120.0):
    """Return (offset_s, confidence) of clip relative to ref.

    offset = seconds to ADD to ref timeline to hit clip's matching moment.
    Uses overlap-normalized FFT cross-correlation (raw correlation biases
    toward beat-periodic peaks on music content).
    """
    ref = ref.astype(np.float64)
    clip = clip.astype(np.float64)
    ref = ref - ref.mean()
    clip = clip - clip.mean()

    corr = signal.correlate(ref, clip, mode="full", method="fft")
    lags = signal.correlation_lags(len(ref), len(clip), mode="full")
    # Normalize by overlap count so later/earlier partial overlaps don't win.
    n = len(ref)
    overlap = np.concatenate([np.arange(1, n + 1), np.arange(n - 1, 0, -1)])
    ncorr = corr / overlap

    # Restrict to the allowed lag window.
    lo = int(-max_shift_s * SR)
    hi = int(max_shift_s * SR)
    mask = (lags >= lo) & (lags <= hi)
    if not mask.any():
        return 0.0, 0.0
    sub_lags = lags[mask]
    sub_corr = ncorr[mask]
    k = int(sub_lags[np.argmax(np.abs(sub_corr))])
    # Parabolic refinement around the peak (in absolute lag space)
    pos = np.where(lags == k)[0][0]
    if 1 <= pos < len(ncorr) - 1:
        a, b, c = ncorr[pos - 1], ncorr[pos], ncorr[pos + 1]
        denom = (a - 2 * b + c)
        if abs(denom) > 1e-12:
            k += 0.5 * (a - c) / denom
    offset_s = k / SR
    # Confidence: peak / median-abs of the search window (how much it stands out)
    med = np.median(np.abs(sub_corr)) or 1.0
    confidence = float(abs(ncorr[pos]) / med) if len(sub_corr) else 0.0
    return offset_s, confidence


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ref")
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--json", help="write JSON output")
    ap.add_argument("--max-shift", type=float, default=120.0)
    args = ap.parse_args()

    print(f"[sync_multicam] reading ref {args.ref} ...", file=sys.stderr)
    ref = read_audio(args.ref)
    print(f"[sync_multicam] ref audio {len(ref)/SR:.1f}s", file=sys.stderr)

    results = {"ref": args.ref, "offsets": {}, "confidences": {}}
    print(f"ref: 0.000s  (conf 1.000)")
    for clip in args.clips:
        print(f"[sync_multicam] reading {clip} ...", file=sys.stderr)
        sig = read_audio(clip)
        off, conf = align(ref, sig, max_shift_s=args.max_shift)
        results["offsets"][clip] = off
        results["confidences"][clip] = conf
        flag = ""
        if conf < 0.05:
            flag = "  <-- LOW CONFIDENCE: clips may not share audio"
        print(f"{clip}: {off:+.3f}s  (conf {conf:.3f}){flag}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"[sync_multicam] wrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
