# Phase 4 — Post-Build Audit Reconciliation (Opus)
**Date:** 2026-06-18 · Two parallel Sonnet auditors (1 = spec/correctness/test-integrity, 2 = security/honesty/adversarial). Suite 46/46 throughout. Reports: `POST-BUILD-AUDIT-1.md` / `-2.md`.

## Headline — no P0s. ROI is honest, the Dispatcher is honest.
- **ROI numbers are honest** (auditor 2): `db.analytics` filters `source='missed_call'` on BOTH branches; revenue is labeled "estimated" everywhere (tile/digest/SMS) with NO actual/collected/cash language; owner-vs-industry `avg_source` shown honestly; milestone gated (a2p-approved, ≥2×, idempotent, never zero/pending).
- **Dispatcher never claims a call it didn't place** (auditor 2): both TwiML routes `@require_twilio_signature`; `dispatcher_call_last_at` recorded only on `status=='placed'`; simulated/error falls silently to the SMS backstop; per-lead rate-limit (Opus's prior fix) confirmed.
- All Phase-4 work-streams IMPLEMENTED-CORRECTLY (auditor 1).

## FIXED now (4 P1s)
1. **`alert_on_roi_milestone` column missing** — `alerts._TOGGLE_COL` maps the new `roi_milestone` kind to this column but the migration never added it (fires today via default-ON, but a Settings toggle write would crash). Added `ALTER TABLE businesses ADD COLUMN alert_on_roi_milestone INTEGER DEFAULT 1`. (db.py)
2. **First-turn bookings skipped the owner alert + milestone** — `open_conversation`'s booking branch mirrored calendar/reminders but not the `booking` alert or the ROI milestone hook (only `handle_inbound` had them), so an AI booking on the opening text-back silently missed the owner's Show-Up-Prepared briefing + the milestone check. Mirrored both into `open_conversation`. (app.py)
3. **"Calling you now" on a non-placed call** (pre-existing voice-callback path): the 'call me' flow promised a call when `place_call` returned `simulated` (no real call). Gated the promise to `status=='placed'` only; added a test that the simulated path never says "Calling you now." (app.py + test_voice.py)
4. **Pricing voice FAQ contradiction** — the FAQ led with "Yes. FirstBack places an AI voice call…" while the tier says "coming soon (beta)". Rewrote the FAQ to lead with "Coming soon." consistent with the tier. (pricing.html)

46/46 test files green after the fixes.

## Deferred (P2, documented)
- **Dispatcher TwiML routes fetch lead by integer id with no cross-tenant ownership check** (auditor 2 P2) — gated by Twilio signature but enumerable in principle. Low practical risk on the single shared Twilio account; revisit when multi-tenant Crew (SF-10) lands and tenants have separate numbers. Noted for the Phase-5 SF-10 work.

## Verdict: Phase 4 ships as-is on staging. Honesty bar met (ROI estimate-only + delivery-gated; dispatcher truthful; no invented proof). 46/46 green.
