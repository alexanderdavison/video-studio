#!/usr/bin/env python3
"""Test 3 evidence package — the duration-choice increment.

Inherits the Test 2 package structure, sync mapping and camera normalization
UNCHANGED. The only difference is the candidate list: each event now offers the
duration faces (b_early / b_glance / b_action / b_hold) plus hold_a, and each
event's dense B burst is widened to cover EVERY legal candidate on that event, so
the editorial model can actually see the frames it is choosing between.

Nothing else moves: same 11 events, same anchors, same source windows' entry
points, same sync lag, same grades, same overview sheet construction.
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys

A_CAM = "/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4"
B1 = "/mnt/media/raw/B CAM/DJI_20260824204627_0038_D.MP4"
B2 = "/mnt/media/raw/B CAM/DJI_20260824211420_0039_D.MP4"
ROOT = ("/opt/video-studio/projects/2026-08-25-tester/work/analysis/"
        "editorial_pkg_300_600")
T2 = ROOT + "/test2_normalized"
OUT = ROOT + "/test3_durations"
CAND = "/opt/video-studio/tools/analysis/p1_out/dur_300_420.json"
GEODIR = ("/opt/video-studio/projects/2026-08-25-tester/work/analysis/test1_grade")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

LAG = 1.2783
K = 1673.8707
B1_DUR = 1672.5973
B2_OFF = 1672.5924

GEO_A = "scale=960:540,crop=iw*0.87:ih*0.87,scale=960:540"
FONT_SIZE_TC = 16
SPAN = (300.0, 600.0)
TILE = (320, 180)
GRID = (5, 6)
PAD = 0.25          # seconds of lead/tail around each event's candidate envelope
DENSE_FPS = 2.0


def b_source(a_t):
    r = a_t - LAG
    if r <= B1_DUR:
        return B1, max(0.0, r), "B1"
    return B2, max(0.0, a_t - K), "B2"


def run(cmd, quiet=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and not quiet:
        print("   rc=%s %s" % (r.returncode, (r.stderr or "")[-300:]))
    return r.returncode == 0


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


def draw_tc(text, size, x="6", y="6"):
    return ("drawtext=fontfile=%s:text='%s':fontsize=%d:fontcolor=white:borderw=2:"
            "bordercolor=black:x=%s:y=%s" % (FONT, esc(text), size, x, y))


def main():
    os.makedirs(OUT, exist_ok=True)
    fin = json.load(open(GEODIR + "/camera_normalization_final.json"))
    grade_a, grade_b, trim = fin["grade_a"], fin["grade_b"], fin["solved_pre_grade"]
    cands = json.load(open(CAND))
    print("A grade  : %s..." % grade_a[:60])
    print("B chain  : solved trim (%d chars) + locked B grade" % len(trim))
    print("policy   : min_b %.1f max_b %.1f min_recovery %.1f  duration_choices=%s"
          % (cands["policy"]["minimum_b_shot"], cands["policy"]["maximum_b_shot"],
             cands["policy"]["minimum_a_recovery"], cands["policy"]["duration_choices"]))

    # ---- 1. overview sheet: identical construction, re-rendered so the burn-in
    #         header matches this package
    tmp = os.path.join(OUT, "_tiles")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    ov = os.path.join(OUT, "overview")
    os.makedirs(ov, exist_ok=True)
    times = [SPAN[0] + i * 10.0 for i in range(30)]
    for i, t in enumerate(times):
        run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", "%.3f" % t, "-i", A_CAM,
             "-frames:v", "1", "-vf", "%s,%s,%s" % ("scale=%d:%d" % TILE, grade_a,
                                                    draw_tc(tc(t), FONT_SIZE_TC)),
             "-q:v", "3", "-y", os.path.join(tmp, "t_%03d.jpg" % i)])
    hdr = ("TEST 3 DURATION-CHOICE  A-CAM  frames 0-29  1 frame @ 10.000s  "
           "SPAN %s -> %s  delivery grades applied" % (tc(times[0]), tc(times[-1])))
    sheet = os.path.join(ov, "sheet_01.jpg")
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-framerate", "1", "-start_number", "0",
         "-i", os.path.join(tmp, "t_%03d.jpg"),
         "-vf", "tile=%dx%d:padding=0:margin=0,pad=%d:%d:0:60:color=black,%s"
                % (GRID[0], GRID[1], TILE[0] * GRID[0], TILE[1] * GRID[1] + 60,
                   draw_tc(hdr, 20, "12", "14")),
         "-frames:v", "1", "-q:v", "4", "-y", sheet], quiet=False)
    if not os.path.exists(sheet):
        print("FATAL: overview sheet not produced")
        return 3
    shutil.rmtree(tmp, ignore_errors=True)
    print("overview sheet: %d B" % os.path.getsize(sheet))

    # ---- 2. per-event candidates + dense burst covering EVERY legal candidate
    pack = json.load(open(T2 + "/evidence_pack.json"))
    by_id = {e["event_id"]: e for e in cands["events"]}
    counts, windows = {}, {}
    for e in pack["events"]:
        eid = e["event_id"]
        src = by_id[eid]
        newc = []
        for c in src["candidates"]:
            c = json.loads(json.dumps(c))
            if c["angle"] == "B":
                c["start"] = round(c["start"] - LAG, 3)
                c["end"] = round(c["end"] - LAG, 3)
                c.setdefault("notes", []).append(
                    "B source shifted by the measured lag (-%.4f s) so this window shows "
                    "the same performance moment as the timeline position" % LAG)
            c.setdefault("notes", []).append(
                "B source window = %.3f -> %.3f s on the B reel; timeline %.3f -> %.3f s"
                % (c["start"], c["end"], c["timeline_start"], c["timeline_end"])
                if c["angle"] == "B" else
                "timeline %.3f -> %.3f s of angle A (no cut)" % (c["timeline_start"], c["timeline_end"]))
            newc.append(c)
        e["candidates"] = newc
        e["duration_choices"] = src["duration_choices"]
        e["candidate_ids"] = [c["id"] for c in newc]

        bs = [c for c in newc if c["angle"] == "B"]
        w0 = max(SPAN[0], round(min(c["start"] for c in bs) + LAG - PAD, 3))
        w1 = min(SPAN[1], round(max(c["end"] for c in bs) + LAG + PAD, 3))
        e["dense_window"] = {"requested": [w0, w1], "used": [w0, w1],
                             "clamped_to_span": (w0 <= SPAN[0] or w1 >= SPAN[1]),
                             "span": [SPAN[0], SPAN[1]],
                             "covers": "every legal candidate for this event (%s)"
                                       % ", ".join("%s %.3f-%.3f" % (c["id"], c["timeline_start"],
                                                                      c["timeline_end"]) for c in bs)}
        windows[eid] = (w0, w1)

        d = os.path.join(OUT, "events", eid)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        dur = w1 - w0
        bpath, b0, btag = b_source(w0)
        vf = ("fps=%g,settb=AVTB,scale=960:540,setpts=N/(%g*TB)+%.6f/TB,"
              "drawtext=fontfile=%s:text='%%{pts\\:hms}':fontsize=22:fontcolor=white:"
              "borderw=2:bordercolor=black:x=8:y=8,%s,%s"
              % (DENSE_FPS, DENSE_FPS, w0, FONT, trim, grade_b))
        raw = os.path.join(d, "f_%03d.jpg")
        run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", "%.3f" % b0,
             "-i", bpath, "-t", "%.3f" % (dur + 0.001), "-vf", vf, "-q:v", "4", "-y", raw])
        n = 0
        for f in sorted(glob.glob(os.path.join(d, "f_*.jpg"))):
            t = w0 + n / DENSE_FPS
            os.rename(f, os.path.join(d, "dense_%s_%s.jpg" % (eid, tc(t).replace(":", "-"))))
            n += 1
        counts[eid] = n
        e["dense_frames"] = {"dir": "events/%s" % eid, "angle": "B", "n_frames": n,
                             "window_used": [w0, w1],
                             "sampled_at": "B source = timeline - %.4f s (approved mapping)" % LAG,
                             "displayed_grade": "solved trim + locked B grade",
                             "covers_every_candidate": True,
                             "files": sorted(os.path.basename(p) for p in
                                             glob.glob(os.path.join(d, "*.jpg")))}
        print("   %s: %d frames over %.3f s (%.3f->%.3f) from %s %.3f | B lengths %s"
              % (eid, n, dur, w0, w1, btag, b0,
                 "/".join("%s=%.3f" % (c["id"], c["duration_s"]) for c in bs)), flush=True)

    # ---- 3. guard: the delivered B look must still be near A's
    yavgs = []
    for e in pack["events"][:8]:
        fs = sorted(glob.glob(os.path.join(OUT, "events", e["event_id"], "*.jpg")))
        if not fs:
            continue
        mid = fs[len(fs) // 2]
        r = subprocess.run(["ffprobe", "-v", "error", "-f", "lavfi",
                            "-i", "movie=%s,signalstats" % mid, "-show_entries",
                            "frame_tags=lavfi.signalstats.YAVG", "-of", "csv=p=0"],
                           capture_output=True, text=True)
        try:
            yavgs.append(float(r.stdout.strip().splitlines()[0]))
        except Exception:
            pass
    if not yavgs:
        print("FATAL: no dense frames")
        return 5
    mean_y = sum(yavgs) / len(yavgs)
    print("\nGUARD: dense B frame luma mean %.1f (n=%d)  [raw B ~4-7, delivered ~30, "
          "over-lifted ~134]" % (mean_y, len(yavgs)))
    if not (12.0 <= mean_y <= 60.0):
        print("FATAL: luma %.1f outside the delivered band — refusing to write the pack" % mean_y)
        return 4

    # ---- 4. pack assembly
    pack["package"] = ("test3_durations (duration-choice increment: same 11 events, same sync "
                       "mapping, same normalization; B_GLANCE / B_ACTION / B_HOLD lengths "
                       "derived per event")
    pack["duration_choice_increment"] = {
        "what_changed": "each event now offers duration faces instead of a single fixed length",
        "what_did_not_change": [
            "the 11 events and their anchors (event selection untouched)",
            "the entry point of every on-boundary B candidate (candidate timing untouched)",
            "the sync mapping r = A_t - %.4f" % LAG,
            "the camera normalization (grades + solved pre-grade)",
            "P1 weights, the eight evidence scores, the reference material",
            "policy: minimum_b_shot %.1f, maximum_b_shot %.1f, minimum_a_recovery %.1f"
            % (cands["policy"]["minimum_b_shot"], cands["policy"]["maximum_b_shot"],
               cands["policy"]["minimum_a_recovery"]),
        ],
        "was": "every B candidate was exactly 4.000 s (33/33 across Test 1 and Test 2 packs)",
        "now": "per event: b_glance / b_action / b_hold (+ b_early), each with its own derived length",
        "rules": cands["duration_choice_rules"],
        "why_the_lengths_differ": ("each length is measured on the event's own footage and landed "
                                   "on the beat/downbeat grid the cut grid already uses, then "
                                   "clamped to policy and to the remaining slot width"),
        "dense_frames_note": ("each event's dense burst now covers its FULL candidate envelope, "
                              "so frames exist for the longest legal choice as well as the "
                              "shortest"),
    }
    pack["policy"]["duration_choices"] = True
    pack["candidates_note"] = (
        "Candidate ids, A-timeline windows and scoring are inherited; what changed in this "
        "package is LENGTH. Each event offers b_glance / b_action / b_hold (plus b_early and "
        "hold_a), each with its own length derived from that event's own footage and landed on "
        "the beat/downbeat grid, clamped to minimum_b_shot, maximum_b_shot and the remaining "
        "slot width. Each candidate carries the rule, the derived length and the clamp that "
        "fired in its notes. Test 1 and Test 2 offered exactly one length (4.000 s) per event.")
    pack.setdefault("sampling", {})["dense_window_s"] = ("per event, equal to that event's "
                                                        "full candidate envelope (varies)")
    pack["sampling"]["dense_covers"] = ("every legal candidate for the event, including the "
                                        "longest")
    pack["counts"] = {"n_sheets": 1, "n_sheet_frames": 30, "n_events": len(pack["events"]),
                      "n_dense_frames": sum(counts.values()),
                      "n_images_total": 1 + sum(counts.values())}
    pack["sheets"] = [{"file": "overview/sheet_01.jpg", "sheet_index": 1, "angle": "A",
                       "source": "A CAM/DJI_20260824204625_0054_D.MP4",
                       "span": [times[0], times[-1]], "interval_s": 10.0,
                       "cols": GRID[0], "rows": GRID[1], "tile_wh": list(TILE),
                       "n_frames": len(times), "n_padded": 0,
                       "geometry": "%dx%d" % (TILE[0] * GRID[0], TILE[1] * GRID[1] + 60),
                       "long_edge_px": TILE[0] * GRID[0], "bytes": os.path.getsize(sheet),
                       "sha256": sha(sheet),
                       "timecode_burn": "HH:MM:SS.mmm absolute source time, top-left of every tile",
                       "tile_order": "row-major (left-to-right, top-to-bottom)",
                       "grade": "delivered A grade",
                       "frames": [[i, round(t, 3)] for i, t in enumerate(times)]}]
    json.dump(pack, open(OUT + "/evidence_pack.json", "w"), indent=1)
    print("dense frames total: %d | pack written" % sum(counts.values()))

    shutil.copy(T2 + "/prompt.md", OUT + "/prompt.md")
    files = []
    for p in sorted(glob.glob(OUT + "/**/*.jpg", recursive=True)):
        files.append({"path": os.path.relpath(p, OUT), "bytes": os.path.getsize(p),
                      "sha256": sha(p), "transmitted": False})
    json.dump({"tool": "test3 evidence builder", "package_root": OUT, "transmitted": False,
               "sync_lag_s": LAG, "b2_reel_offset_s": B2_OFF, "b_trim": trim,
               "counts": pack["counts"],
               "candidate_source": CAND,
               "candidate_source_sha256": sha(CAND),
               "files": files, "file_count": len(files),
               "total_bytes": sum(f["bytes"] for f in files)},
              open(OUT + "/package_index.json", "w"), indent=1)
    print("package: %s | files %d | bytes %d" % (OUT, len(files), sum(f["bytes"] for f in files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
