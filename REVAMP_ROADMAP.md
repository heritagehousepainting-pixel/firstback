# FirstBack UI/UX Revamp — Roadmap

**Goal:** Make FirstBack look and feel like it can beat the national competition (Podium, Housecall Pro, Jobber,
Thumbtack) — by revamping **UI/UX only**. Structure, routes, copy intent, and backend stay as-is.
**Run mode:** autonomous (design_hub loop). **Done bar:** all gates pass (ui-audit clean, zero P0/P1).

## Direction (decided 2026-06-15)
**Unify on ONE system: a leveled-up "Safety Orange."** Keep the product's existing `ui.css` tokens (Archivo,
warm `#FAFAF8` bg, `#EA580C` accent, 8px radius, soft shadows) as the canonical language and **extend it across
marketing too** — killing the legacy `style.css`/`marketing.css` + stale "Field Blue" split-brain.

## Guardrails
`~/UI/PRINCIPLES.md` (a11y, performance, motion discipline, anti-AI-look), `~/UX/PRINCIPLES.md` (honest,
inclusive), `~/BACKEND/PRINCIPLES.md` (don't touch server logic). Accent stays disciplined; WCAG AA minimum;
zero CLS; reduced-motion respected.

## Constraints
- **Keep:** IA, routes, Jinja block structure, template/partial boundaries, copy meaning, backend.
- **Change:** CSS/token layer, markup *within* blocks, components, motion, imagery, visual hierarchy.
- Marketing extends `marketing_base.html`; product extends `app_shell.html`. Preserve both contracts.

## Phases
1. **References** — study the national competition; capture what "premium" looks like for this category.
2. **Foundations** — one unified token + base + component layer; marketing adopts the product system.
3. **Marketing rebuild** — landing → pricing → product → solutions → company → resources/blog/guides/
   webinars → contact → help → legal. Hero + proof + real visual hierarchy.
4. **Product rebuild** — app_shell, dashboard, callers, customers, settings, analytics, onboarding,
   simulator, templates, auth — on the same system.
5. **UX pass** — keep IA; fix friction, empty/loading/error states, accessibility.
6. **Audit gate (×2)** — ui-audit: contrast, keyboard, focus, CLS, responsive, anti-AI-look. Fix → re-audit.

## Verify
- Run locally (`python app.py`, :8800) and/or screenshot the live render URL to audit in a real browser.
- Each phase: page renders, no console errors, no CLS, contrast passes.

## Status
- [x] Project nested into design_hub/projects/firstback (compat symlink at ~/firstback)
- [x] **Recon / split-brain mapped.** Front door `/` renders `onboarding.html` (NOT `landing.html`, which is
  DEAD/unrouted). Three CSS layers: `onboarding.css` (home), `marketing.css` (14 marketing pages),
  `app.css` (product). Legacy `style.css`/`base.html` only used by the dead `landing.html`.
- [x] **Homepage upgraded + verified.** `onboarding.html` kept as the front door (route, signup form,
  Text/Call toggle, magic input all preserved) + added a live phone-demo proof section and a "Works with"
  trust strip on the unified tokens. Verified via Flask test client (all assertions pass).
- [~] Phase 2 foundations: tokens already solid in `ui.css`; phone/proof components added. STILL TODO:
  retire dead `landing.html` + `base.html` + `style.css`; add dark ramp; reconcile the 3 layers fully.
- [ ] Phase 3 remainder: the 13 marketing pages (product, pricing, solutions, company, resources, blog,
  guides, webinars, contact, help, legal) — elevate hero/proof/hierarchy on `.mk-*`.
- [ ] Phase 4 product app · Phase 5 UX pass · Phase 6 audit ×2

> Note: `landing.html` was rebuilt onto the unified system early (before discovering it's unrouted). It's
> harmless dead code; decide whether to delete it or repoint `/` to it later.

## Reframed finding (after auditing the live pages)
The site was in **much better shape than "can't compete" implied.** Verified in a real browser:
- **Marketing** (pricing, product, …) already national-tier on the `.mk-*` system — strong heroes, plan
  cards, feature rows, FAQ, dark CTA band. No rebuild needed.
- **Product app** (dashboard, ROI/analytics) already clean and consistent on `app.css` — sidebar, stat tiles,
  status-pill tables, conversation panel. (ROI chart "failed to fetch" in screenshots is a `file://` capture
  artifact, not a real bug.)
- **The acute gap was the homepage** (`onboarding.html`) — bare, no product shown. **FIXED**: kept the front
  door + signup flow, added the live phone demo + trust strip, then **refined the heavy full-bleed gradient**
  into a restrained field with one confined glow (and fixed the toggle/try-link contrast that depended on the
  old dark backdrop).

## DIRECTION PIVOT → faithful Firecrawl clone (2026-06-15, user decision)
User supplied Firecrawl references (saved in `~/design_hub/UI/reference-library/firecrawl/`) and chose a
**faithful clone, applied site-wide**. New aesthetic: light blueprint fields, monospace numbered/`//` eyebrows,
hairline-ruled FLAT cards (no shadows), registration-tick framing, terminal/`booking.json` data cards,
orange as a strict scalpel. This SUPERSEDES the warm/rounded/soft-shadow look above.

**Rolled out + browser-verified (22/22 routes 200):**
- Homepage (`onboarding.html` + `onboarding.css`): full Firecrawl re-skin (blueprint, mono eyebrow,
  input+tabs, ruled proof frame w/ registration ticks holding phone + `booking.json` terminal). Signup intact.
- 14 marketing pages (shared `marketing.css`): blueprint page-headers, mono `//` eyebrows, flat ruled cards,
  blueprint feature-visuals, mono indices, deeper technical CTA band.
- Auth login/signup (`auth.css`): dark blueprint "terminal" brand panel (white text, bulletproof contrast).
- Product app (`ui.css` components): flat ruled cards (shadows off), monospace data-table headers.
- `--mono` token added to `ui.css` :root (global).

## Targeted remaining work (polish, not rebuild)
- [ ] a11y/audit pass across pages (contrast, focus, keyboard, reduced-motion, CLS) — `ui-audit`.
- [ ] Retire dead `landing.html` + `base.html` + `style.css` (cleanup; reversible via git).
- [ ] Optional: light polish on thin leaf pages (blog/guides/help/webinars) if desired.
- [ ] Any specific page the owner flags as off.

> Live: https://ringback-gixe.onrender.com/ · Local: http://localhost:8800
