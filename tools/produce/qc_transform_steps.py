#!/usr/bin/env python3
"""qc_transform_steps.py — the pure transform discontinuity of a time-varying camera match.

WHY THIS AND NOT A FRAME-TO-FRAME MEASUREMENT: comparing two frames 0.6 s apart mixes the footage's
own motion and lighting changes with the grade movement, so a large delta proves nothing. This test
removes that confound: it takes ONE decoded source frame at a join and pushes that identical frame
through the transform applied just BEFORE the join and the transform applied just AFTER it. Anything
measured is therefore the transform's own discontinuity, with zero photographic content between them.

The renderer evaluates a chunk's curve at the CHUNK MIDPOINT, so at a join at time t the two active
transforms are curve_at(t - max_chunk_s/2) and curve_at(t + max_chunk_s/2).

Usage:
  qc_transform_steps.py --record camera_match_a_tv2.json --source CARD.mp4 --grade "<ffmpeg chain>" \
                        [--cadence 30] [--step-max 1.0] [--json-out OUT]
Exit: 0 = PASS, 1 = FAIL, 2 = error.
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "lib")))
from camera_match import curve_at  # noqa: E402

W, H = 160, 90
CROP = "scale=960:540,crop=iw*0.87:ih*0.87,scale=%d:%d" % (W, H)   # the A push-in geometry

def apply_chain(src, t, curve, grade, cached_png):
    """The SAME source frame through GEOM + curve + grade, and through GEOM + grade alone."""
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", "%.3f" % t,
                    "-i", src, "-frames:v", "1", "-vf", CROP, "-y", cached_png],
                   capture_output=True, check=False)
    outs = {}
    for tag, vf in (("with", curve + "," + grade), ("without", grade)):
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", cached_png,
                            "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True)
        a = np.frombuffer(r.stdout, np.uint8)
        outs[tag] = a.reshape(-1, 3).astype(np.float64) if a.size else None
    return outs


def stats(px):
    if px is None:
        return None
    y = 0.2126 * px[:, 0] + 0.7152 * px[:, 1] + 0.0722 * px[:, 2]
    return {"luma": float(y.mean()),
            "sat": float((px.max(axis=1) - px.min(axis=1)).mean()),
            "rgb": [float(px[:, i].mean()) for i in range(3)]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--grade", required=True)
    ap.add_argument("--cadence", type=float, default=30.0,
                    help="evaluate a join every N seconds of source time")
    ap.add_argument("--step-max", type=float, default=1.0,
                    help="max luma step across a join that counts as invisible")
    ap.add_argument("--sat-step-max", type=float, default=0.6,
                    help="max saturation step across a join")
    ap.add_argument("--rgb-step-max", type=float, default=0.8,
                    help="max single-channel step across a join")
    ap.add_argument("--window", type=float, default=None,
                    help="override the half-window (default max_chunk_s/2 from the record)")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--label", default="")
    a = ap.parse_args()

    rec = json.load(open(a.record))
    tv = rec.get("solved_pre_grade")
    if not isinstance(tv, dict) or not tv.get("points"):
        print(json.dumps({"verdict": "ERROR", "error": "record is not a time_varying match"}))
        return 2
    mcs = float(tv.get("max_chunk_s") or 30.0)
    half = a.window if a.window is not None else mcs / 2.0
    t_hi = max(float(p["t"]) for p in tv["points"])

    tmp = "/tmp/_qc_transform_step.png"
    rows, bad = [], []
    t = a.cadence
    while t < t_hi:
        before = curve_at(tv["points"], max(0.0, t - half))
        after = curve_at(tv["points"], min(t_hi, t + half))
        if before == after:
            t += a.cadence
            continue
        s_b = stats(apply_chain(a.source, t, before, a.grade, tmp)["with"])
        s_a = stats(apply_chain(a.source, t, after, a.grade, tmp)["with"])
        s_c = stats(apply_chain(a.source, t, "null", a.grade, tmp)["without"])
        if not (s_b and s_a):
            rows.append({"t": round(t, 3), "error": "no frame decoded"})
            t += a.cadence
            continue
        dl = s_a["luma"] - s_b["luma"]
        ds = s_a["sat"] - s_b["sat"]
        drgb = [round(s_a["rgb"][i] - s_b["rgb"][i], 3) for i in range(3)]
        fails = []
        if abs(dl) > a.step_max:
            fails.append("luma step %+.3f > %.2f" % (dl, a.step_max))
        # a saturation or single-channel jump is a discontinuity too: a term that switches on at a
        # boundary instead of ramping shows up here and nowhere else (found by inspection 2026-09-15)
        if abs(ds) > a.sat_step_max:
            fails.append("sat step %+.3f > %.2f" % (ds, a.sat_step_max))
        if max(abs(x) for x in drgb) > a.rgb_step_max:
            fails.append("rgb step %s > %.2f" % (drgb, a.rgb_step_max))
        row = {"t": round(t, 3), "luma_step": round(dl, 4), "sat_step": round(ds, 4),
               "rgb_step": drgb, "luma_at_join": round(s_c["luma"], 2),
               "pass": not fails, "failures": fails}
        rows.append(row)
        if fails:
            bad.append(row)
        t += a.cadence

    steps = [abs(r["luma_step"]) for r in rows if "luma_step" in r]
    out = {"verdict": "PASS" if (rows and not bad) else ("FAIL" if bad else "ERROR"),
           "label": a.label, "record": a.record, "max_chunk_s": mcs, "half_window_s": half,
           "cadence_s": a.cadence, "step_max": a.step_max, "sat_step_max": a.sat_step_max, "rgb_step_max": a.rgb_step_max,
           "joins": len(rows),
           "max_abs_sat_step": (round(max(abs(r["sat_step"]) for r in rows if "sat_step" in r), 4)
                                if rows else None),
           "max_abs_luma_step": round(max(steps), 4) if steps else None,
           "mean_abs_luma_step": round(float(np.mean(steps)), 4) if steps else None,
           "note": ("the same decoded frame through the transform either side of each join: this "
                    "measures the transform, not the footage"),
           "rows": rows}
    if a.json_out:
        json.dump(out, open(a.json_out, "w"), indent=1)
    print(json.dumps({k: out[k] for k in ("verdict", "joins", "max_chunk_s", "half_window_s",
                                          "max_abs_luma_step", "mean_abs_luma_step")}))
    for r in bad[:6]:
        print("  FAIL t=%s %s" % (r["t"], r["failures"]))
    return 0 if out["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
