#!/usr/bin/env python3
"""exposure_analyze.py — measure a source reel's exposure profile and classify.

User directive 2026-08-27: the system must LOOK at footage and know whether to
lighten or darken it. This is the analyzer half of that feature.

Per vault canon (2026-08-26-tester-postmortem.md line 98): "B-cam LOG: match
histogram profile (dark%, p10, p90), not mean luma." Mean luma lies when a
frame has a big bright stage-light area and crushed shadows.

Phase 6 (2026-08-27, decision doc 2026-08-27-auto-exposure-analyzer.md): the
classification is RELATIVE, not absolute — the approved A-cam defines the
reference profile and every other source is lightened/darkened toward IT.
Two modes:

  1) Single-file:
       exposure_analyze.py <video> [--reference FILE] [--sample-every N]
                           [--spans a-b,c-d] [--json]
     With --reference, classify the target against the measured reference
     profile. Without it, classify against the built-in absolute TARGET band
     (standalone inspection only).

  2) Manifest pre-grade (the renderer integration path):
       exposure_analyze.py --manifest <job.yaml> --media-root DIR [--write]
     Measures the A-cam over its USED spans (the reference), then measures
     every other source (B files) over THEIR used spans, classifies each
     relative to A, and emits a corrective curves pre-grade per source.
     --write patches the manifest in place (adds/replaces `pre_grades:`).
     Both renderers apply pre_grades BEFORE the locked creative grade and
     record it in the QC postflight block.

Method: sample frames evenly across the reel (fps=1/sample_every), downscale
to 160x90 8-bit gray, and compute the true pixel histogram: dark%, blown%,
p10, p50, p90, mean. Seek-sampled: one quick keyframe seek + single-frame
decode per sample, so a 4K HEVC reel costs seconds, not a full-program decode.

Exit: 0 = measured + classified; 2 = error.
"""

import argparse
import json
import os
import subprocess
import sys

import numpy as np

# Absolute target exposure profile (8-bit luma). Tuned for night DJ footage:
# the DJI A-cam sits comfortably inside this band; GoPro B-cam sits far below.
# Phase 6: the RENDERER path uses relative classification against the A-cam;
# this band remains for standalone inspection only.
TARGET = {
    "dark_pct_max": 8.0,    # pixels < 24 luma
    "blown_pct_max": 2.0,   # pixels > 235 luma
    "p10_min": 0.10,        # 10th percentile >= ~26/255
    "p90_range": (0.55, 0.85),  # 90th percentile in 140-217/255
    "mean_range": (0.38, 0.62),
}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def run_bin(cmd):
    return subprocess.run(cmd, capture_output=True)


def sample_luma(path, sample_every=30, width=160, height=90, spans=None):
    """Return np.ndarray of 8-bit luma samples.

    Seek-sampled: one quick keyframe seek + single-frame decode per sample,
    so a 4K HEVC reel costs seconds, not a full-program decode.

    spans: optional list of (start, end) source-seconds to sample within
    (from the manifest's used cut spans). Measuring only used spans avoids
    dead/black stretches of the reel that never make it into the weave.
    """
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path])
    try:
        dur = float(r.stdout.strip())
    except Exception:
        return None, "cannot probe duration"
    if dur <= 0:
        return None, "bad duration %.3f" % dur

    if spans:
        windows = [(max(a, 0.0), min(b, dur)) for (a, b) in spans if b > a]
        if not windows:
            return None, "no usable spans"
    else:
        windows = [(0.0, dur)]

    bufs = []
    for (ws, we) in windows:
        t = ws
        while t < we:
            r = run_bin(["ffmpeg", "-hide_banner", "-loglevel", "error",
                     "-ss", "%.3f" % t, "-i", path, "-frames:v", "1",
                     "-vf", "scale=%d:%d,format=gray" % (width, height),
                     "-f", "rawvideo", "-pix_fmt", "gray", "-"])
            if r.returncode == 0 and r.stdout:
                bufs.append(r.stdout)
            t += sample_every
    if not bufs:
        return None, "no frames sampled"
    return np.frombuffer(b"".join(bufs), dtype=np.uint8), None


def measure_stats(arr):
    """Full histogram profile from a luma sample array."""
    f = arr.astype(np.float32) / 255.0
    dark_pct = 100.0 * float((arr < 24).mean())
    blown_pct = 100.0 * float((arr > 235).mean())
    return {
        "frames_sampled": int(arr.size // (160 * 90)),
        "dark_pct": round(dark_pct, 2),
        "blown_pct": round(blown_pct, 2),
        "p10": round(float(np.percentile(f, 10)), 3),
        "p50": round(float(np.percentile(f, 50)), 3),
        "p90": round(float(np.percentile(f, 90)), 3),
        "mean": round(float(f.mean()), 3),
    }


def classify(stats):
    """Classify against the built-in ABSOLUTE target band (inspection mode).

    Phase 6: the renderer path uses classify_relative() — the A-cam defines
    the approved look, not this band. Kept for standalone review.
    """
    reasons = []
    dark = stats["dark_pct"]
    blown = stats["blown_pct"]
    p90 = stats["p90"]
    mean = stats["mean"]
    if p90 < TARGET["p90_range"][0] or mean < TARGET["mean_range"][0] or dark > TARGET["dark_pct_max"]:
        if p90 < TARGET["p90_range"][0]:
            reasons.append("p90=%.2f < %.2f (no highlights)" % (p90, TARGET["p90_range"][0]))
        if dark > TARGET["dark_pct_max"]:
            reasons.append("dark%%=%.1f > %.1f%% (crushed shadows)" % (dark, TARGET["dark_pct_max"]))
        if mean < TARGET["mean_range"][0]:
            reasons.append("mean=%.2f < %.2f" % (mean, TARGET["mean_range"][0]))
        label = "underexposed"
    elif mean > TARGET["mean_range"][1] or blown > TARGET["blown_pct_max"]:
        if blown > TARGET["blown_pct_max"]:
            reasons.append("blown%%=%.1f > %.1f%%" % (blown, TARGET["blown_pct_max"]))
        if mean > TARGET["mean_range"][1]:
            reasons.append("mean=%.2f > %.2f" % (mean, TARGET["mean_range"][1]))
        label = "overexposed"
    else:
        label = "normal"
        reasons.append("inside target profile")
    return label, reasons


def corrective_curves(stats):
    """Compute a curves chain that maps measured p10/p90 onto the target
    profile (absolute inspection mode). Linear in the shadows/mids; p90
    pinched to the target ceiling.

    Returns ffmpeg curves=all='...' fragment or None when no correction.
    """
    label = stats["label"]
    if label == "normal":
        return None
    p10 = stats["p10"]
    p90 = stats["p90"]
    t_p10 = 0.12
    t_p90 = 0.80
    if label == "underexposed":
        pts = [(0.0, 0.03),
               (min(p10, 0.25), t_p10),
               (min(p90, 0.9), t_p90),
               (1.0, 1.0)]
    else:  # overexposed: roll highlights down, shadows stay
        pts = [(0.0, 0.0),
               (min(p10, 0.3), max(p10 * 0.9, 0.05)),
               (min(p90, 0.98), min(t_p90, p90 * 0.85)),
               (1.0, 1.0)]
    pts.sort()
    cleaned = [pts[0]]
    for x, y in pts[1:]:
        if x <= cleaned[-1][0]:
            continue
        if y < cleaned[-1][1]:
            y = cleaned[-1][1]
        cleaned.append((x, y))
    s = " ".join("%.2f/%.2f" % (x, y) for x, y in cleaned)
    return "curves=all='%s'" % s


def classify_relative(stats, ref):
    """Classify a source relative to a reference profile (Phase 6).

    No-op when the source histogram is within tolerance of the reference;
    lighten when crushed below it; darken when blown above it.
    """
    reasons = []
    p10 = stats["p10"]
    p90 = stats["p90"]
    r10 = ref["p10"]
    r90 = ref["p90"]
    if r90 <= 0:
        return "normal", ["reference profile degenerate (p90=0) — no classification"]
    dev10 = p10 / max(r10, 1e-4)
    dev90 = p90 / max(r90, 1e-4)
    if dev90 < 0.75 or dev10 < 0.75:
        label = "underexposed"
        reasons.append("p10=%.3f/p90=%.3f vs reference %.3f/%.3f (below tolerance)" % (p10, p90, r10, r90))
    elif dev10 > 1.60 or dev90 > 1.50:
        label = "overexposed"
        reasons.append("p10=%.3f/p90=%.3f vs reference %.3f/%.3f (above tolerance)" % (p10, p90, r10, r90))
    else:
        label = "normal"
        reasons.append("inside reference profile (dev10=%.2f, dev90=%.2f)" % (dev10, dev90))
    return label, reasons


def corrective_curves_relative(stats, ref):
    """Curves chain mapping measured p10/p90 onto the REFERENCE profile.

    Underexposed: lift shadows toward ref p10, mid/high toward ref p90.
    Overexposed: roll highlights down toward ref p90, shadows stay.
    Returns ffmpeg curves=all='...' fragment or None when no correction.
    """
    label = stats["label"]
    if label == "normal":
        return None
    p10 = stats["p10"]
    p90 = stats["p90"]
    r10 = max(ref["p10"], 0.02)
    r90 = min(max(ref["p90"], r10 + 0.05), 0.97)
    if label == "underexposed":
        pts = [(0.0, 0.03),
               (min(p10, 0.25), r10),
               (min(p90, 0.9), r90),
               (1.0, 1.0)]
    else:  # overexposed: roll highlights down, shadows stay
        pts = [(0.0, 0.0),
               (min(p10, 0.3), min(r10, max(p10 * 0.9, 0.05))),
               (min(p90, 0.98), min(r90, p90 * 0.85)),
               (1.0, 1.0)]
    pts.sort()
    cleaned = [pts[0]]
    for x, y in pts[1:]:
        if x <= cleaned[-1][0]:
            continue
        if y < cleaned[-1][1]:
            y = cleaned[-1][1]
        cleaned.append((x, y))
    s = " ".join("%.2f/%.2f" % (x, y) for x, y in cleaned)
    return "curves=all='%s'" % s


def source_spans(manifest, media_root):
    """Return per-source used spans + b_reel offset map.

    Returns (spans, errors): spans = {source_name: [(start, end), ...]} where
    B cuts are split onto the virtual reel's files (same mapping the renderers
    use). Errors list for unreadable b_reel files.
    """
    sources = manifest.get("sources", {})
    a_name = sources.get("a_reel")
    b_names = list(sources.get("b_reel", []))
    spans = {a_name: []} if a_name else {}
    for n in b_names:
        spans.setdefault(n, [])
    offsets = []
    off = 0.0
    errors = []
    for n in b_names:
        path = os.path.join(media_root, n)
        r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path])
        try:
            dur = float(r.stdout.strip())
        except Exception:
            dur = 0.0
            errors.append("cannot probe b_reel file %s" % n)
        offsets.append((n, off, off + dur))
        off += dur
    for cut in manifest.get("timeline", []):
        si = cut.get("source_in")
        so = cut.get("source_out")
        if not isinstance(si, (int, float)) or not isinstance(so, (int, float)):
            continue
        if cut.get("angle") == "A" and a_name:
            spans[a_name].append((si, so))
        elif cut.get("angle") == "B":
            for (n, fstart, fend) in offsets:
                seg_in = max(si, fstart)
                seg_out = min(so, fend)
                if seg_out > seg_in:
                    spans[n].append((seg_in - fstart, seg_out - fstart))
    return spans, errors


def analyze_one(path, sample_every, spans, ref=None):
    """Measure + classify one file. Returns (stats dict, error)."""
    arr, err = sample_luma(path, sample_every, spans=spans)
    if err:
        return None, err
    stats = measure_stats(arr)
    stats["file"] = os.path.basename(path)
    if ref is not None:
        label, reasons = classify_relative(stats, ref)
        stats["label"] = label
        stats["reasons"] = reasons
        stats["corrective"] = corrective_curves_relative(stats, ref)
        stats["reference"] = {"file": ref.get("file"), "p10": ref["p10"], "p90": ref["p90"]}
    else:
        label, reasons = classify(stats)
        stats["label"] = label
        stats["reasons"] = reasons
        stats["corrective"] = corrective_curves(stats)
    return stats, None


def main():
    ap = argparse.ArgumentParser(description="Exposure profile analyzer")
    ap.add_argument("video", nargs="?", default=None,
                    help="video to analyze (not needed with --manifest)")
    ap.add_argument("--reference", default=None,
                    help="reference video — classify the target RELATIVE to its profile (Phase 6)")
    ap.add_argument("--manifest", default=None,
                    help="job manifest: analyze every source over its USED spans, reference = A-cam")
    ap.add_argument("--media-root", default=".",
                    help="base dir for manifest source files (with --manifest)")
    ap.add_argument("--write", action="store_true",
                    help="with --manifest: patch the manifest in place with pre_grades")
    ap.add_argument("--sample-every", type=int, default=30,
                    help="sample one frame per N seconds (default 30)")
    ap.add_argument("--spans", default=None,
                    help="comma list of source windows to measure, e.g. "
                         "178.75-278.75,388.75-488.75 (default: whole reel)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.manifest:
        import yaml
        with open(args.manifest) as f:
            man = yaml.safe_load(f)
        sources = man.get("sources", {})
        a_name = sources.get("a_reel")
        if not a_name:
            print("ERROR: manifest has no a_reel", file=sys.stderr)
            sys.exit(2)
        spans, errs = source_spans(man, args.media_root)
        for e in errs:
            print("WARN: %s" % e)
        a_path = os.path.join(args.media_root, a_name)
        a_arr, a_err = sample_luma(a_path, args.sample_every, spans=spans.get(a_name) or None)
        if a_err:
            print("ERROR: reference (a_reel) sampling failed: %s" % a_err, file=sys.stderr)
            sys.exit(2)
        ref = measure_stats(a_arr)
        ref["file"] = a_name

        pre_grades = {}
        per_source = {}
        for name in spans:
            if name == a_name:
                continue
            path = os.path.join(args.media_root, name)
            stats, err = analyze_one(path, args.sample_every, spans.get(name) or None, ref=ref)
            if err:
                print("WARN: %s sampling failed: %s" % (name, err))
                continue
            per_source[name] = stats
            if stats["corrective"]:
                pre_grades[name] = stats["corrective"]

        if args.write:
            man.pop("pre_grades", None)
            if pre_grades:
                man["pre_grades"] = pre_grades
            with open(args.manifest, "w") as f:
                yaml.safe_dump(man, f, sort_keys=False)

        result = {"ok": True,
                  "reference": {"file": ref["file"], "p10": ref["p10"], "p90": ref["p90"]},
                  "pre_grades": pre_grades,
                  "sources": per_source}
        if args.json:
            print(json.dumps(result, indent=1))
        else:
            print("EXPOSURE MANIFEST (reference=%s p10=%.3f p90=%.3f)" % (ref["file"], ref["p10"], ref["p90"]))
            for name, st in per_source.items():
                print("  %-24s %s  corrective=%s"
                      % (name, st["label"].upper(), st["corrective"] or "none"))
            print("  pre_grades: %s" % (pre_grades or "{}"))
        sys.exit(0)

    if not args.video:
        ap.error("video path required (or --manifest)")
    if not os.path.exists(args.video):
        print("ERROR: no such file: %s" % args.video, file=sys.stderr)
        sys.exit(2)

    spans = None
    if args.spans:
        spans = []
        for part in args.spans.split(","):
            a, b = part.split("-")
            spans.append((float(a), float(b)))

    if args.reference:
        if not os.path.exists(args.reference):
            print("ERROR: no such reference file: %s" % args.reference, file=sys.stderr)
            sys.exit(2)
        r_arr, r_err = sample_luma(args.reference, args.sample_every, spans=spans)
        if r_err:
            print("ERROR: reference sampling failed: %s" % r_err, file=sys.stderr)
            sys.exit(2)
        ref = measure_stats(r_arr)
        ref["file"] = os.path.basename(args.reference)
        stats, err = analyze_one(args.video, args.sample_every, spans, ref=ref)
    else:
        stats, err = analyze_one(args.video, args.sample_every, spans, ref=None)
    if err:
        print("ERROR: %s" % err, file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(stats, indent=1))
    else:
        print("EXPOSURE %s" % stats["label"].upper())
        for k in ("dark_pct", "blown_pct", "p10", "p50", "p90", "mean"):
            print("  %-9s %s" % (k, stats[k]))
        print("  reasons: %s" % "; ".join(stats["reasons"]))
        print("  corrective: %s" % (stats["corrective"] or "none (normal)"))
    sys.exit(0)


if __name__ == "__main__":
    main()
