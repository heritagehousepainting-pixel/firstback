# Batch C UI/A11y Audit — Mobile + Dashboard UX
**Date:** 2026-06-19  
**Diff scope:** `static/app.css`, `static/ui.css`, `static/app.js`, `templates/dashboard.html`, `templates/command.html`  
**Plan:** `product-review/plans/04-mobile-ux.md` · **Red-team:** `product-review/plan-audits/04-audit.md`

---

## Verdict

**SHIP-WITH-FIXES** — Two real bugs and one role mismatch must be fixed before this is a11y-clean; none of them break the product for mouse/touch users, but a keyboard user cannot dial a phone number from any interactive row (desktop or mobile), and a screen reader won't know the mobile lead card is pressable. Both fixes are one-liners in `app.js` and `templates/dashboard.html`.

---

## Findings

| Sev | File:line | Issue | Fix |
|-----|-----------|-------|-----|
| **P1** | `static/app.js:334-337` | **Keyboard dial broken on every tel: link inside an interactive row.** The keydown handler on `.dt-row` calls `e.preventDefault()` unconditionally on Enter/Space. When keyboard focus is on the `<a href="tel:…">` child (which IS in the tab order), `preventDefault()` suppresses the browser's default action — for a link, that means it never fires the click that would open the dialer. This regression affects both desktop table cells (Batch C added tel: links there) and mobile lead cards. The handler has no `e.target` guard. | Add `if (e.target !== row) return;` as the first line of the keydown handler, so only Enter/Space on the row itself opens the conversation, not on focusable children. |
| **P1** | `templates/dashboard.html:77-78` | **Mobile lead card uses `role="listitem"` + `aria-pressed` (invalid combo).** The parent container correctly uses `role="list"`, but `listitem` is a structural role (not a widget). Screen readers will not announce it as interactive. Additionally, `app.js` calls `row.setAttribute("aria-pressed", "false/true")` on every `.dt-row` — including these `listitem` divs — which is an invalid ARIA state for `listitem`. The desktop `<tr>` correctly uses `role="button"`. | Change `role="listitem"` → `role="button"` on the lead card div. Remove `role="list"` from the container (a `role="list"` owned by `role="button"` children is semantically wrong anyway; or add a separate `ul`/`ol` wrapper with cards as `li`). Simplest: keep the `<div role="list">` container, change each card to `role="option"` if you want a composite, or just use `role="button"` and drop the list container. |
| **P1** | `static/app.js:304-305` | **`scrollIntoView({ behavior: "smooth" })` is not gated by `prefers-reduced-motion`.** `ui.css` line 207 sets `scroll-behavior: auto !important` in the reduced-motion media query, but that CSS property governs CSS-triggered scrolls only — the JS `scrollIntoView()` API is unaffected. `motion.js` checks `matchMedia("(prefers-reduced-motion: reduce)")` for scroll reveals, but the conversation auto-scroll does not. | Gate the behavior: `const rm = window.matchMedia('(prefers-reduced-motion:reduce)').matches; convoCard.scrollIntoView({ behavior: rm ? 'auto' : 'smooth', block: 'start' });` |
| **P2** | `static/app.css:194` | **Lead card `focus-visible` uses `--accent-ring` (28% opacity semi-transparent orange) rather than solid `--accent`.** The global `[tabindex]:focus-visible` rule in `ui.css:199` uses solid `--accent` with `outline-offset:2px`. The card rule is more specific (class beats attribute selector) and overrides to the weaker style at ≤640px. On a white surface `rgba(234,88,12,.28)` barely clears 3:1 against `--surface`. | Change `.lead-card:focus-visible` outline color from `var(--accent-ring)` to `var(--accent)` to match the site-wide focus style. |
| **P2** | `templates/command.html:150-155` | **`cap-nudge` JS insertion causes CLS on the command page.** `#usageGauge` starts `hidden` (0px height). When the async `/api/usage` fetch returns with `over_daily_cap=true`, `showCapNudge()` inserts a new `div.cap-nudge` before the gauge, pushing the command dock taller and shrinking the convo area. The layout shift is real but low-frequency (only when the daily cap is hit). | Pre-reserve the space with `min-height` on `.command-dock` or a placeholder element, so the nudge fills existing space rather than adding new height. |
| **P2** | `static/app.css:155-156` | **`.nav-item` at ≤900px uses `font-size:.65rem` and raw `padding:6px` (not a token).** The plan's documented exceptions allow `2px / 8px / 16px / .65rem`. The `6px` padding is NOT in that list. | Use `padding:var(--space-1) var(--space-2)` (4px 8px) or document `6px` as an explicit exception in the codebase comment. |

---

## Verified-good

**Responsive / breakpoint correctness**
- Double-render is clean: `.lead-cards` is `display:none` globally (app.css:214) and `.leads-table-wrap` is `display:none` inside `@media(max-width:640px)`. Both elements are in the DOM but only one is visible at any breakpoint. The 640px/641px seam is a clean cutover — no overlap.
- Nav strip at 320–390px: `sidebar-nav` has `flex:1 1 auto; min-width:0; overflow-x:auto; scrollbar-width:none`. Items are `flex:0 0 auto`. 9 items × ~89px ≈ 801px overflow is fully handled by horizontal scroll. `sidebar-brand` and `sidebar-foot` take fixed space; the nav gets whatever remains. No content escapes the viewport.
- `sidebar-logout` tap target is correctly bumped to `44×44px` at ≤900px (app.css:162).
- `btn-sm` 44px bump at ≤640px is in scope; the appointments table has `dt-wrap` with `overflow-x:auto` so the taller buttons don't break layout.

**A11y — what works**
- `.dt-row[data-id]` on desktop `<tr>` rows: `role="button"`, `tabindex=0`, `aria-pressed`, `aria-label` all present and valid. Keyboard Enter/Space handler wired. Focus style: inset 3px accent (using `:focus-visible td`).
- `aria-label="Urgent"` on `.lead-card-dot` is present (dashboard.html:81).
- `aria-hidden="true"` on the chevron SVG (dashboard.html:93). Correct.
- Tel: links have `aria-label="Call {{ l.name }}"` (dashboard.html:84). Correct.
- `onclick="event.stopPropagation()"` on the tel: link correctly prevents the card's click handler from opening a conversation when the user taps the phone number. (Note: this only applies to click, not keyboard — see P1 above.)
- `briefing--clear` all-clear: `role="status"` is server-rendered, no JS injection, no CLS.
- `.chat-error-turn` contrast: `--danger` (#B91C1C dark red) on `--danger-bg` (#FCE9E9 pale pink) passes WCAG AA for `--text-sm` text.

**Tokens**
- All new CSS custom properties used in Batch C (`--danger-bg`, `--danger-ring`, `--danger`, `--accent-ring`, `--accent-bg`, `--border`, `--border-strong`, `--ink`, `--ink-soft`, `--ink-faint`, `--bg`, `--surface`) are defined in `tokens.css` and/or `ui.css`. No undefined variables.
- New Batch C additions in `app.css` and `ui.css` use `var()` tokens for all color and spacing values. The only non-token literals in the Batch C diff are `6px` (nav-item padding — see P2 above) and `16px`/`8px` within `lead-card-chevron` width/height (documented exceptions).
- `color-mix()` and `:has()` selectors degrade cleanly on browsers without support (old browsers render a normal row / uncolored stat tile — no content disappears).

**JS behavior**
- `app.js:241`: `querySelectorAll('.dt-row[data-id]')` correctly collects BOTH table rows and mobile cards. Both get click + keydown handlers. When one is opened, all rows (both sets) get `is-selected` cleared. This is safe because only one set is visible via `display:none`.
- `addErrorTurn()`: creates a proper error card with optional retry function. Retry removes the error card and calls `openLead(row)` again. No memory leaks; the closure on `row` is correct.
- Deep-link `?lead_id=X`: uses `scrollIntoView({ block: 'nearest' })` (no smooth behavior — correctly avoids the reduced-motion issue for the deep-link path; only the card-tap path has the problem).
- Cap-nudge `showCapNudge()`: `gauge.hidden=true` is correct (gauge was already hidden; this is a safe no-op that makes intent explicit). The `innerHTML=html` write for the refill message uses a trusted server-controlled date string — no user-controlled content is injected unsanitized.

**Consistency**
- `.chat-error-turn` visual aligns with `.sim-banner-urgent` and `.provider-note-err` patterns: same `--danger-bg` / `--danger-ring` / `--danger` tricolor, same `border-radius:var(--radius)`, same `font-size:var(--text-sm)`. Consistent.
- `.cap-nudge` and `.briefing--clear` follow the same structural pattern as `.review-nudge` and `.sim-banner` — token-driven fills, border-radius from `--radius`, no arbitrary hex colors.
- No dead CSS class references found in the diff: every class added in `app.css`/`ui.css` is emitted by a template or `app.js`, and every new class in templates has a matching CSS rule.
