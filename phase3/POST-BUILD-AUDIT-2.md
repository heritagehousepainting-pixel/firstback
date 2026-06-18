# Post-Build Audit 2 — Security + Honesty + PII + Adversarial
**Lane:** SECURITY / HONESTY / PII / ADVERSARIAL  
**Auditor:** 2 of 3 (read-only, no repo edits)  
**Date:** 2026-06-18  
**Base:** staging ~45e9445, 40/40 green  

---

## One-line verdicts

**Cardinal honesty rule: PASS.** `submit_a2p` and all three write functions set only `"pending"`. Only `connections.a2p_sync` (via `db.set_a2p_status`) ever sets `"approved"`, and only after a real Twilio poll maps to `"VERIFIED"/"APPROVED"`. Literal comment present at `connections.py:289`.

**Trust-Hub gate: PASS.** All three write functions (`create_a2p_brand`, `create_a2p_messaging_service`, `create_a2p_campaign`) gate on `trust_hub_configured()` — not just `configured()` — as the first line. When `TWILIO_TRUST_PRODUCT_SID` is unset but Twilio creds exist, all three return `{"status": "simulated"}` with zero `requests.post` calls. Verified by live probe at `/tmp/probe_trust_hub.py`.

---

## Suite results

| Test file | Passed | Failed |
|---|---|---|
| test_sf8_write_api.py | 33 | 0 |
| test_sf8_persist.py | 35 | 0 |
| test_sf8_connections.py | 94 | 0 |
| test_sf8_microsite.py | 19 | 0 |
| test_sf8_signup_fork.py | 18 | 0 |
| test_migration.py | 3 | 0 |
| test_config_hub.py | 13 | 0 |
| test_compliance.py | 30 | 0 |
| test_connect_hub.py | 28 | 0 |
| test_setup.py | 147 | 0 |
| test_webhooks.py | 18 | 0 |
| test_demo_public.py | 20 | 0 |
| test_compliance_core.py | 47 | 0 |
| **TOTAL** | **505** | **0** |

---

## Findings

### P0 — NONE

No cardinal honesty violations, no trust-hub bypass, no direct EIN/PII exposure to client.

---

### P1 — NONE

No high-severity findings.

---

### P2 — Low risk / informational

#### P2-A: Error-string EIN leak is theoretical but not exploitable today
**File:** `messaging.py:469–472`, `connections.py:328`  
**Details:** `create_a2p_brand` catches `Exception as e` and returns `{"status":"error","error":str(e)}`. The exception is `requests.HTTPError` raised by `r.raise_for_status()`. Python's `HTTPError.__str__` is `"422 Client Error: Unprocessable Entity for url: https://trusthub.twilio.com/v1/CustomerProfiles"` — it does NOT include the request body (where EIN lives) or the response body. EIN cannot leak via this path under normal `requests` behavior.

However, if Twilio ever echoes submitted fields in a 422 response body and a future developer changes the error handler to include `r.text` in the error string, EIN could land in stderr logs and in the `{"status":"error","error":...}` dict propagated through `submit_a2p`. The `app.py` `setup_a2p` route correctly does NOT expose this dict to the browser (it just redirects to `?err=a2p_submit`), so no current client-facing leak exists.

**Fix (defensive hardening):** In each write function's except block, instead of `str(e)`, use a sanitized message that references only the HTTP status:
```python
# Before
return {"status": "error", "error": str(e)}
# After
status_code = getattr(getattr(e, "response", None), "status_code", None)
return {"status": "error", "error": f"API call failed (HTTP {status_code})"}
```

#### P2-B: Missing `og-default.png` breaks OG preview on microsite
**File:** `templates/microsite.html:10`  
`<meta property="og:image" content="/static/og-default.png">` references a file that does not exist in `/static/`. Link-preview bots (Slack, Twitter, Facebook) will get a 404 for the OG image. Not a TCR issue — TCR reviewers see the rendered page, not the OG meta. Not a branding issue (the file doesn't exist, so it can't accidentally show a FirstBack logo). Low impact.  
**Fix:** Either create a neutral `og-default.png` (contractor silhouette, no FirstBack branding) or remove the `og:image` meta tag entirely.

---

## Lane-by-lane verdicts

### 1. Cardinal honesty rule
**PASS.** Grepped all assignments of `a2p_status`:
- `connections.py:312` — `status="pending"` (simulated path in submit_a2p)
- `connections.py:349–356` — `status="pending"` (real submission path in submit_a2p)
- `connections.py:512` — `db.set_a2p_status(biz["id"], mapped)` inside `a2p_sync` — the ONLY place `"approved"` is written, and only after polling Twilio
- No other `set_a2p_status` or `set_a2p_registration(status=...)` call in any Phase 3 code path exists

### 2. Trust-Hub gate bypass
**PASS.** Live probe confirmed:
- With no Twilio creds at all: simulated ✓
- With Twilio creds BUT `TWILIO_TRUST_PRODUCT_SID` unset: still simulated ✓ (all 3 functions)
- Gate is first-line guard, no code path reaches `requests.post` to Trust Hub or Messaging when gate fails

### 3. PII / secrets
**PASS (with P2-A caveat above).** 
- `create_a2p_brand` logs only `biz_id + HTTP status` (`messaging.py:464`), never EIN or address values. Verified by live probe capturing stderr: output was only `[firstback] create_a2p_brand biz=42 http=200`.
- Error dict's `"error"` field contains `str(requests.HTTPError)` which does not include request body.
- EIN not fetched in `/c/<slug>` route — SQL only selects `name, legal_business_name, business_address, trade, service_area` (`app.py:506–510`).
- No EIN in error dicts returned to browser (redirect, not jsonify).
- Test `test_sf8_write_api.py` asserts EIN not in stderr: 4 assertions, all passing.

### 4. Micro-site honesty
**PASS.**
- `templates/microsite.html`: No "FirstBack", no logo, no favicon link, no stylesheet from the main app.
- Only contractor name/address/services/service-area + SMS opt-in + `/privacy` + `/terms` links.
- No smart/curly quotes (confirmed by grep and by test assertions).
- Unknown slug returns 404 (`app.py:513`). `abort(404)` before any row data is touched.
- DB query is parameterized (`WHERE micro_site_slug=?`) — no cross-tenant data exposure possible.

### 5. EIN fork mis-route
**PASS.**
- `registration_path()` reads `business_type` column (`connections.py:49–54`).
- `_profile_done()` forks correctly: `sole_prop` → no EIN required; `llc`/`unknown` → EIN required (`connections.py:62–66`).
- Signup fork (`app.py:315–316`): `has_ein` checkbox → `"llc"` or `"sole_prop"`.
- `create_a2p_brand` sole_prop path: EIN field explicitly omitted from payload with comment (`messaging.py:437`).
- `create_a2p_brand` LLC path: `"BusinessIdentity": business.get("ein") or ""` — EIN only sent for LLC path.

### 6. Auto-flush abuse / safety
**PASS — all 8 rules verified:**
1. Freshness window: `cutoff = now_utc - timedelta(hours=max_age_hours)`; stale rows skipped (`connections.py:422–425`).
2. Opt-out: `db.is_suppressed(business_id, to)` checked before each send (`connections.py:428–431`).
3. Quiet-hours: inherited via `send_sms(..., transactional=True)` — not bypassed.
4. Dedupe: `db.mark_flushed(row_id)` at line 452 is BEFORE `messaging.send_sms()` at line 455. On send error, `mark_flush_skipped` adds `skip_reason` without resetting `flushed=1`.
5. Order + cap: `get_blocked_sends(business_id, flushed=False, limit=50)` — tested in `test_sf8_persist.py`.
6. Conversation-coherence: checks for `direction in ('in','out')` messages with non-null `provider_sid` and `created_at > blocked_at` (`connections.py:436–444`).
7. All-stale is handled correctly — `skipped` counter increments, no re-send.
8. Still-blocked guard: `if result.get("status") == "blocked"` → logs + `return` immediately (`connections.py:458–464`).

**Replay attack via `/tasks/run-due`:** Protected by `TASKS_SECRET` constant-time comparison (`app.py:2311`). When `TASKS_SECRET` is empty, endpoint is unconditionally 403.

**Re-sync of already-approved tenant re-triggering flush:** Impossible. `a2p_sync` only fires `flush_blocked_sends` when `mapped == "approved" and current != "approved"` (`connections.py:514`). A second sync of an already-approved tenant skips the flush.

**Flush wrapped in try/except:** Confirmed at `connections.py:515–519`. A flush failure logs the error and the sync tick continues returning `mapped`.

### 7. Auth on new routes
**PASS.**
- `/c/<slug>` — public (no `@login_required`), correct for TCR opt-in URL.
- `/api/places/lookup` — `@login_required` decorator present (`app.py:522`).
- `setup_a2p` `mode=auto` — `@login_required` on the route (`app.py:1186`); uses `current_business()` which is tenant-isolated by session.

---

## Summary

505/505 tests pass, 0 failed. No P0 or P1 findings. Two P2 informational items: a theoretical EIN-in-exception-string risk that is not currently exploitable (P2-A), and a missing `og-default.png` file on the microsite (P2-B). The cardinal honesty rule and trust-hub gate are cleanly implemented and correctly enforced.
