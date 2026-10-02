#!/usr/bin/env python3
"""bside_concat.py — build ONE B signal timeline from a multi-card B recording.

Why this exists: the B camera's recording is split across cards. `render_final.py` resolves B
coverage as a VIRTUAL REEL — the B files concatenated in job order, with each file's window being
its cumulative offset (resolve_b_reel: off += duration). The analysis pipeline only ever looked at
the FIRST B file, so everything past that file's duration had no B coverage at all: those windows
built evidence packs with n_b_frames = 0, could offer no B decision vocabulary, and the sender
correctly refused to transmit them.

This concatenates the per-file analysis artifacts onto the same virtual-reel timeline: sample and
run timestamps from file N are shifted by the sum of the durations of files 0..N-1. Both the
source-time (t / t_src_s) and the performance-time (t_perf_s) fields move together, so the sync
mapping stays exactly as recorded.

Usage:
  bside_concat.py --base signals_b.json --add signals_b2.json --offset 1672.597333 --out signals_b.json
  bside_concat.py --base b_activity.json --add b_activity_b2.json --offset 1672.597333 --out b_activity.json

The artifact kind (visual signals vs activity) is detected from the loaded documents; --kind can
force it. Fails closed if the two documents are not the same kind.
"""

import argparse
import json
import sys
from pathlib import Path


def kind_of(doc):
    if "samples" in doc and doc.get("samples") and "t" in doc["samples"][0]:
        return "signals"
    if "samples" in doc and doc.get("samples") and "t_src_s" in doc["samples"][0]:
        return "activity"
    if "runs" in doc:
        return "activity"
    return None


def shift_signals(doc, off):
    out = json.loads(json.dumps(doc))
    for s in out.get("samples", []):
        s["t"] = round(float(s["t"]) + off, 3)
    return out


def shift_activity(doc, off):
    out = json.loads(json.dumps(doc))
    for s in out.get("samples", []):
        s["t_src_s"] = round(float(s["t_src_s"]) + off, 3)
        if "t_perf_s" in s:
            s["t_perf_s"] = round(float(s["t_perf_s"]) + off, 3)
    for r in out.get("runs", []):
        for k in ("onset_src_s", "end_src_s", "peak_src_s", "onset_perf_s", "end_perf_s", "peak_perf_s"):
            if k in r:
                r[k] = round(float(r[k]) + off, 3)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="artifact for the FIRST B file (kept as-is)")
    ap.add_argument("--add", required=True, help="artifact for the next B file (shifted)")
    ap.add_argument("--offset", type=float, required=True, help="seconds to add (sum of earlier B durations)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--kind", choices=["signals", "activity"], default=None)
    a = ap.parse_args()

    base = json.load(open(a.base))
    add = json.load(open(a.add))
    k1, k2 = kind_of(base), kind_of(add)
    kind = a.kind or k1
    if k1 != k2 or (a.kind and a.kind != k1):
        sys.exit("refusing to merge different artifact kinds: %s vs %s" % (k1, k2))

    if kind == "signals":
        b, s = shift_signals(base, 0.0), shift_signals(add, a.offset)
        merged = b
        merged["samples"] = b.get("samples", []) + s.get("samples", [])
        merged["samples"].sort(key=lambda x: x["t"])
        summ = dict(merged.get("summary") or {})
        summ.update({
            "n_samples": len(merged["samples"]),
            "t_start": merged["samples"][0]["t"] if merged["samples"] else None,
            "t_end": merged["samples"][-1]["t"] if merged["samples"] else None,
            "span_s": round(merged["samples"][-1]["t"] - merged["samples"][0]["t"], 3)
            if merged["samples"] else 0.0,
        })
        merged["summary"] = summ
        merged["concatenated_virtual_reel"] = {
            "note": "B files concatenated in job order to match render_final.resolve_b_reel",
            "files": [b.get("source"), add.get("source")],
            "offset_applied_s": a.offset,
        }
    elif kind == "activity":
        b, s = shift_activity(base, 0.0), shift_activity(add, a.offset)
        merged = b
        merged["samples"] = b.get("samples", []) + s.get("samples", [])
        merged["samples"].sort(key=lambda x: x["t_src_s"])
        merged["runs"] = sorted(b.get("runs", []) + s.get("runs", []), key=lambda x: x["onset_src_s"])
        summ = dict(merged.get("summary") or {})
        summ["n_runs"] = len(merged["runs"])
        merged["summary"] = summ
        samp = dict(merged.get("sampling") or {})
        if merged["samples"]:
            samp["start_src_s"] = merged["samples"][0]["t_src_s"]
            samp["end_src_s"] = merged["samples"][-1]["t_src_s"]
            samp["n_samples"] = len(merged["samples"])
        merged["sampling"] = samp
        merged["concatenated_virtual_reel"] = {
            "note": "B files concatenated in job order to match render_final.resolve_b_reel",
            "files": [b.get("source"), add.get("source")],
            "offset_applied_s": a.offset,
        }
    else:
        sys.exit("unrecognised artifact kind")

    Path(a.out).write_text(json.dumps(merged, indent=1))
    n = len(merged.get("samples", []))
    print("wrote %s (%s: %d samples, %d runs) offset %.3f s"
          % (a.out, kind, n, len(merged.get("runs", [])), a.offset))


if __name__ == "__main__":
    main()
