#!/usr/bin/env python3
"""check_match_provenance.py — the shipping manifest must reference the VALIDATED camera matches.

Invariant (Ish, 2026-09-16):
    The transform that passed QC must be the transform referenced by the shipping manifest.
Not "equivalent", not "same lineage", not "house policy plus something similar". Exact identity.

Why this exists: Set 02's acceptance proofs measured the B transform from camera_match_b.json,
while manifest_final.yaml rendered a different house-lineage B curve. Both the gate and the
shipping path were internally consistent, and nothing compared them, so "B PASS 8/8" certified a
curve that production would never apply. No colour band tightening can catch that class of defect,
because the numbers were never wrong — they were measurements of a different artifact.

Usage:
  check_match_provenance.py --validated-manifest M1.yaml --shipping-manifest M2.yaml
  check_match_provenance.py --ship-check --manifest M.yaml --qc-json RESULT.json

Exit:
  0 = identical identities for every angle the shipping manifest declares
  1 = MISMATCH (fail closed — do not render)
  2 = usage/error, or a required identity could not be established
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import camera_match as CM  # noqa: E402
import yaml  # noqa: E402


def manifest_identities(path):
    man = yaml.safe_load(open(path))
    src = man.get("sources") or {}
    pg = man.get("pre_grades") or {}

    def ident(entry):
        if not entry:
            return None
        return {"mode": CM.match_mode(entry), "sha256": CM.match_sha256(entry)}

    a_key = src.get("a_reel")
    b_keys = src.get("b_reel") or []
    if isinstance(b_keys, str):
        b_keys = [b_keys]
    return {"A": [ident(pg.get(a_key)) if a_key else None],
            "B": [ident(pg.get(k)) for k in b_keys]}


def short(h):
    return (h or "-")[:12]


def compare(v, s):
    """Compare two {angle: [identity,...]} maps. Returns (ok, lines).

    Fail closed: an angle the shipping manifest does not declare, or declares with a different
    number of entries, cannot be proven equal to the validated transform and therefore fails.
    """
    lines = []
    ok = True
    for angle in ("A", "B"):
        vv, ss = v.get(angle) or [], s.get(angle) or []
        if not ss:
            lines.append("  %-2s : shipping declares NO match (%d validated) -> ABSENT, cannot prove"
                         % (angle, len(vv)))
            ok = False
            continue
        if len(ss) != len(vv):
            lines.append("  %-2s : entry count differs (validated %d, shipping %d) -> MISMATCH"
                         % (angle, len(vv), len(ss)))
            ok = False
            continue
        for i, si in enumerate(ss):
            if si is None:
                lines.append("  %-2s[%d] : shipping entry is EMPTY -> fail closed" % (angle, i))
                ok = False
                continue
            vi = vv[i] if i < len(vv) else None
            same = vi is not None and vi.get("sha256") == si.get("sha256")
            lines.append("  %-2s[%d] : validated %s (%s) | shipping %s (%s)  %s"
                         % (angle, i, short(vi["sha256"] if vi else None),
                            (vi or {}).get("mode", "-"), short(si["sha256"]), si.get("mode"),
                            "OK" if same else "MISMATCH"))
            if not same:
                ok = False
    return ok, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validated-manifest", help="manifest that rendered the ACCEPTED QC artifact")
    ap.add_argument("--shipping-manifest", help="manifest production will render")
    ap.add_argument("--manifest", help="single manifest to check against a QC result")
    ap.add_argument("--qc-json", help="accepted QC result carrying camera_matches")
    a = ap.parse_args()

    if a.qc_json:
        qc = json.load(open(a.qc_json))
        got = qc.get("camera_matches")
        if not got:
            print(json.dumps({"verdict": "ERROR",
                              "error": "QC result carries no camera_matches; cannot prove provenance",
                              "qc_json": a.qc_json}, indent=1))
            return 2
        ship = manifest_identities(a.manifest) if a.manifest else got
        ok, lines = compare(got, ship)
        print("QC-validated vs shipping manifest:")
        print("\n".join(lines))
        print("\nverdict: %s" % ("PASS" if ok else "FAIL"))
        print(json.dumps({"verdict": "PASS" if ok else "FAIL",
                          "qc_json": a.qc_json, "manifest": a.manifest,
                          "validated": got, "shipping": ship}, indent=1))
        return 0 if ok else 1

    if not (a.validated_manifest and a.shipping_manifest):
        print(json.dumps({"verdict": "ERROR",
                          "error": "need --validated-manifest and --shipping-manifest, "
                                   "or --manifest with --qc-json"}, indent=1))
        return 2

    v = manifest_identities(a.validated_manifest)
    s = manifest_identities(a.shipping_manifest)
    ok, lines = compare(v, s)
    print("validated: %s" % a.validated_manifest)
    print("shipping : %s" % a.shipping_manifest)
    print("\n".join(lines))
    print("\nverdict: %s" % ("PASS" if ok else "FAIL"))
    print(json.dumps({"verdict": "PASS" if ok else "FAIL",
                      "validated_manifest": a.validated_manifest,
                      "shipping_manifest": a.shipping_manifest,
                      "validated": v, "shipping": s}, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
