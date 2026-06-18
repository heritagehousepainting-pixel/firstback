# Post-Build Audit 1 — Phase 4 (SPEC-COMPLIANCE + CORRECTNESS + TEST-INTEGRITY)
**Auditor:** Opus (Lane 1) · **Date:** 2026-06-18 · **Base:** staging ~bfd6ceb  
**Suite:** 46/46 PASS

---

## Work-Stream Verdicts

| Work-Stream | Verdict | Notes |
|---|---|---|
| F12 Analytics honesty (`source='missed_call'` filter) | **IMPLEMENTED-CORRECTLY** | Both `days` and `days=None` branches filter. E2E probe confirms manual leads excluded. |
| Revenue resolution (owner avg → trade default → $800 floor) | **IMPLEMENTED-CORRECTLY** | db.py:2530-2536; config.py:425-447; $800 floor confirmed via probe. |
| `roi_multiple` math + None-when-zero | **IMPLEMENTED-CORRECTLY** | `round(revenue / PLAN_COST_MONTHLY, 1)` @ db.py:2575; None when revenue==0. |
| `avg_source` key propagation | **IMPLEMENTED-CORRECTLY** | Returned in analytics dict; digest + milestone use it; distinctions honest. |
| Milestone idempotency column (`roi_milestone_sent_at`) | **IMPLEMENTED-CORRECTLY** | Migration @ db.py:719-720; `set_roi_milestone_sent` @ db.py:2585; `check_roi_milestone` gates on it. |
| A2P gate on milestone + digest | **IMPLEMENTED-CORRECTLY** | `compliance.a2p_ready(biz)` checked in both `roi.check_roi_milestone` (roi.py:36) and `convos._roi_block` (convos.py:306). |
| Post-booking hook → milestone | **IMPLEMENTED-CORRECTLY** | app.py:1552-1567; fires `notify_async("roi_milestone")` then `set_roi_milestone_sent`; wrapped in try/except so exception can't crash `handle_inbound`. |
| Show-Up-Prepared (format extension, NOT 2nd send) | **IMPLEMENTED-CORRECTLY** | alerts.py:59-68; enriched ctx passed at app.py:1542-1550 and 1500-1509; starts with "Estimate booked:". |
| Dispatcher Call TwiML routes | **IMPLEMENTED-CORRECTLY** | `/twiml/dispatcher/<id>` + `/connect/<id>`; both `@require_twilio_signature`; uses `db.get_last_inbound_message` (synchronous); falls back to "an urgent message" when empty. |
| Dispatcher rate-limit | **IMPLEMENTED-CORRECTLY** | Guards on `lead.dispatcher_call_last_at` @ app.py:1486-1495; only records on `status=="placed"`. |
| Dispatcher no-false-claim | **IMPLEMENTED-CORRECTLY** | Only calls `set_dispatcher_call_at` when `_call_result.get("status") == "placed"`. |
| ROI digest block | **IMPLEMENTED-CORRECTLY** | `convos._roi_block` @ convos.py:299-331; revenue labeled estimate; non-dollar text when a2p pending. |
| analytics.html ROI tile | **IMPLEMENTED-CORRECTLY** | JS tile @ analytics.html:41-70; hidden when `roi_multiple` null; avg_source label honest. |
| Site CTA/proof fixes | **IMPLEMENTED-CORRECTLY** | Jobber/HCP pills removed from onboarding.html (now: "Google Calendar · Your existing number"). Landing.html: Jobber/HCP gone, testimonial replaced with Jinja comment. Pricing.html: voice is "beta, rolling out on Pro and Crew" (accurate). |
| `ringback-gixe` preservation | **IMPLEMENTED-CORRECTLY** | URL used in all test env vars; no rename in config or app. |
| `convos.py` edited directly (not `trades_core/sync.py`) | **IMPLEMENTED-CORRECTLY** | `_roi_block` lives in `/apps/firstback/convos.py:299`; trades_core untouched. |
| Smart quotes in SMS templates | **IMPLEMENTED-CORRECTLY** | roi.py SMS body uses ASCII `--`. alerts.py em-dashes are email-subject-only (not SMS body); acceptable. |

---

## Findings

### P1 — `alert_on_roi_milestone` column not migrated into `businesses` table
**File:** `db.py` (init_db migration block, ~line 719)  
**Finding:** `alerts._TOGGLE_COL["roi_milestone"] = "alert_on_roi_milestone"` (alerts.py:40) references a column that is never added in `db.init_db`. Confirmed via probe: `ALTER TABLE businesses SET alert_on_roi_milestone=1` raises `no such column`. Effect today: the toggle defaults to `True` (correct for new tenants — `_enabled_for` returns `True` when the key is absent), so **the milestone alert always fires correctly**. However:
1. If a Settings UI is wired to let the owner turn off the roi_milestone alert, the `UPDATE businesses SET alert_on_roi_milestone=0` will raise an unhandled sqlite3 error.
2. Existing rows can never persistently disable it.  
**Fix:** Add to the migration block (alongside the existing `roi_milestone_sent_at` guard):
```python
if "alert_on_roi_milestone" not in biz_cols:
    c.execute("ALTER TABLE businesses ADD COLUMN alert_on_roi_milestone INTEGER")
```

### P1 — `open_conversation` first-turn booking path missing booking alert + milestone hook
**File:** `app.py:1446-1464`  
**Finding:** `open_conversation` has a booking path (a first-turn booking when the AI proposes a slot immediately). It does NOT fire `alerts.notify_async(biz, "booking", ...)` with Show-Up-Prepared fields, and does NOT run the milestone hook. `handle_inbound` (the main conversation path) correctly does both at app.py:1542-1563. In production, first-turn bookings via `open_conversation` will be silently missing the owner booking alert and the milestone check.  
**Context:** The spec cites "app.py:1464/1477 success branch" (both in `handle_inbound`). The open_conversation path pre-dates Phase 4 but was already extended in F04 for GCal + reminders. The same extension was not applied for Phase 4 alerts/milestone.  
**Fix:** After `db.book_appointment` succeeds in `open_conversation` (app.py:1447), add the booking alert context build + `alerts.notify_async(biz, "booking", _book_ctx)` + milestone hook, mirroring handle_inbound:1542-1563.

### P2 — `test_f12_digest.py` stubs `db.analytics` entirely (hollow for db integration)
**File:** `test_f12_digest.py:83-90`  
**Finding:** The digest ROI block test monkeypatches `db.analytics` to return a fixed dict, so it cannot catch a regression in `db.analytics` shape (e.g. a missing `roi_multiple` or `avg_source` key). The `test_f12_analytics.py` tests `db.analytics` in isolation. Together they're acceptable, but the digest test would pass even if `convos._roi_block` accidentally used the wrong key name.  
**Severity:** P2 — not a correctness bug today; the real `db.analytics` is proven by `test_f12_analytics.py`.

### P2 — `test_dispatcher_call.py` stubs `db.get_last_inbound_message` and `db.set_dispatcher_call_at`
**File:** `test_dispatcher_call.py:55-69`  
**Finding:** These are the Agent A seams. The stubs are patched onto the `db` module object **before** `app` is imported, so the TwiML route receives the stub correctly (verified by probe: real words appear in TwiML XML). This is the right pattern. However, the suite does **not** contain an integration test that exercises the full path with a **real** DB row for `get_last_inbound_message` — it relies entirely on the stub. Real function is verified by a separate manual probe but not an automated test.  
**Note:** Opus previously found + fixed a per-lead (not per-business) rate-limit bug; confirmed the fix is in place at app.py:1486-1495 (`lead.get("dispatcher_call_last_at")` not `biz.get(...)`).

### P2 — Em-dashes in `alerts._subject()` email subjects
**File:** `alerts.py:87-93`  
**Finding:** `_subject("roi_milestone")` returns `"FirstBack paid for itself — FirstBack"` with a Unicode em-dash. The spec says "no smart quotes in templates" (targeting .html), not Python strings. Email subjects with em-dashes render correctly in most clients but can appear garbled in some SMS-to-email gateways. Not a spec violation; P2 style concern only.

---

## Test-Integrity Table

| Test File | Real vs Hollow | Verdict |
|---|---|---|
| `test_f12_analytics.py` | Real DB + real `db.analytics`; exercises both days/None branches; checks all new keys | **REAL** |
| `test_roi_milestone.py` | Real DB + real `roi.check_roi_milestone` + real `compliance.a2p_ready`; all 4 gates tested | **REAL** |
| `test_f12_digest.py` | `db.analytics` stubbed; `convos.digest_email` real; ROI block + honest language tested | **ACCEPTABLE** (stub doesn't hollow the digest logic itself) |
| `test_briefing.py` | `alerts.format_message` pure; no stubs needed; covers basic/full/partial/empty briefing | **REAL** |
| `test_dispatcher_call.py` | `db.get_last_inbound_message` + `db.set_dispatcher_call_at` stubbed; TwiML routes real; place_call stubbed; rate-limit, simulated, error all tested | **ACCEPTABLE** (seam stubs are necessary; TwiML logic and app.py wiring are real) |
| `test_f12_milestone_hook.py` | `roi.check_roi_milestone` stubbed (controllable); `db.set_roi_milestone_sent` stubbed; `alerts.notify_async` captured; `db.book_appointment` failure tested; exception safety tested | **ACCEPTABLE** (right level for wiring test; roi itself tested in test_roi_milestone.py) |

---

## E2E Verification (Probes Run)

1. **Analytics honesty filter:** Real DB with 1 missed_call + 1 manual lead → `totals.leads == 1`. ✓
2. **Milestone e2e:** Real DB, approved biz, avg_job_value=5000, 1 booking → `check_roi_milestone` returns dict with multiple≈50x; after `set_roi_milestone_sent` → returns None. ✓
3. **Dispatcher TwiML stub propagation:** Patched `db.get_last_inbound_message` propagates into `app.dispatcher_twiml`; real caller words appear in XML output. ✓
4. **Column migration:** `roi_milestone_sent_at` ✓, `dispatcher_call_last_at` ✓, `alert_on_roi_milestone` ✗ (missing — P1).

---

## Summary

Suite: **46/46 PASS**. All Phase 4 features are implemented and wired end-to-end. Two P1 issues require fixes before go-live: the missing `alert_on_roi_milestone` DB column (benign today, will break a Settings toggle), and the `open_conversation` first-turn booking path which silently skips the owner booking alert and milestone hook.
