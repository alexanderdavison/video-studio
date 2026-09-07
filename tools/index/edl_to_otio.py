#!/usr/bin/env python3
"""
EDL → OpenTimelineIO → NLE export bridge.
Reads a video-use style EDL (JSON: clips with source paths, in/out, and optional
beat-quantized cuts), builds an OTIO timeline, and writes FCPXML, Premiere XML,
or EDL via OTIO adapters — no hand-rolled FCPXML.

Usage:
  edl_to_otio.py cuts.json --out cuts.fcpxml
  edl_to_otio.py cuts.json --out cuts.premierexml
  edl_to_otio.py cuts.json --out cuts.edl
  edl_to_otio.py cuts.json --list-adapters
"""
import argparse, json, sys
from pathlib import Path

try:
    import opentimelineio as otio
except ImportError:
    print("opentimelineio not installed — run: tools/venv/bin/pip install opentimelineio", file=sys.stderr)
    sys.exit(1)

RATE = 30.0  # timeline rate; adjust per project if needed

def parse_edl(edl):
    """Accept both a list of cuts and a {clips:[...]} wrapper. Each cut:
       {source, in, out, [label]} — in/out in seconds."""
    if isinstance(edl, dict):
        edl = edl.get("clips", edl.get("cuts", []))
    if not isinstance(edl, list):
        raise ValueError("EDL must be a list of cuts or {clips: [...]}")
    return edl

def build_timeline(edl):
    tl = otio.schema.Timeline(name="studio_cut")
    tl.global_start_time = otio.opentime.RationalTime(0, RATE)
    track = otio.schema.Track(name="V1", kind="Video")
    tl.tracks.append(track)

    for i, cut in enumerate(edl):
        src = cut.get("source")
        if not src:
            raise ValueError(f"cut {i} missing 'source'")
        rate = RATE
        in_t = otio.opentime.RationalTime(float(cut.get("in", 0)), rate)
        out_t = otio.opentime.RationalTime(float(cut.get("out", 0)), rate)
        dur = otio.opentime.RationalTime(out_t.value - in_t.value, rate)

        # media reference pointing at the source file (relative to /mnt/media)
        ref = otio.schema.ExternalReference(
            target_url=src,
            available_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, rate),
                duration=otio.opentime.RationalTime(max(dur.value, 1.0), rate),
            ),
        )
        clip = otio.schema.Clip(name=cut.get("label", f"cut_{i:03d}"), media_reference=ref)
        clip.source_range = otio.opentime.TimeRange(
            start_time=in_t,
            duration=dur,
        )
        track.append(clip)
    return tl

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("edl", nargs="?", help="EDL JSON file (list of {source,in,out,label})")
    ap.add_argument("--out", help="output file (.fcpxml/.premierexml/.edl)")
    ap.add_argument("--list-adapters", action="store_true")
    args = ap.parse_args()

    if args.list_adapters:
        names = (getattr(otio.adapters, "available_adapter_names", None)
                 or getattr(otio.adapters, "available_adapters", lambda: []))()
        for name in sorted(names):
            print(name)
        return

    if not args.edl or not args.out:
        ap.print_usage()
        sys.exit(1)

    edl = json.loads(Path(args.edl).read_text())
    tl = build_timeline(parse_edl(edl))
    out = Path(args.out)
    suffix = out.suffix.lstrip(".").lower()
    adapter_map = {
        "fcpxml": "fcp_xml",
        "premierexml": "premiere_xml",
        "xml": "premiere_xml",
        "edl": "cmx_3600",
    }
    adapter = adapter_map.get(suffix)
    if not adapter:
        print(f"Unknown output type .{suffix}; use .fcpxml / .premierexml / .edl", file=sys.stderr)
        sys.exit(1)
    otio.adapters.write_to_file(tl, str(out), adapter_name=adapter)
    print(f"Wrote {out} ({adapter}) — {len(parse_edl(edl))} clips")

if __name__ == "__main__":
    main()
