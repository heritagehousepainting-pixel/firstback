"""Phase-4 C: Dispatcher Call tests.  Run: python test_dispatcher_call.py

Covers:
  1. /twiml/dispatcher/<id> TwiML contains the lead's last inbound message words.
  2. /twiml/dispatcher/<id> returns 200 and valid XML (Twilio-signed).
  3. Unsigned request to /twiml/dispatcher/<id> is rejected (403).
  4. /twiml/dispatcher/connect/<id> with digit "1" returns a Dial TwiML.
  5. /twiml/dispatcher/connect/<id> with wrong digit returns Hangup.
  6. Unsigned request to /twiml/dispatcher/connect/<id> is rejected (403).
  7. Urgent path calls messaging.place_call when VOICE_PUBLIC_URL is set
     and owner cell is configured and not already called.
  8. Urgent path records dispatcher_call_last_at via db.set_dispatcher_call_at.
  9. Rate-limit: a second urgent turn does NOT re-place a dispatcher call.
 10. When place_call returns "simulated", no db.set_dispatcher_call_at is called
     (no false claim that a call was placed).
 11. When place_call returns "error", no db.set_dispatcher_call_at is called.
 12. When VOICE_PUBLIC_URL is empty/None, no place_call is attempted.

Stubs (Agent A / B seams):
  - db.get_last_inbound_message(lead_id) -> str
  - db.set_dispatcher_call_at(lead_id, ts)
"""

import base64
import hashlib
import hmac
import os
import sys
import tempfile

os.environ.setdefault("FIRSTBACK_PROVIDER", "demo")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")
os.environ.setdefault("FIRSTBACK_PUBLIC_URL", "https://ringback-gixe.onrender.com")
os.environ.setdefault("FIRSTBACK_TASKS_SECRET", "tasks_secret_test")
# VOICE_PUBLIC_URL must be set so the dispatcher path fires
os.environ["FIRSTBACK_VOICE_PUBLIC_URL"] = "https://ringback-gixe.onrender.com"

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

# ---- Stub Agent A's not-yet-existing db functions ----
_last_inbound_store = {}     # lead_id -> str
_dispatcher_call_store = {}  # lead_id -> ts
_milestone_sent_store = {}   # business_id -> ts


def _stub_get_last_inbound_message(lead_id):
    return _last_inbound_store.get(lead_id, "")


def _stub_set_dispatcher_call_at(lead_id, ts):
    _dispatcher_call_store[lead_id] = ts


def _stub_set_roi_milestone_sent(business_id, ts):
    _milestone_sent_store[business_id] = ts


db.get_last_inbound_message = _stub_get_last_inbound_message
db.set_dispatcher_call_at = _stub_set_dispatcher_call_at
db.set_roi_milestone_sent = _stub_set_roi_milestone_sent

# ---- Stub Agent A's roi module ----
import types
_roi_stub = types.ModuleType("roi")
_roi_stub.check_roi_milestone = lambda bid: None  # always returns None — not milestone focus here
sys.modules["roi"] = _roi_stub

import messaging
import app as _app

messaging.TWILIO_AUTH_TOKEN = "tok_test"
client = _app.app.test_client()

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


def _sign(token, url, params):
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(
        hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def post_signed(path, params=None):
    if params is None:
        params = {}
    url = "http://localhost" + path
    return client.post(path, data=params,
                       headers={"X-Twilio-Signature": _sign("tok_test", url, params)})


# ---- Build a business + lead ----
BIZ_NUM = "+15553140000"
CELL = "+15559990000"
db.set_business_twilio(1, BIZ_NUM, "PN1", forward_to=CELL)
db.update_a2p_profile(1, {"ein": "12-3456789",
                           "business_address": "1 Main St, Philadelphia PA"})
db.set_a2p_status(1, "approved")

lead_id = db.create_lead(1, "Test Urgency", "+15551234567")
_last_inbound_store[lead_id] = "The pipe burst and water is flooding my basement!"


# ===========================================================================
# 1 + 2. /twiml/dispatcher/<id> returns TwiML with last inbound message
# ===========================================================================
r = post_signed(f"/twiml/dispatcher/{lead_id}", {"CallSid": "CA001", "To": BIZ_NUM, "From": CELL})
xml = r.get_data(as_text=True)

check("dispatcher TwiML returns 200", r.status_code == 200)
check("dispatcher TwiML contains the caller's last words",
      "pipe burst and water is flooding" in xml)
check("dispatcher TwiML contains <Gather>", "<Gather" in xml)
check("dispatcher TwiML contains <Say>", "<Say>" in xml)
check("dispatcher TwiML action points to connect route",
      f"/twiml/dispatcher/connect/{lead_id}" in xml)


# ===========================================================================
# 3. Unsigned request to /twiml/dispatcher/<id> is rejected
# ===========================================================================
r_unauth = client.post(f"/twiml/dispatcher/{lead_id}",
                       data={"CallSid": "CAbad", "To": BIZ_NUM})
check("dispatcher TwiML rejects unsigned request (403)",
      r_unauth.status_code == 403)


# ===========================================================================
# 4. /twiml/dispatcher/connect/<id> with digit "1" returns Dial
# ===========================================================================
r_conn = post_signed(f"/twiml/dispatcher/connect/{lead_id}",
                     {"Digits": "1", "CallSid": "CA002", "To": BIZ_NUM})
xml_conn = r_conn.get_data(as_text=True)
check("dispatcher connect with '1' returns 200", r_conn.status_code == 200)
check("dispatcher connect with '1' returns <Dial>", "<Dial>" in xml_conn)
check("dispatcher connect dials the lead's phone number",
      "+15551234567" in xml_conn)


# ===========================================================================
# 5. /twiml/dispatcher/connect/<id> with wrong digit returns Hangup
# ===========================================================================
r_no = post_signed(f"/twiml/dispatcher/connect/{lead_id}",
                   {"Digits": "2", "CallSid": "CA003", "To": BIZ_NUM})
xml_no = r_no.get_data(as_text=True)
check("dispatcher connect with '2' returns 200", r_no.status_code == 200)
check("dispatcher connect with '2' does NOT dial (no <Dial>)", "<Dial>" not in xml_no)
check("dispatcher connect with '2' includes Hangup or Goodbye",
      "<Hangup" in xml_no or "Goodbye" in xml_no)


# ===========================================================================
# 6. Unsigned request to /twiml/dispatcher/connect/<id> is rejected
# ===========================================================================
r_unauth2 = client.post(f"/twiml/dispatcher/connect/{lead_id}",
                         data={"Digits": "1"})
check("dispatcher connect rejects unsigned request (403)",
      r_unauth2.status_code == 403)


# ===========================================================================
# 7 + 8. Urgent path calls place_call and records dispatcher_call_last_at
# ===========================================================================
_place_call_calls = []
_orig_place_call = messaging.place_call


def _fake_place_call_placed(biz, to, twiml_url, status_callback=None):
    _place_call_calls.append({"to": to, "twiml_url": twiml_url, "biz_id": biz.get("id")})
    return {"status": "placed", "sid": "CA_dispatcher_001"}


messaging.place_call = _fake_place_call_placed
_dispatcher_call_store.clear()
_place_call_calls.clear()

# Wire a business that has an alert_sms and VOICE_PUBLIC_URL is set
import config as _config_mod
_orig_voice_url = _config_mod.VOICE_PUBLIC_URL
_config_mod.VOICE_PUBLIC_URL = "https://ringback-gixe.onrender.com"
_app.VOICE_PUBLIC_URL = "https://ringback-gixe.onrender.com"

# Stub ai.detect_urgency to return True
import ai as _ai_mod
_orig_detect_urgency = _ai_mod.detect_urgency
_ai_mod.detect_urgency = lambda body: True

# Stub ai.generate_reply to return a fixed reply (no booking)
_orig_gen_reply = _ai_mod.generate_reply
_ai_mod.generate_reply = lambda biz, history, **kw: ("We will be right there.", None)

# Build a lead WITHOUT dispatcher_call_last_at (so rate-limit doesn't block)
lead_id2 = db.create_lead(1, "Urgent Caller", "+15557654321")
_last_inbound_store[lead_id2] = "My roof is collapsing!"
lead_row2 = db.get_lead(lead_id2)
# Make sure it has no dispatcher_call_last_at (simulated via lead dict)
# The lead row won't have this column since Agent A hasn't added it yet;
# it returns None / missing key naturally.

biz_row = db.get_business(1)
biz_row = dict(biz_row)
biz_row["alert_sms"] = "+15559990000"  # owner's cell

from app import handle_inbound as _handle_inbound

reply, booked, urgent = _handle_inbound(biz_row, lead_row2, "My roof is collapsing!")

check("urgent path: place_call was called once", len(_place_call_calls) == 1)
check("urgent path: place_call targeted the owner cell",
      _place_call_calls[0]["to"] == "+15559990000")
check("urgent path: TwiML URL points to dispatcher route",
      f"/twiml/dispatcher/{lead_id2}" in _place_call_calls[0]["twiml_url"])
check("urgent path: dispatcher_call_last_at was recorded",
      _dispatcher_call_store.get(lead_id2) is not None)
check("urgent path: handle_inbound returned urgent=True", urgent is True)


# ===========================================================================
# 9. Rate-limit: second urgent turn does NOT re-place a dispatcher call
# ===========================================================================
_place_call_calls.clear()

# Simulate lead already having dispatcher_call_last_at set
from datetime import datetime, timezone as _tzutc
_already_ts = datetime.now(_tzutc.utc).isoformat()
lead_row2_already = dict(lead_row2)
lead_row2_already["dispatcher_call_last_at"] = _already_ts

reply2, booked2, urgent2 = _handle_inbound(biz_row, lead_row2_already,
                                            "STILL urgent please come now!")

check("rate-limit: second urgent turn does NOT call place_call again",
      len(_place_call_calls) == 0)


# ===========================================================================
# 10. place_call returns "simulated" -> no db.set_dispatcher_call_at
# ===========================================================================
def _fake_place_call_simulated(biz, to, twiml_url, status_callback=None):
    _place_call_calls.append({"status": "simulated"})
    return {"status": "simulated"}


messaging.place_call = _fake_place_call_simulated
_dispatcher_call_store.clear()
_place_call_calls.clear()

lead_id3 = db.create_lead(1, "Simulated Caller", "+15551112222")
_last_inbound_store[lead_id3] = "URGENT please help"
lead_row3 = db.get_lead(lead_id3)

_handle_inbound(biz_row, lead_row3, "URGENT please help")

check("simulated place_call: no dispatcher_call_last_at recorded",
      _dispatcher_call_store.get(lead_id3) is None)


# ===========================================================================
# 11. place_call returns "error" -> no db.set_dispatcher_call_at
# ===========================================================================
def _fake_place_call_error(biz, to, twiml_url, status_callback=None):
    return {"status": "error", "error": "Twilio is down"}


messaging.place_call = _fake_place_call_error
_dispatcher_call_store.clear()

lead_id4 = db.create_lead(1, "Error Caller", "+15553334444")
_last_inbound_store[lead_id4] = "URGENT call me"
lead_row4 = db.get_lead(lead_id4)

_handle_inbound(biz_row, lead_row4, "URGENT call me")

check("error place_call: no dispatcher_call_last_at recorded",
      _dispatcher_call_store.get(lead_id4) is None)


# ===========================================================================
# 12. When VOICE_PUBLIC_URL is empty/None, no place_call is attempted
# ===========================================================================
messaging.place_call = _fake_place_call_placed
_place_call_calls.clear()
_dispatcher_call_store.clear()

_config_mod.VOICE_PUBLIC_URL = ""
_app.VOICE_PUBLIC_URL = ""

lead_id5 = db.create_lead(1, "No URL Caller", "+15555556666")
_last_inbound_store[lead_id5] = "URGENT situation"
lead_row5 = db.get_lead(lead_id5)

_handle_inbound(biz_row, lead_row5, "URGENT situation")

check("no VOICE_PUBLIC_URL: place_call NOT attempted", len(_place_call_calls) == 0)

# Restore
_config_mod.VOICE_PUBLIC_URL = _orig_voice_url
_app.VOICE_PUBLIC_URL = _orig_voice_url
_ai_mod.detect_urgency = _orig_detect_urgency
_ai_mod.generate_reply = _orig_gen_reply
messaging.place_call = _orig_place_call


# 12. SF-10 P2: cross-tenant ownership guard on dispatcher TwiML.
# A request resolving to business 1's number must NOT serve another business's lead.
_b2 = db.create_business({"name": "Other Co", "trade": "plumbing"})
db.set_business_twilio(_b2, "+15558880000", "PN2", forward_to="+15558881111")
_lead_b2 = db.create_lead(_b2, "Other Lead", "+15559990001")
with _app.app.test_request_context(f"/twiml/dispatcher/{_lead_b2}", method="POST",
                                   data={"From": CELL, "To": BIZ_NUM}):
    check("cross-tenant: biz1 number cannot serve biz2's lead",
          _app._dispatcher_lead_owned(_lead_b2) is None)
with _app.app.test_request_context(f"/twiml/dispatcher/{_lead_b2}", method="POST",
                                   data={"From": "+15558881111", "To": "+15558880000"}):
    check("same-tenant: biz2 number serves biz2's lead",
          _app._dispatcher_lead_owned(_lead_b2) is not None)
with _app.app.test_request_context(f"/twiml/dispatcher/{lead_id}", method="POST",
                                   data={"From": CELL, "To": BIZ_NUM}):
    check("same-tenant: biz1 number serves biz1's lead (no regression)",
          _app._dispatcher_lead_owned(lead_id) is not None)


print(f"==== {_pass} passed, {_fail} failed ====")
sys.exit(1 if _fail else 0)
