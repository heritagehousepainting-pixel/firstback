# Phase 5h — SF-10 Crew Multi-Tenant Schema (DESIGN-ONLY)
**Status:** LOCKED design spec — no code build; migration plan only  
**Date:** 2026-06-18  
**Source branch:** staging ~bfd6ceb  
**Scope:** Design the org/team schema so Starter ships as 1-seat without a later rewrite. Close Phase-4 P2 (dispatcher TwiML cross-tenant ownership). No tables built in this phase; migration SQL drafted and reviewed, implemented in a later "Crew build" phase.

---

## 1. Current Tenancy Model (Ground Truth)

### Schema (db.py:215-224)
```sql
CREATE TABLE businesses (
    id INTEGER PRIMARY KEY,
    name TEXT, trade TEXT, service_area TEXT, hours TEXT,
    owner_name TEXT, ai_instructions TEXT, available_slots TEXT, phone TEXT
    -- + many ALTER TABLE additions: stripe_customer_id, plan, subscription_status,
    --   followups_enabled, growth_on, screen_mode, etc.
    -- All billing is 1-to-1 with business.
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    business_id INTEGER NOT NULL,   -- <-- hard 1:1 user->business binding
    created_at TEXT
);
```

### Auth model (auth.py:19-35)
`current_user()` reads `session['uid']` -> `db.get_user(uid)`.  
`current_business()` reads `current_user()['business_id']` -> `db.get_business(business_id)`.  
One user = one business. Hardcoded in auth.py:26.

### Billing model (billing.py:69-113)
All Stripe objects carry `metadata.business_id`. One Stripe customer per business (`businesses.stripe_customer_id`). All usage grants, subscriptions, and plans are scoped to `business_id`. There is no concept of an "org" or "account" that owns multiple businesses.

### Tenancy assumption everywhere
Every table with data uses `business_id` as the tenant key: leads, messages, appointments, calls, contacts, scheduled_messages, usage_grants, etc. This is correct and does NOT need to change for Crew — the business is already the natural unit of isolation.

### Phase-4 P2 open gap (app.py:1436-1473)
`/twiml/dispatcher/<lead_id>` and `/twiml/dispatcher/connect/<lead_id>` fetch the lead by integer ID with `db.get_lead(lead_id)` — no business_id ownership check. Protected only by Twilio signature verification (`@require_twilio_signature`). With a single shared Twilio number across all tenants this is low-risk, but if Crew tenants ever have separate numbers and the dispatcher URL is guessable, a cross-tenant lead PII exposure is possible.

---

## 2. The Problem Crew Solves

A multi-location contractor (Heritage House Painting with two crews, a plumbing company with a residential and commercial team) wants:
- One login, two or more "business" profiles (each with its own number, calendar, leads, AI name)
- One bill
- Optionally: a crew member account that can view one location but not the billing/settings

Currently, achieving this requires two separate user accounts and two separate Stripe subscriptions. The schema must be redesigned so:
1. Starter = 1 org + 1 user + 1 business (the default; no visible Crew UI)
2. Pro Crew = 1 org + N users + N businesses, one subscription
3. The auth and billing layers target the ORG, not the business

---

## 3. Proposed Schema

### New tables (migration-safe additions — additive only)

```sql
-- The top-level account. Every existing business becomes org_id=1 (or its own org).
-- Billing attaches here, not to businesses.
CREATE TABLE orgs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    plan        TEXT,               -- 'starter' | 'pro' | 'crew'
    stripe_customer_id  TEXT UNIQUE,
    stripe_sub_id       TEXT,
    subscription_status TEXT DEFAULT 'active',
    current_period_end  INTEGER,
    created_at  TEXT
);

-- org_members: who belongs to an org and at what role.
-- Replaces the implicit user->business 1:1.
CREATE TABLE org_members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    role        TEXT NOT NULL DEFAULT 'owner',   -- 'owner' | 'member'
    created_at  TEXT,
    UNIQUE(org_id, user_id)
);
```

### Modified tables (column additions — safe ALTER TABLE)

```sql
-- businesses gain an org_id FK (NULL = legacy row; treated as org_id=1)
ALTER TABLE businesses ADD COLUMN org_id INTEGER REFERENCES orgs(id);

-- users lose the hard 1:1 business_id (it becomes NULL/deprecated; do NOT DROP it
-- in this migration — existing code reads it; deprecate only after auth is rewired)
-- No ALTER needed yet: the column stays, auth.py is refactored to use org_members.
```

### Usage grants move to org scope

```sql
-- usage_grants gains org_id. The existing business_id stays for backward compat;
-- new rows written with both. A later cleanup removes the business_id column.
ALTER TABLE usage_grants ADD COLUMN org_id INTEGER REFERENCES orgs(id);
```

---

## 4. 1-Seat Default Migration (must not disturb Heritage tenant)

Heritage House Painting is live on business_id=1, user_id=1. The migration must:

1. `INSERT INTO orgs (id, name, plan, stripe_customer_id, stripe_sub_id, subscription_status, current_period_end, created_at) SELECT 1, name, plan, stripe_customer_id, stripe_sub_id, subscription_status, current_period_end, created_at FROM businesses WHERE id=1` — copy Heritage's billing state to org_id=1.

2. `UPDATE businesses SET org_id=1 WHERE org_id IS NULL` — all existing businesses join org 1. (In the single-tenant Render deploy, this is only business_id=1.)

3. `INSERT INTO org_members (org_id, user_id, role) SELECT 1, id, 'owner' FROM users WHERE business_id=1` — wire existing user(s) into the org.

4. `UPDATE usage_grants SET org_id=1 WHERE org_id IS NULL` — backfill grants.

**Safety:** Steps 1-4 are idempotent (upsert or WHERE NOT EXISTS guards). The Heritage tenant never loses access: `businesses.id=1` still works, `users.business_id=1` still works (old auth path stays alive), billing still reads `subscription_status` from `businesses` until billing.py is rewired. No column is dropped, no row is deleted.

---

## 5. Seams Billing and Auth Would Touch (deferred build)

### Auth seam (auth.py:19-35)
Current: `current_business() = db.get_business(user['business_id'])` — 1:1.  
New: `current_org() = db.get_org_for_user(uid)` — reads org_members -> orgs.  
`current_business()` becomes context-aware: in Starter, returns the single business for the org. In Crew, returns the business selected by the session (e.g. `session['active_business_id']`).  
The `login_required` decorator is unchanged. A new `org_required` decorator enforces org membership.  
**No auth change in 5h.** This is the deferred-build seam.

### Billing seam (billing.py)
Current: all Stripe metadata carries `business_id`; `create_checkout_session(business_id)` creates a Stripe customer on the business row.  
New: `create_checkout_session(org_id)` creates a Stripe customer on the org row; all invoice webhooks resolve `org_id` from Stripe metadata.  
`_owner_email(org_id)` reads from org_members JOIN users rather than `users WHERE business_id=?`.  
Usage grants become `org_id`-scoped; `conversations_remaining(org_id)` sums grants for the org.  
**No billing change in 5h.** This is the deferred-build seam.

### Dispatcher ownership check (app.py:1436-1473)
Current gap: `db.get_lead(lead_id)` with no business_id check.  
Fix (can land in 5h OR separately as a P2 security patch):
```python
# In dispatcher_twiml and dispatcher_connect_twiml:
lead = db.get_lead(lead_id)
# Verify the TwiML call is for a lead that belongs to the FROM number's business.
# The Twilio FROM number on the request is the owner's number; resolve biz from it.
# If lead.business_id != biz.id: return 403 TwiML or generic hang-up.
```
The fix is 2-3 lines per route. This is the one P2 item that can and should ship independently of the full Crew build — it doesn't require the org schema.

---

## 6. What Is Design-Only vs Deferred Build

| Item | Phase 5h (now) | Deferred Crew build |
|---|---|---|
| `orgs` table DDL | Draft + review | `db.init_db()` addition |
| `org_members` table DDL | Draft + review | `db.init_db()` addition |
| `businesses.org_id` column | Draft | ALTER TABLE migration |
| `usage_grants.org_id` column | Draft | ALTER TABLE migration |
| Heritage 1-seat migration SQL | Draft + safety review | Tested migration script |
| `current_org()` in auth.py | Seam documented | Implemented |
| `org_required` decorator | Seam documented | Implemented |
| `create_checkout_session(org_id)` | Seam documented | Implemented |
| Crew UI (location switcher) | Out of scope | Pro/Crew tier feature |
| `org_members` invite/accept flow | Out of scope | Pro/Crew tier feature |
| Dispatcher ownership check | Design documented here | Patch as P2 security fix NOW |

---

## 7. Migration Safety Rules

1. **Additive only.** No DROP TABLE, no DROP COLUMN, no RENAME COLUMN in this migration.
2. **Heritage tenant stays on business_id=1.** All existing foreign keys on `business_id` continue to resolve. The `org_id` columns are nullable; old code ignores them.
3. **Billing stays on businesses.stripe_customer_id** until `billing.py` is rewired. Two sources of truth temporarily — document this in code and resolve in the Crew build.
4. **users.business_id stays.** Deprecate-in-code comment added; column dropped only after auth.py is migrated and verified.
5. **Test the migration against `firstback.db` on staging before prod.** The Heritage tenant has real usage_grants and billing rows; verify the backfill is idempotent.
6. **Rollback path:** because it is additive, the schema change can be rolled back by dropping the new tables (orgs, org_members) and the two new columns. Existing tables/data are untouched.

---

## 8. Risks

### Primary risk: dual billing source-of-truth
If `billing.py` writes `businesses.stripe_customer_id` AND the org has `orgs.stripe_customer_id`, webhook resolution can go to the wrong row. The migration copies billing state to `orgs` in step 1 — then `billing.py` must stop writing to `businesses` before the next Stripe event. This is a careful ordering problem. Mitigate: in the Crew build, add a `deprecated_stripe_customer_id` rename comment on `businesses.stripe_customer_id` and gate the write path on a feature flag (`CREW_BILLING_ACTIVE`).

### Secondary risk: Heritage tenant disruption
The migration runs on the live single DB. If `orgs.id=1` is inserted with mismatched billing state, Heritage could lose subscription status. Mitigate: run a SELECT-only dry-run first, verify the copied billing row, then INSERT.

### Third risk: auth.py `business_id` hardcoding
There are ~10 call sites in `app.py` that call `current_business()` and assume it returns one result. In the Crew build, `current_business()` must be context-aware (session-selected) or routes break for multi-location owners. Audit all `current_business()` call sites before the auth rewrite.

### Dispatcher P2 gap
The Phase-4 P2 gap (`/twiml/dispatcher/<lead_id>` no ownership check) should be patched as a standalone security fix before Crew ships — it is independent of the schema design and takes <1h. Reference: `app.py:1436-1473`, `app.py:1459-1473`.

---

## 9. Design Verdict

The proposed org->users->businesses->numbers hierarchy is correct and migration-safe. The key insight: the `business` is already the right unit of isolation for all data (leads, messages, calls, numbers) — do not change this. The `org` is only the billing + access-control wrapper above it. Starter ships as the 1-seat case (org with one business, one user, one number) without ANY visible Crew UI — Dave's experience is unchanged. The schema additions are purely additive and reversible. The dispatcher P2 gap should be patched independently and immediately (2-3 lines per route, no schema change needed).

The Crew build (the actual wiring of auth, billing, and UI) is a separate phase gated on a Crew-tier pricing decision and at least one real multi-location customer request.
