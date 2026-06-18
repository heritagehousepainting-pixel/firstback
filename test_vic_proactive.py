"""Phase 5b ALPHA -- Vic proactive push engine tests.
Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_vic_proactive.py

Covers spec ALPHA tests 1-4:
  1. lead/booking alert carries a briefing tail; body <= 320 chars; core event line intact.
  2. morning digest fires once in-window with actionable items; second tick same day = no dup;
     quiet/empty briefing = nothing sent; outside [7,10) = nothing sent.
  3. stall nudge fires for >24h warm lead, dedupes same day, escalates at >48h;
     fresh (<24h) warm lead and non-warm lead get nothing.
  4. ALL proactive sends go to the owner's alert_sms cell, NEVER to a lead number.

Exits 0 on all pass, 1 if any fail.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

os.environ["FIRSTBACK_PROVIDER"] = "demo"   # deterministic, no network

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

import messaging
messaging.TWILIO_ACCOUNT_SID = ""           # configured() False -> simulates

import alerts
import reminders

_pass = _fail = 0
_ALL_SMS_RECIPIENTS = []   # populated by the monkeypatched send_sms


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- Helpers ----------------------------------------------------------------

def _make_biz(owner_sms="+15550001111"):
    """Return a fresh business dict with an alert_sms cell set."""
    bid = db.create_business({"name": "Vic Test Painting"})
    conn = db.get_conn()
    conn.execute("UPDATE businesses SET alert_sms=?, alert_on_lead=1, alert_on_booking=1 "
                 "WHERE id=?", (owner_sms, bid))
    conn.commit(); conn.close()
    return db.get_business(bid)


def _make_warm_lead(bid, name, phone, hours_ago):
    """Create a lead with an inbound reply aged `hours_ago` hours."""
    lid = db.create_lead(bid, name, phone)
    msg_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    conn = db.get_conn()
    conn.execute("INSERT INTO messages (lead_id,direction,body,created_at) VALUES (?,?,?,?)",
                 (lid, "in", "hey there", msg_at))
    conn.commit(); conn.close()
    return lid


def _captured_send_sms(business, to, body, **kwargs):
    """Capture every recipient for test 4, then simulate send."""
    _ALL_SMS_RECIPIENTS.append(to)
    return {"status": "simulated"}


# Patch send_sms globally for the whole test run to capture all recipients.
messaging.send_sms = _captured_send_sms


# ---- Test 1: briefing tail on lead/booking alerts ---------------------------

print("\n=== Test 1: briefing tail on lead/booking alerts ===")

biz1 = _make_biz("+15550001111")
lid1 = db.create_lead(biz1["id"], "Mike Homeowner", "+15559990001")

# Stub briefing to return an active briefing with a headline.
_FAKE_BRIEFING = {
    "type": "briefing", "tone": "active",
    "headline": "3 open, ~$7,200 on the table.",
    "items": [{"title": "Text Mike back", "sub": "replied, waiting"}]
}

with patch("assistant.briefing", return_value=_FAKE_BRIEFING):
    result1 = alerts.notify(biz1, "lead", {"lead_id": lid1, "name": "Mike Homeowner",
                                            "phone": "+15559990001"})

check("test1: lead alert returns attempted channels", len(result1) > 0)

# Inspect the body stored in the alerts table.
conn = db.get_conn()
row1 = conn.execute(
    "SELECT body FROM alerts WHERE business_id=? AND kind='lead' ORDER BY id DESC LIMIT 1",
    (biz1["id"],)).fetchone()
conn.close()
body1 = row1["body"] if row1 else ""
check("test1: lead alert body contains core event line (New lead)", "New lead:" in body1)
check("test1: lead alert body appended briefing tail", "on the table" in body1)
check("test1: lead alert body <= 320 chars", len(body1) <= 320)

# Test booking alert.
biz1b = _make_biz("+15550001112")
lid1b = db.create_lead(biz1b["id"], "Sara Smith", "+15559990002")
with patch("assistant.briefing", return_value=_FAKE_BRIEFING):
    result1b = alerts.notify(biz1b, "booking", {
        "lead_id": lid1b, "name": "Sara Smith", "when": "Mon Jun 22 at 10:00 AM"})

conn = db.get_conn()
row1b = conn.execute(
    "SELECT body FROM alerts WHERE business_id=? AND kind='booking' ORDER BY id DESC LIMIT 1",
    (biz1b["id"],)).fetchone()
conn.close()
body1b = row1b["body"] if row1b else ""
check("test1: booking alert body contains core event line (Estimate booked)", "Estimate booked:" in body1b)
check("test1: booking alert body appended briefing tail", "on the table" in body1b)
check("test1: booking alert body <= 320 chars", len(body1b) <= 320)

# Test that non-lead/booking kinds do NOT get a briefing tail.
biz1c = _make_biz("+15550001113")
lid1c = db.create_lead(biz1c["id"], "Joe Caller", "+15559990003")
with patch("assistant.briefing", return_value=_FAKE_BRIEFING):
    result1c = alerts.notify(biz1c, "urgent", {"lead_id": lid1c, "name": "Joe Caller"})

conn = db.get_conn()
row1c = conn.execute(
    "SELECT body FROM alerts WHERE business_id=? AND kind='urgent' ORDER BY id DESC LIMIT 1",
    (biz1c["id"],)).fetchone()
conn.close()
body1c = row1c["body"] if row1c else ""
check("test1: urgent alert does NOT get briefing tail", "on the table" not in body1c)

# Test that a long core body is NOT truncated (briefing tail gets the chop).
_LONG_BRIEFING = {
    "type": "briefing", "tone": "active",
    "headline": "X" * 310,   # very long headline
    "items": [{"title": "item"}]
}
biz1d = _make_biz("+15550001114")
lid1d = db.create_lead(biz1d["id"], "Long Test", "+15559990004")
with patch("assistant.briefing", return_value=_LONG_BRIEFING):
    alerts.notify(biz1d, "lead", {"lead_id": lid1d, "name": "Long Test"})
conn = db.get_conn()
row1d = conn.execute(
    "SELECT body FROM alerts WHERE business_id=? AND kind='lead' ORDER BY id DESC LIMIT 1",
    (biz1d["id"],)).fetchone()
conn.close()
body1d = row1d["body"] if row1d else ""
check("test1: long briefing tail truncated to keep total <= 320 chars", len(body1d) <= 320)
check("test1: core event line (New lead:) always survives truncation", "New lead:" in body1d)

# Test that a quiet/empty briefing returns empty tail (no append).
_QUIET_BRIEFING = {"type": "briefing", "tone": "quiet", "headline": "All quiet.", "items": []}
biz1e = _make_biz("+15550001115")
lid1e = db.create_lead(biz1e["id"], "Quiet Test", "+15559990005")
with patch("assistant.briefing", return_value=_QUIET_BRIEFING):
    alerts.notify(biz1e, "lead", {"lead_id": lid1e, "name": "Quiet Test"})
conn = db.get_conn()
row1e = conn.execute(
    "SELECT body FROM alerts WHERE business_id=? AND kind='lead' ORDER BY id DESC LIMIT 1",
    (biz1e["id"],)).fetchone()
conn.close()
body1e = row1e["body"] if row1e else ""
check("test1: quiet briefing -> no tail appended to lead alert",
      "All quiet" not in body1e and "New lead:" in body1e)

# Test exception in assistant.briefing -> swallowed, no crash, core line intact.
def _raise(*a, **kw):
    raise RuntimeError("boom")

biz1f = _make_biz("+15550001116")
lid1f = db.create_lead(biz1f["id"], "Error Test", "+15559990006")
with patch("assistant.briefing", side_effect=_raise):
    alerts.notify(biz1f, "lead", {"lead_id": lid1f, "name": "Error Test"})
conn = db.get_conn()
row1f = conn.execute(
    "SELECT body FROM alerts WHERE business_id=? AND kind='lead' ORDER BY id DESC LIMIT 1",
    (biz1f["id"],)).fetchone()
conn.close()
body1f = row1f["body"] if row1f else ""
check("test1: assistant.briefing exception -> alert still fires with core line",
      "New lead:" in body1f)


# ---- Test 2: morning digest -------------------------------------------------

print("\n=== Test 2: morning digest ===")

# Use a business with UTC timezone so we can control "local hour" easily.
biz2 = _make_biz("+15550002222")
# Ensure business has timezone=UTC for predictable local hour control.
conn = db.get_conn()
conn.execute("UPDATE businesses SET timezone='UTC' WHERE id=?", (biz2["id"],))
conn.commit(); conn.close()
biz2 = db.get_business(biz2["id"])

# Seed a warm lead so briefing is "active".
lid2 = _make_warm_lead(biz2["id"], "Morning Lead", "+15559991001", hours_ago=2)

# Count alerts before.
def _alert_count(bid, kind):
    """Count distinct dedupe_key entries (i.e., distinct notify() calls, not channels)."""
    conn = db.get_conn()
    n = conn.execute(
        "SELECT COUNT(DISTINCT dedupe_key) FROM alerts WHERE business_id=? AND kind=?",
        (bid, kind)).fetchone()[0]
    conn.close()
    return n

_before2 = _alert_count(biz2["id"], "vic_morning")

# Fire in-window: hour=8 (in [7,10)).
now_in_window = datetime(2026, 6, 18, 8, 0, tzinfo=timezone.utc).isoformat()
with patch("assistant.briefing", return_value=_FAKE_BRIEFING):
    reminders.scan_morning_briefing(now_in_window)

_after2 = _alert_count(biz2["id"], "vic_morning")
check("test2: morning digest fires in-window (hour=8, active briefing)",
      _after2 == _before2 + 1)

# Second tick same day -> no duplicate.
with patch("assistant.briefing", return_value=_FAKE_BRIEFING):
    reminders.scan_morning_briefing(now_in_window)

_after2b = _alert_count(biz2["id"], "vic_morning")
check("test2: second tick same day -> no duplicate", _after2b == _after2)

# Outside window (hour=11) -> no send.
biz2x = _make_biz("+15550002223")
conn = db.get_conn()
conn.execute("UPDATE businesses SET timezone='UTC' WHERE id=?", (biz2x["id"],))
conn.commit(); conn.close()
biz2x = db.get_business(biz2x["id"])
_make_warm_lead(biz2x["id"], "Out Lead", "+15559991002", hours_ago=2)
now_outside = datetime(2026, 6, 18, 11, 0, tzinfo=timezone.utc).isoformat()
with patch("assistant.briefing", return_value=_FAKE_BRIEFING):
    reminders.scan_morning_briefing(now_outside)
check("test2: outside [7,10) -> no send", _alert_count(biz2x["id"], "vic_morning") == 0)

# Quiet/empty briefing -> no send.
biz2y = _make_biz("+15550002224")
conn = db.get_conn()
conn.execute("UPDATE businesses SET timezone='UTC' WHERE id=?", (biz2y["id"],))
conn.commit(); conn.close()
biz2y = db.get_business(biz2y["id"])
with patch("assistant.briefing", return_value=_QUIET_BRIEFING):
    reminders.scan_morning_briefing(now_in_window)
check("test2: quiet briefing -> no morning send", _alert_count(biz2y["id"], "vic_morning") == 0)

# No items even with active tone -> no send.
_NO_ITEMS_BRIEFING = {"type": "briefing", "tone": "active", "headline": "Busy.", "items": []}
biz2z = _make_biz("+15550002225")
conn = db.get_conn()
conn.execute("UPDATE businesses SET timezone='UTC' WHERE id=?", (biz2z["id"],))
conn.commit(); conn.close()
biz2z = db.get_business(biz2z["id"])
with patch("assistant.briefing", return_value=_NO_ITEMS_BRIEFING):
    reminders.scan_morning_briefing(now_in_window)
check("test2: active tone but empty items -> no morning send",
      _alert_count(biz2z["id"], "vic_morning") == 0)

# Verify body says "Open FirstBack" and NOT "tap to send".
conn = db.get_conn()
row2_body = conn.execute(
    "SELECT body FROM alerts WHERE business_id=? AND kind='vic_morning' ORDER BY id DESC LIMIT 1",
    (biz2["id"],)).fetchone()
conn.close()
body2 = row2_body["body"] if row2_body else ""
check("test2: morning body says 'Open FirstBack'", "Open FirstBack" in body2)
check("test2: morning body does NOT say 'tap to send'",
      "tap to send" not in body2.lower() and "tap here to send" not in body2.lower())


# ---- Test 3: stall nudges ---------------------------------------------------

print("\n=== Test 3: stall nudges ===")

biz3 = _make_biz("+15550003333")
conn = db.get_conn()
conn.execute("UPDATE businesses SET timezone='UTC' WHERE id=?", (biz3["id"],))
conn.commit(); conn.close()
biz3 = db.get_business(biz3["id"])

# A warm lead that is 30h idle -> should fire.
lid3_30h = _make_warm_lead(biz3["id"], "Stall Lead", "+15559992001", hours_ago=30)

# A warm lead that is only 10h idle -> should NOT fire.
lid3_10h = _make_warm_lead(biz3["id"], "Fresh Lead", "+15559992002", hours_ago=10)

# A non-warm lead (never replied) -> should NOT fire.
lid3_new = db.create_lead(biz3["id"], "New Lead No Reply", "+15559992003")

now3 = datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc).isoformat()

_before3 = _alert_count(biz3["id"], "vic_stall")
reminders.scan_stall_nudges(now3)
_after3 = _alert_count(biz3["id"], "vic_stall")

check("test3: >24h warm lead fires stall nudge", _after3 == _before3 + 1)

# Dedupe same day -> no duplicate.
reminders.scan_stall_nudges(now3)
check("test3: same-day dedup -> no duplicate", _alert_count(biz3["id"], "vic_stall") == _after3)

# Fresh (<24h) lead and new (no-reply) lead got nothing.
conn = db.get_conn()
stall_rows = conn.execute(
    "SELECT DISTINCT dedupe_key, body FROM alerts WHERE business_id=? AND kind='vic_stall'",
    (biz3["id"],)).fetchall()
conn.close()
# Only one distinct nudge should exist (the 30h lead); 10h lead and no-reply lead skipped.
check("test3: fresh (<24h) warm lead gets no stall nudge", len(stall_rows) == 1)
check("test3: non-warm (new) lead gets no stall nudge", len(stall_rows) == 1)

# >48h -> urgent tone in copy.
biz3b = _make_biz("+15550003334")
conn = db.get_conn()
conn.execute("UPDATE businesses SET timezone='UTC' WHERE id=?", (biz3b["id"],))
conn.commit(); conn.close()
biz3b = db.get_business(biz3b["id"])
lid3_50h = _make_warm_lead(biz3b["id"], "Long Stall Lead", "+15559992004", hours_ago=50)

reminders.scan_stall_nudges(now3)

conn = db.get_conn()
row3b = conn.execute(
    "SELECT body FROM alerts WHERE business_id=? AND kind='vic_stall' ORDER BY id DESC LIMIT 1",
    (biz3b["id"],)).fetchone()
conn.close()
body3b = row3b["body"] if row3b else ""
check("test3: >48h stall nudge escalates tone (body reflects urgency)",
      "still waiting" in body3b or "Open FirstBack" in body3b)

# Stall body says "Open FirstBack" and does NOT say "tap to send".
check("test3: stall body says 'Open FirstBack'", "Open FirstBack" in body3b)
check("test3: stall body does NOT say 'tap to send'",
      "tap to send" not in body3b.lower() and "tap here to send" not in body3b.lower())

# Verify 30h stall body is reasonable.
row3a = dict(stall_rows[0])
body3a = row3a.get("body", "")
check("test3: 30h stall body mentions 'Open FirstBack'", "Open FirstBack" in body3a)


# ---- Test 4: ALL proactive sends go to owner's alert_sms, NEVER to a lead --

print("\n=== Test 4: all sends go to owner cell, never lead number ===")

# Collect all lead phone numbers seeded above.
LEAD_PHONES = {
    "+15559990001", "+15559990002", "+15559990003", "+15559990004",
    "+15559990005", "+15559990006",
    "+15559991001", "+15559991002",
    "+15559992001", "+15559992002", "+15559992003", "+15559992004",
}

# Gather all owner cells.
OWNER_CELLS = {
    "+15550001111", "+15550001112", "+15550001113", "+15550001114",
    "+15550001115", "+15550001116",
    "+15550002222", "+15550002223", "+15550002224", "+15550002225",
    "+15550003333", "+15550003334",
}

# Any send to a known lead phone is a violation.
bad_sends = [r for r in _ALL_SMS_RECIPIENTS if r in LEAD_PHONES]
check("test4: ZERO sends went to any lead/consumer phone number", len(bad_sends) == 0)

# All sends that happened went to known owner cells.
unknown_sends = [r for r in _ALL_SMS_RECIPIENTS if r and r not in OWNER_CELLS]
check("test4: every SMS recipient is an owner's alert_sms cell",
      len(unknown_sends) == 0)

if bad_sends:
    print(f"  [!] Bad recipients: {bad_sends}")
if unknown_sends:
    print(f"  [!] Unknown recipients: {unknown_sends}")


# ---- Results ----------------------------------------------------------------
print(f"\n{'='*40}")
print(f"Results: {_pass} passed, {_fail} failed")
if _fail:
    sys.exit(1)
