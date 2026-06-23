# HANDOFF — FirstBack Command Center / "Vic" (read this FIRST)

You're taking over FirstBack's **command center** (`/dashboard`). This file contains useful
historical product memory, but the branch/deploy facts have changed over time. **Always verify the
current checkout with `git status --short --branch`, count tests with `find . -maxdepth 1 -name
'test_*.py' | wc -l`, then continue in small slices.**

> **Current repo reality @ 2026-06-23:** checkout is `main` at `8163f56`, with **76**
> root-level standalone `test_*.py` scripts. `.env` is gitignored and was not inspected during the
> cleanup sweep; do not assume live Claude/Twilio/Stripe keys are present. Older notes below that
> mention `staging`, 15 tests, or Phase 6 as unbuilt are historical context unless re-verified.

## 0. Read these, in order
1. **This file.**
2. **`BRAIN.md`** — the north star: Vic (the AI marketing employee), the trust moat, the
   non-negotiables (§10), the 5 delight moments.
3. **`COMMAND_CENTER_MASTER_PLAN.md`** — the *how*: architecture, the **file-ownership map**
   (serialize `assistant.py`/`db.py`/`app.py`; parallelize leaf JS/CSS/template/tests), and the
   **preserve-these contracts** (§6).
4. **`ROADMAP_PHASE3.md`** — the growth "plays" engine design.
5. **`SETUP_NEEDED.md`** — what's live vs gated/simulated per phase, env knobs, and the
   **"## Phase 5" pre-live punch-list**.
6. Auto-memory: `command-center-master-plan`, `firstback-vic-hub-vision`, `firstback-working-style`,
   `firstback-test-harness`, `firstback-token-encryption`, `firstback-brand-tokens`.

## 1. The relationship (don't misread this)
**FirstBack is an ESTABLISHED, already-deployed product** (live Render service **`firstback`**, deploys
from **`main`** — the marketing site, login, pipeline, go-live wizard, etc.). The command center is an
**existing feature**; phases 0–5 **upgrade that feature IN PLACE** into "Vic." Not a new product, not
a rewrite. This local repo (`~/apps/design_hub/projects/firstback`, symlinked `~/apps/firstback`) is the
source of truth for prod.

## 2. Operational facts (don't relearn the hard way)
- **Tests are standalone scripts**, NOT pytest. Run with the venv python:
  `for t in test_*.py; do .venv/bin/python "$t"; done`. There are currently **76** root-level test
  scripts; keep them green.
  (Each test file pins `FIRSTBACK_PROVIDER=demo`, so the suite is free + deterministic regardless of
  `.env`.)
- **Local app:** `./run_local.sh` → `http://localhost:8800`, login `owner@firstback.local` /
  `test1234`, isolated `local_test.db`. **It now honors `.env`** → with the Claude key set it runs the
  **real Claude brain locally and SPENDS API credit** on each chat turn. Force the free demo brain
  with `FIRSTBACK_PROVIDER=demo ./run_local.sh`.
- **`.env` + `*.db` are gitignored.** Do not inspect or assume secrets unless the user asks.
- **Git:** verify the branch before any work. During the 2026-06-23 cleanup sweep the checkout was
  `main...origin/main` and clean before edits.
- **Deploy:** `render.yaml` currently provisions a Render service named `firstbackv2`; treat this as
  deployment configuration that must be reconciled with the intended environment before any deploy.
- **Claude path:** verified **live-fired** end-to-end (real reply + multi-step tool chaining + SSE).
  Model `claude-opus-4-8`. **Invoke the `claude-api` skill before touching any Claude/llm.py code.**

## 3. What's BUILT (phases 0–5, all green)
- **P0 honest hands:** multi-step tool-calling loop (`llm.tool_complete` + `assistant._tool_loop`,
  keyword floor + confirm gate), server-side memory + anaphora, security (CSRF, per-tenant RPM,
  history sanitization, args allow-list, audit log), the **honest confirm** (recipient + editable body
  + opt-out + live/test).
- **P1 first win:** booking tools (list_slots/book/cancel/flag_urgent, slot+lead pinned at confirm),
  search, money-framed lead card.
- **P2 Vic shows up:** money-ranked **Morning Briefing**, tappable ambient feed, the **Vic persona**,
  real-time poll (`GET /api/feed`).
- **P3 growth engine:** `growth.py` plays (compliant reviews — NO sentiment gating; quote follow-up,
  reactivation, win-back, referral, membership, seasonal, density, financing) + Money Left Behind;
  opt-in auto-scheduler (`growth_on`, default OFF, simulated until Twilio+A2P).
- **P4 polish & soul:** **real SSE streaming** (`POST /assistant/stream`, live Claude tokens / chunked
  for demo+MiniMax; `run_stream`/`_tool_loop_stream`; non-stream `/assistant` kept as fallback),
  **daily LLM cap** (`FIRSTBACK_ASSISTANT_DAILY`, degrades to keyword floor), honest/gated orb
  (speaking→responding; reduced-motion+Save-Data+battery), mobile/field (no autofocus on touch, ≥48px
  targets, sunlight contrast, offline banner), a11y, push-to-talk voice (Web Speech API only, never
  auto-sends), the trust headline, delight-moment tuning.
- **P5 deep audit:** 5 parallel Sonnet lanes; **0 P0 regressions**; fixed review-request ≤90d,
  simulated-send no-green-tint, execute() contract, defense-in-depth tenant scoping, a11y contrast/tap
  targets/403 handling, + 2 pre-existing marketing-copy honesty carryovers. Remaining minor items are
  the **SETUP_NEEDED "## Phase 5"** pre-live punch-list (none bite in the current simulated state).

## 4. Vic's ACTUAL tools today (the gap Phase 6 closes)
- **DOES in chat** (gated where it touches a customer): briefing, get_stats, growth_plays,
  money_left_behind, list_leads, list_appointments, find_lead, list_slots, add_contact,
  import_contacts, connect; **text_lead, book_estimate, cancel_estimate, flag_urgent, set_scheduling**.
- **Only GUIDES (links out, doesn't do in chat):** connecting calendar/email/number, changing
  profile/business info, hours, alerts, AI instructions, screening mode, growth_on, voice-vs-text →
  `_route_topic` points to `/settings` or `/setup`.
- **No first-run chaperone** yet.

## 5. NEXT — Phase 6 "Vic, the hub" (defined by the owner, NOT built)
The end goal: a **premium AI service where Vic works just as well INSIDE the app as OUTSIDE.**
- **Outside:** autonomous missed-call catch (quick answering/text-back), qualify, book estimates in a
  *strategized* way.
- **Inside:** Vic is your assistant — run/configure the WHOLE product by talking to it (profile,
  every settings toggle, connect calendar/email/number, voice-vs-text immediate-response, turn things
  on/off) — a hand in **every pocket**.
- **Friction → near zero:** pre-fill + do everything possible; hand the owner only the **final approve
  tap** (the confirm gate is the deliberate "press go").
- **Honest seam (owner accepts):** Google OAuth consent, the carrier call-forwarding star-code, A2P
  submission, pasting a secret key **stay the user's tap** — Vic *initiates, pre-fills, explains,
  confirms*, and fully does anything server-side. "Leave the app, do it, come back" is fine.
- **First-run chaperone:** proactively walk a brand-new user through setup end to end; recede as they
  get comfortable.
Full detail: auto-memory `firstback-vic-hub-vision`. **Brainstorm/scope with the user before building**
(it's a big, multi-tool phase touching the hot files + settings/connection flows + onboarding).

## 6. Ground rules & contracts (non-negotiable)
- **Don't deploy or commit to `main`/prod.** Staging commits/pushes are OK. Confirm outward-facing
  actions; the auto-mode classifier WILL block an un-greenlit commit.
- **The confirm gate is sacred:** every customer outbound shows exact recipient + editable body +
  opt-out + live/test before sending; `execute()` is the only run path for gated tools; new gated
  tools register `confirm: True`. Streaming must NOT bypass it.
- **Preserve contracts (§6 of the master plan):** `run()`/`run_stream()`/`execute()` →
  `{reply,cards,pending_action,meta}`; `golive_summary` shape; `_route_topic` keyword contract;
  `send_sms` A2P gate; the standalone test harness (no pytest).
- **Honesty ethos:** lead with money, talk like a foreman, never claim "live" when simulated, never
  invent a customer detail, review-gating is illegal (ask every customer). Compliance is the product.
- **Build discipline:** serialize the hot files (`assistant.py`/`db.py`/`app.py` — one writer),
  parallelize leaf JS/CSS/template/tests; user likes heavy **parallel Sonnet 4.6 read-only audit
  loops** then orchestrator-owned serial fixes; keep the suite green after each slice; verify on the
  real running app.
- **Invoke the `claude-api` skill** before any Claude/Anthropic/`llm.py` work.

## 7. Your first moves
1. Read §0. 2. Run the suite (expect 15 files / 682 green). 3. `./run_local.sh` (Claude is live — costs
a little) or `FIRSTBACK_PROVIDER=demo ./run_local.sh` for free; click the command center, confirm
reality matches this doc. 4. Pick up the conversation: **scope Phase 6 "Vic, the hub" with the user**
(and/or help them finish the `firstbackv2` staging deploy). Don't touch `main`/prod; keep the suite
green; honor the confirm gate.

## 8. Open decisions (ask the user)
- **Build order:** Phase 6 before the staging deploy, vs deploy `firstbackv2` now and build Phase 6 on
  staging (user: no preference yet).
- **In-chat limits:** which connection steps stay guided vs are forced into the chat where technically
  possible (user wanted to "discuss" — that discussion is the start of Phase 6 scoping).
