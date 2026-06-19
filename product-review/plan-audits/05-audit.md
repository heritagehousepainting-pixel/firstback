# Plan 05 — Audit Report
**Audited:** 2026-06-19  
**Plan file:** `product-review/plans/05-NOTIFICATIONS.md`  
**Auditor role:** READ-ONLY red-team against live code. No source files were modified.

---

## Verdict

**READY-WITH-FIXES**

The plan is structurally sound and the safety architecture is correct. Three anchors are stale (line numbers and one function signature have drifted), one claim about `update_alert_prefs` is already partially shipped, and the `scan_daily_digest` flush mechanism for held alerts is phantom — no flush path exists yet and the plan doesn't build one, which is a silent semantic gap. Fix the anchors, note the missing flush, and ship.

---

## Corrected anchors

| Item | Plan says | Reality (file:line + real code) | Action |
|---|---|---|---|
| **db.py migration block location** | `~line 840` | `db.py:844` — `alert_on_roi_milestone` block starts there; `alert_on_daily_digest` at `849`. The `add after alert_on_daily_digest` instruction is correct but the block ends at line 850, not 840. | Update reference to `:844–850`; instruction is still correct. |
| **db.py `update_alert_prefs` location** | `~line 2395` | `db.py:2395` — exact match. `cols` list is at line `2398–2399`. | No change needed; line ref is accurate. |
| **db.py `update_alert_prefs` `cols` list** | Plan says to add `alert_on_roi_milestone` to `cols` as part of Change 1. | **Already present.** `db.py:2399`: `cols = ["alert_email", "alert_sms", "alert_on_lead", "alert_on_booking", "alert_on_urgent", "alert_on_daily_digest", "alert_on_roi_milestone"]` | Do NOT re-add `alert_on_roi_milestone`. Change 4's "confirm it is present" instruction is accurate — it is already there. Change 1 only needs to append the new columns. |
| **app.py `/settings` POST handler location** | `~line 1135` | `app.py:1135` — exact match. | No change needed. |
| **app.py `/settings` `alert_on_roi_milestone`** | Change 4 says to add `alert_on_roi_milestone` to the POST handler. | **Already present.** `app.py:1142`: `"alert_on_roi_milestone": 1 if request.form.get("alert_on_roi_milestone") else 0,` | Do NOT re-add. Change 4's app.py step is already shipped. Only the template checkbox and the `/signup` default are missing. |
| **app.py `/signup` handler** | Change 4 says to add `"alert_on_roi_milestone": 1` to the signup `update_alert_prefs` call (~line 334). | `app.py:334–341` — the signup call does NOT include `alert_on_roi_milestone`. The column default is `INTEGER DEFAULT 1` in the DB migration (`db.py:845`), so a new signup already gets the value `1` from SQLite's column default — no explicit write is needed. But if the plan wants the prefs row to be explicit (non-NULL), the add is valid and safe. | Low priority. DB column default handles it. The plan step is safe but optional. |
| **reminders.py `scan_stall_nudges` location** | `~line 615` | `reminders.py:619` — function starts at line 619, not 615. The `idle_leads` assignment is at line 640. | Update reference to `:619`. |
| **reminders.py `scan_daily_digest` location** | `~line 733`; "replace the `continue`" | `reminders.py:737–738`: `if n_leads == 0 and plays_count == 0 and not top_stall_name:` / `continue` — exact match for the condition; the `continue` is at line 738. | Update reference from `~733` to `:737–738`. |
| **reminders.py `tick_once` `tick_stale` block** | `~line 876` (`db.get_business(1)`) | `reminders.py:886`: `alerts.notify(db.get_business(1), "tick_stale", {` | Update reference from `~876` to `:886`. |
| **alerts.py `format_message` `daily_digest` branch** | `~line 161` | `alerts.py:169` — `if kind == "daily_digest":` starts at line 169. | Update reference from `~161` to `:169`. |
| **alerts.py `_TOGGLE_COL` claim** | "No change — roi_milestone already present" | `alerts.py:49`: `"roi_milestone": "alert_on_roi_milestone"` — confirmed present. | Correct, no action. |
| **alerts.py `notify()` in-app claim location** | "after `attempted.append(('inapp', 'recorded'))`" | `alerts.py:353–354`: `db.add_alert(bid, kind, "inapp", "", "recorded", dedupe, body)` then `attempted.append(("inapp", "recorded"))`. Quiet-hours gate must insert AFTER line 354. | Correct insertion point. |
| **`_biz_tz_for_alerts` helper** | Plan adds a new `_biz_tz_for_alerts` to `alerts.py` | `reminders.py:37–44` has an identical `_biz_tz()` helper (same lazy-import, same fallback). The plan's proposed `alerts.py` version is a valid non-circular copy. | Fine to duplicate, as the plan notes. Alternatively, import from reminders — but that would introduce a circular import (`alerts` <- `reminders` <- `alerts`). Keep the copy. |

---

## Safety check

**Customer TCPA / quiet-hours backstop is UNTOUCHED by this plan.**

The customer backstop lives at `messaging.py:113–134`:

```python
# Phase 1 C — SF-6: transmit-time quiet-hours backstop.
# Default is transactional=True (EXEMPT): ...
# Owner alerts (gate=False) skip this block entirely. The backstop's real job is to catch
# AD-HOC / GROWTH / MARKETING sends ...
if gate and not transactional:
    ...
    if tc_messaging.quiet_blocked(now_local, QUIET_START, QUIET_END, ...):
        return {"status": "deferred", "reason": "quiet_hours"}
```

Owner alerts already call `messaging.send_sms(..., gate=False)` (`alerts.py:361`), which skips this block at line 120: `if gate and not transactional:` (false when `gate=False`). Plan 05's quiet-hours gate is inserted INSIDE `alerts.notify()` BEFORE the `send_sms` call is even reached — it is a completely separate early-return at the `alerts.py` layer. The `messaging.py` backstop code is never touched.

The plan's `grep -n "gate=False" reminders.py` verification check is also correct and safe — `reminders.py` does not call `messaging.send_sms` with `gate=False` directly (those calls come through `run_due_once` which uses `gate=True` for customer sends). Zero new `gate=False` hits from this plan.

**The in-app "always writes" path is preserved.** `alerts.py:339–354` holds the `_dedupe_lock`, runs the dedupe check, writes `db.add_alert(... "inapp" ...)`, and appends to `attempted` — all BEFORE any quiet-hours gate the plan proposes to insert. The plan's insertion point is after `attempted.append(("inapp", "recorded"))` (line 354), which is correct. The in-app claim is never suppressed.

---

## Already-shipped alert kinds (do NOT re-add)

| Kind | Where registered | Shipped in |
|---|---|---|
| `a2p_approved` | `alerts.py:32` (ALERT_KINDS), `alerts.py:65` (_TOGGLE_COL), `alerts.py:211` (format_message), `alerts.py:242` (_subject), fired from `connections.py:537` | Batch A (plan 01) |
| `"lead"` with `known=True` flag | `alerts.py:80–86` — format_message handles `context.get("known")` to produce "Past customer… just called" copy; `app.py:2615–2623` fires it via `alerts.notify_async(biz, "lead", {..., "known": True})` | Batch B (plan 03) |
| `alert_on_roi_milestone` in `db.update_alert_prefs cols` | `db.py:2399` | Batch A (plan 01) |
| `alert_on_roi_milestone` in `app.py /settings` POST | `app.py:1142` | Batch A (plan 01) |

---

## Blockers

1. **Missing flush path for held alerts (Change 1 — semantic gap, not a crash).** The plan writes a `db.add_alert(... "sms_held", ...)` row when an alert is held during quiet hours, and says "flushed by the `scan_daily_digest` pass at 8 am instead." But `scan_daily_digest` (`reminders.py:675–755`) does NOT query or flush `sms_held` alert rows. It independently queries `assistant.briefing()`, `db.list_held_messages()`, and `db.warm_leads_idle()`. There is no code to read the `sms_held` rows and re-deliver them. The plan documents the intent but does not provide the implementation.  
   **Impact:** held alerts (non-urgent SMS during quiet hours) are silently swallowed — the owner's in-app feed shows a record, but the SMS is never retransmitted in the morning as promised. The quiet-hours gate would still prevent late-night SMS (good), but the "held until morning" promise would not be kept.  
   **Resolution needed:** either (a) implement a `flush_held_alerts(biz)` step inside `scan_daily_digest`, or (b) change the copy to "held alerts are surfaced in your daily digest summary" rather than "re-sent as SMS." The plan must explicitly build the flush path or document the behavior accurately.

2. **`vic_stall` dedupes as DAILY (26h) but Change 2 adds a cross-lead cap.** These two limits are independent and do not interfere, but the plan's test `test_stall_per_lead_dedupe_still_works` must pass when the cap fires before the dedupe window — that is, a cap of 2 means leads 3+ are skipped by the cap, NOT by the dedupe key (which is per-lead-per-day). No blocker, but the test must distinguish cap-skip from dedupe-skip to be meaningful.

---

## Notes

- **`vic_stall` maps to `alert_on_lead` toggle** (`alerts.py:52`) — not its own column. The stall cap (Change 2) could be gated separately from `alert_on_lead`, which is the lead-arrival alert. The plan does not propose splitting this, which is the right call for simplicity; just document that turning off lead alerts also silences stall nudges.

- **Change 6 (`tick_stale` fan-out) and `_DAILY_DEDUPE_SECONDS` (26h).** The plan proposes looping over `db.list_businesses()`. With multiple businesses the dedupe key is `tick_stale:{day}` — this key is scoped per-business since `alert_recent` always filters by `business_id` (`db.py:2438–2441`). Each business gets its own dedupe window. The "no storm" claim in the plan is correct.

- **Webhook + quiet-hours interaction (Change 5).** The plan states the quiet-hours early-return means webhooks are also held during quiet hours. This is the correct behavior for Slack/Teams integrations. No action needed; confirm in tests that the webhook is NOT fired when quiet hours are active.

- **Smart-quote risk.** The plan's Python code blocks use only straight quotes. No curly-quote contamination observed. Safe to copy-paste directly.

- **`_URGENT_BYPASS_KINDS` includes `"tick_stale"`.** This is correct — a scheduler outage is fire-alarm level and should never be held. But `tick_stale` already rides the `alert_on_urgent` toggle (`alerts.py:62`). If the owner disables urgent alerts, the `_enabled_for` check at line 316 will skip the alert entirely before the bypass set is checked. The bypass only controls the quiet-hours gate; it does not override the enabled/disabled toggle. Document this clearly in the implementation: "bypass" means "ignore quiet hours," not "ignore toggle."

- **Change 3 (all_clear) `local_day` context key.** The proposed `ctx` dict includes `"local_day": local_day`. The `_dedupe_key` for `daily_digest` uses `context.get("local_day")` (`alerts.py:272`). Since the all_clear variant calls `alerts.notify(biz, "daily_digest", ctx)` with the same key, the 26h dedupe will correctly prevent a double-send between the real digest and the all-clear variant on the same day. This is the right behavior and the plan's claim is accurate.
