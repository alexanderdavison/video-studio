# harden/ — Phase 1 gates (2026-08-26 master scope)

Enforced code, not lessons. Every render script sources `lib/harden.sh` and
passes through the gates. ALLDONE is ONLY written by `harden_postflight`.

## Layout
- `lib/harden.sh` — the gates (source this):
  - harden_lock_acquire / flock single-render lock (Gate 5)
  - harden_ready_flag_required (Gate 2, READY_TO_BAKE)
  - harden_verify_stage — moov + duration ±1s + stderr fatal patterns (Gate 1)
  - harden_run_captured — checked rc + retained stderr
  - harden_input_hash — sha256 of inputs
  - harden_loudness — full-program ebur128 + true peak (Gate: audio)
  - harden_bt709 — delivery-stream color tags (Gate: format)
  - harden_atomic_final — .partial → verify → rename; refuses clobber
  - harden_postflight — the ONLY writer of ALLDONE
- `lint/ffmpeg_lint.py` — pre-flight linter (Gate 3). Pattern table owned by RCC.
- `preview/preview.sh` — cheap 8s/960px preview (Gate 4)
- `tests/` — regression fixtures + runner:
  - `fixtures.sh` — tiny synthetic clips (lavfi, seconds not hours)
  - `run_tests.sh` — 15 fixtures covering every gate; exit 0 = all pass

## New footgun discovered?
Add a row to the linter pattern table + a fixture. That is how postmortem
"Lessons" sections shrink.

## Verify
    bash tests/run_tests.sh
