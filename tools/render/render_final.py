#!/usr/bin/env python3
"""render_final.py — Phase 4 deterministic final renderer (ISH D template engine).

Consumes the SAME manifest as the Phase 3 proxy and produces the LOCKED
delivery format: 1080p30 h264 yuv420p +faststart, AAC 320k/48k, BT.709,
PB3 grade, A push-in, cd_bug_v9 overlay, cd_youtube_v1 audio chain.

"Existing commands become internal renderer functions" (refined plan §3):
- reel resolution + cross-file split  (from the Phase 3 proxy)
- uniform segment encode (runbook)    (from bake_v6_canonical.sh)
- locked grade/crop/graphics/audio    (from profile_registry.json — never reinvented)
- postflight + atomic publish         (from hardening.sh, sourced via subprocess)

Gate order (Phase 1 gates as code):
  1. validate_manifest.py --media-root --json   rc 1 = invalid, refuse
  2. flock /opt/video-studio/.render.lock        one render at a time
  3. video segments: libx264 crf 20 preset fast, 1080p30 uniform, yuv420p, -an
  4. concat -c copy                              (uniform segments -> safe)
  5. A audio bed 0..total (apad) -> two-pass loudnorm -> alimiter=0.60:level=false
     -> volume=+0.55dB -> AAC 320k/48k           (cd_youtube_v1 LOCKED chain)
  6. graphics: overlay from the manifest's graphics PROFILE. Geometry (asset root,
     verified frame sequence, content bbox, anchor, margins) comes from the profile in
     profile_registry.json — never from renderer constants.
     -t total (never -shortest with -stream_loop -1)
  7. postflight_verify (hardening.sh) -> publish_partial (never clobber verified)
  8. record postflight block into the completed job manifest

Usage:
  python3 render_final.py JOB.yaml --media-root /path/to/mix \
      [--out /mnt/media/finals/<job_id>.mp4] [--work /opt/video-studio/work/final] \
      [--graphics-root /opt/video-studio/assets/graphics] \
      [--crf 20] [--keep-temp] [--json] [--force]

Exit: 0 = delivered + verified; 1 = manifest invalid; 2 = error.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# ONE meaning for a manifest `pre_grades` entry (static filter string OR time-varying control
# points), shared with the proxy renderer so the approval artifact and the delivery artifact cannot
# disagree about the same manifest field (2026-09-15).
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "lib")))
import camera_match as CM  # noqa: E402
MANIFEST_DIR = os.path.normpath(os.path.join(HERE, "..", "manifest"))
VALIDATOR = os.path.join(MANIFEST_DIR, "validate_manifest.py")
REGISTRY = os.path.join(MANIFEST_DIR, "profile_registry.json")
VENV_PY = "/opt/video-studio/tools/venv/bin/python"
LOCK_FILE = "/opt/video-studio/.render.lock"
HARDENING = "/opt/video-studio/tools/lib/hardening.sh"
DEFAULT_GRAPHICS_ROOT = "/opt/video-studio/assets/graphics"

# Graphics geometry is NOT a renderer constant. It belongs to the profile, in
# profile_registry.json (keys: asset_root, frames, canvas, content_bbox, geometry,
# alpha, persistence, animation). The previous module-level BUG_CROP/BUG_POS pair
# encoded cd_bug_v9's geometry globally, so any other asset would render "successfully"
# while being cropped or positioned wrongly — a silent defect, since nothing failed.
# Geometry is derived from the UNION ALPHA CONTENT BBOX of the asset (measured over the
# whole loop by tools/graphics/union_bbox.py), never from the asset's transparent canvas.
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# Locked delivery raster. The segment filter graph scales every angle to this, and the
# graphics layout scales from geometry.reference_height to this height (150 px / 40 px
# at 1080p -> 100 px / 27 px at 720p -> 300 px / 80 px at 2160p).
FINAL_W, FINAL_H = 1920, 1080


_JSON_STDOUT = False


def log(msg):
    # --dump-chain emits machine-readable JSON on stdout; in that mode every human-readable line
    # goes to stderr so a consumer can json.load(stdout) without filtering.
    print(msg, flush=True, file=sys.stderr if _JSON_STDOUT else sys.stdout)


def fail(msg, rc=2):
    log("ERROR: %s" % msg)
    sys.exit(rc)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ffprobe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "default=nw=1:nk=1", path])
    try:
        return float(r.stdout.strip())
    except Exception:
        return None


def ffprobe_has_audio(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path])
    return bool(r.stdout.strip())


def acquire_lock():
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        return None


def load_registry():
    with open(REGISTRY) as f:
        return json.load(f)


def b_grade_name(registry, grade_name):
    """Name of the DEDICATED B-angle camera-match grade, or None when there is none.

    Same naming rule as grade_for() (base WITHOUT the version suffix + _b_v1, both
    spellings tried). Kept as its own function because the pre-grade branch must be able
    to ask "does angle B own its own camera-match grade?" without re-deriving the chain.
    """
    grades = registry["profiles"]["grade"]
    base = re.sub(r"_v\d+$", "", grade_name)
    for cand in (grade_name + "_b_v1", base + "_b_v1"):
        if cand in grades:
            return cand
    return None


def grade_for(registry, grade_name, angle):
    """Renderer rule (2026-08-27): B-cuts use the <base>_b_v1 variant when the
    registry defines it, otherwise the base grade. Variant naming convention:
    base WITHOUT the version suffix + _b_v1 (e.g. club_dispatch_pb3_v1 ->
    club_dispatch_pb3_b_v1). Both spellings are tried so a future variant name
    like <full>_b_v1 also resolves.
    """
    grades = registry["profiles"]["grade"]
    if angle == "B":
        cand = b_grade_name(registry, grade_name)
        if cand:
            return grades[cand]["locked_params"]
    return grades[grade_name]["locked_params"]


def validate_manifest(manifest_path, media_root):
    cmd = [VENV_PY, VALIDATOR, "--media-root", media_root, "--json", manifest_path]
    r = run(cmd)
    try:
        parsed = json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        parsed = {}
    if r.returncode == 0:
        return True, "manifest valid"
    msg = parsed.get("error") or parsed.get("detail") or r.stdout.strip() or "invalid manifest"
    return False, msg


def resolve_b_reel(b_files, media_root):
    """Return (list of (path, start, end), error). Virtual reel = concat order."""
    out = []
    off = 0.0
    for name in b_files:
        path = os.path.join(media_root, name)
        dur = ffprobe_duration(path)
        if dur is None:
            return None, "cannot probe b_reel file %s" % name
        out.append((path, off, off + dur))
        off += dur
    return out, None


def build_segments(cuts, a_path, reel):
    """Return (segments, total). Segment = (src, l_in, l_out, abs_off, angle)."""
    segs = []
    total = 0.0
    for cut in cuts:
        dur = cut["source_out"] - cut["source_in"]
        if cut["angle"] == "A":
            segs.append((a_path, cut["source_in"], cut["source_out"], total, "A"))
        else:
            placed = False
            for (path, fstart, fend) in reel:
                seg_in = max(cut["source_in"], fstart)
                seg_out = min(cut["source_out"], fend)
                if seg_out > seg_in:
                    segs.append((path, seg_in - fstart, seg_out - fstart,
                                 total + (seg_in - cut["source_in"]), "B"))
                    placed = True
            if not placed:
                return None, "cut %s source span not in b_reel" % cut["id"]
        total += dur
    return segs, total


FINAL_FPS = 30000.0 / 1001.0   # the CFR rate every segment is rendered at


def render_video_segment(src, l_in, l_out, vf, seg_path, crf, frames=None):
    """Encode one segment with an EXACT frame count (same defect/fix as the proxy renderer).

    `-t dur` rounds every segment up to the frame grid and `concat -c copy` accumulates it,
    so a 57-segment program drifts ~1 s. The count comes from the GLOBAL frame boundaries.
    """
    dur = l_out - l_in
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", "%.6f" % l_in, "-i", src,
           "-t", "%.6f" % dur,
           "-vf", vf,
           "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
           "-an"]
    if frames:
        cmd += ["-frames:v", str(int(frames))]
    cmd.append(seg_path)
    r = run(cmd)
    if r.returncode != 0:
        return False, r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "ffmpeg failed"
    return True, ""


def concat_video(seg_paths, out_video):
    if len(seg_paths) == 1:
        os.replace(seg_paths[0], out_video)
        return True, ""
    lst = os.path.join(os.path.dirname(out_video), "concat.txt")
    with open(lst, "w") as f:
        for p in seg_paths:
            f.write("file '%s'\n" % p)
    r = run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", lst,
             "-c", "copy", out_video])
    if r.returncode != 0:
        return False, r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "concat failed"
    return True, ""


def build_audio_bed(a_reel, total, out_wav):
    """Continuous A bed 0..total as WAV (input to two-pass loudnorm)."""
    if ffprobe_has_audio(a_reel):
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", "0", "-i", a_reel,
               "-vn",  # CRITICAL: audio-only — decoding 4K video to extract
                       # audio burns 4 cores for 15+ min (runbook lesson).
               "-af", "apad",
               "-t", "%.6f" % total,
               "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", out_wav]
    else:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
               "-t", "%.6f" % total,
               "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", out_wav]
    r = run(cmd)
    if r.returncode != 0:
        return False, r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "audio bed failed"
    return True, ""


def loudnorm_measure(wav):
    """Two-pass pass 1: measure loudnorm parameters on the clean A bed."""
    r = run(["ffmpeg", "-hide_banner", "-i", wav,
             "-af", "loudnorm=I=-13.5:LRA=6.4:TP=-1.5:print_format=json",
             "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        # loudnorm print_format JSON returns numeric fields as strings; the
        # measured values feed %.3f formatting downstream, so coerce them.
        for k in ("input_i", "input_lra", "input_tp", "input_thresh",
                  "target_offset", "target_i", "target_lra", "target_tp"):
            if k in d:
                d[k] = float(d[k])
        return d
    except Exception:
        return None


def measure_integrated(wav):
    """Integrated LUFS of a WAV (post-limiter check for makeup gain)."""
    r = run(["ffmpeg", "-hide_banner", "-i", wav,
             "-af", "loudnorm=I=-13.5:LRA=6.4:TP=-1.5:print_format=json",
             "-f", "null", "-"])
    m = re.search(r"\"input_i\"\s*:\s*\"?(-?\d+\.?\d*)", r.stderr or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def encode_audio_chain(wav, total, out_m4a, measured):
    """Apply the LOCKED audio chain, then DYNAMIC makeup (only if the limiter
    dips loudness below -13.5 — registry: 'makeup only if loudness dips').

    Pass 2a: loudnorm(measured) -> alimiter=limit=0.60:level=false (WAV).
    Pass 2b: measure post-limiter integrated; makeup = 0 if already at/above
             -13.5, else gain up to -13.5 (the hot DJI A bed dips ~0.55 dB
             through the limiter — quiet fixtures do not).
    Pass 2c: volume(makeup) -> AAC 320k/48k.
    """
    ln = ("loudnorm=I=-13.5:LRA=6.4:TP=-1.5:measured_I=%.3f:measured_LRA=%.3f:"
          "measured_TP=%.3f:measured_thresh=%.3f:offset=%.3f:linear=false"
          % (measured["input_i"], measured["input_lra"], measured["input_tp"],
             measured["input_thresh"], measured["target_offset"]))
    lim_wav = wav + ".lim.wav"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", wav,
           "-af", "%s,alimiter=limit=0.60:level=false" % ln,
           "-t", "%.6f" % total,
           "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", lim_wav]
    r = run(cmd)
    if r.returncode != 0:
        return False, (r.stderr or "")[-600:].strip() or "limiter stage failed"

    post = measure_integrated(lim_wav)
    makeup = 0.0
    if post is not None:
        makeup = max(0.0, -13.5 - post)  # never push above -13.5
    log("audio: post-limiter I=%.2f LUFS, makeup=+%.2f dB" % (post or 0.0, makeup))

    vol = ("volume=%.3fdB" % makeup) if makeup > 0.01 else "anull"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", lim_wav,
           "-af", vol,
           "-t", "%.6f" % total,
           "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-ac", "2", out_m4a]
    r = run(cmd)
    if r.returncode != 0:
        return False, (r.stderr or "")[-600:].strip() or "audio encode failed"
    try:
        os.remove(lim_wav)
    except OSError:
        pass
    return True, ""


def _round_half_up(x):
    """Deterministic rounding for scaled geometry.

    Python's round() is banker's rounding (round(0.5) == 0, round(1.5) == 2), so a
    resolved geometry value would depend on the parity of the operand. The graphics
    contract's reference table (1080p 150/40 -> 720p 100/27 -> 2160p 300/80) is
    round-half-up, so that is what this does, explicitly.
    """
    return int(math.floor(x + 0.5))


def list_frame_sequence(directory, pattern, first_index, count):
    """STRICT frame discovery -> ({index: filename}, error).

    Only <pattern> (%04d.png) matches. A bare *.png glob would also swallow macOS
    AppleDouble forks (._0000.png), thumbnails, preview sheets and unrelated PNGs:
    those enter the animation as garbage frames and the render still "succeeds".
    Fails closed on a count mismatch, a duplicate index, or a non-contiguous run.
    """
    rx = re.compile(re.escape(pattern).replace("%04d", r"(\d{4})") + r"$")
    found = {}
    for fn in sorted(os.listdir(directory)):
        m = rx.match(fn)
        if not m:
            continue
        idx = int(m.group(1))
        if idx in found:
            return None, "duplicate frame index %04d: %s and %s" % (idx, found[idx], fn)
        found[idx] = fn
    if len(found) != count:
        return None, ("expected %d frames matching %s in %s, found %d"
                      % (count, pattern, directory, len(found)))
    want = list(range(first_index, first_index + count))
    missing = [i for i in want if i not in found]
    extra = [i for i in sorted(found) if i not in set(want)]
    if missing or extra:
        return None, ("sequence is not contiguous %04d..%04d: %d missing (%s), %d out of range (%s)"
                      % (first_index, first_index + count - 1, len(missing),
                         ", ".join("%04d" % i for i in missing[:5]) or "-", len(extra),
                         ", ".join("%04d" % i for i in extra[:5]) or "-"))
    return found, ""


def resolve_graphics(registry, profile, graphics_root, out_w, out_h, enabled=True):
    """Resolve a graphics profile to a render plan -> (plan, error).

    plan = {input_args, ref, chain, overlay, provenance}. The chain is built from the
    profile's MEASURED content bbox and anchor, and the same chain and position are used
    for every frame of the program (a per-frame crop would make the mark jitter).
    enabled=False (CLI --no-graphics) yields a plan with no inputs, so the editorial
    timeline renders clean and byte-comparable.
    """
    gfx = registry.get("profiles", {}).get("graphics", {})
    if profile not in gfx:
        return None, "unknown graphics profile '%s' (registry knows: %s)" % (
            profile, ", ".join(sorted(gfx)) or "(none)")
    e = gfx[profile]
    asset_root = e.get("asset_root") or graphics_root
    frames = e.get("frames") or {}
    geo = e.get("geometry") or {}
    bbox = e.get("content_bbox")
    canvas = e.get("canvas")
    prov = {
        "profile": profile,
        "enabled": bool(enabled),
        "asset_root": asset_root,
        "asset_type": e.get("asset_type", "png_sequence" if frames else "static_png"),
        "frames": frames,
        "alpha": e.get("alpha"),
        "canvas": canvas,
        "content_bbox": bbox,
        "content_bbox_source": e.get("content_bbox_source"),
        "persistence": e.get("persistence"),
        "animation": e.get("animation"),
        "output_resolution": [out_w, out_h],
    }
    if not enabled:
        prov["note"] = "graphics disabled for this render (--no-graphics): same timeline, no overlay"
        return {"input_args": None, "ref": None, "chain": None, "overlay": None,
                "provenance": prov}, ""

    if frames:
        # ---- production path: strict PNG sequence
        sub = frames.get("dir") or ""      # frames may sit in a subdir of the asset root
        d = os.path.join(asset_root, sub) if sub else asset_root
        if not os.path.isdir(d):
            return None, "graphics profile '%s' frame dir is not a directory: %s" % (profile, d)
        pattern = frames.get("pattern", "%04d.png")
        first = int(frames.get("first_index", 0))
        count = int(frames.get("frame_count") or frames.get("count") or 0)
        if count <= 0:
            return None, "graphics profile '%s' declares no frame_count" % profile
        _seq, err = list_frame_sequence(d, pattern, first, count)
        if err:
            return None, "graphics profile '%s': %s" % (profile, err)
        fps = frames.get("fps", 30)
        prov["frames_verified"] = {"count": count, "first": first, "last": first + count - 1,
                                   "fps": fps, "loop_s": frames.get("loop_s")}
        # frame identity: hash over the verified listing (index, name, size) so a later render
        # can prove it burned the same frames even for a profile with no checksum manifest.
        h = hashlib.sha256()
        for i in sorted(_seq):
            h.update(("%04d %s %d\n" % (i, _seq[i], os.path.getsize(os.path.join(d, _seq[i])))).encode())
        prov["frame_sequence_sha256"] = h.hexdigest()
        cm = os.path.join(asset_root, "checksums.sha256")
        if os.path.exists(cm):
            prov["asset_checksum_manifest"] = cm
            prov["asset_checksum_manifest_sha256"] = hashlib.sha256(open(cm, "rb").read()).hexdigest()
        if os.path.exists(REGISTRY):
            prov["registry"] = REGISTRY
            prov["registry_sha256"] = hashlib.sha256(open(REGISTRY, "rb").read()).hexdigest()
        src_args = ["-framerate", str(fps), "-stream_loop", "-1", "-i", os.path.join(d, pattern)]
    else:
        # ---- legacy path: one pre-composed PNG (kept for fixtures; still bbox-driven)
        static = os.path.join(asset_root, "overlay.png")
        if not os.path.exists(static):
            return None, ("graphics profile '%s' declares no frames and has no overlay.png in %s"
                          % (profile, asset_root))
        log("graphics: profile '%s' uses the static overlay.png path (no frames declared)" % profile)
        src_args = ["-loop", "1", "-i", static]
        if not bbox:
            r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0", static])
            try:
                cw, ch = [int(v) for v in r.stdout.strip().split(",")[:2]]
                bbox = [0, 0, cw, ch]
                canvas = [cw, ch]
                prov["content_bbox"] = bbox
                prov["content_bbox_source"] = "no bbox declared: full static canvas used as-is"
            except Exception:
                return None, "graphics profile '%s': cannot probe %s" % (profile, static)

    # ---- geometry: content bbox -> target size -> anchor position
    if not bbox or len(bbox) != 4:
        return None, "graphics profile '%s' has no content_bbox [x,y,w,h]" % profile
    bx, by, bw, bh = [int(v) for v in bbox]
    ref_h = float(geo.get("reference_height", 1080))
    scale = float(out_h) / ref_h
    vw = _round_half_up(float(geo["visible_width_at_reference"]) * scale)
    vh = _round_half_up(vw * float(bh) / float(bw))
    anchor = geo.get("anchor", "bottom-right")
    if anchor.endswith("right"):
        x = out_w - vw - _round_half_up(float(geo.get("margin_right_at_reference", 0)) * scale)
    else:
        x = _round_half_up(float(geo.get("margin_left_at_reference", 0)) * scale)
    y = out_h - vh - _round_half_up(float(geo.get("margin_bottom_at_reference", 0)) * scale)

    parts = []
    if canvas and (bw, bh) != (int(canvas[0]), int(canvas[1])):
        parts.append("crop=%d:%d:%d:%d" % (bw, bh, bx, by))
    if (vw, vh) != (bw, bh):
        parts.append("scale=%d:%d" % (vw, vh))
    chain = ",".join(parts) if parts else "null"

    prov["geometry"] = {
        "anchor": anchor,
        "reference_height": ref_h,
        "scale": round(scale, 6),
        "visible_width_at_reference": geo.get("visible_width_at_reference"),
        "visible_size": [vw, vh],
        "margins": {k: v for k, v in geo.items() if k.startswith("margin_")},
        "resolved_x": x, "resolved_y": y,
        "filter_chain": chain,
        "overlay": "%d:%d" % (x, y),
        "rounding": "round-half-up",
    }
    expect = geo.get("rendered_size_at_reference")
    if expect and abs(out_h - ref_h) < 1e-9 and [vw, vh] != [int(v) for v in expect]:
        return None, ("graphics profile '%s': resolved visible size %dx%d != registry "
                      "rendered_size_at_reference %s" % (profile, vw, vh, expect))
    return {"input_args": src_args, "ref": "2:v", "chain": chain,
            "overlay": "%d:%d" % (x, y), "provenance": prov}, ""


def final_mux(video_only, audio_m4a, plan, total, out_partial, crf):
    """Graphics overlay + video re-encode + audio mux, -t total, +faststart.

    h264_metadata is a BITSTREAM filter — it belongs on the output stream
    (-bsf:v), never inside the filtergraph (filter_complex rejects it with
    'Invalid argument').

    Geometry comes from the resolved graphics plan (profile-driven). video_only is the
    ALREADY ASSEMBLED program, so the overlay is burned exactly once, after the whole
    editorial program exists — never per window. The overlay input is a looping sequence
    with no PTS reset: its phase is absolute program time, so it cannot restart at a
    camera cut, a window boundary or a chunk boundary.

    plan["input_args"] is None for a clean render (--no-graphics): same editorial
    timeline, no overlay, and the video is still re-encoded through the same path.
    """
    if not plan or plan.get("input_args") is None:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-i", video_only, "-i", audio_m4a,
               "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
               "-c:a", "copy",
               "-t", "%.6f" % total,
               "-movflags", "+faststart",
               "-bsf:v", "h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1",
               "-f", "mp4",  # .partial suffix has no format; be explicit
               out_partial]
    else:
        # the [bug] label attaches to the LAST filter of the profile's chain
        bug_chain = "%s[bug]" % plan["chain"]
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-i", video_only, "-i", audio_m4a] + plan["input_args"] + [
               "-filter_complex",
               "[0:v]format=yuv420p[main];[%s]%s;[main][bug]overlay=%s[v]"
               % (plan["ref"], bug_chain, plan["overlay"]),
               "-map", "[v]", "-map", "1:a",
               "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
               "-c:a", "copy",
               "-t", "%.6f" % total,
               "-movflags", "+faststart",
               "-bsf:v", "h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1",
               "-f", "mp4",  # .partial suffix has no format; be explicit
               out_partial]
    r = run(cmd)
    if r.returncode != 0:
        return False, (r.stderr or "")[-1200:].strip() or "final mux failed"
    return True, ""


def postflight_and_publish(partial, final_dir, final_name, total, force=False):
    """Call the Phase 1 hardening lib: postflight_verify + publish_partial.

    The lib runs postflight on the PARTIAL (creates <partial>.verified and
    ALLDONE beside it), then publishes. We then relocate the verified marker
    to the FINAL path so the next render's never-clobber guard sees it.

    Returns (ok, message, all_done_text).
    """
    final_path = os.path.join(final_dir, final_name)
    force_flag = "--force" if force else ""
    script = (
        "source %s\n"
        "postflight_verify '%s' %.3f --lufs -13.5 --tp -1.5 || exit 3\n"
        "publish_partial '%s' '%s' %s || exit 4\n"
    ) % (HARDENING, partial, total, partial, final_path, force_flag)
    r = run(["bash", "-c", script])
    if r.returncode != 0:
        return False, (r.stdout + r.stderr).strip()[-600:], ""
    # Relocate the verified marker + ALLDONE beside the FINAL (the lib wrote
    # them beside the partial; the never-clobber guard keys on the final).
    partial_verified = partial + ".verified"
    final_verified = final_path + ".verified"
    if os.path.exists(partial_verified):
        if os.path.exists(final_verified):
            os.remove(final_verified)
        # os.replace is rename(2) — EXDEV when work dir and finals live on
        # different filesystems (container disk -> NFS). shutil.move copies
        # cross-device and renames same-device (console path: work on LXC
        # disk, finals on /mnt/media NFS; test suite used same-FS --out so
        # it never caught this).
        shutil.move(partial_verified, final_verified)
    all_done = ""
    partial_ad = os.path.join(os.path.dirname(partial), "ALLDONE")
    final_ad = os.path.join(final_dir, "ALLDONE")
    if os.path.exists(partial_ad):
        with open(partial_ad) as f:
            all_done = f.read().strip()
        if partial_ad != final_ad:
            shutil.move(partial_ad, final_ad)
    return True, r.stdout.strip()[-600:], all_done


def append_postflight_to_manifest(manifest_path, payload):
    """Record the QC results in the COMPLETED job manifest (refined plan)."""
    try:
        with open(manifest_path) as f:
            text = f.read()
    except OSError:
        return False
    lines = ["postflight:"]
    for k, v in payload.items():
        if isinstance(v, dict):
            lines.append("  %s:" % k)
            for k2, v2 in v.items():
                if isinstance(v2, dict):
                    lines.append("    %s:" % k2)
                    for k3, v3 in v2.items():
                        lines.append("      %s: %s" % (k3, v3))
                else:
                    lines.append("    %s: %s" % (k2, v2))
        elif isinstance(v, bool):
            lines.append("  %s: %s" % (k, "true" if v else "false"))
        else:
            lines.append("  %s: %s" % (k, v))
    with open(manifest_path, "a") as f:
        f.write("\n# Phase 4 postflight record (written by render_final.py)\n")
        f.write("\n".join(lines) + "\n")
    return True


def parse_postflight(manifest_path):
    """Read the appended postflight block (for --json output)."""
    try:
        with open(manifest_path) as f:
            text = f.read()
        m = re.search(r"^postflight:\n((?:  .*\n?)*)", text, re.M)
        if not m:
            return {}
        out = {}
        for line in m.group(1).splitlines():
            if line.startswith("  ") and ":" in line:
                k, v = line.strip().split(":", 1)
                out[k] = v.strip()
        return out
    except Exception:
        return {}


def _curve_channels(spec):
    """{channel: [(x, y), ...]} for a curves= filter ('all=…' or 'r=…:g=…:b=…')."""
    ch = {}
    for name, body in re.findall(r"([rgb]|all)='([^']*)'", spec or ""):
        pts = []
        for pair in body.split():
            if "/" in pair:
                try:
                    x, y = pair.split("/")
                    pts.append((float(x), float(y)))
                except ValueError:
                    pass
        if pts:
            ch[name] = sorted(pts)
    return ch


def _shadow_gain(spec, x=0.05):
    """LARGEST per-channel output/input gain of a curves= filter at x (linear interpolation), or None.

    x=0.05 is the reference shadow point for this look (club_dispatch_pb3_v1 / _b_v1 both pin 0.05).
    Calibration: the approved per-channel trim measures 1.004 on its largest channel (G/B) and
    0.927 on R — the recorded net delivered gain — while a genuine shadow lift (the locked B grade,
    or the auto curve that duplicated it) measures 5.6. The 1.5 threshold therefore separates
    "camera match trim" from "shadow lift" with room on both sides.
    """
    if CM.is_time_varying(spec):
        # A time-varying match has no single curve. Evaluate EVERY control point and keep the
        # largest gain: a shadow lift anywhere in the reel must still trip the invariant, so this
        # check fails closed, never open.
        gains = [g for g in (_shadow_gain(c, x) for c in CM.curve_texts(spec)) if g is not None]
        return max(gains) if gains else None
    gains = []
    for name, pts in _curve_channels(spec).items():
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= x <= x1 and x1 > x0:
                gains.append((y0 + (y1 - y0) * (x - x0) / (x1 - x0)) / x)
                break
    return max(gains) if gains else None


def chain_report(registry, grade_name, man, segs, pre_grades, src_to_name, media_root):
    """Effective per-angle render chain + the camera-match invariant verdict (PRODUCTION LOCK v1).

    Reports the chain the renderer will ACTUALLY build (not the config names), and fails closed on:
      duplicate_shadow_lift      two active stages lifting the same shadows (camera match twice)
      duplicate_creative_grade   the same grade chain applied twice in one angle's chain
      unexpected_range_conversion  a range / colour conversion the delivery contract does not own
      missing_approved_camera_match  a non-reference angle with neither an approved match nor a
                                     dedicated camera-match grade
    """
    SHADOW_LIFT = 1.5          # a genuine shadow lift; the approved trim measures 0.927 here
    declared = man.get("pre_grades") or {}
    angles = {}
    for angle in ("A", "B"):
        srcs = sorted({src_to_name.get(s[0]) for s in segs if s[4] == angle
                       and src_to_name.get(s[0])})
        if not srcs:
            continue
        pre = None
        for src in srcs:
            if pre_grades.get(src):
                pre = pre_grades[src]
                break
        origin = ("manifest-approved" if any(src in declared for src in srcs)
                  else ("auto-derived" if pre else "none"))
        grade = grade_for(registry, grade_name, angle)
        dedicated = b_grade_name(registry, grade_name) if angle == "B" else None
        norm = "scale=%d:%d" % (FINAL_W, FINAL_H)
        if angle == "A":
            norm += "," + CROP_FOR(registry, man)
        match_stage = {"stage": "camera_match", "chain": pre, "origin": origin,
                       "sources": srcs}
        if CM.is_time_varying(pre):
            # A time-varying match has no single filter string. The stage still reports the RECORD
            # it read (chain), and adds what the renderer will actually do with it, so a chain dump
            # describes the real chain instead of a name.
            match_stage["resolved"] = CM.describe_match(pre)
        stages = [{"stage": "technical_normalization", "chain": norm,
                   "note": "scale/push-in only; no range or colour conversion"},
                  match_stage,
                  {"stage": "shared_grade", "chain": grade,
                   "registry_name": dedicated if (angle == "B" and dedicated) else grade_name},
                  {"stage": "output_conversion",
                   "chain": "fps=30000/1001,settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p"}]

        lifts = []
        for st in stages:
            g = _shadow_gain(st["chain"]) if st["chain"] else None
            if g is not None and g > SHADOW_LIFT:
                lifts.append({"stage": st["stage"], "shadow_gain_at_0.05": round(g, 3)})
        # Duplicate detection compares each stage's canonical identity, not the raw value: a
        # time-varying camera match is a mapping, not a string. For a static entry the identity IS
        # the chain string, so every existing comparison behaves exactly as before.
        ids = [CM.match_identity(st["chain"]) for st in stages if st["chain"]]
        dup_grade = len(ids) != len(set(ids))
        bad_range = [st["stage"] for st in stages if st["chain"]
                     and ("format=" in CM.chain_text(st["chain"])
                          and "format=yuv420p" not in CM.chain_text(st["chain"])
                          or re.search(r"(range=|in_range|out_range|colorspace=|format=rgb|format=gbr)",
                                       CM.chain_text(st["chain"]) or ""))]
        v = []
        if len(lifts) >= 2:
            v.append({"violation": "duplicate_shadow_lift", "stages": lifts,
                      "detail": "two active stages lift the same shadows: the camera match is being "
                                "applied more than once"})
        if dup_grade:
            v.append({"violation": "duplicate_creative_grade",
                      "detail": "the same chain appears twice in one angle's effective chain"})
        if bad_range:
            v.append({"violation": "unexpected_range_conversion", "stages": bad_range})
        if angle != "A" and not pre and not dedicated:
            v.append({"violation": "missing_approved_camera_match",
                      "detail": "angle %s has no approved pre-grade and no dedicated camera-match "
                                "grade — it would receive the reference camera's grade" % angle})
        angles[angle] = {"sources": srcs, "stages": stages, "shadow_lifts": lifts,
                         "camera_match_mode": CM.match_mode(pre), "violations": v}

    viol = [dict(a["violations"][i], angle=ang) for ang, a in angles.items()
            for i in range(len(a["violations"]))]
    return {"ok": not viol, "manifest": man.get("job_id"), "grade": grade_name,
            "declared_pre_grades": sorted(declared), "angles": angles,
            "auto_pre_grade_refused": (not declared and b_grade_name(registry, grade_name) is not None),
            "violations": viol, "rule": "a camera-match transform may occur exactly once per angle"}


def CROP_FOR(registry, man):
    return registry["profiles"]["crop"][man["profiles"]["crop"]]["locked_params"]


def main():
    ap = argparse.ArgumentParser(description="ISH D deterministic final renderer")
    ap.add_argument("manifest")
    ap.add_argument("--media-root", required=True)
    ap.add_argument("--out", default=None,
                    help="final mp4 (default /mnt/media/finals/<job_id>.mp4)")
    ap.add_argument("--work", default="/opt/video-studio/work/final")
    ap.add_argument("--graphics-root", default=DEFAULT_GRAPHICS_ROOT)
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--keep-temp", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="allow overwriting the last verified final (archives it)")
    ap.add_argument("--no-auto-pre-grade", action="store_true",
                    help="disable Phase 6 exposure pre-grade auto-derivation (default: on when the manifest has none)")
    ap.add_argument("--no-graphics", action="store_true",
                    help="CLEAN render: same editorial timeline, graphics edge not burned "
                         "(graphics profile is still recorded as disabled in provenance)")
    ap.add_argument("--dump-plan", action="store_true",
                    help="resolve the graphics plan (asset root, verified frame sequence, "
                         "geometry, filter chain, provenance) and exit without rendering")
    ap.add_argument("--dump-chain", action="store_true",
                    help="resolve the EFFECTIVE per-angle render chain (technical normalization, "
                         "camera match, shared grade, output conversion) and fail closed (rc 2) on a "
                         "duplicate transform, a missing camera match or a range conversion; renders "
                         "nothing")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.dump_chain:
        global _JSON_STDOUT
        _JSON_STDOUT = True

    ok, msg = validate_manifest(args.manifest, args.media_root)
    if not ok:
        log("manifest rejected: %s" % msg)
        sys.exit(1)

    import yaml
    with open(args.manifest) as f:
        man = yaml.safe_load(f)

    job_id = man["job_id"]
    out_path = args.out or os.path.join("/mnt/media/finals", job_id + ".mp4")
    final_dir = os.path.dirname(out_path)
    final_name = os.path.basename(out_path)
    os.makedirs(final_dir, exist_ok=True)
    work = os.path.join(args.work, job_id)
    os.makedirs(work, exist_ok=True)

    lock_fd = acquire_lock()
    if lock_fd is None:
        fail("render already in progress (lock %s held)" % LOCK_FILE, rc=2)

    registry = load_registry()
    grade_name = man["profiles"]["grade"]
    crop = registry["profiles"]["crop"][man["profiles"]["crop"]]["locked_params"]
    graphics_profile = man["profiles"]["graphics"]
    audio_profile = man["profiles"]["audio"]

    # ---- Graphics: resolve the profile ONCE, up front, and fail closed before any
    # encoding. A declared graphics profile that cannot be resolved to a verified
    # sequence + geometry is a defect, not a reason to silently ship a clean render.
    plan, err = resolve_graphics(registry, graphics_profile, args.graphics_root,
                                 FINAL_W, FINAL_H, enabled=not args.no_graphics)
    if err:
        fail(err, rc=2)
    if args.dump_plan:
        # stdout is pure JSON here: no info lines before the plan (machine-readable).
        print(json.dumps({"manifest": args.manifest, "graphics": plan["provenance"]}, indent=2))
        sys.exit(0)
    log("graphics: profile=%s enabled=%s asset_root=%s"
        % (graphics_profile, plan["provenance"]["enabled"], plan["provenance"]["asset_root"]))
    if plan["provenance"].get("frames_verified"):
        fv = plan["provenance"]["frames_verified"]
        log("graphics: frames %d..%d verified (%d frames @ %s fps, %.3fs loop)"
            % (fv["first"], fv["last"], fv["count"], fv["fps"], fv["loop_s"]))
    if plan["chain"]:
        log("graphics: chain=%s overlay=%s visible=%s"
            % (plan["chain"], plan["overlay"], plan["provenance"].get("geometry", {}).get("visible_size")))

    a_path = os.path.join(args.media_root, man["sources"]["a_reel"])
    b_names = man["sources"].get("b_reel", [])
    pre_grades = man.get("pre_grades") or {}
    src_to_name = {os.path.join(args.media_root, n): n
                   for n in ([man["sources"]["a_reel"]] + b_names)}
    if not pre_grades and not args.no_auto_pre_grade:
        prep = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "exposure", "prepare_grades.py")
        r = run([VENV_PY, prep, "--manifest", args.manifest,
                 "--media-root", args.media_root, "--json"])
        try:
            parsed = json.loads(r.stdout) if r.stdout.strip() else {}
        except Exception:
            parsed = {}
        if r.returncode == 0 and parsed.get("ok"):
            auto = parsed.get("pre_grades") or {}
            # Per-angle camera-match override (2026-08-27 decision doc, point 4: "a hand-set
            # per-source grade ... wins over the auto-pre-grade for that source"). When the
            # registry defines a DEDICATED camera-match grade for the B angle (<base>_b_v1),
            # that chain already IS the derived correction — grade_for() applies it to every
            # B cut. An auto-derived pre-grade on top lifts the same shadows twice. Measured
            # on club-dispatch-set01: B inserts reached mean luma 135.5 / p95 204 with 0.7%
            # new highlight clipping instead of the accepted 28.5 / 76 / 0.01%.
            if auto:
                dedicated = b_grade_name(registry, grade_name)
                held = sorted(k for k in auto if k in b_names) if dedicated else []
                for k in held:
                    auto.pop(k, None)
                if held:
                    log("AUTO PRE-GRADE SKIPPED for %s: angle B owns the camera-match grade "
                        "'%s' (applying both would lift B twice)" % (", ".join(held), dedicated))
            if auto:
                pre_grades = dict(auto)
                log("AUTO PRE-GRADE: %s" % ", ".join("%s=%s" % (k, v) for k, v in auto.items()))
    reel = None
    if b_names:
        reel, err = resolve_b_reel(b_names, args.media_root)
        if err:
            fail(err, rc=2)

    segs, total = build_segments(man["timeline"], a_path, reel)
    if segs is None:
        fail(total, rc=1)

    if args.dump_chain:
        # PRODUCTION LOCK v1: inspect the EFFECTIVE resolved chain (never the config names) and
        # fail closed. Prints pure JSON; rc 2 when any invariant is violated.
        rep = chain_report(registry, grade_name, man, segs, pre_grades, src_to_name, args.media_root)
        print(json.dumps(rep, indent=2))
        sys.exit(0 if rep["ok"] else 2)

    # ---- Stage 3: video segments (1080p30 uniform yuv420p, grade + A push-in)
    UNIFORM = "fps=30000/1001,settb=AVTB,setpts=PTS-STARTPTS"

    def segment_vf(pre, crop_chain, grade_chain, angle):
        """Filter graph for ONE pre-grade entry (a static string, or one chunk's interpolated curve).

        Identical string for a static entry to the single-segment form this replaces.
        """
        pre_part = (pre + ",") if pre else ""
        if angle == "A":
            return "scale=%d:%d,%s%s,%s,%s,format=yuv420p" % (FINAL_W, FINAL_H, pre_part,
                                                              crop_chain, grade_chain, UNIFORM)
        return "scale=%d:%d,%s%s,%s,format=yuv420p" % (FINAL_W, FINAL_H, pre_part,
                                                       grade_chain, UNIFORM)

    seg_paths = []
    proc_bounds = []
    n = 0
    for (src, l_in, l_out, abs_off, angle) in segs:
        n += 1
        grade = grade_for(registry, grade_name, angle)
        pre = pre_grades.get(src_to_name.get(src, ""))
        if CM.is_time_varying(pre):
            # The source's exposure drifts, so no single curve holds the look across a whole
            # segment: render it in frame-aligned chunks, each carrying the curve interpolated at
            # the midpoint of its OWN source window. Every chunk is its OWN file — the overwrite
            # defect that tools/tests/test_render_segment_integrity.py guards against — and each
            # chunk contributes exactly one concat entry. The chunk joins are processing
            # boundaries, not timeline cuts, and are reported as such.
            chunks = CM.tv_chunks(pre, l_in, l_out, abs_off, FINAL_FPS,
                                  float(pre.get("max_chunk_s") or 30.0))
            if not chunks:
                fail("time-varying camera match produced no chunks for segment %d" % n, rc=2)
            for j, ch in enumerate(chunks):
                seg_path = os.path.join(work, "seg_%03d_c%03d.mp4" % (n, j + 1))
                vf = segment_vf(ch["curve"], crop, grade, angle)
                ok, err = render_video_segment(src, ch["l_in"], ch["l_out"], vf, seg_path,
                                               args.crf, frames=ch["frames"])
                if not ok:
                    fail("segment %s chunk %d encode failed: %s" % (seg_path, j + 1, err), rc=2)
                seg_paths.append(seg_path)
                if j:
                    proc_bounds.append(round(ch["abs_off"], 6))
            log("seg %d/%d: time-varying %s (%d chunks, %.2fs)"
                % (n, len(segs), os.path.basename(seg_path), len(chunks), l_out - l_in))
            continue
        seg_path = os.path.join(work, "seg_%03d.mp4" % n)
        vf = segment_vf(pre, crop, grade, angle)
        n_frames = (int(round((abs_off + (l_out - l_in)) * FINAL_FPS))
                    - int(round(abs_off * FINAL_FPS)))
        ok, err = render_video_segment(src, l_in, l_out, vf, seg_path, args.crf,
                                       frames=n_frames)
        if not ok:
            fail("segment %s encode failed: %s" % (seg_path, err), rc=2)
        seg_paths.append(seg_path)
        log("seg %d/%d: %s (%.2fs)" % (n, len(segs), os.path.basename(seg_path), l_out - l_in))

    if proc_bounds:
        pb_path = os.path.join(work, "processing_boundaries.json")
        json.dump({"processing_boundaries_s": sorted(proc_bounds),
                   "n_processing_boundaries": len(proc_bounds)}, open(pb_path, "w"), indent=1)
        log("PROCESSING BOUNDARIES (%d, time-varying chunk joins, not cuts) -> %s"
            % (len(proc_bounds), pb_path))

    # ---- Stage 4: concat -c copy
    out_video = os.path.join(work, "video_only.mp4")
    ok, err = concat_video(seg_paths, out_video)
    if not ok:
        fail("concat failed: %s" % err, rc=2)
    log("concat OK: %.2fs" % total)

    # ---- Stage 5: A audio bed -> two-pass loudnorm -> locked chain -> AAC 320k
    bed = os.path.join(work, "a_bed.wav")
    ok, err = build_audio_bed(a_path, total, bed)
    if not ok:
        fail("audio bed failed: %s" % err, rc=2)
    measured = loudnorm_measure(bed)
    if measured is None:
        fail("loudnorm pass 1 failed to measure A bed", rc=2)
    log("loudnorm measured: I=%.2f LRA=%.2f TP=%.2f" % (
        measured.get("input_i", 0), measured.get("input_lra", 0), measured.get("input_tp", 0)))
    out_audio = os.path.join(work, "audio.m4a")
    ok, err = encode_audio_chain(bed, total, out_audio, measured)
    if not ok:
        fail("audio chain failed: %s" % err, rc=2)
    log("audio chain OK (AAC 320k, %s profile)" % audio_profile)

    # ---- Stage 6: graphics overlay + final mux (plan resolved up front, fail-closed)
    partial = os.path.join(work, job_id + ".partial")
    ok, err = final_mux(out_video, out_audio, plan, total, partial, args.crf)
    if not ok:
        fail("final mux failed: %s" % err, rc=2)
    log("final mux OK -> partial")

    # ---- Stage 7: postflight + atomic publish (Phase 1 gates)
    ok, err, all_done = postflight_and_publish(partial, final_dir, final_name, total, args.force)
    if not ok:
        fail("postflight/publish failed: %s" % err, rc=2)

    # ---- Stage 8: record QC + graphics provenance in the completed manifest
    #
    # The graphics provenance goes to its OWN artifact, named after the DELIVERED FILE
    # (<delivered>.mp4.graphics.json), never after the job alone: a branded and a clean render
    # of one job would otherwise overwrite each other's provenance and the branded record would
    # be lost. It does not go into the manifest's postflight block either — that block is a
    # closed schema, and its narrow text appender turns a nested dict with an unquoted
    # `%04d.png` into malformed YAML (found both the hard way).
    import datetime
    gprov = dict(plan["provenance"])
    gprov["render"] = {"job_id": job_id, "delivered_file": out_path,
                       "rendered_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                       "program_duration_s": round(total, 3), "segments": len(segs),
                       "graphics_chain": plan["chain"], "graphics_overlay_xy": plan["overlay"]}
    gpath = out_path + ".graphics.json"
    with open(gpath, "w") as gf:
        json.dump(gprov, gf, indent=2)
    log("graphics provenance -> %s" % gpath)

    # The manifest's postflight block is a CLOSED schema ("manifest constrains, never expands"):
    # adding graphics_* keys makes the finished manifest fail re-validation, and a nested dict
    # with an unquoted %04d.png makes it malformed YAML. So the manifest records only what it
    # always recorded; every graphics fact goes in the sibling provenance artifact above.
    payload = {
        "status": "passed",
        "checked_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "integrated_lufs": None,
        "true_peak_dbtp": None,
        "delivered_file": out_path,
    }
    if pre_grades:
        # A time-varying entry is a RECORD, not a curve string: record what it is and its hash
        # rather than dumping ten interpolated LUTs into the manifest's YAML postflight block.
        payload["pre_grades_applied"] = {
            name: ({"curve": curve} if not CM.is_time_varying(curve)
                   else {"mode": "time_varying",
                         "control_points": len(curve["points"]),
                         "max_chunk_s": curve.get("max_chunk_s"),
                         "interpolation": curve.get("interpolation"),
                         "curve_sha256": CM.match_sha256(curve)})
            for name, curve in pre_grades.items()}
    m = re.search(r"LUFS=([-\d.]+)\s+TP=([-\d.]+)", all_done)
    if m:
        payload["integrated_lufs"] = float(m.group(1))
        payload["true_peak_dbtp"] = float(m.group(2))
    append_postflight_to_manifest(args.manifest, payload)

    if not args.keep_temp:
        for p in seg_paths:
            try:
                os.remove(p)
            except OSError:
                pass
        for p in (out_video, out_audio, bed):
            try:
                os.remove(p)
            except OSError:
                pass

    if args.json:
        print(json.dumps({"ok": True, "job_id": job_id, "output": out_path,
                          "total_s": round(total, 3),
                          "segments": len(segs), "alldone": all_done}))
    else:
        log("FINAL OK: %s (%.2fs, %d segments)" % (out_path, total, len(segs)))
        log(all_done)
    sys.exit(0)


if __name__ == "__main__":
    main()
