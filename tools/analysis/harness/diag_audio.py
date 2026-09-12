#!/usr/bin/env python3
"""Diagnose the audio mismatch: where does each track actually come from?"""
import subprocess
import numpy as np

OUT = "/mnt/media/proofs/duration_proof_4insert.mp4"
REV = "/mnt/media/proofs/duration_proof_4insert_review_audiosync.mp4"
A = "/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4"
B1 = "/mnt/media/raw/B CAM/DJI_20260824204627_0038_D.MP4"
SR = 8000


def dec(path, ss=None, t=None):
    cmd = ["ffmpeg", "-v", "error"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    cmd += ["-i", path]
    if t is not None:
        cmd += ["-t", "%.3f" % t]
    cmd += ["-vn", "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, dtype=np.float32)


def corr(x, y):
    m = min(len(x), len(y))
    if m < 8000:
        return -2.0
    x, y = x[:m] - x[:m].mean(), y[:m] - y[:m].mean()
    d = np.sqrt((x ** 2).sum() * (y ** 2).sum())
    return float((x * y).sum() / d) if d > 0 else -2.0


print("probe of the A reel streams:")
print(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                      "stream=index,codec_type,codec_name,sample_rate,channels",
                      "-of", "default=nw=1", A], capture_output=True).stdout.decode())

rev = dec(REV)
out = dec(OUT)
print("decoded lengths: review %.2fs  primary %.2fs  (rms rev %.5f  out %.5f)"
      % (len(rev) / SR, len(out) / SR, float(np.sqrt((rev ** 2).mean())),
         float(np.sqrt((out ** 2).mean()))))

print()
print("review-copy audio vs A reel, sweeping the A-reel offset (10 s windows):")
best = (-2, None)
for off in range(0, 400, 5):
    a = dec(A, off, 10.0)
    c = corr(rev[10 * SR:20 * SR], a)
    if c > best[0]:
        best = (c, off)
    if c > 0.5:
        print("   A[%3ds] -> corr %.3f" % (off, c))
print("   best: A[%ss] corr %.3f" % (best[1], best[0]))

print()
print("review-copy audio vs A reel at 300 s: %.3f" % corr(rev, dec(A, 300.0, len(rev) / SR)))
print("primary-output audio vs A reel at   0 s: %.3f" % corr(out, dec(A, 0.0, len(out) / SR)))
print("review-copy audio vs B1 reel at 298.7 s: %.3f"
      % corr(rev, dec(B1, 298.722, len(rev) / SR)))
