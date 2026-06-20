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
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import db
import alerts
import messaging
from config import (app_tz, REMINDER_LEAD_HOURS, FOLLOWUP_IDLE_HOURS, TICK_SECONDS,
                    QUIET_START, QUIET_END, SCREEN_GRADUATION_DAYS, SCREEN_GRADUATION_MIN_VERDICTS,
                    SCREEN_MODE)

# SF-5: per-business timezone helper (Agent 1 defines biz_tz in config.py;
# we import lazily so tests can monkeypatch before the real function exists).
def _biz_tz(business):
    """Resolve a business dict (or id int) to a tzinfo using config.biz_tz when
    available, falling back to config.app_tz() so the module still works without A1."""
    try:
        from config import biz_tz as _cfg_biz_tz
        return _cfg_biz_tz(business)
    except (ImportError, AttributeError):
        return app_tz()


def _int_pref(business, key, default):
    """Coerce an owner-pref column to int with a fallback (never raises). Treats a real 0
    as 0 (e.g. 'mute' for the stall cap) -- only None/junk falls back. Duplicated from
    alerts._int_pref to avoid an alerts<->reminders circular import."""
    val = (business or {}).get(key)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


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
def reminder_body(name, business_name, when, phone=None):
    # Batch B: warmer, plainer, and gives a direct number so a day-of question doesn't have
    # to route back through the AI. phone is optional -> degrades gracefully when unset.
    phone_line = f" Questions or need to reschedule? Call us at {phone} or just reply here." \
        if phone else " Questions or need to reschedule? Just reply here."
    return (f"Hi {_first_name(name)}! Quick reminder: {business_name} is coming {when} for "
            f"your free estimate.{phone_line}")


def followup_body(name, business_name, phone=None):
    contact = f" Call or text us at {phone}." if phone else " Just reply here."
    return (f"Hi {_first_name(name)}, {business_name} here, still happy to get you a free "
            f"estimate.{contact} What day works best?")


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
    nudged, has a phone, last message older than idle_hours). Phase 5e S1: also
    skips rows where has_followup_2 is truthy (Touch-2 already queued — no further
    touches for this lead in this cycle)."""
    try:
        cutoff = datetime.fromisoformat(now_iso_str) - timedelta(hours=idle_hours)
    except (TypeError, ValueError):
        return []
    out = []
    for r in rows:
        if r.get("has_followup") or r.get("has_followup_2") or not (r.get("phone") or "").strip():
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
    tz = _biz_tz(business)  # SF-5: per-business timezone
    if _local(day_iso, slot_time, tz) <= datetime.now(tz):
        return {"status": "skipped", "reason": "estimate already passed"}
    send_at = compute_send_at(day_iso, slot_time, _lead_hours(business), tz,
                              QUIET_START, QUIET_END)
    body = reminder_body(lead.get("name"), business.get("name") or "your contractor",
                         when_phrase(day_iso, slot_time), phone=business.get("phone") or None)
    db.cancel_lead_pending_reminders(lead["id"])
    db.add_scheduled_message(business["id"], lead["id"], appt["id"], "reminder",
                             send_at, body)
    return {"status": "queued", "send_at": send_at}


# ---- F05: Morning-of reminder ----
def enqueue_morning_reminder(business, lead, day_iso, slot_time):
    """Queue an 8 AM local reminder on the morning of the estimate day.

    Skips when:
    - The estimate starts before 10:00 AM (morning IS the estimate; too close).
    - 8 AM that morning is already in the past.
    - A morning_reminder for this lead+appointment already exists (dedupe via
      db.find_scheduled_message).
    Returns a status dict."""
    if not reminders_on(business):
        return {"status": "disabled"}
    phone = (lead.get("phone") or "").strip()
    if not phone:
        return {"status": "skipped", "reason": "no phone"}
    appt = db.find_appointment(business["id"], lead["id"], day_iso, slot_time)
    if not appt:
        return {"status": "skipped", "reason": "appointment not found"}
    # Skip if estimate is before 10:00 AM (morning reminder would be too close or after).
    try:
        slot_hh = int((slot_time or "00:00").split(":")[0])
    except (ValueError, AttributeError):
        slot_hh = 0
    if slot_hh < 10:
        return {"status": "skipped", "reason": "estimate before 10am"}
    tz = _biz_tz(business)
    # Build 8 AM local on the estimate day.
    try:
        y, mo, d = (int(x) for x in day_iso.split("-"))
        morning_local = datetime(y, mo, d, 8, 0, tzinfo=tz)
    except (TypeError, ValueError) as e:
        return {"status": "skipped", "reason": f"bad day_iso: {e}"}
    # Skip if 8 AM has already passed.
    if morning_local <= datetime.now(tz):
        return {"status": "skipped", "reason": "morning already past"}
    # Dedupe: skip if a morning_reminder row already exists for this lead+appt.
    try:
        existing = db.find_scheduled_message(business["id"], lead["id"], "morning_reminder")
        if existing:
            return {"status": "skipped", "reason": "already queued"}
    except Exception:
        pass  # db.find_scheduled_message not yet available (A1); safe no-op
    send_at = morning_local.astimezone(timezone.utc).isoformat()
    body = reminder_body(lead.get("name"), business.get("name") or "your contractor",
                         when_phrase(day_iso, slot_time), phone=business.get("phone") or None)
    db.add_scheduled_message(business["id"], lead["id"], appt["id"], "morning_reminder",
                             send_at, body)
    return {"status": "queued", "send_at": send_at}


# ---- F05: RSVP classification ----
def classify_rsvp(text):
    """Classify a reply as 'yes', 'no', or 'unknown' for RSVP purposes.
    'yes' = confirmed attendance; 'no' = declining/canceling; 'unknown' = unclear.
    Returns one of those three strings. Fails open to 'unknown'."""
    t = (text or "").lower().strip()
    if not t:
        return "unknown"
    # Negation check first (so "no I can't" beats any accidental affirmative words).
    _neg = re.compile(
        r"\b(?:no\b|nope|nah|can'?t|cannot|won'?t|not|don'?t|unable|cancel|"
        r"reschedule|skip|won't|not able|can not|have to cancel|need to cancel|"
        r"not going to|not coming|something came up|can.t make it|won.t be|"
        r"not available|unavailable)\b")
    _yes = re.compile(
        r"\b(?:yes\b|yeah|yep|yup|sure|confirm(?:ed)?|confirmed|on my way|"
        r"see you|be there|i.ll be|i will be|we.ll be|we will be|absolutely|"
        r"definitely|for sure|ok\b|okay|sounds good|all set|set|ready|"
        r"still on|still good|looking forward|can'?t wait)\b")
    if _neg.search(t):
        # "yes" words present too? Negation wins (e.g. "yes I need to cancel").
        return "no"
    if _yes.search(t):
        return "yes"
    return "unknown"


# ---- The scheduler tick ----
def _appt_passed(day_iso, slot_time, business=None):
    """True when the appointment datetime is in the past. Accepts optional
    business (dict or int) for per-business timezone (SF-5); falls back to
    app_tz() when omitted (backward compat)."""
    tz = _biz_tz(business) if business is not None else app_tz()
    try:
        return _local(day_iso, slot_time, tz) <= datetime.now(tz)
    except (TypeError, ValueError):
        return False


def _enqueue_retry(biz, row, attempt):
    """SF-4: schedule an async retry row for a failed SMS. Backoff: 30s/2m/10m.
    At attempt >3, fire an sms_fail owner alert instead. NEVER sync-retries."""
    backoff = {1: 30, 2: 120, 3: 600}
    try:
        if attempt <= 3:
            delay_s = backoff.get(attempt, 600)
            from datetime import timezone as _tz
            send_at = (datetime.now(_tz.utc) + timedelta(seconds=delay_s)).isoformat()
            db.queue_sms_retry(
                row["business_id"],
                row.get("lead_id"),
                row.get("lead_phone", ""),
                row["body"],
                attempt,
                send_at,
            )
        else:
            try:
                import alerts
                alerts.notify_async(biz, "sms_fail", {
                    "lead_id": row.get("lead_id"),
                    "message_id": row["id"],
                })
            except Exception as ae:
                print(f"[firstback] sms_fail alert failed: {ae}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[firstback] enqueue_retry failed (id {row['id']}): {e}",
              file=sys.stderr, flush=True)


# 5d BETA B3: Growth kinds whose delivery should be logged for frequency-cap tracking.
GROWTH_KINDS = {"review_request", "quote_followup", "reactivation",
                "winback", "referral", "membership",
                # 07-5: seasonal blast must log touches so its 28-day/30-day caps work.
                "seasonal"}

def run_due_once(now=None):
    """Send every scheduled message that's due. Idempotent + defensive. Returns the
    count actually sent (or simulated). Handles kinds: reminder, morning_reminder,
    followup, sms_retry (SF-4), and any other kind (best-effort send)."""
    now = now or db.now_iso()
    sent = 0
    biz_cache = {}
    for row in db.due_scheduled_messages(now):
        if not db.claim_scheduled_message(row["id"]):
            continue  # another tick already claimed it
        try:
            kind = row.get("kind", "")
            # reminder + morning_reminder: skip if appointment is gone or passed.
            if kind in ("reminder", "morning_reminder"):
                if row.get("appt_status") and row["appt_status"] != "booked":
                    db.mark_scheduled(row["id"], "canceled")
                    continue
                biz_for_tz = biz_cache.get(row["business_id"]) or db.get_business(row["business_id"])
                biz_cache[row["business_id"]] = biz_for_tz
                if row.get("appt_day") and _appt_passed(
                        row["appt_day"], row.get("appt_slot"), biz_for_tz):
                    db.mark_scheduled(row["id"], "skipped")  # estimate already started
                    continue
            phone = (row.get("lead_phone") or "").strip()
            if not phone:
                db.mark_scheduled(row["id"], "skipped")
                continue
            biz = biz_cache.get(row["business_id"]) or db.get_business(row["business_id"])
            biz_cache[row["business_id"]] = biz
            # S3: Re-check lead status just before send for followup kinds -- a lead
            # that books AFTER queuing but BEFORE firing must never receive a follow-up.
            if kind in ("followup", "followup_2"):
                _live_lead = db.get_lead(row["lead_id"])
                if _live_lead and _live_lead.get("status") == "booked":
                    db.mark_scheduled(row["id"], "canceled")
                    continue
            try:
                # Pre-deploy C1 (TCPA): ONLY solicited/scheduled responses (the booking
                # reminder + the morning-of reminder) are quiet-hours-exempt. EVERYTHING else
                # routed through here -- followup/followup_2 AND every growth marketing kind
                # (review_request, quote_followup, reactivation, winback, referral, membership),
                # plus any sms_retry -- must pass transactional=False to opt into the
                # quiet-hours backstop. (A growth play released by the owner at 11pm would
                # otherwise fire a marketing text at 11pm, since release doesn't reset send_at.)
                _transactional = kind in ("reminder", "morning_reminder")
                res = messaging.send_sms(biz, phone, row["body"], lead_id=row["lead_id"],
                                         transactional=_transactional)
            except Exception as send_err:
                # SF-4: synchronous send errors also go async, never sync-retry.
                print(f"[firstback] send_sms raised (id {row['id']}): {send_err}",
                      file=sys.stderr, flush=True)
                db.mark_scheduled(row["id"], "failed")
                attempt = int(row.get("retry_count") or 0) + 1
                _enqueue_retry(biz, row, attempt)
                continue
            status = res.get("status")
            if status == "simulated":
                # Honest: Twilio is off, nothing really went out -> don't leave the
                # claim's 'sent'. The simulated text still shows on the lead's thread.
                db.mark_scheduled(row["id"], "simulated")
                sent += 1
            elif status == "sent":
                sent += 1  # row already 'sent' from the claim
            else:
                # SF-4: non-sent, non-simulated = delivery failure -> async re-enqueue.
                db.mark_scheduled(row["id"], "failed")
                attempt = int(row.get("retry_count") or 0) + 1
                _enqueue_retry(biz, row, attempt)
            # 5d BETA B3: On successful delivery of a growth-kind, write to growth_touch_log
            # so the frequency cap (recent_growth_touch) reflects actual sends, not queue state.
            if status in ("sent", "simulated") and kind in GROWTH_KINDS:
                try:
                    db.add_growth_touch_log(
                        row["business_id"], row["lead_id"], kind,
                        outcome=status, source="batch_approved")
                except Exception as _gle:
                    print(f"[firstback] growth_touch_log write failed: {_gle}",
                          file=sys.stderr, flush=True)
        except Exception as e:
            db.mark_scheduled(row["id"], "failed")
            print(f"[firstback] scheduled send failed (id {row['id']}): {e}",
                  file=sys.stderr, flush=True)
    return sent


def followup_body_contextual(name, biz_name, last_in_text, phone=None):
    """Phase 5e M1: Contextual Touch-1 copy via Sonnet. Falls back to the generic
    template on any LLM failure (network, rate-limit, bad output). The fallback
    ensures the follow-up always goes out even without an API key."""
    try:
        import llm as _llm
        first = _first_name(name)
        provider = _llm.active_provider()
        system = (
            "You write short SMS follow-ups for a home-service contractor. "
            "Plain trades voice. One offer (free estimate). One ask (reply to schedule). "
            "No urgency language ('NOW', 'limited time'). No incentives or discounts. "
            "Max 130 characters. Return ONLY the SMS text, no quotes, no extra text."
        )
        user_msg = (
            f"Lead name: {first or 'customer'}. "
            f"Business: {biz_name}. "
            f"Their last message: {(last_in_text or '').strip()[:200] or '(none)'}. "
            "Write the follow-up SMS."
        )
        # Phase 6b W3: bound this call (timeout=10) so a slow Sonnet can't stall the
        # ticker / delay run_due_once. On timeout the except below falls back to the
        # generic template -- the contextual copy is a nice-to-have, never load-bearing.
        text = _llm.complete(provider, system,
                             [{"role": "user", "content": user_msg}],
                             max_tokens=80, temperature=0.7, timeout=10)
        text = (text or "").strip()[:160]
        if text:
            return text
    except Exception:
        pass
    return followup_body(name, biz_name, phone=phone)


def scan_followups(now=None):
    """Queue ONE follow-up (Touch-1) and one Touch-2 for each warm lead that just went
    cold. Returns the count of Touch-1 rows queued. Phase 5e: adds opt-out check at
    enqueue time, contextual LLM body for Touch-1, and Touch-2 (followup_2 kind) at
    Touch-1 creation time."""
    now = now or db.now_iso()
    queued = 0
    for biz in db.list_businesses():
        if not followups_on(biz):
            continue
        # SF-5: per-business timezone for followup send_at computation.
        biz_tz = _biz_tz(biz)
        try:
            now_local = datetime.fromisoformat(now).astimezone(biz_tz)
        except (TypeError, ValueError):
            now_local = datetime.now(biz_tz)
        send_at = next_send_time(now_local, QUIET_START, QUIET_END).astimezone(timezone.utc).isoformat()
        rows = db.followup_candidate_rows(biz["id"])
        for lead in due_followup_leads(rows, now, FOLLOWUP_IDLE_HOURS):
            try:
                phone = (lead.get("phone") or "").strip()
                # S2: Opt-out check at enqueue time -- don't queue for suppressed leads.
                # (messaging.send_sms would also catch it at send time, but this avoids
                # cluttering the queue with rows that will never go out.)
                if messaging.outbound_mode(biz, phone) == "suppressed":
                    continue
                biz_name = biz.get("name") or "your contractor"
                last_in_text = lead.get("last_in_text")
                # M1: Use contextual Sonnet copy; falls back to generic template.
                body = followup_body_contextual(lead.get("name"), biz_name, last_in_text,
                                                phone=biz.get("phone") or None)
                t1_id = db.add_scheduled_message(biz["id"], lead["id"], None, "followup",
                                                  send_at, body)
                if t1_id is not None:
                    queued += 1
                    # Phase 6c W6: an automated Touch-1 just went out -- cancel any PENDING
                    # quote_followup growth touch for this lead so they don't get two similar
                    # texts. Pending-only: a HELD tray play (awaiting the owner's GO) is left
                    # untouched, so this never overrides an owner decision.
                    try:
                        db.cancel_lead_growth_touches(lead["id"], ("quote_followup",))
                    except Exception as _w6e:
                        print(f"[firstback] W6 quote_followup exclusion failed "
                              f"(lead {lead.get('id')}): {_w6e}", file=sys.stderr, flush=True)
                    # S5: Queue Touch-2 immediately -- 5 days after Touch-1, quiet-hours
                    # deferred. Import at call site per spec (do NOT edit growth.py).
                    if not lead.get("has_followup_2"):
                        try:
                            from growth import _copy_reactivation
                            t1_dt = datetime.fromisoformat(send_at.replace("Z", "+00:00"))
                            t2_local = (t1_dt + timedelta(days=5)).astimezone(biz_tz)
                            t2_send_at = next_send_time(t2_local, QUIET_START, QUIET_END).astimezone(timezone.utc).isoformat()
                            first = _first_name(lead.get("name"))
                            t2_body = _copy_reactivation(first, biz)
                            db.add_scheduled_message(biz["id"], lead["id"], None, "followup_2",
                                                      t2_send_at, t2_body)
                        except Exception as t2e:
                            print(f"[firstback] followup_2 enqueue failed (lead {lead.get('id')}): {t2e}",
                                  file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[firstback] followup enqueue failed (lead {lead.get('id')}): {e}",
                      file=sys.stderr, flush=True)
    return queued


def scan_morning_briefing(now=None):
    """P1-2: fire ONE morning digest SMS per business per local day when local hour
    is in [7, 10) and the briefing has actionable items (tone=='active', items non-empty).
    Dedupes via alerts.notify's own dedupe key (vic_morning + local YYYY-MM-DD), so a
    restart or double-tick can never double-send. Returns the count of digests fired."""
    now = now or db.now_iso()
    fired = 0
    for biz in db.list_businesses():
        try:
            tz = _biz_tz(biz)
            try:
                now_local = datetime.fromisoformat(now).astimezone(tz)
            except (TypeError, ValueError):
                now_local = datetime.now(tz)
            # Only fire in the [7, 10) morning window.
            if not (7 <= now_local.hour < 10):
                continue
            local_day = now_local.strftime("%Y-%m-%d")
            # Lazy-import assistant here (ALPHA reads it read-only).
            try:
                import assistant as _assistant
                card = _assistant.briefing(biz)
            except Exception:
                continue
            tone = card.get("tone", "")
            items = card.get("items") or []
            if tone != "active" or not items:
                continue
            # Build a compact, honest body. No "tap to send".
            headline = (card.get("headline") or "").strip()
            hottest = ""
            for it in items:
                title = (it.get("title") or "").strip()
                if title:
                    hottest = title
                    break
            # Extract count and money from the headline if available.
            import re as _re
            n_match = _re.search(r"(\d+)\s+lead", headline, _re.I)
            n = int(n_match.group(1)) if n_match else len(items)
            money_match = _re.search(r"~?\$[\d,]+", headline)
            money = money_match.group(0) if money_match else ""
            ctx = {
                "n": n,
                "money": money,
                "hottest": hottest,
                "local_day": local_day,
            }
            result = alerts.notify(biz, "vic_morning", ctx)
            if result:
                fired += 1
        except Exception as e:
            print(f"[firstback] morning briefing scan failed (biz {biz.get('id')}): {e}",
                  file=sys.stderr, flush=True)
    return fired



def scan_growth_tray(now=None):
    """5d BETA B2: Fire ONE 8am growth tray digest per business per local day when
    there are held growth plays awaiting approval. Deduped via alerts.notify's
    'growth_tray' kind (day-stamped 26h window). Returns count of digests fired.

    The digest goes to business["alert_sms"] (owner cell) via gate=False --
    A2P-exempt, same pattern as 5b morning digest. NEVER to a customer number."""
    now = now or db.now_iso()
    fired = 0
    for biz in db.list_businesses():
        try:
            tz = _biz_tz(biz)
            try:
                now_local = datetime.fromisoformat(now).astimezone(tz)
            except (TypeError, ValueError):
                now_local = datetime.now(tz)
            # Only fire in the [8, 9) window (8am local).
            if not (8 <= now_local.hour < 9):
                continue
            local_day = now_local.strftime("%Y-%m-%d")
            # Fetch held plays for this business.
            try:
                rows = db.list_held_messages(biz["id"])
            except Exception as e:
                print(f"[firstback] list_held_messages failed (biz {biz.get('id')}): {e}",
                      file=sys.stderr, flush=True)
                continue
            if not rows:
                continue  # nothing to send; don't wake owner with an empty digest
            # Cap at 10 per batch-size spec (F13-FINAL SS4 G10).
            rows = rows[:10]
            count = len(rows)
            # Assemble plays_summary: "1) Maria (review), 2) Carlos (win-back), ..."
            _KIND_LABEL = {
                "review_request": "review",
                "quote_followup": "follow-up",
                "reactivation": "reactivation",
                "winback": "win-back",
                "referral": "referral",
                "membership": "membership",
            }
            parts = []
            for i, r in enumerate(rows, 1):
                name = (r.get("lead_name") or "Lead").split()[0]
                label = _KIND_LABEL.get(r.get("kind", ""), r.get("kind", ""))
                parts.append(f"{i}) {name} ({label})")
            plays_summary = ", ".join(parts)
            # Money total: use growth.py's _job_value logic (import lazily).
            is_estimated = biz.get("avg_job_value") is None
            try:
                import growth as _growth
                job_val = _growth._job_value(biz)
            except Exception:
                job_val = 2000  # generic fallback
            total = job_val * count
            total_str = f"~${total:,}"
            ctx = {
                "count": count,
                "total_str": total_str,
                "plays_summary": plays_summary,
                "is_estimated": is_estimated,
                "local_day": local_day,
            }
            result = alerts.notify(biz, "growth_tray", ctx)
            if result:
                fired += 1
        except Exception as e:
            print(f"[firstback] growth tray scan failed (biz {biz.get('id')}): {e}",
                  file=sys.stderr, flush=True)
    return fired


def scan_stall_nudges(now=None):
    """P1-3: nudge the owner when a warm lead has gone quiet for >24h (one per lead
    per local day). Escalates copy at >48h. Dedupes via alerts.notify dedupe key
    (vic_stall + lead_id + local YYYY-MM-DD). Returns the count of nudges fired."""
    now = now or db.now_iso()
    fired = 0
    for biz in db.list_businesses():
        try:
            tz = _biz_tz(biz)
            try:
                now_local = datetime.fromisoformat(now).astimezone(tz)
            except (TypeError, ValueError):
                now_local = datetime.now(tz)
            local_day = now_local.strftime("%Y-%m-%d")
            # Phase 6b W2: afternoon-only. The unified 8am digest already surfaces the
            # most-urgent stall, so per-lead morning nudges would duplicate it. Skipping
            # before noon keeps the morning to ONE SMS while still catching leads that go
            # cold later in the day. The per-(lead, local-day) dedupe is unaffected.
            if now_local.hour < 12:
                continue
            # Find warm leads idle >24h (not urgent, replied, not booked).
            idle_leads = db.warm_leads_idle(biz["id"], 24)
            # Plan 05-2: cap the afternoon nudges per business so a pile of stalls can't
            # bury the one that matters. Most-idle first, so the longest-waiting lead is
            # never the one dropped; the rest still ride the 8am digest's top-stall slot.
            idle_leads.sort(key=lambda r: r.get("idle_hours", 0) or 0, reverse=True)
            cap = _int_pref(biz, "max_stall_alerts_day", 2)
            nudged = 0
            avg_val = biz.get("avg_job_value")
            for lead in idle_leads:
                if nudged >= cap:
                    break
                try:
                    name = (lead.get("name") or "").strip() or "They"
                    idle_h = lead.get("idle_hours", 0)
                    # Money hint if avg_job_value is set.
                    money = f"${int(avg_val):,}" if avg_val else ""
                    ctx = {
                        "lead_id": lead["id"],
                        "name": name,
                        "idle_hours": idle_h,
                        "money": money,
                        "local_day": local_day,
                    }
                    result = alerts.notify(biz, "vic_stall", ctx)
                    if result:
                        fired += 1
                        nudged += 1
                except Exception as e:
                    print(f"[firstback] stall nudge failed (lead {lead.get('id')}): {e}",
                          file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[firstback] stall scan failed (biz {biz.get('id')}): {e}",
                  file=sys.stderr, flush=True)
    return fired


# Kind labels for the held-play summary line in the unified digest (mirrors scan_growth_tray).
_DIGEST_KIND_LABEL = {
    "review_request": "review", "quote_followup": "follow-up",
    "reactivation": "reactivation", "winback": "win-back",
    "referral": "referral", "membership": "membership",
}


def scan_daily_digest(now=None):
    """Phase 6b W2: fire ONE unified 8am digest SMS per business per local day,
    REPLACING the separate vic_morning + growth_tray morning sends. It combines:
      (a) leads-need-you count + money (from assistant.briefing -- DB-only, no LLM),
      (b) held growth plays count + summary + 'Reply GO to send all',
      (c) the single most-urgent stalled lead.
    Goes ONLY to business['alert_sms'] (owner cell) via alerts.notify (gate=False,
    A2P-exempt). NEVER sends to a customer and NEVER releases plays (the owner's GO
    reply / in-app tap is the approval event). Deduped via the 'daily_digest' kind
    (day-stamped, 26h). Skips a business entirely when there's nothing to report.
    Returns the count of digests fired."""
    now = now or db.now_iso()
    fired = 0
    for biz in db.list_businesses():
        try:
            tz = _biz_tz(biz)
            try:
                now_local = datetime.fromisoformat(now).astimezone(tz)
            except (TypeError, ValueError):
                now_local = datetime.now(tz)
            # 8am window -- mirrors scan_growth_tray's [8, 9).
            if not (8 <= now_local.hour < 9):
                continue
            local_day = now_local.strftime("%Y-%m-%d")
            # (a) Leads-need-you from the DB-only briefing card.
            try:
                import assistant as _assistant
                card = _assistant.briefing(biz)
            except Exception:
                card = {}
            headline = (card.get("headline") or "").strip()
            import re as _re
            n_match = _re.search(r"(\d+)\s+lead", headline, _re.I)
            n_leads = int(n_match.group(1)) if n_match else 0
            money_match = _re.search(r"~?\$[\d,]+", headline)
            money = money_match.group(0).lstrip("~") if money_match else ""
            is_estimated = biz.get("avg_job_value") is None
            # (b) Held growth plays awaiting owner approval.
            try:
                held_rows = db.list_held_messages(biz["id"])[:10]
            except Exception:
                held_rows = []
            plays_count = len(held_rows)
            parts = []
            for i, r in enumerate(held_rows, 1):
                nm = (r.get("lead_name") or "Lead").split()[0]
                label = _DIGEST_KIND_LABEL.get(r.get("kind", ""), r.get("kind", ""))
                parts.append(f"{i}) {nm} ({label})")
            plays_summary = ", ".join(parts)
            # (c) Top stall: most-idle warm lead (warm_leads_idle is NOT pre-sorted).
            try:
                stalls = db.warm_leads_idle(biz["id"], 24)
                stalls.sort(key=lambda r: r.get("idle_hours", 0) or 0, reverse=True)
            except Exception:
                stalls = []
            top_stall_name = ""
            top_stall_hours = 0
            if stalls:
                raw = (stalls[0].get("name") or "").strip()
                top_stall_name = raw.split()[0] if raw else "a lead"
                top_stall_hours = stalls[0].get("idle_hours", 0)
            # Nothing to report. Default: stay silent. Plan 05-3: if the owner opted in,
            # send a brief "all clear" so they know the system is alive (set-and-forget
            # trust). Same daily_digest dedupe key -> still at most one morning text.
            if n_leads == 0 and plays_count == 0 and not top_stall_name:
                if biz.get("alert_all_clear"):
                    result = alerts.notify(biz, "daily_digest", {
                        "n_leads": 0, "money": "", "is_estimated": False,
                        "plays_count": 0, "plays_summary": "",
                        "top_stall_name": "", "top_stall_hours": 0,
                        "local_day": local_day, "all_clear": True,
                    })
                    if result:
                        fired += 1
                continue
            ctx = {
                "n_leads": n_leads,
                "money": money,
                "is_estimated": is_estimated,
                "plays_count": plays_count,
                "plays_summary": plays_summary,
                "top_stall_name": top_stall_name,
                "top_stall_hours": top_stall_hours,
                "local_day": local_day,
            }
            result = alerts.notify(biz, "daily_digest", ctx)
            if result:
                fired += 1
        except Exception as e:
            print(f"[firstback] daily digest scan failed (biz {biz.get('id')}): {e}",
                  file=sys.stderr, flush=True)
    return fired


def scan_screening_graduation(now=None):
    """Phase 5c: auto-promote businesses from monitor -> enforce after a clean
    observation window. For each business:
      - Skip unless effective mode is 'monitor' and screening_hold is not set.
      - Lazy-init NULL window_start to now() and skip this pass (clock just started).
      - Promote when: window_age >= SCREEN_GRADUATION_DAYS AND
        would_screen count (in-window) >= SCREEN_GRADUATION_MIN_VERDICTS.
      - On promote: db.promote_screening + alerts.notify("screening_graduated").
    A rescue resets window_start, so the clock restarts and the next pass won't promote
    -- this IS the safety valve (a real homeowner can never be silenced without recovery).
    Returns the count of businesses promoted this pass."""
    from datetime import datetime, timezone, timedelta
    now_dt = datetime.now(timezone.utc)
    now_str = now or now_dt.isoformat()
    promoted = 0
    for biz in db.list_businesses():
        try:
            bid = biz.get("id")
            if bid is None:
                continue
            # Effective mode: per-business override wins; falls back to config default.
            effective = (biz.get("screen_mode") or "").strip().lower()
            if effective not in ("off", "monitor", "enforce"):
                effective = SCREEN_MODE
            # Only graduate businesses in monitor mode (never off, never already enforce).
            if effective != "monitor":
                continue
            # Respect the owner's "keep in observe" hold.
            if biz.get("screening_hold"):
                continue
            # Lazy-init NULL window_start: set to now and SKIP this pass (the 7d clock
            # starts from today; we'll graduate on a future pass after 7 days elapse).
            window_start = biz.get("screening_window_start")
            if not window_start:
                conn = db.get_conn()
                conn.execute(
                    "UPDATE businesses SET screening_window_start=? WHERE id=?",
                    (now_dt.isoformat(), bid))
                conn.commit()
                conn.close()
                continue
            # Compute window age.
            try:
                ws_dt = datetime.fromisoformat(window_start)
                if ws_dt.tzinfo is None:
                    ws_dt = ws_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            age_days = (now_dt - ws_dt).total_seconds() / 86400.0
            if age_days < SCREEN_GRADUATION_DAYS:
                continue
            # Check would_screen count since window start.
            stats = db.screening_stats(bid, since=window_start)
            # Graduate on the count of would-have-blocked ROBOCALLERS only -- monitor-mode
            # screened_spam. screened_contact (known personal/vendor the bot stays out of) is
            # NOT a robocaller, so counting it would graduate prematurely + make the alert's
            # "N robocallers" dishonest.
            would_block = stats.get("would_screen_spam", 0)
            if would_block < SCREEN_GRADUATION_MIN_VERDICTS:
                continue
            # Conditions met: promote to enforce.
            db.promote_screening(bid)
            # Refresh business row so alerts reads the updated screen_mode.
            biz_updated = db.get_business(bid)
            alerts.notify(biz_updated, "screening_graduated", {"n": would_block})
            promoted += 1
        except Exception as e:
            print(f"[firstback] screening graduation failed (biz {biz.get('id')}): {e}",
                  file=sys.stderr, flush=True)
    return promoted


def google_contacts_sync_all(now=None):
    """5f: nightly per-business Google Contacts re-sync (cadence-gated once/UTC day).
    INERT if google_contacts.configured() is False -- no log, no DB write, no error.
    Returns {businesses_checked, businesses_synced, suggestions_created}."""
    import google_contacts
    if not google_contacts.configured():
        return {"businesses_checked": 0, "businesses_synced": 0, "suggestions_created": 0}
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    checked = synced = created = 0
    for biz in db.list_businesses():
        bid = biz.get("id")
        if not bid:
            continue
        checked += 1
        if not google_contacts.is_connected(bid):
            continue
        cadence_key = f"contacts_sync_date:{bid}"
        if db.get_meta(cadence_key) == today_utc:
            continue  # already ran today for this business
        try:
            result = google_contacts.sync(bid)
            db.set_meta(cadence_key, today_utc)
            synced += 1
            created += result.get("suggested", 0)
        except Exception as e:
            print(f"[firstback] contacts nightly sync failed (biz {bid}): {e}",
                  file=sys.stderr, flush=True)
    return {"businesses_checked": checked, "businesses_synced": synced, "suggestions_created": created}


def scan_google_reputation(now=None):
    """E4: poll Google Places for each business's review count + rating, then fire a
    reputation_milestone alert when the count has grown by >= 5 since the baseline. Polls
    only when review_count_updated_at is NULL or older than 28 days. Returns counts."""
    now_dt = datetime.now(timezone.utc)
    now_str = now or now_dt.isoformat()
    try:
        cutoff = datetime.fromisoformat(now_str) - timedelta(days=28)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        cutoff = now_dt - timedelta(days=28)
    polled = milestones = 0
    try:
        import reputation as _rep
    except ImportError:
        return {"polled": polled, "milestones": milestones}
    for biz in db.list_businesses():
        try:
            bid = biz.get("id")
            if bid is None:
                continue
            updated_at_str = biz.get("review_count_updated_at")
            if updated_at_str:
                try:
                    updated_dt = datetime.fromisoformat(updated_at_str)
                    if updated_dt.tzinfo is None:
                        updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                    if updated_dt >= cutoff:
                        continue
                except (TypeError, ValueError):
                    pass
            result = _rep.poll_google_reputation(bid)
            if result is None:
                continue
            polled += 1
            fresh_biz = db.get_business(bid)
            baseline = fresh_biz.get("google_review_count_baseline")
            current = fresh_biz.get("google_review_count")
            if baseline is not None and current is not None and (current - baseline) >= 5:
                alerts.notify(fresh_biz, "reputation_milestone",
                              {"baseline": baseline, "current": current,
                               "delta": current - baseline})
                milestones += 1
        except Exception as e:
            print(f"[firstback] scan_google_reputation failed (biz {biz.get('id')}): {e}",
                  file=sys.stderr, flush=True)
    return {"polled": polled, "milestones": milestones}


def scan_monthly_recap(now=None):
    """Plan 06-3: ONE monthly ROI recap SMS per business per calendar month, on day 28-31
    in the [8,9) local window -- the anti-churn touchpoint that pre-empts renewal-day
    cancellation with concrete evidence. Dedupe via db.get_meta/set_meta per-month key.
    Gates: a2p_ready + booked>=1. Screening stats ride as a SECTION (plan 08 fold-in), not
    a second SMS. Returns the count fired."""
    import compliance as _compliance
    now = now or db.now_iso()
    fired = 0
    for biz in db.list_businesses():
        try:
            tz = _biz_tz(biz)
            try:
                now_local = datetime.fromisoformat(now).astimezone(tz)
            except (TypeError, ValueError):
                now_local = datetime.now(tz)
            if now_local.day not in (28, 29, 30, 31):
                continue
            if not (8 <= now_local.hour < 9):
                continue
            bid = biz.get("id")
            if bid is None:
                continue
            ym = now_local.strftime("%Y-%m")
            dedupe_key = f"monthly_recap:{bid}:{ym}"
            if db.get_meta(dedupe_key):
                continue
            if not _compliance.a2p_ready(biz):
                continue
            try:
                a = db.analytics(bid, days=30)
            except Exception:
                continue
            booked = (a.get("totals") or {}).get("booked") or a.get("booked") or 0
            if booked < 1:
                continue
            leads = (a.get("totals") or {}).get("leads") or a.get("leads") or 0
            revenue = a.get("revenue") or 0
            roi_multiple = a.get("roi_multiple")
            avg_source = a.get("avg_source") or "industry_default"
            screening_section = ""
            try:
                sc = db.screening_monthly_stats(bid)
                n_robo = sc.get("robocalls_screened", 0)
                if n_robo:
                    word = "robocall" if n_robo == 1 else "robocalls"
                    screening_section = f"Plus {n_robo} {word} screened."
            except Exception:
                pass
            ctx = {"month": ym, "leads": leads, "booked": booked, "revenue": revenue,
                   "multiple": roi_multiple, "avg_source": avg_source,
                   "screening_section": screening_section}
            if alerts.notify(biz, "monthly_recap", ctx):
                db.set_meta(dedupe_key, now)
                fired += 1
        except Exception as e:
            print(f"[firstback] monthly recap scan failed (biz {biz.get('id')}): {e}",
                  file=sys.stderr, flush=True)
    return fired


def tick_once(now=None):
    """One scheduler pass: refresh caller-triage suggestions, queue follow-ups, then
    send everything due. Always writes a heartbeat to meta (SF-3), even on partial
    failure, so the /health/ticker endpoint can report staleness accurately."""
    now = now or db.now_iso()
    _tick_started = time.monotonic()   # Phase 6c W4: measure the pass for the soft-budget warn
    # Phase 6b: read the PREVIOUS heartbeat before overwriting it (stale-ticker detection).
    _prev_tick_utc = db.get_meta("last_tick_utc")
    # Record the heartbeat FIRST so a partial failure still timestamps the tick.
    _tick_utc = datetime.now(timezone.utc).isoformat()
    try:
        db.set_meta("last_tick_utc", _tick_utc)
    except Exception as e:
        print(f"[firstback] heartbeat write failed: {e}", file=sys.stderr, flush=True)
    # Phase 6b stale-ticker alert: if the gap since the previous tick exceeds the
    # threshold, the scheduler had an outage -- warn the operator. Because the external
    # cron drives /tasks/run-due -> tick_once, this fires on the recovery tick even when
    # the in-process thread died. (Total death, cron+thread both down, needs an external
    # uptime monitor on /health/ticker -- owner-ops, can't self-detect.) Never crashes the tick.
    try:
        if _prev_tick_utc:
            _prev_dt = datetime.fromisoformat(_prev_tick_utc)
            if _prev_dt.tzinfo is None:
                _prev_dt = _prev_dt.replace(tzinfo=timezone.utc)
            _gap_s = (datetime.now(timezone.utc) - _prev_dt).total_seconds()
            if _gap_s > 900:  # 15 min -- generous vs a 60s tick, tight enough to catch a stall
                # Plan 05-6: fan to every tenant -- each owner should know if the scheduler
                # that powers THEIR texts/reminders stalled. The day-stamped dedupe is
                # per-business (alert_recent filters by business_id), so no storm.
                _stale_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                for _biz in db.list_businesses():
                    alerts.notify(_biz, "tick_stale", {
                        "gap_minutes": round(_gap_s / 60, 1),
                        "local_day": _stale_day,
                    })
    except Exception as e:
        print(f"[firstback] tick_stale check failed: {e}", file=sys.stderr, flush=True)
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
        # Even on partial failure, the heartbeat above has already been written.
        print(f"[firstback] growth scan failed: {e}", file=sys.stderr, flush=True)
    # One-line seam (SF-7): probe forwarding health once per tick; A2 defines the function.
    try:
        import connections
        connections.check_forwarding_health()
    except Exception as e:
        print(f"[firstback] forwarding health check failed: {e}", file=sys.stderr, flush=True)
    # Phase 6b W2: ONE unified 8am digest (absorbs the old vic_morning + growth_tray
    # morning sends into a single owner SMS -- the functions remain for their unit tests
    # but the ticker no longer fires them separately).
    try:
        scan_daily_digest(now)
    except Exception as e:
        print(f"[firstback] daily digest tick failed: {e}", file=sys.stderr, flush=True)
    # Plan 06-3: monthly ROI recap (day 28-31, once per month, dedupe via meta key).
    try:
        scan_monthly_recap(now)
    except Exception as e:
        print(f"[firstback] monthly recap tick failed: {e}", file=sys.stderr, flush=True)
    # Plan 07-1: monthly Google review poll (inert without GOOGLE_PLACES_API_KEY).
    try:
        scan_google_reputation(now)
    except Exception as e:
        print(f"[firstback] google reputation tick failed: {e}", file=sys.stderr, flush=True)
    # P1-3: warm-lead stall nudges (proactive owner push; afternoon-only since 6b).
    try:
        scan_stall_nudges(now)
    except Exception as e:
        print(f"[firstback] stall nudge tick failed: {e}", file=sys.stderr, flush=True)
    # Phase 5c: screening graduation (monitor -> enforce after clean 7d window).
    try:
        scan_screening_graduation(now)
    except Exception as e:
        print(f"[firstback] screening graduation tick failed: {e}", file=sys.stderr, flush=True)
    # 5f: nightly Google Contacts re-sync (per-business, cadence-gated).
    contacts_synced = 0
    try:
        _cs = google_contacts_sync_all(now)
        contacts_synced = _cs.get("businesses_synced", 0)
    except Exception as e:
        print(f"[firstback] contacts nightly sync tick failed: {e}", file=sys.stderr, flush=True)
    sent = run_due_once(now)
    # Phase 6c W4 (observability): a tick that runs long risks delaying the next pass's
    # sends. Log a one-line warning when it exceeds a soft budget (80% of the interval,
    # min 30s) so a slow tick is VISIBLE in logs. (Staggering scans / a separate
    # contacts-sync cron / Redis rate-limits are deferred until multi-tenant scale.)
    _tick_elapsed = time.monotonic() - _tick_started
    _tick_budget = max(30, int(TICK_SECONDS * 0.8))
    if _tick_elapsed > _tick_budget:
        print(f"[firstback] WARNING: tick_once ran {_tick_elapsed:.1f}s "
              f"(soft budget {_tick_budget}s) -- scheduler may lag at higher tenant volume.",
              file=sys.stderr, flush=True)
    return {"queued": queued, "growth_queued": growth_queued, "sent": sent,
            "contacts_synced": contacts_synced}


def ticker_is_stale(max_age_s=600):
    """Return True when the ticker hasn't run within max_age_s seconds (default 10 min).
    Returns True (stale) when no heartbeat has ever been written. Used by /health/ticker."""
    raw = db.get_meta("last_tick_utc")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age > max_age_s
    except (TypeError, ValueError):
        return True


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
