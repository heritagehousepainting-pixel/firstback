# PREDEPLOY-08-HONESTY — Honesty / Truthful Claims Audit

**Auditor lane:** Honesty / no bait-and-switch  
**Branch:** staging @ 55d2601  
**Date:** 2026-06-19  
**Verdict:** CONDITIONAL PASS — 0 P0s, 2 P1s, 2 P2s

---

## Scope

Templates reviewed: `onboarding.html`, `landing.html`, `pricing.html`, `analytics.html`, `dashboard.html`, `settings.html`, `simulator.html`, `microsite.html`, `customers.html`, `setup.html`

Code reviewed: `app.py`, `roi.py`, `billing.py`, `messaging.py`, `compliance.py`, `connections.py`, `db.py`, `render.yaml`

---

## P0 Findings (blocks deploy — live falsehood shown to a paying customer)

**None.** All seven audit domains were checked; no P0 conditions found.

---

## P1 Findings (should fix before deploy)

### P1-A — onboarding.html:92-95, JS lines 171-179: "Call" tab drives to /signup with no "not available yet" gate

**What it does:** The onboarding hero has two tabs ("Text" and "Call"). The "Call" tab swaps the headline to "AI voice callback" and the sub-copy to "…a real AI voice on the line. Currently in beta." The **shared** phone-number input form still submits to `/signup` when either tab is active. The "Get started" and "Try it on your phone" buttons are always present and always lead to `/signup`, regardless of which tab is selected.

**The problem:** A prospect who clicks "Call," reads the voice-callback pitch, and then taps "Get started" or submits the form buys a $99/mo subscription expecting voice callback. Voice callback is **not deployed** (render.yaml line 89: the voice service block is commented out; `VOICE_PUBLIC_URL` is unset, so `app.py:2738` never triggers it). The word "Currently in beta" appears only in the hidden JavaScript string `OB_MODES.call.sub`; it is never shown on the Get-started button or below the form — a user can read it and still not register it as "you cannot get this today."

**Gap:** The call tab copy sets an expectation ("books the estimate — a real AI voice on the line") that a paid subscriber on Day 1 will discover is wrong. The gate in code (`VOICE_PUBLIC_URL` unset) is correct, but the pre-signup marketing surface does not prominently gate or disable the CTA for the voice story.

**File:line:** `templates/onboarding.html:92-95, 168-192`

**Fix:** Either disable/hide the "Call" tab from the live public page until voice ships, or add a prominent "Join the waitlist" CTA beneath the Call tab sub-copy instead of pointing to `/signup`.

---

### P1-B — pricing.html:10, pricing.html:71: "set up in a day" / "live within a day" understates A2P wait

**What it does:** Pricing page header says "Pick a plan, get set up in a day" (line 10). The FAQ answer for "How long does setup take?" says "Most contractors are live within a day." (line 71).

**The problem:** A2P 10DLC registration, while submitted instantly in code, is reviewed by carriers and typically takes **1–5 business days**, not one calendar day. `setup.html:134` honestly says "usually within a day" for the status that shows *after* submission, but the public-facing pricing page makes the pre-signup promise "set up in a day" — a promise that a new customer paying $99 on a Monday may find broken on Wednesday when texting still hasn't activated.

The `/setup` wizard (`setup.html:149`) is honest: "texting usually turns on within a day after that." But the discrepancy between the external marketing promise ("set up in a day") and the realistic A2P timeline means a new customer's expectation is not set correctly before they pay.

**Note:** The system does correctly gate all sends behind `compliance.a2p_ready()` (`messaging.py:140`); no texts go out pre-approval. The issue is purely the marketing timeline claim.

**File:line:** `templates/pricing.html:10`, `templates/pricing.html:71`

**Fix:** Change "set up in a day" to "set up in minutes — texting usually live within 1–2 business days after carrier approval" or similar that matches the actual onboarding language.

---

## P2 Findings (notable but not deploy-blocking)

### P2-A — simulator.html:45: "Live" badge on the /demo and /simulator pages

The device panel in `simulator.html` always shows `<span class="sim-live"><span class="dot"></span> Live</span>` (line 45). This same template is used for both the **public /demo** (a sandbox business) and the **logged-in /simulator** (the owner's real tenant). The page title is "Live demo" and the page sub is "See exactly what your customer feels."

For the public /demo route no real SMS is sent, so "Live" refers to the conversation engine being active, not to an actual Twilio send. This is not a P0 (the intro copy says "Simulate missed call," making the sandbox nature clear), but a lay visitor who sees a pulsing green "Live" dot may reasonably believe a real text just went to a real phone. A one-word prefix — "Simulation · Live" — or hiding the badge on `demo_mode=True` would close the gap.

**File:line:** `templates/simulator.html:45`

---

### P2-B — setup.html:134: "Your AI is already answering calls" during A2P pending

When A2P status is "pending," `setup.html:134` shows: "Your AI is already answering calls. Texting turns on automatically, usually within a day."

This is true in the sense that inbound call handling is live (forwarding is wired), but the customer's missed calls are NOT being texted back yet (the A2P gate in `messaging.py:140` blocks all customer-facing SMS). A new subscriber seeing this banner may believe the core product is already working for their customers, when the critical text-back is still gated.

**File:line:** `templates/setup.html:134`

**Fix:** Qualify the claim: "Your AI is ready. Texting turns on automatically — usually within a day — once carrier approval comes through. Until then, missed calls don't get the text-back yet."

---

## Verified PASS items

### (1) AI voice (5g) gate

- `render.yaml:89`: The `firstback-voice` service block is **commented out** — the voice process is never deployed.
- `config.py` / `app.py:2738`: Voice callback is gated on `VOICE_PUBLIC_URL`; when unset (the deploy default), the `if norm in _CALL_WORDS and VOICE_PUBLIC_URL:` branch is never entered.
- `templates/pricing.html:39`: AI voice callback listed as `<span style="opacity:.5">coming soon</span> AI voice callback <span style="opacity:.6">(beta -- not yet available)</span>`.
- `templates/pricing.html:69` (FAQ): "Coming soon. Voice callback is in beta and rolling out on Pro and Crew…Today FirstBack handles everything by text."
- `templates/settings.html:49-52`: Settings voice card shows "Voice callback is live" only when `voice_configured and sms_configured` — both require real env vars.
- **PASS** on every pricing, FAQ, and settings surface. The gate holds.

### (2) ROI / analytics: estimate language

- `roi.py:64-68`: SMS body always uses "estimated $X in jobs" and "(Estimate based on…avg job value)." Never "actual" or "cash."
- `roi.py:5-9`: Gate 1 requires `compliance.a2p_ready()` before milestone fires — only fires when texting is real.
- `roi.py:54-55`: Gate 4 requires `roi_multiple >= 2.0` before firing.
- `analytics.html:36-37`: Footer says "Revenue is an **estimate** based on the average job value you set in Settings -- not collected money."
- `analytics.html:65`: JS `renderHeadline()` `noteEl.textContent` explicitly says "Revenue is an estimate -- not collected money."
- `db.py:2983`: Analytics filtered to `source='missed_call'` only — manually-added leads excluded.
- **PASS** — ROI is consistently labeled an estimate, never cash.

### (3) A2P / onboarding honesty

- `compliance.py:42-46`: `a2p_ready()` requires `a2p_status == "approved"` — only a real Twilio campaign approval sets this.
- `connections.py:289`: Comment: "NEVER set status='approved' here -- only a2p_sync() may, after polling."
- `connections.py:511-526`: `a2p_sync()` maps Twilio's real campaign status; a 200 HTTP response from `submit_a2p` only sets "pending" not "approved."
- `messaging.py:140-145`: Customer-facing SMS blocked when `not compliance.a2p_ready(business)` — logged as `"blocked"/"a2p_not_approved"`.
- **PASS** — A2P gate is properly wired; a 200 from submission does NOT set approved.
- **P1-B** (timing copy) noted above; the code path is honest, the marketing claim is not.

### (4) No fake testimonials / placeholder logos on live routes

- `templates/customers.html:7`: "no invented quotes, no stock photos" — explicit. The three cards show clearly-labeled placeholder text ("Your first customer's quote goes here").
- `templates/landing.html:105`: Testimonial section comment: "placeholder removed -- add a real customer quote here once available." Section itself is not rendered.
- No Jobber, Housecall Pro, ServiceTitan, or other third-party brand logos found in any live-routed template.
- `app.py:284-289`: `/tour` redirects to `/`, and `landing.html` is explicitly unrouted: "landing.html is kept only as reference material and is no longer routed."
- **PASS** — No fake testimonials or placeholder logos on live routes.

### (5) Simulated sends never shown as "delivered"

- `messaging.py:83-155`: "simulated" status only records a message on the lead thread (visible in the UI) but never calls Twilio. The dashboard shows it with `pill('Reminder simulated','neutral')` (`dashboard.html:90`).
- `settings.html:195-196, 218`: Help text on alert fields: "Texts are simulated until Twilio is set up." Reminder card: "Texts are **simulated** until Twilio is set up."
- `settings.html:204-206`: When both SMS and email unconfigured: "alerts are recorded **in-app** only."
- **PASS** — Simulation is disclosed at the settings layer.

### (6) "Live" / "connected" / "verified" badges reflect real state

- `connections.py:122-127`: `is_live()` delegates to `compliance.launch_blockers()` — non-empty = not live.
- `connections.py:86-87`: `step_state` marks a2p "done" only when `compliance.a2p_ready()` and forwarding "done" only when `forwarding_confirmed`.
- `app.py:1387-1388`: `live_verified` requires `is_live AND last_call.engaged` — a real test call that was actually texted back.
- `setup.html:18-52`: Three states: "You're live." (verified), "Setup complete — make a test call" (not yet verified), and progress banner with blockers. Never claims live without the sentinel.
- `connections.py:601,606`: "[DECIDED] Honesty rule: this function NEVER sets forwarding_confirmed=True." Forwarding is confirmed only by the inbound Twilio webhook (`app.py:2627`).
- `google_cal.is_connected()` / `google_contacts.is_connected()` — real OAuth token checks, not optimistic flags.
- **PASS** — All badges track real state.

### (7) Email/SMS "simulated until configured" disclosures

- `settings.html:195`: "Email is simulated until SMTP is set up" in the help text for the alert email field.
- `settings.html:196`: "Texts are simulated until Twilio is set up."
- `settings.html:217-219`: Reminder card note repeats the same.
- `settings.html:204-206`: When neither configured: combined disclosure in a provider-note.
- **PASS** — Disclosures are present and in context.

### (8) /demo sandbox isolation

- `app.py:620-685`: Demo business created with sentinel name `__firstback_demo_sandbox__`. `/api/demo/reply` verifies `lead.biz_id == sandbox_bid` before processing.
- The `/demo` and `/api/demo/*` routes are completely separate from any real tenant's data.
- **PASS** — Demo is isolated.

---

## Day-1 Customer Gap Analysis

A brand-new $99/mo customer (Starter plan) signs up after seeing the "Set up in a day" promise on pricing.html.

**What works immediately (code confirmed):**
- Account creation and /setup wizard
- Twilio number provisioning (if Twilio creds set)
- Call screening UI, settings, dashboard
- AI conversation simulator
- Calendar blocking

**What does NOT work on Day 1 (requires wait):**
- **Text-back to real customers**: blocked until A2P approved (typically 1–5 business days). `messaging.py:140` enforces this.
- **Owner SMS alerts**: gated if ALERT_FROM_NUMBER is unset (same Twilio dependency).

**Disclosures that cover this:**
- /setup wizard: "texting usually turns on within a day after that" (setup.html:149) — honest but understates the range.
- Settings: "Texts are simulated until Twilio is set up" — covers the operator's env-var gap, not the A2P gap.

The A2P wait is the only gap the marketing copy doesn't properly set expectations for (P1-B above). The code gates are correct; it's the promise that's slightly ahead of reality.

---

## Summary Table

| # | Severity | File:line | Issue |
|---|----------|-----------|-------|
| P1-A | P1 | `templates/onboarding.html:92-95, 168-192` | "Call" tab (voice) drives to /signup; "Currently in beta" only in JS copy, not on CTA |
| P1-B | P1 | `templates/pricing.html:10, 71` | "Set up in a day" / "live within a day" — A2P takes 1-5 business days, not one |
| P2-A | P2 | `templates/simulator.html:45` | "Live" green dot appears even on the public /demo sandbox page |
| P2-B | P2 | `templates/setup.html:134` | "Your AI is already answering calls" when A2P pending — core text-back is not yet working |

**Deploy verdict: HOLD on P1-A and P1-B.** Both are fixable in < 30 min with copy edits only (no code change needed for P1-B; P1-A needs either a tab disable or a CTA swap for the Call tab). Once addressed, no honesty blockers remain.
