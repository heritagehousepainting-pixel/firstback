"""Reminders & follow-ups for FirstBack (Feature 1).

Two revenue-savers, both delivered as a single outbound text through the messaging
seam (real Twilio if configured, otherwise simulated onto the lead's thread):
  * Reminder  -- texted before a booked estimate so the customer doesn't no-show.
  * Follow-up -- ONE gentle nudge to a warm lead that replied but went cold.

A single in-process ticker thread (start_ticker, launched from app.py at boot)
wakes every TICK_SECONDS and runs tick_once: queue newly-cold leads, then send
everything that's due. The scheduling math (compute_send_at, quiet-hours deferral,
cold-lead selection, copy) is PURE and unit-tested; the thread is a thin,
defensive wrapper (mirrors app._schedule_notes). For production, where an
in-process ticker dies with the process, POST /tasks/run-due drives the same
tick_once from an external cron (see USER_TO_DO).

Times are business-local (config.app_tz / FIRSTBACK_TZ); stored send_at is UTC ISO.
Idempotent: a row is claimed (pending -> sent) atomically before the send, so a
second tick or a restart mid-send can't double-send. One follow-up per lead, ever.
Honest: when Twilio isn't configured the text is simulated onto the lead's thread,
never reported as really sent.
"""
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import db
import messaging
from config import (app_tz, REMINDER_LEAD_HOURS, FOLLOWUP_IDLE_HOURS, TICK_SECONDS,
                    QUIET_START, QUIET_END)

_PLACEHOLDER_NAMES = {"", "new caller", "homeowner", "unknown", "the caller", "caller"}


def _first_name(name):
    n = (name or "").strip()
    if n.lower() in _PLACEHOLDER_NAMES:
        return "there"
    return n.split()[0]


def when_phrase(day_iso, slot_time):
    """'2026-06-15' + '14:00' -> 'Mon Jun 15 at 2:00 PM' (no dashes, per voice)."""
    try:
        d = datetime.strptime(day_iso, "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    out = f"{d.strftime('%a %b ')}{d.day}"
    try:
        hh, mm = (int(x) for x in (slot_time or "").split(":"))
        out += f" at {hh % 12 or 12}:{mm:02d} {'PM' if hh >= 12 else 'AM'}"
    except (ValueError, AttributeError):
        pass
    return out


# ---- Copy (pure) ----
def reminder_body(name, business_name, when):
    return (f"Hi {_first_name(name)}, this is {business_name}. A friendly reminder of "
            f"your free estimate {when}. We look forward to seeing you, and you can "
            "reply here if anything has changed.")


def followup_body(name, business_name):
    return (f"Hi {_first_name(name)}, this is {business_name} following up on your "
            "project. Would you like to set up your free estimate? We are happy to "
            "find a time that works for you.")


# ---- Scheduling math (pure) ----
def _local(day_iso, slot_time, tz):
    y, m, d = (int(x) for x in day_iso.split("-"))
    hh, mm = (int(x) for x in (slot_time or "00:00").split(":"))
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def next_send_time(dt_local, quiet_start, quiet_end):
    """Shift a local send time into the next allowed window [quiet_start, quiet_end)."""
    if dt_local.hour < quiet_start:
        return dt_local.replace(hour=int(quiet_start), minute=0, second=0, microsecond=0)
    if dt_local.hour >= quiet_end:
        nxt = dt_local + timedelta(days=1)
        return nxt.replace(hour=int(quiet_start), minute=0, second=0, microsecond=0)
    return dt_local


def compute_send_at(day_iso, slot_time, lead_hours, tz, quiet_start, quiet_end):
    """UTC ISO time to send a reminder: the estimate's local time minus lead_hours,
    deferred out of quiet hours, never at/after the estimate itself."""
    appt = _local(day_iso, slot_time, tz)
    target = next_send_time(appt - timedelta(hours=lead_hours), quiet_start, quiet_end)
    if target >= appt:
        target = appt - timedelta(minutes=5)
    return target.astimezone(timezone.utc).isoformat()


def due_followup_leads(rows, now_iso_str, idle_hours):
    """Pure filter: which warm-lead rows are cold enough to nudge (not already
    nudged, has a phone, last message older than idle_hours)."""
    try:
        cutoff = datetime.fromisoformat(now_iso_str) - timedelta(hours=idle_hours)
    except (TypeError, ValueError):
        return []
    out = []
    for r in rows:
        if r.get("has_followup") or not (r.get("phone") or "").strip():
            continue
        try:
            if datetime.fromisoformat(r["last_msg_at"]) <= cutoff:
                out.append(r)
        except (TypeError, ValueError, KeyError):
            continue
    return out


# ---- Prefs ----
def reminders_on(business):
    v = (business or {}).get("reminders_enabled")
    return True if v is None else bool(v)


def followups_on(business):
    v = (business or {}).get("followups_enabled")
    return True if v is None else bool(v)


def _lead_hours(business):
    v = (business or {}).get("reminder_lead_hours")
    try:
        return float(v) if v not in (None, "") else float(REMINDER_LEAD_HOURS)
    except (TypeError, ValueError):
        return float(REMINDER_LEAD_HOURS)


# ---- Enqueue (called on booking; cheap: pure math + a couple of writes) ----
def enqueue_reminder(business, lead, day_iso, slot_time):
    """Queue the pre-estimate reminder for a just-booked appointment. Returns a
    status dict; a safe no-op when reminders are off, the lead has no phone, the
    estimate is already in the past, or the appointment can't be found. Cancels the
    lead's prior pending reminders first, so a reschedule replaces, not duplicates."""
    if not reminders_on(business):
        return {"status": "disabled"}
    phone = (lead.get("phone") or "").strip()
    if not phone:
        return {"status": "skipped", "reason": "no phone"}
    appt = db.find_appointment(business["id"], lead["id"], day_iso, slot_time)
    if not appt:
        return {"status": "skipped", "reason": "appointment not found"}
    tz = app_tz()
    if _local(day_iso, slot_time, tz) <= datetime.now(tz):
        return {"status": "skipped", "reason": "estimate already passed"}
    send_at = compute_send_at(day_iso, slot_time, _lead_hours(business), tz,
                              QUIET_START, QUIET_END)
    body = reminder_body(lead.get("name"), business.get("name") or "your contractor",
                         when_phrase(day_iso, slot_time))
    db.cancel_lead_pending_reminders(lead["id"])
    db.add_scheduled_message(business["id"], lead["id"], appt["id"], "reminder",
                             send_at, body)
    return {"status": "queued", "send_at": send_at}


# ---- The scheduler tick ----
def _appt_passed(day_iso, slot_time):
    tz = app_tz()
    try:
        return _local(day_iso, slot_time, tz) <= datetime.now(tz)
    except (TypeError, ValueError):
        return False


def run_due_once(now=None):
    """Send every scheduled message that's due. Idempotent + defensive. Returns the
    count actually sent (or simulated)."""
    now = now or db.now_iso()
    sent = 0
    biz_cache = {}
    for row in db.due_scheduled_messages(now):
        if not db.claim_scheduled_message(row["id"]):
            continue  # another tick already claimed it
        try:
            if row["kind"] == "reminder":
                if row.get("appt_status") and row["appt_status"] != "booked":
                    db.mark_scheduled(row["id"], "canceled")
                    continue
                if row.get("appt_day") and _appt_passed(row["appt_day"], row.get("appt_slot")):
                    db.mark_scheduled(row["id"], "skipped")  # estimate already started
                    continue
            phone = (row.get("lead_phone") or "").strip()
            if not phone:
                db.mark_scheduled(row["id"], "skipped")
                continue
            biz = biz_cache.get(row["business_id"]) or db.get_business(row["business_id"])
            biz_cache[row["business_id"]] = biz
            res = messaging.send_sms(biz, phone, row["body"], lead_id=row["lead_id"])
            status = res.get("status")
            if status == "simulated":
                # Honest: Twilio is off, nothing really went out -> don't leave the
                # claim's 'sent'. The simulated text still shows on the lead's thread.
                db.mark_scheduled(row["id"], "simulated")
                sent += 1
            elif status == "sent":
                sent += 1  # row already 'sent' from the claim
            else:
                db.mark_scheduled(row["id"], "failed")
        except Exception as e:
            db.mark_scheduled(row["id"], "failed")
            print(f"[firstback] scheduled send failed (id {row['id']}): {e}",
                  file=sys.stderr, flush=True)
    return sent


def scan_followups(now=None):
    """Queue ONE follow-up for each warm lead that just went cold. Returns the count
    queued."""
    now = now or db.now_iso()
    queued = 0
    try:
        now_local = datetime.fromisoformat(now).astimezone(app_tz())
    except (TypeError, ValueError):
        now_local = datetime.now(app_tz())
    send_at = next_send_time(now_local, QUIET_START, QUIET_END).astimezone(timezone.utc).isoformat()
    for biz in db.list_businesses():
        if not followups_on(biz):
            continue
        rows = db.followup_candidate_rows(biz["id"])
        for lead in due_followup_leads(rows, now, FOLLOWUP_IDLE_HOURS):
            try:
                body = followup_body(lead.get("name"), biz.get("name") or "your contractor")
                db.add_scheduled_message(biz["id"], lead["id"], None, "followup",
                                         send_at, body)
                queued += 1
            except Exception as e:
                print(f"[firstback] followup enqueue failed (lead {lead.get('id')}): {e}",
                      file=sys.stderr, flush=True)
    return queued


def tick_once(now=None):
    """One scheduler pass: refresh caller-triage suggestions, queue follow-ups, then
    send everything due."""
    now = now or db.now_iso()
    try:
        import triage
        triage.scan_all_suggestions()  # observe callers -> refresh the review queue
    except Exception as e:
        print(f"[firstback] suggestion scan failed: {e}", file=sys.stderr, flush=True)
    queued = scan_followups(now)
    # Phase 3: enqueue due growth touches (opt-in per business; sent by run_due_once below
    # through the same gate, simulated until Twilio + A2P are live).
    growth_queued = 0
    try:
        import growth
        growth_queued = growth.scan(now).get("queued", 0)
    except Exception as e:
        print(f"[firstback] growth scan failed: {e}", file=sys.stderr, flush=True)
    sent = run_due_once(now)
    return {"queued": queued, "growth_queued": growth_queued, "sent": sent}


# ---- The ticker thread ----
_ticker_started = False
_ticker_lock = threading.Lock()


def start_ticker():
    """Launch the background scheduler once (safe no-op if already running). It's
    in-process and dies with the process -- for resilient prod, also drive
    POST /tasks/run-due from an external cron (see USER_TO_DO)."""
    global _ticker_started
    with _ticker_lock:
        if _ticker_started:
            return
        _ticker_started = True
    interval = max(5, int(TICK_SECONDS))

    def _loop():
        # Sleep BEFORE the first tick. tick_once() touches the DB and may send due
        # reminders/follow-ups (a slow/hanging Twilio call), holding the DB; doing that
        # at boot can block the web worker's first request (the /login health check reads
        # the DB via inject_globals), so Render's port scan never sees the worker and the
        # deploy fails ("No open HTTP ports"). Delaying the first tick lets the worker
        # become ready and answer the health check first. See reference-firstback-wal-boot-hazard.
        while True:
            time.sleep(interval)
            try:
                tick_once()
            except Exception as e:  # never let the scheduler thread die
                print(f"[firstback] scheduler tick failed: {e}", file=sys.stderr, flush=True)

    threading.Thread(target=_loop, daemon=True, name="firstback-ticker").start()
    print(f"[firstback] reminder scheduler started (every {interval}s)",
          file=sys.stderr, flush=True)
