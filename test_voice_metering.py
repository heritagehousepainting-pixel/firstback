"""Phase 5g Slice 3: voice_calls DB helpers + place_call AMD/StatusCallback.

Run: python test_voice_metering.py

Covers:
  - init_db creates voice_calls table with correct columns
  - insert_voice_call + update_voice_call_outcome round-trip
  - voice_spend_this_month sums within the calendar month, excludes prior months
  - last_voice_call_at returns None when no calls; correct ISO when a call exists
  - place_call data dict contains MachineDetection and AsyncAmd when add_amd=True
  - place_call auto-fills StatusCallback from PUBLIC_BASE_URL when set + add_amd=True
  - place_call stays 'simulated' when not configured

No network. Standalone; exit non-zero on failure.
"""
import os
import sys
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest_vm")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_vm")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+15550001111")
os.environ.setdefault("FIRSTBACK_PUBLIC_URL", "https://test.firstback.io")

import config

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name
config.PUBLIC_BASE_URL = "https://test.firstback.io"

import db
db.DB_PATH = _TMP.name
db.DB_BACKUP_PATH = ""
db.init_db()

import messaging

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


# ============================================================
# 1. voice_calls table columns
# ============================================================
import sqlite3 as _sqlite3
_conn = _sqlite3.connect(_TMP.name)
_cols = {r[1] for r in _conn.execute("PRAGMA table_info(voice_calls)").fetchall()}
_conn.close()

check("voice_calls table exists", bool(_cols))
for _col in ("id", "biz_id", "lead_id", "twilio_sid", "started_at", "ended_at",
             "duration_seconds", "turns", "outcome", "cost_cents", "created_at"):
    check(f"voice_calls has column '{_col}'", _col in _cols)


# ============================================================
# 2. insert_voice_call + update_voice_call_outcome round-trip
# ============================================================
_lead_id = db.create_lead(1, "Voice Test", "+14155550100")

_vc_id = db.insert_voice_call(1, _lead_id, "CA_test_001")
check("insert_voice_call returns an integer id", isinstance(_vc_id, int) and _vc_id > 0)

# Verify the row is there with in_progress outcome
_conn2 = _sqlite3.connect(_TMP.name)
_row = _conn2.execute("SELECT * FROM voice_calls WHERE id=?", (_vc_id,)).fetchone()
_conn2.close()
check("insert_voice_call row has outcome=in_progress",
      _row is not None and _row[8] == "in_progress")  # outcome column index

db.update_voice_call_outcome("CA_test_001", "booked", 90, 75)

_conn3 = _sqlite3.connect(_TMP.name)
_row2 = _conn3.execute("SELECT outcome, duration_seconds, cost_cents FROM voice_calls WHERE id=?",
                       (_vc_id,)).fetchone()
_conn3.close()
check("update_voice_call_outcome sets outcome", _row2 is not None and _row2[0] == "booked")
check("update_voice_call_outcome sets duration_seconds", _row2 is not None and _row2[1] == 90)
check("update_voice_call_outcome sets cost_cents", _row2 is not None and _row2[2] == 75)


# ============================================================
# 3. voice_spend_this_month
# ============================================================
from datetime import datetime, timezone, timedelta

# Current-month call already inserted: CA_test_001 costs 75 cents.
# Insert another current-month call.
_lead2 = db.create_lead(1, "Voice Test 2", "+14155550101")
_vc_id2 = db.insert_voice_call(1, _lead2, "CA_test_002")
db.update_voice_call_outcome("CA_test_002", "booked", 120, 100)

spend = db.voice_spend_this_month(1)
check("voice_spend_this_month sums current-month calls", spend == 175)

# Insert a call with a started_at in the PREVIOUS month and verify it is excluded.
_prev_month_ts = (datetime.now(timezone.utc).replace(day=1) - timedelta(days=1)).replace(
    day=1).isoformat()
_conn4 = _sqlite3.connect(_TMP.name)
_conn4.execute(
    "INSERT INTO voice_calls (biz_id, lead_id, twilio_sid, started_at, outcome, cost_cents, created_at)"
    " VALUES (?,?,?,?,?,?,?)",
    (1, _lead_id, "CA_old_001", _prev_month_ts, "booked", 500,
     datetime.now(timezone.utc).isoformat()))
_conn4.commit()
_conn4.close()

spend2 = db.voice_spend_this_month(1)
check("voice_spend_this_month excludes prior-month calls", spend2 == 175)

# Different biz should be 0
spend_biz2 = db.voice_spend_this_month(999)
check("voice_spend_this_month returns 0 for unknown biz_id", spend_biz2 == 0)


# ============================================================
# 4. last_voice_call_at
# ============================================================
# Fresh lead with no calls -> None
_lead3 = db.create_lead(1, "No Call Lead", "+14155550200")
result_none = db.last_voice_call_at(1, "+14155550200")
check("last_voice_call_at returns None when no calls for caller", result_none is None)

# Insert a call for +14155550100 (lead_id = _lead_id).
# CA_test_001 is already there for that lead.
result_found = db.last_voice_call_at(1, "+14155550100")
check("last_voice_call_at returns a string (ISO) when a call exists",
      isinstance(result_found, str) and len(result_found) > 0)

# Wrong biz -> None
result_wrong_biz = db.last_voice_call_at(999, "+14155550100")
check("last_voice_call_at returns None for wrong biz_id", result_wrong_biz is None)


# ============================================================
# 5. place_call: StatusCallback + AMD when add_amd=True
# ============================================================
import requests as _rq

_captured = []
_real_post = _rq.post


def _spy_post(url, auth=None, data=None, timeout=None, **kw):
    _captured.append(dict(data or {}))

    class _Resp:
        status_code = 201
        def raise_for_status(self): pass
        def json(self): return {"sid": "CA_spy_001"}
    return _Resp()


_rq.post = _spy_post
messaging.TWILIO_ACCOUNT_SID = "ACtest_vm"
messaging.TWILIO_AUTH_TOKEN = "tok_vm"
messaging.TWILIO_FROM_NUMBER = "+15550001111"
messaging.PUBLIC_BASE_URL = "https://test.firstback.io"

# Set a from_number on biz 1 so we don't get 'simulated'
db.set_business_twilio(1, "+15550001111", "PN1", forward_to="+15559990000")
db.set_a2p_status(1, "approved")

_biz = db.get_business(1)
_captured.clear()

result = messaging.place_call(_biz, "+14155550100",
                              "https://voice.test/twiml?biz=1&lead=1",
                              add_amd=True)

check("place_call with add_amd=True returns 'placed'",
      result.get("status") == "placed")
check("place_call data dict contains MachineDetection=Enable",
      _captured and _captured[-1].get("MachineDetection") == "Enable")
check("place_call data dict contains AsyncAmd=true",
      _captured and _captured[-1].get("AsyncAmd") == "true")
check("place_call auto-fills StatusCallback when PUBLIC_BASE_URL set",
      _captured and "/webhooks/twilio/voice/status" in _captured[-1].get("StatusCallback", ""))
check("place_call AsyncAmdStatusCallback matches StatusCallback",
      _captured and _captured[-1].get("AsyncAmdStatusCallback") == _captured[-1].get("StatusCallback"))


# ============================================================
# 6. place_call without add_amd does NOT inject AMD params
# ============================================================
_captured.clear()
messaging.place_call(_biz, "+14155550100",
                     "https://voice.test/twiml/dispatcher/5")

check("place_call without add_amd has no MachineDetection",
      _captured and "MachineDetection" not in _captured[-1])
check("place_call without add_amd has no AsyncAmd",
      _captured and "AsyncAmd" not in _captured[-1])


# ============================================================
# 7. place_call stays 'simulated' when not configured
# ============================================================
_rq.post = _real_post
messaging.TWILIO_ACCOUNT_SID = ""
messaging.TWILIO_AUTH_TOKEN = ""

result_sim = messaging.place_call(_biz, "+14155550100",
                                  "https://voice.test/twiml?biz=1&lead=1",
                                  add_amd=True)
check("place_call stays 'simulated' when unconfigured",
      result_sim.get("status") == "simulated")


# ============================================================
# Report
# ============================================================
print(f"\n==== {_pass} passed, {_fail} failed ====")
sys.exit(1 if _fail else 0)
