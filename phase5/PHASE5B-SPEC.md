# Phase 5b — F11 "Vic" Proactive (BUILD SPEC, LOCKED)

**Date:** 2026-06-18 · Opus orchestrator · Base: `staging` @ b6810ff (47/47 green, after 5a).
**Source:** `autonomy-plans/F11-FINAL.md` §1–§5 (P1 list), PREBUILD-SYNTHESIS gates.
**Goal:** Vic comes to Dave. The briefing reaches the lock screen; the day is pre-sorted; the
one-tap is genuinely one tap. **Built as 3 file-disjoint slices (ALPHA/BETA/GAMMA).**

## NON-NEGOTIABLE HONESTY / CONSENT GATES (every slice)
- **No silent customer sends, ever.** All proactive output (morning digest, stall nudge) goes
  to the **OWNER's own cell** (`alerts.notify` → `send_sms(..., gate=False)`, A2P-exempt). A
  proactive push is a **notification, not a send.** A text to a customer ALWAYS requires Dave's
  tap on a 5a server-bound token.
- **Don't claim a channel we don't have.** There is NO push/deep-link/magic-link infra. The
  owner SMS must say "open FirstBack" — NEVER "tap here to send." The in-app briefing chip is
  where the one-tap lives. (SMS-deep-link one-tap is DEFERRED — see bottom.)
- **One per morning.** The 7 AM digest is ONE consolidated SMS per business per day, not
  per-event. Stall nudge dedupes per lead per day.
- **Never guess a recipient** (P1-4): a bare referent with nothing shown → ASK.
- **Never silence a real caller without a double-confirm** (P1-6).
- Degradation ("Vic's resting", P1-5) is informative, not alarming; briefing + one-tap still work.

## SHARED SEAMS / FILE OWNERSHIP (no collisions — clean 3-way)
| File | Owner | Notes |
|---|---|---|
| `db.py` | **ALPHA only** | new read helpers (warm-idle, last-activity); no `businesses` migration |
| `alerts.py` | **ALPHA only** | briefing tail in `notify()`; new kinds `vic_morning`/`vic_stall` |
| `reminders.py` | **ALPHA only** | new ticker scans, wired into `tick_once` |
| `assistant.py` | **BETA only** | referential guard + one-tap draft |
| `app.py` | **GAMMA only** | vic_status surface + enforce 2nd-ack in `assistant_confirm` |
| `static/assistant.js` | **GAMMA only** | status pill + enforce two-stage |
| `templates/*` | **GAMMA only** | status pill markup if needed |
| `test_vic_proactive.py` / `test_vic_guard.py` / `test_vic_surface.py` | A / B / G | one each |

**Cross-slice contracts (read-only, no write seam):** ALPHA lazy-`import assistant` inside the
function (avoid any import-order issue) and calls `assistant.briefing(business)` (pure read).
GAMMA reads `allow_llm` (already in `app.py`) and the stored token row (5a, in `app.py`). No two
slices write the same symbol. Each agent runs the FULL suite before committing.

---

## SLICE ALPHA — Proactive push engine (`alerts.py`, `reminders.py`, `db.py`)

**P1-1 Briefing-enhanced alert SMS.** In `alerts.notify()` (alerts.py), for `kind in ("lead",
"booking")`, compute a one-line briefing tail and append to the body. Implementation: a private
`_briefing_tail(business)` that lazy-imports assistant, calls `assistant.briefing(business)`, and
returns the headline (e.g. "3 open, ~$7,200 on the table.") — empty string on quiet/empty/any
exception (never break an alert). Append in `notify()` so the existing `format_message` stays
pure; **cap the full SMS body at 320 chars** (truncate the tail, not the core event). The core
event line ("New lead: Mike…") must always survive.

**P1-2 Morning digest (7 AM local).** New `scan_morning_briefing(now)` in reminders.py, called
from `tick_once` (after `run_due_once`, wrapped in try/except like the other scans). For each
business: compute local time via `_biz_tz`; fire ONLY when local hour ∈ [7, 10) AND the briefing
has actionable items (`tone == "active"` and non-empty `items`); dedupe ONE per business per
local day via `alerts.notify(business, "vic_morning", {...})` with a day-stamped dedupe key
(`vic_morning` + local YYYY-MM-DD — extend `_dedupe_key` for the new kind). Body (honest, no
"tap to send"): "{N} leads need you{, ~$X on the table}. {hottest item}. Open FirstBack." Owner
cell only.

**P1-3 Warm-lead stall nudge.** New `scan_stall_nudges(now)` in reminders.py, wired into
`tick_once`. For each business, find warm leads (stage=="warm", not urgent) whose last activity
is > 24h old (add `db.warm_leads_idle(bid, hours)` or compute from `leads_with_stage` + last
inbound/outbound message time). Fire `alerts.notify(business, "vic_stall", ctx)`, dedupe per lead
per local day (dedupe key includes lead_id + day). > 48h → urgent tone in the copy. Body: "{Name}
replied {Nh} ago and is still waiting{ — ~$X on the table}. Open FirstBack to text them back."
Owner cell only.

**alerts.py registration:** add `vic_morning`, `vic_stall` to `ALERT_KINDS`; map both in
`_TOGGLE_COL` to an existing column (`alert_on_lead`) so they inherit a toggle Dave understands —
NO new `businesses` column / migration. Add `format_message` branches + `_subject` entries.
`_dedupe_key`: day-stamp `vic_morning`/`vic_stall` (and lead_id for stall).

**ALPHA tests (`test_vic_proactive.py`, standalone, real DB, send-counter):**
1. lead/booking alert body carries the briefing tail; body ≤ 320 chars; core event line intact.
2. morning digest fires once in-window with actionable items; second tick same day = no dup;
   quiet/empty briefing = nothing sent; outside [7,10) = nothing sent.
3. stall nudge fires for a >24h warm lead, dedupes same day, escalates tone at >48h; a fresh
   (<24h) warm lead and a non-warm lead get nothing.
4. **all proactive sends go to the owner's `alert_sms` cell, never to a lead's number** (assert
   recipient == owner cell; zero sends to any consumer number).

---

## SLICE BETA — Assistant guards + genuine one-tap (`assistant.py` ONLY)

**P1-4 Referential ambiguity guard.** In `run()` (assistant.py:~1985) and the gated branch of
`_tool_loop`, BEFORE `_gated`: if `tool in _LEAD_TOOLS` AND `_is_referential(message)` AND NOT
`entities` AND no name/phone present in the message → return
`_say("Which lead? Tell me a name, or say \"list my leads\" first.")`. **Must not break "text my
last lead …"** — verified: "last lead" is NOT in `_is_referential`'s token sets, so it routes via
the most-recent fallback as today. Only bare pronouns/`the last one`/`that one`/ordinals with an
empty `entities` list ask.

**One-tap draft (the F11 kicker — genuine one-tap).** Today `_gated` for `text_lead` with no body
returns "ask what to say" (no token). Change: when `text_lead` is gated with **no message** but a
**single unambiguous target resolves** (`_resolve_lead_target` returns a lead AND the request
named/pinned that lead, i.e. NOT the bare most-recent fallback when the owner gave no referent —
keep asking when truly ambiguous), propose a **deterministic default draft** body so the confirm
card is one-tap. Add `_default_draft(business, lead)` → a short, honest template, e.g.
"Hi {first}, it's {owner} with {business} — saw I missed your call. When's a good time to talk?"
Then mint the token + preview as normal (owner edits/approves on the card). Keep the textarea
editable. **Existing tests must stay green** — "text X saying Y" still uses Y; the ambiguous
"text" with no target still asks.

**BETA tests (`test_vic_guard.py`):**
1. referential + empty entities → asks, no token, no pending send (per `_LEAD_TOOLS`).
2. "text my last lead saying running late" still resolves to the most-recent lead + mints a token
   (regression guard).
3. a briefing-style "text {Name} back" (named target, no body) now yields a confirm WITH a
   non-empty draft body + a token_id (genuine one-tap), targeting the right lead.
4. the draft is editable/overridable (the 5a body-override path still applies).

---

## SLICE GAMMA — Surfaces (`app.py`, `static/assistant.js`, `templates/`)

**P1-5 "Vic's resting" surface.** In `assistant_chat` and the stream `done` frame, when
`allow_llm` is False (budget/no-key), set `out["vic_status"]="resting"` and
`out["resets_at"]=<next local midnight ISO>` (use `_biz_tz`/biz tz; the daily cap resets at the
tenant's local day boundary). Client: a small status pill — "Vic's resting — briefing and one-tap
still work. Back to full power tomorrow." Not an error tint. (Degradation already exists; this only
surfaces it.) Do NOT set it when `allow_llm` is True.

**P1-6 Enforce-mode second acknowledgment.** In `assistant_confirm` (the 5a endpoint), parse the
stored `tool`+`args` from the row **immediately after lookup** (move the parse up; reuse for both
the gate and execution). BEFORE the atomic `claim_confirm_token`: if `tool=="set_screen_mode"` and
stored `args.get("mode")=="enforce"` and `request.form.get("enforce_ack")!="true"` → return a 200
warning reply ("This silences real callers — they get no text back. Tap again to confirm.")
WITHOUT claiming (token stays valid for the second tap). With `enforce_ack=="true"` → proceed
through the normal 5a claim+execute path. Client `runAction`: when `pending.tool=="set_screen_mode"`
and `pending.args.mode=="enforce"`, the first Send tap shows a warning state; the second tap
re-POSTs with `enforce_ack=true`. **All existing 5a confirm behavior must be preserved** (token-
only redemption, idempotent replay, expiry, cross-tenant, text_lead body override). Re-run
`test_confirm_token.py` + `test_assistant.py`.

**GAMMA tests (`test_vic_surface.py`):**
1. `allow_llm=False` turn → response JSON carries `vic_status=="resting"` + a parseable
   `resets_at`; an `allow_llm=True` turn does NOT.
2. enforce confirm: propose `set_screen_mode=enforce` (mint token) → first redeem WITHOUT
   `enforce_ack` → warning reply, token NOT consumed (still redeemable); second redeem WITH
   `enforce_ack=true` → executes; a non-enforce confirm (text_lead) is unaffected (no ack needed).
3. regression: a plain 5a text_lead token still redeems once (no enforce gate interference).

---

## DEFERRED (with honest reason — record in NEXT-SESSION)
- **SMS-deep-link "tap the text to send"** — requires push/magic-link auth infra (its own
  security review). 5b drives the owner SMS → the in-app one-tap instead. F11 §1's "pre-issued
  token in the push payload" assumed that channel; the in-app briefing chip mints a fresh token
  at tap (10-min TTL makes pre-issuing-for-later moot without a deep link).
- P2-1 weekly Vic track record, P2-4 conversation summarization, P2-5 voice input — later.

## MERGE ORDER (Opus)
BETA → ALPHA → GAMMA (assistant guards first; ALPHA reads assistant.briefing; GAMMA touches the
5a confirm path last). Full suite green + un-stubbed e2e (a real warm-idle lead drives a stall
nudge to the OWNER cell, not the customer; an enforce confirm needs two taps) + security/consent
pass (no customer sends; owner-cell only; tokens still bound) before commit.
