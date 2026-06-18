# UI / Motion Ideas — Compiled References

A running log of motion & interaction ideas pulled from reference recordings, to consider for Firstback.
Each entry = one source video. Captured during the 2026-06-14 idea-gathering session.

---

## 1. Firecrawl — homepage hero
- **Source:** https://www.firecrawl.dev (recording: hero section only, ~28s of usable footage)
- **What kind of site:** developer tool / API for scraping the web for AI agents. Slick, technical, "Safety Orange"-adjacent accent (they use orange too — relevant to our palette).

### Motions observed
1. **Two-tone animated headline** — "Power AI agents with **clean web data**" — second line in orange. Large, tight, confident.
2. **Living background grid** — a faint blueprint grid where small dots/squares fade in and out at the intersections (ambient, slow, never distracting). Gives the page a quiet "alive" feel without motion that competes with content.
3. **Orange "+" sparkle markers** scattered on the grid — tiny accent crosses that punctuate the empty space.
4. **Faint scattered code/JSON fragments** in the margins (e.g. `[JSON]`, `SCRAPE`, `url: https://...`) — ambient texture that reinforces "this is a dev tool" without being literal.
5. **Interactive hero input** — a real search/URL bar with mode tabs (Search / Scrape / Map / Crawl) and an orange submit arrow. The primary CTA *is* the product, sitting right in the hero.
6. **★ The standout: a looping "scrape visualization" (~7s loop) below the hero.** It tells the product story as an animation:
   - Left: a wireframe/skeleton of a webpage being built (Logo, Navigation, content blocks).
   - Animated **dashed orange connector lines** flow from the webpage into...
   - Right: a **structured output panel** where JSON/markdown/screenshot fields stream in (`url`, `markdown`, `json`, `screenshot`).
   - Then it resets and loops. It literally shows "messy page → clean structured data" — the whole value prop in one silent loop.

### Worth stealing for Firstback
- The **looping "before → after" product-story animation** is the big idea: show a *missed call → Firstback texts back → call booked* sequence as a silent, looping hero diagram. Same pattern, our story.
- **Living-but-quiet background** (fading grid dots + sparse accent marks) — adds polish without distraction; works with our Safety Orange.
- **Put the product in the hero** — an interactive element instead of a static screenshot.
- Two-tone headline with the orange highlight on the value words.

### Notes / to confirm at build time
- Exact timing/easing not yet measured (a QuickTime control bar overlaid the demo center, and we're in idea mode). When we build, I can do a high-fps frame pass to extract precise durations & easing curves.
- Confirmed detail: the looping scrape demo ends with a **"Scrape Completed" success badge (orange dot)** before resetting — a satisfying completion beat worth copying for our missed-call→booked loop.

---

## 2. Firecrawl — navigation (mega-menu dropdowns)
- **Source:** clip 10.13.00 (18s). Clip 10.12.28 was a duplicate of the hero — no new info.

### Motions observed
1. **Hover-triggered mega-menu.** Hovering a nav item with a ▾ (Products / Integrations / Resources) drops a full-width panel down from under the sticky nav — it expands downward + fades in, and the page behind dims with a subtle scrim.
2. **Structured content.** Each panel is a 2-column grid of rows: icon + bold title + gray subtitle (e.g. "Workflow Automation — Zapier, n8n, Make, and more"). Some menus add a **featured promo column** on the right, divided by a vertical rule (e.g. Resources → "Firecrawl is open source — star us on GitHub," with the app icon + "See GitHub →").
3. **★ Morph-in-place between items.** Sweeping from one nav item to another keeps the single panel open and **swaps its contents while the panel resizes its height to fit** (Resources is taller than Integrations). A smooth morph + crossfade, not a close-then-reopen. This is the premium touch.
4. **Close.** Moving away retracts the panel upward + fades it, the page un-dims, and the hero re-emerges behind it.

### Worth stealing for Firstback
- The **icon + title + subtitle row** structure is a clean way to present features/sections on a marketing nav or in-app menu — scannable, one-line benefit per item.
- A **featured promo column** on the right of a dropdown is a great spot for a CTA ("Book a demo" / "See it live").
- The **morph-in-place** transition between menu items is worth replicating if our nav grows beyond a couple items.

---

## 3. Features — "Start scraping today" (clip 04)
- **Flow:** Reveals on scroll, marked by a **section counter** `[ 01 / 07 ] MAIN FEATURES` (orange active digit). Header pill (🔥 "Developer First") → two-tone headline "Start *scraping* today" → 3 feature cards (Search / Scrape / Interact) → code block with language tabs.
- **Motion:** One feature card highlights at a time (middle "Scrape" card elevated, border + shadow); the code example switches with the active card / language tab. Ambient dot-grid animates throughout.
- **Styling:** Cards = rounded rects, hairline border, active card raised. "NEW" orange micro-badge on Interact. Language switcher (Python / Node.js / cURL / CLI) + "Copy code" button.

## 4. CTA — "Ready to build?" (clips 05 & 06)
- **Flow:** Header pill (🔥 "Get started") → bold headline "Ready to build?" → subtext → two buttons → micro-link "Are you an AI agent? Get an API key here ›". Counter `[ 04 / 07 ] FEATURES`.
- **★ Motion:** **Rising orange "flames"** — particle/ASCII tiny orange dots flickering upward from the section's bottom edge. On-brand (Firecrawl = fire), subtle, looping.
- **Styling:** Primary = orange solid rounded ("Start for free"); secondary = light/outline rounded ("See our plans"). Decorative monospace **corner labels** (`[ MAP ] [ SCRAPE ] [ AGENT ] [ SEARCH ]`) on the blueprint grid.

## 5. Pricing — "Flexible pricing" (clip 07)
- **Flow:** Two-tone headline "Flexible *pricing*" → 4 tier cards (Free / Hobby / Standard / Growth = $0 / $16 / $83 / $333) → "Scale Plans" (Scale / Enterprise) below.
- **Motion:** **Billing toggle** (orange pill switch "Billed yearly · Save $X") animates prices when flipped monthly↔yearly; cards likely animate in on scroll.
- **Styling:** "Standard" tier is the hero — orange **"Most popular"** badge + orange "Subscribe" button; other CTAs light/outline. Big bold price numerals + "/monthly" gray. Check-row feature lists, minimal-border cards.

## 6. Testimonials — "People love building with Firecrawl" (clip 08)
- **Flow:** Header (💬 "Community" pill + two-tone headline "People love building with *Firecrawl*" + subtext) → wall of testimonial cards in 3 columns.
- **★ Motion:** **Auto-scrolling columns** (vertical marquee) — the classic "wall of love," columns drifting upward, looping seamlessly.
- **Styling:** Minimal cards split by hairline grid lines — avatar + name + gray @handle, quote below. **@firecrawl mentions highlighted in orange** inside quotes.

---

## Firecrawl design language (consolidated)
*Answers the buttons / pills / type / size / background questions — consistent site-wide.*

- **Color:** off-white backgrounds (~`#FAFAFA`), near-black text (~`#111`), **one accent** — a Safety-Orange-like `#FA5D19`/`#F03E00`. Accent used only for: the highlighted headline word, primary buttons, badges, active section digit, @mentions, toggles. Very disciplined — one accent, used sparingly.
- **Typography:** clean **geometric/grotesque sans** (Inter/Geist-like). Headlines **large, bold (700+), tight tracking**, always **one word in orange**. Body gray, ~16px. Labels/counters/corner tags in **monospace** (technical flavor).
- **Buttons:** rounded (~8px). **Primary = solid orange, white text. Secondary = light/white, hairline border, dark text.** Compact padding, used identically everywhere (nav "Sign up", hero submit, CTA, pricing).
- **Pills/badges:** small **rounded-full** pills = orange icon (flame/number) + short label ("Developer First", "Get started", "Community"). Plus tiny **uppercase tag badges** ("NEW", "Most popular") in orange.
- **Backgrounds:** signature **faint blueprint dot-grid** that subtly animates (dots fading at intersections), with **orange "+" sparkles**, **monospace corner labels** (`[ SCRAPE ]`…), and scattered faint code/JSON. The CTA swaps it for **rising orange flame particles**.
- **Layout system:** sections **numbered `[ 0X / 07 ]`** + category label → rhythm + wayfinding on a long page. Generous whitespace, centered blocks, hairline dividers.
- **Motion vocabulary (all subtle, none competes with reading):** (1) looping product-story demo, (2) ambient living background, (3) hover mega-menu morphing in place, (4) branded particle flames, (5) animated price toggle, (6) auto-scrolling social-proof wall.

## Top ideas to bring into Firstback
1. **Looping hero demo** of *missed call → auto-text → booked* (their scrape-demo pattern, our story).
2. **One disciplined accent** (Safety Orange) used exactly like theirs — highlight word, primary button, badges, toggles; everything else neutral.
3. **Numbered section system** `[ 01 / 0X ]` on the marketing page — cheap polish + wayfinding.
4. **Two-tone headlines** (one orange word) as a repeating device.
5. **Pill badges** (icon + label) for section intros, plus "NEW"/"Most popular" tags.
6. **Ambient living background** — a Firstback motif (subtle phone / sound-wave dots) in place of flames.
7. **Wall-of-love** auto-scroll for reviews, customer words highlighted.
8. **Pricing**: recommended plan highlighted in orange + animated billing toggle.
9. **Button system**: solid-orange primary / light-outline secondary, rounded, consistent everywhere.

---

# Reference: QuickBooks — product (signed-in) layout & flow
*Added 2026-06-14. The **PRODUCT-tier** reference — the complement to Firecrawl (the marketing-tier reference above). Applies to the **signed-in app only** (`app_shell` pages: dashboard, settings, dense data screens), **never** the public marketing site.*

**The instruction (Jonathan's read):** take the **layout, flow, page-stacking, and pills — NOT the artwork/visual style.** Keep our Safety Orange look; borrow how it's *organized*, not how it's *painted*.

- **Signed-in UI is very nice** → strong model for our product surface.
- **"Stacking pages like settings"** → how QuickBooks organizes dense functionality into stacked, hierarchical sections/sub-pages is the model for our Settings (and other heavy product screens).
- **Pills are "perfect for what they need"** → reinforces our existing `.pill` system (booked / urgent / warning / new / neutral) for status in lists, tables, headers.
- **Artistic/visual design → skip.** Layout + flow + information architecture only.

**To pull (confirm against recordings/screens when they arrive):**
- Page-stacking / IA for settings & dense admin pages (sectioning, sub-nav, progressive disclosure).
- Flow through a multi-section area without feeling lost.
- Pill placement & vocabulary in lists/tables/status.
- Spacing rhythm & density for data-heavy product screens.

**Maps to:** `SITE_MOTION_SYSTEM.md` → **product tier (restrained)** — Dashboard, Settings, Simulator. Layout/flow guidance, distinct from the marketing motion vocabulary above.

**Open question:** what does "stacking pages" mean precisely — stacked **cards/sections** in one page (like Settings today), a settings **sub-sidebar / tabbed sub-nav**, or **breadcrumbed sub-pages** you drill into? (Recordings/screens will answer this.)
