# Command Center — Master Plan

> **Read `BRAIN.md` first.** It is the north star (the *what* and *why* — Vic, the AI marketing
> employee). This doc is the *how* for the plumbing (memory, tool-calling, security, cards). Where
> the two differ on ambition or phase goals, `BRAIN.md` wins; the architecture below still holds.

**Status:** research complete (30-track recon, 2026-06-16), ready to build.
**Scope:** the signed-in home at `/dashboard` (`templates/command.html` + `static/assistant.js` +
`static/assistant.css`, backed by `assistant.py`), plus the engines it sits on (`db.py`,
`messaging.py`, `connections.py`, `convos.py`, `ai.py`/`llm.py`).

This doc is the synthesis of a 30-agent read-only audit. It states the north star, the findings
that matter, the phased build, and — critically — the **file-ownership map** that lets builders run
without clobbering each other.

---

## 1. The north star

> **Today it's a search box with an orb. It should be the contractor's operating surface — the one
> place they run the business from their phone between jobs.**

The defining insight from the competitive scan (B1): every competitor is **AI-for-the-caller** (AI
receptionists, call logs, lead inboxes). **Nobody has built AI-for-the-owner** — a surface where the
contractor themselves drives the business by voice/tap/text over their own leads, bookings, and
revenue. That white space is FirstBack's to own.

Three pillars:

1. **It closes the core loop.** The command center can *book, reschedule, and cancel estimates* —
   the verb the whole product exists for — not just list them. Every consequential action shows an
   honest confirm (who + exact message + opt-out + live/test) before anything reaches a customer.
2. **It's proactive, not reactive.** A once-daily morning briefing + an ambient priority feed put
   "the most important thing right now" in front of the owner — cold leads, today's estimates,
   opt-outs, revenue moments — ranked by money-at-stake. The blank page disappears.
3. **It has memory and acts in steps.** Server-side conversation state means "text her back" and
   "book the second one" resolve correctly; a real tool-calling loop means "book John then text him
   the time" runs as one request. It learns each tenant's shortcuts and gets faster.

**Paradigm decision (B4/B6/B2/B5):** for a one-thumbed contractor in the field, open chat is the
*wrong* primary surface — blank-page paralysis + mobile typing friction. The winning shape is a
**proactive feed of tap-action cards, with chat as the power escape hatch**, plus push-to-talk voice
for hands-busy moments. Chat stays; it stops being the only door.

---

## 2. What the recon found (the case for the work)

**The core loop is missing.** The chat cannot book/reschedule/cancel an estimate even though
`db.book_appointment`, `cancel_appointment`, and `upcoming_slots` already exist (A1, A2). It's a
read/list surface with one outbound-text tool; every corrective action (cancel, engage a screened
caller, spam-flag, view a transcript) lives only in `/pipeline` (A2). Power users are right to live
there — that's the problem.

**The confirm gate is blind.** It renders a static summary string, not the recipient or the message
body, so the owner approves a real customer text sight-unseen (A5). Best practice (B7): show
recipient + verbatim body + opt-out badge + live/test badge, and prefer **edit-before-send** over an
undo window (SMS can't be recalled).

**No memory.** History is client-side, wiped on reload, and cards aren't fed back — so "text him
back" falls to a most-recent-lead guess (A4). The schema to fix it mostly exists
(`assistant_convos`/`assistant_turns`) and needs an entities column (C1).

**The brain is a one-shot router.** A single JSON call parsed by a greedy regex; can't do
multi-step, silently falls back on malformed output (A9). The `TOOLS` dict is already shaped to
become a real provider-agnostic tool-calling loop (C2).

**It's reactive, brittle on mobile, and the orb is theater.** No real-time (a just-missed call
needs a reload, which wipes the chat) (B8); autofocus pops the mobile keyboard, tap targets are
sub-44px, glass UI dies in sunlight (B5); the orb runs a continuous WebGL shader with a fake
"speaking" state and no voice (A12, C6).

**What's already good** (don't break): the go-live surface is honest and well-gated (A6); the
per-tenant learning/coach loop is a genuine edge (A11); the gated `messaging.send_sms` seam (opt-outs
+ A2P + simulated/live) is correctly positioned (A5); the test suite encodes real contracts (A10).

---

## 3. Architecture decisions (reconciled)

- **Memory = server-side, single source of truth (C1 over C3's client round-trip).** Add
  `browser_key` (localStorage, survives reload), `cards_json` on turns, and an
  `assistant_turn_entities` index. Entities shown in a turn are captured server-side; resolution
  reads them back. *Reconciliation:* C3's referent-resolution logic stays, but reads from C1's
  `recent_entities()` instead of echoing entities through the client.
- **Brain = multi-step tool-calling loop (C2),** keeping the deterministic `_demo_route` keyword
  floor (works with no API key) and the confirm-gate (loop breaks at the first gated tool). `TOOLS`
  dict unchanged; schemas derived from it. Provider-agnostic bridge in `llm.py`.
- **Confirm = honest `confirm_sms` card (A5 + B7 + C4):** recipient, editable body, opt-out badge,
  live/test badge; it is its own confirm surface.
- **Real-time + streaming share one SSE channel (B8 + B10).** Poll first (15s `/api/feed?since=`),
  then SSE for both live events *and* token streaming; `gevent`/`gthread` worker + `X-Accel-Buffering:
  no`. Web push via `pywebpush` for the away case; SMS/email fan-out already exists in `alerts.py`.
- **Voice = push-to-talk Web Speech API only (B3).** One day's work, zero infra. Make the orb's
  "speaking" state honest (rename to `responding`); gate the orb on reduced-motion + battery +
  save-data (C6/A12). No realtime voice (duplicates `voice_service.py`).
- **Discoverability + personalization:** replace static `suggestions()` with
  `adaptive_suggestions(business, leads, digest, golive)` — a pure function over signals that already
  exist (B11); add an empty-state example gallery, a "what can you do" inline answer, and a `/` menu
  (B9).
- **Security (C5):** per-tenant rate limit + daily token budget that degrades to the keyword floor;
  validate/sanitize client history before it hits the LLM; wire the dead CSRF token (double-submit);
  audit-log gated actions; allow-list `args` keys per tool.
- **Learning hardening (A11):** add `delete_learning` + a `/training` delete button; min pattern
  length; conflict warning. **Observability (C7):** five metrics from existing tables + a health
  strip on `/training`; flag swallowed LLM errors.

---

## 4. The build — phased, dependency-ordered

### Phase 0 — Foundations (unblock everything; low user-visible surface)
- **F1 Server-side conversation state** (C1) — `db.py`, `db_core.py`, `convos.py`, `app.py`,
  `assistant.js`. Prereq for memory + anaphora.
- **F2 Tool-calling loop** (C2) — `assistant.py`, `llm.py`. Prereq for multi-step + clean new tools.
- **F3 Security baseline** (C5) — rate-limit/token-budget/CSRF/audit before exposing more tools.

### Phase 1 — Close the core loop (highest value)
- **L1 Booking tools** — `book_estimate`, `cancel_estimate`, `reschedule`, `list_slots`,
  `block_day`, `flag_urgent` (A1) in `assistant.py` + `db.py` helpers already exist.
- **L2 Honest confirm + `confirm_sms` card** (A5/B7/C4) — `assistant.py`, `assistant.js`,
  `assistant.css`.
- **L3 Anaphora resolution** (C3) — `assistant.py` (reads C1's `recent_entities`).
- **L4 Search/lookup** — `search_leads` / `lookup_caller` + SQLite FTS5 (A8) — `db.py`,
  `assistant.py`.

### Phase 2 — Make it a command center (proactive)
- **P1 Daily briefing + ambient priority feed** (B6, signals per B11/A7) — new `briefing` +
  `interactive_list` cards, server feed composition.
- **P2 Real-time** — poll → SSE + web push (B8) — `app.py`, `assistant.js`, new `push_subscriptions`.
- **P3 Adaptive suggestions + discoverability** (B11 + B9) — `assistant.py` `adaptive_suggestions`,
  `command.html` empty state, `/` menu.

### Phase 3 — Polish, trust, reach
- **Q1 Streaming** over the SSE channel (B10).
- **Q2 Mobile/field** — drop mobile autofocus, ≥48px targets, sunlight contrast, offline banner (B5).
- **Q3 A11y + orb rework** — contrast fixes, `role="status"` thinking, focus mgmt, honest/gated orb
  (C6/A12).
- **Q4 Voice** push-to-talk (B3).
- **Q5 Pipeline-parity tools** — cancel/engage/spam-flag/transcript view (A2/A3); go-live chat
  enhancements (A6).
- **Q6 Learning hardening** (A11) + **observability** health strip (C7).

---

## 5. File-ownership map (READ BEFORE SPAWNING BUILDERS)

Builders are **not** freely parallel: a few files are touched by almost every lane. Collisions, not
ideas, are the constraint.

**Hot files (serialize — one writer at a time, per phase):**
- `assistant.py` — F2, L1, L2, L3, L4, P3, Q5 all touch it. **One owner per phase**, sequential.
- `db.py` — F1, F3, L1, L4, P2, Q6 touch it (mostly additive schema/helpers; still serialize).
- `app.py` — F1, F3, P2 touch routes.

**Leaf files (safe to parallelize):**
- `static/assistant.js`, `static/assistant.css` — JS/card work (L2, P1, P3, Q1, Q2, Q3) can run in
  parallel with Python lanes, but JS card lanes among themselves share `renderCard`; coordinate via
  C4's "one `else if` branch per type" rule.
- `templates/command.html` — small, mostly P3/Q2/Q3.
- `static/tokens.css`/`ui.css` — token-only additions.

**Execution rule for the builder fleet:** within a phase, assign **one "core" agent** to own
`assistant.py` + `db.py` + `app.py` end-to-end for that phase's lanes, and run **leaf agents** in
parallel on JS/CSS/template/tests. Across phases, go in order (Phase 0 → 3) because F1/F2 are
prerequisites. Each lane ends by running the standalone tests with `.venv/bin/python` and the
ui-audit on touched templates/CSS.

## 6. Contracts every lane must preserve (from A10)

- `run()` → `{reply, cards, pending_action, meta}`; cards empty when `pending_action` is set.
- Confirm gate is non-negotiable: `text_lead` / `set_scheduling` never auto-execute; `execute()` is
  the only run path. New gated tools register `confirm: True` in `TOOLS`.
- `_route_topic()` keyword contract + `golive_summary` card shape (`{status,is_live,live_verified,
  blocker,steps,done,total}`) — go-live tests depend on these names/shapes.
- `send_sms` A2P gate stays (`status=="blocked"` when configured + not approved).
- Tests are standalone scripts: `.venv/bin/python test_*.py` (framework python lacks Flask). Keep the
  bespoke `check()` harness; do not add pytest.

## 7. Open item

- **A7 (proactive-signal inventory)** completed its research but its final write-up hit a transient
  API error; its territory is reconstructed from B11's signal table + B6's briefing design + A1/A2/A3
  data maps. Re-run a dedicated A7 if Phase 2 needs the exhaustive signal catalog.
