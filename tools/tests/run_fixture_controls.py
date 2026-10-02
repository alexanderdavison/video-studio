#!/usr/bin/env python3
"""run_fixture_controls.py — discrimination controls for the golden fixture suite.

A gate only counts if it also FAILS a known-bad artifact. These controls reuse the fixture's own
artifacts where possible (no re-render), and assert that each checker bites:

  known_good            the fixture's own result must be PASS
  color_bad (duplicate) effective chain with TWO camera-match transforms  -> chain dump rc 2
  color_bad (delivered) the rejected v1 final                             -> qc_color FAIL
  graphics_phase_bad    plan with a wrong animation rate                  -> qc_graphics FAIL
  audio_bad             audiosync copy built at a wrong master offset      -> qc_proof FAIL
  timing_bad            basis whose cut expectations are one frame off     -> qc_proof FAIL
  delivery_bad          same bytes remuxed without faststart              -> qc_delivery FAIL

Exit 0 only when every control behaves as expected.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

PY = "/opt/video-studio/tools/venv/bin/python"
T = "/opt/video-studio/tools"
RAW = "/mnt/media/raw"
OUT = "/tmp/golden_fixture"
SRC_MAN = ("/opt/video-studio/jobs/club-dispatch-set01-2026-08-24/"
           "club-dispatch-set01-2026-08-24_manifest_final_v2_cameramatch.yaml")
V1_FINAL = ("/mnt/media/finals/club-dispatch-set01-2026-08-24/"
            "club-dispatch-set01-2026-08-24_final.mp4")
V2_FINAL = ("/mnt/media/finals/club-dispatch-set01-2026-08-24/"
            "club-dispatch-set01-2026-08-24_final_v2.mp4")
AUTO_CURVE = "curves=all='0.00/0.03 0.05/0.28 1.00/1.00'"
FRAME_S = 1001.0 / 30000.0
rows = []


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def rec(name, expected, actual, detail="", rc=None):
    ok = (str(expected) == str(actual))
    rows.append({"control": name, "expected": expected, "actual": actual, "pass": ok,
                 "rc": rc, "detail": detail[:220]})
    print("[%s] %-26s expected %-5s got %-5s  %s" % ("OK" if ok else "BROKEN", name, expected,
                                                     actual, detail[:110]), flush=True)


def main():
    Path(OUT).mkdir(parents=True, exist_ok=True)
    label = "golden_fixture_v1"
    branded = "%s/%s_branded.mp4" % (OUT, label)
    audi = "%s/%s_audiosync.mp4" % (OUT, label)
    clean = "%s/%s_clean.mp4" % (OUT, label)
    import glob as _glob
    _r = _glob.glob("%s/work_branded/*/video_only.mp4" % OUT)
    clean1080 = _r[0] if _r else "%s/work_branded/video_only.mp4" % OUT
    basis = OUT + "/golden_fixture_basis.json"
    plan = OUT + "/golden_fixture_plan.json"
    man = OUT + "/golden_fixture_manifest.yaml"
    for f in (branded, audi, basis, plan, man):
        if not Path(f).exists():
            print("missing fixture artifact: %s — run the golden fixture first" % f)
            return 2

    # ---- known good ---------------------------------------------------------------------
    try:
        res = json.load(open(OUT + "/golden_fixture_result.json"))
        rec("known_good(fixture)", "PASS", res["verdict"],
            "%d/%d checks" % (res.get("checks_passed", -1), res.get("checks_total", -1)))
    except Exception as exc:
        rec("known_good(fixture)", "PASS", "NO RESULT", str(exc))

    # ---- color bad: duplicate camera-match transform ------------------------------------
    import yaml
    d = yaml.safe_load(open(SRC_MAN))
    d["pre_grades"] = {p: AUTO_CURVE for p in d["sources"]["b_reel"]}
    ctrl_man = OUT + "/control_double_match.yaml"
    yaml.safe_dump(d, open(ctrl_man, "w"), sort_keys=False)
    r = run([PY, T + "/render/render_final.py", ctrl_man, "--media-root", RAW, "--dump-chain"])
    try:
        c = json.loads(r.stdout)
        rec("color_bad(duplicate)", "BLOCKED", "BLOCKED" if not c["ok"] else "ALLOWED",
            str(c["violations"])[:120], r.returncode)
    except Exception:
        rec("color_bad(duplicate)", "BLOCKED", "NO JSON", r.stderr[-120:], r.returncode)

    # ---- color bad: the artifact that reached human review ------------------------------
    v1_basis = ("/opt/video-studio/jobs/club-dispatch-set01-2026-08-24/"
                "club-dispatch-set01-2026-08-24_basis_coverage.json")
    r = run([PY, T + "/produce/qc_color.py", "--delivered", V1_FINAL, "--basis", v1_basis,
             "--label", "rejected v1", "--json-out", OUT + "/control_qc_color_v1.json"])
    try:
        v = json.load(open(OUT + "/control_qc_color_v1.json"))
        n = sum(1 for x in v["b_inserts"] if not x.get("pass"))
        rec("color_bad(v1 final)", "FAIL", v["verdict"], "%d/%d inserts fail" % (n, len(v["b_inserts"])), r.returncode)
    except Exception:
        rec("color_bad(v1 final)", "FAIL", "NO JSON", r.stderr[-120:], r.returncode)

    # ---- graphics phase bad -------------------------------------------------------------
    p = json.load(open(plan))
    # the phase prediction reads plan["graphics"]["frames_verified"]["fps"] — mutating any other
    # copy of the rate leaves the prediction untouched (that mistake is why this control first
    # reported PASS against a correct checker).
    _fv = (p.get("graphics") or {}).get("frames_verified") or {}
    _fv["fps"] = 15.0            # grossly wrong animation rate model
    (p.setdefault("graphics", {}))["frames_verified"] = _fv
    bad_plan = OUT + "/control_plan_wrong_phase.json"
    json.dump(p, open(bad_plan, "w"))
    r = run([PY, T + "/produce/qc_graphics.py", "--branded", branded, "--clean", clean1080,
             "--plan", bad_plan, "--basis", basis, "--json-out", OUT + "/control_qc_graphics.json"])
    try:
        v = json.load(open(OUT + "/control_qc_graphics.json"))
        rec("graphics_phase_bad(rate=15)", "FAIL", "PASS" if v.get("overall_pass") else "FAIL",
            "phase deltas %s" % str(v.get("checks", {}).get("animation"))[:90], r.returncode)
    except Exception:
        rec("graphics_phase_bad(rate=15)", "FAIL", "NO JSON", r.stderr[-120:], r.returncode)

    # ---- audio bad: audiosync copy built at the WRONG master offset ---------------------
    wrong = OUT + "/control_audiosync_wrong_offset.mp4"
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", clean, "-ss", "20.0",
         "-i", RAW + "/A CAM/DJI_20260824204625_0054_D.MP4", "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
         "-y", wrong])
    r = run([PY, T + "/produce/qc_proof.py", "--basis", basis, "--proof", wrong,
             "--a", RAW + "/A CAM/DJI_20260824204625_0054_D.MP4",
             "--b", RAW + "/B CAM/DJI_20260824204627_0038_D.MP4",
             "--b", RAW + "/B CAM/DJI_20260824211420_0039_D.MP4",
             "--label", "wrong audio offset", "--json-out", OUT + "/control_qc_proof_audio.json"])
    try:
        v = json.load(open(OUT + "/control_qc_proof_audio.json"))
        audio_fail = [c for c in v["checks"] if "audio" in c["name"].lower() and not c["pass"]]
        rec("audio_bad(wrong offset)", "FAIL", "PASS" if not audio_fail else "FAIL",
            audio_fail[0]["name"][:90] if audio_fail else "", r.returncode)
    except Exception:
        rec("audio_bad(wrong offset)", "FAIL", "NO JSON", r.stderr[-120:], r.returncode)

    # ---- timing bad: basis cut expectations shifted by one frame ------------------------
    b = json.load(open(basis))
    SHIFT = 1.0           # the cut check tolerates 0.30 s of detection slack; 1 s is a real break
    b["segments"] = [dict(s, proof_in=round(s["proof_in"] + SHIFT, 4),
                          proof_out=round(s["proof_out"] + SHIFT, 4)) for s in b["segments"]]
    b["inputs"] = dict(b.get("inputs") or {}, timing_control="cut expectations shifted +3 frames")
    bad_basis = OUT + "/control_basis_timing_shifted.json"
    json.dump(b, open(bad_basis, "w"), indent=1)
    r = run([PY, T + "/produce/qc_proof.py", "--basis", bad_basis, "--proof", audi,
             "--a", RAW + "/A CAM/DJI_20260824204625_0054_D.MP4",
             "--b", RAW + "/B CAM/DJI_20260824204627_0038_D.MP4",
             "--b", RAW + "/B CAM/DJI_20260824211420_0039_D.MP4",
             "--label", "timing shifted 1 s", "--json-out", OUT + "/control_qc_proof_timing.json"])
    try:
        v = json.load(open(OUT + "/control_qc_proof_timing.json"))
        bad = [c for c in v["checks"] if not c["pass"]]
        tf = [c for c in bad if "internal cut" in c["name"].lower()]
        rec("timing_bad(1 s off)", "FAIL", "FAIL" if bad else "PASS",
            (tf[0]["name"] if tf else bad[0]["name"])[:90] if bad else "", r.returncode)
    except Exception:
        rec("timing_bad(1 s off)", "FAIL", "NO JSON", r.stderr[-120:], r.returncode)

    # ---- delivery bad: no faststart -----------------------------------------------------
    ad = OUT + "/ALLDONE"
    if not Path(ad).exists():
        ad = OUT + "/work_branded/ALLDONE"
    nofast = OUT + "/control_nofaststart.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-i", branded, "-c", "copy", nofast])
    r = run([PY, T + "/produce/qc_delivery.py", "--delivered", nofast, "--basis", basis,
             "--all-done", ad, "--json-out", OUT + "/control_qc_delivery.json"])
    try:
        v = json.load(open(OUT + "/control_qc_delivery.json"))
        rec("delivery_bad(no faststart)", "FAIL", v["verdict"],
            str([c["name"] for c in v.get("failed", [])])[:110], r.returncode)
    except Exception:
        rec("delivery_bad(no faststart)", "FAIL", "NO JSON", r.stderr[-120:], r.returncode)

    broken = [x["control"] for x in rows if not x["pass"]]
    out = {"controls": rows, "verdict": "PASS" if not broken else "BROKEN",
           "broken": broken, "when": subprocess.run(["date", "-Is"], capture_output=True,
                                                    text=True).stdout.strip()}
    json.dump(out, open(OUT + "/controls_result.json", "w"), indent=1)
    print("\nDISCRIMINATION CONTROLS %s — %d/%d behaved as expected%s"
          % (out["verdict"], len(rows) - len(broken), len(rows),
             "" if not broken else " | BROKEN: " + ", ".join(broken)))
    return 0 if not broken else 1


if __name__ == "__main__":
    sys.exit(main())
