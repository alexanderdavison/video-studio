#!/usr/bin/env python3
"""Before/after numbers for the sync-anchored entry increment."""
import json
from collections import Counter

P = "/opt/video-studio/tools/analysis/p1_out/"
old = json.load(open(P + "candidates_300_420.json"))       # frozen, flag off
durd = json.load(open(P + "dur_300_420.json"))             # duration choice, slot search
new = json.load(open(P + "sync_300_420.json"))             # sync-anchored
LAG = 1.2783
LEVELS = ("b_glance", "b_action", "b_hold")


def lens(ev):
    return "/".join("%.2f" % next(c for c in ev["candidates"] if c["id"] == k)["duration_s"]
                    for k in LEVELS)


print("%-7s %9s %11s %11s %10s | %-18s %s" %
      ("event", "T", "B entry OLD", "B entry NEW", "corrected", "lengths OLD", "-> NEW"))
for eo, ed, en in zip(old["events"], durd["events"], new["events"]):
    T = eo["context"]["beat"]
    o = next(c for c in eo["candidates"] if c["id"] == "b_downbeat")
    old_entry = round(o["start"] - LAG, 3)
    n = en["context"]["sync_entry"]["entry_source_s"]
    print("%-7s %9.3f %11.3f %11.3f %+10.3f | %-18s %s"
          % (eo["event_id"], T, old_entry, n, old_entry - n, lens(ed), lens(en)))

print()
for lbl, d in (("OLD (slot search)", durd), ("NEW (sync-anchored)", new)):
    vals = sorted(c["duration_s"] for e in d["events"] for c in e["candidates"] if c["angle"] == "B")
    print("%-20s B candidates=%d  distinct=%d  min=%.3f  median=%.3f  max=%.3f"
          % (lbl, len(vals), len(set(vals)), vals[0], vals[len(vals) // 2], vals[-1]))
    print("   distribution:", dict(Counter(vals)))

print()
print("invariant residuals :", [en["context"]["sync_entry"]["invariant_residual_s"] for en in new["events"]])
print("frame snaps         :", [en["context"]["sync_entry"]["frame_snap_s"] for en in new["events"]])
print("coverage_ok         :", set(en["context"]["sync_entry"]["coverage_ok"] for en in new["events"]))
print("b_slot (sync run)   :", set(str(en["context"]["b_slot"]) for en in new["events"]))
print("policy              :", json.dumps({k: v for k, v in new["policy"].items()
                                           if k in ("minimum_b_shot", "maximum_b_shot",
                                                    "minimum_a_recovery", "sync_anchored_entry",
                                                    "duration_choices")}))
print("b_early dropped     :", [en["context"]["sync_entry"].get("b_early")
                                 for en in new["events"] if en["context"]["sync_entry"].get("b_early")] or "none")
