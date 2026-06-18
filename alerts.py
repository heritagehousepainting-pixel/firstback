"""Owner alerts for FirstBack: notify the contractor the moment a lead arrives, an
estimate books, or a lead is flagged urgent -- so they can trust it while they
work instead of watching the dashboard.

Each alert fans out to the business's enabled channels:
  * In-app  -- always recorded to the `alerts` table; shown on the dashboard.
  * SMS     -- via messaging.send_sms (real Twilio if configured, else simulated).
  * Email   -- via mail.send_email (real SMTP if configured, else simulated).

Design (mirrors the rest of the codebase):
  * Off the hot path: notify_async runs on a daemon thread, like
    app._schedule_notes, so a slow SMS/SMTP call never delays the customer's
    text-back. Failures are swallowed and logged, never crashing the worker.
  * Multi-tenant: every channel + row is scoped by business_id.
  * De-duped: identical alerts for the same event collapse within a short window
    (idempotent against double-triggers and restarts).
  * Honest: when a channel isn't configured the send is simulated, not faked;
    the dashboard's in-app feed always reflects what really happened.

The copy/decision helpers (format_message, _enabled_for, _dedupe_key) are pure
and unit-tested without network or DB.
"""
import sys
import threading

import db
import mail
import messaging

ALERT_KINDS = ("lead", "booking", "urgent", "canceled", "sms_fail", "forwarding_lost",
               "roi_milestone", "vic_morning", "vic_stall")
# Collapse identical alerts (same business + event) within this many seconds.
ALERT_DEDUPE_SECONDS = 120

# A cancellation rides the same toggle as a booking (both are "your calendar changed").
# sms_fail and forwarding_lost ride the urgent toggle (operational alerts the owner needs).
_TOGGLE_COL = {"lead": "alert_on_lead", "booking": "alert_on_booking",
               "urgent": "alert_on_urgent", "canceled": "alert_on_booking",
               "sms_fail": "alert_on_urgent", "forwarding_lost": "alert_on_urgent",
               "roi_milestone": "alert_on_roi_milestone",
               # Proactive push kinds (ALPHA): map to the lead-alert toggle so they respect
               # the owner's existing preference without requiring a new DB column.
               "vic_morning": "alert_on_lead", "vic_stall": "alert_on_lead"}
_PLACEHOLDER_NAMES = {"", "new caller", "homeowner", "unknown", "the caller", "caller"}


def _who(context):
    name = (context.get("name") or "").strip()
    return name if name.lower() not in _PLACEHOLDER_NAMES else "a new caller"


def format_message(kind, context):
    """The short, actionable owner-facing copy for an alert. Pure + unit-tested."""
    who = _who(context)
    phone = (context.get("phone") or "").strip()
    tail = f" {phone}" if phone else ""
    if kind == "lead":
        proj = (context.get("project") or "").strip()
        about = f' about "{proj}"' if proj else ""
        return f"New lead: {who}{tail}{about}. Open FirstBack to reply."
    if kind == "booking":
        when = (context.get("when") or "").strip()
        base = f"Estimate booked: {who} for {when}.{tail}".rstrip()
        # Show-Up-Prepared briefing: append job details when the lead has them.
        address = (context.get("address") or "").strip()
        project = (context.get("project") or "").strip()
        summary = (context.get("summary") or "").strip()
        parts = [p for p in (project, address, summary) if p]
        if parts:
            base += " Job: " + " - ".join(parts)
        return base
    if kind == "roi_milestone":
        return context.get("body") or "FirstBack alert (roi_milestone)."
    if kind == "urgent":
        return f"Urgent: {who}{tail} needs attention. Open FirstBack."
    if kind == "canceled":
        when = (context.get("when") or "").strip()
        return f"Estimate canceled: {who}{(' for ' + when) if when else ''}.{tail}".rstrip()
    if kind == "sms_fail":
        attempts = context.get("attempts", 3)
        return (f"SMS delivery failed after {attempts} attempts to {who}{tail}. "
                f"Check FirstBack for details.")
    if kind == "forwarding_lost":
        return (f"Call forwarding may be broken for your FirstBack number. "
                f"Open FirstBack to re-verify.{tail}").rstrip()
    if kind == "vic_morning":
        # Honest digest: tell the owner what's waiting, direct them to the app.
        # NEVER "tap to send" -- the in-app briefing chip is where the one-tap lives.
        n = context.get("n", 0)
        money = (context.get("money") or "").strip()
        hottest = (context.get("hottest") or "").strip()
        money_part = f", ~{money} on the table" if money else ""
        hottest_part = f" {hottest}." if hottest else ""
        lead_word = "lead" if n == 1 else "leads"
        return f"{n} {lead_word} need you{money_part}.{hottest_part} Open FirstBack."
    if kind == "vic_stall":
        name = (context.get("name") or "them").strip()
        idle_h = context.get("idle_hours", 0)
        try:
            idle_h = float(idle_h)
        except (TypeError, ValueError):
            idle_h = 0
        hours_label = f"{int(round(idle_h))}h"
        money = (context.get("money") or "").strip()
        money_part = f" -- ~{money} on the table" if money else ""
        # >48h -> add urgency signal; honest, never alarmist.
        urgency = " They may be shopping around." if idle_h > 48 else ""
        return (f"{name} replied {hours_label} ago and is still waiting{money_part}."
                f"{urgency} Open FirstBack to text them back.")
    return f"FirstBack alert ({kind})."


def _subject(kind):
    return {"lead": "New lead — FirstBack",
            "booking": "Estimate booked — FirstBack",
            "urgent": "Urgent lead — FirstBack",
            "canceled": "Estimate canceled — FirstBack",
            "sms_fail": "SMS delivery failed — FirstBack",
            "forwarding_lost": "Call forwarding issue — FirstBack",
            "roi_milestone": "FirstBack paid for itself — FirstBack",
            "vic_morning": "Your morning briefing — FirstBack",
            "vic_stall": "Lead still waiting — FirstBack"}.get(kind, "FirstBack alert")


def _enabled_for(business, kind):
    """Whether the owner wants alerts of this kind (default ON when unset)."""
    val = (business or {}).get(_TOGGLE_COL[kind])
    return True if val is None else bool(val)


def _dedupe_key(kind, context):
    """A stable key per event so a double-trigger collapses but distinct events
    (a different lead, a re-book to a new time) still each alert.

    vic_morning: day-stamped per business (one per local calendar day).
    vic_stall:   day-stamped per (lead, local calendar day) so each lead is
                 nudged at most once per day, not per window."""
    if kind == "vic_morning":
        day = (context.get("local_day") or "").strip()
        return f"vic_morning:{day}"
    if kind == "vic_stall":
        lead_id = context.get("lead_id", "")
        day = (context.get("local_day") or "").strip()
        return f"vic_stall:{lead_id}:{day}"
    base = f"{kind}:{context.get('lead_id')}"
    return base + (f":{context.get('when')}" if kind in ("booking", "canceled") else "")


def _briefing_tail(business):
    """Return the one-line briefing headline to append to lead/booking alert bodies,
    or an empty string when the briefing is quiet/empty or on any exception.

    Lazy-imports assistant to avoid circular-import issues at module load. This is a
    best-effort enrichment -- it must NEVER break an alert. Returns '' on any failure."""
    try:
        import assistant as _assistant  # lazy: avoids circular import at module level
        card = _assistant.briefing(business)
        tone = card.get("tone", "")
        items = card.get("items") or []
        headline = (card.get("headline") or "").strip()
        if tone != "active" or not items or not headline:
            return ""
        return headline
    except Exception:
        return ""


# Serializes the dedupe check + in-app claim across concurrent notify_async daemon
# threads (all in-process), so one event can't be alerted twice under a burst.
_dedupe_lock = threading.Lock()


def notify(business, kind, context):
    """Fan an alert out to the business's enabled channels. Safe to call from a
    background thread. Returns the list of (channel, status) actually attempted
    (empty when the kind is disabled or de-duped)."""
    if kind not in ALERT_KINDS or not isinstance(business, dict):
        return []
    bid = business.get("id")
    if bid is None or not _enabled_for(business, kind):
        return []
    dedupe = _dedupe_key(kind, context)
    body = format_message(kind, context)
    # P1-1: for lead and booking alerts, append a one-line briefing headline so the
    # owner sees the pipeline state on the same lock-screen notification. Cap the full
    # body at 320 chars; the tail is truncated first (core event line always survives).
    if kind in ("lead", "booking"):
        tail = _briefing_tail(business)
        if tail:
            candidate = body + " " + tail
            if len(candidate) <= 320:
                body = candidate
            elif len(body) <= 320:
                # Fit as much of the tail as possible without exceeding the cap.
                room = 320 - len(body) - 1   # -1 for the space
                if room > 5:
                    body = body + " " + tail[:room]
    attempted = []
    # Claim the event atomically: the dedupe check + the in-app insert run under a
    # process lock so two concurrent notify_async threads for the SAME event can't
    # both pass the check and double-alert. The in-app row is the audit trail AND
    # the claim. (The slow SMS/email sends below stay OUTSIDE the lock.)
    with _dedupe_lock:
        if db.alert_recent(bid, dedupe, ALERT_DEDUPE_SECONDS):
            return []  # already alerted for this event recently
        db.add_alert(bid, kind, "inapp", "", "recorded", dedupe, body)
    attempted.append(("inapp", "recorded"))
    # SMS to the owner's cell. No lead_id: this goes to the OWNER, not onto the
    # customer's conversation thread.
    sms_to = (business.get("alert_sms") or "").strip()
    if sms_to:
        # Owner-facing alert: goes to the contractor's OWN cell, not a consumer, so
        # it's exempt from the A2P 10DLC customer-traffic gate.
        res = messaging.send_sms(business, sms_to, body, gate=False)
        status = res.get("status", "?")
        db.add_alert(bid, kind, "sms", sms_to, status, dedupe, body)
        attempted.append(("sms", status))
    # Email -- defaults to the owner's login email when not overridden in Settings.
    email_to = (business.get("alert_email") or "").strip() or db.owner_email(bid)
    if email_to:
        res = mail.send_email(email_to, _subject(kind), body)
        status = res.get("status", "?")
        db.add_alert(bid, kind, "email", email_to, status, dedupe, body)
        attempted.append(("email", status))
    return attempted


def notify_async(business, kind, context):
    """Fire-and-forget notify on a daemon thread so the request path never blocks
    on a slow SMS/SMTP call. Mirrors google_cal.create_event_async."""
    threading.Thread(target=_safe_notify, args=(business, kind, context),
                     daemon=True).start()


def _safe_notify(business, kind, context):
    try:
        notify(business, kind, context)
    except Exception as e:  # never let an alert failure crash the worker thread
        print(f"[firstback] alert notify failed ({kind}): {e}",
              file=sys.stderr, flush=True)
