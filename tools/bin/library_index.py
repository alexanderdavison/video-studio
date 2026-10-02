#!/usr/bin/env python3
"""Library index for the video studio.

SQLite index over the raw footage library (default /mnt/media/raw):
per-clip ffprobe metadata, analysis sidecars (beats / scenes / vision
scores), tags, FTS5 keyword search, and optional semantic search via
local Ollama embeddings (.22).  Stdlib only — no pip deps.

Subcommands:
  scan    [--dir DIR] [--ext ...] [--prune]   inventory clips (ffprobe)
  enrich  [--transcripts-dir DIR]             attach sidecars found next to clips
  search  [--semantic] "query" [--limit N]    keyword (FTS5) or semantic (Ollama)
  embed   [--model m] [--force]               build embeddings for all clips
  tag     <match> <tag...>                    add space-separated tags
  status  [--json]                            index summary
  rm      <match>                             drop clip + segments + embedding

DB default: /opt/video-studio/data/library.db  (override: --db PATH)
Ollama default: http://$VIDEO_OPS_HOST:11434       (override: --ollama URL)

Examples:
  library_index.py scan
  library_index.py scan --dir /mnt/media/raw/2026-08
  library_index.py embed
  library_index.py search "interview"
  library_index.py search --semantic "crowd reaction footage"
  library_index.py tag DJI_2026 concert crowd
"""

from __future__ import annotations

import os
import argparse
import csv
import io
import json
import math
import sqlite3
import struct
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path("/opt/video-studio/data/library.db")
def _deploy_host(name, default="localhost"):
    """Deployment host: environment, else the untracked .env. See .env.example.

    Tracked source carries no LAN addresses: they are deployment configuration, they make a clone
    unusable anywhere else, and they keep tripping the GitHub mirror's scrubber.
    """
    if os.environ.get(name):
        return os.environ[name]
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        cand = os.path.join(d, ".env")
        if os.path.exists(cand):
            for line in open(cand):
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip()
            break
        if os.path.exists(os.path.join(d, ".git")):
            break                       # reached the repo root without a .env
        d = os.path.dirname(d)
    return default

DEFAULT_OLLAMA = "http://%s:11434" % _deploy_host("VIDEO_OPS_HOST")
DEFAULT_EXTS = {".mp4", ".mov", ".mxf", ".mkv", ".avi", ".m4v", ".ts", ".mts", ".3gp"}
SKIP_PREFIXES = (".", "._")  # .DS_Store, AppleDouble junk

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
  path       TEXT PRIMARY KEY,
  filename   TEXT NOT NULL,
  dir        TEXT NOT NULL,
  size       INTEGER,
  mtime      REAL,
  duration_s REAL,
  width      INTEGER,
  height     INTEGER,
  fps        REAL,
  fps_ratio  TEXT,
  codec_v    TEXT,
  codec_a    TEXT,
  audio      INTEGER DEFAULT 0,
  tags       TEXT DEFAULT '',
  notes      TEXT DEFAULT '',
  indexed_at TEXT
);
CREATE TABLE IF NOT EXISTS segments (
  id        INTEGER PRIMARY KEY,
  clip_path TEXT NOT NULL REFERENCES clips(path) ON DELETE CASCADE,
  kind      TEXT NOT NULL,          -- scene | beat | vision | keyframe
  start_s   REAL,
  end_s     REAL,
  score     REAL,
  meta      TEXT
);
CREATE INDEX IF NOT EXISTS idx_seg_clip ON segments(clip_path);
CREATE VIRTUAL TABLE IF NOT EXISTS clip_fts USING fts5(filename, dir, tags, notes, path UNINDEXED);
CREATE TABLE IF NOT EXISTS embeddings (
  clip_path TEXT PRIMARY KEY REFERENCES clips(path) ON DELETE CASCADE,
  model     TEXT NOT NULL,
  vector    BLOB NOT NULL
);
"""


# ---------------- DB helpers ------------------------------------------------


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    return conn


def fts_upsert(conn: sqlite3.Connection, path: str, filename: str, dir_: str, tags: str, notes: str) -> None:
    conn.execute("DELETE FROM clip_fts WHERE path = ?", (path,))
    conn.execute(
        "INSERT INTO clip_fts (filename, dir, tags, notes, path) VALUES (?,?,?,?,?)",
        (filename, dir_, tags, notes, path),
    )


def fts_delete(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM clip_fts WHERE path = ?", (path,))


# ---------------- ffprobe ---------------------------------------------------


def ffprobe_clip(path: Path) -> dict:
    """Return metadata dict for a media file, or None if unreadable."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate",
        "-show_entries", "format=duration,size",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
        data = json.loads(out)
    except Exception:
        return {}
    streams = data.get("streams", [])
    vs = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not vs:
        return {}
    fmt = data.get("format", {})
    fps_ratio = vs.get("r_frame_rate") or vs.get("avg_frame_rate") or "0/1"
    fps = 0.0
    try:
        n, d = fps_ratio.split("/")
        fps = float(n) / float(d) if float(d) else 0.0
    except Exception:
        pass
    audio = any(s.get("codec_type") == "audio" for s in streams)
    acodec = next((s.get("codec_name", "") for s in streams if s.get("codec_type") == "audio"), "")
    return {
        "size": int(fmt.get("size", 0) or 0),
        "duration_s": float(fmt.get("duration", 0) or 0),
        "width": int(vs.get("width", 0) or 0),
        "height": int(vs.get("height", 0) or 0),
        "fps": round(fps, 6),
        "fps_ratio": fps_ratio,
        "codec_v": vs.get("codec_name", ""),
        "codec_a": acodec,
        "audio": 1 if audio else 0,
    }


# ---------------- scan ------------------------------------------------------


def cmd_scan(args) -> int:
    conn = connect(args.db)
    root = Path(args.dir).resolve()
    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in args.ext}
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in exts and not p.name.startswith(SKIP_PREFIXES)
    )
    added = updated = skipped = 0
    for p in files:
        meta = ffprobe_clip(p)
        if not meta:
            skipped += 1
            continue
        mtime = p.stat().st_mtime
        row = conn.execute(
            "SELECT mtime, size FROM clips WHERE path = ?", (str(p),)
        ).fetchone()
        if row and row[0] == mtime and row[1] == meta["size"]:
            continue  # unchanged
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tags_row = conn.execute("SELECT tags, notes FROM clips WHERE path = ?", (str(p),)).fetchone()
        tags = tags_row[0] if tags_row else ""
        notes = tags_row[1] if tags_row else ""
        conn.execute(
            """INSERT INTO clips (path, filename, dir, size, mtime, duration_s, width, height,
                                  fps, fps_ratio, codec_v, codec_a, audio, tags, notes, indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 size=excluded.size, mtime=excluded.mtime, duration_s=excluded.duration_s,
                 width=excluded.width, height=excluded.height, fps=excluded.fps,
                 fps_ratio=excluded.fps_ratio, codec_v=excluded.codec_v, codec_a=excluded.codec_a,
                 audio=excluded.audio, indexed_at=excluded.indexed_at""",
            (str(p), p.name, str(p.parent), meta["size"], mtime, meta["duration_s"],
             meta["width"], meta["height"], meta["fps"], meta["fps_ratio"],
             meta["codec_v"], meta["codec_a"], meta["audio"], tags, notes, now),
        )
        fts_upsert(conn, str(p), p.name, str(p.parent), tags, notes)
        added += 1
        updated += 1 if row else 0
    if args.prune:
        known = {str(p) for p in files}
        for (path,) in conn.execute("SELECT path FROM clips"):
            if path not in known:
                fts_delete(conn, path)
                conn.execute("DELETE FROM clips WHERE path = ?", (path,))
                print(f"  pruned (missing): {path}")
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    dur = conn.execute("SELECT COALESCE(SUM(duration_s),0) FROM clips").fetchone()[0]
    print(f"scan: {added} indexed, {skipped} skipped (unreadable/not media), {total} clips, "
          f"{dur/3600:.1f} h total")
    return 0


# ---------------- sidecar enrichment ----------------------------------------


def _json_list_candidates(obj: object) -> list[dict]:
    """Tolerant: pull the most likely list-of-dicts from a parsed JSON blob."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for key in ("beats", "shortlist", "results", "scenes", "segments", "items"):
            v = obj.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [x for x in v if isinstance(x, dict)]
        # fall back to any list value
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [x for x in v if isinstance(x, dict)]
    return []


def _ts_of(entry: dict) -> float | None:
    """Pull a start time from an unknown-shaped JSON entry."""
    for key in ("time", "start", "start_s", "t", "timestamp", "beat_time", "frame_ts", "in", "start_time"):
        v = entry.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                pass
    return None


def _score_of(entry: dict) -> float | None:
    for key in ("score", "value", "confidence", "prob", "weight"):
        v = entry.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _end_of(entry: dict, start: float) -> float | None:
    for key in ("end", "end_s", "out", "end_time", "duration", "dur"):
        v = entry.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            end = start + float(v) if key in ("duration", "dur") else float(v)
            return end
    return None


def _import_sidecar(conn: sqlite3.Connection, clip_path: str, sidecar: Path, kind: str) -> int:
    """Import a beats/scores JSON sidecar into segments. Returns count."""
    try:
        obj = json.loads(sidecar.read_text())
    except Exception:
        return 0
    entries = _json_list_candidates(obj)
    if not entries:
        return 0
    n = 0
    for e in entries:
        start = _ts_of(e)
        if start is None:
            continue
        end = _end_of(e, start)
        score = _score_of(e)
        meta = json.dumps(e, sort_keys=True)[:400]
        conn.execute(
            "INSERT INTO segments (clip_path, kind, start_s, end_s, score, meta) VALUES (?,?,?,?,?,?)",
            (clip_path, kind, start, end, score, meta),
        )
        n += 1
    return n


def _import_scenes_csv(conn: sqlite3.Connection, clip_path: str, sidecar: Path) -> int:
    """scenedetect list-scenes CSV: Timecode In / Timecode Out (or Frame In/Out)."""
    def tc_to_s(tc: str) -> float | None:
        parts = tc.strip().split(":")
        if len(parts) == 3:
            try:
                h, m, s = (float(x) for x in parts)
                return h * 3600 + m * 60 + s
            except ValueError:
                return None
        return None

    n = 0
    try:
        with sidecar.open() as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                tin = row.get("Timecode In") or row.get("Start Timecode") or ""
                tout = row.get("Timecode Out") or row.get("End Timecode") or ""
                s, e = tc_to_s(tin), tc_to_s(tout)
                if s is None:
                    continue
                if e is None:
                    try:
                        e = float(row.get("Duration", 0) or 0) + s
                    except ValueError:
                        e = None
                conn.execute(
                    "INSERT INTO segments (clip_path, kind, start_s, end_s, score, meta) VALUES (?,?,?,?,?,?)",
                    (clip_path, "scene", s, e, None, f"csv: {tin} -> {tout}"),
                )
                n += 1
    except Exception:
        return 0
    return n


def cmd_enrich(args) -> int:
    conn = connect(args.db)
    kinds = {"beat": 0, "vision": 0, "scene": 0, "transcript": 0}
    for (path,) in conn.execute("SELECT path FROM clips"):
        clip = Path(path)
        stem = clip.stem
        sidecars = [
            (clip.with_name(f"{stem}.beats.json"), "beat"),
            (clip.with_name(f"{stem}.scores.json"), "vision"),
            (clip.with_name(f"{stem}.scenes.csv"), "scene"),
            (clip.with_name(f"{stem}_scenes.csv"), "scene"),
        ]
        for sidecar, kind in sidecars:
            if sidecar.exists():
                n = _import_sidecar(conn, path, sidecar, kind) if kind != "scene" else _import_scenes_csv(conn, path, sidecar)
                kinds[kind] += n
        # transcripts dir: <stem>.json with a 'words' list
        if args.transcripts_dir:
            tdir = Path(args.transcripts_dir)
            for cand in tdir.glob(f"{stem}*"):
                if cand.suffix.lower() != ".json":
                    continue
                try:
                    obj = json.loads(cand.read_text())
                except Exception:
                    continue
                words = obj.get("words") if isinstance(obj, dict) else None
                if isinstance(words, list) and words:
                    text = " ".join(
                        str(w.get("word", "")) for w in words if isinstance(w, dict) and w.get("type", "word") == "word"
                    )
                    if text.strip():
                        conn.execute("UPDATE clips SET notes = ? WHERE path = ?",
                                     (text[:4000], path))
                        fts_upsert(conn, path, clip.name, str(clip.parent),
                                   conn.execute("SELECT tags FROM clips WHERE path = ?", (path,)).fetchone()[0] or "",
                                   text[:4000])
                        kinds["transcript"] += 1
                    break
    conn.commit()
    print("enrich:" + ", ".join(f" {k}={v}" for k, v in kinds.items() if v) or " enrich: no sidecars found")
    return 0


# ---------------- embeddings ------------------------------------------------


def _embed(ollama: str, model: str, text: str) -> list[float]:
    body = json.dumps({"model": model, "input": text}).encode()
    req = urllib.request.Request(
        f"{ollama}/api/embed", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    vec = data.get("embeddings", [None])[0]
    if not vec:
        raise RuntimeError(f"ollama returned no embedding for model {model}")
    return [float(x) for x in vec]


def _norm(v: list[float]) -> list[float]:
    mag = math.sqrt(sum(x * x for x in v))
    return [x / mag for x in v] if mag else v


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _unpack(b: bytes) -> list[float]:
    return list(struct.unpack(f"{len(b)//4}f", b))


def cmd_embed(args) -> int:
    conn = connect(args.db)
    clips = conn.execute("SELECT path, filename, tags, notes FROM clips").fetchall()
    if not clips:
        print("embed: no clips in index — run scan first")
        return 1
    if args.force:
        conn.execute("DELETE FROM embeddings")
    done = 0
    for path, filename, tags, notes in clips:
        existing = conn.execute(
            "SELECT 1 FROM embeddings WHERE clip_path = ? AND model = ?", (path, args.model)
        ).fetchone()
        if existing:
            continue
        text = " ".join(x for x in (filename, tags, notes) if x)
        if not text.strip():
            continue
        try:
            vec = _norm(_embed(args.ollama, args.model, text))
        except Exception as exc:
            print(f"  embed failed for {filename}: {exc}")
            continue
        conn.execute(
            "INSERT INTO embeddings (clip_path, model, vector) VALUES (?,?,?) "
            "ON CONFLICT(clip_path) DO UPDATE SET model=excluded.model, vector=excluded.vector",
            (path, args.model, _pack(vec)),
        )
        done += 1
    conn.commit()
    print(f"embed: {done} clips embedded with {args.model}")
    return 0


def _semantic_search(conn: sqlite3.Connection, ollama: str, model: str, query: str, limit: int) -> list[tuple]:
    qv = _norm(_embed(ollama, model, query))
    rows = conn.execute("SELECT clip_path, vector FROM embeddings").fetchall()
    scored = []
    for path, blob in rows:
        vec = _unpack(blob)
        if len(vec) != len(qv):
            continue
        dot = sum(a * b for a, b in zip(qv, vec))
        scored.append((dot, path))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


# ---------------- search ----------------------------------------------------


def cmd_search(args) -> int:
    conn = connect(args.db)
    results: list[tuple] = []
    if args.semantic:
        results = _semantic_search(conn, args.ollama, args.model, args.query, args.limit)
        rows = []
        for score, path in results:
            r = conn.execute(
                "SELECT filename, dir, duration_s, width, height, fps, tags FROM clips WHERE path = ?",
                (path,),
            ).fetchone()
            if r:
                rows.append((score, path, r))
        if args.json:
            print(json.dumps([
                {"score": round(score, 4), "path": path, "filename": r[0], "dir": r[1],
                 "duration_s": r[2], "width": r[3], "height": r[4], "fps": r[5], "tags": r[6]}
                for score, path, r in rows
            ], indent=2))
            return 0
        if not rows:
            print(f"no semantic hits (model {args.model}). Run: library_index.py embed")
            return 1
        for score, path, r in rows:
            print(f"{score:7.4f}  {r[0]:32s} {r[2]/60:7.1f}min  {r[3]}x{r[4]}  {r[5]:.2f}fps  {path}")
        return 0

    # keyword
    if not args.query:
        print("search: provide a query, or use --semantic")
        return 1
    q = " AND ".join(f'"{w}"' for w in args.query.split())
    try:
        hits = conn.execute(
            "SELECT path, filename, dir, tags, notes, bm25(clip_fts) AS rank "
            "FROM clip_fts WHERE clip_fts MATCH ? ORDER BY rank LIMIT ?",
            (q, args.limit),
        ).fetchall()
    except sqlite3.OperationalError:
        hits = conn.execute(
            "SELECT path, filename, dir, tags, notes, 0 FROM clip_fts "
            "WHERE filename LIKE ? OR tags LIKE ? OR notes LIKE ? LIMIT ?",
            (f"%{args.query}%", f"%{args.query}%", f"%{args.query}%", args.limit),
        ).fetchall()
    if args.json:
        print(json.dumps([
            {"path": p, "filename": f, "dir": d, "tags": t, "notes": n} for p, f, d, t, n, _ in hits
        ], indent=2))
        return 0
    if not hits:
        print(f"no keyword hits for '{args.query}'. Try: library_index.py search --semantic \"{args.query}\"")
        return 1
    for path, filename, dir_, tags, notes, _ in hits:
        print(f"{filename:32s} tags=[{tags}]  {path}")
    return 0


# ---------------- tag / rm / status ----------------------------------------


def cmd_tag(args) -> int:
    conn = connect(args.db)
    match = f"%{args.match}%"
    rows = conn.execute(
        "SELECT path, filename, tags, notes FROM clips WHERE filename LIKE ? OR path LIKE ?",
        (match, match),
    ).fetchall()
    if not rows:
        print(f"tag: no clips match '{args.match}'")
        return 1
    new_tags = " ".join(args.tags)
    for path, filename, tags, notes in rows:
        merged = " ".join(dict.fromkeys((tags.split() if tags else []) + new_tags.split()))
        conn.execute("UPDATE clips SET tags = ? WHERE path = ?", (merged, path))
        fts_upsert(conn, path, filename, str(Path(path).parent), merged, notes)
        print(f"tag: {filename} -> [{merged}]")
    conn.commit()
    return 0


def cmd_rm(args) -> int:
    conn = connect(args.db)
    match = f"%{args.match}%"
    rows = conn.execute("SELECT path, filename FROM clips WHERE filename LIKE ? OR path LIKE ?", (match, match)).fetchall()
    if not rows:
        print(f"rm: no clips match '{args.match}'")
        return 1
    for path, filename in rows:
        fts_delete(conn, path)
        conn.execute("DELETE FROM clips WHERE path = ?", (path,))
        print(f"rm: {filename}")
    conn.commit()
    return 0


def cmd_status(args) -> int:
    conn = connect(args.db)
    clips = conn.execute("SELECT COUNT(*), COALESCE(SUM(duration_s),0) FROM clips").fetchone()
    segs = conn.execute("SELECT kind, COUNT(*) FROM segments GROUP BY kind").fetchall()
    emb = conn.execute("SELECT model, COUNT(*) FROM embeddings GROUP BY model").fetchall()
    if args.json:
        print(json.dumps({
            "clips": clips[0], "duration_h": round(clips[1] / 3600, 2),
            "segments": {k: c for k, c in segs},
            "embeddings": {m: c for m, c in emb},
            "db": str(args.db),
        }, indent=2))
        return 0
    print(f"db:        {args.db}")
    print(f"clips:     {clips[0]}  ({clips[1]/3600:.1f} h total)")
    print("segments:  " + (", ".join(f"{k}={c}" for k, c in segs) if segs else "none — run enrich"))
    print("embeddings:" + (" " + ", ".join(f"{m}={c}" for m, c in emb) if emb else " none — run embed"))
    return 0


# ---------------- main ------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Video studio library index")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--ollama", default=DEFAULT_OLLAMA)
    ap.add_argument("--model", default="nomic-embed-text")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="inventory clips with ffprobe")
    p.add_argument("--dir", default="/mnt/media/raw")
    p.add_argument("--ext", nargs="*", default=sorted(DEFAULT_EXTS))
    p.add_argument("--prune", action="store_true", help="drop rows whose files are gone")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("enrich", help="attach sidecars (beats/scores/scenes/transcripts)")
    p.add_argument("--transcripts-dir")
    p.set_defaults(fn=cmd_enrich)

    p = sub.add_parser("search", help="keyword (FTS5) or --semantic search")
    p.add_argument("query")
    p.add_argument("--semantic", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("embed", help="build Ollama embeddings for all clips")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_embed)

    p = sub.add_parser("tag", help="add tags to matching clips")
    p.add_argument("match")
    p.add_argument("tags", nargs="+")
    p.set_defaults(fn=cmd_tag)

    p = sub.add_parser("rm", help="remove matching clips from index")
    p.add_argument("match")
    p.set_defaults(fn=cmd_rm)

    p = sub.add_parser("status", help="index summary")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
