"""Go-Live connection wizard orchestration for RingBack.

Takes a contractor from signup to a live missed-call text-back: which setup steps
are done, the A2P 10DLC registration status (synced from Twilio), and the carrier
call-forwarding codes for the missed-call catcher. Number buying/attaching lives in
messaging.py (provision_number / set_business_twilio); this module is the thin glue
the /setup routes call.

Honest by construction: the live/ready signal is computed from
compliance.launch_blockers(), so the wizard can never claim "live" before the number
is bound, A2P is approved, and forwarding is confirmed. Gated + defensive like the
rest of the telephony layer (mirrors messaging.py): the Twilio status sync is a safe
no-op when unconfigured and swallows + logs any API error with the "[ringback]"
prefix, never raising into a request.
"""
import sys

import db
import messaging
import compliance

# ---- Setup steps (the wizard's spine) ----
STEPS = ("profile", "number", "a2p", "forwarding")
_STEP_TITLES = {
    "profile": "Your business",
    "number": "Your RingBack number",
    "a2p": "Carrier registration (A2P)",
    "forwarding": "Forward your missed calls",
}


def _profile_done(biz):
    # Enough to register an A2P brand: a name, an EIN, and a business address.
    return bool(biz.get("name") and biz.get("ein") and biz.get("business_address"))


def profile_complete(biz):
    """Public check that the A2P registration intake is filled in (name+EIN+address)."""
    return _profile_done(biz or {})


# What must be done before a step can be acted on. A2P and forwarding both unlock
# once the number exists -- they're independent, so a contractor can set up call
# forwarding on their phone WHILE the (multi-hour) carrier registration is vetting.
_PREREQS = {"profile": (), "number": ("profile",),
            "a2p": ("number",), "forwarding": ("number",)}


def _step_done(biz, sms_configured=True):
    return {
        "profile": _profile_done(biz),
        "number": sms_configured and bool(biz.get("twilio_number")) and bool(biz.get("webhooks_wired")),
        "a2p": sms_configured and compliance.a2p_ready(biz),
        "forwarding": sms_configured and bool(biz.get("forwarding_confirmed")),
    }


def step_state(business, sms_configured=True):
    """The ordered setup steps, each tagged with a status: 'done', 'current' (the
    first actionable unfinished step), 'ready' (another actionable step), or 'todo'
    (locked until its prerequisites are met). `open` means the step's form should
    show. Drives the wizard's stepper UI."""
    biz = (business if isinstance(business, dict) else db.get_business(business)) or {}
    done = _step_done(biz, sms_configured)
    out, first_open = [], False
    for key in STEPS:
        ready = all(done[p] for p in _PREREQS[key])
        if done[key]:
            status = "done"
        elif not ready:
            status = "todo"
        elif not first_open:
            status, first_open = "current", True
        else:
            status = "ready"
        out.append({"key": key, "title": _STEP_TITLES[key], "status": status,
                    "done": done[key], "open": (not done[key]) and ready})
    return out


def current_step(business, sms_configured=True):
    """The key of the first actionable unfinished step, or None when all are done."""
    for s in step_state(business, sms_configured):
        if s["status"] == "current":
            return s["key"]
    return None


def is_live(business, sms_configured=None):
    """True only when nothing stands between this business and texting customers for
    real -- the single honest 'are we live?' check, delegated to launch_blockers."""
    biz = (business if isinstance(business, dict) else db.get_business(business)) or {}
    cfg = messaging.configured() if sms_configured is None else sms_configured
    return not compliance.launch_blockers(biz, cfg)


def blockers(business, sms_configured=None):
    """Plain-English list of what's left before go-live (empty == live)."""
    biz = (business if isinstance(business, dict) else db.get_business(business)) or {}
    cfg = messaging.configured() if sms_configured is None else sms_configured
    return compliance.launch_blockers(biz, cfg)


def golive_summary(business, sms_configured=None):
    """One dict describing a tenant's go-live state, the single source of truth shared by the
    command-center hero nudge, the command-center status card, and the /setup wizard banner --
    so the surfaces can never disagree. Honest by construction: `status` is "live" only when a
    real inbound test call was actually texted back (live_verified), "setup_complete" when the
    blockers are clear but no test call has confirmed forwarding yet, else "not_live".

    Keys: is_live, live_verified, status, done, total, current, blocker (top one or None),
    steps (list of {key, title, state}; state is done|current|ready|todo from step_state)."""
    biz = (business if isinstance(business, dict) else db.get_business(business)) or {}
    cfg = messaging.configured() if sms_configured is None else sms_configured
    steps = step_state(biz, cfg)
    live = is_live(biz, cfg)
    last = db.last_inbound_call(biz["id"]) if biz.get("id") and biz.get("twilio_number") else None
    verified = bool(live and last and last.get("engaged"))
    bl = blockers(biz, cfg)
    return {
        "is_live": live,
        "live_verified": verified,
        "status": "live" if verified else ("setup_complete" if live else "not_live"),
        "done": sum(1 for s in steps if s["done"]),
        "total": len(steps),
        "current": current_step(biz, cfg),
        "blocker": bl[0] if bl else None,
        "steps": [{"key": s["key"], "title": s["title"], "state": s["status"]} for s in steps],
    }


# ---- "Fully set up" tier: recommended connections beyond go-live ----
# Status-only aggregation for the wizard's checklist. These make RingBack better but
# NEVER gate "live" -- golive_summary owns that, and is left untouched. The wizard
# renders this as a deep-link checklist into the existing Settings forms (no new write
# paths). Signals that need request/auth context (calendar/contacts connection, whether
# the password was changed off the seed) are dependency-injected by the route, mirroring
# how golive_summary takes sms_configured -- so this stays pure and unit-testable.
def recommended_setup(business, *, calendar_connected=False, contacts_connected=False,
                      password_changed=False, ai_default=""):
    """The recommended-connections checklist (the "Fully set up" tier). Returns
    {items: [{key,title,value,done,optional,href,cta}], done, total}. `optional` marks
    genuine add-ons (voice/scheduling/contacts) so the meter doesn't read as broken when
    they're off. `total`/`done` count ALL items; the live tier is computed elsewhere."""
    biz = (business if isinstance(business, dict) else db.get_business(business)) or {}
    ai = (biz.get("ai_instructions") or "").strip()
    ai_done = bool(ai) and ai != (ai_default or "").strip()
    rows = [
        # key          title                        value (one-liner)                                       done                  optional href                              cta
        ("ai",         "Teach your AI",             "How it talks and what it asks on every call",          ai_done,              False, "/settings#set-ai",              "Edit"),
        ("calendar",   "Connect your calendar",     "Booked estimates sync; it avoids your busy times",     calendar_connected,   False, "/settings#set-calendar",        "Connect"),
        ("alerts",     "Owner alerts",              "Get pinged the moment a lead or booking lands",        bool(biz.get("alert_email") or biz.get("alert_sms")), False, "/settings#set-alerts", "Set up"),
        ("screening",  "Call screening",            "Decide who gets a text back",                          bool(biz.get("screen_mode")), False, "/settings#set-screening", "Choose"),
        ("reminders",  "Reminders & follow-ups",    "Cut no-shows and revive quiet leads",                  bool(biz.get("reminders_enabled") or biz.get("followups_enabled")), False, "/settings#set-reminders", "Turn on"),
        ("voice",      "AI voice callback",         "Let a caller reply CALL for a live AI call back",      bool(biz.get("voice_callback_enabled")), True, "/settings#set-voice", "Enable"),
        ("scheduling", "Scheduling & availability", "Your work days, estimate windows, and buffer",         bool(biz.get("estimate_times") or biz.get("working_days") or biz.get("buffer_minutes")), True, "/settings#set-scheduling", "Adjust"),
        ("contacts",   "Import your contacts",      "So the screen recognizes people you already know",     contacts_connected,   True,  "/api/contacts/google/connect",  "Connect"),
        ("password",   "Set your own password",     "Move off the starter password",                        password_changed,     False, "/settings#set-password",        "Change"),
    ]
    items = [{"key": k, "title": t, "value": v, "done": bool(d), "optional": o,
              "href": h, "cta": c} for (k, t, v, d, o, h, c) in rows]
    return {"items": items, "done": sum(1 for it in items if it["done"]), "total": len(items)}


# ---- First-run chaperone: the ordered setup walk Vic guides a brand-new owner through ----
# Money first (instant win, no prerequisite), then the go-live dependency chain (profile is the
# A2P prereq), then the high-value add-ons. Voice (undeployed) and screening (its enforce mode
# silences callers -- exactly wrong to wave at a new owner) are deliberately NOT here.
_CHAPERONE_STEPS = ("avg_job_value", "profile", "number", "a2p", "forwarding",
                    "calendar", "alerts")


def _chaperone_step_done(key, biz, golive, calendar_connected):
    if key == "avg_job_value":
        return bool(biz.get("avg_job_value"))
    if key == "profile":
        return profile_complete(biz)
    if key in ("number", "a2p", "forwarding"):
        st = next((s for s in golive.get("steps", []) if s["key"] == key), None)
        return bool(st and st.get("state") == "done")
    if key == "calendar":
        return bool(calendar_connected)
    if key == "alerts":
        return bool((biz.get("alert_sms") or "").strip() or (biz.get("alert_email") or "").strip())
    return False


def chaperone_next_step(business, golive, calendar_connected=False):
    """The single next unfinished setup step for the chaperone, read from REAL state (golive
    summary + the business row + whether the calendar is connected). None when nothing's left."""
    biz = (business if isinstance(business, dict) else db.get_business(business)) or {}
    for key in _CHAPERONE_STEPS:
        if not _chaperone_step_done(key, biz, golive, calendar_connected):
            return key
    return None


def chaperone_progress(business, golive, calendar_connected=False):
    """(done, total) across the chaperone's steps -- for the briefing's 'N of M done' line."""
    biz = (business if isinstance(business, dict) else db.get_business(business)) or {}
    done = sum(1 for k in _CHAPERONE_STEPS
               if _chaperone_step_done(k, biz, golive, calendar_connected))
    return done, len(_CHAPERONE_STEPS)


def default_area_code(biz):
    """A sensible area-code guess to pre-fill number search: the digits of the
    business's existing phone, else ''."""
    digits = "".join(ch for ch in (biz or {}).get("phone", "") if ch.isdigit())
    # Drop a leading US country code so a +1 215... yields 215, not 121.
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    return digits[:3] if len(digits) >= 10 else ""


def available_numbers(area_code, limit=5):
    """Local numbers available to buy in an area code (voice+SMS). [] when Twilio
    isn't configured or on any error -- delegates to messaging.search_numbers."""
    if not area_code:
        return []
    return messaging.search_numbers(area_code=area_code, limit=limit)


# ---- A2P 10DLC status sync (Twilio -> our a2p_status) ----
# Twilio's campaign status strings mapped onto our 4-state model. Approval is the
# only thing that flips a business to "live"; anything in-flight stays "pending".
_A2P_STATUS_MAP = {
    "VERIFIED": "approved", "APPROVED": "approved",
    "IN_PROGRESS": "pending", "PENDING": "pending", "IN_REVIEW": "pending",
    "SUBMITTED": "pending", "PENDING_REVIEW": "pending",
    "FAILED": "failed", "REJECTED": "failed",
    "REGISTERED": "pending",
    "EXPIRED": "failed", "DELETED": "failed", "SUSPENDED": "failed",
}


def a2p_sync(business):
    """Refresh a business's a2p_status from Twilio when there's a campaign on file.
    Returns the (possibly updated) status. No-op returning the current status when
    there's nothing to sync or Twilio isn't configured. Never raises."""
    biz = (business if isinstance(business, dict) else db.get_business(business))
    if not biz:
        return "unregistered"
    current = compliance.a2p_status(biz)
    raw = messaging.fetch_a2p_campaign_status(biz.get("a2p_messaging_service_sid"),
                                              biz.get("a2p_campaign_sid"))
    mapped = _A2P_STATUS_MAP.get((raw or "").upper())
    if raw and mapped is None:
        print(f"[ringback] a2p unmapped campaign_status {raw!r} (biz {biz['id']}); leaving unchanged",
              file=sys.stderr, flush=True)
    if mapped and mapped != current:
        db.set_a2p_status(biz["id"], mapped)
        return mapped
    return current


def a2p_sync_all():
    """Sync every business that has a campaign registered. For the cron seam
    (/tasks/run-due). Returns how many statuses changed."""
    changed = 0
    for biz in db.list_businesses():
        if biz.get("a2p_campaign_sid"):
            before = compliance.a2p_status(biz)
            if a2p_sync(biz) != before:
                changed += 1
    return changed


# ---- Carrier conditional call-forwarding codes (the missed-call catcher) ----
# "Conditional" = forward only when the contractor doesn't answer / is busy, so they
# keep taking calls normally and only the MISSED ones reach RingBack. {num} is the
# RingBack number. These are dialed once on the contractor's own phone.
CARRIER_FORWARD_CODES = {
    "verizon": {"label": "Verizon", "activate": "*71{num}", "cancel": "*73",
                "note": "Forwards calls you don't answer or when you're on another call."},
    "att": {"label": "AT&T", "activate": "*92{num}", "cancel": "*93",
            "note": "Forwards calls you don't answer. To also forward when you're busy, "
                    "dial *90 then your RingBack number."},
    "tmobile": {"label": "T-Mobile", "activate": "**61*{num}#", "cancel": "##61#",
                "note": "Forwards calls you don't answer. For every missed case at once, "
                        "use the universal code **004* instead."},
    "uscellular": {"label": "US Cellular", "activate": "**61*{num}#", "cancel": "##61#",
                   "note": "Forwards calls you don't answer."},
    "other": {"label": "Other / GSM phone", "activate": "**004*{num}#", "cancel": "##004#",
              "note": "Universal GSM code: forwards busy, no-answer, and unreachable in one step."},
}


def forwarding_code(carrier, number):
    """The exact conditional-forwarding star code for a carrier, with the RingBack
    number baked in, plus the cancel code and a plain-English note. Falls back to the
    universal GSM code for an unknown carrier."""
    c = CARRIER_FORWARD_CODES.get(carrier) or CARRIER_FORWARD_CODES["other"]
    digits = "".join(ch for ch in (number or "") if ch.isdigit() or ch == "+")
    return {"carrier": carrier if carrier in CARRIER_FORWARD_CODES else "other",
            "label": c["label"], "note": c["note"], "cancel": c["cancel"],
            "activate": c["activate"].replace("{num}", digits)}
