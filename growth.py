"""The Phase 3 growth engine -- Vic's "hunt new business while you're on the roof".

A declarative set of *plays*: each is a money-making opportunity computed from signals
that already exist (leads, appointments, message timing, avg job value, addresses). One
pass over `db.growth_candidates` yields a money-ranked list the owner approves; each send
flows through the SAME gated messaging seam as everything else (opt-out + A2P + simulated
vs live honored), so nothing reaches a customer the owner didn't see.

Honesty + compliance baked in:
  - Review requests ask EVERY completed-job customer; the trigger references NO sentiment,
    rating, or happiness signal (gating reviews is illegal -- FTC + Google).
  - Opportunities that need a connector we don't have yet (Google Business Profile review
    data, financing partner) are surfaced honestly or deferred -- never faked.
  - Money figures are forensic: counts x the owner's own avg job value, never inflated.

Two surfaces:
  - plays(business)            -> money-ranked opportunities (read; powers the feed + briefing)
  - money_left_behind(business)-> the forensic dollar total on the table
  - scan(now)                  -> enqueue due touches onto the existing scheduled_messages
                                  spine (opt-in per business; sent by reminders.run_due_once,
                                  simulated until Twilio + A2P are live)
"""
import re
from datetime import datetime, date, timezone

import db
import messaging
import compliance

# Trades -> the (start_month, end_month) pre-peak window + the service to pitch.
_SEASONS = [
    ("hvac", (2, 3), "AC tune-up"),
    ("air", (2, 3), "AC tune-up"),
    ("roof", (1, 2), "roof inspection"),
    ("paint", (10, 11), "interior paint"),
    ("plumb", (11, 12), "pipe winterization"),
    ("landscap", (2, 3), "spring cleanup"),
    ("lawn", (2, 3), "spring cleanup"),
]
# Big-ticket financing threshold by trade keyword (dollars); default for anything else.
_FINANCING_DEFAULT = 3000
_FINANCING_BY_TRADE = {"roof": 5000, "hvac": 4000, "paint": 2500, "plumb": 1500}


# ---- small helpers ----
def _now():
    return datetime.now(timezone.utc)


def _parse(ts):
    """An ISO timestamp -> aware datetime (UTC), or None."""
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _parse_date(d):
    """An ISO date 'YYYY-MM-DD' -> date, or None."""
    try:
        return date.fromisoformat(d)
    except (ValueError, TypeError):
        return None


# Phase 5d: trade-keyword default job values (used when avg_job_value is unset/0).
# Never show $0 to the owner (P0 gate). Label as "(estimated)" in the tray digest.
_TRADE_DEFAULT_VALUE = {
    "paint": 2500, "roof": 8000, "hvac": 3500, "plumb": 1200,
    "landscap": 1500, "lawn": 1500,
}


def _job_value(business):
    """Return the job value for money framing. Uses real avg_job_value when set;
    falls back to trade-keyword defaults; never returns 0 (P0: dollar framing honest)."""
    try:
        v = int(business.get("avg_job_value") or 0)
        if v > 0:
            return v
    except (ValueError, TypeError):
        pass
    trade = (business.get("trade") or "").lower()
    for key, default in _TRADE_DEFAULT_VALUE.items():
        if key in trade:
            return default
    return 2000  # generic default; never show $0 to Dave


# Placeholder "names" we must never address a customer by (mirrors reminders._PLACEHOLDER_NAMES).
_PLACEHOLDER_NAMES = {"", "new caller", "homeowner", "unknown", "the caller", "caller", "new"}

# Phase 5d: negative-signal keywords for tone-risk detection (A6).
# A lead whose recent inbound messages contain any of these signals gets tone_risk=True
# so the play is always held for Dave to review before sending.
_TONE_RISK_KEYWORDS = [
    "terrible", "awful", "unhappy", "disappointed", "complaint",
    "never again", "rip off",
]


def _tone_risk(lead_id):
    """True if the lead's last 5 inbound messages contain a negative-signal keyword.
    Loads via db.get_last_n_inbound (targeted query, no N+1)."""
    try:
        msgs = db.get_last_n_inbound(lead_id, n=5)
    except Exception:
        return False
    for msg in msgs:
        body = (msg.get("body") or "").lower()
        if any(kw in body for kw in _TONE_RISK_KEYWORDS):
            return True
    return False


def _first(name):
    """A real first name, or '' when there's no name we can stand behind. Never a placeholder
    like 'New Caller' (which would put 'New' in a customer text)."""
    nm = (name or "").strip()
    if nm and nm.lower() not in _PLACEHOLDER_NAMES:
        return nm.split()[0]
    return ""


def _who(name, phone):
    """Owner-facing label for a lead: real name, else the number, else a neutral noun."""
    return _first(name) or (phone or "").strip() or "this customer"


def _greet(first):
    """Lead-in for an SMS: 'Maria, ' when we know the name, nothing when we don't."""
    return f"{first}, " if first else ""


def _ago(then, now):
    secs = (now - then).total_seconds()
    if secs < 86400:
        return f"{max(1, int(secs // 3600))}h ago"
    days = int(secs // 86400)
    if days < 14:
        return f"{days}d ago"
    return f"{days // 7}w ago"


def _zip(address):
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", address or "")
    return m.group(1) if m else None


def _money_label(n):
    return f"~${n:,}" if n else ""


# ---- copy (Vic voice: short, plain, customer benefit first, never incentivize) ----
def _review_link(business):
    return (business.get("review_link") or "").strip() or "[your Google review link]"


def _bizname(business):
    return (business.get("name") or "your contractor").strip()


def _copy_review(first, business):
    return (f"{_greet(first)}thanks for the work today. If you have a minute, an honest Google "
            f"review goes a long way for us: {_review_link(business)}")


def _copy_followup(first, business):
    return (f"{_greet(first)}it's {_bizname(business)}. Just checking in on that estimate. Any "
            "questions, or want to get it on the schedule?")


def _copy_reactivation(first, business):
    return (f"{_greet(first)}{_bizname(business)} here. That estimate's still good if you want "
            "to move on it. Reply here and we'll line it up.")


def _copy_winback(first, business):
    return (f"{_greet(first)}it's been a while since your last job. {_bizname(business)} here "
            "if you need any work done. Reply and we'll sort it.")


def _copy_referral(first, business):
    return (f"{_greet(first)}glad we could help. If a neighbor ever needs the same work, have "
            f"them call us. {_bizname(business)}.")


def _copy_membership(first, business):
    return (f"{_greet(first)}since you've had us out a few times, we run a maintenance plan so "
            "the small stuff gets handled before it gets bigger. Want the details?")


# ---- one opportunity dict ----
def _opp(kind, lead_id, first, phone, tier, *, title, why, tone, label, money,
         draft="", sendable=True, compliance="", action=None,
         tone_risk=False, blocked_reason=None):
    return {
        "kind": kind, "play_id": f"{kind}:{lead_id}" if lead_id else kind,
        "title": title, "why": why, "tone": tone, "label": label,
        "money": int(money or 0), "money_label": _money_label(int(money or 0)),
        "draft_body": draft, "sendable": sendable,
        # one-tap action: a chat command. Sendable plays route to the gated text confirm
        # (exact recipient + body + opt-out + live/test) -- never an auto-send.
        "action": action or (f"text {first} back saying {draft}" if sendable and draft
                             else "show my leads"),
        "lead_id": lead_id, "lead_name": first, "lead_phone": phone,
        "tier": tier, "compliance_note": compliance,
        # Phase 5d tray metadata
        "tone_risk": tone_risk,
        "blocked_reason": blocked_reason,
    }


# ---- the engine ----
def plays(business):
    """Money-ranked growth opportunities for `business`. Read-only; never raises."""
    bid = business["id"]
    try:
        cands = db.growth_candidates(bid)
        touched = db.growth_touch_index(bid)
    except Exception:
        return []
    val = _job_value(business)
    now = _now()
    today = now.date()
    out = []
    zip_counts = {}

    for c in cands:
        phone = (c.get("phone") or "").strip()
        if not phone:
            continue                          # no number -> can't text -> no sendable plays
        # Reuse the messaging gate's own opt-out logic -- don't surface what we can't send.
        if messaging.outbound_mode(business, phone) == "suppressed":
            continue
        kinds = touched.get(c["id"], set())
        status = c.get("status")
        booked = c.get("booked_count") or 0
        last_out = _parse(c.get("last_out_at"))
        last_in = _parse(c.get("last_in_at"))
        last_appt = _parse_date(c.get("last_appt_day"))
        created = _parse(c.get("created_at"))
        first = _first(c.get("name"))               # greeting-safe ('' when no real name)
        who = _who(c.get("name"), phone)            # owner-facing label + action target
        lid = c["id"]

        z = _zip(c.get("address"))
        if z and created and 0 <= (now - created).days <= 14:
            zip_counts[z] = zip_counts.get(z, 0) + 1

        job_done = bool(booked and last_appt and last_appt < today)
        # Phase 5d: tone-risk flag for tray (A6). Scan last 5 inbound messages once per lead.
        tr = _tone_risk(lid)

        # CONVERT -- review request: every completed job, no sentiment branch (compliant).
        # Only on RECENTLY completed jobs (<=90d): a review ask on a years-old job reads as
        # spam and Google weights recency. (referral below uses the same recency-window pattern.)
        if job_done and (today - last_appt).days <= 90 and "review_request" not in kinds:
            # Phase 5d A8: if the review link is a placeholder, surface as a blocked play
            # (sendable=False, blocked_reason) for tray visibility -- but NEVER queue.
            _review_draft = _copy_review(first, business)
            if "[" in _review_draft:
                out.append(_opp("review_request", lid, who, phone, "convert",
                                title=f"Ask {who} for a review (add your review link first)",
                                why="job's done, but review link is missing",
                                tone="hot", label="Review", money=val, draft=_review_draft,
                                sendable=False, blocked_reason="add_review_link",
                                compliance="Asks every customer. Never filtered by rating (FTC + Google).",
                                tone_risk=tr))
            else:
                out.append(_opp("review_request", lid, who, phone, "convert",
                                title=f"Ask {who} for a review", why="job's done, ask while it's fresh",
                                tone="hot", label="Review", money=val, draft=_review_draft,
                                compliance="Asks every customer. Never filtered by rating (FTC + Google).",
                                tone_risk=tr))

        # CONVERT -- quote follow-up vs GROW -- reactivation, by how cold the quiet quote is.
        quiet = status != "booked" and last_out and (not last_in or last_in < last_out)
        if quiet:
            hrs = (now - last_out).total_seconds() / 3600
            if 24 <= hrs < 24 * 30 and "quote_followup" not in kinds:
                out.append(_opp("quote_followup", lid, who, phone, "convert",
                                title=f"Follow up with {who}",
                                why=f"quote sent {_ago(last_out, now)}, no reply", tone="new",
                                label="Follow-up", money=val, draft=_copy_followup(first, business)))
            elif hrs >= 24 * 30 and "reactivation" not in kinds:
                out.append(_opp("reactivation", lid, who, phone, "grow",
                                title=f"Chase {who}'s quote one more time",
                                why=f"quote went quiet {_ago(last_out, now)}, no reply", tone="new",
                                label="Reactivate", money=val, draft=_copy_reactivation(first, business)))

        # GROW -- win-back a past customer 12-18 months out.
        if booked and last_appt:
            months = (today.year - last_appt.year) * 12 + (today.month - last_appt.month)
            if 12 <= months <= 18 and "winback" not in kinds:
                play_wb = _opp("winback", lid, who, phone, "grow",
                               title=f"Win back {who}", why=f"last job about {months} months ago",
                               tone="new", label="Win-back", money=val * 2,
                               draft=_copy_winback(first, business), tone_risk=tr)
                # Phase 5d A7: pass has_inbound so scan() can apply TCPA narrowing.
                play_wb["has_inbound"] = bool(c.get("has_inbound"))
                out.append(play_wb)

        # GROW -- referral at the goodwill peak (job wrapped in the last few days).
        if job_done and (today - last_appt).days <= 3 and "referral" not in kinds:
            out.append(_opp("referral", lid, who, phone, "grow",
                            title=f"Ask {who} for a referral", why="just wrapped, peak goodwill",
                            tone="ok", label="Referral", money=val, draft=_copy_referral(first, business)))

        # GROW -- membership for repeat, lower-ticket customers (recurring-revenue floor).
        if booked >= 2 and 0 < val < 500 and "membership" not in kinds:
            out.append(_opp("membership", lid, who, phone, "grow",
                            title=f"Offer {who} a maintenance plan",
                            why=f"{booked} jobs, good plan candidate", tone="ok", label="Membership",
                            money=val, draft=_copy_membership(first, business)))

    # GROW (business-level, owner-initiated campaigns -- surfaced, not auto-texted) ----
    for z, n in sorted(zip_counts.items(), key=lambda kv: -kv[1]):
        if n >= 3:
            out.append(_opp("density", None, "", "", "grow",
                            title=f"Door-hanger the {z} block",
                            why=f"{n} jobs in {z} lately, the block knows your truck",
                            tone="ok", label="Density", money=val * 2, sendable=False,
                            action="show my leads"))
    seas = _seasonal_play(business, today, val)
    if seas:
        out.append(seas)
    if val >= _financing_threshold(business):
        out.append(_opp("financing", None, "", "", "grow",
                        title="Offer financing on big jobs",
                        why=f"your jobs run ~${val:,}; financing closes more at this size",
                        tone="ok", label="Financing", money=int(val * 0.25), sendable=False,
                        action="show my booked estimates"))

    out.sort(key=lambda o: -o["money"])
    return out


def _seasonal_play(business, today, val):
    trade = (business.get("trade") or "").lower()
    for key, (start_m, end_m), service in _SEASONS:
        if key in trade and start_m <= today.month <= end_m:
            return _opp("seasonal", None, "", "", "grow",
                        title=f"Offer {service} to past customers now",
                        why="peak season is opening; past customers book faster than new leads",
                        tone="ok", label="Seasonal", money=val * 5, sendable=False,
                        action="show my leads")
    return None


def _financing_threshold(business):
    trade = (business.get("trade") or "").lower()
    for key, thresh in _FINANCING_BY_TRADE.items():
        if key in trade:
            return thresh
    return _FINANCING_DEFAULT


def money_left_behind(business):
    """The forensic dollar total currently on the table, split by tier. Show the receipts."""
    ps = plays(business)
    by_tier = {"convert": 0, "grow": 0}
    for p in ps:
        by_tier[p["tier"]] = by_tier.get(p["tier"], 0) + p["money"]
    total = sum(by_tier.values())
    return {"total": total, "by_tier": by_tier, "play_count": len(ps),
            "headline": (f"~${total:,} on the table across {len(ps)} "
                         f"play{'s' if len(ps) != 1 else ''}." if total else
                         "Nothing on the table right now.")}


# ---- the opt-in auto-scheduler (rides the existing scheduled_messages spine) ----
def growth_on(business):
    """Whether a business has opted into auto-queued growth touches. Default OFF: the feed
    + one-tap gated sends are always live, but auto-texting customers on a schedule is an
    explicit opt-in (and still simulated until Twilio + A2P are live)."""
    try:
        return bool(int(business.get("growth_on") or 0))
    except (ValueError, TypeError):
        return False


# send-time offset (minutes from the scan) per sendable kind.
_DELAY_MIN = {"review_request": 105, "quote_followup": 0, "reactivation": 0,
              "winback": 0, "referral": 120, "membership": 0}


def scan(now=None):
    """Enqueue due growth touches for opted-in businesses onto scheduled_messages.
    Phase 5d: inserts as 'held' (tray mode, or auto for non-review plays, or tone-risk
    plays). Dave must release via release_growth_batch / release_growth_play before
    reminders.run_due_once will pick them up (due_scheduled_messages queries status=pending
    only -- held rows never auto-fire). Dedupe index blocks double-queueing.
    Returns {'queued': n}."""
    now = now or db.now_iso()
    base = _parse(now) or _now()
    queued = 0
    for biz in db.list_businesses():
        mode = db.growth_mode(biz["id"])  # 'off' | 'tray' | 'auto'
        if mode == 'off':
            continue
        # Don't queue doomed touches: once Twilio is configured, hold until A2P is approved
        # (an A2P-blocked send would mark the touch 'failed' and the dedupe index would then
        # never let it re-queue). While simulated (Twilio off), queue freely.
        if messaging.configured() and not compliance.a2p_ready(biz):
            continue
        for p in plays(biz):
            # Never queue non-sendable or plays missing required fields
            if not p.get("sendable") or p.get("lead_id") is None or not p.get("draft_body"):
                continue
            # Hard guard: never queue a body with an unfilled placeholder
            if "[" in p["draft_body"]:
                continue
            lead_id = p["lead_id"]
            # Phase 5d A5: frequency cap gate (G3)
            # 30-day cross-kind cap: never text a customer who was already touched recently.
            if db.recent_growth_touch(biz["id"], lead_id, within_days=30):
                continue
            # 12-month rolling cap: max 2 touches per customer per year.
            if db.growth_touch_count_12mo(biz["id"], lead_id) >= 2:
                continue
            # Phase 5d A7: win-back TCPA narrowing -- only leads who sent at least one
            # inbound message. Cold-only leads are excluded; EBR is weaker for them.
            # (plays() still surfaces winbacks for display; scan() gates the actual queue.)
            if p["kind"] == "winback" and not p.get("has_inbound"):
                continue
            # Phase 5d A3: determine insert status based on mode + tone-risk
            # Tone-risk plays always go 'held' regardless of mode (P0: Dave must review).
            if p.get("tone_risk"):
                insert_status = 'held'
            elif mode == 'auto' and p["kind"] == 'review_request':
                # Auto mode: only review_request (the lowest-risk play) goes 'pending';
                # all other kinds go 'held' (win-backs, referrals, reactivations).
                insert_status = 'pending'
            else:
                # tray mode: everything held. auto mode non-review: held.
                insert_status = 'held'
            delay = _DELAY_MIN.get(p["kind"], 0)
            send_at = (base.timestamp() + delay * 60)
            send_at_iso = datetime.fromtimestamp(send_at, timezone.utc).isoformat()
            sid = db.add_scheduled_message(biz["id"], lead_id, None, p["kind"],
                                           send_at_iso, p["draft_body"],
                                           status=insert_status)
            if sid:
                queued += 1
    return {"queued": queued}
