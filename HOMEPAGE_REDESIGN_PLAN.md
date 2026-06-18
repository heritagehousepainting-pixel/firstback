# Firstback Homepage Redesign — Plan & Designer Prompts

Goal: turn Firstback's marketing front door into a Firecrawl-caliber experience — a **3D/WebGL hero centerpiece** plus tasteful lightweight motion across the page — built natively in the **Safety Orange** marketing system.

Reference teardown: [UI_MOTION_IDEAS.md](UI_MOTION_IDEAS.md) (full Firecrawl analysis: motion vocabulary + design language).

---

## Decisions (locked with the owner, 2026-06-14)
- **Design system:** Safety Orange (`templates/marketing_base.html` + `static/marketing.css`), **built fresh**. The orphaned blue `landing.html`/`style.css` is retired for the homepage.
- **Hero:** a real **three.js / WebGL** centerpiece.
- **Everything else:** lightweight **Canvas2D + CSS** (no heavy libs beyond three.js).
- **No** single-page/no-redirect constraint (disregarded).

## Stack reality (drives every choice)
- Flask + Jinja templates + plain static CSS/JS. **No bundler / no build step.**
- `marketing_base.html` loads `ui.css`, `onboarding.css`, `marketing.css`, `app.js`. Font: Archivo.
- `marketing.css` already has the right primitives: two-tone headline (`.mk-h1 .dot`), featured pricing card (`.mk-price.featured`), gradient CTA band (`.mk-cta`), numbered steps (`.mk-step .n`). `app.js` already does scroll-reveal via IntersectionObserver.
- Route `/` → `onboarding.html` (the front door; carries the signup path). `landing.html` is unrouted reference.

## The 3D hero concept — "Firstback Signal"
A floating **3D phone** (the hero device) showing the missed-call→booked **SMS conversation that types in message-by-message and loops**, set in a depth field of **orange particles / point-cloud grid** with subtle **mouse + scroll parallax**, and concentric **"firstback" ripple rings** that pulse outward from the phone each time a message lands — the visual metaphor for a missed call ringing back and converting to a booked job. Ends on **"Booked."**, pauses, resets.

Brand mapping: phone = the product · ripples = the callback · typing thread = the AI replying · "Booked." = the outcome. Static fallback = the existing CSS `.phone` mockup from `landing.html`.

## Tech per feature
| Feature | Tech | Notes |
|---|---|---|
| Hero centerpiece | **three.js (WebGL)** | 3D phone + particles + ripples + parallax; lazy-loaded, with fallback |
| SMS thread animation | DOM/CSS overlay (preferred) | message-by-message, loops; reduced-motion → show all |
| Living dot-grid background | Canvas2D | faint, orange "+" sparkles, pause offscreen, off under reduced-motion |
| Scroll reveals | existing IntersectionObserver | add stagger + direction |
| Two-tone headlines | CSS (`.mk-h1 .dot`) | one orange word per headline |
| Section counters `[01/0X]` | HTML + CSS | wayfinding rhythm |
| Pill badges (icon+label) | HTML + CSS | extend marketing.css |
| Testimonial "wall of love" | CSS/JS marquee | auto-scroll columns, pause on hover, reduced-motion → static |
| CTA band motif | Canvas2D | ripple/soundwave over existing `.mk-cta` |

## File plan
- **NEW** `templates/home.html` — extends `marketing_base.html`; the new homepage. Body class `page-home`.
- **NEW** `static/home.css` — homepage-only styles; linked only on home.
- **NEW** `static/hero3d.js` — ES module, the three.js hero.
- **NEW** `static/home-motion.js` — Canvas2D dot-grid, marquee, CTA motif, reveal enhancements (scoped to `.page-home`).
- **NEW** `static/vendor/three.module.js` — pinned three.js, vendored locally (no CDN dependency at runtime).
- **EDIT** `templates/marketing_base.html` — add `{% block head_extra %}` and `{% block scripts %}` hooks + a per-page body-class hook if not present.
- **EDIT** `app.py` — route `/` → `home.html`; move the onboarding front door to `/start` (keep signup reachable). **Confirm with owner before changing `/`.**

## Build sequence
0. Scaffold & route the static Safety-Orange homepage (no motion).
1. Lightweight motion: dot-grid bg, scroll reveals, two-tone headlines, section counters, pills.
2. The three.js WebGL hero (+ fallbacks).
3. Animated SMS thread in the hero.
4. Social-proof marquee + CTA motif.
5. Polish: perf, a11y, reduced-motion, responsive QA.

---

## GLOBAL RULES — append to every prompt below
```
- Stack = Flask + Jinja + plain static CSS/JS, NO build step. Do not add npm/bundlers/frameworks.
- Read first: templates/marketing_base.html, static/marketing.css, templates/onboarding.html,
  templates/landing.html (reference only), static/app.js, and UI_MOTION_IDEAS.md + HOMEPAGE_REDESIGN_PLAN.md.
- Use ONLY the Safety Orange palette via existing marketing.css tokens (--accent, --accent-strong,
  --accent-bg, --ink). No new accent colors.
- Scope all new CSS/JS to the homepage via a `page-home` body class. Do NOT affect the other 14
  marketing pages or the product app (app_shell.html / app.css).
- Everything must work with ZERO motion: honor prefers-reduced-motion and provide non-WebGL / mobile
  fallbacks. Hero text, CTA, and SMS content must be real HTML (SEO + a11y), not baked into canvas.
- Keep the signup funnel intact: the primary hero CTA leads to /signup.
- Verify in the browser with the preview tools on desktop, mobile, AND with reduced-motion enabled.
  Show proof (screenshot / console), do not assert. Fix what you find before moving on.
- Commit nothing unless explicitly asked.
```

---

## Prompt 0 — Scaffold & route the static homepage
```
Build a new Safety Orange marketing homepage to replace the orphaned blue landing.html. The current
front door at "/" is onboarding.html (which carries the signup path).

Task:
1. Create templates/home.html extending marketing_base.html, body class `page-home`.
2. Port the homepage copy/structure from templates/landing.html into the Safety Orange components in
   marketing.css (inspect marketing.css and reuse its classes — .mk-h1/.dot, .mk-card, .mk-step,
   .mk-price/.featured, .mk-cta, pills, etc.). Sections: hero (headline "Turn every missed call into a
   booked job." with "booked job" as the orange .dot word; lead; primary CTA "See the live demo" -> 
   /simulator; secondary "How it works"; trust row) · 4 numbered value features · 3-step "How it works"
   · trades + tools band · testimonial · orange CTA band.
3. For the hero device, reuse landing.html's .phone SMS-thread markup as a STATIC placeholder for now
   (it becomes the no-WebGL fallback later).
4. Create static/home.css for homepage-only tweaks; link it only on home (add a {% block head_extra %}
   to marketing_base.html if needed).
5. Wire routing in app.py: point "/" to home.html and move the onboarding front door to "/start" so
   signup stays reachable. CONFIRM this routing change with me before editing "/".

Acceptance: "/" renders the full static homepage in Safety Orange, visually consistent with /product
and /pricing, no blue anywhere, signup reachable, other pages unaffected. No motion yet.
Do NOT delete landing.html (keep as reference). Verify in preview on desktop + mobile.
```

## Prompt 1 — Lightweight motion layer (Canvas2D + CSS)
```
The static homepage (home.html) exists. Add ambient + scroll motion that needs no WebGL.

Task — create static/home-motion.js (loaded only on .page-home) and extend home.css:
1. Faint animated dot-grid background on the light sections via a single Canvas2D layer: low-opacity
   blueprint dots that fade in/out at grid intersections, sparse orange "+" sparkle accents. Pause when
   tab hidden or canvas offscreen. Fully disabled under prefers-reduced-motion.
2. Enhance scroll reveals: build on the existing IntersectionObserver in app.js; add staggered children
   + a small translate/opacity rise. Ensure every .reveal element in home.html animates in once.
3. Two-tone headlines: one orange .dot word per section headline.
4. Section counters: a [01 / 0X] + category label at the top of each major section (HTML + home.css).
5. Pill badges: small rounded-full "icon + label" intro pills per section, plus a "NEW"/"Most popular"
   style tag.

Acceptance: subtle living background + smooth staggered reveals + counters + pills, all on-brand, 60fps,
and the page is fully static/legible under reduced-motion. Other pages unaffected. Verify in preview
including reduced-motion.
```

## Prompt 2 — The three.js / WebGL hero
```
Build the hero centerpiece — the "Firstback Signal" concept in HOMEPAGE_REDESIGN_PLAN.md.

Task:
1. Vendor three.js locally: place a pinned build at static/vendor/three.module.js (record the version
   in a comment). No runtime CDN dependency.
2. Create static/hero3d.js (ES module). In home.html, replace the static hero device with a <canvas>,
   keeping the static .phone markup as the fallback shown when WebGL is unavailable.
3. Load three only on home, via import map + <script type="module"> in a {% block scripts %}, deferred;
   init on load or when the hero scrolls into view.
4. Scene: a floating 3D phone (rounded box or simple model) as focal point; a depth field of orange
   particles / point-cloud grid; concentric ripple rings pulsing outward from the phone; subtle parallax
   from mouse + scroll; gentle idle camera motion. Palette = Safety Orange + near-black + off-white.
5. Fallbacks: if no WebGL OR prefers-reduced-motion OR small/mobile screen -> hide canvas, show the
   static phone mockup. Lazy-init; pause rendering when offscreen or tab hidden; cap devicePixelRatio.

Acceptance: a polished 3D hero on desktop, graceful static fallback otherwise, no jank, three.js loads
ONLY on home, healthy Lighthouse perf. Verify desktop + mobile + reduced-motion + simulated no-WebGL.
```

## Prompt 3 — Animated SMS thread in the hero
```
Make the hero tell the story.

Task: animate the missed-call -> booked SMS conversation so messages type/appear one-by-one and loop,
synced with a ripple pulse from the phone as each message lands, ending on the "Booked." message, then a
short pause and reset. Prefer a DOM/CSS overlay positioned over the 3D phone (crisp text + accessible);
fall back to a canvas texture only if needed for the 3D integration. Keep all messages as real text.
Under prefers-reduced-motion, show the full thread statically (no typing).

Acceptance: looping, legible, on-brand typing tied to the hero; static full thread under reduced-motion;
readable on mobile. Verify in preview.
```

## Prompt 4 — Social-proof marquee + CTA motif
```
Task:
1. Convert the testimonial section into a "wall of love": 2-3 columns of review cards auto-scrolling
   vertically (CSS/JS marquee), columns at different speeds/directions, pause on hover, seamless loop.
   Highlight the brand name/keyword in orange inside quotes. Use placeholder reviews now, structured so
   real ones drop in later. Under reduced-motion, render a static grid.
2. Add a Canvas2D ripple/soundwave motif behind the existing .mk-cta orange band — a subtle "firstback"
   ripple tied to the brand. Off under reduced-motion.

Acceptance: lively but tasteful social proof + CTA, perf fine, reduced-motion safe. Verify in preview.
```

## Prompt 5 — Polish, performance, a11y, QA
```
Final pass.
1. Audit prefers-reduced-motion across ALL effects — page must be fully usable and static with motion off.
2. Performance: confirm three.js + all canvases lazy-load, pause offscreen/hidden, cap DPR + particle
   counts on mobile, and that the other 14 pages do NOT load any homepage assets.
3. Accessibility: focus states, contrast on orange, canvas aria-hidden, keyboard nav, heading structure.
4. Responsive QA at 360 / 768 / 1280 / 1920.
5. Visual consistency check against /product, /pricing, /solutions.
6. Run Lighthouse; report performance + accessibility scores. Fix issues found.

Acceptance: healthy Lighthouse (perf + a11y), no console errors, consistent brand, smooth on mobile,
fully functional with motion off. Provide before/after screenshots.
```
