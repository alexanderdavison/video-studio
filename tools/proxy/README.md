# Visual Approval Proxy (Phase 3)

`render_proxy.py` — the FIRST consumer of the Phase 2 manifest. Renders a
LOW-RES VAAPI proxy with burned-in review markers so Ish can approve cuts
before the expensive final render. No throwaway proxy workflow: the final
renderer (Phase 4) consumes the SAME manifest.

## What the proxy shows

- **Timeline timecode** (TC, top-left) — absolute mix time, HH:MM:SS.mmm
- **Cut id** (CUT 018, top-right) — 1:1 with manifest `cut_018`
- **Camera label** (A-CAM / B-CAM, bottom-left) — which source is on screen
- **LOW CONF** (bottom-right) — burned only when a cut has `confidence: low`
- **Real continuous A audio** — the full A bed 0..total, B fully muted
- **The locked grade + A push-in** — same creative params the final uses
  (push-in applied at proxy res; exact final chain is the renderer's job)

## Gate order (mirrors Phase 1 hardening)

1. Manifest validated by `validate_manifest.py --media-root --json`
   (rc 1 = invalid, proxy refuses to render).
2. Single-render flock on `/opt/video-studio/.render.lock` — one render at a
   time on the 4-vCPU studio. Locked out → rc 2 with a clear message.
3. Per-segment VAAPI encode, uniform: `fps=30000/1001,settb=AVTB,
   setpts=PTS-STARTPTS,format=yuv420p` (the runbook uniform rule). Segments
   are then concatenated with `-c copy` — safe because every segment is
   encoded with identical VAAPI params (never the mixed-format concat that
   crashed).
4. A audio bed: a_reel audio 0..total (apad if the reel is short), AAC 192k.
5. Verify: ffprobe duration within ±1.5s of expected total, moov present,
   video + audio streams. Any failure → rc 2, no silent output.

## Usage

```bash
/opt/video-studio/tools/venv/bin/python \
  /opt/video-studio/tools/proxy/render_proxy.py job.yaml \
  --media-root /path/to/mix \
  [--out /mnt/media/proofs/<job_id>_proof.mp4] \
  [--res 960x540] [--quality 30] [--work /opt/video-studio/work/proxy] \
  [--keep-temp] [--json]
```

Exit: 0 = rendered + verified; 1 = manifest invalid; 2 = error/lock.

## Tests

`/opt/video-studio/tools/tests/run_proxy_tests.sh` — 7 checks (valid render,
invalid manifest, cross-file B span, A-Cam-Only, confidence marker, missing
source, lock). Uses the Phase 1 media fixtures.

## Known limits (by design)

- Proxy is 960x540 VAAPI — good enough to judge cuts, NOT delivery quality.
- Cross-file B spans are handled: the proxy splits them across the virtual
  reel (same resolution the Phase 4 renderer will use).
- The audio bed is the real A audio at 192k — the final's loudnorm/TP chain
  is a Phase 4 (delivery) concern, not a proxy concern.
