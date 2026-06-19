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
- [ ] E — Make value VISIBLE (plans 06+08+07) — widest parallel fan-out ← current
- [ ] F — SEO/rename/ROI anchor, non-decision parts (plan 09)
- [ ] G — voicemail→lead + web-chat widget, code-only (plan 10)
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
