# Phase 4 — Build Spec ("convert & prove")
**Date:** 2026-06-18 · **Base:** staging @ 5bd0e2b (clean, 40/40 green) · Reconciled by Opus from phase4/PREBUILD-1/2/3 + SYNTHESIS, against verified code anchors.
Build agents: honor every **[DECIDED]** and the SHARED SEAMS table. Tests standalone: `.venv/bin/python test_X.py` (NEVER pytest). convos.py/llm.py NOT a Phase-4 concern except `convos.digest_email` (firstback-local, safe to edit; do NOT run trades_core/sync.py). Preserve `ringback-gixe.onrender.com`.

## Scope
F12 ROI/analytics (honest) + milestone SMS · the shared retention path (Show-Up-Prepared briefing + Dispatcher Call + ROI digest block) · marketing-site CTA/proof fixes. Production domain = OWNER-OPS only (no code).

## GROUND TRUTH (verified — don't re-derive)
- `db.analytics(business_id, days)` (db.py:2500) ALREADY estimates `revenue = booked_n × avg_job_value` (owner-set `avg_job_value` column exists, `set_avg_job_value` db.py:2481) and returns `{totals:{leads,booked,conversion,revenue}, series, avg_job_value, days}`. **BUG (honesty P0):** `lead_rows` (db.py:2513/2520) selects ALL leads with NO `source='missed_call'` filter → "leads"/"recovered"/conversion are inflated by manually-added leads.
- `leads.source` defaults `'missed_call'` (db.py:225); `create_lead(..., source="missed_call")` (db.py:1163).
- `convos.digest_email(business, days=7)` (convos.py:298) builds the weekly digest body — extension point for the ROI block.
- `alerts.format_message(kind, context)` (alerts.py:47, "pure + unit-tested"); booking copy is `"Estimate booked: {who} for {when}."` — extension point for the briefing. Booking alerts fired at app.py:1443 + 1477 with context `{lead_id, name, when}`.
- `handle_inbound` (app.py:1427): urgency at 1433-1436 (`ai.detect_urgency` → `alerts.notify_async(biz,"urgent",...)`); booking at 1443/1464/1477; returns `(reply, booked, urgent)`.
- `messaging.place_call(business, to, twiml_url, status_callback=None)` (messaging.py:209) exists; gated/simulated like send_sms. `db.last_inbound_call` exists (call log) — but the LEAD's last inbound MESSAGE body needs a new helper.
- analytics route (app.py:977/995) returns `db.analytics(...)` verbatim → new dict keys flow through automatically (C need not touch it). `analytics.html` is the ROI UI.
- `businesses.trade` column exists (db.py:18); set at signup (app.py:296).

---

## GLOBAL DECISIONS (locked honesty rules — bake in)
- **[DECIDED] HONESTY P0 (build first):** `db.analytics` must count only missed-call-sourced leads for the recovered/leads/conversion metrics — add `AND source='missed_call'` to both lead queries. This is commit #1 of Agent A.
- **[DECIDED] Revenue is an ESTIMATE (pipeline, never cash).** Resolve the per-business average as: owner-set `avg_job_value` → `avg_source="owner"`; else `TRADE_JOB_VALUE_DEFAULTS[trade]` (with an $800 floor for unknown trades) → `avg_source="industry_default"`. `revenue = booked_n × resolved_avg` is ALWAYS labeled an estimate. The UI/digest/SMS must say "estimated" and distinguish owner vs industry-default (never present a default as measured fact). We do NOT track collected cash, so there is no "actual" state — do not imply one.
- **[DECIDED] ROI multiple** = `revenue / PLAN_COST_MONTHLY` (config, =99), computed at query time, never stored. Surfaced in analytics + digest + milestone.
- **[DECIDED] Milestone SMS threshold ≥ 2.0×** (not 1×) to absorb industry-default variance. Idempotent via a new `roi_milestone_sent_at` column (fire at most once per tenant until reset). Rides the alert channel → SF-6 quiet-hours + consent + opt-out honored (transactional=False so it respects the quiet-hours backstop — it's a celebratory/marketing nudge, not a solicited reply). Only fires when revenue is real enough: require ≥1 booked estimate AND (if the tenant's text-back is A2P-pending/simulated, do NOT fire — see next rule).
- **[DECIDED] A2P-pending honesty:** the digest ROI block and milestone must NOT claim recovered/earned value for a tenant whose text-backs never reached customers. Gate the ROI digest block + milestone on `compliance.a2p_ready(biz)` (approved). For a not-yet-approved tenant, the digest shows the honest "voice is live, texting activating" framing, not an ROI dollar claim.
- **[DECIDED] Dispatcher Call honesty:** trigger on the urgent path; call the OWNER (gate=False, platform alert number) with a TwiML that reads the caller's exact last words + offers press-1 to connect. Source of "caller's words" = `db.get_last_inbound_message(lead_id)` (synchronous, always present) — NOT `leads.summary` (async/may be empty). If `place_call` returns `simulated`/error, fall back to the existing urgent SMS alert and NEVER claim "calling you now"/"connected you." Record `dispatcher_call_last_at`; rate-limit to one dispatcher call per lead urgency (don't re-call on every inbound). Handle owner-no-answer/voicemail gracefully (the SMS alert is the backstop).
- **[DECIDED] Show-Up-Prepared briefing = a FORMAT EXTENSION of the existing booking alert, NOT a second send.** `format_message("booking", ...)` gains optional address/project/summary lines when present in context; the caller passes them from the lead with a **synchronous read-or-fallback** (the lead's enrichment from `_ensure_lead_notes` may not have landed yet — read what's there, fall back to the basic line, never block).
- **[DECIDED] Site proof:** REMOVE the bait-and-switch CTAs/claims (Jobber/HCP integration pills, voice "included on Pro" if not delivered). Do NOT invent testimonials/stats — the landing ROI/founder section is a CONTENT decision left to the owner (leave an honest placeholder or omit; flag in SETUP_NEEDED). Only ship copy that's true today.

---

## SHARED SEAMS (canonical — match exactly)
| Seam | Owner | Callers |
|---|---|---|
| `config.PLAN_COST_MONTHLY = 99` | **A** (config.py) | A (analytics/roi), B (digest) |
| `config.TRADE_JOB_VALUE_DEFAULTS: dict` (+ $800 floor) | **A** (config.py or db.py) | A |
| `db.analytics(...)` returns ADDED keys `roi_multiple: float|None`, `avg_source: "owner"|"industry_default"`, and `revenue` now resolved via default | **A** (db.py) | B (digest), analytics.html (B), app.py route (passthrough) |
| `db.get_last_inbound_message(lead_id) -> str` (last inbound message body, "" if none) | **A** (db.py) | C (dispatcher) |
| `db.set_roi_milestone_sent(business_id, ts)` + column `roi_milestone_sent_at` | **A** (db.py) | C (milestone hook), roi.py |
| `db.set_dispatcher_call_at(business_id_or_lead, ts)` + column `dispatcher_call_last_at` | **A** (db.py) | C (dispatcher) |
| `roi.check_roi_milestone(business_id) -> dict|None` ({"multiple":N,"revenue":$,"body":...} when due+unsent+approved, else None; never raises) | **A** (new roi.py) | C (post-booking hook) |
| `alerts.format_message("booking", ctx)` extended with optional `address`/`project`/`summary`; new kind `"roi_milestone"` | **B** (alerts.py) | C (passes booking ctx + fires roi_milestone) |
| `convos.digest_email` ROI block | **B** (convos.py) | (cron) |

**Existing (don't redefine):** `db.set_avg_job_value`(2481), `db.analytics`(2500, EXTEND), `compliance.a2p_ready`, `alerts.notify_async`(142)/`notify`(101)/`format_message`(47)/`_subject`(74), `messaging.place_call`(209), `ai.detect_urgency`, `handle_inbound`(1427).

## MIGRATIONS (db.init_db, guarded; Agent A) — `roi_milestone_sent_at TEXT`, `dispatcher_call_last_at TEXT` on businesses.

---

## PARTITION (file-disjoint; each agent owns whole files)

### AGENT A — Data & ROI engine: `db.py`, `config.py`, new `roi.py`
1. **[commit #1] Honesty fix:** add `AND source='missed_call'` to both lead queries in `db.analytics` (db.py:2514 + 2521).
2. config: `PLAN_COST_MONTHLY=99`; `TRADE_JOB_VALUE_DEFAULTS` dict (sensible per-trade averages; $800 floor for unknown).
3. Extend `db.analytics`: resolve avg = owner `avg_job_value` (avg_source="owner") else `TRADE_JOB_VALUE_DEFAULTS.get(trade, 800)` (avg_source="industry_default"); `revenue = booked_n × resolved_avg` (always present now, labeled); add `roi_multiple = round(revenue / PLAN_COST_MONTHLY, 1)` (None if revenue 0); add `avg_source` + keep `revenue` an estimate. Keep `days=None` all-time behavior.
4. Migrations + `db.set_roi_milestone_sent`, `db.set_dispatcher_call_at`, `db.get_last_inbound_message(lead_id)` (SELECT body FROM messages WHERE lead_id=? AND direction='in' ORDER BY id DESC LIMIT 1).
5. `roi.py`: `check_roi_milestone(business_id)` — load biz + analytics(all-time); if `compliance.a2p_ready(biz)` AND booked≥1 AND `roi_multiple >= 2.0` AND not `roi_milestone_sent_at`: return `{"multiple", "revenue", "avg_source", "body"}` (honest body, e.g. "FirstBack has booked an estimated ~${revenue} in jobs for you — about {N}x its cost. (estimate based on your {avg_source} job value.)"). Never raises; returns None otherwise.
**Tests:** `test_f12_analytics.py` (missed_call filter excludes manual leads; revenue from owner avg; revenue from trade default + avg_source; roi_multiple math; days=None), `test_roi_milestone.py` (fires at ≥2x+approved+unsent; NOT when a2p pending; NOT when already sent; NOT below 2x; body has no invented "actual"/cash claim). Re-run: test_migration, test_config_hub, test_compliance.

### AGENT B — Surfacing: `convos.py`, `alerts.py`, `templates/` (analytics.html, onboarding.html, landing.html, pricing.html)
1. `alerts.format_message("booking", ctx)`: when ctx has `address`/`project`/`summary`, append a Show-Up-Prepared briefing block (e.g. "Job: {project} · {address} · {summary}") to the existing "Estimate booked: {who} for {when}." line; absent → unchanged basic line. Keep it "pure". Add `"roi_milestone"` kind to `format_message` + `_subject` + the toggle map (default ON).
2. `convos.digest_email`: prepend an ROI block from `db.analytics(bid, days)` ONLY when `compliance.a2p_ready(business)` — "This week FirstBack recovered {leads} missed calls and booked {booked} estimates — an estimated ~${revenue} ({roi_multiple}x its cost; estimate based on your {avg_source} job value)." When not approved, an honest non-dollar line. Never claim cash.
3. `analytics.html`: add the ROI headline tile ("paid for itself ~{N}x" / estimated revenue) + the avg_source label ("based on your average" vs "industry estimate — set yours for an exact number"). No invented numbers; show the estimate honestly. Tokens not literals; no smart quotes.
4. Site CTA/proof fixes: remove the Jobber/HCP integration pills (onboarding.html ~149, landing.html ~99-103), fix voice "included on Pro" copy (pricing.html ~64) to match reality, replace/again-flag the placeholder testimonial (landing.html ~113) — do NOT invent a quote; leave an honest placeholder or omit. No smart quotes.
**Tests:** `test_f12_digest.py` (ROI block present + honest when approved; suppressed/non-dollar when a2p pending), `test_briefing.py` (booking alert includes address/project when present; basic when absent). Re-run: test_assistant (briefing/digest), test_alert_channel, test_compliance.

### AGENT C — Wiring: `app.py` only
1. **Show-Up-Prepared:** at the booking alert sites (app.py:1443, 1477), enrich the context with `address`/`project`/`summary` read from the lead (synchronous read-or-fallback — read what's on the lead row now; if absent, pass nothing → B falls back to basic). Do NOT add a second send.
2. **Dispatcher Call:** new `/twiml/dispatcher/<lead_id>` (returns TwiML: Say the caller's exact last words via `db.get_last_inbound_message` + "press 1 to connect") and `/twiml/dispatcher/connect/<lead_id>` (Dial the caller). On the urgent path (handle_inbound ~1436), after the urgent SMS alert, if `messaging.place_call` is real (configured) call the owner with the dispatcher TwiML + record `db.set_dispatcher_call_at`; rate-limit one per lead-urgency; if place_call returns simulated/error, the existing SMS alert stands and NOTHING claims a call was placed. Twilio-signature the TwiML routes.
3. **Milestone hook:** after a successful booking (app.py:1464/1477 success branch), call `roi.check_roi_milestone(biz["id"])`; if it returns a milestone, fire it via `alerts.notify_async(biz, "roi_milestone", {...})` (consent + quiet-hours via the alert path) and `db.set_roi_milestone_sent`.
**Tests:** `test_dispatcher_call.py` (TwiML reads last inbound message; urgent path calls place_call when configured; simulated/error → no false claim, SMS backstop; rate-limit), `test_f12_milestone_hook.py` (booking that crosses 2x fires roi_milestone once; second booking does not re-fire). Re-run: test_callback, test_webhooks, test_scheduling, test_setup.

## MERGE ORDER (Opus): A first (seams), then B and C. Full suite green after each. REVIEW GATE: un-stubbed e2e of the milestone (real db: missed-call leads + bookings cross 2x → check_roi_milestone fires once, not when a2p pending) + the analytics honesty filter; honesty pass (no cash claims, no invented testimonials, dispatcher never claims a call it didn't place).

## CODE vs OWNER-OPS
- CODE: everything above.
- OWNER-OPS: production domain (registrar + Render custom domain + DNS + `FIRSTBACK_PUBLIC_URL`, re-point Twilio webhooks LAST preserving `ringback-gixe`; same env drives SF-4/SF-7 — verify after swap) · the Render weekly digest cron (the ROI digest is inert until wired) · the real testimonial/founder/ROI content + consent.
