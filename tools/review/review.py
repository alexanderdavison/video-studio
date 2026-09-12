#!/usr/bin/env python3
"""ISH D Edit Console — single-file review + approve surface for the video editor.

Serves the a la carte menu (template / flexibility / grade / LUFS / toggles),
the proposal receipt (cuts, density, recovery, shot length, runtime, warnings),
and the approve -> proof -> final render loop with a HUNG indicator.

Design: ish-d/creative/decisions/2026-08-28-edit-console-ui-design.md
Decisions (Ish 2026-08-28): LUFS hardcoded -13.5; grade auto-follow only
(neutral deferred); strict un-approve on any config change; tab+log completion;
no per-section knob; in-page video overlay; kill button + HUNG banner.

Runs on LXC 204 with the studio venv (pyyaml available). systemd studio-review.
"""

import http.server
import hashlib
import json
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import yaml

ROOT = "/opt/video-studio"
VENV_PY = os.path.join(ROOT, "tools/venv/bin/python")
ENGINE = os.path.join(ROOT, "tools/template/template_engine.py")
PROXY = os.path.join(ROOT, "tools/proxy/render_proxy.py")
FINAL = os.path.join(ROOT, "tools/render/render_final.py")
DEFS = os.path.join(ROOT, "tools/template/definitions")
PROJECTS = os.path.join(ROOT, "projects")
MEDIA_ROOT = "/mnt/media/raw"
SESSION_PATH = os.path.join(ROOT, "tools/review/session.json")
DB_PATH = os.path.join(ROOT, "studio.db")
RUN_DIR = "/run/studio-review"
PID_FILE = os.path.join(RUN_DIR, "render.pid")

TEMPLATES = {
    "standard": ("club_dispatch_standard_v1", "Standard"),
    "minimal": ("club_dispatch_minimal_v1", "Minimal"),
    "active": ("club_dispatch_active_v1", "Active"),
    "acamonly": ("club_dispatch_acamonly_v1", "A-Cam-Only"),
}
FLEX_LABELS = {"tight": "Tight", "standard": "Standard", "loose": "Loose"}

QUIET_AFTER = 300   # 5 min no file progress -> quiet-but-alive (yellow)
HUNG_AFTER = 600    # 10 min no file progress -> HUNG (red)

DEFAULTS = {
    "config": {
        "template": "standard",
        "flexibility": "standard",
        "grade": "auto",
        "auto_exposure": True,
        "lufs": "-13.5",
        "beat_snap": True,
        "action_pref": True,
        "proof_full": False,
    },
}

CONFIG_DOMAINS = {
    "template": ("minimal", "standard", "active", "acamonly"),
    "flexibility": ("tight", "standard", "loose"),
    "grade": ("auto",),
    "lufs": ("-13.5",),
}
BOOL_KEYS = ("auto_exposure", "beat_snap", "action_pref", "proof_full")

SESSION = {
    "project": None,
    "config": dict(DEFAULTS["config"]),
    "proposal": None,     # {hash, ts, stats, warnings, manifest_path, timeline_path}
    "approval": None,     # {hash, ts}
    "last_proof": None,   # {hash, ok, ts, path}
    "render": None,       # {kind, pid, started, log_path, work_dir, output_path}
}

# ---------------------------------------------------------------- helpers

def load_session():
    if os.path.exists(SESSION_PATH):
        try:
            data = json.load(open(SESSION_PATH))
            SESSION["project"] = data.get("project")
            SESSION["config"].update(data.get("config", {}))
            SESSION["proposal"] = data.get("proposal")
            SESSION["approval"] = data.get("approval")
            SESSION["last_proof"] = data.get("last_proof")
        except Exception as e:
            log("session load failed: %s" % e)


def save_session():
    os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
    tmp = SESSION_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"project": SESSION["project"],
                   "config": SESSION["config"],
                   "proposal": SESSION["proposal"],
                   "approval": SESSION["approval"],
                   "last_proof": SESSION["last_proof"]}, f, indent=2)
    os.replace(tmp, SESSION_PATH)


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def projects_list():
    out = []
    for d in sorted(os.listdir(PROJECTS)):
        p = os.path.join(PROJECTS, d)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, "manifest", "mix.yaml")):
            out.append(d)
    return out


def default_project():
    projs = projects_list()
    if not projs:
        return None
    return "2026-08-25-tester" if "2026-08-25-tester" in projs else projs[0]


def mix_path(project):
    return os.path.join(PROJECTS, project, "manifest", "mix.yaml")


def config_hash(project, config):
    canon = json.dumps({"project": project, "config": config}, sort_keys=True)
    return hashlib.sha1(canon.encode()).hexdigest()[:8]


def template_info(template):
    name, label = TEMPLATES.get(template, TEMPLATES["standard"])
    try:
        d = yaml.safe_load(open(os.path.join(DEFS, name + ".yaml")))
    except Exception:
        return {"label": label, "density_band": None, "min_b": None, "max_b": None,
                "min_rec": None, "lufs": None}
    band = d.get("b_camera_density_pct")
    edit = d.get("editing", {})
    return {"label": label, "density_band": band,
            "min_b": edit.get("minimum_b_shot"), "max_b": edit.get("maximum_b_shot"),
            "min_rec": edit.get("minimum_a_recovery"),
            "lufs": d.get("delivery", {}).get("lufs")}


def analysis_files(project):
    a = os.path.join(PROJECTS, project, "work", "analysis")
    return {
        "beats": os.path.join(a, "a_full_beats.json"),
        "b_beats": os.path.join(a, "b_reel_beats.json"),
        "b_profile": os.path.join(a, "b_reel_profile.json"),
    }


def stats_from_manifest(path):
    """Receipt numbers from a generated manifest (matches engine policy math)."""
    d = yaml.safe_load(open(path))
    cuts = d.get("timeline", [])
    if not cuts:
        return None
    b = [c for c in cuts if c["angle"] == "B"]
    a = [c for c in cuts if c["angle"] == "A"]
    total = sum(c["source_out"] - c["source_in"] for c in cuts)
    bdur = sum(c["source_out"] - c["source_in"] for c in b)
    density = 100.0 * bdur / total if total else 0.0
    bd = [c["source_out"] - c["source_in"] for c in b]
    rec = []
    pb = None
    for c in cuts:
        if c["angle"] == "B":
            pb = c["source_out"]
        elif pb is not None:
            rec.append(c["source_in"] - pb)
            pb = None
    return {
        "cuts": len(cuts),
        "b_cuts": len(b),
        "b_density_pct": round(density, 1),
        "b_shot_min": round(min(bd), 1) if bd else None,
        "b_shot_max": round(max(bd), 1) if bd else None,
        "b_shot_avg": round(sum(bd) / len(bd), 1) if bd else None,
        "recovery_min": round(min(rec), 1) if rec else None,
        "runtime_s": int(max(c["source_out"] for c in cuts)),
        "job_id": d.get("job_id"),
    }


# ---------------------------------------------------------------- propose

def run_propose():
    project = SESSION["project"]
    cfg = SESSION["config"]
    tmpl, _ = TEMPLATES[cfg["template"]]
    cmd = [VENV_PY, ENGINE, "--template", tmpl,
           "--mix", mix_path(project), "--propose", "--flex", cfg["flexibility"],
           "--out", os.path.join(PROJECTS, project, "work", "console_manifest.yaml"),
           "--out-timeline", os.path.join(PROJECTS, project, "work", "console_timeline.yaml"),
           "--media-root", MEDIA_ROOT, "--json"]
    af = analysis_files(project)
    if cfg["beat_snap"]:
        if os.path.exists(af["beats"]):
            cmd += ["--beats", af["beats"]]
        if os.path.exists(af["b_beats"]):
            cmd += ["--b-beats", af["b_beats"]]
    if cfg["action_pref"] and os.path.exists(af["b_profile"]):
        cmd += ["--b-profile", af["b_profile"]]
    log("propose: %s" % " ".join(cmd))
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "propose timed out after 300s"}
    out = (r.stdout or "").strip()
    last_json = None
    for line in out.splitlines():
        if line.startswith("{"):
            last_json = line
    if r.returncode != 0 or not last_json:
        return {"ok": False, "error": (r.stderr or out or "engine failed").strip()[-500:]}
    try:
        res = json.loads(last_json)
    except Exception:
        return {"ok": False, "error": "engine returned unparseable output"}
    if not res.get("ok"):
        return {"ok": False, "error": json.dumps(res)}
    manifest_path = res["manifest"]
    stats = stats_from_manifest(manifest_path)
    if stats is None:
        return {"ok": False, "error": "proposal produced an empty timeline"}
    h = config_hash(project, cfg)
    SESSION["proposal"] = {
        "hash": h, "ts": time.time(),
        "stats": stats,
        "warnings": res.get("warnings", []),
        "manifest_path": manifest_path,
        "timeline_path": os.path.join(PROJECTS, project, "work", "console_timeline.yaml"),
    }
    _stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(SESSION["proposal"]["ts"]))
    try:
        _snap = _snapshot(manifest_path,
                          os.path.join(PROJECTS, project, "manifest", "proposals"),
                          "proposal", _stamp, h[:8])
        SESSION["proposal"]["proposal_snapshot"] = _snap
        SESSION["proposal"]["proposal_sha256"] = _sha256(_snap)
    except Exception as e:
        SESSION["proposal"]["proposal_snapshot"] = None
        SESSION["proposal"]["proposal_snapshot_error"] = str(e)
    save_session()
    return {"ok": True, "stats": stats, "warnings": res.get("warnings", []),
            "hash": h}


# ---------------------------------------------------------------- db / approval

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS console_approvals "
              "(hash TEXT PRIMARY KEY, ts REAL, flags TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS approval_timelines ("
              "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
              "  approval_hash TEXT, ts REAL, project TEXT, job_id TEXT,"
              "  template TEXT, policy_version TEXT, config_flags TEXT,"
              "  proposal_path TEXT, proposal_sha256 TEXT,"
              "  approved_path TEXT, approved_sha256 TEXT, snapshot_path TEXT)")
    return c


def _sha256(path):
    """sha256 of a file, streamed — manifests are small but never guess at size."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(src, dest_dir, label, stamp, h8):
    """Immutable copy of a timeline artifact. Never overwrites: the filename carries
    the timestamp and config hash, and re-approving the same config writes a new file
    (history is the point)."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "%s_%s_%s.yaml" % (stamp, h8, label))
    shutil.copy2(src, dest)
    return dest


def _policy_version(template):
    """Content hash of the template definition = the policy that was in force."""
    if not template:
        return None
    path = os.path.join(ROOT, "tools", "template", "definitions", "%s.yaml" % template)
    if os.path.isfile(path):
        return _sha256(path)
    reg = os.path.join(ROOT, "tools", "manifest", "profile_registry.json")
    if os.path.isfile(reg):
        return _sha256(reg)
    return None


def _manifest_job_id(path):
    try:
        with open(path) as fh:
            return (yaml.safe_load(fh) or {}).get("job_id")
    except Exception:
        return None


def approve_current():
    """Record the approval AND bind it to the exact timeline that was reviewed.

    X = the engine proposal snapshot captured by run_propose()
    Y = the timeline the reviewer was looking at when they hit approve
    Both hashes are stored, so "proposed X, reviewed Y, approved Y" is auditable and
    an approval can never again be attributed to an unknown timeline.
    """
    cfg = SESSION["config"]
    project = SESSION["project"]
    h = config_hash(project, cfg)
    prop = SESSION["proposal"] or {}
    ts = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
    h8 = h[:8]

    man_dir = os.path.join(PROJECTS, project, "manifest")
    approved_path = prop.get("manifest_path")
    approved_sha, snap_approved = None, None
    if approved_path and os.path.isfile(approved_path):
        snap_approved = _snapshot(approved_path,
                                  os.path.join(man_dir, "approved"), "approved", stamp, h8)
        approved_sha = _sha256(snap_approved)

    proposal_path = prop.get("proposal_snapshot")
    proposal_sha = prop.get("proposal_sha256")
    if proposal_path and os.path.isfile(proposal_path) and not proposal_sha:
        proposal_sha = _sha256(proposal_path)

    template = cfg.get("template")
    job_id = prop.get("job_id") or _manifest_job_id(approved_path) or project

    c = db()
    c.execute("INSERT OR REPLACE INTO console_approvals (hash, ts, flags) VALUES (?,?,?)",
              (h, ts, json.dumps(cfg)))
    c.execute("INSERT INTO approval_timelines (approval_hash, ts, project, job_id, template,"
              " policy_version, config_flags, proposal_path, proposal_sha256,"
              " approved_path, approved_sha256, snapshot_path)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (h, ts, project, job_id, template, _policy_version(template),
               json.dumps(cfg), proposal_path, proposal_sha,
               approved_path, approved_sha, snap_approved))
    c.commit()
    c.close()
    SESSION["approval"] = {"hash": h, "ts": ts, "approved_sha256": approved_sha,
                           "snapshot": snap_approved, "proposal_sha256": proposal_sha}
    save_session()


# ---------------------------------------------------------------- render

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def render_running():
    return SESSION.get("render") is not None and pid_alive(SESSION["render"]["pid"])


def start_render(kind):
    project = SESSION["project"]
    prop = SESSION["proposal"]
    job_id = prop["stats"].get("job_id") or "job"
    work = os.path.join(PROJECTS, project, "work", "console_render_%s" % kind)
    os.makedirs(work, exist_ok=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    log_path = os.path.join(work, "render.log")
    out_path = None
    if kind == "proof":
        out_path = "/mnt/media/proofs/%s_proof.mp4" % job_id
        cmd = [VENV_PY, PROXY, prop["manifest_path"], "--media-root", MEDIA_ROOT,
               "--work", work]
        if SESSION["config"]["proof_full"]:
            cmd.append("--full")
    else:
        out_path = "/mnt/media/finals/%s.mp4" % job_id
        cmd = [VENV_PY, FINAL, prop["manifest_path"], "--media-root", MEDIA_ROOT,
               "--work", work, "--force"]
    if not SESSION["config"]["auto_exposure"]:
        cmd += ["--no-auto-pre-grade"]
    cmd.append("--json")
    log("render %s: %s" % (kind, " ".join(cmd)))
    if os.path.exists(PID_FILE):
        try:
            old = int(open(PID_FILE).read().strip())
            if pid_alive(old):
                return {"ok": False, "error": "a render is already running (pid %d)" % old}
        except (ValueError, OSError):
            pass
    f = open(log_path, "w")
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                         start_new_session=True)
    open(PID_FILE, "w").write(str(p.pid))
    SESSION["render"] = {"kind": kind, "pid": p.pid, "started": time.time(),
                         "log_path": log_path, "work_dir": work,
                         "output_path": out_path}
    return {"ok": True, "pid": p.pid}


def newest_mtime(paths):
    newest = 0.0
    for p in paths:
        if os.path.exists(p):
            m = os.path.getmtime(p)
            if m > newest:
                newest = m
    return newest


def render_state():
    r = SESSION.get("render")
    if r is None:
        return {"running": False}
    pid = r["pid"]
    alive = pid_alive(pid)
    work = r["work_dir"]
    out = r["output_path"]
    log_tail = ""
    try:
        with open(r["log_path"]) as f:
            tail = f.read()[-4000:]
        log_tail = tail[-1200:]
    except Exception:
        tail = ""
    progress = None
    m = re.findall(r"(?:cut|segment) (\d+)/(\d+)", tail)
    if m:
        progress = "cut %s/%s" % m[-1]
    else:
        m = re.findall(r"(\d+(?:\.\d+)?)%", tail)
        if m:
            progress = "%s%%" % m[-1]
    state = "running"
    elapsed = int(time.time() - r["started"])
    last_progress = None
    if alive:
        mtime = newest_mtime([out] + [os.path.join(work, x) for x in
                                      os.listdir(work) if not x.endswith(".log")])
        if mtime:
            last_progress = int(time.time() - mtime)
            if last_progress >= HUNG_AFTER:
                state = "hung"
            elif last_progress >= QUIET_AFTER:
                state = "quiet"
    else:
        done = '"ok": true' in tail
        ok_file = os.path.exists(out) and os.path.getsize(out) > 0
        if done and ok_file:
            state = "done"
        else:
            state = "failed"
        if r["kind"] == "proof":
            SESSION["last_proof"] = {"hash": SESSION["proposal"]["hash"] if SESSION["proposal"] else None,
                                     "ok": state == "done",
                                     "ts": time.time(), "path": out if os.path.exists(out) else None}
            save_session()
    return {"running": alive, "kind": r["kind"], "pid": pid,
            "started": r["started"], "state": state,
            "elapsed_s": elapsed, "last_progress_s": last_progress,
            "progress": progress, "log_tail": log_tail,
            "output_path": out if os.path.exists(out) else None,
            "started_ts": time.strftime("%H:%M:%S", time.localtime(r["started"]))}


def kill_render():
    r = SESSION.get("render")
    if r is None:
        return {"ok": False, "error": "no render running"}
    pid = r["pid"]
    if pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for _ in range(10):
            if not pid_alive(pid):
                break
            time.sleep(1)
        if pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    SESSION["render"] = None
    return {"ok": True}


def api_state():
    projs = projects_list()
    if SESSION["project"] is None:
        SESSION["project"] = default_project()
        if SESSION["project"] is None:
            return {"ok": False, "error": "no projects found"}
    cfg = SESSION["config"]
    ch = config_hash(SESSION["project"], cfg)
    prop = SESSION["proposal"]
    proposal = None
    dirty = False
    if prop:
        dirty = prop["hash"] != ch
        proposal = {"present": True, "hash": prop["hash"],
                    "ts": prop["ts"], "stats": prop["stats"],
                    "warnings": prop["warnings"],
                    "manifest_path": prop["manifest_path"],
                    "timeline_path": prop["timeline_path"],
                    "dirty": dirty,
                    "ts_str": time.strftime("%H:%M:%S", time.localtime(prop["ts"]))}
    approved = bool(prop and SESSION["approval"] and
                    SESSION["approval"]["hash"] == prop["hash"] and not dirty)
    tinfo = template_info(cfg["template"])
    return {
        "ok": True,
        "projects": projs,
        "project": SESSION["project"],
        "mix": os.path.basename(mix_path(SESSION["project"])),
        "config": cfg,
        "config_hash": ch,
        "template_info": tinfo,
        "proposal": proposal,
        "approved": approved,
        "approval": SESSION["approval"],
        "last_proof": SESSION["last_proof"],
        "render": render_state(),
        "dirty": dirty,
    }


# ---------------------------------------------------------------- validation helpers

VIDEO_ROOTS = ("/mnt/media/proofs/", "/mnt/media/finals/")


def resolve_video_path(path):
    """Resolve a client-supplied video path; return the real path only if it
    stays inside the allowed media roots.  Symlinks and '..' components are
    resolved first so the containment check runs on the FINAL path, not the
    raw string (a startswith guard on the raw string is bypassable via ../)."""
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    try:
        resolved = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    for root in VIDEO_ROOTS:
        if resolved.startswith(root):
            return resolved
    return None


def apply_config(body):
    """Validate + apply a config update. Each key is validated against its own
    domain: the old shared union whitelist accepted e.g. template=tight, which
    then crashed /api/propose with KeyError('tight'). Returns None on success
    or an error dict if any supplied key is out of domain."""
    cfg = SESSION["config"]
    for k, allowed in CONFIG_DOMAINS.items():
        if k in body:
            v = body[k]
            if not isinstance(v, str) or v not in allowed:
                return {"ok": False, "error": "invalid value for %s: %r" % (k, v)}
            cfg[k] = v
    for k in BOOL_KEYS:
        if k in body:
            v = body[k]
            if not isinstance(v, bool):
                return {"ok": False, "error": "invalid value for %s: %r" % (k, v)}
            cfg[k] = v
    return None


# ---------------------------------------------------------------- http

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "StudioReview/1.0"

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            return self._html(PAGE)
        if parsed.path == "/api/state":
            return self._json(api_state())
        if parsed.path == "/api/video":
            q = urllib.parse.parse_qs(parsed.query)
            path = (q.get("path") or [""])[0]
            resolved = resolve_video_path(path)
            if resolved is None:
                return self._json({"ok": False, "error": "forbidden"}, 403)
            if not os.path.isfile(resolved):
                return self._json({"ok": False, "error": "no such file"}, 404)
            return self._serve_video(resolved)
        return self._json({"ok": False, "error": "not found"}, 404)

    def _serve_video(self, path):
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start_s, _, end_s = rng[6:].partition("-")
            try:
                start = int(start_s or 0)
            except ValueError:
                start = 0
            end = int(end_s) if end_s else size - 1
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            start, end, length = 0, size - 1, size
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        if p == "/api/config":
            if render_running():
                return self._json({"ok": False, "error": "menu locked while a render is running"})
            body = self._read_json()
            err = apply_config(body)
            if err:
                return self._json(err, 400)
            save_session()
            return self._json({"ok": True, "dirty": api_state()["dirty"]})
        if p == "/api/propose":
            if render_running():
                return self._json({"ok": False, "error": "locked while a render is running"})
            return self._json(run_propose())
        if p == "/api/approve":
            if render_running():
                return self._json({"ok": False, "error": "locked while a render is running"})
            if SESSION["proposal"] is None:
                return self._json({"ok": False, "error": "no proposal to approve"})
            approve_current()
            return self._json({"ok": True, "approved": True})
        if p == "/api/render":
            body = self._read_json()
            kind = body.get("kind")
            if kind not in ("proof", "final"):
                return self._json({"ok": False, "error": "kind must be proof or final"})
            st = api_state()
            if render_running():
                return self._json({"ok": False, "error": "a render is already running"})
            if not st["proposal"]:
                return self._json({"ok": False, "error": "no proposal"})
            if not st["approved"]:
                return self._json({"ok": False, "error": "approve the current config first"})
            if kind == "final":
                lp = SESSION["last_proof"]
                if not (lp and lp.get("ok") and lp.get("hash") == st["proposal"]["hash"]):
                    return self._json({"ok": False,
                                       "error": "a completed proof for this exact config is required before final"})
            return self._json(start_render(kind))
        if p == "/api/kill":
            return self._json(kill_render())
        return self._json({"ok": False, "error": "not found"}, 404)


# ---------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ISH D EDIT CONSOLE</title>
<style>
:root{
  --ink:#0E1116; --midnight:#1B2438; --panel:#141A26; --line:#232C3E;
  --cream:#F0E6D2; --muted:#8A8F9E; --teal:#4E7A7A; --lavender:#9D8FC0;
  --green:#6EE77E; --orange:#C4551E; --red:#C93B3B;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{background:var(--ink);color:var(--cream);font-family:var(--sans);-webkit-font-smoothing:antialiased;display:grid;grid-template-rows:auto 1fr auto}
header{display:flex;align-items:baseline;gap:18px;padding:14px 26px;border-bottom:1px solid var(--line)}
.brand{font-size:22px;font-weight:800;letter-spacing:1px}
.brand .d{color:var(--teal)}
.brand .bars{color:var(--teal);font-weight:400}
.sub{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase}
.spacer{flex:1}
.badge{font-family:var(--mono);font-size:11px;letter-spacing:1px;padding:4px 10px;border:1px solid var(--line);border-radius:4px}
.badge.none{color:var(--muted)}
.badge.dirty{color:var(--orange);border-color:var(--orange)}
.badge.current{color:var(--green);border-color:var(--green)}
select{background:var(--midnight);color:var(--cream);border:1px solid var(--line);border-radius:4px;padding:6px 8px;font-family:var(--mono);font-size:13px;width:100%}
select:disabled{color:var(--muted)}
main{display:grid;grid-template-columns:340px 1fr;gap:0;min-height:0}
.col{border-right:1px solid var(--line);padding:18px 24px;overflow-y:auto;min-height:0}
.col:last-child{border-right:0}
.sec{margin-bottom:18px}
.sec .lab{font-family:var(--mono);font-size:10px;letter-spacing:2px;color:var(--muted);text-transform:uppercase;margin-bottom:7px}
.field{margin-bottom:10px}
.field label{display:block;font-size:13px;color:var(--cream);margin-bottom:4px}
.radios{display:flex;gap:8px}
.radios label{display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer;border:1px solid var(--line);border-radius:4px;padding:6px 10px;color:var(--muted)}
.radios label.sel{color:var(--cream);border-color:var(--teal)}
.radios input{accent-color:var(--teal)}
.check{display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:8px;cursor:pointer}
.check input{accent-color:var(--teal);width:16px;height:16px}
.hint{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:2px}
.receipt{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px 14px}
.card .k{font-family:var(--mono);font-size:10px;letter-spacing:1px;color:var(--muted);text-transform:uppercase;margin-bottom:6px}
.card .v{font-family:var(--mono);font-size:24px;font-weight:700;color:var(--cream)}
.card .v.small{font-size:15px;line-height:1.5}
.card .ok{color:var(--green)} .card .warn{color:var(--orange)} .card .bad{color:var(--red)}
.warns{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 14px;margin-bottom:14px}
.warns .title{font-family:var(--mono);font-size:11px;letter-spacing:1px;color:var(--orange);margin-bottom:6px}
.warns ul{list-style:none}
.warns li{font-family:var(--mono);font-size:12px;color:var(--muted);padding:2px 0;border-bottom:1px dashed var(--line)}
.warns li:last-child{border-bottom:0}
.meta{font-family:var(--mono);font-size:11px;color:var(--muted);line-height:1.8}
.meta b{color:var(--lavender);font-weight:600}
.apprline{font-family:var(--mono);font-size:12px;margin-top:10px}
.apprline .yes{color:var(--green)} .apprline .no{color:var(--orange)}
footer{display:flex;align-items:center;gap:10px;padding:12px 26px;border-top:1px solid var(--line);background:var(--panel)}
button{background:var(--midnight);color:var(--cream);border:1px solid var(--line);border-radius:4px;font-family:var(--mono);font-size:12px;letter-spacing:1px;padding:10px 14px;cursor:pointer;font-weight:600}
button:hover:not(:disabled){border-color:var(--teal)}
button:disabled{opacity:.35;cursor:not-allowed}
button.primary{background:var(--teal);border-color:var(--teal);color:#fff}
button.danger{background:var(--red);border-color:var(--red);color:#fff}
.status{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--muted);text-align:right;line-height:1.5}
.status .live{color:var(--green)}
.status .quiet{color:var(--orange)}
.hungbar{background:var(--red);color:#fff;font-family:var(--mono);font-size:13px;font-weight:700;letter-spacing:1px;padding:8px 26px;text-align:center;animation:blink 1.2s infinite}
@keyframes blink{50%{opacity:.55}}
#overlay{position:fixed;inset:0;background:rgba(5,8,12,.96);display:none;flex-direction:column;z-index:50}
#overlay.on{display:flex}
#overlay .top{display:flex;justify-content:flex-end;padding:14px 22px}
#overlay video{flex:1;width:100%;height:100%;object-fit:contain}
</style>
</head>
<body>
<header>
  <div class="brand">ISH <span class="d">D</span> <span class="bars">▓▓▓</span> <span class="sub">EDIT CONSOLE</span></div>
  <div class="spacer"></div>
  <div class="sub">SOURCE</div>
  <span id="source-label" style="font-family:var(--mono);font-size:12px;color:var(--teal);letter-spacing:1px">—</span>
  <div class="badge none" id="badge">—</div>
</header>
<div id="hungbar" class="hungbar" style="display:none">⚠ RENDER HUNG — no output progress for 10m</div>
<main>
  <div class="col">
    <div class="sec">
      <div class="lab">A La Carte</div>
      <div class="field"><label>TEMPLATE</label>
        <select id="cfg-template">
          <option value="minimal">Minimal — B 10-15%</option>
          <option value="standard">Standard — B 20-25%</option>
          <option value="active">Active — B 30-35%</option>
          <option value="acamonly">A-Cam-Only — B 0%</option>
        </select>
      </div>
      <div class="field"><label>FLEXIBILITY</label>
        <div class="radios" id="cfg-flex">
          <label><input type="radio" name="flex" value="tight">Tight</label>
          <label><input type="radio" name="flex" value="standard">Standard</label>
          <label><input type="radio" name="flex" value="loose">Loose</label>
        </div>
        <div class="hint">tight: B 6-10s · standard: template clamps · loose: +6% density</div>
      </div>
      <div class="field"><label>GRADE</label>
        <select id="cfg-grade">
          <option value="auto">Auto — PB3 + B shadow-lift</option>
          <option value="neutral" disabled>Neutral — coming soon</option>
        </select>
      </div>
      <div class="field"><label class="check"><input type="checkbox" id="cfg-autoex"> Auto-exposure match (pre-grade)</label></div>
      <div class="field"><label>LUFS</label>
        <select id="cfg-lufs">
          <option value="-13.5">-13.5 club/YouTube</option>
          <option value="-14" disabled>-14 streaming — coming soon</option>
          <option value="-16" disabled>-16 broadcast — coming soon</option>
        </select>
      </div>
      <div class="field"><label>TOGGLES</label>
        <label class="check"><input type="checkbox" id="cfg-beatsnap"> Beat/phrase snap</label>
        <label class="check"><input type="checkbox" id="cfg-actionpref"> Action preference</label>
        <label class="check"><input type="checkbox" id="cfg-prooffull"> Full timeline proof <span class="hint">(unchecked = half-runtime)</span></label>
      </div>
    </div>
  </div>
  <div class="col">
    <div class="lab" style="font-family:var(--mono);font-size:10px;letter-spacing:2px;color:var(--muted);text-transform:uppercase;margin-bottom:12px">Proposal Summary</div>
    <div class="receipt">
      <div class="card"><div class="k">Cuts</div><div class="v" id="r-cuts">—</div></div>
      <div class="card"><div class="k">B-Cam Density</div><div class="v small" id="r-density">—</div></div>
      <div class="card"><div class="k">A-Cam Recovery</div><div class="v small" id="r-recovery">—</div></div>
      <div class="card"><div class="k">B-Shot Length</div><div class="v small" id="r-shot">—</div></div>
      <div class="card"><div class="k">Runtime</div><div class="v" id="r-runtime">—</div></div>
      <div class="card"><div class="k">State</div><div class="v small" id="r-state">no proposal</div></div>
    </div>
    <div class="warns" id="r-warns" style="display:none">
      <div class="title">⚠ WARNINGS</div><ul id="r-warns-list"></ul>
    </div>
    <div class="meta">
      <div>LAST PROPOSED <b id="r-ts">—</b></div>
      <div>CONFIG HASH <b id="r-hash">—</b></div>
      <div>TEMPLATE <b id="r-tmpl">—</b></div>
      <div>LUFS <b id="r-lufs">-13.5</b></div>
    </div>
    <div class="apprline" id="r-approval">APPROVAL <span class="no">✗ not approved for this config</span></div>
  </div>
</main>
<footer>
  <button id="b-regenerate">REGENERATE</button>
  <button id="b-approve">APPROVE</button>
  <button id="b-proof">RENDER PROOF</button>
  <button id="b-watch">WATCH PROOF</button>
  <button id="b-final">RENDER FINAL</button>
  <button id="b-kill" class="danger" style="display:none">KILL RENDER</button>
  <div class="status" id="status">idle · no render running</div>
</footer>
<div id="overlay">
  <div class="top"><button id="close-video">✕ CLOSE</button></div>
  <video id="player" controls></video>
</div>
<script>
let state=null, poll=2000, locked=false;
const $=id=>document.getElementById(id);
const fmt=s=>{const m=Math.floor(s/60),x=Math.round(s%60);return m+"m "+String(x).padStart(2,"0")+"s"};
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function collect(){
  return {template:$("cfg-template").value, flexibility:document.querySelector("input[name=flex]:checked").value,
          grade:$("cfg-grade").value, auto_exposure:$("cfg-autoex").checked,
          lufs:$("cfg-lufs").value, beat_snap:$("cfg-beatsnap").checked,
          action_pref:$("cfg-actionpref").checked, proof_full:$("cfg-prooffull").checked};
}
function setControl(id,val){
  const el=$(id);
  if(document.activeElement!==el){ el.value=val; }
}
function render(s){
  locked = !!s.render.running;
  const c=s.config;
  setControl("cfg-template",c.template); setControl("cfg-grade",c.grade);
  setControl("cfg-lufs",c.lufs);
  $("cfg-autoex").checked=c.auto_exposure; $("cfg-beatsnap").checked=c.beat_snap;
  $("cfg-actionpref").checked=c.action_pref; $("cfg-prooffull").checked=c.proof_full;
  document.querySelectorAll("input[name=flex]").forEach(r=>{r.checked=r.value===c.flexibility;r.closest("label").classList.toggle("sel",r.checked);});
  $("source-label").textContent=s.project||"—";
  const b=$("badge");
  if(s.proposal){ b.className="badge "+(s.proposal.dirty?"dirty":"current"); b.textContent=s.proposal.dirty?"PROPOSAL DIRTY":"PROPOSAL CURRENT"; }
  else { b.className="badge none"; b.textContent="NO PROPOSAL"; }
  const p=s.proposal;
  $("r-cuts").textContent=p?p.stats.cuts:"—";
  $("r-density").textContent=p?(p.stats.b_density_pct+"% · target "+s.template_info.density_band+"%"):"—";
  $("r-recovery").textContent=p?(p.stats.recovery_min+"s · min "+(s.template_info.min_rec||8)+"s"):"—";
  $("r-shot").textContent=p?(p.stats.b_shot_min+"-"+p.stats.b_shot_max+"s · avg "+p.stats.b_shot_avg):"—";
  $("r-runtime").textContent=p?fmt(p.stats.runtime_s):"—";
  $("r-state").textContent=p?(p.dirty?"dirty — regenerate":"current"):"no proposal";
  $("r-state").className="v small "+(p&&!p.dirty?"ok":"warn");
  $("r-ts").textContent=p?p.ts_str:"—";
  $("r-hash").textContent=p?p.hash:"—";
  $("r-tmpl").textContent=s.template_info.label+" / "+{tight:"Tight",standard:"Standard",loose:"Loose"}[c.flexibility];
  $("r-lufs").textContent=s.template_info.lufs??c.lufs;
  const w=p?p.warnings:[];
  $("r-warns").style.display=w.length?"block":"none";
  $("r-warns-list").innerHTML=w.map(x=>`<li>${esc(x)}</li>`).join("");
  $("r-approval").innerHTML=s.approved?'APPROVAL <span class="yes">✓ approved '+esc(p.hash)+'</span>':'APPROVAL <span class="no">✗ not approved for this config</span>';
  const any=locked;
  $("b-regenerate").disabled=any;
  $("b-approve").disabled=any||!p||p.dirty;
  $("b-proof").disabled=any||!s.approved;
  $("b-final").disabled=any||!s.approved||!(s.last_proof&&s.last_proof.ok&&s.last_proof.hash===p?.hash);
  const r=s.render;
  const st=$("status");
  if(r.running){
    $("b-kill").style.display="inline-block";
    const el=fmt(r.elapsed_s);
    const prog=r.progress?(" · "+r.progress):"";
    const lp=r.last_progress_s!=null?(" · last progress "+r.last_progress_s+"s ago"):"";
    const cls=r.state==="hung"?"quiet":(r.state==="quiet"?"quiet":"live");
    st.innerHTML=`<span class="${cls}">RENDERING ${(r.kind||"PROOF").toUpperCase()} · elapsed ${el}${prog}${lp}</span><br>${esc(r.log_tail.split("\\n").slice(-2).join(" / "))}`;
    $("hungbar").style.display=r.state==="hung"?"block":"none";
    $("b-watch").disabled=true;
  } else {
    $("b-kill").style.display="none";
    $("hungbar").style.display="none";
    const lp=s.last_proof;
    $("b-watch").disabled=!(lp&&lp.ok&&lp.path);
    if(r.state==="done"){ st.innerHTML=`<span class="live">${(r.kind||"").toUpperCase()} DONE · ${esc(r.output_path||"")}</span>`; }
    else if(r.state==="failed"){ st.innerHTML=`<span class="quiet">${(r.kind||"").toUpperCase()} FAILED — check log</span><br>${esc(r.log_tail.split("\\n").slice(-3).join(" / "))}`; }
    else { st.innerHTML="idle · no render running"; }
  }
  if(locked){ document.querySelectorAll("select,input").forEach(e=>{e.disabled=true;}); }
  else { document.querySelectorAll("select,input").forEach(e=>{e.disabled=e.id==="cfg-grade"&&e.value==="neutral"||e.id==="cfg-lufs"&&e.value!=="-13.5"||false;}); }
}
async function load(){ try{ const r=await fetch("/api/state"); state=await r.json(); if(state.ok) render(state); }catch(e){} setTimeout(load,poll); }
async function post(path,body){ try{ const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})}); const j=await r.json(); if(!j.ok) alert(j.error||"request failed"); await load(); }catch(e){ alert("request failed"); } }
function changed(){ post("/api/config",collect()); }
["cfg-template","cfg-grade","cfg-lufs"].forEach(id=>$(id).addEventListener("change",changed));
["cfg-autoex","cfg-beatsnap","cfg-actionpref","cfg-prooffull"].forEach(id=>$(id).addEventListener("change",changed));
document.querySelectorAll("input[name=flex]").forEach(r=>r.addEventListener("change",changed));
$("b-regenerate").addEventListener("click",()=>post("/api/propose"));
$("b-approve").addEventListener("click",()=>post("/api/approve"));
$("b-proof").addEventListener("click",()=>post("/api/render",{kind:"proof"}));
$("b-final").addEventListener("click",()=>{ if(confirm("Final render ~1h45m, locks the box, one at a time — proceed?")) post("/api/render",{kind:"final"}); });
$("b-watch").addEventListener("click",()=>{ const p=state?.last_proof?.path; if(p){ $("player").src="/api/video?path="+encodeURIComponent(p); $("overlay").classList.add("on"); $("player").play(); } });
$("close-video").addEventListener("click",()=>{ $("player").pause(); $("player").src=""; $("overlay").classList.remove("on"); });
$("b-kill").addEventListener("click",()=>{ if(confirm("Kill the running render?")) post("/api/kill"); });
load();
</script>
</body>
</html>
"""


def main():
    os.makedirs(RUN_DIR, exist_ok=True)
    load_session()
    if SESSION["project"] is None:
        SESSION["project"] = default_project()
    if os.path.exists(PID_FILE):  # stale lock from a previous boot/crash
        try:
            old = int(open(PID_FILE).read().strip())
            if not pid_alive(old):
                os.remove(PID_FILE)
        except (ValueError, OSError):
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
    port = int(os.environ.get("STUDIO_REVIEW_PORT", "8891"))
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log("ISH D Edit Console listening on :%d (project=%s)" % (port, SESSION["project"]))
    srv.serve_forever()


if __name__ == "__main__":
    main()
