#!/usr/bin/env python3
"""Test 3 transmission: the duration-choice package, sent under the SAME rules as Test 2.

Same provider boundary, same reference material, same model, same single request.
The only change to the request is the candidate lists (each event now carries duration
faces) and the prompt section that explains them.

Sends ONLY: overview contact sheet, per-event dense frames, the reference sheets.
Never the masters, never the full performance, never the local-only deterministic ranking.
"""
import base64, glob, hashlib, json, os, sys, time, urllib.request, urllib.error

PAY = "/root/test1/payload_test3"
REF = "/root/test1/payload/reference_pkg"
OUT = "/root/test1/responses_test3"
MODEL = os.environ.get("T3_MODEL", "gpt-5.6-terra")
API = "https://api.openai.com/v1/chat/completions"
LEDGER = "/root/test1/transmission_ledger_test3.json"

os.makedirs(OUT, exist_ok=True)


def key():
    for line in open("/root/.hermes/.env"):
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no key")


def b64(p):
    with open(p, "rb") as f:
        return base64.b64encode(f.read()).decode()


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def img(p, detail="high"):
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64(p), "detail": detail}}


def build():
    sent = []
    prompt = open(PAY + "/prompt.md").read()
    ev = open(PAY + "/evidence_pack.json").read()
    for needed in ("b_glance", "b_action", "b_hold", "hold_a"):
        assert needed in ev, "evidence pack missing candidate %s" % needed
    prompt = prompt.replace("{{PASTE_EVIDENCE_PACK_JSON_HERE}}", ev)

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
        "here: it does not use one fixed insert length.\n"
    )

    content = [{"type": "text", "text": prompt}]

    ov = sorted(glob.glob(PAY + "/overview/*.jpg"))
    content.append({"type": "text", "text": "## EVIDENCE IMAGES — overview contact sheet (%d). "
                     "Tiles run left-to-right, top-to-bottom; each tile has its absolute timecode "
                     "burned top-left." % len(ov)})
    for p in ov:
        content.append(img(p)); sent.append(p)

    evdirs = sorted(glob.glob(PAY + "/events/evt_*"))
    for d in evdirs:
        fs = sorted(glob.glob(d + "/*.jpg"))
        content.append({"type": "text", "text": "## DENSE FRAMES — %s (%d frames at 2.00 fps inside "
                         "that event's full candidate envelope, so the longest legal choice is "
                         "covered). Timecode is burned top-left and is also in the filename "
                         "(dense_%s_HH-MM-SS.mmm.jpg)."
                         % (os.path.basename(d), len(fs), os.path.basename(d))})
        for p in fs:
            content.append(img(p)); sent.append(p)

    content.append({"type": "text", "text": reftext})
    for p in sorted(glob.glob(REF + "/reference/*.jpg")):
        content.append(img(p)); sent.append(p)

    content.append({"type": "text", "text": "## NOW DECIDE\n"
                    "Emit JSON only, exactly one object per event (evt_01..evt_%02d), each with "
                    "event_id, selection (the id of one candidate listed for that event), "
                    "confidence_score in [0,1], and a one-line rationale (<=240 chars) naming "
                    "what the viewer sees and why that choice earns its place." % len(evdirs)})
    return content, sent


def post(content, model=MODEL, timeout=1500):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}]}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": "Bearer " + key(), "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:4000], time.time() - t0
    except Exception as e:
        return -1, "%s: %s" % (type(e).__name__, e), time.time() - t0


def main():
    content, sent = build()
    print("model:", MODEL, "| TEST 3 duration-choice package")
    print("images:", len(sent), "| bytes:", sum(os.path.getsize(p) for p in sent))
    print("text chars:", sum(len(c["text"]) for c in content if c["type"] == "text"))
    st, raw, dt = post(content)
    ts = time.strftime("%Y%m%d-%H%M%S")
    rp = "%s/response_%s.json" % (OUT, ts)
    open(rp, "w").write(raw if st == 200 else json.dumps({"http": st, "body": raw}))
    print("HTTP", st, "| latency %.1fs" % dt, "| saved", rp)
    if st != 200:
        print("FAILED BODY:", raw[:1200]); return 2
    d = json.loads(raw)
    usage = d.get("usage", {})
    txt = d["choices"][0]["message"]["content"]
    led = {"experiment": "Test 3 — OpenAI + reference editorial, DURATION CHOICE exposed",
           "when_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "model": MODEL, "endpoint": API, "request_count": 1, "http_status": st,
           "latency_s": round(dt, 1), "usage": usage, "response_file": rp,
           "files_sent": [{"path": p.replace(PAY, "test3_durations"), "bytes": os.path.getsize(p),
                           "sha256": sha(p)} for p in sent],
           "bytes_sent": sum(os.path.getsize(p) for p in sent),
           "not_sent": ["camera masters", "full performance", "candidates/deterministic_ranking_LOCAL.json",
                        "studio config/secrets", "audio"],
           "transmitted": True}
    json.dump(led, open(LEDGER, "w"), indent=1)
    print("usage:", json.dumps(usage))
    u = usage
    print("cost: $%.4f  (Test 2 was $0.3603)"
          % (u["prompt_tokens"] / 1e6 * 2.00 + u["completion_tokens"] / 1e6 * 12.00))
    print("ledger:", LEDGER, "| files:", len(led["files_sent"]))
    open(OUT + "/decisions_%s.txt" % ts, "w").write(txt)
    print("--- response head ---")
    print(txt[:800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
