# Batch C Backend Audit — Mobile + Dashboard UX

**Audited diff:** `app.py`, `db.py`, `static/app.js`, `templates/dashboard.html`, `templates/command.html`
**Date:** 2026-06-19
**Auditor:** be-audit skill (automated evidence-based pass)
**Tests run:** 29 / 29 passed (`python3 test_batch_c.py`)

---

## Verdict

**SHIP-WITH-FIXES** — one P1 in `app.py:758` (pre-existing crash path made slightly wider by batch C) and one P2 hardening note on the querySelector deep-link. No P0s. The P1 fix is a one-liner already present in sibling routes; it does not block a staging deploy, but should land before prod goes live with real users.

---

## Findings

| Severity | File:line | Issue | Fix |
|----------|-----------|-------|-----|
| **P1** | `app.py:758` | `db.last_lead(biz["id"])` crashes with `TypeError` if `biz` is `None`. `login_required` only checks `session["uid"]`; an orphaned session (user row deleted, business row missing) passes the auth gate but `current_business()` returns `None`. Pre-existing callers (`convos.digest(biz["id"])`, `golive_summary(biz)`) share this exposure — batch C adds one more call in the same unguarded pattern. At low user counts this is dormant, but it will 500 with a stack trace on the first orphaned session. | Guard with `if not biz: return redirect(url_for("login"))` immediately after `biz = current_business()` — the same pattern used in `/api/feed` implicitly (it also crashes on None, so fix both together). Or add a `current_business_required` decorator to replace the per-route guard. |
| **P2** | `static/app.js:345` | Deep-link querySelector string-concatenates `urlLead` without sanitization: `.dt-row[data-id="' + urlLead + '"]`. A crafted `?lead_id=1"] .anything` produces a valid multi-part selector that silently matches a different element. No XSS (querySelector does not parse HTML) and no cross-tenant data leak (the API call uses the matched row's `data-id`, which is server-rendered and tenant-scoped). Worst case: the wrong lead thread opens in the current tenant's scope. Attack requires a user to click a crafted URL while already logged in. | Sanitize before querySelector: `urlLead.replace(/[^0-9]/g, "")` (lead IDs are integers). One line. |

---

## Verified-good

### 1. `db.last_lead` correctness
- **Tenant-scoped:** `WHERE business_id=? ORDER BY id DESC LIMIT 1` — parameterized, scoped. No cross-tenant leak possible. (`db.py:1412–1420`)
- **Connection closes:** `conn.close()` is the last statement in the function, consistent with every other `get_conn()` caller in `db.py`. No connection leak.
- **None-safe:** `return dict(row) if row else None` — returns `None` cleanly when the tenant has no leads.
- **Not N+1:** One query per page load. Called once in `dashboard()`. Acceptable.
- **Index:** No dedicated index on `leads(business_id, id)`. The `business_id` column was added via `ALTER TABLE` (`db.py:484`) and has no index. The `ORDER BY id DESC LIMIT 1` scan terminates early (SQLite short-circuits at the first row of a LIMIT 1), and `id` is the rowid / PRIMARY KEY, so the scan direction is efficient. For current scale (hundreds of leads per tenant), this is fine. Not a P1.

### 2. `_time_ago` correctness
- **Never raises:** Wrapped in `try/except Exception: return None`. (`app.py:742`)
- **Handles tz-aware timestamps:** `datetime.now(then.tzinfo) if then.tzinfo else datetime.now()` — matches awareness of the stored timestamp so subtraction never raises `TypeError`. (`app.py:734`)
- **Handles naive timestamps:** Fallback to `datetime.now()` for naive ISO strings. Correct.
- **Bucket math verified:**
  - `< 3600s` → "just now" ✓
  - `3600–86399s` → `Xh ago` ✓
  - `86400–1209599s (< 14 days)` → `Xd ago` ✓
  - `≥ 1209600s (≥ 14 days)` → `Xw ago` ✓
- **All test vectors pass** (verified: `_time_ago` tests 1–6 in `test_batch_c.py:103–108`).

### 3. `last_lead_name` / `last_lead_ago` template propagation
- Both reach `command.html` correctly: `render_template(..., last_lead_name=(_last["name"] if _last else None), last_lead_ago=...)`. (`app.py:764–765`)
- Template degrades to `None` path cleanly: `{% if last_lead_name %}` block is conditional; the `{% else %}` branch renders "No missed calls yet." (`command.html:65–68`)
- The all-clear block only renders when `briefing.items` is empty (`{% else %}` of the `{% if briefing and briefing.items %}` gate) — correct.

### 4. Security: `tel:` href injection
- **Attribute breakout blocked:** Jinja2 autoescape (enabled by default for `.html` templates in Flask 3.x) encodes `"` as `&#34;`, neutralizing any `" onclick=...` injection in `href="tel:{{ l.phone }}"` and `aria-label="Call {{ l.name }}"`. Verified empirically with project venv.
- **`javascript:` in `tel:` href:** A crafted phone value `javascript:alert(1)` produces `href="tel:javascript:alert(1)"`. Browsers parse the `tel:` scheme as a dial intent; the string after `tel:` is treated as a phone number, NOT evaluated as JavaScript. Not exploitable.
- **`fmt_phone` filter does not sanitize `href`-raw phones:** The `href="tel:{{ l.phone }}"` uses the raw value (no `|phone` filter); `{{ l.phone|phone }}` is only applied to the link text. The raw phone in the href is still HTML-escaped by Jinja autoescaping. `fmt_phone` falling back to `str(raw or "")` for non-10-digit values does NOT create a new injection path because autoescape blocks attribute breakout.
- **`aria-label="Call {{ l.name }}"` (user-controlled):** Autoescaping encodes `"` → no attribute breakout. `l.name` originates from Twilio caller-ID (over which the caller has limited control) or CSV import (user-controlled format but same escaping applies). Not exploitable.
- **Verdict:** No XSS or attribute-injection risk from the new `tel:` links. The `javascript:` in `tel:` combination is a browser-spec-level non-issue.

### 5. Mobile card DOM duplication — no data leak
- The same tenant-scoped `leads` list (returned by `db.leads_with_stage(biz["id"])`) is iterated twice: once for the desktop `<table>` and once for the mobile `.lead-cards` block. (`dashboard.html:54–97`)
- No separate query, no additional join, no cross-tenant data introduced. Both blocks show identical lead data to the same authenticated user.
- The desktop table is hidden on mobile via CSS (`.leads-table-wrap`), and the mobile cards are hidden on desktop. The duplicated markup is a layout concern, not a data concern.

### 6. `addErrorTurn` + `openLead` retry closure
- **Closure captures `row` correctly:** `row` is the parameter of `async function openLead(row)`. The closure `function () { openLead(row); }` captures this parameter, not a mutable loop variable. Each call to `openLead()` binds its own `row`. No stale-variable bug. (`app.js:326–327`)
- **No listener leak:** The retry button's `click` listener calls `openLead(row)` which does NOT add new event listeners. All `click`/`keydown` listeners on `.dt-row` elements are registered exactly once in the `rows.forEach()` loop at `app.js:331–340`. Re-invoking `openLead()` from retry does not re-register anything.
- **Error card removes itself before retry:** `el.remove()` fires before `retryFn()`, so no duplicate error cards accumulate on repeated retries. (`app.js:112`)

### 7. `querySelector` deep-link — scope of risk
- `urlLead = new URLSearchParams(location.search).get("lead_id")` is attacker-controlled (any user can craft a URL). The string-interpolated selector at `app.js:345` can be malformed or multi-part. However:
  - A `SyntaxError` thrown by `querySelector` for an invalid selector results in `autoRow = null`, and the `if (autoRow)` guard prevents `openLead()` from firing. Safe failure path.
  - A crafted selector that DOES match would open a `.dt-row` element whose `data-id` is server-rendered and tenant-scoped. The API call at `/api/leads/<id>/messages` enforces ownership on the server. No cross-tenant leak.
  - Logged as **P2** for hygiene; not a security hole.

### 8. `assistant.js` scope-trim: verdict CORRECT
- The builder's claim: "assistant.js error paths already use `addTurn()` (visible chat bubble), unlike app.js's `addMeta` (faint timestamp). Leaving it unchanged is correct."
- **Verified:** All catch/error paths in `assistant.js` call `addTurn("agent", msg)` — at lines 553, 620, 675. Zero calls to `addMeta`. Zero calls to `addErrorTurn`.
- `addTurn()` renders a full agent chat bubble in the Vic transcript, which is the correct surface for assistant errors. `addErrorTurn()` is designed for the pipeline/lead-list chat pane (`app.js`), a different context with different visual hierarchy.
- **Leaving `assistant.js` unchanged is the right call.** Applying `addErrorTurn` there would be wrong — it would render a floating error card inside a transcript designed for bubbles.
