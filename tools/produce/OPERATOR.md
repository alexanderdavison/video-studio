# ISH-D AI Director — Operator Guide

One command runs a full set. This document is the operator's view; the architecture lives in the
checkpoint under `/mnt/obsidian-vault/ish-d/creative/decisions/`.

## Start a job

    video-studio init myset-2026-09-13      # scaffolds /opt/video-studio/jobs/myset-2026-09-13/
    $EDITOR /opt/video-studio/jobs/myset-2026-09-13/job.yaml
    video-studio check /opt/video-studio/jobs/myset-2026-09-13/job.yaml
    video-studio run   /opt/video-studio/jobs/myset-2026-09-13/job.yaml

`run` executes the production stages in order — source verify, sync, performance analysis, window plan,
then per window: candidates, matched evidence, transport preflight, one editorial call, response
validation, continuity — then the program merge, the program-level coverage review, the review manifest,
the review render, the audio-correct review copy, QC and the operator report.

You do not run `candidate_score.py`, the evidence builder, the sender, the continuity scripts, the merge,
the renderer or QC by hand. The orchestrator owns all of it.

## Inputs

`job.yaml` names them: `sources.camera_a`, `sources.camera_b` (one or more reels), and
`sources.master_audio` (the A camera file is a valid master audio). `program.start_s` / `program.end_s`
optionally bound the set; both null means the whole A reel.

## How a window is chosen

Default target ~120 s, snapped to a nearby phrase boundary when one is within the allowed distance, and
shortened to 105 s or 90 s if the transport estimate says a window would not fit the API budget. Evidence
is always 1.0 fps at 960x540 — **resolution and frame rate are never reduced to fit**. If even the
shortest rung cannot fit, the job stops rather than degrading the evidence.

## Resume

State lives in `<job>/ledger/job_state.json`, one entry per window, moving through PENDING → BUILT →
PREFLIGHT_PASS → SENT → RESPONSE_RECEIVED → VALIDATED → COMMITTED (or FAILED).

    video-studio status <job.yaml>          # see where it stopped
    video-studio resume <job.yaml>          # continue from the last committed window

A window whose response was validated and committed is **immutable**: resume never resends it and never
pays for it twice. `resume` after a transport failure restarts only the failed window.

## Outputs

Everything for a job is under `/opt/video-studio/jobs/<job_id>/`:

    job.yaml          the contract
    analysis/         sources, sync, 1 Hz signals, activity, window_plan.json, coverage_review.json
    candidates/       one candidate artifact per window
    evidence/         one matched A/B evidence package per window
    responses/        raw + parsed model responses, and each window's policy-applied chain
    continuity/       the continuity state each subsequent window was given
    manifests/        the canonical full-set editorial manifest + basis
    renders/          review proof and the audio-correct review copy
    qc/               qc_report.json
    ledger/           job_state.json, per-window ledgers, job-level cost/token totals

Review proofs are also published to `/mnt/media/proofs/` when a job finishes.

## Modes

* **clean review proof** — what `run` produces today: the editorial program, audio-correct, no graphics.
* **branded final** — after Prompt 2. The job contract already reserves `graphics.enabled`,
  `graphics.profile` and `graphics.bug.version`, and graphics are applied to the merged program as a
  whole, never per window. Nothing burns in yet.

## Program-level coverage review

After the merge, an uninterrupted A run longer than `coverage_review_threshold_s` (trial: 90 s) is
reviewed **once** by a second, program-level pass that can only choose already-legal B opportunities
offered to it, at most `max_promotions_per_span` (2) per span. `KEEP_A_RUN` is a normal, expected answer.
Promoted inserts are marked `decision_origin = program_coverage_review` so the first pass stays
distinguishable. The first pass is never re-run.

## Costs

The report at the end of a job prints windows, editorial calls, coverage-review calls, cost, B inserts,
B seconds, longest uninterrupted A run, manifest, review proof, QC and resume state. Per-window tokens and
cost are in `<job>/ledger/`.
