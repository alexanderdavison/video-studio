#!/usr/bin/env python3
"""test_resolver_agreement.py — all camera-match consumers must resolve the same transform.

Run:
  /opt/video-studio/tools/venv/bin/python /opt/video-studio/tools/tests/test_resolver_agreement.py
  /opt/video-studio/tools/venv/bin/python /opt/video-studio/tools/tests/test_resolver_agreement.py --qc-impl legacy

Exit: 0 = PASS, 1 = FAIL.
"""
import argparse
import importlib.util
import json
import os
import re
import sys

ROOT = "/opt/video-studio"
RECORD = ("/opt/video-studio/jobs/club-dispatch-set02-2026-08-24/analysis/"
          "camera_match_a_tv2e.json")

sys.path.insert(0, os.path.join(ROOT, "tools", "lib"))
import camera_match as cm  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rp = _load(os.path.join(ROOT, "tools", "proxy", "render_proxy.py"), "rp")
rf = _load(os.path.join(ROOT, "tools", "render", "render_final.py"), "rf")
qc = _load(os.path.join(ROOT, "tools", "produce", "qc_transform_steps.py"), "qc")

EQ_RE = re.compile(r"eq=([A-Za-z0-9_]+)=([0-9.]+)")
CURVE_RE = re.compile(r"(r|g|b|all)='([^']*)'")


def legacy_parse_curve(curve):
    out = {}
    for ch, body in CURVE_RE.findall(curve or ""):
        pts = []
        for pair in body.split():
            if "/" in pair:
                x, y = pair.split("/", 1)
                try:
                    pts.append((float(x), float(y)))
                except ValueError:
                    pass
        if pts:
            out[ch] = pts
    return out


def legacy_curve_str(chans):
    return "curves=" + ":".join("%s='%s'" % (ch, " ".join("%.4f/%.4f" % (x, y) for x, y in pts))
                                for ch, pts in chans.items())


def legacy_split_pre(pre):
    lut, extras = "", {}
    for part in (pre or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("curves="):
            lut += part
            continue
        m = EQ_RE.match(part)
        if m:
            extras[m.group(1)] = float(m.group(2))
    return lut, extras


def legacy_interp_curve(c1, c2, f):
    if f <= 0:
        return c1
    if f >= 1:
        return c2
    lut1, ex1 = legacy_split_pre(c1)
    lut2, ex2 = legacy_split_pre(c2)
    a, b = legacy_parse_curve(lut1), legacy_parse_curve(lut2)
    out = {}
    for ch, pa in a.items():
        pb = b.get(ch) or b.get("all") or pa
        ys = [(y + ((pb[i][1] if i < len(pb) else y) - y) * f) for i, (x, y) in enumerate(pa)]
        out[ch] = list(zip([p[0] for p in pa], ys))
    blended = {}
    for k in set(ex1) | set(ex2):
        v1 = ex1.get(k, ex2.get(k))
        v2 = ex2.get(k, ex1.get(k))
        blended[k] = v1 + (v2 - v1) * f
    s = legacy_curve_str(out)
    for k, v in blended.items():
        s += ",eq=%s=%.4f" % (k, v)
    return s


def legacy_curve_at(points, t):
    pts = sorted(points, key=lambda p: float(p["t"]))
    if t <= float(pts[0]["t"]):
        return pts[0]["curve"]
    if t >= float(pts[-1]["t"]):
        return pts[-1]["curve"]
    for i in range(len(pts) - 1):
        t0, t1 = float(pts[i]["t"]), float(pts[i + 1]["t"])
        if t0 <= t <= t1:
            return legacy_interp_curve(pts[i]["curve"], pts[i + 1]["curve"],
                                       0.0 if t1 <= t0 else (t - t0) / (t1 - t0))
    return pts[-1]["curve"]


def timestamps(points):
    pts = sorted(float(p["t"]) for p in points)
    out = [pts[3]] if len(pts) > 3 else [pts[0]]  # exact control point timestamp
    for i in range(len(pts) - 1):
        out.append((pts[i] + pts[i + 1]) / 2.0)    # one per interval
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qc-impl", choices=["current", "legacy"], default="current")
    args = ap.parse_args()

    rec = json.load(open(RECORD))
    tv = rec["solved_pre_grade"]
    points = tv["points"]
    ts = timestamps(points)

    qc_resolver = qc.curve_at if args.qc_impl == "current" else legacy_curve_at
    qc_name = "qc_transform_steps.curve_at" if args.qc_impl == "current" else "legacy_qc.curve_at"

    print("== resolver agreement (%s) ==" % args.qc_impl)
    fails = []
    sat_hits = 0
    for t in ts:
        proxy_curve = rp.tv_chunks(tv, t - 0.25, t + 0.25, 0.0, 4.0, 60.0)[0]["curve"]
        final_curve = rf.CM.tv_chunks(tv, t - 0.25, t + 0.25, 0.0, 4.0, 60.0)[0]["curve"]
        got = {
            "camera_match.curve_at": cm.curve_at(points, t),
            "proxy.render_proxy.tv_chunks": proxy_curve,
            "render.render_final.CM.tv_chunks": final_curve,
            qc_name: qc_resolver(points, t),
        }
        vals = list(got.values())
        ok = all(v == vals[0] for v in vals[1:])
        if "eq=saturation=" in vals[0]:
            sat_hits += 1
        print("  t=%8.3f  %s" % (t, "PASS" if ok else "FAIL"))
        if not ok:
            fails.append((t, got))

    if sat_hits == 0:
        fails.append(("sidecar", {"error": "no timestamp resolved eq=saturation"}))
        print("  sidecar saturation term present             FAIL")
    else:
        print("  sidecar saturation term present             PASS (%d timestamps)" % sat_hits)

    if fails:
        print("\nFAIL: %d disagreements" % len(fails))
        first = fails[0]
        print("  first mismatch at %s" % first[0])
        for name, value in first[1].items():
            print("    %-34s %s" % (name + ":", value))
        return 1

    print("\nPASS: canonical/proxy/final/qc resolvers agree at %d timestamps" % len(ts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
