# Phase 6 · Slice 1 — "Vic, the hub: connections" (design)

**Date:** 2026-06-17 · **Branch:** `staging` · **Status:** approved, building.

> Read `BRAIN.md` (north star), `HANDOFF.md` (state), and the `firstback-vic-hub-vision`
> memory first. This is the first slice of Phase 6 "Vic, the hub."

## Decisions made (the brainstorm)
1. **Build order:** Phase 6 first, locally on the `staging` branch (the real Claude brain
   works locally). Deploy `firstbackv2` *after*.
2. **First slice:** Pillar B (connections) — the biggest wall for a brand-new owner.
3. **In-chat depth:** *initiate + inline card + confirm*. Vic surfaces a real action inline,
   pre-filled, explains why, and confirms status after — it does not rebuild write paths.
4. **Architecture:** the **premium-weighted hybrid** (below), chosen after a 3-agent debate
   (two adversarial advocates + an independent referee). The debate converged: for 5 of 6
   connection steps the final tap is *irreducible*, so card-surfacing is correct; the one step
   where Vic can genuinely "do it" honestly is the business profile.
5. **Profile-write timing:** the gated profile-write ships **in slice 1** (matches the
   $200/mo "don't hold back" bar; it's safe — pure data, no money, instantly reversible).

## Scope
**Is:** connect Google Calendar/Contacts and run the go-live flow (profile → number → A2P →
forwarding) by talking to Vic; reactive + the existing go-live nudge.
**Isn't:** the full first-run chaperone (Pillar C, later), talk-to-configure for every setting
(Pillar A, later), and conversational collection for fields beyond the profile.

## Architecture — the premium-weighted hybrid

### Card-surfacing (non-gated; the user's tap IS the existing audited route)
Each surfaces a `connect_action` card (pre-filled, with Vic's one-line "why" and a live status
badge) pointing at a route that already exists and is already audited/tested:

| Concern | Route | Irreducible tap |
|---|---|---|
| Google Calendar | `/api/calendar/google/connect` | the Google "Allow" consent screen |
| Google Contacts | `/api/contacts/google/connect` | the Google "Allow" consent screen |
| Number / A2P / forwarding | `/setup` (+ `/setup/number`, `/setup/a2p`, `/setup/forwarding`) | money-spend approve; operator concierge; star-code dialed on the phone |

Go-live status/step is read through the **existing** `connections.golive_summary(biz,
sms_configured=messaging.configured())` and surfaced via the **existing** `_golive_card` —
upgraded to emit an actionable, pre-filled `connect_action` card for the owner's *current*
step. Reusing this single source of truth is how chat and the `/setup` wizard can never
disagree (the referee's #1 risk, neutralized by construction).

### Vic-owned gated write (the one place "Vic does it" is honest)
`set_profile` — params: `name`, `ein`, `business_address`, `trade`, `owner_name`.
`confirm: True`, modeled exactly on `set_scheduling`. Pre-fills from the business record, asks
only for what's missing. `_confirm_summary` shows the **EIN + address verbatim** before commit.
`execute()` calls the **same** `db.update_business` + `db.update_a2p_profile` the
`/setup/profile` route calls (app.py) — no reimplementation.

## New card type (leaf — `static/assistant.js`)
`connect_action`: `{ type:"connect_action", title, note, href, cta_label, prefill?, status }`
where `status ∈ {done, current, waiting, todo}` and `prefill` may carry area code / available
numbers / a star-code + `tel:` link. One new branch in `renderCard`, following the existing
`link` card pattern. Keyboard-focusable, ≥48px target, status word never conveyed by color
alone (sr-only label), zero-JS fallback readable.

## Honesty invariants (non-negotiable)
- Number-buy card reads test/live from `messaging.configured()`; on staging (Twilio off) it
  says **"Test mode — not bought for real yet,"** never "I bought you a number."
- `set_profile` shows EIN/address at the confirm — the gate is the deliberate "press go."
- Forwarding card shows the **exact** carrier star-code from `connections.forwarding_code()`
  + `tel:` tap-to-dial; the DB `forwarding_confirmed` flag is set only by the existing route
  *after* the owner dials. Vic never marks it done preemptively.
- Every status read flows through `golive_summary(...)` / `recommended_setup(...)` with the
  same `sms_configured` the route uses.

## Contracts preserved
`run()/run_stream()/execute()` → `{reply, cards, pending_action, meta}`; `set_profile`
registers `confirm:True`, runs only via `execute()`, streaming never bypasses it;
`golive_summary` shape untouched; `_route_topic` keyword contract intact; standalone test
harness (no pytest).

## File ownership (hot-file discipline)
- **`assistant.py`** (one writer): `set_profile` handler + `_confirm_summary` branch + TOOLS
  entries + connect handlers + `_golive_card` upgrade.
- **`db.py` / `app.py`:** expected **zero** changes — `update_business`, `update_a2p_profile`,
  `recommended_setup`, the routes all already exist. (If a helper is genuinely missing it gets
  added to `db.py` serially.)
- **Leaf (parallel-safe):** `static/assistant.js` (+ maybe `static/assistant.css`) for the
  card; the new test file.

## Tests (standalone scripts, `.venv/bin/python`, no pytest; keep 15→16 green)
New `test_connect_hub.py`:
- calendar/contacts → `connect_action` card pointing at the right route, status reflects
  `recommended_setup`.
- go-live → card pre-fills area code (`default_area_code`) + current step from `golive_summary`;
  number card test/live badge is honest when `messaging.configured()` is False.
- forwarding → card carries the exact star-code from `forwarding_code()` + a `tel:` link.
- `set_profile`: `_confirm_summary` shows EIN + address; `execute()` writes via
  `update_business` + `update_a2p_profile`; pre-fill skips known fields; the gate is never
  auto-executed; tenant-scoped.
- cross-surface: chat's current go-live step == the route's `step_state` current (mock
  `messaging.configured()` False).

## Out of scope / deferred to later slices
- First-run chaperone (Pillar C); talk-to-configure settings (Pillar A).
- Vic-owned number-buy / A2P-submit gated writes — stay on the audited form *until* staging
  has Twilio configured AND the confirm previews cost (honest "lead with money"); revisit then.
- Forwarding-confirm write — stays an irreducible user tap, permanently.
