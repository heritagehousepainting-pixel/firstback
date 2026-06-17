# RingBack + design_hub — UI/UX Knowledge Map

> Built 2026-06-16 from a 10-agent parallel read of RingBack and `design_hub`.
> Purpose: the working brief for the **Mobbin-driven UI/UX upgrade**. Read this
> first; it tells you the current state, the constraints, and the tools.

---

## 0. TL;DR — the five things that matter for the upgrade

1. **Brand is locked & strict — extend it, don't reinvent.** Safety Orange
   `#EA580C` (CTA / active-nav / links / logo mark ONLY — "orange is a scalpel"),
   ink `#0B0E14`, off-white `#FAFAF8` (never pure #fff/#000), **Archivo** everywhere,
   hairline borders, **no drop shadows / no glow**, sharp ease-out motion. The
   authoritative source is `CANVA/brands/ringback.md`. `DESIGN.md` is **stale**
   (documents the dead "Field Blue" blue era) — don't trust its color.
2. **Two competing roadmaps target the same surface.** `REVAMP_ROADMAP.md` = a
   visual reskin that *freezes* IA/routes/`/dashboard`. `BRAIN.md` +
   `COMMAND_CENTER_MASTER_PLAN.md` = a *rebuild* of `/dashboard` into **"Vic," an AI
   marketing employee** (proactive feed of tap-action cards, not orb+chatbox).
   **Decide which scope you're in before touching `/dashboard`.**
3. **The design_hub skills are currently un-invokable** — 31/40 skill symlinks are
   dangling (see §5). Fix the wiring first or you can't use `ui-build`/`ux-design`.
4. **Mobbin plugs in at `ui-references` (UI) and `ux-ideate`'s competitive audit
   (UX).** The `UI/reference-library/` is essentially **empty** (6 firecrawl PNGs).
   First upgrade move = feed Mobbin captures in *with "design toward this" notes*.
5. **Honesty UI is a non-negotiable product ethos.** Every outbound shows exact
   recipient + verbatim body + opt-out + live/test badge; never claim "live" when
   gated; honest empty/simulated states everywhere. Preserve this — don't let a
   reference site flatten it into a generic "you're all set."

---

## 1. What these two folders are

### design_hub — the product-build orchestrator (`~/apps/design_hub`)
A prompt → product factory. A strict pipeline **UX → UI → BACKEND**, plus **CANVA**
as a parallel marketing/artwork system. Each system = `PRINCIPLES.md` (binding
guardrails) + `*_TECHNIQUES.md` (knowledge base) + `skills/` + `install.sh`
(symlinks skills into `~/.claude/skills/`). The root `README.md` is the glue: a
6-phase build loop (INTAKE → UX → FRONTEND → BACKEND → AUDIT → DIAGNOSE/ROUTE/FIX),
a **router table** (symptom → owning skill) that drives self-correction, and the
rule "narrow asks bypass the loop" (go straight to the owning sub-skill).
`projects/ringback/` is the only product; `~/apps/ringback` symlinks to it.

### RingBack — the product (`~/apps/design_hub/projects/ringback`)
Flask monolith (`app.py` ~1700 lines, no blueprints), SQLite, multi-tenant by
`business_id` but **effectively single-tenant** (everything defaults to business 1
= Heritage House Painting). Missed-call → instant text-back → AI books an estimate.
Pluggable AI brain (`minimax` default / `claude` / `demo` fallback). Most
integrations (Twilio, Google Cal/Contacts, SMTP, screening, voice) are **built but
gated** — they simulate honestly in-app until credentials exist.

---

## 2. RingBack UI/UX — current state (the upgrade target)

### 2.1 Two front doors (marketing)
- **Live homepage `/` = `onboarding.html`** — a **standalone** template (its own
  `<head>`, loads `ui.css`+`onboarding.css`+`motion.css`, NOT `marketing.css`).
  Firecrawl-style: monospace eyebrow + counter, two-tone headline, **hero CTA is a
  fake "search bar"** (phone input → `/signup`) with a Text/Call segmented toggle,
  a static SMS-thread + `booking.json` terminal proof block.
- **`marketing_base.html`** = shell for ~14 secondary pages (product, pricing,
  solutions, company, customers, contact, resources, guides, help, blog, webinars,
  templates, terms, privacy). Shared nav (mega-menu dropdowns), CTA band, footer.
- **`landing.html` is DEAD/unrouted** — still carries false Jobber/Housecall/Angi
  pills + "4 seconds" claim. Flagged for deletion in two docs. `base.html` +
  `style.css` (legacy "Field Blue") only feed it.
- ⚠️ **Nav is duplicated** between `onboarding.html` and `marketing_base.html` — a
  maintenance hazard. The planned unified `home.html` was never built.

### 2.2 The product app (extends `app_shell.html`)
Left-sidebar shell (248px grid, collapses to a top strip <900px — **no real mobile
drawer**). **Route ↔ template ↔ nav-label are three different words** (a real
onboarding/maintenance hazard):

| Route | Template | Nav label | What it is |
|---|---|---|---|
| `/dashboard` | `command.html` | **Command** | Dark WebGL-orb "Jarvis" command center (chat + suggestion chips). Loads its own `assistant.css`/`assistant.js`. |
| `/pipeline` | `dashboard.html` | **Pipeline** | Light data cockpit: stat tiles, leads table + conversation pane, scheduled estimates, screened calls, alerts. |
| `/setup` | `setup.html` | **Go Live** | 4-step go-live stepper (profile → number → A2P 10DLC → forwarding) + "fully set up" recommended tier. Pinned top until live, then retires with a check. |
| `/settings` | `settings.html` | Settings | One long form: profile, calendar, screening, alerts, reminders, scheduling, AI instructions. Anchored cards for deep-linking. |
| `/simulator` | `simulator.html` | **Demo** | Faux-phone device; fire a missed call, reply as homeowner. |
| `/analytics` | `analytics.html` | **ROI** | JS-driven (`/api/analytics`): range toggle, stat tiles, bar chart. |
| `/training` | `training.html` | **Memory** | Review AI convos, teach corrections, see learnings. **Heavy inline-style debt.** |
| `/callers` | `callers.html` | Callers | Contact import + review inbox + screened numbers (JS-driven). |

**Signed-in landing is adaptive**: not-live tenant → `/setup`, live tenant → `/dashboard`.

### 2.3 Design system (`static/`)
Token-driven, **no build step**. Two layers:
- **`tokens.css`** — shared `trades_core` tokens (synced from upstream
  `trades_core/static/tokens.css` via `sync.py` — **edit upstream, not in place**).
  Identical across RingBack + JobMagnet.
- **`ui.css`** — RingBack's canonical component layer; imports tokens, adds the only
  divergent family (Safety Orange accent) + `--mono`. Rule: *no arbitrary hex/px in
  components*.
- `app.css` (product shell/screens), `app.js` (vanilla glue), `marketing.css`,
  `onboarding.css`, `auth.css`, `setup.css`, `assistant.css`/`.js` (the orb),
  `motion.css`/`.js` (reveals + mega-menu). `ui-gallery.css` powers `/ui` (a
  component gallery — **publicly reachable, unauthenticated**).

**Tokens:** ink `#0B0E14` / soft `#4A5160` / faint `#8A91A0`; bg `#FAFAF8`,
surface `#FFFFFF`, surface-2 `#F1F1EE`; border `#E6E7EB`. Status success `#15803D`,
warning `#B45309`, danger `#B91C1C`, star (amber) `#F59E0B` (**stars only**). Accent
ramp `#EA580C`/hover `#D24E0A`/active `#B8440A`/strong `#C2410C` (AA link)/ink white/
bg `#FCEBDD`/ring `rgba(234,88,12,.28)`. Type: **Archivo** + `--mono`; scale xs .75 →
5xl 4.5rem. Spacing 4px base. **Radius 8px** (note: brand DNA says "radius 0" — the
*intent* is sharp, the *implementation* drifted to 8px — reconcile). Shadows very
subtle, cards default `box-shadow:none`.

**Component library (`ui.css`):** button (primary/secondary/ghost + sm/block, full
`.is-*` state mirrors for the gallery), field, pill (new/booked/urgent/warning/
neutral), card (head/body/foot, `:target` ring for deep-links), data-table, stat-tile,
**chat-bubble** (the hero component: ink agent / surface customer), empty-state, stars.
**Gaps:** no modal/dialog, no toast/snackbar; `--radius-md` and `--space-5` are
*referenced but undefined* (latent bugs); motion timing is hardcoded literals
(`.12s`/`.55s`) except in `assistant.css` which has proper `--dur-*`/`--ease-*` tokens.

**Motion (built):** scroll reveals (IntersectionObserver, reduced-motion aware),
Firecrawl-style mega-menu dropdown, count-ups. **Strong a11y baseline**: focus-visible
rings, keyboard-operable rows, `prefers-reduced-motion` honored in CSS *and* JS.
**Not built** (from the docs): the 3D "Ringback Signal" hero, the animated/looping
SMS→Booked thread, Canvas dot-grid (it's a flat CSS texture), wall-of-love marquee,
View Transitions, tokenized motion.

### 2.4 UX flows & friction (biggest funnel/UX leaks)
- **Public "Live demo" CTA dead-ends at a login wall** — `/simulator` is
  `@login_required` but marketing links it publicly. Highest-impact funnel fix.
- **"Forgot password?" → `/contact`** — no real reset flow.
- **Signup → `/setup` immediately**, straight into a heavy EIN/business-address ask
  (A2P intake) with no welcome / "explore first" path.
- `/ui` gallery is unauthenticated. A2P step is a multi-hour wait with only manual
  re-check. JS-driven screens (callers/analytics/calendar) have weak no-JS fallbacks
  (vs. `/setup` which is carefully zero-JS-resilient).
- **Two "homes" overlap** (`/dashboard` command center vs `/pipeline` cockpit) with
  the relationship explained only in prose.

### 2.5 Brand DNA & product vision
- **Audience:** tradesman 35–55, 1–5 employees, $300K–$1.5M, "cash-positive but
  time-bankrupt." He doesn't want a CRM — "he wants Tuesday to fill up."
- **Positioning:** the inverse of Angi/HomeAdvisor — *"We don't sell your leads. We
  don't share your customers. We don't text anyone you haven't approved."* Category
  white space: everyone builds AI-for-the-caller; RingBack builds **AI-for-the-owner**.
- **Voice = "Vic":** blue-collar fluent, short sentences, leads with money ("Missed
  call, burst-pipe lady — could be $2k"). Banned: leverage/optimize/utilize, emoji,
  streaks, "Great question!".
- **The "Vic" vision (BRAIN.md):** reframe `/dashboard` from a dashboard you operate
  into an employee you delegate to — a **proactive feed of money-ranked tap-action
  cards**, chat as the power escape hatch, push-to-talk voice. 5 signature delight
  moments (Morning Briefing, "It Just Sent", The Win, The 5-Star, The Catch). Delight
  discipline: **never confetti — the delight is the *ratio* of done-for-him vs.
  done-by-him.**
- **Refs that exist:** `refs/motion/` (7 competitor section-by-section `.mov`
  recordings, undocumented), `research/screenshots/` (8 competitor PNGs:
  gohighlevel, housecallpro, leadtruffle, newo, nextphone, rosie, trillet, upfirst),
  `research/design-for-ai/` (the full design-for-ai plugin: `ai-tells.md`,
  checklists, OKLCH palette generator). The Firecrawl aesthetic is the locked visual
  target (`UI/reference-library/firecrawl/`).

### 2.6 What drives each surface (data bindings)
`/dashboard` ← `convos.digest`, `connections.golive_summary`, `assistant.suggestions`;
chat POSTs `/assistant`. `/pipeline` ← `db.leads_with_stage`, `list_appointments`,
stats, `recent_alerts`, `recent_screened_calls`, `screening_stats`. `/setup` ←
`connections.step_state/blockers/is_live/recommended_setup`, `compliance.a2p_status`,
`db.last_inbound_call` (live is only true after a real test call engaged). `/settings`
← the `businesses` row. `/simulator` → shared `open_conversation`/`handle_inbound`
engine (simulated and real look identical). Runtime truth checks the UI must reflect:
`messaging.configured()`, `mail.configured()`, `google_cal.is_connected()`,
`llm.active_provider()`.

---

## 3. design_hub skill catalog (the upgrade toolkit)

### UI system (`UI/`) — orchestrator `ui-build`, 9-phase pipeline
"Design freely, ship safely." Static-first, tokens-never-literals, two tiers
(marketing = expressive incl. 3D/ambient; **product/dashboard = restrained, never
bring splashy motion in**), anti-AI-look, `ui-audit` is the hard ship gate.

| Skill | Use it to… |
|---|---|
| **ui-build** | Orchestrate the whole marketing-site upgrade / any "make this premium" page. Entry point. |
| **ui-foundations** | Formalize/audit RingBack's Safety-Orange tokens as the single source of truth (ships `tokens.css` + `contrast.py`). Do first. |
| **ui-references** | **Mobbin integration point.** Curate 2–4 consistent premium refs per section; works even before the library fills. |
| **ui-layout** | Build/restructure the static skeleton of the hollow homepage sections + marketing pages. |
| **ui-hero** | Upgrade hero composition/hierarchy/media; owns the static-vs-ambient-vs-WebGL escalation call. |
| **ui-hero-3d** | Only if RingBack commits to the cinematic textured-phone/particle hero (marketing tier only). |
| **ui-card** | Feature/pricing/stat cards so they don't read as bordered divs. |
| **ui-background** | Settle the particle layer so it frames rather than competes; quiet section atmosphere. |
| **ui-motion** | Cohesive reveal cadence + micro-interactions after the skeleton is solid. |
| **ui-polish** | Taste finishing pass — hunt and kill AI tells before the gate. |
| **ui-assets** | Only if a generated hero backdrop / 3D object / frame-scrub is needed (Higgsfield/Kling/ffmpeg). |
| **ui-audit** | Mandatory final gate: contrast, semantics, keyboard, reduced-motion, zero-CLS, token-only, responsive + the "$10k rubric" grade. |

⚠️ `UI/reference-library/` is **near-empty** (6 firecrawl PNGs, MANIFEST blank).
Each entry must record a one-line "design toward this" note. This is what Mobbin fills.

### UX system (`UX/`) — orchestrator `ux-design`, design-thinking loop
Usable · Equitable · Enjoyable · Useful. Structure before surface. **For an existing
product, the on-ramp is `ux-usability` (test current app) + `ux-ideate` competitive
audit**, not a cold start from research.

| Skill | Use it to… |
|---|---|
| **ux-design** | Orchestrate the redesign loop, scale rigor, hand off to `ui-build`. |
| **ux-research** | Pick KPIs (drop-off, conversion, time-on-task) to measure the redesign against. |
| **ux-personas** | Map who RingBack serves + emotional journey + current pain points. |
| **ux-define** | Lock the single problem the redesign solves + success benchmark. |
| **ux-ideate** | **Competitive audit — the other Mobbin seam.** Generate IA/flow options. |
| **ux-flows** | Validate RingBack IA/navigation/flows (happy + edge paths) before visuals. |
| **ux-wireframes** | Rough screens structurally (no color) so layout is right first. |
| **ux-prototypes** | Wire flows into a testable prototype; state motion intent for UI. |
| **ux-usability** | Test current app, prioritize fixes P0/P1/P2. **Best first step.** |
| **ux-accessibility** | Threads every phase; sets reading order / a11y intent for handoff. |
| **ux-design-systems** | Produce the sticker sheet that becomes UI tokens (the UX→UI bridge). |

Handoff: sticker sheet → `ui-foundations`; wireframes/flows → `ui-layout`; motion
intent → `ui-motion`; a11y findings → `ui-audit`; assembled under `ui-build`.

### CANVA system (`CANVA/`) — orchestrator `canva-build`
Parallel marketing/client-artwork pipeline (Higgsfield makes imagery → Canva
composes; `content-design` supplies copy). Skills: `canva-build`, `canva-brand-setup`,
`canva-social-post`, `canva-ad`, `canva-carousel`, `canva-client-doc` (Heritage only),
`canva-export` (ship gate). **Relevance to the web upgrade:** it holds the
authoritative RingBack brand kit (`brands/ringback.md`) and the locked "Firecrawl-style
AI-tech, no people" visual direction (`reference-library/ringback-imagery/firecrawl-style/`).
Hard limits: Canva API **can't set font family** (brand font needs a UI-built master);
`generate-design` is draft-only. Nothing is live in Canva yet (kit not configured).

### BACKEND system (`BACKEND/`) — orchestrator `be-build`
Python-first (FastAPI/Django + Postgres). Creed: *"a backend's first job is to never
lie and never lose data."* Skills: `be-build`, `be-data-model`, `be-api`, `be-auth`,
`be-data-layer`, `be-integrations`, **`be-state`** (the UX↔backend seam — owns the
loading/empty/error/success state matrix, hands it to `ui-build`), `be-audit` (ship
gate, P0/P1/P2). Not yet exercised on RingBack (RingBack is its own pre-existing Flask
app, not built through this system). `be-state` is the most upgrade-relevant — it's
the formal owner of the missing-states gap the UI agents flagged.

---

## 4. Recommended upgrade sequence (for when Mobbin is connected)

1. **Fix the skill wiring** (§5) — otherwise none of the above is invokable.
2. **Capture Mobbin references** into `UI/reference-library/<category>/` with
   "design toward this" notes (via `ui-references`), and feed them to a `ux-ideate`
   competitive audit.
3. **Decide scope on `/dashboard`**: visual reskin (REVAMP) vs. the Vic rebuild
   (BRAIN). These conflict; pick one before touching it.
4. **`ux-usability` pass** on the current live app to find the real P0s (the funnel
   leaks in §2.4 are prime candidates).
5. **`ui-foundations`**: reconcile tokens — make `DESIGN.md` truthful (orange, not
   blue), settle radius 0-vs-8, tokenize motion/z-index/breakpoints, add the missing
   `--radius-md`/`--space-5`, add a toast + modal component.
6. **`ui-build` per surface**, tier-aware (marketing expressive, product restrained),
   honoring the honesty-UI ethos. **`ui-audit` gate** every change.

---

## 5. ⚠️ Blocker: the skill symlinks are broken (verified 2026-06-16)

`~/.claude/skills/` has 40 entries; **only 9 resolve, 31 dangle.** They point at
`~/UI/...`, `~/UX/...`, `~/design_hub/BACKEND/...` — but `~/UI`, `~/UX`, `~/BACKEND`,
`~/CANVA`, `~/design_hub` **do not exist**. The repo was moved to `~/apps/design_hub`
after `install.sh` last ran (2026-06-15). Every skill's `~/`-rooted cross-reference
(`~/UI/PRINCIPLES.md`, etc.) is also dangling.

**Fix (not yet applied — needs your OK):**
- Re-create compat symlinks: `~/UI`, `~/UX`, `~/BACKEND`, `~/CANVA` → the matching
  `~/apps/design_hub/<dir>` (README only documents `~/UI`/`~/UX`; `~/BACKEND`/`~/CANVA`
  are referenced by skills but undocumented — likely a README omission).
- Re-run all four `install.sh` so `~/.claude/skills/` links resolve, then restart
  Claude Code to re-scan.

Until this is done, `ui-build`/`ux-design`/`be-build` and their sub-skills can't be
invoked.

---

## 6. Cross-cutting cleanups the agents flagged
- **`DESIGN.md` is stale** (documents dead "Field Blue") — rewrite to the orange
  system or it will mislead any redesign. Same stale-blue references linger in
  `CANVA/README.md`, `CANVA_TECHNIQUES.md §3`, `canva-ad/ad-anatomy.md`.
- **Delete dead `landing.html` / `base.html` / `style.css`** (carry false claims).
- **Reconcile the two RingBack visual directions** (Firecrawl "no people" vs. the
  contractor-photo post-pack master).
- **Two roadmaps** (REVAMP vs. BRAIN/Command-Center) — clarify which governs `/dashboard`.
