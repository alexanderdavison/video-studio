#!/usr/bin/env python3
"""visual_signals.py — cheap per-second VISUAL signals for a source video.

P1 companion to candidate_score.py. Everything here is sampled, never decoded
end-to-end: for every sample time t we run ONE `ffmpeg -ss <t> -i FILE
-frames:v 1` fast-seek, downscale to ~320 px, convert to 8-bit grey and read a
single raw frame. A 17 GB NFS source is therefore touched only at the sample
times we ask for, and --start/--end cap the work hard.

Per sample we emit:
  motion        mean |frame - previous frame| / 255          (frame-difference)
  brightness    mean grey level 0..255
  sharpness     variance of the 4-neighbour Laplacian (blur proxy)
  hist          16-bin normalised grey histogram (sums to 1)
  hist_novelty  1 - histogram intersection vs the previous sample
  obstruction   0..1 severity of "unusable" — near-black OR near-white, DISCOUNTED
                by how much detail the frame has (detail = sharpness/DETAIL_FLOOR).
                A dark-but-detailed club shot scores 0; a covered/capped lens
                (near-black AND flat) scores ~1. Raw components are also emitted
                (black_sev, white_sev, detail) so a reader can re-threshold.
  frozen        bool, motion below threshold for >= FROZEN_RUN samples
  used          bool, sample falls inside an optional used-spans file

Everything is deterministic: same file + same --start/--end/--every/--width
produces byte-identical JSON.

Usage:
  visual_signals.py <video> --out out.json [--beat-json beats.json]
                    [--start S --end E] [--every N] [--json]

  --start/--end   bounded sample window in source seconds (default: whole file,
                  but you almost always want to pass them on 4K NFS material)
  --every N       sample period in seconds (default 1.0 == 1 fps sampled)
  --used-spans F  JSON with used spans ({"spans":[{"start","end"},...]} or a
                  candidate_score.py candidates.json) — marks samples as used
  --resume        reuse samples already in --out and only compute the missing t
  --workers N     parallel ffmpeg fast-seeks (default 3 — the box is CPU capped)

Exit: 0 = written; 2 = error.
"""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np

FFMPEG = "ffmpeg"
GREY_W = 320
HIST_BINS = 16
MOTION_FROZEN_THRESH = 0.002   # mean |diff|/255 below this counts as "no motion"
FROZEN_RUN = 3                 # >= this many consecutive quiet samples = frozen
BLACK_LEVEL = 12.0             # mean grey below this trends unusable (lens capped)
WHITE_LEVEL = 245.0            # mean grey above this is blown out
DETAIL_FLOOR = 30.0            # Laplacian variance at/above this = frame has detail
OBSTRUCTION_FLAG = 0.5         # severity at/above this counts as unusable


def sample_frame(video, t, width=GREY_W, timeout=180):
    """One fast-seek frame as a (h, w) uint8 grey array. None on failure."""
    cmd = [FFMPEG, "-v", "error", "-nostdin",
           "-ss", "%.3f" % t, "-i", video,
           "-frames:v", "1",
           "-vf", "scale=%d:-2" % width,
           "-pix_fmt", "gray", "-f", "rawvideo",
           "-threads", "1", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    n = len(r.stdout)
    if n % width:
        return None
    h = n // width
    if h < 2:
        return None
    return np.frombuffer(r.stdout, dtype=np.uint8).reshape(h, width)


def laplacian_variance(frame):
    """4-neighbour Laplacian variance — higher = more in focus / more detail."""
    f = frame.astype(np.float32)
    p = np.pad(f, 1, mode="edge")
    lap = (4.0 * p[1:-1, 1:-1]
           - p[:-2, 1:-1] - p[2:, 1:-1]
           - p[1:-1, :-2] - p[1:-1, 2:])
    return float(lap.var())


def histogram(frame, bins=HIST_BINS):
    counts, _ = np.histogram(frame, bins=bins, range=(0.0, 256.0))
    return counts.astype(np.float64) / float(frame.size)


def obstruction_components(brightness, sharpness):
    """(obstruction, black_sev, white_sev, detail).

    Darkness/blowout only counts as obstruction when the frame is ALSO flat:
    a dark club shot full of detail is legitimate footage, a capped lens is not.
    """
    near_black = c01((BLACK_LEVEL - brightness) / BLACK_LEVEL) if BLACK_LEVEL > 0 else 0.0
    near_white = c01((brightness - WHITE_LEVEL) / (255.0 - WHITE_LEVEL)) if WHITE_LEVEL < 255 else 0.0
    detail = c01(sharpness / DETAIL_FLOOR) if DETAIL_FLOOR > 0 else 1.0
    black_sev = near_black * (1.0 - detail)
    white_sev = near_white * (1.0 - detail)
    return max(black_sev, white_sev), black_sev, white_sev, detail


def c01(x):
    return float(max(0.0, min(1.0, x)))


def hist_intersection(a, b):
    return float(np.minimum(a, b).sum())


def load_used_spans(path):
    if not path:
        return []
    d = json.load(open(path))
    spans = []
    if isinstance(d, dict) and "spans" in d:
        raw = d["spans"]
    elif isinstance(d, dict) and "events" in d:
        raw = []
        for ev in d["events"]:
            for c in ev.get("candidates", []):
                if c.get("action") == "SWITCH":
                    raw.append({"start": c.get("start"), "end": c.get("end")})
    else:
        raw = d
    for s in raw:
        try:
            spans.append((float(s["start"]), float(s["end"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(spans)


def main():
    ap = argparse.ArgumentParser(description="Cheap per-second visual signals for a source video")
    ap.add_argument("video")
    ap.add_argument("--out", default=None, help="write signals JSON here")
    ap.add_argument("--beat-json", default=None, help="beat grid to align samples against")
    ap.add_argument("--start", type=float, default=None, help="first sample time (s)")
    ap.add_argument("--end", type=float, default=None, help="last sample time (s)")
    ap.add_argument("--every", type=float, default=1.0, help="sample period in seconds")
    ap.add_argument("--width", type=int, default=GREY_W, help="downscale width in px")
    ap.add_argument("--workers", type=int, default=3, help="parallel fast-seeks")
    ap.add_argument("--max-samples", type=int, default=20000, help="hard sample cap")
    ap.add_argument("--used-spans", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.video):
        print("ERROR: no such file: %s" % args.video, file=sys.stderr)
        return 2
    if args.every <= 0:
        print("ERROR: --every must be > 0", file=sys.stderr)
        return 2

    start = 0.0 if args.start is None else float(args.start)
    if args.end is None:
        end = start + 60.0     # bounded default: never walk a 17 GB file unasked
    else:
        end = float(args.end)
    if end < start:
        print("ERROR: --end before --start", file=sys.stderr)
        return 2

    times = []
    t = start
    while t <= end + 1e-9 and len(times) < args.max_samples:
        times.append(round(t, 3))
        t += args.every
    times = sorted(set(times))

    existing = {}
    if args.resume and args.out and os.path.exists(args.out):
        old = json.load(open(args.out))
        for s in old.get("samples", []):
            if not s.get("error"):
                existing[round(float(s["t"]), 3)] = s

    todo = [t for t in times if t not in existing]
    print("[visual_signals] %s  samples=%d  (todo=%d, reused=%d, workers=%d)"
          % (args.video, len(times), len(todo), len(times) - len(todo), args.workers),
          file=sys.stderr)

    # Sequential in time order, but the ffmpeg calls themselves run in a small
    # pool; motion/hist_novelty are then chained in strict time order so the
    # result does not depend on completion order (determinism).
    fresh = {}
    if todo:
        def work(tt):
            return tt, sample_frame(args.video, tt, width=args.width)

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            for tt, frame in ex.map(work, todo):
                if frame is None:
                    fresh[tt] = {"t": tt, "error": "frame not readable",
                                 "motion": None, "brightness": None,
                                 "sharpness": None, "hist": None,
                                 "hist_novelty": None, "obstruction": None,
                                 "black_sev": None, "white_sev": None,
                                 "detail": None, "frozen": None}
                    continue
                h = histogram(frame)
                bright = float(frame.mean())
                sharp = laplacian_variance(frame)
                obs, bsev, wsev, det = obstruction_components(bright, sharp)
                fresh[tt] = {"t": tt, "_frame": frame, "_hist": h,
                             "brightness": round(bright, 3),
                             "sharpness": round(sharp, 3),
                             "hist": [round(float(x), 5) for x in h],
                             "obstruction": round(obs, 4),
                             "black_sev": round(bsev, 4),
                             "white_sev": round(wsev, 4),
                             "detail": round(det, 4)}

    # Chain motion / hist_novelty over the full ordered sample set. With
    # --resume the file's own samples are KEPT (union), so resuming for a small
    # range never shrinks the artifact.
    all_times = sorted(set(times) | set(existing.keys()))
    samples = []
    prev_frame = None
    for tt in all_times:
        s = fresh.get(tt) or existing.get(tt)
        if s is None:
            continue
        if "_frame" in s:
            frame = s.pop("_frame")
            s.pop("_hist", None)
            if prev_frame is not None and prev_frame.shape == frame.shape:
                motion = float(np.abs(frame.astype(np.int16)
                                      - prev_frame.astype(np.int16)).mean()) / 255.0
            else:
                motion = 0.0
            s["motion"] = round(motion, 4)
            s["hist_novelty"] = None
            s["frozen"] = False
            s["used"] = False
            prev_frame = frame
        else:
            s.pop("_hist", None)
            if not isinstance(s.get("motion"), (int, float)):
                s["motion"] = 0.0
            s.setdefault("used", False)
            s.setdefault("frozen", False)
        samples.append(s)

    # hist_novelty is (re)computed from the stored 16-bin histograms over the
    # ordered union, so a fresh run and a resumed run agree on it.
    prev_hist = None
    for s in samples:
        h = s.get("hist")
        if h:
            s["hist_novelty"] = (None if prev_hist is None
                                 else round(1.0 - hist_intersection(np.asarray(h),
                                                                    np.asarray(prev_hist)), 4))
            prev_hist = h
        else:
            s["hist_novelty"] = None

    used = load_used_spans(args.used_spans)
    for s in samples:
        if s.get("t") is not None and used:
            s["used"] = any(a <= s["t"] <= b for a, b in used)

    # Frozen runs: >= FROZEN_RUN consecutive samples with motion under threshold.
    run = []
    for s in samples:
        m = s.get("motion")
        if m is not None and m < MOTION_FROZEN_THRESH:
            run.append(s)
        else:
            if len(run) >= FROZEN_RUN:
                for q in run:
                    q["frozen"] = True
            run = []
    if len(run) >= FROZEN_RUN:
        for q in run:
            q["frozen"] = True

    # ---- summary --------------------------------------------------------
    def col(key):
        return [s[key] for s in samples if isinstance(s.get(key), (int, float))]

    motion = col("motion")
    bright = col("brightness")
    sharp = col("sharpness")
    nov = [s["hist_novelty"] for s in samples
           if isinstance(s.get("hist_novelty"), (int, float))]
    obs = [s["obstruction"] for s in samples
           if isinstance(s.get("obstruction"), (int, float))]
    n = len(samples)
    n_unusable = sum(1 for v in obs if v >= OBSTRUCTION_FLAG)
    n_frozen = sum(1 for s in samples if s.get("frozen"))
    n_clean = sum(1 for s in samples
                  if isinstance(s.get("obstruction"), (int, float))
                  and s["obstruction"] < OBSTRUCTION_FLAG and not s.get("frozen"))
    summary = {
        "n_samples": n,
        "t_start": samples[0]["t"] if samples else None,
        "t_end": samples[-1]["t"] if samples else None,
        "span_s": round((samples[-1]["t"] - samples[0]["t"]), 3) if samples else None,
        "width_px": args.width,
        "fps_sampled": round(1.0 / args.every, 6),
        "error_samples": sum(1 for s in samples if s.get("error")),
        "motion": {"mean": rnd(mean(motion)), "p10": rnd(pct(motion, 10)),
                   "p90": rnd(pct(motion, 90)), "max": rnd(max(motion) if motion else 0.0)},
        "brightness": {"mean": rnd(mean(bright)), "p10": rnd(pct(bright, 10)),
                       "p90": rnd(pct(bright, 90)),
                       "min": rnd(min(bright) if bright else 0.0),
                       "max": rnd(max(bright) if bright else 0.0)},
        "sharpness": {"mean": rnd(mean(sharp)), "p10": rnd(pct(sharp, 10)),
                      "p50": rnd(pct(sharp, 50)), "p90": rnd(pct(sharp, 90))},
        "hist_novelty": {"mean": rnd(mean(nov)), "p90": rnd(pct(nov, 90))},
        "flat": {"count": sum(1 for s in samples
                              if isinstance(s.get("detail"), (int, float))
                              and s["detail"] < 0.5),
                 "detail_floor": DETAIL_FLOOR},
        "obstruction": {"count": n_unusable,
                        "frac": rnd(float(n_unusable) / n if n else 0.0)},
        "frozen": {"count": n_frozen,
                   "frac": rnd(float(n_frozen) / n if n else 0.0)},
        "clean_frac": rnd(float(n_clean) / n if n else 0.0),
        "used_frac": rnd(float(sum(1 for s in samples if s.get("used"))) / n if n else 0.0),
        "thresholds": {"black_level": BLACK_LEVEL, "white_level": WHITE_LEVEL,
                       "detail_floor": DETAIL_FLOOR,
                       "obstruction_flag": OBSTRUCTION_FLAG,
                       "frozen_motion": MOTION_FROZEN_THRESH,
                       "frozen_run": FROZEN_RUN},
    }

    if args.beat_json and os.path.exists(args.beat_json):
        bd = json.load(open(args.beat_json))
        beats = [b["time"] if isinstance(b, dict) else b for b in bd.get("beats", [])]
        beats = np.array([float(b) for b in beats], dtype=np.float64)
        in_span = beats[(beats >= (samples[0]["t"] if samples else 0))
                        & (beats <= (samples[-1]["t"] if samples else 0))] if beats.size else beats
        if beats.size and samples:
            st = np.array([s["t"] for s in samples], dtype=np.float64)
            d = np.abs(st[:, None] - beats[None, :]).min(axis=1)
            summary["beats"] = {
                "source": args.beat_json,
                "bpm": bd.get("bpm"),
                "n_beats_total": int(beats.size),
                "n_beats_in_span": int(in_span.size),
                "mean_dist_to_nearest_beat": rnd(float(d.mean())),
                "p90_dist_to_nearest_beat": rnd(float(np.percentile(d, 90))),
                "aligned_frac_50ms": rnd(float((d <= 0.05).mean())),
            }
        else:
            summary["beats"] = {"source": args.beat_json, "n_beats_total": int(beats.size)}

    out = {"source": args.video,
           "tool": "visual_signals.py",
           "sampling": {"method": "ffmpeg fast-seek single frame, grey, %dpx" % args.width,
                        "every_s": args.every,
                        "requested_start": start, "requested_end": end,
                        "first_sample": samples[0]["t"] if samples else None,
                        "last_sample": samples[-1]["t"] if samples else None,
                        "n_samples": len(samples)},
           "fps_sampled": round(1.0 / args.every, 6),
           "samples": samples,
           "summary": summary}

    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, indent=1)
        print("VISUAL SIGNALS %s -> %s (%d samples, clean_frac=%.3f, obstruction=%d, frozen=%d)"
              % (args.video, args.out, n, summary["clean_frac"],
                 n_unusable, n_frozen))
    else:
        print(json.dumps(out, indent=1))
    if args.json:
        print(json.dumps({"source": out["source"], "summary": summary}, indent=1))
    return 0


def mean(xs):
    return float(np.mean(xs)) if xs else 0.0


def pct(xs, p):
    return float(np.percentile(xs, p)) if xs else 0.0


def rnd(x):
    return round(float(x), 4)


if __name__ == "__main__":
    sys.exit(main())
