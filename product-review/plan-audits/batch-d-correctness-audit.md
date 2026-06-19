# Batch D Correctness Audit — Plan 05 (Owner Alerts / Set-and-Forget)

Audited: `reminders.py`, `db.py`, `app.py`, `templates/settings.html`
Scope: uncommitted diff only (Batch D changes).

---

## Verdict

**SHIP-WITH-FIXES**

One P1 silent-mute defect: `alert_all_clear` text is silently swallowed when the owner has separately toggled off the daily digest (`alert_on_daily_digest=0`), with no UI warning. The remaining findings are P2 — clear in the form and easy to hit. No P0s found.

---

## Findings

| Severity | File:line | Issue | Fix |
|----------|-----------|-------|-----|
| P1 | `reminders.py:763` + `templates/settings.html:204` | `alert_all_clear` fires via `alerts.notify(biz, "daily_digest", ...)`, which is gated by `_TOGGLE_COL["daily_digest"] -> alert_on_daily_digest`. If an owner enables `alert_all_clear` but disables the main daily digest toggle, the all-clear text never sends and no error is shown. The two toggles appear in the same card with no documented dependency. | Add a note in the help text of the `alert_all_clear` toggle: "Requires 'One morning digest' to be on." Alternatively, in `scan_daily_digest`, check `biz.get("alert_all_clear")` before calling `alerts.notify` — but the UI-layer hint is the minimum fix. |
| P2 | `app.py:1163` | `_clamp_form_int` uses `int(request.form.get(name) or default)`. When the user submits `"0"` for `max_stall_alerts_day` (mute), the string `"0"` is truthy so it correctly reaches `int("0") = 0`. However when the user submits `"0"` for `alert_quiet_start` (midnight), the same path correctly returns `0`. This is fine, but the function silently diverges from the `_int_pref` used server-side: `_int_pref` has a `None`-specific guard whereas `_clamp_form_int` uses `or default`, meaning a form POST with a literal `"0"` would be lost only if the field is genuinely absent (not submitted). For `type='number'` inputs a missing field submits `""`, not `"0"`, so the fallback is triggered only on truly empty, not zero. No real-world bug exists today, but the two helpers diverge semantically and could cause confusion if a future caller passes `default=0`. | Document the distinction or unify the two helpers with the same `None`-check idiom. No immediate code change required. |
| P2 | `templates/settings.html:207` | Em-dashes (`—`, U+2014) inside `help='...'` Jinja string literal on the `alert_quiet_start` field call. Jinja2 handles UTF-8 in string literals correctly, and Flask's autoescape renders them safely as HTML text. Not a runtime bug, but some editors and diff tools flag non-ASCII in Jinja templates as a contamination risk. | Replace with HTML entity `&mdash;` or ASCII `--` for hygiene. Not blocking. |
| P2 | `templates/settings.html:209` | Curly/smart quotes (`"` `"`, U+201C/U+201D) inside `help='...'` for the `max_stall_alerts_day` field. Same reasoning as above: safe in HTML text context, but non-standard in source. | Replace with `&ldquo;`/`&rdquo;` or straight ASCII `"`. Not blocking. |
| P2 | `templates/settings.html:210` | Ellipsis (`…`, U+2026) in `placeholder='https://hooks.slack.com/…'`. Same: safe in HTML attribute context. | Replace with `...`. Not blocking. |

---

## Verified-good

**Lane 1 — STALL CAP (`scan_stall_nudges`)**
- Sort is `reverse=True` by `idle_hours` (most-idle first). Confirmed correct at `reminders.py:658`.
- `nudged` initializes to `0` inside the per-business loop (not shared across businesses). Confirmed at line 660.
- `nudged` increments only inside `if result:` (a real fire), not on exception-suppressed skips. Confirmed at lines 678–680.
- `cap=0`: `if nudged >= cap` fires before the first lead (0 >= 0 = True), breaking immediately. Mute works. Verified programmatically.
- Per-`(lead, day)` dedupe is handled by `alerts.notify`'s `vic_stall:{lead_id}:{day}` key, which is independent of `nudged` and `cap`. Unaffected by this change.
- `_int_pref` in `reminders.py:47–69`: uses `if val is None` guard, not `or default`. Treats `0` as `0` (mute), not as missing. Correctly diverges from the plan spec (which had the weaker `or default` pattern).

**Lane 2 — ALL-CLEAR (`scan_daily_digest`)**
- Fires only when `biz.get("alert_all_clear")` is truthy, inside the `n_leads==0 and plays_count==0 and not top_stall_name` guard. Confirmed at `reminders.py:762–771`.
- `continue` at line 772 is at the outer `if n_leads==0...` indent level, NOT inside the `if biz.get("alert_all_clear"):` block. This means the active-day path (`ctx = {...}` at line 773) is always skipped when truly quiet — regardless of the opt-in. Correct.
- Uses `kind="daily_digest"` with `"all_clear": True` in context — shares the `daily_digest:{day}` dedupe key, so still at most one morning text even if both paths race. Confirmed via `alerts.py:300–303`.
- Active-day code path (lines 773–785) is unchanged.

**Lane 3 — `tick_stale` fan-out**
- Loops `db.list_businesses()` at `reminders.py:924`. Each call to `alerts.notify(_biz, "tick_stale", ...)` uses `bid = _biz["id"]`, so `alert_recent(bid, "tick_stale:{day}", window)` filters by `business_id`. No cross-tenant dedupe bleed. Confirmed via `db.py:2466`.
- `tick_stale` is in `_URGENT_BYPASS_KINDS` (`alerts.py:45`), so it bypasses quiet hours — correct for an ops-down alert.

**Lane 4 — DB MIGRATIONS**
- All 5 new columns guarded by `if col not in biz_cols:` with fresh `PRAGMA table_info` reads at each migration block. Confirmed at `db.py:854–863`.
- Defaults: `alert_quiet_start=22`, `alert_quiet_end=7`, `max_stall_alerts_day=2`, `alert_all_clear=0` (opt-out default), `alert_webhook_url TEXT` (NULL). All match the plan spec and are safe for existing rows.
- No collision with any column in the base `CREATE TABLE businesses` (lines 215–219) or earlier migration blocks.
- `update_alert_prefs` whitelist includes all 5 new columns at `db.py:2425–2426`. SET clause is parameterized: `f"{col}=?"` with values as positional args — no SQL injection vector. Confirmed at lines 2431–2433.

**Lane 5 — SETTINGS UI**
- `field()` macro imported from `components/ui/input.html` (`settings.html:4`); signature is `field(label, name='', value='', type='text', placeholder='', help='', ...)`. All new `field()` calls use named args that match the signature. Confirmed at `input.html:3`.
- `alert_toggle()` macro defined locally at `settings.html:15`; signature is `(name, label, on)`. The new `alert_toggle('alert_all_clear', "...", business.alert_all_clear)` matches exactly.
- Value defaults use Jinja2 `is not none` (lowercase) — correct Jinja2 syntax. Confirmed.
- Form field names match `app.py` POST handler keys exactly: `alert_quiet_start`, `alert_quiet_end`, `max_stall_alerts_day`, `alert_all_clear`, `alert_webhook_url`. All present in the `update_alert_prefs` call at lines 1175–1179.
- `_clamp_form_int` defined at `app.py:1161` — inside the `if request.method == "POST":` block, before first use at line 1176. Defined before use; no NameError risk.
- `_clamp_form_int` clamps ranges: `alert_quiet_start` 0–23, `alert_quiet_end` 0–23, `max_stall_alerts_day` 0–10. Invalid strings (e.g. "abc") return the default via `except (TypeError, ValueError)`. No 500 risk on bad input.
- Non-ASCII chars (em-dashes, curly quotes, ellipsis) are in Jinja string literal arguments (`help='...'`, `placeholder='...'`), not in Jinja expressions. Jinja2 handles UTF-8 in string literals; Flask autoescape renders them as HTML text. Not a runtime bug (flagged P2 for source hygiene only).

**Lane 6 — SIGNUP**
- `alert_on_roi_milestone: 1` added to signup `update_alert_prefs` call at `app.py:341`. Correct.
- The 5 new Batch D columns are NOT in the signup call — they rely on SQLite `DEFAULT` values (22/7/2/0/NULL). This is acceptable: defaults match the intended out-of-box behavior (quiet 10pm–7am, cap 2, all-clear off, no webhook). No row is left in an invalid state.
