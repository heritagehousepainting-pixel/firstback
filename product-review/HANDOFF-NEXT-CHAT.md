# Handoff prompt — paste this into the next Claude Code session

You are the Opus orchestrator continuing an AUTONOMOUS `/goal` build of FirstBack (a live
missed-call text-back + AI-booking SaaS for home-services contractors). **FIRST do a
READ-ONLY AUDIT to confirm the state below is still true (don't trust this doc blindly —
verify against the repo + the live site + the Render MCP). THEN resume building.** Do not
change code during the audit.

## The goal (handed off mid-flight)
Autonomously complete ALL the building possible **without the owner**, from the product
review's master plan, each as a loop: build → test (standalone, keep green) → self-/be-audit
gate → commit to `staging` → push `origin staging` (auto-smoke-tests on the RingBackv2 mirror)
→ promote to production **only with explicit owner OK** (the deploy classifier blocks
unattended `staging:main` pushes — that's correct; batch deploys for the owner). Keep a running
"NEEDS OWNER" tab. Don't stop until the buildable-without-owner work is done; tests stay green;
gate integrity (pricing "coming soon", voice gate) stays intact throughout.

## READ FIRST (source of truth — read in this order)
1. `product-review/plans/00-MASTER-PLAN.md` — the sequenced batches + the cross-workstream
   COLLISION RESOLUTIONS (alerts.py edits serialized; ONE monthly digest; milestone refactor
   before won_amount; ALTER-only migrations; kernels edited locally never synced).
2. `product-review/plans/01..10-*.md` — the per-batch build-ready plans (exact file:line + approach + tests).
3. `product-review/00-SYNTHESIS.md` — the 12-lane audit findings (the "why").
4. Memory index `~/.claude/projects/-Users-jonathanmorris-apps/memory/MEMORY.md`, esp.
   `firstback-blueprint`, `no-spin-honest-assessments`, `friction-is-the-product`,
   `quality-bar-more-than-enough`, `multi-agent-research-style`.
5. `firstback/SETUP_NEEDED.md` — owner-ops.

## STATE (verify before trusting)
- Repo `~/apps/firstback`, branch **`staging`**. Tests are STANDALONE: `.venv/bin/python test_X.py`
  (NOT pytest); run all by looping `for t in test_*.py`. Last known: **64/64 green**.
- **Production is LIVE** at `ringback-gixe.onrender.com` (Render service `ringback`,
  id `srv-d8nfbh3tqb8s73d4jdgg`, branch **`main`**, persistent disk `/var/data/firstback.db`).
  `RingBackv2` (`srv-d8pg773tqb8s7385mha0`, branch `staging`, NO disk) is the disposable smoke mirror.
- **DEPLOY MECHANIC:** production deploys from `main`; `staging` contains `main`'s HEAD, so promote
  by `git push origin staging:main` (fast-forward). `origin` = github.com/heritagehousepainting-pixel/RingBack.
  Env on prod is `FIRSTBACK_*` (the old `RINGBACK_*` rename was reconciled). Render MCP is available
  (list/get_service, list/get_deploy, update_environment_variables, create_cron_job, list_logs).
- **DONE + DEPLOYED:** the go-live (env reconcile, deploy, external cron `firstback-tasks-cron`
  every minute → /tasks/run-due), **Batch A** (Tier-0 honesty/signup fixes, commit 2cc0e7c), **Batch B**
  (AI conversation overhaul + core-loop speed, commit **92aacde**). ⚠️ AUDIT ACTION: re-confirm Batch
  B's PRODUCTION deploy actually went live on `ringback` — the last `get_deploy` 404'd before I could
  verify (the `staging:main` push succeeded; the RingBackv2 smoke build for 92aacde WAS live). Curl
  `/health/ticker` + check `ringback`'s latest deploy via the MCP.
- **TASK LIST (TaskList to see):** #1 deploy Batch B (verify live), #2 Batch C (was in_progress,
  NOT started building — only read the plan), #3 Batch D, #4 Batch E, #5 Batch F (non-decision),
  #6 Batch G (code-only), #7 audit pass 1, #8 audit pass 2 + reconcile.

## REMAINING BATCHES (in order; honor the master-plan collision rules)
- **C — Mobile/dashboard UX** (plan 04): card list ≤640px, labeled nav, 44px taps, tel: links,
  styled error states, "all clear" empty state, urgency tint, briefing deep-links. NOTE: UI-only;
  you can't see pixels — verify Jinja parse + routes render 200 + backend context (last_lead_name)
  + suite green; do a REAL Playwright screenshot pass after deploy (mobile viewport), and flag the
  final visual sign-off as an owner check. Scope changes to media-queries so desktop is unaffected.
- **D — Alerts & set-and-forget** (plan 05): the COORDINATED alerts.py pass — owner quiet hours
  (urgent bypass; must NOT touch the customer-facing TCPA backstop in messaging.send_sms),
  stall-nudge daily cap, "all clear" reassurance, webhook channel, tick_stale per-business fan-out.
  Fully testable with the standalone suite.
- **E — Make value visible** (plans 06/08/07 as ONE monthly-digest spine): build the progressive
  milestone refactor FIRST, then monthly recap + loss-framing + dollar-over-multiple, real-dollar
  `won_amount` attribution, the screening monthly section RIDING the one recap, Customer Book page,
  Google-review delta tracking, auto-mode streak unlock. ALTER-only migrations.
- **F (non-decision only)** (plan 09): SEO/OG meta across marketing_base + pages, "conversations"
  → "missed-call replies" rename, the ROI anchor on pricing. SKIP the founder-decision items.
- **G (code-only)** (plan 10): voicemail transcription → lead (Twilio recording webhook),
  web-chat "Text us" widget (public endpoint + snippet). SKIP deposit link (needs owner Stripe link)
  + GBP dashboard (needs Google creds).
- Then **audit pass 1 + 2** (function/wording/layout/security), reconcile SETUP_NEEDED, check in.

## NEEDS OWNER (the running tab — don't build these; surface them)
- 4 FOUNDER DECISIONS (block parts of E/F): money-back guarantee? lead marketing with "it books
  the job" + the Vic briefing? bundle paid caller-reputation (Nomorobo/Hiya, has a real cost)?
  soft-overage billing vs hard cap?
- OWNER-OPS to finish go-live: Stripe (6 Price IDs + keys, or a Payment Link for the first $99 —
  there's NO wired Subscribe button), A2P sole-prop dogfood (so real texts flow; today they
  simulate), Google Calendar/Contacts OAuth creds (Calendar card shows "Coming soon" until set).
- Every PRODUCTION deploy needs the owner's explicit OK (classifier-gated).
- Post-deploy: a live-Claude `/simulator` walkthrough to confirm Batch B's new prompts (persona/
  urgency/price/Spanish) read well — this env is demo-only so quality wasn't verifiable here.

## STANDING RULES / GOTCHAS
- Honesty over spin; never claim simulated/gated/undeployed as live. Voice stays "coming soon"
  until the 7-check gate. Preserve `ringback-gixe.onrender.com`.
- `convos.py`/`llm.py`/`assistant.css` are trades_core kernels — edit firstback's local copies,
  never run `trades_core/sync.py`. Smart quotes break Jinja (ASCII only in templates; verify parse).
- `send_sms(transactional=True)` is quiet-hours-exempt (owner alerts gate=False, owner-cell only);
  marketing passes transactional=False. Batch B already set: ONLY reminder/morning_reminder are
  transactional in run_due_once.
- THE LOOP per batch: read plan → build (one coupled batch directly; agents only for parallel
  read-only audits/planning) → run FULL suite green → self-/be-audit money/auth/PII/consent →
  commit to staging with the two trailers → push staging (smoke) → ask owner before promoting.
  Beware a flaky `test_dispatcher_call.py` in the loop (passes 3/3 standalone — it's transient).
- Commit trailers: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` + the Claude-Session line.

## DO THIS FIRST (the read-only audit, before any building)
1. `git status` + `git log --oneline -6` (confirm clean, on staging @ 92aacde or later).
2. Run the full standalone suite; confirm green (note the dispatcher flake).
3. Verify gate integrity: pricing.html still "coming soon"; no FIRSTBACK_VOICE_URL in render.yaml.
4. Via Render MCP: confirm `ringback`'s latest deploy is `92aacde` and LIVE; curl `/health/ticker`
   (fresh) + spot-check the Batch A/B fixes are live (e.g. /solutions has no "live AI voice").
5. `TaskList` to see the phase tracker. Then RESUME at Batch C (or the first unfinished task),
   running the loop. Check in with the owner after each batch's staging push (they gate prod deploys).
