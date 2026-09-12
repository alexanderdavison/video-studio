#!/usr/bin/env python3
"""Test 4 validation: mechanical checks on the model's decision chain, then the EXISTING
deterministic delivery policy, then the distributions.

Nothing is tuned. Policy is read from the canonical package, not restated.
"""
import glob
import json
import os
import re
import statistics as st

PAY = "/root/test1/payload_test4"
LAG = 1.2783
TOL = 0.036
REF_SPAN = (600.0, 1320.0)          # the approved reference excerpt, in reference source time

pack = json.load(open(PAY + "/evidence_pack.json"))
events = {e["event_id"]: e for e in pack["events"]}
order = [e["event_id"] for e in pack["events"]]
POLICY = pack["candidate_artifact"]["policy"]
MINB, MAXB, MINA = POLICY["minimum_b_shot"], POLICY["maximum_b_shot"], POLICY["minimum_a_recovery"]
SPAN = (pack["span"]["start"], pack["span"]["end"])

f = sorted(glob.glob("/root/test1/responses_test4/parsed_*.json"))[-1]
parsed = json.load(open(f))
decs = parsed["decisions"]
by_id = {d.get("event_id"): d for d in decs}

TS = re.compile(r"\b(\d{2}):(\d{2}):(\d{2})\.(\d{3})\b")
DUR = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:s\b|sec|second)")

problems, notes = [], []
print("== 1. mechanical validation ==")

# all events decided, nothing extra, nothing silently dropped
missing = [e for e in order if e not in by_id]
extra = [d.get("event_id") for d in decs if d.get("event_id") not in events]
dupes = [e for e in set(by_id) if sum(1 for d in decs if d.get("event_id") == e) > 1]
if missing:
    problems.append("events with no decision: %s" % missing)
if extra:
    problems.append("decisions for unknown events: %s" % extra)
if dupes:
    problems.append("duplicate decisions: %s" % dupes)
print("  events decided        : %d/%d   missing %s   extra %s   duplicate %s"
      % (len(order) - len(missing), len(order), missing or "none", extra or "none", dupes or "none"))

rows, ts_report, dur_report = [], [], []
for eid in order:
    d = by_id.get(eid)
    if d is None:
        continue
    ev = events[eid]
    legal = {c["id"]: c for c in ev["candidates"]}
    sel = d.get("selection")

    # schema
    for k, t in (("event_id", str), ("selection", str), ("confidence_score", float),
                 ("rationale", str)):
        if k not in d or not isinstance(d[k], t):
            try:
                float(d[k])
            except Exception:
                problems.append("%s: bad schema field %r" % (eid, k))
    conf = float(d.get("confidence_score", -1))
    if not (0.0 <= conf <= 1.0):
        problems.append("%s: confidence %s out of [0,1]" % (eid, conf))
    if len(d.get("rationale", "")) > 240:
        problems.append("%s: rationale %d chars > 240" % (eid, len(d["rationale"])))

    # legal id, no inventions
    if sel not in legal:
        problems.append("%s: selected %r which is NOT in the canonical candidate file %s"
                        % (eid, sel, sorted(legal)))
        continue
    cand = legal[sel]
    surv, why = False, ""

    if sel != "hold_a":
        # duration bounds
        if not (MINB - 1e-6 <= cand["duration_s"] <= MAXB + 1e-6):
            problems.append("%s/%s: duration %.3f outside [%.1f, %.1f]"
                            % (eid, sel, cand["duration_s"], MINB, MAXB))
        # sync anchoring is a property of the candidate, not the decision: verify it holds
        resid = (cand["timeline_start"] - cand["start"]) - LAG
        if abs(resid) > TOL:
            problems.append("%s/%s: sync invariant violated by %+.4f s" % (eid, sel, resid))
        # matched evidence
        bspan = ev["evidence"]["b"]["span_s"]
        if not (cand["start"] >= bspan[0] - 1e-3 and cand["end"] <= bspan[1] + 1e-3):
            problems.append("%s/%s: selected B span %.3f-%.3f NOT inside its B evidence %.3f-%.3f"
                            % (eid, sel, cand["start"], cand["end"], bspan[0], bspan[1]))
        if not (ev["evidence"]["a"]["n_frames"] > 0 and ev["evidence"]["b"]["n_frames"] > 0):
            problems.append("%s: matched A/B evidence missing" % eid)

    # timecode citations resolve to a real timebase the model was shown
    for m in TS.finditer(d.get("rationale", "")):
        t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000
        av = ev["evidence"]["a"]["span_s"]
        bv = ev["evidence"]["b"]["span_s"]
        where = ("event A evidence" if av[0] - 0.5 <= t <= av[1] + 0.5 else
                 "event B evidence" if bv[0] - 0.5 <= t <= bv[1] + 0.5 else
                 "reference material" if REF_SPAN[0] - 0.5 <= t <= REF_SPAN[1] + 0.5 else
                 "UNRESOLVED")
        ts_report.append((eid, m.group(0), where, round(t, 3)))
        if where == "UNRESOLVED":
            problems.append("%s: cited timecode %s does not exist in the evidence shown"
                            % (eid, m.group(0)))

    # duration citations resolve to a real candidate length
    for m in DUR.finditer(d.get("rationale", "")):
        v = float(m.group(1))
        durs = [c["duration_s"] for c in ev["candidates"]]
        match = min(durs, key=lambda x: abs(x - v))
        ok = abs(match - v) < 0.02
        dur_report.append((eid, v, ok, match))
        if not ok:
            notes.append("%s: cites %.3fs; nearest candidate length is %.3f s"
                         % (eid, v, match))

    rows.append({"event": eid, "selection": sel, "candidate": cand, "confidence": conf,
                 "rationale": d.get("rationale", ""), "is_b": sel != "hold_a"})

print("  every selection exists in that event's canonical candidate file : %s"
      % ("PASS" if not [p for p in problems if "NOT in the canonical" in p] else "FAIL"))
print("  schema parses cleanly                                           : %s"
      % ("PASS" if not [p for p in problems if "schema" in p] else "FAIL"))
print("  cited timecodes resolve                                          : %d/%d"
      % (sum(1 for r in ts_report if r[2] != "UNRESOLVED"), len(ts_report)))
print("  cited durations resolve to a real candidate length               : %d/%d"
      % (sum(1 for r in dur_report if r[2]), len(dur_report)))
print()

# ---- delivery policy: apply the EXISTING deterministic rules verbatim -------------
cuts, holds, last_b_exit = [], [], None
for eid in order:
    r = next((x for x in rows if x["event"] == eid), None)
    if r is None:
        holds.append({"event": eid, "reason": "no decision"})
        continue
    if not r["is_b"]:
        holds.append({"event": eid, "reason": "model chose hold_a"})
        continue
    c = r["candidate"]
    rec = (c["timeline_start"] - last_b_exit) if last_b_exit is not None \
        else (c["timeline_start"] - SPAN[0])
    if rec + 1e-6 < MINA:
        holds.append({"event": eid,
                      "reason": "policy: A recovery %.2fs < %.1fs (forced hold; the model chose %s)"
                                % (rec, MINA, r["selection"])})
        continue
    cuts.append({"event": eid, "candidate": r["selection"], "angle": "B",
                 "tl_in": c["timeline_start"], "tl_out": c["timeline_end"],
                 "dur": c["duration_s"], "src_in": c["start"], "src_out": c["end"],
                 "confidence": r["confidence"]})
    last_b_exit = c["timeline_end"]

print("== 2. delivery policy (existing rules: min B %.1fs, max B %.1fs, min A recovery %.1fs) =="
      % (MINB, MAXB, MINA))
print("  B decisions surviving production policy: %d/%d" % (len(cuts), sum(1 for r in rows if r["is_b"])))
for c in cuts:
    print("    SURVIVES  %s %-9s tl %.3f-%.3f  src %.3f-%.3f  %.3fs  conf %.2f"
          % (c["event"], c["candidate"], c["tl_in"], c["tl_out"], c["src_in"], c["src_out"],
             c["dur"], c["confidence"]))
for h in holds:
    print("    hold      %s (%s)" % (h["event"], h["reason"]))
print()

# ---- distribution -----------------------------------------------------------------
sel_counts = {}
for r in rows:
    sel_counts[r["selection"]] = sel_counts.get(r["selection"], 0) + 1
bd = [c["dur"] for c in cuts]
print("== 3. decision distribution ==")
for k in ("hold_a", "b_glance", "b_action", "b_hold", "b_early"):
    if sel_counts.get(k):
        print("  %-9s %d" % (k, sel_counts[k]))
print("  B decisions surviving policy: %d" % len(cuts))
if bd:
    print("  selected B duration  min %.3f  median %.3f  mean %.3f  max %.3f  (n=%d)"
          % (min(bd), st.median(bd), st.mean(bd), max(bd), len(bd)))
    print("  selected candidates at the %.1fs ceiling: %d" % (MAXB, sum(1 for x in bd if abs(x - MAXB) < 1e-6)))
else:
    print("  selected B duration: none survived -> no duration distribution")
conf_a = [r["confidence"] for r in rows if not r["is_b"]]
conf_b = [r["confidence"] for r in rows if r["is_b"]]
print("  confidence HOLD_A: n=%d min %.2f median %.2f max %.2f"
      % (len(conf_a), min(conf_a), st.median(conf_a), max(conf_a)))
print("  confidence B     : n=%d values %s" % (len(conf_b), conf_b))
print()

print("== 4. candidate lengths that were actually available ==")
for eid in order:
    ev = events[eid]
    d = "  ".join("%s=%.3f" % (c["id"], c["duration_s"]) for c in ev["candidates"])
    print("  %s  %s" % (eid, d))
print()

print("== 5. problems ==")
for p in problems:
    print("   ! %s" % p)
if not problems:
    print("   none")
print("== 6. soft notes ==")
for n in notes:
    print("   - %s" % n)
if not notes:
    print("   none")

json.dump({"response": f, "validation_problems": problems, "soft_notes": notes,
           "rows": [{k: r[k] for k in ("event", "selection", "confidence", "rationale", "is_b")}
                    for r in rows],
           "timecode_citations": ts_report, "duration_citations": dur_report,
           "deliverable_chain_b": cuts, "holds": holds,
           "selection_counts": sel_counts, "surviving_b": len(cuts),
           "selected_b_durations": bd, "policy": POLICY},
          open("/root/test1/comparison_test4.json", "w"), indent=1)
print("\nwrote /root/test1/comparison_test4.json")
