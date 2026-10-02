#!/usr/bin/env python3
"""Build a short Set 02 camera-match proof: representative B inserts across the reel.

Renders through the SAME renderer and the SAME chain as the full program (profiles, push-in,
pre-grade, grade, overlays). Only the timeline length differs, so the colour gate's verdict on
this proof is a valid pre-check for the full re-render.

Usage: build_cammatch_proof.py --match-record <camera_match json> --manifest <stamped manifest> \
                              --out-dir <dir> [--picks cut_002,cut_008,...]
Prints the manifest and basis paths, plus the timeline.
"""
import argparse
import json
import os

import yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match-record", default=None, help="B match record (back-compat alias)")
    ap.add_argument("--match-a", default=None, help="A match record -> pre_grades[a_reel]")
    ap.add_argument("--match-b", default=None, help="B match record -> pre_grades[b_reel]")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--picks", default="cut_002,cut_008,cut_012,cut_020,cut_028,"
                                     "cut_036,cut_044,cut_054")
    ap.add_argument("--context-a-s", type=float, default=20.0)
    ap.add_argument("--context-for", default="",
                    help="per-pick A-context length override, e.g. cut_002=90,cut_054=60; a long "
                         "context makes the renderer split it into time-varying chunks, which is "
                         "where its interpolation joins get exercised")
    a = ap.parse_args()

    if not (a.match_b or a.match_record):
        raise SystemExit("need --match-b (or --match-record) for the B angle")
    rec = json.load(open(a.match_b or a.match_record))
    curve = rec["solved_pre_grade"]
    rec_a = json.load(open(a.match_a)) if a.match_a else None
    lag = float(rec["source_identity"]["lag_a_to_b_s"])
    man = yaml.safe_load(open(a.manifest))
    tl = man["timeline"]
    b_by_id = {s["id"]: s for s in tl if s.get("angle") == "B"}

    picks = [p.strip() for p in a.picks.split(",") if p.strip()]
    ctx_for = {}
    for spec in [x for x in a.context_for.split(",") if x.strip()]:
        k, _, v = spec.partition("=")
        ctx_for[k.strip()] = float(v)
    missing = [p for p in picks if p not in b_by_id]
    if missing:
        raise SystemExit("not in the manifest: %s" % missing)

    os.makedirs(a.out_dir, exist_ok=True)
    out_tl, basis_segs, t = [], [], 0.0
    ctx = 900                                            # validator: id must match ^cut_\\d{3}$
    for pid in picks:
        s = b_by_id[pid]
        p_in = float(s["source_in"]) + lag          # program position of this insert
        ctx_len = ctx_for.get(pid, a.context_a_s)
        a_in = max(0.0, p_in - ctx_len)
        a_len = p_in - a_in
        if a_len > 0.2:
            ctx += 1
            out_tl.append({"angle": "A", "source_in": round(a_in, 3),
                           "source_out": round(p_in, 3), "id": "cut_%03d" % ctx})
            basis_segs.append({"id": "cut_%03d" % ctx, "angle": "A",
                               "proof_in": round(t, 3), "proof_out": round(t + a_len, 3),
                               "context_for": pid, "source_in": round(a_in, 3),
                               "source_out": round(p_in, 3)})
            t += a_len
        dur = float(s["source_out"]) - float(s["source_in"])
        out_tl.append({"angle": "B", "source_in": s["source_in"],
                       "source_out": s["source_out"], "id": pid})
        basis_segs.append({"id": pid, "angle": "B", "proof_in": round(t, 3),
                           "proof_out": round(t + dur, 3),
                           "source_in": s["source_in"], "source_out": s["source_out"]})
        t += dur

    pre_grades = {name: curve for name in man["sources"]["b_reel"]}
    if rec_a:
        pre_grades[man["sources"]["a_reel"]] = rec_a["solved_pre_grade"]
    short = {"manifest_version": man.get("manifest_version", 1),
             "job_id": man["job_id"],
             "template": man.get("template"),
             "sources": man["sources"],
             "timeline": out_tl,
             "profiles": man["profiles"],
             "pre_grades": pre_grades}
    mp = os.path.join(a.out_dir, "cammatch_proof_manifest.yaml")
    bp = os.path.join(a.out_dir, "cammatch_proof_basis.json")
    yaml.safe_dump(short, open(mp, "w"), sort_keys=False)
    json.dump({"job_id": short["job_id"], "segments": basis_segs,
               "camera_match_record": os.path.abspath(a.match_b or a.match_record),
               "camera_match_a_record": os.path.abspath(a.match_a) if a.match_a else None,
               "curve_sha256": rec.get("final_gains"),
               "total_s": round(t, 3)}, open(bp, "w"), indent=1)
    print("manifest %s" % mp)
    print("basis    %s" % bp)
    print("timeline %.3f s: %s" % (t, ["%s/%s" % (s["angle"], s["id"]) for s in out_tl]))
    print("B inserts: %s" % [s["id"] for s in basis_segs])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
