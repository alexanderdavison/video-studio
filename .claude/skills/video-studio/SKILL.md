---
name: video-studio
description: "Use when the user drops footage into a video-studio project and says 'edit this' — the conductor for the AI video editor."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [video, editing, ffmpeg, hyperframes, video-use, transcript]
---

# Video Studio Orchestrator

The conductor for the AI video-editing studio in /opt/video-studio on the
sandbox (LXC davison-sandbox .28, Debian 12). When the user drops footage in a
project's raw/ and says "edit this", run this loop.

## Access

- Host: .11 (pve-m920-01) → `pct exec 204 -- <cmd>`
- Studio root: /opt/video-studio
- PATH: export PATH=/root/.local/bin:/root/.bun/bin:/usr/local/bin:$PATH
- video-use helpers: /opt/video-studio/tools/video-use/helpers/
- Key: ELEVENLABS_API_KEY in tools/video-use/.env (required for transcription)

## The loop

1. **Transcribe** — `python helpers/transcribe.py <raw file>` from
   tools/video-use. Requires the ElevenLabs key; if absent, say so and stop.
2. **Propose a cut** — pull filler, dead air, retakes. Show the TRANSCRIPT with
   cut spans marked + a one-line reason each. WAIT for OK. Never cut first.
3. **Execute the cut** — snap every cut to a word boundary (don't clip
   mid-word) → edit/base.mp4.
4. **Suggest graphics** — 2-3 moments that land better with a hyperframes
   overlay (title card, lower-third, beat accent). Offer to build them.
5. **Render vertical at source resolution** — 9:16, no downscale.
6. **Self-eval** — check each cut boundary; max 3 fix+re-render loops, then
   stop and show honest notes.

## Low-dialog / production footage

Transcript is thin. Cut on silence gaps, dead air, and audio events
(applause, music) instead of filler words. Audio is primary; visuals follow.

## Project layout

projects/<YYYY-MM-DD-slug>/ with raw/, edit/ (transcripts + EDL + cut),
compositions/ (hyperframes HTML), renders/.

## Pitfalls

- Never render before the user approves the proposed cut.
- Never downscale source resolution.
- Subtitles burned in LAST in the filter chain.
- helpers/ scripts are bare-name invoked from tools/video-use dir.
