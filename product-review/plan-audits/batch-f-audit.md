# Batch F Audit — Pricing / Marketing / SEO (Plan 09, non-decision parts)

Auditor: Claude (read-only, uncommitted diff only)
Date: 2026-06-19
Test suite: test_batch_f.py — **18/18 passed, 0 failed**

---

## Verdict

**SHIP**

All template-integrity, gate, honesty, and nav checks pass. The one pre-existing JS curly-apostrophe in `onboarding.html` is untouched by this diff and carries no new risk. The `onboarding.html` webinar link is likewise pre-existing and out of scope. No new paid flows, no overclaimed figures, no broken block nesting.

---

## Findings

| Severity | File:line | Issue | Fix |
|----------|-----------|-------|-----|
| P2 | `onboarding.html:64` | `/webinars` link survives in the **homepage** nav — the de-link was applied to `marketing_base.html` only. `onboarding.html` has its own duplicate nav (it doesn't extend the base). Pre-existing, **not introduced by this diff**, but the de-link is incomplete: a visitor on `/` still sees Webinars in the Resources dropdown. | De-link the same nav item in `onboarding.html` lines 62-65 to match `marketing_base.html`. Remove the `<a class="ob-dditem" href="/webinars" ...>` element from onboarding's nav. |
| P2 | `pricing.html:7` / `customers.html:4` / `solutions.html:29` | Per-page `{% block meta %}` overrides do not include `og:type` or `og:site_name` (those are in the base default, but the override replaces the whole block). Pages will render without `og:type=website` and `og:site_name=FirstBack`. | Add `<meta property="og:type" content="website">` and `<meta property="og:site_name" content="FirstBack">` to each override block, or use Jinja2 `{{ super() }}` + `{% block meta %}` partials. Minor SEO gap, not a user-visible bug. |
| P2 | `onboarding.html:184` | Curly apostrophe (`'`) inside a JS string literal: `sub: 'When you can’t pick up...'`. This is a pre-existing issue, **not introduced by this diff**. The curly quote is inside a single-quoted JS string — it is not a JS syntax error (single-quote strings allow curly apostrophes), but it is a code-style inconsistency that could bite if the string is ever moved or manipulated. | Replace `can’t` with `can't` (ASCII apostrophe). Low urgency. |

*No P0 findings.*

---

## Verified-good

**Template / Jinja integrity**
- `marketing_base.html`: `{% block meta %}...{% endblock %}` is correctly placed after `{% block title %}` (lines 6 vs 9). Block is balanced.
- All four child templates (`pricing.html`, `solutions.html`, `customers.html`, `landing.html`): each opens `{% block title %}`, then `{% block meta %}`, then `{% block content %}` — matching the parent declaration order. All three blocks are closed: 3 opens / 3 endblocks each.
- `onboarding.html` does not extend `marketing_base.html`; its SEO meta is injected as raw `<head>` tags (lines 9–16). Correct approach — no stray `{% block %}` syntax present, no orphan tags.
- No duplicate `<meta name="description">` detected on any rendered page (override pattern is correct).

**Smart-quote / ASCII safety**
- No curly quotes appear inside any `{% ... %}` or `{{ ... }}` Jinja expression across all modified files.
- No curly quotes appear inside any HTML attribute values (`content=`, `href=`, `alt=`, etc.) across all modified files.
- Curly quotes in **display text** (blockquotes, paragraph copy, em-dashes in prose) are consistent with the pre-existing file style — Jinja renders these as-is with no parsing risk.
- The one flagged JS curly apostrophe (`onboarding.html:184`) is pre-existing and not a syntax error in its JS context.

**CSS variable availability**
- `var(--accent)` is defined in `ui.css:16` (`#EA580C`) and is loaded on all marketing pages via `marketing_base.html` → `<link href="/static/ui.css">`.
- `var(--ink-soft)` is defined in `tokens.css:11` (`#4A5160`), which is imported by `ui.css:11` via `@import url("tokens.css")`. Available on all pages.
- `var(--accent-strong)` is defined in `ui.css:19` (`#C2410C`). Available.
- No undefined CSS variables introduced by this diff.

**ROI strip rendering (pricing.html)**
- Strip uses `display:flex; flex-wrap:wrap` — stacks on mobile. Confirmed.
- Colors: `$45K+` and `$300–$800` render in `var(--ink)` (dark on light `--bg:#FAFAF8`). `$99/mo` uses `var(--accent)` (Safety Orange). All legible on the light marketing background.
- Footnote (`pricing.html:41`) explicitly cites the math: "5-10 calls/week at an average job value of $300-$1,500." No fabricated external study claimed. The $45K figure is derivable from stated assumptions (e.g., ~30% conversion at mid-range job value).

**Honesty — no overclaim**
- `$45K+`: backed by the inline footnote citing contractor-specific assumptions. Not presented as a study result.
- Overage FAQ (`pricing.html:99`): soft "we'll let you know so you can upgrade — we don't cut you off mid-month without warning." No unbuilt `$0.75/message` promise. Confirmed clean.
- AI voice callback: correctly flagged "coming soon" / "beta -- not yet available" in both the plan tier list and the FAQ. Not presented as live.
- Pro add-on (`$20/mo`): routes to `/contact`, not a self-serve checkout. No billing flow wired. Confirmed.

**Gate — pricing stays coming-soon**
- No links to `/billing/checkout`, `/pay`, `/subscribe`, or any Stripe URL appear in any modified template. All pricing CTAs go to `/signup` (Get started) or `/contact` (Talk to sales / Crew plan). Gate intact.

**Nav integrity**
- `/webinars` de-linked from `marketing_base.html` nav (the shared nav for all marketing pages). Route `/webinars` still exists in `app.py:628` and is reachable by direct URL. Correct per plan.
- `/resources/customer-stories` remains linked in nav. Customers page now has a functional waitlist card, making the link non-dead-end. Correct.
- No other nav links removed or added.

**Customers page**
- Dead `"Your first customer's quote goes here"` placeholder replaced by an honest waitlist capture card with clear offer ("free month on us") and CTA to `/contact?subject=case-study`.
- Remaining two placeholder cards use honest copy ("Real results... will land here," "This space is waiting for yours") — consistent with pre-existing page tone.
- No invented testimonials.
