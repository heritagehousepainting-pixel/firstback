# Plan 07 Audit — Retention Moat Visibility

**Audited:** 2026-06-19  
**Auditor:** read-only red-team pass against current code  
**Plan file:** `product-review/plans/07-RETENTION-MOAT.md`

---

## Verdict

**READY-WITH-FIXES**

The structural approach is sound and the migrations are clean. Two blockers must be resolved before a builder touches code: (1) `scan_monthly_roi` fires a `"monthly_roi"` alert kind that is not registered in `ALERT_KINDS` — it will silently return `[]` and never send; (2) Change 3c's caller-update instruction says to *replace* `db.set_roi_milestone_sent` with `db.mark_roi_milestone` but the back-compat requirement (stated only in the Risk section) says to *also* set `roi_milestone_sent_at` at level=2 — the two instructions directly conflict and a builder following the step-by-step will break back-compat. Additionally one existing test (`test_growth.py:195`) asserts `not _seas_in["sendable"]` which Change 5 breaks, and the monthly scan name (`scan_monthly_roi`) clashes with the master plan's directive to use one `scan_monthly_recap`. All are fixable annotations, not structural reworks.

---

## Corrected anchors

| Item | Plan says | Reality (file:line + real code) | Action |
|------|-----------|--------------------------------|--------|
| `settings_growth_mode` route | "line 1292" (Change 4c intro) | `app.py:1293` (`@app.route`), `app.py:1295` (`def settings_growth_mode`) | Cosmetic; grep, don't hardcode |
| Mode guard to change | "Change line 1299" (Change 4c) | `app.py:1300`: `if mode not in ("off", "tray"):` | Off by one; use the real text match |
| `_handle_tray_reply` | "line 1257" (Change 4b) | `app.py:1258`: `def _handle_tray_reply(biz, cmd):` | Off by one; use text match |
| `growth_tray_release` route | "line 1334" (Change 4b) | `app.py:1335` (`@app.route`), `app.py:1337` (`def growth_tray_release`) | Off by two; use text match |
| `_seasonal_play` | "line 340" (Change 5, inferred) | `growth.py:340`: `def _seasonal_play(business, today, val):` | Exact match |
| `_copy_referral` | "line 184" (Change 6) | `growth.py:184`: `def _copy_referral(first, business):` | Exact match |
| Referral `out.append` | "line 306" (Change 6) | `growth.py:307`: `out.append(_opp("referral", ...` | Off by one; use text match |
| `zip_counts` build | assumed in `plays()` loop | `growth.py:228`: `zip_counts = {}`, populated at `growth.py:250` | Correct — available at referral block |
| Booking handler 1 (milestone check) | "lines ~1774" (Change 3c) | `app.py:1779`: `_milestone = _roi_mod.check_roi_milestone(biz["id"])` | Within tilde range; confirmed |
| Booking handler 2 (milestone check) | "lines ~1892" (Change 3c) | `app.py:1897`: `_milestone = _roi_mod.check_roi_milestone(biz["id"])` | Within tilde range; confirmed |
| `roi.py` existing gate-2 check | "roi.py:42" (Biggest Risk section) | `roi.py:40`: `if biz.get("roi_milestone_sent_at"):` | Off by two; correct logic, cosmetic |
| `scan()` auto mode check | "line 432" (Change 4, TCPA note) | `growth.py:432`: `elif mode == 'auto' and p["kind"] == 'review_request':` | Exact match |
| `GOOGLE_PLACES_API_KEY` in config | "config.py as GOOGLE_PLACES_API_KEY" | `config.py:149`: `GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")` | Confirmed present |
| `/customers` route | "app.py:594" (Change 2b) | `app.py:594–596`: `@app.route("/customers")` / `def customers():` / `return render_template("customers.html")` — **no `@login_required`** | Confirmed public; plan's fix is correct |
| `customers.html` content | "marketing placeholder / customer stories" | `templates/customers.html` extends `marketing_base.html`, titled "Customer stories" | Confirmed |
| `briefing()` in `assistant.py` | plan adds card in `briefing()` | `assistant.py:545–547`: `def briefing(business):` delegates to `_compose_briefing(business)` — add the card in `_compose_briefing`, not `briefing` | Minor: add to `_compose_briefing` at line 454 |
| `db.analytics` signature | `days=None` for all-time (Change 3d) | `db.py:2965`: `def analytics(business_id, days=30):` — `days=None` is accepted per docstring ("None = all time") | Confirmed correct |
| `recent_growth_touch` in `db.py` | referenced in Change 5 route | `db.py:2752`: `def recent_growth_touch(business_id, lead_id, within_days):` | Confirmed present |
| `_job_value` from `growth.py` | used in Change 2 route | `growth.py:75`: `def _job_value(business):` | Confirmed present |

---

## Back-compat

### `roi_milestone_sent_at` at level=2 — **gap in Change 3c instruction**

The plan's stated intent (Risk section, line 258 + Biggest Risk section, line 531) is clear: `roi_milestone_sent_at` must **still be set** when level=2 fires so any code reading that column still sees it.

Current code (`app.py:1786`, `app.py:1905`):
```python
db.set_roi_milestone_sent(biz["id"], datetime.now(_tzu.utc).isoformat())
```

Change 3c says (line 229):
> "call `db.mark_roi_milestone(biz["id"], milestone["level"], milestone["revenue"])` **instead of** `db.set_roi_milestone_sent(biz["id"], ts)`"

The word **"instead of"** will cause a builder to delete the `set_roi_milestone_sent` call. The back-compat requirement to also write the column only appears in the Risk/Biggest Risk prose — not in the step-by-step instruction. **The step-by-step and the risk note directly contradict each other.**

**Required fix before building:** Change 3c must explicitly say: at level=2, call `db.set_roi_milestone_sent` in addition to `db.mark_roi_milestone`. The caller sites should do both:

```python
db.mark_roi_milestone(biz["id"], milestone["level"], milestone["revenue"])
if milestone["level"] == 2:
    db.set_roi_milestone_sent(biz["id"], datetime.now(_tzu.utc).isoformat())
```

### How plan 06 layers on top

After plan 07 ships:
- `roi_milestones` table holds all fired levels; `roi_milestone_sent_at` remains set at level=2.
- Plan 06 adds `won_amount` on leads and `confirmed_revenue` to the analytics surface — purely additive, no milestone table touch needed.
- Plan 06's loss-framing copy change to `check_roi_milestone()` body (plan 06, Change 2a) applies to the level=2 body in `_milestone_body(2, ...)` — plan 07 should ensure `_milestone_body` is the single source of copy so plan 06's body rewrite lands in one place.
- Plan 06's `scan_monthly_recap` is the **carrier** SMS that also carries the lifetime-ROI/review-delta surfaces from plan 07. Plan 07's `scan_monthly_roi` is a **duplicate carrier** (see Blockers).

---

## Migrations

All new. None of the columns or tables listed below exist in the current codebase.

| Migration | Type | Confirmed absent |
|-----------|------|-----------------|
| `businesses.google_review_count INTEGER` | ADD COLUMN | `grep -n "google_review_count" db.py` → no results |
| `businesses.google_star_rating REAL` | ADD COLUMN | same |
| `businesses.review_count_updated_at TEXT` | ADD COLUMN | same |
| `businesses.google_review_count_baseline INTEGER` | ADD COLUMN | same |
| `businesses.google_star_rating_baseline REAL` | ADD COLUMN | same |
| `businesses.growth_streak_count INTEGER DEFAULT 0` | ADD COLUMN | same |
| `businesses.growth_streak_last_at TEXT` | ADD COLUMN | same |
| `businesses.growth_streak_unlocked_at TEXT` | ADD COLUMN | same |
| `roi_milestones` table (id, business_id, level, fired_at, revenue) + UNIQUE(business_id, level) + index | CREATE TABLE IF NOT EXISTS | `grep -n "roi_milestones" db.py` → no results |

Migration style matches existing pattern: `if col not in biz_cols: c.execute(f"ALTER TABLE ...")` for columns; `c.executescript("CREATE TABLE IF NOT EXISTS ...")` for the new table. Safe.

**Note on `config.py`:** Add `STREAK_THRESHOLD = 7` here. Currently absent (`grep "STREAK_THRESHOLD" config.py` → no results). Plan Change 4a specifies this correctly.

---

## Blockers

### BLOCKER 1: `"monthly_roi"` is not a registered alert kind (Change 3d)

`scan_monthly_roi` calls `alerts.notify(biz, "monthly_roi", ctx)`. Current `alerts.py:30–32`:

```python
ALERT_KINDS = ("lead", "booking", "urgent", "canceled", "sms_fail", "forwarding_lost",
               "roi_milestone", "vic_morning", "vic_stall", "screening_graduated",
               "growth_tray", "daily_digest", "tick_stale", "a2p_approved")
```

`"monthly_roi"` is absent. `alerts.notify` hits the guard at `alerts.py:313`:
```python
if kind not in ALERT_KINDS or not isinstance(business, dict):
    return []
```
The monthly scan will appear to run but **send nothing**. `format_message` also has no branch for this kind.

**Resolution options (builder chooses one):**
- (Preferred per master plan) Fold the monthly ROI content into plan 06's `scan_monthly_recap` + `"monthly_recap"` kind instead of creating a parallel scan. The master plan (line 9) mandates: *"One `scan_monthly_recap` on the 1st, one dedupe key, one SMS."* Plan 07's Change 3d is a second monthly SMS — a direct violation.
- (Acceptable if batching is impossible) Register `"monthly_roi"` in `ALERT_KINDS` + `_TOGGLE_COL` + `format_message` in `alerts.py` before wiring the scan. The master plan (collision 1) says all `alerts.py` edits go through Batch D; coordinate accordingly.

### BLOCKER 2: Change 3c back-compat instruction conflicts with Risk section (see Back-compat above)

Change 3c tells the builder to call `db.mark_roi_milestone` **instead of** `db.set_roi_milestone_sent`. The Risk section says to **also** set `roi_milestone_sent_at` at level=2. The step-by-step must be corrected to include both calls at level=2, otherwise back-compat silently breaks.

---

## Notes

### Existing test breakage: `test_growth.py:195`

```python
# test_growth.py:195
check("seasonal play fires for HVAC inside its pre-peak window (March)",
      _seas_in is not None and _seas_in["kind"] == "seasonal" and not _seas_in["sendable"])
```

Change 5 flips `_seasonal_play` to return `sendable=True`. This assertion will fail. The plan says to extend `test_growth.py` with new seasonal tests but does NOT say to update or remove this existing assertion. **Before shipping Change 5, this test must be updated** (the assertion on `sendable` either removed or changed to `_seas_in["sendable"]`).

### `scan_monthly_roi` vs `scan_monthly_recap` name conflict

Plan 07 introduces `scan_monthly_roi`; plan 06 introduces `scan_monthly_recap`. Master plan collision 2 resolves this explicitly: "One `scan_monthly_recap` on the 1st." Plan 07's separate `scan_monthly_roi` violates this. Per master plan Batch E intent, plan 07's monthly briefing content (lifetime running total, review delta) should be a **section of** plan 06's `scan_monthly_recap`, not a standalone second monthly scan. If plan 07 is built first (as specified), plan 07 should stub `scan_monthly_roi` as a temporary carrier OR skip Change 3d entirely and defer to when 06 ships.

### `db.record_growth_go` needs `biz_tz` import in `db.py` (Change 4a)

The plan specifies that `record_growth_go` computes business-local day using `_biz_tz(biz)`. `biz_tz` lives in `config.py:370` and is **not** currently imported by `db.py`. The builder must add `biz_tz` to `db.py`'s `from config import (...)` line (currently `db.py:14–16`). The plan does not call this out.

### `briefing()` vs `_compose_briefing()` (Change 2e)

Plan says to add a customer_book card in `assistant.py`'s `briefing()` function (`assistant.py:545`). `briefing()` is a one-liner that delegates to `_compose_briefing(business)` (`assistant.py:454`). The actual card-building logic lives in `_compose_briefing`. The builder should add the card there, not in `briefing()`.

### Smart-quote risk: none found

The plan's Python code blocks use straight quotes throughout. No curly/smart quote contamination found.

### `analytics.html` "All time" tab already exists

`templates/analytics.html:13` shows an existing "All time" button:
```html
<button type="button" class="btn btn-secondary btn-sm roi-r" data-range="all" aria-pressed="false">All time</button>
```
Change 3e says "Add an 'All time' tab that is the default when all-time revenue > 0 (currently the 30-day view is the default)." The tab exists; the work is changing the default selection and wiring the milestones timeline, not adding a new tab from scratch.

### `recent_growth_touch_kind` — new helper required (Change 5)

Change 5 references `db.recent_growth_touch_kind(business_id, kind, within_days)` — a variant of `db.recent_growth_touch` that filters by kind. This does **not** exist (`db.py:2752` is the current variant, no kind filter). The plan correctly identifies it as a new function to add. No collision.

### `_opp()` seasonal extra kwargs (Change 5 vs Change 6)

Change 5 adds `seasonal_service=service` as a kwarg to `_opp(...)`. `_opp`'s signature is `_opp(kind, lead_id, first, phone, tier, *, title, why, tone, label, money, draft="", sendable=True, compliance="", action=None, tone_risk=False, blocked_reason=None)` (`growth.py:195–197`). `seasonal_service` is not in the signature — it must be added as `**kwargs` or as an explicit parameter. Plan doesn't spell this out; builder must add it.
