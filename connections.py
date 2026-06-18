"""Go-Live connection wizard orchestration for FirstBack.

Takes a contractor from signup to a live missed-call text-back: which setup steps
are done, the A2P 10DLC registration status (synced from Twilio), and the carrier
call-forwarding codes for the missed-call catcher. Number buying/attaching lives in
messaging.py (provision_number / set_business_twilio); this module is the thin glue
the /setup routes call.

Honest by construction: the live/ready signal is computed from
compliance.launch_blockers(), so the wizard can never claim "live" before the number
is bound, A2P is approved, and forwarding is confirmed. Gated + defensive like the
rest of the telephony layer (mirrors messaging.py): the Twilio status sync is a safe
no-op when unconfigured and swallows + logs any API error with the "[firstback]"
prefix, never raising into a request.
"""
import re
import sys
from datetime import datetime, timezone, timedelta

import config
import db
import messaging
import compliance

# Probe timeout: if a sentinel never confirms within this many seconds, the
# forwarding is considered lost on the next check_forwarding_health run.
_SENTINEL_TIMEOUT_SECS = 120   # 2 minutes -- generous for carrier propagation
_PROBE_INTERVAL_DAYS = 7       # re-probe weekly

# ---- Setup steps (the wizard's spine) ----
STEPS = ("profile", "number", "a2p", "forwarding")
_STEP_TITLES = {
    "profile": "Your business",
    "number": "Your FirstBack number",
    "a2p": "Carrier registration (A2P)",
    "forwarding": "Forward your missed calls",
}


def registration_path(biz):
    """Return the A2P registration path for a business.

    sole_prop -> name + business_address is enough (NO EIN required; an EIN
    actively disqualifies a sole-prop Starter brand submission).
    llc / unknown -> name + ein + business_address are all required.

    Reads biz.get('business_type'). Returns 'sole_prop', 'llc', or 'unknown'.
    """
    bt = (biz or {}).get("business_type") or "unknown"
    if bt == "sole_prop":
        return "sole_prop"
    if bt == "llc":
        return "llc"
    return "unknown"


def _profile_done(biz):
    """Enough to register an A2P brand. Forks on business_type:
    - sole_prop: name + business_address (NO EIN required).
    - llc / unknown: name + ein + business_address required.
    """
    path = registration_path(biz)
    if path == "sole_prop":
        return bool(biz.get("name") and biz.get("business_address"))
    # llc or unknown: EIN is required
    return bool(biz.get("name") and biz.get("ein") and biz.get("business_address"))


def profile_complete(biz):
    """Public check that the A2P registration intake is filled in.
    Forks on business_type: sole_prop relaxes the EIN requirement; llc/unknown require it."""
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
# Status-only aggregation for the wizard's checklist. These make FirstBack better but
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


# ---- Phase 3: A2P submission + micro-site slug helpers ----

def build_slug(name, business_id):
    """Build a URL-safe micro-site slug from a business name + id.

    Lowercases the name, collapses runs of non-alphanumeric chars to a single
    hyphen, strips leading/trailing hyphens, caps at 40 chars, then appends
    '-{business_id}' for uniqueness. Falls back to 'biz-{business_id}' when
    the name normalizes to empty.
    """
    slug = (name or "").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    slug = slug[:40]
    if not slug:
        return f"biz-{business_id}"
    return f"{slug}-{business_id}"


def build_contact_email(slug):
    """Return the micro-site contact email for a slug.
    {slug}@{CLIENTS_EMAIL_DOMAIN} (domain from config.CLIENTS_EMAIL_DOMAIN).
    """
    domain = getattr(config, "CLIENTS_EMAIL_DOMAIN", "clients.firstback.com")
    return f"{slug}@{domain}"


def submit_a2p(business_id):
    """Dispatch A2P registration for a business by registration_path.

    Returns a dict with 'status': 'simulated' | 'submitted' | 'error'.

    # NEVER set status='approved' here -- only a2p_sync() may, after polling.

    When messaging.trust_hub_configured() is False (Trust Hub product SID not
    set), records a2p_status='pending' + submitted_at (so the wizard advances)
    and returns {'status': 'simulated'}.

    Path B (llc/unknown): builds slug + contact_email -> db.set_micro_site ->
    re-fetches biz -> create_a2p_brand -> create_a2p_messaging_service ->
    create_a2p_campaign. Short-circuits on any step error.

    Path A (sole_prop): same brand/svc/campaign chain without micro-site.

    Never raises.
    """
    try:
        biz = db.get_business(business_id)
        if not biz:
            return {"status": "error", "step": "lookup", "error": "business not found"}

        now = datetime.now(timezone.utc).isoformat()

        # Gate: if Trust Hub is not configured, record pending and return simulated.
        if not messaging.trust_hub_configured():
            db.set_a2p_registration(business_id, status="pending", submitted_at=now)
            return {"status": "simulated"}

        path = registration_path(biz)

        if path != "sole_prop":
            # Path B: llc / unknown — build micro-site first.
            slug = build_slug(biz.get("name") or "", business_id)
            contact_email = build_contact_email(slug)
            db.set_micro_site(business_id, slug, contact_email)
            # Re-fetch so brand creation sees the slug/email columns.
            biz = db.get_business(business_id)

        # Each Twilio object is created at real cost ($4 brand / $10 campaign). To keep
        # this idempotent across a retry after a PARTIAL failure, we (a) reuse any SID
        # already stored on the business instead of re-creating it, and (b) persist each
        # SID the instant it's created -- so a later step failing never orphans an
        # already-created Twilio object into a duplicate on the next attempt.

        # --- Step 1: brand ---
        brand_sid = biz.get("a2p_brand_sid")
        if not brand_sid:
            brand_result = messaging.create_a2p_brand(biz)
            if brand_result.get("status") == "error":
                return {"status": "error", "step": "brand", "error": brand_result.get("error", "brand failed")}
            brand_sid = brand_result.get("brand_sid")
            if brand_sid:
                db.set_a2p_registration(business_id, brand_sid=brand_sid)

        # --- Step 2: messaging service ---
        messaging_service_sid = biz.get("a2p_messaging_service_sid")
        if not messaging_service_sid:
            svc_result = messaging.create_a2p_messaging_service(biz)
            if svc_result.get("status") == "error":
                return {"status": "error", "step": "messaging_service",
                        "error": svc_result.get("error", "messaging service failed")}
            messaging_service_sid = svc_result.get("messaging_service_sid")
            if messaging_service_sid:
                db.set_a2p_registration(business_id, messaging_service_sid=messaging_service_sid)

        # --- Step 3: campaign ---
        campaign_sid = biz.get("a2p_campaign_sid")
        if not campaign_sid:
            campaign_result = messaging.create_a2p_campaign(biz, messaging_service_sid, brand_sid)
            if campaign_result.get("status") == "error":
                return {"status": "error", "step": "campaign",
                        "error": campaign_result.get("error", "campaign failed")}
            campaign_sid = campaign_result.get("campaign_sid")
            if campaign_sid:
                db.set_a2p_registration(business_id, campaign_sid=campaign_sid)

        # All three SIDs are in hand -- mark pending + submitted (NEVER approved here).
        db.set_a2p_registration(
            business_id,
            status="pending",
            submitted_at=now,
        )

        result = {"status": "submitted", "path": "A" if path == "sole_prop" else "B"}
        if brand_sid:
            result["brand_sid"] = brand_sid
        if campaign_sid:
            result["campaign_sid"] = campaign_sid
        if messaging_service_sid:
            result["messaging_service_sid"] = messaging_service_sid
        return result

    except Exception as e:
        print(f"[firstback] submit_a2p error (biz {business_id}): {e}",
              file=sys.stderr, flush=True)
        return {"status": "error", "step": "unknown", "error": str(e)}


def flush_blocked_sends(business_id):
    """Replay blocked sends for a business whose A2P has just been approved.

    Implements all 8 safety rules from the AUTO-FLUSH SAFETY SPEC:
    1. Freshness window (FLUSH_MAX_AGE_HOURS, default 6) — skip 'stale'
    2. Opt-out check (db.is_suppressed) — skip 'opted_out'
    3. Quiet-hours — inherited via send_sms transactional=True
    4. Dedupe — mark flushed=1 BEFORE send; on send error mark 'send_error', never reset
    5. Order oldest-first, cap 50
    6. Conversation-coherence — skip 'conversation_progressed' if a real subsequent
       message (direction in/out, non-null provider_sid, created_at > blocked_at) exists
    7. All-stale is correct (no multi-day re-texts)
    8. Still-blocked guard — if send_sms returns 'blocked', log + STOP

    Returns {'flushed': N, 'skipped': N, 'errors': N}. Never raises.
    """
    flushed = skipped = errors = 0
    try:
        biz = db.get_business(business_id)
        if not biz:
            print(f"[firstback] flush_blocked_sends: business {business_id} not found",
                  file=sys.stderr, flush=True)
            return {"flushed": flushed, "skipped": skipped, "errors": errors}

        max_age_hours = getattr(config, "FLUSH_MAX_AGE_HOURS", 6)
        now_utc = datetime.now(timezone.utc)
        cutoff = now_utc - timedelta(hours=max_age_hours)

        rows = db.get_blocked_sends(business_id, flushed=False, limit=50)

        for row in rows:
            row_id = row["id"]
            to = row["to_number"]
            body = row["body"]
            lead_id = row.get("lead_id")
            blocked_at_raw = row.get("blocked_at") or ""

            # --- Rule 1: freshness window ---
            try:
                blocked_at = datetime.fromisoformat(
                    blocked_at_raw.replace("Z", "+00:00"))
                if blocked_at.tzinfo is None:
                    blocked_at = blocked_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                # Can't parse — treat as stale to be safe
                db.mark_flush_skipped(row_id, "stale")
                skipped += 1
                continue

            if blocked_at < cutoff:
                db.mark_flush_skipped(row_id, "stale")
                skipped += 1
                continue

            # --- Rule 2: opt-out ---
            if db.is_suppressed(business_id, to):
                db.mark_flush_skipped(row_id, "opted_out")
                skipped += 1
                continue

            # --- Rule 6: conversation-coherence ---
            if lead_id is not None:
                try:
                    messages = db.get_messages(lead_id)
                    has_subsequent = any(
                        m.get("direction") in ("in", "out")
                        and m.get("provider_sid")
                        and (m.get("created_at") or "") > blocked_at_raw
                        for m in messages
                    )
                    if has_subsequent:
                        db.mark_flush_skipped(row_id, "conversation_progressed")
                        skipped += 1
                        continue
                except Exception as e:
                    print(f"[firstback] flush_blocked_sends: get_messages error "
                          f"(lead {lead_id}): {e}", file=sys.stderr, flush=True)

            # --- Rule 4: dedupe — mark flushed BEFORE send ---
            db.mark_flushed(row_id)

            # --- Rule 3 + 8: send via send_sms (transactional=True) ---
            result = messaging.send_sms(biz, to, body, lead_id=lead_id,
                                        gate=True, transactional=True)

            if isinstance(result, dict) and result.get("status") == "blocked":
                # Rule 8: should be impossible post-approval; log + STOP
                print(f"[firstback] flush_blocked_sends: send_sms returned blocked "
                      f"for biz {business_id} — state inconsistency; stopping flush",
                      file=sys.stderr, flush=True)
                errors += 1
                return {"flushed": flushed, "skipped": skipped, "errors": errors}
            elif isinstance(result, dict) and result.get("status") == "error":
                # Rule 4: on send error, mark send_error (already marked flushed, no reset)
                db.mark_flush_skipped(row_id, "send_error")
                errors += 1
                continue

            flushed += 1

    except Exception as e:
        print(f"[firstback] flush_blocked_sends error (biz {business_id}): {e}",
              file=sys.stderr, flush=True)

    return {"flushed": flushed, "skipped": skipped, "errors": errors}


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
    there's nothing to sync or Twilio isn't configured. Never raises.

    When the transition is pending->approved, fires flush_blocked_sends inside its
    own try/except so a flush failure never breaks the sync tick.
    """
    biz = (business if isinstance(business, dict) else db.get_business(business))
    if not biz:
        return "unregistered"
    current = compliance.a2p_status(biz)
    raw = messaging.fetch_a2p_campaign_status(biz.get("a2p_messaging_service_sid"),
                                              biz.get("a2p_campaign_sid"))
    mapped = _A2P_STATUS_MAP.get((raw or "").upper())
    if raw and mapped is None:
        print(f"[firstback] a2p unmapped campaign_status {raw!r} (biz {biz['id']}); leaving unchanged",
              file=sys.stderr, flush=True)
    if mapped and mapped != current:
        db.set_a2p_status(biz["id"], mapped)
        # Auto-flush: on the pending->approved transition, replay any blocked sends.
        if mapped == "approved" and current != "approved":
            try:
                flush_blocked_sends(biz["id"])
            except Exception as _fe:
                print(f"[firstback] a2p_sync flush error (biz {biz['id']}): {_fe}",
                      file=sys.stderr, flush=True)
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
# keep taking calls normally and only the MISSED ones reach FirstBack. {num} is the
# FirstBack number. These are dialed once on the contractor's own phone.
CARRIER_FORWARD_CODES = {
    "verizon": {"label": "Verizon", "activate": "*71{num}", "cancel": "*73",
                "note": "Forwards calls you don't answer or when you're on another call."},
    "att": {"label": "AT&T", "activate": "*92{num}", "cancel": "*93",
            "note": "Forwards calls you don't answer. To also forward when you're busy, "
                    "dial *90 then your FirstBack number."},
    "tmobile": {"label": "T-Mobile", "activate": "**61*{num}#", "cancel": "##61#",
                "note": "Forwards calls you don't answer. For every missed case at once, "
                        "use the universal code **004* instead."},
    "uscellular": {"label": "US Cellular", "activate": "**61*{num}#", "cancel": "##61#",
                   "note": "Forwards calls you don't answer."},
    "other": {"label": "Other / GSM phone", "activate": "**004*{num}#", "cancel": "##004#",
              "note": "Universal GSM code: forwards busy, no-answer, and unreachable in one step."},
}


def forwarding_code(carrier, number):
    """The exact conditional-forwarding star code for a carrier, with the FirstBack
    number baked in, plus the cancel code and a plain-English note. Falls back to the
    universal GSM code for an unknown carrier."""
    c = CARRIER_FORWARD_CODES.get(carrier) or CARRIER_FORWARD_CODES["other"]
    digits = "".join(ch for ch in (number or "") if ch.isdigit() or ch == "+")
    return {"carrier": carrier if carrier in CARRIER_FORWARD_CODES else "other",
            "label": c["label"], "note": c["note"], "cancel": c["cancel"],
            "activate": c["activate"].replace("{num}", digits)}


# ---- SF-7: Forwarding sentinel (verify call-forwarding is live) ----

def _sentinel_twiml_url():
    """Absolute URL for the sentinel TwiML handler. Uses PUBLIC_BASE_URL
    (FIRSTBACK_PUBLIC_URL env var) when available (production / Render) so
    Twilio can reach the public host."""
    import config
    base = getattr(config, "PUBLIC_BASE_URL", None) or ""
    base = base.rstrip("/")
    return (base + "/webhooks/twilio/voice/sentinel-twiml") if base else None


def send_sentinel_call(business_id, to_number=None):
    """Place an outbound verification call that proves carrier forwarding is live.

    Dial-through mode passes nothing: we call the business's own `forward_to`.
    Catcher mode passes the owner's cell explicitly via `to_number` (forward_to is
    blank in catcher mode by design), because catcher relies on the same carrier
    conditional-forwarding -- dialing the owner's cell rings back to the FirstBack
    number when forwarding is set, which is what the inbound webhook confirms.

    Either way the SID is stored via db.set_forwarding_sentinel and the inbound
    webhook (twilio_voice_inbound) is the ONLY place that sets forwarding_confirmed=True.

    Returns a dict with 'status': one of 'placed', 'simulated', 'error',
    'no_forward_to', 'no_twiml_url'.

    [DECIDED] Honesty rule: this function NEVER sets forwarding_confirmed=True.
    Confirmed=True is set ONLY in twilio_voice_inbound when the inbound CallSid
    matches the stored sentinel SID."""
    biz = db.get_business(business_id) if not isinstance(business_id, dict) else business_id
    if not biz:
        return {"status": "error", "error": "business not found"}
    target = (to_number or biz.get("forward_to") or "").strip()
    if not target:
        return {"status": "no_forward_to"}
    twiml_url = _sentinel_twiml_url()
    if not twiml_url:
        # Dev/local: no public URL configured. Return 'simulated' so the route can
        # show an honest manual-fallback message.
        return {"status": "simulated"}
    result = messaging.place_call(biz, target, twiml_url)
    if result.get("status") == "placed":
        sid = result.get("sid")
        now = datetime.now(timezone.utc).isoformat()
        try:
            db.set_forwarding_sentinel(biz["id"], sid, now)
        except Exception as e:
            print(f"[firstback] set_forwarding_sentinel failed (biz {biz['id']}): {e}",
                  file=sys.stderr, flush=True)
        return {"status": "placed", "sid": sid}
    return result


def check_forwarding_health():
    """Weekly re-probe: for every confirmed forwarding business whose last probe
    is null or >7 days old, place a new sentinel call and record the probe time.

    If a prior sentinel was placed and never confirmed within the timeout window,
    flip forwarding_confirmed=False and fire a 'forwarding_lost' alert to the owner.
    Called by reminders.tick_once (Agent 3 wires this). Never raises."""
    try:
        import alerts
        businesses = db.list_businesses()
        now_utc = datetime.now(timezone.utc)
        for biz in businesses:
            if not biz.get("forwarding_confirmed"):
                continue
            # Check if a prior sentinel was placed but never confirmed (timed out).
            sentinel_sid = biz.get("forwarding_sentinel_sid")
            sentinel_at_raw = biz.get("forwarding_sentinel_at")
            if sentinel_sid and sentinel_at_raw:
                try:
                    sent_at = datetime.fromisoformat(
                        sentinel_at_raw.replace("Z", "+00:00"))
                    if (now_utc - sent_at).total_seconds() > _SENTINEL_TIMEOUT_SECS:
                        # Probe timed out and was never confirmed -> forwarding lost.
                        db.set_forwarding_confirmed(biz["id"], False)
                        db.set_forwarding_sentinel(biz["id"], None, None)
                        try:
                            # Agent 1 adds "forwarding_lost" to ALERT_KINDS; we
                            # call notify_async (the existing fan-out path) so it
                            # works as soon as the kind is registered.
                            alerts.notify_async(biz, "forwarding_lost", {})
                        except Exception as ae:
                            print(f"[firstback] forwarding_lost alert failed "
                                  f"(biz {biz['id']}): {ae}", file=sys.stderr, flush=True)
                        continue
                except (ValueError, TypeError):
                    pass
            # Check if it's time for a weekly re-probe.
            last_probe_raw = biz.get("forwarding_last_probe_at")
            needs_probe = True
            if last_probe_raw:
                try:
                    last_probe = datetime.fromisoformat(
                        last_probe_raw.replace("Z", "+00:00"))
                    age_days = (now_utc - last_probe).total_seconds() / 86400
                    needs_probe = age_days > _PROBE_INTERVAL_DAYS
                except (ValueError, TypeError):
                    needs_probe = True
            if needs_probe:
                try:
                    db.set_forwarding_probe(biz["id"])
                    send_sentinel_call(biz["id"])
                except Exception as e:
                    print(f"[firstback] forwarding probe failed (biz {biz['id']}): {e}",
                          file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[firstback] check_forwarding_health error: {e}",
              file=sys.stderr, flush=True)
