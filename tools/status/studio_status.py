#!/usr/bin/env python3
"""Studio Status — read-only pipeline visibility for /opt/video-studio (LXC 204).

Serves:
  /            dark-terminal HTML page (15s auto-poll)
  /api/status  JSON snapshot (studio.db + filesystem)
Read-only: opens studio.db in mode=ro. No writes anywhere.
Run: python3 studio_status.py [--port 8890] [--bind 0.0.0.0]
"""
import argparse, json, os, sqlite3, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = "/opt/video-studio/studio.db"
PROJECTS = "/opt/video-studio/projects"
FINALS = "/mnt/media/finals"
CUTS = "/mnt/media/cuts"

def db_ro():
    return sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

def snap():
    con = db_ro()
    con.row_factory = sqlite3.Row
    out = {"now": int(time.time())}
    try:
        clips = [dict(r) for r in con.execute(
            "SELECT id, path, status, duration, width, height, ingested_at FROM clips ORDER BY id")]
        segs = dict(con.execute("SELECT clip_id, count(*) FROM transcript_segments GROUP BY clip_id"))
        beats = dict(con.execute("SELECT clip_id, count(*) FROM beats GROUP BY clip_id"))
        scenes = dict(con.execute("SELECT clip_id, count(*) FROM scenes GROUP BY clip_id"))
        for c in clips:
            c["name"] = os.path.basename(c["path"])
            c["segments"] = segs.get(c["id"], 0)
            c["beats"] = beats.get(c["id"], 0)
            c["scenes"] = scenes.get(c["id"], 0)
        jobs = [dict(r) for r in con.execute(
            "SELECT id, clip_id, label, status, created_at, updated_at, error FROM edit_jobs ORDER BY id DESC LIMIT 25")]
        stages = [dict(r) for r in con.execute(
            "SELECT stage, status, count(*) AS n FROM ingest_log GROUP BY stage, status ORDER BY stage")]
        out["clips"], out["jobs"], out["stages"] = clips, jobs, stages
    finally:
        con.close()

    def mp4s(path):
        try:
            return sorted([{"name": e.name, "size": e.stat().st_size,
                            "mtime": int(e.stat().st_mtime)}
                           for e in os.scandir(path) if e.name.lower().endswith(".mp4")],
                          key=lambda x: x["mtime"], reverse=True)[:12]
        except OSError:
            return []
    out["finals"] = mp4s(FINALS)
    out["cuts"] = mp4s(CUTS)
    renders = []
    try:
        for slug in sorted(os.listdir(PROJECTS), reverse=True)[:10]:
            rdir = os.path.join(PROJECTS, slug, "renders")
            renders += [{"project": slug, **r} for r in mp4s(rdir)]
    except OSError:
        pass
    out["renders"] = renders[:12]
    try:
        out["system"] = {"load1": float(open("/proc/loadavg").read().split()[0]),
                         "db_mb": round(os.path.getsize(DB) / 1e6, 1)}
    except OSError:
        out["system"] = {}
    return out

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VIDEO STUDIO — PIPELINE</title>
<style>
:root{--bg:#0a0e0a;--fg:#b8c4b8;--dim:#5f6f5f;--grn:#3ddc55;--amb:#e0b64e;--red:#e05252;--brd:#1c241c}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:12px/1.45 "SF Mono",Menlo,Consolas,monospace;padding:14px}
h1{font-size:14px;letter-spacing:2px;color:var(--grn);margin-bottom:2px}
.sub{color:var(--dim);margin-bottom:10px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--grn);margin-right:6px;vertical-align:1px}
table{width:100%;border-collapse:collapse;margin:4px 0 12px}
th{color:var(--amb);text-align:left;font-weight:normal;border-bottom:1px solid var(--brd);padding:3px 6px;white-space:nowrap}
td{padding:3px 6px;border-bottom:1px solid #121812;white-space:nowrap;max-width:340px;overflow:hidden;text-overflow:ellipsis}
.num{text-align:right}
.ok{color:var(--grn)} .warn{color:var(--amb)} .bad{color:var(--red)} .dim{color:var(--dim)}
.scroll{max-height:34vh;overflow-y:auto}
.wrap{white-space:normal;word-break:break-all}
</style></head><body>
<h1><span class="dot"></span>VIDEO STUDIO — PIPELINE STATUS</h1>
<div class="sub" id="sub">connecting…</div>
<div class="scroll"><table><thead><tr><th>ID</th><th>CLIP</th><th>STATUS</th><th class="num">DUR</th><th class="num">SEG</th><th class="num">BEATS</th><th class="num">SCENES</th></tr></thead><tbody id="clips"></tbody></table></div>
<div class="scroll"><table><thead><tr><th>STAGE</th><th>STATE</th><th class="num">N</th></tr></thead><tbody id="stages"></tbody></table></div>
<div class="scroll"><table><thead><tr><th>JOB</th><th>CLIP</th><th>LABEL</th><th>STATUS</th><th>UPDATED</th><th>ERROR</th></tr></thead><tbody id="jobs"></tbody></table></div>
<div class="scroll"><table><thead><tr><th>FINAL</th><th class="num">SIZE</th><th>MTIME</th></tr></thead><tbody id="finals"></tbody></table></div>
<div class="scroll"><table><thead><tr><th>CUT</th><th class="num">SIZE</th><th>MTIME</th></tr></thead><tbody id="cuts"></tbody></table></div>
<div class="scroll"><table><thead><tr><th>PROJECT</th><th>RENDER</th><th class="num">SIZE</th><th>MTIME</th></tr></thead><tbody id="renders"></tbody></table></div>
<div class="sub" id="sys">—</div>
<script>
const fmt=n=>{if(n==null)return"–";if(n>=1e9)return(n/1e9).toFixed(1)+"G";if(n>=1e6)return(n/1e6).toFixed(1)+"M";if(n>=1e3)return(n/1e3).toFixed(0)+"K";return String(n)};
const ts=t=>{if(!t)return"–";const d=new Date(t*1e3);return d.toLocaleString([],{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"})};
const dur=s=>{if(!s)return"–";const m=Math.floor(s/60);return m+":"+String(Math.round(s%60)).padStart(2,"0")};
const esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
function load(){fetch("/api/status").then(r=>r.json()).then(d=>{
 const st=c=>({indexed:"ok",queued:"warn",failed:"bad",processing:"warn"}[c]||"dim");
 document.getElementById("clips").innerHTML=d.clips.map(c=>`<tr><td>${c.id}</td><td class="wrap">${esc(c.name)}</td><td class="${st(c.status)}">${esc(c.status)}</td><td class="num">${dur(c.duration)}</td><td class="num">${c.segments}</td><td class="num">${c.beats}</td><td class="num">${c.scenes}</td></tr>`).join("")||`<tr><td colspan="7" class="dim">no clips indexed</td></tr>`;
 document.getElementById("stages").innerHTML=d.stages.map(s=>`<tr><td>${esc(s.stage)}</td><td class="${s.status==="ok"?"ok":"bad"}">${esc(s.status)}</td><td class="num">${s.n}</td></tr>`).join("");
 document.getElementById("jobs").innerHTML=d.jobs.map(j=>`<tr><td>${j.id}</td><td>${j.clip_id??"—"}</td><td class="wrap">${esc(j.label)}</td><td class="${st(j.status)}">${esc(j.status)}</td><td>${ts(j.updated_at)}</td><td class="wrap">${esc(j.error)}</td></tr>`).join("")||`<tr><td colspan="6" class="dim">no jobs</td></tr>`;
 const mk=rows=>rows.map(f=>`<tr><td class="wrap">${esc(f.name)}</td><td class="num">${fmt(f.size)}</td><td>${ts(f.mtime)}</td></tr>`).join("")||`<tr><td colspan="3" class="dim">empty</td></tr>`;
 document.getElementById("finals").innerHTML=mk(d.finals);document.getElementById("cuts").innerHTML=mk(d.cuts);
 document.getElementById("renders").innerHTML=d.renders.map(r=>`<tr><td>${esc(r.project)}</td><td class="wrap">${esc(r.name)}</td><td class="num">${fmt(r.size)}</td><td>${ts(r.mtime)}</td></tr>`).join("")||`<tr><td colspan="4" class="dim">empty</td></tr>`;
 document.getElementById("sub").textContent="LIVE — poll 15s · "+new Date().toLocaleTimeString();
 document.getElementById("sys").textContent="load1 "+d.system.load1+" · db "+d.system.db_mb+"MB";
}).catch(e=>{document.getElementById("sub").textContent="ERR "+e+" — retrying 15s";});
}
load();setInterval(load,15000);
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/status":
            body = json.dumps(snap()).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        elif p == "/":
            body = PAGE.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8890)
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args()
    ThreadingHTTPServer((a.bind, a.port), H).serve_forever()
