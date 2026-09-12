#!/opt/video-studio/tools/venv/bin/python
"""validate_manifest.py — ISH D template-engine manifest validator (Phase 2).

Gate for the typed, versioned edit-plan manifest. Must pass BEFORE the visual
proxy (Phase 3) or the deterministic renderer (Phase 4) touches a job.

Usage:
  validate_manifest.py [--media-root DIR] [--registry PATH] [--schema PATH]
                       [--no-media] [--json] manifest.yaml

  --media-root DIR   base dir for source files (default: current dir).
                     Relative source names resolve against it; absolute names
                     are used as-is. Enables real-duration span checks via
                     ffprobe (b8-shape protection).
  --no-media         skip file/duration checks (structural + registry only).
  --registry PATH    profile registry JSON (default: sibling profile_registry.json)
  --schema PATH      JSON Schema (default: sibling manifest_schema.json)
  --json             machine-readable output: {"valid": bool, "errors": [...], "notes": [...]}

Exit codes:
  0  valid
  1  invalid (errors printed)
  2  tool/usage error

The manifest constrains, never expands. Unknown fields are rejected, not
ignored. Cut ids map 1:1 to proxy burn-ins (cut_018 -> CUT 018). Profile and
template names must resolve in the registry — the registry is the ONLY source
of locked creative params.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys

import jsonschema
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCHEMA = os.path.join(HERE, "manifest_schema.json")
DEFAULT_REGISTRY = os.path.join(HERE, "profile_registry.json")

# Friendly one-line explanations per jsonschema validator kind.
VALIDATOR_MSGS = {
    "required": "missing required property '{0}'",
    "type": "expected {0}, got {1}",
    "minimum": "must be >= {0}",
    "exclusiveMinimum": "must be > {0}",
    "maximum": "must be <= {0}",
    "pattern": "must match pattern {0}",
    "const": "must equal {0}",
    "enum": "must be one of {0}",
    "additionalProperties": "unknown property '{0}' (manifest constrains, never expands)",
    "minItems": "must have at least {0} item(s)",
    "minLength": "must be at least {0} character(s)",
    "maxLength": "must be at most {0} character(s)",
    "format": "must be a valid {0}",
}


class ManifestError(Exception):
    pass


def yaml_to_json(obj):
    """Normalize YAML-native types (datetime/date) to JSON-safe values so the
    JSON Schema sees strings (e.g. unquoted ISO timestamps in postflight)."""
    if isinstance(obj, dict):
        return {k: yaml_to_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [yaml_to_json(v) for v in obj]
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    return obj


def load_yaml(path: str):
    """Parse YAML with a line-aware error on malformed input."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise ManifestError(f"malformed YAML{where}: {exc.problem or exc}") from exc
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc


def load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc


def schema_errors(manifest, schema) -> list:
    """Return friendly structural errors from jsonschema."""
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    seen = set()
    for err in validator.iter_errors(manifest):
        # Only report the most specific error at each path (skip children of a
        # reported failure, which would be noise).
        key = err.json_path
        if any(key.startswith(s) for s in seen):
            continue
        seen.add(key)
        path = err.json_path.replace("$", "manifest")
        msg = err.message
        v = err.validator
        if v in VALIDATOR_MSGS and err.validator_value is not None:
            try:
                if v == "type":
                    msg = VALIDATOR_MSGS[v].format(err.validator_value, err.instance.__class__.__name__)
                elif v == "additionalProperties":
                    msg = VALIDATOR_MSGS[v].format(err.validator_value)
                else:
                    msg = VALIDATOR_MSGS[v].format(err.validator_value)
            except Exception:
                msg = err.message
        errors.append(f"{path}: {msg}")
    return errors


def check_registry(manifest, registry) -> list:
    """Template + profile names must resolve in the registry."""
    errors = []
    tmpl = manifest.get("template")
    if tmpl is not None and tmpl not in registry.get("templates", {}):
        known = ", ".join(sorted(registry.get("templates", {}))) or "(none)"
        errors.append(f"manifest.template: unknown template '{tmpl}' (known: {known})")
    profiles = manifest.get("profiles", {})
    reg_profiles = registry.get("profiles", {})
    for key, name in profiles.items():
        if key not in reg_profiles:
            errors.append(f"manifest.profiles.{key}: unknown profile category '{key}'")
            continue
        if name not in reg_profiles[key]:
            known = ", ".join(sorted(reg_profiles[key])) or "(none)"
            errors.append(f"manifest.profiles.{key}: unknown profile '{name}' (known: {known})")
    return errors


def check_timeline(manifest) -> list:
    """Semantic timeline checks independent of media."""
    errors = []
    cuts = manifest.get("timeline", [])
    seen_ids = {}
    b_cuts = 0
    per_angle_prev = {}  # angle -> previous cut (source_in, source_out)
    for i, cut in enumerate(cuts):
        cid = cut.get("id")
        if cid is not None:
            if cid in seen_ids:
                errors.append(f"manifest.timeline[{i}].id: duplicate cut id '{cid}' (first at index {seen_ids[cid]})")
            else:
                seen_ids[cid] = i
        si = cut.get("source_in")
        so = cut.get("source_out")
        if isinstance(si, (int, float)) and isinstance(so, (int, float)) and so <= si:
            errors.append(f"manifest.timeline[{i}].{cid}: source_out {so} must be > source_in {si}")
        angle = cut.get("angle")
        if angle == "B":
            b_cuts += 1
        if angle in ("A", "B"):
            prev = per_angle_prev.get(angle)
            if prev is not None and isinstance(si, (int, float)):
                if si < prev[0]:
                    errors.append(
                        f"manifest.timeline[{i}].{cid}: {angle}-angle cuts out of order — source_in {si} < previous source_in {prev[0]}"
                    )
                elif si < prev[1]:
                    errors.append(
                        f"manifest.timeline[{i}].{cid}: {angle}-angle cuts overlap — source_in {si} < previous {angle} source_out {prev[1]}"
                    )
            if isinstance(si, (int, float)) and isinstance(so, (int, float)):
                per_angle_prev[angle] = (si, so)

    sources = manifest.get("sources", {})
    b_reel = sources.get("b_reel")
    if b_cuts > 0 and not b_reel:
        errors.append("manifest.sources.b_reel: angle B cuts present but no b_reel is defined")
    return errors


def check_pre_grades(manifest) -> list:
    """pre_grades (Phase 6) keys must be declared sources; values non-empty."""
    errors = []
    pre = manifest.get("pre_grades")
    if not pre:
        return errors
    if not isinstance(pre, dict):
        errors.append("manifest.pre_grades: must be a mapping of source name -> curve string")
        return errors
    sources = manifest.get("sources", {})
    declared = set()
    if sources.get("a_reel"):
        declared.add(sources["a_reel"])
    for name in sources.get("b_reel", []):
        declared.add(name)
    for name, curve in pre.items():
        if name not in declared:
            errors.append(
                f"manifest.pre_grades: key '{name}' is not a declared source (a_reel/b_reel)"
            )
        if not isinstance(curve, str) or not curve.strip():
            errors.append(f"manifest.pre_grades.{name}: curve must be a non-empty string")
    return errors


def ffprobe_duration(path: str):
    """Return duration seconds for a media file via ffprobe, or None."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


def check_media(manifest, media_root) -> tuple:
    """File-existence + real-duration span checks (b8-shape protection).

    Returns (errors, notes). Notes are informational (e.g. a B span crossing
    a file boundary — valid shape; Phase 4 auto-splits it).
    """
    errors = []
    notes = []
    sources = manifest.get("sources", {})

    def resolve(name):
        return name if os.path.isabs(name) else os.path.join(media_root, name)

    def check_file(name, what):
        path = resolve(name)
        if not os.path.isfile(path):
            errors.append(f"manifest.sources.{what}: source file not found: {path}")
            return None
        dur = ffprobe_duration(path)
        if dur is None:
            errors.append(f"manifest.sources.{what}: ffprobe could not read duration for {path}")
            return None
        return dur

    a_dur = check_file(sources.get("a_reel", ""), "a_reel") if sources.get("a_reel") else None
    b_reel = sources.get("b_reel") or []
    b_durs = []
    total_b = 0.0
    for idx, name in enumerate(b_reel):
        dur = check_file(name, f"b_reel[{idx}]")
        if dur is None:
            continue
        b_durs.append((name, dur))
        total_b += dur

    # Virtual reel offsets: cumulative starts.
    b_offsets = []
    cum = 0.0
    for name, dur in b_durs:
        b_offsets.append((name, cum, cum + dur))
        cum += dur

    for i, cut in enumerate(manifest.get("timeline", [])):
        cid = cut.get("id", f"[{i}]")
        si = cut.get("source_in")
        so = cut.get("source_out")
        if not isinstance(si, (int, float)) or not isinstance(so, (int, float)):
            continue
        angle = cut.get("angle")
        if angle == "A":
            if a_dur is None:
                continue
            if so > a_dur + 1e-6:
                errors.append(
                    f"manifest.timeline[{i}].{cid}: span ends at {so}s but {sources['a_reel']} is only {a_dur:.3f}s (b8 shape — asking more than the source holds)"
                )
        elif angle == "B":
            if not b_durs:
                continue
            if so > total_b + 1e-6:
                errors.append(
                    f"manifest.timeline[{i}].{cid}: span ends at {so}s but the b_reel virtual reel is only {total_b:.3f}s across {len(b_durs)} file(s)"
                )
            # Shape note: does this span cross a file boundary?
            boundary = None
            for name, start, end in b_offsets:
                if si < end - 1e-6 and so > end + 1e-6:
                    boundary = (name, end)
                    break
            if boundary:
                notes.append(
                    f"manifest.timeline[{i}].{cid}: B span [{si},{so}) crosses file boundary of {boundary[0]} at {boundary[1]:.3f}s — valid shape, auto-split is Phase 4"
                )
    return errors, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate an ISH D template-engine manifest.")
    ap.add_argument("manifest", help="path to the YAML manifest")
    ap.add_argument("--media-root", default=".", help="base dir for source files (default: cwd)")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    ap.add_argument("--no-media", action="store_true", help="skip file/duration checks")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    try:
        schema = load_json(args.schema)
        registry = load_json(args.registry)
        manifest = yaml_to_json(load_yaml(args.manifest))
    except ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if manifest is None:
        errors = ["manifest is empty"]
        notes = []
        if args.json:
            print(json.dumps({"valid": False, "errors": errors, "notes": notes}))
        else:
            print("INVALID: manifest is empty")
        return 1
    if not isinstance(manifest, dict):
        errors = [f"manifest must be a YAML mapping, got {type(manifest).__name__}"]
        if args.json:
            print(json.dumps({"valid": False, "errors": errors, "notes": []}))
        else:
            print("INVALID:", errors[0])
        return 1

    errors = schema_errors(manifest, schema)
    errors += check_registry(manifest, registry)
    errors += check_timeline(manifest)
    errors += check_pre_grades(manifest)
    notes = []

    if not args.no_media:
        media_errors, media_notes = check_media(manifest, args.media_root)
        errors += media_errors
        notes += media_notes

    if errors:
        if args.json:
            print(json.dumps({"valid": False, "errors": errors, "notes": notes}))
        else:
            print(f"INVALID {args.manifest} — {len(errors)} error(s):")
            for e in errors:
                print(f"  - {e}")
        return 1

    summary = (
        f"VALID {args.manifest} — job_id={manifest.get('job_id')}, "
        f"template={manifest.get('template')}, {len(manifest.get('timeline', []))} cut(s)"
    )
    if args.json:
        print(json.dumps({"valid": True, "errors": [], "notes": notes}))
    else:
        print(summary)
        for n in notes:
            print(f"  note: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
