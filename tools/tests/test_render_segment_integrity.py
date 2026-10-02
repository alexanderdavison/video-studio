#!/usr/bin/env python3
"""test_render_segment_integrity.py — the renderer must render every segment to its OWN file.

Regression for a real defect (2026-09-15). While adding time-varying camera matches, the
non-chunked code path lost its per-segment filename assignment, so it reused the stale path of the
last chunk: every B insert overwrote the final chunk of the A segment before it, and that one file
was concatenated TWICE. The timeline total still looked right because the mux cuts to the audio bed,
so nothing complained - the picture was simply in the wrong places.

This fixture is built to reproduce exactly that: a TIME-VARYING pre-grade on the A source (so A
segments are split into chunks) interleaved with plain B segments, with visually DISTINCT sources
(A warm, B cool) so an overwrite or duplicate is visible in the pixels, not just in the file list.

Asserts:
  * every rendered segment file is unique; no path is rendered twice
  * the concat list has exactly one entry per manifest segment (chunks counted)
  * no two ADJACENT concat entries share a path
  * the pixels at each timeline position come from that position's own source
  * no timebase gap: the rendered duration matches the timeline

Exit: 0 = PASS, 1 = FAIL, 2 = error.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import yaml

PY = "/opt/video-studio/tools/venv/bin/python"
RENDERER = "/opt/video-studio/tools/proxy/render_proxy.py"
FPS = 30000.0 / 1001.0
SEG = 3.0                      # seconds per timeline segment
A_DUR, B_DUR = 6.0, 3.0
W, H = 160, 90


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def make_media(path, colour, dur, tone):
    r = run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=%s:size=320x180:rate=30:duration=%.3f" % (colour, dur),
             "-f", "lavfi", "-i", "sine=frequency=%d:duration=%.3f" % (tone, dur),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path])
    assert r.returncode == 0, r.stderr[-300:]


def frame_rgb_bin(path, t):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", "%.3f" % t,
                        "-i", path, "-frames:v", "1", "-vf", "scale=%d:%d" % (W, H),
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, np.uint8)
    if not a.size:
        return None
    return a.reshape(-1, 3).astype(np.float64).mean(axis=0)


def main():
    tmp = tempfile.mkdtemp(prefix="seg_integrity_")
    fails = []
    try:
        a_media = os.path.join(tmp, "A_warm.mp4")
        b1_media = os.path.join(tmp, "B_cool.mp4")
        b2_media = os.path.join(tmp, "B_green.mp4")
        # pure primaries: the dominant channel survives any grade, so a swapped or duplicated
        # segment is unambiguous in the pixels
        make_media(a_media, "red", A_DUR, 440)
        make_media(b1_media, "green", B_DUR, 660)
        make_media(b2_media, "blue", B_DUR, 550)

        # time-varying pre-grade on A forces chunking; B stays static -> the mixed path that broke
        tv = {"mode": "time_varying", "interpolation": "linear", "max_chunk_s": 1.0,
              "points": [{"t": 0.0, "curve": "curves=r='0/0 0.5/0.5 1/1'"},
                         {"t": A_DUR, "curve": "curves=r='0/0 0.5/0.45 1/0.9'"}]}
        man = {
            "manifest_version": 1,
            "job_id": "seg-integrity-fixture",
            "template": "club_dispatch_standard_v1",
            "sources": {"a_reel": a_media, "b_reel": [b1_media, b2_media]},
            "timeline": [
                {"angle": "A", "source_in": 0.0, "source_out": 3.0, "id": "cut_001"},
                {"angle": "B", "source_in": 0.0, "source_out": 3.0, "id": "cut_002"},
                {"angle": "A", "source_in": 3.0, "source_out": 6.0, "id": "cut_003"},
                {"angle": "B", "source_in": 3.0, "source_out": 6.0, "id": "cut_004"},
            ],
            "profiles": {"grade": "club_dispatch_pb3_v1", "crop": "cd_a_pushin_v1",
                         "graphics": "orbit_relay_v1", "audio": "cd_youtube_v1"},
            "pre_grades": {a_media: tv},
        }
        man_path = os.path.join(tmp, "fixture.yaml")
        yaml.safe_dump(man, open(man_path, "w"), sort_keys=False)
        work = os.path.join(tmp, "work")
        out = os.path.join(tmp, "out.mp4")
        r = run([PY, RENDERER, man_path, "--media-root", tmp, "--work", work,
                 "--out", out, "--full", "--json", "--keep-temp"])
        if r.returncode != 0:
            print("renderer failed: %s" % (r.stdout[-400:] + r.stderr[-400:]))
            return 2
        wdir = os.path.join(work, "seg-integrity-fixture")
        concat = os.path.join(wdir, "concat.txt")
        if not os.path.exists(concat):
            print("no concat list produced")
            return 2
        entries = [l.strip()[6:-1] for l in open(concat) if l.strip().startswith("file ")]

        # 1. one entry per rendered segment, and no path used twice
        if len(entries) != len(set(entries)):
            dup = sorted({p for p in entries if entries.count(p) > 1})
            fails.append("a segment file was concatenated more than once: %s"
                         % [os.path.basename(p) for p in dup])
        # 2. no adjacent entries share a path
        for i in range(len(entries) - 1):
            if entries[i] == entries[i + 1]:
                fails.append("adjacent concat entries share a path: %s"
                             % os.path.basename(entries[i]))
        # 3. every entry exists, and every rendered file is used
        on_disk = sorted(os.path.join(wdir, f) for f in os.listdir(wdir) if f.endswith(".mp4"))
        missing = [p for p in entries if not os.path.exists(p)]
        if missing:
            fails.append("concat references missing files: %s"
                         % [os.path.basename(p) for p in missing])
        unused = [p for p in on_disk if p not in entries and os.path.basename(p) != "video.mp4"]
        if unused:
            fails.append("rendered files never concatenated: %s"
                         % [os.path.basename(p) for p in unused])
        # 4. A chunked + B plain = at least 2 A chunks per A segment, plus the 2 B segments
        # the renderer's label is the manifest id WITHOUT its "cut_" prefix
        def _count(entry_list, label):
            return sum(1 for p in entry_list
                       if os.path.basename(p).startswith("seg_%s_" % label))
        n_a_chunks = sum(_count(entries, sid.replace("cut_", ""))
                         for sid in ("cut_001", "cut_003"))
        for sid in ("cut_002", "cut_004"):
            lbl = sid.replace("cut_", "")
            if _count(entries, lbl) != 1:
                fails.append("%s (a plain B segment) produced %d files, expected exactly 1"
                             % (sid, _count(entries, lbl)))
        if n_a_chunks < 4:
            fails.append("A segments were not chunked (found %d A chunk files, the time-varying "
                         "pre-grade should split each 3 s A segment at max_chunk_s 1.0)"
                         % n_a_chunks)

        # 5. the pixels at each timeline position come from that position's own source
        refs = {}
        for tag, path in (("A", a_media), ("B1", b1_media), ("B2", b2_media)):
            px = frame_rgb_bin(path, 1.0)
            refs[tag] = None if px is None else px
        # each timeline position must show ITS OWN source: distinct colours make a swap obvious
        positions = [("cut_001", 1.5, "A"), ("cut_002", 4.5, "B1"),
                     ("cut_003", 7.5, "A"), ("cut_004", 10.5, "B2")]
        rows = []
        for cid, t, want in positions:
            px = frame_rgb_bin(out, t)
            if px is None or refs[want] is None:
                fails.append("no frame decoded at %.1f s" % t)
                continue
            # classify by DOMINANT weighted channel (red / green / blue), which no grade in this
            # chain can reorder - absolute RGB distance is too coarse once the grade lifts shadows
            got = max(px.tolist()[0:1] and {"R": px[0], "G": px[1], "B": px[2]}.items(),
                      key=lambda kv: kv[1])[0]
            want_ch = {"A": "R", "B1": "G", "B2": "B"}[want]
            rows.append("    %s t=%5.1f want %-2s (%s)  dominant %s  rgb %s"
                        % (cid, t, want, want_ch, got,
                           [round(float(v), 1) for v in px]))
            if got != want_ch:
                fails.append("content at %.1f s (%s) is %s-dominant, expected %s (segment overwrite "
                             "or duplication)" % (t, cid, got, want_ch))
            continue
            if got != want:
                fails.append("content at %.1f s (%s) matches %s, expected %s (segment overwrite or "
                             "duplication)" % (t, cid, got, want))
        # 6. duration matches the timeline
        r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", out])
        dur = float(r.stdout.strip() or 0)
        if abs(dur - 12.0) > 0.2:
            fails.append("rendered duration %.3f s, timeline is 12.000 s (a lost or duplicated "
                         "segment changes the total)" % dur)

        print("concat entries: %d (unique %d)" % (len(entries), len(set(entries))))
        print("  " + ", ".join(os.path.basename(p) for p in entries))
        print("segment files on disk: %d" % len(on_disk))
        print("A chunk files: %d" % n_a_chunks)
        print("rendered duration: %.3f s" % dur)
        print("content by position:")
        for row in rows:
            print(row)
        if fails:
            print("\nFAIL")
            for f in fails:
                print("  - %s" % f)
            return 1
        print("\nPASS: every segment rendered to its own file, concatenated once, in order, "
              "with its own pixels")
        return 0
    finally:
        if os.environ.get("KEEP_FIXTURE") != "1":
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
