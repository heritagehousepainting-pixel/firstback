# FirstBack — Autonomous Build Loop (durable tracker)

**Started:** 2026-06-19 · Orchestrator: Opus · Mode: `/goal` autonomous, owner-gated on production.

## The goal
Complete every batch buildable **without the owner**, starting at Batch C, each as a loop:
**plan → audit-the-plan → build → standalone tests green → audit-the-build → commit `staging` → push `origin staging`.**
Production promotion (`staging:main`) is **gated on explicit owner OK** every time.

## The loop, per batch
1. **Audit the plan** — red-team the plan file vs *current* code (Batches A/B moved lines; anchors may be stale). Correct refs, flag bugs/collisions. Output: `plan-audits/NN-audit.md`.
2. **Build** — parallel sonnet agents split by **disjoint file ownership** (safe: plans pin exact interfaces/class names); serial on shared kernels (`alerts.py`, `convos.py`, `llm.py`, `assistant.css`, `app.css`).
3. **Test** — full standalone suite green (`.venv/bin/python test_*.py`) + new per-batch tests.
4. **Audit the build** — parallel read-only agents: `be-audit` (money/consent/alerts), `ui-audit` (touched pages), correctness/wording sweep.
5. **Commit** to `staging`, push `origin staging` (smoke on the mirror). **Stop at the production gate.**

## Agent strategy (honest right-sizing)
Max safe *concurrency at one instant* is bounded by independent file-sets (~4–8 here), not 25 — the
collision rules exist so parallel writers don't corrupt shared files. So: **many sonnet agents in waves**
(total >25 across the build), never 25 colliding on one file. Widest fan-out is **Batch E** (separable
surfaces: digest, screening report, customer book, review-delta, milestones, streak) and **Batch G**
(independent features). **Batch C is solo/serial** — 7 changes all touch the same 4–5 files.

## Batch status
- [x] A — Tier-0 (shipped, live `92aacde`)
- [x] B — AI conversation + core loop (shipped, live `92aacde`)
- [x] C — Mobile + dashboard UX (plan 04) — shipped to staging `6d0b67c`; 66/66 green; UI+BE audit fixes applied
- [x] D — Alerts & set-and-forget (plan 05) — 67/67 green; TCPA backstop confirmed untouched; SSRF guard + _int_pref(0) bug fixed in audit
- [x] E — Make value VISIBLE (plans 06+08+07) — COMPLETE. Built via 5 parallel sonnet worktree
      agents (branched stale from 92aacde) + serial harvest/integration onto staging; 74/74 green.
  - [x] E1 — Customer Book page (07-2)
  - [x] E2a — progressive ROI milestones (07-3)
  - [x] E2b — monthly recap (06-3) + screening section (08 fold-in) + digest loss-framing (06-2b)
  - [x] E4 — Google review tracking (07-1; inert without Places key)
  - [x] E5 — won_amount attribution (06-4) + analytics dollar-flip + loss-note (06-1/2c)
  - [x] E3c — auto-mode streak unlock (07-4); E3ab — seasonal (07-5) + density referral (07-6)
  - integration fix: scan-driven digests bypass owner quiet-hours (Batch D latent bug exposed at night)
  - minor deferrals: 07-3e analytics milestones-timeline/all-time-default + /api/roi_milestones;
    07-2e customer-book briefing-card hook
  - deferred: 07-2e briefing-card hook (additive enrichment)
- [x] F — pricing/marketing/SEO non-decision parts (plan 09) — 75/75 green; audit SHIP
  - shipped: SEO/OG meta (homepage onboarding.html + pricing/solutions/customers/marketing_base;
    og:image omitted until asset exists), "conversations"->"missed-call replies", ROI anchor strip,
    Pro extra-number add-on (->/contact), soft-overage FAQ, customer waitlist card, /webinars de-linked
  - NEEDS-OWNER (skipped): money-back guarantee badge (C3), "books the job"+Vic hero (C6),
    Heritage dogfood quote (C7a), annual-toggle checkout wiring (C1, deferred)
  - NEEDS-ASSET: generate /static/og-default.png (1200x630) then add og:image back
- [x] G — voicemail→lead + web-chat widget, code-only (plan 10) — 76/76 green; audit SHIP
  - voicemail→lead: opt-in <Record> voicemail (single-party, NOT live-call recording) gated on
    voicemail_enabled; recording webhook creates a lead + injects transcript (recording_url on the
    row, no fake direction); no double-greeting. Inert until enabled + Twilio wired.
  - web-chat widget: /widget.js embed + /api/widget/<slug>/config.js + POST /webhooks/widget/lead
    (CORS, rate-limited, E.164, A2P-gated, widget_enabled opt-in); Settings card with embed code.
  - NEEDS-OWNER (skipped): deposit link (Stripe Payment Link), GBP review dashboard (Google re-auth).
- [ ] Audit pass 1 · [ ] Audit pass 2

## Gates that must stay intact
- Pricing `/pricing` = "coming soon" (no live checkout).
- Voice gated off (`VOICE_PUBLIC_URL` empty; separate service).
- Screening OFF until configured.
- Customer TCPA / quiet-hours backstop **never** weakened (relevant in Batch D).

## NEEDS-OWNER (decisions/credentials I cannot supply)
- Founder decisions: 30-day money-back guarantee? · "books the job" hero framing? · bundle paid caller-reputation? · soft-overage billing vs hard cap?
- Owner-ops: Stripe/Payment Link, A2P dogfood, Google business-scope creds.
- Every production promotion (`staging:main`).
