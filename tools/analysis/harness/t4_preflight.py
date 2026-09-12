#!/usr/bin/env python3
"""Test 4 pre-flight: freeze inputs, check the pack for leaked deterministic recommendation."""
import glob
import hashlib
import json
import os

PK = ("/opt/video-studio/projects/2026-08-25-tester/work/analysis/editorial_pkg_300_600/"
      "matched_ab_evidence")
CAND = "/opt/video-studio/tools/analysis/p1_out/sync_300_420.json"
PROMPT = "/opt/video-studio/tools/analysis/editorial_prompt_matched_ab.md"
REF = "/root/test1/payload/reference_pkg"


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


pack = json.load(open(PK + "/evidence_pack.json"))
print("== candidate record keys (event 1) ==")
for c in pack["events"][0]["candidates"]:
    print("  %-10s %s" % (c["id"], sorted(c.keys())))
print()
print("== leak check ==")
for key in ("recommended_by_score", "recommended", "rank", "total", "weighted_sum", "b_slot"):
    hits = [c["id"] for e in pack["events"] for c in e["candidates"] if key in c]
    print("  %-22s present in %d candidate records %s" % (key, len(hits), hits[:3] if hits else ""))
print()
print("== the eight evidence scores ==")
c0 = pack["events"][0]["candidates"][0]
sc = c0.get("scores") or c0.get("evidence") or {}
print("  container:", [k for k in c0 if isinstance(c0[k], dict)])
if isinstance(sc, dict):
    for k, v in sc.items():
        print("    %-22s %s" % (k, v))
print()
print("== policy echoed in the pack ==")
print(" ", json.dumps(pack["candidate_artifact"]["policy"]))
print("== sync mapping echoed ==")
print(" ", pack["sync_mapping"]["mapping"])
print()
print("== freeze SHAs ==")
for label, p in (("prompt (canonical)", PROMPT),
                 ("prompt (in package)", PK + "/prompt.md"),
                 ("evidence_pack.json", PK + "/evidence_pack.json"),
                 ("package_index.json", PK + "/package_index.json"),
                 ("sync_300_420.json", CAND),
                 ("reference_notes.json", REF + "/reference_notes.json")):
    print("  %-24s %s  %s" % (label, sha(p), os.path.getsize(p)))
n_ref = len(glob.glob(REF + "/reference/*.jpg")) + len(glob.glob(REF + "/_grid*.jpg"))
print("  %-24s %d files, %d bytes" % ("reference package", n_ref,
      sum(os.path.getsize(p) for p in glob.glob(REF + "/**/*.jpg", recursive=True))))
rn = json.load(open(REF + "/reference_notes.json"))
print("  reference source      ", rn["source_file"], rn["sha256"][:16], "| shots",
      len(rn["reference_shots"]), "| cadence", json.dumps(rn["cadence"])[:120])
print()
print("== event/candidate inventory ==")
for e in pack["events"]:
    print("  %s  %s" % (e["event_id"], [c["id"] for c in e["candidates"]]))
