"""FSM (Field Service Management) provider interface.

Thin abstract base so a second provider (e.g. Housecall Pro) can plug in later
without touching triage.py, db.py, or routes. Jobber is the v1 implementation
(jobber_fsm.py). fsm_sync.py consumes this interface.

This file is import-safe with no credentials — no env reads at import time.
"""


class FSMProvider:
    """Abstract interface for a field-service-management integration.

    All implementors must:
    * Be gated: ``configured()`` returns False when the operator hasn't set the
      relevant env vars; every other method is a safe no-op in that case.
    * Never raise into a caller — swallow + log every network/API error.
    * Scope every read/write by ``business_id``; never cross tenants.
    """

    PROVIDER_KEY = ""   # e.g. "jobber" | "housecall_pro"

    def configured(self) -> bool:
        """True if this app has API credentials set for this provider."""
        raise NotImplementedError

    def is_connected(self, business_id: int) -> bool:
        """True if this specific business has a valid OAuth connection."""
        raise NotImplementedError

    def auth_url(self, state: str) -> str:
        """The OAuth2 authorization URL to redirect the owner to."""
        raise NotImplementedError

    def connect_with_code(self, business_id: int, code: str) -> None:
        """Exchange an auth code for tokens and persist them."""
        raise NotImplementedError

    def disconnect(self, business_id: int) -> None:
        """Clear tokens for this business (keeps existing synced contacts)."""
        raise NotImplementedError

    def fetch_clients(self, business_id: int) -> list:
        """Return the business's Jobber clients as [{name, phones, email}].

        Returns [] on any error or when not connected (defensive).
        """
        raise NotImplementedError

    def fetch_jobs(self, business_id: int) -> list:
        """Return recent open/completed jobs as [{title, status, client_phone}].

        Returns [] on any error or when not connected (defensive).
        """
        raise NotImplementedError

    def push_quote_request(self, business_id: int, lead: dict, booking: dict):
        """Push a booked FirstBack estimate as a quote request in the FSM.

        Returns the provider's request ID string on success, None on failure.
        Failure must never break the booking — callers guard with a try/except.

        Requires Jobber Connect or higher (write_quote_requests scope).
        """
        raise NotImplementedError
