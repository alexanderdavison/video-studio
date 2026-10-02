#!/usr/bin/env python3
"""qc_proof.py — QC a rendered review proof against the basis it was rendered from.

Basis-driven, so the same tool checks the first-pass program, a coverage-revised program, or any
future N-window job: expected cuts, insert lengths, angles, boundaries and the audio offset all come
from the basis, never from constants.

    qc_proof.py --basis combined_proof_basis.json --proof reviewproof_review_audiosync.mp4
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

import numpy as np

W, H = 160, 90
VIEW_DIFF = 8.0      # 160x90 luma mean-abs-diff above which a sample is a candidate view change
VIEW_CORR = 0.5      # below which the before/after pair is a different camera, not motion
PROBE_S = 509.6      # audio-alignment probe point (seconds into the program)


def frame(src, t):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % t, "-i", src, "-frames:v", "1",
                          "-vf", "scale=%d:%d,format=gray" % (W, H), "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw[:W * H], dtype=np.uint8).astype(np.float32)


def series(src, t0, dur, fps=2.0):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % t0, "-t", "%.3f" % dur, "-i", src,
                          "-vf", "fps=%g,scale=%d:%d,format=gray" % (fps, W, H),
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    n = len(raw) // (W * H)
    a = np.frombuffer(raw[:n * W * H], dtype=np.uint8).reshape(n, H, W).astype(np.float32)
    return a, t0, 1.0 / fps


def corr(x, y):
    # a reference frame can legitimately be missing (before the virtual reel starts); return
    # nan rather than crashing, and let the caller decide what that means
    if x is None or y is None or getattr(x, "size", 0) == 0 or getattr(y, "size", 0) == 0:
        return float("nan")
    x = x - x.mean(); y = y - y.mean()
    d = np.sqrt((x * x).sum() * (y * y).sum())
    return float((x * y).sum() / d) if d else 0.0


def same_source_evidence(c_own_before, c_other_before, c_own_after, c_other_after):
    """True when BOTH samples of a detected change still match the SAME source as the expected angle.

    A spike can be motion inside one camera: the sampling pair correlates poorly with itself while
    each sample still matches the expected manifest source and beats the other one. That is not a
    view change. A real A<->B change leaves one sample on the other angle, so it fails here and is
    never suppressed.
    """
    for own, other in ((c_own_before, c_other_before), (c_own_after, c_other_after)):
        if own != own:
            return False
        if other != other:
            continue
        if not (own > other):
            return False
    return True


def attributable(t, cuts, tol, step):
    """Attribute a detected change to a planned cut.

    The detector samples at `step` (1/fps) and reports a change at the sampled frame where it first
    becomes observable, so a change at t represents a transition inside (t - step, t]. Either the
    point tolerance matches, or the planned cut lies inside that detector interval. This is an
    attribution correction for the resolution of the measurement, not a relaxed tolerance.
    """
    return any(abs(t - e) <= tol or (t - step < e <= t) for e in cuts)


def closes_in_a(corr_a, corr_b):
    """Program close: A must win, but an UNAVAILABLE B reference is N/A, never a NaN comparison.

    Returns (ok, b_available). With no B coverage at the probed program time the accepted A-only
    criterion is used (the same one check 5 applies before the reel starts). No valid A evidence
    either is a refusal.
    """
    b_available = (corr_b == corr_b)
    if corr_a != corr_a:
        return False, b_available
    if not b_available:
        return corr_a > 0.0, False
    return corr_a > corr_b, True


def build_b_reel(cards, single_b):
    """The B virtual reel's cards, in order, as (path, start, end)."""
    names = list(cards or [])
    if not names:
        names = ["/mnt/media/raw/B CAM/DJI_20260824204627_0038_D.MP4" if single_b is None else single_b]
    out, off = [], 0.0
    for n in names:
        p = n if os.path.isabs(n) else os.path.join("/mnt/media/raw", n)
        if not os.path.exists(p):
            continue
        try:
            dur = float(json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", p],
                capture_output=True, text=True).stdout)["format"]["duration"])
        except Exception:
            continue
        out.append((p, off, off + dur))
        off += dur
    return out


def b_frame(s, reel):
    """Frame of the B virtual reel at source time s, from whichever CARD holds it."""
    if s < 0 or not reel:
        return None
    for (p, a, b) in reel:
        if a <= s < b:
            return frame(p, s - a)
    if s < reel[-1][2]:
        p, a, b = reel[-1]
        return frame(p, s - a)
    return None


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,nb_frames",
                          "-show_entries", "format=duration,size", "-of", "json", path],
                         capture_output=True, text=True).stdout
    return json.loads(out)


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--basis", required=True)
    ap.add_argument("--proof", required=True)
    # PRODUCTION: the orchestrator passes the JOB CONTRACT A source and master audio. The Set 01
    # fixture path below is a default for isolated Set 01 fixtures ONLY: a default reference
    # silently QCs a job against different footage (on Set 02 it failed every A reference, the
    # closing check and the audio check while the render itself was correct).
    ap.add_argument("--a", default="/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4")
    ap.add_argument("--master", default=None,
                    help="master audio reference (default: the A source; the contract owns it)")
    ap.add_argument("--b", action="append", default=None,
                    help="B CARD files in virtual-reel order (default: the single legacy card)")
    ap.add_argument("--label", default="")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    for _role, _p in (("A source", args.a), ("master audio", (args.master or args.a))):
        if not os.path.exists(_p):
            print("QC REFUSED: %s not found: %s" % (_role, _p), file=sys.stderr)
            return 2

    basis = json.load(open(args.basis))
    segs = basis["segments"]
    span0 = float(basis["span"][0])
    # CANONICAL lag, fail closed. The merge records basis[lag_a_to_b_s]; older producers put it
    # under inputs. A program with B segments MUST carry it: the old 1.2783 fallback looked every
    # B reference up at the wrong moment on any job whose lag is not the Set 01 constant.
    lag = (basis.get("inputs") or {}).get("lag_a_to_b_s", basis.get("lag_a_to_b_s"))
    if lag is None:
        lag = (basis.get("coverage_application") or {}).get("lag_a_to_b_s")
    if lag is None and any(s.get("angle") == "B" for s in segs):
        print("QC REFUSED: the basis carries no canonical A->B lag and the program has B "
              "segments - refusing to QC against a defaulted timebase", file=sys.stderr)
        return 2
    lag = float(lag) if lag is not None else 0.0
    total = float(basis["total_s"])
    # the B virtual reel, card-aware: comparing every insert against card 1 scored the
    # second card's inserts 0.000 and let them pass trivially
    reel = build_b_reel(args.b, None)
    if not reel:
        print("WARNING: no readable B card — skipping B-reference checks")

    def ref_for(seg, t):
        """Reference frame of the camera `seg` should be showing at proof time t."""
        if seg["angle"] == "A":
            return frame(args.a, span0 + t)
        return b_frame(span0 + t - lag, reel)

    checks, rows = [], []

    def chk(name, ok, detail=""):
        checks.append((name, bool(ok), detail))
        print("  %s  %-52s %s" % ("PASS" if ok else "FAIL", name, detail))

    print("basis : %s" % args.basis)
    print("proof : %s%s" % (args.proof, ("   [%s]" % args.label) if args.label else ""))
    print("        %d segments, %.3f s, %d B insert(s), %d processing-boundary(ies)"
          % (len(segs), total, sum(1 for s in segs if s["angle"] == "B"),
             len(basis.get("boundary_ownership") or [])))

    info = probe(args.proof)
    dur = float(info["format"]["duration"])
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    chk("1  duration matches the basis total        ", abs(dur - total) < 0.5,
        "file %.3f s vs basis %.3f s (%+.3f)" % (dur, total, dur - total))
    chk("2  video stream is intact                  ",
        vs.get("codec_name") == "h264" and int(vs.get("nb_frames") or 0) > 0 and has_audio,
        "%s %sx%s, %s frames, audio=%s" % (vs.get("codec_name"), vs.get("width"), vs.get("height"),
                                           vs.get("nb_frames"), has_audio))

    # ---- cut detection from decoded pixels
    a, t0, step = series(args.proof, 0.0, dur)
    d = np.abs(np.diff(a, axis=0)).mean(axis=(1, 2))
    spikes = [(t0 + (i + 1) * step, float(d[i])) for i in range(len(d)) if d[i] > VIEW_DIFF]
    internal = [float(s["proof_in"]) for s in segs[1:]]
    real, motion_only = [], []
    for t, mag in spikes:
        before, after = frame(args.proof, t - 0.4), frame(args.proof, t + 0.2)
        c = corr(before, after)
        if c >= VIEW_CORR:
            motion_only.append((t, round(c, 3)))
            continue
        exp = next((s for s in segs if float(s["proof_in"]) <= t <= float(s["proof_out"])), None)
        if exp is not None:
            oth = {"angle": "B" if exp["angle"] == "A" else "A"}
            if same_source_evidence(corr(before, ref_for(exp, t - 0.4)),
                                    corr(before, ref_for(oth, t - 0.4)),
                                    corr(after, ref_for(exp, t + 0.2)),
                                    corr(after, ref_for(oth, t + 0.2))):
                motion_only.append((t, round(c, 3)))
                continue
        real.append((t, mag, c, True))
    view_changes = [t for t, mag, c, is_cut in real if is_cut]
    # ---- attribution + confirmation. A pixel-difference detector cannot see a cut between two
    # cameras showing near-identical framing, so equal counts are not the test. Every change the
    # detector finds must BE a manifest cut (else the render invented one), and every manifest cut
    # must be either detected within tolerance or confirmed against the camera references.
    tol = 0.30
    unplanned = [t for t in view_changes if not attributable(t, internal, tol, step)]
    chk("3  every view change is a manifest cut     ",
        not unplanned,
        "%d view change(s): %d at a planned cut%s"
        % (len(view_changes), len(view_changes) - len(unplanned),
           "" if not unplanned
           else "; %d not at a planned cut (nearest planned cut %+.2f to %+.2f s away)"
                % (len(unplanned),
                   min(min(abs(t - e) for e in internal) for t in unplanned),
                   max(min(abs(t - e) for e in internal) for t in unplanned))))
    matched, confirmed, missing, worst = [], [], [], 0.0
    for exp in internal:
        near = min(view_changes, key=lambda t: abs(t - exp)) if view_changes else None
        if near is not None and abs(near - exp) <= tol:
            matched.append(exp)
            worst = max(worst, abs(near - exp))
            continue
        k = internal.index(exp) + 1
        pre, post = segs[k - 1], segs[k]
        f_pre, f_post = frame(args.proof, exp - 0.2), frame(args.proof, exp + 0.2)
        ok = False
        if f_pre is not None and f_post is not None:
            ok = (corr(f_pre, ref_for(pre, exp - 0.2)) > corr(f_pre, ref_for(post, exp - 0.2))
                  and corr(f_post, ref_for(post, exp + 0.2)) > corr(f_post, ref_for(pre, exp + 0.2)))
        (confirmed if ok else missing).append(exp)
    chk("4  each internal cut lands on its manifest time",
        not missing,
        "%d detected within %.2f s (worst %.3f s), %d confirmed by the camera references%s"
        % (len(matched), tol, worst, len(confirmed),
           "" if not missing else "; MISSING %s" % ", ".join("%.2f" % t for t in missing[:5])))
    if motion_only:
        print("        motion-only spikes excluded: %s" % motion_only)

    # ---- per-segment angle and per-insert length
    ang_ok = True
    for s in segs:
        mid = (s["proof_in"] + s["proof_out"]) / 2.0
        f = frame(args.proof, mid)
        a_ref = frame(args.a, span0 + mid)
        b_ref = b_frame(span0 + mid - lag, reel)
        ca, cb = corr(f, a_ref), corr(f, b_ref)
        want_a = s["angle"] == "A"
        # before the reel starts there is no B frame to compare against: an A segment is then
        # confirmed by its own (positive) correlation instead
        ok = ((want_a and ca > 0.0) if cb != cb else ((ca > cb) if want_a else (cb > ca)))
        rows.append((s["id"], s["angle"], float(s["proof_out"] - s["proof_in"]), round(ca, 3),
                     round(cb, 3), ok))
        ang_ok = ang_ok and ok
    chk("5  every segment's pixels match its angle  ", ang_ok, "%d/%d segments" % (sum(1 for r in rows if r[5]), len(rows)))

    ins_ok, ins_rows = True, []
    for s in segs:
        if s["angle"] != "B":
            continue
        length = float(s["proof_out"]) - float(s["proof_in"])
        mid = (s["proof_in"] + s["proof_out"]) / 2.0
        f = frame(args.proof, mid)
        cb = corr(f, b_frame(span0 + mid - lag, reel))
        ca = corr(f, frame(args.a, span0 + mid))
        first_pass = s.get("decision_origin", "first_pass") == "first_pass"
        ok = abs(length - float(s["source_out"]) + float(s["source_in"])) < 0.05 and cb > ca
        ins_rows.append((s["id"], round(length, 3), round(float(s["source_out"]) - float(s["source_in"]), 3),
                         round(cb, 3), round(ca, 3), s.get("decision_origin", "first_pass"), ok))
        ins_ok = ins_ok and ok
    chk("6  each insert's on-screen length is decided and is really B", ins_ok,
        "; ".join("%s %.3f s corrB %+.3f/corrA %+.3f [%s]" % (r[0], r[1], r[3], r[4], r[5]) for r in ins_rows))

    # ---- closing angle + audio
    cf, e = frame(args.proof, total - 1.0), None
    _ca = corr(cf, frame(args.a, span0 + total - 1.0))
    _cb = corr(cf, b_frame(span0 + total - 1.0 - lag, reel))
    _ok7, _b_avail = closes_in_a(_ca, _cb)
    chk("7  the file closes in A                    ", _ok7,
        ("corrA %+.3f vs corrB %+.3f" % (_ca, _cb)) if _b_avail
        else ("corrA %+.3f (B N/A: no B coverage at this program time)" % _ca))

    # ---- boundaries invisible
    b_ok = True
    for o in (basis.get("boundary_ownership") or []):
        b = float(o["boundary_timeline_s"]) - span0
        near = [t for t in view_changes if abs(t - b) <= 1.0]
        planned = [e for e in internal if abs(e - b) <= tol]
        seg_id = (o.get("boundary_lies_inside") or {}).get("id")
        seg = next((s for s in segs if s["id"] == seg_id), None)
        try:
            f1, f2 = frame(args.proof, b - 0.5), frame(args.proof, b + 0.5)
            c = corr(f1, f2)
        except Exception:
            c = 0.0
        attrib = all(attributable(t, internal, tol, step) for t in near)
        if planned:
            # a boundary that coincides with a PLANNED cut is valid: that change is the edit the
            # human decided on, and the plan says so. Only an unattributable change fails.
            ok = attrib
            why = "planned cut %.3f s at the boundary" % planned[0]
        else:
            ok = (not near) and c > 0.5
            why = "no cut planned here"
        if seg is None:
            ok, why = False, "ownership names %r, not a segment in this basis" % seg_id
        b_ok = b_ok and ok
        print("        boundary %.1f s (proof %.3f s): inside %s | %s | changes<=1 s: %s | corr across %+.3f | %s"
              % (float(o["boundary_timeline_s"]), b, seg_id, why, [round(t, 2) for t in near], c,
                 "OK" if ok else "FAIL"))
    chk("8  every processing boundary is invisible in the edit", b_ok)

    # ---- audio alignment against the A reel
    off = float(basis.get("served_audio_offset_s", span0))
    span1 = float((basis.get("span") or [span0, span0 + PROBE_S])[1]) if len(
        basis.get("span") or []) > 1 else span0 + PROBE_S
    # Probe point INSIDE the program. 509.6 s for a full-length set (behaviour on the approved
    # program is bit-identical) but never past the end of a short one: a fixed 509.6 s probe made
    # EVERY program shorter than ~510 s fail this check no matter how correct its audio was.
    probe_t = min(PROBE_S, span0 + 0.5 * max(span1 - span0, 1.0))
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % probe_t, "-i", args.proof, "-t", "1",
                          "-vn", "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                         capture_output=True).stdout
    ref = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % (off + probe_t),
                          "-i", (args.master or args.a), "-t", "1",
                          "-vn", "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                         capture_output=True).stdout
    n = min(len(out), len(ref)) // 2
    x = np.frombuffer(out[:n * 2], dtype=np.int16).astype(np.float32)
    y = np.frombuffer(ref[:n * 2], dtype=np.int16).astype(np.float32)
    ac = corr(x, y) if n else 0.0
    chk("9  audio is the master audio at the right offset", ac > 0.9, "corr %+.3f at offset %.1f s" % (ac, off))

    failed = [c for c in checks if not c[1]]
    print("\n%d check groups, %d failure(s)%s" % (len(checks), len(failed),
                                                 "" if not failed else ": " + ", ".join(c[0].strip() for c in failed)))
    if args.json_out:
        json.dump({"basis": args.basis, "proof": args.proof, "label": args.label,
                   "duration_s": dur, "basis_total_s": total, "view_changes": view_changes,
                   "checks": [{"name": n, "pass": p, "detail": d} for n, p, d in checks],
                   "segments": [{"id": r[0], "angle": r[1], "length_s": r[2], "corr_a": r[3],
                                 "corr_b": r[4], "ok": r[5]} for r in rows],
                   "verdict": "PASS" if not failed else "FAIL", "proof_sha256": sha(args.proof)},
                  open(args.json_out, "w"), indent=1)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
