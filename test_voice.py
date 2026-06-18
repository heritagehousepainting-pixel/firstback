"""Phase 3 AI-voice-agent checks. Run: python3 test_voice.py

Drives the FastAPI voice service via TestClient (HTTP + WebSocket) against a temp
DB and the deterministic demo brain (no network, no real Twilio/Claude). Also
checks the SMS 'call me' consent trigger on the Flask side. Exits non-zero on any
failure.
"""
import base64
import hashlib
import hmac
import json
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"           # deterministic, no network
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
config.VOICE_PUBLIC_URL = "https://voice.firstback.test"   # enable the voice leg

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_AUTH_TOKEN = "tok"   # used to sign the Flask webhook test below
messaging.TWILIO_ACCOUNT_SID = ""     # unconfigured -> place_call/send_sms simulate

import voice_service                   # imports app (Flask) -> init_db()+seed on temp DB
import app as flask_app
flask_app.VOICE_PUBLIC_URL = "https://voice.firstback.test"  # CALL trigger reads app's copy
import compliance
compliance.QUIET_START, compliance.QUIET_END = 0, 24  # deterministic: allow the test CALL any hour

from fastapi.testclient import TestClient
vclient = TestClient(voice_service.fastapi_app)

BIZ_NUM, CALLER = "+15553140000", "+14155550199"
db.set_business_twilio(1, BIZ_NUM, "PN1", forward_to="+15559990000")

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- /twiml: ConversationRelay document ----
lead_id = db.create_lead(1, "Voice Caller", CALLER)
xml = vclient.get(f"/twiml?biz=1&lead={lead_id}").text
check("/twiml returns ConversationRelay TwiML", "<ConversationRelay" in xml)
check("/twiml welcomeGreeting includes the recording disclosure", "may be recorded" in xml)
check("/twiml points the relay at our wss /ws", "wss://voice.firstback.test/ws" in xml)
check("/twiml passes biz + lead as parameters",
      'name="biz"' in xml and f'value="{lead_id}"' in xml)


# ---- /ws: a spoken conversation runs the shared engine and books ----
with vclient.websocket_connect(f"/ws?biz=1&lead={lead_id}") as ws:
    ws.send_text(json.dumps({"type": "setup", "callSid": "CAv1",
                             "customParameters": {"biz": "1", "lead": str(lead_id)}}))
    frames = []
    for utt in ["I need my kitchen painted", "123 Main Street",
                "the first one works", "yes"]:
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": utt, "last": True}))
        frames.append(json.loads(ws.receive_text()))
check("/ws replies with a spoken text frame per prompt",
      len(frames) == 4 and all(f.get("type") == "text" and f.get("token") for f in frames))
ins = [m for m in db.get_messages(lead_id) if m["direction"] == "in"]
check("/ws records the caller's utterances on the lead thread",
      any("kitchen" in (m["body"] or "") for m in ins))
check("/ws books an estimate through the shared engine (voice == SMS path)",
      db.get_lead(lead_id)["status"] == "booked" or len(db.list_appointments(1)) >= 1)


# ---- place_call gating ----
check("place_call simulates when Twilio is unconfigured",
      messaging.place_call(db.get_business(1), CALLER, "https://voice.test/twiml")["status"] == "simulated")


# ---- SMS 'call me' consent trigger (Flask side) ----
fclient = flask_app.app.test_client()


def _sign(url, params):
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(b"tok", data.encode(), hashlib.sha1).digest()).decode()


def fpost(path, params):
    url = "http://localhost" + path
    return fclient.post(path, data=params, headers={"X-Twilio-Signature": _sign(url, params)})


CALLER2 = "+14155550288"
r = fpost("/webhooks/twilio/sms/inbound",
          {"To": BIZ_NUM, "From": CALLER2, "Body": "call me", "MessageSid": "SMc1"})
check("'call me' triggers the voice callback (Calling you now)",
      "Calling you now" in r.get_data(as_text=True))
c = db.get_consent(1, CALLER2)
check("'call me' records affirmative voice consent", bool(c) and c["voice_ok"] == 1)


os.unlink(_TMP.name)
print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
