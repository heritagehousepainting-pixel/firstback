"""Phase 0 BETA — Platform alert channel tests. Run: python test_alert_channel.py

Verifies:
  1. Owner-alert SMS sends FROM ALERT_FROM_NUMBER when it is set, not the tenant number.
  2. A newly-signed-up business row has alert_email + toggles populated (not NULL).

Exits 0 on all pass, 1 if any fail. Standalone-script style (no pytest).
"""
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
# Set a platform alert number for the test.
_PLATFORM_ALERT_NUM = "+15550000001"
os.environ["ALERT_FROM_NUMBER"] = _PLATFORM_ALERT_NUM
# Provide Twilio creds so configured() returns True and real sends don't simulate early.
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+15550000099")

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
import alerts
import app as _app

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


# ---- Test 1: owner alert uses ALERT_FROM_NUMBER when set -----------------------
# We spy on the Twilio POST so we can inspect the From field without a real network call.
import requests as _rq

_sent_data = []
_real_post = _rq.post


def _spy_post(url, auth=None, data=None, timeout=None, **kw):
    _sent_data.append({"url": url, "data": dict(data or {})})
    # Return a minimal mock response (200 OK, fake sid).
    class _Resp:
        status_code = 201
        def raise_for_status(self):
            pass
        def json(self):
            return {"sid": "SMfake001"}
    return _Resp()


# Wire up the spy and ensure messaging sees the platform ALERT_FROM_NUMBER.
_rq.post = _spy_post
messaging.ALERT_FROM_NUMBER = _PLATFORM_ALERT_NUM
messaging.TWILIO_ACCOUNT_SID = "ACtest"
messaging.TWILIO_AUTH_TOKEN = "tok_test"
messaging.TWILIO_FROM_NUMBER = "+15550000099"  # tenant fallback number

# Build a minimal business dict (no twilio_number so _from_number falls back to TWILIO_FROM_NUMBER).
_biz = {"id": 1, "alert_sms": "+15559991234", "alert_on_lead": 1}

# Send an owner alert (gate=False path).
result = messaging.send_sms(_biz, "+15559991234", "Test owner alert", gate=False)

check("send_sms returns sent when Twilio configured + alert number set",
      result.get("status") == "sent")
check("owner alert (gate=False) sends FROM the platform ALERT_FROM_NUMBER",
      bool(_sent_data) and _sent_data[-1]["data"].get("From") == _PLATFORM_ALERT_NUM)

# Verify a customer-facing send (gate=True, approved) still uses the tenant's fallback number.
# We need a2p_ready to return True — mark business as approved.
db.set_a2p_status(1, "approved")
_biz_approved = {"id": 1, "twilio_number": None, "a2p_status": "approved",
                 "a2p_brand_sid": "BN1", "a2p_service_sid": "MG1", "a2p_campaign_sid": "CM1"}
_sent_data.clear()
result2 = messaging.send_sms(_biz_approved, "+15559991234", "Customer text", gate=True)
check("customer-facing send (gate=True) still uses tenant from-number (not alert number)",
      bool(_sent_data) and _sent_data[-1]["data"].get("From") == "+15550000099")

# Restore requests.post.
_rq.post = _real_post


# ---- Test 2: signup route populates alert_email + toggles ON -----------------
# We need a fresh business; call the signup route directly via the test client.
# Suppress Twilio further (we don't need real sends here).
messaging.TWILIO_ACCOUNT_SID = ""  # simulated sends only

with _app.app.test_request_context():
    pass  # ensure app context initialized

r = client.post("/signup", data={
    "business": "Test Painter LLC",
    "owner": "Alice",
    "email": "alice@testpainter.example",
    "password": "securepassword1",
    "trade": "painting",
})

# Should redirect to /setup.
check("signup redirects to /setup",
      r.status_code in (301, 302) and "/setup" in (r.headers.get("Location") or ""))

# Find the newly created business.
conn = db.get_conn()
row = conn.execute(
    "SELECT b.id, b.alert_email, b.alert_sms, b.alert_on_lead, b.alert_on_booking, b.alert_on_urgent "
    "FROM businesses b JOIN users u ON u.business_id = b.id "
    "WHERE u.email = ?",
    ("alice@testpainter.example",)
).fetchone()
conn.close()

check("new signup row exists with alert_email populated",
      row is not None and row["alert_email"] == "alice@testpainter.example")
check("new signup row has alert_on_lead = 1",
      row is not None and row["alert_on_lead"] == 1)
check("new signup row has alert_on_booking = 1",
      row is not None and row["alert_on_booking"] == 1)
check("new signup row has alert_on_urgent = 1",
      row is not None and row["alert_on_urgent"] == 1)


# ---- Test 3: ALERT_FROM_NUMBER unset => fallback to tenant number ------------
messaging.ALERT_FROM_NUMBER = ""  # simulate unset
messaging.TWILIO_ACCOUNT_SID = "ACtest"
messaging.TWILIO_AUTH_TOKEN = "tok_test"
_rq.post = _spy_post
_sent_data.clear()

# Business with its own provisioned number — should use it as fallback.
_biz_owned = {"id": 2, "twilio_number": "+15551112222", "a2p_status": None}
result3 = messaging.send_sms(_biz_owned, "+15559991234", "Fallback test", gate=False)
check("when ALERT_FROM_NUMBER unset, owner alert falls back to tenant twilio_number",
      bool(_sent_data) and _sent_data[-1]["data"].get("From") == "+15551112222")
_rq.post = _real_post


# ---- Teardown ---------------------------------------------------------------
import os as _os
_os.unlink(_TMP.name)

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
