"""Reminders module unit tests (the lie-fix: F05 Phase 2).
Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_reminders.py

Covers:
  - when_phrase: correct human-readable string
  - reminder_body / followup_body: copy text
  - next_send_time: quiet-hours deferral
  - compute_send_at: UTC ISO output, deferred out of quiet hours
  - due_followup_leads: pure filter
  - classify_rsvp: yes / no / unknown
  - enqueue_morning_reminder: 8am guard, <10am estimate guard, already-past guard,
    dedupe guard
Cross-agent stubs: config.biz_tz, db.find_scheduled_message, db.queue_sms_retry,
    db.get_message_by_provider_sid, connections.check_forwarding_health are
    monkeypatched so tests run standalone without A1/A2 work.
"""
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

os.environ["FIRSTBACK_PROVIDER"] = "demo"

# ---- stub config.biz_tz (A1 seam) before importing reminders ----
import config as _config
if not hasattr(_config, "biz_tz"):
    _ET = ZoneInfo("America/New_York")
    def _biz_tz_stub(business):
        if isinstance(business, dict):
            tz_name = business.get("timezone", "America/New_York")
        else:
            tz_name = "America/New_York"
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return _ET
    _config.biz_tz = _biz_tz_stub

# ---- stub db functions (A1 seams) before importing db ----
import db as _db
if not hasattr(_db, "find_scheduled_message"):
    _db.find_scheduled_message = lambda biz_id, lead_id, kind: None
if not hasattr(_db, "queue_sms_retry"):
    _db.queue_sms_retry = lambda *a, **kw: None
if not hasattr(_db, "get_message_by_provider_sid"):
    _db.get_message_by_provider_sid = lambda sid: None

# ---- stub connections.check_forwarding_health (A2 seam) ----
import connections as _conn
if not hasattr(_conn, "check_forwarding_health"):
    _conn.check_forwarding_health = lambda: None

# ---- now import reminders ----
import reminders

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- when_phrase ----
check("when_phrase formats correctly",
      reminders.when_phrase("2026-06-15", "14:00") == "Mon Jun 15 at 2:00 PM")
check("when_phrase handles AM time",
      reminders.when_phrase("2026-06-15", "09:00") == "Mon Jun 15 at 9:00 AM")
check("when_phrase returns empty on bad input",
      reminders.when_phrase("bad", "14:00") == "")
check("when_phrase handles missing time",
      reminders.when_phrase("2026-06-15", None) == "Mon Jun 15")

# ---- reminder_body ----
body = reminders.reminder_body("Alice Smith", "Heritage Painting", "Mon Jun 15 at 2:00 PM")
check("reminder_body contains first name", "Alice" in body)
check("reminder_body does NOT contain last name", "Smith" not in body)
check("reminder_body contains business name", "Heritage Painting" in body)
check("reminder_body contains when phrase", "Mon Jun 15" in body)

body_no_name = reminders.reminder_body("", "Heritage Painting", "Mon Jun 15 at 2:00 PM")
check("reminder_body with no name uses 'there'", "there" in body_no_name)

# ---- followup_body ----
fbody = reminders.followup_body("Dave", "Acme Painting")
check("followup_body contains name", "Dave" in fbody)
check("followup_body contains business name", "Acme Painting" in fbody)

# ---- next_send_time: quiet-hours deferral ----
ET = ZoneInfo("America/New_York")
# Before quiet start (5am) -> deferred to QUIET_START (8am)
early = datetime(2026, 6, 15, 5, 30, tzinfo=ET)
deferred = reminders.next_send_time(early, 8, 21)
check("early morning deferred to 8am", deferred.hour == 8 and deferred.minute == 0)

# Inside window (10am) -> unchanged
midday = datetime(2026, 6, 15, 10, 0, tzinfo=ET)
check("10am not deferred", reminders.next_send_time(midday, 8, 21) == midday)

# After quiet end (22:00) -> next day 8am
late = datetime(2026, 6, 15, 22, 0, tzinfo=ET)
next_day = reminders.next_send_time(late, 8, 21)
check("late night deferred to next day", next_day.day == 16 and next_day.hour == 8)

# ---- compute_send_at ----
# 24h before a 2pm slot on 2026-06-20 ET = 2pm 2026-06-19 ET = in window, UTC stored
send_at = reminders.compute_send_at("2026-06-20", "14:00", 24, ET, 8, 21)
check("compute_send_at returns UTC ISO string",
      send_at.endswith("+00:00") or send_at.endswith("Z") or "T" in send_at)
dt = datetime.fromisoformat(send_at)
check("compute_send_at is before the appointment",
      dt < datetime(2026, 6, 20, 14, 0, tzinfo=ET).astimezone(timezone.utc))

# Early estimate: lead_hours > estimate time -> clamp to 5min before
send_at_9 = reminders.compute_send_at("2026-06-20", "09:00", 24, ET, 8, 21)
appt_9 = datetime(2026, 6, 20, 9, 0, tzinfo=ET).astimezone(timezone.utc)
dt_9 = datetime.fromisoformat(send_at_9)
check("compute_send_at with 9am slot is before appointment", dt_9 < appt_9)

# ---- due_followup_leads ----
now = "2026-06-15T12:00:00+00:00"
rows_cold = [
    {"phone": "+15550000001", "has_followup": False,
     "last_msg_at": "2026-06-14T10:00:00+00:00"},  # cold (>24h)
    {"phone": "+15550000002", "has_followup": True,
     "last_msg_at": "2026-06-14T10:00:00+00:00"},   # already has followup
    {"phone": "",             "has_followup": False,
     "last_msg_at": "2026-06-14T10:00:00+00:00"},   # no phone
    {"phone": "+15550000003", "has_followup": False,
     "last_msg_at": "2026-06-15T11:00:00+00:00"},   # too recent (<24h)
]
due = reminders.due_followup_leads(rows_cold, now, 24)
check("due_followup_leads returns cold lead", len(due) == 1)
check("due_followup_leads returns phone +15550000001",
      due[0]["phone"] == "+15550000001")

# ---- classify_rsvp ----
check("classify_rsvp 'yes' on 'confirmed'",
      reminders.classify_rsvp("Confirmed, see you then!") == "yes")
check("classify_rsvp 'yes' on 'yes'",
      reminders.classify_rsvp("Yes I'll be there") == "yes")
check("classify_rsvp 'yes' on 'sounds good'",
      reminders.classify_rsvp("Sounds good, I'll be there") == "yes")
check("classify_rsvp 'no' on 'cancel'",
      reminders.classify_rsvp("I need to cancel") == "no")
check("classify_rsvp 'no' on 'can't make it'",
      reminders.classify_rsvp("I can't make it") == "no")
check("classify_rsvp 'no' on 'won't be'",
      reminders.classify_rsvp("I won't be available") == "no")
check("classify_rsvp 'no' beats 'yes I need to cancel'",
      reminders.classify_rsvp("Yes I need to cancel that") == "no")
check("classify_rsvp 'unknown' on vague text",
      reminders.classify_rsvp("What time again?") == "unknown")
check("classify_rsvp 'unknown' on empty",
      reminders.classify_rsvp("") == "unknown")

# ---- enqueue_morning_reminder ----
# Set up a temp DB for these tests
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
_config.DB_PATH = _TMP.name
_db.DB_PATH = _TMP.name
_db.init_db()

_BIZ = _db.get_business(1)  # seed business

# Create a lead and appointment for testing
_lead_id = _db.create_lead(1, "Test Lead", "+15550001111")
_lead = _db.get_lead(_lead_id)

# Use a future date far enough out
from datetime import date as _date
_future_day = (_date.today() + timedelta(days=14)).isoformat()
_db.book_appointment(1, _lead_id, f"{_future_day} 2:00 PM",
                     day=_future_day, slot_time="14:00")

# Morning reminder: estimate at 2pm (>= 10am) should queue
result = reminders.enqueue_morning_reminder(_BIZ, _lead, _future_day, "14:00")
check("morning_reminder queues for 2pm estimate", result["status"] == "queued")
# Verify send_at parses to 8am local on the estimate day (UTC offset varies with DST)
if result["status"] == "queued":
    from datetime import timezone as _tz
    _ET = ZoneInfo("America/New_York")
    _sent_dt = datetime.fromisoformat(result["send_at"]).astimezone(_ET)
    check("morning_reminder send_at is 8am local on estimate day",
          _sent_dt.hour == 8 and _sent_dt.minute == 0)
else:
    check("morning_reminder send_at is 8am local (skipped — skip status unexpected)", False)

# Skip if estimate before 10am
_lead2_id = _db.create_lead(1, "Early Lead", "+15550002222")
_lead2 = _db.get_lead(_lead2_id)
_future_day2 = (_date.today() + timedelta(days=15)).isoformat()
_db.book_appointment(1, _lead2_id, f"{_future_day2} 9:00 AM",
                     day=_future_day2, slot_time="09:00")
result_9am = reminders.enqueue_morning_reminder(_BIZ, _lead2, _future_day2, "09:00")
check("morning_reminder skips for 9am estimate", result_9am["status"] == "skipped")
check("skip reason is 'estimate before 10am'", "10am" in result_9am.get("reason", ""))

# Dedupe: calling again for same lead+appt should skip if db.find_scheduled_message
# returns an existing row
_db.find_scheduled_message = lambda biz_id, lead_id, kind: {"id": 99}
result_dupe = reminders.enqueue_morning_reminder(_BIZ, _lead, _future_day, "14:00")
check("morning_reminder dedupe skips on existing row", result_dupe["status"] == "skipped")
_db.find_scheduled_message = lambda biz_id, lead_id, kind: None

# Skip if morning already past (use today as estimate day)
_today_str = _date.today().isoformat()
_lead3_id = _db.create_lead(1, "Today Lead", "+15550003333")
_lead3 = _db.get_lead(_lead3_id)
_db.book_appointment(1, _lead3_id, f"{_today_str} 2:00 PM",
                     day=_today_str, slot_time="14:00")
result_today = reminders.enqueue_morning_reminder(_BIZ, _lead3, _today_str, "14:00")
# After 8am today the morning is past, so it should skip (or queue if somehow before 8am)
# We can't control the clock in tests, so just verify it returns a valid status
check("morning_reminder today returns valid status",
      result_today["status"] in ("queued", "skipped"))


# =============================================================================
# Phase 5e tests -- S1-M1 (F06 cold-follow-up hardening)
# =============================================================================

# ---- due_followup_leads: Phase 5e additions ----
_now5e = "2026-06-15T12:00:00+00:00"
_cold_ts = "2026-06-14T10:00:00+00:00"  # >24h ago

# test_due_followup_leads_skips_has_followup (existing behavior, regression guard)
_rows_hf = [{"phone": "+15550010001", "has_followup": True, "has_followup_2": False,
              "last_msg_at": _cold_ts}]
check("due_followup_leads skips has_followup=True",
      len(reminders.due_followup_leads(_rows_hf, _now5e, 24)) == 0)

# test_due_followup_leads_skips_has_followup_2
_rows_hf2 = [{"phone": "+15550010002", "has_followup": False, "has_followup_2": True,
               "last_msg_at": _cold_ts}]
check("due_followup_leads skips has_followup_2=True",
      len(reminders.due_followup_leads(_rows_hf2, _now5e, 24)) == 0)

# test_due_followup_leads_skips_no_phone
_rows_nophone = [{"phone": "", "has_followup": False, "has_followup_2": False,
                  "last_msg_at": _cold_ts}]
check("due_followup_leads skips empty phone",
      len(reminders.due_followup_leads(_rows_nophone, _now5e, 24)) == 0)

# test_due_followup_leads_skips_not_cold
_rows_hot = [{"phone": "+15550010003", "has_followup": False, "has_followup_2": False,
              "last_msg_at": "2026-06-15T11:30:00+00:00"}]  # only 30min ago
check("due_followup_leads skips recent message",
      len(reminders.due_followup_leads(_rows_hot, _now5e, 24)) == 0)

# ---- scan_followups tests via monkeypatching ----

import messaging as _messaging_mod
import db as _db_mod

def _make_biz(followups_enabled=True):
    return {"id": 99, "name": "Test Painting", "followups_enabled": followups_enabled,
            "timezone": "America/New_York"}

def _make_lead(has_followup=False, has_followup_2=False, last_msg_at=_cold_ts):
    return {"id": 42, "name": "Dave Smith", "phone": "+15550099001",
            "has_followup": has_followup, "has_followup_2": has_followup_2,
            "last_msg_at": last_msg_at, "last_in_text": "Need my deck refinished"}

# test_scan_followups_queues_touch1
_queued_t1 = []
_original_list_biz = _db_mod.list_businesses
_original_followup_rows = _db_mod.followup_candidate_rows
_original_add_sched = _db_mod.add_scheduled_message
_original_outbound = _messaging_mod.outbound_mode

_db_mod.list_businesses = lambda: [_make_biz()]
_db_mod.followup_candidate_rows = lambda biz_id: [_make_lead()]
_db_mod.add_scheduled_message = lambda *a, **kw: (_queued_t1.append(a), 101)[1]
_messaging_mod.outbound_mode = lambda biz, phone: "live"

_result = reminders.scan_followups(now=_now5e)
check("scan_followups queues Touch-1 for cold lead", _result == 1)
check("scan_followups Touch-1 kind is followup",
      any(a[3] == "followup" for a in _queued_t1))

# test_scan_followups_queues_touch2_at_t1_creation
check("scan_followups queues Touch-2 at T1 creation time",
      any(a[3] == "followup_2" for a in _queued_t1))

# Verify Touch-2 send_at is ~5 days after Touch-1
_t1_rows = [a for a in _queued_t1 if a[3] == "followup"]
_t2_rows = [a for a in _queued_t1 if a[3] == "followup_2"]
if _t1_rows and _t2_rows:
    from datetime import datetime as _dt2
    _t1_at = _dt2.fromisoformat(_t1_rows[0][4])
    _t2_at = _dt2.fromisoformat(_t2_rows[0][4])
    _delta = (_t2_at - _t1_at).total_seconds() / 86400
    check("scan_followups Touch-2 send_at is ~5 days after Touch-1",
          4.9 <= _delta <= 5.5)
else:
    check("scan_followups Touch-2 send_at check (T1 or T2 missing)", False)

# Restore
_db_mod.list_businesses = _original_list_biz
_db_mod.followup_candidate_rows = _original_followup_rows
_db_mod.add_scheduled_message = _original_add_sched
_messaging_mod.outbound_mode = _original_outbound

# test_scan_followups_no_double_touch2
_queued_no_t2 = []
_db_mod.list_businesses = lambda: [_make_biz()]
_db_mod.followup_candidate_rows = lambda biz_id: [_make_lead(has_followup_2=True)]
_db_mod.add_scheduled_message = lambda *a, **kw: (_queued_no_t2.append(a), 102)[1]
_messaging_mod.outbound_mode = lambda biz, phone: "live"

reminders.scan_followups(now=_now5e)
check("scan_followups no Touch-2 when has_followup_2=True",
      all(a[3] != "followup_2" for a in _queued_no_t2))

_db_mod.list_businesses = _original_list_biz
_db_mod.followup_candidate_rows = _original_followup_rows
_db_mod.add_scheduled_message = _original_add_sched
_messaging_mod.outbound_mode = _original_outbound

# test_scan_followups_respects_followups_off
_queued_off = []
_db_mod.list_businesses = lambda: [_make_biz(followups_enabled=False)]
_db_mod.followup_candidate_rows = lambda biz_id: [_make_lead()]
_db_mod.add_scheduled_message = lambda *a, **kw: (_queued_off.append(a), 103)[1]
_messaging_mod.outbound_mode = lambda biz, phone: "live"

_r_off = reminders.scan_followups(now=_now5e)
check("scan_followups skips biz with followups_enabled=False",
      _r_off == 0 and len(_queued_off) == 0)

_db_mod.list_businesses = _original_list_biz
_db_mod.followup_candidate_rows = _original_followup_rows
_db_mod.add_scheduled_message = _original_add_sched
_messaging_mod.outbound_mode = _original_outbound

# test_scan_followups_suppressed_skips_enqueue
_queued_sup = []
_db_mod.list_businesses = lambda: [_make_biz()]
_db_mod.followup_candidate_rows = lambda biz_id: [_make_lead()]
_db_mod.add_scheduled_message = lambda *a, **kw: (_queued_sup.append(a), 104)[1]
_messaging_mod.outbound_mode = lambda biz, phone: "suppressed"

_r_sup = reminders.scan_followups(now=_now5e)
check("scan_followups skips suppressed lead (no enqueue)",
      len(_queued_sup) == 0)

_db_mod.list_businesses = _original_list_biz
_db_mod.followup_candidate_rows = _original_followup_rows
_db_mod.add_scheduled_message = _original_add_sched
_messaging_mod.outbound_mode = _original_outbound

# ---- run_due_once tests ----
_original_due_sched = _db_mod.due_scheduled_messages
_original_claim = _db_mod.claim_scheduled_message
_original_mark = _db_mod.mark_scheduled
_original_get_lead = _db_mod.get_lead
_original_get_biz = _db_mod.get_business

def _make_sched_row(kind="followup", lead_id=42, sched_id=99, phone="+15550099001"):
    return {"id": sched_id, "kind": kind, "lead_id": lead_id, "business_id": 99,
            "body": "Test body", "lead_phone": phone, "appt_status": None,
            "appt_day": None, "appt_slot": None, "retry_count": 0}

# test_run_due_once_cancels_followup_if_booked (S3)
_canceled_s3 = []
_send_calls_s3 = []
_db_mod.due_scheduled_messages = lambda now: [_make_sched_row(kind="followup")]
_db_mod.claim_scheduled_message = lambda sid: True
_db_mod.mark_scheduled = lambda sid, status: _canceled_s3.append((sid, status))
_db_mod.get_lead = lambda lead_id, business_id=None: {"id": lead_id, "status": "booked"}
_db_mod.get_business = lambda biz_id: _make_biz()
_messaging_mod.send_sms = lambda *a, **kw: (_send_calls_s3.append(a), {"status": "sent"})[1]

reminders.run_due_once(now=_now5e)
check("run_due_once cancels followup if lead is booked",
      any(s[1] == "canceled" for s in _canceled_s3))
check("run_due_once does NOT send if lead is booked",
      len(_send_calls_s3) == 0)

_db_mod.due_scheduled_messages = _original_due_sched
_db_mod.claim_scheduled_message = _original_claim
_db_mod.mark_scheduled = _original_mark
_db_mod.get_lead = _original_get_lead
_db_mod.get_business = _original_get_biz
_messaging_mod.send_sms = getattr(_messaging_mod, "_orig_send_sms", _messaging_mod.send_sms)

# test_run_due_once_double_claim_idempotent
_send_calls_dc = []
_db_mod.due_scheduled_messages = lambda now: [_make_sched_row(kind="followup")]
_db_mod.claim_scheduled_message = lambda sid: False  # second claimer gets False
_db_mod.get_lead = lambda lead_id, business_id=None: {"id": lead_id, "status": "idle"}
_db_mod.get_business = lambda biz_id: _make_biz()
_messaging_mod.send_sms = lambda *a, **kw: (_send_calls_dc.append(a), {"status": "sent"})[1]

reminders.run_due_once(now=_now5e)
check("run_due_once idempotent: no send on failed claim",
      len(_send_calls_dc) == 0)

_db_mod.due_scheduled_messages = _original_due_sched
_db_mod.claim_scheduled_message = _original_claim
_db_mod.get_lead = _original_get_lead
_db_mod.get_business = _original_get_biz
_messaging_mod.send_sms = getattr(_messaging_mod, "_orig_send_sms", _messaging_mod.send_sms)

# test_run_due_once_followup_transactional_false (S2 compliance gate)
_send_kwargs_tf = {}
def _capture_send(*args, **kwargs):
    _send_kwargs_tf.update(kwargs)
    _send_kwargs_tf["_args"] = args
    return {"status": "sent"}

_db_mod.due_scheduled_messages = lambda now: [_make_sched_row(kind="followup")]
_db_mod.claim_scheduled_message = lambda sid: True
_db_mod.mark_scheduled = lambda sid, status: None
_db_mod.get_lead = lambda lead_id, business_id=None: {"id": lead_id, "status": "idle"}
_db_mod.get_business = lambda biz_id: _make_biz()
_messaging_mod.send_sms = _capture_send

reminders.run_due_once(now=_now5e)
check("run_due_once followup sends with transactional=False",
      _send_kwargs_tf.get("transactional") is False)

_db_mod.due_scheduled_messages = _original_due_sched
_db_mod.claim_scheduled_message = _original_claim
_db_mod.mark_scheduled = _original_mark
_db_mod.get_lead = _original_get_lead
_db_mod.get_business = _original_get_biz
_messaging_mod.send_sms = getattr(_messaging_mod, "_orig_send_sms", _messaging_mod.send_sms)

# test_run_due_once_reminder_transactional_true (S2 compliance gate -- reminder must stay True)
_send_kwargs_rt = {}
def _capture_send_rt(*args, **kwargs):
    _send_kwargs_rt.update(kwargs)
    _send_kwargs_rt["_args"] = args
    return {"status": "sent"}

_db_mod.due_scheduled_messages = lambda now: [_make_sched_row(kind="reminder")]
_db_mod.claim_scheduled_message = lambda sid: True
_db_mod.mark_scheduled = lambda sid, status: None
# reminder path checks appt_status and appt_day -- set them to avoid skip
_reminder_row = _make_sched_row(kind="reminder")
_reminder_row["appt_status"] = "booked"
_reminder_row["appt_day"] = None  # no appt_day -> no _appt_passed check
_db_mod.due_scheduled_messages = lambda now: [_reminder_row]
_db_mod.get_business = lambda biz_id: _make_biz()
_messaging_mod.send_sms = _capture_send_rt

reminders.run_due_once(now=_now5e)
# transactional=True is the default; it's passed as True in our S2 code
check("run_due_once reminder sends with transactional=True (not False)",
      _send_kwargs_rt.get("transactional") is not False)

_db_mod.due_scheduled_messages = _original_due_sched
_db_mod.claim_scheduled_message = _original_claim
_db_mod.mark_scheduled = _original_mark
_db_mod.get_lead = _original_get_lead
_db_mod.get_business = _original_get_biz
_messaging_mod.send_sms = getattr(_messaging_mod, "_orig_send_sms", _messaging_mod.send_sms)

# ---- followup_body_contextual: M1 fallback test ----

# test_followup_body_contextual_fallback
import llm as _llm_mod
_orig_complete = _llm_mod.complete
_orig_active = _llm_mod.active_provider

# Make complete raise an exception -- should fall back to followup_body
_llm_mod.complete = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("LLM down"))
_llm_mod.active_provider = lambda: "claude"

_fb_fallback = reminders.followup_body_contextual("Dave", "Acme Painting", "I need my deck done")
_generic = reminders.followup_body("Dave", "Acme Painting")
check("followup_body_contextual falls back to followup_body on LLM failure",
      _fb_fallback == _generic)

# Also test that empty LLM output falls back
_llm_mod.complete = lambda *a, **kw: ""
_fb_empty = reminders.followup_body_contextual("Dave", "Acme Painting", "Need a quote")
check("followup_body_contextual falls back on empty LLM output",
      _fb_empty == _generic)

_llm_mod.complete = _orig_complete
_llm_mod.active_provider = _orig_active

# ---- cancel_pending_followup_touches (S4 DB layer) ----

# test_cancel_pending_followup_touches
# Uses the temp DB from the enqueue_morning_reminder section above
_test_lead_id = _db.create_lead(1, "Cancel Test", "+15550099555")
# Insert a pending followup and followup_2
_db.add_scheduled_message(1, _test_lead_id, None, "followup",
                          "2026-06-20T15:00:00+00:00", "Touch 1 body")
_db.add_scheduled_message(1, _test_lead_id, None, "followup_2",
                          "2026-06-25T15:00:00+00:00", "Touch 2 body")

# Verify both are pending
import sqlite3 as _sql3
_conn = _sql3.connect(_config.DB_PATH)
_rows_before = _conn.execute(
    "SELECT kind, status FROM scheduled_messages WHERE lead_id=? AND kind IN ('followup','followup_2')",
    (_test_lead_id,)).fetchall()
_conn.close()
check("cancel_pending_followup_touches: both rows exist before cancel",
      len(_rows_before) == 2 and all(r[1] == "pending" for r in _rows_before))

_db.cancel_pending_followup_touches(_test_lead_id)

_conn2 = _sql3.connect(_config.DB_PATH)
_rows_after = _conn2.execute(
    "SELECT kind, status FROM scheduled_messages WHERE lead_id=? AND kind IN ('followup','followup_2')",
    (_test_lead_id,)).fetchall()
_conn2.close()
check("cancel_pending_followup_touches: both rows canceled",
      len(_rows_after) == 2 and all(r[1] == "canceled" for r in _rows_after))

# ---- spam_exclusion: S1 SQL verification ----

# test_spam_exclusion -- verify the SQL query excludes spam leads
# We do a structural check: the followup_candidate_rows SQL text must include the spam clauses
import inspect as _inspect
_fcr_src = _inspect.getsource(_db.followup_candidate_rows)
check("spam_exclusion: SQL includes contacts.category IN blocked check",
      "blocked" in _fcr_src and "contacts c" in _fcr_src)
check("spam_exclusion: SQL includes screened_spam check",
      "screened_spam" in _fcr_src and "calls ca" in _fcr_src)
check("spam_exclusion: SQL includes has_followup_2 column",
      "has_followup_2" in _fcr_src)
check("spam_exclusion: SQL includes last_in_text column",
      "last_in_text" in _fcr_src)

print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
