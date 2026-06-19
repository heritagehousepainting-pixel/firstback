"""Batch D -- Owner alerts / set-and-forget (plan 05). Run: python3 test_batch_d.py

Proves the 6 alert consolidations, with the system's most-called function (alerts.notify)
under careful test:
  1. Owner quiet hours hold non-urgent SMS/email/webhook overnight (urgent bypasses);
     the in-app row is ALWAYS recorded; the customer TCPA path is never touched.
  2. Stall-nudge daily cap (most-idle first).
  3. Opt-in "all clear" daily digest.
  4. ROI-milestone toggle round-trips + gates the alert.
  5. Webhook channel (fire-and-forget, sanitized payload, honest sent/failed status).
  6. tick_stale fans out to every tenant (not hardcoded id=1).

Throwaway temp DB, demo brain.
"""
import os
import tempfile
import datetime as _dtmod
from datetime import timezone, timedelta

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""   # configured() False -> simulate; never a real send

import alerts
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


def chans(attempted):
    return [c for c, _ in attempted]


db.init_db()
# Seed business 1 exists from init; give it an owner cell so the SMS path is reachable.
db.update_alert_prefs(1, {"alert_sms": "+15550001111"})

# ============================================================================
# Change 1 — Owner quiet hours. Control the clock by stubbing alerts.datetime.
# ============================================================================
class _FixedNow:
    hour = 23
    @staticmethod
    def now(tz=None):
        return _dtmod.datetime(2026, 6, 19, _FixedNow.hour, 0, 0, tzinfo=tz)

_real_dt = alerts.datetime
alerts.datetime = _FixedNow

db.update_alert_prefs(1, {"alert_quiet_start": 22, "alert_quiet_end": 7})
biz = db.get_business(1)

_FixedNow.hour = 23
att = alerts.notify(biz, "lead", {"name": "Night Lead", "phone": "+15559999999", "lead_id": 9001})
check("quiet hours hold the lead SMS (only in-app recorded)", att == [("inapp", "recorded")])

# the in-app row is still written -- the owner sees it in the feed immediately
recent = db.recent_alerts(1, 20)
check("quiet-held alert still recorded in-app", any(a.get("kind") == "lead" for a in recent))

_FixedNow.hour = 23
att = alerts.notify(biz, "urgent", {"name": "Emergency", "phone": "+1", "lead_id": 9002})
check("urgent bypasses quiet hours (SMS attempted)", "sms" in chans(att))

_FixedNow.hour = 14
att = alerts.notify(biz, "lead", {"name": "Day Lead", "phone": "+1", "lead_id": 9003})
check("daytime lead sends SMS", "sms" in chans(att))

_FixedNow.hour = 7   # exactly quiet_end -> window is exclusive at the end -> passes
att = alerts.notify(biz, "lead", {"name": "Dawn", "phone": "+1", "lead_id": 9004})
check("hour == quiet_end passes (exclusive end)", "sms" in chans(att))

db.update_alert_prefs(1, {"alert_quiet_start": 2, "alert_quiet_end": 6})  # same-day window
biz = db.get_business(1)
_FixedNow.hour = 3
att = alerts.notify(biz, "lead", {"name": "SameDay", "phone": "+1", "lead_id": 9005})
check("same-day quiet window (2..6) holds at 3am", att == [("inapp", "recorded")])

db.update_alert_prefs(1, {"alert_quiet_start": 9, "alert_quiet_end": 9})  # equal -> disabled
biz = db.get_business(1)
_FixedNow.hour = 9
att = alerts.notify(biz, "lead", {"name": "NoQuiet", "phone": "+1", "lead_id": 9006})
check("start == end disables quiet hours", "sms" in chans(att))

check("_int_pref fallback on missing key", alerts._int_pref({}, "alert_quiet_start", 22) == 22)
check("_int_pref coerces a string", alerts._int_pref({"alert_quiet_start": "5"}, "alert_quiet_start", 22) == 5)
check("_int_pref junk -> default", alerts._int_pref({"alert_quiet_start": "x"}, "alert_quiet_start", 22) == 22)
check("_int_pref keeps a real 0 (not treated as missing)", alerts._int_pref({"max_stall_alerts_day": 0}, "max_stall_alerts_day", 2) == 0)

# customer TCPA backstop is untouched: owner alerts still call send_sms(gate=False).
import inspect as _inspect
_src = _inspect.getsource(alerts.notify)
check("owner SMS stays gate=False (TCPA backstop not co-opted)", "gate=False" in _src)

alerts.datetime = _real_dt   # restore the real clock for the remaining tests
# disable quiet hours so later notify() calls aren't held by the real wall clock
db.update_alert_prefs(1, {"alert_quiet_start": 0, "alert_quiet_end": 0})

# ============================================================================
# Change 5 — Webhook channel
# ============================================================================
import urllib.request
import json as _json
_webhook_calls = []

class _Resp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b"ok"

def _fake_urlopen(req, timeout=None):
    _webhook_calls.append((req.full_url, req.data))
    return _Resp()

_real_urlopen = urllib.request.urlopen
urllib.request.urlopen = _fake_urlopen
# Isolate the SEND path from the SSRF guard (the test host doesn't resolve); the guard
# itself is unit-tested separately below.
_real_allow = alerts._webhook_url_allowed
alerts._webhook_url_allowed = lambda u: True

db.update_alert_prefs(1, {"alert_webhook_url": "https://hooks.example/x"})
biz = db.get_business(1)
att = alerts.notify(biz, "lead", {"name": "Hook", "phone": "+1", "lead_id": 9101})
check("webhook POSTs on alert", len(_webhook_calls) == 1)
check("webhook payload carries kind + body", '"kind": "lead"' in _webhook_calls[0][1].decode())
check("webhook recorded in attempted as sent", ("webhook", "sent") in att)

_webhook_calls.clear()
db.update_alert_prefs(1, {"alert_webhook_url": ""})
biz = db.get_business(1)
alerts.notify(biz, "lead", {"name": "NoHook", "phone": "+1", "lead_id": 9102})
check("no webhook POST when url empty", len(_webhook_calls) == 0)

def _boom(req, timeout=None):
    raise OSError("connection refused")
urllib.request.urlopen = _boom
db.update_alert_prefs(1, {"alert_webhook_url": "https://hooks.example/x"})
biz = db.get_business(1)
_raised = False
try:
    att = alerts.notify(biz, "lead", {"name": "BadHook", "phone": "+1", "lead_id": 9103})
except Exception:
    _raised = True
check("webhook failure never raises", not _raised)
check("webhook failure reports honest 'failed' status", ("webhook", "failed") in att)

urllib.request.urlopen = _fake_urlopen
_webhook_calls.clear()
biz = db.get_business(1)
class _Weird:  # non-serializable context value
    pass
alerts.notify(biz, "lead", {"name": "San", "phone": "+1", "lead_id": 9104, "obj": _Weird()})
_body = _json.loads(_webhook_calls[0][1].decode())
check("webhook context sanitized (non-scalar dropped, JSON still encodes)",
      "obj" not in _body["context"] and _body["kind"] == "lead")

urllib.request.urlopen = _real_urlopen
alerts._webhook_url_allowed = _real_allow
db.update_alert_prefs(1, {"alert_webhook_url": ""})

# SSRF guard unit tests (IP literals -> no DNS needed).
check("webhook guard blocks http:// (non-https)", alerts._webhook_url_allowed("http://8.8.8.8/x") is False)
check("webhook guard blocks loopback", alerts._webhook_url_allowed("https://127.0.0.1/x") is False)
check("webhook guard blocks private 10.x", alerts._webhook_url_allowed("https://10.0.0.5/x") is False)
check("webhook guard blocks link-local (metadata)", alerts._webhook_url_allowed("https://169.254.169.254/latest") is False)
check("webhook guard allows a public https host", alerts._webhook_url_allowed("https://8.8.8.8/x") is True)

# ============================================================================
# Change 4 — ROI milestone toggle
# ============================================================================
db.update_alert_prefs(1, {"alert_on_roi_milestone": 0})
biz = db.get_business(1)
check("roi_milestone toggle saves 0", biz["alert_on_roi_milestone"] == 0)
check("roi_milestone disabled blocks the alert", alerts._enabled_for(biz, "roi_milestone") is False)
db.update_alert_prefs(1, {"alert_on_roi_milestone": 1})

# ============================================================================
# Change 3 — All-clear daily digest copy + scan behavior
# ============================================================================
check("all_clear copy is the reassurance line",
      alerts.format_message("daily_digest", {"all_clear": True})
      == "Good morning. Quiet day -- no leads waiting, nothing to approve. FirstBack is running.")

# The scan windows (8am digest, afternoon stall) are evaluated in the business's LOCAL
# tz; pin it to UTC so the test's UTC `now` values land in the intended local hour.
reminders._biz_tz = lambda b: timezone.utc

import assistant
_orig_brief, _orig_held, _orig_idle, _orig_listbiz = (
    assistant.briefing, db.list_held_messages, db.warm_leads_idle, db.list_businesses)
assistant.briefing = lambda b: {"headline": "", "items": [], "tone": "quiet"}
db.list_held_messages = lambda bid: []
db.warm_leads_idle = lambda bid, h: []
db.list_businesses = lambda: [db.get_business(1)]

db.update_alert_prefs(1, {"alert_all_clear": 1})
fired = reminders.scan_daily_digest(now="2026-06-19T08:30:00+00:00")
check("all_clear fires when opted in on a quiet day", fired == 1)

db.update_alert_prefs(1, {"alert_all_clear": 0})
fired = reminders.scan_daily_digest(now="2026-06-20T08:30:00+00:00")  # new day -> no dedupe carry
check("all_clear silent when opted out (default)", fired == 0)

# ============================================================================
# Change 2 — Stall-nudge daily cap (most-idle first)
# ============================================================================
_fake_idle = [{"id": i, "name": f"L{i}", "idle_hours": 50 - i} for i in range(5)]  # L0 most idle
db.warm_leads_idle = lambda bid, h: list(_fake_idle)
_notify_calls = []
_orig_notify = alerts.notify
alerts.notify = lambda b, k, c: (_notify_calls.append((k, c.get("lead_id"))) or [("inapp", "x")])

db.update_alert_prefs(1, {"max_stall_alerts_day": 2})
reminders.scan_stall_nudges(now="2026-06-19T14:00:00+00:00")
_stall = [c for c in _notify_calls if c[0] == "vic_stall"]
check("stall cap fires exactly the cap (2 of 5)", len(_stall) == 2)
check("stall cap notifies the most-idle lead first", _stall[0][1] == 0)

_notify_calls.clear()
db.update_alert_prefs(1, {"max_stall_alerts_day": 0})
reminders.scan_stall_nudges(now="2026-06-19T14:00:00+00:00")
check("stall cap 0 -> no nudges (mute)", len([c for c in _notify_calls if c[0] == "vic_stall"]) == 0)

alerts.notify = _orig_notify
db.warm_leads_idle = _orig_idle
db.list_held_messages = _orig_held
assistant.briefing = _orig_brief

# ============================================================================
# Change 6 — tick_stale fans out to every tenant (not hardcoded id=1)
# ============================================================================
db.list_businesses = _orig_listbiz   # real -> returns all tenants
db.create_business({"name": "Second Tenant", "alert_sms": "+15550002222"})
_tick_calls = []
alerts.notify = lambda b, k, c: (_tick_calls.append((k, b.get("id"))) or [])
db.set_meta("last_tick_utc", (_dtmod.datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat())
reminders.tick_once()
_stale = [c for c in _tick_calls if c[0] == "tick_stale"]
check("tick_stale fans to ALL businesses (>= 2 tenants alerted)",
      len(_stale) >= 2 and {bid for _, bid in _stale} >= {1, 2})
alerts.notify = _orig_notify

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
