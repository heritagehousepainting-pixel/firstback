"""Real Google Calendar sync for FirstBack, scoped per business.

Design goals:
  * Gated: every entry point is a safe no-op unless Google is CONFIGURED
    (GOOGLE_CLIENT_ID/SECRET set) and the business is CONNECTED (has tokens).
  * Defensive: any network/API error is swallowed and logged, never breaking a
    customer reply or a booking. Availability simply falls back to the in-house
    calendar; a missed event-create is logged.
  * Light: uses `requests` against Google's REST endpoints (no heavy Google SDK).

OAuth: standard web server flow. We ask for offline access so we get a refresh
token, then mint short-lived access tokens on demand.

Timezone note: estimate slots are wall-clock in the business's local time. This
build assumes the server's local timezone is the business's timezone (single
region). Real per-business timezones are a later item (see audit backlog #8).
"""
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import db
from google_oauth import access_is_fresh
from config import (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI,
                    ESTIMATE_TIMES, BOOKING_HORIZON_DAYS)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/calendar/v3"
# Least-privilege: read busy times (freebusy) + create our own events.
SCOPES = ("https://www.googleapis.com/auth/calendar.events "
          "https://www.googleapis.com/auth/calendar.readonly")


def configured():
    """True if the app has Google OAuth credentials at all."""
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def is_connected(business_id):
    """True if this business has linked a Google account (has a refresh token)."""
    intg = db.get_integration(business_id, "google")
    return bool(intg and intg.get("connected") and intg.get("refresh_token"))


# ---- OAuth flow ----
def auth_url(state):
    """The Google consent URL to redirect the contractor to."""
    return AUTH_URL + "?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",     # ask for a refresh token
        "prompt": "consent",          # ensure a refresh token is returned
        "include_granted_scopes": "true",
        "state": state,
    })


def connect_with_code(business_id, code):
    """Exchange an auth code for tokens and store them for the business."""
    import requests
    r = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json()
    db.set_google_tokens(business_id, tok.get("access_token"),
                         tok.get("refresh_token"), _expiry_iso(tok), "primary")


def disconnect(business_id):
    """Forget a business's Google tokens and mark it disconnected."""
    db.set_google_tokens(business_id, None, None, None)


def _expiry_iso(tok):
    secs = int(tok.get("expires_in", 3600))
    return (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()


def _access_token(business_id):
    """A valid access token for the business, refreshing if needed. None if the
    business is not connected or a refresh fails."""
    intg = db.get_integration(business_id, "google")
    if not intg or not intg.get("refresh_token"):
        return None
    if intg.get("access_token") and access_is_fresh(intg.get("token_expiry")):
        return intg["access_token"]
    # Refresh.
    import requests
    try:
        r = requests.post(TOKEN_URL, data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": intg["refresh_token"],
            "grant_type": "refresh_token",
        }, timeout=30)
        r.raise_for_status()
        tok = r.json()
        # Refresh responses usually omit the refresh token; keep the stored one.
        db.set_google_tokens(business_id, tok.get("access_token"),
                             tok.get("refresh_token") or intg["refresh_token"],
                             _expiry_iso(tok), intg.get("calendar_id") or "primary")
        return tok.get("access_token")
    except Exception as e:
        print(f"[firstback] google token refresh failed (biz {business_id}): {e}",
              file=sys.stderr, flush=True)
        return None


# ---- Availability (freebusy) + event creation ----
def _slot_dt(day_iso, time_key_str):
    """Local, tz-aware datetime for a slot ('2026-06-15', '09:00')."""
    y, m, d = (int(x) for x in day_iso.split("-"))
    hh, mm = (int(x) for x in time_key_str.split(":"))
    return datetime(y, m, d, hh, mm).astimezone()


def busy_slot_ids(business_id):
    """Slot ids ('YYYY-MM-DD@HH:MM') across the booking horizon that conflict
    with the business's Google calendar. Empty set if not connected or on error,
    so the AI simply falls back to the in-house calendar."""
    token = _access_token(business_id)
    if not token:
        return set()
    intg = db.get_integration(business_id, "google") or {}
    cal_id = intg.get("calendar_id") or "primary"
    today = datetime.now().date()
    win_start = _slot_dt((today + timedelta(days=1)).isoformat(), "00:00")
    win_end = _slot_dt((today + timedelta(days=BOOKING_HORIZON_DAYS + 1)).isoformat(), "00:00")
    import requests
    try:
        r = requests.post(f"{API_BASE}/freeBusy",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"timeMin": win_start.isoformat(),
                                "timeMax": win_end.isoformat(),
                                "items": [{"id": cal_id}]}, timeout=20)
        r.raise_for_status()
        intervals = r.json().get("calendars", {}).get(cal_id, {}).get("busy", [])
    except Exception as e:
        print(f"[firstback] google freebusy failed (biz {business_id}): {e}",
              file=sys.stderr, flush=True)
        return set()
    return _slots_conflicting(intervals, today)


def _slots_conflicting(intervals, today):
    """Pure helper (unit-testable): given Google busy intervals [{start,end}],
    return the set of estimate slot ids that overlap one. Each estimate is
    treated as a one-hour block."""
    busy = []
    for iv in intervals:
        try:
            bs = datetime.fromisoformat(iv["start"].replace("Z", "+00:00"))
            be = datetime.fromisoformat(iv["end"].replace("Z", "+00:00"))
            busy.append((bs, be))
        except (ValueError, KeyError, AttributeError):
            continue
    out = set()
    for i in range(1, BOOKING_HORIZON_DAYS + 1):
        day = (today + timedelta(days=i)).isoformat()
        for t in ESTIMATE_TIMES:
            tk = db.time_key(t)
            s = _slot_dt(day, tk)
            e = s + timedelta(hours=1)
            if any(bs < e and s < be for (bs, be) in busy):
                out.add(f"{day}@{tk}")
    return out


def create_event(business_id, summary, description, day_iso, time_key_str):
    """Create a 1-hour event on the business's Google calendar. Returns the event
    id, or None if not connected or on error."""
    token = _access_token(business_id)
    if not token:
        return None
    intg = db.get_integration(business_id, "google") or {}
    cal_id = intg.get("calendar_id") or "primary"
    start = _slot_dt(day_iso, time_key_str)
    end = start + timedelta(hours=1)
    import requests
    try:
        r = requests.post(f"{API_BASE}/calendars/{cal_id}/events",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"summary": summary, "description": description,
                                "start": {"dateTime": start.isoformat()},
                                "end": {"dateTime": end.isoformat()}}, timeout=20)
        r.raise_for_status()
        return r.json().get("id")
    except Exception as e:
        print(f"[firstback] google event create failed (biz {business_id}): {e}",
              file=sys.stderr, flush=True)
        return None


def create_event_async(business_id, summary, description, day_iso, time_key_str):
    """Fire-and-forget event creation so booking never blocks on Google."""
    import threading
    threading.Thread(target=create_event,
                     args=(business_id, summary, description, day_iso, time_key_str),
                     daemon=True).start()
