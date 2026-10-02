#!/usr/bin/env python3
"""run_golden_fixture.py — the golden regression fixture (PRODUCTION LOCK v1 §8/§10).

A short, deterministic acceptance run over real source material that exercises every stage the
production renderer and its QC depend on:

  A camera          segments cut from the A master (with the push-in crop)
  B card 1 / card 2 both B cards appear
  real cut boundaries A -> B -> A walking the frame-boundary timing
  a processing boundary a B insert that CROSSES the card1 -> card2 seam (auto-split + handoff)
  camera match      the approved per-channel B trim (declared in the manifest)
  audio             the canonical audiosync procedure (master audio at the served offset)
  Orbit Relay       the graphics profile from the production manifest

The fixture is SELF-CONTAINED: the basis it feeds to QC is generated from its own synthetic
timeline (boundary ownership, expected cuts, source ownership, audio expectation), never copied
from a real program's basis — a synthetic non-contiguous window has its own, different contract.

Artifacts compared like-for-like: the graphics check gets the renderer's own 1080p clean reference
(`video_only.mp4`, kept by --keep-temp), not the 540p review proxy, because diffing two different
rasters inflates the encoder-noise floor and makes the cutoff unreachable.

Usage:
  run_golden_fixture.py                 build, render, run every check (canonical acceptance)
  run_golden_fixture.py --build-only    write the manifest + basis and stop
  run_golden_fixture.py --reuse-renders re-run the checks against artifacts already rendered
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PY = "/opt/video-studio/tools/venv/bin/python"
T = "/opt/video-studio/tools"
RAW = "/mnt/media/raw"
JOB = "/opt/video-studio/jobs/club-dispatch-set01-2026-08-24"
SRC_MAN = JOB + "/club-dispatch-set01-2026-08-24_manifest_final_v2_cameramatch.yaml"
FIX_JOB = "club-dispatch-golden-fixture-v1"
OUT = "/tmp/golden_fixture"
SEAM_REEL_S = 1672.5973          # card1 -> card2 boundary, in B reel coordinates
FRAME_S = 1001.0 / 30000.0       # one delivery frame

# segment ids must match the manifest contract ^cut_\d{3}$
FIXTURE = [("A", 346.695, 354.695, "cut_001"),
           ("B", 353.417, 361.296, "cut_002"),
           ("A", 361.296, 369.296, "cut_003"),
           ("B", 1670.000, 1678.000, "cut_004"),      # crosses the card1 -> card2 seam
           ("A", 900.000, 940.000, "cut_005")]


def run(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print("FAILED: %s\n%s\n%s" % (" ".join(map(str, cmd)), r.stdout[-2000:], r.stderr[-2000:]))
        sys.exit(2)
    return r


def build():
    import yaml
    src = yaml.safe_load(open(SRC_MAN))
    man = {"manifest_version": src.get("manifest_version", 1), "job_id": FIX_JOB,
           "template": src.get("template"), "sources": src["sources"],
           "timeline": [], "profiles": src.get("profiles")}
    if src.get("pre_grades"):
        man["pre_grades"] = src["pre_grades"]              # the APPROVED camera match
    segs, t = [], 0.0
    for angle, si, so, sid in FIXTURE:
        man["timeline"].append({"angle": angle, "source_in": si, "source_out": so, "id": sid})
        segs.append({"angle": angle, "source_in": si, "source_out": so,
                     "proof_in": round(t, 3), "proof_out": round(t + (so - si), 3), "id": sid})
        t += so - si
    total = round(t, 3)
    man_path = OUT + "/golden_fixture_manifest.yaml"
    yaml.safe_dump(man, open(man_path, "w"), sort_keys=False)

    b_screen = sum(so - si for ang, si, so, _ in FIXTURE if ang == "B")
    b_ins = [s for s in segs if s["angle"] == "B"]
    cross = [s for s in b_ins if s["source_in"] < SEAM_REEL_S < s["source_out"]][0]
    seam_tl = round(cross["proof_in"] + (SEAM_REEL_S - cross["source_in"]), 3)
    basis = {
        "job": FIX_JOB, "manifest": man_path, "total_s": total, "span": [0.0, total],
        "segments": segs, "n_segments": len(segs),
        "sections": [{"id": "fixture", "start_s": 0.0, "end_s": total,
                      "segments": [s["id"] for s in segs]}],
        "inserts_by_section": {"fixture": len(b_ins)}, "inserts_total": len(b_ins),
        "sections_without_insert": [],
        "endpoint_candidates": [], "endpoint_rejected": [], "n_holds": 0,
        "endpoint_rule": "n/a — synthetic fixture window, not a coverage-governed program",
        "served_audio_offset_s": 0.0,
        "sync_residuals": [],
        "a_recovery_at_insert_entries": [], "coalesced_A_joins": [],
        "boundary_ownership": [{
            "boundary_timeline_s": seam_tl,
            "final_committed_shot_from_previous_section": dict(cross, length_s=round(cross["source_out"] - cross["source_in"], 3)),
            "boundary_lies_inside": dict(cross, length_s=round(cross["source_out"] - cross["source_in"], 3)),
            "committed_territory_ends_at_timeline_s": seam_tl,
            "earliest_timeline_position_next_section_may_alter": seam_tl,
            "next_shot_after_boundary": {"id": "cut_005", "angle": "A"},
            "boundary_is_a_cut": False,
            "cut_at_boundary": None,
            "note": ("card1 -> card2 processing boundary inside one B insert: there is no editorial "
                     "cut here, so the picture must be visually continuous across it — the same "
                     "invariant the production checker applies to a real program"),
        }],
        "longest_A_run_s": 40.0, "b_screen_time_s": round(b_screen, 3),
        "b_pct_of_program": round(100.0 * b_screen / total, 2),
        "policy": {"source": "golden fixture — basis generated from the fixture's own timeline"},
        "inputs": {"golden_fixture": True, "seam_reel_s": SEAM_REEL_S,
                   "audio_contract": "canonical audiosync copy: master audio at served_audio_offset_s"},
    }
    basis_path = OUT + "/golden_fixture_basis.json"
    json.dump(basis, open(basis_path, "w"), indent=1)
    return man_path, basis_path, total, segs, src, len(man.get("pre_grades") or {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--reuse-renders", action="store_true")
    ap.add_argument("--label", default="golden_fixture_v1")
    a = ap.parse_args()
    Path(OUT).mkdir(parents=True, exist_ok=True)
    man_path, basis_path, total, segs, src, n_pre = build()
    print("fixture manifest : %s  (%d segments, %.3f s)" % (man_path, len(segs), total))
    print("fixture basis    : %s  (self-generated: %d boundary(ies), audio offset %.1f)"
          % (basis_path, 1, 0.0))
    print("approved B match : %s" % ("declared (%d sources)" % n_pre if n_pre
                                     else "NOT DECLARED — check the manifest"))
    if a.build_only:
        print("BUILD ONLY — no render started")
        return 0

    clean = "%s/%s_clean.mp4" % (OUT, a.label)                 # 540p review proxy
    audi = "%s/%s_audiosync.mp4" % (OUT, a.label)              # canonical audio copy
    branded = "%s/%s_branded.mp4" % (OUT, a.label)             # 1080p delivery render
    import glob as _glob
    _ref = _glob.glob("%s/work_branded/*/video_only.mp4" % OUT)   # renderer keeps it per job
    clean1080 = _ref[0] if _ref else "%s/work_branded/%s/video_only.mp4" % (OUT, FIX_JOB)
    plan_path = OUT + "/golden_fixture_plan.json"
    results = {"label": a.label, "fixture_version": 2, "manifest": man_path, "basis": basis_path,
               "total_s": total, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "discrimination_required": True, "checks": []}

    def step(name, cmd, ok_extra=None):
        t0 = time.time()
        r = run(cmd, check=False)
        ok = (r.returncode == 0) if ok_extra is None else bool(ok_extra(r))
        tail = [l for l in r.stdout.strip().splitlines() if l.strip()]
        detail = tail[-1][:220] if tail else (r.stderr.strip().splitlines() or [""])[-1][:220]
        results["checks"].append({"name": name, "rc": r.returncode, "pass": ok,
                                  "s": round(time.time() - t0, 1), "detail": detail})
        print("[%s] %-32s rc=%d %6.1fs  %s" % ("PASS" if ok else "FAIL", name, r.returncode,
                                               time.time() - t0, detail), flush=True)
        return r

    # 1. manifest must validate before anything is rendered
    step("manifest_validates", [PY, T + "/manifest/validate_manifest.py", man_path,
                                "--media-root", RAW])
    # 2. camera-match invariant on the EFFECTIVE resolved chain (fails closed on a duplicate)
    def chain_ok(r):
        try:
            c = json.loads(r.stdout)
            results["camera_match"] = {"ok": c["ok"], "violations": c["violations"],
                                       "lifts": {k: v["shadow_lifts"] for k, v in c["angles"].items()}}
            return c["ok"]
        except Exception:
            results["camera_match"] = {"ok": False, "error": "no JSON"}
            return False
    step("camera_match_invariant", [PY, T + "/render/render_final.py", man_path,
                                    "--media-root", RAW, "--dump-chain"], ok_extra=chain_ok)
    # 3. graphics plan resolves for this program (asset root, frame sequence, geometry)
    def plan_ok(r):
        try:
            json.loads(r.stdout)
            open(plan_path, "w").write(r.stdout)
            return r.returncode == 0
        except Exception:
            open(plan_path, "w").write("{}")
            return False
    step("graphics_plan_resolves", [PY, T + "/render/render_final.py", man_path,
                                    "--media-root", RAW, "--dump-plan"], ok_extra=plan_ok)

    if not a.reuse_renders:
        # stale outputs from an earlier fixture run must go: the renderer refuses to overwrite a
        # published partial, and a leftover file would make a check read the PREVIOUS artifact.
        import glob
        for pat in (clean, audi, branded, clean + ".*", audi + ".*", branded + ".*",
                    OUT + "/work_clean", OUT + "/work_branded", OUT + "/ALLDONE"):
            for f in glob.glob(pat):
                subprocess.run(["rm", "-rf", f])
        # 4. the review proxy (clean) — also the video source for the audiosync copy
        step("clean_proof_render", [PY, T + "/proxy/render_proxy.py", man_path, "--media-root", RAW,
                                    "--work", OUT + "/work_clean", "--out", clean, "--full", "--json"])
        # 5. canonical audiosync procedure: master audio at the served offset, video copied
        off = json.load(open(basis_path))["served_audio_offset_s"]
        step("audiosync_copy", ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", clean,
                                "-ss", str(off), "-i", RAW + "/" + src["sources"]["a_reel"],
                                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                                "-b:a", "192k", "-shortest", "-movflags", "+faststart", "-y", audi])
        # 6. the branded delivery render; --keep-temp keeps the renderer's own 1080p clean reference
        step("branded_final_render", [PY, T + "/render/render_final.py", man_path, "--media-root",
                                      RAW, "--out", branded, "--work", OUT + "/work_branded",
                                      "--keep-temp"])
    else:
        print("     (--reuse-renders: keeping the existing renders)")

    # 7. proof QC on the audiosync copy (the canonical review artifact)
    b_args = []
    for p in src["sources"]["b_reel"]:
        b_args += ["--b", RAW + "/" + p]
    step("qc_proof", [PY, T + "/produce/qc_proof.py", "--basis", basis_path, "--proof", audi,
                      "--a", RAW + "/" + src["sources"]["a_reel"]] + b_args +
                     ["--label", a.label, "--json-out", OUT + "/qc_proof.json"])
    # 8. colour QC on delivered pixels (branded) + on the review copy
    def color_ok(r):
        try:
            v = json.load(open(OUT + "/qc_color_branded.json"))
            results["color"] = {"branded": v["verdict"], "inserts": v.get("b_inserts_checked"),
                                "measured": {k: v.get("measured", {}).get(k) for k in
                                             ("b_luma_mean", "b_p95", "b_clipped_pct", "b_sat_mean")}}
            return v["verdict"] == "PASS"
        except Exception:
            return False
    step("qc_color(branded)", [PY, T + "/produce/qc_color.py", "--delivered", branded, "--basis",
                               basis_path, "--label", a.label, "--json-out", OUT + "/qc_color_branded.json"],
         ok_extra=color_ok)
    step("qc_color(review copy)", [PY, T + "/produce/qc_color.py", "--delivered", audi, "--basis",
                                   basis_path, "--label", a.label + " review copy",
                                   "--json-out", OUT + "/qc_color_review.json"])
    # 9. graphics: placement, phase, alpha — against the renderer's own 1080p clean reference
    if not Path(clean1080).exists():
        results["checks"].append({"name": "graphics_1080p_reference_present", "rc": None, "pass": False,
                                  "s": 0, "detail": "video_only.mp4 missing from work_branded"})
        print("[FAIL] %-32s video_only.mp4 missing — qc_graphics would compare different rasters"
              % "graphics_1080p_reference_present", flush=True)
    else:
        step("graphics_1080p_reference_present", ["test", "-s", clean1080])
    step("qc_graphics", [PY, T + "/produce/qc_graphics.py", "--branded", branded,
                         "--clean", clean1080, "--plan", plan_path, "--basis", basis_path,
                         "--cut-time", str(round(total / 2.0, 3)),
                         "--json-out", OUT + "/qc_graphics.json"])
    # 10. delivery contract on the delivered bytes
    step("qc_delivery", [PY, T + "/produce/qc_delivery.py", "--delivered", branded, "--basis",
                         basis_path, "--label", a.label, "--json-out", OUT + "/qc_delivery.json"])

    bad = [c["name"] for c in results["checks"] if not c["pass"]]
    results["verdict"] = "PASS" if not bad else "FAIL"
    results["failed"] = bad
    results["checks_passed"] = len(results["checks"]) - len(bad)
    results["checks_total"] = len(results["checks"])
    results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    json.dump(results, open(OUT + "/golden_fixture_result.json", "w"), indent=1)
    print("\nGOLDEN FIXTURE %s — %d/%d checks passed%s"
          % (results["verdict"], results["checks_passed"], results["checks_total"],
             "" if not bad else " | FAILED: " + ", ".join(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
