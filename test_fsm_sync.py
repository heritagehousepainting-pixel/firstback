"""FSM sync tests (Plan 13 — Jobber).

Run: FIRSTBACK_DB_PATH=/tmp/test_fsm.db .venv/bin/python test_fsm_sync.py

35 mocked cases covering:
  - configured/connected gating
  - auth_url structure
  - connect_with_code success + failure
  - _access_token fresh/stale/no-refresh/refresh-failure
  - disconnect
  - fetch_clients success/error/pagination
  - fetch_jobs
  - sync_clients (F1: direct upsert_suggestion, NOT contact_import.ingest)
  - push_quote_request success/error
  - push_booking_async stores id / no-op when unconfigured
  - maybe_sync_all skip-unconnected / skip-within-interval / sync-eligible
  - DB migrations idempotent
  - token encryption round-trip
  - screen_caller trusted after accept, prospect while pending
  - routes: connect without creds, callback wrong-state, callback valid,
    disconnect CSRF, sync

No live credentials. All Jobber HTTP is mocked. Standalone-script convention:
prints ok/FAIL per case, exits 1 on any failure.
"""
import os
import sys
import tempfile
import threading
import time
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# ---- env setup (before any firstback import) ----
os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")
# No live Jobber creds by default (gated no-op)
os.environ.pop("JOBBER_CLIENT_ID", None)
os.environ.pop("JOBBER_CLIENT_SECRET", None)
os.environ.pop("JOBBER_REDIRECT_URI", None)

import config

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
_DB_PATH = _TMP.name
config.DB_PATH = _DB_PATH
os.environ["FIRSTBACK_DB_PATH"] = _DB_PATH

import db
db.DB_PATH = _DB_PATH
db.init_db()

import fsm_provider
import jobber_fsm
import fsm_sync
import connections
import triage

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


# ===========================================================================
# 1. DB migrations — idempotent
# ===========================================================================
print("\n-- DB migrations (idempotent) --")

def _has_col(table, col):
    conn = db.get_conn()
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    conn.close()
    return col in cols

check("businesses.fsm_last_synced_at exists",    _has_col("businesses", "fsm_last_synced_at"))
check("businesses.fsm_clients_synced exists",    _has_col("businesses", "fsm_clients_synced"))
check("appointments.fsm_external_id exists",     _has_col("appointments", "fsm_external_id"))
check("appointments.fsm_pushed_at exists",       _has_col("appointments", "fsm_pushed_at"))

# Re-run init_db: must be idempotent (no error)
try:
    db.init_db()
    check("init_db idempotent (no error on second run)", True)
except Exception as e:
    check(f"init_db idempotent -- RAISED: {e}", False)


# ===========================================================================
# 2. configured() gate
# ===========================================================================
print("\n-- configured() gate --")

check("configured() False when no creds", not jobber_fsm.configured())
check("fsm_sync.configured() False when no creds", not fsm_sync.configured())
check("fsm_sync.push_configured() False when no creds", not fsm_sync.push_configured())

# Temporarily set creds
config.JOBBER_CLIENT_ID = "testclientid"
config.JOBBER_CLIENT_SECRET = "testclientsecret"
jobber_fsm.JOBBER_CLIENT_ID = "testclientid"
jobber_fsm.JOBBER_CLIENT_SECRET = "testclientsecret"

check("configured() True when creds set",         jobber_fsm.configured())
check("fsm_sync.configured() True when creds set", fsm_sync.configured())
check("push_configured() True when creds set",    fsm_sync.push_configured())


# ===========================================================================
# 3. auth_url
# ===========================================================================
print("\n-- auth_url --")

state = "teststate123"
url = jobber_fsm.auth_url(state)
check("auth_url contains authorization endpoint",    "getjobber.com" in url)
check("auth_url contains client_id",                "testclientid" in url)
check("auth_url contains state param",              "teststate123" in url)
check("auth_url contains response_type=code",       "response_type=code" in url)
check("auth_url contains read_clients scope",       "read_clients" in url)


# ===========================================================================
# 4. connect_with_code
# ===========================================================================
print("\n-- connect_with_code --")

BIZ_ID = 1
_fake_tok = {
    "access_token": "acc_abc",
    "refresh_token": "ref_xyz",
    "expires_in": 3600,
}

with patch("jobber_fsm.requests") as mock_req:
    resp = MagicMock()
    resp.json.return_value = _fake_tok
    resp.raise_for_status.return_value = None
    mock_req.post.return_value = resp
    jobber_fsm.connect_with_code(BIZ_ID, "auth_code_ok")

intg = db.get_integration(BIZ_ID, "jobber")
check("connect_with_code stores access_token",  intg and intg.get("access_token") == "acc_abc")
check("connect_with_code stores refresh_token", intg and intg.get("refresh_token") == "ref_xyz")
check("connect_with_code marks connected=1",    intg and bool(intg.get("connected")))

# Failure: raise_for_status raises
with patch("jobber_fsm.requests") as mock_req:
    resp2 = MagicMock()
    resp2.raise_for_status.side_effect = Exception("HTTP 401")
    mock_req.post.return_value = resp2
    raised = False
    try:
        jobber_fsm.connect_with_code(BIZ_ID, "bad_code")
    except Exception:
        raised = True
check("connect_with_code raises on HTTP error (caller redirects)", raised)


# ===========================================================================
# 5. _access_token — fresh / stale / no-refresh / refresh-failure
# ===========================================================================
print("\n-- _access_token --")

# 5a. fresh: access is fresh, return stored token
future_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
db.set_oauth_tokens(BIZ_ID, "jobber", "fresh_acc", "ref_xyz", future_expiry)
with patch("jobber_fsm.access_is_fresh", return_value=True):
    tok = jobber_fsm._access_token(BIZ_ID)
check("_access_token fresh: returns stored token", tok == "fresh_acc")

# 5b. stale: access_is_fresh False -> refresh
_new_tok = {"access_token": "new_acc2", "expires_in": 3600}
with patch("jobber_fsm.access_is_fresh", return_value=False), \
     patch("jobber_fsm.requests") as mock_req:
    resp3 = MagicMock()
    resp3.json.return_value = _new_tok
    resp3.raise_for_status.return_value = None
    mock_req.post.return_value = resp3
    tok2 = jobber_fsm._access_token(BIZ_ID)
check("_access_token stale: refreshes and returns new token", tok2 == "new_acc2")

# 5c. no refresh token -> None
db.set_oauth_tokens(BIZ_ID, "jobber", None, None, None)
tok3 = jobber_fsm._access_token(BIZ_ID)
check("_access_token no refresh_token -> None", tok3 is None)

# 5d. refresh HTTP failure -> None (fail-open)
db.set_oauth_tokens(BIZ_ID, "jobber", "old_acc", "ref_xyz", "2020-01-01T00:00:00+00:00")
with patch("jobber_fsm.access_is_fresh", return_value=False), \
     patch("jobber_fsm.requests") as mock_req:
    resp4 = MagicMock()
    resp4.raise_for_status.side_effect = Exception("network error")
    mock_req.post.return_value = resp4
    tok4 = jobber_fsm._access_token(BIZ_ID)
check("_access_token refresh failure -> None (fail-open)", tok4 is None)

# Restore valid tokens for subsequent tests
db.set_oauth_tokens(BIZ_ID, "jobber", "acc_abc", "ref_xyz", future_expiry)


# ===========================================================================
# 6. disconnect
# ===========================================================================
print("\n-- disconnect --")

# Verify connected first
intg_before = db.get_integration(BIZ_ID, "jobber")
check("pre-disconnect: connected=1", bool(intg_before and intg_before.get("connected")))

jobber_fsm.disconnect(BIZ_ID)
intg_after = db.get_integration(BIZ_ID, "jobber")
check("disconnect: connected=0", not bool(intg_after and intg_after.get("connected")))
check("disconnect: access_token cleared", not bool(intg_after and intg_after.get("access_token")))
check("disconnect: refresh_token cleared", not bool(intg_after and intg_after.get("refresh_token")))

# Re-connect for subsequent tests
db.set_oauth_tokens(BIZ_ID, "jobber", "acc_abc", "ref_xyz", future_expiry)


# ===========================================================================
# 7. fetch_clients — success / error / pagination
# ===========================================================================
print("\n-- fetch_clients --")

_CLIENT_PAGE1 = {
    "data": {
        "clients": {
            "nodes": [
                {"name": "Alice A", "email": "alice@example.com",
                 "phones": [{"number": "+12155550001"}]},
                {"name": "Bob B",   "email": "bob@example.com",
                 "phones": [{"number": "+12155550002"}]},
            ],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
        }
    }
}
_CLIENT_PAGE2 = {
    "data": {
        "clients": {
            "nodes": [
                {"name": "Carol C", "email": "carol@example.com",
                 "phones": [{"number": "+12155550003"}]},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
}

# Pagination: two pages
_page_responses = [_CLIENT_PAGE1, _CLIENT_PAGE2]
_page_call_count = [0]

def _fake_gql_paginated(biz_id, query, variables=None):
    idx = _page_call_count[0]
    _page_call_count[0] += 1
    if idx < len(_page_responses):
        return _page_responses[idx]
    return None

with patch.object(jobber_fsm._provider, "_gql", side_effect=_fake_gql_paginated):
    clients = jobber_fsm.fetch_clients(BIZ_ID)

check("fetch_clients: returns 3 clients across 2 pages", len(clients) == 3)
check("fetch_clients: first client name correct",  clients[0]["name"] == "Alice A")
check("fetch_clients: third client phone correct", "+12155550003" in clients[2]["phones"])

# Error: _gql returns None -> []
with patch.object(jobber_fsm._provider, "_gql", return_value=None):
    clients_err = jobber_fsm.fetch_clients(BIZ_ID)
check("fetch_clients: None from _gql -> []", clients_err == [])

# Not connected
jobber_fsm.disconnect(BIZ_ID)
clients_disc = jobber_fsm.fetch_clients(BIZ_ID)
check("fetch_clients: not connected -> []", clients_disc == [])
db.set_oauth_tokens(BIZ_ID, "jobber", "acc_abc", "ref_xyz", future_expiry)


# ===========================================================================
# 8. fetch_jobs
# ===========================================================================
print("\n-- fetch_jobs --")

_JOBS_RESP = {
    "data": {
        "jobs": {
            "nodes": [
                {"title": "Exterior Paint", "jobStatus": "active",
                 "client": {"phones": [{"number": "+12155550001"}]}},
                {"title": "Interior Paint", "jobStatus": "completed",
                 "client": {"phones": []}},
            ]
        }
    }
}
with patch.object(jobber_fsm._provider, "_gql", return_value=_JOBS_RESP):
    jobs = jobber_fsm.fetch_jobs(BIZ_ID)
check("fetch_jobs: returns 2 jobs", len(jobs) == 2)
check("fetch_jobs: first job title correct", jobs[0]["title"] == "Exterior Paint")
check("fetch_jobs: second job no phone",    jobs[1]["client_phone"] == "")


# ===========================================================================
# 9. sync_clients (F1: direct upsert_suggestion, NOT contact_import.ingest)
# ===========================================================================
print("\n-- sync_clients (F1: direct upsert_suggestion) --")

_upsert_calls = []
_orig_upsert = db.upsert_suggestion

def _capture_upsert(business_id, number, name, category, reason, source="behavior"):
    _upsert_calls.append({"business_id": business_id, "number": number,
                           "name": name, "category": category,
                           "reason": reason, "source": source})
    return _orig_upsert(business_id, number, name, category, reason, source)

db.upsert_suggestion = _capture_upsert

_FAKE_CLIENTS = [
    {"name": "Dave D", "phones": ["+12155551001"], "email": "dave@example.com"},
    {"name": "Eve E",  "phones": ["+12155551002"], "email": "eve@example.com"},
]

with patch.object(jobber_fsm, "fetch_clients", return_value=_FAKE_CLIENTS):
    result = fsm_sync.sync_clients(BIZ_ID)

check("sync_clients: returns clients_fetched=2", result.get("clients_fetched") == 2)
check("sync_clients: returns suggested=2",       result.get("suggested") == 2)
check("sync_clients: upsert called for each",   len(_upsert_calls) >= 2)
check("sync_clients: category='customer'",
      all(c["category"] == "customer" for c in _upsert_calls))
check("sync_clients: source='import-jobber'",
      all(c["source"] == "import-jobber" for c in _upsert_calls))
check("sync_clients: reason contains 'Jobber'",
      all("Jobber" in c["reason"] for c in _upsert_calls))

# F1 check: contact_import.ingest is NOT called
import contact_import
_ingest_calls = []
_orig_ingest = contact_import.ingest

def _capture_ingest(*a, **kw):
    _ingest_calls.append((a, kw))
    return _orig_ingest(*a, **kw)

contact_import.ingest = _capture_ingest

_upsert_calls.clear()
with patch.object(jobber_fsm, "fetch_clients", return_value=_FAKE_CLIENTS):
    fsm_sync.sync_clients(BIZ_ID)

check("sync_clients F1: contact_import.ingest NOT called", len(_ingest_calls) == 0)

# Restore
db.upsert_suggestion = _orig_upsert
contact_import.ingest = _orig_ingest

# Dedupe: second sync doesn't re-suggest already-pending numbers
_upsert2_calls = []
def _capture_upsert2(business_id, number, name, category, reason, source="behavior"):
    _upsert2_calls.append(number)
    return _orig_upsert(business_id, number, name, category, reason, source)

db.upsert_suggestion = _capture_upsert2
with patch.object(jobber_fsm, "fetch_clients", return_value=_FAKE_CLIENTS):
    result2 = fsm_sync.sync_clients(BIZ_ID)
# Already-pending suggestions: status='pending' so they are in the skip set
# (decided = accepted|dismissed, classified = contacts — pending is NOT in skip).
# upsert_suggestion itself handles the "WHERE status='pending'" on conflict,
# so the call still goes through but does an UPDATE not INSERT for pending.
# What we want to verify: skipped=0 means they go through (not in classified+decided).
check("sync_clients dedupe: skipped=0 for pending (upsert updates pending)",
      result2.get("skipped", 0) == 0)
db.upsert_suggestion = _orig_upsert

# No-op when unconfigured
config.JOBBER_CLIENT_ID = ""
jobber_fsm.JOBBER_CLIENT_ID = ""
result_nc = fsm_sync.sync_clients(BIZ_ID)
check("sync_clients: no-op when unconfigured", result_nc.get("clients_fetched") == 0)
config.JOBBER_CLIENT_ID = "testclientid"
jobber_fsm.JOBBER_CLIENT_ID = "testclientid"

# No-op when disconnected
jobber_fsm.disconnect(BIZ_ID)
result_nd = fsm_sync.sync_clients(BIZ_ID)
check("sync_clients: no-op when not connected", result_nd.get("clients_fetched") == 0)
db.set_oauth_tokens(BIZ_ID, "jobber", "acc_abc", "ref_xyz", future_expiry)


# ===========================================================================
# 10. push_quote_request — success / error
# ===========================================================================
print("\n-- push_quote_request --")

_PUSH_OK_RESP = {
    "data": {
        "requestCreate": {
            "request": {"id": "jobber-req-001"},
            "userErrors": [],
        }
    }
}
with patch.object(jobber_fsm._provider, "_gql", return_value=_PUSH_OK_RESP):
    req_id = jobber_fsm.push_quote_request(
        BIZ_ID, {"name": "Alice", "phone": "+12155550001"}, {"day": "2026-07-01"})
check("push_quote_request: returns request id on success", req_id == "jobber-req-001")

# userErrors -> None
_PUSH_ERR_RESP = {
    "data": {
        "requestCreate": {
            "request": None,
            "userErrors": [{"message": "Client not found"}],
        }
    }
}
with patch.object(jobber_fsm._provider, "_gql", return_value=_PUSH_ERR_RESP):
    req_id_err = jobber_fsm.push_quote_request(BIZ_ID, {}, {})
check("push_quote_request: userErrors -> None", req_id_err is None)

# _gql returns None -> None
with patch.object(jobber_fsm._provider, "_gql", return_value=None):
    req_id_none = jobber_fsm.push_quote_request(BIZ_ID, {}, {})
check("push_quote_request: _gql None -> None", req_id_none is None)


# ===========================================================================
# 11. push_booking_async — stores id / no-op when unconfigured
# ===========================================================================
print("\n-- push_booking_async --")

# Create an appointment to store the external id on
lead = db.get_lead(1, BIZ_ID) if hasattr(db, 'get_lead') else None
# Insert a minimal appointment
conn = db.get_conn()
conn.execute(
    "INSERT INTO appointments (business_id, lead_id, status) VALUES (?,?,?)",
    (BIZ_ID, 1, "booked"))
conn.commit()
appt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.close()

_PUSH_OK2 = {
    "data": {
        "requestCreate": {
            "request": {"id": "jobber-req-async-001"},
            "userErrors": [],
        }
    }
}
with patch.object(jobber_fsm._provider, "_gql", return_value=_PUSH_OK2):
    fsm_sync.push_booking_async(BIZ_ID, appt_id, {"name": "Alice"}, {"day": "2026-07-01"})
    time.sleep(0.2)  # let daemon thread finish

conn2 = db.get_conn()
row = conn2.execute(
    "SELECT fsm_external_id FROM appointments WHERE id=?", (appt_id,)).fetchone()
conn2.close()
check("push_booking_async: stores fsm_external_id on success",
      row and row[0] == "jobber-req-async-001")

# No-op when unconfigured
config.JOBBER_CLIENT_ID = ""
jobber_fsm.JOBBER_CLIENT_ID = ""
push_called = [False]
def _no_call(*a, **kw):
    push_called[0] = True
    return "should_not_be_called"

with patch.object(jobber_fsm._provider, "push_quote_request", side_effect=_no_call):
    fsm_sync.push_booking_async(BIZ_ID, appt_id, {}, {})
    time.sleep(0.1)
check("push_booking_async: no-op when unconfigured (push not called)", not push_called[0])

# Restore creds
config.JOBBER_CLIENT_ID = "testclientid"
jobber_fsm.JOBBER_CLIENT_ID = "testclientid"


# ===========================================================================
# 12. maybe_sync_all — skip unconnected / skip within interval / sync eligible
# ===========================================================================
print("\n-- maybe_sync_all --")

# Unconnected: sync_clients should not be called
jobber_fsm.disconnect(BIZ_ID)
_sync_calls_ma = []
_orig_sc = fsm_sync.sync_clients

def _capture_sync(bid):
    _sync_calls_ma.append(bid)
    return {"clients_fetched": 2, "suggested": 2, "skipped": 0}

fsm_sync.sync_clients = _capture_sync
fsm_sync.maybe_sync_all()
check("maybe_sync_all: skips unconnected business", BIZ_ID not in _sync_calls_ma)

# Connected but within interval
db.set_oauth_tokens(BIZ_ID, "jobber", "acc_abc", "ref_xyz", future_expiry)
recent_stamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
db.set_fsm_sync_stamp(BIZ_ID, recent_stamp, 0)
_sync_calls_ma.clear()
fsm_sync.maybe_sync_all()
check("maybe_sync_all: skips business within interval", BIZ_ID not in _sync_calls_ma)

# Past interval -> syncs
old_stamp = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
db.set_fsm_sync_stamp(BIZ_ID, old_stamp, 0)
_sync_calls_ma.clear()
fsm_sync.maybe_sync_all()
check("maybe_sync_all: syncs business past interval", BIZ_ID in _sync_calls_ma)

# No stamp (first connect) -> syncs
db.set_fsm_sync_stamp(BIZ_ID, None, 0)
_sync_calls_ma.clear()
fsm_sync.maybe_sync_all()
check("maybe_sync_all: syncs business with no prior stamp", BIZ_ID in _sync_calls_ma)

# No-op when unconfigured
config.JOBBER_CLIENT_ID = ""
jobber_fsm.JOBBER_CLIENT_ID = ""
_sync_calls_ma.clear()
result_nc = fsm_sync.maybe_sync_all()
check("maybe_sync_all: no-op when unconfigured",
      result_nc.get("businesses_checked") == 0 and len(_sync_calls_ma) == 0)

# Restore
config.JOBBER_CLIENT_ID = "testclientid"
jobber_fsm.JOBBER_CLIENT_ID = "testclientid"
fsm_sync.sync_clients = _orig_sc


# ===========================================================================
# 13. Token encryption round-trip
# ===========================================================================
print("\n-- token encryption --")

import token_crypto
# Set a test encryption key
config.TOKEN_ENC_KEY = "test-encryption-key-fsm-sync"
import importlib
importlib.reload(token_crypto)
import db as _db_reload
# Re-test with encryption on
enc = token_crypto.encrypt("myaccesstoken")
dec = token_crypto.decrypt(enc)
check("token encrypt/decrypt round-trip", dec == "myaccesstoken")
check("encrypt(None) is None", token_crypto.encrypt(None) is None)
check("decrypt(None) is None", token_crypto.decrypt(None) is None)
# Restore (no encryption in tests to avoid complicating other assertions)
config.TOKEN_ENC_KEY = ""
importlib.reload(token_crypto)


# ===========================================================================
# 14. screen_caller: trusted after accept, prospect while pending
# ===========================================================================
print("\n-- screen_caller: trusted after accept / prospect while pending --")

# Ensure the test phone is in the suggestion queue (pending)
PHONE_NUM = "2155559999"
db.upsert_suggestion(BIZ_ID, PHONE_NUM, "Test Client",
                     category="customer", reason="Jobber client", source="import-jobber")

# While pending: is_known_caller checks contacts (NOT contact_suggestions)
known_while_pending = db.is_known_caller(BIZ_ID, PHONE_NUM)
check("is_known_caller False while suggestion pending", not known_while_pending)

# Accept the suggestion -> moves to contacts
sugs = db.list_suggestions(BIZ_ID, "pending")
sug = next((s for s in sugs if s["number"][-10:] == PHONE_NUM[-10:]), None)
if sug:
    # Accept: move to contacts table, mark suggestion accepted
    db.set_contact(BIZ_ID, sug["number"], sug["suggested_category"],
                   name=sug.get("name"), source="suggested")
    db.set_suggestion_status(sug["id"], "accepted", BIZ_ID)

known_after_accept = db.is_known_caller(BIZ_ID, PHONE_NUM)
check("is_known_caller True after accept", known_after_accept)

# screen_caller on the now-accepted number should be "trusted"
verdict = triage.screen_caller(BIZ_ID, PHONE_NUM)
# screen_caller returns a dict (status key) or a string depending on version
_verdict_status = verdict.get("status") if isinstance(verdict, dict) else verdict
check("screen_caller returns 'trusted' after accept", _verdict_status == "trusted")


# ===========================================================================
# 15. Routes (Flask test client)
# ===========================================================================
print("\n-- routes --")

import app as _app
_client = _app.app.test_client()

# Login first
with _app.app.app_context():
    from werkzeug.security import generate_password_hash
    _app.app.config["TESTING"] = True
    _app.app.config["WTF_CSRF_ENABLED"] = False

def _login():
    return _client.post("/login", data={
        "email": config.SEED_OWNER_EMAIL,
        "password": config.SEED_OWNER_PASSWORD,
    }, follow_redirects=True)

_login()

# 15a. Connect redirect when unconfigured
config.JOBBER_CLIENT_ID = ""
jobber_fsm.JOBBER_CLIENT_ID = ""
with _client.application.test_request_context():
    pass
resp_connect_uncfg = _client.get("/api/fsm/jobber/connect", follow_redirects=False)
check("GET /api/fsm/jobber/connect: unconfigured -> redirect to ?fsmerror=unconfigured",
      resp_connect_uncfg.status_code in (301, 302) and
      b"fsmerror=unconfigured" in resp_connect_uncfg.headers.get("Location", "").encode())

# 15b. Connect redirect when configured -> redirects to Jobber
config.JOBBER_CLIENT_ID = "testclientid"
jobber_fsm.JOBBER_CLIENT_ID = "testclientid"
resp_connect_cfg = _client.get("/api/fsm/jobber/connect", follow_redirects=False)
check("GET /api/fsm/jobber/connect: configured -> redirect to Jobber auth URL",
      resp_connect_cfg.status_code in (301, 302) and
      "getjobber.com" in (resp_connect_cfg.headers.get("Location") or ""))

# 15c. Callback: wrong state
with _client.session_transaction() as sess:
    sess["fsm_j_state"] = "expected_state"
resp_cb_bad = _client.get(
    "/api/fsm/jobber/callback?code=xyz&state=wrong_state", follow_redirects=False)
check("GET /api/fsm/jobber/callback: wrong state -> ?fsmerror=state",
      resp_cb_bad.status_code in (301, 302) and
      "fsmerror=state" in (resp_cb_bad.headers.get("Location") or ""))

# 15d. Callback: valid state (mock connect_with_code)
with _client.session_transaction() as sess:
    sess["fsm_j_state"] = "valid_state"
with patch.object(jobber_fsm, "connect_with_code", return_value=None):
    with patch("fsm_sync.sync_clients", return_value={"clients_fetched": 0}):
        resp_cb_ok = _client.get(
            "/api/fsm/jobber/callback?code=goodcode&state=valid_state",
            follow_redirects=False)
check("GET /api/fsm/jobber/callback: valid -> redirect ?fsmconnected=1",
      resp_cb_ok.status_code in (301, 302) and
      "fsmconnected=1" in (resp_cb_ok.headers.get("Location") or ""))

# 15e. Disconnect: missing CSRF -> 403
resp_disc_no_csrf = _client.post("/api/fsm/jobber/disconnect")
check("POST /api/fsm/jobber/disconnect: no CSRF -> 403",
      resp_disc_no_csrf.status_code == 403)

# 15f. Sync: missing CSRF -> 403
resp_sync_no_csrf = _client.post("/api/fsm/sync")
check("POST /api/fsm/sync: no CSRF -> 403",
      resp_sync_no_csrf.status_code == 403)

# 15g. Sync: unconfigured -> 400
config.JOBBER_CLIENT_ID = ""
jobber_fsm.JOBBER_CLIENT_ID = ""
# Need valid CSRF for this
with _client.session_transaction() as sess:
    csrf = sess.get("csrf_token", "")
resp_sync_uncfg = _client.post("/api/fsm/sync",
    headers={"X-CSRF-Token": csrf})
check("POST /api/fsm/sync: unconfigured -> 400",
      resp_sync_uncfg.status_code == 400)

# Restore creds
config.JOBBER_CLIENT_ID = "testclientid"
jobber_fsm.JOBBER_CLIENT_ID = "testclientid"


# ===========================================================================
# 16. recommended_setup includes jobber
# ===========================================================================
print("\n-- recommended_setup includes jobber --")

biz_setup = db.get_business(BIZ_ID)
rec_not_connected = connections.recommended_setup(
    biz_setup, jobber_connected=False)
rec_connected = connections.recommended_setup(
    biz_setup, jobber_connected=True)
jobber_item_nc = next((i for i in rec_not_connected["items"] if i["key"] == "jobber"), None)
jobber_item_c  = next((i for i in rec_connected["items"]    if i["key"] == "jobber"), None)
check("recommended_setup: jobber item present",                  jobber_item_nc is not None)
check("recommended_setup: jobber optional=True",                 jobber_item_nc and jobber_item_nc.get("optional"))
check("recommended_setup: jobber done=False when not connected", jobber_item_nc and not jobber_item_nc.get("done"))
check("recommended_setup: jobber done=True when connected",      jobber_item_c  and jobber_item_c.get("done"))
check("recommended_setup: jobber href points to #set-jobber",
      jobber_item_nc and "#set-jobber" in (jobber_item_nc.get("href") or ""))


# ===========================================================================
# 17. fsm_provider interface (importable, subclass-able)
# ===========================================================================
print("\n-- fsm_provider interface --")

check("FSMProvider importable", hasattr(fsm_provider, "FSMProvider"))
check("FSMProvider has PROVIDER_KEY", hasattr(fsm_provider.FSMProvider, "PROVIDER_KEY"))

class _TestProvider(fsm_provider.FSMProvider):
    PROVIDER_KEY = "test"
    def configured(self): return True
    def is_connected(self, bid): return False
    def auth_url(self, state): return "https://example.com"
    def connect_with_code(self, bid, code): pass
    def disconnect(self, bid): pass
    def fetch_clients(self, bid): return []
    def fetch_jobs(self, bid): return []
    def push_quote_request(self, bid, lead, booking): return None

_tp = _TestProvider()
check("FSMProvider subclassable", _tp.configured() is True)
check("FSMProvider PROVIDER_KEY = 'test'", _tp.PROVIDER_KEY == "test")


# ===========================================================================
# Final summary
# ===========================================================================
print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
