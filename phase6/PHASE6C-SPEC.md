# Phase 6c (code) — Integration EDGE CASES — BUILD SPEC
**Date:** 2026-06-19 · Base: `staging` @ 03c6a4c (61/61 green) · Orchestrator: Opus
**Source:** `PLAN-INTEGRATION.md` W4–W7 (the non-blocking reliability/correctness edge cases). Ground-truthed directly against the code (small surgical fixes; no prebuild agent wave). Serial build (db.py + reminders.py collide). be-audit gate after.

> NOTE — this is the OPTIONAL Track-A code for 6c; the 6c GO-LIVE RUNBOOK is owner-ops (separate). None of W4–W7 are launch-blockers.

## W5 — failed growth touch can re-queue (real correctness)
The dedupe is the partial UNIQUE index `uniq_growth_touch_per_lead` (db.py ~783) with predicate `WHERE kind NOT IN (...) AND status!='canceled'`. A `failed` touch (status='failed') still satisfies `status!='canceled'`, so it **holds the (lead, kind) slot forever** → that growth touch can never be re-queued in a future cycle (e.g. next month's review request), even though the customer never received it. **Fix:** change the predicate to `status NOT IN ('canceled','failed')` so a failed row frees its slot. Mechanics: (a) add a rebuild trigger — if the existing index SQL lacks `failed`, DROP+recreate (mirror the existing `sms_retry` rebuild check); (b) update BOTH the pre-rebuild de-dup DELETE and the CREATE predicate to `status NOT IN ('canceled','failed')`.
- The owner-alert half is ALREADY covered: `_enqueue_retry` (reminders.py:284-292) fires `sms_fail` after 3 failed attempts for any kind. No new alert needed.

## W7 — cancel_appointment atomicity (cosmetic correctness)
`cancel_appointment` (db.py:2892) commits the appointment-cancel + lead-status on `conn`, closes, THEN calls `cancel_appointment_reminders` on a SEPARATE connection. A crash in between orphans pending reminders (shown as "skipped" not "canceled"; run_due_once's live-status recheck already prevents any double-text, so this is cosmetic). **Fix:** inline the reminder-cancel UPDATE onto the same `conn` BEFORE commit (one transaction); drop the post-close call. Keep `cancel_appointment_reminders` defined (single caller today, but a public helper).

## W6 — followup vs quote_followup mutual exclusion (defensive)
A warm-but-cold lead can be eligible for both an automated `followup` (Touch-1) and a `quote_followup` growth play. In tray mode quote_followup is held (owner-released) so they don't auto-collide today; this guards the future auto path. **Fix:** in `scan_followups`, right after a Touch-1 is queued (`t1_id is not None`, reminders.py:449), call `db.cancel_lead_growth_touches(lead["id"], ("quote_followup",))`. The helper (db.py:2614) cancels PENDING touches only — it leaves the owner's HELD tray plays untouched, so this never removes a play the owner is deciding on; it only prevents two AUTO sends.

## W4 — ticker budget guard → RIGHT-SIZED to observability only
The plan's full W4 (stagger heavy scans across ticks, move contacts-sync to a separate cron, per-tick skip budget) is a **scale optimization** premature at single-tenant / single-worker launch. Building it now adds complexity with no current benefit. **Right-sized build:** measure `tick_once` wall-duration and, when it exceeds a soft budget (`max(30, int(TICK_SECONDS*0.8))`), log a one-line warning with the duration — so a slow tick becomes VISIBLE in logs (and can later feed an alert) without rearchitecting. The stagger / separate-cron / Redis rate-limit are DEFERRED-UNTIL-SCALE (documented; not built).

## TESTS (new `test_phase6c.py`, standalone)
- W5: a `failed` growth touch frees its slot — after marking a review_request `failed`, `add_scheduled_message(... review_request)` for the same lead succeeds (returns an id, not None); and the live index SQL contains `failed`.
- W6: queuing a Touch-1 followup cancels a PENDING `quote_followup` for that lead, but leaves a HELD growth play untouched.
- W7: `cancel_appointment` cancels the appointment's pending reminder in the SAME call (no orphan), returns the row, rejects a cross-tenant id.
- W4: `tick_once` still returns its result dict and doesn't raise (the timing/warn path is observability; verify it's inert on a fast tick).

## GATES
- be-audit (correctness + any consent surface) after build. No deploy; pricing unchanged; voice "coming soon".
- W5 migration is the riskiest line — verify it's idempotent (re-running init_db doesn't thrash) and doesn't drop real data.
