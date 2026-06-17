# Phase 3 — The Growth Engine (convert + grow)

> North star: `BRAIN.md` §4 (the three tiers) + §3 (the trigger engine) + §7 (the arc).
> This doc is the *how* for Phase 3. Build order is risk-ascending; every customer-facing
> outbound is **gated + simulated** until its connector is live (BRAIN.md §9).

## The unifying architecture — one "plays" engine
Instead of 10 bespoke features, Phase 3 is **one declarative engine**. Each growth feature is
a *play-type*: a trigger predicate over existing signals, a money estimate, a draft body, and a
compliance rule. `growth.plays(business)` evaluates them all and returns a money-ranked list of
opportunities; approving one queues gated outbound through the **existing confirm + messaging
seam** (exact recipient + body + opt-out + live/test — nothing new bypasses the gate). Multi-step
sequences (quote follow-up) persist in a `growth_touches` table; a scheduler tick materializes due
touches (simulated until Twilio/GBP are live).

Files: new `growth.py` (pure engine, leaf), `db.py` (growth_touches table + helpers, HOT/serial),
`assistant.py` (tools, HOT/serial), `app.py` (scheduler tick, HOT/serial), JS/CSS/template (cards,
leaf/parallel), `reminders.py` (reuse the scheduler pattern).

## Compliance (non-negotiable, baked into the engine)
- **Review gating is illegal (FTC + Google).** The review play asks **every** completed-job
  customer; it MUST NOT filter, branch, or rank by expected sentiment, and MUST NOT incentivize.
- Every outbound honors opt-out + A2P + simulated/live (the existing `messaging` seam).
- Never invent a customer detail; never claim "live" when simulated.

## Build order (risk-ascending, thin vertical slices)
- **3.0 Spine** — `growth_touches` schema + `growth.py` engine skeleton + the "Money Left Behind"
  forensic calc (pure, read-only). Safe first.
- **3.1 Convert — review engine** — compliant post-job ask (90–120 min), gated outbound. Highest value.
- **3.2 Convert — quote follow-up sequence** — 24h / 72h / 7-day touches, auto-paused on "booked".
- **3.3 Convert — negative-review rapid response** — <3-star → empathetic, context-aware draft, one-tap.
- **3.4 Grow — reactivation** — lost-quote (30/90-day) + database win-back (12–18 mo).
- **3.5 Grow — proactive prompts** — seasonal pre-peak, referral at close, density (3+ jobs/zip/14d),
  membership/maintenance upsell, financing over threshold, before/after + GBP post.
- **3.6 Surface** — the growth-plays card + "Money Left Behind" feed in the command center/briefing.
- **3.7 Scheduler tick** — fire due touches (gated/simulated) via the cron route.

## Done-when (audit criteria)
- `growth.plays()` returns money-ranked, honest opportunities; review play never branches on sentiment.
- Every approve → the honest confirm; nothing sends without it; simulated until live, said honestly.
- Tenant-scoped throughout; suite green; SETUP_NEEDED lists every connector (Twilio, Google Business
  Profile/reviews) and the cron.

## Subagent plan (15 Sonnet 4.6, where parallel is SAFE)
- Design burst (parallel, read-only): per-play specs + schema + compliance + money calc + copy.
- Build: orchestrator owns hot files (db/assistant/app) serially; leaf agents do cards/CSS/tests.
- Audit burst (parallel lanes): compliance/legal, security, tenant-isolation, correctness, honesty,
  tests, UI/a11y, copy/voice, contract-preservation. Fix serially, loop until green.
