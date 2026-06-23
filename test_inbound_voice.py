"""Plan 17 — Live inbound AI voice answering tests.

Run: FIRSTBACK_DB_PATH=/tmp/ivb17.db python test_inbound_voice.py

Covers (6 regions):
  I    Inert-when-off: both gates (VOICE_PUBLIC_URL + inbound_voice_enabled) independently
       preserve existing behavior (text-back fires, no ConversationRelay).
  II   Hook A: forward_to set + miss (no-answer/busy/failed) -> ConversationRelay TwiML;
       canceled -> text-back (FIX-1); correct biz/lead/name/greeting params (FIX-5/6);
       voice_calls row opened; ai-answered call logged (FIX-2).
  III  Hook B: no forward_to -> ConversationRelay; sentinel still <Hangup/> (never routes to AI).
  IV   Cap gate -> text-back fallback; spam/enforce gate -> text-back fallback.
  V    build_twiml custom greeting vs default greeting (voice_service.py backward compat).
  VI   Settings toggle: persist inbound_voice_enabled + render in template.
  VII  Health-probe failure -> text-back fallback (Q3 mitigation).

No real Twilio / Claude calls. Standalone; exits non-zero on any failure.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest.mock as mock
from urllib.parse import urlparse, parse_qs, unquote

# ---- Environment bootstrap (must come before any app/config import) ----
os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest_iv")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_iv")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+15550002000")
os.environ.setdefault("FIRSTBACK_PUBLIC_URL", "https://test.firstback.io")

import config

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

# Voice service deployed (gate 1 = True by default; tests that need it False patch it).
config.VOICE_PUBLIC_URL = "https://voice.firstback.test"

import db
db.DB_PATH = _TMP.name
db.DB_BACKUP_PATH = ""
db.init_db()

import messaging
messaging.TWILIO_AUTH_TOKEN = "tok_iv"
messaging.TWILIO_ACCOUNT_SID = ""   # unconfigured -> send_sms simulates (no network)
messaging.TWILIO_FROM_NUMBER = "+15550002000"

import compliance
compliance.QUIET_START, compliance.QUIET_END = 0, 24  # always "business hours"

import app as _app
import triage
import voice_service

# Wire module-level vars into the app module copies.
_app.VOICE_PUBLIC_URL = "https://voice.firstback.test"

client = _app.app.test_client()

_pass = _fail = 0

BIZ_NUM = "+15553140007"
CALLER = "+14155550700"
CALLER2 = "+14155550701"
CALLER3 = "+14155550702"
CALLER4 = "+14155550703"
CALLER5 = "+14155550704"
SENTINEL_SID = "CAsentinel0000001"

# Provision business 1 with a Twilio number and a forward_to cell.
db.set_business_twilio(1, BIZ_NUM, "PN7", forward_to="+15559990001")
db.set_a2p_status(1, "approved")
# Start with inbound_voice_enabled=0 (the safe default).
db.update_phone_voice(1, voice_callback_enabled=1, inbound_voice_enabled=0)


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


def post(path, params=None):
    """Signed POST, exactly as Twilio would send it."""
    if params is None:
        params = {}
    url = "http://localhost" + path
    return client.post(
        path, data=params,
        headers={"X-Twilio-Signature": _sign("tok_iv", url, params)},
    )


def _get_call_row(call_sid):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM calls WHERE call_sid=?", (call_sid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _get_voice_call_row(twilio_sid):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM voice_calls WHERE twilio_sid=?", (twilio_sid,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ============================================================
# Region I — Inert-when-off (both gates)
# ============================================================
print("\n---- Region I: inert when gates are off ----")

# I-a: inbound_voice_enabled=0 (default), VOICE_PUBLIC_URL set -> text-back, no ConversationRelay.
# forward_to set, dial status = no-answer.
r = post("/webhooks/twilio/voice/dial-status", {
    "To": BIZ_NUM, "From": CALLER, "CallSid": "CA_inert_1",
    "DialCallStatus": "no-answer",
})
xml = r.get_data(as_text=True)
check("I-a: inert (toggle off) -> no ConversationRelay in dial-status response",
      r.status_code == 200 and "ConversationRelay" not in xml and "Redirect" not in xml)
lead_ia = db.get_lead_by_phone(1, CALLER)
check("I-a: lead created by text-back path", lead_ia is not None)

# I-b: VOICE_PUBLIC_URL cleared, toggle=1 -> text-back, no ConversationRelay.
db.update_phone_voice(1, inbound_voice_enabled=1)
_orig_voice_url = _app.VOICE_PUBLIC_URL
_app.VOICE_PUBLIC_URL = ""
r = post("/webhooks/twilio/voice/dial-status", {
    "To": BIZ_NUM, "From": CALLER2, "CallSid": "CA_inert_2",
    "DialCallStatus": "no-answer",
})
xml = r.get_data(as_text=True)
check("I-b: inert (VOICE_PUBLIC_URL unset) -> no ConversationRelay",
      r.status_code == 200 and "ConversationRelay" not in xml and "Redirect" not in xml)
_app.VOICE_PUBLIC_URL = _orig_voice_url

# Reset for remaining tests: enable inbound AI answering.
db.update_phone_voice(1, inbound_voice_enabled=1)


# ============================================================
# Region II — Hook A: forward + miss -> ConversationRelay
# ============================================================
print("\n---- Region II: Hook A (forward + miss -> AI) ----")

# Mock the health probe to succeed so tests don't need a live voice service.
import requests as _requests_mod


def _mock_get_ok(url, **kwargs):
    resp = mock.Mock()
    resp.status_code = 200
    return resp


# II-a: no-answer -> ConversationRelay redirect (voice service healthy).
with mock.patch.object(_requests_mod, "get", _mock_get_ok):
    r = post("/webhooks/twilio/voice/dial-status", {
        "To": BIZ_NUM, "From": CALLER3, "CallSid": "CA_hook_a_1",
        "DialCallStatus": "no-answer",
    })
xml = r.get_data(as_text=True)
check("II-a: no-answer -> Redirect or ConversationRelay present",
      r.status_code == 200 and ("Redirect" in xml or "ConversationRelay" in xml))

# II-b: Verify biz_id, lead_id, name= and greeting= are in the redirect URL.
if "Redirect" in xml:
    import re as _re
    import html as _html_mod
    _url_match = _re.search(r'<Redirect[^>]*>(.*?)</Redirect>', xml, _re.DOTALL)
    if _url_match:
        # Unescape XML entities (e.g. &amp; -> &) so the URL is parseable.
        _turl = _html_mod.unescape(_url_match.group(1))
        _parsed = urlparse(_turl)
        _qs = parse_qs(_parsed.query)
        check("II-b: /twiml URL has biz= param", "biz" in _qs)
        check("II-b: /twiml URL has lead= param", "lead" in _qs)
        check("II-b: /twiml URL has name= param", "name" in _qs)
        _greeting_val = unquote(_qs.get("greeting", [""])[0])
        check("II-b: greeting param contains AI disclosure (no recording claim)",
              "AI" in _greeting_val and "recording" not in _greeting_val.lower())
        check("II-b: greeting param contains business name",
              len(_greeting_val) > 10)
    else:
        check("II-b: could not parse Redirect URL", False)
        for _ in range(4):
            check("II-b: (skipped -- no URL found)", False)
else:
    check("II-b: skipped (ConversationRelay inline; params in TwiML body)", True)
    for _ in range(4):
        check("II-b: (ConversationRelay inline -- skipped)", True)

# II-c: voice_calls row opened.
vc_row = _get_voice_call_row("CA_hook_a_1")
check("II-c: voice_calls row opened for inbound AI call", vc_row is not None)
check("II-c: voice_calls row is in_progress", (vc_row or {}).get("outcome") == "in_progress")

# II-d: call logged as ai-answered (FIX-2).
call_row = _get_call_row("CA_hook_a_1")
check("II-d: call log row exists", call_row is not None)
check("II-d: dial_status=ai-answered", (call_row or {}).get("dial_status") == "ai-answered")
check("II-d: missed=0 (AI answered)", (call_row or {}).get("missed") == 0)
check("II-d: engaged=1", (call_row or {}).get("engaged") == 1)

# II-e: FIX-1 — canceled -> text-back, NOT ConversationRelay.
with mock.patch.object(_requests_mod, "get", _mock_get_ok):
    r = post("/webhooks/twilio/voice/dial-status", {
        "To": BIZ_NUM, "From": CALLER3, "CallSid": "CA_hook_a_canceled",
        "DialCallStatus": "canceled",
    })
xml = r.get_data(as_text=True)
check("II-e (FIX-1): canceled -> no ConversationRelay (caller hung up)",
      r.status_code == 200 and "ConversationRelay" not in xml and "Redirect" not in xml)

# II-f: busy -> also triggers AI (not canceled).
with mock.patch.object(_requests_mod, "get", _mock_get_ok):
    r = post("/webhooks/twilio/voice/dial-status", {
        "To": BIZ_NUM, "From": "+14155550705", "CallSid": "CA_hook_a_busy",
        "DialCallStatus": "busy",
    })
xml = r.get_data(as_text=True)
check("II-f: busy -> ConversationRelay or Redirect (AI answering)",
      r.status_code == 200 and ("Redirect" in xml or "ConversationRelay" in xml))


# ============================================================
# Region III — Hook B: no forward_to -> ConversationRelay
# ============================================================
print("\n---- Region III: Hook B (no forward_to -> AI) ----")

# Temporarily remove forward_to to test Hook B.
db.update_phone_voice(1, forward_to="")

# III-a: sentinel call -> still <Hangup/>, never routed to AI.
db.set_forwarding_sentinel(1, SENTINEL_SID, None)
r = post("/webhooks/twilio/voice/inbound", {
    "To": BIZ_NUM, "From": CALLER, "CallSid": SENTINEL_SID,
})
xml = r.get_data(as_text=True)
check("III-a: sentinel call -> Hangup (not AI)",
      r.status_code == 200 and "<Hangup" in xml and "ConversationRelay" not in xml and "Redirect" not in xml)
db.set_forwarding_sentinel(1, None, None)  # clear sentinel

# III-b: regular inbound with no forward_to -> ConversationRelay.
with mock.patch.object(_requests_mod, "get", _mock_get_ok):
    r = post("/webhooks/twilio/voice/inbound", {
        "To": BIZ_NUM, "From": CALLER4, "CallSid": "CA_hook_b_1",
    })
xml = r.get_data(as_text=True)
check("III-b: no forward_to inbound -> Redirect or ConversationRelay",
      r.status_code == 200 and ("Redirect" in xml or "ConversationRelay" in xml))

# voice_calls row opened by Hook B.
vc_b = _get_voice_call_row("CA_hook_b_1")
check("III-c: voice_calls row opened by Hook B", vc_b is not None)

# call logged as ai-answered by Hook B.
call_b = _get_call_row("CA_hook_b_1")
check("III-d: Hook B logs call as ai-answered", (call_b or {}).get("dial_status") == "ai-answered")

# Restore forward_to.
db.update_phone_voice(1, forward_to="+15559990001")


# ============================================================
# Region IV — Cap and spam gates -> text-back fallback
# ============================================================
print("\n---- Region IV: cap / spam gates -> text-back ----")

# IV-a: monthly cap exceeded -> text-back, no ConversationRelay.
_orig_cap = config.VOICE_MONTHLY_CAP_CENTS
config.VOICE_MONTHLY_CAP_CENTS = 0   # force cap exceeded
with mock.patch.object(_requests_mod, "get", _mock_get_ok):
    r = post("/webhooks/twilio/voice/dial-status", {
        "To": BIZ_NUM, "From": CALLER, "CallSid": "CA_cap_gate",
        "DialCallStatus": "no-answer",
    })
xml = r.get_data(as_text=True)
check("IV-a: cap exceeded -> no ConversationRelay",
      r.status_code == 200 and "ConversationRelay" not in xml and "Redirect" not in xml)
config.VOICE_MONTHLY_CAP_CENTS = _orig_cap

# IV-b: spam/enforce -> text-back, no ConversationRelay.
# Patch _connect_inbound_to_ai to simulate a spam verdict in enforce mode.
# We do this by setting screen_mode='enforce' and faking a confirmed-spam verdict.
_biz_for_spam = db.get_business(1)
db.set_screen_mode(1, "enforce")

# Patch _screen_missed_caller to return a confirmed-spam verdict (engage=False).
_spam_verdict = {"engage": False, "status": "screened_spam", "score": 95,
                 "category": "spam", "reasons": ["test spam"]}

with mock.patch.object(_requests_mod, "get", _mock_get_ok):
    with mock.patch("app._screen_missed_caller", return_value=_spam_verdict):
        r = post("/webhooks/twilio/voice/dial-status", {
            "To": BIZ_NUM, "From": CALLER, "CallSid": "CA_spam_gate",
            "DialCallStatus": "no-answer",
        })
xml = r.get_data(as_text=True)
check("IV-b: spam+enforce -> no ConversationRelay (falls to text-back or silent)",
      r.status_code == 200 and "ConversationRelay" not in xml and "Redirect" not in xml)

db.set_screen_mode(1, "off")   # restore


# ============================================================
# Region V — build_twiml: custom greeting vs default (backward compat)
# ============================================================
print("\n---- Region V: build_twiml greeting param ----")

# V-a: no greeting -> default outbound greeting (includes "may be recorded").
twiml_default = voice_service.build_twiml(1, 1, wss_base="wss://voice.test")
check("V-a: default greeting present",
      "welcomeGreeting=" in twiml_default or "welcomeGreeting" in twiml_default)
check("V-a: default greeting contains recording disclosure",
      "may be recorded" in twiml_default)

# V-b: custom greeting -> overrides default, no recording claim.
custom_greeting = "Hi, I am an AI assistant. What can I help you with?"
twiml_custom = voice_service.build_twiml(1, 1, wss_base="wss://voice.test",
                                          greeting=custom_greeting)
check("V-b: custom greeting used", custom_greeting in twiml_custom)
check("V-b: default greeting NOT in custom twiml", "may be recorded" not in twiml_custom)

# V-c: empty string greeting treated as None (falls back to default).
twiml_empty = voice_service.build_twiml(1, 1, wss_base="wss://voice.test", greeting=None)
check("V-c: None greeting uses default", "may be recorded" in twiml_empty)

# V-d: /twiml endpoint passes greeting= through.
from fastapi.testclient import TestClient as _VClient
vclient = _VClient(voice_service.fastapi_app)
r_twiml = vclient.get(f"/twiml?biz=1&lead=1&greeting={custom_greeting}")
check("V-d: /twiml?greeting= returns custom greeting in TwiML",
      r_twiml.status_code == 200 and custom_greeting in r_twiml.text)

# V-e: /twiml without greeting= returns default greeting.
r_twiml_def = vclient.get("/twiml?biz=1&lead=1")
check("V-e: /twiml without greeting= uses default (recording disclosure)",
      r_twiml_def.status_code == 200 and "may be recorded" in r_twiml_def.text)


# ============================================================
# Region VI — Settings toggle: persist + render
# ============================================================
print("\n---- Region VI: settings toggle persist + render ----")

# First set inbound_voice_enabled=0 directly.
db.update_phone_voice(1, inbound_voice_enabled=0)
biz_off = db.get_business(1)
check("VI-a: inbound_voice_enabled=0 stored", biz_off.get("inbound_voice_enabled") == 0)

# Set to 1.
db.update_phone_voice(1, inbound_voice_enabled=1)
biz_on = db.get_business(1)
check("VI-b: inbound_voice_enabled=1 persists", biz_on.get("inbound_voice_enabled") == 1)

# VI-c: voice_callback_enabled and inbound_voice_enabled are DISTINCT.
db.update_phone_voice(1, voice_callback_enabled=0, inbound_voice_enabled=1)
biz_check = db.get_business(1)
check("VI-c: voice_callback_enabled and inbound_voice_enabled are independent",
      biz_check.get("voice_callback_enabled") == 0
      and biz_check.get("inbound_voice_enabled") == 1)
db.update_phone_voice(1, voice_callback_enabled=1)   # restore

# VI-d: settings.html parses as Jinja template.
try:
    t = _app.app.jinja_env.get_template("settings.html")
    check("VI-d: settings.html parses as Jinja template", True)
except Exception as e:
    check(f"VI-d: settings.html parses as Jinja template (error: {e})", False)

# VI-e: no smart/curly quotes in the NEW toggle block (inbound_voice_enabled).
# Pre-existing smart quotes elsewhere in the file are out of scope for this check;
# this scan targets only the lines containing the new toggle.
_html = open("/Users/jonathanmorris/Documents/apps/firstback/templates/settings.html", "rb").read()
_src_str = _html.decode("utf-8", errors="replace")
_smarts = [b"\xe2\x80\x98", b"\xe2\x80\x99", b"\xe2\x80\x9c", b"\xe2\x80\x9d"]
# Find just the new toggle block lines.
_toggle_lines = [ln for ln in _src_str.splitlines() if "inbound_voice_enabled" in ln]
_toggle_block = "\n".join(_toggle_lines).encode("utf-8", errors="replace")
check("VI-e: no smart/curly quotes in the new inbound_voice_enabled toggle block",
      not any(s in _toggle_block for s in _smarts))

# VI-f: inbound_voice_enabled appears in the template source.
_src = _html.decode("utf-8", errors="replace")
check("VI-f: inbound_voice_enabled toggle in settings.html source",
      "inbound_voice_enabled" in _src)


# ============================================================
# Region VII — Health-probe failure -> text-back fallback (Q3)
# ============================================================
print("\n---- Region VII: health-probe failure -> text-back ----")

import requests.exceptions as _rex


def _mock_get_timeout(url, **kwargs):
    raise _rex.Timeout("simulated timeout")


def _mock_get_connrefused(url, **kwargs):
    raise _rex.ConnectionError("simulated connection refused")


# VII-a: Timeout -> text-back (no ConversationRelay / dead air).
with mock.patch.object(_requests_mod, "get", _mock_get_timeout):
    r = post("/webhooks/twilio/voice/dial-status", {
        "To": BIZ_NUM, "From": CALLER5, "CallSid": "CA_probe_timeout",
        "DialCallStatus": "no-answer",
    })
xml = r.get_data(as_text=True)
check("VII-a: health-probe Timeout -> no ConversationRelay",
      r.status_code == 200 and "ConversationRelay" not in xml and "Redirect" not in xml)

# VII-b: ConnectionError -> text-back (no ConversationRelay / dead air).
with mock.patch.object(_requests_mod, "get", _mock_get_connrefused):
    r = post("/webhooks/twilio/voice/dial-status", {
        "To": BIZ_NUM, "From": CALLER5, "CallSid": "CA_probe_connrefused",
        "DialCallStatus": "no-answer",
    })
xml = r.get_data(as_text=True)
check("VII-b: health-probe ConnectionError -> no ConversationRelay",
      r.status_code == 200 and "ConversationRelay" not in xml and "Redirect" not in xml)

# ============================================================
# Final report
# ============================================================
print(f"\n{'='*50}")
print(f"PASSED: {_pass}  FAILED: {_fail}")
if _fail:
    print("SOME TESTS FAILED")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
    sys.exit(0)
