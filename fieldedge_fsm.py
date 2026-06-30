"""FieldEdge FSM provider — implements FSMProvider for FieldEdge.

FieldEdge exposes a partner API that is gated behind credentials rather than a public OAuth
flow, so this connects with a per-account API KEY the contractor pastes once (stored in the
integration refresh_token slot, like Workiz/ServiceTitan — no schema migration). The key is
sent as a Bearer token plus an X-Api-Key header (FieldEdge has used both conventions).

Platform gate: FIELDEDGE_ENABLED (off by default). FIELDEDGE_API_BASE sets the host.

⚠️⚠️ HONESTY NOTE: FieldEdge's API is partner-gated and not openly documented, so the exact
endpoint paths + field names below are BEST-EFFORT PLACEHOLDERS modeled on the common REST
shape. This module is structurally complete and safe (every read degrades to [], the push to
None, nothing ever raises into FirstBack), but it will NOT return real data until the endpoints
are confirmed against FieldEdge's actual API once partner credentials are obtained. Treat it as
scaffolding-waiting-for-the-spec, not a verified integration. Keep FIELDEDGE_ENABLED off until
verified.
"""
import sys
import requests

import db
from fsm_provider import FSMProvider
from config import FIELDEDGE_ENABLED, FIELDEDGE_API_BASE

PROVIDER = "fieldedge"
_MAX_PAGES = 20
_PAGE_SIZE = 100


class FieldEdgeFSM(FSMProvider):
    PROVIDER_KEY = PROVIDER

    def configured(self) -> bool:
        """True only when the operator has explicitly enabled FieldEdge (its API is unverified,
        so this stays off until FIELDEDGE_ENABLED is set)."""
        return bool(FIELDEDGE_ENABLED)

    def is_connected(self, business_id: int) -> bool:
        intg = db.get_integration(business_id, PROVIDER)
        return bool(intg and intg.get("connected") and intg.get("refresh_token"))

    # ---- connection: the owner pastes their FieldEdge API key (no redirect) ----
    def connect_token(self, business_id: int, token: str) -> None:
        """Store the contractor's FieldEdge API key. Raises ValueError on empty input."""
        token = (token or "").strip()
        if not token:
            raise ValueError("FieldEdge API key is required")
        db.set_oauth_tokens(business_id, PROVIDER, token, token, None)

    def disconnect(self, business_id: int) -> None:
        db.set_oauth_tokens(business_id, PROVIDER, None, None, None)

    def auth_url(self, state: str) -> str:
        raise NotImplementedError("FieldEdge connects with a pasted API key; use connect_token().")

    def connect_with_code(self, business_id: int, code: str) -> None:
        raise NotImplementedError("FieldEdge connects with a pasted API key; use connect_token().")

    def _token(self, business_id: int):
        intg = db.get_integration(business_id, PROVIDER)
        return (intg or {}).get("refresh_token")

    def _headers(self, token: str) -> dict:
        # FieldEdge has used both Bearer and X-Api-Key; send both so whichever it expects works.
        return {"Authorization": f"Bearer {token}", "X-Api-Key": token}

    def _get(self, business_id: int, resource: str, params: dict = None):
        """Yield rows from paginated GET {base}/{resource}. PLACEHOLDER shape: expects
        {data: [...], hasMore: bool} or a bare list. Yields nothing on error / not connected."""
        token = self._token(business_id)
        if not token:
            return
        url = f"{FIELDEDGE_API_BASE.rstrip('/')}/{resource}"
        page = 1
        for _ in range(_MAX_PAGES):
            q = {"page": page, "pageSize": _PAGE_SIZE}
            if params:
                q.update(params)
            try:
                r = requests.get(url, headers=self._headers(token), params=q, timeout=30)
                r.raise_for_status()
                body = r.json()
            except Exception as e:
                print(f"[firstback] fieldedge GET {resource} failed (biz {business_id}): {e}",
                      file=sys.stderr, flush=True)
                return
            rows = body if isinstance(body, list) else (body.get("data") or [])
            for row in rows:
                yield row
            if isinstance(body, list) or not body.get("hasMore"):
                return
            page += 1

    @staticmethod
    def _phones(row: dict) -> list:
        out = []
        for f in ("phone", "Phone", "mobilePhone", "phoneNumber", "homePhone"):
            v = row.get(f)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        return out

    def fetch_clients(self, business_id: int) -> list:
        """Return FieldEdge customers as [{name, phones, email}] (placeholder field mapping)."""
        out = []
        for c in self._get(business_id, "customers"):
            name = (c.get("name") or c.get("displayName")
                    or " ".join(p for p in [(c.get("firstName") or "").strip(),
                                            (c.get("lastName") or "").strip()] if p)).strip()
            phones = self._phones(c)
            if not phones:
                continue
            out.append({"name": name, "phones": phones,
                        "email": (c.get("email") or "").strip()})
        return out

    def fetch_jobs(self, business_id: int) -> list:
        """Return FieldEdge jobs as [{title, status, client_phone}] (placeholder field mapping)."""
        out = []
        for j in self._get(business_id, "jobs"):
            phones = self._phones(j)
            out.append({
                "title": (j.get("summary") or j.get("description") or j.get("type") or "").strip(),
                "status": (j.get("status") or "").strip(),
                "client_phone": phones[0] if phones else "",
            })
        return out

    def push_quote_request(self, business_id: int, lead: dict, booking: dict):
        """Best-effort lead create (placeholder endpoint). Returns id or None; never raises."""
        token = self._token(business_id)
        if not token:
            return None
        name = (lead.get("name") or "").strip() or "FirstBack lead"
        when = (booking.get("when") or booking.get("day") or "").strip()
        payload = {
            "name": name,
            "phone": (lead.get("phone") or "").strip(),
            "source": "FirstBack",
            "summary": f"FirstBack booked estimate{(' — ' + when) if when else ''}",
        }
        try:
            r = requests.post(f"{FIELDEDGE_API_BASE.rstrip('/')}/leads",
                              json=payload, headers=self._headers(token), timeout=30)
            r.raise_for_status()
            body = r.json() if r.content else {}
            new_id = body.get("id") or (body.get("data") or {}).get("id")
            return str(new_id) if new_id is not None else None
        except Exception as e:
            print(f"[firstback] fieldedge push lead failed (biz {business_id}): {e}",
                  file=sys.stderr, flush=True)
            return None


_provider = FieldEdgeFSM()


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
