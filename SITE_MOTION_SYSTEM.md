# Ringback — Site-Wide Motion & Interaction System

The umbrella plan: turn the whole site (marketing + conversion + product) into a single, consistent motion language tuned for **"minimal but optimal — looks basic until you see it live."**

Companion docs: [UI_MOTION_IDEAS.md](UI_MOTION_IDEAS.md) (Firecrawl teardown / inspiration) · [HOMEPAGE_REDESIGN_PLAN.md](HOMEPAGE_REDESIGN_PLAN.md) (the home page = **Phase C** of this plan).

---

## 1. Doctrine — the "basic until live" rules
1. **Static-first.** Every screen is clean, calm, and fully functional with zero motion. Motion is a layer, never a crutch.
2. **Motion communicates, it doesn't decorate.** Each animation does one job: *feedback* (you did something), *continuity* (where did it come from / go), *hierarchy* (look here first), or *ambient life* (the page is alive). If it does none of those, cut it.
3. **One language.** Every motion uses the same small set of duration + easing tokens. No bespoke timings. Consistency is what reads as "premium."
4. **Restraint = luxury.** Subtle beats flashy. Ambient backgrounds stay near-subliminal (low opacity, slow). Nothing loops in a way that competes with reading.
5. **Two tiers (critical).** **Marketing/Conversion = expressive** (action backgrounds, scroll choreography, the 3D hero). **Product app = restrained** — calm, functional micro-interactions only. Never bring the marketing splash into `/dashboard`, `/settings`, `/simulator`. (Per the existing design rule.)
6. **Free or it doesn't ship.** 60fps, transform/opacity only, no layout thrash, no CLS, pause when offscreen/hidden, lazy-load anything heavy. Honor `prefers-reduced-motion` everywhere (the site already does globally — keep it).

---

## 1a. Reference weighting — Firecrawl leads, QuickBooks supports
Two references, deliberately **unequal — lean heavier on Firecrawl.**
- **Firecrawl = primary design DNA.** Sets the *look + feel everywhere*: Safety-Orange discipline, two-tone headlines, pills, action/ambient backgrounds, the full motion vocabulary, the "minimal-but-alive" feel. Governs the marketing tier almost entirely, and supplies the visual + motion language for the product tier too.
- **QuickBooks = secondary, scoped.** Used ONLY for the **product tier's structure** — page-stacking / information architecture for dense screens (Settings et al.), flow between sections, and pill placement in lists/tables. Its **artwork is not used**; its structural patterns get re-skinned in Firecrawl's language.
- **Tie-breaker:** if the two ever conflict on look / feel / motion → **Firecrawl wins.** QuickBooks only wins on *how a dense product page is organized*.

---

## 2. What exists today (the substrate)
- **Tokens (`ui.css`):** full color/type/spacing(4px)/radius/shadow system. **Gap: no motion tokens.**
- **Motion today:** hardcoded `.12s ease` hover/focus on buttons, fields, nav, table rows, calendar, toggles (`.15s`). **No `@keyframes`, no scroll reveal applied, no page transitions, no ambient/action backgrounds.**
- **Components:** 8 Jinja macros (button, field, pill, card, data_table, stat_tile, chat_bubble, empty_state) + a living gallery at **`/ui`** (`ui_kit.html`) — our proving ground.
- **Shells:** marketing `marketing_base.html` (+`marketing.css`); product `app_shell.html` (+`app.css`); both load `ui.css` + `app.js`. `app.js` = 7 element-gated IIFEs incl. an unused-on-marketing scroll-reveal and the simulator/calendar logic.
- **Conversion:** front door `/` = `onboarding.html` (static multi-layer orange gradient); `auth.html` split panel. A few `scale()` hovers already exist here.

---

## 2b. Audit addendum — current surfaces (2026-06-14)
Re-audited before building. Deltas since this plan was first drafted:
- **Product tier grew by two pages.** **`/callers`** (`callers.html`): a triage inbox — an *Import contacts* card, a *For review* card with **tabs** (To review / Sorted / Dismissed) + search + **bulk-select** + a data table of suggestions with category **pills** and accept/dismiss/undo, and a *Screened numbers* directory. **`/analytics`** (`analytics.html`): the ROI dashboard — a **range toggle** (30d/90d/all), 4 JS-rendered **stat tiles**, and a JS-rendered **SVG bar chart** (leads vs booked). Dashboard now also shows reminder-state + stage pills and a cancel action.
- **JS/CSS are bigger.** `app.js` ≈ **872 lines / 10 element-gated IIFEs** (added analytics render, callers inbox/directory/import/google-sync, re-engage). `app.css` ≈ **349 lines** with new `.cl-*` (tabs/bulk/inbox/import), `.roi-*` (chart), `.screen-*`, `.review-nudge`. Shared JS helpers: `apiFetch`, `addBubble`, `fmtPhone`, `fmtClock`. **Still zero `@keyframes`/reveal/page-transitions — only `.12s`/`.15s ease` hovers.** The motion layer is still entirely net-new.
- **Backend (context only, no UI work):** Twilio voice/SMS webhooks + callback system, owner alerts, reminders/follow-ups, Google Contacts import, compliance — all wired, simulated when unconfigured.

**What this forces into the plan:**
- **Product-tier matrix (§8) gains:**
  - **Callers** — enhance `.cl-tabs` with the **segmented sliding-indicator** primitive; row-select ease; **bulk-bar slide-in**; **toast** on accept/dismiss; **pill pop** when a suggestion is sorted; tab-switch content cross-fade. *Restrained.*
  - **Analytics** — **count-up** the stat tiles; **range toggle** = segmented indicator; **chart bars grow-in** on reveal (animate `renderChart` SVG heights; reduced-motion = final). *Restrained.*
  - **Dashboard** — reminder-state + stage pills use the pill motion; conversation bubbles fade/slide-in.
- **Concrete JS hooks** for the motion engine (don't rewrite the 10 IIFEs — attach to their render fns): `addBubble` (chat entrances), `renderChart`/`renderTiles` (analytics), `renderInbox`/`renderDirectory` (callers). `motion.js` stays separate + additive.
- **Component reconciliation:** `.cl-tabs` and `.roi-range` are *real* tab/segmented controls → they're the segmented-indicator primitive's first production consumers, not just the `/ui` demo.

---

## 3. The motion token layer (add to `ui.css :root`)
The single source of truth. Everything references these.
```css
/* durations */
--dur-instant: 80ms;
--dur-1: 120ms;   /* micro: hover / press (matches today's .12s) */
--dur-2: 200ms;   /* standard: toggles, small reveals */
--dur-3: 320ms;   /* entrances, accordions, section reveals */
--dur-4: 520ms;   /* large / hero choreography */
/* easings */
--ease-standard: cubic-bezier(.2, 0, 0, 1);    /* quick out, soft settle (default) */
--ease-entrance: cubic-bezier(.16, 1, .3, 1);  /* decelerate — things arriving */
--ease-exit:     cubic-bezier(.4, 0, 1, 1);    /* accelerate — things leaving */
--ease-spring:   cubic-bezier(.34, 1.56, .64, 1); /* gentle overshoot — toggles, pops */
/* choreography */
--stagger: 60ms;       /* delay between siblings in a group */
--reveal-rise: 14px;   /* translateY distance for entrances */
```
Reduced-motion: keep the existing global `prefers-reduced-motion` rule; additionally, JS-driven effects must check `matchMedia('(prefers-reduced-motion: reduce)')` and render the static end-state.

---

## 4. Interaction primitives (define once, inherit everywhere)
Upgrade these in `ui.css` + the component macros so every page gets them for free. All built on the tokens above.

| Primitive | Behavior | Tech |
|---|---|---|
| **Button** | press = scale .97 (`--dur-1`/spring); primary hover = -1px lift + shadow; **loading** = label swaps to inline spinner/“…”, width held (no CLS); disabled = no transform | CSS + tiny JS for loading |
| **Pill / badge** | entrance fade+rise on reveal; **urgent** pill = slow dot pulse (2s); “Saved/Booked” = spring pop on appear | CSS keyframes |
| **Input / textarea** | focus = ring grows (`--accent-ring`) + border; label/help settle; **error** = one soft shake then static red; success = check fade-in | CSS + JS on validate |
| **Card** | reveal on scroll; hover = -2px lift + shadow-pop; arrow links = arrow slides 4px | CSS |
| **Link (nav/inline)** | underline grows from left on hover/focus-visible; active nav = persistent underline that **slides** between items | CSS |
| **Accordion (`<details>`)** | animate height open/close + chevron rotate (today it snaps) | JS height-from-auto, or CSS grid-rows trick |
| **Tabs / segmented** | active pill slides under the selected item (shared indicator) | CSS + JS |
| **Toast / inline status** | slide+fade in, auto-dismiss, stack | new tiny component |
| **Focus-visible** | consistent 2px accent ring everywhere (keep) | CSS |

---

## 5. Spacing, text rhythm & flow
- **Vertical rhythm:** standardize section padding to the existing scale — e.g. `--space-24` (96px) desktop / `--space-16` (64px) mobile between major sections; `--space-12` within. One rhythm across all pages = the "flow."
- **Text-box spacing:** body measure ~60–70ch; headline→lead gap `--space-4`; lead→action `--space-8`; input internal padding standardized via the `field` macro; label→control `--space-2`, control→help `--space-2`. Codify in `ui.css`.
- **Scroll flow:** smooth-scroll for in-page anchors; consistent scroll-reveal cadence (rise `--reveal-rise`, `--dur-3`, `--ease-entrance`, `--stagger` between siblings) so every page "breathes" the same way.

---

## 6. Action-background library (the ambient layer)
A small, reusable set. Each is a single GPU-cheap layer, paused offscreen, **off under reduced-motion**, and tier-appropriate (marketing/conversion only — never product).

| Background | Where | Tech |
|---|---|---|
| **Living dot-grid** | marketing section bands (light) | Canvas2D — faint dots fade at intersections + sparse orange "+" sparkles |
| **3D "Ringback Signal" hero** | home only | three.js (lazy, vendored) — see Phase C |
| **Ripple / soundwave** | CTA bands (`.mk-cta`), final CTAs | Canvas2D — concentric "callback" rings |
| **Gradient-mesh drift** | onboarding `.ob-bg`, auth `.au-art` | CSS — animate the *existing* static gradients with a slow 20–30s breathing drift |
| **Particle flames** (optional) | a single brand moment | Canvas2D — Firecrawl-style, used sparingly if at all |

---

## 7. Page-transition system (the cross-page "flow")
Because this is a **server-rendered multi-page** app (not an SPA), use the **View Transitions API** for MPA — pure progressive enhancement:
- Opt in per shell: `@view-transition { navigation: auto; }` + assign `view-transition-name` to elements that persist across pages (logo, nav, page headline, CTA).
- Result: navigating between marketing pages cross-fades and **shared elements morph** in place — the "flow" between pages — with **zero JS framework** and automatic fallback (older browsers just navigate normally).
- Product app: a quieter variant (fast cross-fade only) or skip — keep it instant/functional.

---

## 8. Per-surface application matrix
**MARKETING (expressive tier)**
| Page | Gets |
|---|---|
| Global shell (`marketing_base`) | nav condenses on scroll, sliding active-underline, smooth mobile menu; footer reveal; View-Transitions between pages |
| Home (`/`) | full Phase C: 3D hero, living bg, reveals, counters, marquee, CTA ripple |
| Product | alternating `.mk-row` reveal from sides; `.mk-visual` icons draw/float; steps connect; dot-grid band |
| Solutions | `.mk-trade` grid staggers in; enhanced magnetic hover (lift + icon nudge) |
| Pricing | price cards reveal; **count-up** prices; `.featured` emphasis; smooth FAQ accordion |
| Resources | card grid stagger + hover lift + arrow slide |
| Company | **count-up** stats (`.big`); value cards reveal |
| Customers | testimonial cards reveal; star draw; optional **wall-of-love marquee** |
| Blog | reading-focused: heading reveals + **reading-progress bar**; no heavy bg |
| Guides / Help / Templates | **smooth accordions**; anchored smooth-scroll; Templates: code **copy button** with “Copied” feedback |
| Contact | form micro-interactions (focus, validation, **submit→success** transition); bullets reveal |
| Terms / Privacy | restrained: reading-progress + section reveal; no ambient bg |

**CONVERSION (expressive but focused)**
| Surface | Gets |
|---|---|
| Onboarding (front door) | breathing gradient bg; `.ob-input` focus glow; segmented-toggle sliding indicator; announce pill shimmer |
| Auth | `.au-art` gradient drift + `.au-proof` reveal + star draw; `.au-switch` tab slide; field micro-interactions; submit loading |

**PRODUCT (restrained tier — calm, functional)**
| Page | Gets |
|---|---|
| Shell (`app_shell`) | sidebar active-item sliding indicator; page-head fade; content light stagger-in (once) |
| Dashboard | stat tiles **count-up once**; row hover/selection ease; **conversation bubbles fade+slide-in (staggered)** when a lead opens; urgent-pill dot pulse |
| Settings | card reveal (light); provider-connect **check pop**; **calendar month cross-fade/slide**; toggle thumb spring; **save-bar slide-in when dirty** + “Saved” pop; optimistic save |
| Simulator | the showpiece: **typing indicator** + bubbles appear in sequence; status banners slide+pop (booked/urgent); live-dot pulse; composer enable transition |

---

## 9. Tech stack (all no-build, vendored, progressive)
- **CSS + Web Animations API (JS)** for choreography. **IntersectionObserver** for reveals/count-ups/bg triggers (extend the existing one). **View Transitions API** for page flow. **Canvas2D** for action backgrounds. **three.js** for the home hero only (lazy, vendored to `static/vendor/`). No npm/bundler. Everything degrades gracefully and respects reduced-motion.

### Files
- **EDIT `static/ui.css`** — motion tokens + keyframes + primitive motion (the keystone).
- **NEW `static/motion.js`** — shared engine: reveal v2 (stagger/direction), count-up, accordion-height, tabs indicator, marquee, page-transition hooks, ambient-bg loader, button loading/feedback, toast. Element-gated; loaded site-wide.
- **NEW `static/motion.css`** (or fold into ui.css) — shared keyframes/utilities + `@view-transition` rules.
- **EDIT `marketing_base.html` / `app_shell.html`** — add `data-surface="marketing|product"` on `<body>`, View-Transition opt-in, `view-transition-name`s, and `{% block head_extra %}`/`{% block scripts %}` hooks.
- **EDIT `ui_kit.html` (`/ui`)** — extend the gallery to document & demo every motion (the living style guide).
- **Per-page templates** — mostly additive: `.reveal` hooks, section counters, data attributes.
- **Home extras** (Phase C): `home.html`, `static/home.css`, `static/hero3d.js`, `static/home-motion.js`, `static/vendor/three.module.js`.

---

## 10. Build phases
| Phase | Scope | Why this order |
|---|---|---|
| **A — Foundation** | Motion tokens + primitives in `ui.css`; build `motion.js`/`motion.css`; demo all of it in `/ui` | Get the language right in ONE place before rollout = guaranteed consistency |
| **B — Global shell** | Nav/footer motion + View-Transitions (marketing) + product shell micro-motion | One change lifts every page at once |
| **C — Home** | The 3D hero + full homepage (existing HOMEPAGE_REDESIGN_PLAN.md prompts) | Flagship; proves the ceiling |
| **D — Marketing pages** | Apply the system to all 14 pages per the matrix | Bulk rollout on a proven foundation |
| **E — Conversion** | Onboarding + auth motion | High-leverage funnels |
| **F — Product app** | Restrained micro-interactions (dashboard/settings/simulator) | Calm tier, done last & carefully |
| **G — QA** | Perf, a11y, reduced-motion, responsive, cross-surface consistency, Lighthouse | Ship gate |

---

## GLOBAL RULES — append to every designer prompt
```
- Stack = Flask + Jinja + plain static CSS/JS, NO build step. Vendor any lib locally. No npm/bundlers/frameworks.
- Read first: SITE_MOTION_SYSTEM.md (this plan), static/ui.css, static/app.js, templates/marketing_base.html,
  templates/app_shell.html, templates/ui_kit.html, and UI_MOTION_IDEAS.md.
- Use ONLY the motion tokens from ui.css (--dur-*, --ease-*, --stagger, --reveal-rise) and the Safety Orange
  palette tokens. No bespoke timings or new colors.
- TWO TIERS: marketing/conversion may be expressive; the PRODUCT app (app_shell pages) stays restrained —
  calm functional micro-interactions only, NO ambient/action backgrounds.
- Motion must be transform/opacity only (no layout thrash, no CLS). Pause anims when offscreen or tab hidden.
  Lazy-load anything heavy. Honor prefers-reduced-motion AND matchMedia in JS — always render a static end-state.
- Additive & scoped: don't break existing pages, the 8 component macros, or the product app. Gate JS by element
  presence and by body[data-surface].
- Verify in the browser with the preview tools on desktop, mobile, AND reduced-motion. Show proof, don't assert.
- Commit nothing unless asked.
```

---

## 11. Designer prompts

### Prompt A — Motion foundation (do this first; everything depends on it)
```
Establish Ringback's motion foundation. No page redesigns yet — build the language and prove it in /ui.

1. Add the motion token layer to static/ui.css :root exactly as specified in SITE_MOTION_SYSTEM.md §3
   (--dur-*, --ease-*, --stagger, --reveal-rise). Keep the existing prefers-reduced-motion rule.
2. Add a shared keyframe/utility layer (in ui.css or a new static/motion.css linked by both shells):
   fade-rise (reveal), pop (spring appear), pulse-dot, shimmer (button loading), shake (input error),
   plus .reveal/.reveal.in classes wired to --reveal-rise/--dur-3/--ease-entrance with a --stagger var
   honored via an index custom property.
3. Upgrade the interaction primitives (SITE_MOTION_SYSTEM.md §4) in ui.css + the component macros:
   button press-scale + primary hover-lift + a loading state (label→spinner, width held); pill entrance +
   urgent dot-pulse + "saved" pop; field focus-ring-grow + error shake; card hover-lift; nav/inline link
   underline-grow; smooth <details> accordion; segmented/tab sliding indicator.
4. Create static/motion.js (vanilla, element-gated, loaded by both shells): a reveal engine (IntersectionObserver
   with stagger + direction, replacing/extending the one in app.js), a count-up util, an accordion-height util,
   a tabs-indicator util, a button-loading helper, and a tiny toast. All check matchMedia reduced-motion.
5. Extend templates/ui_kit.html (/ui) to DEMO every token and every primitive motion (hover, press, loading,
   reveal, accordion, tabs, toast, dot-pulse) so /ui becomes the living motion style guide.

Acceptance: /ui shows the full motion system; primitives feel consistent (all using the tokens); zero CLS;
everything static under reduced-motion; no existing page or macro breaks. Verify in preview incl. reduced-motion.
```

### Prompt B — Global shell motion + page transitions
```
Apply motion to BOTH shells so every page benefits at once.

Marketing (marketing_base.html + marketing.css):
- Nav: condense/shrink + shadow on scroll; active link uses the sliding underline indicator; mobile menu
  slides/fades (enhance the existing burger IIFE).
- Footer: reveal on scroll.
- Page transitions: add `@view-transition { navigation: auto }` and assign view-transition-name to the logo,
  nav, page headline (.mk-head h1), and primary CTA so they morph across navigations. Pure progressive
  enhancement — confirm graceful fallback where unsupported.
- Add body[data-surface="marketing"].

Product (app_shell.html + app.css):
- Sidebar: active nav-item sliding indicator; page-head fade; .page-body light stagger-in on load (once).
- A quiet cross-fade view-transition only (or none). body[data-surface="product"].

Acceptance: smooth between-page flow on marketing, calm shell motion on product, nothing janky, fallback clean.
Verify desktop + mobile + reduced-motion across 2–3 marketing pages and 2 product pages.
```

### Prompt C — Home (3D hero + full homepage)
```
Execute HOMEPAGE_REDESIGN_PLAN.md (Prompts 0–5) — the flagship home page with the three.js "Ringback Signal"
hero — now built ON TOP of the motion foundation from Prompt A (reuse its tokens, reveal engine, and primitives
instead of re-inventing them). Follow that doc's acceptance criteria and the GLOBAL RULES here.
```

### Prompt D — Marketing pages rollout
```
Apply the motion system to the marketing pages per the SITE_MOTION_SYSTEM.md §8 matrix. Work page-by-page;
verify each before the next. Reuse Prompt A's tokens/engine/primitives — additive only.
- product: alternating .mk-row reveal from sides; .mk-visual icon draw/float; steps connect; dot-grid band.
- solutions: .mk-trade grid stagger-in + magnetic hover (lift + icon nudge).
- pricing: price-card reveal; count-up prices; .featured emphasis; smooth FAQ accordion.
- resources: card grid stagger + hover lift + arrow slide.
- company: count-up stats; value cards reveal.
- customers: testimonial reveal + star draw + optional wall-of-love marquee.
- blog: heading reveals + reading-progress bar; no heavy bg.
- guides / help / templates: smooth accordions + smooth-scroll anchors; templates code copy-button w/ feedback.
- contact: field micro-interactions + submit→success transition; bullets reveal.
- terms / privacy: reading-progress + section reveal only.
Add the living dot-grid action background to light marketing bands (Canvas2D, paused offscreen, reduced-motion off).
Acceptance: every page uses the same reveal cadence and primitives; tasteful, not busy; perf 60fps; reduced-motion
static. Verify each page desktop + mobile + reduced-motion.
```

### Prompt E — Conversion surfaces
```
Onboarding (front door, onboarding.html + onboarding.css):
- Animate the EXISTING .ob-bg orange gradient with a slow 20–30s breathing drift (CSS, reduced-motion off).
- .ob-input: focus glow/ring; .ob-toggle: sliding active indicator; .ob-announce: subtle shimmer.
  Keep the existing .ob-submit/.ob-chat scale hovers.
Auth (auth.html + auth.css):
- .au-art: gradient drift + .au-proof reveal + star draw; .au-switch: sliding tab indicator; field
  micro-interactions; submit button loading state.
Acceptance: funnels feel alive and premium without slowing entry; reduced-motion safe. Verify desktop + mobile.
```

### Prompt F — Product app (restrained tier)
```
Add calm, FUNCTIONAL micro-interactions only — no ambient/action backgrounds. Reuse Prompt A primitives.
- Shell/Dashboard: stat tiles count-up once on load; row hover/selection ease; when a lead opens, conversation
  bubbles fade+slide-in staggered (update addBubble in app.js to support an entrance, gated by reduced-motion);
  urgent pill dot-pulse.
- Settings: light card reveal; provider-connect check pop; calendar month change cross-fade/slide (animate the
  render() swap); toggle thumb spring; save-bar slides in when the form is dirty + "Saved" pill pop; make save
  optimistic where safe.
- Simulator: typing indicator before each agent bubble; bubbles appear in sequence; status banners slide+pop
  (booked/urgent); live-dot pulse; composer enable transition.
Acceptance: the app feels responsive and polished but calm — clearly a different tier than marketing; no jank;
fully functional and static under reduced-motion. Verify dashboard, settings, simulator on desktop + mobile.
```

### Prompt G — QA & ship gate
```
Final pass across the whole site.
1. Reduced-motion audit: every effect has a static end-state; site fully usable with motion off.
2. Performance: 60fps; transform/opacity only; no CLS; canvases + three.js lazy-load and pause offscreen/hidden;
   confirm marketing assets don't load on product pages and vice-versa; cap DPR/particle counts on mobile.
3. Accessibility: focus-visible rings everywhere, contrast on orange, canvas aria-hidden, keyboard nav, heading
   order, View-Transitions don't trap focus.
4. Responsive QA at 360/768/1280/1920 on a sample of each surface.
5. Consistency: same reveal cadence, easing, and primitive behavior on every page; one motion language.
6. Lighthouse on home + one marketing + one product page; report perf + a11y. Fix what you find.
Acceptance: consistent, fast, accessible, reduced-motion-safe across all surfaces. Provide before/after captures.
```
