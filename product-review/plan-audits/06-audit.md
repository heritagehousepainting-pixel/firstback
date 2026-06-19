# Plan 06 Audit — ROI Proof + Monthly Recap

**Audited:** 2026-06-19  
**Auditor:** read-only red-team pass against current code  
**Plan file:** `product-review/plans/06-ROI-RECAP.md`

---

## Verdict

**READY-WITH-FIXES**

The overall architecture is sound: anchors for the JS headline swap, loss-framing sentence inserts, monthly recap scan, and won_amount schema are all technically feasible. However, six anchors are wrong (off by file, off by line, or the pattern described doesn't match reality), the dedupe strategy for `monthly_recap` misuses the 26-hour `_DAILY_DEDUPE_KINDS` bucket (which would fire daily, not monthly), and the `signed_up_at` column has a simpler existing alternative already in the DB. None of these are blockers for the agent if caught before coding — they are fix-before-implement corrections.

---

## Corrected Anchors

| Item | Plan Says | Reality (file:line + real code) | Action |
|------|-----------|----------------------------------|--------|
| **Change 1 — analytics.html line refs** | "lines 19-21" for the three span elements | Line 19 is `<span class="roi-headline-label">Estimated return</span>` (the label, not the value). The value span is line 20, sub is line 21. Plan says "lines 19-21" collectively — actually correct that the value and sub are at lines 20-21, but the label line (19) is not touched. Acceptable as written; just be precise: edit lines 20–21 only. | Clarify: edit lines 20 and 21 only, not 19. |
| **Change 1 — JS lines 61-62** | `valEl.textContent = 'paid for itself ~' + multiple + 'x'` at line 61, `subEl.textContent = 'estimated ' + rev + ' in booked jobs'` at line 62 | **Confirmed correct.** `templates/analytics.html:61-62` reads exactly as the plan states. | No change needed. |
| **Change 1 — `rev` variable format** | Plan says new valEl will read `rev + ' estimated recovered'` | `rev` at line 60 is already `revenue.toLocaleString(…, {style:'currency',…})` — it yields `"$32,400"` (with the `$` sign built in). The proposed new string `rev + ' estimated recovered'` would read `"$32,400 estimated recovered"` which is fine, but the plan describes it as if `rev` is a bare number. It is not — it is a currency string. Implementor must not double-prepend `$`. | Note in implementation: `rev` already has `$`. New valEl: `rev + ' estimated recovered'` is correct as is. |
| **Change 2a — roi.py `check_roi_milestone()` lines 63-68** | "Append one sentence to the `body` string after the existing estimate copy" at lines 63-68 | `roi.py` has 79 lines total. `check_roi_milestone()` is at line 24. The `body` string is constructed at lines 64-68 (the `f"FirstBack has booked…"` triple). Line 63 is the comment `# Build an honest body`. The plan's "lines 63-68" lands on the right block. **However:** `roi.py` does NOT have a separate `body` variable — it builds the string inline in a single assignment at lines 64-68 and returns it at line 70-75 as part of a dict. To append, the implementor must extend the f-string at line 65-67, not find a standalone `body` variable then `.append()`. | Clarify: the body is built as a single f-string at `roi.py:64-68`. Append a new sentence as a third clause in the f-string or break it into `body = (...) + sentence`. |
| **Change 2b — convos.py `_roi_block()` lines 319-323** | Append `" That's revenue that would have walked without a text-back."` after `"(roi_str}estimate based on {source_label})."` | `convos.py:320-324` — the return statement is at lines 320-324. The closing of the f-string is `)` at line 323. The plan's line range is off by 1: the return is at 320, the parenthetical is inside the f-string on line 323 `"({roi_str}estimate based on {source_label})."`. To append: the sentence must go inside the outer `return (…)` before the closing paren at line 324, or the return must be split. The line numbers are ±1 but the instruction is implementable. **Also note:** the plan has a typo in the f-string reference: `"{roi_str}"` — missing the leading `f`. This is copy-paste, not a real code bug, but the agent should know line 323 reads `f"({roi_str}estimate based on {source_label})."` | Lines ±1 (return at line 320, not 319). Confirm `convos.py:320-324` before editing. Append sentence before closing paren at line 324. |
| **Change 3 — dedupe strategy** | "Add to `_DAILY_DEDUPE_KINDS`" in `alerts.py` — "same 26h dedup window" | `_DAILY_DEDUPE_KINDS` at `alerts.py:38` uses `_DAILY_DEDUPE_SECONDS = 26 * 3600`. A monthly recap added to this bucket would be deduped for only 26 hours — it would re-fire daily. This is **wrong for a monthly send**. Plan 07 uses `alerts.notify(biz, "monthly_roi", ctx)` with a month-stamped dedupe key that collapses across a 26-hour window because the scan itself only runs on day==1; Plan 06's `monthly_recap` runs on days 28-31, so the same month could match 4 days in a row and fire 4 times. The correct approach: add `"monthly_recap"` to `_LONG_DEDUPE_KINDS` (365-day window, keyed by YYYY-MM) OR use a custom window branch in `_dedupe_key` + `notify()` similar to plan 08's `screening_report` (26-day window keyed by month string). | **MUST FIX before implementing.** Do NOT add `"monthly_recap"` to `_DAILY_DEDUPE_KINDS`. Add a 26-day or longer window branch keyed by YYYY-MM. See `alerts.py:38-42` and plan 08's `screening_report` pattern as the model. |
| **Change 3 — `tick_once()` insertion point** | "Add a call after `scan_daily_digest(now)` (line 913)" | `reminders.py:917` is the `scan_daily_digest(now)` call (inside the try block starting at line 916). Line 913 is the comment `"# Phase 6b W2: ONE unified 8am digest…"`. The correct insertion is after the `except` at line 919, before `scan_stall_nudges` at line 922. Line 913 is off; correct insertion point is after line 919. | Insert after line 919 (the except clause of the daily digest try/except), not at 913. |
| **Change 3 — `signed_up_at` column** | "One new column on `businesses`: `signed_up_at TEXT`" | Column does not yet exist in the live DB (confirmed via `PRAGMA table_info`). However, the plan itself notes the simpler alternative: `MIN(users.created_at) WHERE users.business_id=?`. The `users` table at `db.py:220-224` has `created_at TEXT` and `business_id INTEGER`. This query works today with zero migration. The `signed_up_at` column approach is also valid — the migration pattern at `db.py:839-840` is exactly correct to follow. Either is acceptable; the column is not yet present, so no collision exists. | Either path is safe. If `signed_up_at` is added, gate with `"signed_up_at" not in biz_cols` per `db.py:839` pattern. If using `MIN(users.created_at)` instead, no migration needed at all. Document the choice. |
| **Change 4 — `leads` table column status** | `won_at TEXT` and `won_amount REAL` — both new columns via `_migrate()` | Neither column exists in the live DB (`PRAGMA table_info(leads)` confirmed: current columns are `id, name, phone, source, status, urgent, created_at, address, project_type, summary, stage, notes_msgs, business_id, dispatcher_call_last_at`). The migration gate pattern `"won_at" not in lead_cols` is correct — use the `lead_cols = [r[1] for r in c.execute("PRAGMA table_info(leads)").fetchall()]` pattern at `db.py:853` as the model. | Both columns are new. Safe to add with `ALTER TABLE leads ADD COLUMN`. |
| **Change 4 — booking handler line refs** | "two booking handlers" (plan 07 cites lines ~1774 and ~1892) | Confirmed at `app.py:1779` and `app.py:1897` — both call `_roi_mod.check_roi_milestone(biz["id"])` and then `db.set_roi_milestone_sent(biz["id"], …)`. These are the correct two sites for Phase 2's attribution blend. Phase 1 (schema + UI) doesn't touch these. | Confirmed. No correction needed. |

---

## Cross-Plan Ordering

### The Master Plan's instruction (00-MASTER-PLAN.md, item 3):

> "Build 07's milestone refactor FIRST, then 06's won_amount attribution."

### What 07 will change that 06 must NOT duplicate:

Plan 07 Change 3 (`product-review/plans/07-RETENTION-MOAT.md:181-258`) will:
1. Add a `roi_milestones` table (CREATE TABLE) to `db.py` with `(business_id, level, fired_at, revenue)`.
2. Add `db.get_roi_milestones()` and `db.mark_roi_milestone()`.
3. **Refactor `roi.check_roi_milestone()`** to support multi-level (2×, 5×, 10×, 25×), changing the function signature to return a `"level"` key.
4. Update both booking handlers at `app.py:1779` and `app.py:1897` to call `db.mark_roi_milestone()` instead of `db.set_roi_milestone_sent()`.
5. Add `scan_monthly_roi(now=None)` to `reminders.py` with kind `"monthly_roi"`.

**Plan 06's collision surface with 07:**

| 06 Item | What 06 Plans | After 07 Ships | Required Sequencing |
|---------|---------------|----------------|---------------------|
| Change 2a — `roi.py` loss-framing | Append a sentence to the `body` in `check_roi_milestone()` at lines 64-68 | After 07, `check_roi_milestone()` is rewritten with `_milestone_body(level, revenue, avg_source)` — the body is now in a separate helper. Plan 06's line 64-68 patch becomes a wrong-function edit. | 07 ships first. Then 06 patches `_milestone_body()` (the new helper), not the old lines 64-68 inline string. |
| Change 3 — `scan_monthly_recap` | New function in `reminders.py` with kind `"monthly_recap"` | After 07, `scan_monthly_roi(now=None)` with kind `"monthly_roi"` already exists for the 1st-of-month send. The master plan says "ONE monthly digest" — 06's `monthly_recap` (days 28-31) and 07's `monthly_roi` (day 1) are DIFFERENT cadences and are NOT duplicates. However, the master plan says they should consolidate: 07's `monthly_roi` on day 1 is the running-total; 06's day-28 recap is the pre-renewal anti-churn touchpoint. These can coexist as separate sends IF they have distinct dedupe keys and distinct kinds. | Both can ship independently. Coordinate: ensure the two kinds don't share a dedupe bucket. 06's kind `"monthly_recap"` and 07's kind `"monthly_roi"` are distinct — no conflict. |
| Change 4 Phase 2 — analytics blend | `db.analytics()` gets `confirmed_revenue`, `won_n` | 07 doesn't touch `db.analytics()` | No conflict. 06 Phase 2 can land after Phase 1, independent of 07. |

### 08's screening section riding 06's recap:

Master plan item 2: "screening's monthly report (08) rides [06's monthly recap] as a section."

Plan 08 (`08-SCREENING-VALUE.md:182`) already acknowledges this: "The channel should be the **same monthly digest** (the ROI recap). This plan defines the `screening_report` alert kind with the screening numbers; the ROI agent should include it in the monthly digest body OR the screening card should be a second section."

**What this means for the 06 implementation agent:**

1. When building `scan_monthly_recap()` in `reminders.py`, add a `screening_section` key to the context dict (can be `None` if plan 08 hasn't shipped yet).
2. The `format_message("monthly_recap", context)` branch in `alerts.py` must check `context.get("screening_section")` and append it when present — this is how 08's content rides 06's SMS without a second send.
3. Do NOT have 06 call `alerts.notify(biz, "screening_report", …)` — that's plan 08's kind and 08's scan. 06 only owns `"monthly_recap"`.
4. When 08 ships, it populates `screening_section` in the context dict passed by `scan_monthly_recap` — zero change to 06's alert kind, just a richer context.

**Risk if shipped in wrong order:** If 08 ships first and adds `scan_screening_report()` firing independently on the 1st, and then 06 ships a `scan_monthly_recap()` firing on days 28-31, the two sends are on different days (1st vs 28th-31st) and don't conflict. The only bad outcome is if 08's `scan_screening_report` fires on the 1st AND 06's `scan_monthly_recap` also fires on the 1st (because the day range [28,29,30,31] can include the 1st of the NEXT month for businesses in certain timezones). Add a guard: `now_local.day in [28, 29, 30, 31]` (not 1) to prevent overlap.

---

## Migrations

### Columns to add to `businesses`:

| Column | Type | Gate | Already Exists? |
|--------|------|------|-----------------|
| `signed_up_at` | `TEXT` | `"signed_up_at" not in biz_cols` | **No** (confirmed via live DB) |

Migration pattern (from `db.py:839-840`):
```python
biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
if "signed_up_at" not in biz_cols:
    c.execute("ALTER TABLE businesses ADD COLUMN signed_up_at TEXT")
    # Backfill existing rows:
    c.execute("UPDATE businesses SET signed_up_at = datetime('now') WHERE signed_up_at IS NULL")
```

Note: the backfill sets all existing tenants to "now" — an approximation. The plan's suggested alternative (`MIN(users.created_at)` per business) is more accurate. If exact signup date matters for the billing-window calculation, use a per-business subquery in the backfill or skip the column entirely and use the users table query.

### Columns to add to `leads`:

| Column | Type | Gate | Already Exists? |
|--------|------|------|-----------------|
| `won_at` | `TEXT` | `"won_at" not in lead_cols` | **No** |
| `won_amount` | `REAL` | `"won_amount" not in lead_cols` | **No** |

Migration pattern (from `db.py:853-855`):
```python
lead_cols = [r[1] for r in c.execute("PRAGMA table_info(leads)").fetchall()]
if "won_at" not in lead_cols:
    c.execute("ALTER TABLE leads ADD COLUMN won_at TEXT")
if "won_amount" not in lead_cols:
    c.execute("ALTER TABLE leads ADD COLUMN won_amount REAL")
```

Both are safe `ALTER TABLE ADD COLUMN` operations on SQLite. No data loss. Existing rows get `NULL` for both — which is the correct default (not-yet-closed).

**DO NOT** issue a second `lead_cols = [r[1] for r …]` fetch between the two `won_at`/`won_amount` checks — refresh the pragma once before both gates, since the column won't appear in an old pragma snapshot.

---

## Blockers

1. **Dedupe bug (Change 3):** Adding `"monthly_recap"` to `_DAILY_DEDUPE_KINDS` means the 26-hour window is used. On days 28-31, the ticker runs every 60 seconds and will fire a new send every 26 hours until the month ends — potentially 3-4 recaps per month per business. **Must use a month-stamped key with a 26-day+ window instead.** Model after plan 08's `screening_report` pattern:
   ```python
   if kind == "screening_report":  # (plan 08's approach — copy for monthly_recap)
       month = (context.get("month") or "").strip()
       return f"screening_report:{month}"
   ```
   For `monthly_recap`:
   ```python
   if kind == "monthly_recap":
       month = (context.get("month") or "").strip()
       return f"monthly_recap:{month}"
   ```
   And add a custom window branch in `notify()`:
   ```python
   _MONTHLY_DEDUPE_SECONDS = 26 * 24 * 3600  # 26 days
   _MONTHLY_DEDUPE_KINDS = ("monthly_recap",)
   ```
   Or simply add `"monthly_recap"` to `_LONG_DEDUPE_KINDS` with the year-long window (since the month key itself already scopes it correctly).

2. **07's `roi.py` refactor invalidates Change 2a's patch target.** If 07 ships first (as the master plan requires), `check_roi_milestone()` at `roi.py:24-78` will be replaced by a multi-level version that calls `_milestone_body()`. Plan 06's instruction to patch `roi.py:64-68` becomes invalid. The 06 agent must be briefed on the post-07 shape of `roi.py` before editing it.

---

## Notes

1. **Smart-quote risk:** The plan is clean — all code snippets use straight ASCII quotes and double-dashes (`--`). No curly-quote contamination found.

2. **`noteEl` reference (Change 1):** The plan says "The `noteEl` copy stays unchanged." Confirmed: `noteEl` at `analytics.html:48` maps to `#roi-headline-note` at line 23. It is not touched by the headline swap. Correct.

3. **`rev` already has `$` sign (Change 1):** The plan's proposed new `valEl.textContent = rev + ' estimated recovered'` is correct because `rev` (line 60) is a `toLocaleString` currency string that already includes `$`. The implementor should not wrap it in another `$`. The sub line `'paid for itself ~' + multiple + 'x this month'` is also correct — `multiple` is a float, not a currency string, so no `$` is added.

4. **`_roi_block()` convos.py line ref:** The function starts at `convos.py:299`. The return statement spans lines 320-324. The plan cites "lines 319-323" — these are off by 1 (319 is the `roi_str` assignment, the return is 320). The correct append target is the closing of the return f-string paren at line 324. The difference is ±1 line and won't cause failure if the agent reads the file first.

5. **`test_roi_milestone.py` already exists** at `/Users/jonathanmorris/apps/firstback/test_roi_milestone.py` — the plan correctly notes to add tests there. The file exists; don't overwrite it.

6. **`_TOGGLE_COL` for `"monthly_recap"`:** Plan says map to `"alert_on_daily_digest"` — riding the daily digest toggle. This is acceptable (no new DB column required). Confirm that the `alert_on_daily_digest` column exists (it does, confirmed in live DB). This is the correct approach.

7. **Change 4's "update allowed" for a second POST to `/api/leads/<id>/won`:** The plan says "choose update for UX simplicity." This is an intentional design decision. Ensure the endpoint does `UPDATE … SET won_at=?, won_amount=?` (overwrite) not INSERT — no conflict with the schema since `won_at` and `won_amount` are plain columns on `leads`, not a separate table.
