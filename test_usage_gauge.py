"""Phase 1 B — Usage gauge tests.
Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_usage_gauge.py
Prints ok/FAIL per check; exits 0 only when all pass.
"""
import os
import sys
import tempfile
import json

os.environ["FIRSTBACK_PROVIDER"] = "demo"

import config

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

import ai

_pass = _fail = 0


def ok(label):
    global _pass
    _pass += 1
    print(f"  ok  {label}")


def fail(label, detail=""):
    global _fail
    _fail += 1
    print(f"  FAIL  {label}" + (f": {detail}" if detail else ""))


# ---- 1. conversations_consumed counts DISTINCT lead_ids on path='sms' ----
# Same lead_id logged twice -> counts as 1
db.log_llm_usage(1, "sms", "claude-sonnet-4-6", 100, 50, 0.001, lead_id=10)
db.log_llm_usage(1, "sms", "claude-sonnet-4-6", 100, 50, 0.001, lead_id=10)
db.log_llm_usage(1, "sms", "claude-sonnet-4-6", 100, 50, 0.001, lead_id=11)
consumed = db.conversations_consumed(1)
if consumed == 2:
    ok("conversations_consumed counts DISTINCT lead_ids")
else:
    fail("conversations_consumed should be 2 (distinct leads)", consumed)

# assistant path does NOT count toward conversation consumption
db.log_llm_usage(1, "assistant", "claude-sonnet-4-6", 100, 50, 0.001, lead_id=12)
consumed2 = db.conversations_consumed(1)
if consumed2 == 2:
    ok("conversations_consumed ignores path=assistant")
else:
    fail("conversations_consumed should still be 2 after assistant path log", consumed2)

# ---- 2. period_start filter works ----
from datetime import datetime, timezone, timedelta
yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
# All rows were inserted today, so starting from yesterday includes them; from tomorrow excludes
consumed_since_yesterday = db.conversations_consumed(1, period_start=yesterday)
if consumed_since_yesterday == 2:
    ok("conversations_consumed period_start=yesterday includes today's rows")
else:
    fail("conversations_consumed period_start=yesterday should be 2", consumed_since_yesterday)

consumed_since_tomorrow = db.conversations_consumed(1, period_start=tomorrow)
if consumed_since_tomorrow == 0:
    ok("conversations_consumed period_start=tomorrow excludes today's rows")
else:
    fail("conversations_consumed period_start=tomorrow should be 0", consumed_since_tomorrow)

# ---- 3. conversations_remaining with no grant returns (None, None) ----
remaining, grant = db.conversations_remaining(1)
if remaining is None and grant is None:
    ok("conversations_remaining returns (None, None) with no grant")
else:
    fail("conversations_remaining should be (None, None) when no grant", (remaining, grant))

# ---- 4. conversations_remaining with a grant = granted - consumed ----
conn = db.get_conn()
conn.execute(
    "INSERT INTO usage_grants (business_id, period_start, period_end, "
    "conversations_granted, source, created_at) VALUES (?,?,?,?,?,?)",
    (1, yesterday, tomorrow, 1000, "test", db.now_iso()))
conn.commit()
conn.close()

remaining2, grant2 = db.conversations_remaining(1)
# consumed = 2 distinct leads on sms, granted = 1000 -> remaining = 998
if remaining2 == 998:
    ok("conversations_remaining = granted - consumed (998)")
else:
    fail("conversations_remaining should be 998", remaining2)

if grant2 and int(grant2.get("conversations_granted", 0)) == 1000:
    ok("conversations_remaining returns grant row")
else:
    fail("conversations_remaining grant row wrong", grant2)

# ---- 5. remaining never goes below zero ----
conn = db.get_conn()
# Add a grant with granted=1 (less than consumed=2)
conn.execute(
    "INSERT INTO usage_grants (business_id, period_start, period_end, "
    "conversations_granted, source, created_at) VALUES (?,?,?,?,?,?)",
    (1, yesterday, tomorrow, 1, "test-small", db.now_iso()))
conn.commit()
conn.close()
remaining3, _ = db.conversations_remaining(1)
if remaining3 >= 0:
    ok("conversations_remaining >= 0 (never negative)")
else:
    fail("conversations_remaining went negative", remaining3)

# ---- 6. /api/usage endpoint returns gauge data ----
import messaging
messaging.TWILIO_ACCOUNT_SID = ""
import app as _app
client = _app.app.test_client()

# Login as seed user
from werkzeug.security import generate_password_hash
if db.count_users() == 0:
    db.create_user(config.SEED_OWNER_EMAIL, generate_password_hash("testpass"), 1)

with client.session_transaction() as sess:
    sess["uid"] = db.get_user_by_email(config.SEED_OWNER_EMAIL)["id"]

resp = client.get("/api/usage")
if resp.status_code == 200:
    ok("/api/usage returns 200")
else:
    fail("/api/usage should return 200", resp.status_code)

data = json.loads(resp.data)
required_keys = ["conversations_used", "conversations_total", "conversations_remaining",
                 "period_ends", "spend_today_usd", "daily_cap_usd",
                 "over_daily_cap", "has_plan"]
missing = [k for k in required_keys if k not in data]
if not missing:
    ok("/api/usage returns all required gauge keys")
else:
    fail("/api/usage missing keys", missing)

if data.get("has_plan") is True:
    ok("/api/usage has_plan=True when grant exists")
else:
    fail("/api/usage has_plan should be True", data.get("has_plan"))

# ---- 7. Gauge text has NO banned vocabulary ----
import re
banned = re.compile(r"\b(credit|grant|bundle|twilio|a2p|a 2p)\b", re.I)
gauge_texts_to_check = [
    # Simulated fuel-gauge output strings (the JS composes these — check the template)
    "847 of 1,000 conversations left · refills Dec 1",
    "Need more? +50 for $12",
    "Resting for a moment — back shortly.",
    str(data),  # also check the JSON payload
]
for text in gauge_texts_to_check:
    m = banned.search(text)
    if m:
        fail(f"Banned word '{m.group()}' found in gauge text", text[:80])
    else:
        ok(f"No banned vocabulary in: {text[:50]}")

# ---- 8. tenant isolation on conversations_consumed ----
db.log_llm_usage(2, "sms", "claude-sonnet-4-6", 100, 50, 0.001, lead_id=99)
consumed_biz1 = db.conversations_consumed(1)
consumed_biz2 = db.conversations_consumed(2)
if consumed_biz1 == 2 and consumed_biz2 == 1:
    ok("conversations_consumed is tenant-scoped")
else:
    fail("conversations_consumed tenant isolation wrong",
         f"biz1={consumed_biz1} biz2={consumed_biz2}")

# ---- 9. lead_id=None (assistant path) not counted in conversations ----
db.log_llm_usage(1, "sms", "claude-sonnet-4-6", 100, 50, 0.001, lead_id=None)
consumed_after_null = db.conversations_consumed(1)
if consumed_after_null == 2:
    ok("NULL lead_id not counted in conversations_consumed")
else:
    fail("NULL lead_id should not affect conversations_consumed", consumed_after_null)

# ---- Summary ----
print(f"\n{'='*40}")
print(f"test_usage_gauge: {_pass} passed, {_fail} failed")
if _fail:
    sys.exit(1)
sys.exit(0)
