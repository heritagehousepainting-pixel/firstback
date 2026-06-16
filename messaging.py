"""Outbound SMS + Twilio plumbing for RingBack, scoped per business.

This is the single seam every outbound text routes through: reminders, owner
alerts, and the callback system's replies. It mirrors google_cal.py's shape:

  * Gated: `configured()` is False unless Twilio credentials are set, and every
    entry point is a safe no-op / simulated send when they aren't.
  * Defensive: any network/API error is swallowed and logged with the
    "[ringback]" prefix, never breaking a reminder, an alert, or a reply.
  * Light: uses `requests` against Twilio's REST endpoints (no Twilio SDK). The
    X-Twilio-Signature check is implemented with the stdlib.

When Twilio IS configured (and a from-number is available) a message sends for
real. When it ISN'T, the message is recorded as an outbound row on the lead's
thread (so the simulator/dashboard still shows it) and reported with a
"simulated" status, so messaging stays useful in demo mode and honest about what
actually went out.

Number note: real sending needs an E.164 from-number Twilio owns -- the app-wide
TWILIO_FROM_NUMBER, or a business's own `twilio_number` (provisioned here), which
takes precedence. See CALLBACK_SYSTEM_PLAN.md: this module is Phase 0; inbound
webhooks + missed-call detection are Phase 1, the AI voice callback is Phase 3.
"""
import base64
import hashlib
import hmac
import sys

import db
from config import (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER,
                    PUBLIC_BASE_URL)

# Twilio's REST API base. We hit /Accounts/{SID}/... with HTTP basic auth.
API_BASE = "https://api.twilio.com/2010-04-01"

# Webhook paths a provisioned number points at (wired onto routes in Phase 1).
VOICE_INBOUND_PATH = "/webhooks/twilio/voice/inbound"
SMS_INBOUND_PATH = "/webhooks/twilio/sms/inbound"


def configured():
    """True if the app has Twilio API credentials. (Whether a given send can go
    out also depends on a from-number being available -- the app-wide
    TWILIO_FROM_NUMBER or the business's own provisioned number; see send_sms.)"""
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)


def _from_number(business):
    """The number a business texts from: its own provisioned Twilio number if it
    has one, otherwise the app-wide TWILIO_FROM_NUMBER. Never the display `phone`,
    which isn't an E.164 number Twilio can send from. Empty string if neither set."""
    if isinstance(business, dict) and business.get("twilio_number"):
        return business["twilio_number"]
    return TWILIO_FROM_NUMBER


def send_sms(business, to, body, lead_id=None, status_callback=None):
    """Send an SMS for a business, or simulate it when Twilio can't send.

    Returns a status dict whose "status" is one of:
      "sent"       -- handed to Twilio (dict also carries the Message "sid")
      "simulated"  -- Twilio not configured / no from-number; recorded on the
                      lead thread if known
      "suppressed" -- the recipient has opted out for this business (STOP); not sent
      "skipped"    -- no usable destination number or empty body (with a "reason")
      "error"      -- Twilio configured but the API call failed (logged; "error")

    When `lead_id` is given, a successful or simulated send is also recorded as an
    outbound ("out") message on that lead's thread, so the dashboard/simulator
    shows what the customer received. (No row is written on an error or a
    suppression, so the thread never implies a text went out when it didn't.)
    Conversation handlers that already record the reply themselves pass no
    `lead_id`; owner alerts pass none because they go to the owner, not a thread.
    """
    to = (to or "").strip()
    body = (body or "").strip()
    if not to:
        return {"status": "skipped", "reason": "no destination number"}
    if not body:
        return {"status": "skipped", "reason": "empty body"}
    biz_id = business.get("id") if isinstance(business, dict) else None
    # Respect opt-outs (STOP / any revocation) before anything leaves the system.
    if biz_id and db.is_suppressed(biz_id, to):
        return {"status": "suppressed"}

    sender = _from_number(business)
    if not configured() or not sender:
        # Simulated: post to the lead's thread (when known) so the demo still
        # shows the message; never pretend it really went out.
        if lead_id is not None:
            db.add_message(lead_id, "out", body)
        return {"status": "simulated"}

    # Real send via Twilio's REST API (HTTP basic auth: account SID / auth token).
    data = {"From": sender, "To": to, "Body": body}
    if status_callback:
        data["StatusCallback"] = status_callback
    import requests
    try:
        r = requests.post(
            f"{API_BASE}/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), data=data, timeout=20)
        r.raise_for_status()
        sid = r.json().get("sid")
    except Exception as e:
        print(f"[ringback] twilio send failed (biz {biz_id} -> {to}): {e}",
              file=sys.stderr, flush=True)
        return {"status": "error", "error": str(e)}
    # Mirror the sent text onto the thread (with its provider id for delivery
    # reconciliation) so the contractor sees what went out.
    if lead_id is not None:
        db.add_message(lead_id, "out", body, provider_sid=sid)
    return {"status": "sent", "sid": sid}


def place_call(business, to, twiml_url, status_callback=None):
    """Place an outbound voice call that hands off to TwiML at `twiml_url` (our
    ConversationRelay endpoint, served by voice_service.py). Real Twilio when
    configured AND a from-number exists; else a SIMULATED no-op. Never raises.
    Returns {"status": "placed"|"simulated"|"error", ...}."""
    sender = _from_number(business)
    if not configured() or not sender or not twiml_url:
        return {"status": "simulated"}
    data = {"To": to, "From": sender, "Url": twiml_url}
    if status_callback:
        data["StatusCallback"] = status_callback
    import requests
    try:
        r = requests.post(
            f"{API_BASE}/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), data=data, timeout=20)
        r.raise_for_status()
        return {"status": "placed", "sid": r.json().get("sid")}
    except Exception as e:
        bid = business.get("id") if isinstance(business, dict) else None
        print(f"[ringback] twilio place_call failed (biz {bid} -> {to}): {e}",
              file=sys.stderr, flush=True)
        return {"status": "error", "error": str(e)}


# ---- Number provisioning (helpers; exercised once real credentials exist) ----
def search_numbers(area_code=None, contains=None, limit=10):
    """Available local US numbers (voice + SMS capable). [] if not configured or
    on error."""
    if not configured():
        return []
    params = {"VoiceEnabled": "true", "SmsEnabled": "true", "PageSize": limit}
    if area_code:
        params["AreaCode"] = area_code
    if contains:
        params["Contains"] = contains
    import requests
    try:
        r = requests.get(
            f"{API_BASE}/Accounts/{TWILIO_ACCOUNT_SID}/AvailablePhoneNumbers/US/Local.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), params=params, timeout=20)
        r.raise_for_status()
        return [n.get("phone_number")
                for n in r.json().get("available_phone_numbers", [])]
    except Exception as e:
        print(f"[ringback] twilio search_numbers failed: {e}",
              file=sys.stderr, flush=True)
        return []


def provision_number(business_id, phone=None, area_code=None, base_url=None):
    """Buy a number (specific `phone`, else by `area_code`), point its Voice + SMS
    webhooks at this app, and store it on the business. Returns the E.164 number or
    None. Needs PUBLIC_BASE_URL (or `base_url`) so Twilio can reach our webhooks."""
    if not configured():
        return None
    base = (base_url or PUBLIC_BASE_URL or "").rstrip("/")
    data = {}
    if phone:
        data["PhoneNumber"] = phone
    elif area_code:
        data["AreaCode"] = area_code
    if base:
        data["VoiceUrl"] = base + VOICE_INBOUND_PATH
        data["VoiceMethod"] = "POST"
        data["SmsUrl"] = base + SMS_INBOUND_PATH
        data["SmsMethod"] = "POST"
    import requests
    try:
        r = requests.post(
            f"{API_BASE}/Accounts/{TWILIO_ACCOUNT_SID}/IncomingPhoneNumbers.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), data=data, timeout=30)
        r.raise_for_status()
        j = r.json()
        num, sid = j.get("phone_number"), j.get("sid")
        if num:
            db.set_business_twilio(business_id, num, sid or "")
        return num
    except Exception as e:
        print(f"[ringback] twilio provision_number failed (biz {business_id}): {e}",
              file=sys.stderr, flush=True)
        return None


# ---- Webhook authenticity ----
def valid_signature(url, params, signature, auth_token=None):
    """Verify Twilio's X-Twilio-Signature for a webhook request.

    `url` MUST be the exact public URL Twilio called (scheme + host + path +
    query); for POSTs, `params` is the form dict. Behind a TLS-terminating proxy
    or ngrok, reconstruct the https URL before calling this -- a mismatched scheme
    is the #1 cause of false rejections.

    Algorithm (per Twilio): HMAC-SHA1, keyed with the auth token, over the URL
    concatenated with each POST (key, value) pair sorted by key; base64-encoded;
    compared in constant time.
    """
    token = (auth_token if auth_token is not None else TWILIO_AUTH_TOKEN) or ""
    # Fail CLOSED when Twilio is unconfigured: an empty auth token yields a signature
    # any anonymous caller could compute, so reject rather than authenticate against "".
    if not token:
        return False
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params or {}))
    digest = hmac.new(token.encode("utf-8"), data.encode("utf-8"),
                      hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature or "")
