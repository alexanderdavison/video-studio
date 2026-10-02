#!/usr/bin/env python3
"""
Studio index search — semantic query over the SQLite library.
Usage: search.py "find the drop where the crowd goes crazy" [--top 8] [--source whisper|scribe]
"""
import os
import argparse, json, sqlite3, sys, urllib.request
from pathlib import Path

DB = Path("/opt/video-studio/studio.db")
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

OLLAMA = "http://%s:11434" % _deploy_host("VIDEO_OPS_HOST")
EMBED_MODEL = "bge-m3"

def embed(text):
    req = urllib.request.Request(
        f"{OLLAMA}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": [text]}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    return resp["embeddings"][0]

def cosine(a, b):
    import numpy as np
    av, bv = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    return float(av @ bv / (np.linalg.norm(av) * np.linalg.norm(bv) + 1e-9))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="+")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--source", default=None, choices=["whisper", "scribe"])
    ap.add_argument("--clip", default=None, help="filter by clip id")
    args = ap.parse_args()
    q = " ".join(args.query)

    conn = sqlite3.connect(DB)
    qv = embed(q)

    sql = """SELECT ts.id, ts.start, ts.end, ts.text, c.id, c.path, ts.source
             FROM transcript_segments ts JOIN clips c ON c.id = ts.clip_id
             WHERE ts.embedding IS NOT NULL"""
    params = []
    if args.source:
        sql += " AND ts.source = ?"
        params.append(args.source)
    if args.clip:
        sql += " AND c.id = ?"
        params.append(args.clip)
    rows = conn.execute(sql, params).fetchall()

    scored = []
    for segid, start, end, text, cid, path, source in rows:
        blob = conn.execute("SELECT embedding FROM transcript_segments WHERE id=?", (segid,)).fetchone()[0]
        if not blob:
            continue
        import numpy as np
        vec = np.frombuffer(blob, dtype=np.float32)
        scored.append((cosine(qv, vec), start, end, text, cid, path, source))
    scored.sort(key=lambda x: -x[0])

    if not scored:
        print("No indexed segments found. Run ingest.py first.")
        return
    print(f"Top {args.top} of {len(scored)} segments for: {q}\n")
    for score, start, end, text, cid, path, source in scored[:args.top]:
        mm = int(start // 60); ss = int(start % 60)
        print(f"[{score:0.3f}] clip {cid} {path.split('/')[-1]} @ {mm:02d}:{ss:02d} ({source})")
        print(f"      {text[:140]}")
        print()

if __name__ == "__main__":
    main()
