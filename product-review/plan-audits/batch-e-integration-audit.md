# Batch E Integration Audit

**Scope:** `git diff e0c4def..HEAD` — plans 06+07+08, 5 worktree slices hand-harvested onto staging.
**Auditor note:** Each slice shipped with passing tests; this audit focuses solely on integration gaps from the stale-base harvest.

---

## Verdict

**SHIP-WITH-FIXES**

One P1 bug (seasonal frequency cap permanently bypassed because `"seasonal"` is not in `GROWTH_KINDS`) and one silent CSS undefined-variable (`--space-5`) need to be resolved before the growth-tray page is put in front of paying owners. Everything else — cross-slice alert merge, TCPA backstop, tenant scoping, CSRF gates, analytics blend, and honesty labels — is correct.

---

## Findings

| Severity | File:line | Issue | Fix |
|----------|-----------|-------|-----|
| **P1** | `reminders.py:316-317` | `GROWTH_KINDS` set does not include `"seasonal"`. When `run_due_once` delivers a seasonal held message, the `if kind in GROWTH_KINDS` guard at line 389 is false, so `add_growth_touch_log` is never called for `kind="seasonal"`. The 28-day blast cap (`recent_growth_touch_kind(biz, "seasonal", 28)` in `app.py:1489`) and the per-lead 30-day guard (`recent_growth_touch(biz, lead_id, 30)` at `app.py:1499`) both query `growth_touch_log` — which will always be empty for seasonal. An owner can hit `/growth/seasonal/launch` repeatedly on the same day with no cap applied. | Add `"seasonal"` to `GROWTH_KINDS` in `reminders.py:316`. |
| **P2** | `static/tokens.css:31` | `--space-5` is not defined in the design token set (defined values jump from `--space-4:16px` to `--space-6:24px`). Two new lines in `growth_tray.html` use it inline (`line 38`, `line 74`). The browser silently resolves this as `0`, collapsing the bottom-margin separating the seasonal card from the held-batch section. The `--space-5` token is also used by pre-existing code in `app.css:336` and `app.css:351` (pre-existing risk, not introduced by Batch E, but amplified by the new usages). | Define `--space-5:20px;` in `tokens.css` alongside the other space tokens. |
| **P2** | `app.py:1480-1509` | `launch_seasonal_campaign` contains no check for the `growth_streak_unlocked_at` gate that is enforced in `settings_growth_mode`. If an owner is still in Tray mode (not yet unlocked), the seasonal blast button is shown and the route accepts the POST — bypassing the streak contract silently. The seasonal card renders whenever `seasonal_play` is truthy, regardless of `growth_mode`. | Either: (a) skip rendering the seasonal card if `growth_mode != 'auto'`, or (b) add `if growth_mode != 'auto': return redirect("/growth/tray?seasonal_blocked=not_unlocked")` at the top of the route — whichever matches product intent. |

---

## Verified-good

### 1. Cross-slice alerts.py merge integrity

Both `monthly_recap` (E3) and `reputation_milestone` (E4) are present and correct in all four required anchors:

- **`ALERT_KINDS`** (line 33-34): both appended cleanly after `"a2p_approved"` — no collision.
- **`_TOGGLE_COL`** (lines 100-102): `monthly_recap → alert_on_daily_digest`; `reputation_milestone → alert_on_roi_milestone`. Neither shadows an existing key.
- **`format_message`** (lines 135-144 for `reputation_milestone`; lines 258-276 for `monthly_recap`): both branches present in correct if-chain order. `roi_milestone` branch at line 136 is intact (not clobbered).
- **`_subject`** (lines 307-308): both subjects appended inside the dict literal; `.get(kind, "FirstBack alert")` sentinel intact.
- **`_dedupe_key`**: `roi_milestone` branch at line 347 is intact; `monthly_recap` branch at line 351 newly added. `reputation_milestone` has no explicit branch and falls through to `f"{kind}:{context.get('lead_id')}"` → `"reputation_milestone:None"`. Given scan_google_reputation fires at 28-day intervals, the 120s dedupe window is harmless here.

### 2. `_QUIET_BYPASS_KINDS` gate

`alerts.py:433`: the notify() guard is now `if kind not in _URGENT_BYPASS_KINDS and kind not in _QUIET_BYPASS_KINDS`. Both frozensets are checked. `_QUIET_BYPASS_KINDS` = `{daily_digest, monthly_recap, growth_tray, vic_morning, vic_stall, screening_graduated}` — all scan-timed kinds that are never fired at night in production. The owner's TCPA backstop lives in `messaging.send_sms` (`gate=True` path in `messaging.py:77`) and was not touched in this batch — confirmed by `git diff e0c4def..HEAD -- messaging.py` returning no output.

### 3. `db.analytics()` blend

The additive blend at `db.py:3112-3124` is correct:

- All pre-existing return keys (`totals`, `series`, `avg_job_value`, `days`, `revenue`, `roi_multiple`, `avg_source`) are unchanged.
- Three new keys appended: `confirmed_revenue`, `estimated_pipeline`, `won_n`.
- `estimated_pipeline = max(0, (booked_n - won_n) * resolved_avg)` is clamped at zero — no negative pipeline on over-attribution.
- `won_leads()` is defined at line 3918; called from `analytics()` at line 3112. Python resolves function names at call-time, not definition-order, so no `NameError` risk.
- Tenant scoping: `won_leads` uses `WHERE business_id=? AND won_amount IS NOT NULL` with the business_id param. All other new db functions (`screening_monthly_stats`, `set_google_reputation`, `recent_growth_touch_kind`) include `business_id=?` in their WHERE clauses.

### 4. Security

- **`POST /api/leads/<id>/won`** (`app.py:2387`): `_csrf_ok()` checked first; returns 403 on failure. `db.get_lead(lead_id, biz["id"])` enforces tenant ownership — returns None (→ 404) if lead belongs to a different business. `db.mark_lead_won` raises `ValueError` on non-positive amounts; caught and returned as 400.
- **`POST /growth/seasonal/launch`** (`app.py:1480`): `_csrf_ok()` checked first; aborts 403 on failure. Opt-out guard: `messaging.outbound_mode(biz, phone) == "suppressed"` skips suppressed numbers. Per-lead recency guard via `db.recent_growth_touch`. Messages inserted as `status="held"` — never auto-sent; still requires the Tray GO step.
- **`GET /api/reputation`** (`app.py:1196`): `@login_required`; reads current business only via `current_business()`. Read-only; no mutation.
- **`poll_google_reputation`**: guarded by `if not _cfg.GOOGLE_PLACES_API_KEY: return None` — inert without the key. All HTTP calls wrapped in a bare `except Exception` that prints to stderr and returns None.

### 5. Template integrity

**`growth_tray.html` Jinja block balance:** All `{% if %}` / `{% endif %}` and `{% for %}` / `{% endfor %}` blocks are balanced. Block count: 12 `{% if %}` + 12 `{% endif %}` (including the inline one on line 67); 2 `{% for %}` + 2 `{% endfor %}`. No unclosed blocks.

**Block ordering as required:** seasonal card (`{% if seasonal_play %}`, lines 37-49) appears before the `{% if held %}` block (line 51); streak bar (lines 53-72) appears after `{% if held %}` and before the GO button row (line 74). Order is correct.

**CSS variables used in new Batch E template code:**

| Variable | Used in | Defined? |
|----------|---------|----------|
| `--surface` | `growth_tray.html:33,38,55,60` | `tokens.css:14` — YES |
| `--border` | `growth_tray.html:33,60,67` | `tokens.css:16` — YES |
| `--ink-soft` | `growth_tray.html:33,40,62,63` | `tokens.css:11` — YES |
| `--accent` | `growth_tray.html:38,55,67` | `ui.css:16` — YES |
| `--accent-strong` | `growth_tray.html:57` | `ui.css:19` — YES |
| `--radius` | `growth_tray.html:33,38,55,60` | `tokens.css:35` — YES |
| `--space-3` | `growth_tray.html:33,40` | `tokens.css:31` — YES |
| `--space-4` | `growth_tray.html:38,55` | `tokens.css:31` — YES |
| `--space-2` | `growth_tray.html:61` | `tokens.css:31` — YES |
| `--space-5` | `growth_tray.html:38,74` | **MISSING** — tokens.css skips from `--space-4` to `--space-6` _(P2 above)_ |

`--text-muted`, `--surface-card`, `--radius-lg` appear in the pre-existing (pre-Batch-E) body of `growth_tray.html` and are not introduced by this batch; they were pre-existing risk not in scope here.

**`dashboard.html` JS:** `csrfPost` is defined at `static/app.js:51`; `apiFetch` is defined at `static/app.js:25`. The `dashboard.html` `won-save-btn` handler calls both — wired correctly.

**`analytics.html`:** `renderHeadline` rewrite is present (lines 88-119 of the diff). The `confirmed_revenue`/`estimated_pipeline` split is correct — uses `confirmed` label for owner-entered dollars and `~$X estimated` for pipeline. No `collected`/`cash`/`actual` language for estimates. `roi-loss-note` (`id="roi-loss-note"`) is in the DOM (line 24 of diff) and referenced by the JS at lines 83 and 121. Reputation tile (`id="reputation-tile"`) and its JS IIFE are both present and guarded by `if (!data || data.review_count === null || data.review_count === undefined)`.

### 6. Honesty

- `monthly_recap` SMS (`alerts.py:264-276`): revenue labeled `(estimated)` or `(based on your job value)` depending on `avg_source`. No claim of collected money.
- `analytics.html` renderHeadline: owner-entered dollars labeled `"confirmed"` verbatim; unresolved bookings labeled `"~$X estimated"`. Note on page: "Revenue is an estimate — not collected money." intact at line 46 of diff.
- `reputation_milestone` copy (`alerts.py:139-145`): reports actual delta/baseline/current counts. No revenue claim.

### 7. `tick_once` wiring

`reminders.py:1067-1082`: `scan_monthly_recap` and `scan_google_reputation` are both wired in `tick_once` with independent `try/except` blocks. Failure of either does not interrupt the `scan_stall_nudges` or subsequent scanner passes.
