"""Phase 0 callback-system checks. Run: python3 test_callback.py

No framework: prints each check and a summary, exits non-zero on any failure. DB
tests run against a throwaway temp database so the real firstback.db is untouched.
"""
import base64
import hashlib
import hmac
import os
import sys
import tempfile

# Point storage at a temp DB BEFORE importing db so nothing touches firstback.db.
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name      # db copied DB_PATH at import; override there too
db.init_db()                # builds the schema, incl. the new calls/consent tables

import messaging

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


def _reference_sig(token, url, params):
    """Independent re-implementation of Twilio's algorithm, for round-trip tests."""
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


# ---- valid_signature -------------------------------------------------------
TOKEN = "test-auth-token-12345"
URL = "https://app.firstback.test/webhooks/twilio/sms/inbound"
PARAMS = {"From": "+14155551212", "To": "+18005550100", "Body": "hi there",
          "MessageSid": "SM0123456789abcdef"}
good = _reference_sig(TOKEN, URL, PARAMS)
check("valid_signature accepts a correct signature",
      messaging.valid_signature(URL, PARAMS, good, auth_token=TOKEN) is True)
reordered = {k: PARAMS[k] for k in reversed(list(PARAMS))}  # order must not matter
check("valid_signature is param-order independent",
      messaging.valid_signature(URL, reordered, good, auth_token=TOKEN) is True)
check("valid_signature rejects a tampered body",
      messaging.valid_signature(URL, dict(PARAMS, Body="evil"), good,
                                auth_token=TOKEN) is False)
check("valid_signature rejects the wrong auth token",
      messaging.valid_signature(URL, PARAMS, good, auth_token="nope") is False)
check("valid_signature rejects a wrong URL (proxy scheme mismatch)",
      messaging.valid_signature(URL.replace("https", "http"), PARAMS, good,
                                auth_token=TOKEN) is False)
check("valid_signature rejects an empty signature",
      messaging.valid_signature(URL, PARAMS, "", auth_token=TOKEN) is False)


# ---- send_sms simulated / skipped (no network) -----------------------------
messaging.TWILIO_ACCOUNT_SID = ""   # force "not configured" so nothing hits network
messaging.TWILIO_AUTH_TOKEN = ""
check("send_sms simulates when Twilio not configured",
      messaging.send_sms({"id": 1}, "+14155551212", "hello")["status"] == "simulated")
check("send_sms skips an empty destination",
      messaging.send_sms({"id": 1}, "", "hello")["status"] == "skipped")
check("send_sms skips an empty body",
      messaging.send_sms({"id": 1}, "+14155551212", "")["status"] == "skipped")

lead_id = db.create_lead(1, "Tester", "+14155551212")
before = len(db.get_messages(lead_id))
messaging.send_sms({"id": 1}, "+14155551212", "demo reply", lead_id=lead_id)
after = db.get_messages(lead_id)
check("simulated send_sms records the outbound on the lead thread",
      len(after) == before + 1 and after[-1]["direction"] == "out")


# ---- consent / suppression -------------------------------------------------
check("is_suppressed False before any opt-out",
      db.is_suppressed(1, "+14155559999") is False)
db.set_opt_out(1, "(415) 555-9999")  # different formatting, same number
check("is_suppressed True after opt-out (format-independent)",
      db.is_suppressed(1, "+1 415-555-9999") is True)
check("opt-out is scoped per business", db.is_suppressed(2, "+14155559999") is False)
messaging.TWILIO_ACCOUNT_SID = "AC_fake"  # creds present, but recipient opted out
messaging.TWILIO_AUTH_TOKEN = "fake"
messaging.TWILIO_FROM_NUMBER = "+18005550100"
check("send_sms refuses a suppressed recipient (never hits network)",
      messaging.send_sms({"id": 1}, "+14155559999", "no")["status"] == "suppressed")
messaging.TWILIO_ACCOUNT_SID = messaging.TWILIO_AUTH_TOKEN = ""  # back to unconfigured


# ---- A2P 10DLC customer-traffic gate (audit H1) ----------------------------
# A customer-facing real send must be BLOCKED until the tenant's brand+campaign are
# approved (carriers filter unregistered local traffic); owner alerts (gate=False)
# and the unconfigured demo path are exempt. We spy on requests.post so a "blocked"
# result is provably one that NEVER touched the network.
import requests as _gate_requests

class _GatePostSpy:
    def __init__(self):
        self.calls = []
    def __call__(self, url, *a, **kw):
        self.calls.append((url, a, kw))
        class _R:
            def raise_for_status(self): pass
            def json(self): return {"sid": "SM_gate_ok"}
        return _R()

_g_saved_post = _gate_requests.post
_g_saved_sid = messaging.TWILIO_ACCOUNT_SID
_g_saved_tok = messaging.TWILIO_AUTH_TOKEN
_g_saved_from = messaging.TWILIO_FROM_NUMBER

# A real tenant that is NOT a2p-approved (fresh businesses default "unregistered").
_gate_biz_id = db.create_business({"name": "Gate Co"})
_gate_biz = db.get_business(_gate_biz_id)
_gate_lead = db.create_lead(_gate_biz_id, "Gate Lead", "+14155550123")

# (i) configured + NOT approved + lead_id -> "blocked"/a2p_not_approved, no network,
#     but the outbound IS still recorded on the thread.
messaging.TWILIO_ACCOUNT_SID = "AC_fake"
messaging.TWILIO_AUTH_TOKEN = "fake"
messaging.TWILIO_FROM_NUMBER = "+18005550100"
_spy = _GatePostSpy(); _gate_requests.post = _spy
_g_before = len(db.get_messages(_gate_lead))
_g_res = messaging.send_sms(_gate_biz, "+14155550123", "hi", lead_id=_gate_lead)
check("gate: configured + unapproved customer send is blocked",
      _g_res["status"] == "blocked" and _g_res.get("reason") == "a2p_not_approved")
check("gate: a blocked send never hits the network", len(_spy.calls) == 0)
check("gate: a blocked send still records the outbound on the thread",
      len(db.get_messages(_gate_lead)) == _g_before + 1)

# (ii) configured + approved -> the send proceeds for real ("sent").
db.set_a2p_status(_gate_biz_id, "approved")
_gate_biz = db.get_business(_gate_biz_id)
_spy = _GatePostSpy(); _gate_requests.post = _spy
_g_res = messaging.send_sms(_gate_biz, "+14155550123", "hi", lead_id=_gate_lead)
check("gate: configured + approved customer send proceeds (sent)",
      _g_res["status"] == "sent" and _g_res.get("sid") == "SM_gate_ok")
check("gate: an approved send DID hit the network", len(_spy.calls) == 1)

# (iii) owner alert via gate=False + NOT approved -> NOT blocked (proceeds for real).
_unappr = db.get_business(db.create_business({"name": "Owner Co"}))
_spy = _GatePostSpy(); _gate_requests.post = _spy
_g_res = messaging.send_sms(_unappr, "+14155550199", "owner alert", gate=False)
check("gate: owner alert (gate=False) is exempt even when unapproved",
      _g_res["status"] == "sent")

# (iv) regression: unconfigured + NOT approved -> still "simulated" (gate only fires
#      when configured()).
messaging.TWILIO_ACCOUNT_SID = messaging.TWILIO_AUTH_TOKEN = ""
_g_res = messaging.send_sms(_unappr, "+14155550199", "demo")
check("gate: unconfigured + unapproved still simulates (gate only fires when configured)",
      _g_res["status"] == "simulated")

# Restore network + creds so later blocks/suites are unaffected.
_gate_requests.post = _g_saved_post
messaging.TWILIO_ACCOUNT_SID = _g_saved_sid
messaging.TWILIO_AUTH_TOKEN = _g_saved_tok
messaging.TWILIO_FROM_NUMBER = _g_saved_from


# ---- tenant lookup by Twilio number ---------------------------------------
db.set_business_twilio(1, "+15553140000", "PN_test")
check("get_business_by_twilio_number matches (formatting-independent)",
      (db.get_business_by_twilio_number("(555) 314-0000") or {}).get("id") == 1)
check("get_business_by_twilio_number matches +1 prefix variants",
      (db.get_business_by_twilio_number("+1 555-314-0000") or {}).get("id") == 1)
check("get_business_by_twilio_number returns None for an unknown number",
      db.get_business_by_twilio_number("+19998887777") is None)


# ---- log_call idempotency --------------------------------------------------
db.log_call(1, "CAtest123", from_number="+14155551212", to_number="+15553140000")
db.log_call(1, "CAtest123", from_number="+14155551212", to_number="+15553140000",
            dial_status="no-answer", missed=1)  # same SID -> update, not insert
conn = db.get_conn()
rows = conn.execute("SELECT * FROM calls WHERE call_sid='CAtest123'").fetchall()
conn.close()
check("log_call is idempotent on call_sid (one row)", len(rows) == 1)
check("log_call updates the outcome on a repeat event",
      bool(rows) and rows[0]["missed"] == 1 and rows[0]["dial_status"] == "no-answer")


# ---- message provider sid + delivery status --------------------------------
db.add_message(lead_id, "out", "sent via twilio", provider_sid="SMabc")
db.set_message_delivery("SMabc", "delivered")
conn = db.get_conn()
row = conn.execute("SELECT * FROM messages WHERE provider_sid='SMabc'").fetchone()
conn.close()
check("set_message_delivery records delivery status by provider sid",
      row is not None and row["delivery_status"] == "delivered")


# ---- provision_number: refuse-to-buy guard + webhook wiring ----------------
# provision_number does `import requests` locally, which resolves the module from
# sys.modules; monkeypatching requests.post there intercepts every buy attempt.
import requests as _requests

class _FakePostResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload

class _PostSpy:
    """Records calls to requests.post; returns a canned 201-style response."""
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {}
    def __call__(self, url, *args, **kwargs):
        self.calls.append((url, args, kwargs))
        return _FakePostResp(self.payload)

_saved_post = _requests.post
_saved_base = messaging.PUBLIC_BASE_URL
_saved_sid = messaging.TWILIO_ACCOUNT_SID
_saved_tok = messaging.TWILIO_AUTH_TOKEN
# Credentials present so configured() is True for all three cases below.
messaging.TWILIO_ACCOUNT_SID = "AC_fake"
messaging.TWILIO_AUTH_TOKEN = "fake"

_pn_biz = db.create_business({"name": "Prov Co"})

# 1) refuse-when-no-base: empty base + creds set -> None, and NO buy POST made.
messaging.PUBLIC_BASE_URL = ""
_spy = _PostSpy({"phone_number": "+12675559999", "sid": "PNshould_not"})
_requests.post = _spy
_res = messaging.provision_number(_pn_biz)
check("provision_number refuses to buy with no PUBLIC_BASE_URL", _res is None)
check("provision_number makes no buy POST when no base URL", len(_spy.calls) == 0)

# 2) allow_no_webhooks hatch: empty base but override -> buys; webhooks_wired falsy.
_spy = _PostSpy({"phone_number": "+12675550001", "sid": "PNx"})
_requests.post = _spy
_res = messaging.provision_number(_pn_biz, allow_no_webhooks=True)
check("provision_number(allow_no_webhooks) returns the bought number",
      _res == "+12675550001")
check("provision_number(allow_no_webhooks) did POST once", len(_spy.calls) == 1)
_biz = db.get_business(_pn_biz) or {}
check("provision_number(allow_no_webhooks) records webhooks_wired falsy",
      not _biz.get("webhooks_wired"))

# 3) with-base: webhooks wired, POST carries Voice/Sms URLs, webhooks_wired truthy.
messaging.PUBLIC_BASE_URL = "https://x.example"
_spy = _PostSpy({"phone_number": "+12675550002", "sid": "PNy"})
_requests.post = _spy
_res = messaging.provision_number(_pn_biz)
check("provision_number with base returns the bought number",
      _res == "+12675550002")
_data = _spy.calls[0][2].get("data", {}) if _spy.calls else {}
check("provision_number with base sends VoiceUrl at the inbound path",
      _data.get("VoiceUrl", "").endswith(messaging.VOICE_INBOUND_PATH))
check("provision_number with base sends SmsUrl at the inbound path",
      _data.get("SmsUrl", "").endswith(messaging.SMS_INBOUND_PATH))
_biz = db.get_business(_pn_biz) or {}
check("provision_number with base records webhooks_wired truthy",
      bool(_biz.get("webhooks_wired")))

# Restore network + creds + base so later/other suites are unaffected.
_requests.post = _saved_post
messaging.PUBLIC_BASE_URL = _saved_base
messaging.TWILIO_ACCOUNT_SID = _saved_sid
messaging.TWILIO_AUTH_TOKEN = _saved_tok


# ---- fetch_a2p_campaign_status: corrected URL (audit H2) -------------------
# Regression guard for the A2P status URL. The resource is the singleton list
# endpoint .../Services/{service_sid}/Compliance/Usa2p -- the campaign SID must
# NOT appear in the path (the old broken form was .../Compliance/Usa2p/{campaign}).
# We DON'T stub fetch_a2p_campaign_status; we spy requests.get so a regression to
# the old path would fail here loudly instead of passing silently behind a stub.
import requests as _a2p_requests

class _GetSpy:
    """Records requests.get URLs; returns a canned VERIFIED compliance response."""
    def __init__(self):
        self.calls = []
    def __call__(self, url, *args, **kwargs):
        self.calls.append((url, args, kwargs))
        class _R:
            def raise_for_status(self): pass
            def json(self): return {"compliance": [{"campaign_status": "VERIFIED"}]}
        return _R()

_a2p_saved_get = _a2p_requests.get
_a2p_saved_sid = messaging.TWILIO_ACCOUNT_SID
_a2p_saved_tok = messaging.TWILIO_AUTH_TOKEN
messaging.TWILIO_ACCOUNT_SID = "AC_fake"   # configured() True so the fetch runs
messaging.TWILIO_AUTH_TOKEN = "fake"
_a2p_spy = _GetSpy(); _a2p_requests.get = _a2p_spy
_a2p_status = messaging.fetch_a2p_campaign_status("MGxxx", "CMyyy")
_a2p_url = _a2p_spy.calls[0][0] if _a2p_spy.calls else ""
check("fetch_a2p_campaign_status returns the parsed campaign_status",
      _a2p_status == "VERIFIED")
check("fetch_a2p_campaign_status hits the service-scoped /Compliance/Usa2p endpoint",
      _a2p_url.endswith("/Services/MGxxx/Compliance/Usa2p"))
check("fetch_a2p_campaign_status URL carries NO campaign SID in the path (H2 regression guard)",
      "CMyyy" not in _a2p_url)
# Restore network + creds so the summary/teardown is unaffected.
_a2p_requests.get = _a2p_saved_get
messaging.TWILIO_ACCOUNT_SID = _a2p_saved_sid
messaging.TWILIO_AUTH_TOKEN = _a2p_saved_tok


# ---- summary ---------------------------------------------------------------
os.unlink(_TMP.name)
print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
