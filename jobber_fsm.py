"""Jobber FSM provider — implements FSMProvider for Jobber.

Read-only pull (clients, jobs) + one additive write (quote request).

Gated: a safe no-op unless JOBBER_CLIENT_ID + JOBBER_CLIENT_SECRET are set.
Defensive: every network/API error is swallowed and logged; never raises into
a request or a scheduler tick.

Requires Jobber Connect or higher for write_quote_requests scope.
See: https://developer.getjobber.com/docs/api_overview/

Token lifecycle mirrors google_contacts.py:
  * _access_token: check access_is_fresh, refresh if stale, fail-open on error.
  * db.set_oauth_tokens / db.get_integration for all token persistence.
  * token_crypto handles encrypt/decrypt at rest.
"""
import sys
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import db
from fsm_provider import FSMProvider
from google_oauth import access_is_fresh
from config import (
    JOBBER_CLIENT_ID, JOBBER_CLIENT_SECRET,
    JOBBER_REDIRECT_URI,
)

AUTH_URL    = "https://api.getjobber.com/api/oauth/authorize"
TOKEN_URL   = "https://api.getjobber.com/api/oauth/token"
GRAPHQL_URL = "https://api.getjobber.com/api/graphql"
PROVIDER    = "jobber"
SCOPES      = "read_clients read_jobs write_quote_requests"
_MAX_PAGES  = 20   # safety cap: 20 × 50 = 1 000 clients per sync


class JobberFSM(FSMProvider):
    PROVIDER_KEY = PROVIDER

    # ------------------------------------------------------------------
    # Gate
    # ------------------------------------------------------------------
    def configured(self) -> bool:
        """True if the app has Jobber OAuth credentials."""
        return bool(JOBBER_CLIENT_ID and JOBBER_CLIENT_SECRET)

    def is_connected(self, business_id: int) -> bool:
        """True if this business has a valid Jobber refresh token."""
        intg = db.get_integration(business_id, PROVIDER)
        return bool(intg and intg.get("connected") and intg.get("refresh_token"))

    # ------------------------------------------------------------------
    # OAuth flow
    # ------------------------------------------------------------------
    def auth_url(self, state: str) -> str:
        """OAuth2 authorization URL (redirect the owner here)."""
        return AUTH_URL + "?" + urlencode({
            "client_id":     JOBBER_CLIENT_ID,
            "redirect_uri":  JOBBER_REDIRECT_URI,
            "response_type": "code",
            "scope":         SCOPES,
            "state":         state,
        })

    def connect_with_code(self, business_id: int, code: str) -> None:
        """Exchange auth code for tokens and store them.

        Raises on HTTP error so the route can redirect to an error page.
        """
        r = requests.post(TOKEN_URL, data={
            "code":          code,
            "client_id":     JOBBER_CLIENT_ID,
            "client_secret": JOBBER_CLIENT_SECRET,
            "redirect_uri":  JOBBER_REDIRECT_URI,
            "grant_type":    "authorization_code",
        }, timeout=30)
        r.raise_for_status()
        tok = r.json()
        db.set_oauth_tokens(
            business_id, PROVIDER,
            tok.get("access_token"),
            tok.get("refresh_token"),
            _expiry_iso(tok),
        )

    def disconnect(self, business_id: int) -> None:
        """Clear Jobber tokens for this business.

        Keeps already-synced contacts (contact_suggestions) in place —
        a disconnect is a de-auth, not a data wipe.
        """
        db.set_oauth_tokens(business_id, PROVIDER, None, None, None)

    # ------------------------------------------------------------------
    # Token management (internal)
    # ------------------------------------------------------------------
    def _access_token(self, business_id: int):
        """Return a valid access token, refreshing if needed.

        Returns None when not connected or on any refresh failure
        (fail-open: sync skips, screening is never broken).
        """
        intg = db.get_integration(business_id, PROVIDER)
        if not intg or not intg.get("refresh_token"):
            return None
        if intg.get("access_token") and access_is_fresh(intg.get("token_expiry")):
            return intg["access_token"]
        try:
            r = requests.post(TOKEN_URL, data={
                "client_id":     JOBBER_CLIENT_ID,
                "client_secret": JOBBER_CLIENT_SECRET,
                "refresh_token": intg["refresh_token"],
                "grant_type":    "refresh_token",
            }, timeout=30)
            r.raise_for_status()
            tok = r.json()
            # Refresh responses often omit the refresh token; keep the stored one.
            db.set_oauth_tokens(
                business_id, PROVIDER,
                tok.get("access_token"),
                tok.get("refresh_token") or intg["refresh_token"],
                _expiry_iso(tok),
            )
            return tok.get("access_token")
        except Exception as e:
            print(
                f"[firstback] jobber token refresh failed (biz {business_id}): {e}",
                file=sys.stderr, flush=True,
            )
            return None

    # ------------------------------------------------------------------
    # GraphQL helper
    # ------------------------------------------------------------------
    def _gql(self, business_id: int, query: str, variables: dict = None):
        """Execute a Jobber GraphQL query. Returns parsed JSON or None on error."""
        token = self._access_token(business_id)
        if not token:
            return None
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        try:
            r = requests.post(
                GRAPHQL_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-JOBBER-GRAPHQL-VERSION": "2024-02-14",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(
                f"[firstback] jobber graphql error (biz {business_id}): {e}",
                file=sys.stderr, flush=True,
            )
            return None

    # ------------------------------------------------------------------
    # Fetch clients (paginated GraphQL)
    # ------------------------------------------------------------------
    def fetch_clients(self, business_id: int) -> list:
        """Return this business's Jobber clients as [{name, phones, email}].

        Paginates via GraphQL cursors up to _MAX_PAGES. Returns [] when not
        connected or on any error.
        """
        _CLIENTS_QUERY = """
        query FetchClients($first: Int!, $after: String) {
          clients(first: $first, after: $after) {
            nodes {
              name
              email
              phones { number }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        out = []
        cursor = None
        for _ in range(_MAX_PAGES):
            variables = {"first": 50}
            if cursor:
                variables["after"] = cursor
            data = self._gql(business_id, _CLIENTS_QUERY, variables)
            if not data:
                break
            clients_data = (data.get("data") or {}).get("clients") or {}
            for node in (clients_data.get("nodes") or []):
                phones = [p["number"] for p in (node.get("phones") or []) if p.get("number")]
                if phones:
                    out.append({
                        "name":   (node.get("name") or "").strip(),
                        "phones": phones,
                        "email":  (node.get("email") or "").strip(),
                    })
            page_info = clients_data.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
        return out

    # ------------------------------------------------------------------
    # Fetch jobs
    # ------------------------------------------------------------------
    def fetch_jobs(self, business_id: int) -> list:
        """Return open/completed jobs from the last 90 days as [{title, status, client_phone}].

        Returns [] on error or when not connected.
        """
        _JOBS_QUERY = """
        query FetchJobs($first: Int!) {
          jobs(first: $first) {
            nodes {
              title
              jobStatus
              client {
                phones { number }
              }
            }
          }
        }
        """
        data = self._gql(business_id, _JOBS_QUERY, {"first": 100})
        if not data:
            return []
        jobs_data = (data.get("data") or {}).get("jobs") or {}
        out = []
        for node in (jobs_data.get("nodes") or []):
            client_phones = [
                p["number"]
                for p in ((node.get("client") or {}).get("phones") or [])
                if p.get("number")
            ]
            out.append({
                "title":        (node.get("title") or "").strip(),
                "status":       (node.get("jobStatus") or "").strip(),
                "client_phone": client_phones[0] if client_phones else "",
            })
        return out

    # ------------------------------------------------------------------
    # Push quote request
    # ------------------------------------------------------------------
    def push_quote_request(self, business_id: int, lead: dict, booking: dict):
        """Push a booked FirstBack estimate as a Jobber quote request.

        Requires Jobber Connect or higher (write_quote_requests scope).
        Returns the new request's id string on success, None on failure.
        Never raises.
        """
        _CREATE_REQUEST_MUTATION = """
        mutation CreateRequest($input: RequestCreateInput!) {
          requestCreate(input: $input) {
            request { id }
            userErrors { message }
          }
        }
        """
        name    = (lead.get("name") or "").strip() or "Unknown"
        phone   = (lead.get("phone") or "").strip()
        when    = (booking.get("when") or booking.get("day") or "").strip()
        note    = f"FirstBack booked estimate — {name}"
        if phone:
            note += f" ({phone})"
        if when:
            note += f" — {when}"
        try:
            data = self._gql(business_id, _CREATE_REQUEST_MUTATION, {
                "input": {
                    "title":   note,
                    "message": note,
                }
            })
            if not data:
                return None
            result = ((data.get("data") or {}).get("requestCreate") or {})
            errors = result.get("userErrors") or []
            if errors:
                msgs = ", ".join(e.get("message", "") for e in errors)
                print(
                    f"[firstback] jobber push_quote_request userErrors (biz {business_id}): {msgs}",
                    file=sys.stderr, flush=True,
                )
                return None
            req = result.get("request") or {}
            return req.get("id")
        except Exception as e:
            print(
                f"[firstback] jobber push_quote_request error (biz {business_id}): {e}",
                file=sys.stderr, flush=True,
            )
            return None


# Module-level singleton (mirrors google_contacts usage pattern)
_provider = JobberFSM()


def configured() -> bool:
    return _provider.configured()


def is_connected(business_id: int) -> bool:
    return _provider.is_connected(business_id)


def auth_url(state: str) -> str:
    return _provider.auth_url(state)


def connect_with_code(business_id: int, code: str) -> None:
    return _provider.connect_with_code(business_id, code)


def disconnect(business_id: int) -> None:
    return _provider.disconnect(business_id)


def fetch_clients(business_id: int) -> list:
    return _provider.fetch_clients(business_id)


def fetch_jobs(business_id: int) -> list:
    return _provider.fetch_jobs(business_id)


def push_quote_request(business_id: int, lead: dict, booking: dict):
    return _provider.push_quote_request(business_id, lead, booking)


def _access_token(business_id: int):
    """Exposed for tests."""
    return _provider._access_token(business_id)


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------
def _expiry_iso(tok):
    secs = int(tok.get("expires_in", 3600))
    return (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()
