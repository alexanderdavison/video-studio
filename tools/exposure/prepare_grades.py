#!/usr/bin/env python3
"""prepare_grades.py — run the exposure analyzer for a manifest WITHOUT writing it.

Phase 6 renderer integration: the analyzer runs as a PRE-RENDER step; the
resulting corrective curves are applied IN MEMORY by the renderers (their
--auto-pre-grade mode, default on) BEFORE the locked creative grade. The job
manifest stays the reviewed contract — human-authored `pre_grades` always win
over auto-derived ones, and this tool never mutates the manifest.

Usage:
  prepare_grades.py --manifest job.yaml --media-root DIR [--json]

Exit: 0 = measured; 2 = error. Prints {"ok": true, "pre_grades": {...},
"sources": {...}} to stdout.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from exposure_analyze import (  # noqa: E402
    analyze_one,
    measure_stats,
    sample_luma,
    source_spans,
)


def main():
    ap = argparse.ArgumentParser(description="Exposure pre-grade prep (renderer helper)")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--media-root", default=".")
    ap.add_argument("--sample-every", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    import yaml
    try:
        with open(args.manifest) as f:
            man = yaml.safe_load(f)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": "cannot read manifest: %s" % exc}))
        sys.exit(2)

    sources = man.get("sources", {})
    a_name = sources.get("a_reel")
    if not a_name:
        print(json.dumps({"ok": False, "error": "manifest has no a_reel"}))
        sys.exit(2)

    spans, errs = source_spans(man, args.media_root)
    for e in errs:
        print("WARN: %s" % e, file=sys.stderr)

    a_path = os.path.join(args.media_root, a_name)
    a_arr, a_err = sample_luma(a_path, args.sample_every, spans=spans.get(a_name) or None)
    if a_err:
        print(json.dumps({"ok": False, "error": "reference sampling failed: %s" % a_err}))
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
            print("WARN: %s sampling failed: %s" % (name, err), file=sys.stderr)
            continue
        per_source[name] = stats
        if stats["corrective"]:
            pre_grades[name] = stats["corrective"]

    out = {"ok": True, "pre_grades": pre_grades, "sources": per_source}
    print(json.dumps(out, indent=1 if args.json else None))
    sys.exit(0)


if __name__ == "__main__":
    main()
