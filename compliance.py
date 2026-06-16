"""Compliance + deliverability helpers for the callback system (Phase 2).

The real launch gate for live SMS/voice is mostly ACCOUNT + LEGAL work, not code:
A2P 10DLC brand/campaign registration, STIR/SHAKEN attestation, and a TCPA review
(see USER_TO_DO.md and CALLBACK_SYSTEM_PLAN.md section 6). What lives here is the
code half: honoring opt-outs phrased in plain language (not just the word STOP),
keeping automated VOICE inside quiet hours, and reporting how registration-ready a
business is so the UI never implies "live" before it really is.

Not legal advice.
"""
from datetime import datetime

from config import app_tz, QUIET_START, QUIET_END
import consent  # trades_core kernel: the ONE shared opt-out brain (both products)


def detect_revocation(text):
    """True if the message is a plain-language request to stop contact, even when it
    isn't the exact keyword STOP. Delegates to the shared trades_core opt-out detector
    so JobMagnet and RingBack honor opt-outs identically (one auditable implementation)."""
    return consent.opt_out_nlu(text)


def voice_allowed_now(now=None, quiet_start=None, quiet_end=None):
    """True if the current business-local time is inside the allowed window
    [QUIET_START, QUIET_END). The TCPA bars automated calls/texts outside it; we
    gate the AI VOICE callback hard. (An immediate text reply to a call the consumer
    just placed is consumer-initiated, so it is not gated here.) Operators who want
    the stricter 8am-8pm state rule can set RINGBACK_QUIET_END=20."""
    qs = QUIET_START if quiet_start is None else quiet_start
    qe = QUIET_END if quiet_end is None else quiet_end
    hour = (now or datetime.now(app_tz())).hour
    return qs <= hour < qe


# ---- Registration readiness (honest "is this business actually live?") ----
def a2p_status(business):
    return (business or {}).get("a2p_status") or "unregistered"


def a2p_ready(business):
    """True once the tenant's A2P 10DLC brand + campaign are approved (set by the
    registration process documented in USER_TO_DO.md). US carriers filter
    unregistered local-number traffic, so this is what 'can really text' means."""
    return a2p_status(business) == "approved"


def launch_blockers(business, sms_configured):
    """Plain-English list of what still stands between this business and sending for
    real. Empty list means ready. Drives honest Settings / onboarding copy so we
    never show a dormant feature as live."""
    b = business or {}
    out = []
    if not sms_configured:
        out.append("Twilio credentials are not set on the server.")
    if not b.get("twilio_number"):
        out.append("No RingBack phone number is provisioned yet.")
    if not a2p_ready(b):
        out.append(f"A2P 10DLC registration is not approved yet (status: {a2p_status(b)}).")
    if not b.get("forward_to"):
        out.append("No contractor cell is set to ring on inbound calls.")
    return out
