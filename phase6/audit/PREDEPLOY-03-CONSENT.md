# PREDEPLOY-03-CONSENT — Consent / TCPA / A2P / Quiet-Hours Audit

**Auditor:** Lane 03 (CONSENT/TCPA/A2P/QUIET-HOURS)
**Branch:** staging @ 55d2601
**Date:** 2026-06-19
**Scope:** compliance.py, messaging.py, growth.py, alerts.py, reminders.py, app.py (consent/SMS handlers)

---

## FINDINGS

### P0 — BLOCKS DEPLOY

#### P0-1: Growth-kind marketing sends fire `transactional=True` — quiet-hours EXEMPT

**File:** `reminders.py:342`

```python
_transactional = kind not in ("followup", "followup_2")
```

**Impact:** `review_request`, `quote_followup`, `reactivation`, `winback`, `referral`, `membership` all resolve to `_transactional=True`, bypassing the quiet-hours backstop in `messaging.py:120`. These are marketing sends that must pass `transactional=False`. Only `followup` and `followup_2` correctly pass `transactional=False`.

**Scenario:** A business uses growth tray. Owner replies GO at 6pm. One play is a win-back with a 0-minute delay → `add_scheduled_message` sets `send_at` to now. The next tick fires `run_due_once`. It reads the pending row, resolves `_transactional = kind not in ("followup", "followup_2")` → True for "winback". `messaging.send_sms(..., transactional=True)` bypasses the `if gate and not transactional` quiet-hours block entirely. The backstop at `messaging.py:120` never fires. A consumer receives a marketing SMS at 2am if the tick fires after 21:00.

**Required fix:**
```python
# reminders.py:342
MARKETING_KINDS = {"review_request", "quote_followup", "reactivation",
                   "winback", "referral", "membership"}
_transactional = kind not in ("followup", "followup_2") and kind not in MARKETING_KINDS
```

Or equivalently:
```python
_transactional = kind in ("reminder", "morning_reminder")
```

Since all non-reminder, non-followup scheduled sends are marketing by definition, and should be gated.

**Test gap:** `test_compliance_backstop.py` and `test_growth_tray_sms.py` do not exercise growth kinds through `run_due_once` with quiet-hours mocking. The gap means the P0 is untested.

---

### P1 — FIX BEFORE CHARGING

#### P1-1: `flush_blocked_sends` uses `transactional=True` — correct but warrants explicit justification

**File:** `connections.py:467-468`

```python
result = messaging.send_sms(biz, to, body, lead_id=lead_id,
                            gate=True, transactional=True)
```

**Assessment:** The blocked_sends queue only contains initial missed-call text-backs (consumer-initiated — the caller just called). `transactional=True` is defensible for these as consumer-initiated contact. However, if any marketing body ever ends up in the blocked_sends queue (e.g., if blocking logic is widened to other send types), it would bypass quiet-hours. Currently safe because `queue_blocked_send` is only called from `messaging.send_sms` when the A2P gate fires — i.e., on initial customer text-backs, not scheduled growth plays. Confirmed: `growth.scan()` skips queueing when `messaging.configured() and not compliance.a2p_ready(biz)` (growth.py:406) so growth plays never enter blocked_sends.

**Verdict:** P1 informational — currently safe, but should be documented explicitly in code comment.

#### P1-2: `cancel_appointment` sends `transactional=True` (default) — customer reply

**File:** `app.py:2097-2100`

```python
messaging.send_sms(
    biz, lead["phone"],
    f"Your free estimate {when} has been canceled. Reply here any time to rebook.",
    lead_id=lead["id"])
```

Uses default `transactional=True`. This is an owner-initiated action canceling a customer appointment. The customer didn't initiate this specific text. Under strict TCPA, a cancellation notice at 11pm is legally murky. However, it is a service notification (not marketing), and legal consensus generally allows service-critical notifications outside quiet hours. **Risk is low** but the behavior should be documented.

#### P1-3: Voice-recovery SMS at `app.py:3115-3119` uses default `transactional=True`

```python
messaging.send_sms(
    _biz_vs, _lead_vs["phone"],
    "We tried to reach you by phone -- happy to keep chatting "
    "here. What are you looking to get painted?"
)
```

Voice is gated (`VOICE_PUBLIC_URL` required), so this is low-risk, but the SMS fires post-voicemail which is AI-initiated (outbound call the customer consented to via "call me"). `transactional=True` is correct here — the consumer requested the call.

#### P1-4: `__RECOVERY_SMS__` relay at `app.py:2989` uses default `transactional=True`

The relay fires from voice_service through the internal endpoint. Consumer explicitly requested the AI call, so transactional exemption is correct. No issue.

---

### VERIFIED CLEAN — NO FINDINGS

#### ✓ STOP / opt-out suppresses ALL future sends

- `db.is_suppressed()` called before every outbound in `messaging.send_sms:110`
- `set_opt_out` wired on: `norm in _STOP_WORDS` (app.py:2691), `detect_revocation(body)` NLU (app.py:2698), `norm == "cancel"` fallback (app.py:2686)
- All three paths also call `db.set_voice_consent(biz["id"], caller, False)` (R1: voice consent revoked on STOP)
- `growth.plays()` gates via `messaging.outbound_mode(...) == "suppressed"` (growth.py:236) — suppressed leads never surface in feed
- `scan_followups()` gates at enqueue time: `messaging.outbound_mode(biz, phone) == "suppressed"` (reminders.py:441) — suppressed leads never queued

#### ✓ START re-opt-in works

- `consent.opt_in_nlu(body)` check runs BEFORE the `is_suppressed` silent-drop (app.py:2711) — confirmed reading. A suppressed user who sends START is re-subscribed before falling through to the silent-drop.
- `db.set_opt_in()` clears `opted_out=0, opted_out_at=NULL` (db.py:3549-3555)

#### ✓ A2P gate blocks customer sends until approved

- `send_sms:140-145`: `if gate and configured() and not compliance.a2p_ready(business)` → returns `"blocked"`, queues to blocked_sends
- Owner alerts bypass with `gate=False` (alerts.py:347)
- `growth.scan()` skips A2P-unapproved businesses (growth.py:406) to prevent doomed growth queue entries

#### ✓ Quiet-hours backstop fires at TRANSMIT time (not just enqueue)

- `messaging.send_sms:120-134`: transmit-time check runs after the opt-out check, before the A2P gate
- Only fires on `gate=True, transactional=False` — correctly guards marketing sends
- **EXCEPT** for P0-1: growth kinds don't pass `transactional=False` via `run_due_once`

#### ✓ Auto mode is SERVER-locked

- `settings_growth_mode` at app.py:1292-1303 rejects `mode not in ("off", "tray")` → coerces to "off"
- `db.set_growth_mode()` accepts "auto" from code, but the only HTTP endpoint blocks it
- Direct DB write would bypass, but no web-accessible path can set mode="auto"
- In `growth.scan()`, even if `mode == "auto"`, only `review_request` inserts as `pending`; all other growth kinds insert as `held` (growth.py:432-438) — auto mode doesn't bulk-fire everything, only review requests

#### ✓ One-tap requires explicit owner GO

- growth tray release requires: login_required + `_csrf_ok()` + `db.release_growth_batch()` (app.py:1334-1345)
- SMS GO command: `is_owner` check via `messaging.to_e164(caller) == owner_cell` (app.py:2720) — refuses non-owner
- `release_growth_batch` only flips `held` → `pending`; `run_due_once` sends only `pending` rows
- No path exists to auto-send a customer text without owner GO (except the P0-1 quiet-hours bypass)

#### ✓ All proactive digests go to owner cell, never customer

- `alerts.notify()`: `sms_to = (business.get("alert_sms") or "").strip()` (alerts.py:343), `gate=False`
- `scan_daily_digest`, `scan_growth_tray`, `scan_morning_briefing`, `scan_stall_nudges` all route through `alerts.notify()` — confirmed never touch a lead phone number
- `test_vic_proactive.py:383-389` explicitly verifies no send goes to a lead phone number

#### ✓ FCC voice consent revocation on opt-out

- STOP → `db.set_voice_consent(biz["id"], caller, False)` (app.py:2692)
- NLU revocation → `db.set_voice_consent(biz["id"], caller, False)` (app.py:2699)
- cancel→opt-out → `db.set_voice_consent(biz["id"], caller, False)` (app.py:2687)
- Voice is gated behind `VOICE_PUBLIC_URL` — effective dead-gate until voice deploys

#### ✓ Blocked_sends auto-flush safety gates

All 8 rules confirmed in `connections.flush_blocked_sends()`:
1. Freshness: `FLUSH_MAX_AGE_HOURS` (default 6h) — stale rows skipped
2. Opt-out: `db.is_suppressed()` re-checked before flush
3. Quiet-hours: inherited from send_sms (transactional=True, correct for text-backs)
4. Dedupe: mark flushed BEFORE send; errors mark "send_error", never reset
5. Cap: limit=50, oldest-first
6. Conversation-coherence: subsequent real messages block re-send
7. All-stale handled (Rule 1 catches)
8. Still-blocked guard: if send_sms returns "blocked" → log + STOP immediately

---

## WORST-CASE TRACE: "Customer gets a 2am marketing SMS after STOP"

**After STOP:**
- `set_opt_out` sets `opted_out=1` in `contacts_consent`
- `messaging.send_sms:110` → `db.is_suppressed()` returns True → status="suppressed"
- `growth.plays()` → `outbound_mode == "suppressed"` → lead never surfaces
- Result: **CANNOT happen** (opt-out blocks all paths)

**Before STOP, at 2am, for a held growth play:**
- Owner replies GO at, say, 6pm
- `release_growth_batch` flips rows to "pending"
- `run_due_once` fires at next tick (could be seconds later if delay=0)
- P0-1: `_transactional = kind not in ("followup", "followup_2")` → True for winback/review_request
- `send_sms(..., transactional=True)` → quiet-hours block at line 120 is skipped
- **Send goes out at 2am if the tick fires at 2am after a zero-delay play was released at 11:59pm**

Wait — reconsider timing: owner releases GO at 6pm, delay=0, tick fires within TICK_SECONDS (default 60s). The send fires at ~6pm, not 2am. For a 2am scenario, the owner would need to send GO at 2am. The quiet-hours backstop would still be bypassed. **The P0 is real but the window is: owner acts during quiet hours, growth play fires in quiet hours.** That's a genuine TCPA risk.

---

## SUMMARY

| # | Severity | File:Line | Finding |
|---|----------|-----------|---------|
| 1 | **P0** | `reminders.py:342` | Growth kinds (review_request, quote_followup, reactivation, winback, referral, membership) resolve `_transactional=True` and bypass quiet-hours backstop. Only followup/followup_2 correctly pass `transactional=False`. |
| 2 | P1 | `connections.py:467` | flush_blocked_sends uses `transactional=True` — currently correct (text-backs only), but needs comment documenting why |
| 3 | P1 | `app.py:2097` | Appointment cancellation SMS uses default transactional=True — send at any hour; low legal risk (service notification) |

**P0 count: 1 | P1 count: 2**

**Deploy verdict: HOLD on P0-1.** Fix `reminders.py:342` to include all GROWTH_KINDS in the non-transactional set before deploying. The fix is a one-line change with a test.
