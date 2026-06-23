# Build Audit — Plan 13 (Jobber FSM Sync)
**Stage:** S4 BUILD-AUDIT · **Date:** 2026-06-23 · read-only review of uncommitted code.

**Verdict: SHIP-WITH-NITS** → nits fixed by orchestrator before commit (below). No P1, no security holes.

## Confirmed
- **F1 applied:** `fsm_sync.sync_clients` calls `db.upsert_suggestion(…, category="customer", source="import-jobber")` directly; never touches `contact_import.ingest`/`presort`. Tests assert this (and assert ingest is never called).
- **F2 applied:** `recommended_setup` changed in all 3 places (connections.py signature kwarg + rows entry; app.py call site).
- **F3 applied:** `contact_import.ingest` docstring updated + NOTE explaining the bypass.
- **Security:** tokens via `db.set_oauth_tokens` (encrypted, never logged); all DB ops scoped by `business_id`; mutating routes enforce `_csrf_ok()` + `@login_required`; OAuth `state` verify-and-consume; all HTTP errors swallowed/fail-open (never raise into a reply or break a booking).
- **Correctness:** token refresh fail-open returns None; pagination bounded; booking push is a guarded daemon thread; `maybe_sync_all` respects `FSM_SYNC_INTERVAL_HOURS` via `businesses.fsm_last_synced_at`.
- **Tests meaningful:** F1 assertion has real discriminating power; interval tests use real time deltas; no trivial passes.
- Double-checks: threading imported ✓; F4 column reuse ✓; F5 tier note in 3 surfaces ✓; CSRF works (via fallback) ✓.

## Nits found → FIXED before commit
- **N1** dead `clients_synced` computation in `fsm_sync.maybe_sync_all` → removed.
- **N2** manual "Sync now" + connect-time background sync didn't update the synced-count → both now call `db.set_fsm_sync_stamp` (counter no longer stale until the nightly tick).
- **N3** dead `window._csrf` reference in `settings.html` fetch headers → simplified to `(document.querySelector('[name=_csrf]')||{}).value`.
- **DC2** "N clients imported" was misleading (it's total fetched, not net-new added) → relabeled "N clients synced" + "Synced clients appear as suggestions" (honest: they go to the review queue, not straight into contacts).

Post-fix re-verify: `test_fsm_sync.py` 78/0, `test_setup.py` 147/0, `test_sf8_connections.py` 99/0; `import app` clean.
