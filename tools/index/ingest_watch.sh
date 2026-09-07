#!/bin/bash
# Studio index watcher — incremental background indexing of /mnt/media/raw.
# Runs every 15 min; cheap stat gate skips unchanged files, hash-first gate
# prevents re-transcription. Logs to /var/log/studio-ingest.log.
LOG=/var/log/studio-ingest.log
VENV=/opt/video-studio/tools/venv/bin/python
SCRIPT=/opt/video-studio/tools/index/ingest.py

if [ -f /tmp/studio-ingest.lock ]; then
    # stale lock guard: if PID not alive, remove and proceed
    LOCKPID=$(cat /tmp/studio-ingest.lock 2>/dev/null)
    if [ -n "$LOCKPID" ] && kill -0 "$LOCKPID" 2>/dev/null; then
        exit 0  # another ingest is running
    fi
    rm -f /tmp/studio-ingest.lock
fi

echo "=== studio-ingest $(date -Is) ===" >> "$LOG"
"$VENV" "$SCRIPT" --stages probe,transcribe,embed,scenes,beats,keyframes >> "$LOG" 2>&1
echo "=== done $(date -Is) ===" >> "$LOG"
