# Plan 13 — FSM Sync: Jobber (v1, Read-Only)
**Workstream:** Field-service management read-only sync
**Stage:** P2 from DEV-HANDOFF-2026-06-23.md
**Build target:** `staging` only. Owner gates every staging→main promotion.
**Date:** 2026-06-23
**Status:** BUILD-READY plan — READ-ONLY, no code changed

---

## Jobber vs. Housecall Pro — Recommendation

| Dimension | Jobber | Housecall Pro |
|---|---|---|
| OAuth maturity | Standard OAuth 2.0 PKCE; documented read-only scopes per resource | OAuth 2.0; fewer granular read scopes in v1 API |
| Read access to customers/jobs | `read_clients`, `read_jobs` scopes; REST + GraphQL; standard pagination | `/customers` + `/jobs` REST; thinner docs |
| Push (leads/notes) | `create_requests` scope → push a "quote request" | Can POST a customer note; less native "external lead" concept |
| Market fit | Strong in painting, landscaping, HVAC, plumbing — FirstBack's ICP | Strong in HVAC, plumbing, cleaning; less painting/landscaping |
| Webhook support | Yes, HMAC-SHA256 signatures | Yes, fewer event types for this use case |
| API stability | Versioned stable releases w/ deprecation notices | Generally stable; older API style |

**Recommendation: Jobber for v1.** Mature API, OAuth scopes align cleanly (read clients/jobs,
write quote-requests), market penetration maps to the home-services ICP, HMAC-SHA256 webhooks are a
known pattern. The abstraction below keeps a `FSM_PROVIDER` interface so Housecall Pro plugs in later
without touching screening logic.

**FLAG: OWNER decision required** before S3 builds the real client — confirm Jobber vs HCP. The plan
+ provider scaffolding can proceed either way.

---

## Scope

**V1 is strictly read-only plus one additive write:**
- Pull clients (phones + names) from Jobber → seed `contacts` as category `customer`, source
  `import-jobber`, feeding the existing `is_known_caller` / `screen_caller` path.
- Pull open/completed jobs → enrich the same contacts with a note (job title, status).
- Push a booked FirstBack estimate as a Jobber "quote request" (additive — a new request, NOT
  editing existing jobs/customers).
- NOT an FSM replacement. No job management, no scheduling sync, no two-way customer edits.

**Not in v1:** writing to existing Jobber jobs/customers; syncing Jobber calendar as busy times (that's
the calendar integration's job); Jobber→FirstBack webhooks (v2); Housecall Pro (plugs in via interface).

---

## Gated Env Vars
All default empty. Until set, every entry point is a safe no-op:
```
JOBBER_CLIENT_ID          # OAuth2 app client ID (Jobber Developer portal)
JOBBER_CLIENT_SECRET      # OAuth2 app client secret
JOBBER_REDIRECT_URI       # default: http://127.0.0.1:8800/api/fsm/jobber/callback
JOBBER_WEBHOOK_SECRET     # HMAC-SHA256 secret for inbound webhooks (v2, optional now)
FSM_SYNC_INTERVAL_HOURS   # periodic sync frequency (default: 24)
```
Added to `config.py` per the `GOOGLE_CLIENT_ID` / `GOOGLE_PLACES_API_KEY` pattern. No `_is_prod`
hard-fail (integration is entirely additive; missing creds → silent no-op).

---

## Provider Abstraction
`fsm_provider.py` — thin interface so HCP can plug in later without touching triage.py, db.py, or routes:
```python
class FSMProvider:
    PROVIDER_KEY = ""            # "jobber" | "housecall_pro"
    def configured(self) -> bool: ...
    def is_connected(self, business_id: int) -> bool: ...
    def auth_url(self, state: str) -> str: ...
    def connect_with_code(self, business_id: int, code: str) -> None: ...
    def disconnect(self, business_id: int) -> None: ...
    def fetch_clients(self, business_id: int) -> list[dict]: ...    # [{name, phones, email}]
    def fetch_jobs(self, business_id: int) -> list[dict]: ...       # [{title, status, client_phone}]
    def push_quote_request(self, business_id: int, lead: dict, booking: dict) -> str | None: ...
```
`jobber_fsm.py` implements it. `fsm_sync.py` consumes the interface and holds all business logic.

---

## OAuth Flow (mirrors google_contacts.py)
- **Register:** Jobber Developer Portal app; redirect URI; scopes `read_clients`, `read_jobs`, `write_quote_requests`.
- **Auth URL:** `https://api.getjobber.com/api/oauth/authorize?client_id=…&redirect_uri=…&response_type=code&state={state}`.
- **Token exchange:** POST `https://api.getjobber.com/api/oauth/token` (`grant_type=authorization_code`) →
  `db.set_oauth_tokens(business_id, "jobber", access, refresh, expiry_iso)` (encrypts via `token_crypto`).
- **Refresh:** `_access_token` mirrors `google_contacts._access_token` — check `google_oauth.access_is_fresh`,
  refresh if stale; on failure return None (fail-open: sync skips, never breaks screening).
- **Disconnect:** `db.set_oauth_tokens(business_id, "jobber", None, None, None)`; keeps already-synced contacts.

---

## DB Schema / Migrations (additive `ALTER TABLE ADD COLUMN`, idempotent pragma-check)
- No new tables (existing `contacts` handles synced clients with `source='import-jobber'`).
- `businesses`: `fsm_last_synced_at TEXT`, `fsm_clients_synced INTEGER DEFAULT 0`.
- `appointments`: `fsm_external_id TEXT`, `fsm_pushed_at TEXT`.
- `integrations` table already has `(business_id, provider, connected, …, access_token, refresh_token, token_expiry)`; provider key `"jobber"`.

---

## Integration Points
**A. Call screening ("skip contacts you know"):** `triage.screen_caller` → `db.is_known_caller` →
`contacts`. Jobber sync calls `contact_import.ingest(business_id, contacts, source="import-jobber")` →
same `contact_suggestions` (status `pending`, `suggested_category='customer'`) the owner bulk-accepts in
the existing UI. **No changes to triage.py / screen_caller / is_known_caller.** (Owner-friendly note:
v2 could add auto-accept; v1 keeps the review queue for safety.)

**B. Booking push (additive write):** after `db.book_appointment` + `google_cal.create_event_async`,
add a guarded daemon-thread `fsm_sync.push_booking_async(...)`; stores Jobber request ID in
`appointments.fsm_external_id`. Failure never breaks the booking/reply.

**C. Periodic sync:** `fsm_sync.maybe_sync_all()` called from `reminders.tick_once` in its own try/except
(like other scans); respects `FSM_SYNC_INTERVAL_HOURS`; first connect syncs immediately. Driven by the
existing `TASKS_SECRET`-gated `/tasks/run-due` cron.

**D. Settings UI:** "Connect Jobber" card behind `configured()` gate (hidden/placeholder when unset);
Connected state shows "Last synced: N clients" + "Sync now" + Disconnect; deep-link to contacts review.
Add `jobber` to `connections.recommended_setup` as optional.

---

## Step-by-Step Build Order
1. Config + no-op gate (test: `configured()` False when unset).
2. DB migrations (test: idempotent on fresh + existing DB).
3. `fsm_provider.py` interface (importable without creds).
4. `jobber_fsm.py` — `configured/is_connected/auth_url/connect_with_code/disconnect`, `_access_token`,
   `fetch_clients` (paginated GraphQL), `fetch_jobs` (last 90d), `push_quote_request` (mutation).
5. `fsm_sync.py` — `configured`, `sync_clients` (ingest), `sync_jobs` (enrich), `push_booking_async`,
   `maybe_sync_all`, `push_configured`.
6. App routes: `GET /api/fsm/jobber/connect`, `GET /api/fsm/jobber/callback`,
   `POST /api/fsm/jobber/disconnect`, `POST /api/fsm/sync` (all `login_required` + CSRF where mutating).
7. Hook booking push into app.py (additive, guarded).
8. Hook `maybe_sync_all` into `reminders.tick_once` (isolated try/except).
9. Settings UI card + `recommended_setup`.
10. Mocked tests.

---

## Mocked Test Plan (no live creds) — `test_fsm_sync.py`
35 cases covering: configured/connected gating; auth_url; connect_with_code success + failure;
`_access_token` fresh/stale/no-refresh/refresh-failure; disconnect; fetch_clients success/error/pagination;
fetch_jobs; ingest with `source='import-jobber'` + dedupe; push_quote_request success/error;
push_booking_async stores id / no-op when unconfigured; maybe_sync_all skip-unconnected / skip-within-interval
/ sync-eligible; routes (connect without creds, callback wrong-state, callback valid, disconnect CSRF, sync);
migrations idempotent; token encryption; **screen_caller trusted after accept**, **prospect while pending**.

---

## Security
- **Tokens encrypted** via `db.set_oauth_tokens` → `token_crypto.encrypt`; dual-read legacy plaintext.
- **Cross-tenant isolation:** every read/write scoped by `business_id`; composite PK `(business_id, provider)`;
  routes `@login_required` + `current_business()["id"]`.
- **OAuth state CSRF:** `session["fsm_j_state"]` set on connect, verified on callback (mirrors `g_state`).
- **Webhook signature (v2):** HMAC-SHA256 of raw body vs `JOBBER_WEBHOOK_SECRET`, constant-time compare, before any DB write.
- **Rate limits:** 24h sync interval + `_MAX_PAGES` cap on pagination.
- **Data minimalism:** fetch only name/phones/email (+ job title/status); no financial/PII detail.

---

## Honesty / UI Copy
- Unconfigured card: "Jobber sync coming soon — contact us to enable." No broken Connect button.
- "N client suggestions imported — review and confirm below." Never "N clients imported" until accepted.
- Never "Estimate pushed to Jobber" unless `fsm_external_id` stored (a CREATE 200 ≠ operator saw it).
- Pricing: Pro+ or $29/mo add-on once stable (per DEV-HANDOFF); v1 gated at env level only.

## Acceptance Criteria
1. Unset key → every entry point a silent no-op, no import errors. 2. Set but unconnected → "Connect Jobber".
3. OAuth → encrypted tokens, `connected=1`. 4. `sync_clients` → suggestions `source='import-jobber'`/`pending`.
5. Accept → `contacts` `category='customer'`. 6. Next call from that number → `screen_caller` `trusted`,
no text. 7. Pending suggestion → still `prospect`. 8. Push → `fsm_external_id` on success, booking still
succeeds on failure. 9. `maybe_sync_all` respects interval. 10. All 35 tests pass without live creds.
11. No double-booking (unchanged). 12. Cross-tenant isolation holds.

*READ-ONLY planning. No code written or modified.*
