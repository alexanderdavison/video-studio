#!/usr/bin/env python3
"""Test 3 analysis: validate the duration-choice decisions, compare them with Test 2
decision-by-decision, and materialize the Test 3 chain under the SAME frozen policy.

Nothing is tuned here: policy, event set, entry points and source windows are inherited.
The only new thing is that each selection now carries its own length.
"""
import glob
import json
import os
import re
import sys

PAY = "/root/test1/payload_test3"
POLICY = {"minimum_b_shot": 4.0, "maximum_b_shot": 14.0, "minimum_a_recovery": 8.0,
          "cut_grid": "phrase", "low_confidence_action": "stay_on_a"}
SPAN = (300.0, 600.0)
PRICES = {"input_per_m": 2.00, "output_per_m": 12.00}
DUR = re.compile(r"\b\d+(\.\d+)?\s?s\b|second|glance|brief|hold through|longer|shorter|"
                 r"duration|stay|extend|linger|overstay", re.I)


def load_decisions(pattern):
    f = sorted(glob.glob(pattern))[-1]
    t = open(f).read().strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        t = t[4:] if t.lower().startswith("json") else t
    d = json.loads(t)
    return f, {x["event_id"]: x for x in d["decisions"]}


def materialize(order, events, choice):
    cuts, last_b_exit, holds = [], None, []
    for eid in order:
        e = events[eid]
        sel = choice[eid]
        try:
            cand = next(c for c in e["candidates"] if c["id"] == sel)
        except StopIteration:
            continue          # id not offered on this event (control chains only)
        if sel == "hold_a":
            holds.append({"event": eid, "reason": "model chose hold_a"})
            continue
        rec = (cand["timeline_start"] - last_b_exit if last_b_exit is not None
               else cand["timeline_start"] - SPAN[0])
        if rec + 1e-6 < POLICY["minimum_a_recovery"]:
            holds.append({"event": eid, "reason": "policy: A recovery %.2fs < %.1fs (forced hold)"
                          % (rec, POLICY["minimum_a_recovery"])})
            continue
        cuts.append({"event": eid, "candidate": sel, "angle": "B",
                     "tl_in": cand["timeline_start"], "tl_out": cand["timeline_end"],
                     "dur": cand["duration_s"], "src_in": cand["start"], "src_out": cand["end"]})
        last_b_exit = cand["timeline_end"]
    return cuts, holds


def main():
    f3, dec3 = load_decisions("/root/test1/responses_test3/decisions_*.txt")
    f2, dec2 = load_decisions("/root/test1/responses_test2/decisions_*.txt")
    ev = json.load(open(PAY + "/evidence_pack.json"))
    events = {e["event_id"]: e for e in ev["events"]}
    det = {d["event_id"]: d for d in json.load(open("/root/test1/det_winners.json"))}
    order = [e["event_id"] for e in ev["events"]]

    problems = []
    for eid in order:
        if eid not in dec3:
            problems.append("%s missing" % eid); continue
        legal = [c["id"] for c in events[eid]["candidates"]]
        if dec3[eid]["selection"] not in legal:
            problems.append("%s selected illegal %r" % (eid, dec3[eid]["selection"]))
        if not (0 <= float(dec3[eid]["confidence_score"]) <= 1):
            problems.append("%s confidence out of range" % eid)
        if len(dec3[eid].get("rationale", "")) > 240:
            problems.append("%s rationale over 240 chars (%d)"
                            % (eid, len(dec3[eid]["rationale"])))
    print("Test 3 response: %s | validation: %s" % (os.path.basename(f3),
                                                    "PASS" if not problems else "PROBLEMS"))
    for p in problems:
        print("   !", p)
    print()

    hdr = "%-7s %-8s %-8s %-10s %-10s %-6s %-6s  %s" % (
        "event", "anchor", "DET", "TEST2", "TEST3", "conf2", "conf3", "change / length")
    print(hdr); print("-" * (len(hdr) + 8))
    durmap = {}
    for eid in order:
        a = det[eid]["winner"]; b2 = dec2[eid]["selection"]; b3 = dec3[eid]["selection"]
        c2 = dec2[eid]["confidence_score"]; c3 = dec3[eid]["confidence_score"]
        sel = next(c for c in events[eid]["candidates"] if c["id"] == b3)
        durmap[eid] = sel["duration_s"]
        ch = "SAME" if b2 == b3 else "CHANGED %s -> %s" % (b2, b3)
        print("%-7s %-8.3f %-8s %-10s %-10s %-6.2f %-6.2f  %s (%.3fs)"
              % (eid, det[eid]["anchor_t"], a, b2, b3, c2, c3, ch, sel["duration_s"]))
    print()

    n_changed = sum(1 for e in order if dec2[e]["selection"] != dec3[e]["selection"])
    ha2 = sum(1 for e in order if dec2[e]["selection"] == "hold_a")
    ha3 = sum(1 for e in order if dec3[e]["selection"] == "hold_a")
    print("decisions changed Test 2 -> Test 3 : %d/%d" % (n_changed, len(order)))
    print("HOLD_A selections                   : Test 2 %d/11   Test 3 %d/11" % (ha2, ha3))
    print("B selections                        : Test 2 %d/11   Test 3 %d/11"
          % (11 - ha2, 11 - ha3))
    d3 = sum(1 for e in order if DUR.search(dec3[e].get("rationale", "")))
    d2 = sum(1 for e in order if DUR.search(dec2[e].get("rationale", "")))
    print("rationales that reason about length : Test 2 %d/11   Test 3 %d/11" % (d2, d3))

    ca, ha = materialize(order, events, {e: det[e]["winner"] for e in order})
    # the deterministic chain is a CONTROL: it was built on the pre-increment candidate
    # faces (one canonical length per event), which are still on disk in the Test 2 pack.
    ev2 = json.load(open("/root/test1/payload_test2/evidence_pack.json"))
    events2 = {e["event_id"]: e for e in ev2["events"]}
    ca, ha = materialize(order, events2, {e: det[e]["winner"] for e in order})
    cb2, hb2 = materialize(order, events2, {e: dec2[e]["selection"] for e in order})
    cb3, hb3 = materialize(order, events, {e: dec3[e]["selection"] for e in order})
    print()
    for label, cuts, holds in (("A (deterministic)", ca, ha),
                               ("B Test 2 (OpenAI, all 4.0s)", cb2, hb2),
                               ("B Test 3 (OpenAI, duration choice)", cb3, hb3)):
        tot = sum(c["dur"] for c in cuts)
        print("%-34s B inserts=%d  B seconds=%.1f (%.1f%%)  holds=%d"
              % (label, len(cuts), tot, 100.0 * tot / (SPAN[1] - SPAN[0]), len(holds)))
        for c in cuts:
            print("     %s %-9s tl %.3f-%.3f src %.3f-%.3f  %.3fs"
                  % (c["event"], c["candidate"], c["tl_in"], c["tl_out"],
                     c["src_in"], c["src_out"], c["dur"]))
        for h in holds:
            print("     hold %s (%s)" % (h["event"], h["reason"]))
    print()
    print("--- Test 3 decisions verbatim ---")
    for eid in order:
        print("%s  %-9s %.2f  %s" % (eid, dec3[eid]["selection"],
                                     dec3[eid]["confidence_score"],
                                     dec3[eid].get("rationale", "")))

    led = json.load(open("/root/test1/transmission_ledger_test3.json"))
    u = led["usage"]
    cost = (u["prompt_tokens"] / 1e6 * PRICES["input_per_m"]
            + u["completion_tokens"] / 1e6 * PRICES["output_per_m"])
    print()
    print("transmission: HTTP %s | %.1fs | prompt %d | completion %d (reasoning %d) | "
          "images %d | cost $%.4f"
          % (led["http_status"], led["latency_s"], u["prompt_tokens"],
             u["completion_tokens"], u["completion_tokens_details"]["reasoning_tokens"],
             len(led["files_sent"]), cost))

    json.dump({"response_file": f3, "decisions": dec3, "decisions_test2": dec2, "det": det,
               "problems": problems, "chain_a": ca, "chain_b": cb3, "chain_b_test2": cb2,
               "holds_a": ha, "holds_b": hb3, "holds_b_test2": hb2,
               "changed_from_test2": n_changed, "holda_test2": ha2, "holda_test3": ha3,
               "length_rationales": {"test2": d2, "test3": d3},
               "b_inserts": {"test2": len(cb2), "test3": len(cb3)},
               "b_seconds": {"test2": sum(c["dur"] for c in cb2),
                             "test3": sum(c["dur"] for c in cb3)},
               "cost_usd": cost, "usage": u, "latency_s": led["latency_s"]},
              open("/root/test1/comparison_test3.json", "w"), indent=1)
    print("\nwrote /root/test1/comparison_test3.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
