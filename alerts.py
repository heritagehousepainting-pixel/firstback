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
from datetime import datetime

import db
import mail
import messaging

ALERT_KINDS = ("lead", "booking", "urgent", "canceled", "sms_fail", "forwarding_lost",
               "roi_milestone", "vic_morning", "vic_stall", "screening_graduated",
               "growth_tray", "daily_digest", "tick_stale", "a2p_approved", "monthly_recap",
               "reputation_milestone")
# Collapse identical alerts (same business + event) within this many seconds.
ALERT_DEDUPE_SECONDS = 120
# Proactive daily pushes (day-stamped keys) collapse over the whole local day, not 120s --
# the ticker fires every few minutes, so a short window would re-send them on each pass.
_DAILY_DEDUPE_SECONDS = 26 * 3600
_DAILY_DEDUPE_KINDS = ("vic_morning", "vic_stall", "growth_tray", "daily_digest", "tick_stale")
# Graduation fires once per business lifetime (the mode flips once), so dedupe
# over a very long window to prevent any edge-case re-fire.
_LONG_DEDUPE_SECONDS = 365 * 24 * 3600
_LONG_DEDUPE_KINDS = ("screening_graduated",)
# Plan 05-1: kinds that bypass owner quiet hours (fire-alarm level -- never held overnight).
_URGENT_BYPASS_KINDS = frozenset({"urgent", "sms_fail", "forwarding_lost", "tick_stale"})


def _biz_tz_for_alerts(business):
    """Resolve a business dict to tzinfo. Lazy-imports config.biz_tz to avoid a circular
    import at module load; falls back to UTC so a missing tz is always safe."""
    try:
        from config import biz_tz as _biz_tz
        return _biz_tz(business)
    except Exception:
        from datetime import timezone
        return timezone.utc


def _int_pref(business, key, default):
    """Coerce an owner-pref column to int with a fallback (never raises). Treats a real 0
    as 0 (midnight quiet-hour, or 'mute' for a cap) -- only a missing/None value or junk
    falls back to the default."""
    val = (business or {}).get(key)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

# A cancellation rides the same toggle as a booking (both are "your calendar changed").
# sms_fail and forwarding_lost ride the urgent toggle (operational alerts the owner needs).
_TOGGLE_COL = {"lead": "alert_on_lead", "booking": "alert_on_booking",
               "urgent": "alert_on_urgent", "canceled": "alert_on_booking",
               "sms_fail": "alert_on_urgent", "forwarding_lost": "alert_on_urgent",
               "roi_milestone": "alert_on_roi_milestone",
               # Proactive push kinds (ALPHA): map to the lead-alert toggle so they respect
               # the owner's existing preference without requiring a new DB column.
               "vic_morning": "alert_on_lead", "vic_stall": "alert_on_lead",
               # Graduation is an operational owner notification: no new column needed,
               # rides the urgent toggle (owner needs to know their filter went live).
               "screening_graduated": "alert_on_urgent",
               # Growth tray digest (5d BETA): rides the lead-alert toggle -- no new column needed.
               "growth_tray": "alert_on_lead",
               # Phase 6b unified 8am digest: its OWN toggle so the owner can mute the morning
               # buzz without losing real-time lead/booking alerts.
               "daily_digest": "alert_on_daily_digest",
               # Phase 6b ops alert: scheduler stalled -> rides the urgent toggle (no new column).
               "tick_stale": "alert_on_urgent",
               # Tier-0 F8: "you're live" when A2P approves -- a one-time, must-see event;
               # rides the lead toggle so the owner who wants lead alerts gets it.
               "a2p_approved": "alert_on_lead",
               # Monthly recap (day 28-31): rides the daily-digest toggle -- no new column.
               "monthly_recap": "alert_on_daily_digest",
               # E4: Google review milestone -- rides the roi_milestone toggle (no new column).
               "reputation_milestone": "alert_on_roi_milestone"}
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
        # Batch B: a trusted past customer who was screened out (no auto-text) -- the owner
        # needs to know they called so they can ring back personally.
        if context.get("known"):
            return (f"Past customer{tail} just called. We didn't auto-text them (you handle "
                    f"your regulars). Give them a ring when you get a chance.")
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
    if kind == "reputation_milestone":
        # E4: honest copy -- report the actual numbers, name the engine. ASCII only.
        baseline = context.get("baseline", 0)
        current = context.get("current", 0)
        delta = context.get("delta", current - baseline)
        return (f"You've added {delta} Google reviews since you started -- "
                f"{baseline} then, {current} now. That's the FirstBack review engine working.")
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
        lead_word = "lead needs" if n == 1 else "leads need"
        return f"{n} {lead_word} you{money_part}.{hottest_part} Open FirstBack."
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
    if kind == "screening_graduated":
        # Honest: state exactly what happened. Never claim a customer was contacted --
        # these were monitor-mode verdicts (would-have-blocked, no text was suppressed yet).
        n = context.get("n", 0)
        robocaller_word = "robocaller" if n == 1 else "robocallers"
        return (f"Spam blocking is now ON -- this week we'd have blocked {n} "
                f"{robocaller_word} and you rescued none. "
                f"Manage or pause it in Settings.")
    if kind == "growth_tray":
        # 8am tray digest to the owner: honest count, money (labeled if estimated),
        # clear GO/SKIP instructions. Never claims a text was sent.
        count = context.get("count", 0)
        total_str = (context.get("total_str") or "").strip()
        plays_summary = (context.get("plays_summary") or "").strip()
        is_estimated = context.get("is_estimated", False)
        estimated = " (estimated)" if is_estimated else ""
        s = "s" if count != 1 else ""
        base = (f"Good morning. {count} text{s} ready: {plays_summary}. "
                f"{total_str} on the table{estimated}. "
                f"Reply GO to send all, SKIP to hold.")
        # Cap at 320 chars; truncate plays_summary first (keep the GO/SKIP line).
        if len(base) > 320:
            instr = (f"{total_str} on the table{estimated}. "
                     f"Reply GO to send all, SKIP to hold.")
            prefix = f"Good morning. {count} text{s} ready: "
            budget = 320 - len(prefix) - len(instr) - 2  # 2 for ". "
            if budget > 5 and plays_summary:
                short = plays_summary[:budget].rsplit(",", 1)[0]
                base = prefix + short + ". " + instr
            else:
                base = prefix.rstrip() + ". " + instr
        return base
    if kind == "daily_digest":
        # Phase 6b: ONE honest 8am summary absorbing leads + held growth plays + top stall.
        # Never "tap to send" for leads (the in-app one-tap owns that). GO/SKIP is for the
        # held growth texts only. Estimated money is labeled. Never claims a customer text
        # was sent. Caps at 320 chars (truncate the plays detail first, then the stall).
        # Plan 05-3: opt-in "all clear" reassurance on a genuinely quiet day.
        if context.get("all_clear"):
            return "Good morning. Quiet day -- no leads waiting, nothing to approve. FirstBack is running."
        n_leads = context.get("n_leads", 0)
        money = (context.get("money") or "").strip()
        is_estimated = context.get("is_estimated", False)
        plays_count = context.get("plays_count", 0)
        plays_summary = (context.get("plays_summary") or "").strip()
        top_stall_name = (context.get("top_stall_name") or "").strip()
        top_stall_hours = context.get("top_stall_hours", 0)
        # Leads segment -- omitted entirely when there are no open leads (never "0 leads").
        leads_seg = ""
        if n_leads:
            lead_word = "lead needs" if n_leads == 1 else "leads need"
            money_part = f", ~{money} on the table" if money else ""
            est = " (est.)" if (money and is_estimated) else ""
            leads_seg = f" {n_leads} {lead_word} you{money_part}{est}."
        # Held growth plays segment (only when plays exist).
        def _plays_seg(include_summary):
            if not plays_count:
                return ""
            s = "s" if plays_count != 1 else ""
            detail = f" {plays_summary}" if (include_summary and plays_summary) else ""
            return f" {plays_count} text{s} ready:{detail} Reply GO to send all, SKIP to hold."
        # Top-stall segment (only when present).
        stall_seg = ""
        if top_stall_name:
            try:
                stall_h = int(round(float(top_stall_hours)))
            except (TypeError, ValueError):
                stall_h = 0
            stall_seg = f" One stall: {top_stall_name} {stall_h}h."
        base = ("Good morning." + leads_seg + _plays_seg(True) + stall_seg).rstrip()
        if len(base) > 320:  # drop the per-play detail, keep the GO/SKIP line
            base = ("Good morning." + leads_seg + _plays_seg(False) + stall_seg).rstrip()
        if len(base) > 320:  # drop the stall line
            base = ("Good morning." + leads_seg + _plays_seg(False)).rstrip()
        if len(base) > 320:
            base = base[:317].rstrip() + "..."
        return base
    if kind == "monthly_recap":
        # Anti-churn day-28 recap: honest revenue with estimate label, optional ROI multiple,
        # screening section when present. ASCII only, <=320 chars.
        leads = context.get("leads", 0)
        booked = context.get("booked", 0)
        revenue = context.get("revenue", 0)
        multiple = context.get("multiple")
        avg_source = context.get("avg_source", "industry_default")
        est_label = "(estimated)" if avg_source != "owner" else "(based on your job value)"
        multi_line = f" -- about {multiple}x what it costs" if multiple else ""
        screening_section = (context.get("screening_section") or "").strip()
        base = (f"Your FirstBack month: {leads} missed calls rescued, {booked} booked, "
                f"~${revenue:,} recovered {est_label}{multi_line}.")
        if screening_section:
            base += f" {screening_section}"
        base += " Reply STATS to see the full breakdown."
        return base
    if kind == "a2p_approved":
        # Tier-0 F8: the go-live moment. Honest -- texting is now actually on, and any calls
        # that came in during the carrier wait have had their queued text-backs replayed.
        return ("You're live! FirstBack is now texting your missed calls back. Any callers "
                "from while you were getting approved have just been texted -- check your leads.")
    if kind == "tick_stale":
        # Ops alert to the operator: the scheduler hasn't run -- texts/reminders may lag.
        gap = context.get("gap_minutes", 0)
        try:
            gap = int(round(float(gap)))
        except (TypeError, ValueError):
            gap = 0
        return (f"FirstBack's scheduler hasn't run in ~{gap}m -- texts and reminders "
                f"may be delayed. Check the Render cron / restart the service.")
    return f"FirstBack alert ({kind})."


def _subject(kind):
    return {"lead": "New lead -- FirstBack",
            "booking": "Estimate booked -- FirstBack",
            "urgent": "Urgent lead -- FirstBack",
            "canceled": "Estimate canceled -- FirstBack",
            "sms_fail": "SMS delivery failed -- FirstBack",
            "forwarding_lost": "Call forwarding issue -- FirstBack",
            "roi_milestone": "FirstBack paid for itself -- FirstBack",
            "vic_morning": "Your morning briefing -- FirstBack",
            "vic_stall": "Lead still waiting -- FirstBack",
            "screening_graduated": "Spam Shield is now active -- FirstBack",
            "growth_tray": "Your morning growth tray -- FirstBack",
            "daily_digest": "Your morning summary -- FirstBack",
            "tick_stale": "Scheduler may be down -- FirstBack",
            "a2p_approved": "You're live -- FirstBack",
            "monthly_recap": "Your FirstBack month in review -- FirstBack",
            "reputation_milestone": "Your Google reviews are growing -- FirstBack"}.get(kind, "FirstBack alert")


def _enabled_for(business, kind):
    """Whether the owner wants alerts of this kind (default ON when unset)."""
    val = (business or {}).get(_TOGGLE_COL[kind])
    return True if val is None else bool(val)


def _dedupe_key(kind, context):
    """A stable key per event so a double-trigger collapses but distinct events
    (a different lead, a re-book to a new time) still each alert.

    vic_morning:          day-stamped per business (one per local calendar day).
    vic_stall:            day-stamped per (lead, local calendar day).
    screening_graduated:  once per business, keyed by kind only (no context needed;
                          a business graduates exactly once)."""
    if kind == "vic_morning":
        day = (context.get("local_day") or "").strip()
        return f"vic_morning:{day}"
    if kind == "vic_stall":
        lead_id = context.get("lead_id", "")
        day = (context.get("local_day") or "").strip()
        return f"vic_stall:{lead_id}:{day}"
    if kind == "growth_tray":
        # Day-stamped: one digest per business per local calendar day (26h window).
        day = (context.get("local_day") or "").strip()
        return f"growth_tray:{day}"
    if kind == "daily_digest":
        # Day-stamped: one unified morning digest per business per local day (26h window).
        day = (context.get("local_day") or "").strip()
        return f"daily_digest:{day}"
    if kind == "tick_stale":
        # Day-stamped: at most one "scheduler stalled" alert per day, so a sustained
        # outage (the cron retries every 60s) can't become an SMS storm.
        day = (context.get("local_day") or "").strip()
        return f"tick_stale:{day}"
    if kind == "screening_graduated":
        return "screening_graduated"
    if kind == "roi_milestone":
        # Per-level so two bookings seconds apart crossing different milestone levels each
        # send (a bare kind key would collapse them within the 120s window).
        return f"roi_milestone:{context.get('level')}"
    if kind == "monthly_recap":
        # Month-stamped: one recap per business per calendar month. NOT in _DAILY_DEDUPE_KINDS
        # -- the 26h window is wrong for a day-28..31 trigger. The real per-month guard is the
        # caller (scan_monthly_recap) via db.get_meta/set_meta.
        return f"monthly_recap:{context.get('month')}"
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
        # vic_morning/vic_stall carry a day-stamped key and must collapse over the WHOLE
        # local day -- the ticker runs every few minutes, so a 120s window would re-send
        # the morning digest (and every stall nudge) on each pass. Use a 26h window for
        # those (slightly over a day to absorb DST / clock skew); 120s for event alerts.
        # screening_graduated fires once per business lifetime; use a year-long window.
        if kind in _LONG_DEDUPE_KINDS:
            window = _LONG_DEDUPE_SECONDS
        elif kind in _DAILY_DEDUPE_KINDS:
            window = _DAILY_DEDUPE_SECONDS
        else:
            window = ALERT_DEDUPE_SECONDS
        if db.alert_recent(bid, dedupe, window):
            return []  # already alerted for this event recently
        db.add_alert(bid, kind, "inapp", "", "recorded", dedupe, body)
    attempted.append(("inapp", "recorded"))
    # Owner quiet-hours gate (Plan 05-1). Urgent kinds always go through; everything else
    # is held outside the owner's window. The in-app row above is ALWAYS written first, so
    # the owner still sees it in the feed immediately -- only the SMS/email/webhook PUSH is
    # deferred (the 8am daily digest summarizes overnight leads). This NEVER touches the
    # customer TCPA backstop in messaging.send_sms -- that guards CUSTOMER texts; this guards
    # the OWNER's own phone. q_start == q_end means "no quiet hours" (window is empty).
    if kind not in _URGENT_BYPASS_KINDS:
        local_h = datetime.now(_biz_tz_for_alerts(business)).hour
        q_start = _int_pref(business, "alert_quiet_start", 22)
        q_end = _int_pref(business, "alert_quiet_end", 7)
        in_quiet = ((q_start > q_end and (local_h >= q_start or local_h < q_end))
                    or (q_start < q_end and q_start <= local_h < q_end))
        if in_quiet:
            db.add_alert(bid, kind, "sms_held", (business.get("alert_sms") or "").strip(),
                         "held_quiet", dedupe, body)
            return attempted   # SMS/email/webhook deferred; in-app already recorded
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
    # Webhook channel (Plan 05-5): Slack/Teams/Zapier. Fire-and-forget, stdlib only.
    # Held during quiet hours too (the early-return above skips it) -- no 11pm Slack pings.
    webhook_url = (business.get("alert_webhook_url") or "").strip()
    if webhook_url:
        ok = _send_webhook(webhook_url, bid, kind, body, context)
        attempted.append(("webhook", "sent" if ok else "failed"))
    return attempted


def _webhook_url_allowed(url):
    """SSRF guard for the owner-supplied webhook URL. We POST to it server-side, so it
    must be https and resolve to a PUBLIC host -- never loopback/private/link-local/
    reserved (which would let an owner probe our internal network). Best-effort: a DNS
    that fails resolution is rejected. (Slack/Teams/Zapier webhooks are all public https.)"""
    try:
        import urllib.parse
        import socket
        import ipaddress
        p = urllib.parse.urlparse(url)
        if p.scheme != "https" or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, p.port or 443, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


def _send_webhook(url, business_id, kind, body, context):
    """POST a JSON alert payload to the owner's webhook URL (Slack/Teams/Zapier).
    Fire-and-forget: failures are logged but never crash the alert fan-out. Context is
    sanitized to scalar-safe values before serializing. Stdlib only (no new dependency).
    Returns True on a successful POST, False on any failure (so the caller records an
    honest sent/failed status, never a fake 'sent')."""
    if not _webhook_url_allowed(url):
        print(f"[firstback] webhook blocked (not a public https URL): {url[:40]}",
              file=sys.stderr, flush=True)
        return False
    try:
        import urllib.request
        import json as _json
        payload = {
            "business_id": business_id,
            "kind": kind,
            "body": body,
            "context": {k: v for k, v in (context or {}).items()
                        if isinstance(v, (str, int, float, bool, type(None)))},
        }
        data = _json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except Exception as e:
        print(f"[firstback] webhook alert failed ({url[:40]}): {e}",
              file=sys.stderr, flush=True)
        return False


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
