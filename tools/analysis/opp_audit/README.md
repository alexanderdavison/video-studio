# opp_audit — candidate opportunity audit, ISH D 1020–1380 s (2026-09-13)

Audit only. No OpenAI call, no render, no candidate regeneration, no change to the
event grid, P1, the sync mapping, duration policy, references, house style or the prompt.

Full write-up: vault `ish-d/creative/decisions/2026-09-13-candidate-opportunity-audit.md`.
Summary + result: `../../../../../obsidian-vault/ish-d/creative/decisions/2026-09-11-video-editor-checkpoint.md`
(checkpoint, "6:12 HUMAN REVIEW" and "CANDIDATE OPPORTUNITY AUDIT" sections).

## Inputs (hash-verified against the canonical artifacts)

| file | sha256 (first 8) | note |
|---|---|---|
| `sync_1020_1200.json` | `b32d3d35` | canonical Test 5 candidate artifact |
| `sync_1200_1380.json` | `240a343b` | canonical Test 6 candidate artifact |
| `signals_b_1020_1200.json` | `ec795874` | generator's own 1 Hz B signals (validation only) |
| `signals_b_1198_1395.json` | `f67e5f47` | ditto |

## Frame extraction (reproduce the measurement arrays)

```
# primary: 16-bit grey, 2 fps, 480x270, source 1016.0 -> 1384.0 s (736 frames)
ffmpeg -hide_banner -v error -ss 1016 -i "/mnt/media/raw/B CAM/DJI_20260824204627_0038_D.MP4" \
  -t 368 -vf "fps=2,scale=480:270,format=gray16le" -f rawvideo -pix_fmt gray16le b_gray16.raw

# control: 8-bit
ffmpeg -hide_banner -v error -ss 1016 -i "/mnt/media/raw/B CAM/DJI_20260824204627_0038_D.MP4" \
  -t 368 -vf "fps=2,scale=480:270,format=gray" -f rawvideo b_gray.raw

# A camera, context only (732 frames, A source 1018.0 -> 1384.0)
ffmpeg -hide_banner -v error -ss 1018 -i "/mnt/media/raw/A CAM/DJI_20260824204625_0054_D.MP4" \
  -t 366 -vf "fps=2,scale=480:270,format=gray" -f rawvideo a_gray.raw
```

Frame *i* of `b_gray16.raw` is at B source `1016.0 + i/2` s; performance time =
source + 1.2783 (accepted mapping). Arrays are not mirrored here (91–182 MB each);
regenerate with the commands above.

## Tooling

| file | role |
|---|---|
| `detect2.py` | event-grid-blind detector: cell-grid localised activity, smoothing, peak pick + NMS, run expansion. `--metric loc|activ|artic`, `--dtype u8|u16` |
| `classify.py` | consensus clustering across metric variants + classification against the candidate grid |
| `final.py` | the delivered analysis: coverage summary, grid holes, dense scan for runs with poor coverage |
| `detail.py` | per-opportunity listing of every overlapping legal candidate with entry/exit offsets |
| `montage.py` | A/B frame-pair contact sheets for visual checks (contrast-stretched for legibility only) |
| `detect.py`, `compare.py` | first-pass versions, kept for provenance |

## Results

- `eps_u16_{loc,activ,artic}.json` — per-metric opportunity sets (primary)
- `eps_u8_*.json` — 8-bit control
- `final.json` — **the delivered numbers**: 11 opportunities, 5 COVERED / 6 PARTIALLY
  COVERED / 0 MISSED, coverage 45.5 % strict / 72.7 % weighted, 9 grid holes totalling
  34.00 s (9.4 %), the poor-coverage runs, and the declared classification rule
- `eps_u16_series_artic.json` — the activity series itself
- contact sheets: `opp_peaks_AB.png`, `opp_peaks_AB2.png`, `hole_1252_1259_B.png`,
  `probe_A_top_B_bottom.png`

## Verdict

**RESULT A — the candidate layer is the bottleneck, and the defect is timing, not
content.** A B insert can only begin on the event-anchor grid (anchors ≈ 9.66 s apart);
useful B moments arrive between anchors. 6 of 11 strongest opportunities have no
candidate that both enters within ±2 s of the visible action and runs to the end of it,
and 2 sustained runs (6.0 s / 3.0 s, 79th / 68th activity percentile) have under 25 %
legal coverage because the preceding event's four faces all collapsed to the 4.0 s
minimum, leaving 2–5 s holes.

Next increment (recommended, NOT implemented): an alternate-camera action-onset event
proposal in `candidate_score.py`. The entry rule stays as accepted — the entry is derived
from the event timestamp and never searched; what moves is where the event is placed.
`maximum_b_shot` stays 14.0. Then test directly in a 3-section / ≈9-minute continuous
edit.
