"""SF-7 Forwarding sentinel checks. Run: python test_sf7_sentinel.py

Covers:
  1. send_sentinel_call stores the SID when Twilio places the call ("placed").
  2. Inbound twilio_voice_inbound matching the sentinel CallSid confirms forwarding
     + clears the sentinel + records the probe time.
  3. A non-matching CallSid does NOT confirm forwarding.
  4. confirmed is NEVER set on "placed" (the honesty rule -- [DECIDED]).
  5. check_forwarding_health fires a probe when last_probe_at is null.
  6. check_forwarding_health fires a probe when last_probe_at is >7d old.
  7. check_forwarding_health does NOT fire a probe when last_probe_at is <7d old.
  8. check_forwarding_health flips confirmed=False + fires forwarding_lost alert
     when a sentinel SID was placed but never confirmed within the timeout window.
  9. /webhooks/twilio/voice/sentinel-twiml returns a Twilio-signed 200 with
     <Say>+<Hangup> TwiML.

Agent 1's not-yet-existing db functions (set_forwarding_sentinel, set_forwarding_probe,
set_business_timezone) are stubbed at the db module level. No network.
"""
import base64
import hashlib
import hmac
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

os.environ.setdefault("FIRSTBACK_PROVIDER", "demo")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")
os.environ.setdefault("FIRSTBACK_PUBLIC_URL", "https://ringback-gixe.onrender.com")
os.environ.setdefault("FIRSTBACK_TASKS_SECRET", "tasks_secret_test")

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

# ---- Stub Agent 1's not-yet-existing db functions ----
# These will be defined in db.py by Agent 1. For standalone tests we define
# them as thin dict-backed stubs so our tests pass without db.py changes.
_sentinel_store = {}   # business_id -> {"sid": ..., "sent_at": ...} or None
_probe_store = {}      # business_id -> last_probe_at ISO string
_tz_store = {}         # business_id -> tz_name


def _stub_set_forwarding_sentinel(business_id, sid, sent_at):
    """Stub for db.set_forwarding_sentinel(business_id, sid, sent_at).
    None, None clears the sentinel."""
    if sid is None:
        _sentinel_store[business_id] = None
    else:
        _sentinel_store[business_id] = {"sid": sid, "sent_at": sent_at}


def _stub_set_forwarding_probe(business_id):
    """Stub for db.set_forwarding_probe(business_id) -- sets forwarding_last_probe_at."""
    _probe_store[business_id] = datetime.now(timezone.utc).isoformat()


def _stub_set_business_timezone(business_id, tz_name):
    _tz_store[business_id] = tz_name


def _stub_set_google_event_id(appointment_id, event_id):
    pass   # not needed in this test file


db.set_forwarding_sentinel = _stub_set_forwarding_sentinel
db.set_forwarding_probe = _stub_set_forwarding_probe
db.set_business_timezone = _stub_set_business_timezone
db.set_google_event_id = _stub_set_google_event_id

# ---- Wire the test business ----
BIZ_NUM = "+15553140000"
CELL = "+15559990000"
db.set_business_twilio(1, BIZ_NUM, "PN1", forward_to=CELL)
db.update_a2p_profile(1, {"ein": "12-3456789",
                           "business_address": "1 Main St, Philadelphia PA"})
db.set_a2p_status(1, "approved")
db.set_forwarding_confirmed(1, False)

import messaging
import connections

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- Twilio signature helper ----
def _sign(token, url, params):
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(
        hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


# ---- Import app for route tests ----
import app as _app
messaging.TWILIO_AUTH_TOKEN = "tok_test"
client = _app.app.test_client()


def post_signed(path, params):
    url = "http://localhost" + path
    return client.post(path, data=params,
                       headers={"X-Twilio-Signature": _sign("tok_test", url, params)})


# ===========================================================================
# 1. send_sentinel_call: SID stored on "placed"
# ===========================================================================
import requests as _rq_mod

_place_call_calls = []


def _fake_place_call(business, to, twiml_url, status_callback=None):
    _place_call_calls.append({"to": to, "twiml_url": twiml_url})
    return {"status": "placed", "sid": "CA_sentinel_001"}


_orig_place_call = messaging.place_call
messaging.place_call = _fake_place_call
_sentinel_store.clear()

result = connections.send_sentinel_call(1)
check("send_sentinel_call returns 'placed' status",
      result.get("status") == "placed")
check("send_sentinel_call returns the call SID",
      result.get("sid") == "CA_sentinel_001")
check("send_sentinel_call stores the SID via set_forwarding_sentinel",
      _sentinel_store.get(1, {}) and _sentinel_store[1].get("sid") == "CA_sentinel_001")
check("send_sentinel_call stores a sent_at timestamp",
      bool(_sentinel_store.get(1, {}) and _sentinel_store[1].get("sent_at")))

# [DECIDED] HONESTY RULE: forwarding_confirmed MUST NOT be True after "placed".
check("[DECIDED] confirmed is NOT set True after send_sentinel_call (honesty rule)",
      db.get_business(1)["forwarding_confirmed"] == 0)

messaging.place_call = _orig_place_call


# ===========================================================================
# 2. Inbound matching CallSid -> confirms forwarding + clears + records probe
# ===========================================================================
# Set a sentinel SID directly on the business row (simulating what set_forwarding_sentinel
# would do after Agent 1's migration adds the column). Since the column doesn't exist
# yet in db.py, we monkeypatch db.get_business to inject the sentinel field.
_orig_get_biz_by_num = db.get_business_by_twilio_number


def _biz_with_sentinel(number):
    biz = _orig_get_biz_by_num(number)
    if biz and biz.get("twilio_number", "").endswith("3140000"):
        biz = dict(biz)
        biz["forwarding_sentinel_sid"] = "CA_inbound_match"
    return biz


db.get_business_by_twilio_number = _biz_with_sentinel
db.set_forwarding_confirmed(1, False)
_probe_store.clear()
_sentinel_store.clear()

r = post_signed("/webhooks/twilio/voice/inbound",
                {"To": BIZ_NUM, "From": CELL, "CallSid": "CA_inbound_match"})
xml = r.get_data(as_text=True)

check("sentinel inbound match: route returns 200", r.status_code == 200)
check("sentinel inbound match: response is a Hangup (not Dial/Say)",
      "<Hangup" in xml and "<Dial" not in xml and "missed you" not in xml)
check("sentinel inbound match: forwarding_confirmed is now True",
      db.get_business(1)["forwarding_confirmed"] == 1)
check("sentinel inbound match: sentinel is cleared (set to None)",
      _sentinel_store.get(1) is None)
check("sentinel inbound match: probe time was recorded (set_forwarding_probe called)",
      bool(_probe_store.get(1)))

db.get_business_by_twilio_number = _orig_get_biz_by_num


# ===========================================================================
# 3. Non-matching CallSid does NOT confirm forwarding
# ===========================================================================
_orig_get_biz_by_num2 = db.get_business_by_twilio_number


def _biz_with_sentinel_other(number):
    biz = _orig_get_biz_by_num2(number)
    if biz and biz.get("twilio_number", "").endswith("3140000"):
        biz = dict(biz)
        biz["forwarding_sentinel_sid"] = "CA_different_sentinel"
    return biz


db.get_business_by_twilio_number = _biz_with_sentinel_other
db.set_forwarding_confirmed(1, False)

r_normal = post_signed("/webhooks/twilio/voice/inbound",
                       {"To": BIZ_NUM, "From": CELL, "CallSid": "CA_not_the_sentinel"})
xml_normal = r_normal.get_data(as_text=True)

check("non-matching CallSid: does NOT confirm forwarding",
      db.get_business(1)["forwarding_confirmed"] == 0)
check("non-matching CallSid: normal call routing proceeds (<Dial> for forward_to)",
      "<Dial" in xml_normal)

db.get_business_by_twilio_number = _orig_get_biz_by_num2


# ===========================================================================
# 4. [DECIDED] confirmed is NEVER set True from the /setup/forwarding route
#    when Twilio IS configured and a sentinel is placed.
# ===========================================================================
messaging.place_call = lambda biz, to, url, **kw: {"status": "placed", "sid": "CA_setup"}
db.set_forwarding_confirmed(1, False)
client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                             "password": config.SEED_OWNER_PASSWORD})
with client.session_transaction() as _sess:
    _sess["csrf_token"] = "test_csrf"
client.environ_base["HTTP_X_CSRF_TOKEN"] = "test_csrf"
_sentinel_store.clear()
r_setup = client.post("/setup/forwarding",
                      data={"mode": "dial", "forward_to": CELL})
loc = r_setup.headers.get("Location", "")
check("[DECIDED] /setup/forwarding with a placed sentinel redirects (not error)",
      r_setup.status_code in (301, 302))
check("[DECIDED] /setup/forwarding redirects to ?verifying=1 (not ?saved=forwarding only)",
      "verifying=1" in loc)
check("[DECIDED] forwarding_confirmed is still False after 'placed' (honesty rule)",
      db.get_business(1)["forwarding_confirmed"] == 0)

messaging.place_call = _orig_place_call


# ===========================================================================
# 5 & 6 & 7. check_forwarding_health: probe timing logic
# ===========================================================================
# We need to test check_forwarding_health with various last_probe_at values.
# Since the DB column doesn't exist yet, we patch db.list_businesses to return
# controlled business dicts.
import alerts as _alerts_mod

_alerts_fired = []
_orig_notify_async = _alerts_mod.notify_async
_alerts_mod.notify_async = lambda biz, kind, *a, **k: _alerts_fired.append(kind)

_orig_place_call2 = messaging.place_call
_probe_calls = []
messaging.place_call = lambda biz, to, url, **kw: (_probe_calls.append(to), {"status": "placed", "sid": "CA_probe"})[1]

# --- 5. Probe fires when last_probe_at is None ---
_probe_calls.clear()
_probe_store.clear()
_alerts_fired.clear()

_orig_list_biz = db.list_businesses
db.list_businesses = lambda: [{
    "id": 1, "forwarding_confirmed": 1, "forward_to": CELL,
    "forwarding_sentinel_sid": None, "forwarding_sentinel_at": None,
    "forwarding_last_probe_at": None,
    "twilio_number": BIZ_NUM,
}]
db.set_forwarding_probe = _stub_set_forwarding_probe
connections.check_forwarding_health()
check("check_forwarding_health fires probe when last_probe_at is None",
      len(_probe_calls) > 0)

# --- 6. Probe fires when last_probe_at is >7d old ---
_probe_calls.clear()
old_probe = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
db.list_businesses = lambda: [{
    "id": 1, "forwarding_confirmed": 1, "forward_to": CELL,
    "forwarding_sentinel_sid": None, "forwarding_sentinel_at": None,
    "forwarding_last_probe_at": old_probe,
    "twilio_number": BIZ_NUM,
}]
connections.check_forwarding_health()
check("check_forwarding_health fires probe when last_probe_at is >7d old",
      len(_probe_calls) > 0)

# --- 7. Probe does NOT fire when last_probe_at is <7d old ---
_probe_calls.clear()
recent_probe = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
db.list_businesses = lambda: [{
    "id": 1, "forwarding_confirmed": 1, "forward_to": CELL,
    "forwarding_sentinel_sid": None, "forwarding_sentinel_at": None,
    "forwarding_last_probe_at": recent_probe,
    "twilio_number": BIZ_NUM,
}]
connections.check_forwarding_health()
check("check_forwarding_health does NOT probe when last_probe_at is <7d old",
      len(_probe_calls) == 0)

# --- 8. Timed-out sentinel -> flip confirmed=False + forwarding_lost alert ---
_probe_calls.clear()
_alerts_fired.clear()
db.set_forwarding_confirmed(1, True)  # start confirmed
# A sentinel that was placed 200s ago (past the 120s timeout)
old_sentinel_at = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
db.list_businesses = lambda: [{
    "id": 1, "forwarding_confirmed": 1, "forward_to": CELL,
    "forwarding_sentinel_sid": "CA_timed_out",
    "forwarding_sentinel_at": old_sentinel_at,
    "forwarding_last_probe_at": None,
    "twilio_number": BIZ_NUM,
}]
connections.check_forwarding_health()
check("timed-out sentinel: forwarding_confirmed flipped to False",
      db.get_business(1)["forwarding_confirmed"] == 0)
check("timed-out sentinel: forwarding_lost alert fired",
      "forwarding_lost" in _alerts_fired)
check("timed-out sentinel: sentinel cleared",
      _sentinel_store.get(1) is None)
check("timed-out sentinel: no new probe fired (confirmed already flipped)",
      len(_probe_calls) == 0)

db.list_businesses = _orig_list_biz
messaging.place_call = _orig_place_call2
_alerts_mod.notify_async = _orig_notify_async


# ===========================================================================
# 9. /webhooks/twilio/voice/sentinel-twiml returns Say+Hangup TwiML
# ===========================================================================
r_twiml = post_signed("/webhooks/twilio/voice/sentinel-twiml",
                       {"CallSid": "CAsent1", "To": BIZ_NUM, "From": CELL})
xml_twiml = r_twiml.get_data(as_text=True)
check("sentinel-twiml returns 200", r_twiml.status_code == 200)
check("sentinel-twiml contains <Say>", "<Say>" in xml_twiml)
check("sentinel-twiml contains <Hangup>", "<Hangup" in xml_twiml)

# Unsigned request to sentinel-twiml should be rejected (403).
r_unauth = client.post("/webhooks/twilio/voice/sentinel-twiml",
                        data={"CallSid": "CAbad"})
check("sentinel-twiml rejects unsigned request (403)",
      r_unauth.status_code == 403)


print(f"==== {_pass} passed, {_fail} failed ====")
sys.exit(1 if _fail else 0)
