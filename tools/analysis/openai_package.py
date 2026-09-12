#!/usr/bin/env python3
"""openai_package.py — LOCAL builder for the editorial evidence package.

Builds the *package a multimodal model would eventually receive*, and sends
nothing. This tool has no network code at all: no http/socket/urllib import, no
curl, no provider SDK. It is the local half of Test 1 in
`2026-09-11-openai-hybrid-addendum-review.md` §10 — "build the sheet + dense
package" — and it stops at the edge of the lab. `package_index.json` records
`transmitted: false` for every file, which is what makes the package an audit
record of exactly what *would* leave the lab (and, later, what did).

Design constraint (from the review, §2): the API takes images, NOT video. The
model therefore never sees continuous footage; it sees SAMPLED FRAMES carried as
contact sheets, plus dense bursts inside the local candidate windows.

Package layout (exact, under --out/<package-name>):

  openai_test/
    package_index.json          every file + sha256 + bytes + purpose + transmitted:false
    evidence_pack.json          the text half: span, sources, sheet index, candidate
                                sets, beat/phrase context, policy, counts, evidence_notes
                                (< ~40 KB so a prompt stays cheap)
    prompt.md                   the prompt template the pack is pasted into
    reference/                  EMPTY placeholder + README — the human supplies the
                                reference edit later
    overview/                   timecoded contact sheets: 1 frame / 10 s, 30 per sheet,
                                5x6 tiles, burned HH:MM:SS.mmm timecode on every tile
    events/<event_id>/          dense frames per editorial event (--dense-fps inside
                                --dense-window seconds around the event boundary),
                                timecode in the filename
    candidates/<event_id>.json  the legal candidate set for that event
    candidates/deterministic_ranking_LOCAL.json
                                LOCAL-ONLY: the full aggregate ranking. Must never be
                                sent to the model.

AGGREGATE POLICY (owner direction): the model-visible evidence carries the eight
individual evidence scores but NO aggregate. `weighted_sum`/`_w`, any
`recommended_by_score` and any per-event ordering that implies a winner are
stripped from the pack and the candidate files, and preserved only in
candidates/deterministic_ranking_LOCAL.json. Sections that rely on the aggregate
use it where? nowhere in the model-facing files.

Everything is bounded by construction: only `-ss <t> -frames:v 1` fast seeks,
never a decode of the whole file. A 13 GB / 34-minute 4K HEVC source on NFS is
touched only at the sample times asked for.

Usage:
  openai_package.py --manifest JOB.yaml --media-root DIR --span START-END
      --out DIR [--package-name openai_test] [--sheet-frames 30]
      [--dense-window 10 --dense-fps 2] [--candidates C.json]
      [--phrases P.json] [--beats B.json] [--policy P.yaml]
      [--b-profile P.json] [--segment-reason "..."] [--dense-angle auto|a|b|both]
      [--max-events N] [--workers 4] [--echo-cmds] [--json]

Prerequisites: python3 with PyYAML (tools/venv), ffmpeg/ffprobe with drawtext.
Exit: 0 = package written; 2 = fatal (bad span/manifest/sheet failure).
"""

import argparse
import bisect
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import yaml

TOOL = "openai_package.py"
TOOL_VERSION = 2
PROMPT_VERSION = "ep_v2"

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# sheet geometry (5x6 tiles of 16:9 = 1600x1080 of picture + a 60 px provenance band)
DEFAULT_TILE_W = 320
DEFAULT_COLS = 5
DEFAULT_ROWS = 6
SHEET_MAX_LONG_EDGE = 1600
SHEET_BAND_PX = 60
DEFAULT_SHEET_INTERVAL = 10.0      # seconds between sheet frames (1 frame / 10 s)
JPEG_Q = 4                         # ffmpeg -q:v (2 best .. 31 worst) => moderate
DEFAULT_DENSE_W = 960
DEFAULT_DENSE_H = 540              # 510 image tokens/frame per the review

# pack budget: the whole evidence_pack.json must stay under this many bytes
PACK_BUDGET_BYTES = 40 * 1024

# token model used for the estimate (stated in package_index.json)
TOKENS_PER_SHEET = 1700            # 30-frame contact sheet
TOKENS_PER_DENSE_FRAME = 510       # 960x540 frame

# aggregate / ranking keys that must NEVER reach the model-visible evidence
DENY_CANDIDATE_KEYS = ("weighted_sum", "_w")
DENY_EVENT_KEYS = ("recommended_by_score", "recommendation_note", "recommendation")
LOCAL_ONLY_PURPOSE = "local-only comparison — must never be sent to the model"

# per-signal provenance: status + the one-line caveat that must travel with it
EVIDENCE_NOTES = {
    "sync": {"status": "unverified",
             "caveat": "declarative, not measured on this media — the mix declares the "
                       "sources synced; no sync_multicam measurement backs it here."},
    "info_gain": {"status": "potentially_confounded",
                  "caveat": "histogram difference is dominated by the A/B exposure "
                            "difference, not by what each camera actually shows."},
    "action": {"status": "proxy",
               "caveat": "B-camera audio RMS, not visual hand/equipment interaction."},
    "coverage": {"status": "unknown",
                 "caveat": "unknown wherever the value represents footage that was "
                           "never sampled."},
    "phrase_phase": {"status": "nominal",
                     "caveat": "downbeat phase not strongly detected — it is assumed, "
                               "so a cited downbeat is not a measured downbeat."},
    "quality": {"status": "degraded",
                "caveat": "keyframe backfill is incomplete, so sharpness/brightness "
                          "coverage is partial."},
}

PRECEDENCE_RULE = ("Visual evidence and the editorial reference take precedence over "
                   "heuristic scores when they disagree.")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def eprint(*a):
    print(*a, file=sys.stderr)


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_obj(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def run(cmd, timeout=None):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout)


def shell_join(cmd):
    out = []
    for c in cmd:
        if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", c):
            out.append(c)
        else:
            out.append("'" + c.replace("'", "'\\''") + "'")
    return " ".join(out)


def ffprobe_video(path):
    """Real duration/geometry of a source. Header read only — no decode."""
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,r_frame_rate,codec_name,nb_frames",
           "-show_entries", "format=duration,size",
           "-of", "json", path]
    r = run(cmd)
    if r.returncode != 0:
        return None, r.stderr.decode(errors="replace").strip()[-300:]
    try:
        d = json.loads(r.stdout.decode())
    except ValueError as e:
        return None, "ffprobe json: %s" % e
    st = (d.get("streams") or [{}])[0]
    fm = d.get("format") or {}
    rfr = st.get("r_frame_rate") or "0/1"
    try:
        num, den = [int(x) for x in rfr.split("/")]
    except ValueError:
        num, den = 0, 1
    return {
        "width": st.get("width"), "height": st.get("height"),
        "codec_name": st.get("codec_name"),
        "fps": (num / den) if den else None,
        "fps_num": num, "fps_den": den,
        "nb_frames": int(st["nb_frames"]) if str(st.get("nb_frames", "")).isdigit() else None,
        "duration_s": float(fm["duration"]) if fm.get("duration") else None,
        "size_bytes": int(fm["size"]) if fm.get("size") else None,
    }, None


HASH_METHOD = ("metadata fingerprint sha256(path,size,mtime_ns,duration,nb_frames,"
               "codec) — full-file content hash not cheap on NFS 4K masters")


def source_fingerprint(path, info):
    """Cheap, honest identity for a 13 GB NFS source.

    A content hash of the whole file is NOT cheap here (~13 GB over NFS at
    ~13 MB/s ≈ 17 min). We fingerprint metadata instead and say so.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None, "stat failed"
    payload = json.dumps({
        "path": os.path.abspath(path), "size": st.st_size,
        "mtime_ns": st.st_mtime_ns, "duration_s": (info or {}).get("duration_s"),
        "nb_frames": (info or {}).get("nb_frames"),
        "codec": (info or {}).get("codec_name"),
    }, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest(), None


def fmt_tc(t):
    """Seconds -> HH:MM:SS.mmm (truncated to ms, never rounded upward)."""
    if t is None:
        return None
    neg = t < 0
    t = abs(t)
    ms = int(math.floor(t * 1000.0 + 1e-6))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%s%02d:%02d:%02d.%03d" % ("-" if neg else "", h, m, s, ms)


def tc_for_filename(tc):
    """00:05:00.033 -> 00-05-00.033 (colons are illegal in ffmpeg filenames here)."""
    return tc.replace(":", "-")


def trunc_ms(t):
    """Seconds -> the value the burn-in actually shows, i.e. truncated to ms."""
    return math.floor(t * 1000.0 + 1e-6) / 1000.0


def snap_to_frame(t, fps_num, fps_den):
    """Nearest real frame time on the source's grid -> (frame_index, exact_t)."""
    if not fps_num or not fps_den:
        return None, round(t, 3)
    n = int(round(t * fps_num / float(fps_den)))
    return n, n * fps_den / float(fps_num)


def dt(s, fontsize, x, y, borderw=2, fontcolor="white", box=None, boxcolor=None):
    """One drawtext filter clause. Escapes the filtergraph metacharacters."""
    esc = s
    for ch in ("\\", ":", "'", ",", "%", "[", "]"):
        esc = esc.replace(ch, "\\" + ch)
    extra = ""
    if box:
        extra += ":box=1:boxborderw=%d:boxcolor=%s" % (box, boxcolor or "black@0.6")
    return ("drawtext=fontfile=%s:text='%s':fontsize=%d:fontcolor=%s:borderw=%d:"
            "bordercolor=black:x=%s:y=%s%s" % (FONT, esc, fontsize, fontcolor,
                                               borderw, x, y, extra))


def sanitize_id(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))


# --------------------------------------------------------------------------
# input parsing
# --------------------------------------------------------------------------

def parse_span(s):
    m = re.fullmatch(r"\s*([0-9.]+)\s*[-:]\s*([0-9.]+)\s*", s or "")
    if not m:
        raise SystemExit("bad --span %r (want START-END seconds, e.g. 300-600)" % s)
    a, b = float(m.group(1)), float(m.group(2))
    if b <= a:
        raise SystemExit("bad --span %r (end must be > start)" % s)
    return a, b


def load_yaml_json(path):
    with open(path) as f:
        txt = f.read()
    if path.endswith(".json"):
        return json.loads(txt)
    return yaml.safe_load(txt)


POLICY_KEYS = ["minimum_b_shot", "maximum_b_shot", "minimum_a_recovery",
               "cut_grid", "low_confidence_action"]


def find_policy_block(doc):
    """Policy keys may sit at the top level, or under editing:/policy:/policies:."""
    for cand in (doc, (doc or {}).get("editing"), (doc or {}).get("policy"),
                 (doc or {}).get("policies")):
        if isinstance(cand, dict) and any(k in cand for k in POLICY_KEYS):
            return cand
    return None


def load_policy(path, manifest, manifest_path):
    """Return (policy_dict, provenance_note)."""
    src = None
    block = None
    if path:
        doc = load_yaml_json(path)
        block = find_policy_block(doc)
        src = path
        if block is None:
            raise SystemExit("--policy %s has no policy keys (%s)"
                             % (path, ", ".join(POLICY_KEYS)))
    else:
        tmpl = (manifest or {}).get("template")
        if tmpl:
            here = os.path.dirname(os.path.abspath(manifest_path))
            guesses = [
                os.path.join("/opt/video-studio/tools/template/definitions", tmpl + ".yaml"),
                os.path.join(here, tmpl + ".yaml"),
            ]
            for g in guesses:
                if os.path.exists(g):
                    doc = load_yaml_json(g)
                    block = find_policy_block(doc)
                    if block is not None:
                        src = g + " (fallback: --policy not given; manifest template %s)" % tmpl
                        break
    if block is None:
        return {k: None for k in POLICY_KEYS}, "none (no --policy and no template definition found)"
    pol = {k: block.get(k) for k in POLICY_KEYS}
    missing = [k for k in POLICY_KEYS if pol[k] is None]
    note = src
    if missing:
        note += " (missing keys: %s)" % ", ".join(missing)
    return pol, note


def load_beats(path):
    d = load_yaml_json(path)
    if isinstance(d, dict):
        return [float(x) for x in d.get("beats", [])]
    return [float(x) for x in d]


def load_phrases(path):
    d = load_yaml_json(path)
    if not isinstance(d, dict):
        raise SystemExit("--phrases %s: expected an object" % path)
    ph = d.get("phrases") or []
    starts = d.get("phrase_starts") or [p.get("start") for p in ph]
    ends = d.get("phrase_ends") or [p.get("end") for p in ph]
    downbeats = [float(x) for x in (d.get("downbeats") or [])]
    return {
        "raw": d, "phrases": ph,
        "phrase_starts": [float(x) for x in starts],
        "phrase_ends": [float(x) for x in ends],
        "downbeats": downbeats,
        "bpm": d.get("bpm"),
        "beats_per_bar": d.get("beats_per_bar"),
        "bars_per_phrase": d.get("bars_per_phrase"),
        "phase_method": d.get("phase_method"),
        "downbeat_phase": d.get("downbeat_phase"),
        "path": path,
    }


# --------------------------------------------------------------------------
# sources (A reel / B virtual reel)
# --------------------------------------------------------------------------

def resolve_sources(manifest, media_root):
    """Manifest sources -> probed, addressed files. B is ONE virtual reel."""
    src = (manifest or {}).get("sources") or {}
    a_decl = src.get("a_reel")
    b_decl = src.get("b_reel") or []
    if isinstance(b_decl, str):
        b_decl = [b_decl]

    def abs_path(decl):
        if os.path.isabs(decl):
            return decl
        return os.path.join(media_root, decl)

    sources = []
    a_path = abs_path(a_decl) if a_decl else None
    if a_path:
        info, err = ffprobe_video(a_path)
        fp, fperr = source_fingerprint(a_path, info) if info else (None, "no probe")
        sources.append({"name": a_decl, "role": "a_reel", "angle": "A",
                        "path": a_path, "exists": os.path.exists(a_path),
                        "probe": info, "probe_error": err,
                        "fingerprint_sha256": fp, "fingerprint_error": fperr})
    offsets = []
    t = 0.0
    for i, decl in enumerate(b_decl):
        p = abs_path(decl)
        info, err = ffprobe_video(p)
        fp, fperr = source_fingerprint(p, info) if info else (None, "no probe")
        sources.append({"name": decl, "role": "b_reel[%d]" % i, "angle": "B",
                        "path": p, "exists": os.path.exists(p),
                        "probe": info, "probe_error": err,
                        "fingerprint_sha256": fp, "fingerprint_error": fperr})
        dur = (info or {}).get("duration_s") or 0.0
        offsets.append({"index": i, "name": decl, "path": p,
                        "virtual_start": round(t, 3), "virtual_end": round(t + dur, 3),
                        "duration_s": round(dur, 3)})
        t += dur
    return sources, {"duration_s": round(t, 3), "offsets": offsets}


def pick_source(sources, angle, virtual_t):
    """(path, source_name, t_in_file, virtual_t) for an angle at a reel time."""
    if angle == "A":
        for s in sources:
            if s["role"] == "a_reel":
                return s["path"], s["name"], virtual_t, virtual_t
        return None, None, None, virtual_t
    acc = 0.0
    for s in sources:
        if s["role"].startswith("b_reel"):
            dur = (s["probe"] or {}).get("duration_s") or 0.0
            if virtual_t < acc + dur or s is sources[-1]:
                return s["path"], s["name"], virtual_t - acc, virtual_t
            acc += dur
    return None, None, None, virtual_t


# --------------------------------------------------------------------------
# musical context
# --------------------------------------------------------------------------

def musical_context(t, beats, phr):
    """Nearest beat / nearest downbeat / phrase index / bars into phrase.

    Mirrors candidate_score.py's own context block so the two agree — and adds
    the phrase artifact's phase_method, so a NOMINAL (assumed) downbeat phase
    stays visible to the model that is choosing cuts from it.
    """
    ctx = {
        "nearest_beat": None, "beat_distance_s": None,
        "nearest_downbeat": None, "downbeat_distance_s": None, "at_downbeat": False,
        "phrase_index": None, "phrase_id": None,
        "phrase_start": None, "phrase_end": None,
        "bars_into_phrase": None, "at_phrase_boundary": False,
        "bars_per_phrase": None, "beats_per_bar": None, "bpm": None,
    }
    if beats:
        nb = min(beats, key=lambda b: abs(b - t))
        ctx["nearest_beat"] = trunc_ms(nb)
        ctx["beat_distance_s"] = trunc_ms(t - nb)
    if phr:
        ctx["bpm"] = phr.get("bpm")
        ctx["beats_per_bar"] = phr.get("beats_per_bar")
        ctx["bars_per_phrase"] = phr.get("bars_per_phrase")
        dow = phr.get("downbeats") or []
        if dow:
            nd = min(dow, key=lambda d: abs(d - t))
            ctx["nearest_downbeat"] = trunc_ms(nd)
            ctx["downbeat_distance_s"] = trunc_ms(t - nd)
            ctx["at_downbeat"] = abs(t - nd) < 1e-3
        ps = phr.get("phrase_starts") or []
        if ps:
            k = bisect.bisect_right(ps, t + 1e-9) - 1
            if k >= 0:
                ctx["phrase_index"] = k
                ph = phr["phrases"][k] if k < len(phr["phrases"]) else {}
                ctx["phrase_id"] = ph.get("id")
                ctx["phrase_start"] = trunc_ms(ps[k])
                pe = phr.get("phrase_ends") or []
                if k < len(pe):
                    ctx["phrase_end"] = trunc_ms(pe[k])
                ctx["at_phrase_boundary"] = abs(t - ps[k]) < 1e-3
                sb = ph.get("start_bar")
                if sb is None and phr.get("bars_per_phrase") is not None:
                    sb = k * phr["bars_per_phrase"]
                if sb is not None and dow:
                    bi = bisect.bisect_right(dow, t + 1e-9) - 1
                    ctx["bars_into_phrase"] = max(0, bi - int(sb))
    return ctx


# --------------------------------------------------------------------------
# frame / sheet extraction
# --------------------------------------------------------------------------

def tile_filter(tc, angle, tile_w, tile_h):
    """Per-tile burn-in: timecode top-left, ANGLE-CAM bottom-left (proxy style)."""
    return ",".join([
        "scale=%d:%d:flags=bicubic" % (tile_w, tile_h),
        dt(tc, 15, 6, 6, borderw=2),
        dt("%s-CAM" % angle, 12, 6, tile_h - 20, borderw=2, fontcolor="#d0d0d0"),
    ])


def dense_filter(tc, angle, event_id, low_conf, w, h):
    """Dense-frame burn-in: timecode TL, event id TR, ANGLE-CAM BL (+LOW CONF BR)."""
    chain = [
        "scale=%d:%d:flags=bicubic" % (w, h),
        dt(tc, 28, 12, 10, borderw=3),
        dt(event_id, 24, "w-tw-12", 12, borderw=3, fontcolor="#e8e8e8"),
        dt("%s-CAM" % angle, 22, 12, "h-th-12", borderw=3, fontcolor="#d0d0d0"),
    ]
    if low_conf:
        chain.append(dt("LOW CONF", 22, "w-tw-12", "h-th-12", borderw=3,
                        fontcolor="#ff6060"))
    return ",".join(chain)


def extract_frame(src, t, out_path, vf, q=JPEG_Q, timeout=240):
    return [FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
            "-ss", "%.3f" % t, "-i", src, "-frames:v", "1",
            "-vf", vf, "-q:v", str(q), "-y", out_path]


def frame_exists(path):
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def make_blank(path, w, h):
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi",
           "-i", "color=c=0x101010:s=%dx%d" % (w, h), "-frames:v", "1",
           "-q:v", str(JPEG_Q), "-y", path]
    r = run(cmd)
    return r.returncode == 0, cmd, r.stderr.decode(errors="replace")[-200:]


# --------------------------------------------------------------------------
# aggregate hiding
# --------------------------------------------------------------------------

def strip_candidate(c):
    return {k: v for k, v in c.items() if k not in DENY_CANDIDATE_KEYS}


def strip_events_for_model(events_verbatim):
    """Source events minus every field that implies a winner.

    Returns (events, stripped_keys) where stripped_keys is the exact set of
    keys that were removed, so the removal is auditable rather than silent.
    """
    stripped = set()
    out = []
    for ev in events_verbatim:
        e = {k: v for k, v in ev.items() if k not in DENY_EVENT_KEYS}
        for k in DENY_EVENT_KEYS:
            if k in ev:
                stripped.add(k)
        cands = []
        for c in e.get("candidates") or []:
            for k in DENY_CANDIDATE_KEYS:
                if k in c:
                    stripped.add(k)
            cands.append(strip_candidate(c))
        e["candidates"] = cands
        out.append(e)
    return out, sorted(stripped)


# --------------------------------------------------------------------------
# segment selection reason
# --------------------------------------------------------------------------

def derive_segment_reason(span, events, phr, beats, bprof):
    """Build the concrete, evidence-backed reason for this 5-minute span.

    Only descriptive facts about the segment — never a per-event winner, and
    never an aggregate of the ranking. `at_phrase_boundary`, `at_downbeat` and
    the candidate notes are per-candidate evidence, which is what the model is
    allowed to see.
    """
    lo, hi = span
    facts = {}
    clauses = []
    if phr:
        ps = [p for p in phr["phrase_starts"] if lo <= p < hi]
        dow = [d for d in (phr.get("downbeats") or []) if lo <= d < hi]
        idxs = []
        for p in ps[:1] + ps[-1:]:
            k = bisect.bisect_left(phr["phrase_starts"], p)
            idxs.append(k)
        facts["phrase_boundaries_in_span"] = len(ps)
        facts["downbeats_in_span"] = len(dow)
        facts["phrase_index_range"] = idxs if len(idxs) == 2 else None
        clauses.append("%d phrase boundaries (phrase %s..%s) and %d downbeats "
                       "inside the span" % (len(ps),
                                            idxs[0] if idxs else "?",
                                            idxs[-1] if idxs else "?",
                                            len(dow)))
    if beats:
        facts["beats_in_span"] = len([b for b in beats if lo <= b < hi])
        clauses.append("%d beat-grid points, so every boundary in the span is "
                       "musically anchored" % facts["beats_in_span"])
    kinds = {}
    for ev in events:
        if ev["kind"] == "candidate":
            ctx = (ev.get("source_event") or {}).get("context") or {}
            k = ctx.get("boundary_kind") or ("downbeat" if ctx.get("downbeat")
                                             else "beat")
        else:
            k = ev["kind"]
        kinds[k] = kinds.get(k, 0) + 1
    facts["event_kinds"] = kinds
    facts["n_events"] = len(events)
    clauses.append("%d scored editorial events (%s)" %
                   (len(events), ", ".join("%d %s" % (v, k) for k, v in sorted(kinds.items()))))
    # real B opportunities: the B reel's own audio-action profile
    if bprof:
        segs = [s for s in (bprof.get("segments") or [])
                if s.get("start") is not None and lo <= s["start"] < hi]
        if segs:
            vals = sorted(float(s.get("score") or 0.0) for s in segs)
            mean = sum(vals) / len(vals)
            p90 = vals[min(len(vals) - 1, int(round(0.9 * (len(vals) - 1))))]
            peak = max(segs, key=lambda s: float(s.get("score") or 0.0))
            facts["b_action_segments_in_span"] = len(segs)
            facts["b_action_mean"] = round(mean, 3)
            facts["b_action_p90"] = round(p90, 3)
            facts["b_action_peak"] = {"t": peak.get("start"), "score": peak.get("score")}
            clauses.append("real B material is present: %d x 10 s B-reel action "
                           "segments in span, mean %.3f / p90 %.3f, peak at %.1f s"
                           % (len(segs), mean, p90, float(peak.get("start") or 0.0)))
    # plausible hold_a situations: a switch that would violate minimum_a_recovery
    if phr:
        holds = 0
        for ev in events:
            if ev["kind"] != "candidate":
                continue
            cands = (ev.get("source_event") or {}).get("candidates") or []
            for c in cands:
                if any("short A recovery" in n for n in (c.get("notes") or [])):
                    holds += 1
                    break
        facts["events_with_a_short_a_recovery"] = holds
        if holds:
            clauses.append("%d event(s) where a switch would break the minimum A "
                           "recovery, i.e. a plausible hold_a situation on the "
                           "editorial merits" % holds)
        else:
            clauses.append("0 events where a switch would break the minimum A "
                           "recovery (no hold_a-forced situation in this span)")
    reason = ("Chosen for editorial variety over the tester's A/B reels: "
              + "; ".join(clauses)
              + ". Mixer interaction is NOT independently verifiable from this "
                "package (there is no visual hand/equipment signal — the action "
                "score is an audio RMS proxy), so it is listed as an open question "
                "for the reference edit rather than claimed as present.")
    return reason, facts


# --------------------------------------------------------------------------
# prompt template
# --------------------------------------------------------------------------

def build_prompt(span_start, span_end, n_sheets, events, policy, pkg_name):
    eids = [e["event_id"] for e in events]
    policy_line = ", ".join("%s=%s" % (k, policy.get(k)) for k in POLICY_KEYS)
    return """# Editorial selector prompt — DJ set edit (evidence package v2)

> **Provider boundary:** this prompt and its evidence package were built LOCALLY.
> Nothing here has been sent anywhere. `package_index.json` carries
> `"transmitted": false` for every file.

## Role

You are the **editorial selector** for a DJ set edit. You are NOT the editor of
the timeline and you do NOT author cut times. A deterministic tool has already
computed every legal musical boundary and every legal switch for it. Your job is
to **choose among the candidates that are listed for each event**, and to explain,
in one line each, why that choice is the right one for a club edit of this set.

You are looking at SAMPLED FRAMES, not video: contact sheets (`overview/`, one
sheet per ~5 minutes, 30 tiles at 1 frame / 10 s) and dense bursts
(`events/<event_id>/`, a few frames per second inside that event's window).
Every tile and every dense frame carries a burned-in absolute timecode
`HH:MM:SS.mmm`; dense-frame timecodes are also in the filenames
(`dense_<event_id>_HH-MM-SS.mmm.jpg`). Cite times with that format, and only with
times that appear either burned into an image or in the evidence pack.

**%s**

## Hard rules

1. For every event in the evidence pack, choose **exactly one** candidate from
   that event's own `candidates` list, by its `id`. You may not invent, modify or
   interpolate a candidate, and you may not output a time of your own.
2. `hold_a` (stay on the A camera) is **always a legal and acceptable choice** —
   it is present in every event's candidate list. Selecting it is never a
   failure. Choose it whenever a B switch does not earn its place.
3. **Never invent a timing.** All times come from the evidence pack or from burned
   timecodes. If you need a time you were not given, say so in the rationale
   instead of making one up.
4. **One line of rationale** per event, max 240 characters, stated in editorial
   terms (what the viewer sees, why the cut earns its place).
5. **State a confidence between 0 and 1** for every choice. Use low confidence
   when the frames are dark or ambiguous, when the A/B match is unverified, or
   when the phrase phase is only nominal.
6. The `policy` block is binding context, not a suggestion — respect
   `minimum_b_shot`, `maximum_b_shot`, `minimum_a_recovery`, `cut_grid` and
   `low_confidence_action`. `low_confidence_action: stay_on_a` means: when in
   doubt, hold A.
7. **No aggregate is provided, deliberately.** You get eight individual evidence
   scores per candidate and no total, no ranking and no recommendation. Judging
   their relative importance is your job, and the eight scores are not equally
   trustworthy — read `evidence_notes` in the evidence pack before you lean on
   any of them.
8. The phrase artifact's `phase_method` is `nominal`: the downbeat phase is
   **assumed, not measured**. Do not treat a cited downbeat as certain.
9. Output **JSON only** — no prose before or after the JSON. No markdown fences.

## Required output schema

```json
{
  "decisions": [
    {
      "event_id": "evt_01",
      "selection": "b_downbeat",
      "confidence_score": 0.72,
      "rationale": "One line, <=240 chars, saying what the viewer sees and why this cut earns its place."
    }
  ]
}
```

- `event_id` — copied verbatim from the evidence pack.
- `selection` — the `id` of one of that event's listed candidates (including `hold_a`).
- `confidence_score` — number in [0, 1].
- `rationale` — one line, max 240 characters.

Emit exactly one decision object per event id in the evidence pack:
%s

## What you will receive

1. `overview/sheet_NN.jpg` — the contact sheets, in order. Tiles run
   left-to-right, top-to-bottom; each tile's burned timecode is authoritative.
2. `events/<event_id>/dense_<event_id>_HH-MM-SS.mmm.jpg` — the dense frames for
   that event.
3. `candidates/<event_id>.json` — the legal candidate set for that event
   (identical to the `candidates` list embedded in the evidence pack).
4. `evidence_pack.json` — the text half, pasted below in this prompt.
5. `reference/` — the human's approved reference edit, **if** one has been
   supplied. If the directory is still a placeholder, you have no reference: say
   so in your rationale when it changes what you would otherwise choose.

`candidates/deterministic_ranking_LOCAL.json` exists in the package but is
**local-only**: it is not part of your input and must not be treated as evidence.

Span: **%s -> %s** (%d sheet(s) attached).

---

## EVIDENCE PACK

<!-- ==== BEGIN EVIDENCE PACK — paste the full contents of evidence_pack.json here ==== -->
{{PASTE_EVIDENCE_PACK_JSON_HERE}}
<!-- ==== END EVIDENCE PACK ==== -->

Active policy (for reference only; the pack's `policy` block is authoritative):
%s

Package: `%s/`
""" % (PRECEDENCE_RULE, ", ".join(eids), fmt_tc(span_start), fmt_tc(span_end),
       n_sheets, policy_line, pkg_name)


REFERENCE_README = """# reference/ — awaiting the user-supplied reference edit

This directory is a **placeholder**. It is empty by design.

The human supplies the approved reference edit (frames, a proxy clip, or a
written edit description) here *before* the editorial package is given to a
model. Nothing in this directory was auto-populated and no reference was supplied
at package build time, so any model reading this package has **no reference
edit** to compare against — it must say so rather than invent one.

`transmitted: false` — this directory has never left the lab.
"""

# --------------------------------------------------------------------------
# main build
# --------------------------------------------------------------------------

def build(args):
    out_dir = os.path.abspath(args.out)
    pkg = os.path.join(out_dir, args.package_name)
    ovw = os.path.join(pkg, "overview")
    evd = os.path.join(pkg, "events")
    cdd = os.path.join(pkg, "candidates")
    ref = os.path.join(pkg, "reference")
    for d in (pkg, ovw, evd, cdd, ref):
        os.makedirs(d, exist_ok=True)
    media_root = os.path.abspath(args.media_root)
    span_start, span_end = parse_span(args.span)
    cmds = []

    def echo_cmd(cmd):
        cmds.append(shell_join(cmd))
        if args.echo_cmds:
            print("CMD: " + shell_join(cmd), flush=True)

    manifest = load_yaml_json(args.manifest)
    if not isinstance(manifest, dict):
        raise SystemExit("--manifest %s: expected a YAML/JSON object" % args.manifest)
    sources, b_reel = resolve_sources(manifest, media_root)
    missing = [s for s in sources if not s["exists"]]
    if missing:
        raise SystemExit("source(s) not found under --media-root %s: %s"
                         % (media_root, ", ".join(s["path"] for s in missing)))
    if not sources:
        raise SystemExit("--manifest declares no sources")

    beats, phr = [], None
    if args.beats:
        beats = load_beats(args.beats)
    if args.phrases:
        phr = load_phrases(args.phrases)
    if not beats and phr:
        beats = phr["downbeats"]          # explicit fallback, recorded in the pack

    policy, policy_src = load_policy(args.policy, manifest, args.manifest)
    bprof = load_yaml_json(args.b_profile) if args.b_profile else None

    # ------------------------------------------------- candidates (verbatim)
    model_events, cand_events, stripped_keys = [], [], []
    cand_all_events = []
    cand_out_of_span = 0
    if args.candidates:
        cand_doc = load_yaml_json(args.candidates)
        raw_events = (cand_doc.get("events") or []) if isinstance(cand_doc, dict) else []
        cand_all_events = raw_events
        in_span = []
        for ev in raw_events:
            t, _, _ = event_anchor(ev)
            if t is not None and span_start - 1e-6 <= t <= span_end + 1e-6:
                in_span.append(ev)
        cand_out_of_span = len(raw_events) - len(in_span)
        cand_events = in_span
        model_events, stripped_keys = strip_events_for_model(cand_events)

    tile_h = int(round(args.tile_w * 9.0 / 16.0))
    cols, rows = args.sheet_cols, args.sheet_rows
    if cols * rows != args.sheet_frames:
        raise SystemExit("--sheet-cols * --sheet-rows must equal --sheet-frames")

    # --- events: candidates if present, else phrase boundaries
    events = []
    truncated = 0
    if model_events:
        chosen = list(zip(model_events, cand_events))[:args.max_events]
        truncated = len(model_events) - len(chosen)
        for mev, raw in chosen:
            events.append({"event_id": mev.get("event_id") or ("evt_%02d" % (len(events) + 1)),
                           "kind": "candidate", "source_event": raw,
                           "model_event": mev})
        events_src = "candidates"
    elif phr:
        ps = [p for p in phr["phrase_starts"] if span_start <= p < span_end]
        chosen = ps[:args.max_events]
        truncated = len(ps) - len(chosen)
        for p in chosen:
            events.append({"event_id": "phrase_%03d" % (bisect.bisect_left(phr["phrase_starts"], p) + 1),
                           "kind": "phrase_boundary", "anchor": p, "source_event": None})
        events_src = "phrases (no --candidates given)"
    else:
        events_src = "none (no --candidates and no --phrases -> no dense pass)"

    # frame-grid lookup per source path (so every burned timecode is a REAL
    # frame time on that file's own grid, not a nominal 10 s tick)
    fps_by_path = {s["path"]: ((s["probe"] or {}).get("fps_num"),
                               (s["probe"] or {}).get("fps_den")) for s in sources}

    # ------------------------------------------------------- contact sheets
    interval = args.sheet_interval
    sheet_times = []
    t = span_start
    while t < span_end - 1e-9:
        sheet_times.append(round(t, 3))
        t += interval
    groups = [sheet_times[i:i + args.sheet_frames]
              for i in range(0, len(sheet_times), args.sheet_frames)]

    tmp_root = tempfile.mkdtemp(prefix=".opkg_tmp_", dir=out_dir)
    sheets = []
    n_reused = 0
    try:
        blank = os.path.join(tmp_root, "blank.jpg")
        ok, bcmd, berr = make_blank(blank, args.tile_w, tile_h)
        if not ok:
            raise SystemExit("could not create blank padding tile: %s" % berr)

        for si, group in enumerate(groups):
            sheet_name = "sheet_%02d.jpg" % (si + 1)
            rel = "overview/" + sheet_name
            sheet_path = os.path.join(ovw, sheet_name)
            fdir = os.path.join(tmp_root, "s%02d" % si)
            os.makedirs(fdir, exist_ok=True)
            angle = args.sheet_angle.upper()

            jobs = []
            for k, tt in enumerate(group):
                path, sname, t_in_file, vt = pick_source(sources, angle, tt)
                if path is None:
                    raise SystemExit("no source for angle %s" % angle)
                num, den = fps_by_path[path]
                n, at = snap_to_frame(t_in_file, num, den)
                fp = os.path.join(fdir, "f_%03d.jpg" % k)
                vf = tile_filter(fmt_tc(at), angle, args.tile_w, tile_h)
                jobs.append({"k": k, "t_req": tt, "t": at, "t_in_file": t_in_file,
                             "source": sname, "vf": vf, "out": fp,
                             "cmd": extract_frame(path, t_in_file, fp, vf)})
            for j in jobs:
                echo_cmd(j["cmd"])

            todo = [j for j in jobs
                    if not (args.reuse_images and frame_exists(j["out"]))]
            n_reused += len(jobs) - len(todo)
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                rcs = list(ex.map(lambda j: run(j["cmd"]), todo))
            bad = [todo[i]["out"] for i, r in enumerate(rcs) if r.returncode != 0]
            if bad:
                raise SystemExit("sheet %s: %d tile extraction(s) failed, e.g. %s"
                                 % (sheet_name, len(bad), bad[0]))
            for k in range(len(group), args.sheet_frames):
                shutil.copyfile(blank, os.path.join(fdir, "f_%03d.jpg" % k))

            idx_lo = si * args.sheet_frames
            idx_hi = idx_lo + len(group) - 1
            hi = group[-1] if group else span_start
            sname0 = jobs[0]["source"] if jobs else ""
            header = ("SHEET %02d/%02d  %s-CAM  %s  frames %d-%d  %s  @ %.3fs  "
                      "SPAN %s -> %s" % (si + 1, len(groups), angle,
                                         os.path.basename(sname0),
                                         idx_lo, idx_hi, "1 frame", interval,
                                         fmt_tc(group[0] if group else span_start),
                                         fmt_tc(hi)))
            vf = ",".join([
                "tile=%dx%d:padding=0:margin=0" % (cols, rows),
                "pad=%d:%d:0:%d:color=black" % (args.tile_w * cols,
                                                tile_h * rows + SHEET_BAND_PX,
                                                SHEET_BAND_PX),
                "scale='min(%d,iw)':-2" % SHEET_MAX_LONG_EDGE,
                dt(header, 20, 12, 14, borderw=2),
            ])
            cmd = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
                   "-framerate", "1", "-start_number", "0",
                   "-i", os.path.join(fdir, "f_%03d.jpg"),
                   "-vf", vf, "-frames:v", "1", "-q:v", str(JPEG_Q), "-y", sheet_path]
            echo_cmd(cmd)
            r = run(cmd)
            if r.returncode != 0 or not os.path.exists(sheet_path):
                raise SystemExit("sheet %s failed: %s"
                                 % (sheet_name, r.stderr.decode(errors="replace")[-400:]))
            info, err = ffprobe_video(sheet_path)
            long_edge = None
            if info and info.get("width"):
                long_edge = max(info["width"], info["height"] or 0)
            sheets.append({
                "file": rel,
                "sheet_index": si + 1,
                "angle": angle,
                "source": sname0,
                "span": [trunc_ms(group[0]) if group else None,
                         trunc_ms(hi)],
                "interval_s": interval,
                "cols": cols, "rows": rows,
                "tile_wh": [args.tile_w, tile_h],
                "n_frames": len(group),
                "n_padded": args.sheet_frames - len(group),
                "geometry": ("%sx%s" % (info.get("width"), info.get("height"))
                             if info else None),
                "long_edge_px": long_edge,
                "bytes": os.path.getsize(sheet_path),
                "sha256": sha256_file(sheet_path),
                "timecode_burn": "HH:MM:SS.mmm absolute source time, top-left of every tile",
                "tile_order": "row-major (left-to-right, top-to-bottom)",
                "frames": [[j["k"], trunc_ms(j["t"]), fmt_tc(j["t"])] for j in jobs],
                "frame_index_fields": ["tile_index", "burned_t_snapped", "burned_timecode"],
                "frame_times_requested": [trunc_ms(j["t_req"]) for j in jobs],
            })
    finally:
        if args.keep_frames:
            eprint("kept temp frames: %s" % tmp_root)
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)

    # ---------------------------------------------------------- dense frames
    dense_jobs = []
    for ev in events:
        eid = sanitize_id(ev["event_id"])
        if ev["kind"] == "candidate":
            anchor, asrc, aalts = event_anchor(ev["source_event"])
            angles = dense_angles_for_event(ev["source_event"], args.dense_angle)
        else:
            anchor, asrc, aalts = ev["anchor"], "phrase_start", {}
            angles = ["a", "b"] if args.dense_angle == "both" else (
                [args.dense_angle] if args.dense_angle in ("a", "b") else ["b"])
        ev["_dense"] = []          # internal: full records (hashes live in the index)
        ev["anchor_alternatives"] = aalts
        ev["event_dir"] = "events/%s" % eid
        if anchor is None:
            ev["anchor_t"] = None
            ev["anchor_source"] = asrc
            ev["dense_angle"] = None
            ev["note"] = ("event has no anchor time (no SWITCH candidate, no context "
                          "beat, no b_slot) -> dense pass skipped")
            continue
        ev["anchor_t"] = trunc_ms(anchor)
        ev["anchor_source"] = asrc
        ev["dense_angle"] = ",".join(a.upper() for a in angles)
        lo = anchor - args.dense_window / 2.0
        hi = anchor + args.dense_window / 2.0
        lo_c, hi_c = max(lo, span_start), min(hi, span_end)
        ev["dense_window"] = {
            "requested": [trunc_ms(lo), trunc_ms(hi)],
            "used": [trunc_ms(lo_c), trunc_ms(hi_c)],
            "clamped_to_span": bool(lo_c > lo + 1e-9 or hi_c < hi - 1e-9),
            "span": [span_start, span_end],
        }
        lo, hi = lo_c, hi_c
        n = max(1, int(round((hi - lo) * args.dense_fps)) + 1)
        step = (hi - lo) / float(max(1, n - 1)) if n > 1 else 0.0
        target_dir = os.path.join(evd, eid)
        os.makedirs(target_dir, exist_ok=True)
        for angle in angles:
            for i in range(n):
                t = lo + i * step
                path, sname, t_in_file, vt = pick_source(sources, angle.upper(), t)
                if path is None:
                    continue
                num, den = fps_by_path[path]
                _, at = snap_to_frame(t_in_file, num, den)
                angle_tag = "" if len(angles) == 1 and args.dense_angle != "both" else "-%s" % angle
                fname = "dense_%s%s_%s.jpg" % (eid, angle_tag, tc_for_filename(fmt_tc(at)))
                rel = "events/%s/%s" % (eid, fname)
                fp = os.path.join(target_dir, fname)
                low_conf = False
                if ev["kind"] == "candidate":
                    low_conf = any((c.get("confidence") == "low")
                                   for c in ev["source_event"].get("candidates", []))
                vf = dense_filter(fmt_tc(at), angle.upper(), eid, low_conf,
                                  args.dense_w, args.dense_h)
                cmd = extract_frame(path, t_in_file, fp, vf)
                echo_cmd(cmd)
                rec = {"file": rel, "basename": fname, "t_reel": trunc_ms(t),
                       "t_actual": trunc_ms(at), "timecode": fmt_tc(at),
                       "angle": angle.upper(), "source": sname,
                       "source_t": trunc_ms(t_in_file)}
                ev["_dense"].append(rec)
                dense_jobs.append((cmd, rec, fp))

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        todo = [j for j in dense_jobs if not (args.reuse_images and frame_exists(j[2]))]
        n_reused += len(dense_jobs) - len(todo)
        results = list(ex.map(lambda j: (run(j[0]), j[1], j[2]), todo))
    failed = []
    err_by_file = {}
    for r, rec, fp in results:
        if r.returncode != 0:
            err_by_file[rec["file"]] = r.stderr.decode(errors="replace")[-200:]
    for cmd, rec, fp in dense_jobs:
        if not frame_exists(fp):
            failed.append({"file": rec["file"], "cmd": shell_join(cmd),
                           "error": err_by_file.get(rec["file"], "missing output file")})
            rec["failed"] = True
            continue
        rec["bytes"] = os.path.getsize(fp)
        rec["sha256"] = sha256_file(fp)

    # ------------------------------------- candidate files (model + local-only)
    cand_purposes = {}
    for ev in events:
        eid = sanitize_id(ev["event_id"])
        rel = "candidates/%s.json" % eid
        cand_purposes[rel] = "legal candidate set for %s (model input; no aggregate)" % eid
        doc = {"event_id": ev["event_id"], "kind": ev["kind"]}
        if ev["kind"] == "candidate":
            m = ev["model_event"]
            doc["anchor_t"] = ev["anchor_t"]
            doc["anchor_source"] = ev["anchor_source"]
            doc["anchor_alternatives"] = ev["anchor_alternatives"]
            doc["boundary_context_source"] = m.get("context")
            doc["candidates"] = m.get("candidates")
            doc["candidates_sha256"] = sha256_obj(m.get("candidates"))
            doc["aggregate_withheld"] = (
                "this file carries the eight individual evidence scores per candidate "
                "and nothing else: no total, no ranking and no recommendation. The "
                "aggregate ranking is recorded in package_index.json as a local-only "
                "artifact that must never be sent to the model.")
        else:
            doc["anchor_t"] = ev["anchor_t"]
            doc["candidates"] = None
            doc["note"] = ("no --candidates given: this window is centred on a phrase "
                           "boundary from --phrases, not on a scored candidate set")
        with open(os.path.join(cdd, rel.split("/", 1)[1]), "w") as f:
            json.dump(doc, f, indent=1)
        ev["candidates_file"] = rel

    local_rel = "candidates/deterministic_ranking_LOCAL.json"
    cand_purposes[local_rel] = LOCAL_ONLY_PURPOSE
    local_doc = {
        "purpose": LOCAL_ONLY_PURPOSE,
        "transmitted": False,
        "warning": ("This file carries the aggregates withheld from the model-visible "
                    "evidence (the per-candidate weighted total, the per-event "
                    "recommended id and the source event order). It exists for local "
                    "comparison and audit only."),
        "withheld_field_names_per_candidate": list(DENY_CANDIDATE_KEYS),
        "withheld_field_names_per_event": list(DENY_EVENT_KEYS),
        "source_candidates_file": os.path.abspath(args.candidates) if args.candidates else None,
        "span": {"source_start": span_start, "source_end": span_end},
        "policy_at_build": policy,
        "stripped_from_model_visible_files": (stripped_keys or
                                              ["(nothing: no --candidates)"]),
        "events_verbatim_in_span": cand_events,
        "events_verbatim_all_source_events": cand_all_events,
    }
    with open(os.path.join(cdd, "deterministic_ranking_LOCAL.json"), "w") as f:
        json.dump(local_doc, f, indent=1)

    # ------------------------------------------------------------ reference/
    with open(os.path.join(ref, "README.md"), "w") as f:
        f.write(REFERENCE_README)

    # --------------------------------------------------------- evidence pack
    for s in sources:
        pr = s.pop("probe", None) or {}
        s["duration_s"] = trunc_ms(pr["duration_s"]) if pr.get("duration_s") else None
        s["size_bytes"] = pr.get("size_bytes")
        s["width"] = pr.get("width")
        s["height"] = pr.get("height")
        s["fps"] = round(pr.get("fps"), 3) if pr.get("fps") else None
        s["codec"] = pr.get("codec_name")
        s["nb_frames"] = pr.get("nb_frames")
        if not s.get("probe_error"):
            s.pop("probe_error", None)
        if s.get("fingerprint_error"):
            s.pop("fingerprint_error", None)
        s.pop("hash_method", None)

    # ------------------------------------------------------- musical context
    for ev in events:
        anchor = ev.get("anchor_t")
        if anchor is None:
            ev["context"] = {}
            continue
        ctx = musical_context(anchor, beats, phr)
        if phr:
            ctx["phase_method"] = phr.get("phase_method")
            ctx["downbeat_phase"] = phr.get("downbeat_phase")
        ev["context"] = ctx

    seg_reason, seg_facts = derive_segment_reason(
        (span_start, span_end), events, phr, beats, bprof)

    # compact per-event dense index for the pack (hashes/bytes live in the index)
    for ev in events:
        recs = [r for r in (ev.get("_dense") or []) if not r.get("failed")]
        ev["dense_frames_compact"] = {
            "dir": ev.get("event_dir"),
            "angle": ev.get("dense_angle"),
            "n_frames": len(recs),
            "window_used": (ev.get("dense_window") or {}).get("used"),
            "files": [r["basename"] for r in recs],
        }

    pack_events = []
    for ev in sorted(events, key=lambda e: (e["anchor_t"] is None, e["anchor_t"] or 0)):
        if ev["kind"] == "candidate":
            m = ev["model_event"]
            sc = m.get("context") or {}
            pack_events.append({
                "event_id": ev["event_id"],
                "anchor_t": ev["anchor_t"],
                "anchor_source": ev["anchor_source"],
                "candidates_file": ev["candidates_file"],
                "candidates": [{k: v for k, v in c.items() if k != "notes"}
                               for c in (m.get("candidates") or [])],
                "candidates_sha256_full_set": sha256_obj(m.get("candidates")),
                "boundary_context_source": {k: sc.get(k) for k in
                                            ("boundary_kind", "b_slot",
                                             "structure_source") if k in sc},
                "context": ev["context"],
                "dense_window": ev["dense_window"],
                "dense_angle": ev["dense_angle"],
                "dense_frames": ev["dense_frames_compact"],
            })
        else:
            pack_events.append({
                "event_id": ev["event_id"],
                "kind": ev["kind"],
                "anchor_t": ev["anchor_t"],
                "anchor_source": ev["anchor_source"],
                "candidates_file": ev["candidates_file"],
                "candidates": None,
                "context": ev["context"],
                "dense_window": ev.get("dense_window"),
                "dense_angle": ev["dense_angle"],
                "dense_frames": ev["dense_frames_compact"],
                "note": ("no --candidates given: this window is centred on a phrase "
                         "boundary from --phrases, not on a scored candidate set"),
            })
        if phr:
            pass  # phase_method already merged into ev["context"] above

    n_dense_ok = sum(e["dense_frames"]["n_frames"] for e in pack_events)
    n_dense_all = sum(len(ev.get("_dense") or []) for ev in events)

    pack = {
        "package": {
            "name": args.package_name,
            "tool": TOOL, "tool_version": TOOL_VERSION,
            "prompt_version": PROMPT_VERSION,
            "what_this_is": ("LOCAL editorial evidence package. Sampled frames carried "
                             "as contact sheets plus dense bursts; the model chooses "
                             "among the listed candidates, it does not author cut times."),
            "transmitted": False,
            "network_calls_made": 0,
        },
        "layout": {
            "package_index.json": "audit record: every file, sha256, bytes, purpose, transmitted",
            "evidence_pack.json": "this file (paste into prompt.md)",
            "prompt.md": "prompt template for the editorial selector",
            "overview/": "timecoded contact sheets (sparse frames across the whole section)",
            "events/<event_id>/": "dense frames for that editorial event",
            "candidates/<event_id>.json": "legal candidate set for that event (no aggregate)",
            "candidates/deterministic_ranking_LOCAL.json": LOCAL_ONLY_PURPOSE,
            "reference/": ("placeholder awaiting the user-supplied reference edit "
                           "(empty at build time)"),
        },
        "span": {"start": trunc_ms(span_start), "end": trunc_ms(span_end),
                 "duration_s": trunc_ms(span_end - span_start),
                 "start_tc": fmt_tc(span_start), "end_tc": fmt_tc(span_end),
                 "source_start": trunc_ms(span_start),
                 "source_end": trunc_ms(span_end),
                 "reason_for_selection": args.segment_reason or seg_reason,
                 "reason_for_selection_source": ("--segment-reason (given)"
                                                 if args.segment_reason
                                                 else "derived from local evidence"),
                 "selection_evidence": seg_facts},
        "source_start": trunc_ms(span_start),
        "source_end": trunc_ms(span_end),
        "reason_for_selection": args.segment_reason or seg_reason,
        "manifest": {
            "path": os.path.abspath(args.manifest),
            "job_id": manifest.get("job_id"), "template": manifest.get("template"),
            "n_cuts_declared": len(manifest.get("timeline") or []),
            "profiles": manifest.get("profiles"),
        },
        "sources": sources,
        "source_hash_method": HASH_METHOD,
        "b_virtual_reel": b_reel,
        "phrase_artifact": ({
            "path": phr["path"],
            "bpm": phr.get("bpm"),
            "beats_per_bar": phr.get("beats_per_bar"),
            "bars_per_phrase": phr.get("bars_per_phrase"),
            "phase_method": phr.get("phase_method"),
            "downbeat_phase": phr.get("downbeat_phase"),
            "phase_is_assumed": True,
            "note": ("the downbeat PHASE is assumed, not measured on this media — a "
                     "cited downbeat is therefore nominal, and every event context "
                     "carries phase_method to keep that visible downstream"),
        } if phr else None),
        "candidates_note": ("each event's `candidates` list in this pack carries id / "
                            "angle / action / window / the eight individual evidence "
                            "scores. The per-candidate `notes` strings and the anchor "
                            "alternatives live in candidates_file; "
                            "candidates_sha256_full_set covers the full set as written "
                            "there."),
        "sheets": sheets,
        "events": pack_events,
        "policy": dict(policy, **{"source": policy_src}),
        "evidence_notes": EVIDENCE_NOTES,
        "precedence_rule": PRECEDENCE_RULE,
        "aggregate_policy": {
            "aggregates_present_in_model_visible_evidence": False,
            "withheld_field_count_per_candidate": len(DENY_CANDIDATE_KEYS),
            "withheld_field_count_per_event": len(DENY_EVENT_KEYS),
            "where_they_live": local_rel,
            "where_they_live_purpose": LOCAL_ONLY_PURPOSE,
            "names_recorded_in": "package_index.json (aggregate_policy_block) and " + local_rel,
            "note": ("No aggregate, weighted total, ranking or recommendation is "
                     "present anywhere the model can see: only the eight individual "
                     "evidence scores per candidate. Events are listed in chronological "
                     "order, which is not a ranking; candidate order within an event is "
                     "the source order, which is not a ranking either. Read "
                     "evidence_notes before trusting any score."),
        },
        "sampling": {
            "sheet_interval_s": args.sheet_interval,
            "sheet_frames_per_sheet": args.sheet_frames,
            "sheet_grid": [cols, rows],
            "sheet_tile_wh": [args.tile_w, tile_h],
            "dense_window_s": args.dense_window,
            "dense_fps": args.dense_fps,
            "dense_wh": [args.dense_w, args.dense_h],
            "dense_filename_rule": ("dense_<event_id>_HH-MM-SS.mmm.jpg — the burned "
                                    "timecode is IN the filename, so a frame can be "
                                    "cited without opening the image"),
            "dense_index_note": ("each event's dense_frames.files lists the basenames "
                                 "under dense_frames.dir; sha256 and bytes for every "
                                 "frame are in package_index.json"),
            "frames_are_sampled": ("the model reads sampled stills (sheets + dense "
                                   "bursts), never continuous video"),
        },
        "counts": {
            "n_sheets": len(sheets),
            "n_sheet_frames": sum(s["n_frames"] for s in sheets),
            "n_events": len(pack_events),
            "n_events_truncated_by_max_events": truncated,
            "n_candidates_out_of_span_ignored": cand_out_of_span,
            "n_dense_frames": n_dense_all,
            "n_dense_frames_ok": n_dense_ok,
            "n_dense_failures": len(failed),
            "n_images_total": len(sheets) + n_dense_ok,
            "events_source": events_src,
            "event_order": "chronological by anchor_t (not a ranking)",
        },
        "notes": [
            "Frame times are ABSOLUTE source seconds, snapped to the source frame grid; "
            "every sheet tile and dense frame burns its own timecode. sheet.frames rows "
            "are [tile_index, burned_t_snapped, burned_timecode]; "
            "frame_times_requested is the un-snapped request.",
            "Candidate objects are the --candidates objects minus the aggregate keys "
            "listed in aggregate_policy; every other field is unchanged, and "
            "candidates_sha256_full_set covers the full set as written in "
            "candidates/<event_id>.json (which also carries the per-candidate `notes` "
            "that the pack omits to stay inside its size budget).",
            "evidence_notes carries the status and caveat of every heuristic score — "
            "read it before trusting the scores.",
            "Dense windows are clamped to the requested span; "
            "dense_window.clamped_to_span says when that happened.",
        ],
    }
    if failed:
        pack["dense_failures"] = failed

    # compact by default: this text is prompt input, and indentation costs ~40%
    pack_json = json.dumps(pack, separators=(",", ":"), default=str)
    json_style = "compact (separators ',' ':') — indentation would cost ~40% of the budget"
    if args.pretty:
        pack_json = json.dumps(pack, indent=1, default=str)
        json_style = "indent=1 (--pretty, for human reading; may exceed the budget)"
    over_budget = len(pack_json.encode()) > PACK_BUDGET_BYTES
    pack_path = os.path.join(pkg, "evidence_pack.json")
    with open(pack_path, "w") as f:
        f.write(pack_json + "\n")

    # ---------------------------------------------------------------- prompt
    prompt = build_prompt(span_start, span_end, len(sheets), pack_events, policy,
                          args.package_name)
    prompt_path = os.path.join(pkg, "prompt.md")
    with open(prompt_path, "w") as f:
        f.write(prompt)

    # ----------------------------------------------------------------- index
    purpose_map = {
        "evidence_pack.json": "evidence text for the prompt (model input)",
        "prompt.md": "prompt template (model input)",
        "reference/README.md": ("placeholder — awaits the user-supplied reference edit "
                                "(nothing auto-populated)"),
    }
    purpose_map.update(cand_purposes)
    for s in sheets:
        purpose_map[s["file"]] = ("contact sheet (model input): %.1f s of %s-CAM at 1 "
                                  "frame / %.0f s, tiles with burned timecodes"
                                  % (s["span"][1] - s["span"][0], s["angle"],
                                     s["interval_s"]))
    for ev in events:
        for f in (ev.get("_dense") or []):
            purpose_map[f["file"]] = ("dense frame (%s%s, %.1f fps, ±%.1f s window) — "
                                      "model input" % (ev["event_id"],
                                                       " " + f.get("angle") if f.get("angle") else "",
                                                       args.dense_fps,
                                                       args.dense_window / 2.0))
    files = []
    for root, dirs, fnames in os.walk(pkg):
        dirs.sort()
        for fn in sorted(fnames):
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, pkg)
            if rel == "package_index.json":
                continue
            files.append({"path": rel, "bytes": os.path.getsize(p),
                          "sha256": sha256_file(p),
                          "purpose": purpose_map.get(rel, "package artifact"),
                          "transmitted": False})
    files.sort(key=lambda f: f["path"])
    total_bytes = sum(f["bytes"] for f in files)
    tok_sheets = len(sheets) * TOKENS_PER_SHEET
    tok_dense = n_dense_ok * TOKENS_PER_DENSE_FRAME
    index = {
        "tool": TOOL, "tool_version": TOOL_VERSION, "prompt_version": PROMPT_VERSION,
        "built_at_utc": args.built_at,
        "package_root": os.path.abspath(pkg),
        "package_name": args.package_name,
        "span": {"source_start": trunc_ms(span_start), "source_end": trunc_ms(span_end),
                 "start_tc": fmt_tc(span_start), "end_tc": fmt_tc(span_end),
                 "reason_for_selection": pack["span"]["reason_for_selection"]},
        "transmitted": False,
        "network_calls": [],
        "network_note": ("openai_package.py contains no network code (no http/socket/"
                         "urllib import, no curl, no provider SDK). Nothing in this "
                         "package has left the lab."),
        "builder_cmd": shell_join([sys.executable] + sys.argv),
        "files": files,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "local_only_files": [f for f in files if f["transmitted"] is False
                             and f["purpose"] == LOCAL_ONLY_PURPOSE],
        "self_entry": {
            "path": "package_index.json",
            "bytes": None, "sha256": None, "transmitted": False,
            "purpose": ("audit record of the package; not self-hashed (writing its own "
                        "hash would change the file). Its sha256 is printed by the "
                        "builder as INDEX_SHA256 at the end of the run."),
        },
        "aggregate_policy_block": {
            "model_visible_evidence_contains_aggregates": False,
            "withheld_field_names_per_candidate": list(DENY_CANDIDATE_KEYS),
            "withheld_field_names_per_event": list(DENY_EVENT_KEYS),
            "stripped_from_model_visible_files": (stripped_keys or
                                                  ["(nothing: no --candidates)"]),
            "local_only_file": local_rel,
            "local_only_purpose": LOCAL_ONLY_PURPOSE,
            "note": ("These field names are recorded here (an audit record, not model "
                     "input) and in the local-only file, and nowhere else. The "
                     "model-visible evidence files carry the eight individual scores "
                     "and no aggregate."),
        },
        "sources": [{"name": s["name"], "role": s["role"], "path": s["path"],
                     "duration_s": s["duration_s"], "size_bytes": s["size_bytes"],
                     "fingerprint_sha256": s["fingerprint_sha256"],
                     "hash_method": HASH_METHOD} for s in sources],
        "b_virtual_reel": b_reel,
        "sheet_commands": [c for c in cmds if "tile=" in c],
        "ffmpeg_call_count": len(cmds),
        "images_reused_from_previous_run": n_reused,
        "commands_echoed_to_stdout": bool(args.echo_cmds),
        "image_geometry": {
            "sheet": {"tile": [args.tile_w, tile_h], "grid": [cols, rows],
                      "geometry": sheets[0]["geometry"] if sheets else None,
                      "long_edge_px": sheets[0]["long_edge_px"] if sheets else None,
                      "jpeg_q": JPEG_Q},
            "dense": {"wh": [args.dense_w, args.dense_h], "jpeg_q": JPEG_Q},
        },
        "token_estimate": {
            "tokens_per_sheet": TOKENS_PER_SHEET,
            "tokens_per_dense_frame": TOKENS_PER_DENSE_FRAME,
            "n_sheets": len(sheets), "n_dense_frames": n_dense_ok,
            "sheets_tokens": tok_sheets, "dense_tokens": tok_dense,
            "total_image_tokens": tok_sheets + tok_dense,
            "arithmetic": "%d sheets x %d + %d dense frames x %d = %d + %d = %d image tokens"
                          % (len(sheets), TOKENS_PER_SHEET, n_dense_ok,
                             TOKENS_PER_DENSE_FRAME, tok_sheets, tok_dense,
                             tok_sheets + tok_dense),
        },
        "evidence_pack": {"bytes": os.path.getsize(pack_path), "json_style": json_style,
                          "budget_bytes": PACK_BUDGET_BYTES,
                          "within_budget": not over_budget},
        "counts": pack["counts"],
    }
    idx_path = os.path.join(pkg, "package_index.json")
    # write until the self-entry's byte count stops changing (it converges in 1-2 passes)
    for _ in range(4):
        with open(idx_path, "w") as f:
            json.dump(index, f, indent=1, default=str)
        n = os.path.getsize(idx_path)
        if index["self_entry"]["bytes"] == n:
            break
        index["self_entry"]["bytes"] = n
    idx_sha = sha256_file(idx_path)

    print("PACKAGE %s" % pkg)
    print("  sheets      %d (%d frames)  overview/" % (len(sheets),
                                                       sum(s["n_frames"] for s in sheets)))
    print("  events      %d  (%s), dirs events/<id>/" % (len(pack_events), events_src))
    print("  dense       %d ok / %d requested%s"
          % (n_dense_ok, n_dense_all, "  FAILED %d" % len(failed) if failed else ""))
    print("  images      %d   package total %d bytes" % (len(sheets) + n_dense_ok,
                                                         total_bytes))
    print("  evidence    %d bytes (%s, budget %d)"
          % (index["evidence_pack"]["bytes"], json_style, PACK_BUDGET_BYTES))
    print("  aggregates  withheld from model-visible files: %s"
          % (", ".join(stripped_keys) if stripped_keys else "(none)"))
    print("  tokens      %s" % index["token_estimate"]["arithmetic"])
    print("  transmitted false  (no network code in %s)" % TOOL)
    print("  INDEX_SHA256 %s" % idx_sha)
    return 0


def event_anchor(ev):
    """The musical boundary this candidate event is built around -> (t, how, alts).

    Priority: the event's own context.beat (the boundary it exists for), then the
    earliest SWITCH candidate's timeline_start, then context.b_slot[0], then the
    first candidate's timeline_start. All alternatives are reported so the choice
    is auditable.
    """
    ctx = ev.get("context") or {}
    cands = ev.get("candidates") or []
    sw = [c for c in cands if str(c.get("action", "")).upper() == "SWITCH"]
    alts = {
        "context_beat": ctx.get("beat"),
        "min_switch_timeline_start": (min(float(c["timeline_start"]) for c in sw)
                                      if sw else None),
        "b_slot": ctx.get("b_slot"),
        "first_candidate_timeline_start": (cands[0].get("timeline_start")
                                           if cands else None),
    }
    if ctx.get("beat") is not None:
        return float(ctx["beat"]), "context.beat (the boundary this event exists for)", alts
    if alts["min_switch_timeline_start"] is not None:
        return alts["min_switch_timeline_start"], "min SWITCH candidate timeline_start", alts
    if ctx.get("b_slot"):
        return float(ctx["b_slot"][0]), "context.b_slot[0]", alts
    if alts["first_candidate_timeline_start"] is not None:
        return alts["first_candidate_timeline_start"], "first candidate timeline_start", alts
    return None, "none", alts


def dense_angles_for_event(ev, mode):
    """Which camera(s) the dense burst comes from for this event."""
    if mode == "both":
        return ["a", "b"]
    if mode in ("a", "b"):
        return [mode]
    cands = ev.get("candidates") or []
    sw = [c for c in cands if str(c.get("action", "")).upper() == "SWITCH"]
    if sw:
        ang = str(sw[0].get("angle", "B")).lower()
        return [ang if ang in ("a", "b") else "b"]
    if cands:
        ang = str(cands[0].get("angle", "a")).lower()
        return [ang if ang in ("a", "b") else "a"]
    return ["a"]


def main():
    ap = argparse.ArgumentParser(
        description="Build the LOCAL editorial evidence package (overview sheets + "
                    "per-event dense frames + evidence_pack.json + prompt.md + "
                    "package_index.json). No network code: nothing is transmitted.")
    ap.add_argument("--manifest", required=True, help="job manifest YAML/JSON (manifest_version 1)")
    ap.add_argument("--media-root", required=True,
                    help="directory the manifest's sources are relative to")
    ap.add_argument("--span", required=True, help="START-END source seconds, e.g. 300-600")
    ap.add_argument("--out", required=True, help="package PARENT directory (created)")
    ap.add_argument("--package-name", default="openai_test",
                    help="package directory name under --out (default openai_test)")
    ap.add_argument("--sheet-frames", type=int, default=DEFAULT_COLS * DEFAULT_ROWS,
                    help="frames per contact sheet (default 30 = 5x6)")
    ap.add_argument("--sheet-cols", type=int, default=DEFAULT_COLS)
    ap.add_argument("--sheet-rows", type=int, default=DEFAULT_ROWS)
    ap.add_argument("--sheet-interval", type=float, default=DEFAULT_SHEET_INTERVAL,
                    help="seconds between sheet frames (default 10 -> 1 frame / 10 s)")
    ap.add_argument("--sheet-angle", default="a", choices=["a", "b"],
                    help="angle the contact sheets sample (default a = the A spine)")
    ap.add_argument("--tile-w", type=int, default=DEFAULT_TILE_W,
                    help="sheet tile width px (default 320 -> sheet 1600 px long edge)")
    ap.add_argument("--dense-window", type=float, default=10.0,
                    help="seconds of dense frames centred on each event boundary (default 10)")
    ap.add_argument("--dense-fps", type=float, default=2.0, help="dense frame rate (default 2)")
    ap.add_argument("--dense-w", type=int, default=DEFAULT_DENSE_W)
    ap.add_argument("--dense-h", type=int, default=DEFAULT_DENSE_H)
    ap.add_argument("--dense-angle", default="auto", choices=["auto", "a", "b", "both"],
                    help="which camera the dense frames come from (default auto = the "
                         "event's switch angle, A for a hold-only event)")
    ap.add_argument("--max-events", type=int, default=12, help="cap on dense-window events")
    ap.add_argument("--candidates", default=None, help="candidate_score.py candidates.json")
    ap.add_argument("--phrases", default=None, help="phrase_map.py phrase JSON")
    ap.add_argument("--beats", default=None, help="beat_detect.py beat JSON")
    ap.add_argument("--policy", default=None, help="policy/template YAML (editing block)")
    ap.add_argument("--b-profile", default=None,
                    help="action_profile.py B-reel profile (used only for the segment reason)")
    ap.add_argument("--segment-reason", default=None,
                    help="override the derived reason_for_selection text")
    ap.add_argument("--workers", type=int, default=4, help="parallel ffmpeg fast seeks")
    ap.add_argument("--echo-cmds", action="store_true", default=True,
                    help="echo every ffmpeg command as CMD: (default on — audit trail)")
    ap.add_argument("--no-echo-cmds", dest="echo_cmds", action="store_false")
    ap.add_argument("--keep-frames", action="store_true", help="keep the per-tile temp frames")
    ap.add_argument("--reuse-images", action="store_true",
                    help="skip ffmpeg when the target image already exists (regenerate the "
                         "text/index without re-decoding 4K)")
    ap.add_argument("--json", action="store_true", help="print an index summary as JSON too")
    ap.add_argument("--pretty", action="store_true",
                    help="write evidence_pack.json with indent=1 (human reading; may "
                         "exceed the ~40 KB prompt budget)")
    args = ap.parse_args()

    args.built_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rc = build(args)
    if args.json:
        idx = os.path.join(os.path.abspath(args.out), args.package_name,
                           "package_index.json")
        with open(idx) as f:
            d = json.load(f)
        print(json.dumps({"files": d["file_count"], "total_bytes": d["total_bytes"],
                          "transmitted": d["transmitted"],
                          "token_estimate": d["token_estimate"],
                          "evidence_pack": d["evidence_pack"]}, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
