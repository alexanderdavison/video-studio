#!/usr/bin/env python3
"""Increment-3 fail-closed + determinism tests for build_evidence_package.py.

Every failure case must exit non-zero AND write no package. Two healthy runs built into
differently-named PARENT directories must be byte-identical (the pack records its own
basename, so the basename must match; the index is location-independent).
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys

PY = "/opt/video-studio/tools/venv/bin/python"
BLD = "/opt/video-studio/tools/analysis/build_evidence_package.py"
CAND = "/opt/video-studio/tools/analysis/p1_out/sync_300_420.json"
AN = "/opt/video-studio/projects/2026-08-25-tester/work/analysis"
GRADE = AN + "/test1_grade/camera_normalization_final.json"
CAND1 = AN + "/editorial_pkg_300_600/matched_ab_evidence"
cand = json.load(open(CAND))
results = []


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def build(cpath, out, media=None):
    shutil.rmtree(out, ignore_errors=True)
    cmd = [PY, BLD, "--candidates", cpath, "--out", out, "--grade-json", GRADE,
           "--fps", "2", "--section", "300", "600", "--prompt-from", "/tmp/pkg_prompt_src"]
    if media:
        cmd += ["--media-root", media]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r, os.path.exists(out + "/evidence_pack.json")


def expect_closed(label, r, wrote):
    ok = r.returncode != 0 and not wrote
    results.append(ok)
    line = [l for l in r.stderr.strip().split("\n") if l.strip()]
    print("%-46s rc=%d package_written=%-5s %s" % (label, r.returncode, wrote,
                                                   "FAIL-CLOSED OK" if ok else "*** NOT CLOSED"))
    if line:
        print("        %s" % line[-1][:150])
    return ok


print("=== fail-closed ===")
# a. malformed mapping: a B candidate moved 5 s in source without moving the timeline
d = json.loads(json.dumps(cand))
d["events"][0]["candidates"][1]["start"] += 5.0
p = "/tmp/tfc_malformed.json"
json.dump(d, open(p, "w"))
expect_closed("a. malformed mapping (sync invariant broken)",
              *build(p, "/tmp/out_malformed"))

# b. a_offset declared inconsistently with the candidates -> A and B would disagree
d = json.loads(json.dumps(cand))
d["inputs"]["offsets"]["a_offset"] = 1.5
p = "/tmp/tfc_aoff.json"
json.dump(d, open(p, "w"))
expect_closed("b. mis-declared a_offset (A/B would disagree)",
              *build(p, "/tmp/out_aoff"))

# c. missing A evidence: media root whose A file is unreadable
root = "/tmp/tfc_media"
shutil.rmtree(root, ignore_errors=True)
os.makedirs(root + "/A CAM")
os.makedirs(root + "/B CAM")
open(root + "/A CAM/DJI_20260824204625_0054_D.MP4", "wb").write(b"not a video")
for b in os.listdir("/mnt/media/raw/B CAM"):
    os.symlink("/mnt/media/raw/B CAM/" + b, root + "/B CAM/" + b)
expect_closed("c. missing/unreadable A evidence", *build(CAND, "/tmp/out_noA", media=root))

# d. non-sync-anchored artifact must be refused outright
r, wrote = build("/opt/video-studio/tools/analysis/p1_out/candidates_300_420.json",
                 "/tmp/out_nosync")
ok = r.returncode == 2 and not wrote
results.append(ok)
print("%-46s rc=%d package_written=%-5s %s" % ("d. non-sync-anchored artifact", r.returncode,
                                               wrote, "REFUSED OK" if ok else "*** NOT REFUSED"))

print("\n=== determinism (two healthy builds, same basename) ===")
r2, wrote2 = build(CAND, "/tmp/matched_run2/matched_ab_evidence")
if not wrote2:
    print("*** run 2 failed to build:\n%s" % r2.stderr[-600:])
    results.append(False)
    sys.exit(1)
diff, same = [], 0
for f in sorted(glob.glob(CAND1 + "/**/*", recursive=True)):
    rel = os.path.relpath(f, CAND1)
    g = os.path.join("/tmp/matched_run2/matched_ab_evidence", rel)
    if os.path.isdir(f):
        continue
    if not os.path.exists(g):
        diff.append("missing in run2: " + rel)
    elif sha(f) != sha(g):
        diff.append("differs: " + rel)
    else:
        same += 1
n1 = len([f for f in glob.glob(CAND1 + "/**/*", recursive=True) if os.path.isfile(f)])
print("identical: %d / %d files" % (same, n1))
if diff:
    print("*** NOT byte-identical:")
    for d_ in diff[:10]:
        print("      " + d_)
    results.append(False)
else:
    print("two runs byte-identical, including evidence_pack.json and package_index.json")
    results.append(True)

print("\nfail-closed + determinism: %d/%d" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
