# Audit 09 — Pricing + Marketing + SEO
**Plan:** `product-review/plans/09-PRICING-MARKETING.md`
**Auditor:** Read-only red-team pass
**Date:** 2026-06-19

---

## Verdict

**READY-WITH-FIXES**

Seven of the ten changes are BUILD-NOW with accurate anchors. Three require fixes before building: (1) the annual toggle CTA wiring has a gap — `/signup` does not accept `plan`/`interval` query params, so the toggle would be cosmetic without a separate `POST` form or route change; (2) `og-default.png` does not exist in `/static/` yet (would 404 silently on social shares); (3) the Pro tier card does not explicitly list a phone number count, so Change 8's add-on surface needs a minor structural correction. All other anchors match reality.

---

## BUILD-NOW vs NEEDS-OWNER

| # | Change | Classification | Reason |
|---|--------|---------------|--------|
| 1 | Annual toggle + "2 months free" default | BUILD-NOW (copy/CSS) + **WIRE-BEFORE-SHIP** (CTA URL) | Toggle HTML/CSS is buildable now. CTA links need a backend-compatible form POST to `/billing/checkout` or `/signup` must accept `?plan=&interval=` GET params — see Gate Check |
| 2 | ROI anchor above pricing grid | BUILD-NOW | Pure HTML/copy insert between `</header>` (line 11) and `<section>` (line 13) |
| 3 | 30-day money-back guarantee badge | **NEEDS-OWNER** | Real financial commitment; plan explicitly requires founder confirmation before shipping |
| 4 | Rename "conversations" → "missed-call replies" | BUILD-NOW | Three text-only replacements at pricing.html:23, 37, 53 |
| 5 | SEO/OG meta block in all 5 templates | BUILD-NOW (with blocker on og-default.png) | `{% block meta %}` slot is absent from all templates; copy is ready. Must create `og-default.png` first |
| 6 | Hero copy reframe ("most tools text back…") | **NEEDS-OWNER** | Vic morning briefing is login-gated to `/dashboard`; it is NOT a publicly available feature for all users. Briefing reference in hero copy would overclaim unless hedged |
| 7a | Heritage dogfood testimonial slot (Phase 1) | **NEEDS-OWNER** | Requires founder-provided real quote; cannot be fabricated |
| 7b | "Be first case study" waitlist cards (Phase 2) | BUILD-NOW | Replaces placeholder blockquotes in customers.html:13–23 |
| 7c | Landing page testimonial slot (Phase 3) | BUILD-NOW (deferred) | Comment placeholder at landing.html:105 is the correct hook; ready when quote obtained |
| 7d | Nav dead-end: remove "// proof" + links (Phase 4) | BUILD-NOW | marketing_base.html:51–53; comment out two `ob-dditem` entries and the `ob-ddlabel` |
| 8 | $20/mo extra-number add-on on Pro | BUILD-NOW | Pro card at pricing.html:29–44 lacks explicit phone-number line; add the number line + add-on mention; route to `/contact` (not self-serve checkout) |
| 9 | Soft-overage FAQ update (soft version) | BUILD-NOW | FAQ entry at pricing.html:67; use the "we'll alert you" version since no 80%-alert hook exists in billing.py yet |
| 10 | /customers nav dead-end de-link | BUILD-NOW | Same as 7d; covered there |

---

## Corrected Anchors

| Item | Plan says | Reality (file:line + real code) | Action |
|------|-----------|--------------------------------|--------|
| C1: ROI anchor insert point | "Insert between `<header class='mk-head'>` and `<section class='mk-section'>`" | Correct. `</header>` closes at pricing.html:11; `<section class="mk-section">` opens at pricing.html:13. Blank line 12 is the exact insertion point. | No correction needed |
| C1: Annual toggle — price display | Plan shows $79/$159/$319/mo for annual tiers | pricing.html:18 `$950/year`, line 33 `$1,910/year`, line 49 `$3,830/year`. Annual effective monthly: $79.17 / $159.17 / $319.17. Plan rounds to $79/$159/$319 — correct. | No correction needed |
| C1: Annual CTA wiring | "CTA href should pass `?interval=annual`" to `/signup` | `/signup` route (app.py:302–349) reads only `email`, `password`, `business`, `owner`, `phone`, `trade`, `has_ein` from POST form. **It does not read `plan` or `interval`.** The live checkout path is `/billing/checkout` (POST, auth-gated, app.py:2934) which DOES read `plan` and `interval` from the form. `/signup` redirects to `/setup` after account creation, not to checkout. The `?interval=annual` query param would be silently ignored. | **Fix required:** Either (a) pass `plan`/`interval` as hidden fields in a `POST /billing/checkout` form from the pricing page (requires the user to already be logged in), or (b) store the desired `interval` in the session during signup and read it from setup/onboarding. Do not imply the toggle wires through `/signup?interval=annual`. |
| C2: ROI anchor section tag | `<section class="mk-section mk-roi-anchor" style="padding-top:0;padding-bottom:0">` | Valid HTML; the class `mk-roi-anchor` does not yet exist in marketing.css so a new CSS rule is needed to style the strip. | Add `.mk-roi-anchor` styles — not a blocker, can use inline style as the plan shows |
| C4: "conversations" rename targets | 3 hits: 250, 1,000, 3,000 `/mo` | Confirmed at pricing.html:23, 37, 53. `grep "conversations" pricing.html` returns exactly these 3 lines plus nothing else consumer-facing. | No correction needed |
| C5: meta block location | "Add `{% block meta %}` slot after `<meta name='viewport'>`" | marketing_base.html:5 is `<meta name="viewport">`, line 6 is `<title>`. No `{% block meta %}` exists anywhere in any of the 5 target templates. Zero OG tags, zero description tags confirmed. | Insert `{% block meta %}{% endblock %}` between lines 5 and 6 of marketing_base.html |
| C5: title doubled issue | "Current pattern: `{% block title %}{{ app_name }}{% endblock %} · {{ app_name }}`" | Confirmed at marketing_base.html:6: `<title>{% block title %}{{ app_name }}{% endblock %} · {{ app_name }}</title>`. landing.html overrides correctly (line 2). pricing.html:5 overrides with `Pricing` → renders as "Pricing · FirstBack" (fine). solutions.html:27 overrides with `Solutions` → fine. customers.html:2 overrides with `Customer stories` → fine. Only pages that don't override default to "FirstBack · FirstBack". | Check company.html, product.html, guides.html, help.html, blog.html for missing `{% block title %}` overrides — out of scope for this plan but flag as follow-on |
| C5: og-default.png | "Verify it exists at that path before shipping" | `ls /static/og*` → **no matches**. The file is referenced in microsite.html:10 but does NOT exist on disk. | **Blocker before SEO changes ship:** Generate og-default.png (1200×630) before adding the `og:image` tag. A missing image 404s silently on social shares — no error shown, just no image on cards. |
| C6: Vic morning briefing | Plan: "morning briefing on every open lead" in hero copy | Vic briefing (`assistant.briefing()`) is served only from `/dashboard` route (app.py:729, `@login_required`). It is NOT a public feature. Marketing visitors cannot access it. | **NEEDS-OWNER:** Hero copy referencing the briefing overclaims unless hedged ("coming soon" or removed). The Vic persona exists and briefing works for logged-in users, but that is not "live for all users" in the marketing sense. Use the plan's own hedged fallback: drop the briefing reference from hero entirely, save it for Product page under coming-soon. |
| C7: customers.html placeholders | "Three placeholder cards saying 'Your first customer's quote goes here'" | customers.html:13: `"Your first customer's quote goes here — a real contractor, in their own words."` / :17: `"Real results from a real crew will land here"` / :21: `"We'd rather show one true story"`. Third card is not technically a placeholder — it's honest positioning. | Plan's Phase 2 (waitlist-capture) correctly targets cards 1–2. Third card can remain or be converted. |
| C7: nav "// proof" items | marketing_base.html — "Customer stories" and "Webinars" under `// proof`" | Confirmed at marketing_base.html:51–53: `<span class="ob-ddlabel">// proof</span>`, line 52 `/customers`, line 53 `/webinars`. Both live in the `dd-resources` dropdown. | Plan's comment-out approach is correct; cite lines 51–53 |
| C8: Pro tier phone number | Plan says "add below '1 phone number' (implied from Starter tier)" | Pro card (pricing.html:29–44) does NOT list a phone number at all. "Everything in Starter" inherits the 1 number, but it is not explicit. Crew lists "Up to 5 phone numbers" at line 54. | Add `<li>{{ check }} 1 phone number</li>` explicitly to Pro card before the add-on line. Otherwise the add-on has no anchor. |
| C9: billing.py 80% alert | Plan: "billing.py fuel-gauge logic" already exists | `db.conversations_remaining` (db.py:3473) tracks the count. The billing.py comment at line 31 says fuel gauge refills monthly. **No 80% threshold alert hook exists** in billing.py or db.py — grep returns nothing for "80" or "overage alert". | Use the soft FAQ copy ("we'll alert you") not the "$0.75 overage" version. Do not promise what isn't built. |
| C10: nav dead-end | Same as C7d — marketing_base.html:51–53 | Same confirmation. | Ship as comment-out; no dependencies |

---

## Gate Check

**PRICING PAGE STAYS "COMING SOON" — CONFIRMED. No live checkout introduced by this plan.**

Current state: Both "Get started" CTAs on the pricing page link to `/signup` (pricing.html:26, 43). The Crew "Talk to sales" CTA links to `/contact` (pricing.html:59). None link directly to `/billing/checkout`.

`/billing/checkout` (app.py:2934) is `@login_required` and `POST`-only — it cannot be reached by a visitor clicking a link. It also requires a valid CSRF token (`_csrf_ok()` check at app.py:2938). A pricing-page visitor cannot accidentally trigger a paid flow.

**The annual toggle (Change 1) is the only change that touches CTA URLs.** The plan's suggestion to add `?interval=annual` to `/signup` CTA links is harmless — the param is ignored by the signup route and no checkout is triggered. However, **this means the toggle is cosmetic** (the interval preference is not carried forward). This is a UX gap, not a billing safety gap.

**Change 8 (extra-number add-on):** The plan correctly routes to `/contact` (not self-serve checkout). No Stripe session is created. Safe.

**Change 9 (overage FAQ):** Copy-only. No billing routes touched. Safe.

**Verdict: No plan change would expose a live paid flow. Gate passes.**

---

## Blockers

| Blocker | Change | Severity |
|---------|--------|----------|
| `og-default.png` does not exist in `/static/` | C5 (SEO meta) | Hard: OG image tags will 404 silently on social shares. Create the image before adding the tag. |
| Annual toggle CTA wiring gap | C1 | Medium: The toggle itself is buildable; but the `?interval=annual` hint to `/signup` is silently dropped. The interval preference is not carried into checkout. Needs a design decision: hidden-form POST to `/billing/checkout` (logged-in only) vs. session-store approach during signup. |
| Money-back guarantee | C3 | Hard: Founder must confirm policy before this ships. Copy commits the business to a real refund obligation. |
| Vic morning briefing in hero | C6 | Hard: Briefing is `@login_required`, not public. Hero copy referencing it as a live feature overclaims. Use the hedged fallback (drop briefing from hero; add to Product page under coming-soon). |
| Heritage dogfood quote | C7 Phase 1 | Soft: Requires founder-provided real quote. The rest of Phase 2–4 is unblocked. |

---

## Notes

1. **Smart-quote risk:** None found. The plan's HTML snippets use straight ASCII quotes throughout. No curly quotes to trip up Jinja or HTML parsers.

2. **Pricing math:** Annual effective monthly rates in the plan ($79/$159/$319) are correct rounded-down values. The actual rates from the current template ($950/12 = $79.17, $1,910/12 = $159.17, $3,830/12 = $319.17) round correctly. No discrepancy.

3. **Conversations count in billing.py:** The plan correctly notes that "conversations" as an internal term in billing.py and `db.conversations_remaining` does NOT change — only the three consumer-facing copy instances in pricing.html (lines 23, 37, 53) are renamed. This is safe.

4. **Pro tier phone number omission:** The Pro card currently has no explicit phone-number feature line — it relies on "Everything in Starter" inheritance. Change 8's add-on wording needs an explicit `1 phone number` line added to Pro first, or the "add-on" has no anchor to hang off. This is a structural gap in the plan's execution instructions, not in the plan's intent.

5. **`/contact` route exists:** Confirmed at app.py:496 (`@app.route("/contact")`). Change 8's "Ask us →" link to `/contact` is safe.

6. **No collision risk with Tier-0 agent:** Tier-0 owns auth.html (stars), solutions.html (voice claim), product.html (checkmarks), and /simulator CTA links. None of those touch the files in this plan (pricing.html, marketing_base.html, landing.html, customers.html, solutions.html meta block). The solutions.html voice-claim hedge (Tier-0) and the solutions.html meta block (this plan) are in different parts of the file — no conflict.

7. **`ob-contact` vs `/contact?subject=case-study`:** Change 7 Phase 2 uses `href="/contact?subject=case-study"`. The contact route at app.py:496 accepts GET; the `subject` query param is likely a UX hint (pre-filling a dropdown or subject line) and will not 404. Confirm the contact template uses it if subject pre-fill is desired.

8. **Briefing (Vic) is live for logged-in users:** The feature works and is tested. The audit finding is only about hero copy marketing claims — a visitor who hasn't signed up cannot experience it. Once the product has paying users, moving the Vic briefing description to the hero is appropriate. For now, the safer path is the Product page.
