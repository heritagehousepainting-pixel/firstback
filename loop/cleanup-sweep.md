# Cleanup Sweep Loop

Date: 2026-06-23

Goal: clean the codebase before the next feature push, without changing product behavior beyond hardening inconsistent cross-cutting patterns.

## Scope

1. Docs truth
   - Align current README and handoff-style docs with the actual repo state.
   - Remove stale branch/test/provider claims where they are likely to mislead future work.

2. CSRF consistency
   - Keep existing SameSite/session behavior.
   - Make authenticated mutating app routes consistently check CSRF.
   - Prefer one shared JS/header pattern so JSON, form, and multipart requests all use the same token.

3. Dependency and tooling hygiene
   - Pin direct runtime dependencies.
   - Add lightweight formatter/lint/test configuration so future cleanup has a stable target.

4. Hot-file governance
   - Do not split `app.py`, `db.py`, or `assistant.py` in this sweep.
   - Document how changes to those files should be staged, reviewed, and eventually extracted.

## Non-Goals

- No new product features.
- No behavioral rewrite of onboarding, billing, Twilio, AI, or voice.
- No schema split or migration framework introduction in this pass.
- No deployment or external service changes.

## Execution Log

- [x] Create this loop plan.
- [x] Fix docs truth drift.
  - `README.md` now reflects the safe local run posture and current Claude/demo provider reality.
  - `HANDOFF.md` now marks stale branch/test-count/deploy claims as historical unless re-verified.
  - `SETUP_NEEDED.md` now uses the current daily cap env var and avoids treating old staging notes as active gates.
  - `run_local.sh` now describes provider inheritance accurately instead of always claiming demo.
- [x] Normalize CSRF checks and token transport.
  - `_csrf_ok()` accepts `_csrf` form fields and `X-CSRF-Token` headers.
  - `static/app.js` attaches the token to unsafe `apiFetch()` requests.
  - Authenticated mutating form, JSON, and multipart routes now check CSRF consistently.
  - Templates and standalone route tests were aligned with the shared contract.
- [x] Add dependency/tooling hygiene.
  - Direct runtime dependencies now have bounded ranges in `requirements.txt`.
  - `requirements-dev.txt` installs runtime deps plus Ruff.
  - `pyproject.toml` adds a quiet bootstrap Ruff profile for undefined-name checks.
  - `tools/run_tests.sh` runs the repo's standalone `test_*.py` suite with `.venv` fallback handling.
- [x] Document hot-file governance.
  - See `loop/hot-files.md`.
- [x] Run focused tests and broad verification.
  - `.venv/bin/python test_import.py`
  - `.venv/bin/python test_scheduling.py`
  - `.venv/bin/python test_sf7_sentinel.py`
  - `.venv/bin/python test_sf8_signup_fork.py`
  - `.venv/bin/python test_assistant.py`
  - `.venv/bin/python test_screening.py`
  - `.venv/bin/python test_setup.py`
  - `.venv/bin/python test_triage.py`
  - `.venv/bin/python test_f08_nightly.py`
  - `.venv/bin/ruff check --statistics`
  - `tools/run_tests.sh`

## Result

Cleanup sweep completed. The full standalone test suite exits 0 in a fresh `.venv`, and Ruff exits 0 with the
bootstrap correctness profile.
