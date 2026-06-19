# Phase 5e — F06 Cold Follow-Up Hardening
**Status:** LOCKED build-ready spec  
**Date:** 2026-06-18  
**Source branch:** staging ~bfd6ceb  
**Scope:** Contextual Sonnet Touch-1 copy + Touch-2 (followup_2 kind) + robocaller exclusion + race-condition closes + transactional flag fix. NO edit to trades_core originals; edit firstback's vendored copies only.

---

## 1. Current-State Ground Truth (file:line)

### What exists and is correct
| Item | Location | Status |
|---|---|---|
| `scan_followups()` detection loop | `reminders.py:355-380` | Working — queues Touch 1 per cold lead |
| `due_followup_leads()` pure filter | `reminders.py:111-127` | Working — idle/phone/has_followup gates |
| `followup_candidate_rows()` SQL | `db.py:2532-2546` | Working — inbound-only gate at line 2543 |
| `followups_on()` per-biz toggle | `reminders.py:136-138` | Working — defaults ON (None->True) |
| `run_due_once()` idempotent claim | `reminders.py:296-352` | Working — no followup-specific status check |
| `next_send_time()` quiet-hours deferral | `reminders.py:91-98` | Working |
| `followup_body()` generic template | `reminders.py:78-81` | Working but context-free |
| `messaging.send_sms()` opt-out gate | `messaging.py:110` | Working (at send time) |
| `messaging.send_sms()` quiet-hours backstop | `messaging.py:120-134` | Working when `transactional=False` |
| `cancel_lead_pending_reminders()` | `db.py:2424-2430` | Reminder-kind only; no followup analog |
| `cancel_lead_growth_touches()` | `db.py:2517-2529` | Growth-kind only; no followup analog |
| `_copy_reactivation()` in growth.py | `growth.py:135-137` | Exists, ready to reuse for Touch 2 |

### What is MISSING (gaps to close)
1. **Robocaller/spam exclusion in `followup_candidate_rows`** — no WHERE clause on contacts.category or calls.screen_status. A spam number that texted in ("replied" = IN direction exists) qualifies as a candidate today. `leads.triage_flag` column does NOT exist; spam signal lives in the `contacts` table (contacts.category = 'blocked') and `calls` table (calls.screen_status = 'screened_spam'), both keyed by phone digits. The exclusion must JOIN or subquery against contacts/calls by lead phone.

2. **Transactional flag missing** — `run_due_once` calls `messaging.send_sms(biz, phone, row["body"], ...)` at `reminders.py:326` without `transactional=False`. The docstring in `messaging.py:114-119` explicitly states growth/follow-up sends MUST pass `transactional=False` to opt into the quiet-hours backstop. Follow-ups are marketing sends, not solicited responses. Currently the quiet-hours gate (`messaging.py:120`) is never invoked for followup/followup_2 rows because `transactional` defaults to `True`.

3. **`followup_2` kind does not exist** — `scan_followups` only queues kind='followup'. No Touch-2 logic, no 5-day scheduling, no `has_followup_2` dedup check, no `_copy_reactivation` reuse.

4. **`cancel_pending_followup_touches(lead_id)` does not exist** — `db.py` has `cancel_lead_pending_reminders` and `cancel_lead_growth_touches` but nothing for followup/followup_2 kinds. Not called from `handle_inbound` or booking path.

5. **Live status re-check in `run_due_once` for followup kinds** — `reminders.py:309-318` only applies the `appt_status != 'booked'` skip to kinds `reminder` and `morning_reminder`. Followup/followup_2 kinds have no equivalent check. A lead that books AFTER Touch 2 is queued but BEFORE it fires will still receive the text.

6. **Contextual Touch-1 copy via Sonnet** — `scan_followups:373` calls `followup_body()` (generic template, no personalization). No `followup_body_contextual()` function exists. `followup_candidate_rows` does not return `last_in_text` (the newest direction='in' message body).

7. **Opt-out check at enqueue time** — `scan_followups:373-377` calls `db.add_scheduled_message` without first checking `messaging.outbound_mode(biz, phone) != 'suppressed'`. Suppressed leads get queued and then silently blocked at send time. The analogous growth.py check is at `growth.py:192`.

8. **No test file covers `reminders.py` followup paths** — the docstring at `reminders.py:16` says "The scheduling math ... is PURE and unit-tested." Zero test files import `reminders`. `test_reminders.py` exists but tests only `compute_send_at`, `next_send_time`, and `when_phrase` — not `scan_followups`, `due_followup_leads`, or `run_due_once` followup behavior.

---

## 2. Schema Changes

### No new tables required.

### `followup_candidate_rows` query change (db.py:2537-2544)
Add `last_in_text` to the SELECT and a spam exclusion to the WHERE:
```sql
SELECT l.id, l.name, l.phone,
       MAX(m.created_at) AS last_msg_at,
       EXISTS(SELECT 1 FROM scheduled_messages s
              WHERE s.lead_id=l.id AND s.kind='followup') AS has_followup,
       EXISTS(SELECT 1 FROM scheduled_messages s2
              WHERE s2.lead_id=l.id AND s2.kind='followup_2') AS has_followup_2,
       (SELECT mi.body FROM messages mi
        WHERE mi.lead_id=l.id AND mi.direction='in'
        ORDER BY mi.created_at DESC LIMIT 1) AS last_in_text
FROM leads l JOIN messages m ON m.lead_id = l.id
WHERE l.business_id=?
  AND l.status != 'booked'
  AND EXISTS(SELECT 1 FROM messages mi
             WHERE mi.lead_id=l.id AND mi.direction='in')
  -- Robocaller/spam exclusion: skip leads whose phone is flagged in contacts or calls
  AND NOT EXISTS(
    SELECT 1 FROM contacts c
    WHERE c.business_id=l.business_id
      AND c.number = SUBSTR(REPLACE(REPLACE(REPLACE(REPLACE(
            REPLACE(l.phone,'+',''),'-',''),'(',''),')',''),' ',''), -10)
      AND c.category IN ('blocked','personal','vendor'))
  AND NOT EXISTS(
    SELECT 1 FROM calls ca
    WHERE ca.business_id=l.business_id AND ca.lead_id=l.id
      AND ca.screen_status='screened_spam')
GROUP BY l.id
```
Note: `personal` and `vendor` are also excluded because they are never prospects; `blocked` is the explicit spam/robocaller category. The `_digits10` helper pattern is inlined in SQL to match existing db.py contact-keying logic.

### `due_followup_leads()` signature change (reminders.py:111)
Add `has_followup_2` awareness: also filter out rows where `has_followup_2` is truthy (Touch 2 already queued). Update the function signature to pass this through, or handle inside via the row dict.

---

## 3. File-Disjoint Slice Plan

### FILES TOUCHED (full list for collision planning)
- `db.py` — `followup_candidate_rows` query + `cancel_pending_followup_touches` new function
- `reminders.py` — `followup_body_contextual` new function + `scan_followups` Touch-2 logic + opt-out check at enqueue + `due_followup_leads` has_followup_2 filter + `run_due_once` followup live-status check + `transactional=False` on send_sms call
- `app.py` — `handle_inbound` wired to call `cancel_pending_followup_touches` + booking path wired same
- `test_reminders.py` — new/extended tests (see Section 5)
- `llm.py` — new `followup_body_contextual` helper (or place in `reminders.py` with llm import; preference: keep in reminders.py with `import llm` at call site to avoid circular)

No changes to: `triage.py`, `growth.py`, `messaging.py`, `auth.py`, `billing.py`, `convos.py`, `config.py`, `alerts.py`.

---

## 4. Ordered Slices

### S1 — Robocaller exclusion in `followup_candidate_rows` (db.py:2532-2546)
- Rewrite the SQL to add `last_in_text`, `has_followup_2`, and the NOT EXISTS spam exclusion clauses.
- No new table, no migration needed (contacts and calls tables already exist with the relevant columns).
- `due_followup_leads` in `reminders.py:111-127`: add `has_followup_2` awareness — if `r.get("has_followup_2")` is truthy, skip.
- **Gate:** must be the first slice. Blocks spam-number follow-ups before ANY other work goes live.
- Estimate: 1-2h

### S2 — `transactional=False` on followup sends + opt-out check at enqueue (reminders.py)
- `run_due_once:326` — add `transactional=False` to `messaging.send_sms()` call. Because `run_due_once` handles all kinds (reminder, morning_reminder, followup, followup_2, sms_retry), scope the flag to kind-based branching: only set `transactional=False` for `kind in ('followup', 'followup_2')` to avoid changing reminder behavior.
- `scan_followups:373-377` — before `db.add_scheduled_message`, check `messaging.outbound_mode(biz, phone) != 'suppressed'`; skip enqueue if suppressed.
- Estimate: 1h

### S3 — Live lead-status check in `run_due_once` for followup kinds (reminders.py:296-352)
- Mirror the `appt_status` booked-cancel pattern at `reminders.py:309-318`.
- For `kind in ('followup', 'followup_2')`: fetch lead status directly — `db.get_lead(row['lead_id'])` — and if `status == 'booked'`, call `db.mark_scheduled(row['id'], 'canceled')` and `continue`.
- Note: `db.get_lead` already accepts optional `business_id` for ownership scoping.
- Estimate: 1h

### S4 — `cancel_pending_followup_touches(lead_id)` in db.py + wire to `handle_inbound` and booking path (db.py, app.py)
- `db.py` — new function: `def cancel_pending_followup_touches(lead_id):` — UPDATE scheduled_messages SET status='canceled' WHERE lead_id=? AND kind IN ('followup','followup_2') AND status='pending'.
- `app.py:handle_inbound:1637` — add call at top of function body (after `db.add_message`) to cancel any pending followup touches for this lead (lead just re-engaged).
- `app.py` booking path — at the point where `db.book_appointment` is called (line ~1696), also call `db.cancel_pending_followup_touches(lead["id"])`.
- Depends on S3 (belt-and-suspenders with the live-status check).
- Estimate: 2h

### S5 — Touch-2 (`followup_2`) kind in `scan_followups` (reminders.py:355-380)
- After queuing Touch 1, also check `lead.get("has_followup_2")` (from the updated query in S1). If False and Touch 1 was JUST queued (or already was queued), enqueue Touch 2: `kind='followup_2'`, `body=growth._copy_reactivation(first, biz)`, `send_at = touch1_send_at + 5 days`, quiet-hours deferred via `next_send_time`.
- Touch 2 logic: only queue Touch 2 when a Touch 1 row exists (or was just created in this pass). The simplest approach: after successfully inserting Touch 1 (non-None return from `add_scheduled_message`), immediately enqueue Touch 2 in the same loop body.
- `due_followup_leads` already gates on `has_followup` — leads with Touch 1 are not returned again, so Touch 2 is enqueued ONCE at Touch-1 creation time (not on a subsequent scan).
- Import `growth._copy_reactivation` at call site: `from growth import _copy_reactivation` (or call via module to avoid touching growth.py imports).
- Depends on S1, S3, S4.
- Estimate: 2-3h

### M1 — `followup_body_contextual(name, biz_name, last_in_text)` via Sonnet (reminders.py, llm.py)
- New function in `reminders.py`: call `llm.complete()` with a ~100-token system prompt + the lead's `last_in_text`. Return a 130-char SMS. Wrap entirely in try/except with fallback to `followup_body()`.
- Prompt discipline: plain trades voice, one offer, one ask, no urgency language, no incentive. Model: `CLAUDE_MODEL` (already Sonnet at `config.py:48`).
- `scan_followups` passes `lead.get('last_in_text')` to `followup_body_contextual` instead of calling `followup_body()` directly.
- Depends on S1 (last_in_text column available in query result).
- Estimate: 4-6h

### M2 — Morning digest "handled while you worked" (alerts.py or reminders.py)
- Daily scan: aggregate `scheduled_messages WHERE kind IN ('followup','followup_2') AND status IN ('sent','simulated') AND sent_at > [24h ago]`, join to leads for current status.
- Send to `biz.alert_sms` via `alerts.notify(biz, 'followup_digest', ctx)`. Works in simulated mode ("3 follow-ups in test mode").
- Deduped via alerts.notify dedupe key (`followup_digest + local YYYY-MM-DD`).
- Estimate: 4-6h

---

## 5. Tests Required (test_reminders.py)

All tests are PURE (no I/O): monkeypatch `db`, `messaging`, `llm`. The scheduler tests exercise the state machine logic without real Twilio or DB.

| Test | What it covers |
|---|---|
| `test_due_followup_leads_skips_has_followup` | Row with `has_followup=1` is excluded |
| `test_due_followup_leads_skips_has_followup_2` | Row with `has_followup_2=1` is excluded |
| `test_due_followup_leads_skips_no_phone` | Row with empty phone is excluded |
| `test_due_followup_leads_skips_not_cold` | Row with recent last_msg_at is excluded |
| `test_scan_followups_queues_touch1` | Cold lead with no has_followup gets Touch 1 queued |
| `test_scan_followups_queues_touch2_at_t1_creation` | Touch 2 queued immediately after Touch 1 |
| `test_scan_followups_no_double_touch2` | has_followup_2=True skips Touch-2 enqueue |
| `test_scan_followups_respects_followups_off` | `followups_enabled=False` skips biz entirely |
| `test_scan_followups_suppressed_skips_enqueue` | Suppressed lead not queued (opt-out at enqueue) |
| `test_run_due_once_cancels_followup_if_booked` | Followup kind for booked lead gets canceled, not sent |
| `test_run_due_once_double_claim_idempotent` | Second claim attempt returns False, no double-send |
| `test_run_due_once_followup_transactional_false` | `send_sms` called with `transactional=False` for followup kinds |
| `test_run_due_once_reminder_transactional_true` | `send_sms` called with `transactional=True` (default) for reminder kinds |
| `test_followup_body_contextual_fallback` | LLM failure falls back to `followup_body()` |
| `test_cancel_pending_followup_touches` | Both followup and followup_2 rows set to canceled |

---

## 6. Owner-Ops Gates

- **S6 (from F06-FINAL):** Confirm `FIRSTBACK_RUN_TICKER=1` on Render AND/OR `/tasks/run-due` wired from external cron. Nothing ships without this; follow-ups never fire in prod otherwise. Check `app.py:~1873`.
- No new owner-ops required for 5e otherwise. The UI toggle (`followups_enabled`) already exists in the Settings page per the current code; no new setting screen needed.

---

## 7. Non-Negotiable Gates

- `transactional=False` MUST be set for followup/followup_2 send_sms calls. Failing this means 2am marketing texts can go out. Gate before merge.
- Robocaller exclusion (S1) MUST ship before Touch-2 (S5). A spammer that texted in must never receive two follow-ups.
- `cancel_pending_followup_touches` (S4) MUST ship before Touch-2 (S5). Without it, a lead that books after Touch 1 but before Touch 2 fires still receives Touch 2.
- The unique index at `db.py:684-688` only covers kind='followup' (Touch 1). Touch 2 (kind='followup_2') needs its own unique index: `CREATE UNIQUE INDEX IF NOT EXISTS uniq_followup_2 ON scheduled_messages(lead_id) WHERE kind='followup_2'`. Add alongside S5.

---

## 8. Deferred (not this phase)

- L2 Per-business timezone threading for followup send_at (currently uses app-global tz; correct for single-region)
- L3 Configurable cold threshold per business
- L4 Re-engagement cycle reset (3-cycle cap)
- M5 "Already hired" reply detection
- M6 Dashboard follow-up queue panel

---

## 9. Biggest Risk

**The `transactional=False` omission** is the highest-risk gap: `run_due_once` currently bypasses the quiet-hours backstop for followup kinds, meaning a follow-up queued for "next morning" could fire immediately at 2am if a send is triggered outside quiet hours by another mechanism. This is a compliance exposure (CTIA quiet-hours requirement) and the fix is one keyword argument, but it must be gated on correct `kind` branching to avoid breaking reminder behavior. Get this right in S2 before writing any other followup code.

Second risk: the spam exclusion JOIN in S1 uses an inline phone-digits-normalize expression in SQL (replicating `db._digits10`). If leads.phone is stored with inconsistent formatting, the JOIN may miss some spam contacts. Verify against live data in `firstback.db` before deploying.
