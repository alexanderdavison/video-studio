#!/usr/bin/env python3
"""qc_color.py — gross camera-match QC for a delivered program (Phase 6 colour gate).

Purpose: catch the class of error where a camera's colour path diverges so far from the
approved look that a human sees it immediately (club-dispatch-set01: B inserts delivered at
mean luma 135 instead of the accepted 28.5 — a duplicated shadow lift). It deliberately does
NOT enforce pixel equality between angles: A and B are different cameras and the match is a
LOOK, so the gate only checks that every B insert lands inside the measured band of the
APPROVED match, plus the delivery colour metadata.

Band defaults are DERIVED from the corrected accepted match, not guessed:
  accepted B inserts (duration_proof_4insert, test789_longform):
      luma mean 28.5-29.2 | p95 76-79 | p99 115-120 | clipped% 0.01-0.05
      black-clip% 31-34   | sat 14.0-15.2 | rgb ~[33.6-35.8, 27.8-29.7, 23.1-25.0]
  the defective delivery measured luma mean 135, p95 204, clipped% 0.7-0.9,
  black-clip% 0.0, sat 24.1-26.7.

Usage:
  qc_color.py --delivered FILE --basis BASIS.json [--json-out OUT] [--band FILE]
Exit: 0 = PASS, 1 = FAIL, 2 = error.
"""
import argparse
import array
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import camera_match as CM  # noqa: E402
import yaml  # noqa: E402

CROP = "crop=trunc(iw*0.7/2)*2:trunc(ih*0.7/2)*2"

DEFAULT_BAND = {
    "luma_mean": [20.0, 38.0],
    "p95_max": 120,
    "p99_max": 160,
    "clipped_pct_max": 0.30,
    "black_clip_pct_min": 8.0,
    "sat_mean": [8.0, 22.0],
    "color": {"range": "tv", "primaries": "bt709", "transfer": "bt709"},
    "source": "measured on the accepted B inserts of duration_proof_4insert / test789_longform",
}

# Angle A evidence band. WHY: the gate only inspected B because Set 01 A happened to BE the
# reference and therefore always matched itself. Set 02 A delivered 38.6 mean luma (up to 53 in
# the window Set 01 was calibrated on) against an approved A reference of 30.7 and no gate saw it.
# Derived the same way as the B band: from the A segments of the ACCEPTED Set 01 proof, with the
# same relative margins (luma +/-30%, p95/p99 headroom, black-clip floor, sat +/-45%).
DEFAULT_BAND_A = {
    "luma_mean": [24.0, 42.0],
    "p95_max": 132,
    "p99_max": 149,
    "clipped_pct_max": 0.30,
    "black_clip_pct_min": 7.0,
    "sat_mean": [8.0, 21.0],
    "color": {"range": "tv", "primaries": "bt709", "transfer": "bt709"},
    "source": ("derived from the 29 A segments of the ACCEPTED Set 01 proof (luma_mean 31.52-33.51, "
               "p95 80-87, p99 114-121, sat 13.25-14.82, black-clip 28.33-31.70, clipped <=0.017) with "
               "the same relative margins as the B band"),
}


def probe(path, entries):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=" + ",".join(entries),
                        "-of", "default=nw=1", path], capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def frame_stats(path, start, dur, scale="960x540", fps="1"):
    w, h = [int(v) for v in scale.split("x")]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-ss", "%.3f" % start, "-i", path, "-t", "%.3f" % dur,
           "-vf", "%s,scale=%s,fps=%s" % (CROP, scale, fps),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.run(cmd, capture_output=True)
    n_px = w * h
    raw = p.stdout
    n = len(raw) // (n_px * 3)
    if not n:
        return None
    hist = [0] * 256
    rs = gs = bs = 0
    sat = 0.0
    black = 0
    for i in range(n):
        buf = array.array("B")
        buf.frombytes(raw[i * n_px * 3:(i + 1) * n_px * 3])
        r = buf[0::3]
        g = buf[1::3]
        b = buf[2::3]
        rs += sum(r)
        gs += sum(g)
        bs += sum(b)
        for px in range(n_px):
            rp, gp, bp = r[px], g[px], b[px]
            y = (rp * 299 + gp * 587 + bp * 114) // 1000
            hist[y if y < 256 else 255] += 1
            if y < 16:
                black += 1
            sat += max(rp, gp, bp) - min(rp, gp, bp)
    total = n_px * n

    def pct(p):
        acc = 0
        want = total * p
        for v, c in enumerate(hist):
            acc += c
            if acc >= want:
                return v
        return 255

    return {"n_frames": n, "luma_mean": round(sum(i * c for i, c in enumerate(hist)) / total, 2),
            "p95": pct(0.95), "p99": pct(0.99),
            "clipped_pct": round(100.0 * sum(hist[235:]) / total, 3),
            "black_clip_pct": round(100.0 * black / total, 2),
            "sat_mean": round(sat / total, 2),
            "rgb": [round(rs / total, 2), round(gs / total, 2), round(bs / total, 2)]}


def in_band(v, lo, hi):
    return lo <= v <= hi


def match_identities(manifest_path):
    """Per-angle camera-match identity (mode + sha256) for a manifest's pre_grades entries.

    Identity is computed through the canonical resolver, so a time-varying entry hashes as one
    identity over its control points rather than as whichever fragment happens to be first.
    """
    man = yaml.safe_load(open(manifest_path))
    src = man.get("sources") or {}
    pg = man.get("pre_grades") or {}

    def ident(entry):
        return {"mode": CM.match_mode(entry),
                "sha256": CM.match_sha256(entry) if entry else None}

    a_key = src.get("a_reel")
    b_keys = src.get("b_reel") or []
    if isinstance(b_keys, str):
        b_keys = [b_keys]
    out = {"A": [], "B": []}
    if a_key:
        out["A"].append({"source": a_key, **ident(pg.get(a_key))})
    for k in b_keys:
        out["B"].append({"source": k, **ident(pg.get(k))})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delivered", required=True)
    ap.add_argument("--basis", required=True)
    ap.add_argument("--band", default=None, help="JSON band override (angle B)")
    ap.add_argument("--band-a", default=None, help="JSON band override (angle A)")
    ap.add_argument("--require-angles", default="B",
                    help="comma list of angles that MUST be present and pass (default B)")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--label", default="")
    ap.add_argument("--manifest", default=None,
                    help="the manifest that RENDERED this artifact; its per-angle camera-match "
                         "identities are recorded in the result, so an accepted QC result is bound "
                         "to an exact transform rather than to a lineage")
    a = ap.parse_args()
    if not Path(a.delivered).exists():
        print(json.dumps({"verdict": "ERROR", "error": "no such file: %s" % a.delivered}))
        sys.exit(2)

    band = dict(DEFAULT_BAND)
    if a.band:
        band.update(json.load(open(a.band)))
    band_a = dict(DEFAULT_BAND_A)
    if a.band_a:
        band_a.update(json.load(open(a.band_a)))
    bands = {"A": band_a, "B": band}
    required = [x.strip() for x in a.require_angles.split(",") if x.strip()]

    try:
        basis = json.load(open(a.basis))
    except Exception as exc:
        print(json.dumps({"verdict": "ERROR", "error": "cannot read basis: %s" % exc}))
        sys.exit(2)
    segs = basis.get("segments") or []
    present = sorted({s.get("angle") for s in segs if s.get("angle") in bands})
    missing = [x for x in required if x not in present]
    if missing:
        print(json.dumps({"verdict": "ERROR",
                          "error": "basis carries no %s segments" % ",".join(missing),
                          "angles_present": present, "required": required}))
        sys.exit(2)

    probed = probe(a.delivered, ["pix_fmt", "color_range", "color_primaries",
                                 "color_transfer", "color_space", "width", "height"])
    checks = []
    want = band["color"]
    for k in ("range", "primaries", "transfer"):
        ok = probed.get("color_" + k) == want[k]
        checks.append({"check": "color_%s" % k, "pass": ok,
                       "got": probed.get("color_" + k), "want": want[k]})

    rows = []
    for s in segs:
        ang = s.get("angle")
        if ang not in bands:
            continue
        t0 = s.get("proof_in", s.get("timeline_in"))
        t1 = s.get("proof_out", s.get("timeline_out"))
        if t0 is None or t1 is None:
            continue
        st = frame_stats(a.delivered, t0, min(t1 - t0, 12.0))
        if st is None:
            rows.append({"id": s.get("id"), "error": "no frames decoded"})
            continue
        st["id"] = s.get("id")
        st["angle"] = ang
        st["t_in"], st["t_out"] = t0, t1
        band = bands[ang]
        bad = []
        if not in_band(st["luma_mean"], *band["luma_mean"]):
            bad.append("luma_mean %s outside %s" % (st["luma_mean"], band["luma_mean"]))
        if st["p95"] > band["p95_max"]:
            bad.append("p95 %s > %s" % (st["p95"], band["p95_max"]))
        if st["p99"] > band["p99_max"]:
            bad.append("p99 %s > %s" % (st["p99"], band["p99_max"]))
        if st["clipped_pct"] > band["clipped_pct_max"]:
            bad.append("clipped%% %s > %s" % (st["clipped_pct"], band["clipped_pct_max"]))
        if st["black_clip_pct"] < band["black_clip_pct_min"]:
            bad.append("black-clip%% %s < %s (image lifted off the floor)"
                       % (st["black_clip_pct"], band["black_clip_pct_min"]))
        if not in_band(st["sat_mean"], *band["sat_mean"]):
            bad.append("sat %s outside %s" % (st["sat_mean"], band["sat_mean"]))
        st["pass"] = not bad
        st["failures"] = bad
        rows.append(st)

    measured = [r for r in rows if "error" not in r]
    delivered_sha = ""
    if Path(a.delivered).exists():
        _o = subprocess.run(["sha256sum", a.delivered], capture_output=True, text=True).stdout.split()
        delivered_sha = _o[0] if _o else ""
    b_rows = [r for r in measured if r.get("angle") == "B"]
    a_rows = [r for r in measured if r.get("angle") == "A"]
    angles_measured = sorted({r.get("angle") for r in measured})
    missing_now = [x for x in required if x not in angles_measured]
    failed = [r for r in measured if not r.get("pass")]
    verdict = "PASS" if (measured and not failed and not missing_now
                         and all(c["pass"] for c in checks)) else "FAIL"
    out = {"verdict": verdict, "label": a.label, "delivered_file": a.delivered,
           "delivered_sha256": delivered_sha,
           "b_inserts_checked": len(b_rows), "a_segments_checked": len(a_rows),
           "angles_measured": angles_measured, "required_angles": required,
           "band": band, "band_a": band_a, "color_checks": checks,
           "camera_matches": (match_identities(a.manifest) if a.manifest else None),
           "b_inserts": b_rows, "a_segments": a_rows, "inserts": rows}
    if verdict == "FAIL":
        out["reason"] = ("angle-band failure: %s | missing required angles: %s"
                          % (json.dumps([{"id": r.get("id"), "angle": r.get("angle"),
                                          "failures": r.get("failures")} for r in failed])[:400],
                             missing_now))
    if a.json_out:
        json.dump(out, open(a.json_out, "w"), indent=1)
    print(json.dumps(out, indent=1))
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
