#!/usr/bin/env python3
"""action_profile.py — per-segment ACTION score for a B-reel source.

Phase 6 'equipment/action preference' (b_policy): B inserts should show
moments where something is happening (visible DJ actions / equipment
interaction). Without vision, the cheap proxy is AUDIO ENERGY — high RMS
regions of the reel tend to be the moments a DJ is actively performing.

Method: extract mono audio (ffmpeg with -vn — the audio-only lesson), compute
RMS energy per second, normalize 0..1 against the reel's own p95, aggregate
into N-second segments. The template engine's --b-profile consumes the
segments to choose high-action B source windows.

Usage:
  action_profile.py <video> --out profile.json [--segment 10] [--offset 0]

  --offset: virtual-reel offset for multi-file B reels (run per file, merge
            with increasing offsets into one segments list).

Exit: 0 = written; 2 = error.
"""

import argparse
import json
import os
import subprocess
import sys

import numpy as np


def read_mono(path, sr=44100):
    """Stream mono audio via ffmpeg (video stream never decoded)."""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn",
           "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        return None, "ffmpeg audio extract failed: %s" % r.stderr.decode()[-300:]
    if not r.stdout:
        return None, "no audio in %s" % path
    return np.frombuffer(r.stdout, dtype=np.float32), None


def main():
    ap = argparse.ArgumentParser(description="Action profile (audio energy) for a reel")
    ap.add_argument("video")
    ap.add_argument("--out", default=None, help="write profile JSON here")
    ap.add_argument("--segment", type=float, default=10.0,
                    help="segment length in seconds (default 10)")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="virtual-reel offset for this file (multi-file B reels)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.video):
        print("ERROR: no such file: %s" % args.video, file=sys.stderr)
        sys.exit(2)

    samples, err = read_mono(args.video)
    if err:
        print("ERROR: %s" % err, file=sys.stderr)
        sys.exit(2)

    sr = 44100
    dur = len(samples) / sr
    per_sec = []
    n_sec = int(dur) + 1
    for s in range(n_sec):
        seg = samples[s * sr:(s + 1) * sr]
        rms = float(np.sqrt(np.mean(seg ** 2))) if seg.size else 0.0
        per_sec.append(rms)

    arr = np.array(per_sec)
    p95 = float(np.percentile(arr, 95)) if arr.size else 0.0
    norm = np.clip(arr / p95 if p95 > 1e-6 else arr, 0.0, 1.0)

    seg_len = max(1.0, args.segment)
    segments = []
    t = 0.0
    while t < dur:
        e = min(t + seg_len, dur)
        s0, s1 = int(t), int(e)
        sc = float(norm[s0:s1].mean()) if s1 > s0 else 0.0
        segments.append({"start": round(t + args.offset, 3),
                         "end": round(e + args.offset, 3),
                         "score": round(sc, 3)})
        t = e

    out = {"reel": os.path.basename(args.video), "duration_s": round(dur, 3),
           "segments": segments}
    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, indent=1)
        print("ACTION PROFILE %s -> %s (%d segments)" % (args.video, args.out, len(segments)))
    else:
        print(json.dumps(out, indent=1))
    sys.exit(0)


if __name__ == "__main__":
    main()
