# Call Screening — the "Phone Screen"

FirstBack texts back every missed caller. Two callers should **not** get that automated
text: **spam/robocalls** (wastes texts and gets the business number carrier-flagged) and
**people you already know** (an automated "want a free estimate?" to a returning customer
or the owner's mom is embarrassing). The screen decides — modeled on how a phone screens
calls, but tuned so it never silences a real customer.

Shipped 2026-06-16. Live (`main` → Render). Default mode is **monitor** (safe).

---

## The decision, cheapest-first (`triage.screen_caller`)

One verdict — `{engage, status, score (0-100), category, reasons[]}` — evaluated in order,
stopping at the first confident answer:

1. **Identity (free):** opted-out (STOP) → never text. Known non-prospect tag
   (personal/vendor/blocked) → screen out.
2. **Known/trusted, AUTO-DERIVED (free):** anyone with a **booked estimate** or a directory
   entry is "known" → passed to the owner, **no bot** (faithful-Apple). This set builds
   itself from bookings — **no contact import required** (`db.is_known_caller`).
3. **Free hot-path spam signals:** STIR/SHAKEN attestation (`StirVerstat` off the Twilio
   webhook), neighbor-spoofing (caller fakes your area code+prefix), repeat-call-never-
   replied behavior.
4. **Paid reputation (gated, off by default):** Twilio Lookup line-type + Nomorobo/Hiya
   robocall score — consulted only for ambiguous unknowns, cached, fail-open
   (`reputation.py`).
5. **Crowdsourced cross-tenant ledger:** when one business marks a number spam, it helps
   pre-screen that number for everyone (a privacy-safe count only — `db.global_spam_count`).
6. **AI content screen (Tier 3, gated):** classifies a caller's first reply (real homeowner
   vs sales/survey/robocall) and bails mid-conversation (`ai.classify_intent`).

**Precision-first:** no single weak signal hard-screens. `score ≥ HARD (80)` → screened;
`MID (45) ≤ score < HARD` → still **texted** but flagged `review`; `< MID` → normal prospect.
Any error/timeout fails **open** (engages), because silencing a real customer is the one
costly mistake.

`status` ∈ `opted_out | screened_contact | trusted | screened_spam | review | prospect`.

---

## Rollout modes (safe cutover)

`FIRSTBACK_SCREEN_MODE` = `off | monitor | enforce` — the **app-wide default** (default
`monitor`). Each business can override it in **Settings → Call screening** ("Use the
default" inherits; stored in `businesses.screen_mode`, resolved by
`app._effective_screen_mode`).

- **off** — every missed caller gets the text-back (instant rollback).
- **monitor** — computes + LOGS each verdict but still texts everyone. The dashboard shows a
  banner + a "Would screen" stat + the candidate list, so the owner can confirm precision
  before it can silence anyone.
- **enforce** — acts on the verdict (spam/known callers are not texted).

To go from observing to blocking: review the "Would screen" numbers on the dashboard, then
set **Enforce** (per-business in Settings, or `FIRSTBACK_SCREEN_MODE=enforce` app-wide).

---

## Owner controls (UI)

- **Dashboard** (`/pipeline`): a "Calls screened" / "Would screen" stat tile, a monitor-mode
  banner, and a screened-calls strip showing each flagged caller with the spam score +
  reasons. "Text them back" overrides an enforced screen; "Mark spam" confirms a monitor
  candidate.
- **Conversation panel**: a **"Mark as spam"** button on any open lead →
  `POST /api/leads/<id>/flag-spam` (blocks the number + feeds the crowdsource ledger).
- **Screened-calls strip**: `POST /api/calls/<id>/flag-spam` and `/engage`.
- **Simulator** (`/simulator`): "Simulate a spam call" / "Simulate a known caller" buttons
  that show the screen in action (no lead created) — also the marketing demo.
- **Settings → Call screening**: the per-business mode selector + honest status of the
  optional paid reputation and AI-content add-ons.

---

## Marketing surfaces (lead with "Knows who to text")

- Homepage (`onboarding.html`): hero value strip — *Texts real customers · Screens spam &
  robocalls · Skips contacts you know*.
- `/product`: a "Smart screening — It knows who to text" feature card linking to the demo.
- `/pricing`: FAQ — "Will it text spam callers or my existing customers?"

---

## Files

| File | Role |
|------|------|
| `triage.py` | `screen_caller` (the tiered verdict) + pure `spam_score` + `neighbor_spoof`. |
| `reputation.py` | Gated paid robocall-reputation seam (Twilio/Nomorobo, Hiya); cached, fail-open. |
| `ai.py` | `classify_intent` (Tier-3 content screen; fail-open to "prospect"). |
| `db.py` | `is_known_caller`, `screening_stats`, `recent_screened_calls`, reputation cache, `spam_flags` + `global_spam_count`, `set_screen_mode`, `calls.screen_status/spam_score/screen_reasons/screen_mode`, `businesses.screen_mode`. |
| `app.py` | `_screen_missed_caller`, `_missed_call_textback` (mode-aware hot path), `_effective_screen_mode`, the `flag-spam` routes. |
| `config.py` | `SCREEN_MODE`, thresholds, `REPUTATION_PROVIDER`, `SCREEN_AI_CONTENT`. |
| templates/static | dashboard, settings, simulator, product, onboarding, pricing + `app.js`/CSS. |

## Operating knobs (env; see SETUP_NEEDED.md)

- `FIRSTBACK_SCREEN_MODE` (off|monitor|enforce, default monitor) · `FIRSTBACK_SCREEN_HARD` (80) ·
  `FIRSTBACK_SCREEN_MID` (45) · `FIRSTBACK_SCREEN_CROWD_MIN` (2).
- `FIRSTBACK_REPUTATION_PROVIDER` (off|twilio_nomorobo|hiya) + `HIYA_API_KEY` — optional paid tier.
- `FIRSTBACK_SCREEN_AI` (1 to enable the Tier-3 AI content screen; needs a real brain key).

## Tests

`test_screening.py` (57) + `test_reputation.py` (11); full standalone suite **278 passing**.
Run: `for t in test_*.py; do python3 "$t"; done`.

## Status / not-yet

- Screening, rollout modes, per-business toggle, mark-spam, simulator demos, marketing — all
  **live and real**. Default **monitor**, so spam is not actually blocked until a business
  picks Enforce.
- Paid reputation (Tier 2) and AI content screen (Tier 3) are **gated/off** until configured.
