# Phase 6a Build Audit — 6A-BUILD-AUDIT.md

**Date:** 2026-06-19  
**Auditor:** Claude Code (be-audit lens: security / data-integrity / correctness, read-only)  
**Scope:** Uncommitted working-tree changes for Phase-6a hardening (D-1, D-2, D-4, D-8)  
**Suite baseline:** 60/60 green (HEAD)

---

## Summary Verdict

| Priority | Count |
|----------|-------|
| P0 (ship-blocker) | **1** |
| P1 (fix before launch) | **1** |
| P2 (note / hardening) | **4** |

---

## P0 — Ship-Blocker

### P0-1 `MAX_CONTENT_LENGTH` (1 MB) silently breaks the contact-import route (5 MB limit)

**File:** `app.py:55-58` and `app.py:2327`

The spec (PHASE6A-SPEC.md §FIX 2) states: "CSV import (~500 KB for 10k rows) … fits comfortably." The comment in the diff says the same. But the application already has `_MAX_IMPORT_BYTES = 5 * 1024 * 1024` (5 MB) at `app.py:2327`, and the route at `/api/contacts/import` documents a 5 MB limit to the owner (`"That file is too large (limit 5 MB)"`).

Setting `MAX_CONTENT_LENGTH = 1 * 1024 * 1024` (1 MB) means **any vCard or CSV upload larger than 1 MB is rejected with a 413 before the route handler executes**. Flask/Werkzeug raises `RequestEntityTooLarge` at the WSGI layer for multipart file uploads — the route-level `f.read(_MAX_IMPORT_BYTES + 1)` check is unreachable for files >1 MB. The owner-facing error will be Flask's default 413 page, not the graceful `"That file is too large (limit 5 MB)"` message.

A contractor's Google Contacts or phone export is frequently 1–5 MB. This would silently prevent successful imports for a feature that was working before this commit, with no user-visible explanation tied to the real cause.

**The spec's claim that "CSV import (~500 KB for 10k rows)" fits is an estimate, not a hard constraint — a real contractor vCard export for their full address book will routinely exceed 1 MB.**

**Remediation:** Either raise `MAX_CONTENT_LENGTH` to 6 MB (enough for the 5 MB import + headers), or implement per-route override by handling `RequestEntityTooLarge` with a `@app.errorhandler(413)` that is import-route-aware, or exempt the import route by streaming its upload before the content-length check fires. The simplest correct fix is `MAX_CONTENT_LENGTH = 6 * 1024 * 1024` and lower-bounding the import route's own check to 5 MB.

---

## P1 — Fix Before Launch

### P1-1 `growth/tray/release` and `growth/tray/skip` send bulk customer texts without CSRF protection

**Files:** `app.py:1329`, `app.py:1339`, `templates/growth_tray.html:37`, `templates/growth_tray.html:65`

`/growth/tray/release` batch-releases held SMS plays to real customer numbers. `/growth/tray/skip` cancels individual plays. Neither calls `_csrf_ok()`. The forms in `growth_tray.html` (lines 37 and 65) are plain `<form method="post">` with no CSRF token hidden field.

The stated defense for all owner-action POSTs is `SameSite=Lax`. SameSite=Lax does block "unsafe" method (POST) requests originated by **cross-site navigations** (e.g., `<form>` on attacker's domain that doesn't use top-level navigation). However, SameSite=Lax has browser-version and edge-case exceptions, and the D-1 spec decided the mutating family (engage/rescue/flag-spam) needed explicit CSRF tokens. `/growth/tray/release` is a higher-blast-radius action than any of those four: it sends outbound SMS to potentially many customers simultaneously, which is a TCPA event. A CSRF attack that triggers a batch release would violate the operator's "Dave must approve every send" design principle. This endpoint should receive the same `_csrf_ok()` treatment as the D-1 family.

The `billing/checkout` and `billing/portal` routes (app.py:2895, 2914) are a similar concern but lower severity because they redirect to Stripe's domain — an attacker can force the redirect but cannot complete the payment without the owner's Stripe credentials. Still worth noting.

**Remediation:** Add `if not _csrf_ok(): return redirect("/growth/tray"), 403` to `growth_tray_release` and `growth_tray_skip`, and add `<input type="hidden" name="_csrf" value="{{ csrf_token }}">` to the two forms in `growth_tray.html`. The `csrf_token` is already injected by `inject_globals()` (app.py:184-186) and available in every template.

---

## P2 — Notes (Not Ship-Blockers)

### P2-1 Additional mutating owner-action POST endpoints without `_csrf_ok()` — full enumeration

The following `@login_required` POST endpoints mutate state but rely solely on `SameSite=Lax` (no `_csrf_ok()` call). The risk rating depends on the action, but all are documented for completeness:

| Endpoint | Risk if CSRF succeeds |
|----------|-----------------------|
| `POST /settings` (app.py:1122) | Overwrites business profile, AI instructions, alert prefs, screening settings — high impact |
| `POST /settings/password` (app.py:1218) | Changes owner password — **account takeover** but constrained: requires the CURRENT password in the form |
| `POST /settings/growth_mode` (app.py:1287) | Toggles SMS growth mode |
| `POST /setup/profile` (app.py:1402) | Overwrites business profile |
| `POST /setup/number` (app.py:1421) | Provisions a Twilio number — has its own guards |
| `POST /setup/a2p` (app.py:1450) | Submits A2P registration |
| `POST /setup/forwarding` (app.py:1485) | Sets call-forwarding number |
| `POST /api/calendar/busy` (app.py:2012) | Marks calendar days busy |
| `POST /api/integrations` (app.py:2023) | Toggles integrations |
| `POST /api/calendar/google/disconnect` (app.py:2061) | Disconnects Google Calendar |
| `POST /api/appointments/<id>/cancel` (app.py:2068) | Cancels estimate + texts customer |
| `POST /api/contacts` (app.py:2105) | Adds contact category |
| `POST /api/contacts/delete` (app.py:2122) | Deletes contact |
| `POST /api/suggestions/<id>/accept` (app.py:2250) | Accepts a suggestion into directory |
| `POST /api/suggestions/<id>/dismiss` (app.py:2268) | Dismisses suggestion |
| `POST /api/suggestions/<id>/reopen` (app.py:2279) | Reopens suggestion |
| `POST /api/suggestions/bulk` (app.py:2294) | Bulk suggestion action |
| `POST /api/contacts/import` (app.py:2330) | Imports address book |
| `POST /api/contacts/google/sync` (app.py:2396) | Syncs Google Contacts |
| `POST /api/contacts/google/disconnect` (app.py:2410) | Disconnects Google Contacts |
| `POST /digest/send` (app.py:1056) | Sends digest email to owner |
| `POST /training/teach` (app.py:1027) | Teaches the AI a pattern |
| `POST /training/resolve` (app.py:1046) | Resolves a training flag |

The SameSite=Lax defense is broadly effective for POST-via-form cross-origin attacks, but the `growth/tray/release` entry (see P1-1) is where the risk profile crosses the threshold given it sends customer texts.

### P2-2 `assistant.js` reads `#csrfToken` from `app_shell.html` — but the CSRF token is not initialized on the command page until a user interacts

`assistant.js:187` reads `document.getElementById("csrfToken").value` at module-load time. The token is now in `app_shell.html:95` (from the D-1 change), which is rendered server-side, so the token IS populated at load time. This is correct.

However: the `command.html` removal comment (line 106-108) says "assistant.js reads the same #csrfToken element from the shell." This is correct AFTER this commit. Confirm that the diff removed the duplicate `<input id="csrfToken">` from `command.html` (it did — that element is no longer in the file's rendered section). No ID duplication. No issue. **(Noting for record, not a defect.)**

### P2-3 `billing.py` daemon thread: exception in `mail.send_email` does not propagate — confirmed safe

`mail.send_email` (mail.py:18-50) wraps its SMTP send in a bare `except Exception` at line 47, prints to stderr, returns `{"status": "error"}`. The daemon thread's target is `mail.send_email` directly — if it raises, Python's thread exception handler logs to stderr and the thread dies silently. In practice, `send_email` doesn't raise (it catches internally), so the daemon thread is safe. The `_price_to_plan` caller's response is unblocked. **(Confirmed correct, no issue.)**

### P2-4 D-8 ordering in `config.py` — hides one error when multiple prod vars are missing

If a prod env is missing BOTH `FIRSTBACK_SECRET` (defaulted to `"dev-insecure-secret-change-me"`) AND `FIRSTBACK_TOKEN_KEY`, the SECRET check at line 276 fires first and raises, hiding the TOKEN_KEY failure at line 293. The operator fixes SECRET, redeploys, then learns about the TOKEN_KEY error. This is **acceptable engineering** — fail-fast on the first problem — but an operator seeing only one error at a time during initial launch setup may deploy twice.

The D-8 test correctly covers this: it explicitly provides a valid `FIRSTBACK_SECRET` (so the first guard passes) and leaves `FIRSTBACK_TOKEN_KEY` unset, confirming the second guard fires. **(Not a bug, noting for operator awareness.)**

---

## Fix-by-Fix Verification

### D-1 CSRF on the mutating API family

**(a) Guard placement in all 4 handlers — CORRECT**

- `api_engage_screened_call` (app.py:2140): `if not _csrf_ok(): return jsonify(...), 403` is on the line immediately after `biz = current_business()`, before any DB read/write.
- `api_rescue_screened_call` (app.py:2169): same pattern, before `db.get_call`.
- `api_flag_call_spam` (app.py:2205): same, before `db.get_call`.
- `api_flag_lead_spam` (app.py:2223): same, before `db.get_lead`.

All 4 fire before any state change. No bypass path exists (all 4 are `@login_required` only — no other decorator or path skips them).

**(b) Other mutating owner-action POST endpoints without `_csrf_ok()` — see P2-1 and P1-1 above.**

Endpoints protected by CSRF: `/assistant` (app.py:820), `/assistant/stream` (app.py:856), `/assistant/learn` (app.py:906), `/assistant/confirm` (app.py:925), plus the 4 D-1 additions.

Endpoints protected by signature/secret instead of CSRF: all `webhooks/twilio/*` (Twilio signature), `webhooks/stripe` (Stripe HMAC), `internal/voice/*` (INTERNAL_SECRET header), `tasks/run-due` and `tasks/digest` (TASKS_SECRET header).

The 4 new D-1 endpoints are the target scope; previously documented unprotected mutating endpoints remain as they were before this diff.

**(c) Token rendered on every page that app.js runs on — CORRECT with caveat**

`app_shell.html:95` renders `<input type="hidden" id="csrfToken" value="{{ csrf_token }}">`. All dashboard/owner pages extend `app_shell.html` (analytics, callers, command, dashboard, growth_tray, settings, setup, simulator, training, training_convo — verified). `command.html` no longer double-renders the token (the duplicate at the old line 107 is removed). `app.js` reads `APP_CSRF` from `#csrfToken` at line 35. `assistant.js` also reads `#csrfToken` at line 187 independently.

**NOTE:** `base.html` (templates/base.html:33) and `marketing_base.html` also load `app.js`, but neither injects the `#csrfToken` element. Pages extending these (marketing pages like `/contact`, `/pricing`, etc.) will have `APP_CSRF = ""`. However, none of the 4 protected endpoints are reachable from marketing pages (they're `@login_required`), and marketing page forms don't use `csrfPost()`. This is a non-issue for the D-1 scope.

**(d) `_csrf_ok()` safe with missing `session["csrf_token"]` — CORRECT**

`session.get("csrf_token")` returns `None` when absent. `bool(None)` is `False`. Short-circuit evaluation means `secrets.compare_digest` is never called. Returns `False` (fails closed). No crash, no exception.

---

### D-2 Stripe loud fallback

**(a) Daemon thread exception safety — CONFIRMED SAFE**

`mail.send_email` (mail.py:47-49) catches all exceptions internally and returns a status dict. The thread target never raises. Even if it did, a daemon thread exception is logged by Python's unhandled thread exception hook (`sys.excepthook` is not called; `threading.excepthook` or stderr output) — the Stripe webhook's 30s response window is unaffected.

**(b) Warning does not fire for legitimate starter subscriber — CORRECT**

`_price_to_plan` at billing.py:56-58 first checks all configured PRICE_IDS entries. If a price_id matches `os.environ.get("STRIPE_PRICE_STARTER", "")` (or any other configured plan), it returns early. Only if no match is found does it fall to the `if price_id:` branch (line 64). Empty price_id falls through to `return "starter"` silently (the `if price_id:` block is not entered). Correct.

**(c) No PII/secret leaked — CONFIRMED**

The warning includes `price_id` (a Stripe price object ID — not a secret, it's semi-public infrastructure config) and the error message. No customer name, email, phone, or payment method is included. Correct.

**(d) Daemon thread cannot pile up — CONFIRMED**

The thread is `daemon=True` and completes after one `send_email` call (which has a 20-second SMTP timeout, well within the 30s Stripe window since it runs in a separate thread). No persistent state or loop. Correct.

---

### D-8 config fail-fast (`config.py`)

**(a) `_is_prod` is defined earlier — CORRECT**

`_is_prod` is defined at config.py:272-275 (before the D-8 check at line 293). No `NameError` risk.

**(b) Inert in local dev / tests — CONFIRMED**

The `_import_config({})` subprocess test (test_auth_reset.py, third check) verifies that with no env vars set, `import config` succeeds. The test also pops `FIRSTBACK_HTTPS` and `FIRSTBACK_ENV` before running subprocesses. No test file sets these unconditionally.

**(c) Correct prod boot where key IS set — CONFIRMED**

The `_import_config({"FIRSTBACK_HTTPS": "1", ..., "FIRSTBACK_TOKEN_KEY": "a-real-token-key-456"})` test verifies `returncode == 0`. The check is `if _is_prod and not TOKEN_ENC_KEY`, so a set key bypasses the raise.

**(d) Ordering relative to SECRET fail-fast — NOTE (see P2-4)**

SECRET check is at line 276, TOKEN_KEY check at line 293, OWNER_PW check at line 430. Sequential: if SECRET fails, TOKEN_KEY check is not reached. Not a bug — standard fail-fast behavior.

---

### D-4 MAX_CONTENT_LENGTH

**(a) Contact import conflict — P0 (see P0-1 above)**

The 1 MB cap breaks the existing 5 MB contact-import limit. **This is a ship-blocker.**

**(b) Stripe webhook payload — SAFE**

Stripe webhooks are `<10 KB`. `request.get_data()` at app.py:2881 is called before any form parsing; however, `MAX_CONTENT_LENGTH` is enforced at the WSGI stream level regardless, so in theory a webhook >1 MB would 413. In practice, Stripe events are never close to 1 MB. The existing `_billing.handle_webhook` path is safe.

**(c) Flask returns 413 not 500 — CORRECT**

Flask raises `RequestEntityTooLarge` (a `HTTPException`) when `MAX_CONTENT_LENGTH` is exceeded, which generates a proper 413 response, not a 500. The test at test_screening_ui.py:166-168 confirms this by asserting `status_code == 413`.

---

## Test Quality Sanity-Check

### CSRF tests (test_screening_ui.py section 7)
- Tests POST without `_csrf` and with wrong `_csrf` — both assert 403. Guard is genuinely tested (not bypassed).
- Tests that a rejected flag-spam does NOT mutate state. Good non-mutation check.
- Session is seeded with `"csrf_token": "test_csrf"` at line 49-51 and POSTs send `{"_csrf": "test_csrf"}` — consistent. The real `_csrf_ok()` path is exercised (not mocked).

### D-4 test (test_screening_ui.py:164-168)
- Sends a body of 1 MB + 1 KB. Asserts 413. **This test will pass** — the 413 is correctly generated for a route hit that exceeds the cap. However, this test does NOT cover the regression case: a 2 MB file upload to `/api/contacts/import` that should succeed (and formerly did) but now 413s. There is no regression test for the contact import's 5 MB boundary.

### D-2 tests (test_billing.py, section 15)
- Tests recognized price_id → no warning. Correct.
- Tests unrecognized price_id → BILLING WARNING in stderr + `mail.send_email` called + returns "starter". Uses `redirect_stderr` to capture output and `mock.patch.object`. 0.2s sleep to let daemon thread run. Test is sound.
- Tests empty price_id → quiet. Correct.

### D-8 tests (test_auth_reset.py, section 7)
- Uses subprocess to import config in a clean interpreter. Correctly tests the real top-level guard. Three cases: prod+no-token-key → RuntimeError, prod+token-key → success, non-prod → inert. All three are needed and present. Sound.

---

## Finding Summary Table

| ID | Priority | Location | Issue |
|----|----------|----------|-------|
| P0-1 | **P0** | `app.py:55-58` vs `app.py:2327` | `MAX_CONTENT_LENGTH=1 MB` silently breaks contact-import which supports up to 5 MB — legitimate large file uploads will 413 with no actionable error |
| P1-1 | **P1** | `app.py:1329,1339`, `templates/growth_tray.html:37,65` | `growth/tray/release` (batch customer SMS) and `growth/tray/skip` lack `_csrf_ok()` — same defense gap as the D-1 targets, higher blast radius |
| P2-1 | P2 | `app.py` (many routes) | 23 additional mutating `@login_required` POST endpoints without `_csrf_ok()` — all behind SameSite=Lax; documented for awareness |
| P2-2 | P2 | `templates/command.html:106-108` | Informational — duplicate `#csrfToken` correctly removed; `assistant.js` confirmed reading from shell element |
| P2-3 | P2 | `billing.py:73-79`, `mail.py:47-49` | Daemon thread + mail exception safety confirmed — no issue |
| P2-4 | P2 | `config.py:276-298` | D-8 fail-fast ordering: a missing SECRET hides the TOKEN_KEY error; single-repair-per-deploy behavior, not a bug |

---

## Unprotected Mutating Endpoints (not webhook/internal/task-secret-protected, no `_csrf_ok()`)

The following are auth-gated (session cookie + SameSite=Lax) owner-action POST endpoints that mutate state without an explicit `_csrf_ok()` call. Ordered by impact:

1. **`/growth/tray/release`** (app.py:1329) — **P1: batch-sends customer SMS texts** (TCPA event)
2. **`/growth/tray/skip`** (app.py:1339) — P2: cancels scheduled SMS plays
3. **`/settings`** (app.py:1122) — P2: full business profile + AI instructions overwrite
4. **`/api/appointments/<id>/cancel`** (app.py:2068) — P2: cancels estimate + texts customer
5. **`/billing/checkout`** (app.py:2895) — P2: initiates Stripe subscription redirect
6. **`/billing/portal`** (app.py:2914) — P2: redirects to Stripe billing portal
7. `/settings/password`, `/settings/growth_mode`, `/setup/profile`, `/setup/number`, `/setup/a2p`, `/setup/forwarding`, `/api/calendar/busy`, `/api/integrations`, `/api/calendar/google/disconnect`, `/api/appointments/<id>/cancel`, `/api/contacts`, `/api/contacts/delete`, `/api/suggestions/<id>/accept`, `/api/suggestions/<id>/dismiss`, `/api/suggestions/<id>/reopen`, `/api/suggestions/bulk`, `/api/contacts/import`, `/api/contacts/google/sync`, `/api/contacts/google/disconnect`, `/digest/send`, `/training/teach`, `/training/resolve` — see P2-1 table above.

---

## Decision Gate

**DO NOT SHIP** this commit as-is due to P0-1 (contact import regression from 5 MB → 1 MB cap). Fix P0-1 and P1-1 before declaring 6a complete.
