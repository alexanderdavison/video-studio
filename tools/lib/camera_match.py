#!/usr/bin/env python3
"""camera_match.py — ONE meaning for a manifest `pre_grades` entry.

The proxy renderer and the delivery renderer consume the SAME manifest field, so that field can
only have one meaning. It has two forms:

  * STATIC       — an ffmpeg filter fragment: a `curves=` LUT plus optional eq terms.
  * TIME_VARYING — control points in SOURCE time, each holding its own filter fragment, blended
                   between them.

This module IS that meaning. Both renderers import it, so a manifest cannot be read one way by the
approval proxy and another way by the delivery renderer — which is exactly how the delivery
renderer came to refuse a time-varying entry outright while the proxy rendered it.

WHY A TIME-VARYING MATCH EXISTS (2026-09-15): a camera whose exposure drifts during a 40-minute
recording cannot be normalised by one static transform. club-dispatch-set02 A measured luma
71 -> 28 across the set where Set 01 A sat flat at 29-32, so any single curve either crushes the
tail or leaves the head blown. A job may therefore carry a TIME-VARYING match: control points in
SOURCE time, each holding a solved curve, with continuous interpolation between them. It is
TECHNICAL camera normalisation, not a creative grade: it sits before the shared creative grade and
never replaces it.

ffmpeg is the constraint that shapes this. The eq filter in this build does NOT evaluate t in its
expressions (measured: brightness=0.05 gives +14 luma, brightness=0.05*t changes nothing, and a
piecewise form is a parse error), and curves is a static LUT. So the transform is interpolated per
RENDERED CHUNK on the frame grid: each chunk takes the curve evaluated at the MIDPOINT of its own
source window, and chunk length is capped (max_chunk_s) so one step is a fraction of a luma.
Exactly one technical normalisation per angle, applied before the shared creative grade, and the
chunk joins are reported as processing boundaries (they are not timeline cuts).
"""
import hashlib
import json
import re

CURVE_RE = re.compile(r"(r|g|b|all)='([^']*)'")
EQ_RE = re.compile(r"eq=([A-Za-z0-9_]+)=([0-9.]+)")


def split_pre(pre):
    """(curves LUT string, {eq parameter: value}) from a pre-grade filter fragment.

    The curves LUT never contains a comma (points are space separated, channels colon separated),
    so a comma split cleanly separates the LUT from any appended eq terms.
    """
    lut, extras = "", {}
    for part in (pre or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("curves="):
            lut += part
            continue
        m = EQ_RE.match(part)
        if m:
            extras[m.group(1)] = float(m.group(2))
    return lut, extras


def join_pre(lut, extras):
    out = lut
    for k, v in extras.items():
        out += ",eq=%s=%.4f" % (k, v)
    return out


def parse_curve(curve):
    """{channel: [(x, y), ...]} from a curves= pre-grade string."""
    out = {}
    for ch, body in CURVE_RE.findall(curve or ""):
        pts = []
        for pair in body.split():
            if "/" in pair:
                x, y = pair.split("/", 1)
                try:
                    pts.append((float(x), float(y)))
                except ValueError:
                    pass
        if pts:
            out[ch] = pts
    return out


def curve_str(chans):
    return "curves=" + ":".join("%s='%s'" % (ch, " ".join("%.4f/%.4f" % (x, y) for x, y in pts))
                                for ch, pts in chans.items())


def interp_curve(c1, c2, f):
    """Linear blend of two pre-grade fragments at fraction f.

    Blends BOTH halves: the per-channel LUT and any eq parameters (gamma, saturation). A parameter
    present on only one side holds that side's value, so a term cannot be dropped by interpolation.
    """
    if f <= 0.0:
        return c1
    if f >= 1.0:
        return c2
    lut1, ex1 = split_pre(c1)
    lut2, ex2 = split_pre(c2)
    a, b = parse_curve(lut1), parse_curve(lut2)
    out = {}
    for ch, pa in a.items():
        pb = b.get(ch) or b.get("all") or pa
        ys = []
        for i, (x, y) in enumerate(pa):
            yb = pb[i][1] if i < len(pb) else y
            ys.append(y + (yb - y) * f)
        out[ch] = list(zip([p[0] for p in pa], ys))
    blended = {}
    for k in set(ex1) | set(ex2):
        v1 = ex1.get(k, ex2.get(k))
        v2 = ex2.get(k, ex1.get(k))
        blended[k] = v1 + (v2 - v1) * f
    return join_pre(curve_str(out), blended)


def curve_at(points, t):
    """The interpolated curve for source time t over [{t, curve}, ...] control points."""
    def _point_curve(p):
        curve = str(p.get("curve") or "")
        lut, extras = split_pre(curve)
        if not lut:
            return curve
        merged = dict(extras)
        gamma = p.get("gamma")
        sat = p.get("sat", p.get("saturation"))
        if "gamma" not in merged and gamma is not None:
            merged["gamma"] = float(gamma)
        if "saturation" not in merged and sat is not None:
            merged["saturation"] = float(sat)
        if merged == extras:
            return curve
        return join_pre(lut, merged)

    pts = sorted([{"t": float(p["t"]), "curve": _point_curve(p)} for p in points],
                 key=lambda p: p["t"])
    if not pts:
        return None
    if t <= pts[0]["t"]:
        return pts[0]["curve"]
    if t >= pts[-1]["t"]:
        return pts[-1]["curve"]
    for i in range(len(pts) - 1):
        t0, t1 = pts[i]["t"], pts[i + 1]["t"]
        if t0 <= t <= t1:
            f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
            return interp_curve(pts[i]["curve"], pts[i + 1]["curve"], f)
    return pts[-1]["curve"]


def is_time_varying(entry):
    return (isinstance(entry, dict) and entry.get("mode") == "time_varying"
            and entry.get("points"))


def tv_chunks(entry, l_in, l_out, abs_off, fps, max_s):
    """Split one timeline segment into frame-aligned chunks, each with its own interpolated curve.

    Chunk boundaries fall on the GLOBAL program frame grid (same rule as the single-segment path),
    so the concatenated chunk frames land exactly where the segment itself would have.
    """
    f0 = int(round(abs_off * fps))
    f1 = int(round((abs_off + (l_out - l_in)) * fps))
    if f1 <= f0:
        return []
    k = max(1, int(round(max_s * fps)))
    out = []
    f = f0
    while f < f1:
        nf = min(k, f1 - f)
        a, b = f / fps, (f + nf) / fps
        li = l_in + (a - abs_off)
        lo = l_in + (b - abs_off)
        out.append({"l_in": li, "l_out": lo, "abs_off": a, "frames": nf,
                    "curve": curve_at(entry["points"], (li + lo) / 2.0)})
        f += nf
    return out


# ---------------------------------------------------------------------------------------------
# Reporting / identity helpers — a time-varying entry has no single filter string, so anything
# that has to compare, hash or scan "the chain" goes through these instead of assuming a string.
# ---------------------------------------------------------------------------------------------

def match_mode(entry):
    """'time_varying', 'static' or 'none' for a pre_grades entry."""
    if is_time_varying(entry):
        return "time_varying"
    return "static" if entry else "none"


def curve_texts(entry):
    """Every filter fragment the entry applies: one for static, one per control point for tv."""
    if is_time_varying(entry):
        return [str(p.get("curve") or "") for p in entry["points"]]
    return [entry] if entry else []


def chain_text(entry):
    """The entry's fragments as one scannable string (range/format invariant checks)."""
    return "".join(curve_texts(entry))


def match_identity(entry):
    """A hashable canonical identity — what "the SAME transform" means for duplicate detection.

    Returns the filter string unchanged for a static entry, so every existing comparison keeps its
    exact behaviour, and a canonical JSON form for a time-varying one.
    """
    if isinstance(entry, dict):
        return json.dumps(entry, sort_keys=True)
    return entry or ""


def match_sha256(entry):
    """sha256 of the canonical form — the hash a manifest/provenance binding records."""
    return hashlib.sha256(match_identity(entry).encode()).hexdigest()


def describe_match(entry):
    """The entry as it should appear in a chain report or a provenance record.

    A static entry IS its filter string and is reported unchanged (existing reports and regressions
    read that string). A time-varying entry has no single filter string, so it is described by what
    the renderer will actually do with it.
    """
    if not is_time_varying(entry):
        return entry
    pts = sorted(entry["points"], key=lambda p: float(p["t"]))
    return {"mode": "time_varying",
            "interpolation": entry.get("interpolation") or "linear",
            "units": entry.get("units") or "source_seconds",
            "max_chunk_s": entry.get("max_chunk_s"),
            "control_points": len(pts),
            "control_times_s": [float(p["t"]) for p in pts],
            "curve_at_first_control": pts[0]["curve"],
            "curve_at_last_control": pts[-1]["curve"],
            "curve_sha256": match_sha256(entry),
            "applied": ("interpolated at each rendered chunk's midpoint, chunks frame-aligned on "
                        "the program grid and capped at max_chunk_s of source time")}
