#!/usr/bin/env python3
"""Show real candidate sets with their actual durations and endpoints, and say why
each endpoint was chosen. Reads the duration-choice artifact; changes nothing."""
import json
import sys

d = json.load(open("/opt/video-studio/tools/analysis/p1_out/dur_300_420.json"))
want = sys.argv[1:] or ["evt_01", "evt_04", "evt_07", "evt_09"]

print("duration rules in force:")
for k, v in d["duration_choice_rules"].items():
    print("   %-22s %s" % (k, v))
print()
for e in d["events"]:
    if e["event_id"] not in want:
        continue
    dc = e["duration_choices"]
    print("=" * 112)
    print("%s   event boundary %.3f s   B slot [%.3f, %.3f]   %.3f s of slot available"
          % (e["event_id"], e["context"]["beat"], e["context"]["b_slot"][0],
             e["context"]["b_slot"][1], dc["limits"]["remaining_slot_width_s"]))
    print("   motion evidence: %d samples @ %.0f Hz, peak %.4f at %.3f s; "
          "action run %.3f -> %.3f s (>= %.4f motion)%s"
          % (dc["n_samples"], 1.0 / dc["sample_period_s"], dc["peak"]["motion"],
             dc["peak"]["t_s"], dc["action_run"]["start_s"], dc["action_run"]["end_s"],
             dc["action_run"]["threshold_motion"],
             "  [OPEN-ENDED: still moving at the window edge]"
             if dc["action_run"]["open_ended_at_window_edge"] else ""))
    if dc.get("fallback"):
        print("   fallback: %s" % dc["fallback"])
    for note in dc.get("collapsed") or []:
        print("   collapse: %s" % note)
    print()
    print("   %-10s %-4s %-8s %-9s %-9s %-9s  %s"
          % ("candidate", "ang", "action", "tl_start", "src_start", "length_s", "why this endpoint / length"))
    for c in e["candidates"]:
        lvl = (dc["levels"].get(c["id"].replace("b_", "")) if c["id"] != "hold_a" else None)
        why = ""
        for n in c.get("notes", []):
            if n.startswith(("B_GLANCE", "B_ACTION", "B_HOLD", "B_EARLY")):
                why = n
        if c["id"] == "hold_a":
            why = "HOLD_A — the do-nothing option: angle A is already on screen; always present"
        if not why:
            why = (c.get("notes") or [""])[0]
        print("   %-10s %-4s %-8s %-9.3f %-9.3f %-9.3f  %s"
              % (c["id"], c["angle"], c["action"], c["timeline_start"], c["start"],
                 c["duration_s"], why[:220]))
        if lvl:
            print("   %-10s      derived %.3f s -> %.3f s   clamp: %s"
                  % ("", lvl["derived_duration_s"], lvl["duration_s"], lvl["clamp"]))
    print()
