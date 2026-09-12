#!/usr/bin/env python3
"""Build the Test 2 evidence package on the approved technical corrections.

Identical structure to Test 1 (A-cam overview sheet + dense B bursts per event, identical
legal candidates) with exactly two changes, both approved:

  1. SYNC — every B source window comes from the approved mapping
         r = A_t - 1.2783 ;  r <= 1672.5973 -> B1[r]   else B2[r - 1672.5924]
     so an A frame and a B frame in this package depict the same physical moment.
  2. GRADE — frames are rendered through the delivery pipeline exactly as the solved
     camera normalization defines it (A delivered grade; B = solved trim + locked B grade),
     read from camera_normalization_final.json rather than hardcoded, so the evidence can
     never drift from the approved solution.

Candidate ids, A-timeline windows, legal windows, scoring and decision rules are UNCHANGED.
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
T1 = ("/opt/video-studio/projects/2026-08-25-tester/work/analysis/"
      "editorial_pkg_300_600/openai_test")
OUT = ("/opt/video-studio/projects/2026-08-25-tester/work/analysis/"
       "editorial_pkg_300_600/test2_normalized")
GEODIR = "/opt/video-studio/projects/2026-08-25-tester/work/analysis/test1_grade"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# ---- approved mapping (2026-09-11) -----------------------------------------
LAG = 1.2783            # lag(A -> B1), uncertainty +/- 0.0021 s
K = 1673.8707           # A-time at which B2 t=0 sits
B1_DUR = 1672.5973
B2_OFF = 1672.5924      # B2 offset on the virtual B reel (4.9 ms overlap at the seam)

GEO_A = "scale=960:540,crop=iw*0.87:ih*0.87,scale=960:540"
GEO_PLAIN = "scale=960:540"
SPAN = (300.0, 600.0)
TILE = (320, 180)
GRID = (5, 6)
PER_SHEET = GRID[0] * GRID[1]
TILE_GEO = "scale=%d:%d" % (TILE[0], TILE[1])   # tiles MUST be tile-sized before `tile`


def b_source(a_t):
    """Approved mapping: A timeline -> (file, source seconds, card tag)."""
    r = a_t - LAG
    if r <= B1_DUR:
        return B1, max(0.0, r), "B1"
    return B2, max(0.0, a_t - K), "B2"


def run(cmd, quiet=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and not quiet:
        print("   rc=%s %s" % (r.returncode, (r.stderr or "")[-200:]))
    return r.returncode == 0


def tc(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return "%02d:%02d:%06.3f" % (h, m, s)


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
    grade_a = fin["grade_a"]
    grade_b = fin["grade_b"]
    trim = fin["solved_pre_grade"]
    bchain = trim                       # PRE-grade only; grade_b is appended once, below
    print("A grade   : %s" % grade_a[:70])
    print("B chain   : solved trim (%d chars) + locked B grade" % len(trim))
    print("mapping   : r = A_t - %.4f ; B1 <= %.4f s else B2[r - %.4f]" % (LAG, B1_DUR, B2_OFF))

    t1 = json.load(open(T1 + "/evidence_pack.json"))
    print("Test 1 pack: %d events, counts=%s" % (len(t1["events"]), t1.get("counts")))

    # ---- 1. overview sheet from the A camera, delivered grade, 1 frame / 10 s
    tmp = os.path.join(OUT, "_tiles")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    ov = os.path.join(OUT, "overview")
    os.makedirs(ov, exist_ok=True)
    times = [SPAN[0] + i * 10.0 for i in range(30)]
    print("extracting %d sheet tiles from A ..." % len(times))
    for i, t in enumerate(times):
        run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", "%.3f" % t, "-i", A_CAM,
             "-frames:v", "1", "-vf", "%s,%s,%s" % (TILE_GEO, grade_a, draw_tc(tc(t), 16)),
             "-q:v", "3", "-y", os.path.join(tmp, "t_%03d.jpg" % i)])
    hdr = ("TEST 2 NORMALISED  A-CAM  frames 0-29  1 frame @ 10.000s  SPAN %s -> %s  "
           "delivery grades applied" % (tc(times[0]), tc(times[-1])))
    sheet = os.path.join(ov, "sheet_01.jpg")
    run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-framerate", "1", "-start_number", "0",
         "-i", os.path.join(tmp, "t_%03d.jpg"),
         "-vf", "tile=%dx%d:padding=0:margin=0,pad=%d:%d:0:60:color=black,%s"
                % (GRID[0], GRID[1], TILE[0] * GRID[0], TILE[1] * GRID[1] + 60,
                   draw_tc(hdr, 20, "12", "14")),
         "-frames:v", "1", "-q:v", "4", "-y", sheet], quiet=False)
    if not os.path.exists(sheet):
        print("FATAL: overview sheet was not produced — refusing to write a pack that references it")
        return 3
    print("   sheet:", os.path.getsize(sheet), "B")
    shutil.rmtree(tmp, ignore_errors=True)   # tiles are not part of the package

    # ---- 2. dense B bursts per event, sampled at the approved B source times
    events = t1["events"]
    counts = {}
    for e in events:
        eid = e["event_id"]
        d = os.path.join(OUT, "events", eid)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        w0, w1 = e["dense_window"]["used"]
        dur = w1 - w0
        bpath, b0, btag = b_source(w0)
        vf = ("fps=2,settb=AVTB,scale=960:540,setpts=N/(2*TB)+%.6f/TB,"
              "drawtext=fontfile=%s:text='%%{pts\\:hms}':"
              "fontsize=22:fontcolor=white:borderw=2:bordercolor=black:x=8:y=8,%s,%s"
              % (w0, FONT, bchain, grade_b))
        raw = os.path.join(d, "f_%03d.jpg")
        run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", "%.3f" % b0,
             "-i", bpath, "-t", "%.3f" % (dur + 0.001), "-vf", vf, "-q:v", "4", "-y", raw])
        n = 0
        for f in sorted(glob.glob(os.path.join(d, "f_*.jpg"))):
            t = w0 + n * 0.5
            os.rename(f, os.path.join(d, "dense_%s_%s.jpg" % (eid, tc(t).replace(":", "-"))))
            n += 1
        counts[eid] = n
        print("   %s: %d dense frames (one pass, %.1f s window, B from %s %.3f s)"
              % (eid, n, dur, btag, b0), flush=True)

    # ---- 2b. GUARD: the delivered B look must be near A's, not raw and not double-graded
    samples = []
    for eid in [e["event_id"] for e in events][:11]:
        fs = sorted(glob.glob(os.path.join(OUT, "events", eid, "*.jpg")))
        if fs:
            samples.append(fs[len(fs) // 2])
    yavgs = []
    for p in samples[:8]:
        r = subprocess.run(["ffprobe", "-v", "error", "-f", "lavfi",
                            "-i", "movie=%s,signalstats" % p,
                            "-show_entries", "frame_tags=lavfi.signalstats.YAVG",
                            "-of", "csv=p=0"], capture_output=True, text=True)
        try:
            yavgs.append(float(r.stdout.strip().splitlines()[0]))
        except Exception:
            pass
    if yavgs:
        mean_y = sum(yavgs) / len(yavgs)
        print("\nGUARD: dense B frame luma  mean %.1f  min %.1f  max %.1f  (n=%d)"
              % (mean_y, min(yavgs), max(yavgs), len(yavgs)))
        print("       reference: raw B ~4-7 | delivered B should sit near A's ~30 | "
              "double-graded ~108 | Test 1 over-lift ~134")
        if not (12.0 <= mean_y <= 60.0):
            print("FATAL: B evidence luma %.1f is outside the delivered band — refusing to "
                  "write a pack that does not show the delivered appearance" % mean_y)
            return 4
    else:
        print("\nFATAL: no dense frames produced — refusing to write the pack")
        return 5

    # ---- 3. evidence pack
    pack = json.loads(json.dumps(t1))
    pack["package"] = "test2_normalized (Test 2 evidence: approved sync mapping + solved normalization)"
    pack["sync"] = {
        "status": "MEASURED 2026-09-11 and approved. Test 1 used a declarative 0.0 assumption.",
        "method": ("bounded-window normalised cross-correlation on room audio, full search; "
                   "corroborated by an audio-free visual motion leg and by camera wall-clock "
                   "metadata. The earlier whole-file FFT global lag was NOT used."),
        "lag_a_to_b1_s": LAG, "uncertainty_s": 0.0021,
        "anchors_agreeing": "5 of 6 within 0.0042 s; one anchor ambiguous (two competing peaks) and excluded, not averaged",
        "drift": "measured +0.034 ms/min -> +0.001 s across the whole set. Effectively zero.",
        "seam": "continuous: 4.9 ms overlap, no gap",
        "b2_reel_offset_s": B2_OFF,
        "mapping": "r = A_t - %.4f ; r <= %.4f -> B1[r] else B2[r - %.4f]" % (LAG, B1_DUR, B2_OFF),
        "applied_to": "every B source window in the candidates below and in this package's frames",
        "refuted": ("60 s/min drift, a 135.8 s seam hole and the coarse visual_sync.py result are "
                    "measurement artifacts; none of them were averaged into this mapping."),
    }
    pack["camera_normalization"] = {
        "status": "applied to every frame in this package",
        "source": "camera_normalization_final.json (solved 2026-09-11, frozen)",
        "a_grade": grade_a, "b_locked_grade": grade_b, "b_trim_pre_grade": trim,
        "rule": fin.get("rule"),
        "measured_before": {"a_delivered_luma": 30.68, "b_with_auto_pre_grade_luma": 135.22,
                            "b_raw_luma": 4.26},
        "measured_after": {"a_delivered_luma": 30.68, "b_normalized_luma": 29.70,
                           "delta_luma": -0.98, "delta_chroma": 0.63},
        "heldout_check": fin.get("heldout"),
        "note": ("B's locked grade already performs the shadow-lift; the renderer's auto pre-grade "
                 "repeated it, which is what made Test 1's B cutaways read ~4x brighter than A. "
                 "The trim above is a small per-channel correction solved in the delivered domain "
                 "and inverted back through the grade."),
    }
    shifted = 0
    for e in pack["events"]:
        for c in e["candidates"]:
            if c["angle"] == "B":
                c["start"] = round(c["start"] - LAG, 3)
                c["end"] = round(c["end"] - LAG, 3)
                c.setdefault("notes", []).append(
                    "B source shifted by the measured lag (-%.4f s) so this window shows the same "
                    "performance moment as the timeline position" % LAG)
                shifted += 1
        e["dense_frames"] = {
            "dir": "events/%s" % e["event_id"], "angle": "B",
            "n_frames": counts.get(e["event_id"], 0), "window_used": e["dense_window"]["used"],
            "sampled_at": "B source = timeline - %.4f s (approved mapping)" % LAG,
            "displayed_grade": "solved trim + locked B grade",
            "files": sorted(os.path.basename(p) for p in
                            glob.glob(os.path.join(OUT, "events", e["event_id"], "*.jpg"))),
        }
    pack["sheets"] = [{"file": "overview/sheet_01.jpg", "sheet_index": 1, "angle": "A",
                       "source": "A CAM/DJI_20260824204625_0054_D.MP4",
                       "span": [times[0], times[-1]], "interval_s": 10.0,
                       "cols": GRID[0], "rows": GRID[1], "tile_wh": [TILE[0], TILE[1]],
                       "n_frames": len(times), "n_padded": 0,
                       "geometry": "%dx%d" % (TILE[0] * GRID[0], TILE[1] * GRID[1] + 60),
                       "long_edge_px": TILE[0] * GRID[0],
                       "bytes": os.path.getsize(sheet), "sha256": sha(sheet),
                       "timecode_burn": "HH:MM:SS.mmm absolute source time, top-left of every tile",
                       "tile_order": "row-major (left-to-right, top-to-bottom)",
                       "grade": "delivered A grade",
                       "frames": [[i, round(t, 3)] for i, t in enumerate(times)]}]
    pack["counts"] = {"n_sheets": 1, "n_sheet_frames": 30, "n_events": len(events),
                      "n_dense_frames": sum(counts.values()),
                      "n_images_total": 1 + sum(counts.values())}
    pack["candidates_note"] = ((pack.get("candidates_note") or "") +
                               " B source windows shifted by the approved measured sync lag; "
                               "candidate ids, A-timeline windows, scoring and decision rules are "
                               "unchanged from Test 1.")
    json.dump(pack, open(OUT + "/evidence_pack.json", "w"), indent=1)
    print("\ncandidates with shifted B source times:", shifted)
    print("dense frames total:", sum(counts.values()))

    shutil.copy(T1 + "/prompt.md", OUT + "/prompt.md")
    idx = {"tool": "test2 evidence builder (approved corrections)", "package_root": OUT,
           "transmitted": False, "sync_lag_s": LAG, "b2_reel_offset_s": B2_OFF,
           "b_trim": trim, "counts": pack["counts"]}
    files = []
    for p in sorted(glob.glob(OUT + "/**/*.jpg", recursive=True)):
        files.append({"path": os.path.relpath(p, OUT), "bytes": os.path.getsize(p),
                      "sha256": sha(p), "transmitted": False})
    idx["files"] = files
    idx["file_count"] = len(files)
    idx["total_bytes"] = sum(f["bytes"] for f in files)
    json.dump(idx, open(OUT + "/package_index.json", "w"), indent=1)
    print("wrote", OUT, "| files:", len(files), "| bytes:", idx["total_bytes"])
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
