#!/usr/bin/env python3
"""Frame-pair montage builder for the opportunity audit.

For a list of performance times it pulls one frame from each camera at that
performance moment (B at source = perf - 1.2783) and tiles A above B.

Frames are extracted with ffmpeg fast-seek, then contrast-stretched ONLY for
legibility (per-tile min/max percentile stretch). The stretch is a display aid:
the delivered grade is the camera-normalized one, not this.

usage: montage.py out.png "1050.0,1060.0,..." [--w 480]
"""
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw

A_FILE = "/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4"
B_FILE = "/mnt/media/raw/B CAM/DJI_20260824204627_0038_D.MP4"
LAG = 1.2783


def grab(path, t, w):
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", "%.3f" % t,
                        "-i", path, "-frames:v", "1", "-vf", "scale=%d:-2" % w,
                        "-pix_fmt", "gray", "-f", "rawvideo", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    h = len(r.stdout) // w
    return np.frombuffer(r.stdout[:h * w], dtype=np.uint8).reshape(h, w)


def stretch(img, lo_pct=1.0, hi_pct=99.5):
    a = img.astype(np.float32)
    lo, hi = np.percentile(a, lo_pct), np.percentile(a, hi_pct)
    if hi - lo < 1e-6:
        hi = lo + 1
    return np.clip((a - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8)


def main():
    out = sys.argv[1]
    times = [float(x) for x in sys.argv[2].split(',')]
    w = 480
    if '--w' in sys.argv:
        w = int(sys.argv[sys.argv.index('--w') + 1])
    tiles = []
    for t in times:
        a = grab(A_FILE, t, w)
        b = grab(B_FILE, t - LAG, w)
        if a is None or b is None:
            print("FAILED at", t, file=sys.stderr)
            continue
        tiles.append((t, stretch(a), stretch(b), a.mean(), b.mean()))
    if not tiles:
        sys.exit(2)
    h = tiles[0][1].shape[0]
    lab = 26
    W = w * len(tiles)
    H = lab + 2 * (h + lab)
    canvas = Image.new('L', (W, H), 0)
    d = ImageDraw.Draw(canvas)
    for i, (t, a, b, am, bm) in enumerate(tiles):
        x = i * w
        y = lab + lab
        canvas.paste(Image.fromarray(a), (x, lab))
        canvas.paste(Image.fromarray(b), (x, y + h))
        d.text((x + 4, 4), f"A  t={t:.1f} raw_luma={am:.1f}", fill=255)
        d.text((x + 4, lab + h + 4), f"B  src={t - LAG:.1f} raw_luma={bm:.1f}", fill=255)
    canvas.save(out)
    print(out, canvas.size, "tiles", len(tiles))


if __name__ == '__main__':
    main()
