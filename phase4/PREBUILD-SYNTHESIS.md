# Phase 4 — Pre-Build Synthesis ("convert & prove")
**Date:** 2026-06-18 · Synthesized by Opus from 3 parallel Sonnet planners (PREBUILD-1 scope/ops, PREBUILD-2 honesty/risk, PREBUILD-3 data/integration). This is the TIGHTENED plan + gap list — the input to the Phase 4 build loop's spec step (not yet the 3-way build spec). Base: `staging` @ 4dc1dbb (40/40 green).

## EXISTS / PARTIAL / MISSING map (verified in real code)
| Item | State | Anchor / delta |
|---|---|---|
| Production domain | **OWNER-OPS only** | `PUBLIC_BASE_URL` already env-driven (config.py:186). Zero code. |
| Site proof + CTA fixes | **PARTIAL** | fabricated testimonials/events already removed; REMAINING: Jobber/HCP pills (onboarding.html:149, landing.html:99-103), voice "included on Pro" (pricing.html:64), placeholder testimonial (landing.html:113); landing ROI/founder proof section MISSING (needs a CONTENT decision — no invented stats). |
| F12 "calls recovered" | **PARTIAL (proxy)** | `leads WHERE source='missed_call'` already in `db.analytics()` (~db.py:2513); precise call-JOIN deferred. |
| F12 trade job-value defaults | **MISSING** | `db.analytics()` returns `revenue=None`; need `TRADE_JOB_VALUE_DEFAULTS` + per-biz avg + `avg_source`. |
| F12 ROI multiple + headline tile | **MISSING** | payload has no `roi_multiple`; analytics UI has 4 tiles, no "paid for itself Nx" headline. |
| F12 ROI block in weekly digest | **MISSING** | extend `convos.digest_email()` (~convos.py:298). |
| F12 milestone SMS | **MISSING** | no `roi_milestone_sent_at` column (no idempotency → would fire every booking); no `check_roi_milestone()`; not wired to the post-booking path. |
| Show-Up-Prepared briefing | **PARTIAL** | structured lead data exists (`_ensure_lead_notes`, app.py:~1333); booking alert (`alerts.format_message("booking")`, alerts.py:58) only sends name/phone/when → extend it. |
| Dispatcher Call | **MISSING** | `messaging.place_call` exists (messaging.py:209), sentinel-TwiML pattern exists; need a `/twiml/dispatcher` route + trigger on the `urgent=True` path (handle_inbound ~app.py:1433). |
| Weekly digest infra | **EXISTS** | `convos.digest_email()` + `/tasks/digest` fanout + `mail.send_email()` all work; needs ROI block + a Render weekly cron (OWNER-OPS). |

## LOCKED HONESTY RULES (the cardinal Phase-4 trap is fake ROI — bake these into the build spec)
1. **[P0 — LIVE BUG, fix first] `db.analytics()` counts ALL leads, not just missed-call intercepts** (no `source='missed_call'` filter). Every ROI surface built on it overclaims TODAY. The Phase 4 build's FIRST commit fixes this filter; nothing else ships on top until it's correct.
2. **Revenue is PIPELINE, never cash.** "Earned $" = booked-estimate value, not collected money. Every surface uses `~$X` with an explicit three-state label: `ESTIMATE (industry default)` / `ESTIMATE (your avg)` / `ACTUAL`. Never present an assumed default as a measured fact.
3. **Milestone SMS threshold ≥ 2.0×** (not 1.0×) when using a trade default, to absorb default variance (e.g. roofing's high default would fire "45×" on one booking — arithmetically true, misleading). Idempotent via `roi_milestone_sent_at`; rides SF-6 quiet-hours + consent; opt-out honored.
4. **A2P-pending tenants:** the digest ROI block + "calls recovered" must NOT count text-backs that were `simulated`/`blocked` (never reached a customer). Gate the ROI/recovered claim on the tenant being A2P-approved (or count only delivered sends).
5. **Dispatcher Call honesty:** never tell a customer "calling you now" when `place_call` returned `simulated` (no real call). Don't claim "we connected you" if the bridge failed/owner didn't answer.
6. **Site proof:** testimonials/founder/ROI on the marketing site must be REAL + consented (no invented quotes — get sign-off); the landing ROI section is blocked on a content decision, not a code task.
7. **Weekly digest stays the dead-man's-switch:** it's how the owner learns the ticker died — keep that honest signal intact.

## DATA MODEL (locked)
- New `businesses` columns (guarded `if col not in cols`, after the Phase-3 block ~db.py:703): `roi_milestone_sent_at TEXT`, `dispatcher_call_last_at TEXT` (+ optional `job_value_prompt_dismissed_at TEXT`). **No new tables.**
- New config: `PLAN_COST_MONTHLY = 99`; new `TRADE_JOB_VALUE_DEFAULTS` dict (per-trade avg, ~$800 floor).
- **Computation (locked):** `calls_recovered` = `COUNT(leads WHERE source='missed_call')` (delivered/approved only per rule 4); `earned$` = `booked_n × avg_job_value` where `avg_job_value` = the owner's set value (ACTUAL/your-avg) else `TRADE_JOB_VALUE_DEFAULTS[trade]` (industry default); `roi_multiple` = `earned$ / PLAN_COST_MONTHLY`, computed at query time, never stored.

## EXTEND vs BUILD-NEW (locked)
- Weekly digest → **EXTEND** `convos.digest_email()` (prepend ROI block).
- Show-Up-Prepared briefing → **EXTEND** `alerts.format_message("booking")` — a FORMAT extension of the existing booking SMS, NOT a second send (avoid a redundant text).
- Dispatcher Call → **BUILD-NEW** (`/twiml/dispatcher` + connect routes; trigger on the urgent path; `place_call` exists).
- Milestone SMS → **EXTEND** the alert channel with a new `"roi_milestone"` kind + a `check_roi_milestone()` (suggest a new `roi.py`), called from the post-booking success hook.

## GAPS / HOLES to close in the build spec (the planners' flags)
- **Async race (P0-ish):** `_schedule_notes`/`_ensure_lead_notes` computes lead enrichment on a background thread; the booking alert can fire BEFORE it lands → the Show-Up-Prepared briefing silently degrades to name/phone/when with no signal. The builder MUST do a synchronous read-or-fallback (and the spec must say so).
- **Dispatcher "caller's exact words" source:** use `db.get_last_inbound_message(lead_id)` (synchronous, always present) as primary — NOT `leads.summary` (async, may be empty).
- **Landing ROI/founder proof section** is a CONTENT decision (real numbers/quotes), not a code task — flag for the owner; don't let a builder invent stats.
- **Weekly digest cron** is not wired on Render (OWNER-OPS) — the ROI digest block is inert until it is.
- AMD/voicemail + owner-no-answer handling on the Dispatcher Call must be specified (don't leave a dead bridge claiming success).

## CODE vs OWNER-OPS
- **CODE:** the analytics filter fix, trade defaults + ROI multiple + headline tile, ROI digest block, milestone SMS (+ column/idempotency), briefing extension, Dispatcher Call routes+trigger, site CTA fixes (remove Jobber/HCP pills, fix voice/pricing copy).
- **OWNER-OPS:** register the production domain + Render custom domain + DNS CNAME + set `FIRSTBACK_PUBLIC_URL` (and re-point Twilio webhooks LAST, preserving `ringback-gixe` during cutover — the same env also drives SF-4/SF-7, verify those after the swap); the Render weekly digest cron; the real testimonial/founder/ROI content + consent.

## Proposed first-cut 3-way partition (rough; the build-loop audit finalizes)
- **A** — `db.py` + `config.py` + new `roi.py`: analytics filter fix, trade defaults, ROI computation, milestone check + columns. (db.py exclusive.)
- **B** — `convos.py` + `alerts.py` + analytics template: ROI digest block, booking-alert briefing extension, `roi_milestone` alert kind.
- **C** — `app.py` only: Dispatcher Call routes + the urgent-path trigger + the post-booking milestone hook; site/CTA template fixes.

## Recommendation for the build loop
Phase 4 splits cleanly into **(i) F12 ROI/analytics + milestone**, **(ii) the shared retention path (briefing + dispatcher + digest block)**, **(iii) site proof/CTA fixes**, with the production domain as parallel OWNER-OPS. Fix the analytics over-count filter as commit #1 (honesty P0). Then run the standard loop (2-3 auditors → reconciled PHASE4-SPEC → 3 builders → merge/review → post-build audit).
