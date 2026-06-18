# POST-BUILD-AUDIT-1 — Phase 3 Spec Compliance + Correctness + Test Integrity
**Date:** 2026-06-18  
**Auditor lane:** SPEC-COMPLIANCE + CORRECTNESS + TEST-INTEGRITY  
**Commit audited:** staging @ ~45e9445  
**Suite:** 40/40 PASS (all test_*.py via .venv/bin/python)

---

## Work-Stream Verdicts

### WS-1: Trust Hub WRITE API
**IMPLEMENTED-CORRECTLY**

- `messaging.trust_hub_configured()` at messaging.py:53 — `configured() and bool(TWILIO_TRUST_PRODUCT_SID)`. Matches spec exactly.
- All three write functions (`create_a2p_brand`, `create_a2p_messaging_service`, `create_a2p_campaign`) gate on `trust_hub_configured()`, never just `configured()`. messaging.py:424, 480, 512.
- `CUSTOMER_CARE` hardcoded in campaign payload (messaging.py:525). `IsrId` included only when `TWILIO_A2P_RESELLER_SID` is set (messaging.py:544-545).
- EIN and business_address values never logged — only `biz_id` and HTTP status (messaging.py:463, 494, 550).
- HC-3 DEFERRED comment present (messaging.py:433, 434).
- All six new config vars present in config.py: `TWILIO_TRUST_PRODUCT_SID`, `TWILIO_A2P_RESELLER_SID`, `GOOGLE_PLACES_API_KEY`, `MICRO_SITE_DOMAIN` (="firstback.io"), `CLIENTS_EMAIL_DOMAIN` (="clients.firstback.com"), `FLUSH_MAX_AGE_HOURS` (=6).

**No findings.**

---

### WS-2: EIN Fork (business_type column, registration_path, _profile_done)
**IMPLEMENTED-CORRECTLY**

- `business_type` column added with `DEFAULT 'unknown'` in db.py migration (line 703).
- `connections.registration_path(biz)` at connections.py:40 — returns `sole_prop`/`llc`/`unknown` from `biz.get("business_type")`.
- `connections._profile_done()` at connections.py:57 — sole_prop requires name + business_address only (no EIN); llc/unknown require name + ein + business_address. Matches spec.
- `app.py:315-316`: signup POST reads `has_ein`, calls `db.set_business_type(bid, "llc" if has_ein else "sole_prop")`.

**No findings.**

---

### WS-3: connections.submit_a2p Orchestration
**IMPLEMENTED-CORRECTLY**

- connections.py:284 — full implementation with literal comment `# NEVER set status='approved' here -- only a2p_sync() may, after polling.`
- Simulated path: when `not messaging.trust_hub_configured()`, sets `a2p_status="pending"` + `submitted_at` and returns `{"status": "simulated"}` (connections.py:311-313).
- Path B (llc/unknown): builds slug → `db.set_micro_site` → re-fetches biz → brand → svc → campaign in order; short-circuits + returns `{"status":"error","step":...}` on any failure without calling subsequent steps (connections.py:317-344).
- Path A (sole_prop): skips micro-site, runs same 3-call chain (connections.py:315-344).
- Only ever sets `status="pending"` on success; `a2p_sync` is the only path to `"approved"` (connections.py:349-356).
- `a2p_sync` (connections.py:493) fires `flush_blocked_sends` inside its own try/except on `pending→approved` transition only (connections.py:514-518).

**No findings.**

---

### WS-4: Per-contractor micro-site /c/<slug>, /api/places/lookup
**IMPLEMENTED-CORRECTLY**

- `@app.route("/c/<slug>")` at app.py:499 — looks up `micro_site_slug` in DB, 404 if not found, renders `microsite.html` with only name/address/trade/service_area + opt-in + /privacy + /terms.
- No FirstBack branding in microsite.html (confirmed — checked file).
- No smart/curly quotes in microsite.html (confirmed — python check found NONE).
- `@app.route("/api/places/lookup")` at app.py:521 — `@login_required`, gated on `GOOGLE_PLACES_API_KEY`, returns `{}` when unset or on error.

**No findings.**

---

### WS-5: Auto-flush (blocked_sends + flush_blocked_sends + a2p_sync hook)
**IMPLEMENTED-CORRECTLY**

All 8 safety rules implemented in `connections.flush_blocked_sends` (connections.py:373-477):
1. Freshness window — FLUSH_MAX_AGE_HOURS (default 6), skips with `skip_reason='stale'`. ✓
2. Opt-out — `db.is_suppressed(business_id, to)` pre-send, skips `opted_out`. ✓
3. Quiet-hours — inherited via `send_sms(transactional=True)`. ✓
4. Dedupe — `mark_flushed(row_id)` BEFORE send; `send_error` on failure, never reset to 0. ✓
5. Ordering + cap — `get_blocked_sends(limit=50)` + DB query orders ASC. ✓
6. Conversation-coherence — checks `db.get_messages` for subsequent messages with non-null `provider_sid` and `created_at > blocked_at`. ✓
7. All-stale degenerate case — handled correctly (returns 0 flushed, just skipped). ✓
8. Still-blocked guard — returns `{"status":"blocked"}` from send_sms → logs error + STOP (connections.py:458-464). ✓

`send_sms` blocked branch at messaging.py:141-145 calls BOTH `db.add_message(lead_id, "out", body)` AND `db.queue_blocked_send(biz_id, lead_id, to, body)`. The `queue_blocked_send` is correctly only called when `lead_id is not None` (spec-correct: no phantom rows for owner alert sends).

Real integration probe confirmed: `flush_blocked_sends` with real DB rows and patched `send_sms` correctly marks row `flushed=1` and returns `{"flushed":1, "skipped":0, "errors":0}`.

**No findings.**

---

## Seams Wired (SHARED SEAMS verification)

| Seam | Defined? | Callers wired? |
|---|---|---|
| `config.TWILIO_TRUST_PRODUCT_SID/A2P_RESELLER_SID/GOOGLE_PLACES_API_KEY/MICRO_SITE_DOMAIN/CLIENTS_EMAIL_DOMAIN/FLUSH_MAX_AGE_HOURS` | config.py ✓ | messaging.py imports them ✓ |
| `messaging.trust_hub_configured()` | messaging.py:53 ✓ | connections.submit_a2p ✓, all 3 write fns ✓ |
| `messaging.create_a2p_brand/messaging_service/campaign` | messaging.py ✓ | connections.submit_a2p calls all 3 ✓ |
| `db.queue_blocked_send` | db.py:3010 ✓ | messaging.send_sms:144 ✓ |
| `db.get_blocked_sends/mark_flushed/mark_flush_skipped` | db.py:3032/3045/3057 ✓ | connections.flush_blocked_sends ✓ |
| `db.set_business_type` | db.py:2987 ✓ | app.py:316 ✓ |
| `db.set_micro_site` | db.py:2998 ✓ | connections.submit_a2p:321 ✓ |
| `connections.registration_path` | connections.py:40 ✓ | submit_a2p:315 ✓ |
| `connections.submit_a2p` | connections.py:284 ✓ | app.py:1212 ✓ |
| `connections.flush_blocked_sends` | connections.py:373 ✓ | a2p_sync:516 ✓ |
| `connections.build_slug/build_contact_email` | connections.py:259/276 ✓ | submit_a2p:319/320 ✓ |

All seams are defined AND wired to real callers. No stubs in production code.

---

## TEST INTEGRITY TABLE

| Test File | Real or Hollow? | Assessment |
|---|---|---|
| `test_sf8_write_api.py` | **REAL** | Mocks `requests.post` (necessary to avoid Twilio billing); asserts against actual `messaging.create_a2p_*` functions. All assertions check real function behavior. EIN/address log test captures real stderr. Would fail if functions were deleted or trust_hub gate removed. |
| `test_sf8_persist.py` | **REAL** | Uses a real temp SQLite DB. Tests `send_sms` → `queue_blocked_send` integration with real DB writes. Verifies migration idempotency with two `init_db()` calls. Asserts actual DB row values. Would fail if any function were removed. |
| `test_sf8_connections.py` | **MOSTLY REAL — one hollow caveat** | `flush_blocked_sends` tests use in-memory `_blocked_sends_store` instead of the real DB, because A2 wrote this test assuming A1's DB functions were not yet merged. Now that they ARE merged, the flush tests do not exercise the actual `db.get_blocked_sends` / `db.mark_flushed` / `db.mark_flush_skipped` SQL paths — a real flush bug in those functions would not be caught. `submit_a2p` and `a2p_sync` tests use real DB for business state. See P1 below. |
| `test_sf8_microsite.py` | **REAL** | Uses Flask test client against real app routes. Tests microsite rendering with a real DB row, checks rendered HTML content. Verified that `connections.submit_a2p` is stubbed (not needed for microsite route). Smart quote checks are genuine. |
| `test_sf8_signup_fork.py` | **REAL** | Uses Flask test client + real app POST routes. Stubs `db.set_business_type`, `connections.submit_a2p`, `connections.profile_complete` — which is correct; these are seams across agent boundaries. The stubs also write to the real DB where the column exists, so the test exercises the real Flask/app wiring. |

---

## Findings

### P1 — `test_sf8_connections.py` flush tests use in-memory stubs for real DB functions
**File:** `test_sf8_connections.py` lines 78-119  
**Issue:** `db.get_blocked_sends`, `db.mark_flushed`, `db.mark_flush_skipped` are all replaced with in-memory `_blocked_sends_store` dict stubs. Since these A1 functions are now fully implemented in the real DB, this means the flush safety-rule tests (WS-5's most critical logic) don't exercise the real SQL paths. A bug in `db.get_blocked_sends`'s `ORDER BY` or `WHERE flushed=0` filter, or `mark_flushed`'s `flushed_at` timestamp, would pass undetected.  
**Risk level:** Medium — the real DB functions ARE tested in `test_sf8_persist.py` independently, so the layer separation partially covers this. The integration path (flush_blocked_sends → real db funcs) is not tested in any single test.  
**Fix:** Add an integration variant in `test_sf8_persist.py` or a new `test_sf8_integration.py`: real DB, real `db.*` functions, patched `messaging.send_sms` only. This is exactly what the spec's REVIEW GATE (spec line 122) prescribes.

### P2 — `test_sf8_connections.py` "non-existent biz" test passes for wrong reason
**File:** `test_sf8_connections.py` lines 575-578  
**Issue:** `connections.submit_a2p(99999)` is expected to return `{"status": ..., "error": "business not found"}` but `db.get_business(99999)` returns a synthetic `DEFAULT_BUSINESS` dict with `id=99999` (confirmed by probe). So `submit_a2p` never hits the "not found" guard (connections.py:305-307). The test passes (`isinstance(res_no_biz, dict) and "status" in res_no_biz`) but for the wrong reason — it returns `{"status":"simulated"}` via the trust-hub-off path, not the error path. The "business not found" branch is untested.  
**Fix:** Stub `db.get_business` to return `None` for id=99999, or document that the fallback behavior is intentional.

### P2 — Privacy.html has smart/curly quotes in pre-existing sections (not new SMS section)
**File:** `templates/privacy.html` lines 12, 31, 58  
**Issue:** Three lines in the pre-existing content contain curly quotes (LDQ/RDQ/RSQ). The spec says "Straight ASCII quotes only" in the context of the new SMS section — the new section (lines 41-43) is clean. The pre-existing quotes are not new and were not introduced by Phase 3. The test correctly scopes its check to only the new Text messaging section, so it passes.  
**Risk:** Minimal — pre-existing content; carriers only validate the opt-in landing page (microsite.html), not the privacy page content.  
**Fix (optional):** Clean up the three pre-existing curly quotes in privacy.html for hygiene.

---

## Additional Checks

- **convos.py / llm.py untouched:** CONFIRMED. `convos.py` (15335 bytes, md5=65a959cb60dfb6dff1a98294e10d6aff), `llm.py` (13429 bytes, md5=8e98ff9ec45fbb2d892515e291bc726a). No Phase 3 changes.
- **`ringback-gixe.onrender.com` preserved:** CONFIRMED. The string does not appear hardcoded in any production `.py` file; it appears only in test `os.environ.setdefault` calls and markdown docs (confirmed grep). `FIRSTBACK_PUBLIC_URL` env var controls this at runtime.
- **No smart quotes in microsite.html:** CONFIRMED (python unicode scan found NONE).
- **No smart quotes in new SMS section of privacy.html:** CONFIRMED (test checks only the new section; pre-existing lines have curly quotes but are out of scope).
- **Migration idempotency (fresh + 2x init_db):** CONFIRMED via live probe — all three business columns and `blocked_sends` table + index survive two `init_db()` calls without error.

---

## Summary

**Suite: 40/40 PASS**

| Work-Stream | Verdict |
|---|---|
| WS-1 Trust Hub WRITE API | IMPLEMENTED-CORRECTLY |
| WS-2 EIN fork | IMPLEMENTED-CORRECTLY |
| WS-3 submit_a2p orchestration | IMPLEMENTED-CORRECTLY |
| WS-4 micro-site + places lookup | IMPLEMENTED-CORRECTLY |
| WS-5 auto-flush + blocked_sends | IMPLEMENTED-CORRECTLY |

**P0 findings:** NONE  
**P1 findings:** 1 — `test_sf8_connections` flush tests use in-memory stubs; real DB flush integration path untested (the spec REVIEW GATE was not run)  
**P2 findings:** 2 — non-existent biz "error" path unreachable (db.get_business fallback); pre-existing curly quotes in privacy.html
