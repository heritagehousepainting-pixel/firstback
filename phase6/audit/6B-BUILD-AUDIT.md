# Phase 6b Build Audit — Ship Gate

**Date:** 2026-06-19  
**Branch:** `staging` (working tree, uncommitted)  
**Suite:** 61/61 test files green (verified by running all)  
**Auditor lens:** be-audit — consent/TCPA, owner-vs-customer SMS, honesty, correctness  

---

## A. CONSENT / TCPA

### A1. `scan_daily_digest` sends ONLY to the owner cell — CONFIRMED

`scan_daily_digest` (reminders.py:658–743) calls only `alerts.notify(biz, "daily_digest", ctx)`.  
`alerts.notify` (alerts.py:295–358) fans out to:

- `sms_to = business.get("alert_sms")` → the owner's cell, sent with `gate=False` (A2P-exempt) — line 343–347.
- `email_to` → the owner's alert email or login email — line 352–357.

There is no path from `scan_daily_digest` to any customer phone number. The function never passes a lead phone to `messaging.send_sms`. The `run_due_once` send loop (which sends to customer phones via `scheduled_messages`) is called separately and is not touched by the digest. **Zero customer sends: CONFIRMED.**

### A2. `scan_daily_digest` does NOT call `release_growth_batch` — CONFIRMED

A search for `release_growth_batch` in `reminders.py` finds exactly two call sites: `app.py:1263` (`_handle_tray_reply`) and `app.py:1344` (UI tap). `scan_daily_digest` contains no call to `release_growth_batch`, `release_growth_play`, or any status-flip on `scheduled_messages`. Held plays remain `status='held'` after the digest fires. Test D (`test_daily_digest.py:163`) asserts `len(db.list_held_messages(bizB["id"])) == 1` after the digest and passes.

### A3. GO handler is unchanged and gated on owner-cell only — CONFIRMED

`_parse_tray_reply` / `_handle_tray_reply` (app.py:1240–1288) are not modified in this diff. The inbound SMS handler (app.py:2717–2726) gates `is_owner` on `messaging.to_e164(caller) == owner_cell`; any other caller falls through to the normal customer handler. The digest's "Reply GO to send all" instruction routes only if the owner's phone number sends "GO". Test E (`test_daily_digest.py:165–167`) calls `_handle_tray_reply(bizB, {"cmd": "go"})` and confirms plays are released after the digest. **GO still load-bearing: CONFIRMED.**

### A4. No new silent customer send anywhere in the diff — CONFIRMED

The diff adds: `scan_daily_digest`, `tick_once` stale-ticker block, `alerts.format_message` for two new kinds, `alerts.notify` plumbing, `db` migration, `app.py` settings/signup wires, `llm.complete` timeout kwarg. None of these send to a customer phone. `llm.complete` does not send SMS. The timeout kwarg to `followup_body_contextual` does not affect send routing (the existing `except Exception → generic fallback` path was already there).

---

## B. HONESTY

### B1. `format_message('daily_digest')` copy — CONFIRMED HONEST

Examined alerts.py:161–202:

- **No "tap to send" for leads.** The leads segment reads `"N leads need/need you, ~$X on the table (est.)."` with no instruction to tap or send. The GO/SKIP line appears only when `plays_count > 0` — it refers to held growth plays, not leads.
- **No claim a customer was texted.** The digest says "N texts ready" (plays awaiting approval), never "N texts sent." The word "ready" is accurate.
- **Estimated money is labeled.** When `is_estimated=True` and `money` is non-empty, the suffix `(est.)` is appended (line 178). Verified by test A (`"(est.)" in body`).
- **Zero-leads case never says "0 leads".** The `leads_seg` is built only when `n_leads` is truthy (line 175: `if n_leads:`). Tested by `test_daily_digest.py:116–119`.
- **320-char cap cannot produce a misleading truncation.** The cascade is: (1) drop plays detail, (2) drop stall line, (3) hard-truncate to 317 + "...". The third step is de facto dead code: with `_plays_seg(False)` (fixed ~53 chars) plus `leads_seg` (bounded by real integer + currency string, max ~60 chars), the base is always < 200 chars. Tested by test A with a 10-play 370-char summary that still lands ≤ 320.

### B2. `format_message('tick_stale')` copy — CONFIRMED HONEST

alerts.py:203–211: `"FirstBack's scheduler hasn't run in ~Xm -- texts and reminders may be delayed. Check the Render cron / restart the service."` — accurately describes the situation, uses "may be delayed" (hedge appropriate since the scheduler might have self-recovered), never overclaims. Max length at `gap=999m` is 125 chars, well within SMS single-segment. Verified by `test_ticker_health.py` ("tick_stale copy names the delay + the scheduler", len ≤ 200).

---

## C. CORRECTNESS

### C1. Dedupe — CONFIRMED

- `daily_digest` is in `_DAILY_DEDUPE_KINDS` (alerts.py:38) → 26h window. Key = `daily_digest:{local_day}` (alerts.py:256–259). A second `tick_once` call within the same local day hits `db.alert_recent` and returns `[]`. Tested by `test_daily_digest.py:143–146` (second 8am tick → `fired2 == 0`).
- `tick_stale` is in `_DAILY_DEDUPE_KINDS` (alerts.py:38) → 26h window. Key = `tick_stale:{local_day}` (alerts.py:260–264). A sustained outage (cron fires every 60s) cannot produce an SMS storm; only one `tick_stale` fires per UTC day.

### C2. Afternoon-only stall gate uses business-LOCAL hour — CONFIRMED

`scan_stall_nudges` (reminders.py:610–621): the `now_local` variable is resolved via `_biz_tz(biz)` and `datetime.fromisoformat(now).astimezone(tz)`. The `if now_local.hour < 12: continue` guard is evaluated against the business-local datetime, not UTC. The test (`test_vic_proactive.py`) explicitly builds `_morn_local = datetime.now(_btz).replace(hour=9, ...)` and converts to UTC before passing to `scan_stall_nudges`, verifying the business-tz interpretation. **Business-local hour: CONFIRMED.**

### C3. Stale-ticker gap math — CONFIRMED

reminders.py:848–873:

- **Reads prev BEFORE write:** `_prev_tick_utc = db.get_meta("last_tick_utc")` (line 849) runs before `db.set_meta("last_tick_utc", _tick_utc)` (line 853). Correct — the gap is measured against the actual previous tick, not the current one.
- **None / empty-string / first-ever tick:** `db.get_meta` returns `None` when the key is absent; `set_meta` stores an empty string when explicitly set to `""`. Both are falsy; `if _prev_tick_utc:` (line 862) skips. `tick_stale` does not fire on the first-ever tick. Test: `test_ticker_health.py:107–110` sets `""` and asserts zero fires.
- **Naive timestamp handling:** `if _prev_dt.tzinfo is None: _prev_dt = _prev_dt.replace(tzinfo=timezone.utc)` (lines 864–865) guards against naive ISO strings written by older code paths.
- **Never crashes the tick:** entire block is inside `try/except Exception` (lines 861/872–873). Exception is logged to stderr; tick continues.
- **Fires once after a real gap:** tested by `test_ticker_health.py:113–122` (20-minute gap → 1 fire; gap_minutes in [18, 22]).
- **60s normal gap does NOT fire:** tested by `test_ticker_health.py:125–130`.

### C4. `llm.complete` timeout — BACKWARD-COMPATIBLE: CONFIRMED

`llm.complete` signature (llm.py:134–135): `timeout=None` is a keyword-only argument (after `*`). Existing call sites that do not pass `timeout` continue to receive `None`, and the `if timeout is not None:` guard (line 169) leaves the code path byte-for-byte identical to pre-6b — `client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)` (line 173–174). `httpx` is imported lazily inside the `if` block (line 170), so callers that never pass `timeout` do not pay the import cost. Anthropic 0.109.2's `Anthropic.__init__` accepts `timeout: float | Timeout | None | NotGiven` (verified from `_client.py:156`); `Timeout` is re-exported from `httpx` (anthropic `_client.py:36`), so `httpx.Timeout(10, connect=5.0)` is the correct type. **Backward-compatible and type-correct: CONFIRMED.**

### C5. `tick_once` return dict — CONFIRMED INTACT

The old code returned `{"queued": ..., "growth_queued": ..., "sent": ..., "contacts_synced": ...}` (reminders.py:920–921). The 6b diff removes the `scan_morning_briefing` and `scan_growth_tray` call blocks (which had no named return value bindings), replacing them with `scan_daily_digest(now)` (also no named return binding). The four variables `queued`, `growth_queued`, `sent`, `contacts_synced` are all still assigned before the return. **No orphaned variable, return dict intact: CONFIRMED.**

---

## D. NO REGRESSION TO RETAINED FUNCTIONS

`scan_morning_briefing` (reminders.py:472–526) and `scan_growth_tray` (reminders.py:530–599) remain defined and unchanged. `ALERT_KINDS` retains `"vic_morning"` and `"growth_tray"` (alerts.py:31–32). `_DAILY_DEDUPE_KINDS` retains both. `_TOGGLE_COL` retains both. `format_message`, `_subject`, `_dedupe_key` handlers for both remain. The functions are no longer called from `tick_once` (intentional, per spec decision 1), but their direct-call unit tests remain valid. `test_vic_proactive.py` and `test_briefing.py` (which exercise `scan_morning_briefing` paths) both pass with 0 failures.

---

## E. TESTS — SECURITY PROPERTIES ASSERTED

| Property | Test | File | Passes |
|---|---|---|---|
| Owner-cell only, zero customer sends | C, G (G: `not any(to.startswith("+15559992")...)`) | test_daily_digest.py | YES |
| No auto-release of held plays | D: `len(db.list_held_messages(...)) == 1` after digest | test_daily_digest.py | YES |
| GO after digest releases held plays | E: `_handle_tray_reply(bizB, {"cmd": "go"})` → `len == 0` | test_daily_digest.py | YES |
| Dedupe (daily_digest) | B: second 8am tick → `fired2 == 0` | test_daily_digest.py | YES |
| Morning stall suppressed (hour < 12) | "stall nudge suppressed in the morning (local hour 9)" | test_vic_proactive.py | YES |
| tick_stale gap: first-ever tick no fire | `no tick_stale when there is no prior heartbeat` | test_ticker_health.py | YES |
| tick_stale gap: >15m fires once | `tick_stale fires after a >15min heartbeat gap` | test_ticker_health.py | YES |
| tick_stale gap: normal gap no fire | `no tick_stale on a normal ~60s gap` | test_ticker_health.py | YES |
| LLM timeout threaded through | `followup_body_contextual passes a bounded timeout to llm.complete (W3)` | test_reminders.py | YES |
| signup default alert_on_daily_digest=1 | `new signup row has alert_on_daily_digest = 1 (6b default ON)` | test_alert_channel.py | YES |
| No "tap to send", honest labels, ≤320 | A suite (9 checks) | test_daily_digest.py | YES |

**No test weakens pre-existing coverage.** The stall-nudge tests in `test_vic_proactive.py` were strengthened (the bare `scan_stall_nudges()` call now passes an explicit afternoon timestamp, making the test more precise, not less).

---

## FINDINGS

### P0 Findings: ZERO

### P1 Findings: ZERO

### P2 Findings (notes, no ship impact)

**P2-1 — tick_stale `local_day` uses UTC date, not business-local (reminders.py:870)**  
The `tick_stale` dedupe key uses `datetime.now(timezone.utc).strftime("%Y-%m-%d")`. This is intentional for an ops alert (there is no "business" context — it targets `db.get_business(1)`, the operator account), but if UTC midnight crosses a calendar day during an active outage, two stale-ticker alerts could theoretically fire on the same real-world day. The 26h dedupe window substantially mitigates this. Not a TCPA issue. Note for the operator.

**P2-2 — The `test_growth_tray_sms.py` and `test_growth_tray_ui.py` summary lines use non-standard format**  
Their final output uses `===` separators rather than the bare `N passed, N failed` that the test-runner grep targets. Both pass (21/0 and 40/0 respectively); the format difference is a cosmetic pre-existing issue, not introduced by 6b.

**P2-3 — `scan_daily_digest` parses `n_leads` from a regex on the briefing headline (reminders.py:692–697)**  
If `assistant.briefing` changes its headline format, `n_leads` could silently become 0. This would cause the digest to be suppressed when there are actually leads (if plays_count and stall are also zero). Conservative failure mode: the owner misses the morning SMS, but no incorrect data is presented. Not a TCPA issue.

---

## VERDICT

**ZERO P0 findings. ZERO P1 findings.**

The build is clean for the Phase 6b ship gate.

- **Owner-cell-only gate: CONFIRMED.** `scan_daily_digest` routes exclusively through `alerts.notify` → `business["alert_sms"]` (owner cell, `gate=False`). No path to a customer phone exists.
- **No-auto-release gate: CONFIRMED.** `scan_daily_digest` never calls `release_growth_batch` or any status-flip on `scheduled_messages`. Held plays remain held until the owner's inbound GO (app.py:2724–2726) or in-app tap (app.py:1344).
- All 61 test files pass, 0 failures.
