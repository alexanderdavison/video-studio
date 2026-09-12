#!/usr/bin/env python3
"""Backfill scenes + keyframe quality metadata for already-indexed clips.

Why this exists: the scene stage silently wrote zero rows and the keyframe stage
stored ts=0.0 with NULL sharpness/brightness/motion (both fixed 2026-09-11 in
ingest.py / pick_keyframes.py). Clips indexed before the fix keep their bad rows
until the stages are re-run, and the ingest stat gate skips unchanged files — so a
normal cron pass will never repair them.

Runs one clip at a time, taking the ingest lock per clip (so the 15-minute ingest
cron can interleave between clips rather than waiting for the whole job).
Idempotent: each stage deletes that clip's rows before re-inserting.

Usage: backfill_analysis.py [--clips 1,3,4] [--stages scenes,keyframes]
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/video-studio/tools/index")
import ingest  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", default=None, help="comma list of clip ids (default: all)")
    ap.add_argument("--stages", default="scenes,keyframes")
    args = ap.parse_args()
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]

    conn = sqlite3.connect("/opt/video-studio/studio.db", timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    q = "SELECT id, path, camera_source, duration FROM clips ORDER BY id"
    clips = conn.execute(q).fetchall()
    if args.clips:
        want = {int(x) for x in args.clips.split(",")}
        clips = [c for c in clips if c[0] in want]
    print(f"[backfill] {len(clips)} clip(s), stages={stages}", flush=True)

    failures = 0
    for cid, path, cam, dur in clips:
        if not Path(path).exists():
            print(f"[backfill] clip {cid}: MISSING {path}", flush=True)
            failures += 1
            continue
        size_gb = Path(path).stat().st_size / 1e9
        if not ingest.acquire_lock():
            print(f"[backfill] clip {cid}: could not take ingest lock, skipping", flush=True)
            failures += 1
            continue
        t0 = time.time()
        try:
            if "scenes" in stages:
                n = ingest.run_scenedetect(
                    path, Path(f"/opt/video-studio/projects/index/scenes-{cid}"), cid, conn)
                conn.commit()
                print(f"[backfill] clip {cid} ({cam}, {size_gb:.1f} GB): "
                      f"{n} scenes in {time.time()-t0:.0f}s", flush=True)
            if "keyframes" in stages:
                k = ingest.run_keyframes(path, cid, conn)
                conn.commit()
                rows = conn.execute(
                    "SELECT ts, sharpness, brightness, motion FROM keyframes "
                    "WHERE clip_id=? ORDER BY ts", (cid,)).fetchall()
                print(f"[backfill] clip {cid}: {k} keyframes "
                      f"(ts {rows[0][0]:.1f}..{rows[-1][0]:.1f}, "
                      f"sharp {rows[0][1]:.0f}..{rows[-1][1]:.0f})", flush=True)
        except Exception as e:
            failures += 1
            print(f"[backfill] clip {cid}: FAILED {type(e).__name__}: {e}", flush=True)
        finally:
            ingest.release_lock()
        time.sleep(2)  # let the cron in between clips
    print(f"[backfill] done, {failures} failure(s)", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
