#!/usr/bin/env python3
"""Pre-flight ffmpeg linter — Directive 3 (2026-08-26 master scope).

Flags known-dead patterns BEFORE a render launches. The pattern table is
owned by RCC: any new footgun found in a postmortem gets a row here.

Usage:
    echo '<ffmpeg command...>' | ffmpeg_lint.py
    ffmpeg_lint.py <cmd...>
Exit: 0 = clean, 1 = at least one LINT-FAIL (job must not run).
"""
import re
import sys


def lint(cmdline: str) -> int:
    args = cmdline.split()
    joined = cmdline
    fails = 0

    def fail(name: str, detail: str) -> None:
        nonlocal fails
        fails += 1
        print(f"LINT-FAIL: {name} — {detail}")

    n_inputs = sum(1 for a in args if a == "-i")

    # 1. Non-terminating mux: -stream_loop -1 co-occurring with -shortest
    if re.search(r"-stream_loop\s+-1", joined) and "-shortest" in args:
        fail("stream_loop+shortest", "non-terminating mux (writes garbage forever)")

    # 2. VAAPI + multi-input: h264_vaapi with more than one -i
    #    (multi-segment concat with mixed formats = reinit crash)
    if "h264_vaapi" in joined and n_inputs > 1:
        fail("vaapi+multisegment", f"h264_vaapi with {n_inputs} inputs (format-boundary reinit crash)")

    # 3. -c:v copy co-occurring with -pix_fmt or -profile:v
    if "-c:v" in args and "copy" in args and ("-pix_fmt" in args or "-profile:v" in args):
        fail("copy+pixfmt", "stream copy with pixel format/profile override (incompatible combo)")

    # 4. Declared input count vs referenced [N:v]/[N:a] labels
    for m in re.finditer(r"\[(\d+):[va]\]", joined):
        idx = int(m.group(1))
        if idx >= n_inputs:
            fail("stream-label", f"[{idx}:...] referenced but only {n_inputs} input(s) declared")

    # 5. Concat without uniform-segment flags baked in
    #    Heuristic: a -f concat (demuxer) re-encode must carry the uniform
    #    segment timebase flags in the same command, else mixed-res corruption.
    if re.search(r"-f\s+concat", joined) and not re.search(r"settb=AVTB", joined):
        fail("concat+uniform", "concat demuxer without settb=AVTB (mixed-resolution corruption)")

    return 1 if fails else 0


if __name__ == "__main__":
    cmd = sys.stdin.read().strip() if not sys.argv[1:] else " ".join(sys.argv[1:])
    if not cmd:
        print("usage: echo '<ffmpeg cmd>' | ffmpeg_lint.py  (or pass args)")
        sys.exit(2)
    rc = lint(cmd)
    print("LINT-CLEAN" if rc == 0 else f"LINT-BLOCKED ({rc} pattern(s) — job must not run)")
    sys.exit(rc)
