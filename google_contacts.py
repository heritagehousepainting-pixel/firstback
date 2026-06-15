"""Google Contacts import via the People API.

This is a SEPARATE OAuth connection from the Google Calendar sync (google_cal.py),
by design: calendar and contacts connect and disconnect independently, and a
contractor who has linked their calendar is never forced to re-consent just to try
contact import. It uses the SAME Google app credentials (GOOGLE_CLIENT_ID/SECRET)
but its own redirect URI, the read-only `contacts.readonly` scope, and its own
integrations row (provider='google_contacts').

Like google_cal.py it is:
  * Gated: a safe no-op unless CONFIGURED (client id/secret set) and the business is
    CONNECTED (has a refresh token).
  * Defensive: every network/API error is swallowed and logged, never raised into a
    request; a failed fetch simply imports nothing.
  * Light: raw `requests` against the People REST API, no Google SDK.
  * Privacy-minded: we request only names, phoneNumbers, and organizations, and keep
    only number/name/org (see contact_import).
"""
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import db
import contact_import
from config import (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
                    GOOGLE_CONTACTS_REDIRECT_URI)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PEOPLE_API = "https://people.googleapis.com/v1/people/me/connections"
SCOPES = "https://www.googleapis.com/auth/contacts.readonly"
PROVIDER = "google_contacts"
_MAX_PAGES = 50          # safety cap (50k contacts) so a bad nextPageToken can't loop


def configured():
    """True if the app has Google OAuth credentials at all (shared with Calendar)."""
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def is_connected(business_id):
    """True if this business has linked Google Contacts (has a refresh token)."""
    intg = db.get_integration(business_id, PROVIDER)
    return bool(intg and intg.get("connected") and intg.get("refresh_token"))


# ---- OAuth flow (mirrors google_cal, own redirect + scope + provider) ----
def auth_url(state):
    return AUTH_URL + "?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_CONTACTS_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",          # ask for a refresh token
        "prompt": "consent",               # ensure a refresh token is returned
        "include_granted_scopes": "false",  # keep contacts independent of calendar
        "state": state,
    })


def connect_with_code(business_id, code):
    """Exchange an auth code for tokens and store them for the business."""
    import requests
    r = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_CONTACTS_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json()
    db.set_oauth_tokens(business_id, PROVIDER, tok.get("access_token"),
                        tok.get("refresh_token"), _expiry_iso(tok))


def disconnect(business_id):
    """Forget this business's Google Contacts tokens (a clean disconnect: the refresh
    token is cleared, not just marked inactive)."""
    db.set_oauth_tokens(business_id, PROVIDER, None, None, None)


def _expiry_iso(tok):
    secs = int(tok.get("expires_in", 3600))
    return (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()


def _access_token(business_id):
    """A valid access token for the business, refreshing if needed. None if not
    connected or a refresh fails."""
    intg = db.get_integration(business_id, PROVIDER)
    if not intg or not intg.get("refresh_token"):
        return None
    fresh = True
    if intg.get("access_token") and intg.get("token_expiry"):
        try:
            exp = datetime.fromisoformat(intg["token_expiry"])
            fresh = exp <= datetime.now(timezone.utc) + timedelta(seconds=60)
        except ValueError:
            fresh = True
    if not fresh:
        return intg["access_token"]
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
        # A refresh response usually omits the refresh token; keep the stored one.
        db.set_oauth_tokens(business_id, PROVIDER, tok.get("access_token"),
                            tok.get("refresh_token") or intg["refresh_token"],
                            _expiry_iso(tok))
        return tok.get("access_token")
    except Exception as e:
        print(f"[ringback] google contacts token refresh failed (biz {business_id}): {e}",
              file=sys.stderr, flush=True)
        return None


# ---- Fetch + sync ----
def fetch_contacts(business_id):
    """All of the business's Google connections as [{name, org, phones[]}]. Returns
    [] if not connected or on any error (defensive -- a sync just imports nothing)."""
    token = _access_token(business_id)
    if not token:
        return []
    import requests
    out, page_token = [], None
    for _ in range(_MAX_PAGES):
        params = {"personFields": "names,phoneNumbers,organizations", "pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        try:
            r = requests.get(PEOPLE_API, headers={"Authorization": f"Bearer {token}"},
                             params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[ringback] google contacts fetch failed (biz {business_id}): {e}",
                  file=sys.stderr, flush=True)
            break
        for person in data.get("connections", []):
            c = _person_to_contact(person)
            if c["phones"]:
                out.append(c)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return out


def _person_to_contact(person):
    """Map a People API person -> {name, org, phones[]} (pure, unit-testable)."""
    names = person.get("names") or []
    name = (names[0].get("displayName") if names else "") or ""
    orgs = person.get("organizations") or []
    org = (orgs[0].get("name") if orgs else "") or ""
    phones = [p.get("value") for p in (person.get("phoneNumbers") or []) if p.get("value")]
    return {"name": name.strip(), "org": org.strip(), "phones": phones}


def sync(business_id):
    """Fetch the business's Google contacts and queue them as pre-sorted suggestions.
    Returns contact_import.ingest's summary dict."""
    contacts = fetch_contacts(business_id)
    return contact_import.ingest(business_id, contacts, source="import-google")
