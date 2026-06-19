# 04-audit — Mobile + Dashboard UX Plan Red-Team
**Audited:** 2026-06-19  
**Plan:** product-review/plans/04-mobile-ux.md  
**Auditor:** READ-ONLY — no source files edited.

---

## Verdict

**READY-WITH-FIXES**

Most line-number anchors are stale (the file grew since the plan was written), two symbol claims are wrong (`assistant.css` vs `ui.css` for `.chat-meta`; the `/command` route is actually `/dashboard`), and the `assistant.js` catch-block target does not exist in the form the plan describes. All are correctable without rework — the logic of every change is sound; only the addresses and one file name need correcting before an implementer picks this up.

---

## Corrected anchors

| Change # | Plan says | Reality (current file:line + actual code) | Action |
|---|---|---|---|
| **1** | `app.css line 150: .nav-item span{display:none}` | `static/app.css:150` — CORRECT. The rule is exactly there: `.nav-item span{display:none}` inside the `@media(max-width:900px)` block at line 145. | No change needed — anchor valid. |
| **1** | Replace the entire `@media(max-width:900px)` block | Current block: lines 145-155. The plan's replacement CSS is additive/safe but the implementer must be aware the current block does NOT include `.sidebar-logout{width:44px;height:44px}`. Plan already calls this out. | Note only. |
| **2** | `dashboard.html line 56: <td class="dt-muted">{{ l.phone\|phone }}</td>` | `templates/dashboard.html:57` — STALE LINE. Real line is 57. Code is verbatim correct. | Update to line 57. |
| **2** | "Immediately after the closing `{% endcall %}` of the Leads card (after line 71), add a `<div>` block" | `templates/dashboard.html:71` — the `{% endcall %}` closing the Leads card is at line **71** (correct), but the outer card ends at line 82 (closing `{% endcall %}` of the `dash-2col` div is at 82). The `.lead-cards` div must go AFTER line 71 but BEFORE the Conversation card at line 73, still inside the `dash-2col` div — plan step 4 (leads-table-wrap) makes the insertion point clear enough. | Confirm insertion is between lines 71 and 73. |
| **2** | `pill` macro "is already imported at the top of dashboard.html" | `templates/dashboard.html:6` — `{% from "components/ui/badge.html" import pill %}` — CORRECT. `pill` is imported. | No change needed. |
| **2** | `empty_state` macro used in the card list | `templates/dashboard.html:7` — `{% from "components/ui/empty_state.html" import empty_state %}` — CORRECT. | No change needed. |
| **2** | `.dt-row[data-id]` JS selector | `static/app.js:217` — `const rows = document.querySelectorAll(".dt-row[data-id]")` — selector is collected at IIFE entry, not re-queried on interaction. Adding `.lead-card.dt-row` elements **after** the IIFE runs means they will NOT be picked up by the initial `querySelectorAll`. | **BLOCKER — see Blockers section.** |
| **3** | `ui.css line 43: .btn-sm is height:32px` | `static/ui.css:43` — CORRECT. `.btn-sm{height:32px;padding:0 var(--space-3);font-size:var(--text-xs)}` | No change needed. |
| **4** | `dashboard.html line 89: <td class="dt-muted">{{ a.lead_phone\|phone }}</td>` | `templates/dashboard.html:89-91` — STALE. The appointments table row is a single condensed `<tr>` spanning lines 89-91 in the current file. The phone cell is part of the same line 89: `<td class="dt-muted">{{ a.lead_phone\|phone }}</td>` embedded inline. Code matches — only formatting differs. | Verify the substitution on line 89 in context. |
| **4** | `dashboard.html line 119: <td class="dt-strong">{{ c.from_number\|phone }}` | `templates/dashboard.html:119` — STALE. The screened-calls table row phone is at line 119 but the code is: `<td class="dt-strong">{{ c.from_number\|phone }}{% if c.times and c.times > 1 %} <span class="dt-muted">· {{ c.times }} calls</span>{% endif %}</td>` — the plan's BEFORE snippet is INCOMPLETE (omits the conditional). The AFTER must preserve `{% if c.times ...%}` inline. | Expand the AFTER snippet to keep the times-conditional. |
| **5** | `app.js line 295 (openLead catch): addMeta(convo, "Could not load this lead. ...")` | `static/app.js:296` — STALE LINE. The catch body at line 296 is: `addMeta(convo, "Could not load this lead. " + err.message);` — code matches, line is off by one. | Update to line 296. |
| **5** | `addErrorTurn` added "after `addMeta()` (after line 95)" | `static/app.js:89-95` — `addMeta` ends at line 95. Insert after line 95 is correct. | No change needed. |
| **5** | `assistant.js` send catch: "Locate the existing error append (pattern: `addMeta(convo, "Could not send...")`)" | `static/assistant.js` — NO such call exists. `assistant.js` does NOT use `addMeta` at all. Its error path in `postSubmit` (line 615-622) calls `addTurn("agent", msg)` to show a chat bubble. The streaming catch (line 675) calls `addTurn("agent", "The connection dropped mid-reply. Try again.")`. There is NO `addMeta` error call in `assistant.js`. | **BLOCKER — see Blockers section.** |
| **5** | `app_shell.html lines 96-97` load `app.js` then `assistant.js` | `templates/app_shell.html:96` — only `app.js` loads in `app_shell.html`. `assistant.js` is loaded at `templates/command.html:154` via a `<script defer src="/static/assistant.js?v=18">` inside the page-specific `{% block content %}`. Line 97 in `app_shell.html` is `motion.js`, not `assistant.js`. | Update the note: `assistant.js` is a `command.html`-only deferred script, not an `app_shell.html` script. `addErrorTurn` defined in `app.js` IS available when `assistant.js` runs on that page. |
| **5** | "CSS for the error card: add to `static/ui.css` (after the `.chat-meta` rule, around line 151)" | `static/ui.css:149-150` — `.chat-meta` rule starts at line 149. Insertion would be after line 151. Location is correct. | Minor: line 149 not 151 is where `.chat-meta` begins; insert AFTER the closing brace at line 151. |
| **5** | Plan says `.chat-meta` lives in `assistant.css` (wording: "CSS for the clear state (in `assistant.css` or the inline style block in command.html)") — Change 6 section | `static/ui.css:149` — `.chat-meta` is in `ui.css`, NOT in `assistant.css`. The Change 5 section correctly says `ui.css`; Change 6 erroneously mentions `assistant.css`. | In Change 6, use `ui.css` (or command.html's inline `<style>` block — both work). |
| **6** | `command.html line 37: {% if briefing and briefing['items'] %}` | `templates/command.html:37` — CORRECT. Line 37 is exactly `{% if briefing and briefing['items'] %}`. | No change needed. |
| **6** | Template context variables `last_lead_name` and `last_lead_ago` | `app.py:738-742` — the `/dashboard` route (which renders `command.html`) passes: `hello`, `briefing`, `feed_sig`, `digest`, `golive`, `suggestions`. Neither `last_lead_name` nor `last_lead_ago` is passed. The plan correctly flags this as a "backend requirement" but the implementer must know the route function is `dashboard()` at line 727, NOT a `/command` route — there is no `/command` route in `app.py`. | Add `last_lead_name` / `last_lead_ago` to `dashboard()` at `app.py:738`. |
| **7** | `dashboard.html line 31: stat_tile for Urgent` | `templates/dashboard.html:31` — CORRECT. Line 31: `{{ stat_tile(urgent_count, 'Urgent', sub=('Needs follow-up' if urgent_count else 'All clear'), sub_tone=('bad' if urgent_count else 'good')) }}` | No change needed. |
| **7** | `dashboard.html line 55: <tr class="dt-row" data-id="{{ l.id }}"` | `templates/dashboard.html:55` — CORRECT. Exact match at line 55. | No change needed. |
| **8B** | "In `app.js`, at the end of the Dashboard conversation viewer IIFE" | `static/app.js:215-310` — IIFE ends at line 310. The `urlLead` auto-open snippet can be appended before the closing `})();` at line 310. But note: `rows` is collected at line 217 (`querySelectorAll(".dt-row[data-id]")`). The auto-open code references `autoRow = document.querySelector('.dt-row[data-id="' + urlLead + '"]')` — this runs after the IIFE initializes, so it will find the table rows. However if Change 2 card-list rows are also `.dt-row[data-id]`, the first match could be a card rather than the table row (which may have no visible effect on desktop but note the ordering). | No blocker but order-dependency with Change 2 if implemented together. |

---

## Blockers

### B1 — Change 2: `querySelectorAll` collected before card-list elements exist

The dashboard IIFE in `app.js` (lines 216-310) collects:
```js
const rows = document.querySelectorAll(".dt-row[data-id]");
```
at IIFE entry (line 217), which is synchronous at page load. The plan's card-list `<div class="lead-card dt-row" data-id="...">` elements are **in the same HTML document** (rendered server-side, not injected later), so they **will** be collected — but only if the `<div class="lead-cards">` block is placed **before** the script runs, which it will be since `app.js` loads at the bottom of `app_shell.html` (line 96). This is safe.

However, `openLead()` calls `row.classList.remove("is-selected")` and `row.classList.add("is-selected")` on ALL matched rows including card-list divs. The CSS `.dt tbody tr.is-selected` only applies inside a `<tbody>`, so the `.is-selected` will be added to the card div but the CSS rule won't fire. The plan adds a separate `.lead-card.is-selected{background:var(--accent-bg)}` rule — confirm that rule is included in the Change 2 CSS block. It IS listed in the plan's CSS at `@media(max-width:640px){ .lead-card.is-selected{...} }` — but **the `.is-selected` visual is media-query-gated to <=640px**. On desktop, clicking a table row also toggles `.is-selected` on the corresponding card-list div (which is `display:none` on desktop), harmless but worth noting.

**Real blocker:** the plan's Change 2 Step 5 says to add a mobile auto-scroll inside `openLead()` after `convo.innerHTML = ""`. The current `openLead` sets `convo.innerHTML = ""` at **line 276** (after the `convo.innerHTML = ""` line for the notes element). There is no `convo.innerHTML = ""` line — the actual reset is `convo.innerHTML = ""` is NOT in the current openLead; instead the function just appends to `convo`. The actual sequence: `convo` is cleared implicitly by `addMeta` / `addBubble` using `clearEmpty`. **The plan's anchor "add after `convo.innerHTML = ""`" does not exist in the current `openLead` body** (lines 265-298). The implementer should add the scroll snippet after `openLeadId = row.dataset.id` (line 272) or after the initial empty/clear setup.

### B2 — Change 5: `assistant.js` has NO `addMeta` error call to replace

The plan says: "Locate the existing error append (pattern: `addMeta(convo, "Could not send...")`)" in `assistant.js`. This call does not exist. `assistant.js` uses `addTurn("agent", msg)` for errors (lines 618-621, 675). The `convo` variable does not exist in `assistant.js` — the chat container there is `transcript` (line 182). There is nothing to "replace" — the implementer must instead decide whether to:
1. Leave `assistant.js` error bubbles as `addTurn` (they are already styled as agent chat turns — arguably correct behavior)
2. Add a new dedicated error renderer inside `assistant.js` using the same `addErrorTurn` pattern, calling it from `postSubmit` catch (line 615) and `streamSubmit` catch (line 673)

If option 2: the function `addErrorTurn` will be available globally (declared in `app.js` which loads before `assistant.js` on the `/dashboard`→`command.html` page), but `addErrorTurn` takes a `container` parameter — the implementer must pass `transcript` (not `convo`) as the container.

### B3 — The `/command` route does not exist

The plan references "the `/command` route handler" and "the `/command` route context". There is no `/command` route. The page is served by `@app.route("/dashboard")` → `dashboard()` at `app.py:727`. The template `command.html` is rendered from there. All backend changes for Change 6 (`last_lead_name`, `last_lead_ago`) go into the `dashboard()` function at `app.py:738`.

---

## Notes

1. **`assistant.css` vs `ui.css`:** The plan mentions "`assistant.css` or the inline style block" in Change 6. `assistant.css` is a real file (`/static/assistant.css?v=13`, loaded from `command.html:6`), but `.chat-meta` lives in `ui.css:149`, not `assistant.css`. New CSS for `.briefing--clear`, `.chat-error-turn`, and `.cap-nudge` should go into `ui.css` (globally available) or `command.html`'s inline `<style>` block (lines 109-115). Do NOT add to `assistant.css` unless you verify it's the right scope — `assistant.css` is scoped to the command center only and is the correct place for command-center-only styles.

2. **Smart-quote / ASCII risk:** The plan's CSS and JS code blocks use straight ASCII quotes throughout. The `&mdash;` entity in Change 5 and Change 6 is HTML-safe in Jinja. No risk identified.

3. **`stat_tile` macro signature (Change 7 CSS-only approach):** The plan chooses a CSS `:has()` approach over a template param change — this is correct and safe. The `.stat-sub.bad` class is already set by `sub_tone=('bad' if urgent_count else 'good')` at `dashboard.html:31`. Confirmed.

4. **Change 8A (`app_shell.html` active-state note):** The plan says the active-state currently uses `path.startswith('/dashboard')` for the "Command" nav item. `app_shell.html:35` confirms: `{{ 'active' if path.startswith('/dashboard') }}`. The `/command` nav item check is moot because the route is `/dashboard`. If Option A (merge) is implemented, no active-state change is needed — `/dashboard` already activates the Command item.

5. **Nav labels (Change 1):** The current nav labels in `app_shell.html` are: "Go Live", "Command", "Pipeline", "Memory", "Callers", "ROI", "Demo", "Settings". The plan's verify step lists: "Command", "Pipeline", "Memory", "Callers", "ROI", "Demo", "Settings" — correct, though "Go Live" is also present until `golive_complete`. The CSS change to remove `.nav-item span{display:none}` and add `flex-direction:column` will expose ALL spans including "Go Live".

6. **Change 2 ordered dependency:** The plan's Ordered Change List runs Change 1 first, then Change 3, 4, 7, 5, 6, 8B, then Change 2 last. This is correct — Change 2 is the most complex and depends on the `.tel-link` CSS from Change 2 Step 1, which also appears independently in Change 4. If implementing in order, the `.tel-link` rule only needs to be added once (with Change 2 or Change 4, whichever ships first).

7. **`convo.innerHTML = ""` anchor (Change 2, Step 5):** As noted in B1, `openLead()` does NOT have a `convo.innerHTML = ""` line. The plan's insertion point "after `convo.innerHTML = ""`" is invalid. Best insertion point: after `openLeadId = row.dataset.id` at `app.js:272`, before the notes/convo operations begin.

8. **`data_table` macro and the `leads-table-wrap` div (Change 2, Step 4):** The plan says to wrap the `{% call data_table(...) %}` block in a `<div class="leads-table-wrap">`. Current `dashboard.html:53` is `{% call data_table(['Customer','Phone','Stage','Received']) %}`. The `data_table` macro renders its own `.dt-wrap` div (confirmed in `ui.css:115`). So the structure becomes: `.leads-table-wrap > .dt-wrap > table.dt`. The CSS `.leads-table-wrap{display:none}` at <=640px correctly hides both the wrapper and the inner `.dt-wrap`. No conflict.
