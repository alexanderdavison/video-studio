#!/usr/bin/env python3
"""Convert a video-use EDL JSON to FCPXML 1.10 for Premiere Pro / DaVinci Resolve.

Reads the EDL schema used by helpers/render.py:
    {
      "grade": "auto" | preset | raw filter (ignored here),
      "sources": { "name": "/abs/path/or/relative/to/edl" },
      "ranges":  [ { "source": "name", "start": float_s, "end": float_s,
                     "beat"|"note": "..." (optional) } ],
      "subtitles": path (optional, ignored here),
      "overlays": [...]
    }

Outputs a native .fcpxml timeline: each range becomes a clip on the spine,
referencing its source asset with frame-accurate sourceIn/sourceOut.  Sources
are NOT copied or transcoded — the FCPXML references the original files, so
this gives you the same "ready-to-cut project" promise as Wideframe without
the .prproj binary format: open the .fcpxml in Premiere or Resolve and finish
the edit there.

Timebases: integer fps (24/25/30/50/60) and NTSC fractional (23.976/29.97/
59.94) are both handled; NTSC uses NDF frame counting (like a standard
"30 NDF" project).  Durations are snapped to whole source frames.

Usage:
    python3 edl_to_fcpxml.py <edl.json> -o out.fcpxml
    python3 edl_to_fcpxml.py <edl.json> -o out.fcpxml --project-name "Weave v1"
    python3 edl_to_fcpxml.py <edl.json> -o out.fcpxml --rewrite-prefix /mnt/media=/Volumes/media
    python3 edl_to_fcpxml.py <edl.json> -o out.fcpxml --validate

--rewrite-prefix remaps source paths (repeatable) so the project can point at
the NAS as mounted on the editing machine.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

FCPXML_VERSION = "1.10"
AUDIO_RATE = 48000


# ---------------- ffprobe ---------------------------------------------------


def probe_video(path: Path) -> dict:
    """Return {width, height, fps_float, fps_ratio} for the video stream."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(f"no video stream in {path}")
    s = streams[0]
    ratio = s.get("r_frame_rate") or s.get("avg_frame_rate") or "0/1"
    num, den = 1, 1
    try:
        n, d = ratio.split("/")
        num, den = int(n), int(d)
    except Exception:
        pass
    fps = num / den if den else 0.0
    try:
        duration_s = float(data.get("format", {}).get("duration", 0) or 0)
    except ValueError:
        duration_s = 0.0
    return {
        "width": int(s.get("width", 0) or 0),
        "height": int(s.get("height", 0) or 0),
        "fps": fps,
        "fps_ratio": ratio,
        "duration_s": duration_s,
        "num": num,
        "den": den,
    }


# ---------------- format / time helpers -------------------------------------


class Fmt:
    """One FCPXML format + tick math for a source."""

    def __init__(self, probe: dict):
        self.width = probe["width"]
        self.height = probe["height"]
        self.fps = probe["fps"]
        den = probe["den"]
        num = probe["num"]
        self.ntsc = den == 1001
        if self.ntsc:
            # e.g. 24000/1001 -> timebase 24000 ticks/sec, 1001 ticks per frame
            self.timebase = num if num > 0 else 24000
            self.tpf = 1001
            self.nominal = round(self.timebase / 1001)
            self.frame_duration = f"1001/{self.timebase}s"
        else:
            tb = max(int(round(self.fps)), 1)
            self.timebase = tb
            self.tpf = 1
            self.nominal = tb
            self.frame_duration = f"1/{tb}s"
        self.id = None  # set later

    def ticks(self, seconds: float) -> int:
        frames = round(seconds * self.fps)
        return frames * self.tpf

    def tstr(self, ticks: int) -> str:
        return f"{ticks}/{self.timebase}s"

    @property
    def format_name(self) -> str:
        return f"FFVideoFormat{self.width}x{self.height}p{self.nominal}"


def _seconds_to_ticks_in(fmt: Fmt, seconds: float) -> int:
    return fmt.ticks(seconds)


# ---------------- XML building ----------------------------------------------


def _asset_src(path: Path, rewrites: list[tuple[str, str]]) -> str:
    s = str(path)
    for old, new in rewrites:
        if s.startswith(old):
            s = new + s[len(old):]
            break
    return Path(s).as_uri()


def build_fcpxml(edl: dict, edl_dir: Path, project_name: str,
                 rewrites: list[tuple[str, str]]) -> ET.Element:
    sources = edl["sources"]
    ranges = edl.get("ranges", [])
    if not ranges:
        raise RuntimeError("EDL has no ranges")

    # Resolve source paths, probe each unique source once.
    src_paths: dict[str, Path] = {}
    for name, p in sources.items():
        p = Path(p)
        src_paths[name] = p if p.is_absolute() else (edl_dir / p).resolve()

    probes: dict[str, dict] = {}
    fmts: dict[str, Fmt] = {}
    for name, p in src_paths.items():
        if not p.exists():
            raise RuntimeError(f"source missing: {p}")
        probes[name] = probe_video(p)
        fmts[name] = Fmt(probes[name])

    # Assign format ids per unique (width,height,frame_duration).
    fmt_by_key: dict[tuple, str] = {}
    fmt_by_name: dict[str, str] = {}
    for name, f in fmts.items():
        key = (f.width, f.height, f.frame_duration)
        if key not in fmt_by_key:
            fid = f"fmt{len(fmt_by_key) + 1}"
            fmt_by_key[key] = fid
        fmt_by_name[name] = fmt_by_key[key]
        f.id = fmt_by_name[name]

    def fmt_display_name(key: tuple) -> str:
        w, h, fd = key
        if fd.startswith("1001/"):
            tb = int(fd.split("/")[1][:-1])
            nominal = round(tb / 1001)
        else:
            nominal = int(fd[2:-1])
        return f"FFVideoFormat{w}x{h}p{nominal}"

    root = ET.Element("fcpxml", version=FCPXML_VERSION)

    # ---- resources ----
    resources = ET.SubElement(root, "resources")
    for key, fid in fmt_by_key.items():
        w, h, fd = key
        ET.SubElement(resources, "format", id=fid, name=fmt_display_name(key),
                      frameDuration=fd, width=str(w), height=str(h),
                      colorSpace="1-1-1 (Rec. 709)")
    asset_ids: dict[str, str] = {}
    for i, (name, p) in enumerate(src_paths.items(), start=1):
        rid = f"r{i}"
        asset_ids[name] = rid
        probe = probes[name]
        fmt = fmts[name]
        ET.SubElement(
            resources, "asset",
            id=rid,
            name=p.name,
            start="0s",
            duration=fmt.tstr(fmt.ticks(probe.get("duration_s", 0))),
            format=fmt_by_name[name],
            hasVideo="1",
            hasAudio="1",
            src=_asset_src(p, rewrites),
        )

    # ---- library / event / project / sequence ----
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name="Video Studio")
    project = ET.SubElement(event, "project", name=project_name, uid=uuid.uuid4().hex)
    seq_fmt = fmts[ranges[0]["source"]]
    spine_clips: list[tuple[Fmt, str, int, int, int, int]] = []  # (fmt, asset, start_seq, dur_ticks, dur_seq, src_start_ticks)

    running = 0  # timeline position, in the SEQUENCE timebase
    for r in ranges:
        src_name = r["source"]
        if src_name not in fmts:
            raise RuntimeError(f"range references unknown source '{src_name}'")
        fmt = fmts[src_name]
        start = float(r["start"])
        end = float(r["end"])
        dur = max(end - start, 0.0)
        if dur <= 0:
            continue
        start_ticks = fmt.ticks(start)
        dur_ticks = fmt.ticks(dur)
        # Timeline start/duration live in the sequence timebase; the clip's own
        # timebase is only used for sourceStart/sourceDuration.
        dur_seq_ticks = round(dur_ticks * seq_fmt.timebase / fmt.timebase)
        spine_clips.append((fmt, asset_ids[src_name], running, dur_ticks, dur_seq_ticks, start_ticks))
        running += dur_seq_ticks
    if not spine_clips:
        raise RuntimeError("EDL produced no clips (empty or zero-duration ranges)")

    seq_duration_ticks = running
    ET.SubElement(
        project, "sequence",
        format=seq_fmt.id,
        duration=seq_fmt.tstr(seq_duration_ticks),
        tcStart="0s",
        tcFormat="NDF",
        audioLayout="stereo",
        audioRate=str(AUDIO_RATE),
        audioSources="2",
    ).append(ET.Element("spine"))

    # ---- clips ----
    spine = project.find("./sequence/spine")
    for i, (fmt, asset_id, start_seq_ticks, dur_ticks, dur_seq_ticks, src_start_ticks) in enumerate(spine_clips):
        r = ranges[i] if i < len(ranges) else {}
        note = r.get("note") or r.get("beat") or ""
        name = f"seg_{i:02d}" + (f" {note}" if note else "")
        clip = ET.SubElement(
            spine, "clip",
            name=name,
            start=seq_fmt.tstr(start_seq_ticks),
            duration=seq_fmt.tstr(dur_seq_ticks),
            format=fmt.id,
            ref=asset_id,
            tcFormat="NDF",
        )
        src_start = fmt.tstr(src_start_ticks)
        ET.SubElement(clip, "video", offset="0s",
                      sourceStart=src_start,
                      sourceDuration=fmt.tstr(dur_ticks))
        ET.SubElement(clip, "audio", offset="0s",
                      sourceStart=src_start,
                      sourceDuration=fmt.tstr(dur_ticks))
    return root


def pretty_xml(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(root, encoding="unicode")


def main() -> int:
    ap = argparse.ArgumentParser(description="video-use EDL JSON -> FCPXML")
    ap.add_argument("edl", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--project-name", default=None)
    ap.add_argument("--rewrite-prefix", action="append", default=[],
                    help="OLD=NEW path prefix remap (repeatable)")
    ap.add_argument("--validate", action="store_true", help="re-parse and sanity-check output")
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        print(f"edl not found: {edl_path}", file=sys.stderr)
        return 1
    edl = json.loads(edl_path.read_text())
    project_name = args.project_name or edl.get("project") or edl_path.stem

    rewrites = []
    for spec in args.rewrite_prefix:
        if "=" not in spec:
            print(f"--rewrite-prefix expects OLD=NEW, got: {spec}", file=sys.stderr)
            return 1
        old, new = spec.split("=", 1)
        rewrites.append((old, new))

    try:
        root = build_fcpxml(edl, edl_path.parent, project_name, rewrites)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    xml_text = pretty_xml(root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(xml_text)

    n_clips = len(edl.get("ranges", []))
    n_srcs = len(edl.get("sources", {}))
    print(f"wrote {args.output} ({n_clips} clips, {n_srcs} sources, FCPXML {FCPXML_VERSION})")

    if args.validate:
        ET.parse(args.output)  # well-formedness
        ids = [el.attrib["id"] for el in root.iter() if "id" in el.attrib]
        if len(ids) != len(set(ids)):
            print("validate: DUPLICATE ids", file=sys.stderr)
            return 1
        spine = root.find(".//spine")
        if spine is None or len(spine) == 0:
            print("validate: empty spine", file=sys.stderr)
            return 1
        print(f"validate: OK — {len(spine)} clip(s), XML well-formed, ids unique")
    return 0


if __name__ == "__main__":
    sys.exit(main())
