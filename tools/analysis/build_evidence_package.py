#!/usr/bin/env python3
"""build_evidence_package.py — matched A/B candidate-aligned evidence package.

Increment 2 (2026-09-11) aligned B evidence to the presented B candidates.
Increment 3 (2026-09-11) closes the remaining asymmetry: HOLD_A used to be evidenced by
the global A overview sheet at 1 frame / 10 s, whose nearest tile can sit ~5 s from the
event, so A and B were not comparably evidenced and the model could be choosing the more
informative package rather than the better camera.

What this builds, per editorial event, over ONE performance-time envelope:

    performance envelope = union of every presented candidate's own span, converted to
                           performance time (A source time, since A is the master)
    A dense burst        = the A reel over that performance envelope, at the same fps
    B dense burst        = the B reel over that envelope, in B source time
    candidates           = unchanged, from the candidate artifact
    context              = the A overview sheet, kept for broad program context only

Invariant enforced: at any compared performance time T, the A evidence shows A[T] and the
B evidence shows the synchronized B[T] (B source = performance - lag). Changing cameras
changes perspective only.

Assertions, all fatal (no package is written if any fails):

  * every B candidate satisfies the sync invariant (rejects stale pre-sync-anchored spans)
  * every presented candidate's span lies inside the envelope for its own camera
  * the A envelope and the B envelope cover the SAME performance interval
  * hold_a's span lies inside the A dense burst (this is what a mis-declared a_offset
    breaks, so it is the check that makes a malformed mapping fail closed)
  * both bursts actually reach the envelope end on disk, at comparable density
  * neither envelope leaves its reel

Usage:
  build_evidence_package.py --candidates sync_300_420.json --out PKGDIR
      [--media-root /mnt/media/raw] [--fps 2] [--section 300 600]
      [--grade-json .../camera_normalization_final.json] [--prompt-from DIR]

Exit: 0 = package written and validated; 1 = alignment failure (no package);
      2 = error.
"""

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys

# ---- accepted sync mapping (Locked decisions 1) — not to be changed here ------
LAG = 1.2783
K = 1673.8707
B1_DUR = 1672.5973
B2_OFF = 1672.5924
INVARIANT_TOL = 0.036            # accepted mapping uncertainty + ~1 frame

A_CAM = "A CAM/DJI_20260824204625_0054_D.MP4"
B1 = "B CAM/DJI_20260824204627_0038_D.MP4"
B2 = "B CAM/DJI_20260824211420_0039_D.MP4"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
TILE = (320, 180)
GRID = (5, 6)
EPS = 1e-6


def b_source(t):
    """B source time on the virtual reel -> (file, in-file seconds)."""
    if t <= B1_DUR:
        return B1, max(0.0, t)
    return B2, max(0.0, t - B2_OFF)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0, r.stderr[-300:]


def ffprobe_dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def tc(t):
    return "%02d:%02d:%06.3f" % (int(t // 3600), int((t % 3600) // 60), t % 60)


def esc(s):
    return s.replace(":", "\\:").replace("'", "")


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def draw_tc(text, size, x, y):
    return ("drawtext=fontfile=%s:text='%s':fontsize=%d:fontcolor=white:borderw=2:"
            "bordercolor=black:x=%s:y=%s" % (FONT, esc(text), size, x, y))


def main():
    ap = argparse.ArgumentParser(description="Matched A/B candidate-aligned evidence builder")
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--media-root", default="/mnt/media/raw")
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--section", type=float, nargs=2, default=[300.0, 600.0])
    ap.add_argument("--grade-json", default=None)
    ap.add_argument("--prompt-from", default=None)
    ap.add_argument("--sheet", action="store_true", default=True)
    ap.add_argument("--no-sheet", dest="sheet", action="store_false")
    args = ap.parse_args()

    if not os.path.exists(args.candidates):
        print("ERROR: no candidate artifact: %s" % args.candidates, file=sys.stderr)
        return 2
    cand = json.load(open(args.candidates))
    pol = cand["policy"]
    if not pol.get("sync_anchored_entry"):
        print("ERROR: %s was not produced with --sync-anchored-entry; its spans are not "
              "synchronized and must not be used as evidence" % args.candidates, file=sys.stderr)
        return 2
    events = cand["events"]
    lag = LAG
    root = args.media_root
    a_path = os.path.join(root, A_CAM)
    a_off = float((cand.get("inputs", {}).get("offsets") or {}).get("a_offset") or 0.0)
    a_reel_dur = ffprobe_dur(a_path)

    grade_a, b_chain = "", ""
    if args.grade_json and os.path.exists(args.grade_json):
        fin = json.load(open(args.grade_json))
        grade_a = fin["grade_a"]
        b_chain = fin["solved_pre_grade"] + "," + fin["grade_b"]
    print("candidates : %s (%d events, sync_anchored_entry=%s)"
          % (args.candidates, len(events), pol["sync_anchored_entry"]))
    print("timebase   : performance = A source - %.3f s ; B source = performance - %.4f s"
          % (a_off, lag))
    print("grades     : A %s | B chain %d chars" % ("yes" if grade_a else "NONE", len(b_chain)))

    out = args.out
    os.makedirs(out, exist_ok=True)

    # ---------------------------------------------------------------- plan ----
    plan = []
    for ev in events:
        eid = ev["event_id"]
        bfaces = [c for c in ev["candidates"] if c["angle"] == "B"]
        aface = next((c for c in ev["candidates"] if c["id"] == "hold_a"), None)
        if not bfaces and aface is None:
            print("ALIGNMENT FAILURE %s: no candidates to evidence" % eid, file=sys.stderr)
            return 1
        # Two INDEPENDENT estimates of the performance interval, which must agree: the B
        # estimate comes through the accepted sync mapping, the A estimate through the
        # declared a_offset. If they disagree, a_off does not match the candidate artifact
        # and an A burst would show a different moment from the B evidence.
        pb = ((min(c["start"] + lag for c in bfaces), max(c["end"] + lag for c in bfaces))
              if bfaces else None)
        pa = ((aface["start"] - a_off, aface["end"] - a_off) if aface is not None else None)
        if pb is None and pa is None:
            print("ALIGNMENT FAILURE %s: no candidates to evidence" % eid, file=sys.stderr)
            return 1
        # The B-derived interval is authoritative (B candidates are anchored to T by the
        # mapping); hold_a must lie inside it (asserted below). Both bursts then cover
        # exactly this interval, so A and B are aligned by construction AND by check.
        plo, phi = pb if pb is not None else pa
        plan.append({"event_id": eid, "b_faces": bfaces, "a_face": aface,
                     # kept unrounded so the A and B bursts span the same interval exactly;
                     # rounding happens only when emitting metadata
                     "perf": (plo, phi), "perf_a": pa, "perf_b": pb,
                     "a_src": (plo + a_off, phi + a_off),
                     "b_src": (plo - lag, phi - lag) if pb is not None else None})

    # ------------------------------------------------- assertions (pre-render) --
    bad = []
    for p in plan:
        eid = p["event_id"]
        for c in p["b_faces"]:
            resid = (c["timeline_start"] - c["start"]) - lag
            if abs(resid) > INVARIANT_TOL + EPS:
                bad.append("%s/%s: sync invariant violated by %+.4f s — this span is not "
                           "synchronized (stale pre-sync-anchored window?)" % (eid, c["id"], resid))
        for c in p["b_faces"]:
            if c["start"] < p["b_src"][0] - EPS or c["end"] > p["b_src"][1] + EPS:
                bad.append("%s/%s: span %.3f-%.3f outside the B envelope %.3f-%.3f"
                           % (eid, c["id"], c["start"], c["end"], p["b_src"][0], p["b_src"][1]))
        if p["perf_a"] and p["perf_b"]:
            d_lo = p["perf_a"][0] - p["perf_b"][0]
            d_hi = p["perf_a"][1] - p["perf_b"][1]
            if d_lo < -INVARIANT_TOL or d_hi > INVARIANT_TOL:
                bad.append("%s: the A-derived and B-derived performance intervals disagree — "
                           "hold_a lies %+.3f s / %+.3f s outside the B-derived interval, so "
                           "the declared a_offset %.3f does not match the candidates"
                           % (eid, d_lo, d_hi, a_off))
        if p["a_face"] is not None:
            # hold_a is angle A, so a mis-declared a_offset lands it outside the A burst
            a0, a1 = p["a_face"]["start"], p["a_face"]["end"]
            if a0 < p["a_src"][0] - INVARIANT_TOL or a1 > p["a_src"][1] + INVARIANT_TOL:
                bad.append("%s/hold_a: A span %.3f-%.3f outside the A burst %.3f-%.3f "
                           "(mis-declared a_offset %.3f?)"
                           % (eid, a0, a1, p["a_src"][0], p["a_src"][1], a_off))
        # the emitted B span must map back to the performance interval through the mapping
        if p["b_src"] is not None:
            if abs((p["b_src"][0] + lag) - p["perf"][0]) > 1e-6 or \
               abs((p["b_src"][1] + lag) - p["perf"][1]) > 1e-6:
                bad.append("%s: the B burst does not map back onto the performance interval"
                           % eid)
            if abs(p["perf"][0] - p["perf_b"][0]) > 1e-6 or abs(p["perf"][1] - p["perf_b"][1]) > 1e-6:
                bad.append("%s: emitted performance interval is not the B-derived one" % eid)
        reel_b = float(cand["sync_anchored_entry"]["reel_end_s"])
        if p["b_src"] is not None and (p["b_src"][0] < -EPS or p["b_src"][1] > reel_b + EPS):
            bad.append("%s: B envelope %.3f-%.3f leaves the B reel (0-%.3f)"
                       % (eid, p["b_src"][0], p["b_src"][1], reel_b))
        if a_reel_dur and (p["a_src"][0] < -EPS or p["a_src"][1] > a_reel_dur + EPS):
            bad.append("%s: A envelope %.3f-%.3f leaves the A reel (0-%.3f)"
                       % (eid, p["a_src"][0], p["a_src"][1], a_reel_dur))
    if bad:
        print("\nALIGNMENT FAILURE — refusing to build a package that would show the model "
              "footage other than its candidates:", file=sys.stderr)
        for b in bad:
            print("   ! %s" % b, file=sys.stderr)
        return 1
    print("pre-render assertions: %d events, %d B candidates, A+B envelopes share the "
          "performance interval" % (len(plan), sum(len(p["b_faces"]) for p in plan)))

    # ------------------------------------------------------------ overview -----
    S0, S1 = args.section
    times = [S0 + i * 10.0 for i in range(int((S1 - S0) / 10.0))]
    if args.sheet:
        tmp = out + "/_tiles"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        os.makedirs(out + "/overview", exist_ok=True)
        for i, t in enumerate(times):
            run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", "%.3f" % t,
                 "-i", a_path, "-frames:v", "1", "-vf",
                 "scale=%d:%d,%s,%s,%s" % (TILE[0], TILE[1], grade_a,
                                           draw_tc(tc(t), 16, "6", "6"),
                                           draw_tc("A-CAM", 14, "w-tw-6", "6")),
                 "-q:v", "3", "-y", os.path.join(tmp, "t_%03d.jpg" % i)])
        hdr = ("A-CAM OVERVIEW (context only)  frames 0-%d  1 frame @ 10.000s  SPAN %s -> %s  "
               "the per-event dense evidence is authoritative" % (len(times) - 1,
                                                                  tc(times[0]), tc(times[-1])))
        sheet = out + "/overview/sheet_01.jpg"
        ok, err = run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-framerate", "1",
                       "-start_number", "0", "-i", os.path.join(tmp, "t_%03d.jpg"), "-vf",
                       "tile=%dx%d:padding=0:margin=0,pad=%d:%d:0:60:color=black,%s"
                       % (GRID[0], GRID[1], TILE[0] * GRID[0], TILE[1] * GRID[1] + 60,
                          draw_tc(hdr, 20, "12", "14")),
                       "-frames:v", "1", "-q:v", "4", "-y", sheet])
        shutil.rmtree(tmp, ignore_errors=True)
        if not ok or not os.path.exists(sheet):
            print("ERROR: overview sheet failed: %s" % err, file=sys.stderr)
            return 2
        print("overview   : %d tiles, context only, %d B" % (len(times), os.path.getsize(sheet)))

    # ------------------------------------------ dense bursts: A and B, matched ----
    def render(src_file, src_off, dur, prefix, eid, label, chain, base):
        """Render one dense burst, burning the camera label and the source timecode."""
        d = os.path.join(out, "events", eid)
        os.makedirs(d, exist_ok=True)
        raw = os.path.join(d, "_%s_%%04d.jpg" % prefix)
        vf = ("fps=%g,settb=AVTB,scale=960:540,setpts=N/(%g*TB)+%.6f/TB,"
              "drawtext=fontfile=%s:text='%%{pts\\:hms}':fontsize=22:fontcolor=white:"
              "borderw=2:bordercolor=black:x=8:y=8,%s,%s"
              % (args.fps, args.fps, base, FONT, chain, draw_tc(label, 22, "w-tw-8", "8")))
        ok, err = run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                       "-ss", "%.3f" % src_off, "-i", os.path.join(root, src_file),
                       "-t", "%.3f" % (dur + 1e-3), "-vf", vf, "-q:v", "4", "-y", raw])
        n = 0
        for f in sorted(glob.glob(os.path.join(d, "_%s_*.jpg" % prefix))):
            t = base + n / args.fps
            os.rename(f, os.path.join(d, "%s_%s_%s.jpg"
                                      % (prefix, eid, tc(t).replace(":", "-"))))
            n += 1
        return n

    counts = {}
    for p in plan:
        eid = p["event_id"]
        d = os.path.join(out, "events", eid)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        dur = round(p["perf"][1] - p["perf"][0], 6)
        n_a = render(A_CAM, p["a_src"][0], dur, "a", eid, "A-CAM", grade_a, p["a_src"][0])
        if n_a == 0:
            print("ALIGNMENT FAILURE %s: no A dense frames rendered" % eid, file=sys.stderr)
            return 1
        n_b = 0
        if p["b_src"] is not None:
            bfile, boff = b_source(p["b_src"][0])
            n_b = render(bfile, boff, dur, "b", eid, "B-CAM", b_chain, p["b_src"][0])
            if n_b == 0:
                print("ALIGNMENT FAILURE %s: no B dense frames rendered" % eid, file=sys.stderr)
                return 1
        counts[eid] = {"a": n_a, "b": n_b}
        p["n_a"], p["n_b"] = n_a, n_b
        p["last_a_t"] = round(p["a_src"][0] + (n_a - 1) / args.fps, 3)
        p["last_b_t"] = (round(p["b_src"][0] + (n_b - 1) / args.fps, 3) if n_b else None)
        print("   %s  perf %.3f-%.3f (%.3f s)  A %d frames  B %d frames  from %s / %s"
              % (eid, p["perf"][0], p["perf"][1], dur, n_a, n_b, os.path.basename(A_CAM),
                 os.path.basename(bfile) if p["b_src"] is not None else "-"), flush=True)

    # --------------------------- assertions (post-render: the frames on disk) ----
    bad = []
    for p in plan:
        eid = p["event_id"]
        need = int((p["perf"][1] - p["perf"][0]) * args.fps + 1e-9)
        for tag, n, last, lo, hi in (("A", p["n_a"], p["last_a_t"], p["a_src"][0], p["a_src"][1]),
                                     ("B", p["n_b"], p["last_b_t"],
                                      p["b_src"][0] if p["b_src"] else None,
                                      p["b_src"][1] if p["b_src"] else None)):
            if hi is None:
                continue
            if n < need - 1:
                bad.append("%s: %s burst has only %d frames over %.3f s at %g fps (expected ~%d)"
                           % (eid, tag, n, hi - lo, args.fps, need))
            if last is not None and last < hi - 1.0 / args.fps - 1e-6:
                bad.append("%s: %s last frame %.3f s does not reach the envelope end %.3f s"
                           % (eid, tag, last, hi))
        if p["n_a"] and p["n_b"] and abs(p["n_a"] - p["n_b"]) > 1:
            bad.append("%s: A and B frame counts are not comparable (%d vs %d)"
                       % (eid, p["n_a"], p["n_b"]))
        if p["a_face"] is not None:
            a0, a1 = p["a_face"]["start"], p["a_face"]["end"]
            if a0 < p["a_src"][0] - EPS or a1 > p["a_src"][1] + EPS:
                bad.append("%s/hold_a: A span not contained in the rendered A burst" % eid)
    if bad:
        print("\nALIGNMENT FAILURE — rendered bursts do not meet the matched-evidence "
              "requirement:", file=sys.stderr)
        for b in bad:
            print("   ! %s" % b, file=sys.stderr)
        return 1
    print("\npost-render: every presented candidate is contained, A and B are comparably "
          "sampled over the same performance interval")

    # ------------------------------------------------------------- metadata ----
    pack = {"package": os.path.basename(out.rstrip("/")),
            "kind": "matched A/B candidate-aligned evidence package (increment 3, 2026-09-11)",
            "span": {"start": S0, "end": S1, "duration_s": S1 - S0,
                     "start_tc": tc(S0), "end_tc": tc(S1)},
            "candidate_artifact": {"path": args.candidates, "sha256": sha(args.candidates),
                                   "sync_anchored_entry": True,
                                   "policy": {k: pol[k] for k in
                                              ("minimum_b_shot", "maximum_b_shot",
                                               "minimum_a_recovery")}},
            "sync_mapping": {"lag_a_to_b_s": lag, "b1_dur_s": B1_DUR, "b2_reel_offset_s": B2_OFF,
                             "a_offset_s": a_off,
                             "mapping": "B_source = performance - %.4f ; A_source = performance "
                                        "+ %.4f" % (lag, a_off),
                             "invariant": ("at any compared performance time T the A evidence "
                                           "shows A[T] and the B evidence shows the synchronized "
                                           "B[T]; changing cameras changes perspective only")},
            "evidence_alignment": {
                "rule": ("each event carries a MATCHED pair of dense bursts over one "
                         "performance-time envelope: the union of its presented candidates' own "
                         "spans, read from the candidate artifact"),
                "sampling": "A and B at the same frame rate over the same interval",
                "assertions": ["candidate source span contained in its camera's burst",
                               "A and B envelopes cover the same performance interval",
                               "hold_a contained in the A dense burst",
                               "both bursts reach the envelope end on disk",
                               "B candidates satisfy the sync invariant",
                               "envelopes inside their reels"],
                "failure_policy": "any violation aborts package generation; no package is written",
                "hold_a_evidence": ("hold_a is evidenced by the event's dense A burst; the A "
                                    "overview sheet is retained for broad context only and is no "
                                    "longer its primary evidence"),
                "events": [{"event_id": p["event_id"],
                            "performance_span": [round(p["perf"][0], 3), round(p["perf"][1], 3)],
                            "performance_span_tc": [tc(p["perf"][0]), tc(p["perf"][1])],
                            "a_evidence_span": [round(p["a_src"][0], 3), round(p["a_src"][1], 3)],
                            "b_evidence_span": ([round(p["b_src"][0], 3), round(p["b_src"][1], 3)]
                                                if p["b_src"] else None),
                            "a_frames": p["n_a"], "b_frames": p["n_b"],
                            "covers_candidates": [c["id"] for c in p["b_faces"]],
                            "containment": "PASS", "synchronized": "PASS",
                            "sync_invariant_worst_s": round(
                                max([abs((c["timeline_start"] - c["start"]) - lag)
                                     for c in p["b_faces"]] or [0.0]), 6)}
                           for p in plan]},
            "sampling": {"dense_fps": args.fps, "dense_wh": [960, 540],
                         "frame_naming": ("{camera}_{event_id}_HH-MM-SS.mmm.jpg — the timecode is "
                                          "that camera's SOURCE time; A-CAM / B-CAM is also "
                                          "burned into each frame"),
                         "dense_timebase": ("A dense frames are in A source time = performance "
                                            "time; B dense frames are in B source time = "
                                            "performance - %.4f s. Each frame's both timestamps "
                                            "are recorded in the event's evidence.frames list."
                                            % lag),
                         "sheet_interval_s": 10.0, "sheet_role": "broad program context only",
                         "sheet_grid": list(GRID), "sheet_tile_wh": list(TILE)}}

    for p, ev in zip(plan, events):
        d = os.path.join(out, "events", p["event_id"])
        frames = []
        for cam, prefix, src0 in (("A", "a", p["a_src"][0]),
                                  ("B", "b", p["b_src"][0] if p["b_src"] else None)):
            if src0 is None:
                continue
            for idx, f in enumerate(sorted(glob.glob(os.path.join(d, "%s_*.jpg" % prefix)))):
                s = round(src0 + idx / args.fps, 3)
                frames.append({"file": os.path.basename(f), "camera": cam, "source_s": s,
                               "performance_s": round(s + (a_off if cam == "A" else lag), 3)})
        ev["evidence"] = {"performance_span_s": [round(p["perf"][0], 3), round(p["perf"][1], 3)],
                          "performance_span_tc": [tc(p["perf"][0]), tc(p["perf"][1])],
                          "a": {"timebase": "A source = performance time",
                                "span_s": [round(p["a_src"][0], 3), round(p["a_src"][1], 3)],
                                "n_frames": p["n_a"],
                                "source_file": A_CAM, "grade": "delivered A grade"},
                          "b": ({"timebase": "B source",
                                 "span_s": [round(p["b_src"][0], 3), round(p["b_src"][1], 3)],
                                 "n_frames": p["n_b"],
                                 "source_file": b_source(p["b_src"][0])[0],
                                 "grade": "solved pre-grade + locked B grade"}
                                if p["b_src"] else None),
                          "covers_candidates": [c["id"] for c in p["b_faces"]],
                          "hold_a_evidence": "a",
                          "containment": "PASS", "synchronized": "PASS",
                          "dir": "events/%s" % p["event_id"], "frames": frames}
        ev["dense_window"] = {"used": [round(p["perf"][0], 3), round(p["perf"][1], 3)],
                              "timebase": "performance time"}
        # the prompt tells the model to read candidates/<event_id>.json; write exactly the
        # list embedded in the pack so that promise is literally true
        os.makedirs(os.path.join(out, "candidates"), exist_ok=True)
        json.dump(ev["candidates"],
                  open(os.path.join(out, "candidates", "%s.json" % ev["event_id"]), "w"), indent=1)
    if args.sheet and os.path.exists(out + "/overview/sheet_01.jpg"):
        pack["sheets"] = [{"file": "overview/sheet_01.jpg", "angle": "A",
                           "source": A_CAM, "span": [times[0], times[-1]], "interval_s": 10.0,
                           "cols": GRID[0], "rows": GRID[1], "tile_wh": list(TILE),
                           "n_frames": len(times), "timecode_burn": "A source time",
                           "role": "broad program context only — NOT the per-event evidence",
                           "grade": "delivered A grade",
                           "bytes": os.path.getsize(out + "/overview/sheet_01.jpg"),
                           "sha256": sha(out + "/overview/sheet_01.jpg"),
                           "frames": [[i, round(t, 3)] for i, t in enumerate(times)]}]
    pack["events"] = events
    pack["counts"] = {"n_events": len(events),
                      "n_a_frames": sum(c["a"] for c in counts.values()),
                      "n_b_frames": sum(c["b"] for c in counts.values()),
                      "n_dense_frames": sum(c["a"] + c["b"] for c in counts.values()),
                      "n_sheet_frames": len(times) if args.sheet else 0,
                      "n_images_total": sum(c["a"] + c["b"] for c in counts.values())
                                        + (1 if args.sheet else 0)}
    json.dump(pack, open(out + "/evidence_pack.json", "w"), indent=1)

    if args.prompt_from and os.path.exists(args.prompt_from + "/prompt.md"):
        shutil.copy(args.prompt_from + "/prompt.md", out + "/prompt.md")

    files = []
    # every content file in the package except the index itself, so the index is a real
    # integrity manifest (frames, candidate lists, prompt, evidence pack)
    for f in sorted(glob.glob(out + "/**/*", recursive=True)):
        if os.path.isdir(f) or os.path.basename(f) == "package_index.json":
            continue
        files.append({"path": os.path.relpath(f, out), "bytes": os.path.getsize(f),
                      "sha256": sha(f)})
    json.dump({"tool": "build_evidence_package.py",
               "package_root": os.path.basename(out.rstrip("/")),
               "package_root_is": ("basename only, so this index is byte-identical wherever the "
                                   "package is built; every file path below is relative to it"),
               "candidate_artifact": args.candidates,
               "candidate_artifact_sha256": sha(args.candidates),
               "alignment": "matched A/B dense evidence over one performance envelope; PASS",
               "counts": pack["counts"], "files": files, "file_count": len(files),
               "total_bytes": sum(f["bytes"] for f in files)},
              open(out + "/package_index.json", "w"), indent=1)
    print("pack       : %s | A %d + B %d dense frames + 1 sheet | %d bytes"
          % (out, pack["counts"]["n_a_frames"], pack["counts"]["n_b_frames"],
             sum(f["bytes"] for f in files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
