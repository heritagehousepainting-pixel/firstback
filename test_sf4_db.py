"""SF-4 DB + callback URL tests. Run: python test_sf4_db.py

Covers:
  - queue_sms_retry round-trips (inserts row, retry_count set, get by id)
  - get_message_by_provider_sid: hit and miss
  - find_scheduled_message: pending found, completed not found
  - sms_status_callback_url: present when FIRSTBACK_PUBLIC_URL set, empty when not
  - status_callback auto-injected in messaging.send_sms (mocked Twilio POST)

No network. Standalone; exit non-zero on failure.
"""
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"

# Set a public URL so sms_status_callback_url() returns something.
os.environ["FIRSTBACK_PUBLIC_URL"] = "https://test.example.com"
# Provide Twilio creds so real-send path is taken (we'll mock the POST).
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest_sf4")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_sf4")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+15550000099")

import config

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name
config.PUBLIC_BASE_URL = "https://test.example.com"

import db
db.DB_PATH = _TMP.name
db.DB_BACKUP_PATH = ""

db.init_db()

import messaging
import requests as _rq

_pass = _fail = 0
_sent_data = []


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- Spy on Twilio POST calls ----
_real_post = _rq.post


def _spy_post(url, auth=None, data=None, timeout=None, **kw):
    _sent_data.append({"url": url, "data": dict(data or {})})

    class _Resp:
        status_code = 201
        def raise_for_status(self):
            pass
        def json(self):
            return {"sid": "SM_sf4_test001"}
    return _Resp()


_rq.post = _spy_post
messaging.TWILIO_ACCOUNT_SID = "ACtest_sf4"
messaging.TWILIO_AUTH_TOKEN = "tok_sf4"
messaging.TWILIO_FROM_NUMBER = "+15550000099"
messaging.ALERT_FROM_NUMBER = ""

# Reload sms_status_callback_url to pick up the new PUBLIC_BASE_URL.
import importlib
importlib.reload(config)
config.DB_PATH = _TMP.name
config.PUBLIC_BASE_URL = "https://test.example.com"
messaging.sms_status_callback_url = config.sms_status_callback_url

# ---- Seed minimal biz + lead ----
# Business 1 is the Heritage seed from init_db.
db.set_a2p_status(1, "approved")
lead_id = db.create_lead(1, "Retry Test Lead", "+15551119999")


# ---- 1. queue_sms_retry round-trip ----
from datetime import datetime, timezone, timedelta

send_at = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
row_id = db.queue_sms_retry(
    business_id=1,
    lead_id=lead_id,
    to="+15551119999",
    body="Your estimate reminder.",
    attempt=1,
    send_at=send_at,
)
check("queue_sms_retry returns an integer id", isinstance(row_id, int) and row_id > 0)

# Read it back directly.
conn = db.get_conn()
row = conn.execute(
    "SELECT * FROM scheduled_messages WHERE id=?", (row_id,)
).fetchone()
conn.close()

check("queue_sms_retry row has kind='sms_retry'", row is not None and row["kind"] == "sms_retry")
check("queue_sms_retry row has retry_count=1", row is not None and row["retry_count"] == 1)
check("queue_sms_retry row has status='pending'", row is not None and row["status"] == "pending")
check("queue_sms_retry body encodes destination",
      row is not None and "[retry_to:+15551119999]" in row["body"])


# ---- 2. queue_sms_retry attempt 2 ----
send_at2 = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
row_id2 = db.queue_sms_retry(1, lead_id, "+15551119999", "Reminder again.", 2, send_at2)
check("queue_sms_retry attempt=2 returns a new id", isinstance(row_id2, int) and row_id2 != row_id)

conn = db.get_conn()
row2 = conn.execute("SELECT * FROM scheduled_messages WHERE id=?", (row_id2,)).fetchone()
conn.close()
check("attempt=2 row has retry_count=2", row2 is not None and row2["retry_count"] == 2)


# ---- 3. get_message_by_provider_sid ----
# Insert a message with a known provider_sid.
db.add_message(lead_id, "out", "Hello from FirstBack", provider_sid="SM_testSID001")
found = db.get_message_by_provider_sid("SM_testSID001")
check("get_message_by_provider_sid: hit returns dict",
      isinstance(found, dict) and found.get("provider_sid") == "SM_testSID001")
check("get_message_by_provider_sid: returned dict has body",
      found is not None and found.get("body") == "Hello from FirstBack")

miss = db.get_message_by_provider_sid("SM_doesnotexist")
check("get_message_by_provider_sid: miss returns None", miss is None)

check("get_message_by_provider_sid: None input returns None",
      db.get_message_by_provider_sid(None) is None)


# ---- 4. find_scheduled_message ----
# The sms_retry row we inserted above should be findable.
found_sched = db.find_scheduled_message(1, lead_id, "sms_retry")
check("find_scheduled_message: pending row found",
      isinstance(found_sched, dict) and found_sched["kind"] == "sms_retry")

# After marking it sent, it should NOT be found (status != pending).
db.mark_scheduled(row_id2, "sent")
found_after_sent = db.find_scheduled_message(1, lead_id, "sms_retry")
# row_id is still pending; row_id2 is sent. Should still find row_id.
check("find_scheduled_message: still finds the OTHER pending retry",
      found_after_sent is not None and found_after_sent["id"] == row_id)

# Mark all sms_retry rows as sent.
db.mark_scheduled(row_id, "sent")
found_none = db.find_scheduled_message(1, lead_id, "sms_retry")
check("find_scheduled_message: returns None when no pending row exists", found_none is None)

# Non-existent kind returns None.
check("find_scheduled_message: unknown kind returns None",
      db.find_scheduled_message(1, lead_id, "no_such_kind") is None)


# ---- 5. sms_status_callback_url ----
cb_url = config.sms_status_callback_url()
check("sms_status_callback_url: returns non-empty when PUBLIC_BASE_URL set", bool(cb_url))
check("sms_status_callback_url: includes /webhooks/twilio/sms/status",
      "/webhooks/twilio/sms/status" in cb_url)
check("sms_status_callback_url: no double-slash",
      "//webhooks" not in cb_url)

# Simulate unset PUBLIC_BASE_URL.
_orig_base = config.PUBLIC_BASE_URL
config.PUBLIC_BASE_URL = ""
cb_empty = config.sms_status_callback_url()
check("sms_status_callback_url: returns '' when PUBLIC_BASE_URL unset", cb_empty == "")
config.PUBLIC_BASE_URL = _orig_base


# ---- 6. status_callback auto-injected in send_sms ----
# Reload messaging so it picks up the restored PUBLIC_BASE_URL.
messaging.sms_status_callback_url = config.sms_status_callback_url
_sent_data.clear()

biz_approved = {"id": 1, "a2p_status": "approved", "a2p_brand_sid": "BN1",
                "a2p_service_sid": "MG1", "a2p_campaign_sid": "CM1",
                "twilio_number": None}

# Ensure compliance sees the business as approved.
import compliance

_orig_a2p = compliance.a2p_ready
compliance.a2p_ready = lambda b: True

result = messaging.send_sms(biz_approved, "+15551119999", "Test auto-callback",
                            status_callback=None)

check("send_sms returns 'sent' with auto-callback injection",
      result.get("status") == "sent")
check("status_callback auto-injected in Twilio POST data",
      bool(_sent_data) and "StatusCallback" in _sent_data[-1]["data"])
check("auto-injected StatusCallback includes correct path",
      bool(_sent_data) and "/webhooks/twilio/sms/status" in
      (_sent_data[-1]["data"].get("StatusCallback") or ""))

# Explicit status_callback should NOT be overridden.
_sent_data.clear()
result2 = messaging.send_sms(biz_approved, "+15551119999", "Explicit cb",
                             status_callback="https://explicit.example.com/cb")
check("explicit status_callback is preserved (not overridden)",
      bool(_sent_data) and
      _sent_data[-1]["data"].get("StatusCallback") == "https://explicit.example.com/cb")

# When PUBLIC_BASE_URL is empty, no StatusCallback injected.
config.PUBLIC_BASE_URL = ""
messaging.sms_status_callback_url = config.sms_status_callback_url
_sent_data.clear()
result3 = messaging.send_sms(biz_approved, "+15551119999", "No base url", status_callback=None)
check("no StatusCallback injected when PUBLIC_BASE_URL is empty",
      bool(_sent_data) and "StatusCallback" not in _sent_data[-1]["data"])
config.PUBLIC_BASE_URL = _orig_base

# Restore.
compliance.a2p_ready = _orig_a2p
_rq.post = _real_post

# Cleanup.
import os as _os
_os.unlink(_TMP.name)

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
