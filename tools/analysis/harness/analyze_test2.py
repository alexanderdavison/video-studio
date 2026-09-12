#!/usr/bin/env python3
"""Test 2 analysis: validate, compare against Test 1 decision-by-decision, and materialize
the Test 2 A (deterministic) and B (OpenAI+reference) chains under the SAME frozen policy.

Nothing here tunes anything: policy, candidate ids, shot lengths and A-timeline windows are
inherited unchanged. The only differences from Test 1 are the approved sync mapping and the
approved camera normalization.
"""
import glob
import json
import os
import re
import sys

PAY = "/root/test1/payload_test2"
POLICY = {"minimum_b_shot": 4.0, "maximum_b_shot": 14.0, "minimum_a_recovery": 8.0,
          "cut_grid": "phrase", "low_confidence_action": "stay_on_a"}
SPAN = (300.0, 600.0)
PRICES = {"input_per_m": 2.00, "output_per_m": 12.00}

DARK = re.compile(r"\bdark\b|\bdarker\b|underexpos|too dark|near-black|black crush|exposur|unreadable|illegible|murky|poorly lit|low light|low-light", re.I)
EDIT = re.compile(r"\bhands?\b|\baction\b|\bmovement\b|\bbeat\b|\bpacing\b|\bpace\b|transition|reference|hold|timing|\bcut\b|\bphrase\b|\bgesture\b|\bfader|\bknob|\bdeck|\bframe\b", re.I)


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
        cand = next(c for c in e["candidates"] if c["id"] == sel)
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
    f2, dec2 = load_decisions("/root/test1/responses_test2/decisions_*.txt")
    f1, dec1 = load_decisions("/root/test1/responses/decisions_*.txt")
    ev = json.load(open(PAY + "/evidence_pack.json"))
    events = {e["event_id"]: e for e in ev["events"]}
    det = {d["event_id"]: d for d in json.load(open("/root/test1/det_winners.json"))}
    order = [e["event_id"] for e in ev["events"]]

    problems = []
    for eid in order:
        if eid not in dec2:
            problems.append("%s missing" % eid); continue
        legal = [c["id"] for c in events[eid]["candidates"]]
        if dec2[eid]["selection"] not in legal:
            problems.append("%s selected illegal %r" % (eid, dec2[eid]["selection"]))
        if not (0 <= float(dec2[eid]["confidence_score"]) <= 1):
            problems.append("%s confidence out of range" % eid)
        if len(dec2[eid].get("rationale", "")) > 240:
            problems.append("%s rationale over 240 chars" % eid)

    print("Test 2 response: %s | validation: %s" % (os.path.basename(f2),
                                                    "PASS" if not problems else "PROBLEMS"))
    for p in problems:
        print("   !", p)
    print()

    hdr = "%-7s %-8s %-10s %-10s %-10s %-6s %-6s %s" % (
        "event", "anchor", "DET", "TEST1", "TEST2", "conf1", "conf2", "change")
    print(hdr); print("-" * (len(hdr) + 6))
    n_changed = 0
    conf_up = conf_dn = 0
    for eid in order:
        a = det[eid]["winner"]; b1 = dec1[eid]["selection"]; b2 = dec2[eid]["selection"]
        c1 = dec1[eid]["confidence_score"]; c2 = dec2[eid]["confidence_score"]
        ch = "SAME" if b1 == b2 else "CHANGED %s -> %s" % (b1, b2)
        if b1 != b2:
            n_changed += 1
        if c2 > c1:
            conf_up += 1
        elif c2 < c1:
            conf_dn += 1
        print("%-7s %-8.3f %-10s %-10s %-10s %-6.2f %-6.2f %s" % (eid, det[eid]["anchor_t"], a, b1, b2, c1, c2, ch))
    print()
    print("decisions changed from Test 1 : %d/%d" % (n_changed, len(order)))
    print("confidence up / down          : %d / %d" % (conf_up, conf_dn))

    # rationale language shift
    d1 = sum(1 for e in order if DARK.search(dec1[e].get("rationale", "")))
    d2 = sum(1 for e in order if DARK.search(dec2[e].get("rationale", "")))
    e1 = sum(1 for e in order if EDIT.search(dec1[e].get("rationale", "")))
    e2 = sum(1 for e in order if EDIT.search(dec2[e].get("rationale", "")))
    print()
    print("rationales mentioning exposure/darkness : Test 1 %d/11   Test 2 %d/11" % (d1, d2))
    print("rationales citing action/pacing/ref     : Test 1 %d/11   Test 2 %d/11" % (e1, e2))

    ha1 = sum(1 for e in order if dec1[e]["selection"] == "hold_a")
    ha2 = sum(1 for e in order if dec2[e]["selection"] == "hold_a")
    print()
    print("HOLD_A selections : Test 1 %d/11   Test 2 %d/11" % (ha1, ha2))
    print("B selections      : Test 1 %d/11   Test 2 %d/11" % (11 - ha1, 11 - ha2))

    chain_a = {e: det[e]["winner"] for e in order}
    chain_b1 = {e: dec1[e]["selection"] for e in order}
    chain_b2 = {e: dec2[e]["selection"] for e in order}
    ca, ha = materialize(order, events, chain_a)
    cb1, hb1 = materialize(order, events, chain_b1)
    cb2, hb2 = materialize(order, events, chain_b2)
    print()
    for label, cuts, holds in (("A (deterministic)", ca, ha),
                               ("B Test 1 (OpenAI)", cb1, hb1),
                               ("B Test 2 (OpenAI)", cb2, hb2)):
        tot = sum(c["dur"] for c in cuts)
        print("%-20s B inserts=%d  B seconds=%.1f (%.1f%%)  holds=%d"
              % (label, len(cuts), tot, 100.0 * tot / (SPAN[1] - SPAN[0]), len(holds)))
        for c in cuts:
            print("     %s %s tl %.3f-%.3f src %.3f-%.3f (%.1fs)"
                  % (c["event"], c["candidate"], c["tl_in"], c["tl_out"],
                     c["src_in"], c["src_out"], c["dur"]))
    print()
    print("--- verbatim rationales, Test 1 -> Test 2 ---")
    for eid in order:
        print("%s:" % eid)
        print("   T1 (%s %.2f): %s" % (dec1[eid]["selection"], dec1[eid]["confidence_score"],
                                        dec1[eid].get("rationale", "")))
        print("   T2 (%s %.2f): %s" % (dec2[eid]["selection"], dec2[eid]["confidence_score"],
                                        dec2[eid].get("rationale", "")))

    led = json.load(open("/root/test1/transmission_ledger_test2.json"))
    u = led["usage"]
    cost = (u["prompt_tokens"] / 1e6 * PRICES["input_per_m"]
            + u["completion_tokens"] / 1e6 * PRICES["output_per_m"])
    print()
    print("transmission: HTTP %s | 1 request | %.1fs | prompt %d | completion %d (reasoning %d)"
          % (led["http_status"], led["latency_s"], u["prompt_tokens"],
             u["completion_tokens"], u["completion_tokens_details"]["reasoning_tokens"]))
    print("cost: $%.4f  (Test 1 was $0.3473)" % cost)
    print("images sent: %d | bytes: %d" % (len(led["files_sent"]), led["bytes_sent"]))

    json.dump({"response_file": f2, "decisions": dec2, "decisions_test1": dec1, "det": det,
               "problems": problems, "chain_a": ca, "chain_b": cb2, "chain_b_test1": cb1,
               "holds_a": ha, "holds_b": hb2, "holds_b_test1": hb1,
               "changed_from_test1": n_changed, "holda_test1": ha1, "holda_test2": ha2,
               "dark_rationales": {"test1": d1, "test2": d2},
               "edit_rationales": {"test1": e1, "test2": e2},
               "cost_usd": cost, "usage": u, "latency_s": led["latency_s"]},
              open("/root/test1/comparison_test2.json", "w"), indent=1)
    print("\nwrote /root/test1/comparison_test2.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
