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
- [x] **S5 BUILD P6** (sonnet) → DONE. New: `outlook_cal.py`, `test_outlook_cal.py` (85 checks).
      Modified: config/db/connections/app/settings.html/test_setup. Fixes F6/F7/F8/F10 + business-scoped
      `set_outlook_event_id` applied. **Orchestrator caught a CRITICAL bug the build agent missed:** its
      editor mangled the calendar-card Jinja string delimiters into smart quotes → `TemplateSyntaxError`
      (the whole `/settings` page broke; also what was misreported as a "pre-existing" test_scheduling
      failure). Fixed 9 lines back to straight-quote delimiters; verified `/settings` renders 200 with
      both cards. test_outlook_cal 85/0; test_scheduling now 18/0; fsm_sync 78/0; setup 147/0;
      sf8_connections 99/0; screening 57/0.
- [x] **S6 BUILD-AUDIT P6** (sonnet) → DONE. **SHIP-WITH-NITS**, no P1. Audit at
      `product-review/plan-audits/14-build-audit.md`. Confirmed all 5 fixes + clean smart-quote rescan.
      Orchestrator fixed P2-2 (naive-datetime→UTC) + P2-3 (added /settings render guard; outlook 85→88).
      **P6 committed + pushed to staging.**
- [x] **S7 HANDOFF** → DONE. SETUP_NEEDED updated with the owner-cred checklist for Jobber + Outlook;
      memory updated; loop stopped; owner notified. **LOOP COMPLETE.**

## Outcome (2026-06-23)
Both features built, audited (plan + build), and committed to `staging` — gated/inert until the owner
sets credentials. Owner gates the staging→main promotion (NOT yet promoted).
- **P2 Jobber FSM sync** (read-only v1): commit `0c5433e`. test_fsm_sync 78/0.
- **P6 Outlook calendar**: commit `0257f61`. test_outlook_cal 88/0.
- Regressions green throughout (setup 147/0, sf8_connections 99/0, screening 57/0, scheduling 18/0).
- Notable catch: the P6 build agent's editor mangled `settings.html` Jinja delimiters into smart quotes
  (would have crashed `/settings`); orchestrator caught + fixed in verification, added a /settings render
  regression guard. **Owner decision still open: Jobber vs Housecall Pro** for the FSM v1.

## Open decision to surface to owner (do not block the loop on it)
- **Jobber vs Housecall Pro first** (doc open-question #2). Plan agent recommends; owner confirms
  before S3 build of the real client (the plan + scaffolding can proceed either way).

## Log
- 2026-06-23: loop created; S1 plan agent dispatched (background).
