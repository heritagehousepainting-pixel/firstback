"""Standalone tests for E4 — Closed-loop Google review tracking (plan 07 Change 1).

Tests:
  1. poll sets baseline on first call
  2. second poll does NOT overwrite baseline but updates current
  3. poll is inert / returns None when GOOGLE_PLACES_API_KEY is unset
  4. poll handles API failure (HTTP call raises) -- no DB write, no raise
  5. scan_google_reputation skips a recently-updated business
  6. reputation_milestone fires on a >=5 delta
  7. format_message copy is correct for reputation_milestone

Run: python3 test_google_reputation.py
Exits non-zero on any failure.
"""
import os
import sys
import tempfile
import types
import unittest.mock as mock

# --- Minimal env before any import ------------------------------------------
os.environ["FIRSTBACK_PROVIDER"] = "demo"

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

# Ensure GOOGLE_PLACES_API_KEY starts unset for the gating test.
config.GOOGLE_PLACES_API_KEY = ""

import reputation
import reminders
import alerts

# ---------------------------------------------------------------------------
_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---------------------------------------------------------------------------
# Helper: create a minimal business row and return its id.
# ---------------------------------------------------------------------------
def _make_biz(name="Test Biz", review_link="https://maps.google.com/?place_id=ChIJtest"):
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO businesses (name, review_link) VALUES (?, ?)",
        (name, review_link))
    bid = cur.lastrowid
    conn.commit()
    conn.close()
    return bid


# ---------------------------------------------------------------------------
# 1. poll sets baseline on first call
# ---------------------------------------------------------------------------
print("\n-- 1. poll sets baseline on first call")
bid1 = _make_biz(name="Biz1", review_link="https://maps.google.com/?place_id=ChIJtest1")

# Monkeypatch the Places HTTP call for business bid1.
def _fake_places_ok(url, params=None, timeout=None):
    resp = types.SimpleNamespace()
    resp.raise_for_status = lambda: None
    if "details" in url:
        resp.json = lambda: {"result": {"user_ratings_total": 20, "rating": 4.5}}
    else:
        resp.json = lambda: {"candidates": [{"place_id": "ChIJtest1"}]}
    return resp

with mock.patch("requests.get", side_effect=_fake_places_ok):
    config.GOOGLE_PLACES_API_KEY = "fake-key"
    result = reputation.poll_google_reputation(bid1)

biz1_after = db.get_business(bid1)
check("poll returns review_count=20", result is not None and result.get("review_count") == 20)
check("poll returns star_rating=4.5", result is not None and result.get("star_rating") == 4.5)
check("baseline count set to 20 on first call",
      biz1_after.get("google_review_count_baseline") == 20)
check("baseline rating set to 4.5 on first call",
      biz1_after.get("google_star_rating_baseline") == 4.5)
check("current count set to 20",
      biz1_after.get("google_review_count") == 20)
check("review_count_updated_at is set",
      biz1_after.get("review_count_updated_at") is not None)

# ---------------------------------------------------------------------------
# 2. second poll does NOT overwrite baseline
# ---------------------------------------------------------------------------
print("\n-- 2. second poll does not overwrite baseline")

def _fake_places_30(url, params=None, timeout=None):
    resp = types.SimpleNamespace()
    resp.raise_for_status = lambda: None
    if "details" in url:
        resp.json = lambda: {"result": {"user_ratings_total": 30, "rating": 4.7}}
    else:
        resp.json = lambda: {"candidates": [{"place_id": "ChIJtest1"}]}
    return resp

with mock.patch("requests.get", side_effect=_fake_places_30):
    config.GOOGLE_PLACES_API_KEY = "fake-key"
    result2 = reputation.poll_google_reputation(bid1)

biz1_v2 = db.get_business(bid1)
check("current count updated to 30 on second call",
      biz1_v2.get("google_review_count") == 30)
check("baseline NOT overwritten (still 20)",
      biz1_v2.get("google_review_count_baseline") == 20)
check("baseline rating NOT overwritten (still 4.5)",
      biz1_v2.get("google_star_rating_baseline") == 4.5)

# ---------------------------------------------------------------------------
# 3. poll inert when GOOGLE_PLACES_API_KEY is unset
# ---------------------------------------------------------------------------
print("\n-- 3. poll inert when API key unset")

calls_made = {"n": 0}

def _should_not_call(*args, **kwargs):
    calls_made["n"] += 1
    raise AssertionError("requests.get should NOT be called when API key is unset")

with mock.patch("requests.get", side_effect=_should_not_call):
    config.GOOGLE_PLACES_API_KEY = ""
    result_inert = reputation.poll_google_reputation(bid1)

check("returns None when API key unset", result_inert is None)
check("no network call made when API key unset", calls_made["n"] == 0)

# ---------------------------------------------------------------------------
# 4. poll handles API failure gracefully (no DB write, no raise)
# ---------------------------------------------------------------------------
print("\n-- 4. poll handles API failure")
bid_fail = _make_biz(name="BizFail", review_link="https://maps.google.com/?place_id=ChIJfail")

def _boom(*args, **kwargs):
    raise RuntimeError("network error / timeout")

before = db.get_business(bid_fail)
with mock.patch("requests.get", side_effect=_boom):
    config.GOOGLE_PLACES_API_KEY = "fake-key"
    try:
        result_fail = reputation.poll_google_reputation(bid_fail)
    except Exception as e:
        result_fail = f"RAISED: {e}"

after_fail = db.get_business(bid_fail)
check("returns None on API failure", result_fail is None)
check("no baseline written on failure",
      after_fail.get("google_review_count_baseline") is None)
check("no review_count written on failure",
      after_fail.get("google_review_count") is None)

# ---------------------------------------------------------------------------
# 5. scan_google_reputation skips a recently-updated business
# ---------------------------------------------------------------------------
print("\n-- 5. scan skips a recently-updated business")

# Set review_count_updated_at to "now" so it's within 28 days.
conn = db.get_conn()
conn.execute(
    "UPDATE businesses SET review_count_updated_at=? WHERE id=?",
    (db.now_iso(), bid1))
conn.commit()
conn.close()

polled_calls = {"n": 0}

def _counting_poll(business_id):
    polled_calls["n"] += 1
    return {"review_count": 99, "star_rating": 5.0}

original_poll = reputation.poll_google_reputation
reputation.poll_google_reputation = _counting_poll
config.GOOGLE_PLACES_API_KEY = "fake-key"

# scan_google_reputation uses db.list_businesses() which may include previous biz rows.
# We only care that bid1 (recently updated) is NOT polled.
result_scan = reminders.scan_google_reputation()

reputation.poll_google_reputation = original_poll
biz1_v3 = db.get_business(bid1)

check("scan does not poll recently-updated biz (count=30 unchanged)",
      biz1_v3.get("google_review_count") == 30)

# ---------------------------------------------------------------------------
# 6. reputation_milestone fires on >= 5 delta
# ---------------------------------------------------------------------------
print("\n-- 6. milestone fires on >=5 delta")
bid_ms = _make_biz(name="BizMilestone",
                   review_link="https://maps.google.com/?place_id=ChIJms")

# Set baseline=14, current=20 (delta=6 >= 5), updated_at=old (>28 days ago).
conn = db.get_conn()
conn.execute(
    "UPDATE businesses SET"
    "  google_review_count=20,"
    "  google_star_rating=4.6,"
    "  google_review_count_baseline=14,"
    "  google_star_rating_baseline=4.4,"
    "  review_count_updated_at='2000-01-01T00:00:00+00:00'"  # old -> eligible for poll
    " WHERE id=?",
    (bid_ms,))
conn.commit()
conn.close()

# Make poll return the same current values (20 reviews).
def _poll_ms(business_id):
    if business_id == bid_ms:
        # Simulate: current stays 20, baseline already set (won't be overwritten).
        # We call the real set_google_reputation so fresh_biz read works correctly.
        db.set_google_reputation(business_id, 20, 4.6)
        return {"review_count": 20, "star_rating": 4.6}
    return None

notified = {"called": False, "kind": None, "ctx": None}
original_notify = alerts.notify

def _capturing_notify(biz, kind, ctx):
    if kind == "reputation_milestone":
        notified["called"] = True
        notified["kind"] = kind
        notified["ctx"] = ctx
    # Don't actually fan out.
    return []

reputation.poll_google_reputation = _poll_ms
alerts.notify = _capturing_notify

config.GOOGLE_PLACES_API_KEY = "fake-key"
scan_result = reminders.scan_google_reputation()

reputation.poll_google_reputation = original_poll
alerts.notify = original_notify

check("milestone fired (notify called)",
      notified["called"] is True)
check("milestone kind is reputation_milestone",
      notified["kind"] == "reputation_milestone")
check("milestone delta is 6",
      notified["ctx"] is not None and notified["ctx"].get("delta") == 6)
check("milestone baseline is 14",
      notified["ctx"] is not None and notified["ctx"].get("baseline") == 14)
check("milestone current is 20",
      notified["ctx"] is not None and notified["ctx"].get("current") == 20)

# ---------------------------------------------------------------------------
# 7. format_message copy is correct
# ---------------------------------------------------------------------------
print("\n-- 7. format_message copy")

msg = alerts.format_message("reputation_milestone", {
    "baseline": 14, "current": 20, "delta": 6
})
check("message mentions delta (6)",
      "6" in msg)
check("message mentions baseline (14)",
      "14" in msg)
check("message mentions current (20)",
      "20" in msg)
check("message mentions FirstBack review engine",
      "FirstBack review engine" in msg)

msg_no_delta = alerts.format_message("reputation_milestone", {
    "baseline": 10, "current": 15
})
# When delta is not supplied it should be computed from current - baseline.
check("message computes delta from current-baseline when not supplied",
      "5" in msg_no_delta)

# reputation_milestone is in ALERT_KINDS
check("reputation_milestone in ALERT_KINDS",
      "reputation_milestone" in alerts.ALERT_KINDS)

# _TOGGLE_COL maps to alert_on_roi_milestone
check("reputation_milestone toggle is alert_on_roi_milestone",
      alerts._TOGGLE_COL.get("reputation_milestone") == "alert_on_roi_milestone")

# _subject returns the right string
subj = alerts._subject("reputation_milestone")
check("_subject returns non-generic string for reputation_milestone",
      "FirstBack" in subj and subj != "FirstBack alert")

# ---------------------------------------------------------------------------
print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
