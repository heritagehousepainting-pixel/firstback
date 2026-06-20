# Batch E Slice 1 — Customer Book Audit

**Date:** 2026-06-19
**Auditor:** Claude (read-only)
**Scope:** uncommitted diff — `app.py`, `db.py`, `templates/customer_book.html`, `templates/app_shell.html`, `templates/marketing_base.html`, `templates/onboarding.html`, `templates/resources.html`

---

## Verdict

**SHIP-WITH-FIXES** — one correctness defect (tenant isolation in correlated subqueries) and one honesty issue (lifetime revenue tile is labeled `~$` but not surfaced as "estimated" at the tile label level). Both are small, targeted fixes. All routing, CSS, a11y, and the broader security posture are solid.

---

## Findings

| Severity | File:line | Issue | Fix |
|---|---|---|---|
| **P1** | `db.py:1432–1434` | **Tenant isolation gap in correlated subqueries.** The outer query is correctly scoped to `leads.business_id=?`, but the correlated subqueries `SELECT COUNT(*) FROM appointments a WHERE a.lead_id=l.id ...` join only on `lead_id`. `lead_id` is tenant-scoped in practice (leads can only belong to one business), so this is not currently exploitable — but it relies on referential integrity rather than an explicit filter. If a future data-repair or import creates an `appointments` row with a `lead_id` belonging to a different business, the count will cross tenant lines. The `appointments` table has a `business_id` column (added in migration at db.py:487). Add `AND a.business_id=l.business_id` (or `AND a.business_id=?`) to both correlated subqueries. | Add `AND a.business_id=l.business_id` to both correlated `WHERE` clauses. |
| **P2** | `templates/customer_book.html:19` | **Honesty: lifetime value tile does not label the per-job rate as estimated when it falls back to a trade default.** The tile shows `~$<lifetime_str>` with a `sub` of `Est. ~$<avg_str>/job` — the `~` and `Est.` qualifiers are present on the sub-line. However the tile's primary value (`~$` prefix) could be read as a real figure. This is mitigated by the `~` but is incomplete for a user who hasn't set `avg_job_value`: they see a dollar figure derived from a hard-coded trade default (e.g. $2,000 for "generic") with no indication it is a rough estimate rather than a recorded average. | Add a note (e.g. "(trade estimate)" or an info icon) to the tile `sub` when `avg` comes from the trade default rather than owner-set data. The cleanest approach: pass a second boolean `avg_is_estimated` from the route (True when `business.avg_job_value` is unset/zero) and render "Trade estimate — set yours in Settings" as the sub-line. |
| **P2** | `app.py:603–610` | **Double auth guard is redundant but harmless.** `@login_required` redirects to `/login` before `current_business()` is reached; the explicit `if not biz: return redirect("/login")` is a no-op for a properly authenticated session (login always sets biz). The code is defensive — acceptable — but the comment "if not biz" implies this path can be hit, which may mislead future devs into thinking login succeeds without a business. | No code change required; clarify the comment to "belt-and-suspenders: `@login_required` already gates this, but guard against an edge case where session is set without a business row." |

---

## Verified-good

### 1. Tenant isolation (outer query)
`customer_book_stats` filters `FROM leads l WHERE l.business_id=?` with `(business_id,)` bound. Every row in `rows` is for the requesting business. The stat aggregates (`total`, `repeat`, `total_jobs`, `top`) all derive from these rows only. **Outer isolation: pass.** (See P1 above for correlated subquery caveat.)

### 2. Connection closed
`conn.close()` is called at db.py:1436 before returning. No leak. **Pass.**

### 3. NULL handling
`(r["job_count"] or 0)` used in both `sum()` and `repeat` count. `top` sort uses `-(r["job_count"] or 0)`. **Pass.**

### 4. Repeat = 2+
`>= 2` at db.py:1440. Correct. **Pass.**

### 5. Top sorted desc
`sorted(..., key=lambda r: -(r["job_count"] or 0))[:5]` — descending, top 5. **Pass.**

### 6. Jinja autoescape — names and phones
Flask initialises Jinja2 with autoescape enabled for `.html` templates by default (no `autoescape=False` override in `app.py`). `{{ c.name }}` and the display portion `{{ c.phone|phone }}` are both autoescaped. The `tel:{{ c.phone }}` href attribute is also inside a Jinja `{{ }}` expression in an HTML context, so the value is HTML-attribute-escaped (angle brackets, quotes, ampersands encoded). A phone value like `+15555555` contains no HTML-special chars; a crafted value like `"><script>` would be escaped to `&quot;&gt;&lt;script&gt;` in the attribute. **No XSS vector. Pass.**

### 7. `/customers` reference sweep
Results from `grep -rn 'href="/customers"' templates/ static/`:
- `templates/app_shell.html:51` — intentional; the authenticated nav link. **Correct.**
- `templates/marketing_base.html` — updated to `/resources/customer-stories`. **Pass.**
- `templates/onboarding.html` — updated to `/resources/customer-stories`. **Pass.**
- `templates/resources.html` — updated to `/resources/customer-stories`. **Pass.**
- No remaining stale `/customers` hrefs in marketing templates or static JS.

### 8. Sitemap / robots.txt
No `sitemap.xml` or `robots.txt` exists in the repo (only `onboarding/index.html` has a `noindex` meta tag). No SEO entry to update. **Pass.**

### 9. /resources route collision
`/resources` → `resources()` (app.py:483) returns `resources.html`. `/resources/customer-stories` → `customer_stories()` (app.py:596) returns `customers.html`. Flask matches the more-specific path first. No collision. **Pass.**

### 10. growth._job_value circular import
`growth.py` imports only `db` and stdlib (`re`, `datetime`, etc.). `app.py` imports `growth`. No cycle. **Pass.**

### 11. Macro usage
- `stat_row()` / `stat_tile(value, label, sub, sub_tone)` — signatures match `stat_tile.html`. **Pass.**
- `data_table(columns)` — called with `['Customer','Phone','Jobs','Last job']`. Signature in `data_table.html` is `data_table(columns, zebra=False)`. **Pass.**
- `empty_state(title, message, action_label, action_href)` — signature matches `empty_state.html`. **Pass.**
- `card(title, flush)` — not verified in this diff but consistent with other pages using the same macro.

### 12. CSS classes
All classes used in `customer_book.html` — `.stat-row`, `.stat-tile`, `.stat-value`, `.stat-label`, `.stat-sub`, `.empty`, `.empty-*`, `.dt-strong`, `.dt-muted`, `.tel-link`, `.review-nudge`, `.review-nudge-text`, `.page-sub` — confirmed present in `static/ui.css` and `static/app.css`. **Pass.**

### 13. Em-dash convention
Template uses U+2014 em-dashes inline (`—`). Confirmed consistent with `app_shell.html` and other templates. **Pass.**

### 14. Empty-state path
`{% if stats.total_customers %}` — falsy when 0 (which is the zero-customer case). `{% else %}` renders `empty_state(...)` with an action link to `/pipeline`. **Pass.**

### 15. Nav SVG a11y
New `<svg aria-hidden="true">` + `<span>Customers</span>` — matches the pattern of every other nav item in `app_shell.html`. The `<span>` label will show with the Batch C mobile-label CSS. **Pass.**

### 16. Active nav highlight
`{{ 'active' if path.startswith('/customers') }}` where `path = request.path`. `/customers` starts with `/customers`. **Pass.** (Note: no other authenticated route starts with `/customers`, so no false-positive highlight risk.)

### 17. Honesty — tile `~` prefix and `Est.` sub-line
The `stat_tile` for lifetime value shows `~$<n>` as the primary value and `Est. ~$<avg>/job` as the sub. The `~` on the headline and `Est.` on the sub are both present, so the figure is not presented as exact. **Partially passes** — see P2 finding for the suggestion to distinguish owner-set vs. trade-default estimates more explicitly.
