#!/usr/bin/env python3
"""pick_keyframes.py — score frames and pick the best N as JPEG thumbnails.

Uses OpenCV (installed in the tools venv). Scores frames by:
  - sharpness: variance of the Laplacian (blurry frames rank low)
  - brightness: penalize near-black and near-white (over/under-exposed)
  - motion: mean absolute frame difference (penalize total freeze, reward
    moderate action but not full-frame shake)

Efficiency: extracts ~1 fps frames via ffmpeg (fast-seek, downscaled) instead
of decoding every frame — this is what makes it usable on large DJI/GoPro
files over NFS.

Usage:
  pick_keyframes.py <input.mp4> [--out outdir] [--count 5]
                    [--fps 1] [--min-gap 2.0] [--size 640]

--fps: frames per second to sample (default 1).
--min-gap: minimum seconds between chosen frames (avoid clusters).
Output: <outdir>/<basename>_<ts>_<score>.jpg, plus scores.json
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

import cv2
import numpy as np

FFMPEG = "ffmpeg"

SHARPNESS_W = 0.5
BRIGHTNESS_W = 0.25
MOTION_W = 0.25


def score_frame(gray, prev_gray):
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    sharpness = lap.var()
    brightness = gray.mean()
    # Gaussian-ish brightness penalty: peak at ~118 (mid-gray)
    bright_pen = 1.0 - min(1.0, abs(brightness - 118.0) / 118.0)
    if prev_gray is not None:
        diff = cv2.absdiff(gray, prev_gray)
        motion = diff.mean()
        # Reward moderate motion; clamp so extreme shake doesn't dominate
        motion_score = min(1.0, motion / 12.0)
    else:
        # No previous frame: motion is not measurable for the first sample.
        motion = None
        motion_score = 0.5
    total = (SHARPNESS_W * min(1.0, sharpness / 400.0)
             + BRIGHTNESS_W * bright_pen
             + MOTION_W * motion_score)
    # Raw components travel with the score so ingest can persist them:
    # sharpness = variance of the Laplacian, brightness = mean gray, motion = mean abs diff.
    motion_out = float(motion) if motion is not None else None
    return total, float(sharpness), float(brightness), motion_out


def extract_frames(input_path, samples, size, workdir):
    """Sample frames via fast-seek grabs. Returns (ts, path) list.

    Grabs `samples` frames evenly spread across the video duration using
    -ss before -i (index seek) + -frames:v 1 — avoids decoding the whole
    file, critical for large DJI/GoPro files on slow NFS.
    """
    # Probe duration
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration,r_frame_rate",
         "-of", "json", input_path],
        capture_output=True, text=True)
    try:
        info = json.loads(probe.stdout)["streams"][0]
        dur = float(info["duration"])
        rate = info.get("r_frame_rate", "30")
        num, den = (rate.split("/") + ["1"])[:2]
        src_fps = float(num) / float(den) if den else 30.0
    except Exception:
        dur, src_fps = 0.0, 30.0
    if dur <= 0:
        print("error: could not probe duration", file=sys.stderr)
        return None, None, None
    total = int(dur * src_fps)

    interval = dur / max(samples - 1, 1)
    times = [i * interval for i in range(samples)]
    out = []
    for idx, t in enumerate(times):
        fname = f"frame_{idx:06d}.png"
        fpath = os.path.join(workdir, fname)
        cmd = [
            FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", input_path,
            "-frames:v", "1", "-vf", f"scale='min({size},iw)':'min({size},ih)':force_original_aspect_ratio=decrease",
            "-fps_mode", "vfr", "-y", fpath
        ]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and os.path.exists(fpath):
            out.append((t, fpath))
    if not out:
        print("error: no frames extracted", file=sys.stderr)
        return None, None, None
    return out, src_fps, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--out", default=".")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--samples", type=int, default=30,
                    help="number of frames to sample across the video")
    ap.add_argument("--min-gap", type=float, default=2.0)
    ap.add_argument("--size", type=int, default=640)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    base = os.path.splitext(os.path.basename(args.input))[0]

    with tempfile.TemporaryDirectory(prefix="kf_") as workdir:
        print(f"[pick_keyframes] extracting {args.samples} samples from {args.input} ...",
              file=sys.stderr)
        samples, src_fps, total = extract_frames(args.input, args.samples, args.size, workdir)
        if samples is None:
            return 1
        print(f"[pick_keyframes] {len(samples)} frames extracted "
              f"(src {src_fps:.1f} fps, {total} frames)", file=sys.stderr)

        candidates = []
        prev_gray = None
        for ts, path in samples:
            img = cv2.imread(path)
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            score, sharp_v, bright_v, motion_v = score_frame(gray, prev_gray)
            candidates.append((score, ts, img, sharp_v, bright_v, motion_v))
            prev_gray = gray

        candidates.sort(key=lambda c: -c[0])
        chosen = []
        for cand in candidates:
            ts = cand[1]
            if all(abs(ts - c[1]) >= args.min_gap for c in chosen):
                chosen.append(cand)
                if len(chosen) >= args.count:
                    break

        results = []
        for i, (score, ts, frame, sharp_v, bright_v, motion_v) in enumerate(chosen):
            fname = f"{base}_{ts:.2f}s_{score:.3f}.jpg"
            path = os.path.join(args.out, fname)
            cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            results.append({"ts": float(ts), "score": float(score), "file": fname,
                            "sharpness": sharp_v, "brightness": bright_v,
                            "motion": motion_v})
            print(f"{ts:.2f}s  score={score:.3f}  {path}")

        with open(os.path.join(args.out, f"{base}_scores.json"), "w") as f:
            json.dump({"input": args.input, "picks": results}, f, indent=2)
        print(f"[pick_keyframes] wrote {len(chosen)} picks to {args.out}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
