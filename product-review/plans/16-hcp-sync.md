# Plan 16 — Housecall Pro FSM Sync (v1, read-only)
**Stage:** P2 second provider · **Date:** 2026-06-23 · build target `staging`; owner gates main.
**Status:** PLAN. ⚠️ HCP API shapes below are **ASSUMED** (the planner couldn't verify live HCP docs) —
S2 must verify Q1–Q5 against real HCP docs before S3 builds, or the mocks encode wrong contracts.

## Scope (same as Jobber/Plan 13)
Read-only: pull customers/jobs → `db.upsert_suggestion(category="customer", source="import-hcp")` (feeds
screening "skip contacts you know"); push a booked estimate as a **customer note** (HCP has no native
"quote request" object — biggest difference vs Jobber). Carry forward audited Jobber fixes verbatim:
**F1** (upsert-direct, never `contact_import.ingest`), **F2** (`recommended_setup` 3-touch), honesty copy
("synced" not "imported"; never "pushed" unless `fsm_external_id` stored).

## HCP API — ASSUMED (S2 verify via live docs)
- OAuth: `https://auth.housecallpro.com/oauth/{authorize,token}`; base `https://api.housecallpro.com`.
- **Auth header: `Token token="<tok>"`** (NOT `Bearer`) — key diff.
- Scopes: `read:customers read:jobs` (+ maybe a write scope for notes).
- `GET /customers?page&per_page=25` → `{customers:[{id,first_name,last_name,mobile_number,home_number,work_number,email}], total_pages, page}`. REST page pagination (not GraphQL cursors). Normalize first+last→name, 3 phone fields→phones list.
- `GET /jobs` → `{jobs:[{id,work_status,note,customer:{id,mobile_number}}]}`. Title proxy = `note`.
- Push: resolve phone→customer via `GET /customers?mobile_number=`, then `POST /customers/{id}/notes`. No match → return None (fail-open). Store note id via `db.set_fsm_external_id`.

## Provider selection — RECOMMEND Option C (owner sign-off in S2)
`fsm_sync` currently hardcodes Jobber. Add `_get_active_provider(business_id)` → route to whichever
provider `is_connected` (HCP > Jobber tiebreak; if both, prefer HCP + log warning → no double-fire).
`configured()`/`push_configured()` return True if EITHER configured. Simplest correct for single-tenant
dogfood; a per-business `fsm_provider` column is the v2 path if businesses ever run both at once.

## File touch list (no DB schema change — `integrations` PK (business_id, provider); reuse fsm_* columns)
- `config.py`: `HCP_CLIENT_ID/SECRET/REDIRECT_URI` (mirror JOBBER_*).
- **NEW `hcp_fsm.py`**: `HCPProvider(FSMProvider)` mirroring `jobber_fsm.py` (configured/is_connected/auth_url/
  connect_with_code/disconnect/_access_token fail-open/_get/_post/fetch_clients paginated/fetch_jobs/
  push_quote_request [phone-lookup→note]). Reads creds from `config.*`, import-safe with no creds.
- `fsm_sync.py`: add `import hcp_fsm` + `_get_active_provider`; replace `jobber_fsm.*` calls; source/reason
  from `provider.PROVIDER_KEY` (`import-hcp` / "Housecall Pro").
- `app.py`: `import hcp_fsm`; `/api/fsm/hcp/{connect,callback,disconnect}` (state `fsm_h_state`, CSRF on
  disconnect); update `/api/fsm/sync` to abstracted checks; settings ctx (`hcp_configured/hcp_connected/
  hcpconnected/hcperror`); `recommended_setup` call-site `hcp_connected=...` (F2 touch 3).
- `connections.py`: `recommended_setup` `hcp_connected=False` kwarg + "hcp" rows entry (F2 touches 1+2).
- `templates/settings.html`: HCP card (anchor `set-hcp`, mirror Jobber card) + sync/disconnect JS hitting
  `/api/fsm/sync` + `/api/fsm/hcp/disconnect`. **S4 must smart-quote/parse scan this (bit us twice).**
- **NEW `test_hcp_fsm.py`** (~45) + augment `test_fsm_sync.py` provider-selection (~10).

## Build order: config → hcp_fsm.py → fsm_sync routing refactor → app routes → connections F2 → settings card → tests green.

## Mocked test plan (no creds): configured/connected gating; auth_url; connect success/fail; _access_token
fresh/stale/no-refresh/refresh-fail; disconnect; fetch_clients pagination+name-join+multi-phone+error;
fetch_jobs; **sync_clients F1 (upsert category=customer source=import-hcp, ingest never called)**+dedup;
push success(found)/no-match(None)/error(None); push_booking_async stores id/no-op; **provider-selection
(only-jobber/only-hcp/neither/both→HCP+warn)**; maybe_sync_all routing+interval; routes (connect-unconfigured,
callback-wrong-state, callback-valid, disconnect-CSRF, sync routes to HCP); token encryption; screen_caller
trusted-after-accept / prospect-while-pending.

## Security/honesty: encrypted tokens (set_oauth_tokens); business_id scoping; `fsm_h_state` verify-and-consume;
CSRF on mutations; _MAX_PAGES cap; data minimalism (name/phone/email only); never overclaim "pushed"/"imported".

## Acceptance: unset key → all hcp no-ops, Jobber unaffected; OAuth→encrypted tokens; sync→suggestions
import-hcp/pending; accept→trusted (no text), pending→prospect; push→fsm_external_id on success, booking
survives push failure; maybe_sync_all routes correctly + no regression to Jobber; both connected→only HCP;
~60 tests green; no double-booking; cross-tenant isolation; no smart-quote Jinja delimiters.

## S2 must verify (BLOCKING): Q1 OAuth URLs · Q2 auth header (`Token token=` vs Bearer) · Q3 scope strings ·
Q4 note-write scope (or push degrades to silent no-op) · Q5 pagination style · Q6 job description field ·
Q7 OWNER: provider-selection Option C sign-off · Q8 refresh-token lifetime (apply Outlook F8 reconnect if
short-lived) · R1 phone-filter on /customers (else push no-ops) · R2 429 handling · R3 shared sync-stamp.
**Use WebSearch/WebFetch on the HCP developer docs to confirm/correct Q1–Q6 — do not ship mocks built on guesses.**
