# Phase 6c Build Audit — 6C-BUILD-AUDIT.md

**Date:** 2026-06-19
**Auditor:** be-audit (read-only correctness pass)
**Suite:** 62/62 green (including 12/12 new test_phase6c.py)
**Files changed:** `db.py` (+23/-6), `reminders.py` (+20/0)
**Scope:** W4–W7 from PLAN-INTEGRATION (non-blocking reliability/correctness edge cases)

---

## Summary verdict

**1 P1 finding.** 0 P0. The W5 DB migration is idempotent and data-safe. W7, W6, and W4 are correct. The P1 is a functional gap in W5: the unique-index predicate was updated but `growth_touch_index` was not, so the "failed touch frees its slot for re-queue" goal is only partially achieved — the DB constraint allows the INSERT but `plays()` still suppresses the re-offer.

---

## A. W5 — migration data-safety (HIGHEST PRIORITY)

### A-i. Idempotency

**Pass.** The rebuild trigger at `db.py:775` checks whether the existing index SQL contains both `'sms_retry'` and `'failed'`:

```python
if _idx_sql and ("sms_retry" not in _sql or "failed" not in _sql):
```

After the first successful migration the index SQL is:

```
CREATE UNIQUE INDEX uniq_growth_touch_per_lead ON scheduled_messages(lead_id, kind)
WHERE kind NOT IN ('reminder','followup','sms_retry','morning_reminder')
AND status NOT IN ('canceled','failed')
```

Both `'sms_retry'` and `'failed'` are present, so the condition evaluates to `False` and `_needs_growth_idx_rebuild` stays `False`. Neither DROP nor the pre-rebuild DELETE fires again on subsequent `init_db` calls. Confirmed by live test: two sequential `init_db()` calls produce identical index SQL with no row deletions. Idempotency is solid.

### A-ii. Pre-rebuild DELETE data-safety

**Pass.** The DELETE (`db.py:782–787`) uses `_GROWTH_ACTIVE = "status NOT IN ('canceled','failed')"` in its `WHERE` clause and its inner `MIN(id)` subquery. Only rows whose status is `pending`, `held`, `sent`, or `simulated` are touched. Failed rows are invisible to the DELETE.

**Worst-case walk — one `failed` + one `pending` for the same (lead, kind) across the migration:**

*Can this state exist in a pre-6c production DB?* No. Under the OLD index (`status!='canceled'`), both the `failed` and the `pending` row would be covered by the partial index, so inserting the second (pending) row would have raised `UNIQUE constraint failed` — the DB would never have accepted both rows. The worst realistic pre-migration state is exactly ONE `failed` row holding the (lead, kind) slot.

*What the migration does with one `failed` row:*
1. DROP INDEX removes the constraint. The `failed` row remains.
2. DELETE: `failed` is excluded from `_GROWTH_ACTIVE`, so the failed row is NOT touched.
3. CREATE INDEX: the new index predicate excludes `failed`, so the failed row is NOT indexed — the slot is now free.
4. A subsequent `add_scheduled_message(..., 'review_request', ...)` for that lead inserts cleanly; the failed row coexists with the new pending row, and both survive. Confirmed by live execution.

**No data is deleted that should be kept.** The DELETE only removes genuine active duplicates (can't exist in practice anyway, per the above). Orphaned data risk: none.

### A-iii. Dedupe guarantee for non-failed rows (held/sent still occupy the slot)

**Pass.** The new predicate `status NOT IN ('canceled','failed')` still covers `pending`, `held`, `sent`, and `simulated`. The UNIQUE index therefore prevents two `pending`, `held`, or `sent` rows of the same (lead, kind). Any attempt to insert a second active touch raises `sqlite3.IntegrityError`, which `add_scheduled_message` catches and returns `None` for. The W5 test explicitly verifies this:

```python
dup = db.add_scheduled_message(b5, l5, None, "review_request", _SA, "dup while active")
check("W5: a second ACTIVE review_request is blocked (slot held)", dup is None)
```

Held and sent rows both count as occupying the slot — correct.

### A-iv. Existing production DBs (previously migrated through `sms_retry` rebuild)

**Pass.** DBs that already ran the `sms_retry` rebuild have `sms_retry` in their index SQL but not `'failed'`. The trigger `("sms_retry" not in _sql or "failed" not in _sql)` evaluates to `True` (because `"failed" not in _sql`), so they get one more rebuild. The rebuild is safe per the analysis above. DBs that never ran any rebuild (index missing entirely) also trigger a rebuild — the `"uniq_growth_touch_per_lead" not in sched_idx` branch handles that. All paths converge safely.

---

## B. W7 — transaction correctness (cancel_appointment)

**Pass.** Full analysis:

**Same transaction:** The reminder-cancel UPDATE is at `db.py:2915–2917`, placed between the appointment-status UPDATE (`db.py:2911`) and `conn.commit()` (`db.py:2925`). Both writes are on the same `conn` object. A crash between the two UPDATE calls aborts the whole transaction — no half-written state is possible.

**Row targeting:** `WHERE appointment_id=? AND kind='reminder' AND status='pending'`. Correctly scoped: the appointment FK ensures only reminders for THIS appointment are affected; `kind='reminder'` excludes growth touches; `status='pending'` leaves already-sent or already-canceled reminders alone. Identical to the pre-existing `cancel_appointment_reminders` helper at `db.py:2534–2538`.

**Cross-tenant guard intact:** The guard is the `fetchone` at `db.py:2905–2910`:
```python
row = conn.execute(
    "SELECT * FROM appointments WHERE id=? AND business_id=? AND status='booked'",
    (appointment_id, business_id)).fetchone()
if not row:
    conn.close()
    return None
```
The function returns `None` before any write if the appointment doesn't belong to `business_id` or isn't booked. The inlined UPDATE fires AFTER this check, so cross-tenant reads can't trigger any mutation. W7 test confirms: `db.cancel_appointment(999999, appt_id)` returns None.

**Behavioral equivalence of removed call:** The removed `cancel_appointment_reminders(appointment_id)` call (`db.py:2927` pre-diff) executed an identical `UPDATE scheduled_messages SET status='canceled' WHERE appointment_id=? AND kind='reminder' AND status='pending'`. The inlined UPDATE is character-for-character equivalent. No behavior is dropped; the only change is atomicity (improvement).

**`cancel_appointment_reminders` still defined and safe:** The function remains at `db.py:2532`. The only caller was `cancel_appointment`; that caller was removed. No external callers found (`grep -rn cancel_appointment_reminders` returns only the definition). It is now an orphan. Leaving it defined is safe — it's a pure DB helper with no side effects and no import-time execution.

---

## C. W6 — followup vs quote_followup mutual exclusion (defensive)

**Pass.** Full analysis:

**`cancel_lead_growth_touches` cancels PENDING only:** `db.py:2628–2630` filters `AND status='pending'`. A `held` tray play is not canceled. W6 test verifies both:
- `PENDING quote_followup` → becomes `canceled` after call.
- `HELD quote_followup` → status unchanged (`held`).

**Placement inside `t1_id is not None` block:** `reminders.py:449–459`. The call is at line 455, inside `if t1_id is not None:`. It only fires when a Touch-1 was actually queued (not on every loop iteration). Correct.

**Exception wrapper:** Wrapped in `try/except Exception` with `stderr` logging (`reminders.py:455–459`). A failure in the growth-touch cancel cannot propagate up and abort the followup-queue loop or break the Touch-2 queuing that follows at line 462. Safe.

**Consent / fewer-sends direction:** Canceling a pending `quote_followup` reduces the number of texts a customer might receive. This is the conservative, consent-respecting direction. No new sends, no new consent surface.

**Double-cancel / ordering issue with booking-time cancel:** The existing booking-time `cancel_lead_growth_touches` call fires when a lead BOOKS (from a separate code path in the appointment-booking flow). The W6 cancel fires when Touch-1 queues during `scan_followups`. These are independent events; the second cancel on an already-canceled row is a no-op (`SET status='canceled' WHERE ... AND status='pending'` matches 0 rows). No ordering hazard.

---

## D. W4 — ticker budget guard (observability only)

**Pass.** Full analysis:

**Pure observability:** The `_tick_started` capture (`reminders.py:857`) and the budget check (`reminders.py:934–939`) contain no branching that alters control flow, return values, or the behavior of any scan. The `if _tick_elapsed > _tick_budget:` branch only writes to `sys.stderr`.

**Cannot raise:** `time.monotonic()` does not raise. The subtraction is between two monotonic floats, guaranteed to be non-negative. `max(30, int(TICK_SECONDS * 0.8))` is safe: `TICK_SECONDS` is an int (config.py:517: `_num_env("TICK_SECONDS", 60, int)`), so `int(TICK_SECONDS * 0.8)` is always a valid integer, and `max(30, ...)` never goes below 30.

**Return dict unchanged:** `tick_once` returns `{"queued": ..., "growth_queued": ..., "sent": ..., "contacts_synced": ...}` at `reminders.py:940`. These assignments are unchanged from before W4. The W4 code adds only local variables prefixed `_tick_` that are never included in the return dict. W4 test verifies: `isinstance(out, dict) and "sent" in out`.

**`time` already imported:** `reminders.py:25`. No new import.

---

## E. Tests — coverage adequacy

**12/12 pass.** Coverage check:

| Test | What it asserts | Adequate? |
|------|----------------|-----------|
| W5: first review_request queued | DB index allows insert | Yes |
| W5: second ACTIVE review_request blocked | Dedupe still holds | Yes |
| W5: after FAIL, slot frees → re-queue succeeds | Index predicate fixed | Yes |
| W5: unique index SQL contains 'failed' | Index was rebuilt | Yes |
| W6: PENDING quote_followup canceled | cancel helper scopes pending | Yes |
| W6: HELD tray quote_followup untouched | cancel helper skips held | Yes |
| W6: scan_followups cancels on Touch-1 | Integration path wired | Yes |
| W7 setup: reminder is pending | Precondition | Yes |
| W7: cancel_appointment returns row | Function works | Yes |
| W7: reminder canceled in SAME call | Atomicity verified | Yes |
| W7: cross-tenant id rejected | Guard intact | Yes |
| W4: tick_once returns result dict | Observability is inert | Yes |

**Gap:** No test covers the `growth_touch_index` inconsistency (see P1 below). The W5 tests verify the DB insert path (`add_scheduled_message`) succeeds after a failed touch, but do NOT verify that `plays()` or `scan()` re-offer the touch — the other half of the re-queue story.

---

## Findings

### P1 — W5 incomplete: `growth_touch_index` still includes `failed` rows, blocking re-offer in `plays()` and `scan()`

**File:** `db.py:2611`
**Severity:** P1 (fix before launch — the stated W5 goal is not fully achieved)

The W5 fix correctly changes the partial UNIQUE index predicate to `status NOT IN ('canceled','failed')`, so `add_scheduled_message(...)` can INSERT a new pending touch alongside a failed one (the DB constraint no longer blocks it). However, `growth_touch_index` at `db.py:2610–2612` still uses `status!='canceled'`:

```python
rows = conn.execute(
    "SELECT lead_id, kind FROM scheduled_messages "
    "WHERE business_id=? AND status!='canceled' "
    "AND kind NOT IN ('reminder','followup')", (business_id,)).fetchall()
```

This means a `failed` row for (lead, kind) is still returned in the `touched` index that `plays()` (`growth.py:221`) and `scan()` (`growth.py:408`) use. In `plays()`, the guard `"review_request" not in kinds` evaluates `False` (kinds contains `review_request` because of the failed row), so the play is suppressed. `scan()` calls `plays()`, so it never re-queues either.

**End-to-end result:** A lead whose growth touch failed will never have that play re-offered to the owner or auto-queued in a future cycle — the exact bug W5 was meant to fix. The DB constraint fix is a necessary but insufficient component; the application-layer index query must also exclude `failed`.

**Fix:** Change `db.py:2611` from `status!='canceled'` to `status NOT IN ('canceled','failed')`. Also update the docstring on `growth_touch_index` to reflect the new intent.

**Why not P0:** The spec marks W5 "real correctness" but not a launch-blocker (W5 is part of the "non-blocking reliability/correctness edge cases" track). The failure mode is silent suppression of re-queuing (the lead doesn't get re-texted when they should in a future cycle), not a double-send or data corruption. At single-tenant launch volume with `status='failed'` being rare (only after 3 consecutive Twilio failures), the practical blast radius is small. Escalate to P0 if A2P failures are expected to be common during the initial live period.

---

## Non-issues (explicitly confirmed clean)

- **`cancel_appointment_reminders` orphan (db.py:2532):** Defined-but-unused is safe. No external callers. Leave defined per spec.
- **W6 exception swallowing:** The `try/except` in `scan_followups` only covers the non-critical `cancel_lead_growth_touches` call; the Touch-1 queue and Touch-2 queue are outside it. Not a silent data loss.
- **W7 `still_booked` query after reminder cancel:** The `still_booked` check at `db.py:2919–2921` re-uses `conn` after the reminder UPDATE. All three writes are on the same connection in one transaction — correct.
- **W4 budget calculation with default TICK_SECONDS=60:** `max(30, int(60 * 0.8))` = `max(30, 48)` = 48 seconds. A reasonable soft threshold; no overflow, no negative value possible.
- **W5 migration on a brand-new (empty) DB:** The `_needs_growth_idx_rebuild = True` branch via `"uniq_growth_touch_per_lead" not in sched_idx` fires. The DELETE is a no-op on an empty table. The CREATE INDEX succeeds. Correct.
- **Test isolation:** `test_phase6c.py` uses a tempfile DB, sets `FIRSTBACK_PROVIDER=demo`, and patches `messaging.TWILIO_ACCOUNT_SID`. No cross-test state leakage observed.

---

## Verdict

**0 P0, 1 P1.** The build is structurally sound. W7, W6, and W4 are correct and complete. The W5 DB migration is idempotent, data-safe, and preserves the dedupe guarantee for all non-failed active rows. The sole finding (P1) is that `growth_touch_index` was not updated alongside the index predicate, so the re-queue path that W5 intends to enable is blocked at the application layer. Fix `db.py:2611` before the growth engine is live on real Twilio.
