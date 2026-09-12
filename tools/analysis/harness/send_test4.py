#!/usr/bin/env python3
"""Test 4 — matched A/B editorial test. Exactly ONE request.

First valid synchronized A-vs-B editorial test: the model sees the event-specific dense A
burst and dense B burst covering the SAME performance-time envelope, at the same frame rate.

Differs from Test 3 ONLY in the evidence and the per-event framing text:
  * dense frames are now a matched A/B pair per event (a_* / b_*, both envelope-covered)
  * the event header states the matched-pair layout and both timebases
  * the overview sheet is labelled context only

Prompt, model, reference material and the single-request discipline are unchanged.
Sends ONLY: the A overview sheet (context), the per-event A and B dense frames, the
reference sheets. Never the camera masters, never the full performance, never the local
deterministic ranking, never audio.
"""
import base64
import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

PAY = "/root/test1/payload_test4"
REF = "/root/test1/payload/reference_pkg"
OUT = "/root/test1/responses_test4"
LEDGER = "/root/test1/transmission_ledger_test4.json"
CAND_ARTIFACT = "/opt/video-studio/tools/analysis/p1_out/sync_300_420.json"
CANON_PROMPT = "/opt/video-studio/tools/analysis/editorial_prompt_matched_ab.md"
PKG_INDEX_SHA = "sha recorded below"
MODEL = os.environ.get("T4_MODEL", "gpt-5.6-terra")
API = "https://api.openai.com/v1/chat/completions"
LAG = 1.2783
BUILD_STATE = {}

os.makedirs(OUT, exist_ok=True)


def key():
    for line in open("/root/.hermes/.env"):
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no OPENAI_API_KEY in /root/.hermes/.env")


def b64(p):
    with open(p, "rb") as f:
        return base64.b64encode(f.read()).decode()


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def text(s):
    return {"type": "text", "text": s}


def img(p, detail="high"):
    return {"type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + b64(p), "detail": detail}}


def filtered_pack_text():
    """The pack as SENT: identical to the canonical pack except the deterministic
    recommendation is removed, so it cannot anchor the editorial decision.

    The recommendation lives at the EVENT level (`recommended_by_score` plus
    `recommendation_note`), not per candidate. The canonical package on disk is NOT
    modified; this is a transmission-time transformation and is recorded in the ledger.
    """
    pack = json.load(open(PAY + "/evidence_pack.json"))
    removed = []
    for ev in pack.get("events", []):
        for k in ("recommended_by_score", "recommendation_note"):
            if k in ev:
                removed.append({"event_id": ev.get("event_id"), "key": k, "value": ev.pop(k)})
    txt = json.dumps(pack, indent=1)
    assert "recommended_by_score" not in txt and "recommendation_note" not in txt
    return txt, removed


def build():
    sent = []
    prompt = open(PAY + "/prompt.md").read()
    assert "{{PASTE_EVIDENCE_PACK_JSON_HERE}}" in prompt, "prompt placeholder missing"
    ev, removed = filtered_pack_text()
    for needed in ("b_glance", "b_action", "b_hold", "hold_a"):
        assert needed in ev, "evidence pack missing candidate %s" % needed
    prompt = prompt.replace("{{PASTE_EVIDENCE_PACK_JSON_HERE}}", ev)

    content = [text(prompt)]
    BUILD_STATE["removed"] = removed
    BUILD_STATE["pack_as_sent"] = ev

    ov = sorted(glob.glob(PAY + "/overview/*.jpg"))
    content.append(text("## EVIDENCE IMAGES — A-camera overview contact sheet (%d). Tiles run "
                        "left-to-right, top-to-bottom; each tile has its absolute timecode burned "
                        "top-left. This sheet is BROAD PROGRAM CONTEXT ONLY — it is not the "
                        "event-specific evidence and must not stand in for it." % len(ov)))
    for p in ov:
        content.append(img(p))
        sent.append(p)

    for d in sorted(glob.glob(PAY + "/events/evt_*")):
        eid = os.path.basename(d)
        fa = sorted(glob.glob(d + "/a_*.jpg"))
        fb = sorted(glob.glob(d + "/b_*.jpg"))
        assert fa and fb, "%s: missing a matched A/B pair" % eid
        content.append(text(
            "## DENSE FRAMES — %s — MATCHED A/B PAIR (%d A frames + %d B frames at 2.00 fps, "
            "both covering the SAME performance-time envelope, so the longest legal choice is "
            "covered from both angles). `a_%s_HH-MM-SS.mmm.jpg` is A camera, timecode = A "
            "source time = performance time. `b_%s_HH-MM-SS.mmm.jpg` is B camera, timecode = "
            "B source time = performance - %.4f s, so a B frame %.4f s EARLIER in timecode "
            "shows the same performance moment as its A counterpart. `A-CAM` / `B-CAM` is also "
            "burned top-right on every frame."
            % (eid, len(fa), len(fb), eid, eid, LAG, LAG)))
        for p in fa + fb:
            content.append(img(p))
            sent.append(p)

    refnotes = json.load(open(REF + "/reference_notes.json"))
    reftext = (
        "\n\n---\n\n## REFERENCE MATERIAL (human-approved, supplied for Test 1)\n\n"
        "The human has approved the reference edit below. It is the authority on the editorial "
        "language wanted for this set: wide-shot restraint, B-camera/detail usage, transition "
        "anticipation, physical DJ interaction, shot pacing, and the deliberate refusal to cut. "
        "Use it to judge whether a switch earns its place. Cite its timecodes (absolute source "
        "times, `HH:MM:SS.mmm`) when it shapes a decision, and say which behaviour it shows.\n\n"
        "```json\n" + json.dumps({k: refnotes[k] for k in
        ("source_file", "sha256", "title", "channel", "video_id", "excerpt", "reference_shots",
         "cadence", "limitations")}, indent=1) + "\n```\n\n"
        "You will now see the reference's own frames: three sparse contact sheets over its "
        "12-minute excerpt (1 frame / 10 s, timecodes burned on every tile) and one transition "
        "sheet (three frames -1 s / cut / +1 s around each of the reference's own detected cuts). "
        "The measured shot list above is the authoritative pacing record; the tiles are the visual. "
        "Its shot-length distribution is the reference for the DURATION decision you are making "
        "here: it does not use one fixed insert length.\n")
    content.append(text(reftext))
    for p in sorted(glob.glob(REF + "/reference/*.jpg")):
        content.append(img(p))
        sent.append(p)

    n_ev = len(glob.glob(PAY + "/events/evt_*"))
    content.append(text("## NOW DECIDE\n"
                        "Emit JSON only, exactly one object per event (evt_01..evt_%02d), each with "
                        "event_id, selection (the id of one candidate listed for that event), "
                        "confidence_score in [0, 1], and a one-line rationale (<=240 chars) naming "
                        "what the viewer sees and why that choice earns its place." % n_ev))
    return content, sent


def parse_decisions(txt):
    """Tolerant parse of the decision chain; returns (list, note)."""
    s = txt.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    cand = None
    for m in re.finditer(r"[\[{]", s):
        frag = s[m.start():]
        for end in range(len(frag), 1, -1):
            if frag[end - 1] not in "]}":
                continue
            try:
                cand = json.loads(frag[:end])
                break
            except Exception:
                continue
        if cand is not None:
            break
    if cand is None:
        return None, "no parseable JSON found"
    if isinstance(cand, dict):
        for k in ("decisions", "chain", "events", "result", "results"):
            if isinstance(cand.get(k), list):
                return cand[k], "wrapped in object key %r" % k
        return [cand], "single decision object"
    return cand, "bare array"


def main():
    content, sent = build()
    bytes_sent = sum(os.path.getsize(p) for p in sent)
    pas = OUT + "/evidence_pack_as_sent.json"
    open(pas, "w").write(BUILD_STATE["pack_as_sent"])
    print("model:", MODEL, "| TEST 4 matched A/B package")
    print("images:", len(sent), "| bytes:", bytes_sent)
    print("text chars:", sum(len(c["text"]) for c in content if c["type"] == "text"))
    print("recommendation fields removed before sending: %d %s"
          % (len(BUILD_STATE["removed"]),
             sorted({r["key"] for r in BUILD_STATE["removed"]})))
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": content}]}).encode()
    print("request body MB: %.1f" % (len(body) / 1e6))
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": "Bearer " + key(), "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            st, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        st, raw = e.code, e.read().decode()[:8000]
    except Exception as e:
        st, raw = -1, "%s: %s" % (type(e).__name__, e)
    dt = time.time() - t0
    ts = time.strftime("%Y%m%d-%H%M%S")
    rp = "%s/response_%s.json" % (OUT, ts)
    open(rp, "w").write(raw)
    print("HTTP", st, "| latency %.1fs" % dt, "| saved", rp)

    led = {"experiment": "Test 4 — matched synchronized A/B editorial test",
           "when_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "model": MODEL, "endpoint": API, "request_count": 1, "http_status": st,
           "latency_s": round(dt, 1), "response_file": rp, "response_sha256": sha(rp),
           "package": {
               "path": PAY, "origin":
                   "/opt/video-studio/projects/2026-08-25-tester/work/analysis/"
                   "editorial_pkg_300_600/matched_ab_evidence",
               "package_index_sha256": sha(PAY + "/package_index.json"),
               "evidence_pack_sha256": sha(PAY + "/evidence_pack.json"),
               "prompt_in_package_sha256": sha(PAY + "/prompt.md")},
           "canonical_prompt": {"path": CANON_PROMPT, "sha256": sha(CANON_PROMPT)},
           "candidate_artifact": {"path": CAND_ARTIFACT, "sha256": sha(CAND_ARTIFACT)},
           "reference_package": {"path": REF,
                                 "reference_notes_sha256": sha(REF + "/reference_notes.json"),
                                 "frames": sorted(os.path.basename(p) for p in
                                                  glob.glob(REF + "/reference/*.jpg")),
                                 "source_file": ref_notes_source()},
           "evidence": {"n_images": len(sent), "bytes_sent": bytes_sent,
                        "n_a_dense": len(glob.glob(PAY + "/events/*/a_*.jpg")),
                        "n_b_dense": len(glob.glob(PAY + "/events/*/b_*.jpg")),
                        "n_overview": len(glob.glob(PAY + "/overview/*.jpg")),
                        "matched_envelope": True,
                        "steering_added": "none",
                        "recommended_by_score_exposed": False},
           "pack_transformation": {
               "what": ("removed the deterministic per-event recommendation from the pack text "
                        "before transmission"),
               "keys_removed": sorted({r["key"] for r in BUILD_STATE["removed"]}),
               "n_removed": len(BUILD_STATE["removed"]),
               "removed_values": BUILD_STATE["removed"],
               "why": ("the prompt states the pack carries eight scores per candidate and no "
                       "total, ranking or recommendation, but the pack carried a per-event "
                       "recommendation; it must not anchor the editorial decision"),
               "canonical_package_modified": False,
               "eight_scores_preserved": True},
           "evidence_pack_as_sent": {"path": pas, "sha256": sha(pas),
                                     "note": "the exact pack text the model received"},
           "files_sent": [{"path": p.replace(PAY, "matched_ab_evidence").replace(REF, "reference_pkg"),
                           "bytes": os.path.getsize(p), "sha256": sha(p)} for p in sent],
           "not_sent": ["camera masters", "full performance", "audio",
                        "any deterministic ranking or recommendation field",
                        "studio config/secrets"]}
    if st == 200:
        d = json.loads(raw)
        usage = d.get("usage", {})
        led["usage"] = usage
        rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        led["reasoning_tokens"] = rt
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        led["cost_estimate_usd"] = round(pt / 1e6 * 2.00 + ct / 1e6 * 12.00, 4)
        led["cost_basis"] = "prompt $2.00/M, completion $12.00/M (same basis as Test 3)"
        txt = d["choices"][0]["message"]["content"]
        open(OUT + "/decisions_%s.txt" % ts, "w").write(txt)
        dec, note = parse_decisions(txt)
        json.dump({"raw_text": txt, "parse_note": note, "decisions": dec},
                  open(OUT + "/parsed_%s.json" % ts, "w"), indent=1)
        led["decisions_file"] = OUT + "/parsed_%s.json" % ts
        led["parse_note"] = note
        led["n_decisions_parsed"] = len(dec) if dec else 0
        print("usage:", json.dumps(usage))
        print("reasoning tokens:", rt)
        print("cost estimate: $%.4f" % led["cost_estimate_usd"])
        print("parsed %d decisions (%s)" % (len(dec) if dec else 0, note))
        print("--- response head ---")
        print(txt[:1200])
    else:
        print("FAILED BODY:", raw[:1200])
    json.dump(led, open(LEDGER, "w"), indent=1)
    print("ledger:", LEDGER)
    return 0 if st == 200 else 2


def ref_notes_source():
    rn = json.load(open(REF + "/reference_notes.json"))
    return {"source_file": rn["source_file"], "sha256": rn["sha256"],
            "title": rn.get("title"), "n_shots": len(rn["reference_shots"])}


if __name__ == "__main__":
    sys.exit(main())
