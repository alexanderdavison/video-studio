#!/usr/bin/env python3
"""Test 4 reason-grounding: is the 'B is dark' reasoning factually supported?

Camera normalization deliberately brings B up to A's level, so if the model reasons that B
is darker, either the normalization does not hold in the frames actually sent, or the
reasoning is ungrounded. Measure it on the exact frames that were transmitted.
"""
import glob
import json
import os
import re
import subprocess
import statistics as st

PAY = "/root/test1/payload_test4"


def yavg(p):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-i", p, "-vf",
                        "signalstats,metadata=print:file=-", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"YAVG=([0-9.]+)", r.stdout)
    return float(m.group(1)) if m else None


print("per-event mean luma (YAVG) of the transmitted A and B dense frames, 5-frame sample")
print("%-8s %8s %8s %8s   %s" % ("event", "A mean", "B mean", "B-A", "verdict"))
rows = []
for d in sorted(glob.glob(PAY + "/events/evt_*")):
    eid = os.path.basename(d)
    out = {}
    for cam in ("a", "b"):
        fs = sorted(glob.glob("%s/%s_*.jpg" % (d, cam)))
        step = max(1, len(fs) // 5)
        vals = [v for v in (yavg(p) for p in fs[::step]) if v is not None]
        out[cam] = st.mean(vals) if vals else None
    delta = out["b"] - out["a"]
    verdict = ("B is darker" if delta < -2.0 else
               "B is brighter" if delta > 2.0 else "B ~= A (normalized)")
    rows.append({"event": eid, "a": round(out["a"], 2), "b": round(out["b"], 2),
                 "delta": round(delta, 2), "verdict": verdict})
    print("%-8s %8.2f %8.2f %+8.2f   %s" % (eid, out["a"], out["b"], delta, verdict))

dark = [r["event"] for r in rows if r["delta"] < -2.0]
print()
print("events where B is materially darker (>2 luma): %s" % (dark or "NONE"))

print()
print("== rationales that invoke darkness ==")
parsed = json.load(open(sorted(glob.glob("/root/test1/responses_test4/parsed_*.json"))[-1]))
pat = re.compile(r"dark|dimmer|dim\b|exposure|underlit|bright", re.I)
for x in parsed["decisions"]:
    if pat.search(x["rationale"]):
        eid = x["event_id"]
        r = next(r for r in rows if r["event"] == eid)
        print("  %s  conf %.2f  B-A luma %+.2f  (%s)"
              % (eid, x["confidence_score"], r["delta"], r["verdict"]))
        print("      %s" % x["rationale"])

print()
print("== timecode citations, resolved ==")
c = json.load(open("/root/test1/comparison_test4.json"))
for eid, cited, where, t in c["timecode_citations"]:
    ev = next(e for e in json.load(open(PAY + "/evidence_pack.json"))["events"]
              if e["event_id"] == eid)
    av = ev["evidence"]["a"]["span_s"]
    exact = "== A span start" if abs(t - av[0]) < 0.001 else ""
    print("  %s  %s  -> %-20s (t=%.3f) %s" % (eid, cited, where, t, exact))
