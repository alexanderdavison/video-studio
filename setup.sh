#!/bin/bash
# video-studio setup.sh — idempotent: clones/updates tools, installs deps, verifies.
set -euo pipefail
export PATH=/root/.local/bin:/root/.bun/bin:/usr/local/bin:$PATH
STUDIO=/opt/video-studio
cd "$STUDIO"

echo "[1/5] tools/video-use"
if [ -d tools/video-use/.git ]; then git -C tools/video-use pull --ff-only; else git clone --depth 1 https://github.com/browser-use/video-use tools/video-use; fi
cd tools/video-use && uv sync && cd "$STUDIO"

echo "[2/5] tools/hyperframes"
if [ -d tools/hyperframes/.git ]; then git -C tools/hyperframes pull --ff-only; else git clone --depth 1 https://github.com/heygen-com/hyperframes tools/hyperframes; fi
cd tools/hyperframes && bun install && bun run build && cd "$STUDIO"

echo "[3/5] ffmpeg check"
command -v ffmpeg || { echo "ffmpeg MISSING"; exit 1; }
ffmpeg -version | head -1

echo "[4/5] ElevenLabs key"
if [ -n "${ELEVENLABS_API_KEY:-}" ]; then
  printf 'ELEVENLABS_API_KEY=%s\n' "$ELEVENLABS_API_KEY" > tools/video-use/.env
  chmod 600 tools/video-use/.env
  echo "key written from env"
elif grep -q '^ELEVENLABS_API_KEY=..' tools/video-use/.env 2>/dev/null; then
  echo "key already present"
else
  echo "WARN: no ELEVENLABS_API_KEY — transcription will be unavailable until set"
fi

echo "[5/5] project dirs"
mkdir -p projects

echo "DONE. Drop footage in projects/<date>-<slug>/raw/ and say 'edit this'."
