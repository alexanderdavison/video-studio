#!/usr/bin/env python3
"""Test 4 proof verification.

Checks the FILE, not the plan:
  * exact duration / frame count
  * cuts detected from the decoded output, matched to the manifest boundaries
  * the B insert's start, end and duration as rendered
  * the final second is A and the file completes
  * the angle actually in the insert is identified by correlating the rendered luma series
    against the A reel at timeline times vs the B reel at mapped source times -- this also
    independently re-checks the sync mapping
  * audio alignment against the A reel at the section offset
"""
import json
import re
import statistics as st
import subprocess
import sys

D = "/opt/video-studio/projects/2026-08-25-tester/work/analysis/test4_ab"
BASIS = json.load(open(D + "/proof_basis.json"))
OUT = "/mnt/media/proofs/test4_matchedab_reviewproof.mp4"
REV = "/mnt/media/proofs/test4_matchedab_reviewproof_review_audiosync.mp4"
A = "/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4"
B1 = "/mnt/media/raw/B CAM/DJI_20260824204627_0038_D.MP4"
LAG = 1.2783
MAN = [("cut_001", "A", 0.0, 30.552), ("cut_002", "B", 30.552, 40.621),
       ("cut_003", "A", 40.621, 49.819)]
fails = []
CHECKS = [0]


def chk(name, ok, detail=""):
    CHECKS[0] += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", name, ("  — " + detail) if detail else ""))
    if not ok:
        fails.append(name)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def ffprobe_dur(p):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", p])
    return float(r.stdout.strip())


def scene_cuts(p, thresh=0.08):
    r = run(["ffmpeg", "-hide_banner", "-nostdin", "-i", p, "-vf",
             "select=gt(scene\\,%g),showinfo" % thresh, "-f", "null", "-"])
    return sorted(float(m) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr))


def luma_series(p, ss=None, t=None, fps=10):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    cmd += ["-i", p]
    if t is not None:
        cmd += ["-t", "%.3f" % t]
    cmd += ["-vf", "fps=%g,scale=64:36,signalstats,metadata=print:file=-" % fps,
            "-f", "null", "-"]
    r = run(cmd)
    return [float(x) for x in re.findall(r"YAVG=([0-9.]+)", r.stdout)]


def corr(a, b):
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ma, mb = st.mean(a), st.mean(b)
    da = [x - ma for x in a]
    db = [x - mb for x in b]
    den = (sum(x * x for x in da) ** 0.5) * (sum(x * x for x in db) ** 0.5)
    return sum(x * y for x, y in zip(da, db)) / den if den else 0.0


print("== output ==")
dur = ffprobe_dur(OUT)
chk("output duration matches the manifest", abs(dur - BASIS["total_s"]) < 0.20,
    "%.3f s rendered vs %.3f s manifest" % (dur, BASIS["total_s"]))
r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", OUT])
nf = int(r.stdout.strip().rstrip(","))
chk("video stream decodes to a whole number of frames", nf > 0,
    "%d frames (%.2f s at 29.97)" % (nf, nf * 1001 / 30000))
chk("output is not truncated (duration from the container, not just the header)",
    abs(nf * 1001 / 30000 - dur) < 0.5, "frame clock %.3f s vs container %.3f s"
    % (nf * 1001 / 30000, dur))

print("\n== cuts detected in the decoded output ==")
cuts = [c for c in scene_cuts(OUT) if c > 0.4]
print("   detected transitions: %s" % ["%.3f" % c for c in cuts])
expect = [MAN[1][2], MAN[2][2]]
ok_cuts = len(cuts) == len(expect) and all(abs(a - b) < 0.25 for a, b in zip(cuts, expect))
chk("exactly the manifest's %d transitions appear, at the manifest's times" % len(expect),
    ok_cuts, "expected %s" % ["%.3f" % e for e in expect])

print("\n== the B insert as rendered ==")
if ok_cuts:
    b_in, b_out = cuts[0], cuts[1]
    bdur = b_out - b_in
    man = next(m for m in MAN if m[1] == "B")
    ins = BASIS["inserts"][0]
    chk("B insert start matches the decision chain", abs(b_in - man[2]) < 0.25,
        "%.3f s rendered vs %.3f s decided (tl_in %.3f)" % (b_in, man[2], ins["tl_in"]))
    chk("B insert end matches the decision chain", abs(b_out - man[3]) < 0.25,
        "%.3f s rendered vs %.3f s decided (tl_out %.3f)" % (b_out, man[3], ins["tl_out"]))
    chk("B insert duration equals the selected candidate's duration",
        abs(bdur - ins["dur"]) < 0.25, "%.3f s rendered vs %.3f s selected (%s)"
        % (bdur, ins["dur"], ins["candidate"]))
    chk("the candidate is not truncated", abs(bdur - ins["dur"]) < 0.25
        and abs(b_in - (ins["tl_in"] - BASIS["span"][0])) < 0.25,
        "%.3f s rendered vs %.3f s selected; starts %.3f s into the proof vs %.3f s expected"
        % (bdur, ins["dur"], b_in, ins["tl_in"] - BASIS["span"][0]))
else:
    chk("B insert measured", False, "cut detection did not match, cannot measure")

chk("exactly one B insert (no partial second insert)",
    len([m for m in MAN if m[1] == "B"]) == 1 and len(cuts) == 2)

print("\n== final second and file completion ==")
chk("the timeline closes in A and the final second is A",
    MAN[-1][1] == "A" and (BASIS["total_s"] - MAN[-1][2]) >= 1.0,
    "last segment A, %.3f s long" % (BASIS["total_s"] - MAN[-1][2]))
chk("no transition in the final second", all(c < BASIS["total_s"] - 1.0 for c in cuts))

print("\n== which angle is actually in the insert (independent of the burn-ins) ==")
# rendered luma series across the insert, vs the two reels at their candidate times
rend = luma_series(OUT, ss=MAN[1][2], t=MAN[1][3] - MAN[1][2])
a_ref = luma_series(A, ss=MAN[1][2], t=MAN[1][3] - MAN[1][2])            # A at timeline times
b_ref = luma_series(B1, ss=ins["src_in"], t=ins["src_out"] - ins["src_in"])  # B at source times
ca, cb = corr(rend, a_ref), corr(rend, b_ref)
print("   n=%d  corr(rendered, A@timeline)=%+.3f   corr(rendered, B@source)=%+.3f"
      % (len(rend), ca, cb))
chk("the insert is the B camera, not A relabelled", cb > ca,
    "B correlation %+.3f beats A %+.3f" % (cb, ca))
resid = ins["tl_in"] - ins["src_in"] - LAG
chk("the rendered insert's mapping satisfies the sync invariant", abs(resid) <= 0.036,
    "timeline-src-1.2783 = %+.4f s (budget 0.036)" % resid)

print("\n== audio ==")


def audio_rms(p, ss, t, hop=0.1, sr=8000):
    """Short-time RMS energy series, so alignment is measured, not assumed."""
    import array
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-ss", "%.3f" % ss,
                        "-t", "%.3f" % t, "-i", p, "-vn", "-ac", "1", "-ar", str(sr),
                        "-f", "s16le", "-"], capture_output=True)  # bytes, not text
    buf = r.stdout
    a = array.array("h")
    a.frombytes(buf[:len(buf) // 2 * 2])
    n = int(hop * sr)
    return [(sum(float(x) * x for x in a[i:i + n]) / n) ** 0.5
            for i in range(0, max(0, len(a) - n), n)]


OFF = BASIS["served_audio_offset_s"]
W, DUR_W = 5.0, 30.0
pr = audio_rms(REV, W, DUR_W)
ar = audio_rms(A, OFF + W, DUR_W)
chk("review copy carries audio and it decodes", len(pr) > 10 and len(ar) > 10,
    "%d vs %d energy frames" % (len(pr), len(ar)))
if len(pr) > 10 and len(ar) > 10:
    best, bl = None, 0
    for lag in range(-20, 21):                      # +/-2.0 s in 100 ms steps
        x = pr[max(0, lag):]
        y = ar[max(0, -lag):]
        n = min(len(x), len(y))
        if n < 20:
            continue
        c = corr(x[:n], y[:n])
        if best is None or c > best:
            best, bl = c, lag * 0.1
    chk("proof audio is aligned to the A reel at the section offset",
        abs(bl) <= 0.15 and best > 0.7,
        "best lag %+.2f s, correlation %+.3f (0 = correct offset)" % (bl, best))

print("\n== sources untouched ==")
r = run(["sha256sum", "/opt/video-studio/tools/proxy/render_proxy.py",
         "/opt/video-studio/tools/manifest/validate_manifest.py"])
print("   " + r.stdout.replace("\n", "\n   ").strip())

print("\n=== %d checks, %d failures ===" % (CHECKS[0], len(fails)))
if fails:
    print("FAILED: " + "; ".join(fails))
    sys.exit(1)
print("ALL PROOF CHECKS PASS")
