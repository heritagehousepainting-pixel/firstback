# Build loop — P2 Jobber/HCP sync + P6 Outlook calendar
**Started 2026-06-23. Self-paced /loop. Orchestrator = main session; workers = sonnet subagents.**

Goal: build the two genuinely-new roadmap items from `DEV-HANDOFF-2026-06-23.md`:
- **P2** Jobber **or** Housecall Pro **read-only** sync (pull customers/jobs → improve screening
  so we never text a known customer wrong; push booked estimates as leads/notes). Additive, NOT
  an FSM replacement. Read-only for v1.
- **P6** Outlook (Microsoft Graph) calendar as a 2nd provider alongside `google_cal.py`.

## Hard rules (every stage)
- Build on **`staging`** only. **Owner gates every staging→main promotion — never push `main`.**
- All new integrations ship **gated/inert by default** (env-flagged, like `GOOGLE_PLACES_API_KEY` /
  Trust Hub), no-op until the owner sets credentials. Tests use mocks — never require live creds.
- Mirror existing patterns: `google_oauth.py`, `google_cal.py`, `token_crypto.py`, `connections.py`,
  `config.py` env conventions, `db.py` schema/migration style. Encrypt stored OAuth tokens.
- Honesty: no overclaiming in copy; a CREATE/200 ≠ "live". Report tests/outcomes faithfully.
- Kernel files (`convos.py`, `llm.py`, `static/assistant.css`) — edit LOCAL copies, never run sync.py.

## Stages / state
- [x] **S1 PLAN** (sonnet) → `product-review/plans/13-fsm-sync.md` + `14-outlook-calendar.md`. DONE.
      Recommends **Jobber** for v1 (owner to confirm Jobber vs HCP). NOTE: a read-only Plan agent
      can't write files — it returned text, orchestrator wrote the files. Build stages MUST use a
      write-capable agent (general-purpose/claude).
- [ ] **S2 PLAN-AUDIT** (sonnet) → review both plans for scope creep, security (OAuth/token storage,
      webhook signature, cross-tenant isolation), feasibility, honesty. **Verify every assumed
      function/table name against the real code.** Produce go/fix list. ← **IN PROGRESS**
- [ ] **S3 BUILD P2** (sonnet) → gated read-only FSM sync + migrations + mocked tests.
- [ ] **S4 BUILD-AUDIT P2** (sonnet) → review + full test sweep green. Then orchestrator commits/pushes staging.
- [ ] **S5 BUILD P6** (sonnet) → gated Outlook/Graph calendar provider + mocked tests.
- [ ] **S6 BUILD-AUDIT P6** (sonnet) → review + full test sweep green. Then orchestrator commits/pushes staging.
- [ ] **S7 HANDOFF** → update this tracker + SETUP_NEEDED (owner creds to flip live) + memory. Loop stops; notify owner.

## Open decision to surface to owner (do not block the loop on it)
- **Jobber vs Housecall Pro first** (doc open-question #2). Plan agent recommends; owner confirms
  before S3 build of the real client (the plan + scaffolding can proceed either way).

## Log
- 2026-06-23: loop created; S1 plan agent dispatched (background).
