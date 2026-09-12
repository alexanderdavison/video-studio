# `openai_package.py` — LOCAL editorial evidence package builder

P2 of the editorial layer, beside the frozen P1 (`candidate_score.py`,
`visual_signals.py`). It builds the exact package a multimodal model *would*
eventually receive for the 5-minute DJ editorial experiment (Test 1 in
`2026-09-11-openai-hybrid-addendum-review.md` §10) and **sends nothing**. The
privacy gate has not been passed; a package that is built but not sent is the
deliverable.

**No network code.** No `http`/`socket`/`urllib` import, no `requests`, no curl,
no provider SDK, no credential. The only subprocesses are `ffmpeg` and `ffprobe`
over local/NFS file paths. Verified at source level *and* at runtime (see
"Verification" below).

Design constraint from the approved plan (§2): the API takes **images, not
video**. The model never sees continuous footage — it sees sampled frames carried
as contact sheets, plus dense bursts inside the local candidate windows.

---

## 1. Usage

```
openai_package.py --manifest JOB.yaml --media-root DIR --span START-END --out DIR
    [--package-name openai_test] [--sheet-frames 30] [--sheet-cols 5 --sheet-rows 6]
    [--sheet-interval 10] [--sheet-angle a|b] [--tile-w 320]
    [--dense-window 10 --dense-fps 2 --dense-w 960 --dense-h 540]
    [--dense-angle auto|a|b|both] [--max-events N]
    [--candidates CANDIDATES.json] [--phrases PHRASES.json] [--beats BEATS.json]
    [--policy POLICY.yaml] [--b-profile B_REEL_PROFILE.json]
    [--segment-reason "…"] [--workers 4] [--reuse-images] [--pretty]
    [--keep-frames] [--echo-cmds | --no-echo-cmds] [--json]
```

* `--out DIR` is the **parent**; the package root is `DIR/<--package-name>`
  (default `DIR/openai_test`).
* Run with `tools/venv/bin/python` (needs PyYAML; ffmpeg 5.1.9 with `drawtext`).
* Every ffmpeg command is echoed as `CMD: …` by default — the audit trail of
  exactly what was decoded.
* `--reuse-images` skips ffmpeg when a target image already exists, so the
  pack/prompt/index can be regenerated after a text-only change without
  re-decoding 4K. It never changes an already-written image.
* Exit 0 = written, 2 = fatal (bad span/manifest, or a sheet tile failed).

### What it does on the media (bounded by construction)

Every frame is **one** `ffmpeg -ss <t> -i FILE -frames:v 1`. The tool never
decodes an end-to-end span, never walks a 13 GB / 34-minute 4K HEVC file, and
never opens a file outside `--media-root` + the manifest's declared sources.
Measured on this box: ~1.4 s wall per sampled frame at `--workers 4`.

---

## 2. Package layout (exact)

```
openai_test/
  package_index.json                     audit record (see §5)
  evidence_pack.json                     the text half — paste into prompt.md
  prompt.md                              the prompt template
  reference/README.md                    placeholder: awaits the user's reference edit
  overview/sheet_NN.jpg                  timecoded contact sheets (§3)
  events/<event_id>/dense_<event_id>_HH-MM-SS.mmm.jpg     dense frames (§4)
  candidates/<event_id>.json             legal candidate set per event
  candidates/deterministic_ranking_LOCAL.json             LOCAL-ONLY (§6)
```

`--sheet-frames` must equal `--sheet-cols × --sheet-rows`; a short final sheet is
padded with flat tiles and `n_padded` says how many.

---

## 3. Contact sheets (`overview/`)

One sheet per ~5 minutes: **1 frame / 10 s**, 30 frames tiled **5×6** at
320×180 per tile, plus a 60 px provenance band, i.e. **1600×1140** (long edge
1600 ≤ the 1600 px cap). JPEG `-q:v 4` (moderate). Sheets are numbered
sequentially `sheet_01.jpg`, `sheet_02.jpg`, …

* Every tile carries its own burned-in **absolute** source timecode
  `HH:MM:SS.mmm` top-left plus an `A-CAM`/`B-CAM` badge bottom-left, matching the
  proxy burn-in style. The timecode is rendered from a literal Python-formatted
  string (not ffmpeg's `pts`), so the burn-in and the pack index cannot drift.
* Tile times are snapped to the source's real frame grid
  (`n = round(t·fps_num/fps_den)`) and **truncated** to ms, so the burned value is
  never later than the frame it came from. Tiles run row-major.
* The band burns `SHEET k/N`, the source file, the frame index range, the
  sampling cadence and the requested span.
* `evidence_pack.json → sheets[i].frames` is the authoritative
  `[tile_index, burned_t_snapped, burned_timecode]` index; `frame_times_requested`
  keeps the un-snapped 10 s ticks.

## 4. Dense frames (`events/<event_id>/`)

For every event in `--candidates` (or, with no `--candidates`, every phrase
boundary in `--phrases`): `--dense-fps` frames inside `--dense-window` seconds
centred on the event boundary, individually JPEG'd at 960×540 with the timecode
burned top-left, the event id top-right, `A-CAM`/`B-CAM` bottom-left and
`LOW CONF` bottom-right when the source cut is low-confidence.

* Filename = `dense_<event_id>_HH-MM-SS.mmm.jpg` — **the timecode is in the
  filename**, so the model can cite a frame without reading the image.
  With `--dense-angle both` the filename gains a `-a`/`-b` token to stay unique.
* The boundary (`anchor_t`) is the event's `context.beat` when present (the
  boundary the event exists for), else the earliest SWITCH candidate's
  `timeline_start`, else `context.b_slot[0]`. Which was used, and all the
  alternatives, are recorded per event.
* The dense window is **clamped to the requested span**; `dense_window.used` vs
  `requested` and `clamped_to_span` say so. (A boundary at the span edge
  therefore yields a half window.)
* Angle: `auto` = the event's switch angle (B for a b_early/b_downbeat event, A
  for a hold-only event); `a`/`b`/`both` override.

## 5. `package_index.json`

Every file except the index itself appears with `path`, `bytes`, `sha256`,
`purpose` and `transmitted: false`; plus the span, the reason for the segment,
per-source metadata fingerprints, the sheet commands, the ffmpeg call count, the
image geometry, the token estimate and `network_calls: []`.

* `package_index.json` lists itself under `self_entry` with `sha256: null`: it
  cannot hash itself without changing itself. The builder prints
  `INDEX_SHA256 <hex>` as the last line of every run, so the index digest is in
  the build log.
* Source identity is a **metadata fingerprint**
  `sha256(path, size, mtime_ns, duration, nb_frames, codec)`, not a content hash:
  hashing 13 GB over NFS at ~13 MB/s ≈ 17 minutes is not cheap. Recorded honestly
  as `hash_method`.

---

## 6. Hiding the aggregate from the model

The deterministic ranking is **evidence, not the answer**, and the model must not
see it. `weighted_sum`/`_w` (per candidate), `recommended_by_score` /
`recommendation_note` (per event) and any ordering that implies a winner are
stripped from every model-visible file and preserved only in
`candidates/deterministic_ranking_LOCAL.json`, whose index entry reads
`purpose: "local-only comparison — must never be sent to the model"` with
`transmitted: false`.

* The eight individual evidence scores (`sync, musical, action, quality,
  info_gain, continuity, novelty, coverage`) are kept everywhere.
* The pack lists events in **chronological** order and says so; candidate order
  within an event is the source order.
* The pack omits the per-candidate `notes` strings (they are in
  `candidates/<event_id>.json`) purely to stay inside the size budget;
  `candidates_sha256_full_set` proves what the full set was.
* `evidence_pack.json` is written **compact** (no indentation — indentation costs
  ~40% of the prompt budget). `--pretty` writes `indent=1` for humans and may
  exceed the budget.

## 7. `evidence_pack.json` contents

`span` (+ `source_start`, `source_end`, `reason_for_selection` verbatim),
`manifest` (job id, template, declared cut count, profiles), `sources` with real
probed durations, `b_virtual_reel` offsets, `sheets` with the frame→timecode
index, `events` (anchor + alternatives, candidate sets, `boundary_context_source`,
beat/phrase `context` — nearest beat, nearest downbeat, phrase index, bars into
phrase, **and `phase_method`**, so an assumed downbeat phase stays visible
downstream — the dense window and the dense filenames), `policy`
(`minimum_b_shot`, `maximum_b_shot`, `minimum_a_recovery`, `cut_grid`,
`low_confidence_action` + provenance), `evidence_notes`, `precedence_rule`,
`aggregate_policy`, `sampling`, `counts`, `notes`.

`segment_selection` is derived from local evidence (`--b-profile` for real B-reel
action statistics, phrase/downbeat counts, event kinds, and how many events would
break `minimum_a_recovery` i.e. a plausible `hold_a` situation); `--segment-reason`
overrides it. Mixer interaction is **not** claimed: there is no visual
hand/equipment signal, so the reason text lists it as an open question.

### `evidence_notes` (travels with every score)

| signal | status | caveat |
|---|---|---|
| `sync` | unverified | declarative, not measured on this media |
| `info_gain` | potentially_confounded | histogram difference is dominated by the A/B exposure difference |
| `action` | proxy | B-camera audio RMS, not visual hand/equipment interaction |
| `coverage` | unknown | unknown wherever the value represents unsampled footage |
| `phrase_phase` | nominal | downbeat phase not strongly detected |
| `quality` | degraded | keyframe backfill incomplete |

`prompt.md` instructs the model: *"Visual evidence and the editorial reference
take precedence over heuristic scores when they disagree."*

---

## 8. Verification (what was actually proven, and how)

Evidence for the reference package built on the tester media
(`--span 300-600`, A-cam sheet, 11 real candidate events):

| claim | method | result |
|---|---|---|
| timecode burn-in is legible | crop each tile's text box, binarize at >150 and Dice-match against locally re-rendered strings (same font/size/position) | expected string wins **30/30 tiles** (Dice 0.994–1.000 vs ≤0.975 for a 1-digit miss, ≤0.949 for +300 s) |
| dense filenames match their burn-in | same method on the first frame of every event | **11/11** (0.995+ vs ≤0.975) |
| candidate sets pass through unmodified | field-by-field compare vs `--candidates`, both in the pack and in `candidates/<event>.json` | **33 objects, no differences**; only `weighted_sum`/`_w` (by design) and the pack's `notes` (by design) absent |
| no aggregate reaches the model | token scan of `evidence_pack.json`, `prompt.md`, `candidates/*.json`, `reference/README.md` | **no hits** |
| index integrity | every file hashed and compared to its entry | **239/239 match**, all `transmitted: false`, all with a purpose |
| no network call | source scan **and** runtime Python audit hook (`socket.*`, `urllib`, `http`, DNS) during a real build | 22 audit events, **all** `ffmpeg`/`ffprobe` subprocess spawns; **zero** socket/DNS/HTTP events |

No OCR model exists on this box (`tesseract` absent, no local OCR weights), so
the legibility check is a **template match, not a read-back**: it proves the
expected glyph pattern is present at the expected position and beats plausible
decoy strings, including a one-digit miss. A human should eyeball one sheet
before the first real submission.

### Not verified / open

* Whether the model's editorial judgement actually beats the deterministic
  scorer (that is Test 1 itself, and needs a provider call this tool must not
  make).
* Retention terms for the account before any submission (review §8).
* The `+50 ms`/field-level precision of a *reader* of the burn-in; this
  verification only establishes template agreement.

---

## 9. Findings about P1 (reported, **not** fixed)

1. **The candidate artifact does not cover the whole test span.**
   `p1_out/candidates_300_420.json` holds 11 events between ~300.8 s and ~405 s
   because that run used `--start 300 --end 405`. A 300–600 s package therefore
   carries candidates for the first 105 s and nothing for the rest. Build the
   package for the same window the candidates were scored over, or re-run P1 with
   the wider window.
2. **`context.beat` is a boundary, not the nearest beat.** For `evt_01` the field
   is `300.784`, which the raw grid says is the nearest *downbeat* (the nearest
   beat is `300.304`). Reading the name literally ("beat") would mis-place a cut
   by ~0.5 s. The builder anchors on `context.beat` — correct for the event — and
   records the alternatives per event, but the field name invites mistakes.
3. **`recommended_by_score` is in the artifact.** Passing `--candidates` through
   verbatim would hand the model the argmax. The package strips it; P1 itself is
   fine (it labels it informational), but any consumer that echoes the file is
   one line away from leaking the answer.
4. **`notes` leak a whole-reel normalisation.** e.g. `"action profile 209
   segments, reel max 1.000"` — a global statistic, not a per-candidate one. Not
   a ranking, but it is context the model cannot verify from its frames.
5. **The dense burst shows one camera at a time.** `hold_a` candidates carry A
   source times while the b_early/b_downbeat candidates carry B reel times, so
   with `--dense-angle auto` the model judges the switch on B frames but never
   sees the A alternative it is being asked to weigh. `--dense-angle both` fixes
   this and doubles the dense frames (~+510 tokens each).
6. **P1's `policy` block and the template's `editing` block are different
   vocabularies.** The template locks `cut_grid: phrase` and
   `low_confidence_action: stay_on_a`; P1's policy carries `event_window`,
   `min_event_gap`, `novelty_cap_s`. The package records the template's values as
   the binding policy and keeps P1's numbers in the candidate artifact.

---

## 10. Cost model (what the parent reported)

`package_index.json → token_estimate` states the arithmetic the review §3 uses:
**~1700 tokens per 30-frame sheet**, **~510 tokens per 960×540 dense frame**.
It is deliberately *not* in `evidence_pack.json` (the model does not need to
know what it costs).

For the 300–600 s reference package: `1 × 1700 + 223 × 510 = 1,700 + 113,730 =
115,430` image tokens, plus the ~38.6 KB evidence pack (~10 k tokens) and the
attached prompt. At the review's prices that is cents; at the frontier model it
is still under $1.5 for the section. Candidate bursts dominate — 5 s per event at
2 fps is the knob, not the sheets.

`--max-events N` caps the dense pass (default 12) and the pack's `counts` records
the requested count, the truncated count and how many candidate events fell
outside the span.
