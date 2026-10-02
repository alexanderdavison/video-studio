#!/usr/bin/env python3
"""Regression test for the camera-match provenance invariant.

INVARIANT: the transform that passed QC must be the transform referenced by the shipping manifest.
Exact identity, not lineage. Production must fail closed on divergence.

The defect this guards (Set 02, 2026-09-16): the acceptance proofs measured the B transform from
camera_match_b.json while manifest_final.yaml rendered a different house-lineage B curve. Every
number was right and every gate passed, because the gate and the shipping path were measuring
different artifacts. No band tightening can detect that; only an identity check can.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TOOLS, "lib"))

spec = importlib.util.spec_from_file_location(
    "check_match_provenance", os.path.join(TOOLS, "produce", "check_match_provenance.py"))
CMP = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CMP)

A_REC = "curves=r='0.0000/0.0000 0.5000/0.1216 1.0000/0.1373'"
B1_REC = "curves=r='0.0000/0.0000 0.5000/0.3843 1.0000/0.5000'"
B2_REC = "curves=r='0.0000/0.0000 0.5000/0.2000 1.0000/0.3000'"
A_ALT = "curves=r='0.0000/0.0000 0.5000/0.9900 1.0000/0.9999'"
B1_ALT = "curves=r='0.0000/0.0000 0.5000/0.9000 1.0000/0.9999'"

A_KEY = "job/A CAM/A.MP4"
B1 = "job/B CAM/B1.MP4"
B2 = "job/B CAM/B2.MP4"


def write_manifest(path, a_entry, b1_entry, b2_entry, drop_b2=False):
    def blk(entry):
        return "{}" if entry is None else json.dumps(entry)
    lines = ["job_id: t", "sources:", "  a_reel: %s" % A_KEY, "  b_reel:", "  - %s" % B1]
    if not drop_b2:
        lines.append("  - %s" % B2)
    lines += ["pre_grades:",
              "  %s: %s" % (A_KEY, blk(a_entry)),
              "  %s: %s" % (B1, blk(b1_entry))]
    if not drop_b2:
        lines.append("  %s: %s" % (B2, blk(b2_entry)))
    open(path, "w").write("\n".join(lines) + "\n")


def main():
    checks = []
    d = tempfile.mkdtemp(prefix="matchprov_")

    def case(name, validated, shipping, expect_ok, drop_b2=False):
        vp = os.path.join(d, "%s_v.yaml" % name)
        sp = os.path.join(d, "%s_s.yaml" % name)
        write_manifest(vp, *validated)
        write_manifest(sp, *shipping, drop_b2=drop_b2)
        v = CMP.manifest_identities(vp)
        s = CMP.manifest_identities(sp)
        ok, lines = CMP.compare(v, s)
        checks.append((name, ok == expect_ok, ok, expect_ok, lines))

    full = (A_REC, B1_REC, B2_REC)

    case("identical", full, full, True)
    case("b_diverges", full, (A_REC, B1_REC, B1_ALT), False)
    case("a_diverges", full, (A_ALT, B1_REC, B2_REC), False)
    case("a_absent", full, (None, B1_REC, B2_REC), False)
    case("b2_absent", full, (A_REC, B1_REC, None), False)
    case("b_entry_count_differs", full, full, False, drop_b2=True)

    # time-varying identity must be ONE hash over the whole entry, and must differ from any
    # single control point's hash (otherwise a manifest could swap points and still match)
    tv = {"mode": "time_varying", "interpolation": "linear", "units": "source_seconds",
          "max_chunk_s": 20.0,
          "points": [{"t": 0.0, "curve": B1_REC}, {"t": 100.0, "curve": B2_REC}]}
    tvp = os.path.join(d, "tv.yaml")
    write_manifest(tvp, A_REC, tv, B2_REC)
    v = CMP.manifest_identities(tvp)
    h_tv = (v["B"][0] or {}).get("sha256")
    sys.path.insert(0, os.path.join(TOOLS, "lib"))
    import camera_match as CM
    h_pt = CM.match_sha256(B1_REC)
    tv_ok = bool(h_tv) and (v["B"][0].get("mode") == "time_varying") and h_tv != h_pt
    checks.append(("tv_identity_is_whole_entry_hash", tv_ok, tv_ok, True, []))

    print("camera-match provenance invariant tests")
    bad = 0
    for name, good, ok, expect, lines in checks:
        print("  [%s] %-30s verdict=%-5s expected=%s"
              % ("PASS" if good else "FAIL", name, ok, expect))
        if not good:
            bad += 1
            for ln in lines:
                print("        %s" % ln)
    print()
    print("%d/%d checks passed" % (len(checks) - bad, len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
