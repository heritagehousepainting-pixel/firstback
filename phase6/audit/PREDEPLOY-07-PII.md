# PREDEPLOY-07 — PII / Secrets / Logging / Error-Exposure Audit

**Branch:** staging @ 55d2601  
**Auditor:** Lane 7 (PII/Secrets/Logging/Error-Exposure)  
**Date:** 2026-06-19  
**Verdict:** CONDITIONAL PASS — 0 P0, 2 P1, 3 P2

---

## Scope

Cross-cutting audit across the repo (excluding `.venv/`):
1. PII written to stdout/stderr logs
2. OAuth tokens encrypted at rest + prod fail-fasts
3. Hardcoded secrets in code or git history
4. Error handlers (404/500) — stack trace / DB value / PII to client
5. Audit log redaction
6. Micro-site `/c/<slug>` + `/privacy` field exposure
7. Request body size cap (`MAX_CONTENT_LENGTH`)

---

## P0 Findings — Blocks Deploy

**None.**

All three production fail-fasts fire correctly:
- `config.py:276-280`: `FIRSTBACK_SECRET` default raises `RuntimeError` when `_is_prod`.
- `config.py:293-298`: `FIRSTBACK_TOKEN_KEY` unset raises `RuntimeError` when `_is_prod`.
- `config.py:430-434`: `FIRSTBACK_OWNER_PASSWORD` default raises `RuntimeError` when `_is_prod`.

`_is_prod` triggers on `FIRSTBACK_HTTPS=1` (set in `render.yaml:49`), so all three gates are live on Render.

---

## P1 Findings — Fix Before Wide Release

### P1-A: Consumer phone number written to server logs on Twilio failure

**messaging.py:176** — `send_sms` error handler:
```python
print(f"[firstback] twilio send failed (biz {biz_id} -> {to}): {e}",
      file=sys.stderr, flush=True)
```
`to` is the consumer's E.164 phone number (e.g. `+15551234567`). On any Twilio API/network failure this full number lands in the Render log stream.

**messaging.py:241** — `place_call` error handler:
```python
print(f"[firstback] twilio place_call failed (biz {bid} -> {to}): {e}",
      file=sys.stderr, flush=True)
```
Same issue for outbound voice calls.

**Risk:** Render logs are stored and searchable. Consumer phone numbers in logs can create TCPA/CCPA audit exposure and are unnecessary for debugging (the business id and lead id already identify the event).

**Fix:** Replace `{to}` with `lead_id={lead_id}` (already a parameter on `send_sms`; use `biz_id`/`lead_id` only).

---

### P1-B: `FIRSTBACK_TOKEN_KEY` absent from render.yaml (no reminder to set it)

**render.yaml** — `FIRSTBACK_TOKEN_KEY` is **not listed** anywhere in the blueprint (not even as a commented placeholder).

Since `FIRSTBACK_HTTPS=1` is present, `_is_prod` is True and the fail-fast at `config.py:293-298` will prevent startup if `FIRSTBACK_TOKEN_KEY` is not set. The server will crash at boot. This is **the correct protection** — but there is no reminder in `render.yaml` that the operator must set this secret in the Render dashboard before first deploy.

The same applies to `FIRSTBACK_OWNER_PASSWORD` (commented out on line 79 as "optional" — it's actually required in prod once a real owner password is needed).

**Risk:** A new-operator deploy will fail to start with no obvious reason. Risk is operator confusion, not a live PII leak (the fail-fast does its job).

**Fix:** Add commented entries with `sync: false` in render.yaml under the secrets block:
```yaml
# REQUIRED secrets — set in Render dashboard (never commit values):
#   FIRSTBACK_TOKEN_KEY       — encrypts Google OAuth refresh tokens at rest
#   FIRSTBACK_OWNER_PASSWORD  — owner login; required in prod, set a strong value
```

---

## P2 Findings — Should Fix Before Scale

### P2-A: `str(exc)` returned in Stripe billing route JSON responses

**app.py:2903, 2922, 2937, 2939** — `/webhooks/stripe`, `/billing/checkout`, `/billing/portal` catch-alls return `jsonify(error=str(exc))`.

Stripe SDK exceptions include the HTTP response body in `str(exc)` which can contain Stripe API detail strings (e.g. "No such customer: cus_XXXX"). This is visible to:
- Stripe's webhook delivery system (2903 / 500 response)
- Authenticated owner users (2922, 2937, 2939)

No consumer PII in Stripe exceptions, but internal Stripe customer IDs could be disclosed. Authenticated-only routes reduce the attack surface significantly. **Not a deploy blocker** but should be tightened.

**Fix:** Log `exc` to stderr and return a generic `"Billing error. Please try again."` to the client.

### P2-B: `mail.py:48` — owner email address in SMTP failure log

```python
print(f"[firstback] smtp send failed (-> {to}): {e}", file=sys.stderr, flush=True)
```
`to` here is the owner's own email address (not a consumer's). The risk is low (not consumer PII) but email addresses in logs are unnecessary. **Not a blocker.**

**Fix:** Replace `{to}` with `{to[:3]}***` or omit the address entirely.

### P2-C: `render.yaml` missing `FIRSTBACK_TOKEN_KEY` entry (documentation gap)

Already described in P1-B above. Secondary note: `FIRSTBACK_OWNER_PASSWORD` is listed as `# optional` but the prod fail-fast makes it effectively required if deploying with `FIRSTBACK_HTTPS=1`. The comment should say "required in prod."

---

## What Was Verified Clean

| Check | Finding |
|---|---|
| **OAuth token encryption** | `token_crypto.py` implements HKDF-SHA256 + HMAC-SHA256 AES-stream (enc:v1: marker). `db.py:set_google_tokens` and `set_oauth_tokens` both encrypt before write (lines 1843-1844, 1810-1811). Dual-read supports legacy plaintext rows. Prod fail-fast blocks plaintext-token prod run. **CLEAN.** |
| **Hardcoded secrets in code** | Grep for `sk_live`, `sk_test`, `whsec_`, `AC[a-z0-9]{32}`, `api_key=`. No hardcoded live keys found. `test_billing.py:19-20` uses `sk_test_fake` and `whsec_fake` (test-only, never run in prod). `config.py` dev defaults (`dev-insecure-secret-change-me`) are correctly labeled and fail-fasted. **CLEAN.** |
| **`.env` committed to git** | `.env` exists locally but is NOT git-tracked (confirmed via `git ls-files`; `.gitignore` lists `.env`). No `.env` appears in git history. **CLEAN.** |
| **404/500 error handlers** | `app.py:3131-3143` — both handlers return only `"Not found."` / `"Internal server error."` generic strings. No stack traces, no DB values. `500.html` and `404.html` templates contain no dynamic content. `DEBUG` is `False` by default (requires `FIRSTBACK_DEBUG=1`). **CLEAN.** |
| **Audit log (add_audit)** | `db.py:3148-3154` — `add_audit` stores action name + detail capped at 500 chars. The two call sites (`app.py:917, 991`) log `action:pattern[:80]` (non-PII pattern text) and `token={token_id[:8]}` (truncated opaque token + message body). No raw phone, EIN, or OAuth token. **CLEAN.** |
| **Micro-site `/c/<slug>`** | `app.py:534-547` — SQL selects only `name, legal_business_name, business_address, trade, service_area`. No phone, EIN, email, or user data. Rendered to `microsite.html`. **CLEAN.** |
| **MAX_CONTENT_LENGTH** | `app.py:61` — `6 * 1024 * 1024` (6 MB). Werkzeug enforces 413 on oversize. Tested in `test_screening_ui.py`. **CLEAN.** |
| **EIN in logs** | `messaging.py:416, 465, 475` — explicit SECURITY comments; only `biz_id + HTTP status` logged, EIN value never written. **CLEAN.** |
| **Voice transcript / turn_log** | `app.py:3051-3062` (`/internal/voice/turn_log`) — phone regex redaction applied to caller and AI text before DB write. `voice_service.py` accumulates turns in memory only, not logged. **CLEAN.** |
| **Owner-alert phone passthrough** | `alerts.py:74-75` — phone in alert messages (`New lead: ... +15551234567`) is the consumer's number passed to the **owner's** SMS/email. This is intentional (the owner needs the lead's phone). Not a leak to an attacker. **CLEAN.** |
| **Google OAuth token logging** | `google_cal.py:134` logs `google token refresh failed (biz {business_id})` — business ID only, no token value. `google_contacts.py:113` same pattern. **CLEAN.** |
| **Session cookie security** | `app.py:53-54` — SameSite=Lax (CSRF mitigation) + Secure gated on `FIRSTBACK_HTTPS`. **CLEAN.** |

---

## Summary

| Severity | Count | Items |
|---|---|---|
| **P0** | **0** | — |
| **P1** | **2** | messaging.py:176,241 (consumer phone in error log); render.yaml TOKEN_KEY missing |
| **P2** | **3** | app.py:2903/2922/2937/2939 (str(exc) in billing routes); mail.py:48 (owner email in log); render.yaml docs gap |

**Deploy verdict: CONDITIONAL PASS.** No P0 blockers. P1-A (consumer phone numbers in Twilio error logs) should be patched before wide customer rollout but is a low-frequency log event (only fires when Twilio is down). P1-B (render.yaml TOKEN_KEY reminder) is a documentation gap that the prod fail-fast already protects — the server will refuse to start rather than leak. P2s are quality improvements.
