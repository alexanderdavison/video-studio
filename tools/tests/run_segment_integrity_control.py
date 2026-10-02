#!/usr/bin/env python3
"""run_segment_integrity_control.py — prove the segment-integrity fixture can actually fail.

The fixture only means something if it catches the defect it was written for. This runner
deliberately reintroduces that defect (the non-chunked path losing its own filename), runs the
fixture, and requires a FAIL; then it restores the renderer and requires a PASS.

SAFETY: production code must never be left mutated by a control run. The original file is backed up,
its sha256 recorded, and the restore happens in a `finally` block - which runs on exceptions, on a
non-zero fixture exit, and on Ctrl-C - and is then VERIFIED by re-hashing. The runner refuses to
proceed if the restore cannot be proven.

Usage: run_segment_integrity_control.py [--renderer PATH] [--fixture PATH]
Exit: 0 = control behaved correctly (fixture failed broken, passed restored), 1 = control failed,
      2 = error (including an unprovable restore).
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time

PY = "/opt/video-studio/tools/venv/bin/python"
RENDERER = "/opt/video-studio/tools/proxy/render_proxy.py"
FIXTURE = "/opt/video-studio/tools/tests/test_render_segment_integrity.py"
# the exact defect (2026-09-15): the non-chunked path reused the last chunk's filename
BROKEN_PAIR = (
    '            continue\n'
    '        seg_path = os.path.join(work, "seg_%s_%02d.mp4" % (cut_label, n))\n'
)
BROKEN_UNFIXED = '            continue\n'


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def run_fixture():
    r = subprocess.run([PY, FIXTURE], capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--renderer", default=RENDERER)
    ap.add_argument("--fixture", default=FIXTURE)
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args()

    src = open(a.renderer).read()
    if src.count(BROKEN_PAIR) != 1:
        print(json.dumps({"ok": False, "error":
                          "renderer does not contain the expected fixed code exactly once "
                          "(%d found) - refusing to run the control on an unexpected file"
                          % src.count(BROKEN_PAIR)}))
        return 2

    backup = "/tmp/render_proxy.py.control_backup.%d" % int(time.time())
    shutil.copy2(a.renderer, backup)
    before_sha = sha256(a.renderer)
    result = {"ok": False, "renderer": a.renderer, "backup": backup,
              "sha_before": before_sha, "sha_after": None}
    try:
        open(a.renderer, "w").write(src.replace(BROKEN_PAIR, BROKEN_UNFIXED))
        print("defect introduced (non-chunked path loses its own filename)")
        rc_broken, out_broken = run_fixture()
        result["rc_with_defect"] = rc_broken
        print("fixture with the defect: exit %d (a FAIL is required here)" % rc_broken)
        bad_lines = [l for l in out_broken.splitlines() if l.strip().startswith("- ")]
        for l in bad_lines[:5]:
            print("   %s" % l.strip())
    finally:
        # ALWAYS restore, then prove it
        shutil.copy2(backup, a.renderer)
        after_sha = sha256(a.renderer)
        result["sha_after"] = after_sha
        if after_sha != before_sha:
            print(json.dumps({"ok": False, "error": "RESTORE NOT PROVEN: %s != %s - the renderer "
                                                   "may be left mutated, restore from %s"
                                                   % (after_sha, before_sha, backup)}))
            return 2
        print("renderer restored and hash-verified (%s)" % after_sha[:16])

    r = subprocess.run([PY, "-m", "py_compile", a.renderer], capture_output=True, text=True)
    result["compiles_after_restore"] = (r.returncode == 0)
    if r.returncode != 0:
        print(json.dumps({"ok": False, "error": "restored renderer does not compile: %s"
                                               % r.stderr[-200:]}))
        return 2

    rc_fixed, out_fixed = run_fixture()
    result["rc_restored"] = rc_fixed
    print("fixture with the restored renderer: exit %d (a PASS is required here)" % rc_fixed)

    ok = (rc_broken != 0) and (rc_fixed == 0)
    result["ok"] = ok
    result["verdict"] = ("CONTROL OK: the fixture fails on the defect and passes without it"
                         if ok else
                         "CONTROL FAILED: the fixture does not discriminate the defect")
    if a.json_out:
        json.dump(result, open(a.json_out, "w"), indent=1)
    print(result["verdict"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
