# PREDEPLOY-02-AUTH — Auth / Sessions / Access Control / Multi-Tenant Isolation

Auditor lane: AUTH / SESSIONS / ACCESS CONTROL / MULTI-TENANT ISOLATION  
Branch: staging @ 55d2601 | Date: 2026-06-19

---

## Methodology

Read-only audit of `auth.py`, `config.py`, `db.py`, `db_core.py`, `token_crypto.py`, `google_oauth.py`, and the full `app.py` (3 159 lines). Cross-referenced with `test_auth_reset.py`, `test_confirm_token.py`, `test_screening_ui.py`, and `test_growth_tray_ui.py`.

---

## 1. Login / Session Hygiene

### 1a. Password hashing
`app.py:329` — `generate_password_hash(password)` (Werkzeug, bcrypt-backed). ✅

### 1b. No user enumeration
`app.py:394-395` — failed login always returns "Email or password is incorrect." regardless of whether the email exists. ✅  
`app.py:440` — `/auth/forgot` returns the same page (`sent=True`) whether or not the email is registered. ✅

### 1c. Login rate-limit
`app.py:357-377` — in-memory dict, 10 failures / 5-min window per `(email, IP)` pair. Fails open only on the in-memory dict (reset on process restart). ✅

### 1d. X-Forwarded-For spoofing — rate-limit bypass (P1)
`app.py:363`:
```python
ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
```
No `werkzeug.middleware.proxy_fix.ProxyFix` is applied anywhere in `app.py`. On Render, the real client IP arrives as `X-Forwarded-For: <client>, <render-proxy>`. The code takes `.split(",")[0]` — which is the **leftmost** (potentially client-controlled) value. An attacker can send:
```
X-Forwarded-For: 1.2.3.4
```
and rotate synthetic IPs to bypass the per-IP rate limiter, running unlimited credential-stuffing attempts.

**Severity: P1** — the rate limiter exists solely to stop credential stuffing; this header-injection trivially defeats it. Fix: apply `ProxyFix(app, x_for=1)` (trusts only the rightmost hop) or use `request.remote_addr` exclusively (which is set to Render's proxy address, making keying on email-only the safer fallback).

### 1e. Session fixation — cleared on login
`app.py:396`: `session.clear()` before setting `session["uid"]`. ✅  
`app.py:346`: same for signup. ✅

### 1f. Session cookie flags
`app.py:53-54`:
```python
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = SESSION_COOKIE_SECURE  # gated on FIRSTBACK_HTTPS
```
HttpOnly is Flask's default. `config.py:304` gates `SESSION_COOKIE_SECURE` on `FIRSTBACK_HTTPS`. `render.yaml:49` sets `FIRSTBACK_HTTPS=1`. ✅

---

## 2. Password Reset Token

### 2a. Entropy
`app.py:425`: `secrets.token_urlsafe(32)` — 256 bits. ✅

### 2b. Single-use
`db.py:3522-3539`: `consume_password_reset_token` atomically sets `used=1` before returning `uid`. A second call finds `row["used"]` truthy and returns `None`. Tested in `test_auth_reset.py:83-86`. ✅

### 2c. TTL
`app.py:411`: 1-hour TTL via `expires_at`; `db.py:3532` compares against `now` before redemption. ✅

### 2d. Not logged
`app.py:425-439` — the token is sent only by email. Not printed or logged anywhere in app.py. ✅

---

## 3. SECRET_KEY + TOKEN_KEY Production Fail-Fasts

`config.py:273-280`: if `FIRSTBACK_HTTPS=1` or `FIRSTBACK_ENV=production` and `SECRET_KEY == "dev-insecure-secret-change-me"` → `raise RuntimeError`. ✅  
`config.py:293-298`: same guard for `TOKEN_ENC_KEY` being empty in production → `raise RuntimeError`. ✅  
`config.py:430-433`: same for `SEED_OWNER_PASSWORD` dev default in production. ✅  
`render.yaml:40-41`: `FIRSTBACK_SECRET` is `generateValue: true` (random on deploy). ✅  
`test_auth_reset.py:210-229`: subprocess test proves these fire as real `RuntimeError` at import. ✅

All three fail-fasts are real top-level guards; no environment-bypass path found. ✅

---

## 4. CSRF Coverage

### 4a. Mechanism
`app.py:242-256`: double-submit cookie. `_csrf_token()` generates a `secrets.token_hex(32)` stored in `session["csrf_token"]`. `_csrf_ok()` compares `session["csrf_token"]` vs `request.form.get("_csrf", "")` using `secrets.compare_digest`. ✅

**Note:** `_csrf_ok()` only reads `request.form`, not JSON body or headers. Routes that use `request.get_json()` cannot pass this check; they rely on `SameSite=Lax` alone.

### 4b. Explicitly CSRF-checked POST routes (covered)
| Route | Line |
|---|---|
| `/assistant` | 824 |
| `/assistant/stream` | 860 |
| `/assistant/learn` | 910 |
| `/assistant/confirm` | 929 |
| `/growth/tray/release` | 1341 |
| `/growth/tray/skip/<id>` | 1352 |
| `/api/calls/<id>/engage` | 2151 |
| `/api/calls/<id>/real` | 2180 |
| `/api/calls/<id>/flag-spam` | 2216 |
| `/api/leads/<id>/flag-spam` | 2234 |

### 4c. Login-required POST routes without explicit `_csrf_ok()` — P1 (partial)

The following state-mutating, session-authenticated POST routes have **no explicit CSRF check**. They are defended only by `SameSite=Lax`:

**Form-body routes** — `SameSite=Lax` blocks cross-site form POSTs in all modern browsers:

| Route | Risk if CSRF succeeded |
|---|---|
| `app.py:1031` `/training/teach` | plant arbitrary AI routing rules |
| `app.py:1050` `/training/resolve` | suppress flagged gaps |
| `app.py:1060` `/digest/send` | trigger email send |
| `app.py:1126` `/settings` | change business name, alert phone/email, etc. |
| `app.py:1223` `/settings/password` | **change password** (mitigated: requires current password) |
| `app.py:1292` `/settings/growth_mode` | change TCPA-sensitive growth mode |
| `app.py:1413` `/setup/profile` | overwrite A2P legal name/EIN/address |
| `app.py:1432` `/setup/number` | provision or attach a Twilio number |
| `app.py:1461` `/setup/a2p` | submit A2P campaign registration |
| `app.py:1496` `/setup/forwarding` | change call-forward destination |
| `app.py:2072` `/api/calendar/google/disconnect` | revoke Google Calendar |
| `app.py:2079` `/api/appointments/<id>/cancel` | cancel estimate + send cancellation SMS |
| `app.py:2407` `/api/contacts/google/sync` | trigger Google Contacts import |
| `app.py:2421` `/api/contacts/google/disconnect` | revoke Google Contacts |
| `app.py:2906` `/billing/checkout` | initiate Stripe checkout session |
| `app.py:2925` `/billing/portal` | access Stripe billing portal |

**JSON-body routes** — `SameSite=Lax` blocks cross-site `fetch` POSTs (session cookie not sent cross-origin), so these are effectively protected by `SameSite=Lax` in modern browsers as well. However, there is no defense-in-depth CSRF token:

| Route | Risk |
|---|---|
| `app.py:1947` `/api/sim/incoming` | creates lead records |
| `app.py:1972` `/api/sim/reply` | runs AI conversation |
| `app.py:2023` `/api/calendar/busy` | marks a calendar day busy |
| `app.py:2034` `/api/integrations` | toggles third-party integrations |
| `app.py:2116` `/api/contacts` | tags phone numbers |
| `app.py:2133` `/api/contacts/delete` | removes contact tags |
| `app.py:2261` `/api/suggestions/<id>/accept` | accepts caller suggestion |
| `app.py:2279` `/api/suggestions/<id>/dismiss` | dismisses suggestion |
| `app.py:2290` `/api/suggestions/<id>/reopen` | reopens suggestion |
| `app.py:2305` `/api/suggestions/bulk` | bulk-action suggestions |
| `app.py:2341` `/api/contacts/import` | bulk imports contacts |

**Assessment:** In a production context with `SameSite=Lax` on all modern browsers, the practical CSRF risk on these routes is low. However, `/api/appointments/<id>/cancel` (form-based, sends a real cancellation SMS to a customer, `app.py:2097`) is the highest-consequence unguarded mutation. `/billing/checkout` and `/billing/portal` (Stripe) deserve CSRF protection even under SameSite=Lax, because a targeted attack page on a subdomain or a compromised local site could still forge them.

**Severity: P1** for `/api/appointments/<id>/cancel` (triggers outbound SMS), `/billing/checkout`, and `/billing/portal`. Remaining gaps are P2 (SameSite=Lax defense is real, not theoretical).

---

## 5. Multi-Tenant Isolation / IDOR

### 5a. `get_lead` — correctly scoped on all authenticated paths
`db.py:1322-1332`: `get_lead(lead_id, business_id=None)` — when `business_id` is supplied, the SQL is `WHERE id=? AND business_id=?`.

All authenticated callers pass `biz["id"]`:
- `app.py:1997` `/api/leads/<id>/messages` ✅
- `app.py:1979` `/api/sim/reply` ✅
- `app.py:2095` `/api/appointments/<id>/cancel` (`get_lead(appt["lead_id"], biz["id"])`) ✅
- `app.py:2236` `/api/leads/<id>/flag-spam` ✅

Internal (non-tenant) `get_lead(lead_id)` without `business_id` only on:
- `app.py:707` — demo route (creates lead under demo biz; passes that biz's id to `open_conversation`). Not exploitable for cross-tenant.
- `app.py:1576` — `_dispatcher_lead_owned()`, Twilio-signed route; verifies `lead["business_id"] == biz["id"]` before returning. ✅
- `app.py:1673/1687/1696` — `_ensure_lead_notes()` internal background thread; called only with `lead_id` values originating from the current business's own webhook writes. Not reachable by a tenant. ✅
- `app.py:1968` — `sim_incoming()` (authenticated, `biz["id"]` just passed to `create_lead`). ✅
- `app.py:3113` — voice status callback, Twilio-signed, reads `lead_id` from a JOIN on `voice_calls WHERE biz_id`. ✅

**No IDOR on `get_lead` paths.** ✅

### 5b. `get_call` — scoped
`db.py:1892-1898`: `WHERE id=? AND business_id=?`. All callers (`app.py:2153, 2182, 2218`) pass `biz["id"]`. ✅

### 5c. `cancel_appointment` — scoped
`db.py:2902-2911`: `WHERE id=? AND business_id=? AND status='booked'`. ✅

### 5d. `get_confirm_token` — scoped
`db.py:3185-3191`: `WHERE token_id=? AND business_id=?`. `claim_confirm_token` also scopes by `business_id`. Cross-tenant test in `test_confirm_token.py:140-158`. ✅

### 5e. `get_suggestion` — scoped
`db.py:2357`: `WHERE id=? AND business_id=?`. ✅

### 5f. `resolve_flag` — scoped
`db.py:3294`: includes `business_id` in WHERE. ✅

### 5g. `cancel_growth_play` / `release_growth_batch` — scoped
`db.py:2675,2732`: both take `business_id` and scope their SQL. ✅

### 5h. Operator privilege — `setup/a2p` `mode=record`
`app.py:1475-1476`: checks `_is_operator(current_user())` before allowing `brand_sid/campaign_sid` writes. ✅  
`_is_operator` checks against `config.OPERATOR_EMAILS` (env-var allowlist). No tenant can self-escalate to operator. ✅

**Conclusion: No IDOR or cross-tenant data access paths found. Multi-tenant isolation is solid.**

---

## 6. Server-Bound Confirm Token (SF-6)

`app.py:921-995` — full review:

1. **Client cannot substitute tool/args/recipient**: token lookup is `get_confirm_token(biz["id"], token_id)` → stored `tool` and `args_json` are used; any `tool` or `args` in the POST body are ignored. ✅
2. **Single-use / atomic claim**: `claim_confirm_token` uses `UPDATE ... WHERE consumed=0` — only one concurrent claim wins (`rowcount == 1`). ✅
3. **Expiry checked**: `float(row["expires_at"]) < time.time()` before claim. ✅
4. **Cross-tenant fail-closed**: `get_confirm_token(biz["id"], token_id)` scopes by tenant; a different tenant's token returns `None` → 200 with "couldn't find that confirmation" (not a 4xx leak). ✅
5. **Editable body (text_lead only)**: `app.py:984-986` allows editing only `args["message"]`, capped at 1 600 chars. Recipient (`_lead_id` / phone) stays server-bound. ✅
6. **CSRF on confirm**: `_csrf_ok()` checked at `app.py:929`. ✅
7. **Enforce-mode double-ack gate**: `app.py:965-969` requires `enforce_ack=true` before claiming a `set_screen_mode` → enforce token. ✅

Test coverage in `test_confirm_token.py` covers items 1-4. ✅

---

## 7. Google OAuth `state` Parameter

**Calendar OAuth**:  
`app.py:2051-2052`: `state = secrets.token_urlsafe(16)`, stored in `session["g_state"]`.  
`app.py:2059-2063`: `session.pop("g_state", None)`; verifies `request.args.get("state") == expected`. If mismatch or missing → redirect to `gerror=state`. ✅

**Contacts OAuth**:  
`app.py:2386-2388`: same pattern, `session["gc_state"]`.  
`app.py:2394-2398`: verified on callback, same guard. ✅

Both OAuth flows generate fresh state per request, store in session, and verify on return. No open-redirect or state-bypass paths found.

---

## 8. token_crypto.py

- `_hkdf` derives independent subkeys for enc and MAC. ✅
- `encrypt`/`decrypt`: encrypt-then-MAC with HMAC-SHA256; uses `hmac.compare_digest` to prevent timing oracle. ✅
- Dual-read: legacy plaintext returned unchanged; encrypted blob fails MAC → returns `None` (caller handles reconnect). ✅
- When `TOKEN_ENC_KEY` is empty (dev): `encrypt` is a no-op; `decrypt` returns legacy plaintext. ✅
- Production fail-fast (`config.py:293-298`) enforces a non-empty key. ✅

---

## 9. Findings Summary

| Severity | Finding | File:Line |
|---|---|---|
| **P1** | Login rate-limit bypass via `X-Forwarded-For` spoofing (no `ProxyFix`; attacker rotates synthetic IPs) | `app.py:363` |
| **P1** | `/api/appointments/<id>/cancel` sends real cancellation SMS to a customer but has no CSRF token (form-based; SameSite=Lax is the sole guard) | `app.py:2079` |
| **P1** | `/billing/checkout` and `/billing/portal` (Stripe) have no CSRF token — a forged same-site POST can redirect victim to attacker-controlled Stripe session | `app.py:2906, 2925` |
| **P2** | 13 other login-required POST routes (settings, setup/*, training/*, digest, calendar, contacts, suggestions) lack explicit `_csrf_ok()`. SameSite=Lax mitigates in modern browsers but there is no defense-in-depth for subdomain attacks | `app.py:1031,1050,1060,1126,1292,1413,1432,1461,1496,2072,2407,2421` etc. |
| **P2** | `_csrf_ok()` only reads `request.form`; JSON POST routes have no CSRF header/token path — they rely entirely on `SameSite=Lax` | `app.py:252-256` |
| CLEAN | Multi-tenant isolation: all `get_lead/get_call/get_confirm_token/cancel_appointment/get_suggestion` correctly scope by `business_id`. No IDOR found | `db.py:1322,1892,3185,2902,2357` |
| CLEAN | Session fixation: `session.clear()` on both login and signup | `app.py:396, 346` |
| CLEAN | Cookie flags: `SameSite=Lax`, `Secure` (gated on `FIRSTBACK_HTTPS`), HttpOnly (Flask default) | `app.py:53-54` |
| CLEAN | SECRET_KEY + TOKEN_KEY + SEED_PASSWORD fail-fasts: all three fire as `RuntimeError` at import in production | `config.py:276-298,430-433` |
| CLEAN | Password reset: 256-bit entropy, single-use atomic, 1-hour TTL, not logged | `app.py:425,459; db.py:3522` |
| CLEAN | Server-bound confirm token (SF-6): client cannot forge tool/args/recipient, atomic single-use claim, cross-tenant fail-closed, CSRF-checked | `app.py:921-995; db.py:3185-3205` |
| CLEAN | Google OAuth `state`: generated, stored in session, verified on callback for both Calendar and Contacts | `app.py:2051-2063, 2386-2398` |
| CLEAN | Operator privilege escalation: `setup/a2p mode=record` checks `_is_operator()` against env-var allowlist | `app.py:1475` |

---

## Deploy Verdict

**CONDITIONAL DEPLOY.** No P0 (no auth bypass, no IDOR, no boot crash, no secret leak in git). Two P1 issues should be fixed before charging:

1. **P1-A** (`app.py:363`): Apply `werkzeug.middleware.proxy_fix.ProxyFix(app, x_for=1)` so rate-limiting keys on the real client IP, not a spoofed header.
2. **P1-B** (`app.py:2079, 2906, 2925`): Add `_csrf_ok()` checks to `/api/appointments/<id>/cancel`, `/billing/checkout`, and `/billing/portal`. These are the three routes where a successful CSRF forge triggers an outbound customer SMS or a Stripe session respectively.

The remaining P2 gaps (other settings/setup form POSTs) are adequately covered by `SameSite=Lax` in modern browsers and do not block charging, but should be addressed in the next hardening pass.
