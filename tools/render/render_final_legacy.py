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
  6. graphics: cd_bug_v9 overlay                 (crop=152:152:71:505, overlay=72:904)
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
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_DIR = os.path.normpath(os.path.join(HERE, "..", "manifest"))
VALIDATOR = os.path.join(MANIFEST_DIR, "validate_manifest.py")
REGISTRY = os.path.join(MANIFEST_DIR, "profile_registry.json")
VENV_PY = "/opt/video-studio/tools/venv/bin/python"
LOCK_FILE = "/opt/video-studio/.render.lock"
HARDENING = "/opt/video-studio/tools/lib/hardening.sh"
DEFAULT_GRAPHICS_ROOT = "/opt/video-studio/assets/graphics"

# Locked creative params (registry is the source of truth; these are the
# renderer-side constants the registry note says the renderer owns).
BUG_CROP = "152:152:71:505"   # cd_bug_v9 disc bbox (bake_v6 03:35 fix)
BUG_POS = "72:904"            # bottom-left, fully in-frame at 1080p
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def log(msg):
    print(msg, flush=True)


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


def grade_for(registry, grade_name, angle):
    """Renderer rule (2026-08-27): B-cuts use the <base>_b_v1 variant when the
    registry defines it, otherwise the base grade. Variant naming convention:
    base WITHOUT the version suffix + _b_v1 (e.g. club_dispatch_pb3_v1 ->
    club_dispatch_pb3_b_v1). Both spellings are tried so a future variant name
    like <full>_b_v1 also resolves.
    """
    grades = registry["profiles"]["grade"]
    if angle == "B":
        base = re.sub(r"_v\d+$", "", grade_name)
        for cand in (grade_name + "_b_v1", base + "_b_v1"):
            if cand in grades:
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


def render_video_segment(src, l_in, l_out, vf, seg_path, crf):
    dur = l_out - l_in
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", "%.6f" % l_in, "-i", src,
           "-t", "%.6f" % dur,
           "-vf", vf,
           "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
           "-an", seg_path]
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


def graphics_asset(profile, graphics_root):
    """Resolve a graphics profile to an overlay source.

    cd_bug_v9 -> <graphics_root>/cd_bug_v9/  (%04d.png sequence preferred,
    overlay.png fallback for tests). Returns (input_args, overlay_ref,
    needs_crop) or (None, None, False) if the profile has no overlay.
    """
    d = os.path.join(graphics_root, profile)
    seq = os.path.join(d, "%04d.png")
    static = os.path.join(d, "overlay.png")
    if os.path.isdir(d) and (os.path.exists(os.path.join(d, "0001.png")) or os.path.exists(static)):
        # The canonical bug is a full-frame 1280x720 sequence (needs crop to
        # the 152x152 disc bbox); a pre-cropped overlay.png is used as-is.
        needs_crop = True
        src = seq
        if os.path.exists(os.path.join(d, "0001.png")):
            args = ["-framerate", "30", "-stream_loop", "-1", "-i", seq]
        else:
            args = ["-loop", "1", "-i", static]
            src = static
        r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", src])
        if r.returncode == 0 and r.stdout.strip().startswith("152,152"):
            needs_crop = False
        return (args, "2:v", needs_crop)
    return None, None, False


def final_mux(video_only, audio_m4a, overlay_args, overlay_ref, needs_crop, total, out_partial, crf):
    """Graphics overlay + video re-encode + audio mux, -t total, +faststart.

    h264_metadata is a BITSTREAM filter — it belongs on the output stream
    (-bsf:v), never inside the filtergraph (filter_complex rejects it with
    'Invalid argument').
    """
    if overlay_args is None:
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
        crop_chain = ("crop=%s[bug]" % BUG_CROP) if needs_crop else ("null[bug]" % ())
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-i", video_only, "-i", audio_m4a] + overlay_args + [
               "-filter_complex",
               "[0:v]format=yuv420p[main];[%s]%s;[main][bug]overlay=%s[v]"
               % (overlay_ref, crop_chain, BUG_POS),
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
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

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

    # ---- Stage 3: video segments (1080p30 uniform yuv420p, grade + A push-in)
    UNIFORM = "fps=30000/1001,settb=AVTB,setpts=PTS-STARTPTS"
    seg_paths = []
    n = 0
    for (src, l_in, l_out, abs_off, angle) in segs:
        n += 1
        seg_path = os.path.join(work, "seg_%03d.mp4" % n)
        grade = grade_for(registry, grade_name, angle)
        pre = pre_grades.get(src_to_name.get(src, ""))
        pre_part = (pre + ",") if pre else ""
        if angle == "A":
            vf = "scale=1920:1080,%s%s,%s,%s,format=yuv420p" % (pre_part, crop, grade, UNIFORM)
        else:
            vf = "scale=1920:1080,%s%s,%s,format=yuv420p" % (pre_part, grade, UNIFORM)
        ok, err = render_video_segment(src, l_in, l_out, vf, seg_path, args.crf)
        if not ok:
            fail("segment %s encode failed: %s" % (seg_path, err), rc=2)
        seg_paths.append(seg_path)
        log("seg %d/%d: %s (%.2fs)" % (n, len(segs), os.path.basename(seg_path), l_out - l_in))

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

    # ---- Stage 6: graphics overlay + final mux
    overlay_args, overlay_ref, needs_crop = graphics_asset(graphics_profile, args.graphics_root)
    if overlay_args is None:
        # The manifest REQUIRES a graphics profile (schema: profiles.graphics).
        # A missing asset must fail loudly — a silent no-bug final is a defect.
        fail("graphics profile '%s' has no asset in %s (need %%04d.png seq or overlay.png)"
             % (graphics_profile, os.path.join(args.graphics_root, graphics_profile)), rc=2)
    partial = os.path.join(work, job_id + ".partial")
    ok, err = final_mux(out_video, out_audio, overlay_args, overlay_ref, needs_crop, total, partial, args.crf)
    if not ok:
        fail("final mux failed: %s" % err, rc=2)
    log("final mux OK -> partial")

    # ---- Stage 7: postflight + atomic publish (Phase 1 gates)
    ok, err, all_done = postflight_and_publish(partial, final_dir, final_name, total, args.force)
    if not ok:
        fail("postflight/publish failed: %s" % err, rc=2)

    # ---- Stage 8: record QC in the completed manifest
    import datetime
    payload = {
        "status": "passed",
        "checked_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "integrated_lufs": None,
        "true_peak_dbtp": None,
        "delivered_file": out_path,
    }
    if pre_grades:
        payload["pre_grades_applied"] = {name: {"curve": curve}
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
