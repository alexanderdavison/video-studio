#!/usr/bin/env python3
"""P2 — phrase map: musical structure above the individual beat.

Derives bar / phrase boundaries from an existing beat grid. Two modes:

  grid  (default)  nominal structure: every 4th beat is a downbeat, phrases are
                   `--bars-per-phrase` bars. Honest label: phase is ASSUMED.
  grid+kick-phase  with --audio FILE, the downbeat PHASE is detected rather than
                   assumed: decode mono audio, measure low-band energy in a short
                   window after each beat, and pick the phase (beat index mod 4)
                   whose beats carry the most energy. Crew label: phase-detected.

This is deliberately deterministic and audio-only. No vision, no model.

Usage:
  phrase_map.py BEATS.json --out phrases.json [--bars-per-phrase 8] [--audio FILE]
                           [--bpm 126.3] [--json]

Beats JSON schema (beat_detect.py): {"bpm": float, "beats": [seconds, ...]}
or {"beats": [{"time": ...}, ...]} — both accepted.
"""
import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path


def load_beats(path: Path):
    data = json.loads(path.read_text())
    raw = data.get("beats") or data.get("beat_times") or []
    beats = []
    for b in raw:
        if isinstance(b, dict):
            t = b.get("time", b.get("t"))
        else:
            t = b
        try:
            beats.append(float(t))
        except (TypeError, ValueError):
            continue
    beats = sorted(set(beats))
    bpm = data.get("bpm")
    if not bpm and len(beats) > 4:
        gaps = [b - a for a, b in zip(beats, beats[1:]) if b > a]
        gaps.sort()
        med = gaps[len(gaps) // 2]
        bpm = 60.0 / med if med else None
    return beats, (float(bpm) if bpm else None)


def decode_mono(audio: str, rate: int = 8000) -> bytes:
    """One cheap decode pass -> 16-bit mono PCM (kick band lives well under 4 kHz)."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", audio, "-ac", "1", "-ar", str(rate),
         "-f", "s16le", "-"],
        capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {out.stderr[-300:].decode(errors='replace')}")
    return out.stdout, rate


def kick_phase(beats, audio: str, window_s: float = 0.06, beats_per_bar: int = 4):
    """Pick the phase whose beats carry the most low-band energy (kick on the 1)."""
    import array
    pcm, rate = decode_mono(audio)
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n = len(samples)
    w = int(window_s * rate)
    sums = []
    for t in beats:
        i = int(t * rate)
        if i < 0 or i >= n:
            sums.append(0.0)
            continue
        seg = samples[i:min(i + w, n)]
        if not seg:
            sums.append(0.0)
            continue
        acc = 0
        for v in seg:
            acc += v * v
        sums.append((acc / len(seg)) ** 0.5)
    if not sums:
        return None, 0.0, {}
    best_phase, best_mean = None, -1.0
    phase_means = {}
    for phase in range(beats_per_bar):
        vals = [s for idx, s in enumerate(sums) if idx % beats_per_bar == phase]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        phase_means[phase] = round(mean, 2)
        if mean > best_mean:
            best_phase, best_mean = phase, mean
    overall = sum(sums) / len(sums) if sums else 0.0
    lift = (best_mean / overall) if overall else 0.0
    return best_phase, round(lift, 3), phase_means


def build_phrases(beats, beats_per_bar: int, bars_per_phrase: int, phase: int):
    """Group beats into bars and bars into phrases.

    Beat `phase` (and every beats_per_bar-th beat after it) starts a bar. Bar 0 of
    each phrase is a phrase start; the bar before it is a phrase end.
    """
    downbeats = [t for i, t in enumerate(beats) if (i - phase) % beats_per_bar == 0]
    phrases = []
    bars = []
    for bi, start in enumerate(downbeats):
        end = downbeats[bi + 1] if bi + 1 < len(downbeats) else (beats[-1] if beats else start)
        bars.append({"index": bi, "start": round(start, 3), "end": round(end, 3)})
    for pi in range(0, len(bars), bars_per_phrase):
        chunk = bars[pi:pi + bars_per_phrase]
        if not chunk:
            continue
        phrases.append({
            "id": f"phrase_{len(phrases)+1:03d}",
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
            "bars": len(chunk),
            "start_bar": chunk[0]["index"],
            "complete": len(chunk) == bars_per_phrase,
        })
    return downbeats, bars, phrases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("beats_json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--beats-per-bar", type=int, default=4)
    ap.add_argument("--bars-per-phrase", type=int, default=8,
                    help="8 bars = 32 beats is the common EDM phrase; 16 is the long form")
    ap.add_argument("--audio", default=None, help="detect downbeat phase from low-band energy")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    beats, bpm = load_beats(Path(args.beats_json))
    if not beats:
        print(f"no beats in {args.beats_json}", file=sys.stderr)
        return 2

    phase, confidence = 0, "nominal"
    phase_means = {}
    if args.audio:
        try:
            phase, lift, phase_means = kick_phase(beats, args.audio, beats_per_bar=args.beats_per_bar)
            if phase is None:
                phase, confidence = 0, "nominal (phase detection returned nothing)"
            else:
                confidence = "phase-detected" if lift >= 1.05 else \
                             f"nominal (kick lift only {lift}x — phase unreliable)"
        except Exception as e:
            phase, confidence = 0, f"nominal (phase detection failed: {e})"

    downbeats, bars, phrases = build_phrases(beats, args.beats_per_bar, args.bars_per_phrase, phase)

    doc = {
        "source_beats": str(args.beats_json),
        "source_audio": args.audio,
        "bpm": round(bpm, 3) if bpm else None,
        "beats": len(beats),
        "beats_per_bar": args.beats_per_bar,
        "bars_per_phrase": args.bars_per_phrase,
        "downbeat_phase": phase,
        "phase_method": confidence,
        "phase_energy_by_phase": phase_means,
        "bars": len(bars),
        "phrases": phrases,
        "downbeats": [round(t, 3) for t in downbeats],
        "phrase_starts": [p["start"] for p in phrases],
        "phrase_ends": [p["end"] for p in phrases],
    }
    Path(args.out).write_text(json.dumps(doc, indent=1))
    if args.json:
        print(json.dumps({k: v for k, v in doc.items() if k not in ("downbeats",)}, indent=1))
    else:
        print(f"{args.out}: {doc['beats']} beats @ {doc['bpm']} BPM -> {doc['bars']} bars, "
              f"{len(phrases)} phrases of {args.bars_per_phrase} bars "
              f"({args.beats_per_bar}/4), phase={phase}, {confidence}")
        if phrases:
            p = phrases[0]
            print(f"  first phrase: {p['start']:.2f}-{p['end']:.2f}s ({p['bars']} bars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
