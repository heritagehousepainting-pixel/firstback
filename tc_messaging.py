"""Shared outbound-policy + provider primitives for the trades_core kernel (vendored).

The two products log sends into DIFFERENT tables (JobMagnet's business-scoped `messages`
vs FirstBack's lead threads — "false friends" the audit says NOT to merge), so this module
does NOT own logging. It owns the parts that ARE the compliance moat and were duplicated /
divergent:

  * `in_quiet_hours` — the wrap-midnight quiet-hours window (start/end passed in, since the
    two apps name their config keys differently). The basis for closing hole #1.
  * `quiet_blocked` — should THIS send be held for quiet hours? Transactional sends (an
    immediate reply to someone who just contacted the business — FirstBack's missed-call
    text-back) are EXEMPT; marketing/reminder sends are not. See the carve-out note.
  * `twilio_send_sms` — the one Twilio REST call (no SDK), unified from both apps.
  * `valid_signature` — Twilio webhook authenticity, fail-closed when unconfigured.

Each app keeps a thin send_sms that gathers inputs, calls the consent gate
(trades_core/consent.py) + these helpers, then logs its own way.

  ⚖️  QUIET-HOURS CARVE-OUT (needs attorney sign-off — see MIGRATION_NOTES.md):
  TCPA quiet hours (local 21:00–08:00) target telemarketing. A transactional reply to a
  consumer who just called/texted the business is solicited, not telemarketing, so the
  text-back stays exempt (`transactional=True`); reminders/follow-ups/marketing are gated.
  Defaults are conservative; do not widen the exemption without legal review.

Edit trades_core/messaging.py, then run `python3 trades_core/sync.py`.
"""
import base64
import hashlib
import hmac
import sys

API_BASE = "https://api.twilio.com/2010-04-01"


def in_quiet_hours(now, start, end):
    """True if `now` (a datetime, local) falls in the no-send window [start, end).
    Handles a window that wraps midnight (e.g. 21 -> 8). start == end => never."""
    h = now.hour
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end          # wraps midnight


def quiet_blocked(now, start, end, *, transactional=False):
    """Should this send be held for quiet hours? Transactional (immediate replies to an
    inbound) are exempt; everything else is gated. This is the close for hole #1."""
    if transactional:
        return False
    return in_quiet_hours(now, start, end)


def twilio_send_sms(account_sid, auth_token, from_number, to, body, status_callback=None):
    """Send one SMS via Twilio's REST API (HTTP basic auth, no SDK). Returns the
    Message sid. Raises on HTTP/network error — callers decide how to log a failure."""
    import requests
    data = {"From": from_number, "To": to, "Body": body}
    if status_callback:
        data["StatusCallback"] = status_callback
    resp = requests.post(
        f"{API_BASE}/Accounts/{account_sid}/Messages.json",
        auth=(account_sid, auth_token), data=data, timeout=20)
    resp.raise_for_status()
    return resp.json().get("sid", "")


def valid_signature(url, params, signature, auth_token):
    """Verify Twilio's X-Twilio-Signature. `url` is the EXACT public URL Twilio called;
    `params` is the POST form dict. HMAC-SHA1 over url + sorted (k,v) pairs, base64,
    constant-time compare. Fails CLOSED when `auth_token` is empty (an unconfigured
    Twilio yields a signature any anonymous caller could forge)."""
    token = auth_token or ""
    if not token:
        return False
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params or {}))
    digest = hmac.new(token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature or "")
