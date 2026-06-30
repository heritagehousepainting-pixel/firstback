"""Workiz FSM provider — implements FSMProvider for Workiz.

Workiz uses a per-account API TOKEN (the contractor generates it in Workiz → Settings → API),
passed in the REST path: https://api.workiz.com/api/v1/{token}/{resource}/. There is no
app-level OAuth, so the contractor pastes their token once via the connect form. We store it in
the integration row's refresh_token column as the connection identity (no schema migration; the
token is also the bearer used on every request).

Platform gate: WORKIZ_ENABLED (off by default) — the operator flips it on once the integration
has been validated against a live Workiz account. Defensive: reads degrade to [], the push to
None; never raises into a request or tick (connect_token raises so the route can show an error).

⚠️ Endpoint/field shapes are built to Workiz's documented v1 API; confirm against a live account
when a real token is available (a wrong shape just syncs/pushes nothing — it never breaks FirstBack).
See: https://developer.workiz.com/
"""
import sys
import requests

import db
from fsm_provider import FSMProvider
from config import WORKIZ_ENABLED

PROVIDER = "workiz"
API_BASE = "https://api.workiz.com/api/v1"
_MAX_PAGES = 20
_PAGE_SIZE = 100
_PHONE_FIELDS = ("Phone", "phone", "SecondPhone", "CellPhone")


class WorkizFSM(FSMProvider):
    PROVIDER_KEY = PROVIDER

    def configured(self) -> bool:
        """True when the operator has enabled Workiz (per-account tokens, so no app creds)."""
        return bool(WORKIZ_ENABLED)

    def is_connected(self, business_id: int) -> bool:
        intg = db.get_integration(business_id, PROVIDER)
        return bool(intg and intg.get("connected") and intg.get("refresh_token"))

    # ---- connection: the owner pastes their Workiz API token (no redirect) ----
    def connect_token(self, business_id: int, token: str) -> None:
        """Store the contractor's Workiz API token. Raises ValueError on empty input."""
        token = (token or "").strip()
        if not token:
            raise ValueError("Workiz API token is required")
        # Store the token as both the bearer (access_token) and the connection identity
        # (refresh_token). is_connected() keys off refresh_token; requests read it via _token().
        db.set_oauth_tokens(business_id, PROVIDER, token, token, None)

    def disconnect(self, business_id: int) -> None:
        db.set_oauth_tokens(business_id, PROVIDER, None, None, None)

    # Redirect-OAuth hooks don't apply.
    def auth_url(self, state: str) -> str:
        raise NotImplementedError("Workiz connects with a pasted API token; use connect_token().")

    def connect_with_code(self, business_id: int, code: str) -> None:
        raise NotImplementedError("Workiz connects with a pasted API token; use connect_token().")

    def _token(self, business_id: int):
        intg = db.get_integration(business_id, PROVIDER)
        return (intg or {}).get("refresh_token")

    def _get(self, business_id: int, resource: str, params: dict = None):
        """Yield each row from paginated GET {API_BASE}/{token}/{resource}/. Workiz returns
        {flag: bool, data: [...]}. Yields nothing on error or when not connected."""
        token = self._token(business_id)
        if not token:
            return
        url = f"{API_BASE}/{token}/{resource}/"
        offset = 0
        for _ in range(_MAX_PAGES):
            q = {"offset": offset, "records": _PAGE_SIZE}
            if params:
                q.update(params)
            try:
                r = requests.get(url, params=q, timeout=30)
                r.raise_for_status()
                body = r.json()
            except Exception as e:
                print(f"[firstback] workiz GET {resource} failed (biz {business_id}): {e}",
                      file=sys.stderr, flush=True)
                return
            rows = body.get("data") or []
            for row in rows:
                yield row
            if len(rows) < _PAGE_SIZE:
                return
            offset += _PAGE_SIZE

    @staticmethod
    def _phones(row: dict) -> list:
        out = []
        for f in _PHONE_FIELDS:
            v = (row.get(f) or "").strip() if isinstance(row.get(f), str) else row.get(f)
            if v:
                out.append(str(v))
        return out

    def fetch_clients(self, business_id: int) -> list:
        """Return Workiz clients as [{name, phones, email}]. Skips anyone with no phone."""
        out = []
        for c in self._get(business_id, "client"):
            name = " ".join(
                p for p in [(c.get("first_name") or c.get("FirstName") or "").strip(),
                            (c.get("last_name") or c.get("LastName") or "").strip()] if p
            ).strip() or (c.get("name") or c.get("Name") or "").strip()
            phones = self._phones(c)
            if not phones:
                continue
            out.append({
                "name": name,
                "phones": phones,
                "email": (c.get("email") or c.get("Email") or "").strip(),
            })
        return out

    def fetch_jobs(self, business_id: int) -> list:
        """Return Workiz jobs as [{title, status, client_phone}]."""
        out = []
        for j in self._get(business_id, "job"):
            phones = self._phones(j)
            out.append({
                "title": (j.get("JobType") or j.get("job_type") or j.get("Comments") or "").strip(),
                "status": (j.get("Status") or j.get("status") or "").strip(),
                "client_phone": phones[0] if phones else "",
            })
        return out

    def push_quote_request(self, business_id: int, lead: dict, booking: dict):
        """Best-effort: create a Workiz lead for the booked estimate. Returns the new id (str)
        on success, None otherwise. Never raises."""
        token = self._token(business_id)
        if not token:
            return None
        name = (lead.get("name") or "").strip() or "FirstBack lead"
        phone = (lead.get("phone") or "").strip()
        when = (booking.get("when") or booking.get("day") or "").strip()
        payload = {
            "FirstName": name,
            "Phone": phone,
            "JobSource": "FirstBack",
            "Comments": f"FirstBack booked estimate{(' — ' + when) if when else ''}",
        }
        try:
            r = requests.post(f"{API_BASE}/{token}/lead/", json=payload, timeout=30)
            r.raise_for_status()
            body = r.json() if r.content else {}
            new_id = body.get("UUID") or body.get("id") or (body.get("data") or {}).get("UUID")
            return str(new_id) if new_id is not None else None
        except Exception as e:
            print(f"[firstback] workiz push lead failed (biz {business_id}): {e}",
                  file=sys.stderr, flush=True)
            return None


_provider = WorkizFSM()


def configured() -> bool:
    return _provider.configured()


def is_connected(business_id: int) -> bool:
    return _provider.is_connected(business_id)


def connect_token(business_id: int, token: str) -> None:
    return _provider.connect_token(business_id, token)


def disconnect(business_id: int) -> None:
    return _provider.disconnect(business_id)


def fetch_clients(business_id: int) -> list:
    return _provider.fetch_clients(business_id)


def fetch_jobs(business_id: int) -> list:
    return _provider.fetch_jobs(business_id)


def push_quote_request(business_id: int, lead: dict, booking: dict):
    return _provider.push_quote_request(business_id, lead, booking)
