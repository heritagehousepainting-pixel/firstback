# Phase 6b — Integration POLISH — BUILD SPEC (reconciled)
**Date:** 2026-06-19 · Base: `staging` @ b7ca8c1 (60/60 green) · Orchestrator: Opus
**Inputs:** `PLAN-INTEGRATION.md` §W2/W3/3A + the 2-agent prebuild audit (`phase6/audit/6B-PREBUILD-{TICKER,ALERTS}.md`, line-number ground-truthed). Both reports hold the exact line-level diffs; this spec locks decisions + resolves the two tensions.

## Scope (orchestrator-defined 6b = W2 + W3 + stale-ticker alert ONLY; W4–W7 are 6c)
Build directly (serial): reminders.py + alerts.py + db.py + app.py + llm.py + settings.html all collide, so no parallel worktrees. Honesty gate: a be-audit on the consent/owner-SMS/honesty surface after build.

### Grounding wins from the audit (like 6a — premises corrected)
- **`assistant._compose_briefing` is DB-only** (no LLM) → the plan's W3 ("move the Claude call out of the morning briefing") is MOOT for that path. The digest path is LLM-free.
- The REAL ticker LLM call is **`reminders.followup_body_contextual`** (llm.complete → anthropic SDK, default 600s timeout) via `scan_followups`. That is W3's true target.
- **`/tasks/run-due` calls `tick_once()`** (app.py:2867) → the external cron drives tick_once, so gap-detection at tick_once start fires on the recovery tick (resolves the "bootstrap" worry). anthropic 0.109.2 supports the client `timeout=` kwarg.
- **GO-parser folding is SAFE**: the inbound GO handler (app.py:2717-2724 → `_handle_tray_reply` → `db.release_growth_batch`) gates only on owner-cell + body=="GO" + held plays existing; it is independent of any alert kind. The unified digest's "Reply GO" works unchanged.
- **Recipient is owner-cell only**: `alerts.notify` → `business["alert_sms"]`, `gate=False`. No path to a customer phone.

## DECISIONS (decider resolving the two audit tensions)
1. **NO feature flag.** The unified digest is the DEFAULT and only morning behavior. REMOVE the `scan_morning_briefing` + `scan_growth_tray` CALLS from `tick_once`; ADD `scan_daily_digest(now)`. (The alerts auditor suggested gating behind `FIRSTBACK_UNIFIED_DIGEST=1`; overruled — pre-launch, single tenant, the old 3-SMS behavior is the defect we're fixing, a permanent dead config knob isn't worth it.) **KEEP both functions defined + the `vic_morning`/`growth_tray` kinds in ALERT_KINDS** (their direct-call unit tests + the in-app feed's historical rows depend on them).
2. **tick_stale fires from `tick_once` start (gap detection), NOT a separate endpoint.** Because `/tasks/run-due` → `tick_once`, the external cron path triggers the gap check on recovery. Total death (cron+ticker both down) is NOT self-detectable → owner-ops external uptime monitor on `/health/ticker` (document in SETUP_NEEDED).

## THE BUILD (exact diffs in the two audit reports; line refs there)

### W2 — unified 8am digest
- **reminders.py:** new `scan_daily_digest(now)` (6B-TICKER §F) — 8am `[8,9)` window; pull `n`/`money` from `assistant.briefing` (DB-only); held plays from `db.list_held_messages` + `growth._job_value`; top stall = `db.warm_leads_idle` **sorted by idle_hours desc** (warm_leads_idle is NOT pre-sorted — landmine); gate "skip unless n>0 OR held_count>0 OR top_stall"; `alerts.notify(biz,"daily_digest",ctx)`. **MUST NOT call `release_growth_batch`** (L2/TCPA — digest only alerts; the owner's GO releases).
- **reminders.py tick_once:** remove the two morning scan calls (6B-TICKER §E), add `scan_daily_digest(now)`.
- **reminders.py scan_stall_nudges:** add `if now_local.hour < 12: continue` (afternoon-only; the digest's top-stall covers the morning). Fix `test_vic_proactive.py` lines ~419/425 (bare `scan_stall_nudges()` calls) to pass an afternoon timestamp.
- **alerts.py:** add `daily_digest` to ALERT_KINDS, `_DAILY_DEDUPE_KINDS`, `_TOGGLE_COL`→`alert_on_daily_digest`; `format_message` (combine leads + plays(+GO/SKIP only when plays>0) + top-stall, ≤320 cascade, honest: never "tap to send" for leads, label estimated money, never claim a customer text was sent); `_subject`; `_dedupe_key` = `daily_digest:{local_day}`. (6B-ALERTS §1)
- **db.py:** `alert_on_daily_digest INTEGER DEFAULT 1` migration (solo-ALTER pattern); add to `update_alert_prefs` cols whitelist (**L1 — the most-likely-missed line**).
- **app.py:** settings POST handler + signup defaults set `alert_on_daily_digest`; **settings.html** add the toggle. Default-ON for existing tenants (column default + `_enabled_for` null-check + macro).

### W3 — bound the one ticker LLM call
- **llm.py** (kernel — edit firstback's copy directly, never run sync.py): add `timeout=None` kwarg to `complete`; apply only when passed (preserve SDK default for existing callers — minimal blast radius), constructing `anthropic.Anthropic(timeout=httpx.Timeout(timeout, connect=5.0))`.
- **reminders.py `followup_body_contextual`:** pass `timeout=10`. Existing `except Exception → generic fallback` already catches `APITimeoutError`.

### Stale-ticker alert
- **reminders.py tick_once:** read `_prev = db.get_meta("last_tick_utc")` BEFORE the heartbeat write; after the write, if `_prev` existed and gap > 900s (15m), `alerts.notify(db.get_business(1), "tick_stale", {gap_minutes, local_day})`. Wrapped in try/except (never crash the tick).
- **alerts.py:** add `tick_stale` kind (rides `alert_on_urgent`, no new column), `format_message` (honest "scheduler hasn't run in ~Xm — texts/reminders may be delayed"), `_subject`, `_dedupe_key`=`tick_stale:{local_day}`, `_DAILY_DEDUPE_KINDS` (one alert/day during an outage, not one/tick).
- **SETUP_NEEDED:** total death needs an external uptime monitor on `/health/ticker` (owner-ops).

## TESTS (standalone: `.venv/bin/python test_X.py`)
- new `test_daily_digest.py`: fires at 8am with data + dedupes (2nd tick same day = 0); combines leads+plays+stall; **owner-cell-only (zero customer recipients)**; **GO after digest releases held plays**; format ≤320 + "GO" + "(est.)" + "Maria 26h"; empty state (no leads/plays/stall) does NOT fire.
- test_vic_proactive.py: fix the bare-timestamp stall calls; add "morning stall suppressed (hour<12)".
- test_reminders.py: `followup_body_contextual` falls back to the generic template when `llm.complete` raises (timeout).
- test_ticker_health.py: gap-detection fires a `tick_stale` after a >15m stale heartbeat (1st-ever tick with no prev does NOT fire); `tick_stale` format honest + short.
- test_alert_channel.py: signup row `alert_on_daily_digest==1`; settings POST toggles it.

## HONESTY / CONSENT GATES (verify empirically in the e2e)
- The digest goes ONLY to the owner cell; ZERO customer sends from scan_daily_digest.
- "Reply GO" still releases held plays (one-tap/owner-approval stays load-bearing; no silent customer send).
- Honest copy: no "tap to send" for leads, estimated money labeled, never claims a customer was texted.
- Nothing deploys; pricing unchanged; voice stays "coming soon".
