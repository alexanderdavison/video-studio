#!/usr/bin/env python3
"""test_chain_report.py — camera-match invariant regression (PRODUCTION LOCK v1).

Purely local: builds synthetic registries/manifests/segment lists and calls the renderer's own
chain_report(), so the four violation classes are proven WITHOUT rendering anything and WITHOUT
touching the live registry. Discipline (PRODUCTION LOCK v1 §10): every gate needs a known-good
PASS and a known-bad FAIL.

  C01 approved chain (per-channel trim + locked B grade)      -> PASS
  C02 the set01 v1 defect (auto shadow-lift pre-grade + grade) -> FAIL duplicate_shadow_lift
  C03 the same chain as both pre-grade and grade               -> FAIL duplicate_creative_grade
  C04 a range conversion in the crop/normalization stage       -> FAIL unexpected_range_conversion
  C05 a registry with no dedicated B match grade, no pre-grade -> FAIL missing_approved_camera_match
  C06 live registry + no declared pre-grade (auto refused)     -> PASS, auto_pre_grade_refused=True

Run: /opt/video-studio/tools/venv/bin/python /opt/video-studio/tools/tests/test_chain_report.py
Exit code = number of failures (0 = all pass).
"""
import importlib.util
import json
import sys

RF = "/opt/video-studio/tools/render/render_final.py"
REG = "/opt/video-studio/tools/manifest/profile_registry.json"
CAL = ("/opt/video-studio/projects/2026-08-25-tester/work/analysis/test1_grade/"
       "camera_normalization_final.json")

spec = importlib.util.spec_from_file_location("rf", RF)
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)

REGISTRY = json.load(open(REG))
TRIM = json.load(open(CAL))["solved_pre_grade"]
GRADE_B = REGISTRY["profiles"]["grade"]["club_dispatch_pb3_b_v1"]["locked_params"]
AUTO = "curves=all='0.00/0.03 0.05/0.28 1.00/1.00'"
A_SRC = "/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4"
B_SRC = "/mnt/media/raw/B CAM/DJI_20260824204627_0038_D.MP4"


def manifest(pre_grades=None, crop="cd_a_pushin_v1"):
    m = {"manifest_version": 1, "job_id": "chain-test", "template": "club_dispatch_standard_v1",
         "sources": {"a_reel": A_SRC, "b_reel": [B_SRC]},
         "profiles": {"grade": "club_dispatch_pb3_v1", "crop": crop, "graphics": "orbit_relay_v1",
                      "audio": "cd_youtube_v1"}}
    if pre_grades is not None:
        m["pre_grades"] = pre_grades
    return m


SEGS = [(A_SRC, 0.0, 8.0, 0.0, "A"), (B_SRC, 353.417, 361.296, 8.0, "B"),
        (A_SRC, 8.0, 16.0, 15.879, "A")]
SRCNAME = {A_SRC: "A CAM/DJI_20260824204625_0054_D.MP4",
           B_SRC: "B CAM/DJI_20260824204627_0038_D.MP4"}

PASS = FAIL = 0


def check(name, rep, want_ok, want_violation=None, want_flag=None):
    global PASS, FAIL
    viol = [v["violation"] for v in rep["violations"]]
    ok = (rep["ok"] is want_ok) and (want_violation is None or want_violation in viol)
    if want_flag is not None:
        ok = ok and bool(rep.get(want_flag)) is True
    if ok:
        PASS += 1
        print("  %-58s PASS   ok=%s violations=%s%s"
              % (name, rep["ok"], viol or "-",
                 "" if want_flag is None else " %s=%s" % (want_flag, rep.get(want_flag))))
    else:
        FAIL += 1
        print("  %-58s FAIL   wanted ok=%s violation=%s flag=%s, got ok=%s %s"
              % (name, want_ok, want_violation, want_flag, rep["ok"], viol))


def run(tag, reg, man, pre_grades, segs=SEGS, srcname=SRCNAME):
    rep = rf.chain_report(reg, man["profiles"]["grade"], man, segs, pre_grades, srcname,
                          "/mnt/media/raw")
    return rep


print("== camera-match invariant (chain_report) ==")
check("C01 approved chain (trim + locked B grade)",
      run("C01", REGISTRY, manifest({SRCNAME[B_SRC]: TRIM}), {SRCNAME[B_SRC]: TRIM}),
      True)

check("C02 set01 v1 defect (auto shadow lift + locked grade)",
      run("C02", REGISTRY, manifest({SRCNAME[B_SRC]: AUTO}), {SRCNAME[B_SRC]: AUTO}),
      False, "duplicate_shadow_lift")

dup_man = manifest({SRCNAME[B_SRC]: GRADE_B})
check("C03 same chain as pre-grade AND grade",
      run("C03", REGISTRY, dup_man, {SRCNAME[B_SRC]: GRADE_B}),
      False, "duplicate_creative_grade")

reg_bad = json.loads(json.dumps(REGISTRY))
reg_bad["profiles"]["crop"]["cd_a_pushin_v1"]["locked_params"] = ("scale=-2:2160,"
                                                                "crop=iw*0.87:ih*0.87,format=rgb24")
check("C04 range conversion in the normalization stage",
      run("C04", reg_bad, manifest(), {}), False, "unexpected_range_conversion")

reg_nomatch = json.loads(json.dumps(REGISTRY))
reg_nomatch["profiles"]["grade"].pop("club_dispatch_pb3_b_v1")
check("C05 no dedicated B grade and no approved pre-grade",
      run("C05", reg_nomatch, manifest(), {}), False, "missing_approved_camera_match")

check("C06 live registry, no declared pre-grade (auto refused)",
      run("C06", REGISTRY, manifest(), {}), True, want_flag="auto_pre_grade_refused")

print()
print("== RESULT: %d passed, %d failed ==" % (PASS, FAIL))
sys.exit(FAIL)
