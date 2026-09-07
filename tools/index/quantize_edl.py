#!/usr/bin/env python3
"""
Beat-quantized cuts — snap EDL cut points to the nearest beat.

For DJ content: cut on the music, not just on silence. Reads a beats.json
(from beat_detect.py) or beats from the studio DB, and shifts every cut
boundary to the nearest beat within a tolerance window.

Usage:
  quantize_edl.py cuts.json beats.json --out cuts_quantized.json [--tolerance 0.5]
  quantize_edl.py cuts.json --from-db --clip 2 --out cuts_quantized.json
"""
import argparse, json, sqlite3
from pathlib import Path

DB = Path("/opt/video-studio/studio.db")

def load_beats_from_file(path):
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        return data.get("beats", data.get("beat_times", []))
    return data

def load_beats_from_db(clip_id):
    c = sqlite3.connect(DB)
    rows = c.execute("SELECT time FROM beats WHERE clip_id=? ORDER BY time", (clip_id,)).fetchall()
    return [r[0] for r in rows]

def quantize(beats, cut_in, cut_out, tolerance):
    """Snap in/out to nearest beat within tolerance. Returns (new_in, new_out, moved)."""
    new_in, new_out = cut_in, cut_out
    moved = []
    for bound, name in ((cut_in, "in"), (cut_out, "out")):
        nearest = min(beats, key=lambda b: abs(b - bound)) if beats else None
        if nearest is not None and abs(nearest - bound) <= tolerance:
            if name == "in":
                new_in = nearest
            else:
                new_out = nearest
            moved.append((name, round(bound, 3), round(nearest, 3)))
    return new_in, new_out, moved

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("edl")
    ap.add_argument("beats", nargs="?", help="beats.json OR --from-db")
    ap.add_argument("--from-db", action="store_true")
    ap.add_argument("--clip", type=int, help="clip_id for --from-db")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tolerance", type=float, default=0.5, help="max seconds to snap (default 0.5)")
    args = ap.parse_args()

    edl = json.loads(Path(args.edl).read_text())
    if isinstance(edl, dict):
        cuts = edl.get("clips", edl.get("cuts", []))
    else:
        cuts = edl

    if args.from_db:
        if not args.clip:
            print("--from-db requires --clip", file=sys.stderr)
            sys.exit(1)
        beats = load_beats_from_db(args.clip)
    else:
        if not args.beats:
            print("need beats.json path or --from-db --clip N", file=sys.stderr)
            sys.exit(1)
        beats = load_beats_from_file(args.beats)

    total_moved = 0
    out_cuts = []
    for i, cut in enumerate(cuts):
        new_in, new_out, moved = quantize(beats, float(cut.get("in", 0)), float(cut.get("out", 0)), args.tolerance)
        total_moved += len(moved)
        c = dict(cut)
        c["in"], c["out"] = new_in, new_out
        if moved:
            c["quantized"] = moved
        out_cuts.append(c)

    Path(args.out).write_text(json.dumps({"clips": out_cuts, "beats_used": len(beats),
                                          "boundaries_moved": total_moved}, indent=2))
    print(f"wrote {args.out}: {len(out_cuts)} cuts, {total_moved} boundaries snapped, "
          f"beats={len(beats)}, tolerance={args.tolerance}s")

if __name__ == "__main__":
    main()
