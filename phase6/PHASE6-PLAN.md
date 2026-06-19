# Phase 6 — GO-LIVE + HARDENING (PLAN, for owner review)
**Date:** 2026-06-18 · Synthesized by Opus from a 4-agent planning wave (read-only).
**Inputs:** `phase6/PLAN-GOLIVE.md` · `PLAN-READINESS.md` · `PLAN-HARDENING.md` · `PLAN-INTEGRATION.md`.
**Base:** `staging` @ dd4f02d (~58 tests green). **NOT a build doc — planning only; nothing built yet.**

## Verdict (honest, no spin)
**Phase 6 is a SHIP phase, not a build phase.** Two-axis truth (the agents agree):
- **Code maturity ~7.5/10** — Phases 0-5 are genuinely strong (tested loops, real compliance,
  billing, A2P automation, screening graduation, server-bound tokens, growth tray, Vic).
- **Sellable-to-a-new-customer-TODAY ~5/10** (up from ~3/10 at Phase 0). The gap is **NOT
  features** — it's that the product is **undeployed**: still on the old `ringback-gixe.onrender.com`
  brand, zero env vars set, Stripe collects nothing, A2P unsubmitted (sends simulate), owner
  alerts offline, the ticker has no external cron. **Phase 6 closes the gap between "built" and "live."**

## Two parallel tracks
- **TRACK A — CODE (we build):** pre-launch hardening + integration polish. Buildable now, no owner needed.
- **TRACK B — OWNER-OPS (you do, we assist):** the deploy/env/Stripe/A2P/domain runbook. Gated on you.
They interleave: ship the hardening code, then run the go-live runbook against the hardened build.

---

## Sub-phase breakdown (risk + dependency ordered)

### 6a — Pre-launch HARDENING (CODE, build first) — the must-fix-before-launch ledger
From `PLAN-HARDENING.md` (10 MUST-FIX). Several are the P2s I consciously deferred in Phase 5 that
the audit re-escalated to must-fix *because we're now charging money*:
1. **CSRF on the `/api/calls` + `/api/leads` mutating family** (rescue/engage/flag-spam) — `_csrf_ok()` + JS `_csrf` wiring. (The known deferred gap.)
2. **Stripe dual-billing silent downgrade** — a missing Price ID silently strands a Pro/Crew subscriber at Starter; alert + fail-loud instead.
3. **`set_confirm_result` + `mark_call_engaged` tenant scope** — add `AND business_id=?` (2-line each; I scoped one already, finish both).
4. **`MAX_CONTENT_LENGTH`** cap (Flask default 16 MB → 1 MB).
5. **`FIRSTBACK_SECRET` fail-fast bypass** — ensure prod can't silently accept a dev session key.
6. **`FIRSTBACK_TOKEN_KEY`** — Google OAuth refresh tokens are stored plaintext without it (must be set + enforced before 5f/calendar tokens exist in prod).
7. **Stripe webhook `SignatureVerificationError → 400`** (not 500) — verify.
8. **HC-3 A2P payload shape** — the first real Trust Hub submission will 400; the Heritage dogfood is the correction pass (owner-ops, but code may need a fix).
- **`be-audit` on billing/auth/PII/consent** as the 6a gate before any of Track B.

### 6b — Integration POLISH (CODE) — the set-and-forget cohesion fixes
From `PLAN-INTEGRATION.md` (product coheres, but two real cross-feature issues):
1. **Owner-notification volume (top risk):** `vic_morning` (7-10am) + `growth_tray` (8-9am) +
   per-lead `vic_stall` nudges all fire on the same `alert_on_lead` toggle in the same morning
   window — an owner with 4 stalled leads + tray can get **7 SMS in 8 minutes at 8am.** Fix: a
   **unified 8am daily digest** that absorbs all three proactive push kinds into ONE SMS.
2. **Ticker weight:** `scan_morning_briefing` makes a real Claude call *inside* the ticker thread
   at 7am, which can block `run_due_once` (the actual sends) in the highest-traffic window. Move
   the LLM call out of the hot path / make it async.
3. **Stale-ticker alerting:** the in-process ticker dies silently; add an owner/ops alert when the
   heartbeat goes stale (9+ features ride the ticker).

### 6c — GO-LIVE runbook (TRACK B owner-ops, we assist) — the ordered path to first dollar
From `PLAN-GOLIVE.md`. **Critical path (each unblocks the next):**
1. **Render env reconcile BEFORE deploy** — esp. `FIRSTBACK_DB_PATH` (the **single riskiest op**:
   wrong path = empty ephemeral DB, data wiped on next redeploy, no warning) + `FIRSTBACK_SECRET`,
   `FIRSTBACK_OWNER_PASSWORD`, `FIRSTBACK_TASKS_SECRET`, `FIRSTBACK_PUBLIC_URL`, `FIRSTBACK_TOKEN_KEY`.
2. **Deploy `staging`** → fail-fast checks pass.
3. **External cron → `POST /tasks/run-due`** every minute (gated on `FIRSTBACK_TASKS_SECRET`; without
   it the scheduler is dead in prod).
4. **Stripe live:** 6 Price IDs (monthly+annual × 3 tiers) + `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET`; test with 4242.
5. **A2P dogfood:** submit Heritage via `/setup` (sole-prop path = fastest, no EIN/website, OTP "reply YES") — validates end-to-end + corrects HC-3.
6. **Switch Stripe to live keys → first $99 collected.**
7. **Then:** Resend email, custom domain (`firstback.io` — the `*.onrender.com` URL is a trust kill at $99), Google Contacts creds (unlocks 5f).

### 6d — VOICE (finish 5g + deploy + 7-check gate) — depends on owner deploy
5g is mid-build (slices 2+3 merged; slices 4 app.py + 1 voice_service remain). Finish the code
behind the gate, then it needs the owner deploy + 6 env vars + ear-test + the 7-check gate on a
real call before it's claimed live. Pricing stays "coming soon" until then.

### 6e — $99 READINESS polish — close the day-1 friction
From `PLAN-READINESS.md`. The biggest friction is the **silent A2P wait**: Dave clicks "activate
texting," gets no confirmation, hears nothing for 1-10 days. The wait is irreducible (carrier
vetting); **the silence is the fixable part** — once the owner alert channel is live, send "your AI
just caught your first call; texting activates in ~3 days." Plus: the brand/domain swap, and a
day-1 "here's what's working right now (call screening + voice need zero registration)" framing.

---

## Recommended sequence
**6a (hardening) → 6b (integration polish) → be-audit → 6c (owner go-live runbook) → 6e (readiness
polish) → 6d (voice, when you deploy).** Track A (6a/6b) is buildable now without you; Track B (6c)
is where you take over. Finish the in-flight 5g build either before 6a or fold its remaining 2 slices in.

## Open decisions for the owner
1. **Sequencing:** finish the 5g voice build (2 slices left) FIRST, then start 6a? Or pause 5g and
   start 6a hardening now (since 6a gates the money path)?
2. **Domain:** commit to `firstback.io` now (affects A2P + trust) or launch on the Render URL first?
3. **A2P path for the first real tenant:** Heritage sole-prop dogfood first (recommended), yes?
4. **Voice:** still deferred until you're ready to deploy + run the 7-check gate? (recommended)

## Honest bottom line
Nothing in Phase 6 is a heavy build. ~10 hardening fixes + 3 integration-polish items + a deploy
runbook stand between "56 green tests on staging" and "a contractor pays $99 and gets real value
day one." The product is built; Phase 6 makes it **real**.
