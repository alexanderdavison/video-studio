#!/usr/bin/env python3
"""candidate_score.py — deterministic CANDIDATE SCORER for the DJ video editor.

P1 of the editorial layer. It does NOT decide the edit. For each musical
boundary event it emits a SET of legal choices, each carrying eight 0..1 score
keys computed only from evidence that already exists on disk (beat grid, action
profile, visual signals, phrase map, optional keyframes / sync artifact). No
model, no API, no network, no randomness — rerunning on identical inputs gives
byte-identical output.

Per event:
  b_early      SWITCH to angle B one beat BEFORE the boundary (enter before the action)
  b_downbeat   SWITCH to angle B exactly ON the boundary
  hold_a       MANDATORY: stay on angle A (action HOLD)

The eight score keys (identical for every candidate, including hold_a):

  sync        confidence in the A/B alignment (sync_multicam.py artifact, else
              1.0 because the mix DECLARES the sources synced)
  musical     distance of the candidate boundary to the nearest beat / downbeat
              / phrase boundary (phrase_map.py output)
  action      mean action_profile.py segment score overlapping the window
  quality     mean keyframe sharpness/brightness (studio.db `keyframes`) or the
              visual_signals sharpness/brightness summary
  info_gain   1 - mean histogram similarity between the B window and the
              surrounding A window (does B actually show something different)
  continuity  1.0 if the candidate respects minimum_a_recovery and keeps
              monotonic B source order, else the measured shortfall
  novelty     seconds since this camera was last on screen, saturating at a cap
  coverage    0 if any sampled frame in the window is obstruction/frozen, else a
              graded clean fraction

`recommended_by_score` is reported FOR INFORMATION ONLY — it is the argmax of a
fixed weighted sum, i.e. evidence, not the creative answer. The editor chooses.

Usage:
  candidate_score.py --beats BEATS.json --action-profile P.json --signals S.json
      [--signals-a A.json] [--phrases P.json]
      [--min-b 4 --max-b 14 --min-recovery 8] [--events N]
      --out candidates.json [--json]

Exit: 0 = written; 2 = error.
"""

import argparse
import json
import os
import sqlite3
import sys
from bisect import bisect_right

import numpy as np

PRINCIPLE = ("Scores are evidence, not the edit. They rank the legal choices "
             "for a boundary; they do not choose. hold_a is always offered and "
             "must always be weighed by the editor.")

TOOL = "candidate_score.py"

WEIGHTS = {
    "sync": 0.10,
    "musical": 0.15,
    "action": 0.20,
    "quality": 0.10,
    "info_gain": 0.15,
    "continuity": 0.10,
    "novelty": 0.05,
    "coverage": 0.15,
}

SCORE_KEYS = list(WEIGHTS.keys())

# musical alignment tolerances (s) and weights per boundary class
TOL_BEAT = 0.12
TOL_DOWNBEAT = 0.25
TOL_PHRASE = 0.50
W_BEAT = 0.75
W_DOWNBEAT = 0.90
W_PHRASE = 1.00

NOVELTY_CAP = 30.0        # seconds since the camera was last on screen -> saturates
OBSTRUCTION_FLAG = 0.5
SYNC_CONF_SAT = 5.0       # sync_multicam confidence that counts as "fully sure"
UNCOVERED = 0.5           # coverage when no visual samples exist in the window
SEARCH_STEP = 1.0         # seconds between candidate B window starts


# ---------------------------------------------------------------- loading ----

def r3(x):
    return None if x is None else round(float(x), 3)


def c01(x):
    return float(max(0.0, min(1.0, x)))


def load_beats(path):
    d = json.load(open(path))
    raw = d.get("beats") or d.get("beat_times") or []
    beats = []
    for b in raw:
        t = b.get("time", b.get("t")) if isinstance(b, dict) else b
        try:
            beats.append(float(t))
        except (TypeError, ValueError):
            continue
    return sorted(set(beats)), d.get("bpm")


def load_action_profiles(paths, virtual_reel=False):
    """Merge action_profile.py files into one (start, end, score) segment list.

    With --virtual-reel, each successive profile is shifted by the running
    duration of the ones before it (multi-file B reels concatenate in order).
    """
    segs, offset, meta = [], 0.0, []
    for p in paths:
        d = json.load(open(p))
        dur = float(d.get("duration_s") or 0.0)
        for s in d.get("segments", []):
            a = float(s["start"]) + (offset if virtual_reel else 0.0)
            b = float(s["end"]) + (offset if virtual_reel else 0.0)
            segs.append((a, b, float(s.get("score", 0.0))))
        meta.append({"path": p, "reel": d.get("reel"),
                     "duration_s": dur, "offset": offset if virtual_reel else 0.0,
                     "segments": len(d.get("segments", []))})
        offset += dur
    segs.sort(key=lambda x: (x[0], x[1]))
    return segs, meta


def load_phrases(path):
    if not path or not os.path.exists(path):
        return None, None
    d = json.load(open(path))
    return d, d


def derive_structure(beats, phrases_doc, beats_per_bar=4, bars_per_phrase=8):
    """Downbeats + phrase starts/ends, from phrase_map.py output when present,
    else derived from the beat grid with the nominal phase (documented)."""
    if phrases_doc and phrases_doc.get("downbeats"):
        down = [float(x) for x in phrases_doc["downbeats"]]
        phrases = phrases_doc.get("phrases", [])
        pstarts = [float(p["start"]) for p in phrases]
        pends = [float(p["end"]) for p in phrases]
        bpb = int(phrases_doc.get("beats_per_bar", beats_per_bar))
        bpp = int(phrases_doc.get("bars_per_phrase", bars_per_phrase))
        src = "phrase_map.py"
    else:
        down = [t for i, t in enumerate(beats) if i % beats_per_bar == 0]
        bpb, bpp = beats_per_bar, bars_per_phrase
        phrases, pstarts, pends = [], [], []
        for i in range(0, len(down), bpp):
            st = down[i]
            en = down[i + bpp] if i + bpp < len(down) else (beats[-1] if beats else st)
            pstarts.append(st)
            pends.append(en)
            phrases.append({"id": "phrase_%03d" % (len(phrases) + 1),
                            "start": st, "end": en, "bars": None,
                            "start_bar": i})
        src = "derived from beat grid (nominal 4/4, %d bars/phrase)" % bpp
    return {"downbeats": down, "phrase_starts": pstarts, "phrase_ends": pends,
            "phrases": phrases, "beats_per_bar": bpb, "bars_per_phrase": bpp,
            "source": src}


def load_signals(paths):
    """Merge visual_signals.py outputs -> list of dicts with a source tag."""
    out = []
    for p in paths or []:
        d = json.load(open(p))
        samples = [s for s in d.get("samples", [])
                   if isinstance(s.get("t"), (int, float))
                   and isinstance(s.get("brightness"), (int, float))]
        out.append({"path": p, "source": d.get("source"),
                    "summary": d.get("summary", {}), "samples": samples})
    return out


def load_sync(path):
    if not path or not os.path.exists(path):
        return None
    return json.load(open(path))


def keyframe_quality(db_path, clip_id):
    """Quality evidence from studio.db `keyframes` (read-only).

    Returns {"usable": bool, ...} — `usable` is False when the clip has keyframe
    rows but none carry a sharpness (this tester's DJI clips do exactly that),
    in which case candidate_score falls back to visual_signals.
    """
    if not db_path or clip_id is None or not os.path.exists(db_path):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        total = con.execute("SELECT COUNT(*) FROM keyframes WHERE clip_id=?",
                            (int(clip_id),)).fetchone()[0]
        rows = con.execute(
            "SELECT sharpness, brightness FROM keyframes "
            "WHERE clip_id=? AND sharpness IS NOT NULL", (int(clip_id),)).fetchall()
        con.close()
    except sqlite3.Error:
        return None
    if not rows:
        return {"usable": False, "clip_id": int(clip_id), "rows_total": total,
                "rows_with_sharpness": 0,
                "reason": ("clip %d has %d keyframe rows but no sharpness values "
                           "-> quality falls back to visual_signals" % (int(clip_id), total))}
    sharp = [float(r[0]) for r in rows if r[0] is not None]
    bright = [float(r[1]) for r in rows if r[1] is not None]
    return {"usable": True, "clip_id": int(clip_id), "rows_total": total,
            "rows_with_sharpness": len(rows),
            "mean_sharpness": float(np.mean(sharp)) if sharp else None,
            "mean_brightness": float(np.mean(bright)) if bright else None,
            "p90_sharpness": float(np.percentile(sharp, 90)) if sharp else None}


# ------------------------------------------------------------- evidence -----

def action_mean(segs, a, b):
    """Duration-weighted mean action score over segments overlapping [a, b]."""
    num = den = 0.0
    for s0, s1, sc in segs:
        ov = min(b, s1) - max(a, s0)
        if ov > 0:
            num += sc * ov
            den += ov
    return (num / den) if den > 0 else None


def samples_between(sig, a, b):
    return [s for s in sig["samples"] if a - 1e-6 <= s["t"] <= b + 1e-6]


def musical_score(tl_start, structure):
    beats = structure["_beats"]
    down = structure["downbeats"]
    pst = structure["phrase_starts"]
    d_beat = min(abs(tl_start - t) for t in beats) if beats else 1e9
    d_down = min(abs(tl_start - t) for t in down) if down else 1e9
    d_phr = min(abs(tl_start - t) for t in pst) if pst else 1e9
    cand = [W_BEAT * (1.0 - min(1.0, d_beat / TOL_BEAT)),
            W_DOWNBEAT * (1.0 - min(1.0, d_down / TOL_DOWNBEAT)),
            W_PHRASE * (1.0 - min(1.0, d_phr / TOL_PHRASE))]
    return c01(max(cand)), {"d_beat": r3(d_beat), "d_downbeat": r3(d_down),
                            "d_phrase": r3(d_phr)}


def hist_similarity(sig_a, a_lo, a_hi, sig_b, b_lo, b_hi):
    """Mean histogram intersection between paired samples of two windows."""
    wa = samples_between(sig_a, a_lo, a_hi)
    wb = samples_between(sig_b, b_lo, b_hi)
    n = min(len(wa), len(wb))
    if n == 0:
        return None, 0
    sims = []
    for i in range(n):
        ha = wa[i].get("hist")
        hb = wb[i].get("hist")
        if not ha or not hb:
            continue
        sims.append(float(np.minimum(np.asarray(ha), np.asarray(hb)).sum()))
    if not sims:
        return None, 0
    return float(np.mean(sims)), len(sims)


def window_visual(sig, a, b, summary_key="sharpness_p90"):
    ws = samples_between(sig, a, b)
    if not ws:
        return None
    obs = [s["obstruction"] for s in ws if isinstance(s.get("obstruction"), (int, float))]
    frz = [bool(s.get("frozen")) for s in ws]
    clean = [1 for s in ws
             if isinstance(s.get("obstruction"), (int, float))
             and s["obstruction"] < OBSTRUCTION_FLAG and not s.get("frozen")]
    sharp = [s["sharpness"] for s in ws if isinstance(s.get("sharpness"), (int, float))]
    bright = [s["brightness"] for s in ws if isinstance(s.get("brightness"), (int, float))]
    return {"n": len(ws), "coverage": c01(float(len(clean)) / len(ws)),
            "any_flagged": any((isinstance(o, float) and o >= OBSTRUCTION_FLAG) for o in obs)
                           or any(frz),
            "mean_sharpness": float(np.mean(sharp)) if sharp else None,
            "mean_brightness": float(np.mean(bright)) if bright else None}


def quality_score(win, sig_summary, kf):
    """Blend sharpness (vs a p90 reference) and brightness adequacy."""
    if kf and kf.get("usable") and kf.get("p90_sharpness") and kf.get("mean_sharpness") is not None:
        sharp_term = c01(kf["mean_sharpness"] / kf["p90_sharpness"])
        ref_b = kf.get("mean_brightness") or 128.0
        bright_term = c01(1.0 - abs(ref_b - 128.0) / 128.0)
        return c01(0.5 * sharp_term + 0.5 * bright_term), "studio.db keyframes"
    if win and win.get("mean_sharpness") is not None:
        ref = (sig_summary or {}).get("sharpness", {}).get("p90") or 1.0
        sharp_term = c01(win["mean_sharpness"] / ref) if ref else 0.0
        b = win.get("mean_brightness")
        bright_term = c01(1.0 - abs(b - 128.0) / 128.0) if b is not None else 0.0
        return c01(0.5 * sharp_term + 0.5 * bright_term), "visual_signals sharpness/brightness"
    return 0.0, "no evidence"


# ---------------------------------------------------------------- events ----

def pick_events(beats, structure, start, end, want, min_gap):
    """Boundary moments inside [start, end], spaced >= min_gap, deterministic."""
    pool = [t for t in structure["downbeats"] if start <= t <= end]
    kind = "downbeat"
    if len(pool) < want:
        pool = [t for t in beats if start <= t <= end]
        kind = "beat"
    chosen, last = [], None
    for t in pool:
        if last is None or (t - last) >= min_gap - 1e-9:
            chosen.append(t)
            last = t
        if len(chosen) >= want:
            break
    return chosen, kind


def pick_b_window(segs, lo, hi, min_b, max_b, used, cursor=None):
    """Best (start, end) B source window on the virtual reel by mean action.

    Window starts step SEARCH_STEP; duration scans min_b..max_b at 1 s. Ranges
    overlapping `used` are skipped, and with `cursor` the window must also start
    at/after it — that is what keeps B SOURCE ORDER monotonic across events.
    Ties -> earliest start, then shortest duration.
    """
    floor = lo if cursor is None else max(lo, cursor)
    best = None
    for enforce_order, bound in ((True, floor), (False, lo)):
        s = bound
        while s <= hi - min_b + 1e-9:
            dur = float(min_b)
            while dur <= min(max_b, hi - s) + 1e-9:
                e = s + dur
                if not any(not (e <= u0 or s >= u1) for u0, u1 in used):
                    sc = action_mean(segs, s, e)
                    key = (-(sc if sc is not None else -1.0), s, dur)
                    if best is None or key < best[0]:
                        best = (key, s, e, sc, enforce_order)
                dur += 1.0
            s += SEARCH_STEP
        if best is not None:
            return best[1], best[2], best[3], best[4]
    # nothing legal at all: relax the used-spans rule
    s = lo
    while s <= hi - min_b + 1e-9:
        e = min(s + float(max_b), hi)
        sc = action_mean(segs, s, e)
        key = (-(sc if sc is not None else -1.0), s, e - s)
        if best is None or key < best[0]:
            best = (key, s, e, sc, False)
        s += SEARCH_STEP
    return best[1], best[2], best[3], best[4]


ACTION_FRAC = 0.6          # "in the action run": motion >= 60% of the window peak
MOTION_MIN_SAMPLES = 4     # fewer 1 Hz samples than this -> no usable motion evidence

DURATION_RULES = {
    "b_early": "anticipate the boundary by one beat, carrying this event's own "
               "B_ACTION length (its action peak lands ON the boundary)",
    "b_glance": "brief useful detail — exit on the first BEAT at/after the strongest "
                "visible movement in the slot",
    "b_action": "hold through the visible action — exit on the first BEAT at/after the "
                "end of the contiguous action run around that peak",
    "b_hold": "continue past the action to the next useful musical boundary — exit on "
              "the first DOWNBEAT at/after the action run end",
    "hold_a": "stay on angle A (always offered)",
    "endpoint_provenance": "every endpoint is a beat or downbeat of the same beat/phrase "
                           "artifact the cut grid uses; every length is clamped to "
                           "minimum_b_shot, maximum_b_shot and the remaining slot width, "
                           "and the clamp that fired is recorded per candidate",
}

# ------------------------------------------- synchronous multicam entry -------
# Accepted 2026-09-11 — see ish-d/creative/decisions/
# 2026-09-11-synchronous-multicam-candidate-defect.md
#
#   Camera selection may change perspective. It must not change performance time.
#     A cut: timeline_position - source_in = 0
#     B cut: timeline_position - source_in = SYNC_LAG_S
#
# With --sync-anchored-entry the B entry is DERIVED from the event's performance
# timestamp and is never searched. Action evidence may decide how long a shot runs;
# it may not decide when it starts. The historical per-event slot search is the
# defect this replaces: it walked ahead of the event position by +1.162 s per event
# and then moved the window further on an action argmax, displacing the shipped
# source by up to 11.837 s from the moment it claimed to show.

SYNC_LAG_S = 1.2783              # LEGACY fallback: Set 01's accepted mapping
FRAME_SNAP_MAX_S = 0.036         # accepted budget: mapping uncertainty 0.0021 s + ~1 frame
# ---- per-job camera sync map (v1.1, 2026-09-14) --------------------------------
# The offset is JOB DATA, not a house constant. With --sync-map this tool resolves the lag
# and the reel geometry from the job's canonical map; the legacy value above applies ONLY
# when no map is supplied, which is what keeps a Set 01 re-run byte-identical.
SYNC_MAP = None


def apply_sync_map(path):
    """Load and validate the job's canonical camera sync map. Fail-closed, no fallback."""
    global SYNC_LAG_S, SYNC_MAP
    if not path:
        return None
    if not os.path.exists(path):
        raise SystemExit("SYNC MAP MISSING: %s — refusing to build candidates on an "
                         "unresolved timebase" % path)
    m = json.load(open(path))
    problems = []
    if m.get("schema_version") != 1:
        problems.append("unsupported schema_version=%r" % m.get("schema_version"))
    if m.get("reference_angle") != "A":
        problems.append("reference_angle must be A")
    cards = m.get("cards") or []
    if not cards:
        problems.append("the sync map carries no B cards")
    tol = m.get("tolerance_s")
    if tol is None or float(tol) < 0 or float(tol) > FRAME_SNAP_MAX_S:
        problems.append("tolerance_s %r is missing, negative or above the frame budget %.3f"
                        % (tol, FRAME_SNAP_MAX_S))
    prev_end = None
    for c in cards:
        off, dur = c.get("a_time_at_b_zero"), c.get("duration_s")
        if off is None or float(off) < 0:
            problems.append("%s has no valid a_time_at_b_zero" % c.get("name"))
            continue
        if not dur or float(dur) <= 0:
            problems.append("%s has no positive duration_s" % c.get("name"))
            continue
        if prev_end is not None and abs(float(off) - prev_end) > FRAME_SNAP_MAX_S:
            problems.append("%s is discontinuous with the previous card (%.4f vs %.4f)"
                            % (c.get("name"), float(off), prev_end))
        prev_end = float(off) + float(dur)
    lag = m.get("lag_a_to_b_s", cards[0].get("a_time_at_b_zero") if cards else None)
    if lag is None or float(lag) <= 0:
        problems.append("job lag is missing or not positive (sign convention: "
                        "A_time = B_time + a_time_at_b_zero)")
    if problems:
        raise SystemExit("SYNC MAP INVALID (%s): %s" % (path, "; ".join(problems)))
    SYNC_LAG_S = float(lag)
    SYNC_MAP = m
    return m
B_FPS_DEFAULT = 30000.0 / 1001.0  # used only for the frame snap
COVERAGE_NEAR_S = 1.0            # a clean sampled frame this close to the entry is required


def frame_snap(t, fps):
    """Snap a source time to the nearest frame of the B reel.

    Bounded and reported, never a search: at 29.97 fps the move is at most 0.017 s,
    which sits inside the accepted +/-0.036 s budget.
    """
    if fps <= 0:
        return float(t), 0.0
    snapped = round(float(t) * fps) / fps
    return snapped, snapped - float(t)


def b_entry_coverage(sig, entry, reel_end):
    """Is there valid B coverage at the synchronized entry?

    Strict by design. Outside the reel, or no clean sampled frame at the entry, means
    the event offers HOLD_A only — the generator does not search nearby B footage for
    an alternative moment. Returns (ok, reason); the reason is recorded either way.
    """
    if entry < 0.0:
        return False, ("mapped B timestamp %.3f s is before the start of the B reel" % entry)
    if entry > reel_end:
        return False, ("mapped B timestamp %.3f s is past the end of the B reel (%.3f s)"
                       % (entry, reel_end))
    if not sig:
        return False, ("no visual signals supplied, so coverage at the mapped timestamp "
                       "cannot be verified")
    best, dist = None, None
    for s in sig.get("samples") or []:
        tv = s.get("t")
        if not isinstance(tv, (int, float)):
            continue
        d = abs(float(tv) - entry)
        if dist is None or d < dist:
            best, dist = s, d
    if best is None:
        return False, ("the signals carry no samples, so coverage at the mapped timestamp "
                       "cannot be verified")
    if dist > COVERAGE_NEAR_S:
        return False, ("no visual sample within %.1f s of the mapped timestamp (nearest is "
                       "%.3f s away)" % (COVERAGE_NEAR_S, dist))
    if best.get("frozen"):
        return False, ("the B reel is frozen at the mapped timestamp (sample %.3f s)"
                       % float(best["t"]))
    ob = best.get("obstruction")
    if isinstance(ob, (int, float)) and ob >= OBSTRUCTION_FLAG:
        return False, ("the B reel is obstructed at the mapped timestamp (sample %.3f s, "
                       "obstruction %.2f)" % (float(best["t"]), ob))
    return True, ("clean sample %.3f s, %.3f s from the entry" % (float(best["t"]), dist))


def _first_at_or_after(grid, t):
    """First value of a sorted grid at/after t, or None past the end."""
    i = bisect_right(grid, t - 1e-9)
    return grid[i] if i < len(grid) else None


def motion_evidence(sig, lo, hi):
    """1 Hz motion samples of the B window [lo, hi] — the only per-second
    activity evidence that exists for a B window. None when there is too little."""
    if not sig:
        return None
    ws = [(float(s["t"]), float(s["motion"]))
          for s in sig["samples"]
          if lo - 1e-6 <= float(s["t"]) <= hi + 1e-6
          and isinstance(s.get("motion"), (int, float))]
    return ws if len(ws) >= MOTION_MIN_SAMPLES else None


def derive_durations(sig, start, dmax, min_b, max_b, beats, downbeats, span_label="slot width"):
    """DURATION CHOICE: three semantic lengths for one event, from footage + policy.

    The endpoint of each level is a real landmark, and the landmark is what names
    the level — none of them is a fixed bucket:

      B_GLANCE  brief useful detail    -> the first BEAT at/after the strongest
                                          visible movement in the slot (the detail
                                          has been delivered; leave on the grid)
      B_ACTION  hold through the action-> the first BEAT at/after the end of the
                                          contiguous action run around that peak
                                          (the visible movement has decayed)
      B_HOLD    next useful boundary   -> the first DOWNBEAT at/after the action
                                          run end (continue past the action to a
                                          musical landing)

    Every duration is then clamped to the binding policy: >= minimum_b_shot, and
    <= min(maximum_b_shot, remaining slot width). The clamp that fired is recorded
    so a derived length can never be mistaken for a floor or a cap. Levels are
    forced monotonic (glance <= action <= hold) and collapses are reported.

    Returns None when the window carries too little sampled evidence, in which case
    the caller must say so rather than invent a length.
    """
    ev = motion_evidence(sig, start, start + dmax)
    if not ev:
        return None
    period = (ev[1][0] - ev[0][0]) if len(ev) > 1 else 1.0
    pk_i = max(range(len(ev)), key=lambda i: (ev[i][1], -i))
    pk_t, pk_m = ev[pk_i]
    thr = ACTION_FRAC * pk_m
    lo_i = hi_i = pk_i
    i = pk_i - 1
    while i >= 0 and ev[i][1] >= thr:
        lo_i = i
        i -= 1
    i = pk_i + 1
    while i < len(ev) and ev[i][1] >= thr:
        hi_i = i
        i += 1
    run_lo, run_hi = ev[lo_i][0], ev[hi_i][0]
    run_end = run_hi + period
    clipped = (hi_i == len(ev) - 1)      # the run may continue past the window

    marks = {}
    l_detail = _first_at_or_after(beats, pk_t)
    l_action = _first_at_or_after(beats, run_end)
    l_hold = _first_at_or_after(downbeats, run_end)
    fallback = None
    if l_hold is None:
        l_hold, fallback = l_action, "no downbeat inside the reachable grid; fell back to the beat grid"

    def clamp(land):
        raw = land - start
        if raw < min_b:
            return min_b, "clamped UP to minimum_b_shot %.1fs" % min_b, raw
        if raw > dmax:
            why = ("capped by maximum_b_shot %.1fs" % max_b) if max_b <= dmax \
                else "capped by the remaining %s %.3fs" % (span_label, dmax)
            return dmax, why, raw
        return raw, "as derived", raw

    for name, land in (("glance", l_detail), ("action", l_action), ("hold", l_hold)):
        if land is None:
            return None
        d, why, raw = clamp(land)
        marks[name] = {"endpoint_s": r3(land), "duration_s": r3(d),
                       "derived_duration_s": r3(raw), "clamp": why,
                       "landmark": {"glance": "first beat at/after the motion peak",
                                    "action": "first beat at/after the action run end",
                                    "hold": "first downbeat at/after the action run end"}[name]}

    collapsed = []
    for lower, upper in (("glance", "action"), ("action", "hold")):
        if marks[upper]["duration_s"] < marks[lower]["duration_s"]:
            collapsed.append("%s raised to %s's %.3fs (the slot gives no length "
                             "between them)" % (upper, lower, marks[lower]["duration_s"]))
            marks[upper]["duration_s"] = marks[lower]["duration_s"]
            marks[upper]["clamp"] = "raised to %s (monotonic); %s" \
                % (lower, marks[upper]["clamp"])

    return {"instrument": "B-window motion (visual_signals.py, %.1f Hz samples)" % (1.0 / period),
            "n_samples": len(ev), "sample_period_s": r3(period),
            "peak": {"t_s": r3(pk_t), "motion": r3(pk_m)},
            "action_run": {"start_s": r3(run_lo), "end_s": r3(run_end),
                           "threshold_motion": r3(thr),
                           "open_ended_at_window_edge": bool(clipped)},
            "levels": marks,
            "collapsed": collapsed,
            "fallback": fallback,
            "limits": {"minimum_b_shot": min_b, "maximum_b_shot": max_b,
                       "remaining_slot_width_s": r3(dmax)}}


# --------------------------------------- action-onset event proposal ----------
# 2026-09-13, candidate-timing increment. The 1020-1380 s opportunity audit found
# that B content is generally present but that a legal B entry can only ever BEGIN
# on the event-anchor grid (anchors ~9.7 s apart), so an alternate-camera action
# starting between anchors is met either by committing B seconds early or by
# entering seconds late: 6 of 11 strongest opportunities had no candidate that both
# entered near the action and ran to its end, and 9.4 % of the range had no legal B
# at all (holes opened where all four derived faces collapsed to minimum_b_shot).
#
# This step is deliberately narrow:
#
#   a sustained localised B-activity run (b_activity_signals.py) whose onset is not
#   already served by a legal B face may PROPOSE ONE ADDITIONAL EVENT at that onset
#   (snapped to the nearest existing grid beat).
#
# It proposes an EVENT LOCATION and nothing else. The locked rule is untouched: a B
# entry is still DERIVED from the event timestamp through the accepted sync mapping
# and is never searched, so the new event simply carries the mapping for its own
# timestamp. Existing events are never moved, and no candidate score is derived
# from the activity signal — it answers only "should an opportunity exist here?".

ONSET_MIN_RUN_S = 2.5        # a proposal needs a sustained run, not a gesture
ONSET_NEAR_S = 2.0           # "already represented": a face entering this close to the onset
ONSET_TAIL_TOL_S = 1.0       # ... and reaching the action end minus this
ONSET_MAX_SNAP_FRAC = 0.5    # snap to the nearest grid beat, within half a beat period
ONSET_MIN_LUMA_FRAC = 0.5    # fail-closed usability gate against the signal's own median luma
ONSET_MAX_FROZEN_FRAC = 0.5  # ditto for the frozen-sample share inside the run
# Supplemental spacing rule (2026-09-13, after the measured sweep). An editorial event is an
# OPPORTUNITY to decide, not a delivered cut: supplemental events therefore do not inherit the
# base grid's 9.0 s spacing, which exists to keep the musical grid from crowding. The gap sweep
# (2.5-5.0 s) showed 4.5 s captures the entire measured coverage gain (audit 5/6/0 -> 6/5/0,
# legal-B holes 34.00 -> 29.84 s, zero sustained runs left under 25 % covered) with the fewest
# added events, so it is the least aggressive value that materially improves coverage.
# TRIAL STATUS: candidate timing vNext — long-form trial, not permanent ISH-D policy.
ONSET_GAP_DEFAULT_S = 4.5


def load_activity(path):
    """b_activity_signals.py JSON. Returns the parsed doc, or None (fail closed)."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            doc = json.load(f)
    except Exception:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("runs"), list):
        return None
    return doc


def sync_faces(t, sig_b, reel_end, beats, structure, min_b, max_b, b_fps, beat_period):
    """The legal B faces a sync-anchored event at timeline `t` would offer.

    Returns a list of (candidate_id, timeline_start, timeline_end). This is a
    PREDICTION used only to decide whether an activity run is already represented;
    it mirrors the main loop exactly and the caller asserts the emitted artifact
    agrees with it for every base event, so any drift fails closed.
    """
    raw = t - SYNC_LAG_S
    snapped, snap = frame_snap(raw, b_fps)
    if abs(snap) > FRAME_SNAP_MAX_S + 1e-9:
        snapped, snap = raw, 0.0
    entry = round(snapped, 3)
    cov_ok, _ = b_entry_coverage(sig_b, entry, reel_end)
    if not cov_ok:
        return []
    dmax = min(max_b, max(0.0, reel_end - entry))
    dc = derive_durations(sig_b, entry, dmax, min_b, max_b,
                          beats, structure["downbeats"], span_label="reel")
    bi = min(range(len(beats)), key=lambda k: abs(beats[k] - t))
    prev_beat = beats[bi - 1] if bi > 0 else round(t - beat_period, 3)
    beat_shift = round(t - prev_beat, 3)
    if dc is None:
        # No usable motion evidence at the entry: the main loop falls back to the
        # canonical length on b_downbeat (+ b_early), so predict exactly that.
        faces = [("b_downbeat", round(t, 3), round(t + min_b, 3))]
        if round(entry - beat_shift, 3) >= 0.0:
            faces.append(("b_early", round(prev_beat, 3), round(prev_beat + min_b, 3)))
        return faces
    lv = dc["levels"]
    dg = lv["glance"]["duration_s"]
    da = lv["action"]["duration_s"]
    dh = lv["hold"]["duration_s"]
    faces = []
    if round(entry - beat_shift, 3) >= 0.0:
        faces.append(("b_early", round(prev_beat, 3), round(prev_beat + da, 3)))
    faces.append(("b_glance", round(t, 3), round(t + dg, 3)))
    faces.append(("b_action", round(t, 3), round(t + da, 3)))
    faces.append(("b_hold", round(t, 3), round(t + dh, 3)))
    return faces


def propose_onset_events(act, base_events, base_faces, neighbor_anchors, sig_b, reel_end,
                         beats, structure, min_b, max_b, b_fps, beat_period,
                         t_lo, t_hi, min_gap, min_run_s, near_s, tail_tol_s):
    """Decide which activity runs become supplemental events. Deterministic.

    Runs are considered strongest-first (smoothed peak, then onset) so that when two
    runs compete for the same slot the stronger one wins; every run gets exactly one
    decision and the full reason is recorded. Returns (accepted, record).
    """
    luma_med = float((act.get("summary") or {}).get("luma_median") or 0.0)
    runs = sorted(act["runs"],
                  key=lambda r: (-float(r.get("peak_sm") or 0.0),
                                 float(r.get("onset_perf_s") or 0.0)))
    accepted, accepted_anchors, record = [], list(base_events), []
    for r in runs:
        rec = {"run_id": r.get("run_id"),
               "onset_perf_s": r.get("onset_perf_s"), "end_perf_s": r.get("end_perf_s"),
               "duration_s": r.get("duration_s"), "peak_perf_s": r.get("peak_perf_s"),
               "peak_loc": r.get("peak_loc"), "peak_cell": r.get("peak_cell"),
               "decision": None, "reason": None, "anchor_s": None, "snap_s": None,
               "gap_to_nearest_anchor_s": None, "nearest_anchor_kind": None,
               "represented_by": None}
        dur = float(r.get("duration_s") or 0.0)
        onset = float(r.get("onset_perf_s") or 0.0)
        if dur < min_run_s - 1e-9:
            rec["decision"], rec["reason"] = "rejected", "run shorter than %.1f s" % min_run_s
            record.append(rec)
            continue
        # --- snap the proposed anchor to the existing grid, at or near the onset
        bi = min(range(len(beats)), key=lambda k: abs(beats[k] - onset))
        anchor = float(beats[bi])
        snap = round(anchor - onset, 3)
        rec["anchor_s"], rec["snap_s"] = r3(anchor), snap
        if abs(snap) > ONSET_MAX_SNAP_FRAC * beat_period + 1e-9:
            rec["decision"], rec["reason"] = (
                "rejected", "no grid beat within half a beat period of the onset "
                            "(nearest %.3f s away)" % abs(snap))
            record.append(rec)
            continue
        if anchor < t_lo - 1e-9 or anchor > t_hi + 1e-9:
            rec["decision"], rec["reason"] = (
                "rejected", "snapped anchor %.3f s lies outside the event window" % anchor)
            record.append(rec)
            continue

        # --- reel-end rule: the event must be able to evidence its OWN minimum envelope.
        # hold_a's span is pinned at min_b, so an anchor closer than min_b to the end of the
        # reel proposes an event whose only candidate the evidence builder can never render —
        # it fails closed on an envelope that leaves the reel. Reject the proposal here: this
        # defect belongs to the generator, never to the builder or the renderer.
        if anchor + min_b > reel_end + 1e-9:
            rec["decision"], rec["reason"] = (
                "rejected", "rejected_reel_end_min_envelope: anchor %.3f s + the minimum %.3f s "
                            "event envelope reaches %.3f s, past the end of the reel %.3f s — "
                            "no evidenceable candidate exists for this onset"
                            % (anchor, min_b, anchor + min_b, reel_end))
            record.append(rec)
            continue
        # --- usability: fail closed on an unusable signal rather than propose blind
        lmin = r.get("luma_min")
        ffr = r.get("frozen_frac")
        if luma_med > 0 and isinstance(lmin, (int, float)) and \
                float(lmin) < ONSET_MIN_LUMA_FRAC * luma_med:
            rec["decision"], rec["reason"] = (
                "rejected", "run is unusably dark (min luma %.3f vs signal median %.3f)"
                            % (float(lmin), luma_med))
            record.append(rec)
            continue
        if isinstance(ffr, (int, float)) and float(ffr) > ONSET_MAX_FROZEN_FRAC:
            rec["decision"], rec["reason"] = (
                "rejected", "run is %.0f %% frozen samples" % (float(ffr) * 100.0))
            record.append(rec)
            continue
        # --- synchronized coverage at the derived entry (same gate every event uses)
        entry = round(anchor - SYNC_LAG_S, 3)
        cov_ok, cov_why = b_entry_coverage(sig_b, entry, reel_end)
        if not cov_ok:
            rec["decision"], rec["reason"] = (
                "rejected", "no synchronized B coverage at the derived entry %.3f s: %s"
                            % (entry, cov_why))
            record.append(rec)
            continue
        # --- already represented by an existing legal face?
        reps = [(ev_t, cid, f0, f1) for ev_t, faces in base_faces.items()
                for (cid, f0, f1) in faces
                if (onset - near_s) <= f0 <= (onset + near_s) and f1 >= (float(r["end_perf_s"]) - tail_tol_s)]
        if reps:
            ev_t, cid, f0, f1 = reps[0]
            rec["decision"], rec["reason"] = (
                "rejected", "coverage already existed: a legal face (%s, entry %.3f s) enters "
                            "within %.1f s of the onset and reaches the action end"
                            % (cid, f0, near_s))
            rec["represented_by"] = {"anchor_s": r3(ev_t), "candidate": cid,
                                     "entry_s": r3(f0), "exit_s": r3(f1)}
            record.append(rec)
            continue
        # --- min_event_gap against base events, accepted proposals and neighbours
        nearest, kind = None, None
        for a in accepted_anchors:
            if nearest is None or abs(a - anchor) < abs(nearest - anchor):
                nearest, kind = a, "grid"
        for a, _ in accepted:
            if abs(a - anchor) < abs(nearest - anchor):
                nearest, kind = a, "b_action_onset"
        for a in neighbor_anchors:
            if abs(a - anchor) < abs(nearest - anchor):
                nearest, kind = a, "adjacent_section"
        rec["gap_to_nearest_anchor_s"] = r3(abs(anchor - nearest))
        rec["nearest_anchor_kind"] = kind
        if abs(anchor - nearest) < min_gap - 1e-9:
            rec["decision"], rec["reason"] = (
                "rejected", "min_event_gap: nearest anchor (%.3f s, %s) is only %.3f s away"
                            % (nearest, kind, abs(anchor - nearest)))
            record.append(rec)
            continue
        accepted.append((anchor, r))
        accepted_anchors.append(anchor)
        rec["decision"] = "accepted"
        rec["reason"] = ("sustained localised activity (%.2f s) whose onset %.3f s was not served "
                         "by a legal B face; proposed at grid beat %.3f s"
                         % (dur, onset, anchor))
        record.append(rec)
    return accepted, record


def main():
    ap = argparse.ArgumentParser(description="Deterministic candidate scorer (set of choices per event)")
    ap.add_argument("--beats", required=True, help="A-camera beat grid JSON (beat_detect.py)")
    ap.add_argument("--action-profile", required=True, action="append",
                    help="action_profile.py JSON for the B reel (repeat for multi-file reels)")
    ap.add_argument("--signals", required=True, action="append",
                    help="visual_signals.py JSON for angle B (repeatable)")
    ap.add_argument("--signals-a", action="append", default=None,
                    help="visual_signals.py JSON for angle A (enables true info_gain)")
    ap.add_argument("--phrases", default=None, help="phrase_map.py JSON")
    ap.add_argument("--virtual-reel", action="store_true",
                    help="offset repeated --action-profile in list order (one virtual B reel)")
    ap.add_argument("--min-b", type=float, default=4.0)
    ap.add_argument("--max-b", type=float, default=14.0)
    ap.add_argument("--min-recovery", type=float, default=8.0)
    ap.add_argument("--events", type=int, default=12)
    ap.add_argument("--min-event-gap", type=float, default=8.0)
    ap.add_argument("--start", type=float, default=None, help="event window start (timeline s)")
    ap.add_argument("--end", type=float, default=None, help="event window end (timeline s)")
    ap.add_argument("--b-search-from", type=float, default=None)
    ap.add_argument("--b-search-to", type=float, default=None)
    ap.add_argument("--a-offset", type=float, default=0.0, help="declared A source offset")
    ap.add_argument("--b-offset", type=float, default=0.0, help="declared B source offset")
    ap.add_argument("--sync-json", default=None, help="sync_multicam.py --json output")
    ap.add_argument("--keyframes-db", default=None, help="studio.db (read-only) for quality")
    ap.add_argument("--clip-id", type=int, default=None, help="B clip id in studio.db")
    ap.add_argument("--novelty-cap", type=float, default=NOVELTY_CAP)
    ap.add_argument("--duration-choices", action="store_true",
                    help="expose DURATION as its own decision: derive B_GLANCE / "
                         "B_ACTION / B_HOLD lengths from the footage + policy for every "
                         "event (default off: one canonical length per event, as before)")
    ap.add_argument("--sync-anchored-entry", action="store_true",
                    help="derive every B entry from the event's performance timestamp via the "
                         "accepted sync mapping instead of searching the event slot for the "
                         "strongest action. Requires --duration-choices. Default off: the "
                         "historical slot search")
    ap.add_argument("--b-fps", type=float, default=B_FPS_DEFAULT,
                    help="B reel frame rate, used only for the accepted +/-0.036 s frame snap")
    ap.add_argument("--b-action-onset-events", action="store_true",
                    help="propose at most ONE additional event per sustained alternate-camera "
                         "activity run whose onset is not already served by a legal B face. "
                         "Adds events; never moves one. Entry semantics are untouched. "
                         "Requires --b-activity-signals and --sync-anchored-entry. Default off.")
    ap.add_argument("--b-activity-signals", default=None,
                    help="b_activity_signals.py JSON (dense localised B activity; the proposal "
                         "source for --b-action-onset-events)")
    ap.add_argument("--onset-min-run-s", type=float, default=ONSET_MIN_RUN_S,
                    help="minimum sustained run length a proposal may be built from")
    ap.add_argument("--onset-near-s", type=float, default=ONSET_NEAR_S,
                    help="a face entering within this many seconds of the onset counts as "
                         "already representing the action")
    ap.add_argument("--onset-tail-tol-s", type=float, default=ONSET_TAIL_TOL_S,
                    help="... provided it also reaches the action end minus this")
    ap.add_argument("--neighbor-anchors", default=None,
                    help="comma-separated anchors from adjacent processing sections; a proposal "
                         "must also keep min_event_gap from these (cross-boundary context)")
    ap.add_argument("--onset-gap-s", type=float, default=None,
                    help="DIAGNOSTIC ONLY. Minimum gap a supplemental event must keep from other "
                         "anchors. Default (None) = the policy --min-event-gap, which is the "
                         "shipped behaviour: a supplemental event obeys the same gap rule as the "
                         "grid. Overriding it measures what the constraint costs and must not be "
                         "used for a production run without an explicit decision.")
    ap.add_argument("--sync-map", default=None,
                    help="the JOB's canonical camera sync map (v1.1). Without it the tool "
                         "uses the Set 01 legacy constant and refuses any other timebase.")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    apply_sync_map(args.sync_map)

    for p in [args.beats] + args.action_profile + args.signals + (args.signals_a or []):
        if not os.path.exists(p):
            print("ERROR: no such file: %s" % p, file=sys.stderr)
            return 2

    beats, bpm = load_beats(args.beats)
    if not beats:
        print("ERROR: no beats in %s" % args.beats, file=sys.stderr)
        return 2
    if args.min_b <= 0 or args.max_b < args.min_b or args.min_recovery < 0:
        print("ERROR: bad policy numbers", file=sys.stderr)
        return 2
    if args.sync_anchored_entry and not args.duration_choices:
        print("ERROR: --sync-anchored-entry needs --duration-choices: with the entry pinned to "
              "the event timestamp there is no slot search left to supply a shot length, and "
              "offering an unmeasured length would be inventing one.", file=sys.stderr)
        return 2
    if args.b_action_onset_events and not args.sync_anchored_entry:
        print("ERROR: --b-action-onset-events requires --sync-anchored-entry. The proposal only "
              "moves WHERE an event exists; it must not be bolted onto the historical slot "
              "search, which is the defect sync anchoring replaced.", file=sys.stderr)
        return 2
    if args.b_action_onset_events and not args.b_activity_signals:
        print("ERROR: --b-action-onset-events needs --b-activity-signals "
              "(b_activity_signals.py output). Without a proposal source there is nothing to "
              "propose from; refusing to guess.", file=sys.stderr)
        return 2

    segs, prof_meta = load_action_profiles(args.action_profile, args.virtual_reel)
    p_max = max([s[2] for s in segs], default=0.0)
    phrases_doc, _ = load_phrases(args.phrases)
    structure = derive_structure(beats, phrases_doc)
    structure["_beats"] = beats

    sig_b = load_signals(args.signals)
    sig_a = load_signals(args.signals_a)
    if not sig_a:
        sig_a = sig_b                      # documented fallback: B-vs-B proxy
        sig_a_is_proxy = True
    else:
        sig_a_is_proxy = False
    sync_doc = load_sync(args.sync_json)
    kf = keyframe_quality(args.keyframes_db, args.clip_id)

    t_lo = 0.0 if args.start is None else args.start
    t_hi = (max(beats) if args.end is None else args.end)
    s_lo = (args.b_search_from if args.b_search_from is not None else t_lo) + args.b_offset
    s_hi = (args.b_search_to if args.b_search_to is not None else t_hi) + args.b_offset

    events, event_kind = pick_events(beats, structure, t_lo, t_hi, args.events,
                                     args.min_event_gap)
    beat_period = (60.0 / bpm) if bpm else 0.5

    # ---- action-onset event proposal (opt-in; default OFF) -------------------
    # reel_end is needed by the proposal step (coverage / remaining reel), so it is
    # computed here; the value and expression are the ones the path below always used.
    reel_end = max([(m["offset"] + (m["duration_s"] or 0.0)) for m in prof_meta] or [0.0])
    if SYNC_MAP:
        # The job's canonical map owns the reel geometry. The approved action profile is
        # evidence about a reel, not the definition of that reel's length, so a disagreement
        # is reported and the map wins.
        map_reel = float(SYNC_MAP.get("reel_end_s") or 0.0)
        if map_reel and abs(map_reel - reel_end) > FRAME_SNAP_MAX_S:
            print("   note: action-profile reel %.3f s vs canonical sync map %.3f s — using "
                  "the map (job data is authoritative)" % (reel_end, map_reel), file=sys.stderr)
        if map_reel:
            reel_end = map_reel
    origins = ["grid"] * len(events)
    kinds = [event_kind] * len(events)
    onset_map = {}
    onset_record, onset_doc, onset_neighbors = [], None, []
    if args.b_action_onset_events:
        onset_doc = load_activity(args.b_activity_signals)
        if onset_doc is None:
            print("ERROR: --b-activity-signals did not load (%s) — refusing to emit a "
                  "supplemental event without a valid proposal source."
                  % args.b_activity_signals, file=sys.stderr)
            return 2
        act_tol = (float(SYNC_MAP.get("tolerance_s")) if SYNC_MAP else 0.0) + 1e-9
        if abs(float(onset_doc.get("lag_s", -1.0)) - SYNC_LAG_S) > act_tol:
            print("ERROR: activity signal lag_s=%r does not match this job's canonical sync "
                  "map lag %.4f s (tolerance %.4f) — the proposal would be built on a "
                  "different timebase."
                  % (onset_doc.get("lag_s"), SYNC_LAG_S, act_tol), file=sys.stderr)
            return 2
        base_events = list(events)
        base_faces = {t: sync_faces(t, sig_b[0] if sig_b else None, reel_end, beats,
                                    structure, args.min_b, args.max_b, args.b_fps, beat_period)
                      for t in base_events}
        if args.neighbor_anchors:
            onset_neighbors = [float(x) for x in args.neighbor_anchors.split(",") if x.strip()]
        accepted, onset_record = propose_onset_events(
            onset_doc, base_events, base_faces, onset_neighbors,
            sig_b[0] if sig_b else None, reel_end, beats, structure,
            args.min_b, args.max_b, args.b_fps, beat_period, t_lo, t_hi,
            (args.onset_gap_s if args.onset_gap_s is not None else ONSET_GAP_DEFAULT_S),
            args.onset_min_run_s, args.onset_near_s,
            args.onset_tail_tol_s)
        onset_gap_used = (args.onset_gap_s if args.onset_gap_s is not None
                          else ONSET_GAP_DEFAULT_S)
        onset_gap_overridden = args.onset_gap_s is not None
        merged = sorted([(t, "grid") for t in base_events] +
                        [(t, "b_action_onset") for t, _ in accepted], key=lambda x: x[0])
        events = [t for t, _ in merged]
        origins = [o for _, o in merged]
        kinds = [event_kind if o == "grid" else "action_onset" for o in origins]
        onset_map = {t: r for t, r in accepted}

    # canonical reference path.
    # Flag OFF (historical): the B search range is split into ONE SLOT PER EVENT (in
    #   event order) and each event takes its highest-action window inside its own slot.
    #   That is the defect: the slot origin walks ahead of the event position and the
    #   action search moves the window further.
    # Flag ON (--sync-anchored-entry): the entry is DERIVED from the event's own
    #   performance timestamp through the accepted mapping and is never searched.
    used_b = []
    canonical = []
    slots = []
    win_his = []
    sync_entries = []
    n_ev = max(1, len(events))
    slot_len = (s_hi - s_lo) / n_ev
    for i, t in enumerate(events):
        if args.sync_anchored_entry:
            raw = t - SYNC_LAG_S
            snapped, snap = frame_snap(raw, args.b_fps)
            if abs(snap) > FRAME_SNAP_MAX_S + 1e-9:
                snapped, snap = raw, 0.0   # outside the accepted budget: do not snap
            # Every emitted source time in this tool is 3 dp, so the entry is too. That is
            # what lets every B face report the identical entry instead of a value rounded
            # per call; it costs at most 0.5 ms off the frame grid (1.5% of a frame).
            entry = round(snapped, 3)
            cov_ok, cov_why = b_entry_coverage(sig_b[0] if sig_b else None, entry, reel_end)
            dmax = min(args.max_b, max(0.0, reel_end - entry))
            sync_entries.append({"timeline_s": r3(t), "mapped_source_s": r3(raw),
                                 "frame_snapped_source_s": r3(snapped),
                                 "entry_source_s": entry,
                                 "frame_snap_s": r3(snap),
                                 "entry_offset_from_map_s": r3(entry - raw),
                                 "coverage_ok": bool(cov_ok), "coverage": cov_why,
                                 "reel_end_s": r3(reel_end), "max_duration_s": r3(dmax),
                                 "invariant_residual_s": round((t - entry) - SYNC_LAG_S, 6)})
            used_b.append((entry, entry + dmax))
            slots.append(None)
            win_his.append(reel_end)
            canonical.append({"timeline_start": t, "timeline_end": t + args.min_b,
                              "source_start": entry, "source_end": entry + args.min_b,
                              "action_score": action_mean(segs, entry, entry + args.min_b),
                              "source_order_enforced": True})
            continue
        a = s_lo + i * slot_len
        b = min(s_hi, a + slot_len)
        win_hi = min(s_hi, max(b, a + args.min_b))
        mb = min(args.max_b, max(args.min_b, win_hi - a))
        win_his.append(win_hi)
        cursor = max([u1 for _, u1 in used_b], default=a)
        s0, s1, sc, order_ok = pick_b_window(segs, a, win_hi, args.min_b, mb,
                                             used_b, max(cursor, a))
        used_b.append((s0, s1))
        slots.append((round(a, 3), round(b, 3)))
        canonical.append({"timeline_start": t, "timeline_end": t + (s1 - s0),
                          "source_start": s0, "source_end": s1,
                          "action_score": sc, "source_order_enforced": bool(order_ok)})

    out_events = []
    for i, t in enumerate(events):
        canon = canonical[i]
        prev = canonical[i - 1] if i > 0 else None
        dur = round(canon["timeline_end"] - canon["timeline_start"], 3)

        # ---- sync ----
        if sync_doc:
            confs = sync_doc.get("confidences", {})
            conf = None
            for k, v in confs.items():
                conf = float(v)
                break
            sync_sc = c01((conf or 0.0) / SYNC_CONF_SAT)
            sync_note = ("sync_multicam.py confidence %.3f on %s (scaled /%.1f)"
                         % (conf or 0.0, os.path.basename(str(sync_doc.get("ref"))),
                            SYNC_CONF_SAT))
        else:
            sync_sc = 1.0
            sync_note = ("declared synced: mix declares the sources aligned "
                         "(a_offset=%.3f, b_offset=%.3f); no sync_multicam artifact supplied"
                         % (args.a_offset, args.b_offset))

        # ---- candidates ----
        cands = []
        # B source window is picked from the action profile; the TIMELINE entry
        # is the boundary. b_early rolls one ACTUAL grid beat earlier (snapped to
        # the nearest beat before the boundary) so the same action peak — which
        # stays source-anchored — lands ON the boundary.
        bi = min(range(len(beats)), key=lambda k: abs(beats[k] - t))
        prev_beat = beats[bi - 1] if bi > 0 else round(t - beat_period, 3)
        beat_shift = round(t - prev_beat, 3)
        early_tl0 = round(prev_beat, 3)
        early_src0 = round(canon["source_start"] - beat_shift, 3)
        early_src1 = round(canon["source_end"] - beat_shift, 3)
        if early_src0 < 0.0:
            if args.sync_anchored_entry:
                early_src0, early_src1 = None, None   # legal b_early is preserved, not clamped
            else:
                early_src0, early_src1 = 0.0, round(canon["source_end"] - canon["source_start"], 3)
        # ---- duration choice (opt-in, 2026-09-11) -------------------------
        # Exposes HOW LONG the look is, as its own editorial decision, without
        # touching the entry point or the canonical source-order bookkeeping.
        dchoice = None
        hold_only = None
        sync_rec = sync_entries[i] if args.sync_anchored_entry else None
        if args.sync_anchored_entry:
            # The entry is already fixed above. All that is decided here is LENGTH.
            if not sync_rec["coverage_ok"]:
                hold_only = ("NO SYNCHRONIZED B COVERAGE at %.3f s: %s. HOLD_A is the only "
                             "candidate offered on this event — the generator does not search "
                             "nearby B footage for an alternative moment."
                             % (sync_rec["entry_source_s"], sync_rec["coverage"]))
            else:
                dchoice = derive_durations(sig_b[0] if sig_b else None,
                                           canon["source_start"], sync_rec["max_duration_s"],
                                           args.min_b, args.max_b,
                                           structure["_beats"], structure["downbeats"],
                                           span_label="reel")
                if dchoice is None:
                    hold_only = ("no usable motion evidence at the synchronized entry (%.3f s), "
                                 "so no legal length can be derived. HOLD_A is the only "
                                 "candidate offered." % sync_rec["entry_source_s"])
                else:
                    dchoice["entry"] = {
                        "timeline_s": sync_rec["timeline_s"],
                        "mapped_source_s": sync_rec["mapped_source_s"],
                        "entry_source_s": sync_rec["entry_source_s"],
                        "frame_snap_s": sync_rec["frame_snap_s"],
                        "coverage": sync_rec["coverage"],
                        "rule": ("entry = timeline_s - %.4f s (accepted mapping), frame-snapped "
                                 "within +/-%.3f s. Derived from the event timestamp; never "
                                 "searched." % (SYNC_LAG_S, FRAME_SNAP_MAX_S)),
                        "limits": {"maximum_b_shot": args.max_b,
                                   "reel_end_s": sync_rec["reel_end_s"],
                                   "remaining_reel_s": sync_rec["max_duration_s"]}}
        elif args.duration_choices:
            dmax = min(args.max_b, win_his[i] - canon["source_start"])
            dchoice = derive_durations(sig_b[0] if sig_b else None,
                                       canon["source_start"], dmax,
                                       args.min_b, args.max_b,
                                       structure["_beats"], structure["downbeats"])

        if hold_only is not None:
            specs = [["hold_a", "A", "HOLD", round(t, 3),
                      round(t + args.a_offset, 3), round(t + args.a_offset + dur, 3), dur,
                      "HOLD_A — the only candidate on this event. " + hold_only]]
        elif dchoice is None:
            specs = [
                ("b_downbeat", "B", "SWITCH", round(t, 3), canon["source_start"],
                 canon["source_end"], dur, "enter exactly on the boundary"),
                ("b_early", "B", "SWITCH", early_tl0, early_src0, early_src1, dur,
                 "enter one beat (%.3fs) before the boundary; action peak stays on the boundary"
                 % beat_shift),
                ("hold_a", "A", "HOLD", round(t, 3),
                 round(t + args.a_offset, 3), round(t + args.a_offset + dur, 3), dur,
                 "stay on angle A across the boundary"),
            ]
            specs = [list(s) for s in specs]
            if args.duration_choices:
                specs[0][7] = ("DURATION CHOICE UNAVAILABLE: this window carries no usable "
                               "motion evidence, so no length could be derived — the canonical "
                               "length is offered instead. " + specs[0][7])
                specs[1][7] = ("DURATION CHOICE UNAVAILABLE: same window. " + specs[1][7])
        else:
            lv = dchoice["levels"]
            dg = lv["glance"]["duration_s"]
            da = lv["action"]["duration_s"]
            dh = lv["hold"]["duration_s"]
            S = round(canon["source_start"], 3)
            e0 = round(S - beat_shift, 3)
            e1 = round(S + da - beat_shift, 3)
            early_ok = e0 >= 0.0
            if e0 < 0.0 and not args.sync_anchored_entry:
                e0, e1 = 0.0, round(da, 3)
                early_ok = True

            def dnote(blurb, m):
                return ("%s. Endpoint %.3fs = %s. Length %.3fs (%s); derived %.3fs."
                        % (blurb, m["endpoint_s"], m["landmark"], m["duration_s"],
                           m["clamp"], m["derived_duration_s"]))

            specs = [
                ("b_early", "B", "SWITCH", early_tl0, e0, e1, da,
                 "B_EARLY — enter one beat (%.3fs) before the boundary, carrying the "
                 "B_ACTION length, so the action peak lands ON the boundary: %s"
                 % (beat_shift, dnote("Duration taken from this event's B_ACTION level "
                                      "(the visible action), not a fixed bucket",
                                      lv["action"]))),
                ("b_glance", "B", "SWITCH", round(t, 3), S, round(S + dg, 3), dg,
                 dnote("B_GLANCE — a brief useful detail: leave as soon as the strongest "
                       "visible movement in the slot has been delivered, on the next beat",
                       lv["glance"])),
                ("b_action", "B", "SWITCH", round(t, 3), S, round(S + da, 3), da,
                 dnote("B_ACTION — hold through the visible action: stay until the "
                       "contiguous movement around the peak has run out, then leave on the "
                       "next beat", lv["action"])),
                ("b_hold", "B", "SWITCH", round(t, 3), S, round(S + dh, 3), dh,
                 dnote("B_HOLD — continue past the action to the next useful musical "
                       "boundary (the next downbeat)", lv["hold"])),
                ("hold_a", "A", "HOLD", round(t, 3),
                 round(t + args.a_offset, 3), round(t + args.a_offset + dur, 3), dur,
                 "stay on angle A across the boundary"),
            ]
            specs = [list(s) for s in specs]
            if not early_ok:
                specs = [s for s in specs if s[0] != "b_early"]
                if sync_rec is not None:
                    sync_rec["b_early"] = ("dropped: one beat (%.3f s) before the entry "
                                           "(%.3f s) would start before the B reel, and clamping "
                                           "it would break the sync invariant"
                                           % (beat_shift, S))
            for s in specs:
                if s[1] == "B":
                    s[7] += (" Evidence: %d motion samples at %.0f Hz over this window "
                             "(peak %.4f)." % (dchoice["n_samples"],
                                               1.0 / dchoice["sample_period_s"],
                                               dchoice["peak"]["motion"]))
            if sync_rec is not None:
                for s in specs:
                    if s[1] != "B":
                        continue
                    if s[0] == "b_early":
                        s[7] += (" Synchronized entry: timeline %.3f s -> B %.3f s "
                                 "(r = A_t - %.4f s). Window shifted one beat in BOTH timebases, "
                                 "so at timeline %.3f s it still shows performance time "
                                 "%.3f s. Entry derived, never searched."
                                 % (t, S, SYNC_LAG_S, t, S))
                    else:
                        s[7] += (" Synchronized entry: timeline %.3f s -> B %.3f s "
                                 "(r = A_t - %.4f s). Entry derived from the event timestamp "
                                 "and never searched; length extends forward only."
                                 % (t, S, SYNC_LAG_S))

        for cid, angle, action, tl0, src0, src1, cdur, spec_note in specs:
            tl1 = round(tl0 + cdur, 3)
            notes = [spec_note]
            scores = {}

            scores["sync"] = r3(sync_sc)

            mus, dbg = musical_score(tl0, structure)
            scores["musical"] = r3(mus)

            if action == "HOLD":
                notes.append("angle A was already on screen; HOLD is the 'do nothing' option")
                notes.append("action/novelty/info_gain are legitimately LOW for hold_a")

            # action: only meaningful for a B window
            if action == "SWITCH":
                am = action_mean(segs, src0, src1)
                act = c01((am or 0.0) / p_max) if p_max > 0 else 0.0
                notes.append("action profile %d segments, reel max %.3f"
                             % (len(segs), p_max))
            else:
                act = 0.0
            scores["action"] = r3(act)

            # quality + coverage + info_gain need windows
            if action == "SWITCH":
                win = window_visual(sig_b[0], src0, src1) if sig_b else None
                sig_sum = sig_b[0]["summary"] if sig_b else {}
                q, qsrc = quality_score(win, sig_sum, kf)
                cov = win["coverage"] if win else UNCOVERED
                if win and win["any_flagged"]:
                    cov = 0.0
                    notes.append("PROBLEM FRAME: obstruction/frozen inside the B window")
                if win is None:
                    notes.append("no visual samples in the B window (coverage=%.2f unused)" % UNCOVERED)
                sim, n_pairs = hist_similarity(sig_a[0], tl0, tl1, sig_b[0], src0, src1)
                ig = c01(1.0 - sim) if sim is not None else 0.0
                if sim is not None:
                    notes.append("B-vs-%s histogram similarity %.3f over %d paired samples"
                                 % ("A" if not sig_a_is_proxy else "B(surrounding proxy)",
                                    sim, n_pairs))
                elif sig_a_is_proxy:
                    notes.append("info_gain fallback: treated as 0 (no paired samples)")
            else:
                win_a = window_visual(sig_a[0], tl0, tl1) if sig_a else None
                sig_sum = sig_a[0]["summary"] if sig_a else {}
                q, qsrc = quality_score(win_a, sig_sum, kf)
                cov = win_a["coverage"] if win_a else UNCOVERED
                if win_a and win_a["any_flagged"]:
                    cov = 0.0
                if win_a is None:
                    notes.append("no visual samples in the A window (coverage=%.2f unused)" % UNCOVERED)
                ig = 0.0     # same angle both sides: nothing new is shown
                notes.append("info_gain=0 by construction (A stays A)")
            if qsrc != "no evidence":
                notes.append("quality from %s" % qsrc)
            scores["quality"] = r3(q)
            scores["coverage"] = r3(cov)
            scores["info_gain"] = r3(ig)

            # continuity + novelty against the canonical path
            if action == "SWITCH":
                if not canon["source_order_enforced"]:
                    notes.append("no in-order B window left in the search range; "
                                 "canonical window reorders/reuses B material")
                if prev is None:
                    gap = tl0
                    cont = 1.0
                    notes.append("first event in the run")
                else:
                    gap = tl0 - prev["timeline_end"]
                    order_ok = src0 >= prev["source_end"] - 1e-9
                    rec = c01(gap / args.min_recovery) if args.min_recovery > 0 else 1.0
                    if gap >= args.min_recovery - 1e-9 and order_ok:
                        cont = 1.0
                    else:
                        cont = min(rec, 0.25) if not order_ok else rec
                        if gap < args.min_recovery - 1e-9:
                            notes.append("short A recovery: %.2fs < minimum %.2fs"
                                         % (max(gap, 0.0), args.min_recovery))
                        if not order_ok:
                            notes.append("B source order not monotonic vs previous B window")
                nov = c01(gap / args.novelty_cap) if args.novelty_cap > 0 else 0.0
                notes.append("gap since previous B exit: %.2fs (novelty cap %.1fs)"
                             % (max(gap, 0.0), args.novelty_cap))
            else:
                cont = 1.0
                nov = 0.0
                notes.append("angle A is the safe master: continuity holds by definition")
                notes.append("seconds since A was last on screen: 0 (it is on screen)")
            scores["continuity"] = r3(cont)
            scores["novelty"] = r3(nov)

            for k in SCORE_KEYS:
                if k not in scores:
                    scores[k] = 0.0

            cands.append({
                "id": cid, "angle": angle, "action": action,
                "start": r3(src0), "end": r3(src1),
                "timeline_start": r3(tl0), "timeline_end": r3(tl1),
                "duration_s": r3(cdur),
                "scores": {k: scores[k] for k in SCORE_KEYS},
                "notes": notes,
            })

        for c in cands:
            c["_w"] = sum(WEIGHTS[k] * (c["scores"][k] or 0.0) for k in SCORE_KEYS)
            c["weighted_sum"] = r3(c["_w"])
        best = max(cands, key=lambda c: (c["_w"], c["id"]))
        recommended = best["id"]

        pi, bars_in = None, None
        pstarts = [p["start"] for p in structure["phrases"]]
        k = bisect_right(pstarts, t + 1e-9) - 1
        if 0 <= k < len(structure["phrases"]):
            p = structure["phrases"][k]
            pi = k
            sb = p.get("start_bar")
            if sb is None:
                sb = k * structure["bars_per_phrase"]
            bar_idx = bisect_right(structure["downbeats"], t + 1e-9) - 1
            bars_in = max(0, bar_idx - int(sb))

        nearest_beat = min(beats, key=lambda b: abs(b - t))
        ctxt = {
            "beat": r3(nearest_beat),
            "downbeat": bool(any(abs(t - d) < 1e-6 for d in structure["downbeats"])),
            "phrase_boundary": bool(any(abs(t - ps) < 1e-6 for ps in structure["phrase_starts"])),
            "phrase_index": pi,
            "bars_into_phrase": bars_in,
            "b_slot": (list(slots[i]) if slots[i] else None),
            "boundary_kind": kinds[i],
            "structure_source": structure["source"],
        }
        ev = {
            "event_id": "evt_%02d" % (i + 1),
            "context": ctxt,
            "candidates": [{k: v for k, v in c.items() if k != "_w"} for c in cands],
            "recommended_by_score": recommended,
            "recommendation_note": ("argmax of a fixed weighted sum over the eight "
                                    "evidence scores; informational only"),
        }
        # New records are emitted ONLY under their own flag, so a flag-off run keeps
        # byte-identity with the frozen pre-change control artifact.
        if args.duration_choices:
            ev["duration_choices"] = dchoice
        if args.sync_anchored_entry:
            ctxt["sync_entry"] = sync_rec
        if args.b_action_onset_events:
            # Provenance, for auditability only. It is a factual label of where the
            # event came from — it carries no preference, no score and no rank, and
            # nothing downstream may read it as a reason to cut.
            ctxt["event_origin"] = origins[i]
            if origins[i] == "b_action_onset":
                r = onset_map[t]
                ctxt["action_onset"] = {
                    "signal": os.path.basename(args.b_activity_signals),
                    "run_id": r.get("run_id"),
                    "onset_perf_s": r.get("onset_perf_s"),
                    "end_perf_s": r.get("end_perf_s"),
                    "duration_s": r.get("duration_s"),
                    "peak_perf_s": r.get("peak_perf_s"),
                    "peak_loc": r.get("peak_loc"),
                    "peak_cell": r.get("peak_cell"),
                    "snap_to_grid_beat_s": r.get("snap_s"),
                    "why": ("a sustained localised alternate-camera activity run began here and "
                            "no existing legal B face served its onset; this event exists so the "
                            "accepted sync mapping can offer B at the action. The candidates on "
                            "this event carry the same evidence scores as any grid event."),
                }
        out_events.append(ev)

    doc = {
        "tool": TOOL,
        "principle": PRINCIPLE,
        "recommended_by_score_is": "informational only — not the creative answer",
        "inputs": {
            "beats": args.beats,
            "bpm": bpm,
            "n_beats": len(beats),
            "action_profiles": prof_meta,
            "signals_b": [s["path"] for s in sig_b],
            "signals_a": [s["path"] for s in sig_a],
            "signals_a_is_b_proxy": sig_a_is_proxy,
            "phrases": args.phrases,
            "structure": {"source": structure["source"],
                          "downbeats": len(structure["downbeats"]),
                          "phrases": len(structure["phrases"]),
                          "beats_per_bar": structure["beats_per_bar"],
                          "bars_per_phrase": structure["bars_per_phrase"]},
            "sync_json": args.sync_json,
            "keyframes": kf,
            "offsets": {"a_offset": args.a_offset, "b_offset": args.b_offset},
        },
        "policy": {"minimum_b_shot": args.min_b, "maximum_b_shot": args.max_b,
                   "minimum_a_recovery": args.min_recovery,
                   "events_requested": args.events, "min_event_gap": args.min_event_gap,
                   "event_window": [t_lo, t_hi],
                   "b_search_window": [s_lo, s_hi],
                   "novelty_cap_s": args.novelty_cap},
        "weights": WEIGHTS,
        "score_keys": SCORE_KEYS,
        "events": out_events,
    }

    # Flag-gated records, appended only when the flag is on: a flag-off run must stay
    # byte-identical to the frozen pre-change artifact, key order included.
    if args.duration_choices:
        doc["policy"]["duration_choices"] = True
        doc["duration_choice_rules"] = DURATION_RULES
    if args.sync_anchored_entry:
        doc["policy"]["sync_anchored_entry"] = True
        doc["sync_anchored_entry"] = {
            "lag_s": SYNC_LAG_S,
            "frame_snap_max_s": FRAME_SNAP_MAX_S,
            "b_fps": args.b_fps,
            "reel_end_s": r3(reel_end),
            "coverage_near_s": COVERAGE_NEAR_S,
            "invariant": {"A": "timeline_position - source_in = 0",
                          "B": "timeline_position - source_in = %.4f" % SYNC_LAG_S,
                          "tolerance_s": FRAME_SNAP_MAX_S},
            "why": ("B entry is derived from the event's performance timestamp through the "
                    "accepted mapping and is never searched. Action evidence may decide how "
                    "long a shot runs; it may not decide when it starts."),
            "missing_coverage": ("the event offers hold_a only, with the reason recorded; no "
                                 "search of nearby B footage"),
            "b_early": ("preserved: timeline and source shift together, so at the boundary it "
                        "still shows the synchronized moment. Dropped (not clamped) if the "
                        "anticipation would start before the B reel."),
            "events": sync_entries}

    if args.b_action_onset_events:
        doc["policy"]["b_action_onset_events"] = True
        acc = [r for r in onset_record if r["decision"] == "accepted"]
        rej = [r for r in onset_record if r["decision"] == "rejected"]
        doc["b_action_onset_events"] = {
            "why": ("a sustained localised alternate-camera activity run whose onset is not "
                    "already served by a legal B face proposes ONE ADDITIONAL EVENT at that "
                    "onset (snapped to the nearest existing grid beat). It changes WHERE an "
                    "editorial event exists, never how a camera candidate maps to performance "
                    "time: the entry is still derived from the event timestamp through the "
                    "accepted sync mapping and is never searched. The activity signal is a "
                    "proposal signal only — it is not a candidate score and no score here is "
                    "derived from it."),
            "signal": args.b_activity_signals,
            "lag_s": SYNC_LAG_S,
            "rule": {"min_run_s": args.onset_min_run_s,
                     "snap": "nearest grid beat within half a beat period (%.4f s)"
                             % (ONSET_MAX_SNAP_FRAC * beat_period),
                     "already_represented": ("a legal face entering within %.1f s of the onset "
                                             "and reaching the action end minus %.1f s"
                                             % (args.onset_near_s, args.onset_tail_tol_s)),
                     "min_event_gap_s": onset_gap_used,
                     "min_event_gap_s_grid": args.min_event_gap,
                     "min_event_gap_overridden": bool(onset_gap_overridden),
                     "usability": ("fail closed if the run's minimum luma is below %.1f x the "
                                   "signal's median luma or more than %.0f %% of its samples are "
                                   "frozen" % (ONSET_MIN_LUMA_FRAC, ONSET_MAX_FROZEN_FRAC * 100)),
                     "coverage": ("b_entry_coverage at the derived entry must pass; otherwise "
                                  "no event is proposed"),
                     "per_run": "at most one supplemental event per activity run"},
            "counts": {"base_events": len(base_events), "runs_considered": len(onset_record),
                       "accepted": len(acc), "rejected": len(rej),
                       "final_events": len(out_events)},
            "neighbor_anchors": onset_neighbors,
            "accepted_events": [{"anchor_s": r["anchor_s"], "run_id": r["run_id"],
                                 "onset_perf_s": r["onset_perf_s"],
                                 "action_end_perf_s": r["end_perf_s"],
                                 "duration_s": r["duration_s"],
                                 "snap_s": r["snap_s"],
                                 "gap_to_nearest_anchor_s": r["gap_to_nearest_anchor_s"],
                                 "nearest_anchor_kind": r["nearest_anchor_kind"]}
                                for r in sorted(acc, key=lambda x: x["anchor_s"])],
            "runs": onset_record,
        }
        # Fail-closed self-check: the face prediction behind the "already represented"
        # test must agree with the faces actually emitted for every base event, so a
        # drifted model of the grid cannot ship a wrong proposal.
        bad = []
        for i, t in enumerate(events):
            if origins[i] != "grid":
                continue
            pred = sorted(base_faces[t])
            got = sorted((c["id"], c["timeline_start"], c["timeline_end"])
                         for c in out_events[i]["candidates"] if c["angle"] == "B")
            if pred != got:
                bad.append({"anchor_s": r3(t), "predicted": pred, "emitted": got})
        if bad:
            print("ERROR: face prediction disagrees with the emitted artifact for %d base "
                  "event(s) — refusing to ship a proposal built on a stale model of the grid: %s"
                  % (len(bad), json.dumps(bad[:2])), file=sys.stderr)
            return 2

    if args.out:
        with open(args.out, "w") as f:
            json.dump(doc, f, indent=1)
        print("CANDIDATES %s -> %s (%d events, hold_a present in all: %s)"
              % (args.beats, args.out, len(out_events),
                 all(any(c["id"] == "hold_a" for c in e["candidates"]) for e in out_events)))
        if args.duration_choices:
            for e in out_events:
                d = {c["id"]: c["duration_s"] for c in e["candidates"] if c["angle"] == "B"}
                print("   %s B lengths: %s" % (e["event_id"],
                      "  ".join("%s=%.3fs" % (k, d[k]) for k in
                                ("b_early", "b_glance", "b_action", "b_hold") if k in d)))
        if args.b_action_onset_events:
            c0 = doc["b_action_onset_events"]["counts"]
            print("   ACTION-ONSET EVENTS: %d base + %d supplemental = %d events "
                  "(%d runs considered, %d rejected)"
                  % (c0["base_events"], c0["accepted"], c0["final_events"],
                     c0["runs_considered"], c0["rejected"]))
            for r in doc["b_action_onset_events"]["accepted_events"]:
                print("      + event at %.3f s (onset %.3f, action %.3f-%.3f, snap %+.3f, "
                      "nearest anchor %.3f s / %s)"
                      % (r["anchor_s"], r["onset_perf_s"], r["onset_perf_s"],
                         r["action_end_perf_s"], r["snap_s"],
                         r["gap_to_nearest_anchor_s"], r["nearest_anchor_kind"]))
        if args.sync_anchored_entry:
            print("   SYNC-ANCHORED ENTRY: entry = T - %.4f s (accepted mapping), frame snap "
                  "<= %.3f s, B reel end %.3f s" % (SYNC_LAG_S, FRAME_SNAP_MAX_S, reel_end))
            for se, e in zip(sync_entries, out_events):
                ids = [c["id"] for c in e["candidates"] if c["angle"] == "B"]
                print("   %s T=%.3f -> B %.3f (snap %+.4f, residual %+.6f)  %s"
                      % (e["event_id"], se["timeline_s"], se["entry_source_s"],
                         se["frame_snap_s"], se["invariant_residual_s"],
                         ("B faces " + "/".join(ids)) if ids
                         else ("HOLD_A only — " + se["coverage"])))
    else:
        print(json.dumps(doc, indent=1))
    if args.json:
        print(json.dumps({"events": [{"event_id": e["event_id"],
                                      "recommended_by_score": e["recommended_by_score"],
                                      "candidates": [{"id": c["id"], "action": c["action"],
                                                      "weighted_sum": c["weighted_sum"],
                                                      "scores": c["scores"]}
                                                     for c in e["candidates"]]}
                                    for e in out_events]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
