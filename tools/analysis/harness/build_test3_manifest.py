#!/usr/bin/env python3
"""Build + validate the Test 3 proof manifest.

The artifact renders the SAME deterministic event set the existing proof uses
(evt_02/04/06/08/10 — the every-other-event chain), with exactly one variable changed:
each B insert now carries its DERIVED B_ACTION length instead of the fixed 4.000 s.

Why not the OpenAI chain: in this section the editorial model selected hold_a on 10 of 11
events and its single B selection (evt_01) is voided by the A-recovery rule because the
section starts 0.78 s before it. That chain therefore contains no B cut at all and cannot
show a duration. The demonstration below isolates the increment instead: same events, same
entry points, same sync mapping, same normalization, derived lengths only.

B_ACTION is used because it is the level the model itself selected at the one event where
it did choose B.
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
MEDIA = "/mnt/media/raw"
A_CAM = "A CAM/DJI_20260824204625_0054_D.MP4"
B_REEL = ["B CAM/DJI_20260824204627_0038_D.MP4", "B CAM/DJI_20260824211420_0039_D.MP4"]
SPAN = (300.0, 600.0)
LAG = 1.2783
LEVEL = "b_action"
EVENTS = ["evt_02", "evt_04", "evt_06", "evt_08", "evt_10"]


def build(job, inserts):
    tl, cur = [], SPAN[0]
    for c in inserts:
        if c["tl_in"] - cur > 1e-6:
            tl.append({"angle": "A", "source_in": round(cur, 3), "source_out": round(c["tl_in"], 3)})
        tl.append({"angle": "B", "source_in": round(c["src_in"], 3), "source_out": round(c["src_out"], 3)})
        cur = c["tl_out"]
    tl.append({"angle": "A", "source_in": round(cur, 3), "source_out": round(SPAN[1], 3)})
    for i, c in enumerate(tl, 1):
        c["id"] = "cut_%03d" % i
    man = {"manifest_version": 1, "job_id": job,
           "template": "club_dispatch_standard_v1",
           "sources": {"a_reel": A_CAM, "b_reel": B_REEL},
           "timeline": [{"id": c["id"], "angle": c["angle"], "source_in": c["source_in"],
                         "source_out": c["source_out"]} for c in tl],
           "profiles": {"grade": "club_dispatch_pb3_v1", "crop": "cd_a_pushin_v1",
                        "graphics": "cd_bug_v9", "audio": "cd_youtube_v1"},
           "pre_grades": {B_REEL[0]: TRIM, B_REEL[1]: TRIM}}
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "%s.yaml" % job.lower())
    yaml.safe_dump(man, open(p, "w"), sort_keys=False, default_flow_style=False)
    bsec = sum(c["source_out"] - c["source_in"] for c in tl if c["angle"] == "B")
    print("%-34s cuts=%d timeline=%.3fs B=%.1fs (%.1f%%) -> %s"
          % (job, len(tl), sum(c["source_out"] - c["source_in"] for c in tl), bsec,
             100.0 * bsec / (SPAN[1] - SPAN[0]), os.path.basename(p)))
    return p


def validate(p):
    r = subprocess.run([V, "/opt/video-studio/tools/manifest/validate_manifest.py",
                        "--media-root", MEDIA, "--json", p], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except Exception:
        print("   validator rc=%s raw %s%s" % (r.returncode, r.stdout[:300], r.stderr[:300]))
        return False
    print("   validator rc=%s valid=%s errors=%s pre_grades_applied=%s"
          % (r.returncode, d.get("valid"), d.get("errors"), d.get("pre_grades_applied")))
    for n in (d.get("notes") or [])[:4]:
        print("   note:", n)
    return r.returncode == 0


if __name__ == "__main__":
    TRIM = json.load(open(GRADE_DIR + "/camera_normalization_final.json"))["solved_pre_grade"]
    pack = json.load(open(ROOT + "/editorial_pkg_300_600/test3_durations/evidence_pack.json"))
    ev = {e["event_id"]: e for e in pack["events"]}
    ins = []
    for eid in EVENTS:
        c = next(c for c in ev[eid]["candidates"] if c["id"] == LEVEL)
        ins.append({"event": eid, "tl_in": c["timeline_start"], "tl_out": c["timeline_end"],
                    "src_in": c["start"], "src_out": c["end"], "dur": c["duration_s"]})
    print("demonstration: %d events, B_ACTION level, derived lengths %s"
          % (len(ins), ", ".join("%s=%.3fs" % (c["event"], c["dur"]) for c in ins)))
    print("recovery gaps:", ", ".join(
        "%.2fs" % (ins[i]["tl_in"] - ins[i - 1]["tl_out"]) for i in range(1, len(ins))))
    p = build("Test3_Duration_Demo_Section300_600", ins)
    ok = validate(p)
    json.dump({"level": LEVEL, "events": ins, "manifest": p, "valid": ok,
               "note": "deterministic event set at DERIVED lengths; not the OpenAI chain "
                       "(which selected no B insert in this section)"},
              open(os.path.join(OUT, "basis.json"), "w"), indent=1)
    print("\nvalid:", ok)
    sys.exit(0 if ok else 1)
