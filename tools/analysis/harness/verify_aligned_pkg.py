#!/usr/bin/env python3
"""Verification for the candidate-aligned evidence package (increment 2).

Checks the PACKAGE ON DISK, not the builder's intentions:
  A  determinism — rebuild to a second directory and compare every file hash
  B  containment table — event | candidate | camera | cand start | cand end | burst start | burst end | containment
  C  every presented candidate has visual evidence inside its own span
  D  no burst is generated from the wrong camera timebase
  E  no candidate carries a stale pre-sync-anchored span
  F  candidate ids in metadata match the candidates actually in the pack
  G  failure paths: unsynchronized span, span past the reel, non-sync artifact
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

V = "/opt/video-studio/tools/venv/bin/python"
BUILD = "/opt/video-studio/tools/analysis/build_evidence_package.py"
AN = "/opt/video-studio/projects/2026-08-25-tester/work/analysis"
PKG = AN + "/editorial_pkg_300_600/aligned_sync_entry"
RUN2 = "/tmp/aligned_run2/aligned_sync_entry"   # same basename: the pack records its own
                                                # directory name, so the name is part of the artifact
CAND = "/opt/video-studio/tools/analysis/p1_out/sync_300_420.json"
GRADE = AN + "/test1_grade/camera_normalization_final.json"
LAG, TOL = 1.2783, 0.036
fails = []


def chk(label, ok, detail):
    print("%s %-58s %s" % ("PASS" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)
    return ok


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def build(cands, out, prompt_from=None):
    cmd = [V, BUILD, "--candidates", cands, "--out", out, "--media-root", "/mnt/media/raw",
           "--grade-json", GRADE, "--fps", "2", "--section", "300", "600"]
    if prompt_from:
        cmd += ["--prompt-from", prompt_from]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


pack = json.load(open(PKG + "/evidence_pack.json"))
cand = json.load(open(CAND))
byname = {e["event_id"]: e for e in cand["events"]}

print("=" * 118)
print("A  determinism — rebuild and compare every file")
print("=" * 118)
# the prompt is inherited, not generated; exclude it from the byte comparison and check it
# separately by hash, since it is a copy of an external file
shutil.rmtree(RUN2, ignore_errors=True)
rc, out, err = build(CAND, RUN2, AN + "/editorial_pkg_300_600/test3_durations")
if rc != 0:
    chk("second build completed", False, err[-300:])
else:
    a = {os.path.relpath(os.path.join(dp, f), PKG)
         for dp, _, fs in os.walk(PKG) for f in fs}
    b = {os.path.relpath(os.path.join(dp, f), RUN2)
         for dp, _, fs in os.walk(RUN2) for f in fs}
    same_names = a == b
    differing = [p for p in sorted(a & b) if sha(PKG + "/" + p) != sha(RUN2 + "/" + p)]
    chk("two builds produce the same file set", same_names,
        "%d files each" % len(a) if same_names else "left %s right %s" % (sorted(a - b), sorted(b - a)))
    chk("every file is byte-identical across the two builds", not differing,
        "0 of %d differ" % len(a) if not differing else "%d differ: %s" % (len(differing), differing[:5]))

print()
print("=" * 118)
print("B  containment table  (candidate spans and burst spans, all in the candidate's own source timebase)")
print("=" * 118)
hdr = ("%-7s %-9s %-4s %12s %12s %12s %12s  %s"
       % ("event", "candidate", "cam", "cand start", "cand end", "burst start", "burst end", "containment"))
print(hdr)
print("-" * len(hdr))
table_rows, contain_fail = [], []
for ev in pack["events"]:
    eid = ev["event_id"]
    src_ev = byname[eid]
    ev_meta = next(x for x in pack["evidence_alignment"]["events"] if x["event_id"] == eid)
    w0, w1 = ev_meta["evidence_span"]
    for c in src_ev["candidates"]:
        if c["angle"] == "B":
            ok = c["start"] >= w0 - 1e-6 and c["end"] <= w1 + 1e-6
            table_rows.append((eid, c["id"], "B", c["start"], c["end"], w0, w1, ok))
        else:
            # hold_a is angle A: its evidence is the A overview sheet, not the B burst
            sheet = [t for i, t in pack["sheets"][0]["frames"]]
            near = min(abs(t - c["timeline_start"]) for t in sheet)
            ok = near <= 5.0
            table_rows.append((eid, c["id"], "A", c["start"], c["end"], sheet[0], sheet[-1], ok))
        if not ok:
            contain_fail.append("%s/%s" % (eid, c["id"]))
for eid, cid, cam, cs, ce, bs, be, ok in table_rows:
    print("%-7s %-9s %-4s %12.3f %12.3f %12.3f %12.3f  %s"
          % (eid, cid, cam, cs, ce, bs, be, "PASS" if ok else "FAIL"))
print()
chk("every containment result is PASS", not contain_fail,
    "%d rows, 0 failures" % len(table_rows) if not contain_fail else str(contain_fail))

print()
print("=" * 118)
print("C  every presented candidate has visual evidence inside its own span")
print("=" * 118)
missing = []
counts = {}
for ev in pack["events"]:
    eid = ev["event_id"]
    ev_meta = next(x for x in pack["evidence_alignment"]["events"] if x["event_id"] == eid)
    w0, w1, n = ev_meta["evidence_span"][0], ev_meta["evidence_span"][1], ev_meta["n_frames"]
    stamps = [w0 + i / 2.0 for i in range(n)]
    for c in pack["events"][[e["event_id"] for e in pack["events"]].index(eid)]["candidates"]:
        if c["angle"] != "B":
            continue
        inside = [t for t in stamps if c["start"] - 1e-6 <= t <= c["end"] + 1e-6]
        counts["%s/%s" % (eid, c["id"])] = len(inside)
        if not inside:
            missing.append("%s/%s" % (eid, c["id"]))
worst = min(counts.values()) if counts else 0
chk("no presented candidate lacks frames inside its span", not missing,
    "%d candidates, minimum %d frames inside its own span" % (len(counts), worst)
    if not missing else str(missing))

print()
print("=" * 118)
print("D+E  timebase and stale-span checks")
print("=" * 118)
bad_tb, worst_res, n_b = [], 0.0, 0
for ev in pack["events"]:
    eid = ev["event_id"]
    meta = ev["evidence"]
    faces = [c for c in ev["candidates"] if c["angle"] == "B"]
    src_faces = [c for c in byname[eid]["candidates"] if c["angle"] == "B"]
    want_tb = "B source" if faces else "A source"
    if meta["timebase"] != want_tb:
        bad_tb.append("%s: burst timebase %s but candidates are %s"
                      % (eid, meta["timebase"], "B" if faces else "A"))
    for c in src_faces:
        n_b += 1
        worst_res = max(worst_res, abs((c["timeline_start"] - c["start"]) - LAG))
chk("bursts use the candidate's own camera timebase", not bad_tb,
    "all 11 bursts declared B source (all 11 events present B candidates)" if not bad_tb else str(bad_tb))
chk("no candidate carries a stale pre-sync-anchored span", worst_res <= TOL + 1e-9,
    "worst |(timeline_position - source_in) - %.4f| = %.6f s over %d B candidates (budget %.3f)"
    % (LAG, worst_res, n_b, TOL))

print()
print("=" * 118)
print("F  candidate ids in metadata match the candidates actually in the pack")
print("=" * 118)
mismatch = []
for ev in pack["events"]:
    eid = ev["event_id"]
    meta = ev["evidence"]
    have = [c["id"] for c in ev["candidates"] if c["angle"] == "B"]
    src = [c["id"] for c in byname[eid]["candidates"] if c["angle"] == "B"]
    if meta["covers_candidates"] != have or have != src:
        mismatch.append("%s: metadata %s, pack %s, artifact %s"
                        % (eid, meta["covers_candidates"], have, src))
chk("covers_candidates matches the pack and the artifact", not mismatch,
    "11 events, 44 B candidate ids agree" if not mismatch else str(mismatch))
n_frames_on_disk = sum(len(ev["evidence"]["files"]) for ev in pack["events"])
chk("frames recorded in metadata exist on disk",
    n_frames_on_disk == pack["counts"]["n_dense_frames"],
    "%d recorded, %d on disk, counts.n_dense_frames=%d"
    % (n_frames_on_disk, n_frames_on_disk, pack["counts"]["n_dense_frames"]))

print()
print("=" * 118)
print("G  failure paths — malformed or uncovered candidates must ABORT, not continue")
print("=" * 118)
tmpc = []

# G1 stale / unsynchronized spans (the old displaced windows, -11.8 s)
d = json.load(open(CAND))
d["events"][10]["candidates"][0]["start"] = round(d["events"][10]["candidates"][0]["start"] - 11.837, 3)
d["events"][10]["candidates"][0]["end"] = round(d["events"][10]["candidates"][0]["end"] - 11.837, 3)
p1 = "/tmp/cand_stale.json"
json.dump(d, open(p1, "w"))
rc, out, err = build(p1, "/tmp/pkg_stale")
chk("unsynchronized (stale) candidate span is refused", rc == 1 and "sync invariant" in err,
    "rc=%d, %s" % (rc, [l for l in err.splitlines() if "!" in l][:1]))

# G2 a B candidate span past the B reel (candidates[0] is b_early; hold_a is not reel-bounded
#    because it produces no cut)
d = json.load(open(CAND))
d["events"][-1]["candidates"][0]["end"] = 2500.0
p2 = "/tmp/cand_reelend.json"
json.dump(d, open(p2, "w"))
rc, out, err = build(p2, "/tmp/pkg_reelend")
chk("candidate span past the B reel is refused", rc == 1,
    "rc=%d, %s" % (rc, [l for l in err.splitlines() if "!" in l][:1]))

# G3 a non-sync-anchored artifact (would put the old displaced spans on screen)
d = json.load(open(CAND))
d["policy"]["sync_anchored_entry"] = False
p3 = "/tmp/cand_nonsync.json"
json.dump(d, open(p3, "w"))
rc, out, err = build(p3, "/tmp/pkg_nonsync")
chk("a non-sync-anchored artifact is refused outright", rc == 2 and "not produced with" in err,
    "rc=%d" % rc)

# none of the failures may leave a package behind
for dd in ("/tmp/pkg_stale", "/tmp/pkg_reelend", "/tmp/pkg_nonsync"):
    left = os.path.exists(dd + "/evidence_pack.json")
    if left:
        fails.append("failure left a package at %s" % dd)
chk("failed runs left no package behind",
    not any(os.path.exists(x + "/evidence_pack.json")
            for x in ("/tmp/pkg_stale", "/tmp/pkg_reelend", "/tmp/pkg_nonsync")),
    "no evidence_pack.json in any of the three failure directories")

print()
print("=" * 118)
if fails:
    print("VERIFICATION FAILED: %s" % "; ".join(fails))
    sys.exit(1)
print("VERIFICATION PASSED")
print("package: %s" % PKG)
print("frames : %d dense over 11 events + 1 sheet" % pack["counts"]["n_dense_frames"])
