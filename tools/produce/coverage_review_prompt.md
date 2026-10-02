# PROGRAM-LEVEL CAMERA COVERAGE REVIEW

You are reviewing a **completed first-pass edit of one continuous program**, not editing a window from
scratch. The first pass is already decided and delivered; your decisions there stand. This is a second,
program-level review of one specific problem: a stretch of program that stayed on the A camera for a
long time.

The span under review:

Span: **{{SPAN_START_TC}} -> {{SPAN_END_TC}}** ({{SPAN_DURATION}})

## What you are looking at

The attached evidence is **bounded** — it is not the whole program. It contains:

1. An A-camera overview contact sheet for the span, 1 frame / 10 s, timecodes burned on every tile.
   This is broad context only; it is not the per-event evidence.
2. Matched A/B dense frames for the strongest **unused legal alternate-camera opportunities** inside
   the span. Every opportunity shown is already legal: valid sync, usable non-frozen B coverage, and a
   duration inside the production range. `A-CAM` / `B-CAM` is burned top-right on every frame, and
   `a_<event>_HH-MM-SS.mmm.jpg` is A source time = performance time while `b_<event>_HH-MM-SS.mmm.jpg`
   is B source time = performance − 1.2783 s.
3. The program's own edit state around this span, as facts: how the span was entered, how long the
   program has been on A, where the last alternate-camera insert ended, and what follows.

You are NOT being asked to re-edit the span, and you are NOT being asked to reach a cut frequency.

## The evidence package (authoritative)

Everything the editor sees for this span is assembled below and transmitted verbatim: the pack's event
list, the per-event candidate sets with their measured durations and scores, and the sync mapping. Read
it as fact. Nothing outside it exists for this decision.

```json
{{PASTE_EVIDENCE_PACK_JSON_HERE}}
```

## The question

> This span remained continuously on the A camera in the first-pass edit. Review the strongest unused
> legal B opportunities inside the span. **Preserve the existing A run if it is editorially justified.**
> Otherwise select the minimum number of supporting-camera insertions necessary to keep the program
> visually alive while preserving the established restrained style.

`KEEP_A_RUN` is a fully legal answer and it is expected to be the common one. A long A run is not a
defect. Restraint remains the house style: a wide shot that keeps working does not need rescuing.

## Rules

1. Choose **only** from the candidate IDs listed below. They are the only options; nothing else may be
   requested, and no other source time may be searched.
2. Choose **at most 2** promotions for this span. This is a hard ceiling, and fewer is better.
   Choose zero if the span is editorially justified as it stands.
3. A promotion must be a genuinely useful supporting-camera moment inside the span — sustained
   hands-on-controls work, two-handed mixer/deck work, visible fader/EQ movement, transition
   preparation, a meaningful reach across the equipment, or a perspective that communicates the action
   materially better than A. Do not promote simply because time has passed.
4. Do not propose a promotion that would sit less than 8 s of A recovery away from another promotion.
5. Judge every opportunity on the footage in front of you, not on how long the span has been running.

## Candidate IDs offered for this span

{{OPPORTUNITY_TABLE}}

## Response

Answer with JSON only, no prose outside it:

```json
{
  "span_id": "{{SPAN_ID}}",
  "decision": "KEEP_A_RUN",
  "promotions": [],
  "rationale": "why the span as it stands is right, or why each promoted moment earns an insert"
}
```

For a promotion, one object per insert, in program order:

```json
{
  "span_id": "{{SPAN_ID}}",
  "decision": "PROMOTE",
  "promotions": [
    {"candidate_id": "<one of the offered IDs>", "semantic": "<b_early|b_glance|b_action|b_hold>",
     "duration_s": <number inside the production range>, "rationale": "what B shows that A cannot"}
  ],
  "rationale": "the program-level reason for this many inserts rather than fewer"
}
```
