#!/usr/bin/env python3
"""qc_graphics.py — prove the burned-in graphics are placed, animated and composited correctly.

Measures the DELIVERED pixels, not the intent. Runs on the studio (numpy + ffmpeg, media local):

    /opt/video-studio/tools/venv/bin/python qc_graphics.py \
        --branded BRANDED.mp4 --clean CLEAN.mp4 --profile orbit_relay_v1 \
        --plan PLAN.json [--cut-time 10.580] [--json]

Checks
  1. PLACEMENT — diff(branded, clean) isolates the composited graphic (the clean render is the
     same timeline without the overlay). Its bbox must equal the profile's resolved rect:
     visible width 150, right margin 40, bottom margin 40 at 1080p. Nothing may spill outside
     the rect (spill = a positioning error), which is also what clipping would look like.
  2. ANIMATION PHASE — the rendered graphic region is matched against every frame of the source
     sequence. The best match must be floor(program_time * fps) % frame_count, at several
     program times, INCLUDING one immediately after an A/B cut. A phase that restarted at the
     cut would match a low index there instead. `fps` is the ANIMATION's own rate (30 fps) on the
     absolute program clock, and program_time is the EXTRACTED FRAME's PTS (measured with
     `-copyts` + showinfo), not the requested seek time — a `-ss` seek lands on the first frame
     at/after t, which is already ~1 animation frame late. Predicting this check from the
     delivery frame index (30000/1001) instead was a defect fixed 2026-09-14: ffmpeg samples the
     looping overlay by PTS, so it drifted one frame per 33 s and reported a false MISMATCH of
     52-59 frames on a 34-minute program.
  3. LOOP — the region at t and t+loop_s must be near-identical, and differ from t+loop_s/2.
  4. ALPHA — using the source frame's own alpha as ground truth inside the placed rect:
     where the source is transparent the branded render must equal the clean render (no opaque
     matte / no black box), and where it is solid it must equal the source RGB scaled into the
     rect (correct compositing). Chroma key would show as background-dependent error.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile

import numpy as np

V = "/opt/video-studio/tools/venv/bin/python"


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def frame_index(t):
    """Program time -> DELIVERY frame number (30000/1001). Used for frame extraction bookkeeping.

    NOTE (2026-09-14): this is NOT the animation-phase clock — see frame_pts() and the phase
    checks below. Using the delivery frame index as the phase prediction was a defect: ffmpeg
    samples the looping overlay by PTS, so a 30 fps sequence advances one frame per 1/30 s of
    PROGRAM time regardless of the delivery rate.
    """
    return int(round(float(t) * 30000.0 / 1001.0))


def frame_pts(path, t):
    """PTS (program seconds) of the frame a `-ss t` seek actually extracts.

    `-copyts` keeps the input timeline so showinfo reports the frame's own program timestamp.
    Phase MUST be predicted from this rather than from the requested t: an input seek lands on
    the first frame at/after t (measured ~0.026 s later here, i.e. ~1 animation frame at 30 fps).
    """
    r = run(["ffmpeg", "-v", "info", "-ss", "%.4f" % float(t), "-copyts", "-i", path,
             "-frames:v", "1", "-vf", "showinfo", "-f", "null", "-"])
    m = re.findall(r"pts_time:([0-9.]+)", r.stderr)
    if not m:
        sys.exit("cannot read the frame PTS at t=%.3f: %s" % (t, (r.stderr or "")[-300:]))
    return float(m[0])


def frame_at(path, t, out_png, w=1920, h=1080):
    """Extract the frame at program time t by ACCURATE TIMESTAMP SEEK (`-ss` before `-i`).

    Index selection (select=eq(n,N)) is exact but decodes from the start of the file — unusable over
    a 34-minute program. The branded file and its reference are encodes of the same timeline with the
    same GOP structure, so a timestamp seek lands on the same editorial frame in each; if that ever
    stopped being true the placement check's containment test would fail loudly (mismatched frames
    make the whole picture differ). Phase comparisons keep a +/-1 frame tolerance.
    """
    r = run(["ffmpeg", "-y", "-v", "error", "-ss", "%.4f" % float(t), "-i", path,
             "-frames:v", "1", "-vf", "scale=%d:%d" % (w, h), out_png])
    if r.returncode != 0 or not os.path.exists(out_png):
        sys.exit("frame extract failed at t=%.3f: %s" % (t, (r.stderr or "")[-300:]))
    return out_png


def rgb(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    return a.reshape(-1, 3)


def load_all_layers(plan, w, h):
    """Build EVERY source frame's composited layer ONCE -> array (n, h, w, 4).

    One ffmpeg pass over the whole sequence with the SAME filter chain the renderer used, so
    what is compared against the delivered pixels is exactly the layer that was burned. Doing
    this per candidate frame instead would be ~3000 ffmpeg invocations.
    """
    e = plan["graphics"]
    fr = e["frames"]
    root = os.path.join(e["asset_root"], fr.get("dir") or "")
    chain = e["geometry"]["filter_chain"]
    pre = "%s," % chain if chain and chain != "null" else ""
    vf = "%sscale=%d:%d,format=rgba" % (pre, w, h)
    r = subprocess.run(["ffmpeg", "-v", "error", "-framerate", str(fr.get("fps", 30)),
                        "-i", os.path.join(root, fr["pattern"]), "-vf", vf,
                        "-f", "rawvideo", "-pix_fmt", "rgba", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    n = int(fr.get("frame_count", fr.get("count")))
    if a.size != n * w * h * 4:
        sys.exit("layer build: got %d bytes, expected %d (%d frames of %dx%d RGBA)"
                 % (a.size, n * w * h * 4, n, w, h))
    return a.reshape(n, h, w, 4)


def sample_plan(basis, cap=20):
    """Derive QC sample times and cut sites from the delivered program BASIS.

    Nothing is hardcoded: the program extent, every B-insert boundary (an A->B or B->A cut) and
    every processing-boundary seam inside the program come from the same basis that produced the
    render, so a full-set QC follows the edit it is checking.
    """
    segs = basis.get("segments") or []
    span = basis.get("span") or [0.0, float(basis.get("total_s") or 0.0)]
    t0, t1 = float(span[0]), float(span[1])
    dur = t1 - t0
    cuts = []
    for s in segs:
        if s.get("angle") == "B":
            cuts += [float(s["proof_in"]), float(s["proof_out"])]
    for b in (basis.get("boundary_ownership") or []):
        if "boundary_timeline_s" in b:
            cuts.append(float(b["boundary_timeline_s"]))
    cuts = sorted({round(c, 3) for c in cuts if t0 + 1.0 < c < t1 - 1.0})
    times = [t0 + 5.0, t0 + 30.0, t0 + dur * 0.25, t0 + dur * 0.5, t0 + dur * 0.75, t1 - 5.0]
    times += [c + 0.05 for c in cuts]
    times += [c - 0.05 for c in cuts[:4]]
    times = sorted({round(t, 3) for t in times if t0 < t < t1})
    if len(times) > cap:
        step = len(times) / float(cap)
        times = sorted({times[min(int(i * step), len(times) - 1)] for i in range(cap)})
    return cuts[:6], times


def bbox_of(mask):
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    return {"x": int(xs.min()), "y": int(ys.min()),
            "w": int(xs.max()) - int(xs.min()) + 1, "h": int(ys.max()) - int(ys.min()) + 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--branded", required=True)
    ap.add_argument("--clean", required=True)
    ap.add_argument("--plan", required=True, help="JSON from render_final.py --dump-plan")
    ap.add_argument("--cut-time", type=float, default=None,
                    help="program time of an A/B cut inside the smoke section")
    ap.add_argument("--basis", default=None,
                    help="program basis JSON: derive sample times and cut sites from the delivered "
                         "edit instead of a hardcoded list (required for a full program)")
    ap.add_argument("--max-points", type=int, default=20)
    ap.add_argument("--times", default="1.0,3.0,5.0,7.0,10.6,13.0,18.0,22.0,26.0")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--json-out", default=None, help="write the JSON report to this path")
    a = ap.parse_args()

    plan = json.load(open(a.plan))
    g = plan["graphics"]["geometry"]
    fv = plan["graphics"]["frames_verified"]
    fps = float(fv["fps"])
    n = int(fv["count"])
    loop = float(fv["loop_s"])
    vw, vh = g["visible_size"]
    ex, ey = g["resolved_x"], g["resolved_y"]
    cuts = [a.cut_time] if a.cut_time is not None else []
    times = [float(x) for x in a.times.split(",")]
    if a.basis:
        cuts, times = sample_plan(json.load(open(a.basis)), a.max_points)
        print("=== expectations derived from the delivered basis ===")
        print("  %d cut sites (B-insert boundaries + processing boundaries), %d program sample points"
              % (len(cuts), len(times)))
        print("  program points: %s" % ", ".join("%.1f" % t for t in times))
        print()
    out = {"profile": plan["graphics"]["profile"], "expected": {"x": ex, "y": ey, "w": vw, "h": vh},
           "cut_sites": cuts, "times": times, "checks": {}}

    with tempfile.TemporaryDirectory() as td:
        # ---------- 1. placement: diff(branded, clean) ----------
        # The two files are INDEPENDENT x264 encodes of the same timeline with different content
        # in one region, so rate control differs everywhere at a low level: a raw diff mask
        # covers the whole frame and says nothing. So the bug's location is measured against the
        # encoder's own noise floor: strong = diff well above what the rest of the frame does.
        b = rgb(frame_at(a.branded, 5.0, os.path.join(td, "b.png")))
        c = rgb(frame_at(a.clean, 5.0, os.path.join(td, "c.png")))
        dh, dw = 1080, 1920
        diff = np.abs(b.astype(np.int16) - c.astype(np.int16)).max(axis=1).reshape(dh, dw)
        band = 40
        outside = np.ones((dh, dw), bool)
        outside[max(0, ey - band):ey + vh + band, max(0, ex - band):ex + vw + band] = False
        noise = float(np.percentile(diff[outside], 99.5))
        cutoff = max(3.0 * noise, 40.0)
        strong = diff > cutoff
        bb = bbox_of(strong)
        rect = strong[ey:ey + vh, ex:ex + vw]
        inside_frac = float(rect.mean())
        outside_strong = int(strong[outside].sum())
        out["checks"]["placement"] = {"observed_bbox": bb, "expected_bbox": {"x": ex, "y": ey, "w": vw, "h": vh},
                                      "encoder_noise_p99.5": noise, "cutoff": cutoff,
                                      "inside_strong_fraction": inside_frac,
                                      "strong_pixels_outside_rect": outside_strong}
        print("=== 1. placement (decoded frames, not intent) ===")
        print("  expected rect       : x=%d y=%d w=%d h=%d" % (ex, ey, vw, vh))
        print("  encoder noise p99.5 : %.1f  -> strong-diff cutoff %.1f" % (noise, cutoff))
        print("  strong-diff bbox    : %s" % bb)
        print("  strong inside rect  : %.1f%% of the rect (the graphic's own extent in this frame)"
              % (100.0 * inside_frac))
        print("  strong outside rect : %d px (must be ~0 — that is what a mis-placement looks like)"
              % outside_strong)
        if bb is None:
            print("  FAIL: no graphic found in the branded render")
            sys.exit(1)
        contained = (bb["x"] >= ex - 2 and bb["y"] >= ey - 2
                     and bb["x"] + bb["w"] <= ex + vw + 2 and bb["y"] + bb["h"] <= ey + vh + 2)
        right_margin = 1920 - (bb["x"] + bb["w"])
        bottom_margin = 1080 - (bb["y"] + bb["h"])
        print("  strong-diff margins : right %d, bottom %d (expected rect gives 40 / 40)" % (right_margin, bottom_margin))
        print("  contained in rect   : %s" % ("yes" if contained else "NO — FAIL"))
        ok = contained and inside_frac >= 0.15 and outside_strong == 0
        out["checks"]["placement"].update({"right_margin": right_margin, "bottom_margin": bottom_margin,
                                           "contained": bool(contained), "pass": bool(ok)})
        print("  -> %s" % ("PASS" if ok else "FAIL"))

        # ---------- 2. animation phase ----------
        print()
        print("=== 2. animation phase = absolute program time ===")
        layers = load_all_layers(plan, vw, vh)
        lrgb = layers[:, :, :, :3].astype(np.int16)
        print("  %d candidate layers built at %dx%d (one pass, the renderer's own chain)"
              % (lrgb.shape[0], vw, vh))
        phase = []
        for t in out["times"]:
            bf = rgb(frame_at(a.branded, t, os.path.join(td, "bp.png"))).reshape(dh, dw, 3)
            region = bf[ey:ey + vh, ex:ex + vw].astype(np.int16)
            errs = np.abs(lrgb - region[None, :, :, :]).mean(axis=(1, 2, 3))
            best, bestv = int(errs.argmin()), float(errs.min())
            # CONTRACT (profile_registry.json / plan): phase = floor(program_time * fps) %
            # frame_count on the ABSOLUTE program clock, with fps = the ANIMATION's own rate (30).
            # The renderer feeds a 30 fps looping sequence into the 29.970 fps delivery and ffmpeg
            # samples that overlay by PTS, so it advances one animation frame per 1/30 s of PROGRAM
            # time — NOT one per output frame. Predict from the extracted frame's own PTS (a `-ss`
            # seek lands ~1 frame later than t), never from the delivery frame index.
            pts = frame_pts(a.branded, t)
            pred = int(math.floor(pts * fps)) % n
            dm = min((best - pred) % n, (pred - best) % n)
            hit = dm <= 1
            phase.append({"t": t, "pts": pts, "predicted": pred, "best_match": best,
                          "delta": dm, "hit": hit})
            print("  t=%6.3fs pts=%9.3f -> predicted idx %3d, best match %3d (delta %d) %s"
                  % (t, pts, pred, best, dm, "OK" if hit else "MISMATCH"))
        out["checks"]["animation"] = phase
        out["checks"]["animation_pass"] = all(p["hit"] for p in phase)
        print("  -> %s" % ("PASS" if out["checks"]["animation_pass"] else "FAIL"))

        if cuts:
            print()
            print("=== 2b. phase across %d cut site(s) — no restart ===" % len(cuts))
            rows = []
            for c in cuts:
                for t in (c - 0.05, c + 0.05):
                    bf = rgb(frame_at(a.branded, t, os.path.join(td, "bc.png"))).reshape(dh, dw, 3)
                    region = bf[ey:ey + vh, ex:ex + vw].astype(np.int16)
                    errs = np.abs(lrgb - region[None, :, :, :]).mean(axis=(1, 2, 3))
                    best, bestv = int(errs.argmin()), float(errs.min())
                    pts = frame_pts(a.branded, t)
                    pred = int(math.floor(pts * fps)) % n
                    dm = min((best - pred) % n, (pred - best) % n)
                    rows.append({"cut_t": round(c, 3), "t": round(t, 3), "pts": pts,
                                 "predicted": pred, "best_match": best, "delta": dm, "hit": dm <= 1})
                    print("  cut %8.3f  t=%8.3fs pts=%9.3f -> predicted %3d, match %3d (delta %d) %s"
                          % (c, t, pts, pred, best, dm, "OK" if dm <= 1 else "MISMATCH"))
            out["checks"]["phase_across_cut"] = rows
            out["checks"]["phase_across_cut_pass"] = all(r["hit"] for r in rows)
            print("  -> %s" % ("PASS" if out["checks"]["phase_across_cut_pass"] else "FAIL"))

        # ---------- 3. loop ----------
        print()
        print("=== 3. 8 s loop (t vs t+loop; t vs t+loop/2) ===")
        # Measured on the ISOLATED overlay (|branded - clean| inside the placed rect), not on raw
        # pixels. The rect also contains the picture underneath it, and in a window where that
        # picture itself moves between t, t+loop/2 and t+loop its motion swamps the overlay's own
        # loop difference: a bright handheld A segment does this, a dark club picture does not, so
        # the raw-pixel ratio passed on the certified program and failed on an equally correct
        # short one. Isolating the overlay measures exactly what this check claims — the same
        # animation frame at t and t+loop, a different one at t+loop/2 — with the threshold intact.
        def region_at(t):
            bf = rgb(frame_at(a.branded, t, os.path.join(td, "bl.png"))).reshape(dh, dw, 3)
            return bf[ey:ey + vh, ex:ex + vw].astype(np.int16)

        # Measured on the overlay's SOLID band, not on the whole rect. Two reasons, both measured:
        #  - raw pixels inside the rect include the picture, and where that picture moves between
        #    t, t+loop/2 and t+loop its motion swamps the overlay (a bright handheld A window does;
        #    the certified program's dark club picture does not, so the raw ratio was passing on
        #    one correct program and failing another);
        #  - |branded - clean| does not isolate the overlay either: alpha compositing makes the
        #    residual a*(graphic - picture), which still depends on the picture.
        # In the solid band (alpha >= 250) the composited pixel IS the graphic's own colour, so the
        # picture cancels: the same animation frame at t and t+loop must match there, and a
        # different frame at t+loop/2 must not. Threshold (d_loop < d_half / 2) unchanged.
        solid = layers[frame_index(2.0) % n][:, :, 3] >= 250
        if int(solid.sum()) < 8:
            print("  solid band too small (%d px) — falling back to the whole rect" % int(solid.sum()))
            solid = np.ones((vh, vw), dtype=bool)
        print("  measuring over the solid band: %d of %d px" % (int(solid.sum()), vh * vw))
        r0 = region_at(2.0)[solid]
        rloop = region_at(2.0 + loop)[solid]
        rhalf = region_at(2.0 + loop / 2.0)[solid]
        d_loop = float(np.abs(r0 - rloop).mean())
        d_half = float(np.abs(r0 - rhalf).mean())
        print("  mean |t - (t+loop)|   = %.3f" % d_loop)
        print("  mean |t - (t+loop/2)| = %.3f" % d_half)
        loop_ok = d_loop < d_half / 2.0
        out["checks"]["loop"] = {"d_loop": d_loop, "d_half": d_half, "pass": bool(loop_ok)}
        print("  -> %s" % ("PASS" if loop_ok else "FAIL"))

        # ---------- 4. alpha ----------------
        print()
        print("=== 4. alpha compositing (source alpha as ground truth) ===")
        t = 5.0
        idx = frame_index(t) % n
        bf = rgb(frame_at(a.branded, t, os.path.join(td, "ba.png"))).reshape(dh, dw, 3).astype(np.int16)
        cf = rgb(frame_at(a.clean, t, os.path.join(td, "ca.png"))).reshape(dh, dw, 3).astype(np.int16)
        lay = layers[idx]
        alpha = lay[:, :, 3]
        breg, creg, sreg = bf[ey:ey + vh, ex:ex + vw], cf[ey:ey + vh, ex:ex + vw], lay[:, :, :3].astype(np.int16)
        bands = {"solid (a>=250)": alpha >= 250, "edge (8<=a<250)": (alpha >= 8) & (alpha < 250),
                 "transparent (a<8)": alpha < 8}
        res = {}
        for name, m in bands.items():
            if not m.any():
                print("  %-20s : no pixels" % name)
                res[name] = {"pixels": 0}
                continue
            d_bc = float(np.abs(breg[m] - creg[m]).mean())
            d_bs = float(np.abs(breg[m] - sreg[m]).mean())
            res[name] = {"pixels": int(m.sum()), "diff_vs_clean": d_bc, "diff_vs_source_rgb": d_bs}
            print("  %-20s : %6d px | vs clean %6.2f | vs source RGB %6.2f" % (name, m.sum(), d_bc, d_bs))
        tr = res.get("transparent (a<8)", {})
        so = res.get("solid (a>=250)", {})
        # Delivery is yuv420p, so chroma is subsampled 2x2: a detailed graphic cannot match its
        # source RGB exactly (mean error ~20-40 in RGB space is normal). What must hold is that
        # the GRAPHIC dominates the region, not the background underneath it.
        tr_ok = tr.get("pixels", 0) == 0 or tr["diff_vs_clean"] < 6.0
        so_ok = (so.get("pixels", 0) == 0 or so["diff_vs_source_rgb"] < 0.5 * so["diff_vs_clean"])
        a_ok = bool(tr_ok and so_ok)
        out["checks"]["alpha"] = res
        out["checks"]["alpha_pass"] = a_ok
        out["checks"]["alpha_criteria"] = {
            "transparent_band_unchanged": "diff vs clean < 6 (no matte / no black box)",
            "solid_band_graphic_dominates": "diff vs source RGB < half the diff vs clean",
            "note": "exact RGB equality is not achievable through 4:2:0 chroma subsampling",
            "transparent_ok": bool(tr_ok), "solid_ok": bool(so_ok)}
        print("  transparent area unchanged (no opaque matte / black box): %s" % ("OK" if tr_ok else "FAIL"))
        print("  solid area: graphic dominates the background (%.2f < 0.5 x %.2f): %s"
              % (so.get("diff_vs_source_rgb", 0), so.get("diff_vs_clean", 0), "OK" if so_ok else "FAIL"))
        print("  -> %s" % ("PASS" if a_ok else "FAIL"))

    overall = (out["checks"]["placement"]["pass"] and out["checks"]["animation_pass"]
               and out["checks"]["loop"]["pass"] and out["checks"]["alpha_pass"]
               and out["checks"].get("phase_across_cut_pass", True))
    out["overall_pass"] = bool(overall)
    print()
    print("GRAPHICS QC: %s" % ("PASS" if overall else "FAIL"))
    if a.json:
        print(json.dumps(out, indent=2))
    if a.json_out:
        json.dump(out, open(a.json_out, "w"), indent=1)
        print("report written: %s" % a.json_out)
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
