# Pre-Deploy Audit — SYNTHESIS + GO/NO-GO
**Date:** 2026-06-19 · Base: `staging` @ 55d2601 · Method: 10 parallel Sonnet auditors (lanes 01–10), Opus reconciliation + independent code verification of every P0/P1.
**Lane reports:** `phase6/audit/PREDEPLOY-01..10-*.md`.

## Verdict: NO-GO until the launch-blockers below are fixed. Then GO.
The product is fundamentally sound — multi-tenant isolation, signature verification on every webhook, idempotency on duplicate event-ids, the confirm-token, the screening safety valve, atomic booking/claim, OAuth encryption, the voice gate, ROI honesty all verified CLEAN. The blockers are a focused set of real defects (one TCPA exposure, two billing mis-grants, a few CSRF/timeout/log gaps, and a render.yaml boot-crash) — all small, all verified in code, all fixed in this pass.

## LAUNCH-BLOCKERS — fixing now (verified against code)
| ID | Sev | Lane | File:line | Issue | Fix |
|----|-----|------|-----------|-------|-----|
| C1 | P0 | Consent | reminders.py:342 | Growth marketing kinds (review_request/quote_followup/etc.) send with `transactional=True` → **quiet-hours EXEMPT**. Owner GO at 11pm → 11pm marketing text. `release_growth_batch` doesn't reset send_at + ~0min delay = immediate send. TCPA. | `_transactional = kind in ("reminder","morning_reminder")` (everything else opts into the quiet-hours backstop) |
| M1 | P1 | Money | db.py stripe_event_seen | A transient processing error writes a `status='error'` row + raises→retry; `stripe_event_seen` matches ANY row → retry returns "already processed" → **grant permanently lost**. | filter `AND status='ok'` (error events re-process on Stripe retry; `mark` is INSERT OR REPLACE) |
| M2 | P1 | Money | billing.py `_plan_from_subscription` + `_on_invoice_paid` | Stale checkout `metadata.plan` overrides the ACTUAL billed price → a Billing-Portal upgrade (Starter→Crew) grants the OLD tier. Customer pays $399, gets 250 convos. | price_id match wins; metadata only as fallback for an unconfigured price |
| A1 | P1 | Auth/Consent | app.py:~2079 `/api/appointments/<id>/cancel` | `@login_required` only, no `_csrf_ok` — and it **sends the customer a cancellation SMS**. CSRF can cancel + text a customer. | add `_csrf_ok()` guard |
| A2 | P1 | Auth | app.py:2906/2925 `/billing/checkout`+`/portal` | No `_csrf_ok` on Stripe session creation (forgeable plan/redirect). | add `_csrf_ok()` guard |
| D1 | P1 | Data | db.py:3535 `consume_password_reset_token` | SELECT-then-UPDATE (TOCTOU): two concurrent submits with one token both succeed. | atomic `UPDATE…SET used=1 WHERE id=? AND used=0` + rowcount==1 (mirror claim_confirm_token) |
| I1 | P1 | Integrations | ai.py:136 `_claude_reply` | `llm.complete("claude")` with no timeout → 600s SDK default; a hung Anthropic call wedges the **inbound-SMS weblhook** worker → Twilio retries. | pass `timeout=30` |
| L1 | P1 | Core loop | db.py cancel_appointment (W7 site) + cancel_lead_pending_reminders:2507 | Both cancel `kind='reminder'` only, NOT `morning_reminder` → on reschedule the orphan blocks `enqueue_morning_reminder` → the rebooked customer gets **no morning-of reminder**. | include `morning_reminder` in both cancel queries |
| P1 | P1 | PII | messaging.py:~176,241 | On Twilio failure the consumer's full E.164 phone is logged to stderr. | log `lead_id`/biz id, not the number |
| G1 | P0 | Config | render.yaml | `FIRSTBACK_HTTPS=1` arms the 6a prod fail-fasts, but `FIRSTBACK_TOKEN_KEY` is absent and `FIRSTBACK_OWNER_PASSWORD` is only a comment → **blueprint deploy boot-crashes**. | add both with `generateValue: true` |
| H1 | P1 | Honesty | templates/onboarding.html | The "Call" tab implies AI voice works; the "beta" caveat is only in a JS string, the CTA still → /signup. Voice is gated/unbuilt. | put the "coming soon/beta" caveat on the visible Call surface/CTA |
| H2 | P1 | Honesty | templates/pricing.html:10,71 | "live within a day" understates the 1–5 day A2P carrier wait. | soften to set the honest expectation |

## DEFERRED — documented, NOT launch-blocking (fast-follow / owner-ops / scale)
- **Auth P1 — login rate-limit trusts X-Forwarded-For verbatim** (app.py:363): an attacker rotating the header evades the per-(email,IP) limit. The password is still required; the limit is defense-in-depth. Fast-follow: key on email + ProxyFix. Not a launch-blocker.
- **Auth P2 — 13 other login-required form routes lack `_csrf_ok`** (settings/setup/training/digest): protected by SameSite=Lax; defense-in-depth sweep is the already-tracked 6a follow-up.
- **Money/Integrations P1 — Stripe `seen`+`mark` not atomic (TOCTOU double-grant)**: only reachable with multiple workers; Render runs a single worker today. Use `INSERT OR IGNORE` as the gate before scaling.
- **Ticker P1 — external cron not in render.yaml**: the in-process ticker (`FIRSTBACK_RUN_TICKER=1`) is the sole scheduler; the atomic claim prevents dup sends and the 6b stale-alert fires on recovery — but a dyno recycle delays sends until the next tick. Wire the external cron → `/tasks/run-due` in the go-live runbook (owner-ops). FIRSTBACK_TASKS_SECRET gates it fail-CLOSED (verified).
- P2s across lanes (kind-exclusion constants, MiniMax timeout, dispatcher window, etc.) — recorded in the lane reports.

## CLEAN (verified, no action)
Multi-tenant/IDOR isolation; webhook signature verification (Twilio + Stripe + internal secret, fail-closed); duplicate-event-id idempotency; grant amounts + annual refill + gauge; cancellation/past_due lifecycle; SF-6 confirm token; session fixation; SECRET/TOKEN fail-fasts (once env set); init_db idempotency + non-destructive migrations; partial UNIQUE indexes; atomic booking/cancel/claim; OAuth encryption at rest; no hardcoded secrets; 404/500 no stack/PII leak; the voice gate (5g) holds on every surface; ROI = estimate not cash; A2P "approved" only via real sync; no fake testimonials/logos on live routes.
