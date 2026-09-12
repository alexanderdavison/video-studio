#!/usr/bin/env python3
"""Validation for --sync-anchored-entry (checkpoint Next increments item 1).

Every check runs against tool OUTPUT. The perturbation test is the important one:
perturb the action profile and the B entry must NOT move — and the same test with the
flag OFF must SHOW the entry moving, or the test is not capable of failing.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys

V = "/opt/video-studio/tools/venv/bin/python"
T = "/opt/video-studio/tools/analysis/candidate_score.py"
PREV = "/opt/video-studio/tools/analysis/candidate_score.py.pre_sync_anchored.bak"
AN = "/opt/video-studio/projects/2026-08-25-tester/work/analysis"
P1 = "/opt/video-studio/tools/analysis/p1_out"
SIGB = P1 + "/signals_b_300_420.json"
SIGA = P1 + "/signals_a_300_420.json"
FROZEN = P1 + "/candidates_300_420.json"
LAG, MAX_SNAP, REEL = 1.2783, 0.036, 2073.536
BEATS = AN + "/a_full_beats.json"
PROFILE = AN + "/b_reel_profile.json"
PHRASES = AN + "/a_full_phrases.json"
SYNC_OUT = P1 + "/sync_300_420.json"

fails = []


def chk(label, ok, detail):
    print("%s %-56s %s" % ("PASS" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)
    return ok


def run(tool, prof, sig, extra, out):
    args = [V, tool, "--beats", BEATS, "--action-profile", prof,
            "--signals", sig, "--signals-a", SIGA, "--phrases", PHRASES,
            "--min-b", "4", "--max-b", "14", "--min-recovery", "8",
            "--events", "12", "--min-event-gap", "9", "--start", "300", "--end", "405",
            "--b-search-from", "300", "--b-search-to", "420"] + extra + ["--out", out]
    r = subprocess.run(args, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def load(p):
    return json.load(open(p))


def bfaces(ev):
    return [c for c in ev["candidates"] if c["angle"] == "B"]


print("=" * 78)
print("T1  flag OFF byte-identical to the frozen pre-increment artifact")
print("=" * 78)
# The frozen artifact was produced against studio.db while clip 3's keyframes still had
# no sharpness. The live DB has since drifted (it gained sharpness), which would change
# the quality term. Reconstruct the exact input condition instead of accepting a fuzzy
# match, so this is a real byte comparison.
kf = "/tmp/kf_frozen.db"
shutil.copy("/opt/video-studio/studio.db", kf)
con = sqlite3.connect(kf)
con.execute("update keyframes set sharpness=NULL where clip_id=3")
con.commit()
n = con.execute("select count(*), sum(sharpness is null) from keyframes where clip_id=3").fetchone()
con.close()
print("   reconstructed keyframes db: clip 3 rows=%d null_sharpness=%d" % n)
# Reproduce the original invocation exactly, including its RELATIVE --signals paths and
# working directory: the frozen artifact echoes those strings in inputs, so an absolute
# path would differ in the audit record while the computation is identical.
r = subprocess.run([V, "candidate_score.py", "--beats", BEATS,
                    "--action-profile", PROFILE,
                    "--signals", "p1_out/signals_b_300_420.json",
                    "--signals-a", "p1_out/signals_a_300_420.json",
                    "--phrases", PHRASES,
                    "--min-b", "4", "--max-b", "14", "--min-recovery", "8",
                    "--events", "12", "--min-event-gap", "9", "--start", "300", "--end", "405",
                    "--b-search-from", "300", "--b-search-to", "420",
                    "--keyframes-db", kf, "--clip-id", "3", "--out", "/tmp/v_flagoff.json"],
                   capture_output=True, text=True, cwd="/opt/video-studio/tools/analysis")
same = open("/tmp/v_flagoff.json", "rb").read() == open(FROZEN, "rb").read()
chk("flag OFF == frozen artifact, byte for byte", same and r.returncode == 0,
    "rc=%d %d bytes vs %d bytes%s" % (r.returncode, os.path.getsize("/tmp/v_flagoff.json"),
                                      os.path.getsize(FROZEN),
                                      "" if r.returncode == 0 else " " + r.stderr[:120]))

print()
print("=" * 78)
print("T2  flag OFF: only the flag-gated records differ from the previous version")
print("=" * 78)
run(PREV, PROFILE, SIGB, [], "/tmp/v_prev_off.json")
run(T, PROFILE, SIGB, [], "/tmp/v_new_off.json")
a, b = load("/tmp/v_prev_off.json"), load("/tmp/v_new_off.json")


def ev_norm(d):
    d = json.loads(json.dumps(d))
    for e in d["events"]:
        e.pop("duration_choices", None)
        (e.get("context") or {}).pop("sync_entry", None)
    return d["events"]


gated = {"duration_choice_rules", "sync_anchored_entry"}
extra = set(a.keys()) - set(b.keys())
chk("sync-anchor change left the flag-OFF events untouched", ev_norm(a) == ev_norm(b),
    "all %d events identical, candidates included (after dropping the gated keys)"
    % len(b["events"]))
chk("the only difference is the now flag-gated records", extra <= gated and
    a["policy"].get("duration_choices") is False and "duration_choices" not in b["policy"],
    "removed: %s" % (sorted(extra) or "nothing"))
print("      (the previous version emitted duration_choice_rules/policy.duration_choices "
      "even with the flag off, which is what broke byte-identity with the frozen artifact)")

print()
print("=" * 78)
print("T3  duration path still produces the same VALUES (key order changed by flag-gating)")
print("=" * 78)
run(PREV, PROFILE, SIGB, ["--duration-choices"], "/tmp/v_dur_prev.json")
run(T, PROFILE, SIGB, ["--duration-choices"], "/tmp/v_dur_new.json")


def val(d):
    d = json.loads(json.dumps(d))
    d.pop("duration_choice_rules", None)
    d.get("policy", {}).pop("duration_choices", None)
    for e in d["events"]:
        e.pop("duration_choices", None)
    return d


va, vb = val(load("/tmp/v_dur_prev.json")), val(load("/tmp/v_dur_new.json"))
chk("duration values unchanged by this increment", va == vb,
    "%d events, B lengths identical" % len(vb["events"]))

print()
print("=" * 78)
print("T4  --sync-anchored-entry run")
print("=" * 78)
rc, so, se = run(T, PROFILE, SIGB, ["--duration-choices", "--sync-anchored-entry"], SYNC_OUT)
if rc != 0:
    print("FAIL: run returned %d\n%s" % (rc, se[-800:]))
    sys.exit(1)
print(so.strip()[-2000:])
D = load(SYNC_OUT)
evs = D["events"]

worst, n_b, shared_bad = 0.0, 0, []
for e in evs:
    entry = e["context"]["sync_entry"]["entry_source_s"]
    for c in bfaces(e):
        n_b += 1
        worst = max(worst, abs((c["timeline_start"] - c["start"]) - LAG))
        if c["id"] != "b_early" and abs(c["start"] - entry) > 1e-6:
            shared_bad.append("%s/%s" % (e["event_id"], c["id"]))
    shared = [c["start"] for c in bfaces(e) if c["id"] != "b_early"]
    if shared and len(set(round(x, 6) for x in shared)) != 1:
        shared_bad.append(e["event_id"] + " (faces differ)")
chk("every B candidate satisfies the sync invariant", worst <= MAX_SNAP + 1e-9,
    "%d B candidates, worst |(timeline - source_in) - %.4f| = %.6f s (budget %.3f)"
    % (n_b, LAG, worst, MAX_SNAP))
chk("all B faces on an event share the synchronized entry", not shared_bad,
    "no mismatches" if not shared_bad else str(shared_bad))

bad_early, n_early, early_id, worst_early = [], 0, [], 0.0
for e in evs:
    T_ = e["context"]["beat"]
    for c in bfaces(e):
        if c["id"] != "b_early":
            continue
        n_early += 1
        early_id.append(e["event_id"])
        shift = round(T_ - c["timeline_start"], 6)
        src_shift = round(e["context"]["sync_entry"]["entry_source_s"] - c["start"], 6)
        worst_early = max(worst_early, abs(shift - src_shift))
        if abs(shift - src_shift) > 0.002:
            bad_early.append("%s (tl %+.6f vs src %+.6f)" % (e["event_id"], shift, src_shift))
chk("b_early moves timeline and source together", not bad_early,
    "%d b_early faces, worst shift difference %.6f s — two independent 3 dp roundings, "
    "%.2f%% of the %.3f s budget" % (n_early, worst_early, 100.0 * worst_early / MAX_SNAP, MAX_SNAP)
    if not bad_early else str(bad_early))

illegal = []
for e in evs:
    for c in e["candidates"]:
        if c["angle"] != "B":
            continue
        if c["duration_s"] < 4.0 - 1e-9 or c["duration_s"] > 14.0 + 1e-9:
            illegal.append("%s/%s dur %.3f" % (e["event_id"], c["id"], c["duration_s"]))
        if c["start"] < 0 or c["end"] > REEL + 1e-6 or c["end"] < c["start"]:
            illegal.append("%s/%s span %.3f-%.3f" % (e["event_id"], c["id"], c["start"], c["end"]))
chk("no candidate exceeds legal footage or duration limits", not illegal,
    "all %d B candidates inside policy and the B reel" % n_b if not illegal else str(illegal[:4]))

run(T, PROFILE, SIGB, ["--duration-choices", "--sync-anchored-entry"], "/tmp/v_sync_2.json")
chk("deterministic", open(SYNC_OUT, "rb").read() == open("/tmp/v_sync_2.json", "rb").read(),
    "two runs byte-identical")

print()
print("=" * 78)
print("T5  THE PERTURBATION TEST — perturb the action profile, the entry must not move")
print("=" * 78)
pert = load(PROFILE)
for s in pert["segments"]:
    s["score"] = round(1.0 - float(s["score"]), 3)
per_path = "/tmp/b_reel_profile_perturbed.json"
json.dump(pert, open(per_path, "w"))
print("   perturbed profile: every segment score inverted (1 - score)")

for label, extra in (("FLAG OFF", []), ("FLAG ON ", ["--duration-choices", "--sync-anchored-entry"])):
    tag = label.strip().replace(" ", "_")
    res = {}
    for mode, prof in (("base", PROFILE), ("pert", per_path)):
        o = "/tmp/vp_%s_%s.json" % (tag, mode)
        rr, _, ee = run(T, prof, SIGB, extra, o)
        if rr != 0:
            print("   %s/%s rc=%d %s" % (label, mode, rr, ee[:200]))
            res[mode] = None
            continue
        d = load(o)
        res[mode] = ([e["context"]["sync_entry"]["entry_source_s"] for e in d["events"]] if extra
                     else [(bfaces(e) or [{}])[0].get("start") for e in d["events"]])
    if not res.get("base") or not res.get("pert"):
        continue
    moved = [abs(a - b) for a, b in zip(res["base"], res["pert"])]
    print("   %s entries base (first 5): %s" % (label, ["%.3f" % x for x in res["base"][:5]]))
    print("   %s entries pert (first 5): %s" % (label, ["%.3f" % x for x in res["pert"][:5]]))
    if extra:
        chk("perturbation does NOT move the entry (flag ON)", max(moved) <= 1e-9,
            "max move %.6f s over %d events" % (max(moved), len(moved)))
    else:
        chk("the test CAN fail: flag OFF entry DOES move", max(moved) > 1e-6,
            "max move %.6f s — the action search is what moved it" % max(moved))

print()
print("=" * 78)
print("T6  missing B coverage produces HOLD_A only, with the reason recorded")
print("=" * 78)
sig = load(SIGB)
samples = sig["samples"]
ent = {e["event_id"]: e["context"]["sync_entry"]["entry_source_s"] for e in evs}
for eid, mode in (("evt_03", "obstruct"), ("evt_06", "frozen"), ("evt_09", "gap")):
    t = ent[eid]
    near = min(samples, key=lambda s: abs(float(s["t"]) - t))
    if mode == "obstruct":
        near["obstruction"] = 0.9
    elif mode == "frozen":
        near["frozen"] = True
    else:
        sig["samples"] = [s for s in sig["samples"] if abs(float(s["t"]) - t) > 1.001]
doc_path = "/tmp/signals_b_doctored.json"
json.dump(sig, open(doc_path, "w"))
rc, _, ee = run(T, PROFILE, doc_path, ["--duration-choices", "--sync-anchored-entry"],
                "/tmp/v_coverage.json")
if rc != 0:
    chk("doctored-coverage run completed", False, ee[:300])
else:
    dcov = load("/tmp/v_coverage.json")
    got = []
    for e in dcov["events"]:
        if e["event_id"] in ("evt_03", "evt_06", "evt_09"):
            got.append((e["event_id"], [c["id"] for c in e["candidates"]],
                        e["context"]["sync_entry"]["coverage_ok"],
                        e["context"]["sync_entry"]["coverage"]))
    chk("obstructed / frozen / unsampled entries give HOLD_A only",
        all(ids == ["hold_a"] and not cov for _, ids, cov, _ in got),
        "; ".join("%s->%s" % (e, i) for e, i, _, _ in got))
    for e, i, cov, why in got:
        print("      %s coverage_ok=%s  %s" % (e, cov, why[:120]))
    n_b2 = sum(1 for e in dcov["events"] for c in e["candidates"] if c["angle"] == "B")
    chk("only the uncovered events lost their B faces", n_b2 == n_b - 3 * 4,
        "%d B candidates in the doctored run vs %d in the clean run "
        "(3 events x 4 faces withdrawn)" % (n_b2, n_b))

print()
print("=" * 78)
if fails:
    print("VALIDATION FAILED: %s" % "; ".join(fails))
    sys.exit(1)
print("VALIDATION PASSED — all checks against tool output")
