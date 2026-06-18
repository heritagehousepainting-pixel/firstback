# Phase 4 Post-Build Audit 2 — Security + Honesty + Adversarial

**Date:** 2026-06-18  
**Auditor:** Sonnet 4.6 (Lane 2: Security / Honesty / Adversarial)  
**Base:** staging @ bfd6ceb (clean, 46 test files / 1375 tests, 0 failed)

---

## One-line verdicts

**ROI numbers are honest.** Both query branches in `db.analytics` filter `source='missed_call'` (db.py:2542, 2549–2551); revenue is always labeled an estimate; avg_source distinction (owner vs industry_default) surfaces in every consumer (analytics tile, digest, milestone SMS). No cash/actual/collected language found anywhere in Phase-4 code.

**Dispatcher never claims a call it didn't place.** The urgent-path trigger (app.py:1492–1495) records `dispatcher_call_last_at` ONLY when `place_call` returns `status=='placed'`; simulated/error falls through to the existing SMS backstop with no false claim. Both TwiML routes are `@require_twilio_signature`.

---

## Findings

### P1 — Pre-existing "Calling you now" on simulated status (voice consent path, NOT dispatcher)

**File:** `app.py:2328–2329`  
**Issue:** On the *customer-facing* voice-consent callback path (`twilio_sms_inbound` when a customer texts "call me"), the code sends the message "Calling you now." to the customer even when `place_call` returns `status=='simulated'` (no real call was placed). This predates Phase 4 — confirmed via `git show d455618:app.py` which shows identical logic at line 2238 — but is still a live honesty issue.

**Scope:** This is the CUSTOMER voice-consent path, not the dispatcher call. The dispatcher path (Phase 4 new code) correctly gates the timestamp on `'placed'` only.

**Fix:** Change the condition to `if res.get("status") == "placed":` (remove `"simulated"` from the check at app.py:2328) so the customer only receives the "Calling you now." message when a real call was actually placed.

---

### P1 — Pricing FAQ says "Yes" to voice when voice is "not yet available"

**File:** `templates/pricing.html:69`  
**Issue:** The Pro tier feature list correctly marks AI voice callback as "coming soon (beta -- not yet available)." However, the FAQ answer immediately below says: *"Yes. When a customer would rather talk, FirstBack places an AI voice call..."* — an affirmative present-tense claim for a feature that isn't delivered yet. The final clause ("currently in beta and rolling out on Pro and Crew") softens but does not cancel the "Yes."

**Fix:** Change the FAQ answer opener from "Yes." to "Not yet." or "Coming soon." followed by the beta/rollout framing, matching the pricing tier label.

---

### P2 — Dispatcher TwiML has no cross-tenant ownership check

**File:** `app.py:1293–1313` (`dispatcher_twiml`) and `app.py:1316–1330` (`dispatcher_connect_twiml`)  
**Issue:** Both routes accept any integer `lead_id` and serve that lead's last inbound message body (PII: caller's words) to whoever has a valid Twilio signature. A valid Twilio signature proves the request came from a Twilio account, but on a multi-tenant deployment where each tenant shares the same `TWILIO_AUTH_TOKEN` (single account), a call legitimately placed by Twilio for tenant A could be crafted to fetch lead data belonging to tenant B by guessing/enumerating lead IDs.

**Risk level context:** This is low-to-medium in practice — (a) all requests require Twilio to have actually called that URL, meaning Twilio initiated the request; (b) the spec confirms one shared Twilio account (per the Twilio+A2P architecture memo), so the auth token is the same across tenants and there is no cross-account forging path. However, if lead IDs are sequential integers, a motivated insider with Twilio access could enumerate. The connect route also dials the lead's phone number without ownership scoping.

**Fix:** Look up the lead's `business_id` and confirm it matches the business that owns the Twilio number that initiated the call (via `request.form.get("To")` → `db.get_business_by_twilio_number`). If mismatched, return `<Hangup/>` with a 404-style response.

---

## Lane-by-lane results

### 1. ROI Honesty

| Check | Result |
|---|---|
| Both lead query branches have `AND source='missed_call'` | PASS — db.py:2542 (days-limited) and db.py:2549–2551 (all-time) |
| Revenue always labeled estimate, never cash | PASS — "estimated" in all 3 surfaces (analytics.html:65, convos.py:323, roi.py:64) |
| avg_source label shown (owner vs industry_default) | PASS — analytics.html:64–68, digest convos.py:316–318, milestone roi.py:59–63 |
| No "actual"/"collected"/"earned" cash language | PASS — body test suite verifies this (test_roi_milestone: 8 body checks, all green) |
| Trade defaults present with $800 floor | PASS — config.py:425+; floor verified in test_f12_analytics.py "unknown trade" test |
| roi_multiple computed at query time, not stored | PASS — db.py:2575 |

### 2. Milestone Honesty

| Check | Result |
|---|---|
| Only fires when `a2p_ready(biz)` | PASS — roi.py:36 |
| Only fires at roi_multiple >= 2.0 | PASS — roi.py:55 |
| Idempotent via `roi_milestone_sent_at` | PASS — roi.py:40; db.py:2585–2590; column on `businesses` confirmed |
| Column on LEADS not businesses (rate-limit) | PASS — `dispatcher_call_last_at` on leads; `roi_milestone_sent_at` on businesses |
| Rides alert channel (quiet-hours + consent) | PASS — app.py:1559: `alerts.notify_async(biz, "roi_milestone", ...)` |
| Does NOT fire when A2P pending | PASS — probe + test_roi_milestone 21/21 |
| Does NOT double-fire | PASS — `roi_milestone_sent_at` set immediately after fire (app.py:1563) |

### 3. Digest Honesty

| Check | Result |
|---|---|
| Dollar block suppressed when A2P pending | PASS — convos.py:306, confirmed by probe and test_f12_digest 17/17 |
| Honest non-dollar line when pending | PASS — convos.py:328–331 |
| Revenue labeled estimate in approved block | PASS — convos.py:321–324 |

### 4. Dispatcher Call Security

| Check | Result |
|---|---|
| Both TwiML routes `@require_twilio_signature` | PASS — app.py:1294, 1317 |
| Signature validation fails closed (no auth token → reject) | PASS — messaging.py:577 |
| Dials OWNER cell (alert_sms or phone), not caller | PASS — app.py:1485 uses `biz.get("alert_sms") or biz.get("phone")` |
| Connect route dials caller only if owner presses 1 | PASS — app.py:1321–1323 |
| `dispatcher_call_last_at` recorded only on `placed` | PASS — app.py:1492–1495 |
| Simulated/error → no false claim | PASS — test_dispatcher_call 22/22 |
| Rate-limit: per-lead (not per-business) | PASS — column on leads table; checked fresh from DB each request |
| No false "calling you now" in dispatcher path | PASS — dispatcher never emits that string |
| Caller's words from `get_last_inbound_message` (sync), not async summary | PASS — app.py:1300, db.py:2603–2612 |
| Caller PII (words) read under Twilio-sig gate | PASS — both routes signed; **P2 cross-tenant gap noted** |
| Burst inbounds: second call blocked by rate-limit | PASS — `_already_called = lead.get("dispatcher_call_last_at")` read fresh |

### 5. Site Proof Honesty

| Check | Result |
|---|---|
| Jobber/HCP integration pills removed | PASS — not found in landing.html or onboarding.html |
| No invented testimonials | PASS — placeholder section absent (no fabricated quotes found) |
| Voice "coming soon" correctly labeled | PASS — pricing.html:39 ("not yet available") |
| Voice FAQ matches the pricing tier label | **FAIL (P1)** — FAQ says "Yes" while tier says "not yet available" |
| No smart quotes in template changes | PASS — only ASCII apostrophes and quotes used |

### 6. PII / Auth

| Check | Result |
|---|---|
| New TwiML routes require Twilio signature | PASS |
| Dispatcher does not log PII to stderr | PASS — only milestone hook errors are logged (biz id only) |
| Digest route (`/tasks/digest`) has auth gate | PASS — pre-existing, no Phase-4 change |
| Analytics API route scoped to `current_business()` | PASS — app.py:977 pre-existing |
| Dispatcher cross-tenant scope | **GAP (P2)** — no `biz_id` ownership check |

---

## Summary

**Suite:** 1375/1375 tests passed (46 test files), 0 failures.

**P0:** None found. The cardinal Phase-4 honesty traps (ROI over-count, cash claims, double-fire milestone, dispatcher false claim) are all correctly blocked.

**P1 (2):**
1. `app.py:2328` — "Calling you now" sent to customer on `simulated` call (pre-existing, not Phase-4 introduced, but live honesty issue).
2. `templates/pricing.html:69` — Voice FAQ says "Yes" while Pro tier correctly says "not yet available."

**P2 (1):**
3. `app.py:1293,1316` — Dispatcher TwiML routes read lead PII by integer ID with no cross-tenant ownership check (mitigated by shared Twilio account, but enumerable in principle).
