#!/usr/bin/env python3
"""template_engine.py — Phase 5 template engine (ISH D DJ-video template engine).

Turns per-mix variables + a timeline into a VALIDATED manifest that both the
Phase 3 proxy and the Phase 4 final renderer consume. The template is the
complete editorial policy (definition file); the engine resolves its profile
names, applies editorial-policy checks to the timeline, writes the manifest,
and runs the Phase 2 validator as the final gate.

Template = policy. Engine = generator + policy checker. Never redesigns
creative params — names resolve against the registry/definitions.

Usage:
  python3 template_engine.py --template club_dispatch_standard_v1 \
      --mix mix.yaml [--timeline timeline.yaml] [--out job.yaml] \
      [--media-root /path] [--strict] [--json]

  mix.yaml      per-mix variables (the only input Ish supplies):
                series, title, subtitle, artist, date, a_camera, b_camera[],
                edit_energy, b_camera_density, intro_duration, outro_duration
  timeline.yaml ordered cuts: [{angle: A|B, source_in, source_out,
                confidence?: low|high}] — cut ids are assigned cut_001..
                (Riley/RCC supplies footage-specific decisions here)

Policy checks (warnings by default; --strict turns violations into rc 1):
  - B density within the template's preset range
  - each B cut within [minimum_b_shot, maximum_b_shot]
  - A recovery after a B cut >= minimum_a_recovery
  - low-confidence B cut -> stay_on_a warning (template low_confidence_action)

Exit: 0 = manifest written + validator passed; 1 = invalid/strict violation;
2 = error (missing template/definition, unreadable inputs).
"""

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFS = os.path.join(HERE, "definitions")
MANIFEST_DIR = os.path.normpath(os.path.join(HERE, "..", "manifest"))
VALIDATOR = os.path.join(MANIFEST_DIR, "validate_manifest.py")
REGISTRY = os.path.join(MANIFEST_DIR, "profile_registry.json")
VENV_PY = "/opt/video-studio/tools/venv/bin/python"


def log(msg):
    print(msg, flush=True)


def fail(msg, rc=2):
    log("ERROR: %s" % msg)
    sys.exit(rc)


def load_yaml(path):
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


def slugify(text):
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip()).strip("_")
    return s or "job"


def resolve_template(template_name):
    """Return (definition dict, error). Definition file wins; registry fallback
    for density metadata only (policy checks need the full definition)."""
    path = os.path.join(DEFS, template_name + ".yaml")
    if not os.path.exists(path):
        # Fall back: registry templates metadata (density + pacing only).
        try:
            with open(REGISTRY) as f:
                reg = json.load(f)
            t = reg["templates"].get(template_name)
            if t is None:
                return None, "unknown template %s (no definition file, no registry entry)" % template_name
            return {"template": template_name, "preset": t["preset"],
                    "b_camera_density_pct": t["b_camera_density_pct"],
                    "pacing": t["pacing"],
                    "registry_only": True}, None
        except Exception as e:
            return None, "cannot read registry: %s" % e
    return load_yaml(path), None


def density_bounds(pct_str):
    """'20-25' -> (20, 25); '0' -> (0, 0)."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?\s*", pct_str or "")
    if not m:
        return None
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    return (lo, hi)


def parse_timeline(timeline_path):
    data = load_yaml(timeline_path)
    cuts = data.get("timeline") if isinstance(data, dict) else data
    if not isinstance(cuts, list) or not cuts:
        return None, "timeline must be a list of cuts"
    out = []
    for i, c in enumerate(cuts, 1):
        if not isinstance(c, dict) or "angle" not in c or "source_in" not in c or "source_out" not in c:
            return None, "cut #%d missing angle/source_in/source_out" % i
        cut = {
            "id": "cut_%03d" % i,
            "angle": c["angle"],
            "source_in": float(c["source_in"]),
            "source_out": float(c["source_out"]),
        }
        if c.get("confidence"):
            cut["confidence"] = c["confidence"]
        out.append(cut)
    return out, None


def ffprobe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


def target_density(template, mix):
    """B-camera density as a FRACTION (0..1), nudged by mix.b_camera_density."""
    bounds = density_bounds(template.get("b_camera_density_pct"))
    if not bounds:
        return 0.22
    lo, hi = bounds
    nudge = {"light": lo, "moderate": (lo + hi) / 2.0, "heavy": hi}.get(
        str(mix.get("b_camera_density") or "moderate").strip().lower())
    if nudge is None:
        nudge = (lo + hi) / 2.0
    return nudge / 100.0


def build_sections(mix, spine_start, spine_end, base_density):
    """Split the spine into (start, end, density_fraction) windows.

    mix.sections (optional): [{start_s, end_s, density_pct}] overrides the
    cadence inside a window — Phase 6 'section overrides'.
    """
    out = []
    cur = spine_start
    for s in mix.get("sections") or []:
        try:
            ss = float(s.get("start_s"))
            se = float(s.get("end_s"))
            dp = float(s.get("density_pct"))
        except (TypeError, ValueError):
            continue
        if ss < cur:
            ss = cur
        if ss >= se or se <= spine_start or ss >= spine_end:
            continue
        if ss > cur:
            out.append((cur, min(ss, spine_end), base_density))
        out.append((max(ss, spine_start), min(se, spine_end), dp / 100.0))
        cur = max(cur, se)
    if cur < spine_end:
        out.append((cur, spine_end, base_density))
    return [(a, b, d) for (a, b, d) in out if b - a > 0.5]


def snap_to_beats(bound, beats, tolerance=0.5):
    """Snap a boundary to the nearest beat within tolerance (quantize_edl rule)."""
    if not beats:
        return bound
    nearest = min(beats, key=lambda b: abs(b - bound))
    if abs(nearest - bound) <= tolerance:
        return nearest
    return bound


def choose_b_window(duration, reel_dur, profile, used_ranges, min_start=0.0,
                    max_start=None, center=0.0):
    """Pick the best UNUSED, SYNCED B source window by mean action score.

    profile: list of (start, end, score) segments on the virtual reel.
    used_ranges: list of (start, end) already-claimed windows.
    min_start: window START must be >= this (monotonic source order + the
    previous B window's end).
    max_start: window START must be <= this — enforces A recovery: the B
    window must end before the weave resumes (bs_out <= be - min_recovery).
    center: preferred window start (the weave position, bs - duration) —
    the search is BIASED toward it so the B reel stays synced to the
    performance; the action score nudges WITHIN the sync-safe range.
    Returns (start, end) or None (fall back to cursor placement).
    """
    if not profile or reel_dur <= 0:
        return None

    def overlap(a1, a2, b1, b2):
        return max(0.0, min(a2, b2) - max(a1, b1))

    def used(t, end):
        return any(overlap(t, end, u1, u2) > 0.01 for (u1, u2) in used_ranges)

    def score(t, end):
        tot = 0.0
        seg_len = end - t
        if seg_len <= 0:
            return 0.0
        for (s1, s2, sc) in profile:
            tot += sc * overlap(t, end, s1, s2) / seg_len
        return tot

    best = None
    best_score = -1.0
    hi = max_start if max_start is not None else reel_dur - duration
    span = max(hi - min_start, 1.0)
    t = min_start
    step = 0.5
    while t <= hi + 1e-6:
        end = t + duration
        if end <= reel_dur + 1e-6 and not used(t, end):
            sc = score(t, end) - 0.25 * abs(t - center) / span
            if sc > best_score:
                best_score = sc
                best = (t, end)
        t += step
    return best


def propose_timeline(template, mix, media_root, beats=None, b_beats=None, b_profile=None,
                     flex="standard"):
    """Phase 6 first-cut: generate the proposed timeline from policy.

    A spine = full A reel trimmed by intro/outro. B inserts placed at the
    template's target B density, each 4-14s, minimum A recovery after every
    B, section overrides honored. Returns (cuts, errors). This is the
    PROPOSE half of the operator loop (PROPOSE -> Ish OK -> EXECUTE):
    write the result with --out-timeline for review, then re-run the engine
    with that timeline to lock it.
    """
    a_name = mix["a_camera"]
    a_path = a_name if os.path.isabs(a_name) else os.path.join(media_root, a_name)
    a_dur = ffprobe_duration(a_path)
    if a_dur is None:
        return None, ["cannot probe a_camera %s" % a_name]
    try:
        intro = float(mix.get("intro_duration") or 7.0)
        outro = float(mix.get("outro_duration") or 7.0)
    except (TypeError, ValueError):
        intro, outro = 7.0, 7.0
    if intro + outro >= a_dur - 1.0:
        return None, ["intro+outro (%.1f+%.1f) leaves no spine in %s (%.1fs)"
                      % (intro, outro, a_name, a_dur)]

    edit = template.get("editing", {})
    min_b = float(edit.get("minimum_b_shot") or 4.0)
    max_b = float(edit.get("maximum_b_shot") or 14.0)
    min_rec = float(edit.get("minimum_a_recovery") or 8.0)
    # --flex presets: clamp the B-shot window and optionally allow upward
    # density flex beyond the template target (standard == current behavior).
    if flex == "tight":
        min_b, max_b = max(min_b, 6.0), min(max_b, 10.0)
    nudge_bonus = 0.06 if flex == "loose" else 0.0

    b_names = mix.get("b_camera") or []
    spine_end = a_dur - outro
    if not b_names:
        return [{"id": "cut_001", "angle": "A",
                 "source_in": round(intro, 3), "source_out": round(spine_end, 3)}], []

    off = 0.0
    for name in b_names:
        path = name if os.path.isabs(name) else os.path.join(media_root, name)
        d = ffprobe_duration(path)
        if d is None:
            return None, ["cannot probe b_camera %s" % name]
        off += d
    b_reel_dur = off
    if b_reel_dur <= 0:
        return None, ["b_reel has zero usable duration"]

    base_density = min(target_density(template, mix) + nudge_bonus, 0.5)
    sections = build_sections(mix, intro, spine_end, base_density)
    b_shot = min(max(8.0, min_b), max_b)

    cuts = []
    last_b_src_end = None
    last_b_end = None
    open_at = intro
    used_b_ranges = []

    def add_a(si, so):
        if so - si < 0.5:
            return
        cuts.append({"id": "cut_%03d" % (len(cuts) + 1), "angle": "A",
                     "source_in": round(si, 3), "source_out": round(so, 3)})

    def add_b(si, so):
        cuts.append({"id": "cut_%03d" % (len(cuts) + 1), "angle": "B",
                     "source_in": round(si, 3), "source_out": round(so, 3)})

    for (ss, se, dens) in sections:
        if open_at < ss - 0.5:
            add_a(open_at, ss)
            open_at = ss
        if open_at >= se - 0.5:
            continue
        span = se - open_at
        n = max(0, int(round(span * dens / b_shot)))
        if n <= 0:
            continue
        period = span / n
        anchor = open_at  # fixed section anchor — open_at moves after each B
        for k in range(n):
            bs = anchor + (k + 0.5) * period
            if last_b_end is not None:
                bs = max(bs, last_b_end + min_rec)
            be = min(bs + b_shot, se)
            if beats:
                bs_s = snap_to_beats(bs, beats)
                be_s = snap_to_beats(be, beats)
                if last_b_end is not None and bs_s < last_b_end + min_rec - 1e-6:
                    bs_s = bs  # snapping would violate A recovery — keep unsnapped
                if be_s <= bs_s:
                    be_s = be
                bs, be = bs_s, be_s
            if be - bs < 0.5:
                break
            dur = be - bs
            lo = last_b_src_end if last_b_src_end is not None else 0.0
            hi = min(bs - min_rec, b_reel_dur - dur)
            if hi - lo < 0.5:
                continue  # no room for a synced, recovery-safe window — skip this insert
            if bs > open_at + 0.5:
                add_a(open_at, bs)
            center = min(max(bs - dur, lo), hi)  # window ending at the weave position
            window = choose_b_window(dur, b_reel_dur, b_profile, used_b_ranges,
                                     min_start=lo, max_start=hi, center=center)
            if window is None:
                bs_in = center
                bs_out = min(center + dur, b_reel_dur)
            else:
                bs_in, bs_out = window
            used_b_ranges.append((bs_in, bs_out))
            if b_beats:
                bs_in_s = snap_to_beats(bs_in, b_beats)
                bs_out_s = snap_to_beats(bs_out, b_beats)
                if bs_in_s < lo - 1e-6:
                    bs_in_s = bs_in  # snapping backward would break source order
                if bs_out_s > bs + dur - min_rec - 1e-6:
                    bs_out_s = bs_out  # snapping forward would break A recovery
                if bs_out_s - bs_in_s >= 0.5:
                    bs_in, bs_out = bs_in_s, bs_out_s
            last_b_src_end = bs_out
            add_b(bs_in, bs_out)
            last_b_end = be
            open_at = be
    if open_at < spine_end - 0.5:
        add_a(open_at, spine_end)
    if not cuts:
        add_a(intro, spine_end)
    return cuts, []


def policy_checks(template, cuts, strict):
    """Return (violations list). Density + shot-length + A-recovery checks."""
    violations = []
    if not cuts:
        return violations
    total = sum(c["source_out"] - c["source_in"] for c in cuts)
    b_total = sum((c["source_out"] - c["source_in"]) for c in cuts if c["angle"] == "B")
    density = 100.0 * b_total / total if total else 0.0
    bounds = density_bounds(template.get("b_camera_density_pct"))
    if bounds:
        lo, hi = bounds
        if density < lo - 0.01 or density > hi + 0.01:
            violations.append("B density %.1f%% outside template range %s-%s%%"
                              % (density, lo, hi))
    # shot lengths
    min_b = template.get("editing", {}).get("minimum_b_shot")
    max_b = template.get("editing", {}).get("maximum_b_shot")
    for c in cuts:
        if c["angle"] != "B":
            continue
        d = c["source_out"] - c["source_in"]
        if min_b is not None and d < min_b - 0.01:
            violations.append("%s: B shot %.1fs shorter than template minimum %ss"
                              % (c["id"], d, min_b))
        if max_b is not None and d > max_b + 0.01:
            violations.append("%s: B shot %.1fs longer than template maximum %ss"
                              % (c["id"], d, max_b))
        if c.get("confidence") == "low" and template.get("editing", {}).get("low_confidence_action") == "stay_on_a":
            violations.append("%s: low-confidence B cut — template says stay on A"
                              % c["id"])
    # A recovery after B exit
    min_rec = template.get("editing", {}).get("minimum_a_recovery")
    if min_rec is not None:
        prev_b_end = None
        for c in cuts:
            if c["angle"] == "B":
                prev_b_end = c["source_out"]
            elif prev_b_end is not None:
                gap = c["source_in"] - prev_b_end
                if gap < min_rec - 0.01:
                    violations.append("%s: A recovery %.1fs after B shorter than template minimum %ss"
                                      % (c["id"], gap, min_rec))
                prev_b_end = None
    return violations


def build_manifest(template, mix, cuts, out_path):
    """Construct the manifest dict per the Phase 2 schema."""
    sources = {"a_reel": mix["a_camera"]}
    if mix.get("b_camera"):
        sources["b_reel"] = list(mix["b_camera"])
    profiles = {"grade": template["profiles"]["grade"],
                "crop": template["profiles"]["crop"],
                "graphics": template["profiles"]["graphics"],
                "audio": template["profiles"]["audio"]}
    return {
        "manifest_version": 1,
        "job_id": slugify(mix.get("title") or mix.get("series") or "job"),
        "template": template["template"],
        "sources": sources,
        "timeline": cuts,
        "profiles": profiles,
    }


def validate_manifest(manifest_path, media_root):
    cmd = [VENV_PY, VALIDATOR, "--media-root", media_root, "--json", manifest_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        return True, ""
    try:
        parsed = json.loads(r.stdout) if r.stdout.strip() else {}
        msg = parsed.get("error") or parsed.get("detail") or r.stdout.strip()
    except Exception:
        msg = r.stdout.strip() or r.stderr.strip() or "validator failed"
    return False, msg


def main():
    ap = argparse.ArgumentParser(description="ISH D template engine")
    ap.add_argument("--template", required=True)
    ap.add_argument("--mix", required=True)
    ap.add_argument("--timeline", required=False,
                    help="timeline YAML (required unless --propose)")
    ap.add_argument("--propose", action="store_true",
                    help="Phase 6: generate the first-cut timeline from mix + template policy instead of reading --timeline")
    ap.add_argument("--out-timeline", default=None,
                    help="with --propose, also write the proposed timeline YAML here for review")
    ap.add_argument("--beats", default=None,
                    help="A-reel beats.json ({\"bpm\":..., \"beats\":[...]}) — snap B weave boundaries to phrase")
    ap.add_argument("--b-beats", default=None,
                    help="B-reel beats.json — snap B source windows to phrase")
    ap.add_argument("--b-profile", default=None,
                    help="B-reel action profile JSON ({\"segments\":[{\"start\",\"end\",\"score\"}]}) — prefer high-action B windows")
    ap.add_argument("--out", required=True)
    ap.add_argument("--media-root", required=True)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--flex", choices=["tight", "standard", "loose"], default="standard",
                    help="cut flexibility preset: tight (B 6-10s, no upward density nudge), "
                         "standard (template clamps + mix nudge, current behavior), "
                         "loose (template clamps + up to +6% upward density nudge)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    template, err = resolve_template(args.template)
    if err:
        fail(err, rc=2)
    mix = load_yaml(args.mix)
    if not isinstance(mix, dict) or not mix.get("a_camera"):
        fail("mix.yaml must contain a_camera (and optionally b_camera, title, series)", rc=1)

    if args.propose:
        beats = None
        b_beats = None
        b_profile = None
        if args.beats:
            d = json.load(open(args.beats))
            beats = d.get("beats") or d.get("beat_times") or []
        if args.b_beats:
            d = json.load(open(args.b_beats))
            b_beats = d.get("beats") or d.get("beat_times") or []
        if args.b_profile:
            d = json.load(open(args.b_profile))
            b_profile = [(float(s["start"]), float(s["end"]), float(s.get("score", 0)))
                         for s in d.get("segments", [])]
        cuts, perr = propose_timeline(template, mix, args.media_root,
                                      beats=beats, b_beats=b_beats, b_profile=b_profile,
                                      flex=args.flex)
        if perr:
            fail("propose failed: %s" % "; ".join(perr), rc=1)
        if args.out_timeline:
            import yaml as _yaml
            with open(args.out_timeline, "w") as f:
                _yaml.safe_dump({"timeline": cuts}, f, sort_keys=False)
            log("PROPOSED TIMELINE -> %s (review, then re-run without --propose)" % args.out_timeline)
    else:
        if not args.timeline:
            fail("--timeline required unless --propose", rc=2)
        cuts, err = parse_timeline(args.timeline)
        if err:
            fail(err, rc=1)

    violations = policy_checks(template, cuts, args.strict)

    man = build_manifest(template, mix, cuts, args.out)
    import yaml
    with open(args.out, "w") as f:
        yaml.safe_dump(man, f, sort_keys=False)

    ok, verr = validate_manifest(args.out, args.media_root)
    if not ok:
        fail("generated manifest failed validation: %s" % verr, rc=1)

    if violations and args.strict:
        for v in violations:
            log("POLICY VIOLATION: %s" % v)
        fail("%d strict policy violation(s) — manifest written but NOT approved" % len(violations), rc=1)

    if args.json:
        print(json.dumps({"ok": True, "manifest": args.out, "job_id": man["job_id"],
                          "template": args.template, "cuts": len(cuts),
                          "warnings": violations}))
    else:
        log("TEMPLATE OK: %s (template %s, %d cuts)" % (args.out, args.template, len(cuts)))
        for v in violations:
            log("WARNING: %s" % v)
    sys.exit(0)


if __name__ == "__main__":
    main()
