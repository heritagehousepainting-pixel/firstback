"""E3a+E3b: Seasonal campaign (Change 5) + density-aware referral (Change 6) tests.

Standalone, temp DB + demo brain; no network.

Covers:
  - seasonal_cohort filters by recency (>90 days) and excludes non-booked leads
  - _copy_seasonal generates correct copy
  - recent_growth_touch_kind DB helper (kind-filtered frequency cap)
  - launch logic: queues held rows for cohort, skips opt-outs, skips recent touches,
    skips bodies with unfilled placeholders, enforces 28-day frequency cap
  - _copy_referral_dense fires at zip_count >= 2, standard at 0
  - density-aware referral: plays() uses dense copy when nearby >= 2
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone, date

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
import messaging
messaging.TWILIO_ACCOUNT_SID = ""   # simulated; opt-out still suppresses
import app as _app  # noqa: F401  # runs migrations
import growth

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


def day_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


def iso_ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def clear_scheduled(business_id):
    conn = db.get_conn()
    conn.execute("DELETE FROM scheduled_messages WHERE business_id=?", (business_id,))
    conn.commit()
    conn.close()


def clear_touch_log(business_id):
    conn = db.get_conn()
    conn.execute("DELETE FROM growth_touch_log WHERE business_id=?", (business_id,))
    conn.commit()
    conn.close()


def count_held_seasonal(business_id):
    conn = db.get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM scheduled_messages "
        "WHERE business_id=? AND kind='seasonal' AND status='held'",
        (business_id,)).fetchone()[0]
    conn.close()
    return n


def simulate_launch(biz, service="AC tune-up"):
    """Exercise the core logic of launch_seasonal_campaign without HTTP layer.
    Returns {'queued': n, 'blocked': bool}."""
    if db.recent_growth_touch_kind(biz["id"], "seasonal", within_days=28):
        return {"queued": 0, "blocked": True}
    today = date.today()
    cohort = growth.seasonal_cohort(biz["id"], today)
    queued = 0
    for lead in cohort:
        phone = lead.get("phone", "").strip()
        if not phone or messaging.outbound_mode(biz, phone) == "suppressed":
            continue
        if db.recent_growth_touch(biz["id"], lead["id"], within_days=30):
            continue
        body = growth._copy_seasonal(lead.get("first", ""), biz, service)
        if "[" in body:
            continue
        send_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        db.add_scheduled_message(biz["id"], lead["id"], None, "seasonal",
                                 send_at, body, status="held")
        queued += 1
    return {"queued": queued, "blocked": False}


# ===========================================================================
# Setup: business with HVAC trade
# ===========================================================================
db.set_avg_job_value(1, 2000)
db.update_business(1, {"trade": "HVAC", "review_link": "https://g.page/r/testlink"})
biz = db.get_business(1)
_today = date.today()

# ===========================================================================
# seasonal_cohort: recency filter
# ===========================================================================
# Lead with last appt 100 days ago -> eligible (> 90 days)
_old = db.create_lead(1, "Old Customer", "+15550100001")
db.book_appointment(1, _old, "old job", day=day_ago(100), slot_time="09:00")

# Lead with last appt 30 days ago -> NOT eligible (<= 90 days)
_recent = db.create_lead(1, "Recent Customer", "+15550100002")
db.book_appointment(1, _recent, "recent job", day=day_ago(30), slot_time="10:00")

# Lead with no appointment -> NOT eligible
_noAppt = db.create_lead(1, "No Appt Lead", "+15550100003")

# Lead with appointment exactly 90 days ago -> NOT eligible (border: must be > 90)
_border = db.create_lead(1, "Border Lead", "+15550100004")
db.book_appointment(1, _border, "border job", day=day_ago(90), slot_time="11:00")

_cohort = growth.seasonal_cohort(1, _today)
_cohort_ids = [c["id"] for c in _cohort]

check("seasonal_cohort includes lead with last appt > 90 days ago", _old in _cohort_ids)
check("seasonal_cohort excludes lead with last appt <= 90 days ago (30 days)", _recent not in _cohort_ids)
check("seasonal_cohort excludes lead with no appointments", _noAppt not in _cohort_ids)
check("seasonal_cohort excludes lead at exactly 90 days (border — must be > 90)", _border not in _cohort_ids)

# Cohort entry structure
if _cohort:
    _sample = next((c for c in _cohort if c["id"] == _old), None)
    check("cohort entry has required fields (id, name, phone, first)",
          _sample and all(k in _sample for k in ("id", "name", "phone", "first")))
    check("cohort 'first' is derived from full name",
          _sample and _sample.get("first") == "Old")

# ===========================================================================
# _copy_seasonal: copy function
# ===========================================================================
_biz_stub = {"name": "ABC HVAC", "review_link": ""}
_seas_copy = growth._copy_seasonal("Dave", _biz_stub, "AC tune-up")
check("_copy_seasonal contains the service name", "AC tune-up" in _seas_copy)
check("_copy_seasonal addresses lead by first name", "Dave," in _seas_copy)
check("_copy_seasonal contains business name", "ABC HVAC" in _seas_copy)
check("_copy_seasonal has no unfilled placeholders", "[" not in _seas_copy)

# No first name case
_seas_noname = growth._copy_seasonal("", _biz_stub, "AC tune-up")
check("_copy_seasonal works with no first name (no leading comma)", not _seas_noname.startswith(","))

# ===========================================================================
# recent_growth_touch_kind: DB helper
# ===========================================================================
check("recent_growth_touch_kind returns False when no touch exists",
      not db.recent_growth_touch_kind(1, "seasonal", within_days=28))

# Insert a touch and verify
_conn = db.get_conn()
_conn.execute(
    "INSERT INTO growth_touch_log (business_id, lead_id, kind, sent_at) VALUES (?,?,?,?)",
    (1, _old, "seasonal", datetime.now(timezone.utc).isoformat()))
_conn.commit()
_conn.close()

check("recent_growth_touch_kind returns True after inserting a seasonal touch",
      db.recent_growth_touch_kind(1, "seasonal", within_days=28))
check("recent_growth_touch_kind is kind-filtered (winback not affected by seasonal touch)",
      not db.recent_growth_touch_kind(1, "winback", within_days=28))
check("recent_growth_touch_kind is business-scoped (different biz not affected)",
      not db.recent_growth_touch_kind(999, "seasonal", within_days=28))

# Old touch outside window -> False
_conn = db.get_conn()
_conn.execute("DELETE FROM growth_touch_log WHERE business_id=? AND kind='seasonal'", (1,))
_conn.execute(
    "INSERT INTO growth_touch_log (business_id, lead_id, kind, sent_at) VALUES (?,?,?,?)",
    (1, _old, "seasonal", (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()))
_conn.commit()
_conn.close()

check("recent_growth_touch_kind returns False for touch older than within_days",
      not db.recent_growth_touch_kind(1, "seasonal", within_days=28))

# Clean up
clear_touch_log(1)

# ===========================================================================
# launch logic: queues N held rows for eligible cohort
# ===========================================================================
biz2 = db.create_business({"name": "Launch Test HVAC", "owner_email": "launch@x.io"})
db.set_avg_job_value(biz2, 2000)
db.update_business(biz2, {"trade": "HVAC", "review_link": "https://g.page/r/launch"})
_biz2 = db.get_business(biz2)

_l1 = db.create_lead(biz2, "Alice Customer", "+15550200001")
db.book_appointment(biz2, _l1, "old job 1", day=day_ago(120), slot_time="09:00")
_l2 = db.create_lead(biz2, "Bob Customer", "+15550200002")
db.book_appointment(biz2, _l2, "old job 2", day=day_ago(150), slot_time="10:00")
_l3 = db.create_lead(biz2, "Carol Customer", "+15550200003")
db.book_appointment(biz2, _l3, "old job 3", day=day_ago(200), slot_time="11:00")

result = simulate_launch(_biz2, "AC tune-up")
check("launch queues held rows for eligible cohort (3 customers)",
      result["queued"] == 3)
check("launch is not blocked on first run",
      not result["blocked"])
check("queued rows are status=held",
      count_held_seasonal(biz2) == 3)

# Verify body content
_conn = db.get_conn()
_rows = _conn.execute(
    "SELECT body FROM scheduled_messages WHERE business_id=? AND kind='seasonal' AND status='held'",
    (biz2,)).fetchall()
_conn.close()
check("all queued bodies contain the service name", all("AC tune-up" in r[0] for r in _rows))
check("no queued body has unfilled placeholders", all("[" not in r[0] for r in _rows))

# ===========================================================================
# frequency cap: 28-day window blocks repeat launch
# ===========================================================================
# Insert a recent seasonal touch for this business
_conn = db.get_conn()
_conn.execute(
    "INSERT INTO growth_touch_log (business_id, lead_id, kind, sent_at) VALUES (?,?,?,?)",
    (biz2, _l1, "seasonal", datetime.now(timezone.utc).isoformat()))
_conn.commit()
_conn.close()

result2 = simulate_launch(_biz2, "AC tune-up")
check("frequency cap blocks repeat launch within 28 days",
      result2["blocked"])
check("frequency cap: no new rows queued when blocked",
      count_held_seasonal(biz2) == 3)  # same as before

clear_touch_log(biz2)
clear_scheduled(biz2)

# ===========================================================================
# opt-out excluded from launch
# ===========================================================================
biz3 = db.create_business({"name": "OptOut Test HVAC", "owner_email": "optout@x.io"})
db.set_avg_job_value(biz3, 2000)
db.update_business(biz3, {"trade": "HVAC", "review_link": "https://g.page/r/opt"})
_biz3 = db.get_business(biz3)

_oa1 = db.create_lead(biz3, "Active Cust", "+15550300001")
db.book_appointment(biz3, _oa1, "job a", day=day_ago(120), slot_time="09:00")
_oa2 = db.create_lead(biz3, "OptOut Cust", "+15550300002")
db.book_appointment(biz3, _oa2, "job b", day=day_ago(150), slot_time="10:00")
db.set_opt_out(biz3, "+15550300002")

result3 = simulate_launch(_biz3, "AC tune-up")
check("opt-out excluded from launch (only 1 of 2 queued)", result3["queued"] == 1)

# Verify the opted-out phone got no row
_conn = db.get_conn()
_opted_rows = _conn.execute(
    "SELECT COUNT(*) FROM scheduled_messages s "
    "JOIN leads l ON l.id=s.lead_id "
    "WHERE s.business_id=? AND s.kind='seasonal' AND l.phone='+15550300002'",
    (biz3,)).fetchone()[0]
_conn.close()
check("opted-out phone has no queued seasonal row", _opted_rows == 0)

# ===========================================================================
# recent_growth_touch skips already-touched leads
# ===========================================================================
biz4 = db.create_business({"name": "Touch Test HVAC", "owner_email": "touch@x.io"})
db.set_avg_job_value(biz4, 2000)
db.update_business(biz4, {"trade": "HVAC", "review_link": "https://g.page/r/touch"})
_biz4 = db.get_business(biz4)

_tl1 = db.create_lead(biz4, "Touched Lead", "+15550400001")
db.book_appointment(biz4, _tl1, "old job", day=day_ago(120), slot_time="09:00")
_tl2 = db.create_lead(biz4, "Fresh Lead", "+15550400002")
db.book_appointment(biz4, _tl2, "old job 2", day=day_ago(150), slot_time="10:00")

# Simulate recent touch for _tl1
_conn = db.get_conn()
_conn.execute(
    "INSERT INTO growth_touch_log (business_id, lead_id, kind, sent_at) VALUES (?,?,?,?)",
    (biz4, _tl1, "review_request", datetime.now(timezone.utc).isoformat()))
_conn.commit()
_conn.close()

result4 = simulate_launch(_biz4, "AC tune-up")
check("recently-touched lead is skipped in seasonal launch", result4["queued"] == 1)

# ===========================================================================
# density-aware referral copy (Change 6)
# ===========================================================================
_rbz = db.create_business({"name": "Referral Dense Co", "owner_email": "rdens@x.io"})
db.set_avg_job_value(_rbz, 2000)
db.update_business(_rbz, {"review_link": "https://g.page/r/reftest"})

# Two leads with same zip, created now (within 14 days window for zip_counts)
for i in range(2):
    _rdl = db.create_lead(_rbz, f"Dense Neighbor {i}", f"+1555050100{i}")
    db.set_lead_notes(_rdl, address=f"{i+1} Main St, Springfield, IL 62704")

# One lead with recent job (wrapped yesterday) + same zip -> referral candidate
_ref_lead = db.create_lead(_rbz, "Referral Lead", "+15550501099")
db.set_lead_notes(_ref_lead, address="3 Main St, Springfield, IL 62704")
db.book_appointment(_rbz, _ref_lead, "recent job", day=day_ago(1), slot_time="10:00")

_rps = growth.plays(db.get_business(_rbz))
_rref = [p for p in _rps if p["kind"] == "referral" and p["lead_id"] == _ref_lead]
check("referral fires for the recent-job lead in dense zip", len(_rref) == 1)
check("dense referral copy contains 'busy on your block' when zip_count >= 2",
      _rref and "busy on your block" in _rref[0].get("draft_body", ""))

# Business with 0 density -> standard copy
_rbz2 = db.create_business({"name": "Referral Sparse Co", "owner_email": "rsparse@x.io"})
db.set_avg_job_value(_rbz2, 2000)
db.update_business(_rbz2, {"review_link": "https://g.page/r/reftest2"})

_sparse_ref = db.create_lead(_rbz2, "Sparse Lead", "+15550600001")
db.set_lead_notes(_sparse_ref, address="99 Lonely St, Nowhere, TX 75001")
db.book_appointment(_rbz2, _sparse_ref, "solo job", day=day_ago(2), slot_time="11:00")

_sps = growth.plays(db.get_business(_rbz2))
_sref = [p for p in _sps if p["kind"] == "referral" and p["lead_id"] == _sparse_ref]
check("referral fires for the sparse-zip lead", len(_sref) == 1)
check("sparse referral copy uses standard copy (no 'busy on your block')",
      _sref and "busy on your block" not in _sref[0].get("draft_body", ""))
check("sparse referral uses standard copy ('glad we could help')",
      _sref and "glad we could help" in _sref[0].get("draft_body", ""))

# ===========================================================================
# _copy_referral_dense and _copy_referral direct tests
# ===========================================================================
_biz_stub2 = {"name": "Test Biz", "review_link": ""}
_dense_copy = growth._copy_referral_dense("Maria", _biz_stub2)
_std_copy = growth._copy_referral("Maria", _biz_stub2)
check("_copy_referral_dense contains 'busy on your block'", "busy on your block" in _dense_copy)
check("_copy_referral_dense addresses lead by first name", "Maria," in _dense_copy)
check("_copy_referral standard copy does not contain 'busy on your block'",
      "busy on your block" not in _std_copy)
check("_copy_referral standard copy contains 'glad we could help'",
      "glad we could help" in _std_copy)

# No first name: dense copy
_dense_noname = growth._copy_referral_dense("", _biz_stub2)
check("_copy_referral_dense works without first name (no leading comma)",
      not _dense_noname.startswith(","))

# ===========================================================================
# seasonal play via plays() is sendable with correct action + seasonal_service
# ===========================================================================
import datetime as _dtmod
_seas_play = growth._seasonal_play(db.get_business(1), _dtmod.date(2026, 3, 1), 2000)
check("_seasonal_play returns sendable=True in-season",
      _seas_play is not None and _seas_play["sendable"] is True)
check("_seasonal_play action is 'launch_seasonal_campaign'",
      _seas_play is not None and _seas_play.get("action") == "launch_seasonal_campaign")
check("_seasonal_play carries seasonal_service field",
      _seas_play is not None and _seas_play.get("seasonal_service") == "AC tune-up")
check("_seasonal_play lead_id is None (cohort-level, not per-lead)",
      _seas_play is not None and _seas_play.get("lead_id") is None)

# scan() must skip the seasonal play (lead_id=None guard)
# Use a dedicated business to avoid interference from other test data.
_scan_biz_id = db.create_business({"name": "Scan Test HVAC", "owner_email": "scantest@x.io"})
db.set_avg_job_value(_scan_biz_id, 2000)
db.update_business(_scan_biz_id, {"trade": "HVAC", "review_link": "https://g.page/r/scan"})
db.set_growth_on(_scan_biz_id, 1)
db.set_growth_mode(_scan_biz_id, 'tray')
# Add an old customer so seasonal_cohort would have results
_scan_old = db.create_lead(_scan_biz_id, "Scan Old Cust", "+15550900001")
db.book_appointment(_scan_biz_id, _scan_old, "old job", day=day_ago(120), slot_time="09:00")

_scan_result = growth.scan()
_conn = db.get_conn()
# Scope to the scan test business only
_seasonal_in_queue = _conn.execute(
    "SELECT COUNT(*) FROM scheduled_messages WHERE kind='seasonal' AND business_id=?",
    (_scan_biz_id,)).fetchone()[0]
_conn.close()
check("scan() does NOT auto-queue seasonal plays (lead_id=None guard preserves cohort-only path)",
      _seasonal_in_queue == 0)

# ===========================================================================
print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass

import sys
sys.exit(1 if _fail else 0)
