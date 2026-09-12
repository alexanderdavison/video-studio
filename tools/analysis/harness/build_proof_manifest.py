#!/usr/bin/env python3
"""Build + validate the clean 4-insert duration proof manifest.

Everything here is DERIVED, nothing hard-coded:
  * the section start comes from the packaged span
  * the proof's end comes from the anchor of the NEXT event after the last insert
    (a real cut point in the same event grid), so the timeline ends in A and the
    final second of the output is A
  * the B inserts are the already-derived B_ACTION candidates, unmodified
  * no renderer option cuts the output: the manifest IS the proof

Exactly four complete B inserts, no fifth, nothing truncated.
"""
import json
import os
import subprocess
import sys

import yaml

V = "/opt/video-studio/tools/venv/bin/python"
ROOT = "/opt/video-studio/projects/2026-08-25-tester/work/analysis"
OUT = ROOT + "/test3_ab"
GRADE_DIR = ROOT + "/test1_grade"
PACK = ROOT + "/editorial_pkg_300_600/test3_durations/evidence_pack.json"
MEDIA = "/mnt/media/raw"
A_CAM = "A CAM/DJI_20260824204625_0054_D.MP4"
B_REEL = ["B CAM/DJI_20260824204627_0038_D.MP4", "B CAM/DJI_20260824211420_0039_D.MP4"]
LEVEL = "b_action"
EVENTS = ["evt_02", "evt_04", "evt_06", "evt_08"]     # 4 inserts
NEXT_EVENT = "evt_10"                                  # supplies the closing A cut point

pack = json.load(open(PACK))
ev = {e["event_id"]: e for e in pack["events"]}
span_start = float(pack["span"]["start"])
span_end = float(ev[NEXT_EVENT]["context"]["nearest_beat"])   # next event anchor
trim = json.load(open(GRADE_DIR + "/camera_normalization_final.json"))["solved_pre_grade"]

inserts = []
for eid in EVENTS:
    c = next(c for c in ev[eid]["candidates"] if c["id"] == LEVEL)
    inserts.append({"event": eid, "candidate": c["id"],
                    "tl_in": c["timeline_start"], "tl_out": c["timeline_end"],
                    "src_in": c["start"], "src_out": c["end"], "dur": c["duration_s"]})

tl, cur = [], span_start
for c in inserts:
    if c["tl_in"] - cur > 1e-6:
        tl.append({"angle": "A", "source_in": round(cur, 3), "source_out": round(c["tl_in"], 3)})
    tl.append({"angle": "B", "source_in": round(c["src_in"], 3), "source_out": round(c["src_out"], 3)})
    cur = c["tl_out"]
tl.append({"angle": "A", "source_in": round(cur, 3), "source_out": round(span_end, 3)})
for i, c in enumerate(tl, 1):
    c["id"] = "cut_%03d" % i

total = round(sum(c["source_out"] - c["source_in"] for c in tl), 3)
n_b = sum(1 for c in tl if c["angle"] == "B")

# --- self-checks on the construction, before anything is rendered -------------
assert n_b == 4, "expected exactly 4 B inserts, built %d" % n_b
assert tl[-1]["angle"] == "A", "timeline must end in A"
assert tl[-1]["source_out"] - tl[-1]["source_in"] >= 14.0, "closing A tail too short"
assert abs(total - (span_end - span_start)) < 1e-6
for i, c in enumerate(tl):
    if c["angle"] == "B":
        assert i + 1 < len(tl) and tl[i + 1]["angle"] == "A", "a B insert is not followed by A"

job = "Test3_Duration_Proof_4Insert"
man = {"manifest_version": 1, "job_id": job,
       "template": "club_dispatch_standard_v1",
       "sources": {"a_reel": A_CAM, "b_reel": B_REEL},
       "timeline": [{"id": c["id"], "angle": c["angle"], "source_in": c["source_in"],
                     "source_out": c["source_out"]} for c in tl],
       "profiles": {"grade": "club_dispatch_pb3_v1", "crop": "cd_a_pushin_v1",
                    "graphics": "cd_bug_v9", "audio": "cd_youtube_v1"},
       "pre_grades": {B_REEL[0]: trim, B_REEL[1]: trim}}
os.makedirs(OUT, exist_ok=True)
p = os.path.join(OUT, "%s.yaml" % job.lower())
yaml.safe_dump(man, open(p, "w"), sort_keys=False, default_flow_style=False)

print("proof manifest : %s" % p)
print("span           : %.3f -> %.3f  (closing cut point = %s anchor %.3f)"
      % (span_start, span_end, NEXT_EVENT, span_end))
print("total          : %.3fs   cuts %d   B inserts %d" % (total, len(tl), n_b))
t = 0.0
for c in tl:
    d = c["source_out"] - c["source_in"]
    print("  %s %-3s proof %.3f-%.3f  %7.3fs  src %.3f-%.3f%s"
          % (c["id"], c["angle"], t, t + d, d, c["source_in"], c["source_out"],
             "   <-- B INSERT" if c["angle"] == "B" else ""))
    t += d
json.dump({"job": job, "manifest": p, "total_s": total, "span": [span_start, span_end],
           "inserts": inserts, "served_audio_offset_s": span_start,
           "next_event_cut_point": NEXT_EVENT},
          open(os.path.join(OUT, "proof_basis.json"), "w"), indent=1)

r = subprocess.run([V, "/opt/video-studio/tools/manifest/validate_manifest.py",
                    "--media-root", MEDIA, "--json", p], capture_output=True, text=True)
try:
    d = json.loads(r.stdout)
except Exception:
    print("validator rc=%s %s%s" % (r.returncode, r.stdout[:300], r.stderr[:300]))
    sys.exit(1)
print("validator      : rc=%s valid=%s errors=%s" % (r.returncode, d.get("valid"), d.get("errors")))
for n in (d.get("notes") or [])[:5]:
    print("  note:", n)
sys.exit(0 if r.returncode == 0 else 1)
