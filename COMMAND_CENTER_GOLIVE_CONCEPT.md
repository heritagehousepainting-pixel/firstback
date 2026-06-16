# Concept + Handoff — Go-Live in the Command Center

**For:** the design-dev agent.
**Goal:** make "getting live" feel native to the command center (`/dashboard`) **without** moving the
Go-Live wizard into the chat. The wizard at `/setup` stays the workshop (it's static-first, stateful,
resumable, operator-gated, and already hardened with tests). The command center becomes the
**glanceable status + the front door**.

> Decision already made (do not relitigate): we are NOT rebuilding the 4-step form as chat turns.
> Collecting an EIN, address, number-pick, carrier, and an operator SID-paste through free-text is the
> wrong tool. We surface *status + entry*, and deep-link to `/setup` for input.

This handoff covers TWO surfaces. Read the existing wizard brief too: `DESIGN_AGENT_GOLIVE_BRIEF.md`
(separate task — the `/setup` page itself).

---

## The bones you're building on (already in the repo)

`templates/command.html` — the command center:
- `.orb-stage` (the animated Mason orb), `.convo` → `.convo-hero` (`.convo-kicker`, `.convo-hello`,
  `.convo-sub`, and an optional `.convo-digest` line), `#transcript` (JS-populated, `aria-live`),
  and `.command-dock` (chips + the translucent `.command-bar`).
- Loads `static/assistant.css?v=6` and `static/assistant.js?v=7`.
- The command bar `<form>` has **no action/method** — the chat surface is JS-driven by design.

`static/assistant.js` → `renderCard(card, host)` already supports card `type`s:
`stat`, `list`/`plays`, `draft`, `link`, `note`. Cards render into `.a-card` inside a `.cards` wrap.
The assistant currently answers go-live questions with a `link` card → `/setup`
(`assistant.py` `_route_topic`, the "Open Go Live" card).

Design system: `static/tokens.css` + `static/ui.css` + `static/app.css` + `static/assistant.css`.
Rules of the house — `~/apps/design_hub/UI/PRINCIPLES.md`. **Tokens only, never literals. No inline
styles. WCAG AA. Static-first where the surface allows.**

---

## Surface 1 — "Finish setup" hero nudge  (server-rendered, STATIC, no-JS)

A single persistent line in the command hero, shown only while the tenant isn't live. This is the
honest, glanceable "your #1 job" prompt, and because it's server-rendered it is the **no-JS fallback**
path to go-live (the chat card below is the JS enhancement).

Place it in `.convo-hero` in `command.html`, directly after `.convo-sub` (mirror the existing
`{% if digest ... %}<p class="convo-digest">` pattern — same altitude, same restraint).

Three states, driven by the new `golive` context var (contract below):

```
not live        →  ⚡ Finish setup to start catching missed calls — {{golive.done}} of {{golive.total}} done  →   [link to /setup]
live, unverified→  ✓ You're set up — make a test call to confirm forwarding works  →                              [link to /setup]
live + verified →  (render nothing — don't nag a live tenant)
```

ASCII intent (one line, not a big banner — it lives under the hero sub-copy):

```
 Evening, Mike.
 Tell me what you want done. I can pull your numbers, show your leads… work by hand in Pipeline.
 ┌────────────────────────────────────────────────────────────────────────┐
 │ ⚡ Finish setup to start catching missed calls — 2 of 4 done        →    │   ← .convo-setup, links /setup
 └────────────────────────────────────────────────────────────────────────┘
```

Requirements:
- A real `<a href="/setup">` (works without JS). The whole line is the link, or a trailing arrow link.
- The leading glyph is a **decorative inline SVG** (token-colored, `aria-hidden="true"`), not an emoji.
- New class `.convo-setup` (scope it like the existing `.convo-*`), tokens only. Use an attention/accent
  treatment for "not live" and a calmer success-tint for "live, unverified". Must pass AA on the hero bg.
- Never render the line when `golive.live_verified` is true.

---

## Surface 2 — "Go-Live status" chat card  (new card type in the JS surface)

When the contractor asks anything go-live-shaped ("how do I go live", "it's not texting customers",
"am I live yet"), the assistant returns a richer card than today's bare link — a compact status board
that mirrors the wizard's stepper, then deep-links to `/setup`.

Add a new branch to `renderCard` in `assistant.js` for `card.type === "golive"`, plus styles in
`assistant.css`. Compose visually from what already exists (the wizard's stepper language + a primary
`a-btn`), but condensed for the transcript.

Card payload the backend will send (you consume it — see contract):

```jsonc
{
  "type": "golive",
  "status": "not_live" | "setup_complete" | "live",   // headline state
  "done": 2, "total": 4,
  "steps": [                                            // condensed stepper (titles come from the backend)
    {"key":"profile",    "title":"Your business",             "state":"done"},
    {"key":"number",     "title":"Your RingBack number",      "state":"done"},
    {"key":"a2p",        "title":"Carrier registration (A2P)","state":"current"}, // current = first actionable
    {"key":"forwarding", "title":"Forward your missed calls", "state":"todo"}
  ],
  // state ∈ { "done" | "current" | "ready" | "todo" }  (NOTE: no "blocked").
  //   done    = complete · current = the one actionable step to do next
  //   ready   = actionable but not the next-up one (rare; both prereqs met)
  //   todo    = locked until prerequisites are met
  // A stalled/failed A2P does NOT produce a distinct node state — the step simply
  // isn't "done", so it shows as current/ready. Use the `blocker` string for the "why".
  "blocker": "A2P 10DLC registration is not approved yet (status: pending).",  // top plain-English blocker, or null
  "href": "/setup",
  "label": "Open Go Live"
}
```

ASCII intent for the card (inside the transcript, `.a-card`):

```
 ┌──────────────────────────────────────────────┐
 │ Getting you live           ⟨ Almost there ⟩   │  ← title + status pill
 │                                                │
 │  ●──●──◐──○   2 of 4                           │  ← mini-stepper: done done current todo
 │  Business  Number  Carrier  Forwarding         │
 │                                                │
 │  Next: carrier registration is pending.        │  ← blocker line (omit if none)
 │                                                │
 │  [ Open Go Live → ]                            │  ← a-btn primary, href /setup
 └──────────────────────────────────────────────┘
```

Status pill mapping (reuse the existing pill/badge classes; do NOT invent colors):
- `not_live` → attention/neutral pill, text "Not live yet"
- `setup_complete` → caution/accent pill, text "Make a test call" (this is `is_live && !live_verified`)
- `live` → success pill, text "You're live" (only when truly verified)

Mini-stepper states → reuse the wizard's visual vocabulary: `done` = filled/check, `current` = accent
ring, `ready` = actionable-muted, `todo` = muted/locked. (There is no `blocked` state — the `blocker`
string carries the "why".) Convey state with shape/text too, not color alone. Mark the active node
`aria-current="step"`; give the stepper an accessible name ("2 of 4 steps complete"); decorative node
glyphs `aria-hidden`.

---

## Honesty rules (non-negotiable — this is the product's whole ethos)

1. The card's "live" pill and the hero's "you're live" affirmation may render **only** when the backend
   says so — `status === "live"` / `golive.live_verified === true`. Never infer "live" from
   `done === total`. (A tenant can have all 4 steps checked but still not be live if server Twilio
   creds are missing or A2P isn't approved — the backend already accounts for this.)
2. `setup_complete` is a distinct state from `live`: setup is attested but no real test call has been
   texted back yet. Its copy must say "confirm with a test call", not "you're live".
3. If `blocker` is null, omit the blocker line entirely — don't fabricate reassurance.

---

## Data contract (backend provides — you consume; do not write the Python)

Backend changes are OURS to make; you build the template/JS/CSS against this contract. Flag any desync.

**Surface 1 (hero nudge), `dashboard()` route → `command.html` context.** WIRED: the route passes
`golive=connections.golive_summary(biz)` — the **full** summary dict (same one the card uses), so the
hero may read `golive.status`/`golive.steps` directly too:
| var | type | meaning |
|---|---|---|
| `golive.status` | str | `"not_live"` · `"setup_complete"` · `"live"` — drives the 3 hero states |
| `golive.is_live` | bool | all launch blockers clear (server-configured + number + webhooks wired + A2P approved + forwarding) |
| `golive.live_verified` | bool | `is_live` AND a real inbound test call was texted back |
| `golive.done` / `golive.total` | int | completed step count / total (already factors `sms_configured`) |
| `golive.current` | str·null | key of the first actionable step (for deep-link/label) |
| `golive.blocker` | str·null | top plain-English blocker |
| `golive.steps` | list | `[{key, title, state}]` — same condensed stepper the card uses |

**Surface 2 (chat card):** the assistant returns the `golive` card payload shown above from its
go-live route (replacing today's bare `link` card). Same source of truth
(`connections.step_state` / `is_live` / `blockers`).

---

## Acceptance criteria

- `python3 ~/apps/design_hub/UI/skills/ui-audit/scripts/audit.py templates/command.html static/assistant.css`
  → **0 errors / 0 warnings**. Run before and after.
- Zero hardcoded colors/px-for-tokens/rgba literals; zero inline `style=`; every `var(--token)` resolves
  in tokens.css/ui.css.
- Hero nudge is a real server-rendered `<a href="/setup">` and works with JS disabled; it disappears when
  `live_verified`.
- The `golive` card never shows "live" unless `status === "live"`; shows the "make a test call" state for
  `setup_complete`; omits the blocker line when `blocker` is null.
- Mini-stepper conveys state beyond color (shape + text), has `aria-current="step"` on the active node and
  an accessible name; decorative glyphs `aria-hidden="true"`.
- Visually consistent with the command center's existing language (translucent/glass dock, orb palette,
  the wizard's stepper vocabulary). Matches the `/setup` banner states 1:1 so the two surfaces agree.
- No regression to the existing chat cards (`stat`/`list`/`draft`/`link`/`note`) or the orb.

## Files in scope
- `templates/command.html` (hero nudge block)
- `static/assistant.js` (`renderCard` → add `golive` branch; bump the `?v=` cache-buster)
- `static/assistant.css` (`.convo-setup`, `.a-card` go-live styles, mini-stepper, status pill — tokens only)
- **Backend — ALREADY WIRED (do not touch):** `connections.golive_summary()` is the source of truth;
  `app.py dashboard()` passes `golive=...` into the template; `assistant.py` returns the `{type:"golive"}`
  card on go-live questions. Covered by tests in `test_setup.py` (golive_summary 3 states + card shape +
  `/dashboard` renders). You build template/JS/CSS against the live contract — it's already returning real
  data; you do NOT need to write or change any Python.

## Suggested build order
1. Hero nudge (Surface 1) — static, smallest, immediately useful, no-JS safe.
2. `golive` card (Surface 2) — the richer enhancement.
3. Lint, AA-check both states of each surface, confirm the orb + existing cards still render.
