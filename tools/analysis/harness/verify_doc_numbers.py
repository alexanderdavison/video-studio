#!/usr/bin/env python3
"""Recompute every number in the defect document from the artifacts on disk."""
import json

LAG = 1.2783
C = json.load(open("/opt/video-studio/tools/analysis/p1_out/candidates_300_420.json"))
MAN = "/opt/video-studio/projects/2026-08-25-tester/work/analysis/editorial_pkg_300_600"
T2 = json.load(open(MAN + "/test2_normalized/evidence_pack.json"))

pol = C["policy"]
s_lo, s_hi = pol["b_search_window"]
n = len(C["events"])
slot = (s_hi - s_lo) / n
anchors = [e["context"]["beat"] for e in C["events"]]
print("b_search_window [%.3f, %.3f]  events %d  slot_len %.4f  (drift per event %.4f)"
      % (s_lo, s_hi, n, slot, slot - (anchors[-1] - anchors[0]) / (n - 1)))
print()
print("%-7s %9s %9s %9s %9s %9s %9s" %
      ("event", "anchor T", "slot orig", "origin off", "B src0", "B src0-T", "in-slot"))
disp = []
for i, e in enumerate(C["events"]):
    bd = next(c for c in e["candidates"] if c["id"] == "b_downbeat")
    T = e["context"]["beat"]
    so = s_lo + i * slot
    d = round(bd["start"] - T, 3)
    disp.append(d)
    print("%-7s %9.3f %9.3f %+9.3f %9.3f %+9.3f %+9.3f"
          % (e["event_id"], T, so, so - T, bd["start"], d, bd["start"] - so))
print()
print("displacement range %.3f .. %.3f   median %.3f   |max| %.3f"
      % (min(disp), max(disp), sorted(disp)[len(disp) // 2], max(abs(x) for x in disp)))
print("claims in the doc: range -0.784 .. +11.837, max 11.837, drift/event 1.162, "
      "slot 10.909, mean spacing 9.747")
print("mean spacing recomputed: %.3f" % ((anchors[-1] - anchors[0]) / (n - 1)))
slip = 398.254 - LAG
print("evt_11 shipped requirement %.3f vs package value 408.813 -> residual %.3f"
      % (slip, 408.813 - slip))
print()
print("=== Test 2 burst vs candidate overlap ===")
zero = 0
for e in T2["events"]:
    dw = e["dense_window"]["used"]
    bs = [c for c in e["candidates"] if c["angle"] == "B"]
    lo = min(c["start"] for c in bs)
    hi = max(c["end"] for c in bs)
    bl, bh = dw[0] - LAG, dw[1] - LAG
    ov = max(0.0, min(hi, bh) - max(lo, bl))
    zero += (ov == 0.0)
    print("  %s burst %.3f-%.3f  cand %.3f-%.3f  overlap %.3f of %.3f%s"
          % (e["event_id"], bl, bh, lo, hi, ov, hi - lo, "   ZERO" if ov == 0 else ""))
print("events with zero overlap: %d of %d" % (zero, len(T2["events"])))
print("doc claims: evt_02 6.114, evt_05 9.343, evt_10 11.667, evt_11 11.837; "
      "zero overlap on evt_05/06/08/09/10/11 (6 of 11)")
