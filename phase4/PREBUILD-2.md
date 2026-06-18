# PREBUILD-2 — Phase 4 Honesty / Risk Lane
**Pre-Build Planner 2 of 3 — READ-ONLY analysis**
**Date:** 2026-06-18
**Scope:** Phase 4 ("convert & prove") — ROI/analytics (F12), shared retention path (Dispatcher Call, Show-Up-Prepared, weekly digest), production domain, site proof.
**Lane:** HONESTY / RISK / "CAN-WE-CHARGE-AND-NOT-LIE"

---

## Cardinal Honesty Rules for ROI (bake these into every spec, test, and copy review)

1. **ROI counts only `source='missed_call'` leads that reached `status='booked'` appointments.** No other source (manual add, growth, import) counts toward "FirstBack earned you $X." (`db.analytics()` at `db.py:2513` currently does NOT filter by source — it counts ALL leads. This is the live attribution bug.)

2. **Revenue is always pipeline (booked estimate), never closed cash.** Label as `~$X` (tilde-prefix) everywhere. Never a bare `$X`. The word "estimated" or "pipeline" must accompany every dollar figure until L9 ("Mark as won") ships.

3. **"Calls recovered" must be computed from the DB, not assumed.** V1 proxy (`leads_n` where `source='missed_call'`) is acceptable only when labeled "leads from missed calls." The precise metric (calls where `missed=1` AND a lead row exists within 5 min on the same `from_number`) requires a new `db.calls_recovered()` query (M8). Never use raw `leads_n` and call it "calls recovered" — those are two different claims.

4. **When A2P is still pending (SF-8 not yet approved), ROI MUST NOT count sends that never reached a customer.** If a text-back shows on the thread but was `status='simulated'` or `status='blocked'` (pre-A2P), the lead was never actually engaged. Do not count it as "recovered." The DB does not currently distinguish delivered vs simulated leads; this must be added before ROI numbers are surfaced to any customer who is pre-A2P.

5. **Trade-based defaults are estimates, not facts. Label them.** `TRADE_JOB_VALUE_DEFAULTS` (planning value: painting=$2,800, plumbing=$850, etc.) must display as `ESTIMATE (industry default)` in every surface — not just the dashboard. This includes the digest email body and the milestone SMS text.

6. **Milestone SMS threshold is conditioned on `avg_source`.** If using a trade default (not Dave's real number), fire only when `roi_multiple >= 2.0`. If Dave set his real number, fire at `>= 1.0`. A trade-default-fueled "you paid for yourself!" at 1.01× could be wrong by ±50% depending on trade variance.

7. **Milestone SMS is once-per-billing-period, idempotent, with quiet-hours compliance.** The `roi_milestone_sent_at` column does not yet exist in `businesses` — must be added via migration before M5 ships. Without it, a busy booking week could fire the SMS on every booking.

8. **Suppress the ROI block (not the whole digest) when `leads_n == 0 AND booked_n == 0`.** Never send a "$0 in pipeline" milestone SMS. Send the "quiet week" variant instead.

9. **Show the math explicitly in every ROI surface.** Format: `Y booked × ~$Z avg (painting industry est.) = ~$W in pipeline. FirstBack: $99/mo. Ratio: ~Nx.` No black-box numbers. Audit trail: `avg_source` must be returned in every analytics payload.

10. **The `analytics()` function returns `revenue = None` for any new account without `avg_job_value` set (verified at `db.py:2507`).** Trade defaults are NOT yet in the code. Until S1 lands, new-account dashboards show a broken/null revenue tile. Do not deploy any ROI-facing copy on the site until S1 is in production.

11. **Never claim a domain is "live" or "custom" before DNS resolves and the SSL cert is provisioned.** The trust upgrade is real only when `firstback.app` (or equivalent) resolves. Preserve `ringback-gixe.onrender.com` as the live failover throughout the cutover.

12. **Testimonials must be real, consented, and verbatim.** The landing page currently shows a visible placeholder blockquote (landing.html:113). The customers.html page honestly labels placeholders as placeholders ("no invented quotes, no stock photos"). The risk is a builder who replaces landing.html:113 with a "realistic-sounding" invented quote to polish the demo — that is a legal and trust violation. The rule: if no real customer exists, keep the placeholder visible or remove the section entirely. No fabricated quotes ever.

---

## Risk Register — Phase 4 Items

### P0-A — ROI analytics counts ALL leads, not just missed-call intercepts (LIVE BUG)
**Risk:** `db.analytics()` at `db.py:2513–2524` queries `leads WHERE business_id=?` with NO `source` filter. Manually added leads and any future growth-engine leads will inflate the "FirstBack caught X calls" claim. If even one lead is added manually before ROI ships, the numbers are wrong and unchallengeable.
**Where it bites:** The moment any tenant has a manually-added lead AND the ROI block fires in the digest, FirstBack is claiming credit for a call it never intercepted.
**Honest rule:** Add `AND source='missed_call'` to the leads query in `db.analytics()` (and to the "calls recovered" V1 proxy). Return `by_source` grouping so the UI can footnote excluded leads. This is a one-line fix with large honesty consequences.

### P0-B — Trade defaults not in code; new accounts see null revenue tile today
**Risk:** F12-FINAL plans trade defaults in S1, but they are not shipped. Any builder who skips S1 and ships S2 (ROI multiple tile) first will display `None` or a broken tile to every new account. The headline "FirstBack paid for itself Nx" will compute as division-by-zero or show "--×."
**Where it bites:** Day-1 account experience. The retention moment becomes a broken UI.
**Honest rule:** S1 (trade defaults + `avg_source`) MUST land before any ROI tile, milestone SMS, or digest ROI block is deployed. Gate S2/S3/M5 on S1.

### P0-C — `roi_milestone_sent_at` column does not exist; milestone SMS has no idempotency guard
**Risk:** The M5 milestone SMS depends on `businesses.roi_milestone_sent_at` to prevent re-firing. This column is not in the current schema (verified: not in `db.py` migrations). A builder who implements `check_roi_milestone()` without adding the migration will send the milestone SMS on every booking in a high-activity week.
**Where it bites:** Dave gets the "paid for itself 84×" SMS 7 times in one week. Trust destroyed.
**Honest rule:** Migration adding `roi_milestone_sent_at TEXT` (and `roi_milestone_sent_period TEXT` for billing-period scoping) must be the first commit of M5. The function must check both: (a) the column exists, (b) it's NULL or from a prior billing period.

### P0-D — Dispatcher Call: `place_call` returns `"simulated"` when Twilio unconfigured; app.py:2239 treats "simulated" == success and tells the customer "Calling you now"
**Risk:** At `app.py:2239`, when a customer texts "CALL," the code sends `"Calling you now."` for BOTH `placed` AND `simulated` statuses. In a demo or pre-Twilio environment, the customer gets that confirmation and then no call arrives. This is a false claim to a real person.
**Where it bites:** Any tenant whose Twilio is not yet configured (including the demo mode) can trigger this false "Calling you now" response. Also applies to the Dispatcher Call to Dave: if `place_call` returns `"error"`, the alert currently does not distinguish "we tried and failed" from "we called you." The owner's alert log will show the call was attempted but not whether Dave's phone actually rang.
**Honest rule:** (1) Gate the "Calling you now" customer SMS on `status == "placed"` only, not `("placed", "simulated")`. (2) When `status == "simulated"`, reply honestly: "Voice callback isn't active yet — keep texting here and we'll get you sorted." (3) For the Dispatcher Call to Dave: record the `place_call` result status in the alert log row. If `status != "placed"`, the alert body must read "We tried to call you but couldn't connect — check the lead in FirstBack" not "we connected you."

### P1-A — Milestone SMS threshold gaming: `roi_multiple >= 1.0` with industry default fires on one roofing booking ($4,500 ÷ $99 = 45×) on day 2 — misleading
**Risk:** A roofing contractor books one estimate. Trade default is $4,500. ROI multiple = 45.4×. FirstBack sends "Paid for itself 45×." Dave forwards it to his wife. But it's one booking at an industry-average estimate value, not a closed job. The number is arithmetically true but gives the impression of 45 booked jobs.
**Where it bites:** When Dave's real average job is $800 and the trade default is inflated, the milestone fires on a number that's 5× his actual reality. The "positive reveal" (where Dave's real number is higher than the default) relies on trade defaults being conservative. Roofing at $4,500 is NOT conservative — it may be higher than Dave's average.
**Honest rule:** Milestone SMS for roofing (and any high-variance trade) requires `booked_n >= 2` before firing. For all trades using industry defaults (not Dave's number), add: "Based on a ~$X industry estimate — update your avg in Settings to see your real number." Include the disclaimer in the SMS itself, not just the dashboard.

### P1-B — "Set up in a day" claim conflicts with A2P 1–10 business-day vetting reality
**Risk:** pricing.html, help.html, company.html, and setup.html all say "most contractors live within a day." The blueprint (§7) clearly states: "TCR carrier vetting (1–10 business days) is external and cannot be accelerated. The honest ceiling on time-to-value is ~1–3 days, not zero." The current copy implies full functionality in 24 hours. A contractor who pays $99, completes setup, and waits 5 days for texting to activate will feel deceived.
**Where it bites:** The setup.html A2P pending copy says "usually within a day" which is optimistic. For LLC/Standard-brand registrations, 1–10 days is the real range. For voice-only (no SMS yet), "live within a day" is true for call screening but NOT for text-back, which is the primary value prop.
**Honest rule:** All "set up in a day" copy must be qualified: "Call screening is live instantly. AI text-back activates within 1–3 business days after carrier registration (a one-time step)." The setup page A2P pending note should say "usually 1–3 business days" not "usually within a day." Never imply full text-back is same-day.

### P1-C — Placeholder testimonial on landing.html is visible to the public; Stars + blockquote format looks like a real review
**Risk:** `landing.html:113` currently shows `"[ Your first customer's quote goes here ... ]"` inside a `<blockquote>` with `★★★★★` stars and a fake attribution line `— Name, Business`. The bracket notation makes it obviously a placeholder in dev, but stars + quote structure reads as a real review pattern to visitors. If a builder replaces the bracket text with "realistic-sounding" placeholder copy to clean up the page before a real customer exists, it becomes a fabricated testimonial.
**Where it bites:** FTC regulations require testimonials to be from real customers. An invented quote in a `<blockquote>` with 5-star attribution violates FTC endorsement guidelines regardless of intent.
**Honest rule:** (1) Remove the testimonial section from landing.html entirely until a real customer consents to a quote. Do not substitute a "realistic placeholder." (2) Alternatively, keep the section but replace the star/blockquote/attribution structure with a "Coming soon" card matching customers.html's honest framing ("Real results from a real crew will land here once they're live and happy to share"). (3) Any real quote must be: verbatim, consented in writing, attributed by name (first name + trade or location minimum), and not edited to be more favorable.

### P1-D — Weekly digest ROI block has no A2P delivery-state awareness; could report "3 calls recovered" for sends that never reached customers
**Risk:** `convos.digest_email()` (planned S3 extension at `convos.py:298`) will call `db.analytics(bid, 7)` and report leads + booked counts. But `leads` rows are written when a call is missed and a lead is created — regardless of whether the text-back was `delivered`, `simulated`, or `blocked`. A pre-A2P tenant will see digest emails claiming "3 calls recovered this week" when in fact the texts were never sent (A2P pending, all sends simulated or blocked).
**Where it bites:** The digest is the "while you worked" proof surface. If the proof is false (sends never went out), the owner's trust is built on a false foundation.
**Honest rule:** Before adding the ROI block to the digest, add a check: if any lead in the window was created while `a2p_status != 'approved'`, the ROI block must say "X callers texted back (pending carrier activation — texts send automatically once live)" rather than implying the engagement happened. Alternatively, gate the ROI block entirely until A2P is approved for the tenant.

### P1-E — The Dispatcher Call has no TwiML endpoint yet (it's planned, not built)
**Risk:** The blueprint treats the Dispatcher Call as a "shared retention primitive" in Phase 4. `messaging.place_call()` exists, the sentinel TwiML endpoint exists (`/webhooks/twilio/voice/sentinel-twiml`), but there is NO Dispatcher Call TwiML endpoint that reads the caller's words to Dave and offers "press 1 to connect." The F09-FINAL plan describes it; the code does not implement it. A builder who wires the urgent-alert path to `place_call` with a missing or wrong TwiML URL will either fail silently (Twilio returns an error) or play a broken/empty call to Dave.
**Where it bites:** The "jaw-drop" marquee moment (§5 item 2) requires this to actually work. If it ships as a stub that calls Dave and then plays silence, it's worse than not having it.
**Honest rule:** The Dispatcher Call TwiML endpoint (`/webhooks/twilio/voice/dispatcher-twiml`) must be fully implemented and tested (including "press 1 to connect" bridging) before the urgent-alert path is wired to `place_call`. Until then, the urgent path stays SMS-only. No claiming "we'll call you" until this actually calls.

### P2-A — Production domain: don't claim trust upgrade before DNS resolves
**Risk:** Phase 4 includes "production domain (kills `*.onrender.com` trust hit)." The trust hit only disappears when DNS resolves, SSL provisions, and the old URL redirects cleanly. The risk is shipping a site that says "firstback.app" on the marketing pages before the domain is actually pointing there, or breaking the existing `ringback-gixe.onrender.com` during cutover.
**Honest rule:** Keep `ringback-gixe.onrender.com` live until `firstback.app` is confirmed resolving in all major DNS resolvers (check with `dig` from multiple regions). Never redirect old→new before both are live. The `render.yaml` custom domain config must be committed and verified before any marketing copy references the new domain.

### P2-B — Month-over-month delta shows on day 2 of a billing period (suppression guard not yet in code)
**Risk:** F12-FINAL specifies "only show delta after 14+ days" but `db.analytics()` has no such guard today. A builder implementing M6 who forgets the 14-day gate will show "+3 more estimates than last month" on day 2, which is noise masquerading as signal.
**Honest rule:** `db.analytics_compare()` must gate: `if current_period_days < 14: return {"delta": None, "show": False}`. The UI must respect `show=False` and display nothing, not a zero or a dash.

### P2-C — Digest "dead-man's switch" integrity: if SF-3 ticker dies, the digest stops and the owner has no signal
**Risk:** The blueprint (§7) correctly identifies the weekly digest as the human-facing dead-man's switch. If the ticker dies (SF-3), `app.py:678` (`/tasks/digest`) never fires, the digest stops, and Dave notices nothing until he eventually asks why he stopped hearing from FirstBack. There's no "the digest didn't fire" alert separate from the digest itself.
**Honest rule:** The heartbeat row (`last_tick_utc`) and the `/health/ticker` endpoint (SF-3) must be deployed before the digest ROI block is added. If the ROI block is the primary "proof that FirstBack is working," then the system that delivers the proof must have an external health check. Phase 4 must confirm SF-3 is wired (Render cron → `/tasks/run-due`) before treating the digest as a reliable signal.

### P2-D — Voice callback pricing page: "currently in beta and rolling out on Pro and Crew" — but voice may not be deployed
**Risk:** `pricing.html:69` says voice callback is "currently in beta and rolling out on Pro and Crew." `settings.html:50` says "Voice callback is live" when `VOICE_PUBLIC_URL` is set. The blueprint (§7) says voice "must not be sold until streaming + premium-voice ear-test + the 7-check quality gate all pass." If voice is not deployed (no `firstback-voice` service), these copy fragments are promises that can't be fulfilled. A Pro-tier subscriber who pays $199/mo expecting voice gets nothing.
**Honest rule:** Until voice is actually deployed and passing the quality gate, pricing.html must not imply it's "rolling out" on Pro. Change to "Voice callback coming soon — on Pro and Crew when it ships." The settings.html "live" label should only render when `VOICE_PUBLIC_URL` is confirmed responding to a health check, not just when the env var is set.

---

## Honesty Rules Summary Table

| Rule | Applies to | P-level |
|------|-----------|---------|
| Filter analytics by `source='missed_call'` only | ROI calc, digest, milestone | P0-A |
| Ship trade defaults (S1) before any ROI surface | Analytics tile, digest, milestone SMS | P0-B |
| Add `roi_milestone_sent_at` migration before M5 | Milestone SMS idempotency | P0-C |
| Gate "Calling you now" on `status=='placed'` only | Dispatcher Call + voice callback | P0-D |
| Milestone SMS: require `booked_n >= 2` for high-variance trades with default avg | Roofing + HVAC | P1-A |
| "Set up in a day" → "call screening instant, texting 1–3 days" | All marketing copy | P1-B |
| Remove or honestly frame the 5-star placeholder testimonial | landing.html | P1-C |
| Gate digest ROI block on A2P approval per tenant | digest_email() | P1-D |
| Build Dispatcher TwiML endpoint before wiring urgent-alert to place_call | Alerts urgent path | P1-E |
| Keep old host live until new domain verified | Domain cutover | P2-A |
| Gate month-over-month delta on 14+ days | analytics_compare() | P2-B |
| Confirm SF-3 ticker health before treating digest as reliable signal | Digest as dead-man's-switch | P2-C |
| Voice "rolling out" copy must match actual deployment state | pricing.html, settings.html | P2-D |

---

## The Single Riskiest Item

**P0-A (attribution filter bug in `db.analytics()`)** is the riskiest because it is a live, unfixed bug in production code that will silently overclaim ROI for any tenant who has manually-added leads. It requires a one-line fix (`AND source='missed_call'`) but has multi-surface consequences: the dashboard, the digest email, and the milestone SMS all derive from `db.analytics()`. Every ROI surface built on top of the current function is overclaiming by construction.

---

*Written by Pre-Build Planner 2 of 3 (Honesty / Risk Lane). Detail to file; builder should read P0 items before writing any code.*
*Code references verified against: `db.py:2500–2549`, `messaging.py:209–231`, `alerts.py:101–139`, `app.py:2230–2257`, `convos.py:298–313`, `templates/landing.html:113`, `templates/customers.html`, `templates/setup.html:128–154`, `templates/pricing.html:10,69`.*
