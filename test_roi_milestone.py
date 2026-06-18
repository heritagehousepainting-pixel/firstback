"""ROI milestone check tests. Run: python3 test_roi_milestone.py

Covers:
  - fires at >= 2x when a2p approved + booked >= 1 + roi_milestone_sent_at empty.
  - does NOT fire when a2p is pending (not approved).
  - does NOT fire when roi_milestone_sent_at is already set.
  - does NOT fire below 2x roi_multiple.
  - the body contains no "actual", "cash", or "collected" wording.
No network. Standalone; exit non-zero on failure.
"""
import os
import sys
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.DB_BACKUP_PATH = ""
db.init_db()

import roi

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
# Helpers
# ---------------------------------------------------------------------------
def _make_biz(trade="plumbing", a2p_status="approved", avg_job_value=None,
              roi_milestone_sent_at=None):
    """Insert a fresh business and return its id."""
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO businesses (name, trade, a2p_status, avg_job_value, "
        "roi_milestone_sent_at) VALUES (?,?,?,?,?)",
        (f"Test Biz {trade}", trade, a2p_status, avg_job_value, roi_milestone_sent_at))
    bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return bid


def _insert_lead(business_id, source="missed_call"):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO leads (business_id, name, phone, source, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (business_id, "Test Caller", "+15550000001", source, "new", db.now_iso()))
    lid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return lid


def _insert_appointment(business_id, lead_id):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO appointments (business_id, lead_id, scheduled_for, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (business_id, lead_id, "2026-06-20 09:00", "booked", db.now_iso()))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# TEST 1: fires at >= 2x when a2p approved + booked >= 1 + not already sent.
# With plumbing default ($1800) and PLAN_COST_MONTHLY=$99, roi_multiple = ~18x.
# ---------------------------------------------------------------------------
bid1 = _make_biz(trade="plumbing", a2p_status="approved")
lid1 = _insert_lead(bid1, source="missed_call")
_insert_appointment(bid1, lid1)

result1 = roi.check_roi_milestone(bid1)
check("fires: returns a dict when a2p approved, booked >= 1, multiple >= 2",
      result1 is not None)
check("fires: dict has 'multiple' key",
      result1 is not None and "multiple" in result1)
check("fires: dict has 'revenue' key",
      result1 is not None and "revenue" in result1)
check("fires: dict has 'avg_source' key",
      result1 is not None and "avg_source" in result1)
check("fires: dict has 'body' key",
      result1 is not None and "body" in result1)
check("fires: multiple is >= 2.0",
      result1 is not None and result1["multiple"] >= 2.0)


# ---------------------------------------------------------------------------
# TEST 2: does NOT fire when a2p is pending (not approved).
# ---------------------------------------------------------------------------
bid2 = _make_biz(trade="plumbing", a2p_status="pending")
lid2 = _insert_lead(bid2, source="missed_call")
_insert_appointment(bid2, lid2)

result2 = roi.check_roi_milestone(bid2)
check("does NOT fire when a2p is pending",
      result2 is None)


# ---------------------------------------------------------------------------
# TEST 3: does NOT fire when roi_milestone_sent_at is already set.
# ---------------------------------------------------------------------------
bid3 = _make_biz(trade="plumbing", a2p_status="approved",
                 roi_milestone_sent_at="2026-06-17T10:00:00+00:00")
lid3 = _insert_lead(bid3, source="missed_call")
_insert_appointment(bid3, lid3)

result3 = roi.check_roi_milestone(bid3)
check("does NOT fire when roi_milestone_sent_at is already set",
      result3 is None)


# ---------------------------------------------------------------------------
# TEST 4: does NOT fire when roi_multiple is below 2.0.
# Use a very high PLAN_COST_MONTHLY equivalent by using a trade with a low default
# and setting avg_job_value to something that gives < 2x (e.g. $100 job, $99 plan).
# But PLAN_COST_MONTHLY=99, so $100 job gives ~1.01x < 2.0.
# ---------------------------------------------------------------------------
bid4 = _make_biz(trade="plumbing", a2p_status="approved", avg_job_value=100.0)
lid4 = _insert_lead(bid4, source="missed_call")
_insert_appointment(bid4, lid4)

result4 = roi.check_roi_milestone(bid4)
check("does NOT fire when roi_multiple is below 2.0 (avg_job_value=$100, plan=$99)",
      result4 is None)


# ---------------------------------------------------------------------------
# TEST 5: body language — no invented "actual", "cash", or "collected" wording.
# ---------------------------------------------------------------------------
body = result1["body"] if result1 else ""
body_lower = body.lower()
check("body: contains 'estimated' (not cash language)",
      "estimated" in body_lower or "estimate" in body_lower)
check("body: does NOT contain the word 'actual'",
      "actual" not in body_lower)
check("body: does NOT contain the word 'cash'",
      "cash" not in body_lower)
check("body: does NOT contain the word 'collected'",
      "collected" not in body_lower)
check("body: contains the dollar amount",
      "$" in body)
check("body: references 'x' multiplier",
      "x" in body_lower)


# ---------------------------------------------------------------------------
# TEST 6: does NOT fire when there are no bookings (booked = 0).
# ---------------------------------------------------------------------------
bid6 = _make_biz(trade="plumbing", a2p_status="approved")
_insert_lead(bid6, source="missed_call")
# No appointment inserted.

result6 = roi.check_roi_milestone(bid6)
check("does NOT fire when booked = 0",
      result6 is None)


# ---------------------------------------------------------------------------
# TEST 7: never raises — returns None even for a bad business_id.
# ---------------------------------------------------------------------------
bad_result = roi.check_roi_milestone(999999)
check("never raises: returns None for a nonexistent business_id",
      bad_result is None)


# ---------------------------------------------------------------------------
# TEST 8: avg_source distinction in body (owner vs industry_default).
# ---------------------------------------------------------------------------
bid8 = _make_biz(trade="plumbing", a2p_status="approved", avg_job_value=5000.0)
lid8 = _insert_lead(bid8, source="missed_call")
_insert_appointment(bid8, lid8)

result8 = roi.check_roi_milestone(bid8)
check("owner avg: avg_source is 'owner'",
      result8 is not None and result8.get("avg_source") == "owner")
check("owner avg body: references 'your average job value'",
      result8 is not None and "your average job value" in result8.get("body", "").lower())

bid9 = _make_biz(trade="plumbing", a2p_status="approved")  # no avg_job_value
lid9 = _insert_lead(bid9, source="missed_call")
_insert_appointment(bid9, lid9)

result9 = roi.check_roi_milestone(bid9)
check("industry default: avg_source is 'industry_default'",
      result9 is not None and result9.get("avg_source") == "industry_default")
check("industry default body: references 'industry-average job value'",
      result9 is not None and "industry" in result9.get("body", "").lower())


print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
