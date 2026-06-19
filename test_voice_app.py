"""Phase 5g Slice 4 app.py tests.

Run: python test_voice_app.py

Covers (5 regions):
  R1  STOP / detect_revocation / cancel->opt-out all also clear voice_ok
  R2  Pre-call guard order: voice_ok=0 / spam>=HARD / called <60min / over monthly cap
      each independently skips place_call and replies by text
  R3  /internal/voice/stream: secret-gated, uses Haiku, does NOT call booking path (P0-2)
  R4  /internal/voice/turn_log: secret-gated, stores [VOICE] system messages, no raw phone
  R5  /webhooks/twilio/voice/status: AnsweredBy=machine_start -> voicemail + recovery SMS;
      cost computed from real CallDuration

No real Twilio / Claude calls. Standalone; exits non-zero on failure.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import types

# ---- Environment bootstrap (must come before any app/config import) ----
os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest_va")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_va")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+15550001000")
os.environ.setdefault("FIRSTBACK_PUBLIC_URL", "https://test.firstback.io")

import config

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

# Enable voice and internal-secret so we can test the guards
config.VOICE_PUBLIC_URL = "https://voice.firstback.test"

import db
db.DB_PATH = _TMP.name
db.DB_BACKUP_PATH = ""
db.init_db()

import messaging
messaging.TWILIO_AUTH_TOKEN = "tok_va"
messaging.TWILIO_ACCOUNT_SID = ""   # unconfigured -> send_sms simulates (no network)
messaging.TWILIO_FROM_NUMBER = "+15550001000"

import compliance
compliance.QUIET_START, compliance.QUIET_END = 0, 24   # always "business hours" in tests

import app as _app
import triage

# Wire the INTERNAL_SECRET and VOICE_PUBLIC_URL into the app module copies
_INTERNAL_SECRET = "test_internal_secret_abc123"
_app.INTERNAL_SECRET = _INTERNAL_SECRET
_app.VOICE_PUBLIC_URL = "https://voice.firstback.test"

client = _app.app.test_client()

_pass = _fail = 0

BIZ_NUM = "+15553140001"
CALLER = "+14155550300"
CALLER2 = "+14155550301"
CALLER3 = "+14155550302"
CALLER4 = "+14155550303"

db.set_business_twilio(1, BIZ_NUM, "PN2", forward_to="+15559990001")
db.set_a2p_status(1, "approved")


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


def _sign(token, url, params):
    """Compute a Twilio request signature for testing require_twilio_signature."""
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(
        hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()
    ).decode()


def post_signed(path, params=None):
    """POST with a valid Twilio signature."""
    if params is None:
        params = {}
    url = "http://localhost" + path
    return client.post(
        path, data=params,
        headers={"X-Twilio-Signature": _sign("tok_va", url, params)},
    )


def post_internal(path, payload, secret=None):
    """POST to an /internal/* endpoint with X-Internal-Secret header."""
    if secret is None:
        secret = _INTERNAL_SECRET
    return client.post(
        path,
        data=json.dumps(payload),
        content_type="application/json",
        headers={"X-Internal-Secret": secret},
    )


# ============================================================
# R1 -- STOP / detect_revocation / cancel->opt-out clear voice_ok
# ============================================================
print("\n---- R1: opt-out paths clear voice_ok ----")

# Seed a lead with voice_ok=1 for CALLER
_lead_r1 = db.create_lead(1, "R1 Caller", CALLER)
db.set_voice_consent(1, CALLER, True)   # set voice_ok=1
_consent_before = db.get_consent(1, CALLER)
check("R1 setup: voice_ok=1 before STOP",
      _consent_before is not None and _consent_before.get("voice_ok") == 1)

# Send STOP
r_stop = post_signed("/webhooks/twilio/sms/inbound",
                     {"To": BIZ_NUM, "From": CALLER, "Body": "STOP"})
check("R1 STOP returns 200", r_stop.status_code == 200)
_consent_after_stop = db.get_consent(1, CALLER)
check("R1 STOP clears voice_ok to 0",
      _consent_after_stop is not None and _consent_after_stop.get("voice_ok") == 0)

# Seed a second lead with voice_ok=1, then trigger NLU revocation
_lead_r1b = db.create_lead(1, "R1b Caller", CALLER2)
db.set_voice_consent(1, CALLER2, True)
_consent_before_b = db.get_consent(1, CALLER2)
check("R1b setup: voice_ok=1 before NLU revocation",
      _consent_before_b is not None and _consent_before_b.get("voice_ok") == 1)

# "please remove me from your list" triggers compliance.detect_revocation
r_nlu = post_signed("/webhooks/twilio/sms/inbound",
                    {"To": BIZ_NUM, "From": CALLER2,
                     "Body": "please remove me from your list"})
check("R1b NLU revocation returns 200", r_nlu.status_code == 200)
_consent_after_nlu = db.get_consent(1, CALLER2)
check("R1b detect_revocation clears voice_ok to 0",
      _consent_after_nlu is not None and _consent_after_nlu.get("voice_ok") == 0)

# Seed a third lead with voice_ok=1, then test cancel->opt-out path
# (cancel falls back to opt-out when there is no pending estimate to cancel)
_lead_r1c = db.create_lead(1, "R1c Caller", CALLER3)
db.set_voice_consent(1, CALLER3, True)
_consent_before_c = db.get_consent(1, CALLER3)
check("R1c setup: voice_ok=1 before cancel->opt-out",
      _consent_before_c is not None and _consent_before_c.get("voice_ok") == 1)

r_cancel = post_signed("/webhooks/twilio/sms/inbound",
                       {"To": BIZ_NUM, "From": CALLER3, "Body": "cancel"})
check("R1c cancel->opt-out returns 200", r_cancel.status_code == 200)
_consent_after_c = db.get_consent(1, CALLER3)
check("R1c cancel->opt-out clears voice_ok to 0",
      _consent_after_c is not None and _consent_after_c.get("voice_ok") == 0)


# ============================================================
# R2 -- Pre-call guard: voice_ok=0 skips place_call
# ============================================================
print("\n---- R2: pre-call guards ----")

# CALLER already has voice_ok=0 after STOP above.
# Sending "call me" must not place a call.
_place_call_calls = []
_orig_place_call = messaging.place_call


def _spy_place_call(biz, to, twiml_url, add_amd=False, status_callback=None):
    _place_call_calls.append({"to": to, "twiml_url": twiml_url})
    return {"status": "placed", "sid": "CA_spy_001"}


messaging.place_call = _spy_place_call

_place_call_calls.clear()
r_voice_ok0 = post_signed("/webhooks/twilio/sms/inbound",
                           {"To": BIZ_NUM, "From": CALLER, "Body": "call me"})
check("R2a voice_ok=0: place_call NOT called",
      len(_place_call_calls) == 0)
check("R2a voice_ok=0: endpoint returns 200", r_voice_ok0.status_code == 200)

# R2b: spam score >= SCREEN_SCORE_HARD skips place_call
# Use CALLER4 (no prior opt-out) but override triage.spam_score to return HARD score.
_lead_r2b = db.create_lead(1, "R2b Spam", CALLER4)
db.set_voice_consent(1, CALLER4, True)

_orig_spam_score = triage.spam_score


def _spam_high(_signals):
    return (config.SCREEN_SCORE_HARD + 1, ["forced-high"])


triage.spam_score = _spam_high

_place_call_calls.clear()
r_spam = post_signed("/webhooks/twilio/sms/inbound",
                     {"To": BIZ_NUM, "From": CALLER4, "Body": "call me"})
check("R2b spam>=HARD: place_call NOT called",
      len(_place_call_calls) == 0)
check("R2b spam>=HARD: endpoint returns 200", r_spam.status_code == 200)

triage.spam_score = _orig_spam_score

# R2c: called within 60 minutes skips place_call
# Insert a recent voice_calls row for CALLER4
_lead_r2c_id = db.create_lead(1, "R2c DedupeCallerA", "+14155550400")
db.set_voice_consent(1, "+14155550400", True)

from datetime import datetime, timezone, timedelta
_recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
import sqlite3 as _sqlite3
_conn_ded = _sqlite3.connect(_TMP.name)
_conn_ded.execute(
    "INSERT INTO voice_calls (biz_id, lead_id, twilio_sid, started_at, outcome, cost_cents, created_at)"
    " VALUES (?,?,?,?,?,?,?)",
    (1, _lead_r2c_id, "CA_dedup_001", _recent_ts, "in_progress", 0,
     datetime.now(timezone.utc).isoformat()),
)
_conn_ded.commit()
_conn_ded.close()

_place_call_calls.clear()
r_dedup = post_signed("/webhooks/twilio/sms/inbound",
                      {"To": BIZ_NUM, "From": "+14155550400", "Body": "call me"})
check("R2c <60min de-dupe: place_call NOT called",
      len(_place_call_calls) == 0)
check("R2c <60min de-dupe: endpoint returns 200", r_dedup.status_code == 200)

# R2d: monthly cap exceeded skips place_call
_caller_cap = "+14155550401"
_lead_r2d = db.create_lead(1, "R2d CapCaller", _caller_cap)
db.set_voice_consent(1, _caller_cap, True)

# Insert a large-cost voice_calls row to blow the cap
_conn_cap = _sqlite3.connect(_TMP.name)
_conn_cap.execute(
    "INSERT INTO voice_calls (biz_id, lead_id, twilio_sid, started_at, outcome, cost_cents, created_at)"
    " VALUES (?,?,?,?,?,?,?)",
    (1, _lead_r2d, "CA_cap_001",
     datetime.now(timezone.utc).isoformat(), "booked",
     config.VOICE_MONTHLY_CAP_CENTS + 100,
     datetime.now(timezone.utc).isoformat()),
)
_conn_cap.commit()
_conn_cap.close()

_place_call_calls.clear()
r_cap = post_signed("/webhooks/twilio/sms/inbound",
                    {"To": BIZ_NUM, "From": _caller_cap, "Body": "call me"})
check("R2d monthly cap: place_call NOT called",
      len(_place_call_calls) == 0)
check("R2d monthly cap: endpoint returns 200", r_cap.status_code == 200)

# Restore original place_call
messaging.place_call = _orig_place_call


# ============================================================
# R3 -- /internal/voice/stream: secret-gated, uses Haiku, no booking write
# ============================================================
print("\n---- R3: /internal/voice/stream ----")

# Wrong/no secret -> 403
r_stream_bad = client.post(
    "/internal/voice/stream",
    data=json.dumps({"biz": 1, "lead": 1, "text": "hi", "history": []}),
    content_type="application/json",
    headers={"X-Internal-Secret": "wrongsecret"},
)
check("R3 wrong secret returns 403", r_stream_bad.status_code == 403)

r_stream_none = client.post(
    "/internal/voice/stream",
    data=json.dumps({"biz": 1, "lead": 1, "text": "hi", "history": []}),
    content_type="application/json",
)
check("R3 missing secret returns 403", r_stream_none.status_code == 403)

# Create a lead for stream tests
_stream_lead_id = db.create_lead(1, "Stream Lead", "+14155550500")

# Capture any calls to handle_inbound and llm.complete_stream_voice
_handle_inbound_calls = []
import app as _app_ref
_orig_handle_inbound = _app_ref.handle_inbound


def _spy_handle_inbound(biz, lead, text):
    _handle_inbound_calls.append(text)
    return ("reply", False, False)


_app_ref.handle_inbound = _spy_handle_inbound

# Capture model used in complete_stream_voice calls
_stream_models_captured = []
import llm as _llm

_orig_stream_voice = _llm.complete_stream_voice


def _spy_stream_voice(system, messages, **kw):
    # Record the model that would be used -- complete_stream_voice always uses CLAUDE_MODEL_VOICE
    _stream_models_captured.append(config.CLAUDE_MODEL_VOICE)
    yield "hello"


_llm.complete_stream_voice = _spy_stream_voice

_handle_inbound_calls.clear()
_stream_models_captured.clear()

r_stream_ok = post_internal(
    "/internal/voice/stream",
    {"biz": 1, "lead": _stream_lead_id, "text": "I need a quote", "history": []},
)
check("R3 correct secret returns 200", r_stream_ok.status_code == 200)
check("R3 response mimetype is SSE",
      "text/event-stream" in r_stream_ok.content_type)

_stream_body = r_stream_ok.get_data(as_text=True)
check("R3 SSE body contains data: lines", "data:" in _stream_body)
check("R3 SSE body contains done=true sentinel", '"done": true' in _stream_body or
      '"done":true' in _stream_body)

# P0-2 CHECK: handle_inbound must NOT be called during streaming
check("R3 handle_inbound NOT called during stream (P0-2)",
      len(_handle_inbound_calls) == 0)

# Model check: complete_stream_voice was called (uses CLAUDE_MODEL_VOICE by definition)
check("R3 complete_stream_voice (Haiku path) was invoked",
      len(_stream_models_captured) > 0)
check("R3 model recorded is CLAUDE_MODEL_VOICE (Haiku)",
      _stream_models_captured and "haiku" in _stream_models_captured[0].lower())

# Restore
_llm.complete_stream_voice = _orig_stream_voice
_app_ref.handle_inbound = _orig_handle_inbound


# ============================================================
# R4 -- /internal/voice/turn_log: secret-gated, [VOICE] messages, no raw phone
# ============================================================
print("\n---- R4: /internal/voice/turn_log ----")

_tl_lead_id = db.create_lead(1, "TurnLog Lead", "+14155550600")

# Wrong secret -> 403
r_tl_bad = client.post(
    "/internal/voice/turn_log",
    data=json.dumps({"biz": 1, "lead": _tl_lead_id, "turns": []}),
    content_type="application/json",
    headers={"X-Internal-Secret": "bad"},
)
check("R4 wrong secret returns 403", r_tl_bad.status_code == 403)

# Valid request with turns containing a phone number in the text
_turns = [
    {"in": "Hi I am at 123 Main Street", "out": "Great, what time works?"},
    {"in": "Call me at +14155550600 to confirm", "out": "I will follow up by text."},
]
r_tl_ok = post_internal(
    "/internal/voice/turn_log",
    {"biz": 1, "lead": _tl_lead_id, "turns": _turns},
)
check("R4 correct secret returns 200", r_tl_ok.status_code == 200)

_tl_data = r_tl_ok.get_json()
check("R4 response contains written count",
      isinstance(_tl_data.get("written"), int) and _tl_data["written"] > 0)

# Verify messages were written as direction='system' with [VOICE] prefix
_messages = db.get_messages(_tl_lead_id)
_voice_msgs = [m for m in _messages if m.get("direction") == "system"]
check("R4 system messages written to lead thread", len(_voice_msgs) >= 2)
check("R4 all [VOICE] messages have [VOICE] prefix",
      all("[VOICE]" in (m.get("body") or "") for m in _voice_msgs))

# PII check: no raw phone number in message bodies
_raw_phone = "+14155550600"
check("R4 raw phone number scrubbed from [VOICE] message bodies",
      not any(_raw_phone in (m.get("body") or "") for m in _voice_msgs))


# ============================================================
# R5 -- /webhooks/twilio/voice/status: AMD voicemail, cost, recovery SMS
# ============================================================
print("\n---- R5: /webhooks/twilio/voice/status ----")

# Seed a voice_call row + lead for the status webhook
_r5_caller = "+14155550700"
_r5_lead_id = db.create_lead(1, "Status Lead", _r5_caller)
_r5_vc_id = db.insert_voice_call(1, _r5_lead_id, "CA_status_001")

# Track recovery SMS sends
_sms_sent = []
_orig_send_sms = messaging.send_sms


def _spy_send_sms(biz, to, body, **kw):
    _sms_sent.append({"to": to, "body": body})
    return {"status": "simulated"}


messaging.send_sms = _spy_send_sms

# AnsweredBy=machine_start -> outcome=voicemail + recovery SMS
_sms_sent.clear()
r_vm = post_signed(
    "/webhooks/twilio/voice/status",
    {
        "CallSid": "CA_status_001",
        "CallStatus": "in-progress",
        "AnsweredBy": "machine_start",
        "CallDuration": "45",
        "To": BIZ_NUM,
        "From": _r5_caller,
    },
)
check("R5 voicemail status returns 200", r_vm.status_code == 200)

# Check outcome was updated to voicemail
_conn_r5 = _sqlite3.connect(_TMP.name)
_row_r5 = _conn_r5.execute(
    "SELECT outcome, cost_cents FROM voice_calls WHERE twilio_sid=?", ("CA_status_001",)
).fetchone()
_conn_r5.close()
check("R5 AnsweredBy=machine_start sets outcome=voicemail",
      _row_r5 is not None and _row_r5[0] == "voicemail")

# Cost: 45s -> ceil(45/30)=2 blocks * 25 cents = 50 cents
check("R5 cost computed from real CallDuration (45s -> 50 cents)",
      _row_r5 is not None and _row_r5[1] == 50)

# Recovery SMS was sent
check("R5 voicemail triggers recovery SMS",
      len(_sms_sent) > 0)
if _sms_sent:
    check("R5 recovery SMS body mentions 'phone'",
          "phone" in _sms_sent[0]["body"].lower())

# R5b: status=no-answer -> outcome=no_answer, no recovery SMS
_r5_lead_2 = db.create_lead(1, "No-Answer Lead", "+14155550701")
_r5_vc_id2 = db.insert_voice_call(1, _r5_lead_2, "CA_status_002")
_sms_sent.clear()

r_noanswer = post_signed(
    "/webhooks/twilio/voice/status",
    {
        "CallSid": "CA_status_002",
        "CallStatus": "no-answer",
        "AnsweredBy": "",
        "CallDuration": "0",
        "To": BIZ_NUM,
        "From": "+14155550701",
    },
)
check("R5b no-answer returns 200", r_noanswer.status_code == 200)
_conn_r5b = _sqlite3.connect(_TMP.name)
_row_r5b = _conn_r5b.execute(
    "SELECT outcome, cost_cents FROM voice_calls WHERE twilio_sid=?", ("CA_status_002",)
).fetchone()
_conn_r5b.close()
check("R5b no-answer sets outcome=no_answer",
      _row_r5b is not None and _row_r5b[0] == "no_answer")
check("R5b no-answer has zero cost (0s duration)",
      _row_r5b is not None and _row_r5b[1] == 0)
check("R5b no-answer does NOT send recovery SMS",
      len(_sms_sent) == 0)

# Restore
messaging.send_sms = _orig_send_sms


# ============================================================
# R6 -- /internal/voice/turn __RECOVERY_SMS__ sentinel (5g P1, UN-MOCKED)
# The voice service relays post-call recovery texts through /internal/voice/turn
# with a sentinel prefix. The endpoint must send them DIRECTLY via messaging and
# must NOT feed the sentinel into handle_inbound (booking brain). This test spies
# at the app boundary (no _send_recovery_sms mock) so the bug can't be masked.
# ============================================================
print("\n---- R6: __RECOVERY_SMS__ sentinel routed to send_sms, not handle_inbound ----")

_r6_caller = "+14155550800"
_r6_lead_id = db.create_lead(1, "Recovery Lead", _r6_caller)

_r6_sms = []
_r6_hi_calls = []
_orig_send_sms_r6 = messaging.send_sms
_orig_hi_r6 = _app_ref.handle_inbound


def _spy_send_sms_r6(biz, to, body, **kw):
    _r6_sms.append({"to": to, "body": body})
    return {"status": "simulated"}


def _spy_hi_r6(biz, lead, text):
    _r6_hi_calls.append(text)
    return ("reply", False, False)


messaging.send_sms = _spy_send_sms_r6
_app_ref.handle_inbound = _spy_hi_r6

_r6_body = "I enjoyed our chat -- any questions, just text here."
r_recov = post_internal(
    "/internal/voice/turn",
    {"biz": 1, "lead": _r6_lead_id, "text": f"__RECOVERY_SMS__:{_r6_body}"},
)
check("R6 sentinel turn returns 200", r_recov.status_code == 200)
_r6_json = r_recov.get_json()
check("R6 response flags recovery_sms=True", bool(_r6_json.get("recovery_sms")))
check("R6 handle_inbound NOT called on sentinel (no booking-brain corruption)",
      len(_r6_hi_calls) == 0)
check("R6 recovery SMS sent directly via messaging.send_sms",
      len(_r6_sms) == 1)
check("R6 SMS body is the stripped recovery text (sentinel removed)",
      _r6_sms and _r6_sms[0]["body"] == _r6_body)
check("R6 SMS addressed to the lead phone",
      _r6_sms and _r6_caller[-10:] in _r6_sms[0]["to"])

# Sanity: a NORMAL (non-sentinel) turn still reaches handle_inbound.
_r6_sms.clear()
_r6_hi_calls.clear()
r_normal = post_internal(
    "/internal/voice/turn",
    {"biz": 1, "lead": _r6_lead_id, "text": "I need a quote for my fence"},
)
check("R6 normal turn returns 200", r_normal.status_code == 200)
check("R6 normal turn DOES reach handle_inbound", len(_r6_hi_calls) == 1)
check("R6 normal turn sends no recovery SMS", len(_r6_sms) == 0)

messaging.send_sms = _orig_send_sms_r6
_app_ref.handle_inbound = _orig_hi_r6


# ============================================================
# R7 -- AMD machine_end_beep is a voicemail value (5g P1)
# machine_end_beep (reached-the-beep) previously fell through to the error/no-recovery
# branch. It must classify as voicemail + fire a recovery SMS.
# ============================================================
print("\n---- R7: machine_end_beep -> voicemail + recovery SMS ----")

_r7_caller = "+14155550810"
_r7_lead_id = db.create_lead(1, "Beep Lead", _r7_caller)
db.insert_voice_call(1, _r7_lead_id, "CA_beep_001")

_r7_sms = []
_orig_send_sms_r7 = messaging.send_sms


def _spy_send_sms_r7(biz, to, body, **kw):
    _r7_sms.append({"to": to, "body": body})
    return {"status": "simulated"}


messaging.send_sms = _spy_send_sms_r7

r_beep = post_signed(
    "/webhooks/twilio/voice/status",
    {
        "CallSid": "CA_beep_001",
        "CallStatus": "in-progress",
        "AnsweredBy": "machine_end_beep",
        "CallDuration": "30",
        "To": BIZ_NUM,
        "From": _r7_caller,
    },
)
check("R7 machine_end_beep returns 200", r_beep.status_code == 200)
_conn_r7 = _sqlite3.connect(_TMP.name)
_row_r7 = _conn_r7.execute(
    "SELECT outcome FROM voice_calls WHERE twilio_sid=?", ("CA_beep_001",)
).fetchone()
_conn_r7.close()
check("R7 machine_end_beep sets outcome=voicemail",
      _row_r7 is not None and _row_r7[0] == "voicemail")
check("R7 machine_end_beep fires a recovery SMS", len(_r7_sms) == 1)

messaging.send_sms = _orig_send_sms_r7


# ============================================================
# R8 -- completed does NOT clobber a prior voicemail/no_answer (5g P2)
# The AMD callback and the final completed callback share this URL. On `completed`
# the webhook meters but must not overwrite a terminal outcome already set by AMD.
# A clean finished call (still in_progress) becomes 'completed'.
# ============================================================
print("\n---- R8: completed callback does not clobber AMD outcome ----")

_r8_caller = "+14155550820"
_r8_lead = db.create_lead(1, "Clobber Lead", _r8_caller)
db.insert_voice_call(1, _r8_lead, "CA_clobber_001")

_orig_send_sms_r8 = messaging.send_sms
messaging.send_sms = lambda *a, **k: {"status": "simulated"}

# 1) AMD callback classifies voicemail.
post_signed("/webhooks/twilio/voice/status", {
    "CallSid": "CA_clobber_001", "CallStatus": "in-progress",
    "AnsweredBy": "machine_end_beep", "CallDuration": "30",
    "To": BIZ_NUM, "From": _r8_caller})
# 2) Final completed callback (no AnsweredBy) -- must NOT clobber voicemail.
post_signed("/webhooks/twilio/voice/status", {
    "CallSid": "CA_clobber_001", "CallStatus": "completed",
    "AnsweredBy": "", "CallDuration": "60",
    "To": BIZ_NUM, "From": _r8_caller})

_conn_r8 = _sqlite3.connect(_TMP.name)
_row_r8 = _conn_r8.execute(
    "SELECT outcome, duration_seconds, cost_cents FROM voice_calls "
    "WHERE twilio_sid=?", ("CA_clobber_001",)).fetchone()
_conn_r8.close()
check("R8 completed does NOT clobber voicemail outcome",
      _row_r8 is not None and _row_r8[0] == "voicemail")
check("R8 metering still updated by completed (60s -> 50 cents)",
      _row_r8 is not None and _row_r8[1] == 60 and _row_r8[2] == 50)

# A clean finished call (no AMD) -> completed.
_r8b_lead = db.create_lead(1, "Clean Lead", "+14155550821")
db.insert_voice_call(1, _r8b_lead, "CA_clean_001")
post_signed("/webhooks/twilio/voice/status", {
    "CallSid": "CA_clean_001", "CallStatus": "completed",
    "AnsweredBy": "", "CallDuration": "30",
    "To": BIZ_NUM, "From": "+14155550821"})
_conn_r8b = _sqlite3.connect(_TMP.name)
_row_r8b = _conn_r8b.execute(
    "SELECT outcome FROM voice_calls WHERE twilio_sid=?", ("CA_clean_001",)).fetchone()
_conn_r8b.close()
check("R8 clean completed call (no AMD) -> outcome=completed",
      _row_r8b is not None and _row_r8b[0] == "completed")

messaging.send_sms = _orig_send_sms_r8


# ============================================================
# Report
# ============================================================
print(f"\n==== {_pass} passed, {_fail} failed ====")
sys.exit(1 if _fail else 0)
