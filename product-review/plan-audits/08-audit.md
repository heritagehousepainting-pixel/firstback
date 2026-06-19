# Plan 08 Audit — Call Screening Value + Visibility

**Audited:** 2026-06-19
**Auditor:** read-only red-team pass against current code
**Plan file:** `product-review/plans/08-SCREENING-VALUE.md`

---

## Verdict

**NEEDS-REWORK**

Change 4 (Monthly Screening Report) violates the master-plan's "one monthly digest" mandate: it adds `scan_screening_report()` as an independent scanner that fires its own SMS on the 1st, rather than riding plan 06's `scan_monthly_recap()` as a `screening_section` context key. The plan even acknowledges this conflict in a coordination note but then implements the wrong path anyway. Additionally, the `_TEMPLATES`/`_SUBJECT` dict approach described for `alerts.py` is a fiction — those dicts do not exist; `format_message` is an if/elif chain — and the plan's code snippets contain em-dashes in string literals that will not paste as ASCII. Changes 1, 2, 3, and 5 are sound with minor anchor corrections.

---

## Corrected Anchors

| Item | Plan Says | Reality (file:line + real code) | Action |
|------|-----------|----------------------------------|--------|
| **C1a — settings.html subtitle line 87** | "card subtitle at line 87, currently: `Like your phone's screen...`" | Confirmed: `templates/settings.html:87` reads `'Like your phone's screen: FirstBack only texts back real prospects — it skips people you already know (they're yours to handle) and suspected spam/robocallers. No setup, no contact import required.'` | Correct. Edit line 87 Jinja `subtitle=` argument. |
| **C1a — settings.html enforce description line 111** | "Change the enforce-mode description paragraph (line 111) to lead with the allowlist" | `settings.html:111` reads `<p class="provider-note provider-note-ok"><strong>Enforcing.</strong> Known callers (anyone you've booked or saved)...`. The plan's target paragraph is on line 111. Confirmed. | Correct. |
| **C1b — dashboard.html "Spam Shield is active" card lines 100–105** | "lines 100–105 is the only post-enforcement surface" | `dashboard.html:99-105` is the enforce block. Line 103 is the false-positive line. The plan says "Add one sentence after the robocallers-blocked count" — the count is at line 102, false-positives at 103. New sentence would go between lines 102 and 103 (or after 103 before `</span>`). | Lines are correct. The "after the robocallers-blocked count" instruction means after line 102. The sentence goes before the false-positive line (line 103). |
| **C1c — dashboard.html monitor-mode nudge line 38** | "monitor-mode nudge (line 38) currently explains what it would screen. Prepend the allowlist benefit" | `dashboard.html:38` reads `<span class="review-nudge-text"><strong>Call screening is in monitor mode.</strong> It's logging what it *would* screen...`. Confirmed. The nudge is wrapped in a `{% if monitoring %}` at line 35. | Correct. |
| **C2a — app.py dashboard route `~L786–814`** | "extend the graduation block to also pass the in-window would_screen_spam count" using `_window_start_str` | The dashboard route is at `app.py:727`. The graduation block runs `788–803`. The plan proposes `_window_start_str = _window_start` and then calls `db.screening_stats(biz["id"], since=_window_start_str)`. However, `db.screening_stats` is already called at line 808 **without a `since` parameter** (returning all-time stats). A second `since`-filtered call must be added for the graduation progress stats only — it's additive, not a change to the existing call. | Add a second `db.screening_stats(biz["id"], since=_window_start)` call inside the `if (_window_start and ...)` block (lines 793–802), not in place of line 808. The variable name `_window_start_str` is unused complexity — pass `_window_start` directly. |
| **C2b — dashboard.html "shield-learning" card lines 107–110** | `<strong>Spam Shield: Learning (Day {{ grad_day }} of {{ grad_total }}...` | `dashboard.html:107-109` confirmed: `<span class="review-nudge-text"><strong>Spam Shield: Learning (Day {{ grad_day }} of {{ grad_total }}).</strong>\n  Watching for {{ grad_total }} days before it can block automatically. Nothing is silenced yet.</span>` | Correct lines. The plan's replacement block is sound; add `grad_verdicts` and `grad_verdicts_min` to context. |
| **C2d — reminders.py scan_screening_graduation `~L754–823`** | "after the `would_block` read, add the volume-aware path" | Actual code: `reminders.py:810-816` — `stats = db.screening_stats(bid, since=window_start)` then `would_block = stats.get("would_screen_spam", 0)` then `if would_block < SCREEN_GRADUATION_MIN_VERDICTS: continue`. Note: `db.screening_stats()` already returns `total` (COUNT(*)) at `db.py:1987`. The plan's `stats.get("total", 0)` is valid without any db.py change. The plan's new `min_verdicts` branch must go between lines 815 and 816 (after `would_block =` assignment, before the `if would_block < ...` check). | Insert the volume-aware block at `reminders.py:815.5` (between the `would_block` assignment and the `if would_block < SCREEN_GRADUATION_MIN_VERDICTS` guard). `db.screening_stats` already provides `total`. No db.py change needed. |
| **C2d — config import update** | "Import `SCREEN_GRADUATION_MIN_VERDICTS_LOW_VOLUME` and `SCREEN_GRADUATION_LOW_VOLUME_THRESHOLD` from config at the top of `reminders.py` (add to the existing config import line)" | `reminders.py:31-33` has `from config import (app_tz, REMINDER_LEAD_HOURS, FOLLOWUP_IDLE_HOURS, TICK_SECONDS, QUIET_START, QUIET_END, SCREEN_GRADUATION_DAYS, SCREEN_GRADUATION_MIN_VERDICTS, SCREEN_MODE)`. These two new constants must be added to this tuple. | Confirmed — extend the existing import at line 32. |
| **C3a — dashboard.html line 103 false-positive text** | `· {{ screening_false_positives or 0 }} false positive{{ 's' if screening_false_positives != 1 else '' }}.` | `dashboard.html:103` confirmed: exact match. | Correct. |
| **C3b — app.py rescue route `~L2175–2203`** | "after `db.record_screening_rescue`, return a success payload with a toast message" | The rescue route decorator is at `app.py:2180`, function body at `2182–2208`. The current return at line 2208 is `return jsonify(ok=True, lead_id=lead["id"])` with no `toast` key. The `screen-real` JS handler in `static/app.js:1001` calls `await apiFetch("/api/calls/" + btn.dataset.id + "/real", csrfPost())` and then `window.location.reload()` — it does NOT read `data.toast`. Adding `toast=` to the JSON response is safe but the plan also requires **updating the JS handler** to display `data.toast` before reloading. | Plan says "Check `templates/dashboard.html` JS for the `screen-real` button handler" — the handler is actually in `static/app.js:991-1009`, not in `dashboard.html`. Update `static/app.js:1001-1002` to capture the response and display `data.toast` before `window.location.reload()`. |
| **C3c — settings.html line 90 "Spam Shield is enforcing" note** | "line 90 (`Spam Shield is enforcing` note)" | `settings.html:90` reads `<strong>Screening is enforcing.</strong> FirstBack is now blocking the text-back...`. The plan's proposed append is sound. | Correct. |
| **C4 — alerts.py `_TEMPLATES` dict** | "add `screening_report` to `_TEMPLATES` dict" | `_TEMPLATES` does NOT exist in `alerts.py`. The message body is defined via an if/elif chain in `format_message()` at `alerts.py:74-239`. | Add an `if kind == "screening_report":` branch to the `format_message()` chain, not to a `_TEMPLATES` dict. Same fix applies to `_SUBJECT` — it does not exist either; subjects are not part of `format_message` (SMS-only system, no email subject line needed). Drop the `_SUBJECT` instruction entirely. |
| **C4 — alerts.py `_TOGGLE_COL` for `screening_report`** | "add `screening_report`: `alert_on_screening_report` to `_TOGGLE_COL`" | `_TOGGLE_COL` at `alerts.py:46-65` is confirmed. `"alert_on_screening_report"` does not yet exist in the dict. | Add to `_TOGGLE_COL` dict and to `db.update_alert_prefs` cols list at `db.py:2398-2399`. Also add to the settings POST handler at `app.py:1135-1143` (currently only handles 5 toggles — `alert_on_screening_report` must be captured from the form and persisted). |
| **C5 — config.py `REPUTATION_INCLUDED`** | "`REPUTATION_INCLUDED = REPUTATION_PROVIDER != 'off' and bool(...)`" | Neither `REPUTATION_INCLUDED` nor the two low-volume constants exist in `config.py`. `REPUTATION_PROVIDER` at `config.py:132` and `TWILIO_ACCOUNT_SID/AUTH_TOKEN` at `config.py:196-197` are confirmed present. | All three new constants (`REPUTATION_INCLUDED`, `SCREEN_GRADUATION_MIN_VERDICTS_LOW_VOLUME`, `SCREEN_GRADUATION_LOW_VOLUME_THRESHOLD`) are new — safe to add. |
| **C5 — reputation.py `is_included()` circular import** | "`from config import REPUTATION_INCLUDED`" inside `is_included()` | `reputation.py:26-28` already imports from config at module load: `from config import (REPUTATION_PROVIDER, ..., TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, HIYA_API_KEY, HIYA_BASE_URL)`. The lazy `from config import REPUTATION_INCLUDED` inside the function body is unnecessary — add `REPUTATION_INCLUDED` to the existing top-level import line instead. | Move `REPUTATION_INCLUDED` to the existing `reputation.py:26-28` import. |
| **C5 — settings.html provider-card reputation block lines 117–128** | "The provider-card block (lines 117–128)" | `settings.html:118-127` — confirmed as the reputation provider card. The existing structure matches what the plan targets. Line 118 currently uses `' is-soon'` when not configured. The plan replaces it with `' is-available'`. That CSS class may not exist — check `app.css` before implementing. | Confirm `is-available` CSS class exists or reuse `is-soon`. |
| **C5 — settings.html prose "The free tiers above..."** | `"The free tiers above are always on. The two add-ons are extra layers..."` | `settings.html:141` confirmed: `The free tiers above are always on. The two add-ons are extra layers you can switch on later -- the screen works without them.` | Correct anchor. |

---

## One-Digest Compliance

**VIOLATION — Change 4 sends its own SMS; it does NOT ride plan 06's monthly recap.**

### Master plan mandate (00-MASTER-PLAN.md:9)

> "One monthly digest, not three. Monthly ROI recap (06) is the carrier SMS; **screening's monthly report (08) rides it as a section**, and the lifetime-ROI/review-delta surfaces (07) feed the same digest. One `scan_monthly_recap` on the 1st, one dedupe key, one SMS."

### What plan 08 actually does (Change 4c)

Plan 08 adds `scan_screening_report(now=None)` as a **standalone scanner** in `reminders.py` that:
1. Iterates every business independently
2. Calls `alerts.notify(biz, "screening_report", ctx)` with its own dedupe key
3. Fires on the 1st of the month in the [8, 10) local window — the same day as 06's `scan_monthly_recap` would run

This is a **second SMS to the owner on the 1st of the month**, in direct violation of the master plan.

### Plan 08's own coordination note (plan:182) says

> "The channel should be the **same monthly digest** (the ROI recap). This plan defines the `screening_report` alert kind with the screening numbers; the ROI agent should include it in the monthly digest body OR the screening card should be a second section of the same monthly email/SMS."

The plan acknowledges the correct path but then implements the wrong path. The `scan_screening_report()` function and the standalone `screening_report` alert kind must not be built as described.

### Required correction

Per 06-audit.md (the implementation guide for plan 06):

1. When `scan_monthly_recap()` is built (plan 06), it must pass a `screening_section` key in its context dict — populated by calling `db.screening_monthly_stats(biz["id"], prev_year, prev_month)`.
2. The `format_message("monthly_recap", context)` branch appends the screening content when `context.get("screening_section")` is set.
3. Plan 08 contributes: the `db.screening_monthly_stats()` function (Change 4a — safe to build independently) and the `screening_section` formatting logic inside `format_message("monthly_recap", ...)`.
4. Plan 08 must NOT add `scan_screening_report()`, must NOT add a standalone `"screening_report"` kind to `ALERT_KINDS`, and must NOT add a separate `alert_on_screening_report` toggle.

**The db function (`screening_monthly_stats`) and the settings toggle are the only Change 4 deliverables that survive unchanged.** Everything else in Change 4 must be restructured as a section inside the plan 06 monthly recap.

---

## Migrations

### New column: `businesses.alert_on_screening_report`

| Column | Type | DDL | Already Exists? |
|--------|------|-----|-----------------|
| `alert_on_screening_report` | `INTEGER DEFAULT 1` | `ALTER TABLE businesses ADD COLUMN alert_on_screening_report INTEGER DEFAULT 1` | **No** — confirmed absent from `db.py` schema and `db.update_alert_prefs` whitelist |

Migration gate pattern (follow `db.py:844-845`):
```python
if "alert_on_screening_report" not in biz_cols:
    c.execute("ALTER TABLE businesses ADD COLUMN alert_on_screening_report INTEGER DEFAULT 1")
```

Note: this column should only be added IF the standalone `screening_report` alert kind is built. Under the one-digest compliance fix, this column is NOT needed (the screening content rides the `monthly_recap` toggle already). If the column is added anyway as a future hook, gate it.

### No other new db columns required

- `SCREEN_GRADUATION_MIN_VERDICTS_LOW_VOLUME` and `SCREEN_GRADUATION_LOW_VOLUME_THRESHOLD` are config constants only — no DB columns.
- `REPUTATION_INCLUDED` is a derived config bool — no DB column.
- `screening_monthly_stats()` is a query function only — no schema change.
- All graduation columns (`screening_window_start`, `screening_false_positives`, `screening_promoted_at`, `screening_hold`, `screen_hard`, `screen_mid`, `reputation_enabled`) already exist — confirmed at `db.py:624-640`.

---

## Blockers

### Blocker 1 — Change 4 violates the one-digest mandate (critical)

`scan_screening_report()` sends a standalone SMS. This is explicitly forbidden by the master plan. The entire Change 4c (scanner function + `tick_once` wire-up + standalone `"screening_report"` kind registration) must be replaced with:
- `db.screening_monthly_stats()` contributed as a helper (Change 4a — ship as-is)
- A `screening_section` context key in plan 06's `scan_monthly_recap()`
- A `screening_section` block in plan 06's `format_message("monthly_recap", ...)` branch

### Blocker 2 — `_TEMPLATES` and `_SUBJECT` dicts don't exist in alerts.py

Plan 08 Change 4b says "add `screening_report` to `_TEMPLATES` dict" and "In `_SUBJECT` dict". Neither structure exists. `format_message` is an if/elif chain at `alerts.py:74-239`. `_SUBJECT` is irrelevant (SMS only). The agent implementing Change 4b must write an `if kind == "screening_report":` branch in `format_message()`, not a dict entry.

### Blocker 3 — `update_alert_prefs` whitelist not updated

The settings POST handler at `app.py:1135-1143` hardcodes which toggle columns are persisted. Adding `alert_on_screening_report` to the `settings.html` template (Change 4d) without also adding it to:
1. `db.update_alert_prefs` whitelist at `db.py:2398-2399`
2. The POST handler dict at `app.py:1135-1143`

...means the toggle renders but is silently ignored on save. This is a standard pattern that has been followed for every prior toggle; it must be followed here too.

### Blocker 4 — JS toast handler location is wrong

Change 3b says "Check `templates/dashboard.html` JS for the `screen-real` button handler." The handler is in `static/app.js:991-1009`, not in `dashboard.html`. The `dashboard.html` template only has the button markup at line 134. The JS must be updated at `static/app.js:1001-1002` to read `data.toast` from the API response before reloading.

---

## Notes

### Em-dashes in code snippets (smart-quote risk)

Seven em-dashes (`—`) appear inside fenced code blocks in the plan. When pasted directly into Python or Jinja, they will produce a `SyntaxError` or corrupt the string value. Affected snippets:

1. **Change 3a template block:** `rescued — saved as` (Jinja string literal — should be `—` or `—` or rewritten as ` — ` using ASCII ` - `)
2. **Change 3b return value:** `toast="Saved as a customer — they'll always..."` (Python string — em-dash in a string is allowed at runtime but risky when copy-pasted; use an ASCII hyphen or `—`)
3. **Change 4a docstring:** `-- defer to v2.` (already uses `--` ASCII in the docstring, but one line uses `—`)
4. **Change 4b `_TEMPLATES` lambda:** `f"FirstBack Screening — {ctx.get(...)}"` and `_SUBJECT` value (em-dash in f-string)
5. **Change 5b `is_included()` docstring:** `# shapes the settings UI copy — shapes...`
6. **Change 5c HTML title attr:** `Included in Pro — contact support...`

All must be replaced with ASCII `--` (double-hyphen) or ` - ` (space-hyphen-space) in actual code. The plan's markdown formatting is fine for reading but the dashes must not be pasted verbatim.

### settings.html smart-quote in subtitle (Change 1a target text)

The existing subtitle at `settings.html:87` contains HTML-entity apostrophes rendered as curly quotes in the browser but stored as straight ASCII in the template. When replacing, ensure the new subtitle uses the same straight-quote HTML encoding as the surrounding Jinja `subtitle=` argument.

### `is-available` CSS class (Change 5c)

The plan replaces `is-soon` with `is-available` on the reputation provider card. Confirm this class exists in `app.css` before using it. If it does not exist, reuse `is-soon` (already styled) or add the class.

### `grad_verdicts` context key on enforce-mode dashboards

The plan says "add the stats query only when `_window_start` is set to avoid the extra query on enforce-mode dashboards." The current code already gates on `_window_start and not biz.get("screening_promoted_at")` at `app.py:793`. The new `db.screening_stats(since=_window_start)` call must be inside that same `if` block — it must NOT replace the unconditional `screen_stats=db.screening_stats(biz["id"])` call at line 808 (which feeds the enforce-mode card).

### `scan_screening_report` fires on the 1st — same day as 06's scan

The plan's proposed scanner fires on `now_local.day == 1` in the `[8, 10)` window. Plan 06's `scan_monthly_recap` fires on days 28-31 (per the plan) OR on the 1st (depending on final implementation). If both scans run on the 1st, the same business owner gets two SMS messages in a two-hour window. The one-digest fix (Blocker 1) eliminates this entirely — confirm the resolution is the one-digest path, not separate day-gating.

### No Batch A/B collisions

Changes 1, 2, and 3 are pure template copy edits and config/logic extensions. None of the functions they touch (`is_known_caller`, `screening_stats`, `record_screening_rescue`, `promote_screening`, `scan_screening_graduation`) were modified by Batch A or B. No collision.

### `test_screening_graduation.py` exists and should be extended

The file exists at `/Users/jonathanmorris/apps/firstback/test_screening_graduation.py`. The plan's new low-volume graduation tests (Change 2 standalone tests) should be added to this file, not a new one. The existing test infrastructure (real temp DB, `check()` helper, `db.init_db()`) is already set up correctly for these cases.
