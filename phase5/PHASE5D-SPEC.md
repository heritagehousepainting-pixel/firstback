# Phase 5d -- F13 Growth Engine / Tray (BUILD SPEC, LOCKED)

**Date:** 2026-06-18 · Opus orchestrator · Base: `staging` (after 5a/5b/5c, ~50+ tests green).
**Source:** `autonomy-plans/F13-FINAL.md` (definitive plan, all 8 sections),
`phase5/PREBUILD-SYNTHESIS.md` (F13 row + held-rows hazard + TCPA gate).
**Goal:** Turn the existing `plays()`/`scan()` engine into the full tray autopilot:
`growth_mode` off/tray/auto + `status='held'` with ATOMIC query guard + morning tray SMS +
one-tap batch release + frequency cap + audit log. Built as 3 file-disjoint slices.

---

## 1. NON-NEGOTIABLE GATES (bake into every slice)

**[P0] `growth_mode='auto'` UI-locked until earned-trust L2 streak.**
  `auto` is stored in the DB from day 1 (for future unlock) but is visible-and-not-clickable in
  the UI. Auto-sending marketing to past customers without Dave's tap = TCPA exposure.
  Tray/off ONLY until L2 (7-day YES streak) ships. Win-backs always stay in tray, never auto.

**[P0] The `held`-rows query guard is the single most critical correctness point.**
  Inserting `status='held'` rows without simultaneously excluding them from `due_scheduled_messages`
  means they auto-fire on the next ticker tick (within <=60s). The exclusion guard, the insert
  path, and the release API must be committed ATOMICALLY in one slice. No partial states allowed.
  CURRENT: `due_scheduled_messages` (db.py:2452) queries `status='pending'` only -- so 'held'
  rows are already safe once they are inserted as 'held'. The guard just makes this explicit and
  locked. The insert in `scan()` (growth.py:353) currently inserts `status='pending'` and must
  be changed to `status='held'` ONLY after the exclusion is verified.

**[P0] One-tap stays one-tap.**
  Every customer-facing growth send requires Dave's tap on a 5a server-bound token OR the batch
  release (`release_growth_batch`). The batch release IS the approval event (Dave saw names,
  dollar value, approved). No silent sends ever. `release_growth_batch` flips held->pending;
  `reminders.run_due_once` delivers through the full compliance gate.

**[P0] Dollar framing is honest.**
  Money figures come from real past jobs (avg_job_value, booked count). When avg_job_value is
  unset, use the trade-keyword default (see S6) but label it "(estimated)". Never show $0 to Dave.

**[P0] Owner-cell only for all tray/digest sends.**
  The 8am digest SMS goes to `business["alert_sms"]` via `alerts.notify(biz, "growth_tray", {...})`
  with `gate=False` -- A2P-exempt, same pattern as 5b morning digest. NEVER to a customer number.

---

## 2. CURRENT-STATE AUDIT (verified, file:line)

| Item | Current state | File:line |
|---|---|---|
| `growth_on` boolean | `INTEGER DEFAULT 0` in businesses | db.py:534 |
| `set_growth_on` | Sets `growth_on` 0/1 | db.py:953-960 |
| `growth_on()` in growth.py | Returns `bool(int(business.get("growth_on") or 0))` | growth.py:313-319 |
| `scan()` | Iterates businesses, calls `plays()`, inserts as `status='pending'` | growth.py:328-356 |
| `add_scheduled_message` | Always inserts `status='pending'` (hardcoded string) | db.py:2402 |
| `due_scheduled_messages` | Queries `status='pending'` only -- 'held' rows already excluded | db.py:2452 |
| `claim_scheduled_message` | Atomic `UPDATE WHERE status='pending'` | db.py:2462 |
| `growth_touch_index` | `status!='canceled'` scan -- will include 'held' rows (correct: blocks re-queue) | db.py:2506-2513 |
| `uniq_growth_touch_per_lead` index | `WHERE kind NOT IN ('reminder','followup','sms_retry','morning_reminder') AND status!='canceled'` -- 'held' status is NOT 'canceled', so index correctly blocks re-queuing the same play while held | db.py:717-720 |
| `run_due_once` | Processes all `due_scheduled_messages` rows; no growth-kind branching | reminders.py:296-352 |
| `tick_once` | Calls `growth.scan(now)` then `run_due_once(now)` | reminders.py:571-601 |
| quiet-hours backstop | ALREADY SHIPPED (Phase 5a) -- `messaging.send_sms` with `transactional=False` returns `{"status": "deferred"}` | messaging.py:120-134 |
| START re-subscribe | ALREADY SHIPPED (Phase 5a) -- `consent.opt_in_nlu` + `db.set_opt_in` in inbound handler | app.py:2512-2521 |
| `_issue_token` | Shipped (5a): `assistant.py:1931`, `db.issue_confirm_token` | assistant.py:1931 |
| `alerts.notify` / dedupe | `ALERT_KINDS`, `_DAILY_DEDUPE_KINDS`, `notify()` pattern | alerts.py:30-258 |
| Settings handler | Handles screening, reminders, scheduling, screening prefs -- NO growth_mode field | app.py:1118-1209 |
| App.py growth routes | NONE -- no `/growth/*` or `/settings/growth_mode` routes exist | app.py (none found) |
| `growth_touch_log` table | MISSING | -- |
| `growth_approvals` table | MISSING | -- |
| `growth_mode` column | MISSING (only `growth_on` exists) | -- |
| Morning tray digest | MISSING (distinct from 5b `vic_morning` briefing digest) | -- |
| Batch release API | MISSING | -- |
| Tray reply parser | MISSING | -- |

**Key confirmed facts:**
- `due_scheduled_messages` queries `WHERE s.status='pending'` (db.py:2452), so inserting as
  'held' is already safe today -- but scan() inserts as 'pending' (db.py:2402, hardcoded),
  which is the bug. Changing the insert and adding explicit status guard are the same atomic commit.
- `growth_touch_index` includes 'held' rows (status!='canceled'), which is CORRECT: the dedupe
  index prevents re-queuing the same play while it's held. No change needed here.
- G1 (quiet-hours backstop) and G2 (START re-subscribe) from F13-FINAL are ALREADY SHIPPED
  in Phase 5a. They are NOT part of 5d. Both confirmed in code: messaging.py:120-134,
  app.py:2512-2521. Do not re-implement.
- `_issue_token` (assistant.py:1931) is the 5a server-bound token. Growth batch release does NOT
  use `_issue_token` -- the batch release IS the approval event (Dave sees the tray and sends GO/
  taps "Send All"). Individual play one-taps via the Vic feed still route through `_issue_token`.

---

## 3. SCHEMA / MIGRATION

All migrations use the `db.py:init_db()` executescript/ALTER pattern. Existing DBs pick up new
tables/columns at next boot via `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ... ADD COLUMN`.

### 3A. `businesses` column: `growth_mode TEXT DEFAULT 'off'`

Add in the businesses migration block (after the `growth_on` entry at db.py:534):

```python
("growth_mode", "TEXT DEFAULT 'off'")
```

This is additive. `growth_on` stays in the schema for backward compat (legacy code that reads it
still gets 0; the new `growth_mode()` function in growth.py supersedes it).

Set `growth_mode` from `growth_on` for existing opted-in rows (one-time backfill):
```sql
UPDATE businesses SET growth_mode='tray' WHERE growth_on=1 AND growth_mode='off';
```

Valid values: `'off'` | `'tray'` | `'auto'`. `'auto'` is stored but UI-locked (no button enables
it until L2 streak ships in a later phase).

### 3B. `growth_touch_log` table (new)

```sql
CREATE TABLE IF NOT EXISTS growth_touch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL,
    lead_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    outcome     TEXT DEFAULT 'sent',
    source      TEXT DEFAULT 'batch_approved'
);
CREATE INDEX IF NOT EXISTS idx_gtl_biz_lead
    ON growth_touch_log(business_id, lead_id, sent_at);
```

`outcome` values: `'sent'` | `'simulated'` | `'replied_positive'` | `'review_landed'` (future GBP).
`source` values: `'batch_approved'` | `'sms_go'` | `'ui_tap'` | `'auto'` (future).

Written by `reminders.run_due_once` on actual delivery of growth kinds (not on 'held' insert).

### 3C. `growth_approvals` table (new)

```sql
CREATE TABLE IF NOT EXISTS growth_approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id     INTEGER NOT NULL,
    batch_id        TEXT NOT NULL,
    lead_id         INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    approved_at     TEXT NOT NULL,
    approved_via    TEXT NOT NULL,
    consent_basis   TEXT
);
CREATE INDEX IF NOT EXISTS idx_gappr_biz
    ON growth_approvals(business_id, approved_at);
```

`approved_via`: `'sms_go'` | `'ui_tap'`.
`consent_basis`: `'EBR:last_job:YYYY-MM-DD'` | `'EBR:quote:YYYY-MM-DD'` | `'EBR:inbound:YYYY-MM-DD'`.
`batch_id`: a `secrets.token_hex(8)` generated at release time (groups all rows from one GO).

### 3D. `scheduled_messages` status guard

No schema change needed. `due_scheduled_messages` (db.py:2452) already queries `status='pending'`.
The ONLY change: `scan()` inserts `status='held'` instead of `'pending'` (growth.py:353).

To make the exclusion explicit and self-documenting, add a SQL comment in `due_scheduled_messages`:
```sql
-- status='pending' excludes 'held' (growth batch awaiting approval), 'sent', 'simulated', etc.
WHERE s.status='pending' AND s.send_at <= ?
```

---

## 4. NEW DB FUNCTIONS (SLICE ALPHA owns all of these)

```python
# db.py additions

def set_growth_mode(business_id, mode):
    """Set growth mode: 'off'|'tray'|'auto'. Invalid values reset to 'off'."""

def growth_mode(business_id):
    """Return 'off'|'tray'|'auto' for the business. Default 'off'."""

def list_held_messages(business_id):
    """All status='held' scheduled_messages rows for a business, with lead name+phone."""

def release_growth_batch(business_id, approved_via='ui_tap'):
    """Atomically flip all status='held' -> status='pending' for the business.
    Also writes growth_approvals rows (one per play, same batch_id).
    Returns {'released': n, 'batch_id': hex}. Callers must pass approved_via."""

def release_growth_play(scheduled_message_id, business_id, approved_via='ui_tap'):
    """Flip a single 'held' row to 'pending'. Scoped by business_id (no cross-tenant).
    Writes one growth_approvals row. Returns True if released."""

def cancel_growth_play(scheduled_message_id, business_id):
    """Set status='canceled' for one held play. Scoped by business_id."""

def recent_growth_touch(business_id, lead_id, within_days):
    """True if any growth_touch_log row for (biz, lead) is within the last within_days days."""

def growth_touch_count_12mo(business_id, lead_id):
    """Count of growth_touch_log rows for (biz, lead) in the past 365 days."""

def add_growth_touch_log(business_id, lead_id, kind, outcome='sent', source='batch_approved'):
    """Write one row to growth_touch_log at delivery time."""

def consent_basis_for_lead(business_id, lead_id):
    """Return a consent_basis string ('EBR:last_job:YYYY-MM-DD' etc.) by inspecting
    appointments + messages for the lead. Used at release time for growth_approvals."""
```

---

## 5. SHARED SEAMS / FILE OWNERSHIP (file-disjoint, no collision)

| File | Owner slice | What changes |
|---|---|---|
| `db.py` | **ALPHA only** | new tables (3B/3C), new column `growth_mode` (3A), 8 new functions (§4), scan() exclusion comment |
| `growth.py` | **ALPHA only** | rename `growth_on()` -> `growth_mode()`, update `scan()` to branch on mode, insert 'held' not 'pending', add `_job_value()` trade defaults, add tone-risk flag to `plays()` |
| `reminders.py` | **BETA only** | new `scan_growth_tray(now)` for 8am digest, `tick_once` wire-in, `run_due_once` growth-kind branch for `growth_touch_log` write |
| `app.py` | **GAMMA only** | `GET /growth/tray`, `POST /growth/tray/release`, `POST /growth/tray/skip/<id>`, `POST /settings/growth_mode`, tray-reply intent branch in inbound SMS handler, `POST /growth/reply-outcome` |
| `alerts.py` | **BETA only** | add `'growth_tray'` to `ALERT_KINDS`, `_DAILY_DEDUPE_KINDS`, `_TOGGLE_COL`, `format_message`, `_dedupe_key`, `_subject` |
| `templates/growth_tray.html` | **GAMMA only** | new tray page |
| `templates/settings.html` | **GAMMA only** | Growth Autopilot card (off/tray/auto radio, auto locked) |
| `test_growth_tray.py` | **ALPHA** | DB + growth.py + batch-release unit tests |
| `test_growth_tray_sms.py` | **BETA** | digest SMS + reply parser tests |
| `test_growth_tray_ui.py` | **GAMMA** | route + template tests |

**Cross-slice read-only contract (no write seam):**
- BETA reads `db.list_held_messages` (ALPHA-defined) -- lazy import pattern, same as 5b.
- GAMMA reads `db.list_held_messages`, `db.release_growth_batch`, `db.cancel_growth_play` (ALPHA).
- GAMMA reads `alerts.notify` for `'growth_tray'` kind (BETA adds the kind).
- ALPHA does NOT touch reminders.py, app.py, alerts.py, or templates.
- BETA does NOT touch db.py (new tables/functions), growth.py, app.py, or templates.
- GAMMA does NOT touch db.py or growth.py.

**Files NOT touched in 5d:** assistant.py, messaging.py (quiet-hours already shipped), consent.py,
tc_messaging.py, compliance.py, ai.py, billing.py, auth.py, triage.py, voice_service.py.

**Cross-sub-phase collision map:**
- `db.py`: ALPHA only. 5e (F06) may also touch db.py -- serialize (5d ALPHA completes first).
- `app.py`: GAMMA only. 5e may also touch app.py -- serialize.
- `reminders.py`: BETA only. 5e may touch reminders.py -- serialize.
- `alerts.py`: BETA. Not touched by 5e/5f/5g. Safe.
- `growth.py`: ALPHA. Not touched by any other sub-phase. Safe.
- `test_growth.py`: NOT touched by 5d (existing tests stay green). New test files only.

---

## 6. SLICE ALPHA -- DB + GROWTH ENGINE (db.py, growth.py)

**ALPHA commits all three schema additions atomically: new tables + column + scan() change.**
The migration guard pattern: each table uses `CREATE TABLE IF NOT EXISTS`; the `growth_mode`
column uses the `ALTER TABLE ... ADD COLUMN` pattern with a `biz_cols` check (exactly as
db.py:596-617 for screening columns). Include the backfill `UPDATE` immediately after.

### A1. `growth_mode` migration + `set_growth_mode` / `growth_mode()` functions (db.py)

In the businesses migration block at db.py:534, after `("growth_on", "INTEGER DEFAULT 0")`:
```python
("growth_mode", "TEXT DEFAULT 'off'")
```
Immediately after the loop, add the backfill:
```python
c.execute("UPDATE businesses SET growth_mode='tray' WHERE growth_on=1 AND growth_mode='off'")
```

Add `set_growth_mode(business_id, mode)` and `growth_mode(business_id)` near `set_growth_on`
(db.py:953). Valid modes: `'off'`, `'tray'`, `'auto'`; anything else -> store `'off'`.

### A2. `growth_touch_log` + `growth_approvals` tables (db.py)

Add as `c.executescript(...)` blocks in `init_db()`, after the `stripe_events` table
(db.py:648-656). Use `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`.

Add the 8 new functions from §4. Place them near `growth_candidates` (db.py:2481).

`release_growth_batch` implementation notes:
- Use a single connection; run in a transaction.
- SELECT all `status='held'` rows for `business_id` first (to get lead_ids and kinds for approvals).
- Generate `batch_id = secrets.token_hex(8)` once per call.
- `UPDATE scheduled_messages SET status='pending' WHERE business_id=? AND status='held'`
- For each held row, call `consent_basis_for_lead(business_id, lead_id)` and INSERT one
  `growth_approvals` row.
- Commit atomically.
- Return `{'released': rowcount, 'batch_id': batch_id}`.

`consent_basis_for_lead` logic:
- Find `MAX(a.day)` from `appointments WHERE business_id=? AND lead_id=? AND status='booked'`
  -- if found, return `f"EBR:last_job:{day}"`
- Else find last outbound message date -- return `"EBR:quote:YYYY-MM-DD"`
- Else return `"EBR:inbound"`.

### A3. `scan()` mode branching + 'held' insert (growth.py)

Replace `growth_on(business)` call (growth.py:337) with `growth_mode(business)` check:
```python
mode = db.growth_mode(biz["id"])   # 'off'|'tray'|'auto'
if mode == 'off':
    continue
# mode == 'tray': insert as 'held' (batch-approval required)
# mode == 'auto': insert as 'pending' ONLY for review_request kind; all others go 'held'
insert_status = 'pending' if (mode == 'auto' and p["kind"] == 'review_request') else 'held'
```

In `add_scheduled_message` call (growth.py:353), pass `status=insert_status`:
```python
sid = db.add_scheduled_message(biz["id"], p["lead_id"], None, p["kind"],
                               send_at_iso, p["draft_body"], status=insert_status)
```
This requires updating `add_scheduled_message` signature (db.py:2393) to accept
`status='pending'` as an optional param (default 'pending' for non-growth callers).

### A4. `_job_value()` trade-keyword defaults (growth.py:67-71)

```python
_TRADE_DEFAULT_VALUE = {
    "paint": 2500, "roof": 8000, "hvac": 3500, "plumb": 1200,
    "landscap": 1500, "lawn": 1500,
}

def _job_value(business):
    try:
        v = int(business.get("avg_job_value") or 0)
        if v > 0:
            return v
    except (ValueError, TypeError):
        pass
    trade = (business.get("trade") or "").lower()
    for key, default in _TRADE_DEFAULT_VALUE.items():
        if key in trade:
            return default
    return 2000  # generic default; never show $0
```
Note: dollar framing in the tray digest must label the total "(estimated)" when `avg_job_value`
is unset so Dave knows it's a trade-based estimate, not a real figure.

### A5. Frequency cap gate in `scan()` (growth.py)

After the existing `sendable`/`lead_id`/`draft_body`/`"["` guards (growth.py:345-348), add:
```python
# Cross-kind 30-day frequency cap (G3)
if db.recent_growth_touch(biz["id"], p["lead_id"], within_days=30):
    continue
# 12-month rolling cap (max 2 touches per customer per year)
if db.growth_touch_count_12mo(biz["id"], p["lead_id"]) >= 2:
    continue
```

### A6. Tone-risk flag in `plays()` (growth.py)

Add `_tone_risk(lead_id)` helper: loads last 5 inbound messages from `db.get_messages(lead_id)`
(already loaded in `run_due_once`'s biz_cache pattern, but `plays()` doesn't cache -- do a
targeted `db.get_last_n_inbound(lead_id, n=5)` helper query). Scan for negative-signal keywords:
`["terrible", "awful", "unhappy", "disappointed", "complaint", "never again", "rip off"]`.
If any match, add `"tone_risk": True` to the play dict.

In `scan()`: if `p.get("tone_risk")`, always insert as `'held'` (never auto-send), regardless of mode.

### A7. Win-back TCPA narrowing (growth.py:236-242)

Add an inbound-initiated check before the winback play:
```python
# Win-back TCPA posture: only customers who initiated at least one inbound text
# (they contacted us; EBR is stronger). Cold-only leads excluded until Phase 2 opt-in.
had_inbound = any(m["direction"] == "in" for m in db.get_messages(lid))
if not had_inbound:
    continue  # skip win-back for cold-only leads
```
Note: `db.get_messages(lid)` is already called in `plays()` context; add a helper or pass the
growth_candidates data. The simplest approach: add `has_inbound` to `growth_candidates` as a
correlated subquery (one extra column, no N+1).

Add to `growth_candidates` query (db.py:2486-2498):
```sql
EXISTS(SELECT 1 FROM messages m WHERE m.lead_id=l.id AND m.direction='in') AS has_inbound
```

### A8. Blocked-play visibility for missing review link (growth.py:344-348)

Currently: plays with `"["` in draft body are silently skipped. Change: insert a play dict
with `"sendable": False`, `"blocked_reason": "add_review_link"` instead of skipping entirely.
The tray UI (GAMMA) renders these as grayed cards.
Keep the hard guard: never queue a `"["` body to `scheduled_messages`. The visibility change
is in the `plays()` output only, not in `scan()`.

### ALPHA TESTS (test_growth_tray.py)

Must run standalone (real temp DB, no network). Run FULL suite (test_growth.py must stay green).

1. `growth_mode`: set 'tray' -> `growth_mode(biz_id)` returns 'tray'; set invalid -> returns 'off'.
2. Backfill: existing `growth_on=1` row after migration has `growth_mode='tray'`.
3. `scan()` with mode='tray' inserts `status='held'`, not 'pending'.
4. `scan()` with mode='off' queues nothing (regression: same as old `growth_on=False`).
5. `scan()` with mode='auto': `review_request` kind inserts as 'pending'; `winback` inserts as 'held'.
6. `due_scheduled_messages` does NOT return 'held' rows (the core correctness proof).
7. A re-scan does not double-queue a 'held' play (dedupe index blocks it).
8. `release_growth_batch`: flips all 'held'->'pending' for the biz (not other biz), writes
   `growth_approvals` rows with correct `approved_via`, returns `released` count.
9. `release_growth_play`: flips one 'held'->'pending', writes one approval row, scoped by biz.
10. `cancel_growth_play`: flips 'held'->'canceled', does not touch other statuses.
11. `recent_growth_touch(biz, lead, 30)`: True when a touch exists within 30 days; False outside.
12. `growth_touch_count_12mo`: correctly counts; past 12-month boundary excluded.
13. Frequency cap: a lead with a touch 20 days ago has no play surfaced in `scan()`.
14. `_job_value()`: returns trade default for `avg_job_value=0` or None; returns real value when set.
15. Win-back: lead with NO inbound messages -> no winback play; lead WITH inbound -> winback fires.
16. Tone-risk: lead with negative inbound text -> play has `tone_risk=True`; in scan() -> 'held'
    regardless of mode.
17. Blocked play (missing review link): `plays()` returns play with `sendable=False` and
    `blocked_reason='add_review_link'`; `scan()` still does NOT queue it.
18. `consent_basis_for_lead`: returns 'EBR:last_job:YYYY-MM-DD' for a customer with bookings.
19. `add_growth_touch_log` + read back via `recent_growth_touch`.

---

## 7. SLICE BETA -- DIGEST + DELIVERY (alerts.py, reminders.py)

### B1. Register `growth_tray` alert kind (alerts.py)

Add `'growth_tray'` to `ALERT_KINDS` (alerts.py:31), `_DAILY_DEDUPE_KINDS` (alerts.py:37),
`_TOGGLE_COL` (alerts.py:51, map to `'alert_on_lead'` -- no new column needed),
`format_message` (context keys: `count`, `total_str`, `plays_summary`, `is_estimated`),
`_dedupe_key` (day-stamped: `f"growth_tray:{context.get('local_day', '')}"`, same as
`vic_morning` pattern at alerts.py:159-161), and `_subject` entry.

Format template:
```
Good morning. {count} text{s} ready: {plays_summary}. ~{total_str} on the table{estimated}.
Reply GO to send all, SKIP to hold, or SKIP N to skip play #N.
```
Where `estimated` = ` (estimated)` when `is_estimated=True`, else `""`.
Cap at 160 chars per SMS segment; truncate the plays_summary first (keep the GO/SKIP instruction).

`plays_summary` is assembled by the caller (BETA's `scan_growth_tray`), e.g.
`"1) Maria (review), 2) Carlos (win-back), 3) Tom (follow-up)"`.

### B2. `scan_growth_tray(now)` in reminders.py

New function, wired into `tick_once` between `scan_morning_briefing` and `run_due_once`.

```python
def scan_growth_tray(now=None):
    """Fire ONE 8am growth tray digest per business per local day when there are
    held growth plays awaiting approval. Deduped via alerts.notify's 'growth_tray' kind.
    Returns count of digests fired."""
```

Logic:
1. `now = now or db.now_iso()`
2. For each business via `db.list_businesses()`:
   a. Resolve local time via `_biz_tz(biz)`.
   b. Fire ONLY when local hour is exactly `8` (window: `8 <= now_local.hour < 9`).
   c. Query `db.list_held_messages(biz["id"])` -> rows.
   d. If no held rows: skip (nothing to send).
   e. Assemble plays_summary (max 10, per batch-size cap from F13-FINAL §4 G10).
   f. Compute total money: sum play money values. Detect if avg_job_value is unset
      (`biz.get("avg_job_value") is None`) -> `is_estimated=True`.
   g. total_str: `f"~${total:,}"`.
   h. `local_day = now_local.strftime("%Y-%m-%d")`.
   i. Call `alerts.notify(biz, "growth_tray", {"count": n, "total_str": total_str,
      "plays_summary": summary, "is_estimated": is_estimated, "local_day": local_day})`.
   j. The dedupe key (`growth_tray:YYYY-MM-DD`) collapses re-ticks on the same day.
3. Return count of digests fired.

Wire into `tick_once` (reminders.py:554) after `scan_morning_briefing` (line ~587):
```python
try:
    scan_growth_tray(now)
except Exception as e:
    print(f"[firstback] growth tray scan failed: {e}", file=sys.stderr, flush=True)
```

### B3. `run_due_once` growth-kind delivery hook (reminders.py)

In the `run_due_once` loop (reminders.py:303-351), after a successful send (`status in ("sent",
"simulated")`), add a growth-touch-log write for growth kinds:

```python
GROWTH_KINDS = {"review_request", "quote_followup", "reactivation",
                "winback", "referral", "membership"}
if status in ("sent", "simulated") and kind in GROWTH_KINDS:
    try:
        db.add_growth_touch_log(
            row["business_id"], row["lead_id"], kind,
            outcome=status, source="batch_approved")
    except Exception as _gle:
        print(f"[firstback] growth_touch_log write failed: {_gle}",
              file=sys.stderr, flush=True)
```

This ensures the frequency cap (`recent_growth_touch`) reflects actual deliveries, not queue state.

### BETA TESTS (test_growth_tray_sms.py)

1. `scan_growth_tray`: fires when local hour == 8 and held plays exist; no fire outside [8,9).
2. No digest when zero held plays (even if hour is 8).
3. Digest fires once per day (second tick same day -> dedupe -> no second SMS).
4. Digest goes to `business["alert_sms"]` (owner cell), NOT to any lead phone (assert recipient).
5. Digest body includes play count, money figure, GO/SKIP instructions (≤ 320 chars).
6. `is_estimated=True` when avg_job_value is NULL -> body contains "(estimated)".
7. `run_due_once`: after a growth-kind `'sent'` delivery, a `growth_touch_log` row exists for
   that (biz, lead, kind); a non-growth kind (reminder/followup) does NOT write to the log.
8. `run_due_once`: after a growth-kind `'simulated'` delivery, log still written with
   `outcome='simulated'` (simulated sends still count for frequency cap).

---

## 8. SLICE GAMMA -- ROUTES + UI (app.py, templates/)

### G1. `POST /settings/growth_mode` (app.py)

New route near the existing `/settings` handler (app.py:1118):
```python
@app.route("/settings/growth_mode", methods=["POST"])
@login_required
def settings_growth_mode():
    biz = current_business()
    mode = (request.form.get("mode") or "off").strip()
    if mode not in ("off", "tray"):  # 'auto' not accepted until L2 unlocks it
        mode = "off"
    db.set_growth_mode(biz["id"], mode)
    return redirect("/settings?growth_saved=1")
```

Note: `auto` is rejected at the server level (not just UI-locked). Even a crafted POST cannot
enable auto until L2 ships. Auto mode requires a separate L2-unlock endpoint.

### G2. `GET /growth/tray` (app.py)

```python
@app.route("/growth/tray")
@login_required
def growth_tray():
    biz = current_business()
    held = db.list_held_messages(biz["id"])
    # Money total (same logic as scan_growth_tray: sum play money)
    # For display, add the lead name + kind label from held rows
    return render_template("growth_tray.html", business=biz, held=held,
                           growth_mode=db.growth_mode(biz["id"]))
```

### G3. `POST /growth/tray/release` (app.py)

```python
@app.route("/growth/tray/release", methods=["POST"])
@login_required
def growth_tray_release():
    biz = current_business()
    result = db.release_growth_batch(biz["id"], approved_via="ui_tap")
    return redirect(f"/growth/tray?released={result['released']}")
```

### G4. `POST /growth/tray/skip/<int:sched_id>` (app.py)

```python
@app.route("/growth/tray/skip/<int:sched_id>", methods=["POST"])
@login_required
def growth_tray_skip(sched_id):
    biz = current_business()
    db.cancel_growth_play(sched_id, biz["id"])
    return redirect("/growth/tray")
```

### G5. Tray-reply intent branch in inbound SMS handler (app.py)

In the inbound Twilio webhook handler (the large handler around app.py:2490-2562), add a branch
BEFORE the main `handle_inbound` call. This runs ONLY when the inbound number matches the
business owner's cell (`biz["alert_sms"]`):

```python
# Growth tray reply: GO / SKIP / SKIP N from the owner's cell
owner_cell = messaging.to_e164((biz.get("alert_sms") or "").strip())
is_owner = owner_cell and caller and messaging.to_e164(caller) == owner_cell
if is_owner:
    tray_cmd = _parse_tray_reply(body)
    if tray_cmd:
        return _handle_tray_reply(biz, tray_cmd)
```

`_parse_tray_reply(body)` (pure function, testable):
- `body.upper().strip() == "GO"` -> `{"cmd": "go"}`
- `body.upper().strip() in ("SKIP", "SKIP ALL")` -> `{"cmd": "skip_all"}`
- `re.match(r"^SKIP\s+(\d+)$", body.strip(), re.IGNORECASE)` -> `{"cmd": "skip_n", "n": int(m.group(1))}`
- else -> `None` (not a tray command; fall through to normal inbound handler)

`_handle_tray_reply(biz, cmd)` (private app.py helper):
- `"go"`: call `db.release_growth_batch(biz["id"], approved_via="sms_go")`, reply
  `"{n} texts queued. They will go out shortly."` via `messaging.send_sms(biz, owner_cell, ..., gate=False)`.
- `"skip_all"`: call `db.cancel_growth_play` for each held row, reply "Held for tomorrow."
- `"skip_n"`: find the Nth held row (ordered by id), cancel it, release the rest, reply
  "Skipped #{n}, sending the rest."
- Return TwiML `<Response/>` (the reply goes via direct `messaging.send_sms`, not TwiML body).

**IMPORTANT:** The tray-reply branch must NOT intercept inbound messages from customers.
Scope guard: `is_owner` check prevents any customer number from triggering a tray command.

### G6. Settings page Growth Autopilot card (templates/settings.html)

Add a "Growth Autopilot" card to the settings page, after the reminder prefs section:

```html
<div class="settings-card">
  <h3>Growth Autopilot</h3>
  <form method="post" action="/settings/growth_mode">
    <label>
      <input type="radio" name="mode" value="off" {% if growth_mode == 'off' %}checked{% endif %}>
      <strong>Off</strong> -- plays appear in Vic's feed only; you approve each one individually.
    </label>
    <label>
      <input type="radio" name="mode" value="tray" {% if growth_mode == 'tray' %}checked{% endif %}>
      <strong>Morning Tray</strong> -- I draft overnight, you get a text at 8am. Reply GO to send all.
    </label>
    <label style="opacity:0.5;pointer-events:none;" title="Unlocks after 7 consecutive morning approvals">
      <input type="radio" name="mode" value="auto" disabled>
      <strong>Auto</strong> (locked -- reply GO every morning for 7 days to unlock)
    </label>
    <button type="submit">Save</button>
  </form>
</div>
```

Pass `growth_mode=db.growth_mode(biz["id"])` from the settings route (GAMMA must update the
`settings()` handler at app.py:1185 to include `growth_mode=db.growth_mode(biz["id"])` in the
`render_template` call -- this is the ONLY change to the existing `settings()` function body).

### G7. Growth tray page (templates/growth_tray.html)

- Header: "N texts ready -- ~$X on the table" (with "(estimated)" when applicable).
- "Send All" button -> POST /growth/tray/release.
- Card list (max 10 displayed; link to "show all" for 15+):
  - Name, kind label (Review / Win-back / Follow-up / Referral / Reactivation).
  - Draft body preview (truncated to 100 chars).
  - Money value ("~$X").
  - Tone-risk badge (yellow "Review thread first") when `tone_risk=True`.
  - Blocked-reason badge (gray "Add Google Review link") when `sendable=False`.
  - Skip button -> POST /growth/tray/skip/<id>.
- If zero held plays: "Nothing waiting -- plays appear here once you turn on Morning Tray mode."
- Link back to /command-center.

### GAMMA TESTS (test_growth_tray_ui.py)

1. `POST /settings/growth_mode` with `mode='tray'` -> redirects to `/settings?growth_saved=1`;
   `db.growth_mode(biz_id)` returns 'tray'.
2. `POST /settings/growth_mode` with `mode='auto'` (forged POST) -> server rejects, sets 'off'.
   **This is the auto-lock gate test -- must pass.**
3. `GET /growth/tray` with held plays -> 200, HTML contains "Send All" button and play count.
4. `POST /growth/tray/release` -> redirects, all held rows are now 'pending'.
5. `POST /growth/tray/skip/<id>` -> play is 'canceled'; others remain 'held'.
6. `_parse_tray_reply("GO")` -> `{"cmd": "go"}`.
7. `_parse_tray_reply("SKIP 2")` -> `{"cmd": "skip_n", "n": 2}`.
8. `_parse_tray_reply("hello")` -> None (falls through to normal handler).
9. SMS "GO" from owner cell -> `release_growth_batch` called, confirmation SMS sent to owner
   (not to any customer). Assert zero sends to customer numbers.
10. SMS "SKIP" from owner cell -> all held plays canceled.
11. SMS "GO" from a CUSTOMER number (not owner cell) -> tray branch NOT triggered; message
    routed to normal `handle_inbound`. **This is the cross-number guard test -- must pass.**
12. Growth mode card renders in settings.html: 'auto' option has `disabled` attr and opacity;
    current mode is pre-selected.
13. Tone-risk play in tray: "Review thread first" badge visible in HTML.
14. Blocked-reason play in tray: "Add Google Review link" badge visible; no skip button
    (no point canceling a play that's not queued anyway).

---

## 9. HELD-TO-RELEASE ATOMIC-COMMIT DESIGN (the critical correctness path)

The end-to-end flow for one play, fully specified:

```
~9:30pm tick_once()
  -> growth.scan(now)
     -> growth_mode(biz_id) == 'tray'
     -> plays(biz) surfaces a review_request for lead_id=42
     -> frequency cap checks pass
     -> add_scheduled_message(bid, 42, None, 'review_request', send_at, body, status='held')
        -> INSERT INTO scheduled_messages (..., status, ...) VALUES (..., 'held', ...)
        -> uniq_growth_touch_per_lead index: 'held' != 'canceled' -> blocks re-queue (correct)
  -> run_due_once(now)
     -> due_scheduled_messages(now): SELECT WHERE status='pending' -> 'held' rows NOT returned
     -> nothing fires (correct)

8:00am tick_once()
  -> scan_growth_tray(now)
     -> local hour == 8
     -> list_held_messages(bid): SELECT WHERE status='held' -> [row for lead 42]
     -> alerts.notify(biz, 'growth_tray', {...})
     -> SMS sent to biz["alert_sms"] (owner cell): "1 text ready: Maria (review). ~$2,500..."
  -> run_due_once(now): 'held' rows still excluded -> nothing fires (correct)

Dave replies "GO":
  -> twilio_sms_inbound() receives SMS from owner cell
  -> is_owner == True
  -> _parse_tray_reply("GO") == {"cmd": "go"}
  -> _handle_tray_reply(biz, {"cmd": "go"})
  -> db.release_growth_batch(bid, approved_via='sms_go')
     BEGIN TRANSACTION
       SELECT id, lead_id, kind FROM scheduled_messages WHERE business_id=? AND status='held'
       UPDATE scheduled_messages SET status='pending' WHERE business_id=? AND status='held'
       INSERT INTO growth_approvals (...) VALUES (bid, batch_id, 42, 'review_request', now, 'sms_go', 'EBR:last_job:2026-04-10')
     COMMIT
     returns {'released': 1, 'batch_id': 'abc12345'}
  -> messaging.send_sms(biz, owner_cell, "1 text queued...", gate=False) (confirmation to owner)

Next tick (within <=60s):
  -> run_due_once(now)
  -> due_scheduled_messages(now): row for lead 42 is now 'pending' -> returned
  -> claim_scheduled_message(row_id): UPDATE WHERE status='pending' -> atomically claimed
  -> messaging.send_sms(biz, lead_phone, body, transactional=False)
     -> quiet-hours check (transactional=False): 8am local -> allowed
     -> A2P gate
     -> sends (or simulates)
  -> status='sent' -> db.add_growth_touch_log(bid, 42, 'review_request', outcome='sent')
  -> frequency cap updated: `recent_growth_touch(bid, 42, 30)` will now return True for 30 days
```

The only race condition possible: two tick_once calls run simultaneously. `claim_scheduled_message`
(db.py:2462) uses `UPDATE WHERE status='pending'` atomically -> only one claimer wins. Safe.

---

## 10. WHAT IS CODE vs. OWNER-OPS vs. DEFERRED

### CODE (ships in 5d)
- All schema migrations (growth_mode, growth_touch_log, growth_approvals)
- `scan()` mode branching + 'held' insert
- `release_growth_batch` / `release_growth_play` / `cancel_growth_play`
- 8am tray digest SMS via `alerts.notify` (owner cell, A2P-exempt)
- Tray-reply parser (GO / SKIP / SKIP N)
- Settings UI: growth mode card (off/tray/auto-locked)
- Growth tray page (/growth/tray)
- Frequency cap (30-day + 12-month)
- `growth_approvals` audit log
- `growth_touch_log` + delivery hook in `run_due_once`
- Trade-keyword job value defaults
- Blocked-play visibility (missing review link)
- Win-back TCPA narrowing (inbound-only)
- Tone-risk flag + tray badge
- Batch-size cap (top 10 of 15+ held plays)

### OWNER-OPS (nothing new for 5d)
- Dave sets `alert_sms` in Settings (already required for 5b morning digest) -- needed for tray SMS.
- Dave sets `avg_job_value` in Settings for accurate money framing (trade defaults ship as fallback).
- Dave sets Google Review link in Settings (needed for review_request copy; missing link shows
  blocked-play card nudging him to add it).
- Twilio + A2P: already required for live sends; simulated path still works without them.

### DEFERRED (not in 5d, noted for future phases)
- **L1 (growth analytics surface)**: reply tracking via `growth_touch_log.outcome` is shipped in
  5d as the foundation; the analytics dashboard card is deferred.
- **L2 (7-day streak -> unlock auto mode)**: streak detection and auto-unlock logic are deferred.
  `'auto'` mode column + server-side rejection of forged POSTs are shipped in 5d to keep the
  path clean for L2 later.
- **L3 (weekly digest SMS)**: deferred.
- **L4 (dedicated /growth dashboard page)**: 5d ships /growth/tray; the full /growth analytics
  page is deferred.
- **L5 (GBP connector for review detection)**: deferred.
- **L6 (campaign composer for non-sendable plays)**: deferred.
- **G8 (skip plays for active prospects)**: deferred; mentioned in F13-FINAL but not blocking.
  `cancel_lead_growth_touches` at app.py:1705 already handles quote_followup/reactivation when
  a lead books; growth plays for active leads can be added to this call later.
- **M4 (reply-outcome tracking beyond 'positive' detection)**: 5d wires the `growth_touch_log`
  table and the `add_growth_touch_log` call on delivery. Outcome updates from customer replies
  (positive NLU -> `replied_positive`) are deferred to a later sub-phase (or included if GAMMA
  has capacity -- the inbound handler at app.py:2526 is already in GAMMA's slice).

---

## 11. INTEGRATION RISKS

**R1 (CRITICAL): Scan-before-release ordering.**
`tick_once` calls `growth.scan()` then `run_due_once()`. If scan() inserts 'held' rows and
run_due_once() is changed to pick up 'held' rows accidentally, plays fire without approval.
GUARD: `due_scheduled_messages` must be verified to query `status='pending'` only BEFORE
any test runs. The spec's explicit test (ALPHA test #6) locks this. Do not merge ALPHA until
test #6 passes.

**R2 (HIGH): Tray-reply owner-phone matching.**
`_handle_tray_reply` fires only when `is_owner == True`. The owner-phone comparison uses
`messaging.to_e164()` for normalization. If `biz["alert_sms"]` is empty or unnormalized and
`to_e164()` returns `None` for both sides, the `is_owner` check could evaluate True (None == None).
GUARD: add explicit `if not owner_cell: is_owner = False` before the comparison.

**R3 (HIGH): `growth_touch_index` includes 'held' rows.**
This is CORRECT behavior (prevents re-queuing a held play), but if Dave cancels a play via
`cancel_growth_play` (sets 'canceled'), the dedupe index partial condition (`status!='canceled'`)
removes the cancled row, allowing the play to re-surface in the next scan. This is intentional
(a canceled play should re-evaluate next cycle) but must be communicated in comments.

**R4 (MEDIUM): `add_scheduled_message` signature change.**
Adding `status='pending'` as an optional parameter to `add_scheduled_message` (db.py:2393) must
not break existing callers (reminders.py, growth.py). Default must stay 'pending'. Verify all
existing call sites in test_reminders.py and test_growth.py still pass.

**R5 (MEDIUM): `growth_mode` column backfill race.**
If the migration runs while `growth_on=1` but after `growth_mode` column exists (e.g. in a
multi-process deploy), the backfill UPDATE must be idempotent: `WHERE growth_on=1 AND growth_mode='off'`
prevents re-backfilling rows already set to 'tray'. Safe.

**R6 (LOW): Morning tray vs. Vic morning digest (5b) firing the same morning.**
Both run in `tick_once` at 8am. `scan_morning_briefing` fires at 7-9am for briefing items;
`scan_growth_tray` fires at 8-8:59am for held plays. They use different alert kinds and different
dedupe keys -- no collision. Owner receives two separate SMS: one briefing (Vic), one growth tray.
This may feel like two texts in the same minute. Acceptable in the short term; combine in L4 if
Dave reports it as noisy.

**R7 (LOW): `release_growth_batch` in a concurrent tick.**
If `tick_once` calls `growth.scan()` while a batch release is mid-commit, the scan may see zero
held rows (they just flipped to 'pending') or all held rows (before flip). Both are safe: zero
held rows means no new 'held' inserts (dedupe blocks them); all held rows means the scan attempts
inserts which are blocked by the dedupe index (`uniq_growth_touch_per_lead`, status!='canceled').

---

## 12. MERGE ORDER

**ALPHA -> BETA -> GAMMA** (same pattern as 5b).

- ALPHA commits first: schema + db functions + growth.py changes. Run test_growth.py (must stay
  green) + test_growth_tray.py (all 19 new tests green).
- BETA commits second: alerts.py kind registration + reminders.py digest + run_due_once hook.
  Run FULL suite + test_growth_tray_sms.py.
- GAMMA commits last: app.py routes + templates. Run FULL suite + test_growth_tray_ui.py.

Final pre-commit verification (Opus orchestrator):
- [ ] `due_scheduled_messages` confirmed to exclude 'held' rows (ALPHA test #6).
- [ ] Auto mode rejected at server (GAMMA test #2).
- [ ] Tray reply from customer number falls through to normal handler (GAMMA test #11).
- [ ] All proactive sends go to owner cell, never to customer (BETA test #4, GAMMA test #9).
- [ ] `growth_approvals` has a row for every released play (ALPHA test #8).
- [ ] Full suite green (no regressions in any existing test file).
