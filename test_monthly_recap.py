"""Plan-06 Change-3 + Plan-08 fold-in: Monthly ROI recap SMS with screening section.
Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_monthly_recap.py

Proves:
  A. scan_monthly_recap fires on day 28 with bookings + A2P ready.
  B. Skips when the meta key is already set (already sent this month).
  C. Skips when A2P is pending.
  D. Skips when zero bookings in the last 30 days.
  E. format_message('monthly_recap') is <=320 chars, contains estimate label.
  F. screening_section appears in the message when screening stats are present.
  G. No second SMS after dedupe (meta gate).

Exits 0 on all pass, 1 if any fail.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

os.environ["FIRSTBACK_PROVIDER"] = "demo"   # deterministic, no network

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

import messaging
messaging.TWILIO_ACCOUNT_SID = ""    # configured() False -> simulates

import alerts
import reminders
import compliance

_APP_TZ = config.app_tz()
_pass = _fail = 0
_SENT = []   # (to, body) captures


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


def _captured_send(business, to, body, **kwargs):
    _SENT.append((to, body))
    return {"status": "simulated"}


messaging.send_sms = _captured_send


def _iso_at_day_hour(day_of_month, hour):
    """ISO UTC string whose _APP_TZ-local representation is at the given
    day-of-month and hour in the current month."""
    now_local = datetime.now(_APP_TZ)
    try:
        local = now_local.replace(day=day_of_month, hour=hour,
                                   minute=0, second=0, microsecond=0)
    except ValueError:
        # Day doesn't exist in this month (e.g. day=31 in June). Use last valid day.
        import calendar
        last_day = calendar.monthrange(now_local.year, now_local.month)[1]
        local = now_local.replace(day=min(day_of_month, last_day), hour=hour,
                                   minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).isoformat()


def _make_biz(owner_sms, a2p_status="approved", avg_job_value=None):
    """Create a business with the given A2P status and owner SMS."""
    bid = db.create_business({"name": "Monthly Recap Test Co"})
    conn = db.get_conn()
    conn.execute(
        "UPDATE businesses SET alert_sms=?, alert_on_daily_digest=1, a2p_status=? "
        "WHERE id=?",
        (owner_sms, a2p_status, bid))
    if avg_job_value is not None:
        conn.execute("UPDATE businesses SET avg_job_value=? WHERE id=?",
                     (avg_job_value, bid))
    conn.commit()
    conn.close()
    return db.get_business(bid)


def _add_booking(biz, days_ago=5):
    """Insert a lead + booked appointment to satisfy the booked>=1 gate."""
    lid = db.create_lead(biz["id"], "Test Customer", "+15551234567",
                         source="missed_call")
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn = db.get_conn()
    # Update the lead's created_at so analytics() includes it in the 30-day window
    conn.execute("UPDATE leads SET created_at=? WHERE id=?", (ts, lid))
    # Insert a booked appointment
    conn.execute(
        "INSERT INTO appointments (business_id, lead_id, day, slot_time, status, created_at) "
        "VALUES (?, ?, ?, ?, 'booked', ?)",
        (biz["id"], lid, "2026-06-10", "14:00", ts))
    conn.commit()
    conn.close()
    return lid


def _add_screened_call(biz, screen_status="screened_spam", screen_mode="enforce"):
    """Insert a missed call with a given screen verdict for testing the screening section."""
    ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO calls (business_id, from_number, missed, screen_status, screen_mode, "
        "created_at) VALUES (?, ?, 1, ?, ?, ?)",
        (biz["id"], "+15559990001", screen_status, screen_mode, ts))
    conn.commit()
    conn.close()


# ===========================================================================
# A. Fires on day 28 with bookings + A2P ready
# ===========================================================================
print("\n=== A: fires on day 28 with bookings + A2P ===")
_SENT.clear()
bizA = _make_biz("+15550030001", a2p_status="approved", avg_job_value=3000)
_add_booking(bizA)

firedA = reminders.scan_monthly_recap(_iso_at_day_hour(28, 8))
own_A = [b for (to, b) in _SENT if to == bizA["alert_sms"]]
check("A: fires on day 28 at 8am", firedA >= 1)
check("A: exactly one SMS to owner cell", len(own_A) == 1)
if own_A:
    msg = own_A[0]
    check("A: body mentions 'rescued'", "rescued" in msg)
    check("A: body mentions 'booked'", "booked" in msg)
    check("A: body contains 'Reply STATS'", "Reply STATS" in msg)
    check("A: body <= 320 chars", len(msg) <= 320)

# ===========================================================================
# B. Skips when meta key is already set (already sent this month)
# ===========================================================================
print("\n=== B: skips when already sent (meta set) ===")
_SENT.clear()
bizB = _make_biz("+15550030002", a2p_status="approved", avg_job_value=2500)
_add_booking(bizB)
# Pre-set the meta key as if already sent
ym = datetime.now(_APP_TZ).strftime("%Y-%m")
db.set_meta(f"monthly_recap:{bizB['id']}:{ym}", datetime.now(timezone.utc).isoformat())

firedB = reminders.scan_monthly_recap(_iso_at_day_hour(28, 8))
own_B = [b for (to, b) in _SENT if to == bizB["alert_sms"]]
check("B: skips when meta already set", len(own_B) == 0)
check("B: fired count 0 for already-sent biz", firedB == 0)

# ===========================================================================
# C. Skips when A2P is pending
# ===========================================================================
print("\n=== C: skips when A2P pending ===")
_SENT.clear()
bizC = _make_biz("+15550030003", a2p_status="pending")
_add_booking(bizC)

firedC = reminders.scan_monthly_recap(_iso_at_day_hour(28, 8))
own_C = [b for (to, b) in _SENT if to == bizC["alert_sms"]]
check("C: skips when A2P pending (not approved)", len(own_C) == 0)

# ===========================================================================
# D. Skips when zero bookings
# ===========================================================================
print("\n=== D: skips when zero bookings ===")
_SENT.clear()
bizD = _make_biz("+15550030004", a2p_status="approved")
# No bookings added for bizD

firedD = reminders.scan_monthly_recap(_iso_at_day_hour(28, 8))
own_D = [b for (to, b) in _SENT if to == bizD["alert_sms"]]
check("D: skips when zero bookings", len(own_D) == 0)

# ===========================================================================
# E. format_message('monthly_recap') <= 320 chars, estimate label present
# ===========================================================================
print("\n=== E: format_message honesty + char cap ===")
# With industry_default avg_source -> "(estimated)"
msg_e1 = alerts.format_message("monthly_recap", {
    "month": "2026-06",
    "leads": 12,
    "booked": 4,
    "revenue": 9600,
    "multiple": 9.6,
    "avg_source": "industry_default",
    "screening_section": "",
})
check("E: estimated label when avg_source=industry_default",
      "(estimated)" in msg_e1)
check("E: contains 'recovered'", "recovered" in msg_e1)
check("E: body <= 320 chars (industry_default)", len(msg_e1) <= 320)
check("E: multiple shown when present", "9.6x" in msg_e1)

# With owner avg_source -> "(based on your job value)"
msg_e2 = alerts.format_message("monthly_recap", {
    "month": "2026-06",
    "leads": 5,
    "booked": 2,
    "revenue": 6000,
    "multiple": 6.0,
    "avg_source": "owner",
    "screening_section": "",
})
check("E: 'based on your job value' when avg_source=owner",
      "(based on your job value)" in msg_e2)
check("E: body <= 320 chars (owner)", len(msg_e2) <= 320)

# Without multiple
msg_e3 = alerts.format_message("monthly_recap", {
    "month": "2026-06",
    "leads": 3,
    "booked": 1,
    "revenue": 800,
    "multiple": None,
    "avg_source": "industry_default",
    "screening_section": "",
})
check("E: no multiple line when multiple is None", "about" not in msg_e3)
check("E: ends with 'Reply STATS'", "Reply STATS" in msg_e3)
check("E: body <= 320 chars (no multiple)", len(msg_e3) <= 320)

# ===========================================================================
# F. screening_section appears when screening stats are present
# ===========================================================================
print("\n=== F: screening_section included when robo stats present ===")
_SENT.clear()
bizF = _make_biz("+15550030005", a2p_status="approved", avg_job_value=2000)
_add_booking(bizF)
_add_screened_call(bizF, screen_status="screened_spam", screen_mode="enforce")

firedF = reminders.scan_monthly_recap(_iso_at_day_hour(28, 8))
own_F = [b for (to, b) in _SENT if to == bizF["alert_sms"]]
check("F: fires (screening stats present)", firedF >= 1)
if own_F:
    msg_f = own_F[0]
    check("F: body contains 'screened'", "screened" in msg_f.lower())
    check("F: body <= 320 chars with screening section", len(msg_f) <= 320)

# Verify format_message directly with a screening_section
msg_f2 = alerts.format_message("monthly_recap", {
    "month": "2026-06",
    "leads": 8,
    "booked": 3,
    "revenue": 6000,
    "multiple": 6.0,
    "avg_source": "industry_default",
    "screening_section": "Plus 5 robocalls screened.",
})
check("F: direct format includes screening_section text",
      "Plus 5 robocalls screened." in msg_f2)
check("F: direct format <= 320 with screening_section", len(msg_f2) <= 320)

# No screening_section when stats are absent/zero (falsy -> omitted)
msg_f3 = alerts.format_message("monthly_recap", {
    "month": "2026-06",
    "leads": 2,
    "booked": 1,
    "revenue": 2000,
    "multiple": None,
    "avg_source": "industry_default",
    "screening_section": "",
})
check("F: no screening text when screening_section is empty",
      "screened" not in msg_f3.lower())

# ===========================================================================
# G. Second tick same month -> 0 (meta gate after first send)
# ===========================================================================
print("\n=== G: second tick same month fires 0 (meta gate) ===")
_SENT.clear()
bizG = _make_biz("+15550030006", a2p_status="approved", avg_job_value=1500)
_add_booking(bizG)

fired_G1 = reminders.scan_monthly_recap(_iso_at_day_hour(28, 8))
count_G1 = len([b for (to, b) in _SENT if to == bizG["alert_sms"]])
check("G: first tick fires", fired_G1 >= 1 and count_G1 == 1)

# Second tick same month
fired_G2 = reminders.scan_monthly_recap(_iso_at_day_hour(29, 8))
count_G2 = len([b for (to, b) in _SENT if to == bizG["alert_sms"]])
check("G: second tick same month fires 0", fired_G2 == 0)
check("G: still only one SMS total after second tick", count_G2 == 1)

# ===========================================================================
# Results
# ===========================================================================
print(f"\n{'='*44}")
print(f"Results: {_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
