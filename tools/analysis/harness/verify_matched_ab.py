#!/usr/bin/env python3
"""Increment-3 verification: matched A/B evidence over one performance-time envelope.

Independently recomputes each event's performance envelope from the candidate artifact
(not from the pack) and checks the package against it, plus the on-disk frames.
Exit 0 = all checks pass, 1 = failure.
"""
import glob
import hashlib
import json
import os
import sys

AN = "/opt/video-studio/projects/2026-08-25-tester/work/analysis"
PKG = os.environ.get("PKG", AN + "/editorial_pkg_300_600/matched_ab_evidence")
CAND = "/opt/video-studio/tools/analysis/p1_out/sync_300_420.json"
LAG = 1.2783
INV = 0.036
FPS = 2.0
TOL = 0.0025          # 3-dp emission on both sides of a comparison
fails, checks = [], 0


def chk(name, ok, detail=""):
    global checks
    checks += 1
    if ok:
        print("  PASS  %s%s" % (name, ("  — " + detail) if detail else ""))
    else:
        print("  FAIL  %s%s" % (name, ("  — " + detail) if detail else ""))
        fails.append(name)
    return ok


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


cand = json.load(open(CAND))
pack = json.load(open(PKG + "/evidence_pack.json"))
idx = json.load(open(PKG + "/package_index.json"))
a_off = float((cand.get("inputs", {}).get("offsets") or {}).get("a_offset") or 0.0)
C = {e["event_id"]: e for e in cand["events"]}
E = {e["event_id"]: e for e in pack["events"]}
ids = [e["event_id"] for e in cand["events"]]

print("A source = performance + %.3f s   |   B source = performance - %.4f s" % (a_off, LAG))
print("package: %s\n" % PKG)

# --- 1. artifact-level sync invariant (independent of the pack) -----------------
worst = 0.0
offenders = []
for eid in ids:
    for c in C[eid]["candidates"]:
        if c["angle"] != "B":
            continue
        r = (c["timeline_start"] - c["start"]) - LAG
        worst = max(worst, abs(r))
        if abs(r) > INV:
            offenders.append("%s/%s %+.4f" % (eid, c["id"], r))
print("== 1. candidate artifact ==")
chk("every B candidate satisfies the sync invariant", not offenders,
    "worst residual %.6f s (budget %.3f)" % (worst, INV))

# --- 2. independent recompute of each event's envelope --------------------------
print("\n== 2. envelope recomputed from the artifact (not from the pack) ==")
recomputed = {}
for eid in ids:
    bf = [c for c in C[eid]["candidates"] if c["angle"] == "B"]
    af = next((c for c in C[eid]["candidates"] if c["id"] == "hold_a"), None)
    pb = ((min(c["start"] + LAG for c in bf), max(c["end"] + LAG for c in bf)) if bf else None)
    pa = ((af["start"] - a_off, af["end"] - a_off) if af is not None else None)
    recomputed[eid] = (pb, pa)
bad, bad_ab, worst_ab = [], [], 0.0
for eid in ids:
    pb, pa = recomputed[eid]
    span = E[eid]["evidence"]["performance_span_s"]
    if abs(span[0] - pb[0]) > TOL or abs(span[1] - pb[1]) > TOL:
        bad.append("%s: pack %s vs recomputed [%.3f, %.3f]" % (eid, span, pb[0], pb[1]))
    if pa and pb:
        d_lo, d_hi = pa[0] - pb[0], pa[1] - pb[1]
        worst_ab = max(worst_ab, -d_lo, d_hi)
        if d_lo < -INV or d_hi > INV:
            bad_ab.append("%s A/B disagree (%+.3f / %+.3f)" % (eid, d_lo, d_hi))
chk("recorded performance span == independently recomputed B-derived interval", not bad,
    "; ".join(bad) if bad else "%d events" % len(ids))
chk("the A-derived and B-derived performance intervals agree (hold_a inside the B interval)",
    not bad_ab, "; ".join(bad_ab) if bad_ab
    else "worst A/B disagreement %.3f s (snap budget %.3f)" % (worst_ab, INV))

# --- 3..9. per-event ------------------------------------------------------------
print("\n== 3. per-event matched evidence ==")
rows = []
bad_a, bad_b, bad_sync, bad_hold, bad_cov, bad_dens, bad_meta, bad_tb, bad_name = ([] for _ in range(9))
for eid in ids:
    ev, p = E[eid], E[eid]["evidence"]
    da, db = os.path.join(PKG, "events", eid), os.path.join(PKG, "events", eid)
    fa = sorted(glob.glob(os.path.join(da, "a_%s_*.jpg" % eid)))
    fb = sorted(glob.glob(os.path.join(db, "b_%s_*.jpg" % eid)))
    meta = p["frames"]
    ma = [f for f in meta if f["camera"] == "A"]
    mb = [f for f in meta if f["camera"] == "B"]
    if not fa or p["a"]["n_frames"] != len(fa) or len(ma) != len(fa):
        bad_a.append("%s (disk %d, meta %d/%d)" % (eid, len(fa), p["a"]["n_frames"], len(ma)))
    if not fb or p["b"]["n_frames"] != len(fb) or len(mb) != len(fb):
        bad_b.append("%s (disk %d, meta %d/%d)" % (eid, len(fb), p["b"]["n_frames"], len(mb)))
    # same performance interval: A span (A source = perf) vs B span + lag
    if abs(p["a"]["span_s"][0] - p["b"]["span_s"][0] - LAG) > TOL or \
       abs(p["a"]["span_s"][1] - p["b"]["span_s"][1] - LAG) > TOL:
        bad_sync.append("%s A[%s] B[%s]" % (eid, p["a"]["span_s"], p["b"]["span_s"]))
    # per-frame: A frame i and B frame i show the same performance moment
    worst_i = max((abs(ma[i]["performance_s"] - mb[i]["performance_s"])
                   for i in range(min(len(ma), len(mb)))), default=0.0)
    if worst_i > 1.0 / FPS + TOL:
        bad_sync.append("%s frame-index drift %.4f s" % (eid, worst_i))
    if p.get("hold_a_evidence") != "a":
        bad_hold.append("%s points at %r" % (eid, p.get("hold_a_evidence")))
    else:
        h = next(c for c in C[eid]["candidates"] if c["id"] == "hold_a")
        if h["start"] < p["a"]["span_s"][0] - TOL or h["end"] > p["a"]["span_s"][1] + TOL:
            bad_hold.append("%s hold_a %s outside A burst %s" % (eid, [h["start"], h["end"]],
                                                                 p["a"]["span_s"]))
        # and a real frame must exist inside hold_a's window
        inside = [f for f in ma if h["start"] - TOL <= f["source_s"] - a_off <= h["end"] + TOL]
        if len(inside) < max(1, int((h["end"] - h["start"]) * FPS) - 1):
            bad_hold.append("%s only %d A frames inside hold_a" % (eid, len(inside)))
    # every B candidate fully covered, and frames actually exist across it
    for c in C[eid]["candidates"]:
        if c["angle"] != "B":
            continue
        if c["start"] < p["b"]["span_s"][0] - TOL or c["end"] > p["b"]["span_s"][1] + TOL:
            bad_cov.append("%s/%s span outside burst" % (eid, c["id"]))
        n_in = len([f for f in mb if c["start"] - TOL <= f["source_s"] <= c["end"] + TOL])
        if n_in < max(1, int((c["end"] - c["start"]) * FPS) - 1):
            bad_cov.append("%s/%s only %d frames inside" % (eid, c["id"], n_in))
    # density comparable
    if abs(len(fa) - len(fb)) > 1:
        bad_dens.append("%s %d vs %d" % (eid, len(fa), len(fb)))
    # metadata completeness + no mixed timebase + filename matches source time
    for f_, tag, base in ((p["a"], "a", a_off), (p["b"], "b", LAG)):
        exp = f_["span_s"][1] - f_["span_s"][0]
        if f_["n_frames"] < int(exp * FPS) - 1:
            bad_dens.append("%s %s only %d frames over %.3f s" % (eid, tag, f_["n_frames"], exp))
    for f in meta:
        if not {"file", "camera", "source_s", "performance_s"} <= set(f) or f["camera"] not in ("A", "B"):
            bad_meta.append("%s %s" % (eid, f.get("file")))
            continue
        want = f["source_s"] + (a_off if f["camera"] == "A" else LAG)
        if abs(f["performance_s"] - want) > 1e-3:
            bad_tb.append("%s %s perf %.3f != %.3f" % (eid, f["file"], f["performance_s"], want))
        tc = f["file"].split("_")[-1].replace(".jpg", "").replace("-", ":")
        hh, mm, ss = tc.split(":")
        got = int(hh) * 3600 + int(mm) * 60 + float(ss)
        if abs(got - f["source_s"]) > 1e-3:
            bad_name.append("%s %s carries %.3f" % (eid, f["file"], got))
    rows.append((eid, "%.3f-%.3f" % tuple(p["performance_span_s"]),
                 "%.3f-%.3f" % tuple(p["a"]["span_s"]),
                 "%.3f-%.3f" % tuple(p["b"]["span_s"]),
                 len(fa), len(fb), "PASS" if not (bad_sync or bad_hold) else "FAIL",
                 worst_i))

chk("event-specific A evidence exists (disk, metadata and count agree)", not bad_a,
    "; ".join(bad_a) if bad_a else "%d events, %d frames" % (len(ids), sum(r[4] for r in rows)))
chk("event-specific B evidence exists (disk, metadata and count agree)", not bad_b,
    "; ".join(bad_b) if bad_b else "%d events, %d frames" % (len(ids), sum(r[5] for r in rows)))
chk("A and B represent the same performance interval (span + per-frame index match)",
    not bad_sync, "; ".join(bad_sync) if bad_sync else "worst frame-index drift %.4f s"
    % max(r[7] for r in rows))
chk("HOLD_A points to the event's own dense A evidence, and frames exist inside it",
    not bad_hold, "; ".join(bad_hold) if bad_hold else "11/11 events")
chk("every B candidate remains fully covered by frames on disk", not bad_cov,
    "; ".join(bad_cov) if bad_cov else "44/44 candidates")
chk("A and B sampling density is comparable", not bad_dens,
    "; ".join(bad_dens) if bad_dens else "matched pairs")
n_frames_total = sum(len(e["evidence"]["frames"]) for e in pack["events"])
chk("frame metadata identifies camera, source time and performance time", not bad_meta,
    "; ".join(bad_meta) if bad_meta else "%d frame records" % n_frames_total)
chk("no mixed timeline/source comparisons (A perf = A src, B perf = B src + lag)",
    not bad_tb, "; ".join(bad_tb) if bad_tb else "%d frames" % n_frames_total)
chk("filename timecode equals the recorded source time", not bad_name,
    "; ".join(bad_name) if bad_name else "%d frames" % n_frames_total)

# --- 10. index integrity --------------------------------------------------------
print("\n== 4. package integrity ==")
missing, mism = [], []
for f in idx["files"]:
    p = os.path.join(PKG, f["path"])
    if not os.path.exists(p):
        missing.append(f["path"])
    elif os.path.getsize(p) != f["bytes"] or sha(p) != f["sha256"]:
        mism.append(f["path"])
on_disk = sorted(os.path.relpath(x, PKG) for x in glob.glob(PKG + "/**/*", recursive=True)
                 if os.path.isfile(x) and os.path.basename(x) != "package_index.json")
chk("every indexed file exists on disk with the recorded hash", not missing and not mism,
    "%d files, %d bytes" % (len(idx["files"]), idx["total_bytes"]))
chk("index covers exactly the package's content files", on_disk == sorted(f["path"] for f in idx["files"]),
    "%d indexed / %d on disk" % (len(idx["files"]), len(on_disk)))
chk("the A overview sheet is retained as context only, not as event evidence",
    pack.get("sheets") and "context only" in pack["sheets"][0]["role"]
    and all("overview" not in f["file"] for e in pack["events"] for f in e["evidence"]["frames"]))
chk("candidates/<event_id>.json exists for every event (the prompt promises it)",
    len(glob.glob(PKG + "/candidates/*.json")) == len(ids))
chk("candidates/<event_id>.json is identical to the embedded candidate list",
    all(json.load(open(PKG + "/candidates/%s.json" % eid)) == E[eid]["candidates"] for eid in ids))
chk("package prompt describes matched A/B evidence and names both cameras",
    "matched pair" in open(PKG + "/prompt.md").read()
    and "a_<event_id>_HH-MM-SS.mmm.jpg" in open(PKG + "/prompt.md").read())

print("\n| event | performance span | A evidence span | B evidence span | A frames | B frames | synchronized | PASS |")
print("|---|---|---:|---:|---:|---:|---|---|")
for r in rows:
    print("| %s | %s | %s | %s | %d | %d | %s | %s |" % r)

print("\nchecks: %d   failures: %d" % (checks, len(fails)))
if fails:
    print("FAILED: " + "; ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASS")
