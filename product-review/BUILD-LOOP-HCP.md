# Build loop — Housecall Pro (2nd FSM provider)
**Started 2026-06-23. Self-paced /loop. Orchestrator = main session; workers = sonnet subagents.**
**Queued next:** live inbound voice answering (separate loop after this one).

Goal: add **Housecall Pro** as a second read-only FSM provider alongside Jobber, using the existing
`fsm_provider.py` interface (built for exactly this in the Jobber loop). Same v1 scope as Jobber:
pull customers/jobs → feed call screening "skip contacts you know"; push booked estimates back.

## Hard rules
- Build on `staging` only. **Owner gates every staging→main promotion — never push `main`.**
- Gated/inert: every entry point a no-op until `HCP_CLIENT_ID` set. Mocked tests only; no live creds.
- Mirror `jobber_fsm.py` + the audited Jobber patterns: OAuth via `db.set_oauth_tokens` (encrypted),
  `_access_token` fail-open refresh, `db.upsert_suggestion(category="customer", source="import-hcp")`
  (NOT `contact_import.ingest` — the presort F1 bug), business_id scoping, CSRF + verify-and-consume state.
- Honest copy; never "imported"/"connected" before true. No smart-quote Jinja delimiters (re-scan + parse
  after the build — this bit us twice).

## Provider selection (key design Q for the plan)
`fsm_sync.py` currently targets Jobber. Decide how a business picks Jobber vs HCP (env `FSM_PROVIDER`,
or per-business column, or "whichever is connected"). Plan must specify; recommend the simplest correct
option and flag if it's an owner decision.

## Stages / state
- [x] **S1 PLAN** (sonnet) → DONE. `product-review/plans/16-hcp-sync.md`. Recommends provider-selection
      **Option C** (route to whichever connected, HCP>Jobber, no double-fire). ⚠️ HCP API shapes are
      ASSUMED — flagged Q1–Q8 for S2 to verify against live docs.
- [ ] **S2 PLAN-AUDIT** (sonnet) → verify assumed FirstBack names vs real code + **verify the HCP API
      assumptions (Q1–Q6) against live HCP docs via WebSearch/WebFetch** + sign off provider-selection.
      go/fix list. ← **IN PROGRESS**
- [ ] **S3 BUILD** (sonnet, write-capable) → hcp_fsm.py + routing + settings + mocked tests green.
- [ ] **S4 BUILD-AUDIT** (sonnet) → review (security/honesty/smart-quote scan) + tests green. Orchestrator commits/pushes staging.
- [ ] **S5 HANDOFF** → SETUP_NEEDED HCP creds; memory; loop stops; then kick the inbound-voice loop.

## Log
- 2026-06-23: loop created (after Jobber P2 + Outlook P6 + voice P1 already on staging); S1 plan agent dispatched.
