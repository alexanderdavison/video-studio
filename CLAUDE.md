# Video Studio — House Rules (CLAUDE.md equivalent)

Personal AI video-editing studio, orchestrated by Hermes (or Claude Code).
Located at /opt/video-studio on the sandbox LXC (davison-sandbox, .28).

## Stack

- tools/video-use — transcript-driven cutter (Python, uv sync'd)
- tools/hyperframes — HTML/CSS to MP4 motion graphics (Node 22 + bun)
- ffmpeg 5.1 — all cutting/rendering

## House Rules

1. **Output is 9:16 vertical, PRESERVE source resolution.** Never downscale.
   1080x1920 is the floor, used only for quick previews.
2. **Editing happens on the TRANSCRIPT first, not the timeline.** Transcribe,
   propose a cut in plain English, get confirmation, then execute. Never render
   before approval.
3. **Subtitles: short chunks, burned in LAST in the filter chain.**
4. **Every project lives in projects/<YYYY-MM-DD-slug>/** with:
   - raw/ (source footage)
   - edit/ (transcripts + EDL + cut)
   - compositions/ (hyperframes HTML)
   - renders/
5. **Low-dialog / production footage:** the transcript is thin. Cut on silence
   gaps, dead air, and audio events (applause, music) — audio is primary,
   visuals follow. Do not require dialog to make a cut.
6. **Self-eval at every cut boundary** — max 3 fix+re-render loops, then stop
   and show the result with honest notes.
7. **ElevenLabs key required for transcription** — ELEVENLABS_API_KEY in
   tools/video-use/.env. Without it, transcription is unavailable; say so
   plainly rather than pretending.

## Quickstart

Drop raw footage into a project's raw/ and say "edit this". The orchestrator
skill (video-studio) drives the loop.
