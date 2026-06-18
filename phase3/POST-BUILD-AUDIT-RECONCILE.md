# Phase 3 — Post-Build Audit Reconciliation (Opus)
**Date:** 2026-06-18 · Three parallel Sonnet auditors (1 = spec/test-integrity, 2 = security/honesty/PII, 3 = auto-flush/state-machine/WRITE-API-realism). Suite 40/40 green throughout. Reports: `POST-BUILD-AUDIT-1.md` / `-2.md` / `-3.md`.

## Headline: Phase 3's LIVE, testable surface is solid. No P0s.
- **Cardinal honesty rule — PASS** (auditor 2, deep): `a2p_status="approved"` is set in exactly ONE place — `connections.a2p_sync` after polling Twilio. `submit_a2p` + all `create_a2p_*` only ever set "pending"; a Twilio CREATE 200 never flips approved.
- **Trust-Hub gate — PASS:** all 3 write functions gate on `trust_hub_configured()` (not just `configured()`); with base Twilio creds but no `TWILIO_TRUST_PRODUCT_SID`, they return `simulated` with zero HTTP (live-probed).
- **Auto-flush — all 8 safety rules PROVEN** (auditor 3, real-DB probes): freshness/opt-out/quiet-hours(transactional)/dedupe(flushed-before-send)/ordering+cap/conversation-coherence/all-stale/still-blocked-guard.
- **PII — clean:** EIN/address never logged.

## FIXED now (pure logic, testable with mocks)
- **submit_a2p partial-failure idempotency** (auditor 3 P1 #1). Before: SIDs were only written after the campaign step, so a failure mid-chain orphaned an already-created Twilio brand/service → a retry duplicated a $4 brand. Now: each SID is persisted the instant it's created, and a step is SKIPPED if its SID already exists → a retry resumes where it left off, never duplicates. Added a `test_sf8_connections` idempotency case (pre-existing SIDs → no re-create) + a `_clear_a2p_sids()` helper (the old resets used `set_a2p_registration(brand_sid=None)`, a partial-write that can't blank a column). 40/40 green.

## DEFERRED → HC-3 live-confirmation punch-list (gated behind trust_hub; UNVERIFIABLE without a real Twilio submission — do NOT speculatively "fix" against guesses; correct these during the Heritage dogfood)
Auditor 3 found the Trust Hub WRITE payloads are structurally wrong for the real API and would 400 on the first real call. Precise corrections to apply when running the real submission:
- **Brand (`create_a2p_brand`)**: the real US A2P Standard flow is NOT one POST — it's the multi-step Customer Profile → Secondary/Trust Product → A2P Brand chain (≈5-6 API calls). The single-POST shape is a placeholder. `PolicyDocument` should be `PolicySid`; `Email` is required and currently missing; several business-detail fields aren't valid CustomerProfiles params.
- **Campaign (`create_a2p_campaign`)**: `MessageSamples` must be 2+ repeated form params (not one string); `OptInImageUrls` is the wrong field for the opt-in page URL.
- **Sole-prop (Path A)**: the Starter-brand endpoint/payload + OTP differ from Standard (HC-3 in PHASE3-SPEC) — confirm with one real submission.
These were always DEFERRED in PHASE3-SPEC (the code is gated + mock-tested; the live shape needs a real submission). This punch-list just makes the corrections explicit for that pass.

## Minor (P2) — documented, not changed
- Returned error dicts currently embed `str(exception)` (URL only, NOT the body/EIN → no active leak). Hardening suggestion: sanitize to `f"API call failed (HTTP {code})"` if a future dev ever reads `r.text`. Low priority.
- `microsite.html` references `/static/og-default.png` (missing) → broken OG social-preview image only; not a TCR/branding issue.
- `submit_a2p(<nonexistent id>)` "not found" guard is unreachable because `db.get_business` returns a DEFAULT_BUSINESS fallback (pre-existing behavior, harmless).
- A few typographic curly quotes in `privacy.html` BODY TEXT (not Jinja delimiters → render fine).

## Verdict: Phase 3 ships as-is on staging. The automated-A2P WRITE path is correct in structure/gating/honesty; its exact Twilio payloads are owner-ops/live-confirmation work (HC-1/2/3), now with a precise punch-list. Auto-flush — the live Phase-3 value — is proven correct. 40/40 green at the post-fix HEAD.
