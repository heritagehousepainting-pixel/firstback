# Phase 6 Plan-Readiness: Honest Day-1 Scorecard
**Date:** 2026-06-18  
**Lens:** What does a brand-new paying customer actually get? The $99 product-readiness gap.  
**Method:** Code + docs audit across staging HEAD (dd4f02d), SETUP_NEEDED.md, compliance.py, connections.py, billing.py, setup.html, render.yaml, plus the NEXT-SESSION and AUTONOMY-BLUEPRINT records.

---

## THE HONEST HEADLINE

**Code quality after Phases 0–5:** ~7.5/10. Genuinely excellent engineering — tested loops, real compliance spine, server-bound confirm tokens, honest ROI, best-in-class screening graduation, a real A2P automation path with auto-flush, working Stripe billing/webhooks, delivery receipts + retry, timezone-correct scheduling, proactive Vic with morning digest and stall nudges.

**Can you honestly charge $99 for what a NEW customer gets TODAY?** ~5/10.

This is a real improvement from the ~3/10 at Phase 0 start. The billing, auth, compliance, and core-loop correctness problems are SOLVED in code. But "not deployed" is the new "not built." The entire product still runs on `ringback-gixe.onrender.com` (the old brand URL) with zero env vars set. A new customer who signs up today gets: a working dashboard, a demo mode they can poke, and a 2–10 day wait before a single text ever reaches a real phone. The $99 value is almost entirely deferred.

**Two separate truths — never blend them:**

| Axis | Score |
|------|-------|
| Code maturity (built, tested, architecturally sound) | ~7.5/10 |
| Delivered value to a brand-new paying customer TODAY | ~5/10 |

---

## PART A: What a New Customer Actually Gets Day-1

### REAL (works without any env configuration, day-1)
- A functional dashboard with lead management and command center ("Vic")
- Public `/demo` simulator — they can see a fictional booking happen (not their calls)
- Spam screening in monitor mode (logs what it would block, doesn't act)
- The setup wizard (`/setup`) with honest progress — clearly shows blockers, never claims "live" until proven
- Password reset, secure sessions, rate-limited login
- A beautiful ROI analytics view — correctly labeled "estimate" with trade-defaults
- Conversation memory in the command center; Vic's morning briefing logic (fires in code — but useless until their calls come in)

### SIMULATED / GATED (code works but blocked by unset env vars — Dave gets none of this day-1)
- **Real SMS text-backs** — requires Twilio configured + A2P approved + forwarding set. Estimated wait: 1–10 business days (carrier TCR vetting is irreducible). Until then: `send_sms` simulates in-app; no customer ever sees a text.
- **Billing/payment collection** — Stripe code is built and correct, but `STRIPE_SECRET_KEY` / 6 Price IDs / `STRIPE_WEBHOOK_SECRET` are unset in Render. The checkout button hits an unconfigured API. FirstBack CANNOT collect $99 today.
- **Forwarding sentinel verification** — built (real call placed + inbound CallSid match), but requires `FIRSTBACK_PUBLIC_URL` to build the TwiML URL. Unset = falls back to labeled manual confirm (honest but weaker).
- **Delivery receipts + retry** — built, but the StatusCallback URL is only constructed when `FIRSTBACK_PUBLIC_URL` is set. Until then: silent failure (no retry on a failed send).
- **A2P Trust Hub write API** — built and gated on `TWILIO_TRUST_PRODUCT_SID`. Without it: `submit_a2p` returns `{"status": "simulated"}` and submits nothing. Dave clicks "Activate texting" and… nothing happens.
- **Owner alerts (SMS/email)** — built with `ALERT_FROM_NUMBER` + Resend, but neither is configured. Dave's phone never rings with a "Dave, you have a new lead!" text.
- **Ticker / scheduled features** (reminders, follow-ups, morning Vic SMS, screening graduation, growth tray, nightly contacts sync) — code runs in-process, but without an external Render cron hitting `/tasks/run-due`, the ticker dies with the Render process and never fires scheduled jobs reliably.
- **ROI digest and milestone SMS** — gated on A2P-approved (correct — honest).
- **Dispatcher Call** (urgent caller → owner's phone rings) — gated on `FIRSTBACK_VOICE_URL`. Not set.
- **Google Calendar two-way sync** — requires Google OAuth creds (`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`). Not configured.
- **Voice callback (F10)** — DEFERRED explicitly by the owner. The code built in Phase 5g has critical gaps: streaming absent (1–3s dead air per turn), barge-in is a no-op, AMD absent, metering absent. All 7 quality checks need a real deployment. Pricing correctly says "coming soon."

### BAKED-IN TRUST LIABILITIES (honest copy, nothing oversold)
- All ROI figures labeled "estimate" — correct
- Voice labeled "coming soon/beta" — correct
- Testimonials removed (placeholder) — correct
- No "free trial" copy (paid-only) — correct
- Simulator correctly labeled as demo — correct
- Growth auto-send requires tray approval — correct (TCPA-safe by construction)

---

## PART B: Time-to-First-Real-Value — The Path and Its Friction

### The A2P Wait: The Master Gate

The honest TTFV path for Dave today:

1. Sign up → fill business profile (3 min, reasonable)
2. Buy/attach a phone number (automated, 1 click — good)
3. "Activate texting" → `submit_a2p` returns "simulated" because `TWILIO_TRUST_PRODUCT_SID` not set → **Dave sees nothing happen**. This is a silent failure today. Even if the Trust Hub SID is set, submission accepted ≠ approved. 1–10 business days to carrier approval.
4. Set up forwarding (one phone tap, irreducible, guided) — but the sentinel call needs `FIRSTBACK_PUBLIC_URL` set
5. Make a test call to prove the net is on (live feedback, good UX)
6. Wait for A2P approval → auto-flush of any blocked sends from the wait period

**Honest TTFV:** Not "instant." The gap between signup and a customer's phone receiving a text is 1–10 business days, and the A2P wait happens during that gap. The code auto-flushes blocked sends on approval, which is the right move. But day-1 value is voice-only (call screening, forwarding detection) — which is real and important but not the core $99 promise (text-back + booking).

### The Dave Test: Current Friction Audit

| Step | Dave can do it? | Friction level |
|------|-----------------|----------------|
| Sign up, fill profile | Yes | Low (wizard is clean) |
| Business lookup prefill | Maybe (requires `GOOGLE_PLACES_API_KEY` — unset) | Medium |
| Buy phone number | Yes (Twilio configured) | Low |
| Activate texting | FAILS SILENTLY if Trust Hub SID not set | **Critical** |
| Set forwarding | Yes (guided with carrier code) | Low-medium |
| Prove forwarding works | Needs `FIRSTBACK_PUBLIC_URL` | Medium |
| Get first owner SMS alert | Needs `ALERT_FROM_NUMBER` + Resend | **High — silent** |
| See Vic morning briefing on phone | Needs owner alert channel live | **High — silent** |
| Receive ROI milestone SMS | Needs A2P + 2+ bookings | Gated correctly |

**Biggest single friction point:** The silence. Dave does setup steps, sees "pending" status, gets no proactive confirmation, hears nothing for 1–10 days. No "it's working, your call was caught, we're activating texting" text to his phone because the owner alert channel is not configured. The A2P wait happens in silence — the #1 churn risk identified in the friction-audit still applies because the env vars that make the system "talk to Dave" are not set.

---

## PART C: Remaining Gaps — "Would Feel Stupid Leaving" vs. Today

### The gap is not feature completeness. The gap is deployment.

Every major Phase 0–5 feature is built and tested. The gap between "built" and "a contractor would feel stupid leaving" is almost entirely:

1. **Not deployed** — the live site is still `ringback-gixe.onrender.com` with an old brand and likely mismatched env vars. No customer can reach the product as built.
2. **Owner-ops env vars not set** — Stripe can't collect money, Twilio can't send texts, alerts can't reach Dave's phone, the ticker can't be reliably trusted, A2P submissions simulate rather than submit.
3. **No real proof on the site** — testimonials are placeholders, no production domain, no real ROI numbers from real customers.
4. **Voice is explicitly deferred** — which is the right call (mediocre voice is worse than none), but it leaves the "jaw-drop demo moment" on the table.
5. **CSRF on mutating API family** — the `/api/calls`, `/api/leads` (including the screening rescue tap) use SameSite=Lax with no CSRF double-submit token. A pre-existing, documented, un-closed hardening gap.

### Per-feature honest current state vs. "feel-stupid-leaving"

| Feature | Built? | Deployed/Live? | "Feel stupid leaving" gap |
|---------|--------|----------------|---------------------------|
| Missed-call capture + forwarding | Yes (sentinel verified) | No (needs `FIRSTBACK_PUBLIC_URL`) | Deploy |
| AI text-back | Yes | No (A2P unsubmitted, sim-only) | Deploy + A2P |
| AI booking brain | Yes (guards, turn cap, cost cap) | No | Deploy |
| Reminders + RSVP | Yes (tested) | No (ticker unreliable without cron) | Deploy + cron |
| Google Calendar sync | Yes | No (`GOOGLE_CLIENT_ID` unset) | Owner OAuth creds |
| Spam screening | Yes (auto-graduation) | No (monitor, no enforce, no cron) | Deploy + cron |
| Owner alerts | Yes (Resend/SMTP) | No (`ALERT_FROM_NUMBER` unset, no Resend acct) | Resend account |
| Vic proactive (morning SMS) | Yes | No (owner alert channel offline) | Owner alert channel |
| Growth approval tray | Yes (consent-safe) | No (needs A2P, no cron) | Deploy + A2P + cron |
| Cold follow-up | Yes (2-touch, spam-excluded) | No (same as growth) | Deploy + A2P |
| Contacts sync | Yes (inert until creds) | No (Google OAuth unset) | Owner OAuth creds |
| ROI / analytics | Yes (honest estimates) | No (no real data, no deployed product) | Deploy + real usage |
| AI voice callback | Partially (critical gaps: no streaming, no AMD) | No (deferred) | Spike + deploy (post-initial) |
| Stripe billing | Yes (webhooks + grants) | No (keys unset, Price IDs uncreated) | Set Stripe + Price IDs |

---

## PART D: Ranked — What Phase 6 Must Close

### TIER 1: Block charging $99 (must close before any paying customer)

**1. Deploy the staging branch to production.**
The entire product is built and tested but not deployed. The live URL still serves the old RingBack brand. No one can buy what doesn't exist at a URL they can visit.
- Owner-ops: reconcile `FIRSTBACK_*` env vars in Render before deploy (DB path mismatch = DB reset)
- Owner-ops: set `FIRSTBACK_SECRET` (real SECRET_KEY), `FIRSTBACK_HTTPS=1`, `FIRSTBACK_RUN_TICKER=1`
- Owner-ops: point a real domain (firstback.io or similar); `*.onrender.com` is a trust kill at $99

**2. Enable money collection.**
`STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` + 6 Price IDs (3 monthly + 3 annual) are not set. The checkout button hits an unconfigured API. This is a literal zero-revenue blocker.

**3. Wire the owner alert channel.**
The platform needs `ALERT_FROM_NUMBER` (a platform-owned Twilio number for owner-only SMS) + a Resend account + `SMTP_HOST/FROM`. Without this: Dave gets no notifications when leads come in, Vic's morning SMS never fires, no milestone alerts, no "your AI just booked a job!" text. This is the biggest silent-failure risk for early churn.

**4. Set `FIRSTBACK_PUBLIC_URL`.**
This single env var activates SMS delivery receipts + retry (SF-4) and the forwarding sentinel verification (SF-7). Without it, failed texts disappear silently and forwarding is weaker (honest label, but no real sentinel).

**5. Wire the external cron (`/tasks/run-due` every 60s).**
Nine features ride the ticker: reminders, follow-ups, screening graduation, growth tray, contacts sync, morning Vic digest, health probes, weekly ROI digest, A2P sync. The in-process ticker is a fallback that dies with the Render process. Without the cron, these features are unreliable. Dave will miss reminders.

**6. Run the Heritage dogfood A2P submission.**
The `submit_a2p` Trust Hub write path is built but never run against Twilio's real API. `TWILIO_TRUST_PRODUCT_SID` is unset — every "Activate texting" click returns "simulated." Before charging any customer, we need:
- Set `TWILIO_TRUST_PRODUCT_SID` in Render
- Run Heritage's own sole-prop submission as the proof case (HC-3 — confirm the exact payload + OTP mechanic)
- Confirm `<slug>.firstback.io` page passes TCR (HC-1) — or have a fallback plan if it doesn't
- The 1–10 day carrier wait is irreducible; Phase 6 just needs to make the wait VISIBLE and FEEL ALIVE

### TIER 2: Without these, customers churn in week 1 (close before public launch)

**7. The A2P wait must feel alive, not silent.**
The auto-flush logic is built. But during the wait, Dave needs:
- An onboarding "activating your texting" progress state that actively updates
- A "we caught your first call!" text to his phone the moment forwarding is verified (instant gratification before any wait)
- A status SMS when A2P approves ("Your texting is now live — 3 calls were caught while you waited, we texted them all back")
Currently: nothing. Dave sees a "pending" badge and hears nothing for days.

**8. Set `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.**
Google Calendar two-way sync and Google Contacts nightly sync are built but inert without OAuth creds. A contractor who connects Google gets dramatically more value (booking to real calendar, known customer recognition). Setup friction is currently medium-high for a feature that's table-stakes.

**9. Close the CSRF gap on mutating API family.**
`/api/calls/<id>/real` (screening rescue), `/api/leads` mutating endpoints, growth batch release — SameSite=Lax only. The server-bound confirm token covers Vic's write actions but NOT the direct API family. This is a documented pre-existing gap. It is a security liability before real customers with real leads.

**10. Voice: decide and ship or defer properly.**
Voice is "coming soon" in pricing — correctly labeled. But the code in `voice_service.py` has critical missing gaps (no streaming, barge-in is a no-op, no AMD). If Phase 6 includes any voice promise to customers, the streaming spike must ship. If deferred, the pricing page should remove it from Pro and put it as a visible future roadmap item, not "coming soon" which implies soon.

### TIER 3: Polish — real but deferrable past first $99

- Real proof content: testimonials, ROI numbers from Heritage dogfood. Cannot have these until Heritage is live and consents.
- CSRF hardening on the direct API family (can add after first customer, before scaling)
- Google Business Profile connector for review-request visibility (the 5-Star / negative-review play — not built yet, correctly deferred)
- Voice streaming spike (can be a post-launch feature, but don't sell it while deferred)
- Production domain `firstback.io` (ops only — critical for trust but purely DNS/Render config)
- Web push / SSE lower-latency notifications (the 25s poll fallback is honest and works)

---

## THE DAVE TEST SUMMARY

Dave, 52, painting crew, two-thumb texter, iPhone. Can he set this up today and have it running?

**Today (undeployed, env vars unset):** No. The product doesn't exist at a real URL. If you hand him the staging URL, he can poke the demo and see a cool booking simulation. He cannot pay. He cannot get a real text sent.

**After Phase 6 Tier 1 ops are done (deployed + Stripe + alerts + cron + domain):** Mostly yes. He fills out the profile, clicks "Activate texting," it submits his sole-prop registration automatically, he sets forwarding on his phone (one call), his phone gets a "we just caught your first call!" text. Then he waits 1–10 days for carrier approval — and that wait needs to feel alive (Tier 2 item #7). Once approved, the auto-flush sends every queued text. From there: reminders fire, Vic sends the morning briefing to his phone, growth tray asks before sending anything.

**The Dave test pass/fail:** Tier 1 ops done = ~7/10 for Dave. He can get through setup. The A2P wait is the only rough spot, and the auto-flush + proactive status makes it manageable. Tier 2 items (alive A2P wait, Google creds, CSRF) get it to ~8/10.

---

## SCORECARD: NOW vs. POST-PHASE-6

| Dimension | Now | Post-Phase-6 Tier 1 | Post-Phase-6 Full |
|-----------|-----|---------------------|-------------------|
| Code quality | 7.5/10 | 7.5/10 (unchanged) | 8/10 |
| Sellable (new customer day-1) | 5/10 | 7/10 | 8.5/10 |
| Dave friction | 8/10 friction | 3/10 friction | 2/10 friction |
| Billing works | 0/10 | 10/10 | 10/10 |
| Real texts reachable | 0/10 | 8/10 (post-A2P) | 9/10 |
| Feels alive during wait | 2/10 | 4/10 | 8/10 |
| Trust/proof on site | 3/10 | 4/10 | 7/10 |

---

## WHAT PHASE 6 IS

Phase 6 is not a build phase. It is a **ship phase.**

The code is done. Phase 6 = the owner-ops sequence that turns the staging branch into a live product that can take money and deliver real value to a real contractor.

**The Phase 6 sequence (in dependency order):**

1. **Render reconcile + deploy** — env var audit before any deploy (DB path mismatch = reset). Set all `FIRSTBACK_*` vars, deploy staging branch to prod.
2. **Domain** — register firstback.io (or confirm existing), CNAME to Render, set `FIRSTBACK_PUBLIC_URL`.
3. **Stripe** — create test keys + 6 Price IDs, set env vars, run a test checkout + webhook.
4. **Resend account** — verify `firstback.app` sending domain, set `SMTP_*` vars. Wire `ALERT_FROM_NUMBER`.
5. **External cron** — Render cron job → `POST /tasks/run-due` every 60s with the tasks secret header.
6. **A2P dogfood** — set `TWILIO_TRUST_PRODUCT_SID`, run Heritage sole-prop submission, confirm OTP mechanic, confirm `<slug>.firstback.io` subdomain resolves + passes TCR. Wait for approval. Verify auto-flush fires.
7. **Google OAuth** — create OAuth app in Google Console, set client creds, test Calendar + Contacts connect flows.
8. **Voice decision** — build the streaming spike (S-2 in PHASE5G-SPEC.md: `tool_complete_stream` wired to voice path + `/internal/voice/stream` endpoint) or explicitly defer voice to a post-launch roadmap. Don't leave it "coming soon" with 1–3s dead air behind it.
9. **CSRF hardening** — add double-submit token or CSRF middleware to the mutating `/api/calls` and `/api/leads` family.
10. **`be-audit` pass** — billing webhooks + auth + new CSRF surface before any real paying customer.
11. **Heritage go-live** — turn Heritage House Painting on as the first real tenant. Run a real missed call. Get the text-back. Get the calendar event. Get the morning Vic SMS. Verify the full loop end-to-end.
12. **First paying customer** — after Heritage proves the loop, open the door.

**Phase 6 is the last mile. Everything that makes it worth $99 is already built.**

---

## HONEST RISKS

- **A2P/TCR timing is external and irreducible.** The 1–10 day carrier vetting cannot be compressed. The honest path: Heritage submits day 1, waits, proves the path. Other customers can start setup during that window but won't get real texts until approved.
- **Streaming voice is missing** from the voice code. If Phase 6 promises voice, the streaming spike (estimate: M effort, 1 slice) must ship first. A blocking HTTP turn with 1–3s dead air is worse than no voice.
- **The `*.onrender.com` domain is a trust kill.** A contractor paying $99/mo will not trust a product at `firstback-gixe.onrender.com`. Domain is non-negotiable before charging.
- **Heritage dogfood is the proof.** Until Heritage is live and real texts are flowing, every claim about what the product delivers is unconfirmed. The 3 HC blockers (TCR subdomain, email path, sole-prop OTP) need real confirmation, not code-mocked tests.

---

*Written 2026-06-18. Source: staging HEAD dd4f02d + SETUP_NEEDED.md + AUTONOMY-BLUEPRINT.md + NEXT-SESSION.md + per-phase build records. No spin applied.*
