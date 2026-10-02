#!/usr/bin/env python3
"""b_activity_signals.py — dense LOCALISED activity signal for the B reel.

Purpose (2026-09-13, candidate opportunity increment): give the candidate
generator an *alternate-camera* activity signal fine enough to see the onset of
sustained useful B action, so an editorial event can be proposed where that
action actually starts. It is a PROPOSAL signal: it answers "should an editorial
opportunity exist here?", never "should we cut here?".

Method — the same measurement validated by the 2026-09-13 opportunity audit:

  1. the B reel is decoded at 2 fps into 480x270 16-bit grey frames
     (gray16le preserves the 10-bit source; the B reel sits in the bottom ~2 %
     of the range, so 8-bit loses real signal);
  2. consecutive-sample absolute differences are reduced to an 8x6 cell grid;
  3. per sample: loc (max cell), glob (mean cell), activ (Σmax(0, cell-glob) —
     change above the frame's own background) and artic (Σmax(0, cell-floor));
  4. `artic` is smoothed over 5 samples and thresholded on a robust noise floor;
     runs around each local maximum (expanded while sm > run_frac * peak, at
     least min_run_s long) are the reported sustained activity runs.

Everything is deterministic: two runs on the same source produce a byte-identical
JSON. No model, no network.

usage:
  b_activity_signals.py --video "B CAM/x.mp4" --out b_activity.json \\
      --start 1016 --end 1562 [--lag 1.2783]

  --start/--end   B SOURCE seconds (performance = source + lag)
  --raw PATH      reuse an existing raw grey16 stream instead of decoding
  --raw-start S   source time of that stream's first frame
  --work DIR      where a temporary raw lands when decoding (default /tmp)
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np

FFMPEG = "ffmpeg"
W, H = 480, 270
FPS_DEFAULT = 2.0
CELLS = (8, 6)                 # x, y
SMOOTH = 5                     # samples (2.5 s at 2 fps)
RUN_FRAC = 0.60
MIN_RUN_S = 2.5
MAX_RUN_S = 20.0
NMS_SEP_S = 4.0
TARGET_P995 = 60.0             # global gain target, delivered-like level


def extract(video, start, dur, fps, w, h, out):
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-nostdin",
           "-ss", "%.3f" % start, "-i", video, "-t", "%.3f" % dur,
           "-vf", "fps=%g,scale=%d:%d,format=gray16le" % (fps, w, h),
           "-f", "rawvideo", "-pix_fmt", "gray16le", "-y", out]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr.decode("utf-8", "replace")[-2000:])
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=float, default=None, help="B SOURCE seconds")
    ap.add_argument("--end", type=float, default=None, help="B SOURCE seconds")
    ap.add_argument("--fps", type=float, default=FPS_DEFAULT)
    ap.add_argument("--width", type=int, default=W)
    ap.add_argument("--height", type=int, default=H)
    ap.add_argument("--lag", type=float, default=1.2783)
    ap.add_argument("--raw", default=None)
    ap.add_argument("--raw-start", type=float, default=None)
    ap.add_argument("--work", default="/tmp")
    ap.add_argument("--min-run", type=float, default=MIN_RUN_S)
    ap.add_argument("--max-run", type=float, default=MAX_RUN_S)
    ap.add_argument("--nms-min-sep", type=float, default=NMS_SEP_S)
    ap.add_argument("--run-frac", type=float, default=RUN_FRAC)
    ap.add_argument("--smooth", type=int, default=SMOOTH)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.raw:
        raw = a.raw
        start = a.raw_start if a.raw_start is not None else (a.start or 0.0)
        tmp = None
    else:
        if not a.video or a.start is None or a.end is None:
            print("ERROR: need --video with --start/--end, or --raw with --raw-start",
                  file=sys.stderr)
            return 2
        if a.end <= a.start:
            print("ERROR: --end must be past --start", file=sys.stderr)
            return 2
        start = a.start
        tmp = os.path.join(a.work, "b_activity_%d_%d.raw" % (int(a.start), int(a.end)))
        if not extract(a.video, a.start, a.end - a.start, a.fps, a.width, a.height, tmp):
            print("ERROR: ffmpeg extraction failed", file=sys.stderr)
            return 2
        raw = tmp

    n = a.width * a.height
    buf = np.fromfile(raw, dtype=np.uint16)
    got = buf.size // n
    if got < 2:
        print("ERROR: %s holds %d frames" % (raw, got), file=sys.stderr)
        return 2
    frames = buf[:got * n].reshape(got, a.height, a.width).astype(np.float32) / 257.0
    gain = TARGET_P995 / float(np.percentile(frames, 99.5))
    frames *= gain

    t_src = start + (np.arange(got) + 0.5) / a.fps
    diff = np.abs(np.diff(frames, axis=0))
    cx, cy = CELLS
    ch, cw = a.height // cy, a.width // cx
    cm = diff[:, :ch * cy, :cw * cx].reshape(diff.shape[0], cy, ch, cx, cw).mean(axis=(2, 4))
    glob = cm.mean(axis=(1, 2))
    loc = cm.max(axis=(1, 2))
    idx = cm.reshape(cm.shape[0], -1).argmax(axis=1)
    activ = np.clip(cm - glob[:, None, None], 0, None).sum(axis=(1, 2))
    floor = 2.5 * float(np.percentile(glob, 10))
    artic = np.clip(cm - floor, 0, None).sum(axis=(1, 2))
    luma = frames[:-1].mean(axis=(1, 2))

    ker = np.ones(a.smooth) / a.smooth
    sm = np.convolve(artic, ker, mode="same")
    med = float(np.median(sm))
    luma_med = float(np.median(luma))

    # Proposal-grade runs. A local maximum only qualifies as a proposal peak when it
    # sits at/above the series median (the audit's own "strong run" criterion) — a low
    # peak would otherwise expand into a whole-section plateau. Run membership then
    # requires the smoothed series to stay above BOTH run_frac x peak and the median,
    # and a run is capped at MAX_RUN_S so it can never claim minutes of "action".
    # Runs are greedily non-max-suppressed: a peak inside an accepted run, or within
    # NMS_SEP_S of an accepted onset, is the same action seen twice.
    peaks = [i for i in range(2, len(sm) - 2)
             if sm[i] == max(sm[i - 2:i + 3]) and sm[i] >= med]
    peaks.sort(key=lambda i: -sm[i])
    runs, accepted_spans, accepted_onsets = [], [], []
    for i in peaks:
        pk = sm[i]
        floor = max(a.run_frac * pk, med)
        lo, hi = i, i
        while lo > 0 and sm[lo - 1] > floor:
            lo -= 1
        while hi + 1 < len(sm) and sm[hi + 1] > floor:
            hi += 1
        trimmed = False
        if (hi + 1 - lo) / a.fps > a.max_run:
            half = int(round(a.max_run * a.fps / 2))
            lo, hi = max(0, i - half), min(len(sm) - 1, i + half)
            trimmed = True
        onset = float(t_src[lo])
        if any(lo < s1 and hi > s0 for s0, s1 in accepted_spans):
            continue
        if any(abs(onset - o) < a.nms_min_sep for o in accepted_onsets):
            continue
        dur = (hi + 1 - lo) / a.fps
        if dur < a.min_run - 1e-9:
            continue
        accepted_spans.append((lo, hi))
        accepted_onsets.append(onset)
        seg = sm[lo:hi + 1] - floor
        runs.append(dict(
            onset_src_s=round(float(t_src[lo]), 3),
            end_src_s=round(float(t_src[hi + 1]), 3),
            onset_perf_s=round(float(t_src[lo] + a.lag), 3),
            end_perf_s=round(float(t_src[hi + 1] + a.lag), 3),
            peak_src_s=round(float(t_src[i]), 3),
            peak_perf_s=round(float(t_src[i] + a.lag), 3),
            duration_s=round(float(dur), 3),
            peak_artic=round(float(artic[i]), 4),
            peak_sm=round(float(pk), 4),
            peak_loc=round(float(loc[i]), 4),
            peak_glob=round(float(glob[i]), 4),
            conc=round(float(loc[i] / max(glob[i], 1e-6)), 4),
            peak_cell=[int(idx[i] // cx), int(idx[i] % cx)],
            mass=round(float(seg.sum()) / a.fps, 4),
            strength_pct=round(float((sm < pk).mean() * 100.0), 2),
            luma_mean=round(float(luma[lo:hi + 1].mean()), 4),
            luma_min=round(float(luma[lo:hi + 1].min()), 4),
            frozen_frac=round(float((glob[lo:hi + 1] < 0.35).mean()), 4),
            length_capped=bool(trimmed),
        ))
    runs.sort(key=lambda r: r["onset_perf_s"])
    for k, r in enumerate(runs, 1):
        r["run_id"] = "run_%02d" % k

    doc = {
        "tool": "b_activity_signals.py",
        "principle": ("proposal signal only — 'should an editorial opportunity exist here?', "
                      "never 'should we cut here?'. It is not a candidate score and is never "
                      "shown to the editorial model as a reason to cut."),
        "source": a.video or raw,
        "lag_s": a.lag,
        "coordinate_note": ("sample t_src is B SOURCE time; performance = t_src + lag_s "
                            "(accepted mapping r = A_t - lag)"),
        "sampling": {"fps": a.fps, "wh": [a.width, a.height], "pix_fmt": "gray16le",
                     "start_src_s": round(float(start), 3),
                     "end_src_s": round(float(t_src[-1] + 1.0 / a.fps), 3),
                     "n_samples": int(len(sm)), "cell_grid": [cy, cx], "gain": round(gain, 6)},
        "thresholds": {"metric": "artic = SUM(max(0, cell - 2.5 x quiet_background))",
                       "noise_floor": round(floor, 6), "median_smoothed": round(med, 6),
                       "smooth_samples": a.smooth, "run_frac": a.run_frac,
                       "min_run_s": a.min_run,
                       "peak_rule": "local maximum over a 5-sample window"},
        "summary": {"n_runs": len(runs),
                    "runs_min_s": min([r["duration_s"] for r in runs], default=None),
                    "runs_max_s": max([r["duration_s"] for r in runs], default=None),
                    "luma_median": round(luma_med, 4),
                    "samples_min_s": round(float(t_src[0]), 3),
                    "samples_max_s": round(float(t_src[-1]), 3)},
        "runs": runs,
    }
    if a.json:
        doc["samples"] = [{"t_src_s": round(float(t_src[j]), 3),
                           "t_perf_s": round(float(t_src[j] + a.lag), 3),
                           "loc": round(float(loc[j]), 4),
                           "glob": round(float(glob[j]), 4),
                           "activ": round(float(activ[j]), 4),
                           "artic": round(float(artic[j]), 4),
                           "sm": round(float(sm[j]), 4),
                           "luma": round(float(luma[j]), 4)}
                          for j in range(len(sm))]

    with open(a.out, "w") as f:
        json.dump(doc, f, indent=1)
    if tmp and os.path.exists(tmp):
        os.remove(tmp)
    print("ACTIVITY %s -> %s : %d samples %.3f..%.3f src, %d sustained runs"
          % (a.video or raw, a.out, doc["sampling"]["n_samples"],
             doc["sampling"]["start_src_s"], doc["sampling"]["end_src_s"], len(runs)))
    for r in runs:
        print("   %s onset perf %.3f src %.3f  dur %.2fs  peak_loc %.2f conc %.2f "
              "mass %.1f pct %.1f"
              % (r["run_id"], r["onset_perf_s"], r["onset_src_s"], r["duration_s"],
                 r["peak_loc"], r["conc"], r["mass"], r["strength_pct"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
