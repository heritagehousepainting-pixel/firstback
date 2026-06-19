# Phase 6a — Pre-launch HARDENING — BUILD SPEC (reconciled)
**Date:** 2026-06-18 · Base: `staging` @ 9d8ca66 (60/60 green) · Orchestrator: Opus
**Inputs:** `PLAN-HARDENING.md` §4 Tier-1 + the 2-agent prebuild audit (`phase6/audit/6A-PREBUILD-{AUTH,MONEY}.md`, line-number ground-truthed).

## Verdict after the audit wave
The 10-item must-fix ledger collapses: **3 are already implemented** (verified in code), **1 is pure owner-ops**, **2 are owner-ops env settings**, leaving **4 real code fixes**. Built directly (not parallel worktrees): D-1 is an atomic server+template+JS+test change centred on app.py, so file-disjoint fan-out would collide. Honesty gate: a be-audit on the money/auth surface after the build.

### ALREADY DONE (verified in code — no change, add regression tests only)
- **D-3 `set_confirm_result`** — db.py:3188 already has `business_id` param + conditional `AND business_id=?`; call site app.py:981 passes `biz["id"]`. (Plan line 2813 drifted +375.)
- **D-5 `mark_call_engaged`** — db.py:1889 already scoped; both call sites app.py:2150/2179 pass `biz["id"]`. (Plan line 1821 drifted +68.)
- **D-6 Stripe webhook 400** — app.py:2864 `stripe_webhook` already catches `SignatureVerificationError` → 400, other → 500. No change.

### OWNER-OPS (→ SETUP_NEEDED, not code)
- **D-7** Render must set `FIRSTBACK_HTTPS=1` (or `FIRSTBACK_ENV=production`) so the existing SECRET fail-fast fires + Secure cookie turns on.
- **D-2 ops half** set the 6 `STRIPE_PRICE_*` IDs; **D-8 ops half** set `FIRSTBACK_TOKEN_KEY`; cron `FIRSTBACK_TASKS_SECRET`; `FIRSTBACK_DB_PATH=/var/data/...`.

## THE 4 CODE FIXES

### FIX 1 — D-1 CSRF on the mutating API family (ATOMIC: server + template + JS + tests together)
The 4 handlers are `@login_required` only; `_csrf_ok()` (app.py:245, reads `request.form["_csrf"]` vs `session["csrf_token"]`) is not called. `app.js` (which drives all 4 buttons via `apiFetch`) never sends `_csrf`, and `app_shell.html` (the shell app.js runs in) exposes no token. **Must land as ONE commit** — server guard without the JS body would 403 the owner's own dashboard.
- **Server (app.py):** add `if not _csrf_ok(): return jsonify({"error": "bad_csrf"}), 403` right after `biz = current_business()` in `api_engage_screened_call` (2130), `api_rescue_screened_call` (2156), `api_flag_call_spam` (2194), `api_flag_lead_spam` (2209).
- **Template (app_shell.html):** add `<input type="hidden" id="csrfToken" value="{{ csrf_token }}">` before the `app.js` script (line 94). **CORRECTION to the AUTH report:** `command.html` *does* `{% extends "app_shell.html" %}` (line 1) and already has its own `#csrfToken` (line 107) → remove that redundant line 107 to avoid a duplicate ID on the command page (assistant.js still reads the shell's token; same value).
- **JS (app.js):** add `APP_CSRF` const + `csrfBody(extra)` helper after `apiFetch`; wire all 4 fetches (235 leads/flag-spam, 955 engage, 978 real, 999 calls/flag-spam) to `{ method:"POST", body: csrfBody(), headers:{"Content-Type":"application/x-www-form-urlencoded"} }`.
- **Tests (test_screening_ui.py):** existing mutating POSTs must now send `_csrf` (scrape it from a rendered page) or they 403; add missing/bad-`_csrf` → 403 cases for all 4.

### FIX 2 — D-4 MAX_CONTENT_LENGTH (app.py)
After `app.config["SESSION_COOKIE_SECURE"] = SESSION_COOKIE_SECURE` (line 54): `app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024` (Flask → 413 on oversize). CSV import (~500KB for 10k rows) and Stripe events (~10KB) both fit. +413 test.

### FIX 3 — D-2 Stripe loud fallback (billing.py)
`_price_to_plan` (billing.py:52) silently returns `"starter"` for a non-empty `price_id` that isn't in `PRICE_IDS` (missing env) → silent downgrade of a paying Pro/Crew renewal. `sub_meta_plan` is only a partial net. Fix: when `price_id` is non-empty and unmatched, `print(... BILLING WARNING ...)` to stderr (always) + best-effort **async** `mail.send_email(SEED_OWNER_EMAIL, ...)` (daemon thread — SMTP must not block the ≤30s Stripe webhook), THEN still `return "starter"` (safe). No tenant-scoped `alerts` kind exists for operator infra alerts → stderr+email is the right channel. +test (unrecognized price_id → warning + starter grant).

### FIX 4 — D-8 FIRSTBACK_TOKEN_KEY prod fail-fast (config.py)
After `TOKEN_ENC_KEY = os.environ.get("FIRSTBACK_TOKEN_KEY", "").strip()` (line 288): `if _is_prod and not TOKEN_ENC_KEY: raise RuntimeError(...)`. Reuses the existing `_is_prod` signal (FIRSTBACK_HTTPS/ENV). Hard-fail chosen: a plaintext Google refresh token in the Render disk is a P1 leak; an operator launching text-only can set the key to any non-empty string. **Inert in tests** (verified: no test sets FIRSTBACK_ENV=production/HTTPS=1 unconditionally; test_auth_reset + test_compliance_backstop pop both before importing config). +test (prod+no-key → RuntimeError; no-prod → inert).

## TEST PLAN (standalone: `.venv/bin/python test_X.py`)
- test_screening_ui.py: CSRF wiring + 4× missing/bad-`_csrf`→403; (opt) mark_call_engaged cross-tenant no-op.
- test_confirm_token.py: set_confirm_result wrong-`business_id` → no-op (D-3 regression).
- test_billing.py: unrecognized price_id → stderr BILLING WARNING + still grants starter (D-2).
- test_auth_reset.py: TOKEN_ENC_KEY prod fail-fast fires / inert (D-8).
- D-4: a >1MB POST → 413 (in test_screening_ui.py or a small new file).

## HONESTY / GATES
- D-1 server+JS+template+tests are ONE commit (no half-deploy that 403s the owner).
- be-audit (money/auth/PII/consent) after build, before declaring 6a done.
- Nothing here deploys or changes pricing; voice stays "coming soon".
