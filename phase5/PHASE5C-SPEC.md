# Phase 5c — F07 Screening Graduation (BUILD SPEC, LOCKED)

**Date:** 2026-06-18 · Opus orchestrator · Base: `staging` @ e50ccaf (50/50 green).
**Source:** `autonomy-plans/F07-FINAL.md` (P0 S1/S2/S4 + P1 M1/M2/S3+M3).
**Built as 2 file-disjoint slices: CORE (engine+graduation) + UI (endpoints+surfaces).**

## What exists (verified)
Scoring engine `triage.spam_score`/`screen_caller` (triage.py:70-183), `screening_stats`/
`global_spam_count`/`add_spam_flag`/`set_screen_mode` (db.py), `_screen_missed_caller`/
`_effective_screen_mode`/`_missed_call_textback` (app.py:2248+), the `api_engage_screened_call`
(re-engage) + `flag-spam` endpoints (app.py:1915-1976), and the dashboard screened-calls strip.
**`businesses` has NO `created_at`** — the graduation clock uses its own window column.

## NON-NEGOTIABLE GATES (bake in)
- **[P1] Rescue ships WITH/BEFORE graduation, and a rescue ALWAYS defers promotion.** The
  "This was real" tap resets the observation window; the graduation job promotes ONLY when the
  current window is clean (≥7d old, ≥10 would-block verdicts, no rescue since window start).
  Never silence a real homeowner with no recovery.
- **Precision-first stays.** Do NOT lower HARD; velocity burst (+35) still needs corroboration
  to reach 80. Crowd signal keeps CROWD_MIN=2 (one tenant can't poison the ledger).
- **Monitor never blocks.** Only `enforce` suppresses; graduation is the deliberate, announced
  auto-path (7-day monitor + a notification with a pause link) — distinct from the 5b
  `enforce_ack` 2-tap (which guards the assistant "enforce NOW" command; do NOT regress it).
- **Fail-open preserved**: reputation errors/timeouts already return {} → no signal.

## SHARED SEAM CONTRACT (CORE provides ↓ — UI builds against these; no file overlap)
**New `businesses` columns** (CORE adds via db.py ALTER-migration; backfill noted):
- `screening_window_start TEXT` — observation window start; backfill existing rows = now();
  the graduation job lazy-inits NULL → now() (and skips that pass).
- `screening_false_positives INTEGER DEFAULT 0` — lifetime rescue count (display + transparency).
- `screen_hard INTEGER`, `screen_mid INTEGER` — per-tenant sensitivity overrides (NULL = inherit config).
- `reputation_enabled INTEGER DEFAULT 0` — per-tenant paid-tier toggle.
- `screening_promoted_at TEXT` — set when graduation flips to enforce.
- `screening_hold INTEGER DEFAULT 0` — "keep in observe mode" (defer graduation when set).

**CORE functions (UI consumes):**
- `db.record_screening_rescue(business_id, number)` → `set_contact(bid, number, "customer", source="owner-rescue")`
  + `screening_false_positives += 1` + `screening_window_start = now()` (atomic). Returns the contact/None.
- `db.global_spam_count(number, exclude_business_id=None, within_hours=None)` — add `within_hours`
  (WHERE created_at >= now-Nh) for the burst signal. (CORE's `screen_caller` calls it; UI doesn't.)
- `triage.screen_caller(business_id, number, *, attestation=None, neighbor_spoof=False,
  reputation=None, behavior=None, hard=None, mid=None)` — new `hard`/`mid` band overrides
  (default to `SCREEN_SCORE_HARD`/`MID`). Internally also reads `burst_count =
  db.global_spam_count(number, exclude_business_id=bid, within_hours=24)` and feeds `spam_score`.
- `config.SCREEN_SENSITIVITY_PRESETS = {"conservative": (90, 55), "balanced": (80, 45),
  "aggressive": (65, 35)}` (UI maps the radio → thresholds), `SCREEN_GRADUATION_DAYS = 7`,
  `SCREEN_GRADUATION_MIN_VERDICTS = 10`.

---

## SLICE CORE — engine + auto-graduation (`db.py`, `triage.py`, `reminders.py`, `alerts.py`, `config.py`)
**NO app.py, NO templates.**

1. **db.py** — the 7 columns above (ALTER if absent; backfill window_start=now()); `within_hours`
   on `global_spam_count`; `record_screening_rescue`; a `promote_screening(business_id)` (set
   `screen_mode='enforce'`, `screening_promoted_at=now()` atomically); reuse `screening_stats(bid,
   since=window_start)` for the in-window count (use its `would_screen` = monitor screened_spam).
2. **triage.py** — `spam_score`: new `burst_count` signal — `if burst_count >= 3: score += 35;
   reason "calling dozens of businesses in the past hour"` (corroboration still required to reach
   HARD). `screen_caller`: accept `hard`/`mid` overrides for the band checks; read burst via
   `db.global_spam_count(..., within_hours=24)` and pass it into `spam_score`.
3. **reminders.py** — `scan_screening_graduation(now)`, wired into `tick_once` (try/except like the
   other scans). For each business: compute effective mode inline (`biz['screen_mode'] if in
   (off,monitor,enforce) else config.SCREEN_MODE`); skip unless effective == `monitor` and not
   `screening_hold`. Lazy-init NULL window_start → now() (skip this pass). Promote when
   `now - window_start >= SCREEN_GRADUATION_DAYS` AND `screening_stats(bid, since=window_start)
   ['would_screen'] >= SCREEN_GRADUATION_MIN_VERDICTS` → `db.promote_screening(bid)` +
   `alerts.notify(biz, "screening_graduated", {n: would_screen})`. (A rescue reset window_start →
   clock restarts → no promotion. This IS the safety valve.)
4. **alerts.py** — register `screening_graduated` (ALERT_KINDS + `_TOGGLE_COL` → `alert_on_urgent`,
   no new column) + `format_message` (honest: "Spam blocking is now ON — this week we'd have
   blocked {N} robocallers and you rescued none. Manage or pause it in Settings." NEVER claim a
   customer was contacted) + `_subject`. Dedupe: once per business (key `screening_graduated`,
   long window) — promotion happens once anyway.

**CORE test `test_screening_graduation.py`** (standalone, real DB): graduation fires at ≥7d +
≥10 would-block verdicts; does NOT fire <7d, <10, when `screening_hold`, or when effective mode
isn't monitor; a `record_screening_rescue` resets the window so a subsequent pass does NOT promote
(the ordering gate) + increments false_positives + upserts the number as `customer`; burst ≥3 in
24h adds +35 (and <3 adds 0); `within_hours` filters the ledger; `hard`/`mid` overrides move the
verdict band; on promotion `screen_mode='enforce'` + `screening_promoted_at` set + ONE graduation
alert to the owner (never a customer). Re-run `test_triage.py`/`test_screening.py`/`test_reputation.py`.

---

## SLICE UI — endpoints + surfaces (`app.py`, `templates/*.html`, `static/*.js`)
**NO db.py/triage.py/reminders.py/alerts.py/config.py edits** (call CORE's functions only).

1. **`_screen_missed_caller`** — resolve per-tenant thresholds: `hard = biz.get('screen_hard') or
   SCREEN_SCORE_HARD`, `mid = biz.get('screen_mid') or SCREEN_SCORE_MID`; pass `hard=`/`mid=` into
   both `triage.screen_caller` calls. Gate the paid lookup on a new `_effective_reputation_enabled(biz)`
   = `reputation.configured() and bool(biz.get('reputation_enabled'))` (parallel to `_effective_screen_mode`).
2. **Rescue endpoint** `POST /api/calls/<int:call_id>/real` (login_required, tenant-scoped, refuse
   opted-out): `db.record_screening_rescue(biz['id'], caller)` THEN re-engage exactly like
   `api_engage_screened_call` (open conversation + send text-back if thread empty, `mark_call_engaged`).
   Return ok + lead_id. (This is S2; it MUST exist for graduation to be safe.)
3. **Settings POST** (`/settings` handler): sensitivity radio (`screen_sensitivity` →
   `SCREEN_SENSITIVITY_PRESETS` → write `screen_hard`/`screen_mid` via `db.update_business`);
   reputation toggle (`reputation_enabled`); keep-in-observe (`screening_hold`). Render current values.
4. **Command center** — "This was real" button on `screened_spam` call rows (posts to the rescue
   endpoint); a blocked-counter stat pill from `screening_stats` ("Blocked N robocallers this
   month — N texts saved"); a "Spam Shield: Learning (Day N of 7)" card during the window and a
   "Shield active — N blocked, 0 false positives. [Pause]" card after `screening_promoted_at`.
5. **static/*.js** — wire the "This was real" button + settings interactions.

**UI test `test_screening_ui.py`** (standalone, `app.test_client`): rescue endpoint upserts the
number as `customer` + increments false_positives + re-engages (and a 2nd tap doesn't double-text);
per-tenant `screen_hard`/`screen_mid` actually change a borderline verdict via `_screen_missed_caller`;
reputation toggle off → no paid lookup even if configured, on → lookup runs; the blocked counter +
graduation card render; opted-out number can't be rescued. Re-run `test_screening.py`/`test_toggles_hub.py`/`test_config_hub.py`.

## MERGE ORDER (Opus)
CORE → UI (UI builds against CORE's columns/functions). Full suite green + un-stubbed e2e: a real
monitor-mode business with 10 seeded would-block calls over a >7d window graduates to enforce and
fires ONE owner alert; a rescue mid-window defers it; a rescued number is `trusted` on its next
call. Security/consent pass: rescue is tenant-scoped + refuses opted-out; graduation alert never
implies a customer was texted; crowd/burst still need corroboration.

## DEFERRED (honest) — P2 from F07-FINAL
Spoof-of-known-contact badge, country/international block, per-tenant self-flag weight, the richer
graduation "confidence" animation. Operator-ops: `FIRSTBACK_REPUTATION_PROVIDER` on Render (already
in SETUP_NEEDED) gates the paid tier; the per-tenant toggle is built but inert until that's set.
