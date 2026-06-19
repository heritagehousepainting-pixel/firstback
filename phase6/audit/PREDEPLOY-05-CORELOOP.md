# PREDEPLOY-05-CORELOOP — Core Product Loop Audit

Auditor lane: missed call → triage → text-back → AI conversation → booking → reminder.
Branch: staging @ 55d2601. READ-ONLY.

---

## Summary

**P0: 0 | P1: 2 | P2: 2 | DEPLOY VERDICT: PROCEED WITH MITIGATIONS**

The core product loop is correct end-to-end. The missed-call guard, triage/screening
graduation, booking guards, double-booking recovery, and reminder atomicity all work as
designed. Two P1s involve the `morning_reminder` lifecycle on rebook/cancel paths; neither
causes a customer to get a wrong text, but one causes a silent miss (no 8am reminder after
a rebook). Two P2s are cosmetic or low-probability.

---

## Traced Happy Path

**Missed call (catcher mode):**
`twilio_voice_inbound` (app.py:2619) → no `forward_to` → `_missed_call_textback` (app.py:2576) →
`_screen_missed_caller` → `triage.screen_caller` (triage.py:146) → verdict + `db.log_call`
(uses `ON CONFLICT(call_sid) DO UPDATE` for idempotency) → `if not db.get_messages(lead.id)`
→ `open_conversation` → `ai.generate_reply(biz, [])` → `db.add_message(out)` →
`messaging.send_sms`. Exactly one text sent per new-lead missed call. Repeat missed calls
mid-conversation: messages are present → skip open_conversation → no double-text. ✓

**Missed call (dial-through mode):**
`twilio_voice_inbound` → `<Dial action=dial-status>` → contractor answers → no textback. ✓
`twilio_voice_dial_status` (app.py:2653) → `status in _MISSED_DIAL` → `_missed_call_textback`.
These two paths are mutually exclusive (catcher sets no `forward_to`, dial sets it).
No double-text possible between `inbound` and `dial-status`. ✓

**Triage verdict in monitor mode:**
`_effective_screen_mode` reads `biz.screen_mode` else `SCREEN_MODE` (app.py:2569-2573). ✓
Monitor: verdict computed + logged, engage always True (app.py:2604). ✓
Enforce: `not verdict["engage"]` → `log_call(engaged=0)` + `return False` → no text. ✓
Off: hardcoded `engage=True` (app.py:2595). ✓

**5c graduation (monitor → enforce):**
`scan_screening_graduation` (reminders.py:750): 7-day window + ≥10 `screened_spam` verdicts
(NOT `screened_contact`) + no `screening_hold` → `db.promote_screening` (atomic).
Rescue resets `screening_window_start` to now → subsequent pass skips (<7d). ✓
Lazy-init NULL window: sets to now + skips → 7-day clock starts fresh. ✓
Tests in `test_screening_graduation.py` cover all branches (15 cases). ✓

**"This was real" rescue (api_rescue_screened_call, app.py:2171):**
`db.record_screening_rescue`: upserts as customer + increments `false_positives` + resets
window (atomic, db.py:2096). Re-engages only on empty thread (`if not db.get_messages`).
Refuses opted-out number. Does NOT re-text if thread already open. ✓
`api_engage_screened_call` (app.py:2143): same empty-thread guard. ✓

**AI conversation → booking:**
Turn cap at 12 inbound turns → phone handoff (ai.py:475-483). ✓
Price guard: regex `_PRICE_RE` scrubs `$NNN` / `NNN dollars` (ai.py:430-448). ✓
Length cap: 480 chars at sentence boundary (ai.py:451-461). ✓
`[[BOOK: slot_id]]` parse: `BOOK_MARKER.search(raw)` (ai.py:503) → `_resolve_booking`
reconciles against what was actually offered + caller's explicit choice (ai.py:377-407).
Conflict: caller's explicit slot wins over model marker (ai.py:510-514, logged). ✓

**Double-booking recovery:**
`db.book_appointment` returns `False` on `IntegrityError` (SQLite UNIQUE partial index on
`(business_id, day, slot_time) WHERE status='booked'`, db.py:492-494). ✓
On False: `recovery_history_ext` injected, new `generate_reply` called, recovery reply
replaces the already-recorded out message (app.py:1922-1940). ✓
Two callers racing one slot: second gets recovery reply, slot not double-booked. ✓

**Booking side-effects:**
`google_cal.create_event_async` (app.py:1915) — off-hot-path. ✓
`reminders.enqueue_reminder` (app.py:1920) — queues pre-estimate reminder. ✓
`reminders.enqueue_morning_reminder` (app.py:1921) — queues 8am reminder (SEE P1 BELOW). 
`alerts.notify_async(biz, "booking", ...)` (app.py:1887). ✓
`db.learn_customer(...)` (app.py:1862) — marks number as `customer` so next call is `trusted`. ✓

**Reminder tick (run_due_once):**
`claim_scheduled_message` is atomic: `UPDATE ... WHERE id=? AND status='pending'` →
rowcount==1 only for the claimer (db.py:2559-2568). No double-send. ✓
`reminder` + `morning_reminder`: skipped if `appt_status != 'booked'` OR appointment passed. ✓
`followup`/`followup_2`: re-checks lead status before send (S3 guard). ✓
Business-local timezone (SF-5 `_biz_tz`). ✓

**RSVP cancel (SMS "cancel" keyword, app.py:2680):**
`_cancel_estimate_for` → `db.cancel_appointment` (atomic: cancels appointment + `kind='reminder'`
rows + resets lead status in one commit, db.py:2902-2931) → alerts owner. ✓
Cancels all booked appointments for that phone. Falls back to opt-out only when nothing
to cancel. ✓

**Owner cancel (api_cancel_appointment, app.py:2079):**
`db.cancel_appointment` → free slot + cancel reminders → Google Calendar async cancel →
SMS customer heads-up. Scoped to owner's business. ✓

---

## P0 Findings (blocks deploy)

None.

---

## P1 Findings (must fix before or immediately after deploy)

### P1-A · morning_reminder orphaned on rebook; dedupe skips new one
**File/lines:** `db.py:2510-2516` (`cancel_lead_pending_reminders`), `reminders.py:214-218`
(`enqueue_morning_reminder` dedupe), `db.py:2919` (`cancel_appointment` SQL)

**Root cause:** When a lead rebooks (new slot replaces old), `handle_inbound` calls
`db.cancel_appointment(old_appt)` (app.py:1877), then `enqueue_morning_reminder` (app.py:1921).
`cancel_appointment` only cancels `kind='reminder'` rows (db.py:2919):

```python
conn.execute("UPDATE scheduled_messages SET status='canceled' "
             "WHERE appointment_id=? AND kind='reminder' AND status='pending'",
             (appointment_id,))
```

It does NOT cancel `kind='morning_reminder'` rows. The old `morning_reminder` stays `pending`.

`enqueue_morning_reminder` dedupes via `db.find_scheduled_message(business_id, lead_id,
"morning_reminder")` (reminders.py:215) which finds the stale `pending` old morning_reminder
and returns early (`"already queued"`). The new appointment gets no morning_reminder.

At fire time, `run_due_once` catches the old morning_reminder (appt_status='canceled') and
marks it 'skipped' (reminders.py:317), so no wrong text fires. But no 8am reminder is ever
sent for the rebooked appointment.

**Impact:** Customer who reschedules gets no morning-of reminder for their new date. Missed
revenue opportunity + no-show risk. NOT a double-text / wrong-text issue.

**Fix:** In `cancel_appointment` (db.py:2919), change the WHERE clause to cancel both kinds:
```sql
WHERE appointment_id=? AND kind IN ('reminder','morning_reminder') AND status='pending'
```
And update `cancel_lead_pending_reminders` (db.py:2513) similarly.

---

### P1-B · cancel_appointment cancels `kind='reminder'` only, leaving morning_reminder pending
**File/lines:** `db.py:2919`, `db.py:2533-2540` (`cancel_appointment_reminders`)

This is the same root cause as P1-A applied to the owner-initiated cancel path
(`api_cancel_appointment`, app.py:2087). After an owner cancels an estimate, the
`morning_reminder` row stays `pending` until the next tick. `run_due_once` guards it
(appt_status='canceled' → mark 'skipped'), so no spurious text fires. But the DB shows
an orphaned-pending row, and `cancel_appointment_reminders` (db.py:2533) has the same bug.

**Impact:** Cosmetically visible orphaned `morning_reminder` rows (status='pending') after any
cancel, until the next tick fires and skips them. No customer-visible mis-send.

**Severity:** P1 because it shares the same code path as P1-A and the same single-line fix
resolves both.

---

## P2 Findings (notable, non-blocking)

### P2-A · First-message [[BOOK]] marker can book without caller consent
**File/line:** `ai.py:398-400` (`_resolve_booking`)

If the LLM emits `[[BOOK: slot_id]]` in the opening message (before any caller agreement),
`_resolve_booking` honors it: `offered = []` → `not offered` is True → `chosen = marker_slot`.
The demo brain never does this and Claude is instructed not to, but there is no explicit guard
preventing it. Very low probability (LLM hallucination), but if triggered would book the
customer silently on first text.

**Fix:** Add `if not history or not any(m["direction"] == "in" for m in history): marker_slot = None`
before the resolve logic, so the opening message can never produce a booking.

---

### P2-B · Dispatcher rate-limit is per-lead-lifetime, not per-window
**File/line:** `app.py:1815-1816`

```python
_already_called = lead.get("dispatcher_call_last_at")
if _owner_cell and not _already_called:
```

The guard fires exactly once per lead, ever (not per 5h or any time window). A lead whose
urgency re-triggers (e.g. sends a second urgent message much later) will never get a second
dispatcher call. This is conservative (avoids spam) but may miss a genuine re-escalation.
Not a safety or correctness issue — the SMS alert path still fires on every urgency event.

---

## Clean Items (confirmed correct)

- Missed-call idempotency via `db.get_messages` check: no double-text on Twilio retry or
  repeat missed calls. ✓
- `call_sid` UNIQUE constraint + `ON CONFLICT DO UPDATE` on `db.log_call`: idempotent across
  Twilio retries. ✓
- Triage monitor/enforce mode split: monitor observes, enforce acts. ✓
- 5c graduation only counts `screened_spam` (not `screened_contact`) toward threshold. ✓
- Rescue resets clock atomically → graduation blocked. ✓
- Double-booking protected by SQLite partial UNIQUE index (business, day, slot_time / booked). ✓
- Re-confirm same slot: recognized, `booked = booking` without calling `book_appointment` again. ✓
- Rebook: old appointments canceled before new one inserted → slot freed. ✓
- `claim_scheduled_message` atomic (rowcount guard): no double-send on concurrent ticks. ✓
- S3 guard: `followup`/`followup_2` re-checks lead status at fire-time. ✓
- `cancel` SMS → cancel estimate first, opt-out only on empty. ✓
- Owner-cancel: Google Calendar async cancel + customer SMS + reminder cancel in same commit. ✓
- Booking side-effects (Google event, reminder, owner alert, ROI milestone) in both
  `open_conversation` (first-turn) and `handle_inbound` (subsequent turns). ✓
- `start_ticker` sleeps before first tick to avoid blocking deploy health check. ✓
- Business-local timezone for all reminder scheduling (SF-5). ✓
- AI turn cap (12 inbound) → phone handoff. ✓
- Price scrub + length cap on every AI reply. ✓
