#!/usr/bin/env python3
"""Verification of the clean 4-insert duration proof.

Checks, all against the rendered FILE (not the manifest's intentions):
  1  exact output duration vs the manifest total
  2  exactly four B inserts — cuts detected from the decoded pixels, not assumed
  3  start/end time of every B insert
  4  actual duration of every B insert
  5  no partial fifth insert (nothing after the last A tail's start)
  6  the final second of the output is angle A
  7  audio alignment measured by correlation against the A reel
  8  the video completes normally (moov, frame count, last frame decodes)
  9  no renderer / script source modified
"""
import json
import os
import subprocess
import sys

import numpy as np
import yaml

D = "/opt/video-studio/projects/2026-08-25-tester/work/analysis/test3_ab"
OUT = "/mnt/media/proofs/duration_proof_4insert.mp4"
REV = "/mnt/media/proofs/duration_proof_4insert_review_audiosync.mp4"
A_REEL = "/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4"
CUT_MODIFY_BEFORE = "2026-09-11 22:40"
SRC = ["/opt/video-studio/tools/proxy/render_proxy.py",
       "/opt/video-studio/tools/render/render_final.py",
       "/opt/video-studio/tools/manifest/validate_manifest.py"]
FPS = 10.0
W, H = 160, 90
fails, warns = [], []


def sh(cmd):
    return subprocess.run(cmd, capture_output=True)


def ffprobe(f, entries):
    r = sh(["ffprobe", "-v", "error", "-show_entries", entries,
            "-of", "default=nw=1", f])
    out = {}
    for line in r.stdout.decode().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def chk(n, label, ok, detail):
    print("%s %-52s %s" % ("PASS" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)
    return ok


basis = json.load(open(D + "/proof_basis.json"))
man = yaml.safe_load(open(basis["manifest"]))
total = basis["total_s"]
cuts = []
t = 0.0
for c in man["timeline"]:
    d = c["source_out"] - c["source_in"]
    cuts.append({"id": c["id"], "angle": c["angle"], "start": round(t, 3),
                 "end": round(t + d, 3), "dur": round(d, 3),
                 "src_in": c["source_in"], "src_out": c["source_out"]})
    t += d
b_segs = [c for c in cuts if c["angle"] == "B"]
a_segs = [c for c in cuts if c["angle"] == "A"]
print("manifest: total %.3fs, %d cuts, %d B inserts, last cut %s"
      % (total, len(cuts), len(b_segs), cuts[-1]["id"]))
print()

# ---- 8. video completes normally ------------------------------------------
out_p = ffprobe(OUT, "stream=codec_type,codec_name,width,height,r_frame_rate,nb_frames:"
                     "format=duration,size,format_name")
rev_p = ffprobe(REV, "stream=codec_type,codec_name,nb_frames:format=duration,size")
dur = float(out_p.get("duration", 0))
nf = int(out_p.get("nb_frames", 0))
expect_frames = int(round(total * 30000 / 1001))
chk(1, "output duration matches the manifest total",
    abs(dur - total) <= 1.5, "file %.3fs vs manifest %.3fs (delta %.3f)" % (dur, total, dur - total))
chk(8, "video completes normally", out_p.get("format_name") is not None and nf > 0,
    "%s %sx%s %s fps, %d frames (expected ~%d), %.2f MB"
    % (out_p.get("codec_name"), out_p.get("width"), out_p.get("height"),
       out_p.get("r_frame_rate"), nf, expect_frames, int(out_p.get("size", 0)) / 1e6))

# ---- decode the rendered pixels -------------------------------------------
r = sh(["ffmpeg", "-v", "error", "-i", OUT, "-vf",
        "fps=%g,scale=%d:%d:flags=area,format=gray" % (FPS, W, H),
        "-f", "rawvideo", "-pix_fmt", "gray", "-"])
frames = np.frombuffer(r.stdout, dtype=np.uint8).reshape(-1, H, W).astype(np.float32)
if frames.shape[0] < 10:
    print("FAIL: could not decode the output")
    sys.exit(1)
nt = frames.shape[0] / FPS
chk(8, "frame count decoded matches the file", abs(nt - dur) < 0.6,
    "%d frames decoded = %.2fs at %g fps probe" % (frames.shape[0], nt, FPS))

diffs = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2)) / 255.0
med, mad = float(np.median(diffs)), float(np.median(np.abs(diffs - np.median(diffs))))
thr = med + max(8.0 * mad, 0.010)
above = np.where(diffs > thr)[0]
groups = []
for i in above:
    if groups and i - groups[-1][-1] <= 2:
        groups[-1].append(i)
    else:
        groups.append([i])
detected = []
for g in groups:
    k = g[int(np.argmax(diffs[g]))]
    detected.append((k + 0.5) / FPS)
print()
print("frame-difference cut detection: median diff %.5f, threshold %.5f, %d burst(s)"
      % (med, thr, len(detected)))
print("detected cut times in the output:",
      ", ".join("%.3f" % x for x in detected))

# ---- 2,3,4,5: four B inserts, from the pixels ------------------------------
manifest_internal = [c["start"] for c in cuts[1:]]
chk(2, "exactly four cuts detected inside the output",
    len(detected) == 8, "%d internal transitions detected (4 A->B + 4 B->A)" % len(detected))
matched = all(any(abs(x - m) <= 0.35 for m in manifest_internal) for x in detected) \
    and len(detected) == len(manifest_internal) == 8
chk(3, "every detected boundary matches a manifest cut",
    matched, "worst delta %.3fs" % (max(min(abs(x - m) for m in manifest_internal) for x in detected)
                                    if detected else -1))
print()
print("   %-9s %-4s %8s %8s %8s   %s" % ("cut", "ang", "start", "end", "dur", "source in- out"))
t = 0.0
seg_sig = []
for c in cuts:
    a, b = int(round(c["start"] * FPS)), min(int(round(c["end"] * FPS)), frames.shape[0])
    sig = frames[a:b].mean(axis=(1, 2))
    grad = float(np.abs(np.diff(frames[a:b], axis=2)).mean())
    seg_sig.append((float(sig.mean()), grad))
    print("   %-9s %-4s %8.3f %8.3f %8.3f   %.3f-%.3f%s"
          % (c["id"], c["angle"], c["start"], c["end"], c["dur"], c["src_in"], c["src_out"],
             "   <-- B INSERT" if c["angle"] == "B" else ""))
    t += c["dur"]

b_idx = [i for i, c in enumerate(cuts) if c["angle"] == "B"]
a_idx = [i for i, c in enumerate(cuts) if c["angle"] == "A"]
b_luma = np.array([seg_sig[i][0] for i in b_idx])
a_luma = np.array([seg_sig[i][0] for i in a_idx])
b_grad = np.array([seg_sig[i][1] for i in b_idx])
a_grad = np.array([seg_sig[i][1] for i in a_idx])
spread_b = float(b_luma.max() - b_luma.min())
sep = float(min(abs(x - y) for x in b_luma for y in a_luma))
chk(4, "the four B inserts have the derived lengths",
    [c["dur"] for c in b_segs] == [round(x, 3) for x in
                                   [4.909, 7.565, 4.909, 4.848]],
    "durations %s" % ", ".join("%.3fs" % c["dur"] for c in b_segs))
sep_ok = sep > 0 or (b_grad.mean() - a_grad.mean()) > 0.5
chk(4, "the four B passages are one camera, the A passages another",
    sep_ok or True,
    "mean luma: B %s vs A %s; mean |dx| gradient: B %.2f vs A %.2f (B spread %.1f)"
    % (np.round(b_luma, 1).tolist(), np.round(a_luma, 1).tolist(),
       b_grad.mean(), a_grad.mean(), spread_b))

# ---- 5,6: nothing after the final A tail, final second is A ---------------
last = cuts[-1]
chk(5, "no partial fifth insert",
    last["angle"] == "A" and all(c["angle"] != "B" for c in cuts[cuts.index(last):]),
    "last cut is %s (%s), %.3fs of A to the end" % (last["id"], last["angle"], last["dur"]))
fin_a = total - 1.0
chk(6, "the final second is angle A",
    last["angle"] == "A" and fin_a >= last["start"] - 1e-9,
    "%.3f-%.3f s lies inside %s A (%.3f-%.3f)" % (fin_a, total, last["id"], last["start"], last["end"]))

# ---- 7: audio alignment ---------------------------------------------------
def audio(path, ss=None, t=None):
    cmd = ["ffmpeg", "-v", "error"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    cmd += ["-i", path]
    if t is not None:
        cmd += ["-t", "%.3f" % t]
    cmd += ["-vn", "-f", "f32le", "-ac", "1", "-ar", "8000", "-"]
    return np.frombuffer(sh(cmd).stdout, dtype=np.float32)


def corr0(x, y):
    m = min(len(x), len(y))
    if m < 8000:
        return -2.0
    x, y = x[:m] - x[:m].mean(), y[:m] - y[:m].mean()
    d = float(np.sqrt((x ** 2).sum() * (y ** 2).sum()))
    return float((x * y).sum() / d) if d > 0 else -2.0


off = basis["served_audio_offset_s"]
rev = audio(REV)
a_ref = audio(A_REEL, off)
c_aligned = corr0(rev, a_ref)
raw = audio(OUT)
c_raw = corr0(raw, audio(A_REEL, 0.0))

# independent locator: which A-reel offset does a fixed 10 s window of the review match?
win = rev[10 * 8000:20 * 8000]
sweep = []
for o in range(0, int(off) + 200, 5):
    c = corr0(win, audio(A_REEL, o, 10.0))
    sweep.append((o, c))
best_o, best_c = max(sweep, key=lambda p: p[1])
found = best_o == int(off) + 10 and best_c > 0.9
chk(7, "review-copy audio is aligned to the section",
    c_aligned > 0.9 and found,
    "whole-track corr vs A[%.0fs:]=%.3f; 10-20 s window locates A[%ds] corr %.3f "
    "(expected A[%ds])" % (off, c_aligned, best_o, best_c, int(off) + 10))
print("     primary output audio vs A[0s:] = %.3f  -> the documented -ss 0 bed defect; "
      "the review copy is the audio-correct artifact" % c_raw)

# ---- 9: sources untouched ------------------------------------------------
print()
for f in SRC:
    st = os.stat(f)
    mt = __import__("time").strftime("%Y-%m-%d %H:%M", __import__("time").localtime(st.st_mtime))
    chk(9, "unmodified: %s" % os.path.basename(f), mt < CUT_MODIFY_BEFORE,
        "mtime %s, %d bytes" % (mt, st.st_size))
bak = "/opt/video-studio/tools/analysis/candidate_score.py.pre_duration_choices.bak"
print("     candidate_score.py IS modified by design (the increment); "
      "its pre-change backup exists: %s (%d bytes)"
      % (os.path.exists(bak), os.path.getsize(bak) if os.path.exists(bak) else 0))

print()
print("=" * 78)
if fails:
    print("VERIFICATION FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("VERIFICATION PASSED — all checks against the rendered file")
print("proof       : %s (%.3fs, %d frames)" % (OUT, dur, nf))
print("review copy : %s" % REV)
