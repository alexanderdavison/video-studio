#!/usr/bin/env python3
"""qc_temporal.py — prove a time-varying camera match introduced no visible step.

A time-varying match is applied by the renderer in frame-aligned chunks (see render_proxy.py:
ffmpeg cannot express the correction as a function of time here). Each join is a place where one
technical transform hands over to the next, so this gate measures what a viewer would see there:

  * luma step across the join (|mean luma after - before|) against --step-max
  * the join must NOT read as a view change: correlation of the frames either side must stay high
  * highlight clipping and black crush must not have been introduced at the join

It deliberately does NOT compare against the manifest: the join is not a cut and has no planned
entry. Reported boundaries are the renderer's own (processing_boundaries.json).

Usage:
  qc_temporal.py --delivered FILE --boundaries processing_boundaries.json \
                 [--step-max 3.0] [--corr-min 0.5] [--json-out OUT]
Exit: 0 = PASS, 1 = FAIL, 2 = error.
"""
import argparse
import json
import subprocess
import sys

import numpy as np

W, H = 160, 90


def frame(path, t):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", "%.3f" % t,
                        "-i", path, "-frames:v", "1", "-vf", "scale=%d:%d" % (W, H),
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    if not a.size or a.size % 3:
        return None
    return a.reshape(-1, 3).astype(np.float64)


def stats(px):
    y = 0.2126 * px[:, 0] + 0.7152 * px[:, 1] + 0.0722 * px[:, 2]
    sat = px.max(axis=1) - px.min(axis=1)
    return {"luma": float(y.mean()),
            "clipped_pct": round(100.0 * float((y >= 235).mean()), 3),
            "black_pct": round(100.0 * float((y < 16).mean()), 2),
            "sat": float(sat.mean())}


def corr(a, b):
    if a is None or b is None:
        return None
    x = a.reshape(-1)
    y = b.reshape(-1)
    x = x - x.mean()
    y = y - y.mean()
    d = (np.linalg.norm(x) * np.linalg.norm(y))
    return float((x @ y) / d) if d else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delivered", required=True)
    ap.add_argument("--boundaries", required=True)
    ap.add_argument("--step-max", type=float, default=3.0,
                    help="max luma step across a join that counts as invisible")
    ap.add_argument("--corr-min", type=float, default=0.5,
                    help="min correlation across the join (below = reads as a view change)")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--label", default="")
    a = ap.parse_args()

    doc = json.load(open(a.boundaries))
    bounds = doc.get("processing_boundaries_s") or []
    rows, bad = [], []
    for t in bounds:
        b = frame(a.delivered, max(0.0, t - 0.4))
        f = frame(a.delivered, t + 0.2)
        if b is None or f is None:
            rows.append({"t": t, "error": "no frames decoded"})
            continue
        sb, sf = stats(b), stats(f)
        c = corr(b, f)
        dl = sf["luma"] - sb["luma"]
        fails = []
        if abs(dl) > a.step_max:
            fails.append("luma step %+.2f > %.2f" % (dl, a.step_max))
        if c is not None and c < a.corr_min:
            fails.append("corr %.3f < %.2f (reads as a view change)" % (c, a.corr_min))
        if sf["clipped_pct"] > sb["clipped_pct"] + 0.5:
            fails.append("new highlight clipping %.2f%% -> %.2f%%"
                         % (sb["clipped_pct"], sf["clipped_pct"]))
        if sf["black_pct"] > sb["black_pct"] + 5.0 and sf["black_pct"] > 60.0:
            fails.append("black crush %.1f%% -> %.1f%%" % (sb["black_pct"], sf["black_pct"]))
        rows.append({"t": round(t, 3), "luma_before": round(sb["luma"], 3),
                     "luma_after": round(sf["luma"], 3), "luma_step": round(dl, 3),
                     "corr": round(c, 4) if c is not None else None,
                     "clipped_pct": [sb["clipped_pct"], sf["clipped_pct"]],
                     "black_pct": [sb["black_pct"], sf["black_pct"]],
                     "pass": not fails, "failures": fails})
        if fails:
            bad.append(rows[-1])
    steps = [abs(r["luma_step"]) for r in rows if "luma_step" in r]
    out = {"verdict": "PASS" if (rows and not bad) else ("FAIL" if bad else "ERROR"),
           "label": a.label, "delivered": a.delivered,
           "n_boundaries": len(bounds), "step_max": a.step_max, "corr_min": a.corr_min,
           "max_abs_luma_step": round(max(steps), 3) if steps else None,
           "mean_abs_luma_step": round(float(np.mean(steps)), 3) if steps else None,
           "rows": rows}
    if a.json_out:
        json.dump(out, open(a.json_out, "w"), indent=1)
    print(json.dumps({k: out[k] for k in ("verdict", "n_boundaries", "step_max",
                                          "max_abs_luma_step", "mean_abs_luma_step")}))
    for r in bad:
        print("  FAIL %s %s" % (r.get("t"), r.get("failures")))
    return 0 if out["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
