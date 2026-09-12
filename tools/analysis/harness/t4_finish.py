#!/usr/bin/env python3
"""Write the Test 4 ledger + parsed decision chain from the ALREADY-SAVED raw response.

No OpenAI request is made here. The sender saved the raw response and then crashed on two
ledger paths that live on the studio host, so the record is completed from the saved bytes.
"""
import glob
import hashlib
import json
import os
import sys

sys.path.insert(0, "/root/vs3")
import send_test4 as s  # noqa: E402  (reuse build(), parse_decisions(), paths, sha())

REMOTE = json.loads(os.environ["REMOTE_SHAS"])
rp = sorted(glob.glob(s.OUT + "/response_*.json"))[-1]
raw = open(rp).read()
d = json.loads(raw)
usage = d.get("usage", {})
txt = d["choices"][0]["message"]["content"]
dec, note = s.parse_decisions(txt)
ts = os.path.basename(rp).replace("response_", "").replace(".json", "")

open(s.OUT + "/decisions_%s.txt" % ts, "w").write(txt)
json.dump({"raw_text": txt, "parse_note": note, "decisions": dec},
          open(s.OUT + "/parsed_%s.json" % ts, "w"), indent=1)

# rebuild the sent-file list and the pack-as-sent by re-running build() (no network)
content, sent = s.build()
bytes_sent = sum(os.path.getsize(p) for p in sent)
pas = s.OUT + "/evidence_pack_as_sent.json"
open(pas, "w").write(s.BUILD_STATE["pack_as_sent"])

rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
pt, ct = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
rn = json.load(open(s.REF + "/reference_notes.json"))

led = {
    "experiment": "Test 4 — matched synchronized A/B editorial test",
    "when_utc": "2026-09-12T07:25:53Z",
    "completed_utc": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 __import__("time").gmtime()),
    "model": s.MODEL, "endpoint": s.API, "request_count": 1, "http_status": 200,
    "latency_s": 24.6, "response_file": rp, "response_sha256": s.sha(rp),
    "usage": usage, "reasoning_tokens": rt,
    "cost_estimate_usd": round(pt / 1e6 * 2.00 + ct / 1e6 * 12.00, 4),
    "cost_basis": "prompt $2.00/M, completion $12.00/M (same basis as Test 3)",
    "package": {"path": s.PAY,
                "origin": ("/opt/video-studio/projects/2026-08-25-tester/work/analysis/"
                           "editorial_pkg_300_600/matched_ab_evidence"),
                "package_index_sha256": s.sha(s.PAY + "/package_index.json"),
                "evidence_pack_sha256": s.sha(s.PAY + "/evidence_pack.json"),
                "prompt_in_package_sha256": s.sha(s.PAY + "/prompt.md")},
    "canonical_prompt": {"path": REMOTE["canonical_prompt_path"],
                         "sha256": REMOTE["canonical_prompt_sha256"],
                         "note": "lives on the studio host; sha measured there"},
    "candidate_artifact": {"path": REMOTE["candidate_artifact_path"],
                           "sha256": REMOTE["candidate_artifact_sha256"],
                           "note": "lives on the studio host; sha measured there"},
    "reference_package": {"path": s.REF,
                          "reference_notes_sha256": s.sha(s.REF + "/reference_notes.json"),
                          "frames": sorted(os.path.basename(p) for p in
                                           glob.glob(s.REF + "/reference/*.jpg")),
                          "source_file": rn["source_file"], "source_sha256": rn["sha256"],
                          "title": rn.get("title"), "n_reference_shots": len(rn["reference_shots"])},
    "evidence": {"n_images": len(sent), "bytes_sent": bytes_sent,
                 "n_a_dense": len(glob.glob(s.PAY + "/events/*/a_*.jpg")),
                 "n_b_dense": len(glob.glob(s.PAY + "/events/*/b_*.jpg")),
                 "n_overview": len(glob.glob(s.PAY + "/overview/*.jpg")),
                 "matched_envelope": True, "steering_added": "none",
                 "recommended_by_score_exposed": False},
    "pack_transformation": {
        "what": "removed the deterministic per-event recommendation from the pack text before sending",
        "keys_removed": sorted({r["key"] for r in s.BUILD_STATE["removed"]}),
        "n_removed": len(s.BUILD_STATE["removed"]),
        "removed_values": s.BUILD_STATE["removed"],
        "why": ("the prompt states the pack carries eight scores per candidate and no total, "
                "ranking or recommendation, but the pack carried a per-event recommendation; "
                "it must not anchor the editorial decision"),
        "canonical_package_modified": False, "eight_scores_preserved": True},
    "evidence_pack_as_sent": {"path": pas, "sha256": s.sha(pas),
                              "note": "the exact pack text the model received"},
    "files_sent": [{"path": p.replace(s.PAY, "matched_ab_evidence").replace(s.REF, "reference_pkg"),
                    "bytes": os.path.getsize(p), "sha256": s.sha(p)} for p in sent],
    "not_sent": ["camera masters", "full performance", "audio",
                 "any deterministic ranking or recommendation field",
                 "studio config/secrets"],
    "decisions_file": s.OUT + "/parsed_%s.json" % ts,
    "parse_note": note, "n_decisions_parsed": len(dec) if dec else 0,
    "harness_note": ("the sender's post-call ledger write raised FileNotFoundError on two "
                     "studio-host paths; the HTTP 200 response was already saved and this "
                     "record was completed from those bytes with no further API request"),
}
json.dump(led, open(s.LEDGER, "w"), indent=1)

print("response :", rp)
print("response sha:", led["response_sha256"])
print("usage    :", json.dumps(usage))
print("reasoning tokens:", rt)
print("cost estimate: $%.4f" % led["cost_estimate_usd"])
print("ledger   :", s.LEDGER)
print("parsed   : %d decisions (%s)" % (led["n_decisions_parsed"], note))
print()
print("--- raw response head ---")
print(txt[:1500])
