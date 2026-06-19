# PREDEPLOY-01-MONEY — Billing Audit
**Auditor:** Lane 01 (Money/Billing)
**Date:** 2026-06-19
**Branch:** staging @ 55d2601
**Scope:** billing.py end-to-end + every Stripe surface in app.py

---

## Verdict: CONDITIONAL SHIP

Two P1 findings. Zero P0 findings. Core payment flow is sound. Both P1s must be fixed before collecting real money because one can strand a paying customer with no conversations and the other silently downgrades a plan upgrade.

---

## Findings

### P1-01 — Failed webhook event permanently silenced on Stripe retry
**File:** `billing.py:197–202` + `db.py:907–914`
**What happens:**
`handle_webhook` catches any handler exception, writes `stripe_events` with `status="error"`, then re-raises so Flask returns 500. Stripe sees 500 and retries. On retry, `stripe_event_seen` queries `SELECT 1 FROM stripe_events WHERE event_id=?` — which matches the error-status row and returns `True` → the retry path hits `"already processed", 200` and the grant is never written.

**Real scenario:** During a deploy, the old Render instance is releasing the SQLite file-lock. A `SQLITE_BUSY` / `busy_timeout` error fires inside `add_usage_grant`. The error row is written. All subsequent Stripe retries (up to 3 days) are silently swallowed. The paying customer never gets their conversation allotment.

**Fix:** Change `stripe_event_seen` to filter `AND status='ok'` so error-marked events are visible to retries. Alternatively, do not write the event row on error at all (let Stripe retry).

```python
# db.py stripe_event_seen — current (wrong):
"SELECT 1 FROM stripe_events WHERE event_id=?"
# Fixed:
"SELECT 1 FROM stripe_events WHERE event_id=? AND status='ok'"
```

---

### P1-02 — Plan upgrade via Stripe portal downgrades grant to stale metadata plan
**File:** `billing.py:282–292` (in `_on_invoice_paid`) and `billing.py:313–323` (in `_plan_from_subscription`)
**What happens:**
Both plan-resolution paths prefer `subscription.metadata.plan` (set at checkout time) over the actual current Stripe price ID. When an owner upgrades from Starter → Crew via the Stripe Billing Portal, Stripe fires `invoice.paid` with the Crew price in the line items. `_on_invoice_paid` resolves the price correctly to `crew`, but then the override check:

```python
sub_meta_plan = (invoice_obj.get("subscription_details") or {}).get("metadata", {}).get("plan")
if sub_meta_plan and sub_meta_plan in PLAN_GRANTS:
    plan = sub_meta_plan  # overwrites 'crew' with stale 'starter'
```

…overwrites the resolved plan with `starter` because the portal upgrade does not go through our checkout and therefore does not update `subscription.metadata.plan`. The customer pays for Crew ($399/mo, 3 000 conversations) but receives a Starter grant (250 conversations).

**`_plan_from_subscription` has the same priority inversion:** it returns `meta_plan` first if it's set, ignoring the actual price ID.

**Fix:** Invert the priority in both functions: trust the price_id lookup first; fall back to metadata only when price_id lookup returns the default "starter" due to unrecognized ID. The Phase 6a D-2 loudness already fires a warning on unrecognized price IDs, so unrecognized = missing env var = operator error, not a silent downgrade.

```python
# Correct ordering in _on_invoice_paid:
plan = "starter"
for line in lines:
    price = (line.get("price") or {}).get("id", "")
    if price:
        plan = _price_to_plan(price)
        break
# Only fall back to metadata if price lookup returned starter due to empty/missing price:
if plan == "starter":
    sub_meta_plan = (invoice_obj.get("subscription_details") or {}).get("metadata", {}).get("plan")
    if sub_meta_plan and sub_meta_plan in PLAN_GRANTS:
        plan = sub_meta_plan
```

---

## Checked Clean (no finding)

### Checkout session creation
- `billing.py:93–128`: correct `price_id` looked up from `PRICE_IDS[(plan, interval)]`, validated before use (raises `ValueError` on unknown combo). Metadata includes `business_id`, `plan`, `interval`. Tenant-bound via `biz["id"]` from session, not user-supplied. Auth-gated by `@login_required` at `app.py:2907`. ✓

### Webhook signature verification
- `billing.py:175`: `stripe.Webhook.construct_event` raises `SignatureVerificationError` on bad sig. `app.py:2900–2901`: catches specifically and returns 400 (not 500). Stripe interprets 400 as "do not retry." ✓

### TOCTOU race (stripe_event_seen / mark_stripe_event)
- Deployment config: 1 gunicorn worker, 8 threads (`render.yaml:22`). Concurrent webhook delivery of the same event_id is theoretically possible. The race window exists: two threads can both pass `stripe_event_seen` before either writes. However: (a) Stripe's documented behavior is to wait for HTTP response before delivering a retry, making true concurrency rare; (b) the PRIMARY KEY on `stripe_events.event_id` means the second `INSERT OR REPLACE` merely overwrites — no double row; (c) `add_usage_grant` would write two rows. Net risk: LOW (single-tenant, Stripe serializes delivery). Not a P0. Recommend a future fix: atomic `INSERT OR IGNORE` as the idempotency guard, with subsequent SELECT to decide whether to proceed.

### No-replay double-grant (idempotency on same event_id)
- `test_billing.py:234–244` (test 7): proven — a second `handle_webhook` call with the same `evt_invoice_001` hits `stripe_event_seen` → True → "already processed", no second grant written. ✓

### _price_to_plan: Phase 6a D-2 fix
- `billing.py:55–80`: unrecognized price_id goes loud (stderr + operator email, async thread), still grants starter. Verified by `test_billing.py:385–414`. ✓

### Plan grant amounts
- `PLAN_GRANTS` at `billing.py:42–46`: Starter=250, Pro=1000, Crew=3000. Tested in `test_billing.py:339–372`. ✓

### Subscription gate (launch_blockers)
- `compliance.py:60–82`: canceled and past_due block launch. Proven for both values in `test_billing.py:246–277`. ✓
- `compliance.subscription_active`: NULL/missing treated as active (seed/legacy tenant — intentional, `db.py:666`). ✓

### Cancellation / past_due / downgrade lifecycle
- `billing.py:239–268`: `customer.subscription.updated` with `status=canceled` maps to `internal_status="canceled"`. `invoice.payment_failed` maps to `past_due`. `db.update_billing` writes the status. `compliance.subscription_active` checks it. ✓

### Usage gauge (conversations_remaining)
- `db.py:3463–3485`: reads most recent grant row (`ORDER BY id DESC LIMIT 1`), counts consumption in the current *calendar month* (`month_start`), never goes negative (`max(0, ...)`). Correctly handles annual subscriptions (same monthly allotment, refills by moving month_start forward with no new invoice needed). `conversations_consumed` counts `DISTINCT lead_id` on `path='sms'`, preventing per-turn inflation. Tenant-scoped (`WHERE business_id=?`). All proven in `test_usage_gauge.py`. ✓

### Seed tenant not locked out
- `db.py:665–667`: migration backfills `subscription_status='active'` for any NULL row. `compliance.subscription_active` treats NULL as active (belt + suspenders). ✓

### No fabricated money values
- `roi.py:13,58,65`: ROI copy explicitly uses "estimated" language; never implies collected cash. ✓

### Billing routes auth
- `/billing/checkout` (app.py:2906–2907): `@login_required`. ✓
- `/billing/portal` (app.py:2925–2926): `@login_required`. ✓
- `/webhooks/stripe` (app.py:2886): auth-free, protected by HMAC only — correct for Stripe webhooks. ✓

### Billing success/cancel redirect
- `billing.py:113`: `success_url` defaults to `/billing/success?session_id={CHECKOUT_SESSION_ID}`. **No `/billing/success` route exists in `app.py`** — a Stripe redirect after checkout would hit a 404. This is a UX issue (the customer completed checkout but lands on an error page) but not a money error: the webhook has already processed and the grant is already written. Classify P2.

---

## P2 (Note, not blocking)

### P2-01 — Missing /billing/success and /billing/cancel routes
**File:** `billing.py:113`, `app.py` (absent)
The default `success_url` and `cancel_url` point to `/billing/success` and `/billing/cancel` which do not exist as Flask routes. Post-checkout redirect lands on a 404. The customer completed payment and the webhook already granted service; this is purely a UX gap. Add redirect routes before launch for a polished experience.

### P2-02 — New signups via /signup get implicit free subscription
**File:** `app.py:302–349`, `db.py:990–1009`, `compliance.py:49–57`
`/signup` is a public route. `create_business` does not set `subscription_status`, so new businesses default to NULL → `subscription_active` treats NULL as active → no subscription required. Currently FirstBack is single-tenant (founder is the only user) so this is inert. For multi-tenant charging, new businesses must be created with `subscription_status='pending'` or `'trial'` and gated explicitly.

### P2-03 — interval parameter not validated before passing to checkout
**File:** `app.py:2915–2917`
An arbitrary `interval` string from the form is accepted (e.g., "quarterly"). `_norm_interval` silently maps anything non-year to "month". No error is returned. Safe (won't create a wrong Stripe price), but could silently confuse an operator. Clamp to `("month", "year")` with a 400 on unknown values.

---

## What the operator must still supply (prod secrets)

These are missing from `render.yaml` (correct — secrets must not be in git) and must be set in the Render dashboard before go-live:

| Env var | Required for |
|---|---|
| `STRIPE_SECRET_KEY` | All billing routes + webhook |
| `STRIPE_WEBHOOK_SECRET` | Webhook signature verification |
| `STRIPE_PRICE_STARTER` | Checkout → Starter plan |
| `STRIPE_PRICE_PRO` | Checkout → Pro plan |
| `STRIPE_PRICE_CREW` | Checkout → Crew plan |
| `STRIPE_PRICE_STARTER_ANNUAL` | Annual checkout |
| `STRIPE_PRICE_PRO_ANNUAL` | Annual checkout |
| `STRIPE_PRICE_CREW_ANNUAL` | Annual checkout |
| `SEED_OWNER_EMAIL` (config) | Operator billing-warning emails |

---

## Summary table

| ID | Severity | File:Line | Description |
|---|---|---|---|
| P1-01 | P1 | `billing.py:197–202` + `db.py:907–914` | Failed webhook silenced on retry; paying customer loses grant |
| P1-02 | P1 | `billing.py:282–292`, `billing.py:313–323` | Stale metadata overrides price_id; plan upgrade via portal downgrades grant |
| P2-01 | P2 | `billing.py:113` / `app.py` (absent) | Missing /billing/success and /billing/cancel routes → 404 after checkout |
| P2-02 | P2 | `app.py:302–349` + `db.py:990–1009` | New signups default to free active subscription |
| P2-03 | P2 | `app.py:2915` | interval param not clamped to valid values |
