# HANDOFF — RingBack Command Center (read this first)

You're taking over an in-flight build of RingBack's **Command Center** (`/dashboard`). Context from
the prior chat is exhausted; everything you need is in the repo + auto-memory. **Read the docs in §0,
run the suite, then continue WITH the user — deliver a slice, check in, take the redirect.**

> **Status @ 2026-06-17:** **Phases 0, 1, 2, and 3 are ALL built, audited, and green.**
> **656 checks across 14 standalone test files, 0 failing.** Nothing committed — all in the working
> tree on `main`. **Next: Phase 4 ("polish & soul")**, or the clean commit when the user asks.

## 0. Read these, in order
1. **`BRAIN.md`** — the north star. Persona "Vic" (the AI marketing employee), the growth engine, the
   trust moat, the non-negotiables (§10), the 5 unforgettable moments. Internalize the soul.
2. **`COMMAND_CENTER_MASTER_PLAN.md`** — the technical *how*: architecture, the **file-ownership map**
   (serialize on `assistant.py`/`db.py`/`app.py`; parallelize leaf JS/CSS/template/tests), and the
   **preserve-these contracts**.
3. **`ROADMAP_PHASE3.md`** — the growth-engine design (the "plays" engine).
4. **`SETUP_NEEDED.md`** — what's live vs. gated/simulated per phase, env knobs, deferred items, and
   the **`./run_local.sh`** local instance.
   Also load auto-memory `command-center-master-plan` + `ringback-working-style` + `ringback-test-harness`.

## 1. Operational facts (don't relearn the hard way)
- **Tests are standalone scripts**, NOT pytest. Run with the venv python (system python lacks Flask):
  `for t in test_*.py; do .venv/bin/python "$t"; done`. **656 checks, 14 files — keep it green.**
- **Local:** `./run_local.sh` → `http://localhost:8800`, login `owner@ringback.local` / `test1234`,
  isolated `local_test.db`, keyless `demo` brain. `.env` is NOT auto-loaded (export by hand for a real LLM).
- Two repo paths are the same tree (symlink): `~/apps/ringback` == `~/apps/design_hub/projects/ringback`.
- **DO NOT deploy to Render. DO NOT commit** unless the user asks (branch off `main` first if so).
- An unrelated **`DESIGN.md`** edit sits in the working tree (UI "signature moment" PROTECTED note) —
  **exclude it from a phases-0–3 commit.**

## 2. What's BUILT (all green)
- **Phase 0 "honest hands":** multi-step tool-calling loop (`llm.tool_complete` + `assistant._tool_loop`,
  keyword floor + confirm gate preserved), server-side memory + anaphora, security (CSRF, per-tenant rate
  limit `RINGBACK_ASSISTANT_RPM`, history sanitization, args allow-list, audit_log), the **honest confirm**
  (recipient + editable body + opt-out + live/test via `messaging.outbound_mode`).
- **Phase 1 "first win loop":** booking tools (list_slots / book_estimate / cancel_estimate / flag_urgent,
  slot+lead pinned at confirm, `_resolve_lead_target`, shared `_gated`), search (`db.search_leads`),
  money-framed lead card.
- **Phase 2 "Vic shows up":** server-rendered money-ranked **Morning Briefing**; **tappable ambient feed**
  (one tap → gated text confirm); the **Vic persona** (`assistant._VIC_PERSONA`, in both LLM paths + the
  keyword floor); **real-time** poll baseline (`GET /api/feed` + content signature + 25s in-place refresh
  that never wipes the chat). SSE + web push DEFERRED (need a streaming worker + VAPID) — in SETUP_NEEDED.
- **Phase 3 "growth engine":** `growth.py` — a unified declarative **plays engine** riding the existing
  `scheduled_messages` + `reminders.run_due_once` + gated `messaging.send_sms` spine (NO new table; new
  `kind`s + the `uniq_growth_touch_per_lead` partial index). Plays: compliant **review request** (asks
  every completed-job customer; trigger references NO sentiment — gating is illegal), **quote follow-up**,
  **reactivation**, **win-back**, **referral**, **membership**, **seasonal**, **density**, **financing**,
  + **Money Left Behind**. Chat tools `growth_plays` / `money_left_behind` reuse the briefing + stat cards.
  Owner one-tap sends reuse gated `text_lead`. Opt-in auto-scheduler `growth.scan` behind `growth_on`
  (default OFF; simulated until Twilio+A2P live; skips placeholder bodies + holds when A2P not ready);
  auto-pause on booking (command-center + SMS self-book). New settable cols `review_link`, `growth_on`
  (+ `db.set_growth_on`). DEFERRED (need a Google Business Profile connector): negative-review response
  drafting + before/after GBP post — surfaced honestly, not faked.

## 3. The brain (so you don't break it)
`MiniMax-M2.5` (base `https://api.minimax.io`) chains READ tools well but declines WRITE tools regardless
of the thinking flag — so `run()` routes clear confirm-gated WRITES (text/book/cancel/scheduling) through
the deterministic keyword router even when an LLM is keyed (reliable gate); the LLM loop handles
reads/chat/fuzzy. **The confirm gate is never bypassed on either path.** Claude `tool_complete` branch is
code-verified vs the official API ref but never live-fired (no `ANTHROPIC_API_KEY`). `run()` →
`{reply, cards, pending_action, meta}`; cards may accompany a `pending_action` only in a multi-step chain.

## 4. Two open decisions (user dismissed when asked — revisit when ready)
1. Add a daily/cumulative "rate memory" cap (`db.incr_rate(biz,"daily",86400)` + a number) — per-minute
   burst limiter exists; no daily quota yet.
2. Keep MiniMax (writes deterministic, works today) vs. switch the brain to Claude for richest multi-step
   agentic writes.

## 5. How the user drives the work
- Hands a phase off as **`/goal`**, then asks for **heavy parallel-Sonnet-4.6 subagent audit-and-fix
  loops** ("use N subagents"). Spawn Sonnet agents for design specs, leaf work, and **read-only multi-lane
  audits** (compliance/security/tenant/correctness/honesty/tests/UI/voice/contract → each returns
  P0/P1/P2 + GREEN/NOT-GREEN); the orchestrator owns the **serial hot-file** build/fixes. Loop until green.
- Verify-don't-assume (real runs for anything the suite can't exercise; report verified vs assumed).
  Honesty ethos: lead with money, talk like a foreman, never claim "live" when simulated.

## 6. Your first moves
1. Read §0 docs. 2. Run the suite (expect 14 files / 656 green). 3. Boot `./run_local.sh` and click the
command center (try: "what should I focus on?", "what plays do I have", "money left behind", tap a play
→ honest confirm). 4. Confirm reality matches this doc, then plan **Phase 4** (streaming, mobile/field,
a11y + honest orb, push-to-talk voice, trust headline, signature delight moments) with the user — or do
the clean commit. Keep the suite green, honor the file-ownership map, don't deploy, don't commit unless asked.
