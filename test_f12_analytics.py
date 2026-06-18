"""F12 ROI/analytics honesty tests. Run: python3 test_f12_analytics.py

Covers:
  - HONESTY P0: source='missed_call' filter excludes manually-added leads from
    the leads/recovered/conversion count.
  - Revenue resolved from owner avg_job_value (avg_source='owner').
  - Revenue resolved from TRADE_JOB_VALUE_DEFAULTS (avg_source='industry_default').
  - roi_multiple math is correct.
  - revenue=0 (no bookings) -> roi_multiple is None.
  - days=None all-time path works.
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
# Helpers: insert leads + appointments directly into the DB for a clean business.
# ---------------------------------------------------------------------------
def _insert_lead(business_id, source="missed_call"):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO leads (business_id, name, phone, source, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (business_id, "Test Lead", "+15550000001", source, "new", db.now_iso()))
    conn.commit()
    lid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
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


# Set up an isolated business for this test suite (avoid business 1's seeded trade).
conn = db.get_conn()
conn.execute("INSERT INTO businesses (name, trade) VALUES (?,?)",
             ("Test Plumber Co", "plumbing"))
biz_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.commit()
conn.close()


# ---------------------------------------------------------------------------
# TEST 1: source filter — manually-added lead does NOT count as a recovered call.
# ---------------------------------------------------------------------------
# Insert one missed_call lead and one manual lead (different source).
mc_lid = _insert_lead(biz_id, source="missed_call")
_insert_lead(biz_id, source="manual")       # should be excluded

result = db.analytics(biz_id, days=30)
check("missed_call filter: only the missed-call lead counts in leads total",
      result["totals"]["leads"] == 1)
check("missed_call filter: a manually-added lead is excluded from leads count",
      result["totals"]["leads"] == 1)  # same assertion, different framing


# ---------------------------------------------------------------------------
# TEST 2: revenue from owner avg_job_value (avg_source='owner').
# ---------------------------------------------------------------------------
biz_owner_conn = db.get_conn()
biz_owner_conn.execute("INSERT INTO businesses (name, trade, avg_job_value) VALUES (?,?,?)",
                        ("Owner Avg Biz", "painting", 2500.0))
biz_owner_id = biz_owner_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
biz_owner_conn.commit()
biz_owner_conn.close()

mc_lid2 = _insert_lead(biz_owner_id, source="missed_call")
_insert_appointment(biz_owner_id, mc_lid2)

r2 = db.analytics(biz_owner_id, days=30)
check("owner avg: avg_source is 'owner' when avg_job_value is set",
      r2.get("avg_source") == "owner")
check("owner avg: revenue = booked_n * owner avg_job_value",
      r2["revenue"] == 1 * 2500)
check("owner avg: totals.revenue matches top-level revenue key",
      r2["totals"]["revenue"] == r2["revenue"])


# ---------------------------------------------------------------------------
# TEST 3: revenue from TRADE_JOB_VALUE_DEFAULTS (avg_source='industry_default').
# ---------------------------------------------------------------------------
biz_trade_conn = db.get_conn()
biz_trade_conn.execute("INSERT INTO businesses (name, trade) VALUES (?,?)",
                        ("Plumbing Inc", "plumbing"))
biz_trade_id = biz_trade_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
biz_trade_conn.commit()
biz_trade_conn.close()

mc_lid3 = _insert_lead(biz_trade_id, source="missed_call")
_insert_appointment(biz_trade_id, mc_lid3)

r3 = db.analytics(biz_trade_id, days=30)
check("trade default: avg_source is 'industry_default' when no owner avg set",
      r3.get("avg_source") == "industry_default")
# plumbing default = 1800 from TRADE_JOB_VALUE_DEFAULTS
expected_revenue_trade = 1 * config.TRADE_JOB_VALUE_DEFAULTS["plumbing"]
check("trade default: revenue = booked_n * TRADE_JOB_VALUE_DEFAULTS['plumbing']",
      r3["revenue"] == expected_revenue_trade)


# ---------------------------------------------------------------------------
# TEST 4: roi_multiple math.
# ---------------------------------------------------------------------------
# roi_multiple = round(revenue / PLAN_COST_MONTHLY, 1)
expected_multiple = round(expected_revenue_trade / config.PLAN_COST_MONTHLY, 1)
check("roi_multiple: math is round(revenue / PLAN_COST_MONTHLY, 1)",
      r3.get("roi_multiple") == expected_multiple)


# ---------------------------------------------------------------------------
# TEST 5: revenue = 0 (no bookings) -> roi_multiple is None.
# ---------------------------------------------------------------------------
biz_no_book_conn = db.get_conn()
biz_no_book_conn.execute("INSERT INTO businesses (name, trade) VALUES (?,?)",
                          ("No Booking Biz", "plumbing"))
biz_no_book_id = biz_no_book_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
biz_no_book_conn.commit()
biz_no_book_conn.close()

# No leads, no appointments -> revenue = 0 -> roi_multiple = None
r5 = db.analytics(biz_no_book_id, days=30)
check("no bookings: revenue is 0",
      r5["revenue"] == 0)
check("no bookings: roi_multiple is None when revenue is 0",
      r5.get("roi_multiple") is None)


# ---------------------------------------------------------------------------
# TEST 6: days=None all-time path.
# ---------------------------------------------------------------------------
# Use the plumbing business (biz_trade_id) which has 1 missed-call lead + 1 booking.
r6 = db.analytics(biz_trade_id, days=None)
check("days=None: returns totals for all time (leads >= 1)",
      r6["totals"]["leads"] >= 1)
check("days=None: revenue is present (not None)",
      r6["revenue"] is not None)
check("days=None: avg_source is present",
      r6.get("avg_source") in ("owner", "industry_default"))
check("days=None: days key is None",
      r6["days"] is None)


# ---------------------------------------------------------------------------
# TEST 7: unknown trade falls back to $800 floor.
# ---------------------------------------------------------------------------
biz_unknown_conn = db.get_conn()
biz_unknown_conn.execute("INSERT INTO businesses (name, trade) VALUES (?,?)",
                          ("Weird Trade Biz", "underwater basket weaving"))
biz_unknown_id = biz_unknown_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
biz_unknown_conn.commit()
biz_unknown_conn.close()

mc_lid7 = _insert_lead(biz_unknown_id, source="missed_call")
_insert_appointment(biz_unknown_id, mc_lid7)

r7 = db.analytics(biz_unknown_id, days=30)
check("unknown trade: falls back to $800 floor",
      r7["revenue"] == 800)
check("unknown trade: avg_source is 'industry_default'",
      r7.get("avg_source") == "industry_default")


print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
