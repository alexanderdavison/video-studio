#!/usr/bin/env python3
"""P0 — proposal vs approved diff, and the accumulating disagreement profile.

Answers "where does Ish actually disagree with the deterministic editor?" by
comparing the manifest the engine PROPOSED against the manifest that was APPROVED
for the same mix, then storing the result so the profile strengthens automatically
as more mixes pass through the console.

One comparison:
  proposal_diff.py diff PROPOSED.yaml APPROVED.yaml [--project NAME] [--store]
                                          [--out report.json] [--json]

Aggregate across every stored comparison:
  proposal_diff.py profile [--json] [--out profile.json]

Classification (all thresholds are CLI-tunable):
  removed_b      B cut present in the proposal, absent from the approved timeline
  added_b        B cut in the approved timeline with no proposal counterpart
  angle_flipped  same time window, different camera
  entered_late   matched B, approved start later than proposed (by > --shift)
  entered_early  matched B, approved start earlier
  held_longer    matched cut, approved duration longer (by > --dur)
  shortened      matched cut, approved duration shorter
  missed_b       a long A-only span in the proposal that the approved timeline fills with B

Matching is greedy nearest-start within --tolerance seconds on the same virtual-reel
timeline; unmatched proposal cuts count as removed, unmatched approved cuts as added.

CAVEAT recorded with every row: if the two manifests use different templates or
different flex settings, part of the difference is POLICY, not manual editing. The
store keeps both template names so the profile can filter on that later.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

STORE_DDL = """
CREATE TABLE IF NOT EXISTS edit_deltas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  attribution TEXT NOT NULL DEFAULT 'legacy_ambiguous',
  project TEXT,
  proposed_path TEXT,
  approved_path TEXT,
  proposed_template TEXT,
  approved_template TEXT,
  proposed_cuts INTEGER,
  approved_cuts INTEGER,
  proposed_b_pct REAL,
  approved_b_pct REAL,
  deltas_json TEXT
);
"""


def load_manifest(path):
    if yaml is None:
        raise RuntimeError("pyyaml missing — run with the studio tools venv python")
    doc = yaml.safe_load(Path(path).read_text())
    tl = doc.get("timeline") or []
    cuts = []
    for c in tl:
        try:
            cuts.append({
                "id": c.get("id"),
                "angle": c.get("angle"),
                "in": float(c.get("source_in", 0.0)),
                "out": float(c.get("source_out", 0.0)),
                "dur": float(c.get("source_out", 0.0)) - float(c.get("source_in", 0.0)),
            })
        except (TypeError, ValueError):
            continue
    return {"template": doc.get("template"), "job_id": doc.get("job_id"), "cuts": cuts}


def b_pct(cuts):
    tot = sum(c["dur"] for c in cuts)
    b = sum(c["dur"] for c in cuts if c["angle"] == "B")
    return round(100.0 * b / tot, 2) if tot else 0.0


def match(proposed, approved, tolerance):
    """Greedy nearest-start match on same angle where possible."""
    used = set()
    pairs, removed, added = [], [], []
    for p in sorted(proposed, key=lambda c: c["in"]):
        best, best_d = None, None
        for i, a in enumerate(approved):
            if i in used:
                continue
            d = abs(a["in"] - p["in"])
            if d <= tolerance and (best_d is None or d < best_d):
                best, best_d = i, d
        if best is None:
            removed.append(p)
        else:
            used.add(best)
            pairs.append((p, approved[best]))
    for i, a in enumerate(approved):
        if i not in used:
            added.append(a)
    return pairs, removed, added


def classify(pairs, removed, added, shift, dur):
    out = {"angle_flipped": [], "entered_late": [], "entered_early": [],
           "held_longer": [], "shortened": [], "unchanged": []}
    for p, a in pairs:
        if p["angle"] != a["angle"]:
            out["angle_flipped"].append({
                "proposed": p, "approved": a,
                "delta_in": round(a["in"] - p["in"], 3)})
            continue
        dd = a["dur"] - p["dur"]
        di = a["in"] - p["in"]
        tagged = False
        if di > shift:
            out["entered_late"].append({"proposed": p, "approved": a, "delta_in": round(di, 3)})
            tagged = True
        elif di < -shift:
            out["entered_early"].append({"proposed": p, "approved": a, "delta_in": round(di, 3)})
            tagged = True
        if dd > dur:
            out["held_longer"].append({"proposed": p, "approved": a, "delta_dur": round(dd, 3)})
            tagged = True
        elif dd < -dur:
            out["shortened"].append({"proposed": p, "approved": a, "delta_dur": round(dd, 3)})
            tagged = True
        if not tagged:
            out["unchanged"].append({"proposed": p, "approved": a})
    out["removed_b"] = [c for c in removed if c["angle"] == "B"]
    out["removed_a"] = [c for c in removed if c["angle"] == "A"]
    out["added_b"] = [c for c in added if c["angle"] == "B"]
    out["added_a"] = [c for c in added if c["angle"] == "A"]
    return out


def missed_b(approved_cuts, min_span=20.0):
    """Long A-only runs in the APPROVED timeline where no B appears."""
    runs, cur = [], None
    for c in sorted(approved_cuts, key=lambda c: c["in"]):
        if c["angle"] == "A":
            cur = {"start": c["in"], "end": c["out"]} if cur is None else \
                  {"start": cur["start"], "end": c["out"]}
        else:
            if cur and cur["end"] - cur["start"] >= min_span:
                runs.append({"start": round(cur["start"], 3), "end": round(cur["end"], 3),
                             "seconds": round(cur["end"] - cur["start"], 1)})
            cur = None
    if cur and cur["end"] - cur["start"] >= min_span:
        runs.append({"start": round(cur["start"], 3), "end": round(cur["end"], 3),
                     "seconds": round(cur["end"] - cur["start"], 1)})
    return runs


def store(row, db):
    conn = sqlite3.connect(db, timeout=30)
    conn.executescript(STORE_DDL)
    cols = {c[1] for c in conn.execute("PRAGMA table_info(edit_deltas)")}
    if "attribution" not in cols:                      # in-place migration
        conn.execute("ALTER TABLE edit_deltas ADD COLUMN attribution TEXT "
                     "NOT NULL DEFAULT 'legacy_ambiguous'")
    conn.execute(
        "INSERT INTO edit_deltas (attribution, project, proposed_path, approved_path,"
        " proposed_template, approved_template, proposed_cuts, approved_cuts,"
        " proposed_b_pct, approved_b_pct, deltas_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (row["attribution"], row["project"], row["proposed_path"], row["approved_path"],
         row["proposed_template"], row["approved_template"],
         row["proposed_cuts"], row["approved_cuts"],
         row["proposed_b_pct"], row["approved_b_pct"], json.dumps(row["deltas"])))
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM edit_deltas").fetchone()[0]


def cmd_diff(args):
    prop = load_manifest(args.proposed)
    appr = load_manifest(args.approved)
    pairs, removed, added = match(prop["cuts"], appr["cuts"], args.tolerance)
    deltas = classify(pairs, removed, added, args.shift, args.dur)
    deltas["missed_b"] = missed_b(appr["cuts"], args.min_span)
    row = {
        "project": args.project or Path(args.approved).parent.parent.name,
        "proposed_path": args.proposed, "approved_path": args.approved,
        "proposed_template": prop["template"], "approved_template": appr["template"],
        "proposed_cuts": len(prop["cuts"]), "approved_cuts": len(appr["cuts"]),
        "proposed_b_pct": b_pct(prop["cuts"]), "approved_b_pct": b_pct(appr["cuts"]),
        "attribution": args.attribution,
        "deltas": {k: v for k, v in deltas.items()},
    }
    summary = {
        "project": row["project"],
        "templates": f"{prop['template']} -> {appr['template']}",
        "cuts": f"{row['proposed_cuts']} -> {row['approved_cuts']}",
        "b_pct": f"{row['proposed_b_pct']}% -> {row['approved_b_pct']}%",
        "counts": {k: len(v) for k, v in deltas.items() if isinstance(v, list) and v},
        "missed_b": len(deltas["missed_b"]),
        "policy_confound": prop["template"] != appr["template"],
        "attribution": args.attribution,
    }
    if args.store:
        row["stored_count"] = store(row, args.db)
        summary["stored_rows"] = row["stored_count"]
    if args.out:
        Path(args.out).write_text(json.dumps(row, indent=1))
    if args.json:
        print(json.dumps(summary, indent=1))
    else:
        print(f"P0 DIFF — {summary['project']}")
        print(f"  templates {summary['templates']}"
              f"{'   [CONFOUND: template changed, part of this is policy not editing]' if summary['policy_confound'] else ''}")
        print(f"  cuts {summary['cuts']}   B share {summary['b_pct']}")
        print(f"  disagreement: {summary['counts'] or 'none'}")
        print(f"  long A-only spans (>= {args.min_span:.0f}s) in approved: {summary['missed_b']}")
        if args.attribution != "bound":
            print("  ATTRIBUTION: legacy/ambiguous — EXCLUDED from the preference profile"
                  " (only bound approvals count as human signal)")
    return 0


def cmd_profile(args):
    conn = sqlite3.connect(args.db, timeout=30)
    try:
        where = "" if args.include_legacy else " WHERE attribution='bound'"
        rows = conn.execute("SELECT project, proposed_template, approved_template, proposed_cuts,"
                            " approved_cuts, proposed_b_pct, approved_b_pct, deltas_json, ts"
                            " FROM edit_deltas" + where + " ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        total = conn.execute("SELECT COUNT(*) FROM edit_deltas").fetchone()[0]
        print(f"no BOUND comparisons yet ({total} legacy/ambiguous row(s) excluded).")
        print("Bound data starts accumulating with the next approval made through the"
              " edit console (review.py now writes approval_timelines).")
        return 0
    agg = {}
    tot = {"cuts": [0, 0], "b_pct": [0.0, 0.0]}
    for (proj, pt, at, pc, ac, pb, ab, dj, ts) in rows:
        d = json.loads(dj)
        tot["cuts"][0] += pc
        tot["cuts"][1] += ac
        tot["b_pct"][0] += pb
        tot["b_pct"][1] += ab
        for k, v in d.items():
            if isinstance(v, list):
                agg[k] = agg.get(k, 0) + len(v)
    n = len(rows)
    profile = {
        "comparisons": n,
        "projects": [r[0] for r in rows],
        "mean_proposed_cuts": round(tot["cuts"][0] / n, 1),
        "mean_approved_cuts": round(tot["cuts"][1] / n, 1),
        "mean_b_pct": [round(tot["b_pct"][0] / n, 1), round(tot["b_pct"][1] / n, 1)],
        "total_by_category": dict(sorted(agg.items(), key=lambda kv: -kv[1])),
    }
    if args.out:
        Path(args.out).write_text(json.dumps(profile, indent=1))
    if args.json:
        print(json.dumps(profile, indent=1))
    else:
        print(f"P0 DISAGREEMENT PROFILE — {n} comparison(s): {', '.join(profile['projects'])}")
        print(f"  cuts {profile['mean_proposed_cuts']} -> {profile['mean_approved_cuts']} (mean)")
        print(f"  B share {profile['mean_b_pct'][0]}% -> {profile['mean_b_pct'][1]}% (mean)")
        print("  disagreement by category (totals):")
        for k, v in profile["total_by_category"].items():
            print(f"    {k:15s} {v}")
        print("  NOTE: n grows automatically as more proposal/approved pairs are diffed.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["diff", "profile"])
    ap.add_argument("proposed", nargs="?")
    ap.add_argument("approved", nargs="?")
    ap.add_argument("--project", default=None)
    ap.add_argument("--store", action="store_true")
    ap.add_argument("--db", default="/opt/video-studio/studio.db")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tolerance", type=float, default=3.0, help="seconds, cut matching window")
    ap.add_argument("--shift", type=float, default=1.0, help="seconds, entry/exit shift to count")
    ap.add_argument("--dur", type=float, default=2.0, help="seconds, duration change to count")
    ap.add_argument("--min-span", type=float, default=20.0, help="A-only span to report")
    ap.add_argument("--attribution", choices=["bound", "legacy_ambiguous"],
                    default="legacy_ambiguous",
                    help="bound = approval tied to an exact timeline (counts as human signal)")
    ap.add_argument("--include-legacy", action="store_true",
                    help="profile: also count legacy/ambiguous rows (diagnostic only)")
    a = ap.parse_args()
    if a.cmd == "diff":
        if not a.proposed or not a.approved:
            ap.error("diff needs PROPOSED.yaml APPROVED.yaml")
        sys.exit(cmd_diff(a))
    sys.exit(cmd_profile(a))
