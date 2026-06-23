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
- [x] **S2 PLAN-AUDIT** (sonnet) → DONE. Both plans **GO-WITH-FIXES**. Audit at
      `product-review/plan-audits/13-14-audit.md`. Caught a CRITICAL bug (F1: `presort` drops all
      Jobber customers → use `db.upsert_suggestion` directly). Key fixes folded into both plans.
- [x] **S3 BUILD P2** (sonnet) → DONE. New: `fsm_provider.py`, `jobber_fsm.py`, `fsm_sync.py`,
      `test_fsm_sync.py`. Modified: config/db/connections/contact_import/app/reminders/settings.html/test_setup.
      Audit fixes F1–F5 applied. **Independently re-verified: test_fsm_sync 78/0; setup 147/0;
      sf8_connections 99/0; screening 57/0; import 45/0; app imports clean.**
- [x] **S4 BUILD-AUDIT P2** (sonnet) → DONE. **SHIP-WITH-NITS**; no P1/security holes. Audit at
      `product-review/plan-audits/13-build-audit.md`. Orchestrator fixed all 4 nits (N1 dead code,
      N2 stale counter, N3 dead CSRF ref, DC2 "imported"→"synced" honesty) + re-verified green.
      **P2 committed + pushed to staging.**
- [ ] **S5 BUILD P6** (sonnet, write-capable) → gated Outlook/Graph calendar provider + mocked tests,
      applying audit fixes F6 (Windows-tz shim), F7 (recommended_setup 3-touch), F8 (refresh→reconnect),
      F10 (module-top import). Runs AFTER P2 commit (shared files). ← **NEXT**
- [ ] **S6 BUILD-AUDIT P6** (sonnet) → review + full test sweep green. Then orchestrator commits/pushes staging.
- [ ] **S7 HANDOFF** → update this tracker + SETUP_NEEDED (owner creds to flip live) + memory. Loop stops; notify owner.

## Open decision to surface to owner (do not block the loop on it)
- **Jobber vs Housecall Pro first** (doc open-question #2). Plan agent recommends; owner confirms
  before S3 build of the real client (the plan + scaffolding can proceed either way).

## Log
- 2026-06-23: loop created; S1 plan agent dispatched (background).
