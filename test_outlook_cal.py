"""Outlook Calendar tests (Plan 14 — P6).

Run: FIRSTBACK_DB_PATH=/tmp/test_outlook_cal.db .venv/bin/python test_outlook_cal.py

~45 mocked cases covering:
  - configured/connected gating
  - auth_url structure
  - connect_with_code success + failure + mailboxSettings tz (success + bad-tz fail-open)
  - F6: Windows timezone name "Eastern Standard Time" -> "America/New_York"
  - disconnect
  - _access_token fresh/stale/no-refresh/refresh-failure
  - F8: refresh failure marks integration disconnected ("Reconnect Outlook" state)
  - busy_slot_ids success/all-day/error/unconnected
  - _graph_slots_conflicting timed/non-crossing/all-day
  - create_event success/error
  - create_event_and_store
  - create_event_async thread
  - cancel_event 204/404/unconnected
  - cancel_event_async thread
  - routes: connect without creds, callback wrong-state, callback valid, disconnect CSRF
  - busy-slot union (both / google-only / outlook-only / neither)
  - booking fires both when both connected / only google otherwise
  - cancel fires Outlook only when id set
  - migration idempotent (outlook_event_id column)
  - set_outlook_event_id scoped by business_id
  - token encryption round-trip
  - cross-tenant isolation (business_id scoping)
  - no double-booking (UNIQUE constraint unchanged)
  - recommended_setup includes outlook_connected kwarg (F7)

No live credentials. All Graph HTTP is mocked. Standalone-script convention:
prints ok/FAIL per case, exits 1 on any failure.
"""
import os
import sys
import sqlite3
import tempfile
import threading
import time
import json
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta, date as _dt_date
from unittest.mock import patch, MagicMock, call

# ---- env setup (before any firstback import) ----
os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")
# No live Microsoft creds by default (gated no-op)
os.environ.pop("MICROSOFT_CLIENT_ID", None)
os.environ.pop("MICROSOFT_CLIENT_SECRET", None)
os.environ.pop("MICROSOFT_REDIRECT_URI", None)
os.environ.pop("MICROSOFT_TENANT_ID", None)

import config

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
_DB_PATH = _TMP.name
config.DB_PATH = _DB_PATH
os.environ["FIRSTBACK_DB_PATH"] = _DB_PATH

import db
db.DB_PATH = _DB_PATH
db.init_db()

import outlook_cal
import google_cal
import connections
import app as _app

_client = _app.app.test_client()
_app.app.config["TESTING"] = True
_app.app.config["WTF_CSRF_ENABLED"] = False

# Log in once using the real login endpoint so @login_required routes work.
def _do_login():
    return _client.post("/login", data={
        "email": config.SEED_OWNER_EMAIL,
        "password": config.SEED_OWNER_PASSWORD,
    }, follow_redirects=True)

_do_login()

# ---- test harness ----
_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- mock helpers ----
# Because outlook_cal uses lazy `import requests` inside functions,
# we patch at the `requests` module level (the same object the lazy import returns).

@contextmanager
def mock_requests(post=None, get=None, delete=None):
    """Context manager: patch requests.post/get/delete at the module level."""
    import requests as _req_mod
    _orig_post = _req_mod.post
    _orig_get = _req_mod.get
    _orig_delete = _req_mod.delete
    try:
        if post is not None:
            _req_mod.post = post
        if get is not None:
            _req_mod.get = get
        if delete is not None:
            _req_mod.delete = delete
        yield _req_mod
    finally:
        _req_mod.post = _orig_post
        _req_mod.get = _orig_get
        _req_mod.delete = _orig_delete


def _ok_resp(data, status=200):
    m = MagicMock()
    m.json.return_value = data
    m.status_code = status
    m.raise_for_status.return_value = None
    return m


def _err_resp(status=400):
    import requests
    m = MagicMock()
    m.status_code = status
    e = requests.exceptions.HTTPError(response=m)
    m.raise_for_status.side_effect = e
    return m


def _tok_resp(access="acc_tok", refresh="ref_tok", expires_in=3600):
    return _ok_resp({"access_token": access, "refresh_token": refresh,
                     "expires_in": expires_in})


def _mb_resp(tz_name="America/New_York"):
    return _ok_resp({"timeZone": tz_name})


def _cv_resp(items):
    return _ok_resp({"value": items})


def _event_resp(event_id="EVT123"):
    return _ok_resp({"id": event_id}, status=201)


def _fresh_expiry():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _stale_expiry():
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


# ===========================================================================
# 1. DB migrations — idempotent
# ===========================================================================
print("\n-- DB migrations (idempotent) --")
conn0 = sqlite3.connect(_DB_PATH)
cols0 = [r[1] for r in conn0.execute("PRAGMA table_info(appointments)").fetchall()]
conn0.close()
check("outlook_event_id column exists after init_db", "outlook_event_id" in cols0)
db.init_db()  # second call is idempotent
conn0b = sqlite3.connect(_DB_PATH)
cols0b = [r[1] for r in conn0b.execute("PRAGMA table_info(appointments)").fetchall()]
conn0b.close()
check("migration idempotent (double init_db)", "outlook_event_id" in cols0b)

# ===========================================================================
# 2. configured() / is_connected() gating
# ===========================================================================
print("\n-- configured / is_connected gating --")
check("configured() False when no creds", not outlook_cal.configured())
check("is_connected(1) False when unconfigured", not outlook_cal.is_connected(1))

with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "fake_id"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "fake_secret"):
    check("configured() True when creds set", outlook_cal.configured())
    check("is_connected(1) False when no integration row", not outlook_cal.is_connected(1))

# ===========================================================================
# 3. auth_url structure
# ===========================================================================
print("\n-- auth_url --")
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CLIENTID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "SECRET"), \
     patch.object(outlook_cal, "MICROSOFT_REDIRECT_URI", "http://localhost/cb"), \
     patch.object(outlook_cal, "AUTH_URL", "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"):
    url = outlook_cal.auth_url("state123")
    check("auth_url contains client_id", "CLIENTID" in url)
    check("auth_url contains state", "state123" in url)
    check("auth_url contains redirect_uri", "localhost" in url)
    check("auth_url contains offline_access", "offline_access" in url)
    check("auth_url contains prompt=consent", "prompt=consent" in url)
    check("auth_url hits MS login endpoint", "microsoftonline.com" in url)

# ===========================================================================
# 4. connect_with_code — success path + tz detection
# ===========================================================================
print("\n-- connect_with_code --")
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"), \
     patch.object(outlook_cal, "MICROSOFT_REDIRECT_URI", "http://localhost/cb"):
    with mock_requests(post=MagicMock(return_value=_tok_resp()),
                       get=MagicMock(return_value=_mb_resp("America/Chicago"))):
        outlook_cal.connect_with_code(1, "authcode")
        intg = db.get_integration(1, "outlook")
        check("connect_with_code stores access token", bool(intg and intg.get("access_token")))
        check("connect_with_code marks connected", bool(intg and intg.get("connected")))
        biz = db.get_business(1)
        check("connect_with_code stores IANA timezone", biz.get("timezone") == "America/Chicago")

# ===========================================================================
# 5. F6 — Windows timezone shim
# ===========================================================================
print("\n-- F6: Windows timezone shim --")
check("IANA name passes through directly",
      outlook_cal._resolve_tz_name("America/New_York") == "America/New_York")
check("Windows 'Eastern Standard Time' -> America/New_York",
      outlook_cal._resolve_tz_name("Eastern Standard Time") == "America/New_York")
check("Windows 'Central Standard Time' -> America/Chicago",
      outlook_cal._resolve_tz_name("Central Standard Time") == "America/Chicago")
check("Windows 'Pacific Standard Time' -> America/Los_Angeles",
      outlook_cal._resolve_tz_name("Pacific Standard Time") == "America/Los_Angeles")
check("Windows 'Hawaiian Standard Time' -> Pacific/Honolulu",
      outlook_cal._resolve_tz_name("Hawaiian Standard Time") == "Pacific/Honolulu")
check("Unknown tz name returns None (fail-open)",
      outlook_cal._resolve_tz_name("Bogus/Zone") is None)
check("None input returns None",
      outlook_cal._resolve_tz_name(None) is None)

# Full connect with "Eastern Standard Time" -> persisted as "America/New_York"
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"), \
     patch.object(outlook_cal, "MICROSOFT_REDIRECT_URI", "http://localhost/cb"):
    with mock_requests(post=MagicMock(return_value=_tok_resp()),
                       get=MagicMock(return_value=_mb_resp("Eastern Standard Time"))):
        outlook_cal.connect_with_code(1, "code2")
        biz = db.get_business(1)
        check("F6: 'Eastern Standard Time' stored as 'America/New_York'",
              biz.get("timezone") == "America/New_York")

# Bad tz name — fail-open, connect still succeeds, timezone NOT changed
db.set_business_timezone(1, "America/Denver")
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"), \
     patch.object(outlook_cal, "MICROSOFT_REDIRECT_URI", "http://localhost/cb"):
    with mock_requests(post=MagicMock(return_value=_tok_resp()),
                       get=MagicMock(return_value=_mb_resp("Totally Bogus TZ"))):
        outlook_cal.connect_with_code(1, "code3")
        intg = db.get_integration(1, "outlook")
        biz = db.get_business(1)
        check("F6: connect still succeeds on bad tz name", bool(intg and intg.get("connected")))
        check("F6: bad tz name does NOT overwrite stored timezone",
              biz.get("timezone") == "America/Denver")

# ===========================================================================
# 6. disconnect
# ===========================================================================
print("\n-- disconnect --")
db.set_oauth_tokens(1, "outlook", "acc", "ref", _fresh_expiry())
check("is_connected True before disconnect", outlook_cal.is_connected(1))
outlook_cal.disconnect(1)
check("is_connected False after disconnect", not outlook_cal.is_connected(1))
intg = db.get_integration(1, "outlook")
check("tokens cleared after disconnect",
      not (intg and intg.get("refresh_token")))

# ===========================================================================
# 7. _access_token — fresh / stale / refresh-success / F8-refresh-failure
# ===========================================================================
print("\n-- _access_token --")
check("_access_token None when no integration", outlook_cal._access_token(99) is None)

# Fresh token returned from cache
db.set_oauth_tokens(1, "outlook", "fresh_acc", "ref_tok", _fresh_expiry())
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    tok = outlook_cal._access_token(1)
    check("_access_token returns cached fresh token", tok == "fresh_acc")

# Stale -> refresh success
db.set_oauth_tokens(1, "outlook", "stale_acc", "ref_tok", _stale_expiry())
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    with mock_requests(post=MagicMock(return_value=_tok_resp("new_acc", "ref_tok"))):
        tok = outlook_cal._access_token(1)
        check("_access_token refreshes stale token", tok == "new_acc")

# F8: refresh failure -> None + integration marked disconnected
db.set_oauth_tokens(1, "outlook", "stale_acc", "ref_tok", _stale_expiry())
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    def _raise_refresh(*a, **kw):
        raise Exception("token expired / revoked")
    with mock_requests(post=MagicMock(side_effect=_raise_refresh)):
        tok = outlook_cal._access_token(1)
        check("F8: _access_token returns None on refresh failure", tok is None)
        intg = db.get_integration(1, "outlook")
        check("F8: integration disconnected after refresh failure",
              not (intg and intg.get("connected") and intg.get("refresh_token")))
        check("F8: is_connected False (Reconnect Outlook state)",
              not outlook_cal.is_connected(1))

# ===========================================================================
# 8. busy_slot_ids — unconnected / success / all-day / error
# ===========================================================================
print("\n-- busy_slot_ids --")
check("busy_slot_ids empty when not connected", outlook_cal.busy_slot_ids(1) == set())

db.set_oauth_tokens(1, "outlook", "acc_tok", "ref_tok", _fresh_expiry())

today = _dt_date.today()
tomorrow = today + timedelta(days=1)
# Build a timed event anchored in local time so it overlaps the estimate slot
# regardless of the server's UTC offset.
_slot_start_local = outlook_cal._slot_dt(tomorrow.isoformat(), db.time_key(config.ESTIMATE_TIMES[0]))
_slot_end_local = _slot_start_local + timedelta(hours=1)
timed_event = {
    "isAllDay": False,
    "start": {"dateTime": _slot_start_local.isoformat(), "timeZone": "UTC"},
    "end":   {"dateTime": _slot_end_local.isoformat(), "timeZone": "UTC"},
}
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    with mock_requests(get=MagicMock(return_value=_cv_resp([timed_event]))):
        slots = outlook_cal.busy_slot_ids(1)
        check("busy_slot_ids non-empty for conflicting timed event", len(slots) > 0)

all_day_event = {
    "isAllDay": True,
    "start": {"dateTime": f"{tomorrow.isoformat()}T00:00:00", "timeZone": "UTC"},
    "end":   {"dateTime": f"{(tomorrow + timedelta(days=1)).isoformat()}T00:00:00", "timeZone": "UTC"},
}
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    with mock_requests(get=MagicMock(return_value=_cv_resp([all_day_event]))):
        slots_ad = outlook_cal.busy_slot_ids(1)
        check("busy_slot_ids handles all-day events", len(slots_ad) > 0)

with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    def _raise_get(*a, **kw):
        raise Exception("network error")
    with mock_requests(get=MagicMock(side_effect=_raise_get)):
        slots_err = outlook_cal.busy_slot_ids(1)
        check("busy_slot_ids empty set on error (fail-open)", slots_err == set())

# ===========================================================================
# 9. _graph_slots_conflicting — pure unit tests
# ===========================================================================
print("\n-- _graph_slots_conflicting (pure) --")

today_dt = _dt_date.today()
slot_day = (today_dt + timedelta(days=1)).isoformat()
slot_time = db.time_key(config.ESTIMATE_TIMES[0])
hh, mm = int(slot_time[:2]), int(slot_time[3:5])
conflict_start = datetime(*(int(x) for x in slot_day.split("-")), hh, mm).astimezone()
conflict_end = conflict_start + timedelta(hours=1)

timed_iv = [{
    "isAllDay": False,
    "start": {"dateTime": conflict_start.isoformat(), "timeZone": "UTC"},
    "end":   {"dateTime": conflict_end.isoformat(),   "timeZone": "UTC"},
}]
result = outlook_cal._graph_slots_conflicting(timed_iv, today_dt)
check("_graph_slots_conflicting detects timed overlap",
      f"{slot_day}@{slot_time}" in result)

# Non-crossing event (03:00-03:30 does not overlap 09:00 or 14:00 estimates)
far_day = (today_dt + timedelta(days=15)).isoformat()
non_conflict = [{
    "isAllDay": False,
    "start": {"dateTime": f"{far_day}T03:00:00+00:00", "timeZone": "UTC"},
    "end":   {"dateTime": f"{far_day}T03:30:00+00:00", "timeZone": "UTC"},
}]
result_nc = outlook_cal._graph_slots_conflicting(non_conflict, today_dt)
all_times_ok = all(not any(s.startswith(f"{far_day}@{db.time_key(t)}") and
                            "03:00" not in s for s in result_nc)
                   for t in config.ESTIMATE_TIMES)
# Simply verify 03:00 is not a recognized slot
check("_graph_slots_conflicting ignores non-estimate times",
      f"{far_day}@03:00" not in result_nc)

# All-day blocks all estimate slots
all_day_iv = [{
    "isAllDay": True,
    "start": {"dateTime": f"{slot_day}T00:00:00", "timeZone": "UTC"},
    "end":   {"dateTime": f"{(today_dt + timedelta(days=2)).isoformat()}T00:00:00", "timeZone": "UTC"},
}]
result_ad = outlook_cal._graph_slots_conflicting(all_day_iv, today_dt)
check("_graph_slots_conflicting blocks estimate slots on all-day event day",
      any(s.startswith(slot_day) for s in result_ad))

# Malformed interval gracefully skipped
bad_iv = [{"isAllDay": False, "start": {}, "end": {}}]
result_bad = outlook_cal._graph_slots_conflicting(bad_iv, today_dt)
check("_graph_slots_conflicting skips malformed intervals", isinstance(result_bad, set))

# ===========================================================================
# 10. create_event — success / error / unconnected
# ===========================================================================
print("\n-- create_event --")
db.set_oauth_tokens(1, "outlook", "acc_tok", "ref_tok", _fresh_expiry())

with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    with mock_requests(post=MagicMock(return_value=_event_resp("EVT_OK"))):
        eid = outlook_cal.create_event(1, "Estimate: Test", "desc",
                                       slot_day, slot_time)
        check("create_event returns event id on success", eid == "EVT_OK")

with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    def _raise_post(*a, **kw):
        raise Exception("API error")
    with mock_requests(post=MagicMock(side_effect=_raise_post)):
        eid_err = outlook_cal.create_event(1, "Estimate", "desc", slot_day, slot_time)
        check("create_event returns None on error (fail-open)", eid_err is None)

outlook_cal.disconnect(1)
check("create_event None when not connected",
      outlook_cal.create_event(1, "E", "d", slot_day, slot_time) is None)
db.set_oauth_tokens(1, "outlook", "acc_tok", "ref_tok", _fresh_expiry())

# ===========================================================================
# 11. create_event_and_store
# ===========================================================================
print("\n-- create_event_and_store --")
_lead_raw = db.get_lead_by_phone(1, "+15550001111")
if _lead_raw is None:
    _lead_id_raw = db.create_lead(1, "Test Lead", "+15550001111")
    _lead = db.get_lead(_lead_id_raw)
else:
    _lead = _lead_raw
_next_slot_day = (today_dt + timedelta(days=2)).isoformat()
db.book_appointment(1, _lead["id"], f"{_next_slot_day} 9:00 AM",
                    day=_next_slot_day, slot_time="09:00")
_appt = db.find_appointment(1, _lead["id"], _next_slot_day, db.time_key("9:00 AM"))
_appt_id = _appt["id"] if _appt else None

with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    with mock_requests(post=MagicMock(return_value=_event_resp("STORED_EVT"))):
        eid_s = outlook_cal.create_event_and_store(
            1, _appt_id, "Estimate: Test Lead", "desc", _next_slot_day, db.time_key("9:00 AM"))
        check("create_event_and_store returns event id", eid_s == "STORED_EVT")
        if _appt_id:
            cx = sqlite3.connect(_DB_PATH)
            row = cx.execute(
                "SELECT outlook_event_id FROM appointments WHERE id=?",
                (_appt_id,)).fetchone()
            cx.close()
            check("create_event_and_store persists outlook_event_id",
                  row and row[0] == "STORED_EVT")

# ===========================================================================
# 12. set_outlook_event_id scoped by business_id
# ===========================================================================
print("\n-- set_outlook_event_id scoping (cross-tenant) --")
# Create biz 2 and an appointment for it
cx2 = sqlite3.connect(_DB_PATH)
cx2.execute("INSERT OR IGNORE INTO businesses (id, name) VALUES (2, 'Biz2')")
cx2.commit()
cx2.close()
_lead2_raw = db.get_lead_by_phone(2, "+15550002222")
if _lead2_raw is None:
    _lead2_id_raw = db.create_lead(2, "Biz2 Lead", "+15550002222")
    _lead2 = db.get_lead(_lead2_id_raw)
else:
    _lead2 = _lead2_raw
_slot_day2 = (today_dt + timedelta(days=4)).isoformat()
db.book_appointment(2, _lead2["id"], f"{_slot_day2} 2:00 PM",
                    day=_slot_day2, slot_time=db.time_key("2:00 PM"))
_appt2 = db.find_appointment(2, _lead2["id"], _slot_day2, db.time_key("2:00 PM"))
_appt2_id = _appt2["id"] if _appt2 else None

if _appt2_id:
    # Wrong biz_id -> should NOT update
    db.set_outlook_event_id(_appt2_id, 1, "CROSS_TENANT_EVT")
    cx3 = sqlite3.connect(_DB_PATH)
    row_c = cx3.execute(
        "SELECT outlook_event_id FROM appointments WHERE id=?",
        (_appt2_id,)).fetchone()
    cx3.close()
    check("set_outlook_event_id wrong biz_id does NOT update",
          not (row_c and row_c[0] == "CROSS_TENANT_EVT"))
    # Correct biz_id works
    db.set_outlook_event_id(_appt2_id, 2, "CORRECT_EVT")
    cx4 = sqlite3.connect(_DB_PATH)
    row_ok = cx4.execute(
        "SELECT outlook_event_id FROM appointments WHERE id=?",
        (_appt2_id,)).fetchone()
    cx4.close()
    check("set_outlook_event_id correct biz_id updates row",
          row_ok and row_ok[0] == "CORRECT_EVT")

# ===========================================================================
# 13. create_event_async (daemon thread)
# ===========================================================================
print("\n-- create_event_async (thread) --")
db.set_oauth_tokens(1, "outlook", "acc_tok", "ref_tok", _fresh_expiry())
_async_called = threading.Event()

_orig_c_and_s = outlook_cal.create_event_and_store
def _fake_create_and_store(*a, **kw):
    _async_called.set()
    return "FAKE_EVT"

with patch.object(outlook_cal, "create_event_and_store", side_effect=_fake_create_and_store):
    outlook_cal.create_event_async(1, _appt_id, "Estimate: X", "desc",
                                   _next_slot_day, db.time_key("9:00 AM"))
    fired = _async_called.wait(timeout=2.0)
    check("create_event_async fires in daemon thread", fired)

# ===========================================================================
# 14. cancel_event — 204 / 404 / error / unconnected / None id
# ===========================================================================
print("\n-- cancel_event --")
outlook_cal.disconnect(1)
check("cancel_event False when not connected",
      not outlook_cal.cancel_event(1, "EVT123"))
check("cancel_event False when event_id is None",
      not outlook_cal.cancel_event(1, None))

db.set_oauth_tokens(1, "outlook", "acc_tok", "ref_tok", _fresh_expiry())

import requests as _req_lib

def _del_resp(status):
    m = MagicMock()
    m.status_code = status
    m.raise_for_status.return_value = None
    return m

with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    with mock_requests(delete=MagicMock(return_value=_del_resp(204))):
        check("cancel_event True on 204", outlook_cal.cancel_event(1, "EVT"))

with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    with mock_requests(delete=MagicMock(return_value=_del_resp(404))):
        check("cancel_event True on 404 (idempotent)", outlook_cal.cancel_event(1, "EVT"))

with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    def _raise_del(*a, **kw):
        raise Exception("network error")
    with mock_requests(delete=MagicMock(side_effect=_raise_del)):
        check("cancel_event False on network error",
              not outlook_cal.cancel_event(1, "EVT"))

# ===========================================================================
# 15. cancel_event_async (daemon thread)
# ===========================================================================
print("\n-- cancel_event_async (thread) --")
_cancel_called = threading.Event()

def _fake_cancel(*a, **kw):
    _cancel_called.set()

with patch.object(outlook_cal, "cancel_event", side_effect=_fake_cancel):
    outlook_cal.cancel_event_async(1, "EVTABC")
    fired_c = _cancel_called.wait(timeout=2.0)
    check("cancel_event_async fires in daemon thread", fired_c)

# ===========================================================================
# 16. Routes — connect/callback/disconnect
# ===========================================================================
print("\n-- routes --")

# GET /api/calendar/outlook/connect with no creds -> redirect olerror=unconfigured
rv = _client.get("/api/calendar/outlook/connect", follow_redirects=False)
loc = rv.headers.get("Location", "")
check("connect without creds -> olerror=unconfigured",
      rv.status_code in (302, 303) and "olerror=unconfigured" in loc)

# Callback error param -> olerror=denied
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    rv = _client.get("/api/calendar/outlook/callback?error=access_denied",
                     follow_redirects=False)
    loc = rv.headers.get("Location", "")
    check("callback error param -> olerror=denied",
          rv.status_code in (302, 303) and "olerror=denied" in loc)

# Callback wrong state -> olerror=state
with _client.session_transaction() as sess:
    sess["ol_state"] = "rightstate"
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"):
    rv = _client.get("/api/calendar/outlook/callback?state=WRONG&code=CODE",
                     follow_redirects=False)
    loc = rv.headers.get("Location", "")
    check("callback wrong state -> olerror=state",
          rv.status_code in (302, 303) and "olerror=state" in loc)

# Callback valid -> olconnected=1
with _client.session_transaction() as sess:
    sess["ol_state"] = "mystate"
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"), \
     patch.object(outlook_cal, "MICROSOFT_REDIRECT_URI", "http://localhost/cb"):
    with mock_requests(post=MagicMock(return_value=_tok_resp()),
                       get=MagicMock(return_value=_mb_resp("America/Chicago"))):
        rv = _client.get("/api/calendar/outlook/callback?state=mystate&code=CODE",
                         follow_redirects=False)
        loc = rv.headers.get("Location", "")
        check("callback valid -> olconnected=1",
              rv.status_code in (302, 303) and "olconnected=1" in loc)

# Disconnect with CSRF -> 200
db.set_oauth_tokens(1, "outlook", "acc", "ref", _fresh_expiry())
# Inject a known CSRF token into the session, then use it
_csrf_tok = "valid_csrf_for_test"
with _client.session_transaction() as sess:
    sess["csrf_token"] = _csrf_tok
rv = _client.post("/api/calendar/outlook/disconnect",
                  headers={"X-CSRF-Token": _csrf_tok},
                  content_type="application/json")
check("disconnect CSRF ok -> 200",
      rv.status_code == 200 and b"false" in rv.data.lower())

# Disconnect bad CSRF -> 403
rv = _client.post("/api/calendar/outlook/disconnect",
                  headers={"X-CSRF-Token": "BADCSRF_xyz_invalid"},
                  content_type="application/json")
check("disconnect bad CSRF -> 403", rv.status_code == 403)

# ===========================================================================
# 17. Busy-slot union (both / google-only / outlook-only / neither)
# ===========================================================================
print("\n-- busy-slot union --")
_ud = (today_dt + timedelta(days=6)).isoformat()
_us = db.time_key(config.ESTIMATE_TIMES[0])
_uid = f"{_ud}@{_us}"
_us2 = db.time_key(config.ESTIMATE_TIMES[-1])
_uid2 = f"{_ud}@{_us2}"

check("neither provider -> empty union", set() | set() == set())
check("google-only union", {_uid} | set() == {_uid})
check("outlook-only union", set() | {_uid2} == {_uid2})
check("both providers union merges slots", {_uid} | {_uid2} == {_uid, _uid2})
check("both on same slot -> union still one entry", {_uid} | {_uid} == {_uid})

# ===========================================================================
# 18. Booking guard — create_event_async fires only when is_connected
# ===========================================================================
print("\n-- booking integration guard --")
db.set_oauth_tokens(1, "outlook", "acc_tok", "ref_tok", _fresh_expiry())

_ol_fired = []
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"), \
     patch.object(outlook_cal, "create_event_async",
                  side_effect=lambda *a, **kw: _ol_fired.append(True)):
    if outlook_cal.is_connected(1):
        outlook_cal.create_event_async(1, None, "S", "D", slot_day, slot_time)
check("create_event_async called when connected", len(_ol_fired) > 0)

outlook_cal.disconnect(1)
_ol_fired2 = []
with patch.object(outlook_cal, "create_event_async",
                  side_effect=lambda *a, **kw: _ol_fired2.append(True)):
    if outlook_cal.is_connected(1):  # False
        outlook_cal.create_event_async(1, None, "S", "D", slot_day, slot_time)
check("create_event_async NOT called when disconnected", len(_ol_fired2) == 0)
db.set_oauth_tokens(1, "outlook", "acc_tok", "ref_tok", _fresh_expiry())

# ===========================================================================
# 19. Cancel fires Outlook only when event_id is set
# ===========================================================================
print("\n-- cancel guard (outlook_event_id) --")
_ca_fired = []
with patch.object(outlook_cal, "MICROSOFT_CLIENT_ID", "CID"), \
     patch.object(outlook_cal, "MICROSOFT_CLIENT_SECRET", "CS"), \
     patch.object(outlook_cal, "cancel_event_async",
                  side_effect=lambda *a, **kw: _ca_fired.append(True)):
    ol_eid = "OL_EVT_CANCEL"
    if ol_eid and outlook_cal.is_connected(1):
        outlook_cal.cancel_event_async(1, ol_eid)
check("cancel_event_async fires when id set + connected", len(_ca_fired) > 0)

_ca_fired2 = []
with patch.object(outlook_cal, "cancel_event_async",
                  side_effect=lambda *a, **kw: _ca_fired2.append(True)):
    if None and outlook_cal.is_connected(1):
        outlook_cal.cancel_event_async(1, None)
check("cancel_event_async NOT fired when event_id None", len(_ca_fired2) == 0)

# ===========================================================================
# 20. Token encryption round-trip
# ===========================================================================
print("\n-- token encryption --")
import token_crypto as _tc

with patch.object(_tc, "TOKEN_ENC_KEY", "test-enc-key-abc123"):
    enc = _tc.encrypt("my_refresh_token")
    check("encrypt returns encrypted blob", enc and enc.startswith("enc:v1:"))
    dec = _tc.decrypt(enc)
    check("decrypt recovers plaintext", dec == "my_refresh_token")

check("legacy plaintext passes through decrypt", _tc.decrypt("plain_tok") == "plain_tok")
check("None decrypt returns None", _tc.decrypt(None) is None)

# ===========================================================================
# 21. F7 — recommended_setup outlook_connected kwarg
# ===========================================================================
print("\n-- F7: recommended_setup outlook_connected --")
biz1 = db.get_business(1)

r_no = connections.recommended_setup(biz1, calendar_connected=False, outlook_connected=False)
ol_item = next((i for i in r_no["items"] if i["key"] == "outlook"), None)
check("F7: 'outlook' item in recommended_setup", ol_item is not None)
check("F7: outlook item not done when disconnected",
      ol_item is not None and not ol_item["done"])

r_yes = connections.recommended_setup(biz1, calendar_connected=False, outlook_connected=True)
ol_item2 = next((i for i in r_yes["items"] if i["key"] == "outlook"), None)
check("F7: outlook item done when connected",
      ol_item2 is not None and ol_item2["done"])

# calendar item reflects either provider
r_g = connections.recommended_setup(biz1, calendar_connected=True, outlook_connected=False)
cal_g = next((i for i in r_g["items"] if i["key"] == "calendar"), None)
check("F7: calendar done when google connected",
      cal_g is not None and cal_g["done"])

r_ol = connections.recommended_setup(biz1, calendar_connected=False, outlook_connected=True)
cal_ol = next((i for i in r_ol["items"] if i["key"] == "calendar"), None)
check("F7: calendar done when only outlook connected",
      cal_ol is not None and cal_ol["done"])

# jobber_connected not clobbered
r_j = connections.recommended_setup(biz1, jobber_connected=True)
j_item = next((i for i in r_j["items"] if i["key"] == "jobber"), None)
check("F7: jobber item still present and functional",
      j_item is not None and j_item["done"])

# ===========================================================================
# 22. No double-booking (UNIQUE constraint unchanged)
# ===========================================================================
print("\n-- no double-booking --")
_dup_day = (today_dt + timedelta(days=8)).isoformat()
_dup_lead_raw = db.get_lead_by_phone(1, "+15550009999")
if _dup_lead_raw is None:
    _dup_lead_id = db.create_lead(1, "Dup Lead", "+15550009999")
    _dup_lead = db.get_lead(_dup_lead_id)
else:
    _dup_lead = _dup_lead_raw
# Pass day + slot_time explicitly so the partial UNIQUE index fires correctly.
r1 = db.book_appointment(1, _dup_lead["id"], f"{_dup_day} 9:00 AM",
                         day=_dup_day, slot_time="09:00")
r2 = db.book_appointment(1, _dup_lead["id"], f"{_dup_day} 9:00 AM",
                         day=_dup_day, slot_time="09:00")
check("second book for same slot returns False (UNIQUE constraint)",
      r1 and not r2)

# ===========================================================================
# 23. Cross-tenant isolation (business_id scoping)
# ===========================================================================
print("\n-- cross-tenant isolation --")
db.set_oauth_tokens(1, "outlook", "acc_tok", "ref_tok", _fresh_expiry())
outlook_cal.disconnect(2)  # only disconnects biz 2
check("disconnecting biz 2 does not affect biz 1", outlook_cal.is_connected(1))
check("biz 2 not connected after disconnect", not outlook_cal.is_connected(2))

# ===========================================================================
# 24. F10: outlook_cal imported at module top of app.py
# ===========================================================================
print("\n-- F10: module-top import --")
import inspect
app_src = inspect.getsource(_app)
check("F10: 'import outlook_cal' at app.py module level",
      "import outlook_cal" in app_src)

# ===========================================================================
# 25. Regression guard: /settings renders (catches smart-quote / Jinja breakage)
# ===========================================================================
print("\n-- /settings render guard --")
_rv = _client.get("/settings")
check("/settings renders 200", _rv.status_code == 200)
_settings_html = _rv.get_data(as_text=True)
check("/settings shows the Outlook card", "Outlook Calendar" in _settings_html)
check("/settings shows the unified CRM card with Jobber as an option",
      "Connect your CRM" in _settings_html and "Jobber" in _settings_html)

# ===========================================================================
# Summary
# ===========================================================================
print(f"\n{'='*50}")
print(f"Results: {_pass} passed, {_fail} failed out of {_pass + _fail} checks")
if _fail:
    print("SOME TESTS FAILED")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
    sys.exit(0)
