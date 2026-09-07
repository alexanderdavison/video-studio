#!/usr/bin/env python3
"""
Conductor queue — durable propose/approve/execute state for the edit loop.
Rows live in the same SQLite DB as the index, so a conductor crash mid-EXECUTE
is resumable: status column is the source of truth, not a live session.

Status flow: proposed -> approved -> executing -> done | failed
Usage:
  conductor.py propose <clip_id> <edl.json> [--label ...]   # Riley PROPOSE
  conductor.py list [--status proposed|approved|...]
  conductor.py approve <job_id>                             # Ish approves
  conductor.py start <job_id>                               # mark executing
  conductor.py done <job_id>                                # mark done
  conductor.py fail <job_id> "reason"
  conductor.py status <job_id>
"""
import argparse, json, sqlite3, sys
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
  error TEXT
);
"""

def conn():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=60000")
    c.executescript(SCHEMA)
    return c

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("propose")
    p.add_argument("clip_id", type=int)
    p.add_argument("edl", help="path to EDL JSON")
    p.add_argument("--label", default=None)

    sub.add_parser("list")

    p = sub.add_parser("approve"); p.add_argument("job_id", type=int)
    p = sub.add_parser("start");   p.add_argument("job_id", type=int)
    p = sub.add_parser("done");    p.add_argument("job_id", type=int)
    p = sub.add_parser("fail");    p.add_argument("job_id", type=int); p.add_argument("reason")
    p = sub.add_parser("status");  p.add_argument("job_id", type=int)

    args = ap.parse_args()
    c = conn()

    if args.cmd == "propose":
        edl = json.loads(Path(args.edl).read_text())
        cur = c.execute(
            "INSERT INTO edit_jobs (clip_id, edl_json, label, status) VALUES (?,?,?, 'proposed')",
            (args.clip_id, json.dumps(edl), args.label))
        c.commit()
        print(f"proposed job {cur.lastrowid} for clip {args.clip_id}")

    elif args.cmd == "list":
        rows = c.execute("SELECT id, clip_id, label, status, created_at FROM edit_jobs ORDER BY id DESC").fetchall()
        if not rows:
            print("no edit jobs")
        for r in rows:
            print(f"#{r[0]}  clip {r[1]}  {r[3]:9s}  {r[2] or ''}  ({r[4]})")

    elif args.cmd in ("approve", "start", "done", "fail"):
        status = args.cmd
        error = None
        if args.cmd == "fail":
            status, error = "failed", args.reason
        c.execute("UPDATE edit_jobs SET status=?, updated_at=datetime('now'), error=? WHERE id=?",
                  (status, error, args.job_id))
        c.commit()
        print(f"job {args.job_id} -> {status}")

    elif args.cmd == "status":
        r = c.execute("SELECT * FROM edit_jobs WHERE id=?", (args.job_id,)).fetchone()
        if not r:
            print(f"no job {args.job_id}")
        else:
            print(f"job {r[0]}: clip {r[1]} [{r[4]}] {r[2] or ''}")
            print(f"  created {r[5]}  updated {r[6]}")
            if r[7]:
                print(f"  error: {r[7]}")
            print(f"  edl: {r[3][:200]}...")

    c.close()

if __name__ == "__main__":
    main()
