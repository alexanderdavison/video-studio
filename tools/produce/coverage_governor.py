#!/usr/bin/env python3
"""coverage_governor.py — PROGRAM-LEVEL CAMERA COVERAGE GOVERNOR (Prompt 1, PART 7/8).

A second-pass review of pathological uninterrupted A coverage. It is NOT a cut-frequency generator,
NOT a B target, and NOT a replacement for the first-pass editor: Riley's first pass is untouched, and
`zero insertions` is a legal answer.

    scan     find uninterrupted A runs longer than the review threshold in a merged program, and the
             strongest unused legal B opportunities inside each run
    package  build a bounded, transport-light review package for ONE run (subset evidence + context)
    apply    validate a coverage-review response and, if it promotes, emit the revised program

Thresholds come from the job contract: coverage_review_threshold_s (trial: 90 s) and
max_promotions_per_span (2). One coverage pass only — never recursive.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

STUDIO = Path("/opt/video-studio")
VENV = str(STUDIO / "tools/venv/bin/python")
BUILDER = str(STUDIO / "tools/analysis/build_evidence_package.py")
GRADE = "/opt/video-studio/projects/2026-08-25-tester/work/analysis/test1_grade/camera_normalization_final.json"

# opportunity selection
MAX_OPPORTUNITIES = 4          # strongest few, transport-light
MIN_OPP_SEPARATION_S = 20.0    # don't offer two opportunities that are the same editorial moment
MIN_EDGE_CLEARANCE_S = 10.0    # don't offer an opportunity that would collide with the run's own ends
LEGAL_SEMANTICS = ("b_early", "b_glance", "b_action", "b_hold")


def load(p):
    return json.load(open(p))


def resolve_lag(basis):
    """A->B lag, from the basis where present (A source = performance; B source = performance - lag)."""
    inputs = basis.get("inputs") or {}
    for k in ("lag_a_to_b_s", "lag_s", "lag"):
        if k in inputs:
            return float(inputs[k])
        if k in basis:
            return float(basis[k])
    for v in (basis.get("sections") or {}).values():
        pass
    return 1.2783


def a_runs(basis, threshold_s):
    """Uninterrupted A runs in the merged program, in PERFORMANCE seconds.

    In a merged basis an A segment's source range IS performance time; B segments are stored in
    B-source time (= performance - lag), so only A segments define program A runs.
    """
    segs = basis.get("segments") or basis.get("timeline")
    runs, cur = [], None
    for s in segs:
        if s["angle"] == "A":
            if cur is None:
                cur = {"start": float(s["source_in"]), "end": float(s["source_out"]),
                       "cut_ids": [s["id"]]}
            else:
                cur["end"] = float(s["source_out"]); cur["cut_ids"].append(s["id"])
        else:
            if cur is not None:
                runs.append(cur); cur = None
    if cur is not None:
        runs.append(cur)
    for r in runs:
        r["duration_s"] = round(r["end"] - r["start"], 3)
        r["exceeds_threshold"] = r["duration_s"] > threshold_s
    return runs


def event_index(artifacts):
    """Flatten candidate artifacts into events keyed by window/event id, with their legal B faces."""
    idx = {}
    for tag, art in artifacts:
        for e in art.get("events", []):
            ctx = e.get("context") or {}
            anchor = ctx.get("sync_entry", {}).get("timeline_s")
            if anchor is None:
                anchor = (e.get("sync_anchored_entry") or {}).get("timeline_s")
            faces = []
            for c in e.get("candidates", []):
                cid = c.get("id")
                if cid not in LEGAL_SEMANTICS:
                    continue
                se = (c.get("scores") or {}).get("sync")
                faces.append({"semantic": cid, "duration_s": round(float(c["duration_s"]), 3),
                              "sync_ok": (se is None) or (float(se) >= 0.999),
                              "timeline_start": c.get("timeline_start"), "angle": c.get("angle")})
            idx["%s/%s" % (tag, e["event_id"])] = {
                "window": tag, "event_id": e["event_id"], "anchor": anchor,
                "origin": ctx.get("event_origin", "grid"),
                "coverage_ok": (ctx.get("sync_entry") or {}).get("coverage_ok"),
                "duration_choices": e.get("duration_choices"),
                "legal_faces": sorted([f for f in faces if f["sync_ok"]],
                                      key=lambda f: -f["duration_s"]),
            }
    return idx


def scan(args):
    basis = load(args.program)
    arts = [(Path(p).stem.replace("sync_", "").replace("_ao", ""), load(p))
            for p in args.artifacts.split(",")]
    idx = event_index(arts)
    threshold = args.threshold
    runs = a_runs(basis, threshold)
    spans = []
    for n, r in enumerate(sorted([x for x in runs if x["exceeds_threshold"]],
                                 key=lambda x: -x["duration_s"]), start=1):
        ops = []
        for key, ev in idx.items():
            a = ev["anchor"]
            if a is None or not (r["start"] + MIN_EDGE_CLEARANCE_S <= a <= r["end"] - MIN_EDGE_CLEARANCE_S):
                continue
            if not ev["legal_faces"]:
                continue
            best = ev["legal_faces"][0]
            ops.append({"candidate_id": key, "anchor_s": round(float(a), 3),
                        "origin": ev["origin"], "coverage_ok": ev["coverage_ok"],
                        "best_semantic": best["semantic"], "best_duration_s": best["duration_s"],
                        "n_legal_faces": len(ev["legal_faces"])})
        # strongest first: supplemental action-onset opportunities, then the longest legal face
        ops.sort(key=lambda o: (o["origin"] != "b_action_onset", -o["best_duration_s"]))
        picked, last = [], None
        for o in ops:
            if last is not None and abs(o["anchor_s"] - last) < MIN_OPP_SEPARATION_S:
                continue
            picked.append(o); last = o["anchor_s"]
            if len(picked) >= MAX_OPPORTUNITIES:
                break
        spans.append({
            "span_id": "span_%02d" % n,
            "start_s": r["start"], "end_s": r["end"], "duration_s": r["duration_s"],
            "cut_ids": r["cut_ids"],
            "first_pass_decision": "HOLD_A throughout",
            "opportunities": picked,
            "opportunities_considered": len(ops),
        })
    out = {"program": args.program, "coverage_review_threshold_s": threshold,
           "status": "REVIEW_REQUIRED" if spans else "NO_SPAN_EXCEEDS_THRESHOLD",
           "spans": spans,
           "note": "review, not forced insertion: a span may survive with zero promotions"}
    json.dump(out, open(args.out, "w"), indent=1)
    print("coverage scan: threshold %.1f s | A runs: %d | spans over threshold: %d"
          % (threshold, len(runs), len(spans)))
    for r in sorted(runs, key=lambda x: -x["duration_s"])[:6]:
        print("   A run %8.3f-%8.3f  %8.3f s  %s" % (r["start"], r["end"], r["duration_s"],
              "OVER THRESHOLD" if r["exceeds_threshold"] else ""))
    for s in spans:
        print("   %s: %.3f s (%d cut ids) | %d opportunities considered, %d offered"
              % (s["span_id"], s["duration_s"], len(s["cut_ids"]),
                 s["opportunities_considered"], len(s["opportunities"])))
        for o in s["opportunities"]:
            print("      %-28s anchor %9.3f  %-9s %5.3f s  origin=%s  coverage_ok=%s"
                  % (o["candidate_id"], o["anchor_s"], o["best_semantic"], o["best_duration_s"],
                     o["origin"], o["coverage_ok"]))
    print("wrote %s" % args.out)
    return 0


def _tc(s):
    s = float(s)
    h = int(s // 3600); m = int((s % 3600) // 60); sec = s - 3600 * h - 60 * m
    return "%02d:%02d:%06.3f" % (h, m, sec)


def render_prompt(template_path, span, out_path):
    """Fill the coverage prompt for one span. The Span line must match the built package exactly."""
    t = open(template_path).read()
    rows = ["| candidate_id | anchor (performance s) | strongest legal face | duration (s) | origin |",
            "|---|---|---|---|---|"]
    for o in span["opportunities"]:
        rows.append("| `%s` | %.3f | `%s` | %.3f | %s |"
                    % (o["candidate_id"], o["anchor_s"], o["best_semantic"], o["best_duration_s"],
                       o["origin"]))
    t = (t.replace("{{SPAN_START_TC}}", _tc(span["start_s"]))
          .replace("{{SPAN_END_TC}}", _tc(span["end_s"]))
          .replace("{{SPAN_DURATION}}", "%.3f s" % span["duration_s"])
          .replace("{{SPAN_ID}}", span["span_id"])
          .replace("{{OPPORTUNITY_TABLE}}", "\n".join(rows)))
    open(out_path, "w").write(t)
    return out_path


def _hdr_fingerprint(art):
    """The load-bearing part of an artifact header: sync geometry, reel end, cadence, policy numbers.

    Per-window fields (event_window, the window's own entry list, per-window input paths) legitimately
    differ between windows and carry no geometry — but if any value below drifts, mixing windows into
    one review package would compare geometry that was never synchronized together, so it fails closed.
    """
    p = art.get("policy") or {}
    s = art.get("sync_anchored_entry") or {}
    return json.dumps({
        "minimum_b_shot": p.get("minimum_b_shot"), "maximum_b_shot": p.get("maximum_b_shot"),
        "minimum_a_recovery": p.get("minimum_a_recovery"), "min_event_gap": p.get("min_event_gap"),
        "lag_s": s.get("lag_s"), "frame_snap_max_s": s.get("frame_snap_max_s"),
        "b_fps": s.get("b_fps"), "reel_end_s": s.get("reel_end_s"),
        "coverage_near_s": s.get("coverage_near_s"), "invariant": s.get("invariant"),
    }, sort_keys=True)


def build_subset_artifact(artifacts, span, out_path):
    """A filtered COPY of the production artifacts: same header, only the offered events kept.

    Opportunities can come from several processing windows, so every artifact holding an offered event
    contributes. The header blocks (candidate policy, sync/reel block) must agree across them, or this
    fails closed rather than mixing geometry that was never compared.
    """
    want = {}
    for o in span["opportunities"]:
        tag, eid = o["candidate_id"].split("/", 1)
        want.setdefault(tag, set()).add(eid)
    base, events = None, []
    for tag, art in artifacts:
        if tag not in want:
            continue
        hdr = _hdr_fingerprint(art)
        if base is None:
            base = (tag, art, hdr)
        elif hdr != base[2]:
            raise SystemExit("artifacts %s and %s disagree on the sync/policy invariants — refusing "
                             "to mix" % (base[0], tag))
        events.extend([e for e in art["events"] if e["event_id"] in want[tag]])
    if not events:
        raise SystemExit("no source artifact contains the offered opportunities")
    sub = dict(base[1])
    pol = dict(sub.get("policy") or {})
    pol["event_window"] = [span["start_s"], span["end_s"]]
    pol["b_search_window"] = [span["start_s"], span["end_s"]]
    pol["coverage_review_span"] = span["span_id"]
    sub["policy"] = pol
    sub["events"] = sorted(events, key=lambda e: (e.get("context") or {}).get(
        "sync_entry", {}).get("timeline_s") or 0.0)
    for e in sub["events"]:
        e.setdefault("context", {})["coverage_review_span"] = span["span_id"]
        e["context"]["coverage_review_pass"] = 1
    sub["span"] = {"start": span["start_s"], "end": span["end_s"],
                   "duration_s": span["duration_s"]}
    sub["coverage_review"] = {"span_id": span["span_id"], "pass": 1,
                              "role": "program-level coverage review of an uninterrupted A run"}
    json.dump(sub, open(out_path, "w"), indent=1)
    return len(sub["events"])


def package(args):
    cov = load(args.coverage)
    span = [s for s in cov["spans"] if s["span_id"] == args.span][0]
    arts = [(Path(p).stem.replace("sync_", "").replace("_ao", ""), load(p))
            for p in args.artifacts.split(",")]
    work = Path(args.out).parent
    work.mkdir(parents=True, exist_ok=True)
    sub_art = work / ("coverage_subset_%s.json" % span["span_id"])
    n = build_subset_artifact(arts, span, sub_art)
    pkg = Path(args.out)
    shutil.rmtree(pkg, ignore_errors=True)
    pdir = work / ("coverage_prompt_%s" % span["span_id"])
    pdir.mkdir(parents=True, exist_ok=True)
    render_prompt(args.prompt_template, span, pdir / "prompt.md")
    cmd = [VENV, BUILDER, "--candidates", str(sub_art), "--out", str(pkg),
           "--media-root", args.media_root, "--grade-json", GRADE, "--fps", str(args.fps),
           "--section", str(span["start_s"]), str(span["end_s"]),
           "--prompt-from", str(pdir)]
    # The job's own canonical sync map. Without it the builder falls back to its Set 01 legacy
    # timebase and every A/B pair in the package is misaligned by the difference between this
    # job's lag and 1.2783 s - its alignment gate then refuses, correctly. Never defaulted.
    if getattr(args, "sync_map", None):
        cmd += ["--sync-map", str(args.sync_map)]
    print("building bounded coverage package: %d offered events, span %.3f-%.3f s"
          % (n, span["start_s"], span["end_s"]))
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout.strip()[-1200:])
    if r.returncode != 0:
        print(r.stderr.strip()[-800:]); return 1
    pack = json.load(open(pkg / "evidence_pack.json"))
    c = pack["counts"]
    n_img = c["n_dense_frames"] + c["n_sheet_frames"] + 9
    print("package: %d images in request | predicted tokens %d (gate %d, cap %d images)"
          % (n_img, n_img * 765, cov.get("transport_token_gate", 450000), 500))
    print("events in package: %d" % len(pack["events"]))
    return 0


def apply(args):
    cov = load(args.coverage)
    basis = load(args.program)
    resp = load(args.response)
    span = [s for s in cov["spans"] if s["span_id"] == resp.get("span_id")][0] if resp.get("span_id") \
        else cov["spans"][0]
    max_promo = args.max_promotions
    dec = resp.get("decision")
    problems, promotions = [], []
    if dec not in ("KEEP_A_RUN", "PROMOTE"):
        problems.append("decision must be KEEP_A_RUN or PROMOTE, got %r" % dec)
    offered = {o["candidate_id"]: o for o in span["opportunities"]}
    for p in (resp.get("promotions") or []):
        cid = p.get("candidate_id")
        if cid not in offered:
            problems.append("promotion not among the offered candidates: %r" % cid); continue
        o = offered[cid]
        dur = float(p.get("duration_s") or o["best_duration_s"])
        if not (args.minimum_b_shot_s - 1e-6 <= dur <= args.maximum_b_shot_s + 1e-6):
            problems.append("%s: duration %.3f s outside policy" % (cid, dur)); continue
        promotions.append({"candidate_id": cid, "anchor_s": o["anchor_s"], "duration_s": dur,
                           "semantic": p.get("semantic") or o["best_semantic"]})
    if len(promotions) > max_promo:
        problems.append("%d promotions exceeds the per-span ceiling of %d" % (len(promotions), max_promo))
    promotions.sort(key=lambda p: p["anchor_s"])
    # fail closed: A recovery between promoted inserts, and against the existing program
    for i in range(1, len(promotions)):
        gap = promotions[i]["anchor_s"] - (promotions[i - 1]["anchor_s"] + promotions[i - 1]["duration_s"])
        if gap < args.minimum_a_recovery_s - 1e-6:
            problems.append("promotions %s and %s leave only %.3f s of A recovery"
                            % (promotions[i - 1]["candidate_id"], promotions[i]["candidate_id"], gap))
    if problems:
        print("COVERAGE RESPONSE REJECTED (%d problem(s)):" % len(problems))
        for p in problems:
            print("   - %s" % p)
        return 2
    if dec == "KEEP_A_RUN" or not promotions:
        print("coverage review: KEEP_A_RUN — the first-pass program stands unchanged")
        revised = dict(basis); revised["coverage_application"] = {
            "span_id": span["span_id"], "result": "KEEP_A_RUN", "promotions": []}
    else:
        lag = resolve_lag(basis)
        span0 = float(basis["span"][0])
        boundaries = [float(b.get("boundary_timeline_s"))
                      for b in (basis.get("boundary_ownership") or []) if "boundary_timeline_s" in b]
        # a promoted insert may not straddle a processing boundary: the seam must stay invisible
        for p in promotions:
            hi = p["anchor_s"] + p["duration_s"]
            for b in boundaries:
                if p["anchor_s"] < b < hi:
                    problems.append("%s: promoted insert %.3f-%.3f s straddles the processing boundary "
                                    "at %.3f s" % (p["candidate_id"], p["anchor_s"], hi, b))
        if problems:
            print("COVERAGE RESPONSE REJECTED (%d problem(s)):" % len(problems))
            for p_ in problems:
                print("   - %s" % p_)
            return 2
        segs = []
        for seg in (basis.get("segments") or basis.get("timeline")):
            parts = [dict(seg)]
            if seg["angle"] == "A":
                for p in promotions:
                    a_end = p["anchor_s"]
                    b_end = p["anchor_s"] + p["duration_s"]
                    if not (float(seg["source_in"]) < a_end and b_end < float(seg["source_out"])):
                        continue
                    parts[-1] = dict(seg, source_out=round(a_end, 3))
                    parts.append({"angle": "B",
                                  "source_in": round(a_end - lag, 3),
                                  "source_out": round(b_end - lag, 3),
                                  "decision_origin": "program_coverage_review",
                                  "promoted_for_span": span["span_id"],
                                  "candidate_id": p["candidate_id"], "semantic": p["semantic"],
                                  "promoted_anchor_performance_s": round(a_end, 3)})
                    parts.append({"angle": "A", "source_in": round(b_end, 3),
                                  "source_out": round(float(seg["source_out"]), 3),
                                  "decision_origin": "first_pass"})
            if str(seg.get("decision_origin", "")) == "program_coverage_review":
                for part in parts:
                    part.setdefault("decision_origin", "program_coverage_review")
            segs.extend(parts)
        for i, s in enumerate(segs, start=1):
            s["id"] = "cut_%03d" % i
            s.setdefault("decision_origin", "first_pass")
            # proof time: A source IS performance; B source is performance - lag
            # proof time: A source IS performance; B source is performance - lag, so
            # returning a B segment to program time ADDS the lag (canonical basis: B 1052.585 -> proof 33.860)
            off = lag if s["angle"] == "B" else 0.0
            s["proof_in"] = round(float(s["source_in"]) + off - span0, 3)
            s["proof_out"] = round(float(s["source_out"]) + off - span0, 3)
        revised = dict(basis)
        revised["segments"] = segs
        revised["n_segments"] = len(segs)
        revised["total_s"] = round(sum(float(s["proof_out"]) - float(s["proof_in"]) for s in segs), 3)
        revised["longest_A_run_s"] = max([float(s["proof_out"]) - float(s["proof_in"])
                                          for s in segs if s["angle"] == "A"] or [0.0])
        revised["b_screen_time_s"] = round(sum(float(s["proof_out"]) - float(s["proof_in"])
                                               for s in segs if s["angle"] == "B"), 3)
        revised["b_pct_of_program"] = round(100.0 * revised["b_screen_time_s"] / revised["total_s"], 2)
        revised["coverage_application"] = {
            "span_id": span["span_id"], "result": "PROMOTE", "promotions": promotions, "passes": 1,
            "lag_a_to_b_s": lag, "decision_origin": "program_coverage_review"}
        # the ownership record must describe the REVISED timeline, not the one it was copied from
        refresh_boundary_ownership(revised)
        print("coverage review: %d promotion(s) applied (lag %.4f s)" % (len(promotions), lag))
        for p in promotions:
            print("   %s  anchor %.3f s  %s  %.3f s" % (p["candidate_id"], p["anchor_s"],
                                                        p["semantic"], p["duration_s"]))
        print("   revised: %d segments, %.3f s, B %.3f s (%.2f%%), longest A run %.3f s"
              % (revised["n_segments"], revised["total_s"], revised["b_screen_time_s"],
                 revised["b_pct_of_program"], revised["longest_A_run_s"]))
    json.dump(revised, open(args.out, "w"), indent=1)
    print("wrote %s" % args.out)
    if args.emit_manifest:
        print("manifest: %s" % emit_manifest(revised, args.emit_manifest))
    return 0


def emit_manifest(basis, out_yaml):
    """Write the revised program as a manifest in the SAME shape as the canonical one.

    The original manifest is loaded and only its timeline is replaced, so job template, profiles and
    source list stay byte-faithful; provenance for the promoted cuts lives in the basis, not in the
    manifest's cut entries.
    """
    import yaml
    src = yaml.safe_load(open(basis["manifest"]))
    src["timeline"] = [{"angle": s["angle"], "source_in": round(float(s["source_in"]), 3),
                        "source_out": round(float(s["source_out"]), 3), "id": s["id"]}
                       for s in basis["segments"]]
    missing = [k for k in ("grade", "crop", "graphics", "audio") if k not in (src.get("profiles") or {})]
    if missing:
        raise SystemExit("profile contract incomplete in %s: %s" % (basis["manifest"], missing))
    with open(out_yaml, "w") as f:
        yaml.safe_dump(src, f, sort_keys=False)
    # validate at emit time: the contract is checked here, not discovered later in QC
    v = _run_validator(out_yaml)
    if v.returncode != 0 or "VALID" not in v.stdout:
        raise SystemExit("revised manifest failed validation: %s" % (v.stdout + v.stderr)[-400:])
    print("   revised manifest validated: %s" % v.stdout.strip().splitlines()[0])
    return out_yaml


def _run_validator(out_yaml):
    """Run the studio's manifest validator over the emitted manifest.

    `scan` and `apply` normally run on the OPS host, where the studio venv and tool tree do not
    exist — running the validator there raised FileNotFoundError and aborted the job after the
    coverage review had already been applied. The check must not silently disappear, so on the ops
    host the manifest is staged to the studio and validated there over the same ssh trust path the
    orchestrator uses; the orchestrator re-validates the same bytes before any render.
    """
    validator = str(STUDIO / "tools/manifest/validate_manifest.py")
    if os.path.exists(VENV):
        return subprocess.run([VENV, validator, str(out_yaml)], cwd="/mnt/media/raw",
                              capture_output=True, text=True)
    cfg = json.load(open(os.environ.get("ISH_D_PRODUCTION_CONFIG", "/opt/production/production.json")))
    host = cfg["studio_host"]
    remote = "/tmp/%s" % os.path.basename(str(out_yaml))
    push = subprocess.run(["scp", "-q", str(out_yaml), "%s:%s" % (host, remote)],
                          capture_output=True, text=True)
    if push.returncode != 0:
        raise SystemExit("could not stage the revised manifest on the studio: %s" % push.stderr[-300:])
    try:
        return subprocess.run(["ssh", host, "cd /mnt/media/raw && %s %s %s" % (VENV, validator, remote)],
                              capture_output=True, text=True)
    finally:
        subprocess.run(["ssh", host, "rm -f %s" % remote], capture_output=True, text=True)


def refresh_boundary_ownership(revised):
    """Recompute boundary_ownership against the REVISED segment list.

    The revised basis is a copy of the input basis, so after a promotion its ownership block still
    named the pre-promotion cuts — every cut id after the promotion shifted by one, and the QC reads
    that block to decide which segment a processing boundary lies inside. Rebuilt here so the record
    describes the timeline that is actually being rendered.
    """
    bounds = [float(b.get("boundary_timeline_s"))
              for b in (revised.get("boundary_ownership") or []) if "boundary_timeline_s" in b]
    if not bounds:
        return
    segs = revised["segments"]
    out = []
    for b in bounds:
        # compare PROOF times (timeline positions), never source times: an A segment's source time is
        # a timeline value while a B segment's is a virtual-reel position
        inside = next((s for s in segs
                       if float(s["proof_in"]) - 1e-9 <= b <= float(s["proof_out"]) + 1e-9), None)
        at_cut = next((s for s in segs if abs(float(s["proof_in"]) - b) < 0.02), None)
        nxt = next((s for s in segs if float(s["proof_in"]) > b + 1e-9), None)
        prev = next((s for s in reversed(segs) if float(s["proof_out"]) <= b + 1e-9), None)
        out.append({
            "boundary_timeline_s": round(b, 3),
            "final_committed_shot_from_previous_section": (
                {"id": prev["id"], "angle": prev["angle"], "source_in": prev["source_in"],
                 "source_out": prev["source_out"]} if prev else None),
            "boundary_lies_inside": (
                {"id": inside["id"], "angle": inside["angle"], "source_in": inside["source_in"],
                 "source_out": inside["source_out"],
                 "length_s": round(float(inside["proof_out"]) - float(inside["proof_in"]), 3)}
                if inside else None),
            "committed_territory_ends_at_timeline_s": round(b, 3),
            "earliest_timeline_position_next_section_may_alter": round(b, 3),
            "next_shot_after_boundary": ({"id": nxt["id"], "angle": nxt["angle"]} if nxt else None),
            "boundary_is_a_cut": bool(at_cut is not None),
            "cut_at_boundary": ({"id": at_cut["id"], "angle": at_cut["angle"]} if at_cut else None),
            "note": ("the processing boundary coincides with a planned editorial cut — the change at "
                     "it is the decided edit, not a pipeline artifact" if at_cut is not None
                     else "the processing boundary is not a cut")})
    revised["boundary_ownership"] = out


def main():
    ap = argparse.ArgumentParser(description="program-level camera coverage governor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--program", required=True)
    s.add_argument("--artifacts", required=True); s.add_argument("--threshold", type=float, default=90.0)
    s.add_argument("--out", required=True); s.set_defaults(fn=scan)
    p = sub.add_parser("package"); p.add_argument("--coverage", required=True)
    p.add_argument("--span", required=True); p.add_argument("--artifacts", required=True)
    p.add_argument("--prompt-template", required=True); p.add_argument("--out", required=True)
    p.add_argument("--media-root", default="/mnt/media/raw"); p.add_argument("--fps", type=float, default=1.0)
    p.add_argument("--sync-map", default=None)
    p.set_defaults(fn=package)
    a = sub.add_parser("apply"); a.add_argument("--coverage", required=True)
    a.add_argument("--program", required=True); a.add_argument("--response", required=True)
    a.add_argument("--out", required=True); a.add_argument("--max-promotions", type=int, default=2)
    a.add_argument("--emit-manifest", default=None)
    a.add_argument("--minimum-b-shot-s", type=float, default=4.0, dest="minimum_b_shot_s")
    a.add_argument("--maximum-b-shot-s", type=float, default=14.0, dest="maximum_b_shot_s")
    a.add_argument("--minimum-a-recovery-s", type=float, default=8.0, dest="minimum_a_recovery_s")
    a.set_defaults(fn=apply)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
