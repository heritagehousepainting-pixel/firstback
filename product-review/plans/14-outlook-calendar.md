# Plan 14 — Outlook Calendar (Microsoft Graph API)
**Workstream:** Second calendar provider alongside Google Calendar
**Stage:** P6 from DEV-HANDOFF-2026-06-23.md
**Build target:** `staging` only. Owner gates every staging→main promotion.
**Date:** 2026-06-23
**Status:** BUILD-READY plan — READ-ONLY, no code changed

---

## Scope
Add Microsoft Outlook / Microsoft 365 as a second calendar provider with the **same surface as Google Calendar**:
- Read free/busy → `busy_slot_ids()` → AI avoids double-booking.
- Write a 1-hour event on booking → `create_event_async()`.
- Cancel that event on appointment cancel → `cancel_event_async()`.
- OAuth: Microsoft Identity Platform auth-code flow; encrypted tokens; refresh on demand.
- Additive: a business may connect BOTH Google and Outlook; availability = union of busy slots; events written to both.
- Gated by `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` — silent no-op until set.

**Not in v1:** Apple/CalDAV; Outlook Contacts; Teams meeting links (v2); multi-calendar selection (primary only, like Google).

---

## Gated Env Vars (default empty → safe no-op)
```
MICROSOFT_CLIENT_ID        # Azure AD app client ID
MICROSOFT_CLIENT_SECRET    # Azure AD app client secret
MICROSOFT_REDIRECT_URI     # default: http://127.0.0.1:8800/api/calendar/outlook/callback
MICROSOFT_TENANT_ID        # 'common' (orgs + personal) or tenant UUID; default 'common'
```
Added to `config.py` per the `GOOGLE_CLIENT_ID` pattern.

---

## OAuth Flow (mirrors google_cal.py)
- **Azure App Registration:** account types "any org directory + personal MS accounts" (`tenant=common`);
  redirect URI; delegated permissions `Calendars.ReadWrite`, `offline_access`; client secret.
- **Auth URL:** `https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?client_id=…&response_type=code&redirect_uri=…&response_mode=query&scope=https://graph.microsoft.com/Calendars.ReadWrite offline_access&state={state}&prompt=consent` (`prompt=consent` ensures a refresh token, like Google).
- **Token exchange:** POST `…/oauth2/v2.0/token` (`grant_type=authorization_code`) →
  `db.set_oauth_tokens(business_id, "outlook", access, refresh, expiry_iso)` (encrypts via `token_crypto`).
- **Timezone on connect (SF-5):** GET `https://graph.microsoft.com/v1.0/me/mailboxSettings` → `timeZone`,
  validate via `ZoneInfo`, store via `db.set_business_timezone`; fail-open.
- **Refresh:** `_access_token` mirrors google_cal — `google_oauth.access_is_fresh` then refresh POST; on failure None (fail-open).
- **Disconnect:** `db.set_oauth_tokens(business_id, "outlook", None, None, None)`.

---

## DB Schema / Migrations
- `integrations` already has the columns; provider key `"outlook"`; reuse `calendar_id` for the Outlook default-calendar id.
- `appointments`: add `outlook_event_id TEXT` (mirrors `google_event_id`).
- Add `db.set_outlook_event_id(appointment_id, event_id)` helper (mirrors `set_google_event_id`).

---

## Integration Points — where google_cal is called, where Outlook plugs in
| Site | What it does | Outlook addition |
|---|---|---|
| `handle_inbound` booking (~2075) | `google_cal.create_event_async(...)` | add `outlook_cal.create_event_async(...)` |
| `open_conversation` booking (~1940) | `google_cal.create_event_async(...)` | same |
| `api_cancel_appointment` (~2265) | `google_cal.cancel_event_async(...)` | add `outlook_cal.cancel_event_async(biz_id, outlook_event_id)` |
| availability (~2019) | `exclude = google_cal.busy_slot_ids(...)` | `exclude = google_cal.busy_slot_ids(...) \| outlook_cal.busy_slot_ids(...)` |

Each addition is behind `outlook_cal.is_connected(biz_id)`, writes in a daemon thread, reads merged as a
set; an Outlook failure never affects the Google path or the booking. (Line numbers approximate — locate
at build time; app.py was trimmed in the prod cleanup, so re-grep the call sites.)

---

## `outlook_cal.py` — module design (mirrors google_cal.py)
```python
TENANT_ID = config.MICROSOFT_TENANT_ID or "common"
AUTH_URL  = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
API_BASE  = "https://graph.microsoft.com/v1.0"
SCOPES    = "https://graph.microsoft.com/Calendars.ReadWrite offline_access"
PROVIDER  = "outlook"
```
- `configured()` = `bool(MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET)`.
- `is_connected(business_id)` via `db.get_integration(business_id, "outlook")`.
- `busy_slot_ids()` — GET `{API_BASE}/me/calendar/calendarView` over the booking horizon; normalize
  `{value:[{start:{dateTime,timeZone}, end:{...}}]}` → set of `YYYY-MM-DD@HH:MM` via pure `_graph_slots_conflicting`.
- `create_event()` — POST `{API_BASE}/me/calendar/events` (or `…/calendars/{calendar_id}/events`) with
  `{subject, body, start{dateTime,timeZone}, end{...}}`; returns `id`. (Graph carries tz per field — no UTC conversion.)
- `create_event_and_store / create_event_async / cancel_event / cancel_event_async` — identical pattern;
  use `db.set_outlook_event_id` + `appointments.outlook_event_id`.
- Calendar id: GET `{API_BASE}/me/calendar` for default `id`; fall back to `me/calendar` base path if null.

---

## Step-by-Step Build Order
1. Config + gate (test: `configured()` False when unset).
2. DB migration `outlook_event_id` + `db.set_outlook_event_id` (idempotent).
3. `outlook_cal.py` core (all HTTP mockable): configured/is_connected/auth_url/connect_with_code/disconnect,
   `_access_token`, `_expiry_iso`, `busy_slot_ids` + pure `_graph_slots_conflicting`, create/cancel (+async).
4. Routes: `GET /api/calendar/outlook/connect`, `GET /api/calendar/outlook/callback`,
   `POST /api/calendar/outlook/disconnect` (CSRF).
5. Merge busy-slot union in app.py (two-line change; both empty when unconnected).
6. Add Outlook event creation in both booking paths (additive, guarded; store `outlook_event_id`).
7. Add Outlook cancellation in cancel path (when `outlook_event_id` set + connected).
8. Settings UI "Outlook Calendar" card behind `configured()`; both providers shown simultaneously; add
   `"outlook_calendar"` to `connections.recommended_setup`.
9. Mocked tests.

---

## Mocked Test Plan (no live creds) — `test_outlook_cal.py`
45 cases: configured/connected gating; auth_url; connect_with_code success/failure + mailboxSettings tz
(success + bad-tz fail-open); disconnect; `_access_token` fresh/stale/no-refresh/refresh-failure;
busy_slot_ids success/all-day/error/unconnected; `_graph_slots_conflicting` timed/non-crossing/all-day;
create_event success/error; create_event_and_store; create_event_async thread; cancel_event 204/404/unconnected;
cancel_event_async thread; routes (connect without creds, callback wrong-state, callback valid, disconnect CSRF);
**busy-slot union** (both / google-only / outlook-only / neither); booking fires both when both connected /
only google otherwise; cancel fires Outlook only when id set; migration idempotent; token encryption;
cross-tenant; **no double-booking** (UNIQUE constraint unchanged).

---

## Security
- **Tokens encrypted** via `db.set_oauth_tokens` → `token_crypto`; dual-read legacy.
- **Cross-tenant isolation:** scoped by `business_id`; composite PK; routes `@login_required`;
  `set_outlook_event_id` scoped via tenant-scoped cancel path (no raw appt-id bypass).
- **OAuth state CSRF:** `session["ol_state"]` set/verified (mirrors `g_state`).
- **Least privilege:** scopes only `Calendars.ReadWrite offline_access` — no mail/contacts/directory.
- **No Graph webhooks v1** (change notifications deferred; would need validation handshake + signature).
- **Personal vs work:** `tenant=common` supports Outlook.com + M365 (correct for contractors).

---

## Honesty / UI Copy
- Unconfigured: "Outlook Calendar — contact us to enable, or sign in with Google." No broken Connect.
- Both Google + Outlook can show "Connected" simultaneously (not mutually exclusive).
- Only claim timezone detection when the read actually succeeds.
- Event titles mirror Google: "Estimate: [Lead Name]".

## Acceptance Criteria
1. Unset key → silent no-op everywhere, no import errors. 2. Set + unconnected → "Connect Outlook".
3. OAuth → encrypted tokens, `connected=1`, tz persisted if available. 4. `busy_slot_ids` correct from
mocked calendarView. 5. Google+Outlook busy sets merge; no slot offered if blocked on either. 6.
`create_event_async` stores `outlook_event_id`. 7. `cancel_event_async` fires when id set. 8. No
double-booking (UNIQUE on `appointments(business_id, day, slot_time)` unchanged; Outlook additive). 9. Both
providers connectable at once. 10. All 45 tests pass without live creds. 11. Cross-tenant isolation. 12.
Prod `TOKEN_ENC_KEY` requirement auto-covers Outlook tokens (same `set_oauth_tokens` path).

## Risks for audit to scrutinize
1. Jobber GraphQL query shapes for `clients`/`jobs` + `requestCreate` availability per plan tier.
2. Graph `calendarView` timezone normalization — personal Outlook.com may return Windows tz names
   ("Eastern Standard Time") not IANA; need a conversion/zoneinfo fallback.
3. Jobber clients via review-queue vs bulk-accept/auto-apply for 500+ clients (friction).
4. Per-provider `*_event_id` columns vs a `calendar_events(appointment_id, provider, event_id)` table now.
5. MS refresh-token expiry on personal accounts (24h inactivity / 90d) → need a "re-auth needed" state.

*READ-ONLY planning. No code written or modified.*
