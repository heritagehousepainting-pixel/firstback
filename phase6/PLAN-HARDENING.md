# Phase 6 — Pre-Production Hardening Plan
**Date:** 2026-06-18  
**Branch audited:** `staging` @ 503a2ea (5a–5f + 5h merged; 5g voice DEFERRED)  
**Status:** 58 tests green, NOT deployed  
**Lens:** security / money / PII / consent — final pass before charging real money  

---

## SECTION 1 — BE-AUDIT SCOPE: Money / Auth / PII / Consent Paths

The following surfaces require a final security review pass before any real tenant pays:

### 1A. Billing / Money (billing.py + app.py webhook routes)

| Surface | File:line | What to verify |
|---|---|---|
| Stripe webhook signature | `billing.py:151` — `s.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)` | Raises `SignatureVerificationError` on bad sig; verify the route lets it propagate → 400 (Stripe retries). Confirm `STRIPE_WEBHOOK_SECRET` fail-hard if unset at boot. |
| Idempotency dedup | `billing.py:157` — `db.stripe_event_seen(event_id)` | Verify the events table has a UNIQUE index on `event_id`; verify no window where two concurrent webhooks both pass the seen-check before either marks. |
| `_on_invoice_paid` plan resolution | `billing.py:247–278` | Two parallel plan lookups (price_to_plan + sub_meta_plan); the `sub_meta_plan` wins if present. Verify the final `granted` is always a legitimate tier (not attacker-supplied metadata inflating their grant). |
| `_price_to_plan` fallback | `billing.py:56` — `return "starter"` | Correct safe default; verify it is not reachable when `price_id` is a live price that wasn't in `PRICE_IDS` at boot (env mismatch would silently starter-downgrade paying Pro/Crew users). |
| Dual-billing-source Crew risk | `billing.py:_on_checkout_completed` + `_on_invoice_paid` | Both write `usage_grants`. On first checkout the plan comes from session metadata; on recurring invoice it comes from price lookup. If the price lookup fails (unconfigured PRICE_IDs in prod) it falls back to `starter` — a paying Pro/Crew user would lose conversations every month silently. |
| `STRIPE_WEBHOOK_SECRET` guard | `billing.py:147` — `raise RuntimeError(...)` when unset | Good. Verify the Flask route for `/webhooks/stripe` wraps `handle_webhook` and returns 400 (not 500) on `SignatureVerificationError`. |

### 1B. Auth (app.py auth routes + auth.py)

| Surface | File:line | What to verify |
|---|---|---|
| Login rate limit | `app.py:349–369` | In-memory dict with no TTL eviction thread; memory grows unbounded under a slow credential-stuffing attack (distinct email:IP keys never pruned). Acceptable for launch, but document. |
| Login rate limit bypass | `app.py:354–356` | `X-Forwarded-For` is trusted verbatim; a proxied attacker can rotate IPs in the header. Rate key is per `email:IP` — attacker just changes the IP header. Consider IP-only or pure-email key option. |
| Password reset token | `app.py:417–420` — `secrets.token_urlsafe(32)` | Entropy good (256-bit). TTL = 1 hour. Single-use (burned on `consume_password_reset_token`). Verify the email link does not expose the token in server logs (the `link` string is passed to `mail.send` — confirm no logging of `link`). |
| Session fixation | `app.py:388` — `session.clear()` before setting uid | Correct. |
| Session secret fail-fast | `config.py:276–280` | `RuntimeError` if `FIRSTBACK_SECRET == "dev-insecure-secret-change-me"` and `FIRSTBACK_HTTPS=1` or `FIRSTBACK_ENV=production`. Correct — but if neither env var is set on Render (e.g. operator forgets `FIRSTBACK_ENV=production`), the default key is accepted silently. Verify Render env includes at least one of these. |
| `_safe_next` redirect validation | `auth.py:38–41` | Relative paths only; no `//` prefix allowed. Correct. |
| CSRF token implementation | `app.py:245–249` — `secrets.compare_digest` | Correct. Applied to `/assistant`, `/assistant/stream`, `/assistant/confirm`, `/assistant/learn`. NOT applied to the `/api/calls/*` / `/api/leads/*` mutating family (see Deferred Item D-1 below). |
| Google OAuth state parameter | `app.py:2037` — `session["g_state"] = state` and `app.py:2364` — `session["gc_state"]` | These are CSRF guards for OAuth callbacks. Verify they are checked (not just set) in the callback handlers. |

### 1C. PII Handling

| Surface | File:line | What to verify |
|---|---|---|
| Audit log redaction | `app.py:983` — `add_audit(biz["id"], f"confirm:{tool}", f"token={token_id[:8]} {str(args.get('message') or '')[:100]}")` | No phone, no EIN. Confirmed. |
| SMS retry log | `app.py:twilio_sms_status` | Verify retry-failure alert body contains only lead_id (int), not raw phone. Check `alerts.format_message` for `sms_fail` kind. |
| Transcript storage | `db.add_message` | Verify voice transcript bodies stored as `direction="system"`, prefixed `[VOICE]`, no raw phone numbers in body (voice is DEFERRED but the requirement exists for when Slice E lands). |
| EIN / A2P payload | `messaging.create_a2p_brand` | EIN never logged; only `biz_id` + HTTP status. Confirmed by Phase-3 audit. |
| Error handlers | `app.py:2921–2931` — `@app.errorhandler(404)` and `@app.errorhandler(500)` | Verify 500 does not echo stack traces containing DB values or phone numbers to the client (Flask default in non-DEBUG mode suppresses this; confirm `DEBUG=False` in prod). |
| Google OAuth tokens at rest | `config.py:288` — `TOKEN_ENC_KEY` | Plaintext if `FIRSTBACK_TOKEN_KEY` is not set. Needs to be set in prod Render env before any tenant connects Google. |

### 1D. Consent / A2P / TCPA Surfaces

| Surface | File:line | What to verify |
|---|---|---|
| `send_sms` quiet-hours gate | `messaging.py:77` — `gate=True, transactional=True` | Default is transactional (exempt from quiet hours). Growth/follow-up passes `transactional=False` (fixed in Phase 5e). Verify no new caller site passes the wrong value. |
| Auto-flush after A2P approval | `connections.a2p_sync` → `flush_blocked_sends` | The 6-rule safety gate (freshness 6h, opt-out, quiet-hours, dedupe, cap-50, coherence) is proven by Phase-3 audit. Verify `transactional` flag is correct on flush sends. |
| Growth tray `auto` mode UI lock | `growth.py:400` | `auto` is rejected SERVER-side (not just UI-disabled). The TCPA risk is a silent bulk marketing send without per-send owner approval. Verify the `auto` code path is actually unreachable, not just hidden. |
| Growth tray failed-touch retry gap | `growth_touch_index` | A failed touch holds its dedupe slot and blocks re-queue. Noted in SETUP_NEEDED Phase 5 audit. MUST fix before `growth_on=1` can be safely enabled. |
| STOP / detect_revocation voice_ok | `app.py:2666–2673` | Does NOT clear `voice_ok`. Identified as P0 in 5G-AUDIT-SAFETY (C-2). DEFERRED pending voice build (5g). MUST be fixed before voice goes live (5g Slice B). |
| A2P brand/campaign payload shape | `messaging.create_a2p_*` | Known-wrong for real Twilio Trust Hub API (Phase-3 HC-3 deferred). Not a code security issue; an operational correctness issue — first real submission will 400 without corrections. |

---

## SECTION 2 — DEFERRED ITEMS LEDGER

All known-deferred items from Phases 0–5 with launch verdicts.

### MUST-FIX-BEFORE-LAUNCH (blocking)

| ID | Item | Source | File:line | Why Blocking |
|---|---|---|---|---|
| **D-1** | CSRF gap on `/api/calls/<id>/engage`, `/api/calls/<id>/real`, `/api/calls/<id>/flag-spam`, `/api/leads/<id>/flag-spam` — `@login_required` only, no `_csrf_ok()` | 5C-AUDIT-SECURITY P2-B | `app.py:2128, 2154, 2192, 2207` | The rescue endpoint resets the 7-day graduation clock AND upserts a number as `customer`. A CSRF-driven rescue could suppress graduation indefinitely and contaminate the contact directory. SameSite=Lax is meaningful but not sufficient (older browsers, subdomain confusion). Fix: add `if not _csrf_ok(): return jsonify(error="bad_csrf"), 403` and wire JS `_csrf` for these buttons. |
| **D-2** | `_on_invoice_paid` dual-source plan resolution — unconfigured PRICE_IDs silent fallback to `starter` | Billing analysis | `billing.py:253–271` | A Pro/Crew customer whose Price ID is missing from Render env gets downgraded to 250 conversations/mo silently every renewal. No error thrown. Verify all 6 Price IDs are set before first real subscriber, and add an alert/log on fallback. |
| **D-3** | `set_confirm_result` missing `business_id` in WHERE clause | 5AB-AUDIT-SECURITY Finding 1 | `db.py:2813` | Defense-in-depth gap. Future call paths calling `set_confirm_result` without prior tenant-scoping could write another tenant's token row. Low severity today; fix before production because the fix is 2 lines. |
| **D-4** | No `MAX_CONTENT_LENGTH` set on Flask app | 5AB-AUDIT-SECURITY Finding 2 | `app.py:45–55` | Authenticated owner can POST multi-MB body to `/assistant/confirm` — wastes server resources, stores oversized `args_json`/`result_json`. Cap at 1 MB: `app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024`. |
| **D-5** | `mark_call_engaged` missing `AND business_id=?` in UPDATE | 5C-AUDIT-SECURITY P2-A | `db.py:1821` | Safe today because all callers scope through `get_call(call_id, biz["id"])` first. But future call paths skipping `get_call` could flip another tenant's call. 2-line fix. |
| **D-6** | Stripe webhook route error handling | Phase 1 audit note in SETUP_NEEDED | `app.py:/webhooks/stripe` | Verify `SignatureVerificationError` returns 400 not 500 so Stripe retries correctly. Must be verified before first live payment. |
| **D-7** | `FIRSTBACK_SECRET` / `FIRSTBACK_ENV=production` both potentially unset in Render | SETUP_NEEDED Phase 1 | `config.py:272–280` | If neither `FIRSTBACK_HTTPS=1` nor `FIRSTBACK_ENV=production` is set, the fail-fast does not fire and the dev-insecure default session key can be used in prod. Verify Render env has at least one of these. |
| **D-8** | `FIRSTBACK_TOKEN_KEY` not set = Google OAuth tokens stored plaintext | SETUP_NEEDED + config.py | `config.py:288` | Plaintext refresh tokens in the SQLite file. Anyone who reads the DB file (Render disk snapshot, backup file) gets all tenants' Google OAuth. Set before first tenant connects Google. |
| **D-9** | HC-3 A2P payload shape is wrong for real Twilio Trust Hub API (3 fixes: Brand multi-step chain, Campaign `MessageSamples` format, sole-prop OTP mechanics) | Phase-3 RECONCILE + POST-BUILD-AUDIT-3 DEFERRED | `messaging.create_a2p_*` | First live Heritage dogfood submission will 400 without corrections. Blocking for A2P go-live, not for code-launch. Required before any real A2P submission. |
| **D-10** | HC-1/HC-2: live confirmations that `<slug>.firstback.io` passes TCR and `<slug>@clients.firstback.com` satisfies Authentication+ email rule | Phase-3 SETUP_NEEDED | operational | Cannot be verified in code. Blocking for scaling A2P to paying customers. |

### FIX-SOON (important but not day-1 blockers)

| ID | Item | Source | Severity |
|---|---|---|---|
| **D-11** | `run_stream` / `_tool_loop_stream` missing `elif not _is_named_or_pinned(args): return _say("Which lead?")` guard | 5AB-AUDIT-CONSENT P1 + 5AB-AUDIT-CORRECTNESS P1-A | "text her" with streaming Claude path shows confirm card with wrong lead instead of asking. Not a silent send — owner must still tap — but UX consent flaw. Fix: 3 lines per path. |
| **D-12** | Dispatcher TwiML routes: no cross-tenant `lead_id` ownership check | Phase-4 RECONCILE P2, fixed by 5h dispatcher patch | `app.py:dispatcher TwiML` | Phase-5h shipped `_dispatcher_lead_owned` fix. Verify it is in the HEAD build. |
| **D-13** | `growth_on=1` UI toggle not yet built, but when built it must include per-send approval OR explicit consent disclosure | SETUP_NEEDED Phase 5 + Phase-5 audit | Marketing TCPA exposure if `growth_on` toggle lands without safeguards. |
| **D-14** | Failed growth touch blocks re-queue (dedupe slot held on `failed` status) | SETUP_NEEDED Phase 5 | Must fix before `growth_on=1` goes live. Not a blocking issue until growth auto-send is enabled. |
| **D-15** | `settings.html` unconditional monitor-mode note renders even in enforce/off mode | 5C-AUDIT-CORRECTNESS P1-A | `templates/settings.html:88–90` | Contradictory copy to owner in enforce mode. Fix: wrap in `{% if screen_mode == 'monitor' %}`. |
| **D-16** | `would_screen` counts `screened_contact` toward graduation threshold + alert ("robocallers" claim) | 5C-AUDIT-CORRECTNESS P1-B | `db.py:1891`, `reminders.py:534–535`, `alerts.py:125–128` | Dishonest alert body. Could trigger premature graduation. Fix: use `screened_spam` only. |
| **D-17** | `login_rate_key` uses `X-Forwarded-For` verbatim; attacker can rotate IP header | Phase-6 analysis | `app.py:354–356` | Not a P0; rate limit adds friction. Consider email-only key or capping at 3 distinct IPs. |
| **D-18** | In-memory `_LOGIN_FAILURES` dict never evicted; memory grows unbounded under credential-stuffing | Phase-6 analysis | `app.py:349–369` | Operational risk at scale. Add periodic GC or switch to bounded structure. |
| **D-19** | `vic_morning`/`vic_stall` share `alert_on_lead` toggle with no independent control | 5AB-AUDIT-CONSENT P2 | `alerts.py:47` | Turning off lead alerts also silences morning digest and stall nudges without explanation. Document in Settings copy. |
| **D-20** | `resets_at` JS pill uses static "Back to full power tomorrow." regardless of actual reset time | 5AB-AUDIT-CORRECTNESS P2-C | `static/assistant.js:497–506` | Minor copy accuracy; DST edge case. |

### VOICE DEFERRED — MUST-FIX BEFORE 5G GOES LIVE (not blocking for text-only launch)

| ID | Item | Source | File:line |
|---|---|---|---|
| **V-1** | STOP/`detect_revocation` does NOT clear `voice_ok` — FCC consent violation | 5G-AUDIT-SAFETY P0 C-2 | `app.py:2666–2673` |
| **V-2** | No `voice_ok` re-check before `place_call()` | 5G-AUDIT-SAFETY P0 PG-2 | `app.py:~2712` |
| **V-3** | No spam-score gate before `place_call()` | 5G-AUDIT-SAFETY P0 PG-3 | `app.py:~2712` |
| **V-4** | No 60-min de-dupe before `place_call()` | 5G-AUDIT-SAFETY P0 PG-4 | `app.py:~2712` |
| **V-5** | No monthly voice cost cap check before `place_call()` | 5G-AUDIT-SAFETY P0 CC-2 | `app.py:~2720` |
| **V-6** | No `MachineDetection` / `AsyncAmd` params in `place_call()` | 5G-AUDIT-SAFETY P0 VM-1 | `messaging.py:217–219` |
| **V-7** | `WebSocketDisconnect` handler is `pass` — no recovery SMS | 5G-AUDIT-SAFETY P0 VM-2 | `voice_service.py:177–178` |
| **V-8** | No `voice_calls` table / no monthly spend tracking | 5G-AUDIT-SAFETY P0 CC-1 | `db.py` |
| **V-9** | `tool_complete_stream` hardcodes `CLAUDE_MODEL` (Sonnet), not Haiku | 5G-AUDIT-ARCH P0-1 | `llm.py:251` |
| **V-10** | Double-LLM call if `handle_inbound` called inside stream | 5G-AUDIT-ARCH P0-2 | Slice 4 build constraint |

### ACCEPT-AND-DOCUMENT

| ID | Item | Accepted Risk | Rationale |
|---|---|---|---|
| **A-1** | `due_scheduled_messages` INNER JOIN drops NULL-lead retry rows | Inert: retry rows always have `lead_id` | Phase-2 RECONCILE; changing to LEFT JOIN riskier |
| **A-2** | Dispatcher TwiML cross-tenant check via Twilio signature only | Low: single shared Twilio account; 5h patch adds ownership check | Acceptable until Crew multi-tenant with separate numbers |
| **A-3** | HC-3 A2P sole-prop Starter payload unverified | Deferred to Heritage dogfood | Cannot verify without real submission |
| **A-4** | `microsite.html` missing `/static/og-default.png` | Broken OG preview only; no TCR/branding impact | P2, cosmetic |
| **A-5** | `submit_a2p(<nonexistent id>)` guard unreachable | `db.get_business` returns DEFAULT_BUSINESS | Harmless |
| **A-6** | Rate-counter window edge (`incr_rate` not atomic) | Cost guard, not security; impact negligible | SETUP_NEEDED Phase 5 |
| **A-7** | Render cold-start kills Twilio TwiML timeout on Starter plan | Operational; upgrade voice service to Standard when deploying 5g | 5G-AUDIT-ARCH P2-1 |
| **A-8** | `_tool_loop_stream` `_tool_loop` BETA test regression guard vacuously true | Test quality only; behavior is correct | 5AB-AUDIT-CORRECTNESS P2-B |
| **A-9** | `crowd_poison` surface: CROWD_MIN=2 allows 2 colluding accounts to add crowd signal | P2, no ownership verification | 5C-AUDIT-SECURITY recommendation; low practical risk at launch scale |

---

## SECTION 3 — PRODUCTION RELIABILITY / OBSERVABILITY GAPS

### 3A. The Ticker: Silent Death Risk

The in-process ticker (`reminders.start_ticker()`, `daemon=True` thread) catches all exceptions at the loop level (`except Exception as e: print(...)`) but has no heartbeat beyond `/health/ticker`. If the process crashes and restarts under a process manager that doesn't restart (Render with `crashLoopBackoff`), or if the thread exits silently for a reason the broad `except` doesn't catch (e.g., Python interpreter shutdown), reminders/follow-ups/growth scans stop silently.

**Mitigation already in place:** `/health/ticker` reports stale if >10 min since last tick. **Gap:** there is no alert to the owner when it goes stale — the owner must manually check.

**Required before launch:**
- External cron (`POST /tasks/run-due` every 60s with `X-Tasks-Secret`) is the production path. Verify it is wired in Render. Without it, a Render process restart means zero scheduled sends until the cron picks up.
- Add a `tick_stale` alert kind to `alerts.py` so Dave gets an SMS if the ticker has been stale >15 min and the cron is also down.

### 3B. Database Backup / Durability

- `db.start_backup_daemon()` snapshots to the network disk every 60s. The WAL-heal logic is built and documented.
- **Gap:** there is no off-site backup (Render disk is ephemeral if the persistent volume is not configured correctly). Verify `FIRSTBACK_DB_PATH` points to Render's `/var/data` persistent volume, not `/tmp` or the ephemeral container layer.
- **Gap:** backup failure is silent (the daemon logs but does not alert). A corrupted/missing backup would not be discovered until a recovery attempt.
- **Required:** verify Render persistent disk is mounted and `FIRSTBACK_DB_PATH` is set; add an alert on backup daemon failure.

### 3C. Error Alerting

- `@app.errorhandler(500)` exists (`app.py:2928`) but renders a page — no owner alert or external notification is sent.
- Stripe webhook 500s are returned as JSON (Stripe retries) but not proactively alerted.
- There is no Sentry/Rollbar/equivalent wired. For a production SaaS charging $99/mo, silent server errors will go undetected.
- **Required before launch:** wire at minimum `alerts.notify(platform_biz, "server_error", ...)` or a Render log drain to catch 500 patterns.

### 3D. Rate Limiting at Fleet Scale

- `/assistant` rate limit: in-memory `FIRSTBACK_ASSISTANT_RPM` (default 60/min/tenant). In-memory dict means each Render worker instance has its own counter. With multiple workers, a tenant gets N×60 turns/min (N workers).
- **Acceptable at launch** (single worker, Render Starter). **Document** this and the plan to move to Redis-backed rate limiting when scaling to multiple workers.
- `/tasks/run-due` is gated on `TASKS_SECRET` (`app.py:~2870`). Verify this is set in Render env — if unset, the endpoint is open to any caller with the URL.

### 3E. Logging / Observability

- All logging is `print(..., file=sys.stderr)`. Render captures stderr in its log drain.
- No structured logging (JSON), no log levels, no request-id correlation. Debugging a production incident requires grep.
- **Acceptable at launch.** Document the known gap and the path to structured logging.

### 3F. Secret Rotation Plan

The following secrets have no rotation procedure documented:
- `FIRSTBACK_SECRET` (session key) — rotating invalidates all active sessions
- `STRIPE_WEBHOOK_SECRET` — rotating requires a Stripe dashboard update and a coordination window
- `TWILIO_AUTH_TOKEN` — rotating invalidates all Twilio signature checks for in-flight webhooks
- `FIRSTBACK_TOKEN_KEY` — rotating makes all existing Google OAuth tokens unreadable; affected businesses reconnect

**Required:** document the rotation procedure for each in `SETUP_NEEDED.md` before production.

---

## SECTION 4 — HARDENING CHECKLIST (ordered by risk)

### Tier 1 — Must-complete before first paying customer

- [ ] **D-1** Add `_csrf_ok()` to `/api/calls/<id>/engage`, `/api/calls/<id>/real`, `/api/calls/<id>/flag-spam`, `/api/leads/<id>/flag-spam` AND wire `_csrf` in dashboard JS for those buttons. (`app.py:2128, 2154, 2192, 2207`)
- [ ] **D-7** Verify Render env has `FIRSTBACK_ENV=production` OR `FIRSTBACK_HTTPS=1` so the `SECRET_KEY` fail-fast fires if `FIRSTBACK_SECRET` is missing.
- [ ] **D-2** Set all 6 Stripe Price IDs (`STRIPE_PRICE_{STARTER,PRO,CREW}{,_ANNUAL}`) in Render env; add a log warning when `_price_to_plan` returns the starter fallback on a live price_id that doesn't match.
- [ ] **D-6** Audit the `/webhooks/stripe` Flask route: verify `stripe.error.SignatureVerificationError` is caught and returns 400 (not an unhandled 500); verify `STRIPE_WEBHOOK_SECRET` is set in Render.
- [ ] **D-8** Set `FIRSTBACK_TOKEN_KEY` in Render env before any tenant connects Google Calendar/Contacts.
- [ ] **3A** Wire the external Render cron (`POST /tasks/run-due` every 60s with `X-Tasks-Secret`). Verify `FIRSTBACK_TASKS_SECRET` is set.
- [ ] **3B** Confirm `FIRSTBACK_DB_PATH=/var/data/firstback.db` (Render persistent disk). Verify the persistent disk is attached in Render settings.
- [ ] **D-3** Add `AND business_id=?` to `set_confirm_result` UPDATE and pass `biz["id"]` from the call site in `app.py:959`. (`db.py:2813`)
- [ ] **D-4** Set `app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024` in `app.py` (after the Flask app is created, before first request).
- [ ] **D-5** Add `business_id` parameter to `mark_call_engaged` and add `AND business_id=?` to the UPDATE. (`db.py:1821`)

### Tier 2 — Before first week in production

- [ ] **D-15** Wrap unconditional monitor-mode note in `settings.html:88–90` with `{% if screen_mode == 'monitor' %}`.
- [ ] **D-16** Fix `would_screen` in `db.screening_stats` to count only `screened_spam` verdicts, not `screened_contact`. Update the alert body.
- [ ] **D-11** Add the referential-ambiguity guard to `run_stream()` and `_tool_loop_stream()` — 3 lines each. (`assistant.py:2179–2181, 2229–2233`)
- [ ] **3C** Wire error alerting: at minimum add `alerts.notify` in the 500 error handler for server-side exceptions.
- [ ] **3A** Add a `tick_stale` alert when `/health/ticker` sees >15 min stale and the cron is down.
- [ ] **3F** Document secret rotation procedures for `FIRSTBACK_SECRET`, `STRIPE_WEBHOOK_SECRET`, `TWILIO_AUTH_TOKEN`, `FIRSTBACK_TOKEN_KEY` in `SETUP_NEEDED.md`.

### Tier 3 — Before scaling (multiple workers or >10 paying tenants)

- [ ] **D-17/D-18** Harden login rate limiting: consider email-only key or bounded eviction for `_LOGIN_FAILURES`.
- [ ] Move rate counters to Redis-backed storage (multi-worker safety).
- [ ] Add structured JSON logging with request-id correlation.
- [ ] **D-9** Apply HC-3 corrections to `create_a2p_brand`, `create_a2p_campaign`, `submit_a2p` before any real A2P submission beyond Heritage dogfood.
- [ ] **D-10** Run HC-1/HC-2 live confirmations (slug micro-site passing TCR; catch-all email Authentication+).
- [ ] **A-9** Consider raising `SCREEN_CROWD_MIN` from 2 to 3 to reduce crowd-poisoning risk from colluding accounts.

### Tier 4 — Before voice (5g) goes live (post-text-launch)

- [ ] **V-1** Add `db.set_voice_consent(biz["id"], caller, False)` to all three opt-out paths in `app.py:2663, 2667, 2673`.
- [ ] **V-2** Add `get_consent(voice_ok)==0 → skip + text fallback` before `place_call()`.
- [ ] **V-3** Add `spam_score >= SCREEN_SCORE_HARD → skip + text fallback` before `place_call()`.
- [ ] **V-4** Add `last_voice_call_at` within-60-min guard (None-safe: no-op until Slice C creates `voice_calls` table).
- [ ] **V-5/V-8** Add `voice_calls` table + `voice_spend_this_month` + pre-dial cap check.
- [ ] **V-6** Add `MachineDetection="Enable"`, `AsyncAmd="true"`, `AsyncAmdStatusCallback` to `place_call()` data dict.
- [ ] **V-7** Replace `WebSocketDisconnect: pass` with recovery SMS per call outcome.
- [ ] **V-9** Add `model=None` parameter to `tool_complete_stream`; pass `CLAUDE_MODEL_VOICE` for voice path.

---

## SECTION 5 — BIGGEST UNMITIGATED RISKS (honest assessment)

### Risk 1: Silent cron failure = no reminders/follow-ups/growth sends
**Severity:** High for customer trust / retention  
**Status:** The in-process ticker is a fallback; the cron is the production path. If the cron is not wired before launch, reminders simply don't fire for any tenant on a Render restart. Owner has no alert. Customers no-show. **This is the #1 reliability risk at launch.**

### Risk 2: Dual-billing price-ID silent downgrade
**Severity:** High for revenue / trust  
**Status:** If Stripe Price IDs are missing in prod env at invoice time, the `_price_to_plan` fallback silently grants 250 conversations to a paying Pro/Crew customer. No error log, no alert, no refund path. A month of running before discovery is plausible.

### Risk 3: CSRF gap on screening rescue/engage
**Severity:** Medium  
**Status:** The rescue endpoint resets the 7-day graduation clock and upserts a number as `customer`. An attacker with a crafted page could delay graduation indefinitely if SameSite=Lax fails. Low real-world probability but meaningful consequence. Fix is 1 line per endpoint.

### Risk 4: A2P payload shape (HC-3 deferred)
**Severity:** High for go-live timeline, not a security risk  
**Status:** The first real A2P submission will 400 with the current payload shapes. Heritage dogfood is the correction pass. Blocking for any real text-back delivery to customers.

---

## COUNTS

| Category | Count |
|---|---|
| MUST-FIX-BEFORE-LAUNCH (D-1 through D-10) | **10** |
| FIX-SOON before scaling (D-11 through D-20) | **10** |
| VOICE-DEFERRED P0s (V-1 through V-10) | **10** |
| ACCEPT-AND-DOCUMENT (A-1 through A-9) | **9** |
| **Total deferred items** | **39** |

Of the 10 MUST-FIX items: 3 are 2-line defense-in-depth DB fixes (D-3, D-5, D-9 structural); 5 are operational Render env / configuration items (D-2, D-6, D-7, D-8, 3A/3B); 2 require code changes with corresponding JS wiring (D-1 CSRF, D-4 MAX_CONTENT_LENGTH).

---

**Report:** `/Users/jonathanmorris/apps/firstback/phase6/PLAN-HARDENING.md`  
**Next step:** Run `be-audit` skill against `billing.py` (Stripe webhook + idempotency) and the `/auth/reset` + `/webhooks/stripe` routes as a targeted adversarial code pass, then work through Tier 1 checklist items.
