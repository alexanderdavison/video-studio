#!/usr/bin/env python3
"""
ffmpeg_lint.py — pre-flight linter for every ffmpeg invocation in /opt/video-studio.

Owned by RCC (the pattern table below). Source:
vault creative/decisions/2026-08-26-pipeline-hardening-directives.md, Directive 3.
New footguns found in future postmortems get a ROW ADDED HERE — never a new lesson.

Usage:
  ffmpeg_lint.py <ffmpeg args...>      lint a command (exit 0 = pass, 1 = FAIL)
  ffmpeg_lint.py --list-patterns       dump the pattern table

Design rule: if this linter flags a command, the job does NOT run, full stop.
Only a handful of known-dead patterns are hard FAILs; anything uncertain stays
out of the table so we never block a healthy render.
"""
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# PATTERN TABLE — RCC owned. Each row: (id, detector(args)->bool, message)
# Detectors must be pure over the argv list (plus safe local file probes).
# ---------------------------------------------------------------------------
PATTERNS = []


def _has(args, needle):
    """True if any single arg equals needle (exact token match)."""
    return needle in args


def _has_prefix(args, prefix):
    return any(a.startswith(prefix) for a in args)


def _find_option_values(args, opt):
    """Return values following -opt (next token), e.g. -i file -> ['file']."""
    out = []
    for i, a in enumerate(args):
        if a == opt and i + 1 < len(args):
            out.append(args[i + 1])
    return out


def _inputs(args):
    return _find_option_values(args, "-i")


def _codec_copy(args):
    """True if any -c:v / -c:a / -c / -c:v:0 etc is 'copy'."""
    for i, a in enumerate(args):
        if a in ("-c", "-c:v", "-c:a", "-c:v:0", "-c:a:0") and i + 1 < len(args) and args[i + 1] == "copy":
            return True
        if re.match(r"^-c:[va](:\d+)?$", a) and i + 1 < len(args) and args[i + 1] == "copy":
            return True
    return False


def _concat_list_files(args):
    """If -f concat is used, return the concat list file paths (the -i values)."""
    if "-f" in args:
        idx = args.index("-f")
        if idx + 1 < len(args) and args[idx + 1] == "concat":
            return _inputs(args)
    return []


# 1. stream_loop + shortest — non-terminating mux (postmortem #1)
def _pat_streamloop_shortest(args):
    return _has(args, "-stream_loop") and _has(args, "-shortest")


PATTERNS.append((
    "streamloop-shortest",
    _pat_streamloop_shortest,
    "-stream_loop co-occurs with -shortest — non-terminating mux (postmortem #1). "
    "Remove one of them (bounded -t + stream_loop is the safe pattern).",
))

# 2. h264_vaapi + concat of mixed-format segments — VAAPI reinit crash (postmortem #2)
def _pat_vaapi_concat(args):
    vaapi = _has(args, "h264_vaapi") or _has(args, "-vaapi_device")
    concat = bool(_concat_list_files(args)) or _has_prefix(args, "concat=")
    multi = len(_inputs(args)) > 1
    return vaapi and concat and multi


PATTERNS.append((
    "vaapi-concat",
    _pat_vaapi_concat,
    "h264_vaapi with multi-input concat of mixed formats — VAAPI filter reinit crash "
    "(postmortem #2). Encode segments with libx264 before concat, or keep one uniform input.",
))

# 3. declared inputs vs referenced [N:v]/[N:a] labels mismatch (postmortem #12)
def _pat_label_mismatch(args):
    n_inputs = len(_inputs(args))
    if n_inputs == 0:
        return False
    refs = re.findall(r"\[(\d+):[vas]\]", " ".join(args))
    if not refs:
        return False
    max_idx = max(int(r) for r in refs)
    return max_idx >= n_inputs


PATTERNS.append((
    "stream-label-mismatch",
    _pat_label_mismatch,
    "filter_complex references [N:v]/[N:a] with N >= declared input count — swapped/mismatched "
    "stream labels (postmortem #12). Check the -i order vs the [N:...] labels.",
))

# 4. -c:v copy + -pix_fmt / -profile:v — encoder options on a copied stream (known dead combo)
def _pat_copy_with_encoder_opts(args):
    return _codec_copy(args) and (_has(args, "-pix_fmt") or _has_prefix(args, "-pix_fmt=")
                                  or _has(args, "-profile:v") or _has_prefix(args, "-profile:v="))


PATTERNS.append((
    "copy-encoder-opts",
    _pat_copy_with_encoder_opts,
    "-c copy with -pix_fmt / -profile:v — encoder options cannot apply to a copied stream "
    "('Undefined constant or missing ( in high'). Drop the encoder options; the segments "
    "must already carry yuv420p/High.",
))

# 5. concat -c copy without uniform segment flags (mixed-resolution -c copy corruption)
def _pat_concat_nonuniform(args):
    lists = _concat_list_files(args)
    if not lists or not _codec_copy(args):
        return False
    # Probe the segments in each concat list: resolution/codec/pix_fmt/fps must match.
    for lst in lists:
        p = Path(lst)
        if not p.is_file():
            continue  # can't probe (remote/absent) — not our job to block on unreadable
        segments = []
        try:
            for line in p.read_text(errors="ignore").splitlines():
                m = re.search(r"file\s+'([^']+)'", line)
                if m:
                    segments.append((p.parent / m.group(1)).resolve())
        except Exception:
            continue
        if not segments:
            continue
        sigs = set()
        for seg in segments:
            if not seg.is_file():
                return True  # list references a missing segment — definitely broken
            try:
                out = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "stream=codec_name,width,height,pix_fmt,r_frame_rate",
                     "-select_streams", "v:0", "-of", "csv=p=0", str(seg)],
                    capture_output=True, text=True, timeout=20)
                sigs.add(out.stdout.strip())
            except Exception:
                sigs.add("unprobeable")
        if len(sigs) > 1:
            return True
    return False


PATTERNS.append((
    "concat-nonuniform",
    _pat_concat_nonuniform,
    "concat -c copy over segments that are NOT uniform (codec/resolution/pix_fmt/fps differ). "
    "Mixed-resolution -c copy corrupts. Rebuild segments with "
    "fps=30000/1001,settb=AVTB,setpts=PTS-STARTPTS baked in first.",
))

# 6. scale_vaapi on this box — driver rejects it (studio-video-render-ops, 2026-08-25)
def _pat_scale_vaapi(args):
    return _has_prefix(args, "scale_vaapi") or "scale_vaapi" in " ".join(args)


PATTERNS.append((
    "scale-vaapi-unsupported",
    _pat_scale_vaapi,
    "scale_vaapi is NOT supported by this iGPU driver ('the requested VAProfile is not "
    "supported'). Scale on CPU before hwupload (scale=...,format=nv12,hwupload).",
))

# 7. alimiter=limit=0.95 — locked audio chain violation (audio-true-peak-repair)
def _pat_limiter_095(args):
    return any("alimiter=limit=0.95" in a for a in args)


PATTERNS.append((
    "limiter-095-locked-chain",
    _pat_limiter_095,
    "alimiter=limit=0.95 — the TP gate failed with this limiter (2026-08-26 audio "
    "true-peak repair; rings +2-7 dBTP on DJ audio at any bitrate). Locked chain: "
    "alimiter=limit=0.60:level=false then +0.55dB makeup. Use the locked chain, not 0.95.",
))

# 8. AAC 192k on a DELIVERY mux — locked chain violation (delivery is AAC 320k)
def _pat_aac_192k(args):
    if not any(a == "-c:v" or a.startswith("-c:v") or a == "-movflags" for a in args):
        return False  # audio-only extraction/intermediates may use 192k
    for i, a in enumerate(args):
        if (a == "-b:a" and i + 1 < len(args) and args[i + 1] == "192k") or a == "-b:a=192k":
            return True
    return False


PATTERNS.append((
    "aac-192k-locked-chain",
    _pat_aac_192k,
    "AAC 192k on a video mux — native AAC encoder rings +2-7 dBTP on DJ audio; delivery "
    "is AAC 320k (audio-true-peak-repair lock). 192k cannot pass the -1.5 dBTP gate at "
    "any limiter. Use -b:a 320k on the delivery mux.",
))


# ---------------------------------------------------------------------------
def lint(args):
    # strip a leading "ffmpeg" token if the whole command was passed
    if args and Path(args[0]).name == "ffmpeg":
        args = args[1:]
    fails = []
    for pid, detector, msg in PATTERNS:
        try:
            if detector(args):
                fails.append(f"[{pid}] {msg}")
        except Exception as e:  # never crash the linter on a weird argv
            print(f"lint: detector {pid} errored: {e}", file=sys.stderr)
    if fails:
        print("PREFLIGHT FAIL — ffmpeg command blocked:", file=sys.stderr)
        for f in fails:
            print(f"  - {f}", file=sys.stderr)
        return 1
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--list-patterns":
        print("ffmpeg_lint pattern table (RCC-owned):")
        for pid, _det, msg in PATTERNS:
            print(f"  {pid}: {msg}")
        return 0
    return lint(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
