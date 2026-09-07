#!/usr/bin/env python3
"""
Studio index — SQLite library for the AI video editor (sandbox LXC 204).
DB lives on LOCAL disk (/opt/video-studio/studio.db), NOT NFS.
Embeddings: bge-m3 via Ollama on .22, stored as float32 BLOB (no sqlite-vec).
Transcripts: whisper (search-grade, free) on ingest; scribe (cut-grade, paid) on-demand.
"""
import hashlib, json, os, sqlite3, subprocess, sys, time, urllib.request
from pathlib import Path

DB = Path("/opt/video-studio/studio.db")
RAW = Path("/mnt/media/raw")
OLLAMA = "http://192.168.1.22:11434"
EMBED_MODEL = "bge-m3"
MAX_RETRIES = 3
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".ts"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  sha256 TEXT,
  size INTEGER,
  mtime REAL,
  ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
  duration REAL, width INTEGER, height INTEGER, fps REAL,
  codec TEXT, camera_source TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  retries INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_clips_sha ON clips(sha256);

CREATE TABLE IF NOT EXISTS transcript_segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  source TEXT NOT NULL CHECK (source IN ('whisper','scribe')),
  seq INTEGER NOT NULL,
  start REAL NOT NULL,
  end REAL NOT NULL,
  text TEXT NOT NULL,
  embedding BLOB,
  UNIQUE(clip_id, source, seq)
);
CREATE INDEX IF NOT EXISTS idx_ts_clip_source ON transcript_segments(clip_id, source);

CREATE TABLE IF NOT EXISTS scenes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  start REAL NOT NULL,
  end REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scenes_clip ON scenes(clip_id);

CREATE TABLE IF NOT EXISTS beats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  time REAL NOT NULL,
  bpm REAL
);
CREATE INDEX IF NOT EXISTS idx_beats_clip ON beats(clip_id);

CREATE TABLE IF NOT EXISTS keyframes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  ts REAL NOT NULL,
  path TEXT,
  sharpness REAL, brightness REAL, motion REAL
);

CREATE TABLE IF NOT EXISTS edits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  edl_json TEXT NOT NULL,
  label TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ingest_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_hash TEXT NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_hash_stage ON ingest_log(file_hash, stage);
"""

def connect_db():
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def acquire_lock():
    """Simple pidfile — one ingest at a time (watcher + manual never collide)."""
    lockfile = Path("/tmp/studio-ingest.lock")
    if lockfile.exists():
        try:
            pid = int(lockfile.read_text().strip())
            os.kill(pid, 0)
            return False
        except (ValueError, ProcessLookupError):
            pass
    lockfile.write_text(str(os.getpid()))
    return True

def release_lock():
    try:
        Path("/tmp/studio-ingest.lock").unlink(missing_ok=True)
    except Exception:
        pass

def log(conn, file_hash, stage, status, error=None):
    conn.execute("INSERT INTO ingest_log (file_hash, stage, status, error, finished_at) VALUES (?,?,?,?,datetime('now'))",
                 (file_hash, stage, status, error))

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_name,width,height,r_frame_rate",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr[:300]}")
    d = json.loads(out.stdout)
    fmt = d.get("format", {})
    vstream = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    fr = vstream.get("r_frame_rate", "")
    fps = None
    try:
        num, den = fr.split("/")
        fps = round(float(num) / float(den), 3) if float(den) else None
    except Exception:
        pass
    return {
        "duration": float(fmt.get("duration", 0) or 0),
        "size": int(fmt.get("size", 0) or 0),
        "codec": vstream.get("codec_name"),
        "width": vstream.get("width"),
        "height": vstream.get("height"),
        "fps": fps,
    }

def camera_source(path):
    p = str(path)
    name = Path(p).name
    if name.startswith("DJI_"):
        return "dji"
    if name.startswith("GP"):
        return "gopro"
    return None

def embed_batch(texts):
    """bge-m3 via Ollama /api/embed. Returns list of float32 lists."""
    if not texts:
        return []
    req = urllib.request.Request(
        f"{OLLAMA}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read())
    return resp.get("embeddings", [])

def transcribe_whisper(path, max_seconds=None):
    """Run whisper via system python3 (base.en). Returns segments [{start,end,text}]."""
    # write a small runner to avoid quoting pain
    runner = "/tmp/whisper_runner.py"
    code = (
        "import json,sys,whisper\n"
        "m=whisper.load_model('base.en')\n"
        "r=m.transcribe(sys.argv[1], fp16=False)\n"
        "print(json.dumps(r['segments']))\n"
    )
    Path(runner).write_text(code)
    cmd = ["/usr/bin/python3", runner, str(path)]
    if max_seconds:
        cmd += [str(max_seconds)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if out.returncode != 0:
        raise RuntimeError(f"whisper failed: {out.stderr[:400]}")
    segs = json.loads(out.stdout)
    return [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in segs]

def run_scenedetect(path, outdir, clip_id, conn):
    outdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["/opt/video-studio/tools/venv/bin/scenedetect", "-i", str(path),
         "-o", str(outdir), "-d", "--downscale", "16"],
        capture_output=True, text=True, timeout=1800)
    csv = outdir / "scenes.csv"
    if not csv.exists():
        # scenedetect writes to output dir with a generated name; find any csv
        csvs = list(outdir.glob("*.csv"))
        if not csvs:
            return 0
        csv = csvs[0]
    n = 0
    for line in csv.read_text().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 2:
            try:
                start = float(parts[0])
                end = float(parts[1])
            except ValueError:
                continue
            conn.execute("INSERT INTO scenes (clip_id, start, end) VALUES (?,?,?)", (clip_id, start, end))
            n += 1
    return n

def run_beats(path, clip_id, conn):
    out = subprocess.run(
        ["/opt/video-studio/tools/venv/bin/python", "/opt/video-studio/tools/bin/beat_detect.py",
         str(path), "--json", "/tmp/beats.json"],
        capture_output=True, text=True, timeout=1800)
    bpm = None
    try:
        data = json.loads(Path("/tmp/beats.json").read_text())
        bpm = data.get("bpm")
        beats = data.get("beats", [])
    except Exception:
        beats = []
    for b in beats:
        conn.execute("INSERT INTO beats (clip_id, time, bpm) VALUES (?,?,?)", (clip_id, float(b), bpm))
    return len(beats), bpm

def run_keyframes(path, clip_id, conn):
    outdir = Path(f"/opt/video-studio/projects/index/keyframes-{clip_id}")
    outdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["/opt/video-studio/tools/venv/bin/python", "/opt/video-studio/tools/bin/pick_keyframes.py",
         str(path), "--out", str(outdir), "--count", "5", "--samples", "30"],
        capture_output=True, text=True, timeout=1800)
    for img in sorted(outdir.glob("*.jpg")):
        conn.execute("INSERT INTO keyframes (clip_id, ts, path) VALUES (?,?,?)", (clip_id, 0.0, str(img)))

def ingest_one(conn, path, stages, max_seconds=None):
    path = Path(path)
    st = path.stat()
    fhash = sha256_file(path)
    # HASH-FIRST GATE — before any probe/transcribe
    existing = conn.execute("SELECT id, status, retries FROM clips WHERE sha256=?", (fhash,)).fetchone()
    if existing:
        cid, status, retries = existing
        if status == "indexed":
            return f"skip (already indexed {path.name})"
        if status == "quarantined" or retries >= MAX_RETRIES:
            return f"skip (quarantined {path.name})"
    else:
        cid = None

    if cid is None:
        cur = conn.execute(
            "INSERT INTO clips (path, sha256, size, mtime, status) VALUES (?,?,?,?, 'pending')",
            (str(path), fhash, st.st_size, st.st_mtime))
        cid = cur.lastrowid
        conn.commit()

    results = []
    if "probe" in stages:
        try:
            meta = ffprobe(path)
            conn.execute("UPDATE clips SET duration=?,width=?,height=?,fps=?,codec=?,camera_source=? WHERE id=?",
                         (meta["duration"], meta["width"], meta["height"], meta["fps"], meta["codec"],
                          camera_source(path), cid))
            conn.commit()
            log(conn, fhash, "probe", "ok")
            results.append(f"probe: {meta['width']}x{meta['height']} {meta['codec']} {meta['duration']:.0f}s")
        except Exception as e:
            log(conn, fhash, "probe", "failed", str(e))
            return fail_clip(conn, cid, fhash, "probe", str(e), results)

    if "transcribe" in stages:
        try:
            segs = transcribe_whisper(path, max_seconds)
            for i, s in enumerate(segs):
                conn.execute(
                    "INSERT OR REPLACE INTO transcript_segments (clip_id, source, seq, start, end, text) VALUES (?,?,?,?,?,?)",
                    (cid, "whisper", i, s["start"], s["end"], s["text"]))
            conn.commit()
            log(conn, fhash, "transcribe", "ok")
            results.append(f"transcribe: {len(segs)} segments")
        except Exception as e:
            log(conn, fhash, "transcribe", "failed", str(e))
            return fail_clip(conn, cid, fhash, "transcribe", str(e), results)

    if "embed" in stages:
        try:
            rows = conn.execute(
                "SELECT id, text FROM transcript_segments WHERE clip_id=? AND source='whisper' AND embedding IS NULL",
                (cid,)).fetchall()
            for i in range(0, len(rows), 32):
                batch = rows[i:i+32]
                vecs = embed_batch([r[1] for r in batch])
                for (segid, _), vec in zip(batch, vecs):
                    import numpy as np
                    blob = np.asarray(vec, dtype=np.float32).tobytes()
                    conn.execute("UPDATE transcript_segments SET embedding=? WHERE id=?", (blob, segid))
            conn.commit()
            log(conn, fhash, "embed", "ok")
            results.append(f"embed: {len(rows)} segments")
        except Exception as e:
            log(conn, fhash, "embed", "failed", str(e))
            return fail_clip(conn, cid, fhash, "embed", str(e), results)

    if "scenes" in stages:
        try:
            n = run_scenedetect(path, Path(f"/opt/video-studio/projects/index/scenes-{cid}"), cid, conn)
            conn.commit()
            log(conn, fhash, "scenes", "ok")
            results.append(f"scenes: {n}")
        except Exception as e:
            log(conn, fhash, "scenes", "failed", str(e))
            return fail_clip(conn, cid, fhash, "scenes", str(e), results)

    if "beats" in stages:
        try:
            n, bpm = run_beats(path, cid, conn)
            conn.commit()
            log(conn, fhash, "beats", "ok")
            results.append(f"beats: {n} @ {bpm} BPM")
        except Exception as e:
            log(conn, fhash, "beats", "failed", str(e))
            return fail_clip(conn, cid, fhash, "beats", str(e), results)

    if "keyframes" in stages:
        try:
            run_keyframes(path, cid, conn)
            conn.commit()
            log(conn, fhash, "keyframes", "ok")
            results.append("keyframes: ok")
        except Exception as e:
            log(conn, fhash, "keyframes", "failed", str(e))
            return fail_clip(conn, cid, fhash, "keyframes", str(e), results)

    conn.execute("UPDATE clips SET status='indexed', retries=0 WHERE id=?", (cid,))
    conn.commit()
    return "; ".join(results)

def fail_clip(conn, cid, fhash, stage, err, results):
    conn.execute("UPDATE clips SET retries=retries+1, status=CASE WHEN retries+1>=? THEN 'quarantined' ELSE 'pending' END WHERE id=?",
                 (MAX_RETRIES, cid))
    conn.commit()
    return "; ".join(results + [f"FAILED@{stage}: {err[:120]}"])

def scan_and_ingest(stages, max_seconds=None, limit=None):
    if not acquire_lock():
        print("Another ingest is running (pidfile /tmp/studio-ingest.lock). Exiting.")
        return
    try:
        conn = connect_db()
        conn.executescript(SCHEMA)
        conn.commit()
        files = [p for p in sorted(RAW.rglob("*")) if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
        done = 0
        for f in files:
            try:
                st = f.stat()
            except OSError as e:
                print(f"stat fail {f}: {e}")
                continue
            row = conn.execute("SELECT id, size, mtime, sha256, status, retries FROM clips WHERE path=?", (str(f),)).fetchone()
            if row and row[1] == st.st_size and abs((row[2] or 0) - st.st_mtime) < 1 and row[4] == "indexed":
                continue  # cheap stat gate — unchanged
            print(f"== {f.name}")
            try:
                msg = ingest_one(conn, f, stages, max_seconds)
            except Exception as e:
                msg = f"EXC: {e}"
            print(f"   {msg}")
            done += 1
            if limit and done >= limit:
                break
        conn.close()
    finally:
        release_lock()

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="probe,transcribe,embed,scenes,beats,keyframes",
                    help="comma list of stages")
    ap.add_argument("--max-seconds", type=float, default=None, help="limit whisper to first N sec")
    ap.add_argument("--limit", type=int, default=None, help="max files this run")
    ap.add_argument("--file", default=None, help="single file instead of scan")
    args = ap.parse_args()
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    if args.file:
        if not acquire_lock():
            print("Another ingest is running (pidfile /tmp/studio-ingest.lock). Exiting.")
            sys.exit(1)
        try:
            conn = connect_db()
            conn.executescript(SCHEMA)
            conn.commit()
            print(ingest_one(conn, args.file, stages, args.max_seconds))
            conn.close()
        finally:
            release_lock()
    else:
        scan_and_ingest(stages, args.max_seconds, args.limit)
