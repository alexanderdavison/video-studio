#!/usr/bin/env python3
"""Materialize and validate the Test 2 A/B section manifests.

Identical edit structure to Test 1 (same section, same shot lengths, same policy). Exactly
two technical changes, both approved:

  1. every B source window shifted by the approved mapping (B_source = A_timeline - 1.2783)
  2. `pre_grades` pinned to the solved per-camera trim for BOTH B cards, so the renderer does
     NOT auto-derive a pre-grade on top of B's already-lifted locked grade (the Test 1
     over-lift that made B cutaways read ~134 against A's ~31)

A = deterministic chain (Test 1 winners, unchanged selection). B = Test 2 OpenAI + reference
chain (from comparison_test2.json).
"""
import json
import os
import subprocess
import sys

import yaml

V = "/opt/video-studio/tools/venv/bin/python"
ROOT = "/opt/video-studio/projects/2026-08-25-tester/work/analysis/test1_ab"
OUT = "/opt/video-studio/projects/2026-08-25-tester/work/analysis/test2_ab"
GRADE_DIR = "/opt/video-studio/projects/2026-08-25-tester/work/analysis/test1_grade"
MEDIA = "/mnt/media/raw"
A_CAM = "A CAM/DJI_20260824204625_0054_D.MP4"
B_REEL = ["B CAM/DJI_20260824204627_0038_D.MP4", "B CAM/DJI_20260824211420_0039_D.MP4"]
SPAN = (300.0, 600.0)
LAG = 1.2783


def build(label, inserts, job):
    tl = []
    cur = SPAN[0]
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
    tot = sum(c["source_out"] - c["source_in"] for c in tl)
    bsec = sum(c["source_out"] - c["source_in"] for c in tl if c["angle"] == "B")
    print("%-22s cuts=%d timeline=%.3fs B=%.1fs (%.1f%%) -> %s"
          % (label, len(tl), tot, bsec, 100.0 * bsec / (SPAN[1] - SPAN[0]), os.path.basename(p)))
    return p


def validate(p):
    r = subprocess.run([V, "/opt/video-studio/tools/manifest/validate_manifest.py",
                        "--media-root", MEDIA, "--json", p], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except Exception:
        print("   validator rc=%s raw %s%s" % (r.returncode, r.stdout[:200], r.stderr[:200]))
        return False
    print("   validator rc=%s valid=%s errors=%s pre_grades_applied=%s"
          % (r.returncode, d.get("valid"), d.get("errors"), d.get("pre_grades_applied")))
    for n in (d.get("notes") or [])[:4]:
        print("   note:", n)
    return r.returncode == 0


if __name__ == "__main__":
    TRIM = json.load(open(GRADE_DIR + "/camera_normalization_final.json"))["solved_pre_grade"]
    cmp2 = json.load(open("/root/test1/comparison_test2.json"))
    # chain_a / chain_b in comparison_test2.json were materialized from the Test 2 pack, so
    # their src_in/src_out are ALREADY the approved shifted values. Do not shift again.
    inserts_t1_a = [{"tl_in": c["tl_in"], "tl_out": c["tl_out"], "src_in": c["src_in"],
                     "src_out": c["src_out"]} for c in cmp2["chain_a"]]
    inserts_t2_b = [{"tl_in": c["tl_in"], "tl_out": c["tl_out"], "src_in": c["src_in"],
                     "src_out": c["src_out"]} for c in cmp2["chain_b"]]
    print("A inserts: %d (Test 1 deterministic winners, B windows shifted by -%.4f s)"
          % (len(inserts_t1_a), LAG))
    print("B inserts: %d (Test 2 OpenAI selections: %s)"
          % (len(inserts_t2_b), ", ".join(c["event"] for c in cmp2["chain_b"])))
    ok = []
    pA = build("A deterministic", inserts_t1_a, "Test2_A_Corrected_Section300_600")
    ok.append(validate(pA))
    pB = build("B openai+reference", inserts_t2_b, "Test2_B_Corrected_Section300_600")
    ok.append(validate(pB))
    json.dump({"lag_s": LAG, "pre_grades_applied_to": B_REEL, "manifests_valid": all(ok),
               "a_inserts": len(inserts_t1_a), "b_inserts": len(inserts_t2_b),
               "note": "identical edit structure to Test 1; only the sync mapping and the "
                       "per-camera normalization differ"},
              open(os.path.join(OUT, "basis.json"), "w"), indent=1)
    print("\nall valid:", all(ok))
    sys.exit(0 if all(ok) else 1)
