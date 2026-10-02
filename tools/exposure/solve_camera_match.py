#!/usr/bin/env python3
"""solve_camera_match.py — solve a per-JOB camera match for one angle against a chosen reference.

WHY THIS EXISTS (2026-09-15, club-dispatch-set02)

`camera_normalization_final.json` is the solve a human accepted for Set 01. The pipeline stamped
it into every later job's manifest, so Set 02 rendered with Set 01's B trim. Set 02's B source
measures ~2x Set 01's at the same program positions, so the inherited trim left all 27 B inserts
at delivered luma 50.6-58.3 against an approved band of [20.0, 38.0] and the colour gate refused it.

The lesson: the HOUSE LOOK (the locked grades) is reusable policy; the TRANSFORM that brings a
particular camera to that look is derived job/camera data with its own provenance.

METHOD — identical to the accepted lineage (`grade_calibrate3.py`, 2026-09-11):

  the angle keeps its locked grade and takes NO auto pre-grade; a per-channel trim is solved IN
  THE DELIVERED DOMAIN and inverted back through the locked grade, emitted as a dense per-channel
  LUT so the curves spline has no room to bulge.

  pre(v) = trans_c^-1(gain_c * trans_c(v))     trans = the transfer of that angle's locked grade

REFERENCE (`--target`):
  job   match this angle to THIS JOB's other camera at the same program instants (the accepted
        lineage: B is matched to the job's own A). Preserves within-job A/B consistency.
  house match this angle to the approved A reference recorded in the house look record. Preserves
        the approved absolute level, at the cost of A/B consistency when the job's own A is off it.

Usage:
  solve_camera_match.py --angle B --target job --a A_CARD --b B1 --b B2 \
      --sync-map sync_map.json --house-record camera_normalization_final.json \
      --out camera_match_set02_b.json [--samples 18] [--json]

Exit: 0 = solved, 1 = fail-closed control tripped, 2 = error.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
# ONE meaning for a manifest `pre_grades` entry — the canonical resolver, shared with both
# renderers, so a B solve cannot interpret a time-varying A record differently from the renderer.
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "lib")))
import camera_match as CM  # noqa: E402

GEOM = "scale=960:540,crop=iw*0.87:ih*0.87,scale=960:540"
GRADE_A = "eq=brightness=-0.02:saturation=0.92,colorbalance=rs=0.0:bs=0.0:rm=0.0"
GRADE_B = ("curves=all='0/0.03 0.05/0.28 0.10/0.50 0.20/0.70 0.35/0.85 1/1',"
           "eq=brightness=-0.02:saturation=1.05,colorbalance=rs=0.0:bs=0.0:rm=0.0")
# The colour gate measures the delivered proof through its own 70% centre crop. Solving in that same
# domain makes the per-control-point check predict the gate instead of a fuller frame.
GATE_GEOM = GEOM + ",crop=trunc(iw*0.7/2)*2:trunc(ih*0.7/2)*2,scale=960:540"
GAMMAS = [1.0, 0.98, 0.96, 0.94, 0.92, 0.90, 0.88, 0.86, 0.84, 0.82, 0.80]
SATS = [1.0, 0.97, 0.94, 0.91, 0.88, 0.85, 0.82]

# The colour gate's own acceptance band (qc_color.py DEFAULT_BAND). Reported as a PREDICTION only;
# the decisive check is the gate itself on a rendered proof.
BAND = {"luma_mean": [20.0, 38.0], "p95_max": 120, "p99_max": 160, "clipped_pct_max": 0.30,
        "black_clip_pct_min": 8.0, "sat_mean": [8.0, 22.0]}


def frames(path, t, vf, out_png=None, pix="rgb24"):
    if out_png:
        subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", "%.3f" % t,
                        "-i", path, "-frames:v", "1", "-vf", vf, "-y", out_png],
                       capture_output=True, check=False)
        path, t = out_png, 0.0
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", "%.3f" % t,
                        "-i", path, "-frames:v", "1", "-vf", vf, "-f", "rawvideo",
                        "-pix_fmt", pix, "-"], capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    return a.reshape(-1, 3).astype(np.float64) if a.size and a.size % 3 == 0 else None


def stats(px):
    lum = 0.2126 * px[:, 0] + 0.7152 * px[:, 1] + 0.0722 * px[:, 2]
    return {"luma": float(lum.mean()), "chroma": float((px.max(axis=1) - px.min(axis=1)).mean()),
            "rgb": [float(px[:, i].mean()) for i in range(3)]}


def gate_stats(px):
    """Byte-for-byte the quantities qc_color.py enforces, so a prediction is comparable to the
    gate itself: integer BT.601 luma, black = y < 16, clipped = y >= 235, histogram quantiles."""
    r = px[:, 0].astype(np.int64)
    g = px[:, 1].astype(np.int64)
    b = px[:, 2].astype(np.int64)
    y = (r * 299 + g * 587 + b * 114) // 1000
    sat = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    tot = float(y.size)
    return {"luma_mean": round(float(y.mean()), 2),
            "p95": int(np.percentile(y, 95)),
            "p99": int(np.percentile(y, 99)),
            "sat_mean": round(float(sat.mean()), 2),
            "clipped_pct": round(100.0 * float((y >= 235).sum()) / tot, 3),
            "black_clip_pct": round(100.0 * float((y < 16).sum()) / tot, 2)}


def in_band(gs):
    bad = []
    if not (BAND["luma_mean"][0] <= gs["luma_mean"] <= BAND["luma_mean"][1]):
        bad.append("luma_mean %s outside %s" % (gs["luma_mean"], BAND["luma_mean"]))
    if gs["p95"] > BAND["p95_max"]:
        bad.append("p95 %s > %s" % (gs["p95"], BAND["p95_max"]))
    if gs["p99"] > BAND["p99_max"]:
        bad.append("p99 %s > %s" % (gs["p99"], BAND["p99_max"]))
    if gs.get("clipped_pct", 0.0) > BAND["clipped_pct_max"]:
        bad.append("clipped_pct %s > %s" % (gs["clipped_pct"], BAND["clipped_pct_max"]))
    if gs["black_clip_pct"] < BAND["black_clip_pct_min"]:
        bad.append("black_clip %s < %s" % (gs["black_clip_pct"], BAND["black_clip_pct_min"]))
    if not (BAND["sat_mean"][0] <= gs["sat_mean"] <= BAND["sat_mean"][1]):
        bad.append("sat %s outside %s" % (gs["sat_mean"], BAND["sat_mean"]))
    return bad


def avg(rows):
    rows = [r for r in rows if r]
    if not rows:
        return None
    return {k: (float(np.mean([r[k] for r in rows])) if k != "rgb"
                else [float(np.mean([r["rgb"][i] for r in rows])) for i in range(3)])
            for k in ("luma", "chroma", "rgb")}


def grade_transfer(grade):
    data = np.repeat(np.arange(256, dtype=np.uint8).reshape(256, 1, 1), 3, axis=2).tobytes()
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-s", "256x1", "-i", "-", "-vf", grade,
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       input=data, capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    if a.size != 256 * 3:
        raise SystemExit("grade transfer failed: got %d bytes" % a.size)
    return a.reshape(256, 3).astype(np.float64)


def build_pre(trans, gains, n_pts=33):
    xs = np.linspace(0, 255, n_pts)
    out = {}
    for ci, ch in enumerate("rgb"):
        col = trans[:, ci]
        ys = []
        for v in xs:
            want = np.clip(gains[ci] * np.interp(v, np.arange(256), col), 0, 255)
            ys.append(float(np.argmin(np.abs(col - want))))
        ys = np.maximum.accumulate(np.clip(ys, 0, 255))
        out[ch] = " ".join("%.4f/%.4f" % (x / 255.0, y / 255.0) for x, y in zip(xs, ys))
    return "curves=r='%s':g='%s':b='%s'" % (out["r"], out["g"], out["b"])


def sha256(path, chunk=1 << 24):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--angle", choices=("A", "B"), default="B")
    ap.add_argument("--target", choices=("job", "house"), default="job")
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", action="append", default=[], help="B card, in reel order")
    ap.add_argument("--sync-map", required=True)
    ap.add_argument("--house-record", required=True)
    ap.add_argument("--a-pre-record", default=None,
                    help="an existing per-job A match record; A is then measured through it, so a "
                         "B solve targets the CORRECTED A rather than raw A")
    ap.add_argument("--out", required=True)
    ap.add_argument("--samples", type=int, default=18)
    ap.add_argument("--a-tolerance", type=float, default=2.0,
                    help="luma distance from the approved A reference beyond which --strict-a fails")
    ap.add_argument("--strict-a", action="store_true",
                    help="fail closed when this job's A disagrees with the approved A reference")
    ap.add_argument("--work", default="/tmp/solve_camera_match")
    ap.add_argument("--no-sha", action="store_true")
    ap.add_argument("--hash-source", action="append", default=[],
                    help="provenance JSON whose verified source sha256 values are REUSED "
                         "(never re-hash 15 GB of media for a value that already exists)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--time-varying", action="store_true",
                    help="solve a MATCH PER CONTROL TIME with smooth interpolation instead of one "
                         "transform for the whole reel (a source whose exposure drifts)")
    ap.add_argument("--control-times", default="",
                    help="comma list of source/control seconds for --time-varying")
    ap.add_argument("--window", type=float, default=20.0,
                    help="half-width of the local sample window around each control time")
    ap.add_argument("--window-step", type=float, default=5.0)
    ap.add_argument("--max-chunk-s", type=float, default=30.0,
                    help="renderer chunk cap for the interpolated transform")
    ap.add_argument("--measure-domain", choices=("full", "gate"), default="full",
                    help="gate = measure through the colour gate's own centre crop")
    ap.add_argument("--gamma", type=float, default=1.0,
                    help="extra highlight/midtone compression appended to the pre-grade")
    ap.add_argument("--fit-gamma-sat", action="store_true",
                    help="fit gamma (highlight/midtone) and saturation inside the solve loop, "
                         "aiming at --target-p95/--target-p99/--target-sat instead of the band edge")
    ap.add_argument("--target-p95", type=float, default=118.0,
                    help="aim below the band's p95 max (132) so burned-in overlays do not tip it")
    ap.add_argument("--target-p99", type=float, default=138.0,
                    help="aim below the band's p99 max (149)")
    ap.add_argument("--target-sat", type=float, default=19.2,
                    help="aim below the band's saturation max (21)")
    ap.add_argument("--auto-fit", action="store_true",
                    help="search the SMALLEST gamma/saturation correction that puts every control "
                         "point inside the gate band (measured in --measure-domain)")
    ap.add_argument("--sat", type=float, default=1.0,
                    help="extra saturation factor appended to the pre-grade (used ONLY when the "
                         "downstream gate reports this angle failing on saturation alone; still "
                         "exactly one camera-match transform for the angle)")
    args = ap.parse_args()

    sm = json.load(open(args.sync_map))
    cards = sm["cards"]
    lag = float(cards[0]["a_time_at_b_zero"])
    reel_end = sum(float(c["duration_s"]) for c in cards)
    cover_lo, cover_hi = lag, lag + reel_end
    lo, hi = cover_lo + 8.0, cover_hi - 8.0
    at = [round(lo + (hi - lo) * i / (args.samples - 1), 3) for i in range(args.samples)]
    step = (hi - lo) / (2 * args.samples + 1)
    ho = [round(lo + step * (2 * i + 1), 3) for i in range(args.samples // 2)]

    def card_for(reel_t):
        for c in cards:
            o = float(c["reel_offset_cumulative_s"])
            if o <= reel_t < o + float(c["duration_s"]):
                return c["source"], reel_t - o
        return None, None

    def resolve(angle, t):
        """(source path, source-local time) for an angle at program time t."""
        if angle == "A":
            return args.a, t
        return card_for(t - lag)

    GRADE = {"A": GRADE_A, "B": GRADE_B}
    angle, other = args.angle, ("A" if args.angle == "B" else "B")

    print("job reel: %.3f-%.3f s on the A timebase (lag %.4f s), %d B cards" %
          (cover_lo, cover_hi, lag, len(cards)))
    print("solving angle %s against target '%s'; %d samples, %d held out" %
          (angle, args.target, len(at), len(ho)))

    house = json.load(open(args.house_record))
    tgt_house = house["target_a"]
    # When a per-job A match already exists, the A reference IS the corrected A: measure A through
    # its own match so B is solved against the corrected A at synchronized instants, not raw A.
    pre_a = ""
    pre_a_tv = None
    if args.a_pre_record:
        pre_a = json.load(open(args.a_pre_record))["solved_pre_grade"]
        if CM.is_time_varying(pre_a):
            # A drifts, so ONE fragment cannot represent it: resolve A's fragment AT EACH SAMPLE
            # TIME through the canonical resolver. Reading solved_pre_grade as a string assumed a
            # static A record and crashed on a time-varying one — the exact case where B most needs
            # to target corrected A.
            pre_a_tv = pre_a
            print("A measured through its own job match, TIME_VARYING (%d control points): %s"
                  % (len(pre_a["points"]), args.a_pre_record))
        else:
            print("A measured through its own job match: %s" % args.a_pre_record)

    def a_vf(t):
        frag = CM.curve_at(pre_a_tv["points"], t) if pre_a_tv else pre_a
        return GEOM + "," + (frag + "," if frag else "") + GRADE_A

    measured = {"A": avg([stats(frames(*resolve("A", t), a_vf(t))) for t in at])}
    if args.b:
        measured["B"] = avg([stats(frames(*resolve("B", t), GEOM + "," + GRADE_B)) for t in at])
    for k in ("A", "B"):
        if measured.get(k):
            print("%s delivered (this job)   luma %6.2f chroma %5.2f rgb %s" %
                  (k, measured[k]["luma"], measured[k]["chroma"],
                   [round(x, 1) for x in measured[k]["rgb"]]))
    print("A reference (approved)    luma %6.2f chroma %5.2f rgb %s" %
          (tgt_house["luma"], tgt_house["chroma"], [round(x, 1) for x in tgt_house["rgb"]]))
    dl = measured["A"]["luma"] - tgt_house["luma"]
    print("job A vs approved A       luma %+.2f (tolerance %.2f)%s" %
          (dl, args.a_tolerance, "  [STRICT]" if args.strict_a else ""))
    if args.strict_a and abs(dl) > args.a_tolerance:
        print(json.dumps({"ok": False,
                          "error": "this job's A does not agree with the approved A reference",
                          "a_delivered_luma": measured["A"]["luma"],
                          "a_reference_luma": tgt_house["luma"], "delta": dl}))
        return 1

    if args.target == "house":
        tgt = tgt_house
    else:
        if angle == "B":
            if not measured.get("B"):
                raise SystemExit("--target job for angle B needs --b cards")
            tgt = measured["A"]            # the accepted lineage: B is matched to this job's A
        else:
            tgt = measured["B"] if measured.get("B") else tgt_house
    print("target used               luma %6.2f chroma %5.2f rgb %s" %
          (tgt["luma"], tgt["chroma"], [round(x, 1) for x in tgt["rgb"]]))

    os.makedirs(args.work, exist_ok=True)
    cached, missing = [], 0
    for i, t in enumerate(at):
        src, st = resolve(angle, t)
        if not src:
            missing += 1
            continue
        p = os.path.join(args.work, "%s_%02d.png" % (angle, i))
        if not os.path.exists(p):
            frames(src, st, GEOM, out_png=p)
        cached.append(p)
    if missing or not cached:
        print(json.dumps({"ok": False, "error": "%d samples had no %s coverage" % (missing, angle)}))
        return 2

    def compose_pre(gains):
        """One camera-match pre-grade for the angle: the solved per-channel trim, plus an optional
        minimum saturation correction when the gate reports this angle failing on saturation."""
        s = build_pre(trans, gains)
        if abs(args.sat - 1.0) > 1e-9:
            s += ",eq=saturation=%.4f" % args.sat
        return s

    def measure_cache(pre):
        return avg([stats(frames(p, 0.0, pre + "," + GRADE[angle])) for p in cached])

    nopre = measure_cache("null")
    print("%s no pre-grade           luma %6.2f chroma %5.2f rgb %s" %
          (angle, nopre["luma"], nopre["chroma"], [round(x, 1) for x in nopre["rgb"]]))

    trans = grade_transfer(GRADE[angle])
    gains = [tgt["rgb"][i] / max(1e-6, m) for i, m in enumerate(nopre["rgb"])]
    print("initial gains from rgb means: R %.4f G %.4f B %.4f" % tuple(gains))

    trace = []
    for it in range(4):
        pre = compose_pre(gains)
        cur = measure_cache(pre)
        err = [tgt["rgb"][i] / max(1e-6, cur["rgb"][i]) for i in range(3)]
        trace.append({"iter": it + 1, "luma": round(cur["luma"], 2),
                      "chroma": round(cur["chroma"], 2),
                      "rgb": [round(x, 2) for x in cur["rgb"]],
                      "err": [round(e, 4) for e in err]})
        print("  iter %d: luma %6.2f (target %.2f)  chroma %5.2f (target %.2f)  rgb %s" %
              (it + 1, cur["luma"], tgt["luma"], cur["chroma"], tgt["chroma"],
               [round(x, 1) for x in cur["rgb"]]))
        gains = [gains[i] * err[i] for i in range(3)]
        if max(abs(e - 1) for e in err) < 0.01:
            print("  converged")
            break

    pre = compose_pre(gains)
    final = measure_cache(pre)
    print("\n== final ==")
    print("  target                 luma %6.2f  chroma %5.2f  rgb %s" %
          (tgt["luma"], tgt["chroma"], [round(x, 1) for x in tgt["rgb"]]))
    print("  %s no pre-grade         luma %6.2f  chroma %5.2f  rgb %s" %
          (angle, nopre["luma"], nopre["chroma"], [round(x, 1) for x in nopre["rgb"]]))
    print("  %s normalized           luma %6.2f  chroma %5.2f  rgb %s" %
          (angle, final["luma"], final["chroma"], [round(x, 1) for x in final["rgb"]]))
    print("  delta vs target        luma %+6.2f  chroma %+5.2f" %
          (final["luma"] - tgt["luma"], final["chroma"] - tgt["chroma"]))

    # end-to-end from the ORIGINAL media (not the cached frames), on the held-out set
    ha = avg([stats(frames(*resolve("A", t), a_vf(t))) for t in ho])
    hraw = []
    for t in ho:
        src, st = resolve(angle, t)
        if src:
            hraw.append(frames(src, st, GEOM + "," + pre + "," + GRADE[angle]))
    hb = avg([stats(p) for p in hraw if p is not None])
    print("  held-out  A %.2f  %s %.2f  (delta %+.2f)" %
          (ha["luma"], angle, hb["luma"], hb["luma"] - ha["luma"]))

    pred = [gate_stats(p) for p in hraw if p is not None]
    merged = {k: round(float(np.mean([p[k] for p in pred])), 2)
              for k in ("luma_mean", "p95", "p99", "sat_mean", "clipped_pct", "black_clip_pct")}
    bad = in_band(merged) if angle == "B" else []
    print("  gate-band prediction   %s" % json.dumps(merged))
    print("  in band                %s" % ("YES" if not bad else "NO: " + "; ".join(bad)))

    src_of, hash_sources = {}, []
    for hp in (args.hash_source or []):
        try:
            doc = json.load(open(hp))
        except Exception as exc:
            print(json.dumps({"ok": False, "error": "cannot read hash source %s: %s" % (hp, exc)}))
            return 2
        for s in (doc.get("sources") or []):
            if s.get("path") and s.get("sha256"):
                src_of[s["path"]] = s["sha256"]
        hash_sources.append({"record": hp, "record_sha256": sha256(hp),
                             "source_identity": doc.get("source_identity")})
    if not src_of and not args.no_sha:
        for p in [args.a] + list(args.b):
            src_of[p] = sha256(p)

    if args.time_varying:
        cts = [float(x) for x in args.control_times.split(",") if x.strip()]
        if not cts:
            print(json.dumps({"ok": False, "error": "--time-varying needs --control-times"}))
            return 2
        MEAS = GATE_GEOM if args.measure_domain == "gate" else GEOM
        print("measurement domain: %s" % args.measure_domain)
        points, all_before, all_after = [], [], []
        a_dur = float((sm.get("reference") or {}).get("duration_s") or cover_hi)
        span_hi = a_dur if angle == "A" else reel_end
        for ti in cts:
            lo = max(0.0, ti - args.window)
            hi = min(max(0.0, span_hi) - 0.5, ti + args.window)
            ts = []
            t = lo
            while t <= hi + 1e-6:
                src, st = resolve(angle, t)
                if src:
                    ts.append((t, src, st))
                t += args.window_step
            if len(ts) < 3:
                print(json.dumps({"ok": False, "error": "control time %.1f has %d usable samples"
                                                     % (ti, len(ts))}))
                return 2
            pngs = []
            for j, (t, src, st) in enumerate(ts):
                pg = os.path.join(args.work, "tv_%s_%s_%06.1f_%02d.png"
                                  % (angle, args.measure_domain, ti, j))
                if not os.path.exists(pg):
                    frames(src, st, MEAS, out_png=pg)
                pngs.append(pg)

            def meas(paths, vf):
                return avg([stats(frames(p, 0.0, vf)) for p in paths])

            gamma, sat = args.gamma, args.sat

            def compose(g, gamma, sat):
                pre_i = build_pre(trans, g)
                if abs(gamma - 1.0) > 1e-9:
                    pre_i += ",eq=gamma=%.4f" % gamma
                if abs(sat - 1.0) > 1e-9:
                    pre_i += ",eq=saturation=%.4f" % sat
                return pre_i

            def gstats(paths, pre):
                return {k: round(float(np.mean([gate_stats(frames(p, 0.0, pre + "," + GRADE[angle]))[k]
                                                for p in paths])), 2)
                        for k in ("luma_mean", "p95", "p99", "sat_mean", "clipped_pct",
                                  "black_clip_pct")}

            def band_fail(gd, lo_luma, hi_luma):
                bad = []
                if not (lo_luma <= gd["luma_mean"] <= hi_luma):
                    bad.append("luma %.2f outside [%.1f,%.1f]" % (gd["luma_mean"], lo_luma, hi_luma))
                if gd["p95"] > BAND["p95_max"]:
                    bad.append("p95 %s > %s" % (gd["p95"], BAND["p95_max"]))
                if gd["p99"] > BAND["p99_max"]:
                    bad.append("p99 %s > %s" % (gd["p99"], BAND["p99_max"]))
                if gd["clipped_pct"] > BAND["clipped_pct_max"]:
                    bad.append("clipped %s > %s" % (gd["clipped_pct"], BAND["clipped_pct_max"]))
                if not (BAND["sat_mean"][0] <= gd["sat_mean"] <= BAND["sat_mean"][1]):
                    bad.append("sat %s outside %s" % (gd["sat_mean"], BAND["sat_mean"]))
                return bad

            before = meas(pngs, "null," + GRADE[angle])
            fail_before = None
            g = [tgt["rgb"][i] / max(1e-6, m) for i, m in enumerate(before["rgb"])]
            gd_now = None
            for _it in range(8):
                pre_i = compose(g, gamma, sat)
                cur = meas(pngs, pre_i + "," + GRADE[angle])
                err = [tgt["rgb"][i] / max(1e-6, cur["rgb"][i]) for i in range(3)]
                g = [g[i] * err[i] for i in range(3)]
                if not args.fit_gamma_sat:
                    if max(abs(e - 1) for e in err) < 0.01:
                        break
                    continue
                # gamma / saturation from the same pass: measured, not guessed
                gd_now = gstats(pngs, compose(g, gamma, sat))
                step = 0.06
                if gd_now["p99"] > args.target_p99:
                    gamma = max(0.70, gamma * (1.0 - step))
                elif gd_now["p99"] < args.target_p99 * 0.90 and gamma < 1.0:
                    gamma = min(1.0, gamma * (1.0 + step * 0.5))
                if gd_now["sat_mean"] > args.target_sat:
                    sat = max(0.60, sat * (1.0 - step))
                elif gd_now["sat_mean"] < args.target_sat * 0.90 and sat < 1.0:
                    sat = min(1.0, sat * (1.0 + step * 0.5))
                if (max(abs(e - 1) for e in err) < 0.01
                        and abs(gd_now["p99"] - args.target_p99) < 6.0
                        and abs(gd_now["sat_mean"] - args.target_sat) < 1.2):
                    break
            if args.fit_gamma_sat:
                print("           fitted gamma %.3f sat %.3f (p95 %.0f p99 %.0f sat %.2f)"
                      % (gamma, sat, gd_now["p95"], gd_now["p99"], gd_now["sat_mean"]))
            curve = compose(g, gamma, sat)
            if args.auto_fit:
                # smallest correction that enters the band, searched gamma-major then sat
                found = None
                for gi in GAMMAS:
                    for si in SATS:
                        c = compose(g, gi, si)
                        gd = gstats(pngs, c)
                        if band_fail(gd, 24.0, 42.0):
                            continue
                        found = (gi, si, c, gd)
                        break
                    if found:
                        break
                if found:
                    gamma, sat, curve = found[0], found[1], found[2]
                    print("           auto-fit: gamma %.2f sat %.2f is the smallest correction in "
                          "band" % (gamma, sat))
                else:
                    gamma, sat = GAMMAS[-1], SATS[-1]
                    curve = compose(g, gamma, sat)
                    print("           auto-fit: NO correction in the search grid enters the band")
            after = meas(pngs, curve + "," + GRADE[angle])
            gs_b = {k: round(float(np.mean([gate_stats(frames(p, 0.0, "null," + GRADE[angle]))[k]
                                            for p in pngs])), 2)
                    for k in ("luma_mean", "p95", "p99", "sat_mean", "black_clip_pct")}
            gs_a = {k: round(float(np.mean([gate_stats(frames(p, 0.0, curve + "," + GRADE[angle]))[k]
                                            for p in pngs])), 2)
                    for k in ("luma_mean", "p95", "p99", "sat_mean", "black_clip_pct")}
            points.append({"t": round(ti, 3), "curve": curve,
                           "gamma": gamma, "sat": sat,
                           "gains": [round(x, 6) for x in g],
                           "samples_s": [round(t, 3) for t, _, _ in ts],
                           "before": {"luma": round(before["luma"], 3),
                                      "chroma": round(before["chroma"], 3),
                                      "rgb": [round(x, 3) for x in before["rgb"]]},
                           "after": {"luma": round(after["luma"], 3),
                                     "chroma": round(after["chroma"], 3),
                                     "rgb": [round(x, 3) for x in after["rgb"]]},
                           "gate_stats_before": gs_b, "gate_stats_after": gs_a})
            print("t=%8.1f s  before luma %6.2f -> after %6.2f (target %.2f)  gains %s"
                  % (ti, before["luma"], after["luma"], tgt["luma"],
                     [round(x, 4) for x in g]))
            print("           gate-domain: luma %6.2f p95 %5.1f p99 %5.1f sat %6.2f -> "
                  "luma %6.2f p95 %5.1f p99 %5.1f sat %6.2f"
                  % (gs_b["luma_mean"], gs_b["p95"], gs_b["p99"], gs_b["sat_mean"],
                     gs_a["luma_mean"], gs_a["p95"], gs_a["p99"], gs_a["sat_mean"]))
            all_before.append(before["luma"])
            all_after.append(after["luma"])

        tv = {"mode": "time_varying", "interpolation": "linear", "units": "source_seconds",
              "max_chunk_s": args.max_chunk_s, "points": points}
        print("\n== time-varying summary ==")
        print("  control points : %d" % len(points))
        print("  luma before    : %.2f - %.2f" % (min(all_before), max(all_before)))
        print("  luma after     : %.2f - %.2f (reference %.2f)"
              % (min(all_after), max(all_after), tgt["luma"]))
        print("  interpolation  : linear, max_chunk_s %.1f (renderer interpolates per chunk)"
              % args.max_chunk_s)
        tvrec = {
            "schema": "camera_match/2",
            "job_id": sm.get("job_id"),
            "angle": angle,
            "match_mode": "time_varying",
            "solved_by": "solve_camera_match.py --time-varying (per-job camera data)",
            "method": ("per-channel trim solved in the delivered domain at each control time and "
                       "inverted through the locked grade; interpolated continuously between "
                       "control points by the renderer (per rendered chunk on the frame grid)"),
            "scope": ("derived job/camera data for a source whose exposure DRIFTS over the "
                      "recording. Valid ONLY for the source identity recorded below; the approved "
                      "house look remains reusable policy."),
            "reference_mode": args.target,
            "house_look": {"grade_a": GRADE_A, "grade_b": GRADE_B, "geometry": GEOM,
                           "reference_record": args.house_record,
                           "reference_target_a": tgt_house},
            "source_identity": {
                "a_reel": {"path": args.a, "sha256": src_of.get(args.a)},
                "b_reel": [{"path": p, "sha256": src_of.get(p)} for p in args.b],
                "lag_a_to_b_s": lag,
                "reel_geometry": [{"name": c["name"], "duration_s": c["duration_s"],
                                   "reel_offset_cumulative_s": c["reel_offset_cumulative_s"]}
                                  for c in cards],
                "reel_end_s": reel_end,
                "hashes_reused_from": hash_sources},
            "solve": {"control_times_s": cts, "window_s": args.window,
                      "window_step_s": args.window_step,
                      "sat_correction": args.sat,
                      "target_used": tgt,
                      "job_a_vs_approved_a_luma": round(dl, 3),
                      "luma_before_range": [round(min(all_before), 3), round(max(all_before), 3)],
                      "luma_after_range": [round(min(all_after), 3), round(max(all_after), 3)]},
            "solved_pre_grade": tv,
            "validation": {"status": "PENDING", "note": "the rendered short proof gates this match on "
                                                        "both angles; see the job analysis dir"},
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        json.dump(tvrec, open(args.out, "w"), indent=1)
        print("wrote %s" % args.out)
        return 0

    rec = {
        "schema": "camera_match/1",
        "job_id": sm.get("job_id"),
        "angle": angle,
        "solved_by": "solve_camera_match.py (per-job camera match; house look unchanged)",
        "method": ("per-channel trim solved in the delivered domain and inverted through the "
                   "locked grade, exactly as the accepted Set 01 lineage (grade_calibrate3.py)"),
        "scope": ("derived job/camera data. Valid ONLY for the source identity recorded below. "
                  "The approved house look is reusable policy; this transform is not."),
        "reference_mode": args.target,
        "a_pre_record": args.a_pre_record,
        "house_look": {"grade_a": GRADE_A, "grade_b": GRADE_B, "geometry": GEOM,
                       "reference_record": args.house_record,
                       "reference_target_a": tgt_house},
        "source_identity": {
            "a_reel": {"path": args.a, "sha256": src_of.get(args.a)},
            "b_reel": [{"path": p, "sha256": src_of.get(p)} for p in args.b],
            "lag_a_to_b_s": lag,
            "reel_geometry": [{"name": c["name"], "duration_s": c["duration_s"],
                               "reel_offset_cumulative_s": c["reel_offset_cumulative_s"]}
                              for c in cards],
            "reel_end_s": reel_end,
            "hashes_reused_from": hash_sources,
        },
        "solve": {"program_samples_s": at, "heldout_samples_s": ho,
                  "measured": measured, "target_used": tgt,
                  "job_a_vs_approved_a_luma": round(dl, 3),
                  "iteration_trace": trace},
        "target_a": tgt,
        "no_pre_grade": nopre,
        "normalized": final,
        "heldout": {"a": ha, angle.lower(): hb},
        "final_gains": gains,
        "sat_correction": args.sat,
        "solved_pre_grade": pre,
        "predicted_gate_band": merged,
        "predicted_gate_failures": bad,
        "rule": ("the angle keeps its locked grade; the auto pre-grade is suppressed; the pre-grade "
                 "below is a per-channel trim solved in the delivered domain and inverted through "
                 "the grade"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(rec, open(args.out, "w"), indent=1)
    print("\npre_grade for the manifest:\n%s" % pre)
    print("wrote %s" % args.out)
    if args.json:
        print(json.dumps({k: rec[k] for k in ("final_gains", "predicted_gate_band",
                                              "predicted_gate_failures")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
