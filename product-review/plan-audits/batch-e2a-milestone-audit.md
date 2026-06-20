# Batch E2a — Progressive ROI Milestones Audit

**Scope:** Uncommitted diff to `roi.py`, `db.py`, `app.py` (plan 07 Change 3, E2a foundation).  
**Auditor:** read-only correctness + honesty pass.  
**Date:** 2026-06-19  
**Out of scope:** monthly_roi scan (E2b), analytics.html timeline (E2c).

---

## Verdict

**SHIP-WITH-FIXES**

The structural refactor is sound: no-walk-down, back-compat, UNIQUE/INSERT-OR-IGNORE idempotency, and all four honesty invariants are correctly implemented. One P1 fix is required before shipping — the `notify_async` context must include `level` so a rapid back-to-back booking pair can't suppress a progression SMS via the shared 120s dedupe key — and one P2 note documents a pre-existing low-frequency edge.

---

## Findings

| Severity | File:line | Issue | Fix |
|----------|-----------|-------|-----|
| **P1** | `app.py:779–781` | `notify_async` context omits `level`, so the `_dedupe_key` for `roi_milestone` always resolves to `"roi_milestone:None"` for every level. If two bookings arrive within 120 seconds — booking N crosses level 2, booking N+1 crosses level 5 — the second `notify_async` call hits `db.alert_recent("roi_milestone:None", 120)` and returns `[]` (deduped). The level-5 SMS is silently dropped. The DB row for level 5 IS written correctly via `mark_roi_milestone`; only the SMS push is lost and the level can never re-fire. | Add `"level": m["level"]` to the context dict passed to `notify_async`. Then in `alerts.py::_dedupe_key` add a branch: `if kind == "roi_milestone": return f"roi_milestone:{context.get('level')}"`. This makes each level its own dedupe key, eliminating the collision. |
| **P2** | `app.py:779` | `notify_async` is queued before `mark_roi_milestone` (line 782). A process crash in the narrow gap between the two would leave the notification fired but the DB row absent, causing a re-fire on the next booking. | Low frequency; try/except already wraps the block. Preferred fix: swap order so `mark_roi_milestone` is called first, then `notify_async`. This makes the DB the source of truth and worst-case is a missed SMS (not a double-SMS). Acceptable to defer to E2b. |

---

## Verified-good

### Correctness: no-walk-down and no-re-fire

`check_roi_milestone` iterates `reversed([2, 5, 10, 25])` and returns on the **first** (highest) level satisfying both `roi_multiple >= level` AND `level > max_fired`. `max_fired = max(fired)` is the ceiling — no level at or below it can ever match.

Traced scenarios (all correct):

| Multiple | Fired set | Returns |
|----------|-----------|---------|
| 5.0 | {10} | None — cannot walk down to 5 |
| 12.0 | {10} | None — not yet at 25 |
| 25.0 | {10} | 25 |
| 25.0 | {25} | None — idempotent |
| 11.0 | {} | 10 — highest crossed, not 5 or 2 |

A tenant can never re-fire a level or walk down from a higher level.

### Back-compat: legacy tenant with `roi_milestone_sent_at` set

`roi.py:86–87`:
```python
if biz.get("roi_milestone_sent_at"):
    fired.add(2)
```
This injected synthetic `{2}` into the fired set even when the `roi_milestones` table has no row. The 07-audit Blocker 2 (plan step-by-step vs Risk note contradiction) was resolved correctly: `_fire_roi_milestone` calls **both** `db.mark_roi_milestone` (line 782) AND `db.set_roi_milestone_sent` at level 2 (lines 783–785). A legacy tenant at 5x will correctly receive the level-5 alert and will never re-receive level-2.

Traced:

| Tenant state | Multiple | max_fired | Returns |
|---|---|---|---|
| `roi_milestone_sent_at` set, no table row | 2.0 | 2 | None |
| `roi_milestone_sent_at` set, no table row | 5.0 | 2 | 5 |
| `roi_milestone_sent_at` set, no table row | 10.1 | 2 | 10 (skips 5, goes highest) |

### UNIQUE constraint + INSERT OR IGNORE under concurrent bookings

`db.mark_roi_milestone` (`db.py:3121–3129`) uses `INSERT OR IGNORE` against `UNIQUE(business_id, level)`. Under a true concurrent racing double-booking where both calls pass the check before either writes, the second insert is silently ignored. The `alerts._dedupe_lock` + 120s dedupe key additionally collapses concurrent `notify_async` threads for the same level within 2 minutes. Data integrity is guaranteed; the P1 above is about cross-level dedupe interference, not same-level concurrency.

### `_fire_roi_milestone` never raises

`app.py:770–788`: entire body is inside `try: ... except Exception as _me: print(...)`. A milestone failure cannot propagate to break the booking confirmation reply.

### Both booking sites call the helper once at the correct point

- `open_conversation` (`app.py:1858`): called immediately after `alerts.notify_async(biz, "booking", _book_ctx)`, once per booking branch.
- `handle_inbound` (`app.py:1963`): called immediately after `alerts.notify_async(biz, "booking", _book_ctx)`, once per booking branch.

Neither site calls the helper more than once per booking event. No double-fire within a single booking.

### Migration idempotency

`db.py:844–852`:
```python
c.execute("""CREATE TABLE IF NOT EXISTS roi_milestones (...)""")
c.execute("CREATE INDEX IF NOT EXISTS idx_roi_milestones_biz ON roi_milestones(business_id)")
```
`CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` — fully idempotent, re-runnable. The `roi_milestone_sent_at` column guard (`ALTER TABLE ADD COLUMN` behind `if col not in biz_cols`) is unchanged and untouched.

### Honesty invariants (06 §225)

All four milestone levels verified:

| Level | "estimated" | avg_source label | loss-framing tail | No "cash"/"collected"/"actual" |
|-------|------------|-----------------|-------------------|-------------------------------|
| 2 | yes — "booked an estimated $X" | yes — "(Estimate based on {avg_label}.)" | yes | clean |
| 5 | yes — "an estimated $X" | yes | yes | clean |
| 10 | yes — "an estimated $X in jobs" | yes | yes | clean |
| 25 | yes — "an estimated $X in booked jobs" | yes | yes | clean |

`_LOSS_TAIL` ("Without FirstBack, those calls go unanswered and the job likely goes to a competitor.") is appended by `return core + _LOSS_TAIL` at `roi.py:50`, unconditionally for all four branches. No level implies collected cash or an "actual" dollar figure.

Gates still intact in the new `check_roi_milestone`: `compliance.a2p_ready` (line 65), `booked_n >= 1` (line 76), `roi_multiple >= 2.0` (line 80).

### Imports available

`app.py` top-level: `import sys` (line 10), `from datetime import datetime` (line 13), `import db` (line 24), `import alerts` (line 31). `roi` is lazy-imported inside the helper (`import roi as _roi_mod`) to match existing pattern. No import error risk.

### Helper defined before call sites

`_fire_roi_milestone` is defined at `app.py:770`. Both call sites are at lines 1858 and 1963 — well after the definition.

### 07-audit blockers for E2a

- **Blocker 1** (`monthly_roi` kind not registered) — out of scope for E2a (that's E2b `scan_monthly_roi`). Not present in this diff.
- **Blocker 2** (back-compat contradiction) — **resolved correctly** by E2a. Both `mark_roi_milestone` and `set_roi_milestone_sent` are called at level 2 (`app.py:782–785`).
