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

ALERT_KINDS = ("lead", "booking", "urgent", "canceled")
# Collapse identical alerts (same business + event) within this many seconds.
ALERT_DEDUPE_SECONDS = 120

# A cancellation rides the same toggle as a booking (both are "your calendar changed").
_TOGGLE_COL = {"lead": "alert_on_lead", "booking": "alert_on_booking",
               "urgent": "alert_on_urgent", "canceled": "alert_on_booking"}
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
        return f"Estimate booked: {who} for {when}.{tail}".rstrip()
    if kind == "urgent":
        return f"Urgent: {who}{tail} needs attention. Open FirstBack."
    if kind == "canceled":
        when = (context.get("when") or "").strip()
        return f"Estimate canceled: {who}{(' for ' + when) if when else ''}.{tail}".rstrip()
    return f"FirstBack alert ({kind})."


def _subject(kind):
    return {"lead": "New lead — FirstBack",
            "booking": "Estimate booked — FirstBack",
            "urgent": "Urgent lead — FirstBack",
            "canceled": "Estimate canceled — FirstBack"}.get(kind, "FirstBack alert")


def _enabled_for(business, kind):
    """Whether the owner wants alerts of this kind (default ON when unset)."""
    val = (business or {}).get(_TOGGLE_COL[kind])
    return True if val is None else bool(val)


def _dedupe_key(kind, context):
    """A stable key per event so a double-trigger collapses but distinct events
    (a different lead, a re-book to a new time) still each alert."""
    base = f"{kind}:{context.get('lead_id')}"
    return base + (f":{context.get('when')}" if kind in ("booking", "canceled") else "")


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
