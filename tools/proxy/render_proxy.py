#!/usr/bin/env python3
"""render_proxy.py — Phase 3 visual approval proxy (ISH D template engine).

Consumes a Phase-2-validated manifest and renders a LOW-RES VAAPI proxy with
burned-in timeline timecode, cut IDs (CUT 018), camera labels (A-CAM / B-CAM)
and optional LOW CONF markers, plus the real continuous A audio bed.

This is the FIRST manifest consumer: the same manifest the final renderer
(Phase 4) will use. No throwaway proxy workflow.

Gate order:
  1. Manifest validation via validate_manifest.py (--media-root --json).
     rc 1 = invalid manifest, rc 2 = error. Proxy never touches an invalid job.
  2. Single-render lock (flock on /opt/video-studio/.render.lock) — one render
     at a time on the 4-vCPU studio.
  3. HALF-RUNTIME proof policy (user directive 2026-08-27): the proxy is a
     review artifact, not the export. By default the timeline is truncated to
     total/2 — segments past the midpoint are skipped and the straddling
     segment is trimmed. --full or --max-duration N overrides.
  4. VAAPI per-segment encode (uniform: 30000/1001, settb=AVTB,
     setpts=PTS-STARTPTS, format=yuv420p) then concat -c copy (all segments
     identical params -> safe; matches the runbook uniform rule).
  5. A audio bed: a_reel audio 0..rendered_total (apad silence if the reel is
     short), AAC 192k. Real continuous A audio, B fully muted.
  6. Verify: ffprobe duration within +-1.5s of rendered total, moov present,
     video + audio streams. Fail loudly otherwise.

Usage:
  python3 render_proxy.py JOB.yaml --media-root /path/to/mix \
      [--out /mnt/media/proofs/<job_id>_proof.mp4] [--res 960x540] \
      [--quality 30] [--work /opt/video-studio/work/proxy] \
      [--keep-temp] [--json] [--full | --max-duration N]

Exit: 0 = proxy rendered + verified; 1 = manifest invalid; 2 = error.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_DIR = os.path.normpath(os.path.join(HERE, "..", "manifest"))
VALIDATOR = os.path.join(MANIFEST_DIR, "validate_manifest.py")
REGISTRY = os.path.join(MANIFEST_DIR, "profile_registry.json")
VENV_PY = "/opt/video-studio/tools/venv/bin/python"
LOCK_FILE = "/opt/video-studio/.render.lock"
VAAPI_DEVICE = "/dev/dri/renderD128"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
GRADE_PB3 = "eq=brightness=-0.02:saturation=0.92,colorbalance=rs=0.0:bs=0.0:rm=0.0"
# NOTE: runtime grade comes from profile_registry.json (club_dispatch_pb3_v1 =
# approved neutral chain, 2026-08-25). This constant is a documented fallback,
# kept in sync so a broken registry can never silently re-introduce the
# rejected warm look (rs=0.09:bs=-0.06).
PUSHIN_REL = "crop=iw*0.87:ih*0.87"  # 1.15x push-in at proxy res (locked intent)


def log(msg):
    print(msg, flush=True)


def fail(msg, rc=2):
    log(f"ERROR: {msg}")
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
    """Returns (ok, message). Gate 1: the Phase 2 validator."""
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


def dt(text, x_expr, y, size=26, color="white"):
    """One drawtext filter. x_expr is a literal x=... expression."""
    return (f"drawtext=fontfile={FONT}:text='{text}':{x_expr}:y={y}:"
            f"fontsize={size}:fontcolor={color}:borderw=2:bordercolor=black")


def segment_filters(res, grade, pushin, abs_off, cam_label, cut_label, low_conf, pre_grade=None):
    w, h = res.split("x")
    chain = []
    if pushin:
        chain.append(f"scale={w}:{h},{PUSHIN_REL},scale={w}:{h}")
    else:
        chain.append(f"scale={w}:{h}")
    if pre_grade:
        chain.append(pre_grade)
    chain.append(grade)
    chain.append("fps=30000/1001,settb=AVTB,setpts=PTS-STARTPTS+%.6f/TB" % abs_off)
    chain.append(dt("TC %{pts\\:hms}", "x=16", 12, size=30, color="white"))
    chain.append(dt("CUT %s" % cut_label, "x=w-tw-16", 12, size=30, color="yellow"))
    chain.append(dt(cam_label, "x=16", "h-44", size=26, color="lime"))
    if low_conf:
        chain.append(dt("LOW CONF", "x=w-tw-16", "h-44", size=26, color="red"))
    chain.append("setpts=PTS-STARTPTS,format=nv12,hwupload")
    return ",".join(chain)


def render_segment(src, local_in, local_out, abs_off, vf, seg_path, quality):
    dur = local_out - local_in
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-vaapi_device", VAAPI_DEVICE,
           "-ss", "%.6f" % local_in, "-i", src,
           "-t", "%.6f" % dur,
           "-vf", vf,
           "-c:v", "h264_vaapi", "-global_quality", str(quality),
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


def build_audio_bed(a_reel, total, out_audio):
    if ffprobe_has_audio(a_reel):
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", "0", "-i", a_reel,
               "-vn",  # CRITICAL: audio-only — decoding 4K video to extract
                       # audio burns 4 cores for 15+ min (runbook lesson).
               "-af", "apad",
               "-t", "%.6f" % total,
               "-c:a", "aac", "-b:a", "192k", "-ac", "2", out_audio]
    else:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
               "-t", "%.6f" % total,
               "-c:a", "aac", "-b:a", "192k", "-ac", "2", out_audio]
    r = run(cmd)
    if r.returncode != 0:
        return False, r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "audio failed"
    return True, ""


def mux(out_video, out_audio, out_path, total):
    r = run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", out_video, "-i", out_audio,
             "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart",
             "-t", "%.6f" % total, out_path])
    if r.returncode != 0:
        return False, r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "mux failed"
    return True, ""


def verify(out_path, expected_total):
    dur = ffprobe_duration(out_path)
    if dur is None:
        return False, "ffprobe cannot read output (no moov?)"
    if abs(dur - expected_total) > 1.5:
        return False, "duration %.2fs != expected %.2fs (+-1.5s)" % (dur, expected_total)
    r = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", out_path])
    types = [t for t in r.stdout.split() if t]
    if "video" not in types:
        return False, "output has no video stream"
    if "audio" not in types:
        return False, "output has no audio stream (A bed missing)"
    return True, "ok (%.2fs, v+a)" % dur


def resolve_b_reel(b_files, media_root):
    """Return list of (path, start, end) over the concatenated virtual reel."""
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
    """Return (segments, total). Each segment:
    (src, local_in, local_out, abs_off, angle, cut_label, low_conf)"""
    segs = []
    total = 0.0
    for cut in cuts:
        dur = cut["source_out"] - cut["source_in"]
        cut_label = cut["id"][4:]  # cut_018 -> 018
        low_conf = cut.get("confidence") == "low"
        if cut["angle"] == "A":
            segs.append((a_path, cut["source_in"], cut["source_out"],
                         total, "A", cut_label, low_conf))
        else:
            placed = False
            for (path, fstart, fend) in reel:
                seg_in = max(cut["source_in"], fstart)
                seg_out = min(cut["source_out"], fend)
                if seg_out > seg_in:
                    segs.append((path, seg_in - fstart, seg_out - fstart,
                                 total + (seg_in - cut["source_in"]),
                                 "B", cut_label, low_conf))
                    placed = True
            if not placed:
                return None, "cut %s source span not in b_reel" % cut["id"]
        total += dur
    return segs, total


def truncate_segments(segs, total, target):
    """Trim the segment list so the proxy ends at target timeline seconds.

    Segments past the midpoint are skipped; the straddling segment is trimmed
    so the render ends exactly at target. Returns (new_segs, new_total).
    """
    if target is None or target <= 0 or target >= total:
        return segs, total
    out = []
    acc = 0.0
    for (src, l_in, l_out, abs_off, angle, cut_label, low_conf) in segs:
        dur = l_out - l_in
        if acc + dur <= target:
            out.append((src, l_in, l_out, abs_off, angle, cut_label, low_conf))
            acc += dur
        elif acc < target:
            keep = target - acc
            out.append((src, l_in, l_in + keep, abs_off, angle, cut_label, low_conf))
            acc = target
            break
        else:
            break
    return out, acc


def main():
    ap = argparse.ArgumentParser(description="ISH D visual approval proxy")
    ap.add_argument("manifest")
    ap.add_argument("--media-root", required=True,
                    help="dir containing a_reel / b_reel files")
    ap.add_argument("--out", default=None,
                    help="output mp4 (default /mnt/media/proofs/<job_id>_proof.mp4)")
    ap.add_argument("--res", default="960x540")
    ap.add_argument("--quality", type=int, default=30)
    ap.add_argument("--work", default="/opt/video-studio/work/proxy")
    ap.add_argument("--keep-temp", action="store_true")
    ap.add_argument("--full", action="store_true",
                    help="render the FULL timeline (default is half-runtime proof)")
    ap.add_argument("--max-duration", type=float, default=None,
                    help="render only the first N seconds (default: total/2)")
    ap.add_argument("--no-auto-pre-grade", action="store_true",
                    help="disable Phase 6 exposure pre-grade auto-derivation (default: on when the manifest has none)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ok, msg = validate_manifest(args.manifest, args.media_root)
    if not ok:
        if args.json:
            print(json.dumps({"ok": False, "stage": "validate", "error": msg}))
        else:
            fail("manifest rejected: %s" % msg, rc=1)
        sys.exit(1 if not ok else 0)

    import yaml
    with open(args.manifest) as f:
        man = yaml.safe_load(f)

    job_id = man["job_id"]
    out_path = args.out or os.path.join("/mnt/media/proofs", job_id + "_proof.mp4")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    work = os.path.join(args.work, job_id)
    os.makedirs(work, exist_ok=True)

    # Gate 2: single-render lock
    lock_fd = acquire_lock()
    if lock_fd is None:
        fail("render already in progress (lock %s held)" % LOCK_FILE, rc=2)

    registry = load_registry()
    grade_name = man["profiles"]["grade"]
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

    full_total = total
    if args.full:
        target = full_total
    elif args.max_duration is not None:
        target = min(args.max_duration, full_total)
    else:
        target = full_total / 2.0  # half-runtime proof policy (2026-08-27)
    segs, total = truncate_segments(segs, full_total, target)
    if not segs:
        fail("truncation left no segments (target %.3fs of %.3fs)" % (target, full_total), rc=2)

    seg_paths = []
    n = 0
    for (src, l_in, l_out, abs_off, angle, cut_label, low_conf) in segs:
        n += 1
        seg_path = os.path.join(work, "seg_%s_%02d.mp4" % (cut_label, n))
        cam = "A-CAM" if angle == "A" else "B-CAM"
        pushin = (angle == "A")  # push-in is an A-cam look
        grade = grade_for(registry, grade_name, angle)
        pre = pre_grades.get(src_to_name.get(src, ""))
        vf = segment_filters(args.res, grade, pushin, abs_off, cam, cut_label, low_conf, pre)
        ok, err = render_segment(src, l_in, l_out, abs_off, vf, seg_path, args.quality)
        if not ok:
            fail("segment %s encode failed: %s" % (seg_path, err), rc=2)
        seg_paths.append(seg_path)

    out_video = os.path.join(work, "video.mp4")
    ok, err = concat_video(seg_paths, out_video)
    if not ok:
        fail("concat failed: %s" % err, rc=2)

    out_audio = os.path.join(work, "audio.m4a")
    ok, err = build_audio_bed(a_path, total, out_audio)
    if not ok:
        fail("audio bed failed: %s" % err, rc=2)

    ok, err = mux(out_video, out_audio, out_path, total)
    if not ok:
        fail("mux failed: %s" % err, rc=2)

    ok, err = verify(out_path, total)
    if not ok:
        fail("verification failed: %s" % err, rc=2)

    if not args.keep_temp:
        for p in seg_paths:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.remove(out_video)
            os.remove(out_audio)
            os.remove(os.path.join(work, "concat.txt"))
        except OSError:
            pass

    if args.json:
        print(json.dumps({"ok": True, "job_id": job_id, "output": out_path,
                          "segments": len(segs), "total_s": round(total, 3),
                          "full_total_s": round(full_total, 3)}))
    else:
        log("PROXY OK: %s (%d segment(s), %.2fs total)" % (out_path, len(segs), total))
    sys.exit(0)


if __name__ == "__main__":
    main()
