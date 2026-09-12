#!/usr/bin/env python3
"""Test 4 review proof manifest — built from the model's VALIDATED decision chain.

The chain has exactly one B decision (evt_04 b_glance, 10.069 s) and it survives production
policy; the other ten events are hold_a, i.e. no cut. The proof therefore covers the section
from its start through the first A cut point after the insert, which is the whole of the
deliverable's editing activity, and ends in A.

Nothing is hard-coded that can be derived: the insert comes from the chain, the closing cut
point comes from the next event's real anchor, and both recoveries are asserted against the
package's own minimum_a_recovery.
"""
import json
import os
import subprocess
import sys

import yaml

V = "/opt/video-studio/tools/venv/bin/python"
ROOT = "/opt/video-studio/projects/2026-08-25-tester/work/analysis"
OUT = ROOT + "/test4_ab"
GRADE_DIR = ROOT + "/test1_grade"
MEDIA = "/mnt/media/raw"
PACK = ROOT + "/editorial_pkg_300_600/matched_ab_evidence/evidence_pack.json"
CHAIN = "/root/test4_chain.json"
A_CAM = "A CAM/DJI_20260824204625_0054_D.MP4"
B_REEL = ["B CAM/DJI_20260824204627_0038_D.MP4", "B CAM/DJI_20260824211420_0039_D.MP4"]

pack = json.load(open(PACK))
chain = json.load(open(CHAIN))
ev = {e["event_id"]: e for e in pack["events"]}
POLICY = pack["candidate_artifact"]["policy"]
MINA = POLICY["minimum_a_recovery"]
SPAN_START = float(pack["span"]["start"])
inserts = chain["deliverable_chain_b"]
holds = chain["holds"]

assert len(inserts) == 1, "expected exactly 1 surviving B insert, found %d" % len(inserts)
ins = inserts[0]
last_out = ins["tl_out"]

# the first real event anchor after the insert supplies the closing A cut point
closing = None
for e in pack["events"]:
    anchor = float(e["context"]["beat"])
    if anchor > last_out + 1e-6:
        closing, closing_ev = anchor, e["event_id"]
        break
assert closing is not None, "no event anchor after the insert to close on"

lead = ins["tl_in"] - SPAN_START
tail = closing - ins["tl_out"]
assert lead + 1e-6 >= MINA, "A lead-in %.3fs < minimum_a_recovery %.1fs" % (lead, MINA)
assert tail + 1e-6 >= MINA, "A tail %.3fs < minimum_a_recovery %.1fs" % (tail, MINA)

tl = [{"angle": "A", "source_in": round(SPAN_START, 3), "source_out": round(ins["tl_in"], 3)},
      {"angle": "B", "source_in": round(ins["src_in"], 3), "source_out": round(ins["src_out"], 3)},
      {"angle": "A", "source_in": round(ins["tl_out"], 3), "source_out": round(closing, 3)}]
for i, c in enumerate(tl, 1):
    c["id"] = "cut_%03d" % i
total = round(sum(c["source_out"] - c["source_in"] for c in tl), 3)
assert tl[-1]["angle"] == "A"

trim = json.load(open(GRADE_DIR + "/camera_normalization_final.json"))["solved_pre_grade"]
job = "Test4_MatchedAB_ReviewProof"
man = {"manifest_version": 1, "job_id": job,
       "template": "club_dispatch_standard_v1",
       "sources": {"a_reel": A_CAM, "b_reel": B_REEL},
       "timeline": tl,
       "profiles": {"grade": "club_dispatch_pb3_v1", "crop": "cd_a_pushin_v1",
                    "graphics": "cd_bug_v9", "audio": "cd_youtube_v1"},
       "pre_grades": {B_REEL[0]: trim, B_REEL[1]: trim}}
os.makedirs(OUT, exist_ok=True)
p = os.path.join(OUT, "%s.yaml" % job.lower())
yaml.safe_dump(man, open(p, "w"), sort_keys=False, default_flow_style=False)

print("Test 4 deliverable chain: %d B insert(s), %d holds" % (len(inserts), len(holds)))
print("manifest       : %s" % p)
print("total          : %.3fs   cuts %d   B inserts 1" % (total, len(tl)))
t = 0.0
for c in tl:
    d = c["source_out"] - c["source_in"]
    print("  %s %-3s proof %.3f-%.3f  %7.3fs  src %.3f-%.3f%s"
          % (c["id"], c["angle"], t, t + d, d, c["source_in"], c["source_out"],
             "   <-- B INSERT (%s, conf %.2f)" % (ins["candidate"], ins["confidence"])
             if c["angle"] == "B" else ""))
    t += d
print("A lead-in %.3fs  |  A tail %.3fs (min %.1fs)  |  closes on %s anchor %.3f"
      % (lead, tail, MINA, closing_ev, closing))
json.dump({"job": job, "manifest": p, "total_s": total,
           "span": [SPAN_START, closing], "inserts": inserts,
           "served_audio_offset_s": SPAN_START, "closing_event": closing_ev,
           "source_chain": CHAIN, "n_holds": len(holds)},
          open(os.path.join(OUT, "proof_basis.json"), "w"), indent=1)

r = subprocess.run([V, "/opt/video-studio/tools/manifest/validate_manifest.py",
                    "--media-root", MEDIA, "--json", p], capture_output=True, text=True)
try:
    d = json.loads(r.stdout)
except Exception:
    print("validator rc=%s %s%s" % (r.returncode, r.stdout[:400], r.stderr[:400]))
    sys.exit(1)
print("validator      : rc=%s valid=%s errors=%s" % (r.returncode, d.get("valid"), d.get("errors")))
for n in (d.get("notes") or [])[:6]:
    print("  note:", n)
sys.exit(0 if r.returncode == 0 else 1)
