"""Stripe billing for FirstBack — Checkout, customer portal, and webhook handler.

Tiers (must match the Price IDs you create in Stripe):
  Starter  $99/mo  → 250 conversations/period
  Pro     $199/mo  → 1 000 conversations/period
  Crew    $399/mo  → 3 000 conversations/period

Config (environment variables):
  STRIPE_SECRET_KEY     sk_test_... or sk_live_...
  STRIPE_WEBHOOK_SECRET whsec_...
  STRIPE_PRICE_STARTER  price_...
  STRIPE_PRICE_PRO      price_...
  STRIPE_PRICE_CREW     price_...
  FIRSTBACK_PUBLIC_URL  https://yourapp.com  (for success/cancel redirects)
"""
import os
import sys
import threading
import stripe

import db
import mail
from config import VOICE_PUBLIC_URL, PUBLIC_BASE_URL, SEED_OWNER_EMAIL

# ── Config ────────────────────────────────────────────────────────────────────
STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# Stripe Price IDs keyed by (plan, interval). Annual is billed once a year at 20% off
# the monthly rate (i.e. monthly × 12 × 0.8). The monthly conversation allotment is the
# SAME on annual — the fuel gauge still refills every calendar month (see db.conversations_remaining).
PRICE_IDS = {
    ("starter", "month"): os.environ.get("STRIPE_PRICE_STARTER", ""),
    ("pro",     "month"): os.environ.get("STRIPE_PRICE_PRO", ""),
    ("crew",    "month"): os.environ.get("STRIPE_PRICE_CREW", ""),
    ("starter", "year"):  os.environ.get("STRIPE_PRICE_STARTER_ANNUAL", ""),
    ("pro",     "year"):  os.environ.get("STRIPE_PRICE_PRO_ANNUAL", ""),
    ("crew",    "year"):  os.environ.get("STRIPE_PRICE_CREW_ANNUAL", ""),
}

# Conversations granted per MONTH by plan (annual subscribers get this same monthly refill).
PLAN_GRANTS: dict[str, int] = {
    "starter": 250,
    "pro":     1000,
    "crew":    3000,
}


def configured() -> bool:
    """True when Stripe billing is wired: secret key + the three monthly Price IDs.
    Used to gate the subscribe UI so checkout buttons only appear once billing is live."""
    return bool(STRIPE_SECRET_KEY
                and PRICE_IDS.get(("starter", "month"))
                and PRICE_IDS.get(("pro", "month"))
                and PRICE_IDS.get(("crew", "month")))


def _web_base() -> str:
    """Base URL for Checkout success/cancel + portal return — the FLASK web app, NOT the
    voice service. (Was VOICE_PUBLIC_URL, which is the separate voice host — wrong target.)"""
    return (PUBLIC_BASE_URL or VOICE_PUBLIC_URL or "").rstrip("/")


def _norm_interval(interval: str) -> str:
    """Normalize any annual-ish word to 'year', everything else to 'month'."""
    return "year" if str(interval or "").lower() in ("year", "annual", "annually", "yr", "yearly") else "month"


# Map Stripe Price ID → internal plan key (works for both monthly and annual IDs).
def _price_to_plan(price_id: str) -> str:
    for (plan, _interval), pid in PRICE_IDS.items():
        if pid and pid == price_id:
            return plan
    # Phase 6a D-2: price_id matched no configured PRICE_IDS entry. If it is a real
    # (non-empty) Stripe price ID, this is a missing STRIPE_PRICE_* env var on the
    # server -- returning "starter" here would SILENTLY downgrade a paying Pro/Crew
    # renewal. Still grant starter (safer than stranding the account), but make the
    # misconfiguration LOUD so the operator fixes the env before the next renewal.
    if price_id:
        warning = (
            f"[firstback] BILLING WARNING: unrecognized Stripe price_id {price_id!r} "
            f"not in PRICE_IDS -- granting 'starter'. A Pro/Crew subscriber may be "
            f"downgraded. Check the STRIPE_PRICE_* env vars on Render."
        )
        print(warning, file=sys.stderr, flush=True)
        # Best-effort operator email, async so a slow SMTP never blocks the <=30s
        # Stripe webhook response (mail.send_email swallows its own errors).
        threading.Thread(
            target=mail.send_email,
            args=(SEED_OWNER_EMAIL,
                  "BILLING WARNING: unrecognized Stripe price_id -- FirstBack",
                  warning),
            daemon=True,
        ).start()
    return "starter"  # safe fallback -- MUST have a valid price id in practice


# ── Stripe client ─────────────────────────────────────────────────────────────
def _stripe():
    """Return the stripe module with the secret key set; raises if no key."""
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured.")
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


# ── Checkout ──────────────────────────────────────────────────────────────────
def create_checkout_session(business_id: int, plan: str, interval: str = "month",
                            success_url: str = None, cancel_url: str = None):
    """Create a Stripe Checkout session for a new subscription.

    `interval` is 'month' (default) or 'year' (annual, 20% off). Returns the session
    dict (has .url for the redirect). The customer id is learned from the first webhook.
    """
    s = _stripe()
    interval = _norm_interval(interval)
    price_id = PRICE_IDS.get((plan, interval))
    if not price_id:
        raise ValueError(f"Unknown or unconfigured plan/interval: {plan!r}/{interval!r}")

    base = _web_base()
    biz = db.get_business(business_id) or {}

    # Reuse the Stripe customer if we already created one for this tenant.
    kwargs: dict = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url or f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url":  cancel_url  or f"{base}/billing/cancel",
        "metadata":    {"business_id": str(business_id), "plan": plan, "interval": interval},
        "subscription_data": {"metadata": {"business_id": str(business_id), "plan": plan, "interval": interval}},
    }
    customer_id = biz.get("stripe_customer_id")
    if customer_id:
        kwargs["customer"] = customer_id
    else:
        # Pre-fill email so the Checkout form is shorter.
        u = _owner_email(business_id)
        if u:
            kwargs["customer_email"] = u

    session = s.checkout.Session.create(**kwargs)
    return session


def _owner_email(business_id: int) -> str:
    """Best-effort: first user email for this business."""
    try:
        conn = db.get_conn()
        row = conn.execute(
            "SELECT email FROM users WHERE business_id=? ORDER BY id LIMIT 1",
            (business_id,),
        ).fetchone()
        conn.close()
        return row["email"] if row else ""
    except Exception:
        return ""


# ── Customer Portal ───────────────────────────────────────────────────────────
def create_portal_session(business_id: int, return_url: str = None):
    """Create a Stripe Billing Portal session so the tenant can manage their sub."""
    s = _stripe()
    biz = db.get_business(business_id) or {}
    customer_id = biz.get("stripe_customer_id")
    if not customer_id:
        raise ValueError(f"Business {business_id} has no Stripe customer yet.")

    base = _web_base()
    session = s.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url or f"{base}/settings",
    )
    return session


# ── Webhook handler ───────────────────────────────────────────────────────────
def handle_webhook(payload: bytes, sig_header: str):
    """Verify the Stripe webhook signature, dedupe on event id, dispatch.

    Returns (status_str, http_code) suitable for a Flask response.
    Raises stripe.error.SignatureVerificationError on a bad signature.
    """
    s = _stripe()
    if not STRIPE_WEBHOOK_SECRET:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not configured.")

    # This raises SignatureVerificationError on a bad sig — let the route
    # catch it and return 400 so Stripe knows to retry.
    event = s.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)

    event_id   = event["id"]
    event_type = event["type"]

    # ── Idempotency: skip already-processed events ────────────────────────────
    if db.stripe_event_seen(event_id):
        return "already processed", 200

    # ── Dispatch ──────────────────────────────────────────────────────────────
    obj = event["data"]["object"]
    try:
        if event_type == "checkout.session.completed":
            _on_checkout_completed(obj)
        elif event_type in ("customer.subscription.updated",
                            "customer.subscription.deleted"):
            _on_subscription_changed(obj)
        elif event_type == "invoice.paid":
            _on_invoice_paid(obj)
        elif event_type == "invoice.payment_failed":
            _on_invoice_failed(obj)
        # Other events are silently acknowledged (no-op).
    except Exception as exc:
        # Record the event so we don't retry forever on a bad fixture, but
        # surface the error so the caller can log it.
        db.mark_stripe_event(event_id, event_type, status="error",
                             detail=str(exc)[:500])
        raise

    db.mark_stripe_event(event_id, event_type, status="ok")
    return "ok", 200


# ── Stripe event handlers ─────────────────────────────────────────────────────
def _business_id_from_obj(obj) -> int | None:
    """Extract business_id from Stripe object metadata or customer lookup."""
    bid = (obj.get("metadata") or {}).get("business_id")
    if bid:
        try:
            return int(bid)
        except (TypeError, ValueError):
            pass
    # Fall back to customer reverse-lookup.
    customer_id = obj.get("customer")
    if customer_id:
        return db.get_business_id_by_stripe_customer(customer_id)
    return None


def _on_checkout_completed(session_obj):
    """checkout.session.completed → store customer id + mark subscription active."""
    business_id = _business_id_from_obj(session_obj)
    if not business_id:
        return
    customer_id = session_obj.get("customer")
    sub_id      = session_obj.get("subscription")
    plan        = (session_obj.get("metadata") or {}).get("plan", "starter")
    db.update_billing(business_id,
                      stripe_customer_id=customer_id,
                      stripe_sub_id=sub_id,
                      subscription_status="active",
                      plan=plan)


def _on_subscription_changed(sub_obj):
    """customer.subscription.updated/deleted → sync status + period."""
    business_id = _business_id_from_obj(sub_obj)
    if not business_id:
        return

    status = sub_obj.get("status", "")  # active | past_due | canceled | ...
    # Map Stripe statuses to our 3 buckets.
    internal_status = {
        "active":    "active",
        "trialing":  "active",
        "past_due":  "past_due",
        "canceled":  "canceled",
        "unpaid":    "past_due",
        "incomplete_expired": "canceled",
    }.get(status, "past_due")

    # Pull plan from subscription items if available.
    plan = _plan_from_subscription(sub_obj)

    period_end = None
    items = sub_obj.get("items", {}).get("data", [])
    if items:
        period_end = sub_obj.get("current_period_end")

    db.update_billing(business_id,
                      stripe_sub_id=sub_obj.get("id"),
                      subscription_status=internal_status,
                      plan=plan,
                      current_period_end=period_end)


def _on_invoice_paid(invoice_obj):
    """invoice.paid → write a usage_grants row for the next billing period."""
    business_id = _business_id_from_obj(invoice_obj)
    if not business_id:
        return

    # Find the plan from the subscription line items.
    sub_id   = invoice_obj.get("subscription")
    period_s = invoice_obj.get("period_start") or invoice_obj.get("lines", {}).get("data", [{}])[0].get("period", {}).get("start")
    period_e = invoice_obj.get("period_end")   or invoice_obj.get("lines", {}).get("data", [{}])[0].get("period", {}).get("end")

    # Pre-deploy M2: the invoice's billed price is authoritative; a confirmed price match
    # WINS over (possibly stale) checkout metadata. Metadata only covers an unconfigured price.
    plan = None
    invoice_price = ""
    lines = (invoice_obj.get("lines") or {}).get("data", [])
    for line in lines:
        price = (line.get("price") or {}).get("id", "")
        if price:
            invoice_price = invoice_price or price
            plan = _confirmed_plan_for_price(price)
            if plan:
                break
    if plan is None:
        sub_meta_plan = (invoice_obj.get("subscription_details") or {}).get("metadata", {}).get("plan")
        if sub_meta_plan and sub_meta_plan in PLAN_GRANTS:
            plan = sub_meta_plan
        elif invoice_price:
            plan = _price_to_plan(invoice_price)   # fires the 6a unrecognized-price warning
        else:
            plan = "starter"

    granted = PLAN_GRANTS.get(plan, PLAN_GRANTS["starter"])
    db.add_usage_grant(business_id,
                       period_start=period_s,
                       period_end=period_e,
                       conversations_granted=granted,
                       source=f"stripe:{sub_id or 'invoice'}")

    # Also ensure subscription_status is active.
    db.update_billing(business_id, subscription_status="active", plan=plan)


def _on_invoice_failed(invoice_obj):
    """invoice.payment_failed → mark subscription past_due."""
    business_id = _business_id_from_obj(invoice_obj)
    if not business_id:
        return
    db.update_billing(business_id, subscription_status="past_due")


def _confirmed_plan_for_price(price_id: str):
    """The plan for a price_id ONLY if it matches a configured PRICE_ID, else None.
    Unlike _price_to_plan (which silently defaults to 'starter' on a miss), this returns
    None on a miss so callers can PREFER a confirmed price match over possibly-stale
    checkout metadata (pre-deploy M2)."""
    for (plan, _interval), pid in PRICE_IDS.items():
        if pid and pid == price_id:
            return plan
    return None


def _plan_from_subscription(sub_obj) -> str:
    """Resolve the internal plan from a subscription. Pre-deploy M2: the ACTUAL billed
    price is authoritative -- a Stripe Billing-Portal upgrade changes the subscription's
    price item but NOT our checkout metadata, so a confirmed price match must WIN over
    (possibly stale) metadata. Metadata is only the fallback for an unconfigured price."""
    items = sub_obj.get("items", {}).get("data", [])
    first_price = ""
    for item in items:
        price_id = (item.get("price") or {}).get("id", "")
        if price_id:
            first_price = first_price or price_id
            p = _confirmed_plan_for_price(price_id)
            if p:
                return p
    meta_plan = (sub_obj.get("metadata") or {}).get("plan")
    if meta_plan and meta_plan in PLAN_GRANTS:
        return meta_plan
    if first_price:
        return _price_to_plan(first_price)   # fires the 6a unrecognized-price warning + email
    return "starter"
