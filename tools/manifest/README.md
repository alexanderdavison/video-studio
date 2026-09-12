# Manifest — ISH D template engine (Phase 2)

Typed, versioned edit-plan manifests for the ISH D DJ-video template engine.
The manifest is the single input contract for BOTH the visual proxy (Phase 3)
and the future deterministic renderer (Phase 4). No throwaway proxy workflow:
what the proxy consumes is what the final render consumes.

Riley (ish-d conductor) updates the manifest — never shell scripts. Ish never
touches it during normal use.

## Files

| File | Role |
|---|---|
| `manifest_schema.json` | JSON Schema (draft 2020-12). Structural contract. |
| `profile_registry.json` | Versioned template + profile names -> LOCKED creative params. The ONLY source of names a manifest may use. |
| `validate_manifest.py` | CLI gate. Must pass before any renderer/proxy work starts. |
| `examples/` | One manifest per Club Dispatch preset. |

## Usage

```bash
V=/opt/video-studio/tools/venv/bin/python
$V tools/manifest/validate_manifest.py --media-root /path/to/mix/folder job.yaml
```

- `--media-root` points at the folder holding the source files. When given,
  the validator probes each source with ffprobe and checks every cut span
  against REAL durations (b8-shape protection: a cut may never ask for more
  than the source holds).
- Structural checks (schema + registry + timeline semantics) run regardless;
  `--no-media` skips only the file/duration checks.
- Exit code 0 = valid, 1 = invalid, 2 = tool error. `--json` emits
  `{"valid": bool, "errors": [...], "notes": [...]}` for tooling.

## Rules (enforced, not aspirational)

1. **The manifest constrains, never expands.** Unknown fields are rejected.
   To add capability, extend the schema deliberately with a reason — then
   bump `manifest_version`.
2. **Cut ids map 1:1 to proxy burn-ins.** `cut_018` renders as `CUT 018`.
   Three zero-padded digits, unique within the timeline.
3. **A is the safe master; B is one virtual reel.** `sources.b_reel` files
   concatenate in list order. B cut times are measured on that concatenation.
4. **A B span may cross a file boundary** (that is a valid shape — the Phase 4
   renderer auto-splits it). It may NOT exceed the total reel, and no span may
   exceed a single file's real duration.
5. **Profiles are locked.** `club_dispatch_pb3_v1`, `cd_a_pushin_v1`,
   `cd_bug_v9`, `cd_youtube_v1` resolve to the creative params in
   `profile_registry.json`. The app encodes those params; it never redesigns
   them. New creative decisions = NEW versioned names, never edits in place.
6. **Presets differ by template name, not by hand.** Minimal / Standard /
   Active / A-Cam-Only all carry the same grade/crop/graphics/audio contract;
   only editorial density changes (`club_dispatch_<preset>_v1`).
7. **`postflight` is written by the verifier, never authored.** After a
   completed job, the postflight verifier appends integrated loudness + BT.709
   confirmation + delivered file under `postflight:`. The schema accepts it;
   Riley does not type it in.

## Adding a profile

1. Add the versioned name to `profile_registry.json` with its locked params
   and the decision source.
2. Manifests may reference it immediately. No schema change needed.

## Extending the schema

Phase 3 (proxy) and Phase 4 (renderer) will extend this file. Extension rule:
add ONLY what a downstream stage demonstrably consumes, keep
`additionalProperties: false`, and record each extension in the Phase 2
decision doc (`ish-d/creative/decisions/2026-08-26-phase2-manifest-schema.md`).
