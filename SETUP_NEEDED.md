# Setup Needed — to make every claim fully live & true

The site truth-audit (see `SITE_TRUTH_AUDIT.md`) made the copy honest about what's live
vs. gated. This is the list of things **you** must do to turn the honest-but-gated /
beta / placeholder states into fully live ones. Integration credential steps live in
`USER_TO_DO.md`; this file tracks the truth-audit follow-ups.

## Autonomy Blueprint — Foundation phase (2026-06-18) — OWNER OPS to flip it live
The foundation CODE is built + merged (ticker heartbeat + `/health/ticker`; platform owner-alert
channel `ALERT_FROM_NUMBER` + signup auto-fills alert prefs; public `/demo` on a sandbox business;
monitor-mode disclosure). 23/23 test files green. These items are **yours** — code can't do them:
- **Render env:** `FIRSTBACK_RUN_TICKER=1`, `FIRSTBACK_TASKS_SECRET=<rand>`, `FIRSTBACK_TOKEN_KEY=<rand>`
  (encrypts OAuth tokens at rest — code already honors it), `SMTP_HOST=smtp.resend.com`,
  `SMTP_FROM=alerts@firstback.app` (+ key), `ALERT_FROM_NUMBER=<platform Twilio # for owner alerts>`.
- **Reconcile all `FIRSTBACK_*` env + the DB path in Render BEFORE the next deploy** (the rename
  changed env-var names + the DB filename; mismatch = the app falls back to defaults / resets the DB).
- **External Render cron → `POST /tasks/run-due` every 60s** with header `X-Tasks-Secret` (the
  in-process ticker is only a fallback; `/health/ticker` now reports if it goes stale >10 min).
- **Create the Resend account** + verify the `firstback.app` sending domain.
- **Voice decision (product call):** deploy `firstback-voice` or demote it from the Pro feature list
  until deployed (don't sell a text fallback as a $199 feature).

Full spec + the rest of the build order: `~/apps/COO/firstback-blueprint/phase0/PHASE0-SPEC.md` and `AUTONOMY-BLUEPRINT.md`.

## Autonomy Blueprint — Phase 1 (2026-06-18) — OWNER OPS to flip it live
Phase 1 CODE is built + merged on `staging` (Stripe billing + subscription gating; auth password-reset + SECRET_KEY/seed hardening + login rate-limit; LLM cost spine = Sonnet/Haiku + prompt caching + token ledger + per-tenant dollar cap; usage "conversations" fuel gauge; SF-6 quiet-hours backstop + START re-opt-in). 28/28 test files green; billing/auth security spot-check passed. Owner ops to flip live:
- **Stripe account (test mode first):** set `STRIPE_SECRET_KEY=sk_test_…`, `STRIPE_WEBHOOK_SECRET=whsec_…`, and create **6 Price IDs** — monthly `STRIPE_PRICE_{STARTER,PRO,CREW}` ($99/$199/$399 /mo) **+ annual (20% off, billed yearly) `STRIPE_PRICE_{STARTER,PRO,CREW}_ANNUAL` ($950 / $1,910 / $3,830 per year)** — and wire them in. (Code is built + tested against mocked Stripe; nothing hits Stripe until these are set.) Note: annual subscribers still get the SAME monthly conversation allotment — the fuel gauge refills every calendar month regardless of billing interval.
- **Set `CLAUDE_DAILY_COST_CAP_USD`** if you want a per-tenant daily AI spend cap other than the $1.00 default.
- **`be-audit` before any production deploy** of the `/webhooks/stripe` + `/auth/reset` paths (money/security gate). Minor P2: wrap `get_llm_spend_today` to fail-open.
- Phase-0 ops above still pending (Render env, cron, Resend, voice decision).

## Autonomy Blueprint — Phase 2 (2026-06-18) — OWNER OPS to flip it live
Phase 2 CODE is built + merged on `staging` ("core loop is trustworthy"): SF-4 SMS delivery receipts + async retry (30s/2m/10m, cap 3, owner alert on final fail); SF-5 per-business timezone (`biz_tz` reads the stored IANA, auto-read from Google Calendar on connect, NPA area-code fallback) threaded through reminders/slots; SF-7 forwarding **sentinel verification** (both dial AND catcher modes now place a real call and confirm forwarding ONLY when it rings back inbound — no more self-attestation) + weekly health probe; F04 Google write-loop closed (persist `google_event_id`, cancel/patch, all-day-event fix, new businesses default 60-min buffer, first-turn bookings now create the event + reminder); F03 booking-brain guards (12-turn cap with phone handoff, price-quote scrub, length cap, double-booking recovery); F05 `test_reminders.py` (the docstring's "unit-tested" claim is now TRUE) + morning-of reminder + RSVP keyword classification; reliability `@app.errorhandler` 404/500 pages. **35/35 test files green** (28 baseline + 7 new); be-audit-style pass on the new external surfaces clean (status webhook is Twilio-signed; sentinel call is `@login_required`; retries bounded; alerts carry IDs not PII). Owner ops to flip live:
- **`FIRSTBACK_PUBLIC_URL` must be set in Render env** — without it the SMS StatusCallback URL is blank (no delivery receipts → no retry) and the sentinel TwiML URL can't be built (forwarding falls back to the labelled manual confirm). This is the single env that activates SF-4 + SF-7.
- **Timezone (SF-5):** auto-read from the owner's primary Google Calendar on connect; before connecting, the owner can set it in Settings. NPA area-code fallback covers ~60 common US codes; everything else falls back to the global `FIRSTBACK_TZ`.
- **Forwarding (SF-7):** the owner still dials the carrier star code once (irreducible). FirstBack now VERIFIES it — tapping the forwarding step fires a sentinel call (to the cell in dial mode, to the owner's `alert_sms`/signup cell in catcher mode); the phone should ring within ~30s and the wizard flips to confirmed only when the call rings back through. A weekly probe re-checks it and alerts (`forwarding_lost`) if a carrier change silently breaks it.
- **Calendar (F04):** Google Calendar must be connected for live event create/cancel; until then bookings persist locally only.

## Autonomy Blueprint — Phase 3 (2026-06-18) — OWNER OPS to flip it live
Phase 3 CODE is built + merged on `staging` ("time-to-first-value": SF-8 automated A2P + done-for-you onboarding + auto-flush): the **Twilio Trust Hub WRITE API** client (`create_a2p_brand` / `create_a2p_messaging_service` / `create_a2p_campaign`, Customer-Care use case, gated on a separate `trust_hub_configured()` so it never fires real billable submissions when only base Twilio creds are set); the **EIN fork** (`business_type` sole_prop|llc — sole-proprietors are no longer blocked at the profile step, and a sole-prop submission collects ZERO EIN); `connections.submit_a2p` orchestration (Path A sole-prop / Path B LLC); a per-contractor **micro-site** route `/c/<slug>` (contractor identity only, no FirstBack branding — the original-denial fix) + the `/privacy` SMS-opt-out section; `/api/places/lookup` business-name prefill; **auto-flush**: blocked text-backs are persisted to a `blocked_sends` table and replayed when A2P approval lands (`a2p_sync` transition hook), through a 6-rule safety gate (6h freshness window, opt-out, quiet-hours, dedupe, oldest-first cap 50, conversation-coherence). **40/40 test files green** (35 + 5 new); end-to-end un-stubbed flush integration verified; honesty/PII audit clean (only `a2p_sync` ever sets `approved` — never a 200 response; EIN never logged; micro-site carries no FirstBack branding/smart-quotes). **The cardinal honesty rule holds: a Twilio CREATE 200 = "submission accepted", NOT "approved" — only the status poll flips a tenant live.** Owner ops to flip live:
- **`TWILIO_TRUST_PRODUCT_SID`** (+ optional `TWILIO_A2P_RESELLER_SID`) in Render env — the gate that turns the WRITE API from simulated to live. Until set, `submit_a2p` returns "simulated" and submits nothing.
- **`firstback.io` domain + `*.firstback.io` wildcard DNS → the Render app** — so `<slug>.firstback.io` micro-sites resolve (the brand opt-in URL TCR inspects). The `/c/<slug>` route works today; the subdomain needs the DNS.
- **Cloudflare Email Routing catch-all `@clients.firstback.com`** — so the per-contractor authorized-rep email (`<slug>@clients.firstback.com`) can receive Twilio's verification.
- **`GOOGLE_PLACES_API_KEY`** — optional; enables business-name prefill at signup (returns `{}` / Dave types manually when unset).
- **DEFERRED — the 2 live confirmations before scaling (F14):** (HC-1) one real LLC submission to confirm a `<slug>.firstback.io` page passes TCR for a contractor brand (Likely, not Confirmed); (HC-2) one Twilio CSP call to confirm `<slug>@clients.firstback.com` satisfies the Authentication+ email rule for Standard brands. Also DEFERRED (HC-3): the exact Twilio sole-prop Starter-brand payload + OTP mechanics — confirm with the Heritage dogfood submission. Code is built + mock-tested for all three; none can be PROVEN without a live submission, and nothing claims they are.
- **Heritage House dogfood:** run the first real sole-prop submission (reply YES to the OTP) to validate Path A end-to-end before charging customers.

## Autonomy Blueprint — Phase 4 (2026-06-18) — OWNER OPS to flip it live
Phase 4 CODE is built + merged on `staging` ("convert & prove"): **F12 ROI/analytics made honest** (fixed a live over-count bug — `db.analytics` now counts only `source='missed_call'` leads, not manually-added ones) + **trade-based job-value defaults** (`TRADE_JOB_VALUE_DEFAULTS`, $800 floor) + **ROI multiple** + an `avg_source` label ("based on your average" vs "industry estimate") — revenue is always shown as an ESTIMATE (pipeline, never collected cash); **milestone SMS** ("FirstBack booked ~$X — about Nx its cost") gated on A2P-approved + ≥2× + idempotent (`roi_milestone_sent_at`); **Show-Up-Prepared briefing** (the booking alert now carries address/project/summary when present — a format extension, not a 2nd text); the **Dispatcher Call** (urgent caller → owner's phone rings, hears the caller's exact words, press-1 to connect; rate-limited per-lead; never claims a call it didn't place); **ROI block in the weekly digest** (gated on A2P-approved); and honest **site/CTA fixes** (removed Jobber/Housecall-Pro pills, marked AI voice "coming soon/beta", removed the placeholder testimonial). **46/46 test files green** (40 + 6 new); review caught + fixed a real per-lead-vs-per-business rate-limit bug; honesty pass clean (no cash claims, no invented testimonials, ROI gated on real delivery). Owner ops:
- **Production domain (the `*.onrender.com` trust fix) — pure OWNER-OPS, zero code:** register the domain → add it as a Render custom domain → point DNS (CNAME) → set `FIRSTBACK_PUBLIC_URL` to the new domain → re-point the Twilio voice/SMS webhook URLs LAST. Note: `FIRSTBACK_PUBLIC_URL` also drives SF-4 delivery receipts + SF-7 sentinel — verify those still work after the swap. Keep `ringback-gixe.onrender.com` reachable during cutover.
- **Weekly digest cron:** the ROI digest block is inert until an external Render cron hits the digest fanout weekly (same pattern as `/tasks/run-due`). Wire it.
- **Dispatcher Call needs `FIRSTBACK_VOICE_URL` set** (the public base for the TwiML) — the same env the AI-voice feature uses; until set, urgent leads still get the SMS alert (honest fallback), just no live owner call.
- **The marketing site's real proof content** (testimonials/founder/ROI numbers) is a CONTENT decision — supply real, consented copy before re-adding any testimonial or stat. No invented quotes.
- **`avg_job_value`:** each owner can set their real average in Settings for an exact ROI number; until then the trade default is used and clearly labeled an industry estimate.

## Command Center — Phase 0 (the "honest hands") — what's live vs. gated
The `/dashboard` command center was hardened (see `BRAIN.md` for the full vision). Phase 0
needs **no new external accounts**; it works keyless in the demo brain. Notes:
- **Honest confirm:** before any text reaches a customer the owner now sees the exact
  recipient, the editable body, and a live/test/opt-out badge. The badge keys off the Twilio +
  A2P setup already tracked below — until those are live it correctly reads **"Test mode — not
  sent for real yet."** Nothing new to configure for the safety itself.
- **Conversation memory + "text her back":** server-side, no setup. Works in `demo`.
- **Multi-step tool-calling brain:** engages when a real LLM key is set (`ANTHROPIC_API_KEY` +
  `FIRSTBACK_PROVIDER=claude`, or `MINIMAX_API_KEY` + `FIRSTBACK_PROVIDER=minimax`); with no key the
  deterministic keyword router runs everything (honest, single-step). **Verified live against
  MiniMax** (multi-step reads work end-to-end); the Claude path is verified against the official
  tool-use API reference. **Reliability note:** MiniMax does not reliably *invoke* write tools
  (it tends to talk about texting instead of calling the tool), so **clear write intents (text /
  book / cancel / scheduling change) are routed through the deterministic router even when an LLM
  is keyed** — they gate reliably. Reads, chat, and fuzzy phrasing use the LLM loop. The confirm
  gate is never bypassed on either path. For the richest LLM behavior (multi-step writes like
  "book John then text him"), use Claude (`ANTHROPIC_API_KEY`).
- **Rate limit (new knob):** `FIRSTBACK_ASSISTANT_RPM` (default **60** assistant turns/min per
  tenant) caps runaway LLM cost/abuse. Raise/lower in env; no action needed for normal use.
- **CSRF:** the assistant POSTs now require the per-session token (auto-wired in the page). No
  setup; just don't strip the hidden `csrfToken` field from `command.html`.

### Phase 1 (booking + search + money-framed leads) — no new setup
The chat can now **book / cancel estimates, show open windows, flag urgent, and find a lead by
name or number** (all tenant-scoped; the slot/appointment is pinned at confirm so you book exactly
what you saw). No new accounts or keys. One thing to set for the **dollar framing** on the lead
card ("3 open, ~$8,400 on the table"): the owner's **average job value** in *Settings* (or it just
shows leads without the money line until then). Booking does **not** text the customer — it holds
the slot and offers to send a confirmation, which still goes through the honest confirm.

### Phase 2 (Vic shows up: briefing + tappable feed + persona + real-time) — no new setup
The command center is now proactive. Live, keyless, works in the `demo` brain:
- **Morning Briefing** — a server-rendered, money-ranked card on `/dashboard`: what needs
  the owner now (urgent → replied → today's estimates → new leads), one action each,
  read in ~12 seconds. Honest by construction (composed from real leads/estimates, never a
  guessed name; quiet + empty when there's nothing yet). Also summonable by chat
  ("what should I focus on?", "catch me up").
- **Tappable ambient feed** — each briefing item is a one-tap action (text the lead, show
  booked estimates, show leads). Keyboard-focusable, ≥44px targets, sr-only status words so
  tone is never conveyed by color alone. Zero-JS fallback: items are still readable.
- **The Vic persona** — one foreman voice woven through every LLM reply path (lead with
  money, own the recommendation, never perform, never make up a customer detail). Engages
  when an LLM key is set; the keyword floor speaks the same way. No setup.
- **Real-time refresh (poll baseline)** — `GET /api/feed` returns the current briefing +
  chips + a content signature; the page polls every 25s (and on tab focus) and refreshes the
  feed **in place without wiping the chat**. A just-missed call now surfaces without a reload.
  Read-only, tenant-scoped, login-gated. No setup.

**Documented next step (NOT built yet) — lower-latency + away-case notifications:**
- **SSE** (server-sent events) would replace the 25s poll with a push stream for instant
  updates. Needs a streaming-capable worker (gunicorn `gthread`/`gevent` + `X-Accel-Buffering:
  no`); the poll baseline above is the honest, working fallback until then.
- **Web push** (notify the owner when the app is closed) needs **VAPID keys** (operator
  generates a keypair, sets `VAPID_PUBLIC_KEY`/`VAPID_PRIVATE_KEY`), `pywebpush`, a service
  worker, and a `push_subscriptions` table. This is real user setup + a service-worker build,
  so it's deferred and called out here rather than half-built. SMS/email owner alerts
  (`alerts.py`) already cover the away case in the meantime.

## Phase 3 — The growth engine (convert + grow)
Vic now hunts new business: a money-ranked **plays** feed computed from signals that already
exist (`growth.py`), surfaced via the chat ("what plays do I have", "money left behind",
"grow my business", "get reviews") and the **Money Left Behind** stat. Each play is one tap
to a **gated** draft text — the owner sees exact recipient + body + opt-out + live/test and
approves before anything sends (same confirm as everywhere else). Live, keyless:
- **Convert:** compliant **review requests** (asks EVERY completed-job customer — the trigger
  references no sentiment/rating signal; review gating is illegal, FTC + Google) and **quote
  follow-ups** (a quiet quote 24h–30d out).
- **Grow:** **reactivation** (cold quotes 30d+), **win-back** (past customers 12–18 mo),
  **referral** (just-wrapped jobs), **membership** (repeat, lower-ticket customers), plus
  owner-initiated **seasonal**, **density** (3+ jobs in a parsed zip / 14 days), and
  **financing** prompts (over the trade threshold).
- **Auto-pause:** booking a lead cancels its pending follow-up/reactivation touches.
- **Honest proxies (no new external data):** "job completed" ≈ a booked appointment whose
  day has passed; "ticket value" ≈ your **average job value** (set it in Settings for the
  dollar figures); "zip" ≈ parsed from a lead's address (skipped when absent).

### What you must set / decide for Phase 3
- **Your Google review link** — set `review_link` on the business so review-request copy links
  straight to your Google review page (until set, the draft shows a `[your Google review link]`
  placeholder the owner fills before sending). No UI toggle yet; set via Settings/DB.
- **Auto-send opt-in (`growth_on`, default OFF)** — the plays feed + one-tap gated sends are
  always live. **Auto-queued** growth texts (the background scheduler enqueuing reviews /
  follow-ups on a schedule) are an explicit per-business opt-in via `db.set_growth_on(biz, 1)`,
  and even then they only *simulate* until Twilio + A2P are live (see below). This is the
  safety so nobody blasts customers by accident. No UI toggle yet.
- **The scheduler** — auto-touches fire from the same `POST /tasks/run-due` cron as reminders
  (`reminders.tick_once` now also runs `growth.scan`). Already wired; needs the cron hitting
  `/tasks/run-due` in prod (same as reminders/followups).

### Deferred — needs a Google Business Profile (GBP) connector (NOT built)
- **Negative-review rapid response** (draft a reply to a <3-star review) and **before/after +
  GBP post** require reading/writing Google Business Profile review + post data. There is no
  GBP integration yet (only Google Calendar/Contacts OAuth). These are intentionally **not**
  built rather than faked. To add: a GBP OAuth connector + a `reviews` store, then the draft
  play lights up. Tracked here so it's not a surprise.

### Local testing (no Render, no keys): `./run_local.sh`
Spins up an **isolated** instance at `http://localhost:8800` on its own `local_test.db` (your
real `firstback.db` is never touched), keyless demo brain, seeded with a login
(`owner@firstback.local` / `test1234`) and 3 sample leads. Try: *"show my leads"* →
*"text the second lead saying running 10 minutes late"* to see the honest confirm + anaphora.

## Phase 4 — Polish & soul (streaming, brain, mobile/field, a11y, voice, trust)
Phase 4 made the command center feel like an employee. Live, keyless where possible; the
honest live-vs-deferred status:

- **Brain → Claude (recommended default).** `config.PROVIDER` now defaults to **`claude`**.
  It only engages once **`ANTHROPIC_API_KEY`** is set; with no key it falls back to the demo
  brain (safe no-op locally) — set `FIRSTBACK_PROVIDER=minimax` to use MiniMax instead. To run
  Claude for real, set `ANTHROPIC_API_KEY` + `FIRSTBACK_PROVIDER=claude` (`CLAUDE_MODEL`
  defaults to `claude-opus-4-8`). **Status: the Claude path — including the new streaming
  branch — is code-verified against the official Messages API reference but NOT live-fired
  (no key in this environment).** The demo + MiniMax paths are exercised.
- **Streaming replies (`/assistant/stream`, real SSE).** A genuine `text/event-stream`
  sibling of `/assistant`: identical auth + CSRF + rate-limit + memory + the **confirm gate**
  (a write still stops at a `pending_action` you approve). Each frame is `data: {json}` — text
  `delta`s, then one `done` carrying the same `{reply, cards, pending_action, coach}` shape.
  **Honest scope:** tokens stream **live from the model only on the Claude path**
  (`messages.stream`); for the demo / MiniMax / keyword-routed paths the server-computed reply
  is streamed **chunked** over the same SSE transport (a keyword router has no tokens to
  stream). The non-streaming `/assistant` stays the fallback (used automatically when the
  browser can't stream, when reduced-motion is set, or if a stream fails before any text).
  **Prod note:** SSE needs a streaming-capable worker (gunicorn `gthread`/`gevent` +
  `X-Accel-Buffering: no`, already set on the response) — the same requirement the Phase 2 SSE
  note tracks. On a single-threaded sync worker the stream still works but ties up the worker
  for the turn.
- **Daily LLM budget (new knob).** `FIRSTBACK_ASSISTANT_DAILY` (default **400** LLM-backed
  turns/tenant/day) caps cumulative cost on top of the per-minute `FIRSTBACK_ASSISTANT_RPM`.
  Past it the assistant **degrades to the keyword floor** (booking, lists, and the confirm
  gate all still work; only the fuzzy/chat LLM path is withheld until the window rolls over) —
  it does not hard-block. No action needed for normal use.
- **Push-to-talk voice — Web Speech API only, no new infra.** A mic button in the command bar
  dictates into the bar so the owner can read/edit before sending (**never auto-sends**). It
  is hidden automatically when the browser has no `SpeechRecognition` (e.g. most desktop
  Firefox). Nothing to configure; no server-side voice service involved (that's the separate,
  still-deferred `firstback-voice` beta below).
- **Honest/gated orb + a11y + mobile/field.** The orb's old "speaking" state is renamed
  **"responding"** (there is no audio); the WebGL orb is gated off (static glow) for
  reduced-motion, **Save-Data**, and a **low/unplugged battery**. Mobile: autofocus dropped on
  touch (no keyboard-pop over the briefing), ≥48px tap targets, a **Save-Data/`prefers-contrast`
  sunlight** treatment, and an **offline banner**. No setup.
- **The trust headline** ("We don't sell your leads. We don't share your customers. We don't
  text anyone you haven't approved.") is surfaced on the command center. It's a promise the
  product keeps by construction (per-tenant isolation + the confirm gate) — keep it true.
- **Delight moments:** the Morning Briefing, the "replied/waiting" nudge, and **The Win**
  (a booked estimate shown with `~$<avg job value> booked`, only when the value is set) are
  tuned for restraint. **The 5-Star** (review thank-you) and **The Catch** (slot-collision
  warning) are honestly **not built** — the 5-Star needs the Google Business Profile connector
  (see Phase 3 deferred), and collision detection is a future slice, not faked.

## Phase 5 — deep-audit punch-list (none block local/simulated use; close before LIVE sends)
A 5-agent deep audit of the whole command center (2026-06-17) found **zero P0 regressions**; the
gate, tenant isolation, and review-gating compliance are sound. Fixed in code: review-request now
only fires on jobs ≤90 days old; a simulated send no longer renders with a green "success" tint;
`execute()` returns the full `{reply,cards,pending_action,meta}` shape; defense-in-depth tenant
scoping added to `mark_lead_urgent` / `set_suggestion_status`; a batch of frontend a11y fixes
(WCAG-AA label contrast, ≥44px chip/button tap targets, expired-session message on 403, mic
stop on navigation, confirm-button focus); and two pre-existing marketing-copy honesty carryovers
(the "live AI voice" lede on `/product`, the invented testimonial on the login page). These remain
as a punch-list — **none affect the current simulated/local state**, but address before real
customer sends / prod cron:
- **Growth auto-send + the trust headline.** Enabling `growth_on=1` makes the background scheduler
  send growth texts without a per-send approval — which makes the "we don't text anyone you haven't
  approved" promise conditional. `growth_on` is **OFF by default with no UI toggle**, so this can't
  happen from the app today. **Before shipping a `growth_on` UI toggle, add a per-send/per-batch
  approval step** (or an explicit opt-in disclosure that carves scheduled automations out of the
  headline).
- **A failed growth touch currently can't be retried.** The dedupe index keeps one touch per lead
  per kind for any status except `canceled`; a touch that lands in `failed` (a Twilio error once
  live) holds the slot and blocks re-queue. Only relevant once Twilio + A2P are live. Fix: exclude
  `failed` from `uniq_growth_touch_per_lead` and `growth_touch_index` (index migration), or cancel
  a failed touch so the slot frees.
- **Cancel-then-reminder ordering.** `cancel_appointment` cancels the appointment, then its reminders
  on a separate connection. The ticker re-checks appointment status before sending, so **no double
  text can go out** — but a canceled appt's reminder can show "skipped" instead of "canceled", and a
  crash between the two leaves an orphaned reminder row. Minor; fold the reminder-cancel into the
  same transaction when convenient.
- **Rate-counter window edge.** At the exact second a rate window rolls over, a concurrent prune can
  reset the next window's counter, letting a few extra turns through. It's a cost guard, not a
  security control; impact is negligible. Optional: wrap `incr_rate` in `BEGIN IMMEDIATE`.
- **Cron secret required in prod.** `/tasks/run-due` (reminders + growth scan) returns 403 unless
  `FIRSTBACK_TASKS_SECRET` (and `FIRSTBACK_INTERNAL_SECRET` for internal calls) are set in the prod
  env. Code fails closed when unset — so set them, or the scheduler silently never runs.

## Go-Live wizard (`/setup`) — contractor self-serve connection
The **Go Live** page (`connections.py` + `templates/setup.html`) takes a contractor from
signup to live missed-call text-back without a shell or the Twilio console: business profile
+ A2P intake → buy/attach a number (auto-wires webhooks) → submit A2P registration with live
status → carrier call-forwarding code. Honest by construction (driven by
`compliance.launch_blockers` — never "live" until the number is bound, A2P is **approved**,
and forwarding is confirmed).

**Automated:** number provisioning + webhook wiring, A2P status **sync** from Twilio (on view
+ via the `/tasks/run-due` cron), the carrier star-code guide, and the go-live gate.

**Still operator/concierge (v1 — by design):**
- **Server Twilio credentials** (`TWILIO_ACCOUNT_SID/AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
  `FIRSTBACK_PUBLIC_URL`) are set once in Render env by the operator (one shared account).
- **A2P brand + campaign submission** is concierge: "Submit for registration" marks the tenant
  `pending` and emails the operator (gated `mail` seam) the packet; the operator registers the
  brand+campaign in Twilio, then pastes the **campaign SIDs** into the wizard's *Installer*
  disclosure. `connections.a2p_sync` then flips the tenant to `approved` automatically. **Next
  phase:** submit brand+campaign via the Twilio Trust Hub API so even this is self-serve.
- **Carrier conditional-forwarding** happens on the contractor's phone (a star code); the
  wizard gives the exact code + tap-to-dial + a test-call check, but can't be set server-side.

## To drop the "beta" label on AI voice callback
- Deploy the voice service and set `VOICE_PUBLIC_URL` (re-add the `firstback-voice` service
  to `render.yaml` once it has a shared DB / write-relay — see `firstback-render-deploy`).
- Until then the copy correctly says **"in beta / rolling out on Pro and Crew"** and the
  product falls back to text. Don't sell it as fully included until it's deployed in prod.

## To roll out & tune call screening (the "phone screen")
- **Rollout mode** — `FIRSTBACK_SCREEN_MODE` (`off` | `monitor` | `enforce`, default `monitor`)
  is the **app-wide default**. It ships in **monitor**: it logs what it *would* screen (see the
  "Would screen" list + banner on the dashboard) but still texts everyone, so nothing real is
  silenced. Each business can also pick its own mode in **Settings → Call screening** (which
  overrides the env default; "Use the default" inherits it). When the monitor numbers look
  right, switch to **Enforce**. `off` is the instant rollback. Thresholds:
  `FIRSTBACK_SCREEN_HARD` (default 80) / `FIRSTBACK_SCREEN_MID` (45).
- **Optional paid robocall reputation (Tier 2)** — `FIRSTBACK_REPUTATION_PROVIDER`
  (`off` | `twilio_nomorobo` | `hiya`). `twilio_nomorobo` reuses your Twilio creds (Lookup +
  Nomorobo Spam Score add-on); `hiya` needs `HIYA_API_KEY`. Cached, fail-open. The free tiers
  screen spam without it.
- **Optional AI message screening (Tier 3)** — `FIRSTBACK_SCREEN_AI=1` reads a caller's first
  reply to bail on an obvious sales/robocall message (needs a real AI provider key; the demo
  brain always passes — fail-open).

## To make "works with Google Calendar / texts / email" say LIVE instead of simulated
- Add the credentials in `USER_TO_DO.md`: Twilio (SMS), Google OAuth (Calendar + Contacts),
  SMTP (email). Each is a gated no-op that simulates in-app until configured — the UI
  already says so honestly.

## To encrypt stored Google tokens at rest (recommended before prod)
- Set **`FIRSTBACK_TOKEN_KEY`** (any long random string, different from `FIRSTBACK_SECRET`).
  With it set, every Google access/refresh token is encrypted in the SQLite file
  (`token_crypto.py`: stdlib HKDF + SHA-256 keystream + HMAC, encrypt-then-MAC, marked
  `enc:v1:`). **Unset = safe no-op** so local dev and the current DB keep working.
- **Migration-safe / dual-read:** already-connected businesses are untouched — their
  legacy plaintext token still reads, and the next token refresh re-stores it encrypted.
  No code mutates the live DB. **One-time re-encrypt path:** have each connected business
  click **Disconnect → Connect** once (or just wait for the hourly refresh to roll them
  over). Rotating the key makes old-key tokens unreadable → those businesses reconnect once.
- See `USER_TO_DO.md → A2` for the step-by-step.

## Password reset (currently a gap)
- There is **no automated password-reset flow**. "Forgot password?" on the login page now
  routes to `/contact` (honest: you reset it manually) instead of a dead `#` link.
- To deliver self-serve reset: build a reset-token email flow (needs SMTP configured).

## Real customer stories (placeholders today)
- `/customers` now shows honest **placeholders** instead of invented testimonials.
- Replace with real, consented quotes once contractors are live. Keep the "no invented
  quotes" promise — get written sign-off before naming a business.

## Webinars (coming-soon today)
- `/webinars` no longer lists a fake dated event or "watch on demand" recordings.
- Before re-adding a live event: actually schedule it and wire "Get notified" (`/contact`)
  to a real list, or add a registration route.

## Pricing / "free" wording
- CTAs changed from "Sign up for free" to **"Get started"** (pricing is paid-only: $99/$199/$399).
- There is **no billing system** yet. Before charging, add billing (e.g. Stripe) so the
  "cancel anytime / no per-call fees" promises in pricing/FAQ are actually enforceable.
- If you want a real free trial, add trial copy + logic and the CTAs can say "Start free trial".

## Phase 6a hardening (2026-06-19) — OWNER OPS now ENFORCED by the code
The 6a pre-launch hardening shipped (commit 70f585c). Two new env requirements are now
**fail-fast in production** — the app will refuse to boot or warn loudly if they're wrong:
- **`FIRSTBACK_TOKEN_KEY` is now REQUIRED in production.** If `FIRSTBACK_HTTPS=1` (or
  `FIRSTBACK_ENV=production`) is set and `FIRSTBACK_TOKEN_KEY` is empty, boot raises a
  RuntimeError (was: silent plaintext Google OAuth tokens). Set any long random string,
  different from `FIRSTBACK_SECRET`. If you launch text-only (no Google yet), still set it
  to any non-empty value.
- **Set `FIRSTBACK_HTTPS=1` (or `FIRSTBACK_ENV=production`) on Render.** Without one of these
  the existing `FIRSTBACK_SECRET` fail-fast can't fire, the Secure cookie flag stays off, AND
  the new TOKEN_KEY guard above stays inert — i.e. the prod safety net only arms when one of
  these is set. This is the single most important env flag for the security posture.
- **Set all 6 `STRIPE_PRICE_*` IDs before the first real subscriber.** If a live invoice's
  price_id isn't in the env, the code now logs a **BILLING WARNING** to stderr and emails the
  owner (it no longer silently downgrades a Pro/Crew renewal to starter) — but the customer is
  still granted starter until you fix the env, so watch for that email/log.
- **Deferred (defense-in-depth, NOT a launch blocker):** the authenticated config forms
  `settings/growth_mode`, `setup/*`, `training/*` don't yet carry the `_csrf` double-submit
  token. They're protected by the active `SameSite=Lax` session cookie (cross-site POSTs drop
  the cookie) and password-change already requires the current password. A full form-CSRF
  sweep is a tracked follow-up; the customer-facing/graduation endpoints (call/lead family +
  growth-tray sends) ARE now CSRF-guarded.

## Phase 6b integration polish (2026-06-19) — one OWNER OP
The unified 8am owner digest, ticker LLM timeout, and an in-app stale-ticker alert shipped
(commit 587f326). One owner-ops item the code cannot do:
- **Add an external uptime monitor on `GET /health/ticker`** (Render's built-in health check,
  or UptimeRobot, alerting on `fresh:false`). The new in-app `tick_stale` alert fires when a
  tick runs AFTER a >15-min gap (i.e. the scheduler recovered) — but if the cron AND the
  in-process ticker are BOTH down, nothing runs to self-detect it. The external monitor is the
  only thing that catches total death. `/health/ticker` already returns `{fresh, last_tick_utc, age_s}`.
- (No new required env var. The digest is default-ON per tenant via `alert_on_daily_digest`;
  the owner can mute just the morning buzz in Settings without losing real-time lead/booking alerts.)

## Pre-deploy audit (2026-06-19) — owner-ops + fast-follows
A 10-agent pre-deploy sweep (`phase6/audit/PREDEPLOY-*`) verified the product and fixed 12
launch-blockers in code (commit cbfa24d). Remaining items that are NOT code:
- **HOW THE FIRST $99 IS COLLECTED — there is no wired "Subscribe" button in the UI.** The
  billing backend is complete (checkout session, webhook, grants — all tested) but nothing in
  the shipped pages POSTs to `/billing/checkout`. For launch, either (a) collect via a Stripe
  **Payment Link** out-of-band, or (b) wire a subscribe/upgrade button whose form includes
  `{{ csrf_token }}` (the routes are now CSRF-guarded). Decide this before "first $99".
- **External cron** → `POST /tasks/run-due` every 60s (gated fail-closed by `FIRSTBACK_TASKS_SECRET`).
  The in-process ticker works, but a Render dyno recycle delays scheduled sends until the next
  tick; the cron is the durable path. (Already in the go-live list.)
- **External uptime monitor on `/health/ticker`** (catches total scheduler death — see Phase 6b note above).
- **Fast-follows (not launch-blocking, documented in PREDEPLOY-SYNTHESIS):** harden the login
  rate-limit (it trusts `X-Forwarded-For`; add ProxyFix / email-keying); finish the
  defense-in-depth `_csrf` sweep on the remaining authenticated config forms (SameSite=Lax
  covers them today); make the Stripe `seen`+`mark` atomic (`INSERT OR IGNORE`) before running
  multiple workers.

## Optional cleanup flagged by the audit
- Delete the dead, unrouted `landing.html` (still contains the old Jobber/Housecall/Angi
  logos; harmless since it isn't served, but worth removing — roadmap already flags it).

---

## Autonomous build C–G (this session) — owner items to go live

The batches C/D/E/F/G shipped to **`staging` only** (13 commits, 76/76 tests green, 4 ship-gate
audit lanes passed). **Production (`main`) is untouched at `92aacde`.** Everything new is
**inert/opt-in by default** — nothing changes for the live Heritage tenant until you act.

### The one hard gate
- **Promote `staging` → `main`.** Held all session by design. Review the staging deploy on the
  RingBackv2 mirror, then give an explicit OK. Nothing reaches real callers until this.

### Turn on the new features (each is OFF by default)
- **Voicemail → lead** (Batch G): flip *Voicemail* on in **Settings → Website widget & voicemail**,
  AND enable Twilio recording/transcription on the voice number (the `<Record>` TwiML + the
  `/webhooks/twilio/voice/recording` webhook are wired; transcription is a Twilio config + cost).
- **Web-chat "Text us" widget** (Batch G): flip *Widget* on in Settings, paste the one-line embed
  shown there onto the site. Sends are A2P-gated (inert until A2P approved).
- **Google review tracking** (Batch E): set `GOOGLE_PLACES_API_KEY` (already used for setup
  autocomplete). Inert/no-op without it.

### Founder decisions (gate copy/policy I deliberately did NOT ship)
- 30-day **money-back guarantee** badge (real refund commitment).
- Lead the hero with **"it books the job" + the Vic briefing** framing (briefing is login-gated;
  hero claim would overclaim until it's a public feature).
- Bundle **paid caller-reputation** (Nomorobo/Hiya) into a paid tier — set
  `FIRSTBACK_REPUTATION_PROVIDER`; has a real cost + pricing implication.
- **Soft-overage billing** (charge per extra reply) vs the hard cap (the FAQ ships the soft
  "we'll alert you" version only — no $0.75 promise until billing is wired).

### Still NEEDS-OWNER (not built — external credentials)
- **Deposit link at booking** (plan 10-3): owner creates a Stripe **Payment Link**, pastes the URL.
- **GBP review dashboard** (plan 10-4): Google **business-scope re-auth** + enable the GBP API.

### NEEDS-ASSET / small deferrals
- Generate **`/static/og-default.png`** (1200×630, dark + wordmark + "Miss a call. We text back.
  They book.") then re-add the `og:image`/`twitter:image` tags (omitted to avoid a silent 404).
- Deferred enrichments: analytics **milestones-timeline + /api/roi_milestones** (07-3e), the
  **customer-book briefing card** hook (07-2e), the **briefing deep_link** backend (C8B frontend
  works for any `?lead_id`), and the **annual-toggle** checkout wiring (cosmetic until checkout).

_Note: the prior "finish the _csrf sweep on authenticated config forms" item is now partly done —
this session added the CSRF guard to `/settings/growth_mode` (a TCPA-sensitive mutation)._
