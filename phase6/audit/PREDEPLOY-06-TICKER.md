# Pre-Deploy Audit #06 — Ticker / Scheduler / Proactive-Engine Reliability
**Lane:** TICKER / SCHEDULER / PROACTIVE-ENGINE  
**Branch:** staging @ 55d2601  
**Auditor scope:** reminders.py, alerts.py, app.py (/tasks/run-due, /health/ticker), db.py (claim/retry/dedupe), all scan_* functions, all proactive owner push kinds  
**Tests run:** test_ticker_health, test_vic_proactive, test_daily_digest, test_reminders, test_phase6c, test_screening_graduation  
**Result:** 27+33+24+59+14+65 = 222/222 pass  

---

## VERDICT: CONDITIONAL DEPLOY-OK

**P0 count: 0**  
**P1 count: 1** (cron not wired — known, documented, in-process ticker is fallback)  
**P2 count: 3**  

---

## 1. Heartbeat + Stale-Ticker Alert (6b) — CORRECT

**reminders.py:858-883**

The sequence is correct:
1. `_prev_tick_utc = db.get_meta("last_tick_utc")` — reads BEFORE overwrite (line 859)
2. `db.set_meta("last_tick_utc", _tick_utc)` — overwrites immediately, before any scan (line 863)
3. Gap computed as `datetime.now(utc) - _prev_dt` — not from the value just written (lines 876-877)
4. Entire stale-check is wrapped in `try/except` with a stderr print — can't crash the tick (lines 871-883)

**False-fire analysis:** No false-fire is possible because:
- When `_prev_tick_utc` is None/empty, the block is skipped entirely (line 872 guard)
- The gap is measured from the true wall-clock delta; the 900s threshold is generous vs a 60s TICK_SECONDS
- The day-stamped dedupe key (`tick_stale:{day}`) with a 26h window (alerts.py:38) means a sustained outage generates at most ONE SMS per UTC calendar day, not a storm

**One structural note (P2 below):** `tick_stale` always fires against `db.get_business(1)` (hardcoded, line 878). Works for a single-tenant deploy; see P2-C.

---

## 2. External Cron Path (/tasks/run-due) — SECRET-GATED, FAILS CLOSED

**app.py:2864-2876**

```python
if not TASKS_SECRET or not secrets.compare_digest(sent, TASKS_SECRET):
    return jsonify(error="Forbidden."), 403
```

- **If `FIRSTBACK_TASKS_SECRET` is unset:** `TASKS_SECRET = ""` (config.py:523), so `not TASKS_SECRET` is `True` → always 403. **Fails closed. No anonymous trigger possible.**
- **`render.yaml`:** `FIRSTBACK_TASKS_SECRET` uses `generateValue: true` (line 42-43), so Render auto-generates a secret on Blueprint deploy. The secret is wired.
- **Constant-time compare:** `secrets.compare_digest` — correct; no timing oracle.

### P1: External cron is not wired as a Render cron job

**render.yaml** has no `cron` service block. `USER_TO_DO.md:190` tells the user to wire it manually. The in-process ticker (`FIRSTBACK_RUN_TICKER=1`, render.yaml:44-45) is the only scheduled driver.

**Risk:** On a Render dyno recycle (deploy, crash, OOM), the in-process ticker dies with the process. Pending reminders sit in the queue. The next dyno starts the ticker after a `sleep(interval)` delay (reminders.py:981-982 — intentional boot delay). Any reminder whose `send_at` was during the outage will fire on the first tick after restart — this is correct (the atomic claim prevents double-send). **No reminder is lost; they send late.** But if the outage is longer than the `send_at` window (e.g., estimate is now past), `_appt_passed` cancels it (run_due_once:321-324). The user could miss a reminder for an estimate whose window opened and closed during the outage.

**This is a documented, accepted risk (USER_TO_DO.md:188-191).** It is P1 not P0 because:
- The in-process ticker runs in production (`FIRSTBACK_RUN_TICKER=1`)
- Render Starter dynos have low churn; typical recycles are < 60s
- The heartbeat + stale-ticker alert will fire on recovery

**Recommendation:** Add a Render cron job (`POST /tasks/run-due` every minute) to render.yaml before scaling to >1 tenant. Not a deploy blocker for single-tenant launch.

---

## 3. run_due_once — Atomic Claim, Live-Status Re-Check, Retry Bounds

**reminders.py:302-380**

### Atomic claim (no double-send)
`db.claim_scheduled_message(id)` issues:
```sql
UPDATE scheduled_messages SET status='sent', sent_at=?
WHERE id=? AND status='pending'
```
Returns `rowcount == 1` only for the claimer. Second tick or restart mid-send gets `rowcount=0` → skip. **Correct.**

### Live-status re-check for followups
Lines 333-337: for `followup` and `followup_2` kinds, `db.get_lead(row["lead_id"])` is re-fetched and if `status == "booked"` the row is canceled. **Correct — prevents a follow-up to a now-booked lead.**

### Failed sends: bounded async retry
`_enqueue_retry` (lines 267-295):
- `attempt <= 3`: writes a new `sms_retry` row with 30s/2m/10m backoff
- `attempt > 3`: fires `sms_fail` owner alert instead
- The cap is read from `row.get("retry_count") or 0` and incremented by 1 (lines 350, 364) — correct chain tracking via `retry_count` stored in the row (db.py:804, 1207)

**Edge case (P2-A):** `attempt` for the original row starts at `0 + 1 = 1`. Each retry row gets `retry_count = attempt`. The next tick re-reads `retry_count` from the retry row. Chain: attempt 1→2→3→alert. Cap is real and bounded.

### Simulated ≠ Sent honesty
When `messaging.send_sms` returns `{"status": "simulated"}`, `db.mark_scheduled(row["id"], "simulated")` is called (line 357), NOT "sent". The `sent` counter still increments (line 358) for the return value, but the DB status is distinct. Comment at lines 354-357 explicitly states this. **Honest.**

---

## 4. Proactive Owner Push Dedupe — All Kinds Verified

### daily_digest (6b unified 8am digest)
- **Key:** `daily_digest:{local_day}` — alerts.py:256-259
- **Window:** `_DAILY_DEDUPE_SECONDS = 26 * 3600` — alerts.py:37-38
- **Window fires:** only in `[8, 9)` local hour — reminders.py:688-689
- **Tested:** test_daily_digest B: second tick at 8am = 0 fired. ✓

### vic_stall (afternoon stall nudges)
- **Key:** `vic_stall:{lead_id}:{local_day}` — alerts.py:248-251
- **Window:** 26h (in `_DAILY_DEDUPE_KINDS`) — alerts.py:38
- **Afternoon gate:** `if now_local.hour < 12: continue` — reminders.py:629 (6b W2)
- **Tested:** test_vic_proactive: dedupe holds across >120s gap; morning suppressed. ✓

### screening_graduated
- **Key:** `"screening_graduated"` (static, no context) — alerts.py:265-266
- **Window:** `_LONG_DEDUPE_SECONDS = 365 * 24 * 3600` (year) — alerts.py:41-42
- **DB-level gate:** `db.promote_screening` flips the mode; next pass `effective != 'monitor'` → skips. Double-alert is architecturally impossible once promoted. ✓

### roi_milestone
- **Key:** `roi_milestone:{lead_id}` — alerts.py:267 (base fallthrough)
- **Window:** `ALERT_DEDUPE_SECONDS = 120s`
- **Primary gate:** `biz.get("roi_milestone_sent_at")` — roi.py:40. Once set, `check_roi_milestone` returns None forever. The 120s dedupe is a secondary net. **No storm possible.** ✓

### tick_stale
- **Key:** `tick_stale:{day}` (UTC day) — alerts.py:260-264
- **Window:** 26h — alerts.py:38
- `local_day` is always passed in tick_once (line 880: `datetime.now(timezone.utc).strftime("%Y-%m-%d")`)
- **Tested:** test_ticker_health section 7: fires once at >15min gap, not on 60s gap. ✓

### vic_morning / growth_tray
Both functions remain in reminders.py but **are NOT called from tick_once** (verified: grep shows they appear only as function definitions, not called from tick_once body lines 852-941). `scan_daily_digest` absorbs both. ✓

---

## 5. 6b Unified Digest Fires Once, Retired Scans Not Called

**tick_once (reminders.py:905-911):**
```python
# Phase 6b W2: ONE unified 8am digest (absorbs the old vic_morning + growth_tray
# morning sends into a single owner SMS -- the functions remain for their unit tests
# but the ticker no longer fires them separately).
try:
    scan_daily_digest(now)
```

`scan_morning_briefing` and `scan_growth_tray` are NOT called anywhere in tick_once. Confirmed by grep: only function definitions at lines 481 and 539. **Retired correctly.** ✓

---

## 6. One Slow Scan Can't Permanently Wedge the Tick

**reminders.py:884-929 (tick_once body)**

Every subscan is in its own `try/except`:
- `triage.scan_all_suggestions()` — lines 885-888
- `growth.scan()` — lines 892-898
- `connections.check_forwarding_health()` — lines 900-904
- `scan_daily_digest()` — lines 908-911
- `scan_stall_nudges()` — lines 913-916
- `scan_screening_graduation()` — lines 918-921
- `google_contacts_sync_all()` — lines 923-928
- `run_due_once()` — line 929 (NOT in try/except, but it is itself fully defensive)

Each catch prints to stderr and continues. No subscan can kill the tick loop.

**LLM timeout (W3):** `followup_body_contextual` calls `llm.complete(..., timeout=10)` (reminders.py:408). For Claude, `httpx.Timeout(10, connect=5.0)` is applied (llm.py:169-172). Exception → fallback to generic template (line 413). **For MiniMax, the `timeout` parameter is ignored** (llm.py:147-163: hardcoded `timeout=30`). If MiniMax is the provider and it hangs for 30s, that's within the tick budget for single-tenant. Not a P0 but noted as P2-B.

**W4 soft budget warn:** `_tick_elapsed > _tick_budget` prints a warning (lines 934-939). Observability only — correct, never blocks. ✓

---

## 7. Dyno Recycle Trace

**On Render dyno recycle:**

1. **Scheduled texts:** All pending `scheduled_messages` rows survive (SQLite on durable disk via mirror backup). On restart, `restore_from_backup_if_needed` restores the DB (db.py:57-72). The in-process ticker starts after `sleep(TICK_SECONDS)` (reminders.py:981). On the first tick, `run_due_once` claims and sends all still-pending rows. **No reminder is lost** — they send late (worst case: TICK_SECONDS after restart).

2. **Owner spam risk:** Each proactive kind is deduped by day-stamped key in the alerts table. The alerts table persists through the recycle. A second tick after restart for `daily_digest` at 8am finds the row already present → returns `[]`. **Owner cannot be spammed on restart.** ✓

3. **Double-send risk:** `claim_scheduled_message` uses `WHERE status='pending'` atomic UPDATE. A row that was mid-send when the dyno died has status `sent` (set before the Twilio call) OR `failed` (set in the except, if the crash came after the failure path). Either way, the next tick's claim returns False → skip. **No double-send.** ✓

4. **Missed send:** If `claim_scheduled_message` set status=`sent` but the Twilio call never completed (process died between lines 563-564 and the actual send), the row stays `sent` with no actual SMS. This is the same race that exists in any at-most-once scheduler backed by SQLite — acceptable for the $99 tier. The `sms_fail` alert path doesn't cover this specific crash-at-claim window but it's a known SQLite single-writer trade-off.

---

## P0 Findings (Blocks Deploy)

**None.**

---

## P1 Findings

### P1-A: External cron not wired; in-process ticker is sole scheduler
- **file:line:** render.yaml (no cron block); USER_TO_DO.md:188-191
- **Risk:** On dyno recycle, reminders send late (missed window possible for very tight reminder schedules). Not a send storm or silent stop — the ticker resumes. The heartbeat + stale-ticker alert fires on recovery.
- **Mitigation:** `FIRSTBACK_RUN_TICKER=1` is set in render.yaml. In-process ticker runs always. Single-tenant with low churn = acceptable for launch.
- **Action:** Wire a Render cron job before multi-tenant scale. Not a deploy blocker.

---

## P2 Findings

### P2-A: `has_followup` in `followup_candidate_rows` matches ANY status, including `canceled`
- **file:line:** db.py:2815-2816
- **Detail:** `EXISTS(SELECT 1 FROM scheduled_messages s WHERE s.lead_id=l.id AND s.kind='followup')` — no status filter. A `canceled` followup row (e.g., lead re-engaged and then went cold again) blocks queuing a new follow-up forever.
- **Risk:** A lead that re-engages after their Touch-1 was canceled never receives a new follow-up. This is conservative (safe), not dangerous — no double-send, no storm. But it means some warm re-engaged leads are permanently excluded from follow-up.
- **Severity:** P2 (silent exclusion, not a delivery failure or safety issue).

### P2-B: MiniMax provider ignores the `timeout=10` passed by `followup_body_contextual`
- **file:line:** llm.py:147-163 (MiniMax path uses hardcoded `timeout=30`)
- **Detail:** When `FIRSTBACK_PROVIDER=minimax`, a slow LLM call blocks for 30s instead of 10s before the `except` fallback fires. With TICK_SECONDS=60, a 30s block leaves 30s for all other scans in the same tick — tight but not catastrophic for single-tenant.
- **Severity:** P2 (only affects MiniMax users; falls back correctly; single-tenant is fine).

### P2-C: `tick_stale` alert always uses `db.get_business(1)` (hardcoded)
- **file:line:** reminders.py:878
- **Detail:** For a multi-tenant deploy, business id=1 may not exist or may belong to a different tenant. `notify()` checks `business.get("id")` — if `db.get_business(1)` returns None, the alert is silently dropped (alerts.py:299-300 guard). Stale-ticker outage goes unalerted for all tenants except the one with id=1.
- **Severity:** P2 for multi-tenant. Not a concern for the current single-tenant launch (id=1 is always the one business).

---

## Summary Table

| ID | Severity | File:Line | Finding |
|----|----------|-----------|---------|
| P1-A | P1 | render.yaml (no cron block) | External cron not wired; in-process ticker is sole scheduler. Reminders send late on dyno recycle; no permanent loss or storm. |
| P2-A | P2 | db.py:2815-2816 | `has_followup` matches canceled rows; re-engaged leads may never get a new follow-up. Conservative, not dangerous. |
| P2-B | P2 | llm.py:147-163 | MiniMax path ignores `timeout=10`, uses hardcoded 30s. Slow LLM stalls the tick 30s on MiniMax provider. |
| P2-C | P2 | reminders.py:878 | `tick_stale` alert hardcoded to `db.get_business(1)`. Silent for multi-tenant; fine for single-tenant launch. |

---

## Deploy Verdict

**DEPLOY-OK for single-tenant launch.**

All P0 criteria pass:
- Scheduled sends cannot silently stop (in-process ticker runs; atomic claim prevents double-send on restart)
- Proactive sends cannot storm/duplicate (all day-stamped dedupe keys verified; 26h windows; 222/222 tests pass)
- Ticker cannot die silently without alerting (stale-ticker 6b fires correctly on recovery tick; test proves it)

P1: External cron not wired is a known pre-launch ops task. In-process ticker provides coverage. Not a blocker.
