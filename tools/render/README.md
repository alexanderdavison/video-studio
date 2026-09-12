# Final Renderer (Phase 4) — /opt/video-studio/tools/render/render_final.py

The deterministic final renderer: compiles a validated Phase 2 manifest into
the LOCKED delivery format. Never hand-written ffmpeg by an agent — the
renderer owns filtergraph, labels, paths, temp files and process control.

## Delivery contract (unchanged from the runbook)

1080p30 h264 yuv420p +faststart · AAC 320k/48k · BT.709 tags · PB3 grade ·
A push-in crop · cd_bug_v9 overlay (152x152 @ 72:904) · cd_youtube_v1 audio
chain (loudnorm -13.5 -> alimiter=0.60:level=false -> +0.55dB).

## Stage order (each maps to a Phase 1 gate)

1. `validate_manifest.py --media-root --json` — rc 1 = refuse before work.
2. flock `/opt/video-studio/.render.lock` — one render at a time.
3. Video segments: libx264 crf 20 preset fast, 1080p30, uniform
   (`fps=30000/1001,settb=AVTB,setpts=PTS-STARTPTS`), `format=yuv420p` on
   EVERY segment (the yuv444p concat-crash lesson), A gets push-in crop,
   B gets grade only.
4. Concat `-c copy` (uniform segments -> safe).
5. A audio bed 0..total (apad if short) -> two-pass loudnorm (measure, then
   apply measured offsets) -> alimiter -> +0.55dB -> AAC 320k.
6. Graphics overlay + final mux, `-t total` (never -shortest with
   `-stream_loop -1`), `+faststart`.
7. `postflight_verify` (hardening.sh) — moov, faststart, duration ±1s,
   streams, yuv420p, decode, BT.709, full-program LUFS ±0.3, TP ≤ -1.5 —
   then `publish_partial` (never clobbers a `.verified` final without
   `--force`; ALLDONE written only by postflight).
8. QC record appended to the completed job manifest (`postflight:` block).

## Usage

```bash
/opt/video-studio/tools/venv/bin/python \
  /opt/video-studio/tools/render/render_final.py job.yaml \
  --media-root /path/to/mix \
  [--out /mnt/media/finals/<job_id>.mp4] \
  [--work /opt/video-studio/work/final] \
  [--graphics-root /opt/video-studio/assets/graphics] \
  [--crf 20] [--force] [--json]
```

Exit: 0 = delivered + verified; 1 = manifest invalid; 2 = error/lock/
clobber-refused.

## Graphics assets

`--graphics-root/<profile>/` holds the overlay source: `%04d.png` sequence
(preferred, cd_bug_v9) or a single `overlay.png` (test fixture). The registry
name locks WHICH bug; crop/placement are renderer-owned constants.

## Tests

`/opt/video-studio/tools/tests/run_render_tests.sh` — 8 checks (R01-R08).
Manifests are copied to work/ before rendering so the postflight append never
mutates the shared fixtures.

## Known limits

- ~30 min for a 33-min weave at crf 20 on the 4-vCPU studio — this is the
  expected cost of the delivery render, not a bug. The proxy exists so you
  only pay it once per approved manifest.
- Two-pass loudnorm on a very short/quiet A bed can report odd measured
  values; the postflight LUFS gate on the DELIVERED file is the authority.
