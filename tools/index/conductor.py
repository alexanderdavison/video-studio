#!/usr/bin/env python3
"""
Conductor queue — durable propose/approve/execute state for the edit loop.
Rows live in the same SQLite DB as the index, so a conductor crash mid-EXECUTE
is resumable: status column is the source of truth, not a live session.

Status flow: proposed -> approved -> executing -> done | failed

Hardening (2026-08-26 pipeline directives):
  D2 — a job proposed with --project <dir> is BLOCKED (not pending) until
       <dir>/READY_TO_BAKE exists. It does NOT advance on a timer — only on
       the flag appearing (written by make_preview.sh approve as the terminal
       action of preview approval).
  D1 — `done --verify <file> --expected-dur <sec>` runs the stage-verification
       gate (moov + duration + stderr fatal patterns) BEFORE marking done.
       Riley reads the gate result, not a separate watcher signal; a stage
       that fails verification is never marked done.

Usage:
  conductor.py propose <clip_id> <edl.json> [--label ...] [--project <dir>]
  conductor.py list [--status proposed|approved|...]
  conductor.py approve <job_id>              # blocked without READY_TO_BAKE
  conductor.py start <job_id>                # also gated on the flag
  conductor.py done <job_id> [--verify <file> --expected-dur <sec>]
  conductor.py fail <job_id> "reason"
  conductor.py status <job_id>
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

DB = Path("/opt/video-studio/studio.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS edit_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  edl_json TEXT NOT NULL,
  label TEXT,
  status TEXT NOT NULL DEFAULT 'proposed',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  error TEXT,
  project_dir TEXT
);
"""


def conn():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=60000")
    c.executescript(SCHEMA)
    # migrate pre-existing DBs that lack the project_dir column (D2)
    cols = [r[1] for r in c.execute("PRAGMA table_info(edit_jobs)").fetchall()]
    if "project_dir" not in cols:
        c.execute("ALTER TABLE edit_jobs ADD COLUMN project_dir TEXT")
        c.commit()
    return c


def ready_to_bake(job):
    """Return (ok, reason). A job with a project dir is blocked until READY_TO_BAKE."""
    proj = job[8] if len(job) > 8 else None
    if not proj:
        return True, None
    flag = Path(proj) / "READY_TO_BAKE"
    if not flag.is_file():
        return False, f"BLOCKED: no READY_TO_BAKE in {proj} (preview not approved)"
    return True, None


def verify_stage(file, expected_dur=None):
    """Mirror of hardening.sh verify_stage — returns (ok, reason)."""
    p = Path(file)
    if not p.is_file():
        return False, f"FAIL: output file missing: {file}"
    # moov sits at the FRONT on faststart files and at the END otherwise —
    # scan both windows (whole file only when it's small).
    size = p.stat().st_size
    if size > 8_000_000:
        with open(p, "rb") as fh:
            head = fh.read(8_000_000)
            fh.seek(max(0, size - 8_000_000))
            tail = fh.read(8_000_000)
        if b"moov" not in head and b"moov" not in tail:
            return False, f"FAIL: no moov atom in {file}"
    else:
        if b"moov" not in p.read_bytes():
            return False, f"FAIL: no moov atom in {file}"
    dur = None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=60)
        dur = out.stdout.strip()
    except Exception as e:
        return False, f"FAIL: ffprobe error: {e}"
    if not dur or dur == "N/A":
        return False, f"FAIL: ffprobe returned no duration for {file}"
    if expected_dur is not None:
        try:
            ok = abs(float(dur) - float(expected_dur)) < 1.0
        except ValueError:
            ok = False
        if not ok:
            return False, f"FAIL: duration {dur} != expected {expected_dur} ({file})"
    log = Path(str(p).rsplit(".mp4", 1)[0] + ".stderr.log")
    if log.is_file():
        txt = log.read_text(errors="ignore")
        if re.search(r"Error reinitializing filters|Invalid duration|Conversion failed", txt):
            return False, f"FAIL: fatal pattern in stderr log {log}"
    return True, f"PASS: {file} dur={dur}" + (f" expected={expected_dur}" if expected_dur else "")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("propose")
    p.add_argument("clip_id", type=int)
    p.add_argument("edl", help="path to EDL JSON")
    p.add_argument("--label", default=None)
    p.add_argument("--project", default=None, help="project dir; job is BLOCKED until READY_TO_BAKE")

    p = sub.add_parser("list")
    p.add_argument("--status", default=None, help="filter by status")

    p = sub.add_parser("approve"); p.add_argument("job_id", type=int)
    p = sub.add_parser("start");   p.add_argument("job_id", type=int)
    p = sub.add_parser("done");    p.add_argument("job_id", type=int)
    p.add_argument("--verify", default=None, help="output file to verify before marking done (D1)")
    p.add_argument("--expected-dur", default=None, help="expected duration for --verify")
    p = sub.add_parser("fail");    p.add_argument("job_id", type=int); p.add_argument("reason")
    p = sub.add_parser("status");  p.add_argument("job_id", type=int)

    args = ap.parse_args()
    c = conn()

    if args.cmd == "propose":
        edl = json.loads(Path(args.edl).read_text())
        cur = c.execute(
            "INSERT INTO edit_jobs (clip_id, edl_json, label, status, project_dir) VALUES (?,?,?, 'proposed', ?)",
            (args.clip_id, json.dumps(edl), args.label, args.project))
        c.commit()
        job_id = cur.lastrowid
        ok, reason = ready_to_bake((None,)*8 + (args.project,))
        if not ok:
            print(f"proposed job {job_id} for clip {args.clip_id} — {reason}")
        else:
            print(f"proposed job {job_id} for clip {args.clip_id}")

    elif args.cmd == "list":
        if args.status:
            rows = c.execute("SELECT id, clip_id, label, status, created_at, project_dir FROM edit_jobs WHERE status=? ORDER BY id DESC", (args.status,)).fetchall()
        else:
            rows = c.execute("SELECT id, clip_id, label, status, created_at, project_dir FROM edit_jobs ORDER BY id DESC").fetchall()
        if not rows:
            print("no edit jobs")
        for r in rows:
            flag = ""
            if r[5] and r[3] in ("proposed", "approved"):
                ok, reason = ready_to_bake((None,)*8 + (r[5],))
                if not ok:
                    flag = "  [BLOCKED]"
            print(f"#{r[0]}  clip {r[1]}  {r[3]:9s}  {r[2] or ''}  ({r[4]}){flag}")

    elif args.cmd == "approve":
        job = c.execute("SELECT * FROM edit_jobs WHERE id=?", (args.job_id,)).fetchone()
        if not job:
            print(f"no job {args.job_id}"); sys.exit(1)
        ok, reason = ready_to_bake(job)
        if not ok:
            print(f"job {args.job_id} stays proposed — {reason}")
            sys.exit(1)
        c.execute("UPDATE edit_jobs SET status='approved', updated_at=datetime('now') WHERE id=?", (args.job_id,))
        c.commit()
        print(f"job {args.job_id} -> approved")

    elif args.cmd == "start":
        job = c.execute("SELECT * FROM edit_jobs WHERE id=?", (args.job_id,)).fetchone()
        if not job:
            print(f"no job {args.job_id}"); sys.exit(1)
        ok, reason = ready_to_bake(job)
        if not ok:
            print(f"job {args.job_id} stays {job[4]} — {reason}")
            sys.exit(1)
        c.execute("UPDATE edit_jobs SET status='executing', updated_at=datetime('now') WHERE id=?", (args.job_id,))
        c.commit()
        print(f"job {args.job_id} -> executing")

    elif args.cmd == "done":
        job = c.execute("SELECT * FROM edit_jobs WHERE id=?", (args.job_id,)).fetchone()
        if not job:
            print(f"no job {args.job_id}"); sys.exit(1)
        if args.verify:
            ok, reason = verify_stage(args.verify, args.expected_dur)
            print(reason)
            if not ok:
                c.execute("UPDATE edit_jobs SET status='failed', error=?, updated_at=datetime('now') WHERE id=?",
                          (reason, args.job_id))
                c.commit()
                print(f"job {args.job_id} -> failed (gate FAIL)")
                sys.exit(1)
        c.execute("UPDATE edit_jobs SET status='done', error=NULL, updated_at=datetime('now') WHERE id=?", (args.job_id,))
        c.commit()
        print(f"job {args.job_id} -> done")

    elif args.cmd == "fail":
        c.execute("UPDATE edit_jobs SET status='failed', error=?, updated_at=datetime('now') WHERE id=?",
                  (args.reason, args.job_id))
        c.commit()
        print(f"job {args.job_id} -> failed")

    elif args.cmd == "status":
        r = c.execute("SELECT * FROM edit_jobs WHERE id=?", (args.job_id,)).fetchone()
        if not r:
            print(f"no job {args.job_id}")
        else:
            print(f"job {r[0]}: clip {r[1]} [{r[4]}] {r[2] or ''}")
            print(f"  created {r[5]}  updated {r[6]}")
            if r[7]:
                print(f"  error: {r[7]}")
            if len(r) > 8 and r[8]:
                ok, reason = ready_to_bake(r)
                print(f"  project: {r[8]}  flag: {'OK' if ok else reason}")
            print(f"  edl: {r[3][:200]}...")

    c.close()


if __name__ == "__main__":
    main()
