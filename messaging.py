"""Outbound SMS + Twilio plumbing for FirstBack, scoped per business.

This is the single seam every outbound text routes through: reminders, owner
alerts, and the callback system's replies. It mirrors google_cal.py's shape:

  * Gated: `configured()` is False unless Twilio credentials are set, and every
    entry point is a safe no-op / simulated send when they aren't.
  * Defensive: any network/API error is swallowed and logged with the
    "[firstback]" prefix, never breaking a reminder, an alert, or a reply.
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
import re
import sys
from datetime import datetime

import compliance
import db
import tc_messaging
from config import (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER,
                    PUBLIC_BASE_URL, ALERT_FROM_NUMBER, QUIET_START, QUIET_END, app_tz)

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


def _alert_from_number(business):
    """The from-number for OWNER alerts. Uses the platform-wide ALERT_FROM_NUMBER when
    set (recommended: avoids depending on the tenant's A2P approval). Falls back to the
    tenant's own number via _from_number() when ALERT_FROM_NUMBER is unset."""
    return ALERT_FROM_NUMBER if ALERT_FROM_NUMBER else _from_number(business)


def send_sms(business, to, body, lead_id=None, status_callback=None, gate=True,
             transactional=False):
    """Send an SMS for a business, or simulate it when Twilio can't send.

    Returns a status dict whose "status" is one of:
      "sent"       -- handed to Twilio (dict also carries the Message "sid")
      "simulated"  -- Twilio not configured / no from-number; recorded on the
                      lead thread if known
      "blocked"    -- Twilio is configured but this tenant's A2P 10DLC brand +
                      campaign aren't approved yet, so a customer-facing send is
                      held back (carriers filter unregistered local traffic); the
                      message is still recorded on the lead thread if known
                      ("reason": "a2p_not_approved"). Owner alerts bypass this with
                      gate=False.
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

    # Phase 1 C — SF-6: transmit-time quiet-hours backstop.
    # Ad-hoc/growth sends (gate=True, i.e. customer-facing) must not fire during
    # quiet hours even if the scheduled-reminder path already guards them.
    # Owner alerts (gate=False) are exempt (they go to the contractor, not a consumer).
    # Transactional immediate text-backs (the missed-call response) are called from the
    # webhook with gate=True too, but they originate from the consumer's own call so
    # they satisfy the solicited-response exemption — that path passes transactional=True.
    # Default: gate=True + no transactional flag → non-transactional → check quiet hours.
    if gate:
        # Use the business's timezone when available, else the app-wide default.
        tz = None
        if isinstance(business, dict) and business.get("timezone"):
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(business["timezone"])
            except Exception:
                pass
        if tz is None:
            tz = app_tz()
        now_local = datetime.now(tz)
        if tc_messaging.quiet_blocked(now_local, QUIET_START, QUIET_END,
                                      transactional=transactional):
            return {"status": "deferred", "reason": "quiet_hours"}

    # A2P 10DLC gate: a customer-facing real send must NOT go out until this tenant's
    # brand+campaign are approved -- carriers filter unregistered local traffic. No-op
    # while unconfigured (the simulated path below still handles demo mode). Owner
    # alerts pass gate=False (they go to the contractor's own phone, not a consumer).
    if gate and configured() and not compliance.a2p_ready(business):
        if lead_id is not None:
            db.add_message(lead_id, "out", body)
        return {"status": "blocked", "reason": "a2p_not_approved"}

    # Owner alerts (gate=False) use the platform alert number so they never depend
    # on the tenant's A2P approval status. Customer-facing sends use the tenant's number.
    sender = _alert_from_number(business) if not gate else _from_number(business)
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
        print(f"[firstback] twilio send failed (biz {biz_id} -> {to}): {e}",
              file=sys.stderr, flush=True)
        return {"status": "error", "error": str(e)}
    # Mirror the sent text onto the thread (with its provider id for delivery
    # reconciliation) so the contractor sees what went out.
    if lead_id is not None:
        db.add_message(lead_id, "out", body, provider_sid=sid)
    return {"status": "sent", "sid": sid}


def outbound_mode(business, to):
    """What a real send to `to` WOULD do right now, computed without sending -- so the
    command center can show an honest confirm before anything leaves. Mirrors the gate
    order in send_sms(). One of:
      "suppressed" -- recipient opted out; nothing would go out
      "simulated"  -- Twilio creds / from-number not set; recorded but not really sent
      "blocked"    -- configured, but A2P 10DLC not approved yet; held back
      "live"       -- would actually send for real
      "skipped"    -- no destination number
    """
    to = (to or "").strip()
    if not to:
        return "skipped"
    biz_id = business.get("id") if isinstance(business, dict) else None
    if biz_id and db.is_suppressed(biz_id, to):
        return "suppressed"
    if not configured() or not _from_number(business):
        return "simulated"
    if not compliance.a2p_ready(business):
        return "blocked"
    return "live"


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
        print(f"[firstback] twilio place_call failed (biz {bid} -> {to}): {e}",
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
        print(f"[firstback] twilio search_numbers failed: {e}",
              file=sys.stderr, flush=True)
        return []


_E164_RE = re.compile(r"\+\d{10,15}")
def to_e164(raw):
    """Canonicalize a user-entered phone to E.164, US-default. Never truncate/guess."""
    s = (raw or "").strip()
    if not s:
        return None
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if plus:
        candidate = "+" + digits
    elif len(digits) == 10:
        candidate = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        candidate = "+" + digits
    else:
        return None
    return candidate if _E164_RE.fullmatch(candidate) else None


def account_owns_number(e164):
    """True iff THIS Twilio account owns exactly one IncomingPhoneNumber == e164. Fails closed."""
    if not configured() or not e164:
        return False
    import requests
    try:
        r = requests.get(
            f"{API_BASE}/Accounts/{TWILIO_ACCOUNT_SID}/IncomingPhoneNumbers.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            params={"PhoneNumber": e164}, timeout=20)
        r.raise_for_status()
        rows = [n for n in r.json().get("incoming_phone_numbers", []) if n.get("phone_number") == e164]
        return len(rows) == 1
    except Exception as e:
        print(f"[firstback] account_owns_number failed ({e164}): {e}", file=sys.stderr, flush=True)
        return False


def attach_owned_number(e164, business_id, base_url=None):
    """Wire an already-owned number's Voice+SMS webhooks at this app and store it. Caller must
    have confirmed ownership first. Returns True on success. Fails closed."""
    if not configured() or not e164:
        return False
    import requests
    base = (base_url or PUBLIC_BASE_URL or "").rstrip("/")
    try:
        r = requests.get(
            f"{API_BASE}/Accounts/{TWILIO_ACCOUNT_SID}/IncomingPhoneNumbers.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            params={"PhoneNumber": e164}, timeout=20)
        r.raise_for_status()
        rows = [n for n in r.json().get("incoming_phone_numbers", []) if n.get("phone_number") == e164]
        if len(rows) != 1:
            return False
        sid = rows[0].get("sid")
        if not sid:
            return False
        data = {}
        if base:
            data["VoiceUrl"] = base + VOICE_INBOUND_PATH; data["VoiceMethod"] = "POST"
            data["SmsUrl"] = base + SMS_INBOUND_PATH; data["SmsMethod"] = "POST"
        if data:
            u = requests.post(
                f"{API_BASE}/Accounts/{TWILIO_ACCOUNT_SID}/IncomingPhoneNumbers/{sid}.json",
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), data=data, timeout=30)
            u.raise_for_status()
        db.set_business_twilio(business_id, e164, sid, webhooks_wired=True)
        return True
    except Exception as e:
        print(f"[firstback] attach_owned_number failed (biz {business_id}, {e164}): {e}", file=sys.stderr, flush=True)
        return False


def fetch_a2p_campaign_status(service_sid, campaign_sid):
    """Twilio's raw US A2P campaign_status (e.g. 'VERIFIED','IN_PROGRESS','FAILED') for a
    messaging service, or None when unconfigured/unknown/on any error. campaign_sid is only a
    'registration exists' guard; the resource is addressed by the service SID via the singleton
    list endpoint .../Compliance/Usa2p. Never raises."""
    if not (configured() and service_sid and campaign_sid):
        return None
    import requests
    try:
        r = requests.get(
            f"https://messaging.twilio.com/v1/Services/{service_sid}/Compliance/Usa2p",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=20)
        r.raise_for_status()
        items = r.json().get("compliance") or []
        return items[0].get("campaign_status") if items else None
    except Exception as e:
        print(f"[firstback] a2p status fetch failed ({campaign_sid}): {e}", file=sys.stderr, flush=True)
        return None


def provision_number(business_id, phone=None, area_code=None, base_url=None,
                     allow_no_webhooks=False):
    """Buy a number (specific `phone`, else by `area_code`), point its Voice + SMS
    webhooks at this app, and store it on the business. Returns the E.164 number or
    None. Needs PUBLIC_BASE_URL (or `base_url`) so Twilio can reach our webhooks.

    Returns None WITHOUT buying when no public URL is set (so we never hand a
    business a number whose inbound calls/texts go nowhere) -- unless
    `allow_no_webhooks=True` is passed to deliberately override that guard. On a
    successful buy the business's `webhooks_wired` flag records whether the Voice +
    SMS webhooks were actually wired (i.e. whether a base URL was present)."""
    if not configured():
        return None
    base = (base_url or PUBLIC_BASE_URL or "").rstrip("/")
    if not base and not allow_no_webhooks:
        print(f"[firstback] provision_number refused (biz {business_id}): "
              f"no PUBLIC_BASE_URL, would buy a number with no webhooks",
              file=sys.stderr, flush=True)
        return None
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
            db.set_business_twilio(business_id, num, sid or "", webhooks_wired=bool(base))
        return num
    except Exception as e:
        print(f"[firstback] twilio provision_number failed (biz {business_id}): {e}",
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
