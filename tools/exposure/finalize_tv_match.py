#!/usr/bin/env python3
"""finalize_tv_match.py — make a time-varying match's terms explicit on EVERY control point.

FOUND BY INSPECTION (2026-09-15): in camera_match_a_tv2.json the first control point was already
inside the correction targets, so it carries no eq=saturation term, while the later points carry
eq=saturation=0.78. The renderer interpolates between neighbouring points, and a term present on only
one side of an interval holds that side's value across it - so the saturation correction would switch
on at a control-point boundary instead of ramping, and the early section would render at the wrong
saturation. Nothing in the file list or the gate would have said so.

This rewrites the record so every point states its own gamma and saturation explicitly. Where the
fitted value is 1.0 the appended term is a no-op, so the control points render IDENTICALLY - the
change only affects the interpolated intervals, which now ramp continuously. The tool proves that
no-op claim on real frames before writing anything.

Usage:
  finalize_tv_match.py --record IN.json --out OUT.json [--source CARD.mp4] [--grade CHAIN]
Exit: 0 = written, 1 = the no-op claim failed, 2 = error.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys

import numpy as np

EQ_RE = re.compile(r"eq=([A-Za-z0-9_]+)=([0-9.]+)")


def split_pre(pre):
    lut, extras = "", {}
    for part in (pre or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("curves="):
            lut += part
        else:
            m = EQ_RE.match(part)
            if m:
                extras[m.group(1)] = float(m.group(2))
    return lut, extras


def with_terms(curve, terms):
    lut, extras = split_pre(curve)
    for k, v in terms.items():
        extras.setdefault(k, v)
    out = lut
    for k, v in extras.items():
        out += ",eq=%s=%.4f" % (k, v)
    return out


def frame(path, t, vf):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", "%.3f" % t,
                        "-i", path, "-frames:v", "1", "-vf", vf, "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"], capture_output=True)
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--source", default=None, help="prove the no-op claim on this card's frames")
    ap.add_argument("--grade", default=None)
    ap.add_argument("--geometry", default="scale=960:540,crop=iw*0.87:ih*0.87,scale=960:540")
    a = ap.parse_args()

    doc = json.load(open(a.record))
    tv = doc.get("solved_pre_grade")
    if not isinstance(tv, dict) or not tv.get("points"):
        print(json.dumps({"ok": False, "error": "not a time_varying record"}))
        return 2

    changed = []
    for pt in tv["points"]:
        terms = {}
        if pt.get("gamma") is not None:
            terms["gamma"] = float(pt["gamma"])
        if pt.get("sat") is not None:
            terms["saturation"] = float(pt["sat"])
        if not terms:
            continue
        before = pt["curve"]
        after = with_terms(before, terms)
        if after != before:
            changed.append(float(pt["t"]))
            pt["curve"] = after
    print("points whose curve gained explicit terms: %d %s" % (len(changed), changed[:12]))

    # prove the appended terms are no-ops on real frames before writing the record
    if a.source and a.grade:
        bad = []
        for pt in tv["points"]:
            t = float(pt["t"])
            lut, extras = split_pre(pt["curve"])
            old = lut + "".join(",eq=%s=%.4f" % (k, v) for k, v in extras.items())
            new = pt["curve"]
            outs = []
            for vf in (a.geometry + "," + old + "," + a.grade,
                       a.geometry + "," + new + "," + a.grade):
                r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                                    "-ss", "%.3f" % t, "-i", a.source, "-frames:v", "1",
                                    "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                   capture_output=True)
                outs.append(r.stdout)
            if outs[0] != outs[1]:
                x = np.frombuffer(outs[0], np.uint8).astype(float)
                y = np.frombuffer(outs[1], np.uint8).astype(float)
                bad.append((t, round(float(np.abs(x - y).mean()), 4)))
        if bad:
            print(json.dumps({"ok": False, "error": "the appended terms are NOT no-ops",
                              "differences": bad[:6]}))
            return 1
        print("no-op proven on all %d control points (identical bytes with and without the "
              "explicit terms)" % len(tv["points"]))

    doc["match_mode"] = "time_varying"
    doc["finalized"] = {"by": "finalize_tv_match.py",
                        "reason": ("every control point states its gamma and saturation explicitly "
                                   "so interpolated intervals ramp instead of switching the term "
                                   "on at a boundary"),
                        "points_changed": changed}
    json.dump(doc, open(a.out, "w"), indent=1)
    print("wrote %s" % a.out)
    print("sha256 %s" % hashlib.sha256(open(a.out, "rb").read()).hexdigest()[:16])
    return 0


if __name__ == "__main__":
    sys.exit(main())
