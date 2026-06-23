"""Phase 5d ALPHA: growth tray DB + engine tests.
Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_growth_tray.py

Covers all 19 ALPHA tests from PHASE5D-SPEC.md section 6.
Standalone: real temp DB, no network.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone, date

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
import messaging
messaging.TWILIO_ACCOUNT_SID = ""   # simulate; no network
import app   # noqa: F401  # runs migrations
import growth

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1; print(f"  ok   {name}")
    else:
        _fail += 1; print(f"FAIL   {name}")


def iso_ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def day_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()


def clear_scheduled(business_id):
    c = db.get_conn()
    c.execute("DELETE FROM scheduled_messages WHERE business_id=?", (business_id,))
    c.commit(); c.close()


def clear_growth_logs(business_id):
    c = db.get_conn()
    c.execute("DELETE FROM growth_touch_log WHERE business_id=?", (business_id,))
    c.execute("DELETE FROM growth_approvals WHERE business_id=?", (business_id,))
    c.commit(); c.close()


def make_biz(name="TestBiz", trade="painting", review_link="https://g.page/r/x", avg_job=2500):
    c = db.get_conn()
    cur = c.execute(
        "INSERT INTO businesses (name, trade, avg_job_value, review_link) VALUES (?,?,?,?)",
        (name, trade, avg_job, review_link))
    c.commit()
    bid = cur.lastrowid
    c.close()
    return db.get_business(bid)


# ============================================================
# Set up biz 1 baseline
# ============================================================
db.update_business(1, {"trade": "painting", "review_link": "https://g.page/r/testlink"})
db.set_avg_job_value(1, 2500)

# ============================================================
# TEST #1: set_growth_mode / growth_mode round-trip
# ============================================================
print("\n--- Test 1: set_growth_mode / growth_mode ---")
db.set_growth_mode(1, "tray")
check("#1a growth_mode returns 'tray' after set", db.growth_mode(1) == "tray")
db.set_growth_mode(1, "auto")
check("#1b growth_mode returns 'auto' after set", db.growth_mode(1) == "auto")
db.set_growth_mode(1, "INVALID")
check("#1c invalid mode -> 'off'", db.growth_mode(1) == "off")

# ============================================================
# TEST #2: backfill growth_on=1 -> growth_mode='tray'
# ============================================================
print("\n--- Test 2: backfill ---")
c = db.get_conn()
cur = c.execute(
    "INSERT INTO businesses (name, trade, growth_on, growth_mode) VALUES (?,?,1,'off')",
    ("BackfillBiz", "HVAC"))
c.commit()
bf_bid = cur.lastrowid
c.close()
# Run the same backfill SQL that init_db runs
c = db.get_conn()
c.execute("UPDATE businesses SET growth_mode='tray' WHERE growth_on=1 AND growth_mode='off'")
c.commit(); c.close()
bf_biz = db.get_business(bf_bid)
check("#2 growth_on=1 row gets growth_mode='tray' after backfill",
      bf_biz.get("growth_mode") == "tray")

# ============================================================
# TEST #3: scan() tray -> 'held'
# ============================================================
print("\n--- Test 3: scan() tray -> held ---")
lead3 = db.create_lead(1, "Tray Person", "+15550000301")
db.book_appointment(1, lead3, "job done", day=day_ago(5), slot_time="10:00")
db.add_message(lead3, "in", "Thanks!")
db.set_growth_mode(1, "tray")
clear_scheduled(1)
growth.scan()
c = db.get_conn()
rows3 = c.execute(
    "SELECT status FROM scheduled_messages WHERE business_id=1 "
    "AND kind NOT IN ('reminder','followup')"
).fetchall()
c.close()
statuses3 = [r[0] for r in rows3]
check("#3a scan(tray) queued at least one row", len(statuses3) >= 1)
check("#3b all rows status='held'", len(statuses3) >= 1 and all(s == "held" for s in statuses3))

# ============================================================
# TEST #4: scan() off -> nothing queued
# ============================================================
print("\n--- Test 4: scan() off ---")
db.set_growth_mode(1, "off")
clear_scheduled(1)
growth.scan()
c = db.get_conn()
rows4 = c.execute(
    "SELECT id FROM scheduled_messages WHERE business_id=1 "
    "AND kind NOT IN ('reminder','followup')"
).fetchall()
c.close()
check("#4 scan(off) queues nothing", len(rows4) == 0)

# ============================================================
# TEST #5: scan() auto -> review_request='pending', winback='held'
# ============================================================
print("\n--- Test 5: scan() auto branching ---")
biz5 = make_biz(name="AutoBiz", trade="painting", review_link="https://g.page/r/auto5")
bid5 = biz5["id"]
db.set_avg_job_value(bid5, 2500)

# review_request play
lead5r = db.create_lead(bid5, "Auto Review", "+15550000501")
db.book_appointment(bid5, lead5r, "3d ago", day=day_ago(3), slot_time="10:00")
db.add_message(lead5r, "in", "Thanks!")

# winback play: job 13 months ago + inbound msg
winback_day = (date.today().replace(year=date.today().year - 1) - timedelta(days=30)).isoformat()
lead5w = db.create_lead(bid5, "Auto Winback", "+15550000502")
db.book_appointment(bid5, lead5w, "old job", day=winback_day, slot_time="10:00")
db.add_message(lead5w, "in", "Good job last year!")

db.set_growth_mode(bid5, "auto")
clear_scheduled(bid5)
growth.scan()
c = db.get_conn()
rows5 = c.execute(
    "SELECT kind, status FROM scheduled_messages WHERE business_id=? "
    "AND kind NOT IN ('reminder','followup')", (bid5,)
).fetchall()
c.close()
d5 = {r[0]: r[1] for r in rows5}
check("#5a auto: review_request -> 'pending'", d5.get("review_request") == "pending")
check("#5b auto: winback -> 'held'", d5.get("winback") == "held")

# ============================================================
# TEST #6: due_scheduled_messages EXCLUDES 'held'
# ============================================================
print("\n--- Test 6: due_scheduled_messages excludes 'held' ---")
lead6 = db.create_lead(1, "Held Lead", "+15550000601")
c = db.get_conn()
past6 = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
c.execute(
    "INSERT INTO scheduled_messages"
    " (business_id, lead_id, kind, send_at, body, status, created_at)"
    " VALUES (?,?,'review_request_held_test',?,'test','held',?)",
    (1, lead6, past6, db.now_iso()))
c.commit(); c.close()
due6 = db.due_scheduled_messages(db.now_iso())
in_due6 = [r for r in due6 if r.get("kind") == "review_request_held_test"]
check("#6 due_scheduled_messages EXCLUDES 'held' rows", len(in_due6) == 0)
c = db.get_conn()
c.execute("DELETE FROM scheduled_messages WHERE kind='review_request_held_test'")
c.commit(); c.close()

# ============================================================
# TEST #7: re-scan dedupe
# ============================================================
print("\n--- Test 7: re-scan dedupe ---")
db.set_growth_mode(1, "tray")
clear_scheduled(1)
growth.scan()
c = db.get_conn()
cnt_before = c.execute(
    "SELECT COUNT(*) FROM scheduled_messages WHERE business_id=1 "
    "AND kind NOT IN ('reminder','followup')"
).fetchone()[0]
c.close()
growth.scan()
c = db.get_conn()
cnt_after = c.execute(
    "SELECT COUNT(*) FROM scheduled_messages WHERE business_id=1 "
    "AND kind NOT IN ('reminder','followup')"
).fetchone()[0]
c.close()
check("#7 re-scan does not double-queue (dedupe index holds)",
      cnt_after == cnt_before and cnt_before >= 1)

# ============================================================
# TEST #8: release_growth_batch
# ============================================================
print("\n--- Test 8: release_growth_batch ---")
# Use biz5 (bid5) which has held winback from test #5.
# First ensure bid5 has held rows (winback is still held)
c = db.get_conn()
held8 = c.execute(
    "SELECT COUNT(*) FROM scheduled_messages WHERE business_id=? AND status='held'",
    (bid5,)).fetchone()[0]
c.close()
check("#8a bid5 has at least one held row", held8 >= 1)

# Also create another biz to verify cross-tenant scope
biz8other = make_biz(name="OtherBiz8")
bid8other = biz8other["id"]
lead8other = db.create_lead(bid8other, "Other", "+15550000801")
c = db.get_conn()
c.execute(
    "INSERT INTO scheduled_messages"
    " (business_id, lead_id, kind, send_at, body, status, created_at)"
    " VALUES (?,?,'winback',?,'other held','held',?)",
    (bid8other, lead8other,
     (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
     db.now_iso()))
c.commit(); c.close()

result8 = db.release_growth_batch(bid5, approved_via="sms_go")
check("#8b release returns released >= 1", result8.get("released", 0) >= 1)
check("#8c release returns a 16-char batch_id", len(result8.get("batch_id", "")) == 16)

c = db.get_conn()
still_held5 = c.execute(
    "SELECT COUNT(*) FROM scheduled_messages WHERE business_id=? AND status='held'",
    (bid5,)).fetchone()[0]
other_held = c.execute(
    "SELECT COUNT(*) FROM scheduled_messages WHERE business_id=? AND status='held'",
    (bid8other,)).fetchone()[0]
approvals8 = c.execute(
    "SELECT approved_via FROM growth_approvals WHERE business_id=? AND batch_id=?",
    (bid5, result8["batch_id"])).fetchall()
c.close()
check("#8d all bid5 held rows flipped to 'pending'", still_held5 == 0)
check("#8e other biz held row untouched (cross-tenant scope)", other_held == 1)
check("#8f growth_approvals written with correct approved_via",
      len(approvals8) >= 1 and all(r[0] == "sms_go" for r in approvals8))

# ============================================================
# TEST #9: release_growth_play (single)
# ============================================================
print("\n--- Test 9: release_growth_play ---")
# biz 1 still has held rows from test #7. Get one.
c = db.get_conn()
row9 = c.execute(
    "SELECT id, lead_id FROM scheduled_messages WHERE business_id=1 AND status='held' LIMIT 1"
).fetchone()
c.close()
if row9:
    sid9, lid9 = row9[0], row9[1]
    released9 = db.release_growth_play(sid9, 1, approved_via="ui_tap")
    check("#9a release_growth_play returns True", released9 is True)
    c = db.get_conn()
    st9 = c.execute("SELECT status FROM scheduled_messages WHERE id=?", (sid9,)).fetchone()
    appr9 = c.execute(
        "SELECT id FROM growth_approvals WHERE business_id=1 AND lead_id=? AND approved_via='ui_tap'",
        (lid9,)).fetchone()
    c.close()
    check("#9b play is now 'pending'", st9 and st9[0] == "pending")
    check("#9c growth_approvals row written", appr9 is not None)
else:
    check("#9a release_growth_play returns True", False)
    check("#9b play is now 'pending'", False)
    check("#9c growth_approvals row written", False)

# ============================================================
# TEST #10: cancel_growth_play
# ============================================================
print("\n--- Test 10: cancel_growth_play ---")
biz10 = make_biz(name="CancelBiz")
bid10 = biz10["id"]
lead10a = db.create_lead(bid10, "Cancel A", "+15550001001")
lead10b = db.create_lead(bid10, "Cancel B", "+15550001002")
past10 = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
c = db.get_conn()
cur10a = c.execute(
    "INSERT INTO scheduled_messages"
    " (business_id, lead_id, kind, send_at, body, status, created_at)"
    " VALUES (?,?,'winback',?,'body a','held',?)",
    (bid10, lead10a, past10, db.now_iso()))
cur10b = c.execute(
    "INSERT INTO scheduled_messages"
    " (business_id, lead_id, kind, send_at, body, status, created_at)"
    " VALUES (?,?,'reactivation',?,'body b','held',?)",
    (bid10, lead10b, past10, db.now_iso()))
c.commit()
sid10a = cur10a.lastrowid; sid10b = cur10b.lastrowid
c.close()
db.cancel_growth_play(sid10a, bid10)
c = db.get_conn()
st10a = c.execute("SELECT status FROM scheduled_messages WHERE id=?", (sid10a,)).fetchone()
st10b = c.execute("SELECT status FROM scheduled_messages WHERE id=?", (sid10b,)).fetchone()
c.close()
check("#10a cancel sets status='canceled'", st10a and st10a[0] == "canceled")
check("#10b other row remains 'held'", st10b and st10b[0] == "held")

# ============================================================
# TEST #11: recent_growth_touch
# ============================================================
print("\n--- Test 11: recent_growth_touch ---")
biz11 = make_biz(name="FreqBiz11")
bid11 = biz11["id"]
lead11 = db.create_lead(bid11, "Freq Lead", "+15550001101")
clear_growth_logs(bid11)
check("#11a False when no log entry", db.recent_growth_touch(bid11, lead11, 30) is False)
c = db.get_conn()
c.execute(
    "INSERT INTO growth_touch_log (business_id, lead_id, kind, sent_at) VALUES (?,?,?,?)",
    (bid11, lead11, "review_request",
     (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()))
c.commit(); c.close()
check("#11b True within 30 days (touch 20d ago)", db.recent_growth_touch(bid11, lead11, 30) is True)
check("#11c False for 10-day window (touch is 20d old)",
      db.recent_growth_touch(bid11, lead11, 10) is False)

# ============================================================
# TEST #12: growth_touch_count_12mo
# ============================================================
print("\n--- Test 12: growth_touch_count_12mo ---")
biz12 = make_biz(name="CountBiz12")
bid12 = biz12["id"]
lead12 = db.create_lead(bid12, "Count Lead", "+15550001201")
c = db.get_conn()
c.execute(
    "INSERT INTO growth_touch_log (business_id, lead_id, kind, sent_at) VALUES (?,?,?,?)",
    (bid12, lead12, "review_request",
     (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()))
c.execute(
    "INSERT INTO growth_touch_log (business_id, lead_id, kind, sent_at) VALUES (?,?,?,?)",
    (bid12, lead12, "winback",
     (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()))
c.commit(); c.close()
check("#12 count_12mo counts only within 365 days",
      db.growth_touch_count_12mo(bid12, lead12) == 1)

# ============================================================
# TEST #13: frequency cap in scan()
# ============================================================
print("\n--- Test 13: frequency cap ---")
biz13 = make_biz(name="FreqCapBiz", trade="painting",
                 review_link="https://g.page/r/test13")
bid13 = biz13["id"]
db.set_avg_job_value(bid13, 2500)
lead13 = db.create_lead(bid13, "Freq Person", "+15550001301")
db.book_appointment(bid13, lead13, "job", day=day_ago(5), slot_time="10:00")
db.add_message(lead13, "in", "Great!")
# Touch 20 days ago -> within 30-day cap
c = db.get_conn()
c.execute(
    "INSERT INTO growth_touch_log (business_id, lead_id, kind, sent_at) VALUES (?,?,?,?)",
    (bid13, lead13, "review_request",
     (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()))
c.commit(); c.close()
db.set_growth_mode(bid13, "tray")
clear_scheduled(bid13)
growth.scan()
c = db.get_conn()
rows13 = c.execute(
    "SELECT id FROM scheduled_messages WHERE business_id=? "
    "AND kind NOT IN ('reminder','followup')", (bid13,)).fetchall()
c.close()
check("#13 freq cap: 20d old touch -> no play queued", len(rows13) == 0)

# ============================================================
# TEST #14: _job_value trade defaults
# ============================================================
print("\n--- Test 14: _job_value ---")
check("#14a paint -> 2500", growth._job_value({"avg_job_value": None, "trade": "painting"}) == 2500)
check("#14b roof -> 8000", growth._job_value({"avg_job_value": 0, "trade": "roofing"}) == 8000)
check("#14c hvac -> 3500", growth._job_value({"avg_job_value": None, "trade": "hvac"}) == 3500)
check("#14d real value used as-is", growth._job_value({"avg_job_value": 5000, "trade": "hvac"}) == 5000)
check("#14e generic fallback 2000 (never $0)",
      growth._job_value({"avg_job_value": None, "trade": "general"}) == 2000)

# ============================================================
# TEST #15: win-back inbound-only TCPA narrowing (enforced in scan(), not plays())
# plays() returns winback for display regardless of inbound status.
# scan() gates the actual queue: cold leads (no inbound) are skipped.
# ============================================================
print("\n--- Test 15: winback inbound-only ---")
biz15 = make_biz(name="WinbackBiz", trade="painting",
                 review_link="https://g.page/r/test15")
bid15 = biz15["id"]
db.set_avg_job_value(bid15, 2500)
winback_day15 = (date.today().replace(year=date.today().year - 1) - timedelta(days=35)).isoformat()

# Cold lead: ONLY outbound messages (no inbound contact)
lead15c = db.create_lead(bid15, "Cold", "+15550001501")
db.book_appointment(bid15, lead15c, "old", day=winback_day15, slot_time="10:00")
db.add_message(lead15c, "out", "Job done!")

# Warm lead: has at least one inbound message
lead15w = db.create_lead(bid15, "Warm", "+15550001502")
db.book_appointment(bid15, lead15w, "old", day=winback_day15, slot_time="14:00")
db.add_message(lead15w, "in", "Thanks great job!")

# Test via scan(): cold lead must NOT be queued; warm lead must be queued.
db.set_growth_mode(bid15, "tray")
clear_scheduled(bid15)
growth.scan()
c = db.get_conn()
queued15 = c.execute(
    "SELECT lead_id FROM scheduled_messages WHERE business_id=? AND kind='winback'",
    (bid15,)).fetchall()
c.close()
queued15_ids = [r[0] for r in queued15]
check("#15a no winback queued for cold-only lead (no inbound; TCPA gate)",
      lead15c not in queued15_ids)
check("#15b winback queued for warm lead (has inbound; EBR stronger)",
      lead15w in queued15_ids)

# ============================================================
# TEST #16: tone-risk
# ============================================================
print("\n--- Test 16: tone-risk ---")
biz16 = make_biz(name="ToneRiskBiz", trade="painting",
                 review_link="https://g.page/r/test16")
bid16 = biz16["id"]
db.set_avg_job_value(bid16, 2500)
lead16 = db.create_lead(bid16, "Angry", "+15550001601")
db.book_appointment(bid16, lead16, "job", day=day_ago(5), slot_time="10:00")
db.add_message(lead16, "in", "This was terrible and I am unhappy with everything!")

biz16_full = db.get_business(bid16)
plays16 = growth.plays(biz16_full)
rr16 = [p for p in plays16 if p["kind"] == "review_request" and p["lead_id"] == lead16]
check("#16a tone_risk=True on negative inbound",
      len(rr16) == 1 and rr16[0].get("tone_risk") is True)

# In auto mode: tone-risk plays must be 'held' even for review_request
db.set_growth_mode(bid16, "auto")
clear_scheduled(bid16)
growth.scan()
c = db.get_conn()
rows16 = c.execute(
    "SELECT status FROM scheduled_messages WHERE business_id=? AND lead_id=?",
    (bid16, lead16)).fetchall()
c.close()
check("#16b tone-risk play in auto mode -> 'held' (not 'pending')",
      len(rows16) >= 1 and all(r[0] == "held" for r in rows16))

# ============================================================
# TEST #17: blocked play (no review link)
# ============================================================
print("\n--- Test 17: blocked play ---")
biz17 = make_biz(name="NoLinkBiz", trade="painting", review_link="")
bid17 = biz17["id"]
db.set_avg_job_value(bid17, 2500)
lead17 = db.create_lead(bid17, "No Link", "+15550001701")
db.book_appointment(bid17, lead17, "job", day=day_ago(5), slot_time="10:00")
db.add_message(lead17, "in", "Great work!")

biz17_full = db.get_business(bid17)
plays17 = growth.plays(biz17_full)
blocked17 = [p for p in plays17
             if p["kind"] == "review_request" and p["lead_id"] == lead17
             and not p.get("sendable")]
check("#17a plays() returns sendable=False when review link missing", len(blocked17) == 1)
check("#17b blocked_reason='add_review_link'",
      blocked17 and blocked17[0].get("blocked_reason") == "add_review_link")

# scan() must NOT queue it
db.set_growth_mode(bid17, "tray")
clear_scheduled(bid17)
growth.scan()
c = db.get_conn()
rows17 = c.execute(
    "SELECT id FROM scheduled_messages WHERE business_id=? "
    "AND kind NOT IN ('reminder','followup')", (bid17,)).fetchall()
c.close()
check("#17c scan() does NOT queue blocked play", len(rows17) == 0)

# ============================================================
# TEST #18: consent_basis_for_lead
# ============================================================
print("\n--- Test 18: consent_basis_for_lead ---")
biz18 = make_biz(name="ConsentBiz")
bid18 = biz18["id"]
lead18 = db.create_lead(bid18, "Consent Lead", "+15550001801")
booked18 = day_ago(10)
db.book_appointment(bid18, lead18, "job", day=booked18, slot_time="10:00")
basis18 = db.consent_basis_for_lead(bid18, lead18)
check("#18 consent_basis_for_lead -> EBR:last_job:YYYY-MM-DD",
      basis18 == f"EBR:last_job:{booked18}")

# ============================================================
# TEST #19: add_growth_touch_log + recent_growth_touch
# ============================================================
print("\n--- Test 19: add_growth_touch_log ---")
biz19 = make_biz(name="LogBiz")
bid19 = biz19["id"]
lead19 = db.create_lead(bid19, "Log Lead", "+15550001901")
clear_growth_logs(bid19)
check("#19a no touch -> recent_growth_touch False",
      db.recent_growth_touch(bid19, lead19, 30) is False)
db.add_growth_touch_log(bid19, lead19, "review_request",
                        outcome="sent", source="batch_approved")
check("#19b after log write -> recent_growth_touch True",
      db.recent_growth_touch(bid19, lead19, 30) is True)
c = db.get_conn()
log19 = c.execute(
    "SELECT kind, outcome, source FROM growth_touch_log WHERE business_id=? AND lead_id=?",
    (bid19, lead19)).fetchone()
c.close()
check("#19c log entry has correct kind/outcome/source",
      log19 and log19[0] == "review_request"
      and log19[1] == "sent" and log19[2] == "batch_approved")

# ============================================================
# Summary
# ============================================================
print(f"\n{'='*50}")
print(f"ALPHA: {_pass} passed, {_fail} failed out of {_pass + _fail} tests")
if _fail:
    import sys; sys.exit(1)
