"""FSM sync orchestration layer.

Consumes the FSMProvider interface and holds all the business logic:
client-sync -> contact suggestions, job enrichment, booking push, and the
periodic-sync cadence.

Provider routing (Option C — owner-approved): _get_active_provider returns the
single active provider for a business. HCP > Jobber tiebreak when both connected
(logs a warning; never double-fires). `configured()` / `push_configured()` return
True when EITHER provider has credentials.

Gated: every entry point is a safe no-op when no provider has credentials.
Defensive: all errors swallowed + logged; never raises into a request or tick.

F1 (critical): sync_clients calls db.upsert_suggestion DIRECTLY — it does NOT
route through contact_import.ingest whose presort() drops all new customers
(those without a prior booking and without an org field).
"""
import sys
import threading
from datetime import datetime, timezone

import db
import jobber_fsm
import hcp_fsm
import servicetitan_fsm
import workiz_fsm
import fieldedge_fsm
from config import FSM_SYNC_INTERVAL_HOURS


# ------------------------------------------------------------------
# Gate helpers
# ------------------------------------------------------------------
# All FSM providers, in tiebreak priority order (a contractor uses one CRM; this is just a
# deterministic pick when more than one is connected — safe for single-tenant, no double-fire).
_ALL_PROVIDERS = (servicetitan_fsm, workiz_fsm, fieldedge_fsm, hcp_fsm, jobber_fsm)


def configured() -> bool:
    """True if ANY FSM provider is enabled/has credentials."""
    return any(p.configured() for p in _ALL_PROVIDERS)


def push_configured() -> bool:
    """True if the push (quote-request) path is configured. Same gate as configured() in v1
    (Jobber/ServiceTitan/Workiz/FieldEdge attempt a real push; HCP push is a v1 no-op)."""
    return configured()


# ------------------------------------------------------------------
# Provider selection (Option C)
# ------------------------------------------------------------------
def _get_active_provider(business_id: int):
    """Return the single active FSMProvider for this business, or None.

    Priority order is _ALL_PROVIDERS (ServiceTitan > Workiz > FieldEdge > HCP > Jobber): the
    first enabled+connected provider wins. A contractor uses one CRM, so this is just a
    deterministic tiebreak, safe for single-tenant — no double-fire. A per-business fsm_provider
    column is the v2 path for multi-tenant multi-provider.
    """
    connected = [p for p in _ALL_PROVIDERS if p.configured() and p.is_connected(business_id)]
    if len(connected) > 1:
        winner = connected[0]._provider.PROVIDER_KEY
        others = ", ".join(p._provider.PROVIDER_KEY for p in connected[1:])
        print(
            f"[firstback] fsm: multiple CRMs connected for biz {business_id} "
            f"({winner} + {others}); using {winner} (tiebreak). Others paused until disconnected.",
            file=sys.stderr, flush=True,
        )
    return connected[0]._provider if connected else None


# ------------------------------------------------------------------
# Sync clients (F1: direct upsert_suggestion, NOT contact_import.ingest)
# ------------------------------------------------------------------
def sync_clients(business_id: int) -> dict:
    """Pull FSM clients and queue them as pending suggestions in the review
    inbox (category='customer', source='import-{provider}').

    Calls db.upsert_suggestion DIRECTLY — bypasses contact_import.ingest whose
    presort() returns None for anyone without a prior booking and without an org
    field, which drops 100% of first-sync customers.

    Routes to the active provider for this business (HCP or Jobber). Returns
    {'clients_fetched': N, 'suggested': N, 'skipped': N}.
    """
    if not configured():
        return {"clients_fetched": 0, "suggested": 0, "skipped": 0}

    provider = _get_active_provider(business_id)
    if provider is None:
        return {"clients_fetched": 0, "suggested": 0, "skipped": 0}

    provider_key = provider.PROVIDER_KEY  # e.g. "jobber" or "housecall_pro"
    source = f"import-{provider_key}"     # e.g. "import-jobber" or "import-housecall_pro"

    # Human-readable provider name for the reason string.
    _provider_labels = {
        "jobber": "Jobber",
        "housecall_pro": "Housecall Pro",
        "servicetitan": "ServiceTitan",
        "workiz": "Workiz",
        "fieldedge": "FieldEdge",
    }
    provider_label = _provider_labels.get(provider_key, provider_key)

    try:
        clients = provider.fetch_clients(business_id)
    except Exception as e:
        print(f"[firstback] fsm sync_clients fetch error (biz {business_id}): {e}",
              file=sys.stderr, flush=True)
        return {"clients_fetched": 0, "suggested": 0, "skipped": 0}

    # Numbers already in the contacts table (the owner has already decided).
    classified = {c["number"] for c in db.list_contacts(business_id)}
    decided = set()
    for st in ("accepted", "dismissed"):
        decided |= {s["number"] for s in db.list_suggestions(business_id, st)}
    skip = classified | decided

    import re as _re

    def _digits10(s):
        return _re.sub(r"\D", "", str(s or ""))[-10:]

    suggested = skipped = 0
    seen = set()
    for client in clients:
        name = (client.get("name") or "").strip() or None
        for phone in (client.get("phones") or []):
            key = _digits10(phone)
            if len(key) < 10 or key in seen:
                continue
            seen.add(key)
            if key in skip:
                skipped += 1
                continue
            # F1: direct call — category is always "customer" for FSM clients.
            db.upsert_suggestion(
                business_id, key, name,
                category="customer",
                reason=f"Existing {provider_label} client",
                source=source,
            )
            suggested += 1

    return {"clients_fetched": len(clients), "suggested": suggested, "skipped": skipped}


# ------------------------------------------------------------------
# Sync jobs (enrich existing suggestions / contacts with job context)
# ------------------------------------------------------------------
def sync_jobs(business_id: int) -> dict:
    """Pull recent FSM jobs and enrich matching contact suggestions with a note.

    Returns {'jobs_fetched': N, 'enriched': N}.
    """
    if not configured():
        return {"jobs_fetched": 0, "enriched": 0}

    provider = _get_active_provider(business_id)
    if provider is None:
        return {"jobs_fetched": 0, "enriched": 0}

    try:
        jobs = provider.fetch_jobs(business_id)
    except Exception as e:
        print(f"[firstback] fsm sync_jobs fetch error (biz {business_id}): {e}",
              file=sys.stderr, flush=True)
        return {"jobs_fetched": 0, "enriched": 0}

    # For now, job enrichment is best-effort: we log but don't update suggestions
    # (the v1 plan notes job-title/status enrich; the contact already has a reason
    # from sync_clients; a fuller enrichment is v2). Return the count so tests can
    # verify the fetch was attempted.
    return {"jobs_fetched": len(jobs), "enriched": 0}


# ------------------------------------------------------------------
# Booking push (additive write, daemon thread, never blocks the reply)
# ------------------------------------------------------------------
def push_booking_async(business_id: int, appointment_id: int,
                       lead: dict, booking: dict) -> None:
    """Push a booked estimate to the active FSM provider in a daemon thread.

    For Jobber: stores the request id in appointments.fsm_external_id on success.
    For HCP: push_quote_request is a v1 no-op (returns None); fsm_external_id is
    NOT set (no confirmed endpoint — never claim "pushed").

    Failure never breaks the booking — the thread is fully isolated.
    """
    if not configured():
        return

    provider = _get_active_provider(business_id)
    if provider is None:
        return

    def _push():
        try:
            ext_id = provider.push_quote_request(business_id, lead, booking)
            if ext_id and appointment_id:
                now = datetime.now(timezone.utc).isoformat()
                db.set_fsm_external_id(appointment_id, business_id, ext_id, now)
        except Exception as e:
            print(
                f"[firstback] fsm push_booking_async error "
                f"(biz {business_id}, appt {appointment_id}): {e}",
                file=sys.stderr, flush=True,
            )

    t = threading.Thread(target=_push, daemon=True)
    t.start()


# ------------------------------------------------------------------
# Periodic sync
# ------------------------------------------------------------------
def maybe_sync_all(now=None) -> dict:
    """Sync every connected business that is past its sync interval.

    Called by reminders.tick_once in an isolated try/except. Returns a summary
    dict for logging.

    Cadence: FSM_SYNC_INTERVAL_HOURS (default 24). Uses businesses.fsm_last_synced_at
    for the interval check AND for the UI display (F4: reuse the column, no separate meta key).
    """
    if not configured():
        return {"businesses_checked": 0, "businesses_synced": 0}

    try:
        businesses = db.list_businesses()
    except Exception as e:
        print(f"[firstback] fsm maybe_sync_all list_businesses error: {e}",
              file=sys.stderr, flush=True)
        return {"businesses_checked": 0, "businesses_synced": 0}

    now_dt = datetime.now(timezone.utc)
    interval_hours = FSM_SYNC_INTERVAL_HOURS
    checked = synced = 0

    for biz in businesses:
        bid = biz.get("id")
        if not bid:
            continue
        checked += 1

        # Skip businesses with no active provider
        if _get_active_provider(bid) is None:
            continue

        # Check interval
        last_raw = biz.get("fsm_last_synced_at")
        if last_raw:
            try:
                last_dt = datetime.fromisoformat(last_raw)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                elapsed_h = (now_dt - last_dt).total_seconds() / 3600
                if elapsed_h < interval_hours:
                    continue
            except (ValueError, TypeError):
                pass  # bad timestamp -> sync anyway

        # Run sync
        try:
            result = sync_clients(bid)
            # Store the total clients fetched for the settings card.
            clients_fetched = result.get("clients_fetched", 0)
            db.set_fsm_sync_stamp(bid, now_dt.isoformat(), clients_fetched)
            synced += 1
        except Exception as e:
            print(f"[firstback] fsm maybe_sync_all sync error (biz {bid}): {e}",
                  file=sys.stderr, flush=True)

    return {"businesses_checked": checked, "businesses_synced": synced}
