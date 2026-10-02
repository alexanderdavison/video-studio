#!/usr/bin/env python3
"""qc_delivery.py — DELIVERY_QC on the delivered file (PRODUCTION LOCK v1).

Checks the delivery CONTRACT on the bytes a person will watch, not on the intent:
  container/video    h264 High, 1920x1080, yuv420p, tv range, bt709 primaries/transfer,
                     r_frame_rate 30000/1001                    (matrix untagged is accepted)
  frame count        == round(basis_total_s * 30000/1001) within 1 frame (no accumulated drift)
  duration           within 1.5 s of the basis total
  faststart          moov atom before mdat in the first 8 MB
  audio              aac, 48000 Hz, 2 channels, bitrate >= 256k
  loudness gates     integrated LUFS within 0.3 of -13.5 and true peak <= -1.5 dBTP, read from the
                     renderer's ALLDONE record beside the delivered file (NOT_RUN if absent)

Usage: qc_delivery.py --delivered FILE [--basis BASIS.json] [--all-done FILE] [--json-out OUT]
Exit: 0 PASS, 1 FAIL, 2 error.  A missing ALLDONE record yields FAIL (delivery gate NOT_RUN).
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

WANT = {"codec_name": "h264", "profile": "High", "width": "1920", "height": "1080",
        "pix_fmt": "yuv420p", "color_range": "tv", "color_primaries": "bt709",
        "color_transfer": "bt709", "r_frame_rate": "30000/1001"}
AUD = {"codec_name": "aac", "sample_rate": "48000", "channels": "2"}


def probe(path, entries, stream="v:0"):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream,
                        "-show_entries", "stream=" + ",".join(entries),
                        "-of", "default=nw=1", path], capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delivered", required=True)
    ap.add_argument("--basis", default=None)
    ap.add_argument("--all-done", default=None)
    ap.add_argument("--label", default="")
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args()
    p = Path(a.delivered)
    if not p.exists():
        print(json.dumps({"verdict": "ERROR", "error": "no such file: %s" % p}))
        sys.exit(2)

    checks = []

    def chk(name, ok, detail=""):
        checks.append({"name": name, "pass": bool(ok), "detail": detail})

    v = probe(p.as_posix(), list(WANT) + ["nb_frames"])
    for k, want in WANT.items():
        chk("video.%s == %s" % (k, want), v.get(k) == want, "got %r" % v.get(k))
    chk("video.color_space tagged or unknown",
        v.get("color_space", "unknown") in ("bt709", "unknown"), "got %r" % v.get("color_space"))

    dur = None
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", p.as_posix()], capture_output=True, text=True)
    try:
        dur = float(r.stdout.strip())
    except ValueError:
        chk("duration readable", False, r.stdout.strip()[:80])
    if dur is not None:
        chk("duration readable", True, "%.3f s" % dur)

    if a.basis:
        b = json.load(open(a.basis))
        total = float(b.get("total_s") or 0.0)
        want_frames = round(total * 30000.0 / 1001.0)
        got = int(v.get("nb_frames") or -1)
        chk("frame count == plan (%d)" % want_frames, abs(got - want_frames) <= 1,
            "got %d (delta %d)" % (got, got - want_frames))
        chk("duration within 1.5 s of the basis total",
            dur is not None and abs(dur - total) <= 1.5, "got %.3f vs %.3f" % (dur or -1, total))
    else:
        chk("frame count vs plan", False, "no --basis given: cannot verify frame count (NOT_RUN)")

    # faststart: moov before mdat inside the head of the file
    with open(p, "rb") as f:
        head = f.read(8_000_000)
    mo, md = head.find(b"moov"), head.find(b"mdat")
    chk("faststart (moov before mdat)", 0 <= mo < md, "moov@%d mdat@%d" % (mo, md))

    au = probe(p.as_posix(), list(AUD) + ["bit_rate"], stream="a:0")
    for k, want in AUD.items():
        chk("audio.%s == %s" % (k, want), au.get(k) == want, "got %r" % au.get(k))
    try:
        br = int(au.get("bit_rate") or 0)
    except ValueError:
        br = 0
    chk("audio.bit_rate >= 256k", br >= 256000, "got %d" % br)

    # loudness gates from the renderer's own ALLDONE record (absent => NOT_RUN = FAIL)
    ad = Path(a.all_done) if a.all_done else p.parent / "ALLDONE"
    lufs = tp = None
    if ad.exists():
        txt = ad.read_text().strip()
        m1 = re.search(r"LUFS=(-?[0-9.]+)", txt)
        m2 = re.search(r"TP=(-?[0-9.]+)", txt)
        lufs = float(m1.group(1)) if m1 else None
        tp = float(m2.group(1)) if m2 else None
    chk("integrated loudness within 0.3 of -13.5", lufs is not None and abs(lufs + 13.5) <= 0.3,
        "got %s from %s" % (lufs, ad))
    chk("true peak <= -1.5 dBTP", tp is not None and tp <= -1.5, "got %s from %s" % (tp, ad))

    verdict = "PASS" if all(c["pass"] for c in checks) else "FAIL"
    out = {"verdict": verdict, "label": a.label, "delivered_file": p.as_posix(),
           "delivered_sha256": subprocess.run(["sha256sum", p.as_posix()],
                                              capture_output=True, text=True).stdout.split()[0],
           "duration_s": dur, "checks": checks,
           "loudness": {"integrated_lufs": lufs, "true_peak_dbtp": tp, "source": str(ad)}}
    if verdict == "FAIL":
        out["failed"] = [c for c in checks if not c["pass"]]
    if a.json_out:
        json.dump(out, open(a.json_out, "w"), indent=1)
    print(json.dumps(out, indent=1)[:4000])
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
