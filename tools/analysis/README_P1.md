# P1 — deterministic candidate scoring + visual signals

**Principle: the scores are evidence, not the edit.** `candidate_score.py` never
picks a cut. For every musical boundary it emits a *set* of legal choices with
eight 0..1 score keys each, always including the mandatory `hold_a`. The
`recommended_by_score` field is the argmax of a fixed weighted sum and is
reported **for information only** — it is not the creative answer. The editor
chooses.

Everything in P1 is deterministic, audio/vision-cheap, offline and network-free:
no model, no API, no randomness. Re-running on identical inputs produces
byte-identical JSON (verified — see "Determinism" below).

## Files

| path | what |
|---|---|
| `tools/analysis/visual_signals.py` | per-second sampled visual signals for a source |
| `tools/analysis/candidate_score.py` | candidate sets + evidence scores per boundary event |
| `tools/analysis/p1_out/` | generated artifacts (signals, candidates, logs) — regenerable |

Nothing else in the studio is modified by P1; `studio.db` is opened read-only.

---

## 1. `visual_signals.py`

```
visual_signals.py <video> --out out.json [--beat-json beats.json]
                  [--start S --end E] [--every N] [--json]
```

Extras: `--width 320` (downscale px), `--workers 3` (parallel ffmpeg fast-seeks),
`--max-samples 20000`, `--used-spans FILE`, `--resume`.

### How it samples (bounded by construction)

For each sample time it runs **one** `ffmpeg -ss <t> -i FILE -frames:v 1` fast
seek, downscales to `--width` px, converts to 8-bit grey and reads the single
raw frame. The source is therefore touched only at the sample times asked for —
a 17 GB NFS file is never walked end to end. `--start`/`--end` cap the work
hard; if `--end` is omitted the default span is 60 s, never the whole file.
Measured on this box: ~1.7 s wall per sample at `--workers 3` on 4K30 HEVC over
NFS (`ffmpeg -ss` fast seek), i.e. **a 120 s span costs ~3 minutes**, not a full
decode.

`--resume` reuses samples already present in `--out` and computes only the
missing times. The file's existing samples are KEPT (the output is the union of
old and new times, in time order), so resuming for a narrow range never shrinks
the artifact. `hist_novelty` is always recomputed from the stored 16-bin
histograms, so for the **same requested range** a resumed run reproduces the
fresh file byte-for-byte (verified: `848c0959…` both ways). `motion` for reused
samples is inherited as stored, because the frames themselves are not kept.

Top-level shape: `{source, tool, sampling:{method, every_s, requested_start,
requested_end, first_sample, last_sample, n_samples}, fps_sampled, samples,
summary}`. `sampling` records what was *asked for* (`requested_*`) separately
from what the file actually holds (`first_sample`/`last_sample`/`n_samples`),
so a resumed file is never mislabelled as covering only the resumed range.

### Samples (one per `--every` seconds, default 1 → `fps_sampled = 1.0`)

```json
{"t": 300.0,
 "motion": 0.0062,        // mean |frame - prev frame| / 255  (1.0 = full swing)
 "brightness": 5.389,     // mean grey 0..255
 "sharpness": 140.337,    // variance of the 4-neighbour Laplacian (blur proxy)
 "hist": [ ...16 floats, sums to 1... ],
 "hist_novelty": 0.0031,  // 1 - histogram intersection vs previous sample
 "obstruction": 0.0,      // 0..1 unusable severity (see calibration)
 "black_sev": 0.55,       // raw components, so you can re-threshold
 "white_sev": 0.0,
 "detail": 1.0,
 "frozen": false,         // motion < 0.002 for >= 3 consecutive samples
 "used": false}           // inside an optional --used-spans range
```

### Summary

```json
"summary": {
 "n_samples": 121, "t_start": 300.0, "t_end": 420.0, "span_s": 120.0,
 "width_px": 320, "fps_sampled": 1.0, "error_samples": 0,
 "motion":     {"mean":…, "p10":…, "p90":…, "max":…},
 "brightness": {"mean":…, "p10":…, "p90":…, "min":…, "max":…},
 "sharpness":  {"mean":…, "p10":…, "p50":…, "p90":…},
 "hist_novelty": {"mean":…, "p90":…},
 "flat":       {"count": 0, "detail_floor": 30.0},
 "obstruction":{"count": 0, "frac": 0.0},
 "frozen":     {"count": 0, "frac": 0.0},
 "clean_frac": 1.0, "used_frac": 0.0,
 "thresholds": {"black_level":12.0,"white_level":245.0,"detail_floor":30.0,
                "obstruction_flag":0.5,"frozen_motion":0.002,"frozen_run":3},
 "beats": {"source":…, "bpm":…, "n_beats_total":…, "n_beats_in_span":…,
           "mean_dist_to_nearest_beat":…, "aligned_frac_50ms":…}   // if --beat-json
}
```

### Obstruction calibration (important, measured)

`obstruction` is **near-black or near-white AND flat**:

```
near_black = clamp((12 - brightness) / 12)
near_white = clamp((brightness - 245) / 10)
detail     = clamp(sharpness / 30)
obstruction = max(near_black, near_white) * (1 - detail)
```

A first pass used a plain absolute darkness threshold and flagged 100 % of the
tester's B-reel (mean grey ≈ 5.4) as unusable. Extracting those frames and
looking at them showed a genuinely dark-but-legible club shot of the DJ at the
console (Laplacian variance ≈ 130, i.e. real detail). The detail discount is the
fix: a **dark shot with detail is footage; a capped/covered lens (dark AND flat)
is not**. Evidence on a synthetic clip (`color=black` / `color=white` /
`testsrc2`, 15 samples):

```
t     motion  bright  sharp  obstruction  frozen
  0.0  0.0000     0.0     0.0       1.000  True     <- black + flat  => unusable
  4.0  0.0000     0.0     0.0       1.000  True
  5.0  1.0000   255.0     0.0       1.000  False    <- blown white    => unusable
  9.0  0.0000   255.0     0.0       1.000  True
 10.0  0.5215   122.0  1326.0       0.000  False    <- detailed      => clean
 14.0  0.0410   122.7  1489.7       0.000  False
obstruction {"count": 10, "frac": 0.6667} frozen {"count": 9, "frac": 0.6} clean_frac 0.3333
```

Raw components (`black_sev`, `white_sev`, `detail`) ride along in every sample,
so a different creative threshold is a re-read, not a re-run.

---

## 2. `candidate_score.py`

```
candidate_score.py --beats BEATS.json --action-profile P.json --signals S.json
    [--signals-a A.json] [--phrases P.json]
    [--min-b 4 --max-b 14 --min-recovery 8] [--events N]
    --out candidates.json [--json]
```

Other options: `--virtual-reel` (offset repeated `--action-profile` in list
order, one virtual B reel), `--start/--end` (event window), `--min-event-gap 8`,
`--b-search-from/--b-search-to` (where in the B virtual reel windows may be
picked), `--a-offset/--b-offset` (declared sync), `--sync-json`, `--keyframes-db`
+ `--clip-id` (read-only quality evidence), `--novelty-cap 30`.

### Events

An event is a musical boundary moment: `downbeats` from phrase_map.py inside
`[--start, --end]`, greedily thinned to `--min-event-gap` seconds, capped at
`--events`. If the phrase map has no downbeats the raw beat grid is used
(`boundary_kind` says which). Events are emitted in time order. `context` carries
`beat`, `downbeat`, `phrase_boundary`, `phrase_index`, `bars_into_phrase`
(0..bars_per_phrase-1, counted from the phrase's own `start_bar`) and `b_slot`.

**B material allocation.** The B search range `[--b-search-from, --b-search-to]`
is split into **one slot per event, in event order**, and each event takes its
highest-action window inside its own slot. That is what keeps the canonical B
source order monotonic and spreads material instead of letting the first event
grab the loudest window in the whole reel (a greedy over the whole range picks
the tail first and then runs out of range — it did, in testing). The slot is
reported per event as `context.b_slot` so the allocation is auditable.

### Candidates per event (always all three)

| id | angle | action | timeline entry | B source window |
|---|---|---|---|---|
| `b_downbeat` | B | SWITCH | exactly the boundary | action-picked window `[s0,s1]` |
| `b_early` | B | SWITCH | the **actual previous grid beat** before the boundary | the same action-anchored window shifted back by that beat, so the action peak lands ON the boundary |
| `hold_a` | **mandatory** | **HOLD** | the boundary | angle A |

The B source window is `[s0, s1]` with `min_b <= s1-s0 <= max_b` chosen from the
action profile: highest duration-weighted mean action score over a 1 s start
grid, skipping windows that overlap an already-used B range (this is what keeps
B source order monotonic). Timeline entry and source time are different things:
the window is picked wherever the action is, then placed on the boundary.

`hold_a` always carries the **same eight keys**. Its `action`, `novelty` and
`info_gain` are legitimately low (duration-weighted action is not attributed to
A; A is already on screen at the boundary; A staying A shows nothing new). Its
`continuity` is 1.0 by definition and its `quality` comes from the A window.

### The eight score keys (all 0..1, identical set for every candidate)

| key | source of evidence | formula |
|---|---|---|
| `sync` | `sync_multicam.py --json` | `clamp(conf / 5.0)`; if no artifact is supplied, **1.0** because the mix *declares* the sources synced (offsets are then reported in the notes) |
| `musical` | `phrase_map.py` output (beats, downbeats, phrase starts) | `max(0.75·(1-d_beat/0.12), 0.90·(1-d_downbeat/0.25), 1.00·(1-d_phrase/0.50))`, distances from the candidate's timeline entry, each term floored at 0 |
| `action` | `action_profile.py` segments | duration-weighted mean segment score over the B source window ÷ the reel's max segment score |
| `quality` | studio.db `keyframes` for the clip, else `visual_signals` sharpness/brightness | `0.5·min(1, mean_sharp/p90_sharp) + 0.5·(1-abs(mean_bright-128)/128)` |
| `info_gain` | the two `visual_signals` files | `1 - mean(histogram intersection)` between paired samples of the B source window and the A timeline window (0 by construction for `hold_a`) |
| `continuity` | canonical path (each event's `b_downbeat`) | `1.0` if `gap >= minimum_a_recovery` and B source order is monotonic; otherwise the measured shortfall `clamp(gap/min_recovery)`, or `min(shortfall, 0.25)` when source order breaks |
| `novelty` | canonical path | `clamp(gap / novelty_cap)` where gap = seconds since that camera was last on screen under the canonical path (`hold_a` → 0.0: A *is* on screen) |
| `coverage` | the angle's `visual_signals` window | `0.0` if any sampled frame in the window is obstruction/frozen, else the clean fraction; `0.5` when the window has no samples at all (flagged in the notes) |

Weights (fixed, documented): `sync .10, musical .15, action .20, quality .10,
info_gain .15, continuity .10, novelty .05, coverage .15`.

### Output schema

```json
{
 "tool": "candidate_score.py",
 "principle": "Scores are evidence, not the edit. …",
 "recommended_by_score_is": "informational only — not the creative answer",
 "inputs":  {"beats":…, "bpm":…, "n_beats":…, "action_profiles":[…],
             "signals_b":[…], "signals_a":[…], "signals_a_is_b_proxy":false,
             "phrases":…, "structure":{…}, "sync_json":…, "keyframes":…,
             "offsets":{"a_offset":0.0,"b_offset":0.0}},
 "policy":  {"minimum_b_shot":4.0, "maximum_b_shot":14.0,
             "minimum_a_recovery":8.0, "events_requested":…,
             "min_event_gap":…, "event_window":[…], "b_search_window":[…],
             "novelty_cap_s":30.0},
 "weights": {…8 keys…},
 "score_keys": ["sync","musical","action","quality","info_gain","continuity","novelty","coverage"],
 "events": [
   {"event_id": "evt_01",
    "context": {"beat":…, "downbeat":true, "phrase_boundary":…,
                "phrase_index":…, "bars_into_phrase":…,
                "b_slot":[…,…], "boundary_kind":"downbeat",
                "structure_source":"phrase_map.py"},
    "candidates": [
      {"id":"b_downbeat","angle":"B","action":"SWITCH",
       "start":…,"end":…,              // seconds in the ANGLE's own source
       "timeline_start":…,"timeline_end":…, "duration_s":…,
       "scores":{"sync":…,"musical":…,"action":…,"quality":…,
                 "info_gain":…,"continuity":…,"novelty":…,"coverage":…},
       "weighted_sum":…, "notes":["…"]},
      {"id":"b_early",  …},
      {"id":"hold_a","angle":"A","action":"HOLD", …}
    ],
    "recommended_by_score": "b_downbeat",
    "recommendation_note": "argmax of a fixed weighted sum … informational only"}
 ]
}
```

`start`/`end` are source seconds on the candidate's own angle reel;
`timeline_start`/`timeline_end` are the position on the A timeline. This is why
both are emitted.

Two other fields are worth knowing:

* `inputs.keyframes` is either the keyframe evidence actually used, or
  `{"usable": false, "rows_total": N, "reason": "clip N has M keyframe rows but
  no sharpness values -> quality falls back to visual_signals"}`. The fallback is
  recorded, not silent. (On this tester, `--clip-id 3` hits exactly that case.)
* the canonical path used by `continuity`/`novelty` is each event's
  `b_downbeat` window. When no in-order B window could be found in a slot, the
  candidate notes say so explicitly
  (`no in-order B window left in the search range; canonical window reorders/reuses B material`).

---

## 3. Regenerating each input artifact from real media

All commands run on the studio host as `root`, from `/opt/video-studio`.
`V=/opt/video-studio/tools/venv/bin/python`; `A` is the A camera, `B1`/`B2` the B
cameras in the order they concatenate.

```bash
V=/opt/video-studio/tools/venv/bin/python
A="/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4"
B1="/mnt/media/raw/B CAM/DJI_20260824204627_0038_D.MP4"
B2="/mnt/media/raw/B CAM/DJI_20260824211420_0039_D.MP4"
W=/opt/video-studio/projects/2026-08-25-tester/work/analysis
```

**1. Beat grid** (`beat_detect.py`, aubio; also lands in `studio.db.beats` via ingest)

```bash
$V tools/bin/beat_detect.py "$A"   --json $W/a_full_beats.json
$V tools/bin/beat_detect.py "$B1"  --json $W/b0038_beats.json --method specdiff
$V tools/bin/beat_detect.py "$B2"  --json $W/b0039_beats.json --method specdiff
```

**2. Phrase map** (bars/phrases above the beat)

```bash
$V tools/bin/phrase_map.py $W/a_full_beats.json --out $W/a_full_phrases.json \
    --bars-per-phrase 8 --audio $W/a_full.wav
```

**3. Action profile** (per-B-file; `--offset` builds the virtual reel)

```bash
$V tools/analysis/action_profile.py "$B1" --out $W/b0038_profile.json --offset 0
$V tools/analysis/action_profile.py "$B2" --out $W/b0039_profile.json --offset 1672.597
# merged single file for the virtual reel:
$V tools/analysis/action_profile.py "$B1" --out $W/b_reel_profile.json --offset 0
```

(or pass both files to `candidate_score.py --action-profile A --action-profile B
--virtual-reel` and let it accumulate the offsets.)

**4. Sync confidence** (optional; otherwise `sync` = 1.0 "declared synced")

```bash
$V tools/bin/sync_multicam.py "$A" "$B1" "$B2" --json $W/sync.json
```

**5. Keyframe quality evidence** (read-only, already ingested)

```bash
$V tools/index/ingest.py --help     # ingest / backfill keyframes into studio.db
# candidate_score reads it read-only:
#   --keyframes-db /opt/video-studio/studio.db --clip-id <B clip id>
```

**6. Visual signals** (bounded spans; never a full decode)

```bash
$V tools/analysis/visual_signals.py "$B1" --out tools/analysis/p1_out/signals_b_300_420.json \
    --beat-json $W/b_reel_beats.json --start 300 --end 420 --every 1 --workers 3
$V tools/analysis/visual_signals.py "$A"  --out tools/analysis/p1_out/signals_a_300_420.json \
    --beat-json $W/a_full_beats.json  --start 300 --end 420 --every 1 --workers 3
```

(~3 minutes each at `--workers 3` on 4K over NFS. `--resume` continues an
interrupted run; a wider `--end` appends.)

**7. Candidates**

```bash
$V tools/analysis/candidate_score.py \
    --beats $W/a_full_beats.json \
    --action-profile $W/b_reel_profile.json \
    --signals tools/analysis/p1_out/signals_b_300_420.json \
    --signals-a tools/analysis/p1_out/signals_a_300_420.json \
    --phrases $W/a_full_phrases.json \
    --min-b 4 --max-b 14 --min-recovery 8 --events 12 \
    --start 300 --end 405 --min-event-gap 9 \
    --b-search-from 300 --b-search-to 420 \
    --a-offset 0 --b-offset 0 \
    --keyframes-db /opt/video-studio/studio.db --clip-id 3 \
    --out tools/analysis/p1_out/candidates_300_420.json
```

Multi-file B reels: pass `--action-profile b0038.json --action-profile b0039.json
--virtual-reel` and let the tool accumulate the source offsets, instead of
pre-merging `b_reel_profile.json`.

## 4. Determinism

Measured on the tester inputs (11 events, `--events 12`):

```
$ candidate_score.py … --out p1_out/det_run1.json
$ candidate_score.py … --out p1_out/det_run2.json
$ diff p1_out/det_run1.json p1_out/det_run2.json      # (no output)
$ sha256sum p1_out/det_run1.json p1_out/det_run2.json
8852cc4dbc286aa10b1972ce7dd0ca1b61b1aaca641df2d8d493e0d89e950c55  p1_out/det_run1.json
8852cc4dbc286aa10b1972ce7dd0ca1b61b1aaca641df2d8d493e0d89e950c55  p1_out/det_run2.json
```

No clock, no RNG, no set-iteration order and no dict-order dependence is used
anywhere in the scoring path; all scores are rounded to 3 decimals; sample
chaining in `visual_signals.py` happens in strict time order regardless of which
ffmpeg process finished first.

`visual_signals.py` is deterministic the same way (same file + same
`--start/--end/--every/--width` → same JSON), and `--resume` is idempotent for a
fixed requested range:

```
$ visual_signals.py … --out t.json --start 300 --end 303 --every 1     # fresh, todo=4
$ visual_signals.py … --out t.json --start 300 --end 303 --every 1 --resume   # todo=0, reused=4
848c0959067701ce1ddc84dedd677bd4297dab3c44a48ba7df1ece725f1a29bf  t.json   # both runs
```

## 5. Limits — what these scores cannot know

* **No semantics.** Neither tool knows what is on screen. `motion`,
  `brightness`, `sharpness` and the 16-bin histogram cannot tell a DJ's hands
  from a speaker stack, a face from a wall, or a good moment from a boring one.
  Visual "interest" is not measured — only change and technical usability.
* **`action` is audio RMS, not action.** `action_profile.py` scores B-reel *audio
  energy*. A loud but static shot scores high; a silent but visually great shot
  scores low. It is a proxy, and B audio is muted in the delivery anyway.
* **`quality` is sharpness/brightness, not taste.** Blur and exposure are
  proxies. On this tester, `studio.db.keyframes` has **no usable sharpness for
  the DJI clips** (clip 3/4 rows carry `sharpness = NULL`), so `quality` falls
  back to the sampled-frame sharpness; that is a 1 fps 320 px sample, not a full
  frame QC.
* **`info_gain` is histogram distance, and it is confounded by exposure.** Two
  different edits of the same framing read as similar, and — worse — two
  *completely different* framings read as different mainly because their exposure
  differs. Measured on the tester at 300–420 s: the B cam sits at mean grey 5.4
  and the A cam at 37.7, and every B candidate scores `info_gain` ≈ 0.74–0.76
  with a B-vs-A histogram similarity of only ≈ 0.25. That number is driven by the
  B cam being a much darker shot, not by B showing a different subject. Read it
  as "materially different to a histogram", never as "adds information".
* **The tester B reel is nearly static in this span.** Sampled motion over
  300–420 s on B is mean 0.0035 (0.9 grey levels) and `hist_novelty` mean 0.0021 —
  a locked-off tight shot. On that material `motion`/`hist_novelty` carry almost
  no discrimination; they are not "action" measures and never were.
* **`musical` trusts the grid.** It measures distance to whatever boundary set
  it is given. `phrase_map.py` labels the tester phrase map
  `nominal (kick lift only 1.021x — phase unreliable)`: on this material the
  4/4 downbeat **phase is assumed, not detected**. A wrong phase makes
  `downbeat`/`phrase_boundary` confidently wrong.
* **`sync` is a declaration unless an artifact is passed.** When no
  `--sync-json` is supplied the score is 1.0 because the mix claims alignment.
  It is not evidence and must not be read as "verified sync".
* **`continuity` is measured against a reference path** (every event's
  `b_downbeat`), not against the edit the editor will actually make. Once a
  different choice is taken, `continuity` is a stale prediction.
* **`novelty` saturates at `--novelty-cap`** and only counts canonical-path
  screen time; it does not model fatigue from similar-looking B material.
* **Coverage only covers what was sampled.** With a 120 s span, windows outside
  it get `coverage = 0.5` with a note — that is "unknown", not "clean".
* **No meaning is assigned to the weights.** They are one defensible ranking for
  `recommended_by_score`, nothing more. Changing them changes the ranking, not
  the evidence.
