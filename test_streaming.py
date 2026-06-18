"""Streaming + daily-budget checks. Run: python3 test_streaming.py

Proves the SSE streaming sibling of the assistant is faithful to the non-streaming
contract: run_stream yields ('delta', ...) text slices then exactly one ('done', result)
whose dict matches run()'s shape; the confirm gate is never bypassed over the stream; the
/assistant/stream endpoint is CSRF-gated, emits text/event-stream frames, and ends with a
done frame carrying cards/pending_action. Also proves the per-tenant DAILY budget degrades
to the keyword floor (allow_llm=False) without breaking the turn. Throwaway temp DB + the
deterministic demo brain; no network.
"""
import os
import json
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"          # deterministic, no network
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""                  # configured() False -> sends simulate

import assistant
import app
client = app.app.test_client()

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


lead_id = db.create_lead(1, "Dana Homeowner", "+15551234567")
biz = db.get_business(1)


# --- _chunk_reply reconstructs the text exactly (no dropped/duplicated chars) ---
for sample in ["", "One.", "Slow week. 8 missed calls, only 2 booked back. Want me to text them?"]:
    joined = "".join(assistant._chunk_reply(sample))
    check(f"chunk_reply reconstructs exactly ({len(sample)} chars)", joined == sample)
check("chunk_reply yields nothing for empty text",
      list(assistant._chunk_reply("")) == [])


# --- run_stream: a read turn streams deltas then exactly one done matching run() ---
events = list(assistant.run_stream(biz, "show my leads", [], entities=[]))
deltas = [v for (k, v) in events if k == "delta"]
dones = [v for (k, v) in events if k == "done"]
check("run_stream emits exactly one done event", len(dones) == 1)
check("run_stream streams the reply as deltas", len(deltas) >= 1)
done = dones[0]
check("streamed deltas reconstruct the reply",
      "".join(deltas) == (done.get("reply") or ""))
check("run_stream done has the run() shape (reply/cards/pending_action/meta)",
      all(k in done for k in ("reply", "cards", "pending_action", "meta")))
# Parity with the non-streaming brain for the same turn.
direct = assistant.run(biz, "show my leads", [], entities=[])
check("run_stream reply matches run() for the same read turn",
      done.get("reply") == direct.get("reply"))
check("run_stream surfaces the same cards as run()",
      bool(done.get("cards")) == bool(direct.get("cards")))


# --- the confirm gate is NEVER bypassed over the stream (write -> pending_action) ---
# "the first lead" is the phrasing the deterministic router resolves; it routes WRITE intents
# through the keyword path even when an LLM is keyed, so the gate behaves identically here.
gev = list(assistant.run_stream(biz, "text the first lead saying running 10 minutes late", [],
                                entities=[]))
gdone = [v for (k, v) in gev if k == "done"][0]
pend = gdone.get("pending_action")
check("a write intent over the stream returns a pending_action (gated)", bool(pend))
check("the gated write is text_lead with an honest preview",
      bool(pend) and pend.get("tool") == "text_lead" and "preview" in pend)
check("nothing auto-sent: the stream never carries a 'sent' status",
      gdone.get("meta", {}).get("status") != "sent")


# --- empty message streams a single done, no deltas ---
eev = list(assistant.run_stream(biz, "   ", [], entities=[]))
check("empty message yields exactly one done and no deltas",
      [k for (k, v) in eev] == ["done"])


# --- the /assistant/stream endpoint: auth + CSRF + SSE frames ---
client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
import re as _re
html = client.get("/dashboard").get_data(as_text=True)
_cm = _re.search(r'id="csrfToken" value="([^"]+)"', html)
CSRF = _cm.group(1) if _cm else ""

bad = client.post("/assistant/stream", data={"message": "show my leads", "_csrf": "nope"})
check("/assistant/stream rejects a bad CSRF token", bad.status_code == 403)


def stream_frames(message):
    r = client.post("/assistant/stream",
                    data={"message": message, "_csrf": CSRF, "history": "[]",
                          "convo_key": "k1", "browser_key": "b1"})
    ctype = r.headers.get("Content-Type", "")
    frames = []
    for line in r.get_data(as_text=True).splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[len("data: "):]))
    return ctype, frames


ctype, frames = stream_frames("show my leads")
check("/assistant/stream returns an event-stream content type",
      ctype.startswith("text/event-stream"))
check("/assistant/stream emits at least one delta frame then a done frame",
      any(f.get("t") == "delta" for f in frames) and frames[-1].get("t") == "done")
res = frames[-1].get("result", {})
check("/assistant/stream done result has cards for a read turn", bool(res.get("cards")))
check("/assistant/stream reconstructed reply matches the done result",
      "".join(f["v"] for f in frames if f.get("t") == "delta") == (res.get("reply") or ""))

# Endpoint preserves the gate too.
_, gframes = stream_frames("text the first lead saying I am on the way")
gres = gframes[-1].get("result", {})
check("/assistant/stream keeps the confirm gate (pending_action over SSE)",
      bool(gres.get("pending_action")))


# --- daily budget: degrades to the keyword floor without breaking the turn ---
# Reset this tenant's rate counters first (the endpoint calls above already spent some).
_c = db.get_conn(); _c.execute("DELETE FROM rate_limits WHERE business_id=1"); _c.commit(); _c.close()
_saved = app.ASSISTANT_DAILY
try:
    app.ASSISTANT_DAILY = 1
    a1, t1 = app._assistant_budget(biz, "show my leads")   # 1st daily turn: within budget
    a2, t2 = app._assistant_budget(biz, "show my leads")   # 2nd: over the daily budget
    check("daily budget allows the first turn", a1 is True)
    check("daily budget withholds the LLM after the cap (degrade, not block)", a2 is False)
    check("daily budget does not hard-throttle (per-minute is separate)", t2 is False)
finally:
    app.ASSISTANT_DAILY = _saved

# allow_llm=False still answers from the deterministic floor (no broken turn).
floored = assistant.run(biz, "show my leads", [], entities=[], allow_llm=False)
check("allow_llm=False still returns a usable reply", bool(floored.get("reply")))
check("allow_llm=False still surfaces read cards", bool(floored.get("cards")))

# empty message never spends budget.
ea, et = app._assistant_budget(biz, "")
check("an empty message spends no budget", ea is True and et is False)


print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
