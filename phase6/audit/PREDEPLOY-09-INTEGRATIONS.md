# PREDEPLOY-09-INTEGRATIONS — External-Boundary Audit
**Auditor:** Lane 9 of 10 · Integrations / External Boundaries
**Scope:** messaging.py · connections.py · google_cal.py · google_contacts.py · google_oauth.py · mail.py · voice_service.py · billing.py · webhook routes in app.py
**Date:** 2026-06-19
**Branch:** staging @ 55d2601

---

## Checklist methodology

For each check, the file:line citation points to the exact code under audit.

---

## 1. Webhook Authenticity

### 1a. Twilio inbound webhooks (voice/SMS/status) — PASS

`require_twilio_signature` (app.py:91–107) wraps every inbound Twilio route:
- `/webhooks/twilio/voice/inbound` (app.py:2620)
- `/webhooks/twilio/voice/dial-status` (app.py:2654)
- `/webhooks/twilio/voice/sentinel-twiml` (app.py:1559)
- `/webhooks/twilio/sms/inbound` (app.py:2670)
- `/webhooks/twilio/sms/status` (app.py:2817)
- `/webhooks/twilio/voice/status` (app.py:3073)
- `/twiml/dispatcher/<lead_id>` (app.py:1587)
- `/twiml/dispatcher/connect/<lead_id>` (app.py:1612)

The decorator reconstructs the public HTTPS URL via `X-Forwarded-Proto` (app.py:99–101), which is the correct fix for the TLS-terminating proxy / ngrok false-rejection problem.

`valid_signature` (messaging.py:574–595) uses `hmac.compare_digest` for constant-time comparison, and **fails closed**: an empty `TWILIO_AUTH_TOKEN` returns `False` (messaging.py:589–590). This is correct — an unconfigured token cannot be computed against and accepted.

### 1b. Stripe webhook — PASS

`stripe_webhook` (app.py:2886–2903) reads the raw request body before any form parsing (`request.get_data()`), then delegates to `billing.handle_webhook`. There, `billing.py:170–175`:
- Raises `RuntimeError` if `STRIPE_WEBHOOK_SECRET` is unset — the route catches this and returns 500 (Stripe retries).
- Calls `stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)` which raises `SignatureVerificationError` on a bad sig — caught as 400, stopping Stripe retries on a forged payload.

**No missing-secret-fail-open path.** Both missing secret (500, retry) and bad sig (400, stop) behave correctly.

### 1c. /internal/voice/* shared secret — PASS WITH NOTE

All three internal voice routes check the secret with `secrets.compare_digest` and fail closed when `INTERNAL_SECRET` is empty:

```python
# app.py:2969-2971
sent = request.headers.get("X-Internal-Secret", "")
if not INTERNAL_SECRET or not secrets.compare_digest(sent, INTERNAL_SECRET):
    return jsonify(error="Forbidden."), 403
```

The `not INTERNAL_SECRET` guard ensures an unset secret blocks ALL requests (403). This is correct. The same pattern is at app.py:3002–3004 and app.py:3037–3039.

**Note (P2):** voice_service.py uses `INTERNAL_SECRET` from config (voice_service.py:47) and sends it in `X-Internal-Secret`. When `INTERNAL_SECRET` is not set on the voice service's environment, it sends an empty string — which app.py will reject with 403 (the `not INTERNAL_SECRET` gate fires). This is the correct behaviour, but means all voice turns silently fail until the operator sets the secret on BOTH processes. This should be documented in the deploy runbook.

---

## 2. Idempotency on Retried Actions

### 2a. Twilio SMS status callbacks — PASS

`twilio_sms_status` (app.py:2818–2855) calls `db.set_message_delivery(msg_sid, msg_status)` — a write that is safe on replay since it upserts by provider SID. On `failed`/`undelivered`, it enqueues a retry via `db.queue_sms_retry`. The retry cap is enforced by `db.count_sms_retries(lead_id, within_minutes=30)` (db.py:1238) which counts existing retry rows in the 30-minute window. A Twilio re-delivery of the same status callback fires `set_message_delivery` again (idempotent upsert) and then re-enters the retry-enqueue path. The `attempt = db.count_sms_retries(...) + 1` means a second identical callback sees `attempt >= 4` and stops. **This is correct and bounded.**

### 2b. Stripe event replay — PASS WITH NOTE

`billing.handle_webhook` (billing.py:163–205) dedupes on Stripe's globally unique `event["id"]` via `db.stripe_event_seen(event_id)` which checks a `stripe_events` table with `event_id TEXT PRIMARY KEY` (db.py:672–678). First call: event is not seen → dispatch → `mark_stripe_event(..., "ok")`. Retry: event is seen → return `"already processed", 200`.

**P1 finding — TOCTOU race on Stripe idempotency:**
The `stripe_event_seen` read (billing.py:181) and the `mark_stripe_event` write (billing.py:204) are **not in the same transaction**. Stripe sends events with rapid-fire delivery when it retries within milliseconds. Two concurrent workers (Render can spin multiple instances) could both pass the `stripe_event_seen` check simultaneously before either marks the event. This would result in a **double-grant on `invoice.paid`** — the only concrete harm, since `_on_invoice_paid` calls `db.add_usage_grant` with a plain `INSERT` (db.py:930–942, no `INSERT OR IGNORE`, no UNIQUE constraint on `(business_id, period_start, source)`). Double-invoking `_on_subscription_changed` or `_on_checkout_completed` is less harmful since both are upserts, but `add_usage_grant` is not.

**Mitigation path:** `INSERT OR IGNORE INTO stripe_events` at the START of the handler (before dispatch) using SQLite's `PRIMARY KEY` constraint as the race-safe gate. Or: add a `UNIQUE(business_id, source)` constraint to `usage_grants` so a duplicate INSERT is silently dropped.

**Current deploy risk:** Single-worker Render config (app.py:3157 comment "SINGLE worker") means this race is unlikely to trigger in production today, but it is not a correctness guarantee. Render can restart with overlap.

### 2c. Booking idempotency across voice retries — PASS

`db.book_appointment` (db.py:1472) uses `INSERT INTO appointments` with a `UNIQUE` index enforced at db.py:472–492 (`uniq_booked_slot`). A duplicate booking attempt for the same `(business_id, day, slot_time)` returns `False` without inserting. `handle_inbound` (app.py:1857) checks this return value. A Twilio retry of a voice turn that already booked will pass through the booking write a second time but the DB constraint stops the double-book. **This is correct.**

### 2d. A2P submission idempotency — PASS

`connections.submit_a2p` (connections.py:284–382) reuses existing SIDs on partial-failure retry (connections.py:328–361): each step checks `biz.get("a2p_brand_sid")` / `biz.get("a2p_messaging_service_sid")` / `biz.get("a2p_campaign_sid")` before creating, and persists the SID the instant it's returned. A retry after a partial failure reuses the already-created Twilio object rather than creating a duplicate. **Correct and explicitly documented.**

---

## 3. Outbound Call/HTTP Timeouts

### 3a. Twilio REST calls (messaging.py) — PASS

All outbound Twilio HTTP calls have explicit timeouts:
- `send_sms` POST: `timeout=20` (messaging.py:172)
- `place_call` POST: `timeout=20` (messaging.py:236)
- `search_numbers` GET: `timeout=20` (messaging.py:261)
- `account_owns_number` GET: `timeout=20` (messaging.py:301)
- `attach_owned_number` GET: `timeout=20`; POST: `timeout=30` (messaging.py:319, 335)
- `fetch_a2p_campaign_status` GET: `timeout=20` (messaging.py:355)
- `provision_number` POST: `timeout=30` (messaging.py:397)
- A2P Trust Hub writes: `timeout=30` (messaging.py:471, 504, 561)

### 3b. Google Calendar/Contacts (google_cal.py, google_contacts.py) — PASS

- Token exchange: `timeout=30` (google_cal.py:75)
- Token refresh: `timeout=30` (google_cal.py:124)
- Calendar read (calendars/primary): `timeout=20` (google_cal.py:85)
- FreeBusy query: `timeout=20` (google_cal.py:169)
- Event create: `timeout=20` (google_cal.py:241)
- Event delete: `timeout=20` (google_cal.py:292)
- Contacts token exchange: `timeout=30` (google_contacts.py:65)
- Contacts token refresh: `timeout=30` (google_contacts.py:99)
- Contacts list: `timeout=30` (google_contacts.py:132)

### 3c. SMTP (mail.py) — PASS

`smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)` (mail.py:41). The `timeout` applies to the TCP connection and each command. Any hang beyond 20s raises an exception, which is caught and logged as `"error"` (mail.py:47–49). **Bounded.**

### 3d. Claude LLM (llm.py) — P1 FINDING

`complete()` for the `"claude"` provider (llm.py:164–191) passes `timeout=None` by default, which means the Anthropic SDK uses its own default timeout (600 seconds as of the current SDK). For the **main SMS reply path** (`ai._claude_reply` → `llm.complete`, ai.py:136), which is called synchronously in a Flask request handler (`handle_inbound`, app.py:1789), a Sonnet call that hangs for 600s would **wedge the WSGI worker for 10 minutes**. The ticker has `timeout=10` (reminders.py:406), but the hot-path webhook reply does not.

**P1:** `ai._claude_reply` calls `_complete("claude", ..., max_tokens=300)` with no `timeout` kwarg. The Anthropic SDK default is 600s. On a hung or slow Anthropic API, the Flask/WSGI worker processing the `/webhooks/twilio/sms/inbound` request is blocked for up to 10 minutes. Twilio will redeliver the webhook (retries at ~3× on non-2xx, plus if the worker holds the request without responding). The app comment at llm.py:166–168 explicitly notes the ticker gets `timeout=10` but "existing request-thread callers are unchanged." This gap should be closed.

**Recommended fix:** Pass `timeout=30` (or a configurable `LLM_SMS_TIMEOUT` constant) to `_complete` in `ai._claude_reply` and `ai._llm_complete`.

### 3e. voice_service.py internal relay — PASS

`_process_turn` HTTP call: `timeout=30` (voice_service.py:124).
`_stream_tokens` httpx stream: `httpx.AsyncClient(timeout=60)` (voice_service.py:159).
`_post_turn_log` httpx call: `timeout=10` (voice_service.py:193).
`_send_recovery_sms` httpx call: `timeout=10` (voice_service.py:224).

Stream END commit: `timeout=30` (voice_service.py:406).

All bounded. The 60s stream timeout is generous but the voice path is async and won't wedge the main WSGI worker.

### 3f. complete_stream_voice (llm.py:300–330) — P2 FINDING

`complete_stream_voice` creates an `anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)` client with no timeout override (llm.py:318). The `/internal/voice/stream` endpoint (app.py:3000) is a synchronous Flask route that streams via a generator. A hung Haiku stream could hold the SSE connection open for 600s. This is less severe than 3d (voice is async at the voice_service side, and the voice_service has its own 60s httpx timeout), but the web app's WSGI worker is still blocked for the duration of the SSE generator. **P2**: set `timeout=httpx.Timeout(60)` on the `complete_stream_voice` Anthropic client.

### 3g. tool_complete / tool_complete_stream (llm.py) — P2 FINDING

`tool_complete` (llm.py:207–208) and `tool_complete_stream` (llm.py:273–274) instantiate `anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)` with no timeout. These are used by the assistant for owner-facing queries (not the hot SMS path), but still run synchronously in a request thread. Lower severity since assistant calls are owner-initiated, not inbound-webhook driven. **P2.**

---

## 4. Graceful Degradation When a Dependency is DOWN or Unconfigured

### 4a. Twilio unconfigured — PASS

`messaging.configured()` (messaging.py:46–50) gates all sends. `send_sms` returns `{"status": "simulated"}` (messaging.py:153–155) when not configured. `place_call` returns `{"status": "simulated"}` (messaging.py:220–221). All callers handle the simulated case without crashing.

### 4b. Google Calendar/Contacts unconfigured or down — PASS

`google_cal.busy_slot_ids` (google_cal.py:152–176) returns `set()` on any error or when not connected — booking falls back to the in-house calendar. `google_cal.create_event_async` (google_cal.py:266–276) is fire-and-forget (daemon thread), so a Google outage never blocks the reply path. `cancel_event` (google_cal.py:279–306) treats 410 as success (idempotent) and returns `False` on errors without raising.

### 4c. SMTP unconfigured or down — PASS

`mail.configured()` (mail.py:13–15) gates sends. `send_email` returns `{"status": "simulated"}` (mail.py:30–31) when not configured, `{"status": "error"}` on send failure. Callers in `alerts.py` handle non-"sent" statuses without crashing.

### 4d. Stripe unconfigured — PASS

`billing._stripe()` (billing.py:84–89) raises `RuntimeError("STRIPE_SECRET_KEY is not configured.")` when the key is absent. The checkout and portal routes are auth-gated and raise this as a 500. The webhook route's `handle_webhook` similarly raises on missing `STRIPE_WEBHOOK_SECRET`. These are acceptable fail-closed paths (no crash in background, routes just error).

### 4e. Anthropic API down — PARTIAL (see P1 in §3d)

`llm.complete` for the `"claude"` provider does not timeout (falls to SDK default 600s). On an Anthropic outage the call hangs instead of failing fast. The demo provider fallback (`return ""`) requires `provider == "demo"`, not a timeout. Fix: explicit `timeout=30` on the SMS path.

---

## 5. Forwarding Sentinel Verification

### 5a. Sentinel is real (confirmed only on inbound CallSid match) — PASS

`twilio_voice_inbound` (app.py:2621–2650) is the **ONLY** place `forwarding_confirmed = True` is set when Twilio is configured (app.py:2635). The match is:

```python
sentinel_sid = biz.get("forwarding_sentinel_sid")
if sentinel_sid and call_sid and call_sid == sentinel_sid:
    db.set_forwarding_confirmed(biz["id"], True)
```

`call_sid` comes from Twilio's `CallSid` form field on a Twilio-signed request. Since the route is decorated with `@require_twilio_signature`, only a genuine Twilio call can set this. The sentinel SID is stored at placement time (connections.py:625 via `db.set_forwarding_sentinel`), and the match is exact string equality.

**Self-attestation check:** `setup_forwarding` (app.py:1519–1553) does NOT call `db.set_forwarding_confirmed(True)` when Twilio is configured, only when it is **not** configured (local dev path, app.py:1532, app.py:1553). This is correct. The comment at app.py:1525–1528 explicitly documents this was a deliberate decision.

### 5b. Sentinel timeout + health re-probe — PASS

`check_forwarding_health` (connections.py:633–689) correctly expires a pending sentinel after `_SENTINEL_TIMEOUT_SECS = 120` seconds and flips `forwarding_confirmed = False`, triggering a `forwarding_lost` alert. Weekly re-probes run on `_PROBE_INTERVAL_DAYS = 7` cadence.

---

## 6. A2P Trust Hub Write API Gate

### 6a. Gate on trust_hub_configured() — PASS

`trust_hub_configured()` (messaging.py:53–58) requires both `configured()` AND `TWILIO_TRUST_PRODUCT_SID`. All three A2P write functions gate on this (messaging.py:437, 492, 524):

```python
if not trust_hub_configured():
    return {"status": "simulated"}
```

With only Twilio account credentials but no Trust Hub product SID, all A2P write calls return simulated. **No accidental real submissions on base credentials.**

### 6b. HC-3 payload-shape gap documented — PASS (documented, not silently broken)

The sole-prop Starter brand path (messaging.py:444–453) has an explicit `# DEFERRED HC-3` comment:
> "confirm sole-prop Starter brand payload + OTP with one real submission"

The code returns `{"status": "simulated"}` when `trust_hub_configured()` is False, so it never fires garbage against the real Trust Hub in dev. The gap is documented at the function level AND at `create_a2p_brand` (messaging.py:434–435). The `submit_a2p` function in connections.py does not document this secondary, but callers will see the simulated result. **Not silently broken.**

---

## 7. Error/Retry Handling: No Leaks or Infinite Loops

### 7a. Twilio retries a status callback 3× — SAFE

Scenario: Twilio calls `/webhooks/twilio/sms/status` for `failed` message, retries 3×.
- Each call: `db.set_message_delivery(msg_sid, msg_status)` — idempotent upsert, safe.
- Each call: `count_sms_retries(lead_id, within_minutes=30)` returns 0 on first → queues attempt=1. Returns 1 on second → queues attempt=2. Returns 2 on third → queues attempt=3. Fourth call → `attempt=4 > 3` → alerts owner, no more retries. **Bounded at 3 retries.**
- However: the `count_sms_retries` window is 30 minutes. If Twilio retries come in within 30 minutes AND the background ticker has already run and sent the first retry, the count correctly reflects the existing rows. **No infinite loop.**

### 7b. Stripe replays an invoice.paid event — SAFE (with TOCTOU caveat per §2b)

On a single-worker deploy, the `stripe_event_seen` check reliably gates at billing.py:181. On multi-worker, the TOCTOU window is small (milliseconds) but nonzero. In the worst case, `add_usage_grant` fires twice for the same invoice, granting double conversations. The `stripe_events` PRIMARY KEY constraint means the second `mark_stripe_event` call (billing.py:204) is an `INSERT OR REPLACE` that succeeds without raising — so the grant is doubled but the system doesn't crash.

### 7c. Google is down at booking time — SAFE

`google_cal.busy_slot_ids` returns `set()` on error (google_cal.py:174–176). The booking proceeds with only the in-house calendar. `create_event_async` fires in a daemon thread (google_cal.py:273) — if Google is down, the thread exits silently with a log line (google_cal.py:247–248). The appointment row is already written to DB; the Google event just doesn't exist. This is the documented graceful-degradation path.

---

## 8. Secondary Findings

### 8a. mail.py — No SMTP_SSL / port-465 path — P2

`mail.py:41` uses `smtplib.SMTP(..., timeout=20)` followed by conditional `starttls()`. If an operator sets `SMTP_HOST` to a port-465 (implicit SSL) server such as smtp.gmail.com:465, this will fail (`SMTP()` cannot negotiate implicit SSL; it needs `smtplib.SMTP_SSL`). The app will log an error but silently degrade to no email alerts. This is not P0 because alerts are advisory and SMTP is optional, but it could silently swallow all owner alert emails if the operator picks port 465. **P2: document this limitation in setup guidance; consider auto-detecting port 465 and using SMTP_SSL.**

### 8b. `_on_invoice_paid` — plan downgrade if Stripe price_id changes — P1 (noted, visible)

`billing._price_to_plan` (billing.py:55–80) logs a loud warning and emails the operator when a price_id doesn't match any configured env var. It falls back to `"starter"`. This is the correct visible-failure path — the operator is notified. The warning is already in the codebase. **Not a silent failure, but worth confirming STRIPE_PRICE_* vars match exactly before flipping to production.**

---

## Summary Table

| Severity | Finding | File:Line | Blocks Deploy? |
|----------|---------|-----------|----------------|
| **P1** | Claude SMS path (`_claude_reply`) has no LLM timeout — Anthropic SDK defaults to 600s; a hung call wedges the WSGI worker processing the inbound SMS webhook for up to 10 minutes, stacking Twilio retries | ai.py:136 / llm.py:174 | **YES — P0-adjacent on a production Anthropic outage** |
| **P1** | Stripe idempotency: `stripe_event_seen` check and dispatch are not in the same SQLite transaction; two concurrent workers (restart overlap) can double-process `invoice.paid`, resulting in a double `add_usage_grant` (no UNIQUE guard on usage_grants) | billing.py:181–204 / db.py:930–942 | No (single-worker mitigates; low probability) |
| **P2** | `complete_stream_voice` and `tool_complete`/`tool_complete_stream` instantiate Anthropic client with no timeout | llm.py:318, 208, 274 | No |
| **P2** | mail.py does not support port-465/implicit-SSL SMTP; an operator misconfiguration silently drops all email alerts | mail.py:41 | No |
| **P2** | INTERNAL_SECRET missing on voice_service causes all voice turns to silently fail (403) — should be in deploy runbook | voice_service.py:47 / app.py:2970 | No (voice is gated) |

---

## Deploy Verdict

**CONDITIONAL DEPLOY — P1 on LLM timeout.**

Everything else is clean:
- All Twilio webhooks are signature-verified, fail-closed on missing token.
- Stripe webhook verifies HMAC, dedupes on event_id.
- /internal/voice/* uses constant-time secret comparison, fails 403 when unset.
- Forwarding sentinel only confirms on a real inbound Twilio CallSid match (no self-attestation when configured).
- A2P Trust Hub write API is gated on trust_hub_configured().
- HC-3 payload gap is documented, not silently broken.
- All Google, Twilio, and SMTP calls have bounded timeouts except the main Claude SMS path.
- Twilio 3× retry scenario is bounded (3-retry cap, idempotent upserts).
- Booking is idempotent (DB UNIQUE constraint).

**P1 LLM timeout (ai.py:136):** A Sonnet API hang wedges the WSGI worker handling `/webhooks/twilio/sms/inbound` for up to 600s. Twilio will retry the webhook because it gets no timely 2xx response. The fix is one line: pass `timeout=30` (or a named constant) to `_complete` in `ai._claude_reply`. This is a 5-minute fix and should be applied before or as part of the deploy.

**P1 Stripe TOCTOU:** Acceptable on a confirmed single-worker deploy. Mitigate by adding `UNIQUE(business_id, source)` to `usage_grants` before scaling to multiple workers.
