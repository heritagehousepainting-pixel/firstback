# Phase 5 Pre-Build — HONESTY / CONSENT / RISK Lane

**Date:** 2026-06-18  
**Lane:** Planner 2 of 2 — honesty traps, consent/legal risks, quality gates  
**Scope:** Phase 5 features (SF-6, F11, F07, F13, F10, F06, F08) on staging branch ~bfd6ceb  
**Standing rules:** honesty over spin; Dave test; consent-gated one-taps STAY one-tap; never serve at a loss; preserve `ringback-gixe.onrender.com`

---

## THE ONE-TAP RULE (applies across F11 and F13 — read first)

**The consent promise in `assistant.py:637` is load-bearing:** "You approve before anything sends."

Any proactive feature that reaches a customer without a Dave tap breaks this promise and may trigger TCPA exposure. The distinction between "transactional response" (auto-send safe) and "marketing outreach" (must gate) is categorical, not a design preference:

| Category | TCPA posture | Dave gate | Examples |
|---|---|---|---|
| Response to customer-initiated contact | Transactional — auto-send safe | None needed | F06 follow-up (customer texted first), F03 booking brain, reminders |
| Proactive outreach to PAST customers | Marketing — EBR is a risk mitigator, NOT a safe harbor | One tap, batch OK | F13 win-backs, referral asks, review requests |
| Vic customer-facing writes (text_lead, book_estimate) | Customer-facing mutation | One tap per action via confirm token | F11 proactive chips |

**The tap IS the trust guarantee.** Do not remove it to increase automation scores. It is deliberately preserved (AUTONOMY-BLUEPRINT §6). Removing it breaks the product promise and creates real TCPA exposure.

---

## P0 ITEMS — BLOCKERS BEFORE ANY PHASE 5 FEATURE SHIPS TO CUSTOMERS

### P0-A: SF-6 Server-Bound Confirm Token (F11 unblocks, F13 batch release depends on it)

**The honesty trap:** `app.py:582–602` executes `assistant.execute(biz, tool, args)` where `tool` and `args` come from the client POST body. `_clean_args` strips unknown keys but does NOT verify the action was ever proposed or that args match what Dave saw. Same-origin requests with a valid CSRF token can POST `text_lead` with any body to any recipient. "You approve exactly what you saw" is NOT currently true at the server — it is only true in the UI.

**The honest contract the token must enforce:**

1. `_gated()` calls `_issue_token(bid, tool, args)` — INSERT into `pending_confirms(token_id TEXT PK, bid INT, tool TEXT, args_json TEXT, preview_hash TEXT, expires_at REAL, consumed INT DEFAULT 0, result_json TEXT)`. TTL = 10 minutes.
2. `pending_action` in the response carries `{token_id}` ONLY — never `{tool, args}`.
3. `/assistant/confirm` POSTs `{confirm_token, csrf}` ONLY. Server looks up by `token_id`, verifies `bid == current_business().id`, verifies not expired, verifies `consumed == 0`, executes STORED `args_json` (ignores any client-supplied tool/args), marks `consumed = 1`, stores `result_json`.
4. No token → 400. Expired → "This confirm expired — here's the current picture." Second tap same token → returns stored `result_json`, NO re-execution (idempotency).
5. Every confirm adds `token_id` to the `db.add_audit` row.

**Non-idempotent redeem risk:** If consumed flag is not checked before execution, a network retry or double-tap sends the same text twice or double-books. The `consumed = 1` check + returning `result_json` on second tap is mandatory.

**Stale/edited message risk:** A token issued for "Hey Maria, want Thursday at 2?" must bind to those exact stored args. If the client POSTs modified args alongside the token, the server must ignore them and execute what was stored. This is the entire point — the server is the source of truth, not the client.

**Files:** `assistant.py` (`_gated`, new `_issue_token`), `app.py:582–602` (redemption rewrite), `db.py` (new `pending_confirms` table). Size: S (~40 lines). This is Phase 5 item 1 — nothing else in Phase 5 that gates a customer-facing write is honest without it.

---

### P0-B: F13 `growth_mode='auto'` Must Remain Locked Until L2 (7-day streak)

**The honesty trap:** `growth_mode='auto'` sends marketing messages to past customers without Dave's morning tap. This is the single highest TCPA exposure in the product. If a builder exposes the `auto` toggle in Settings UI before the L2 streak-gate ships, contractors will enable it, and FirstBack will send marketing SMS without batch approval.

**The honest rule:** `growth_mode='auto'` is visible in the UI (scoped to review requests only) but locked behind the L2 implementation. The UI label must say "Unlocks after 7 mornings of GO." Never ship the `auto` toggle as an enabled setting. The three-mode design (off/tray/auto) is correct; the `auto` enablement gate is mandatory before it becomes clickable.

**Additional honesty requirement:** The `auto` mode label in Settings must say "Sends review requests automatically — you receive a nightly report" — not "fully automatic." The "you approve everything" promise changes here, and Dave must explicitly see that in plain language before opting in.

---

### P0-C: F10 Voice — Do Not Claim or Activate Until the 7-Check Quality Gate Passes

**The honesty trap:** Mediocre voice (robotic blocks, broken barge-in, 3-second dead air) is actively damaging to Dave's business reputation — worse than no AI at all. A homeowner who hears a robot that pauses for 3 seconds, can't be interrupted, and reads a stilted script will hang up and call the competitor. Dave cannot turn this around.

**The honest rule (AUTONOMY-BLUEPRINT §7 verbatim):** "It must not be sold until streaming (the real build) + premium-voice ear-test + the 7-check quality gate all pass."

**The 7-check gate (F10-FINAL §4.5) — ALL must pass before any tenant activation:**

1. Deploy gate: Render voice service running, `FIRSTBACK_VOICE_URL` set, all 6 env vars correct.
2. End-to-end real call: "call me" → Twilio dials → recording disclosure → 4-turn booking → slot in DB.
3. Streaming gate: First AI response arrives at TTS within 1.5 seconds of homeowner finishing speaking.
4. Barge-in gate: Interrupt the AI mid-sentence. It stops and processes the new utterance.
5. Voicemail gate: Call goes to voicemail → AI does NOT leave a voicemail → SMS fallback fires within 30 seconds.
6. Quiet-hours gate: "call me" after 9 PM → no call placed → correct after-hours text sent.
7. Premium voice gate: ≥4 Twilio ConversationRelay voices auditioned on a trades script; winner sounds warm, not corporate.

**Don't ship until rule:** Until all 7 pass, the pricing page shows voice as "coming soon" (not a checkmark). The `firstback-voice` Render service is currently commented out in `render.yaml:64–81`. S-2 (streaming) must ship before S-6 (quality gate). Do not run the quality gate without streaming — it will fail gates 3 and 4 by design.

**Haiku is not negotiable:** `config.py:46` defaults to `claude-opus-4-8`. The voice service needs `CLAUDE_MODEL=claude-haiku-4-5` as its own Render env var, isolated from the web app. Opus adds 1.5–3 seconds of dead air per turn. That is a failing call quality.

---

## P1 ITEMS — MUST BE IN THE SUB-PHASE SPEC BEFORE BUILD STARTS

### P1-1: F11 Proactive Push — One-Tap is One-Tap (No Silent Sends)

**The honesty trap:** Vic "proactive" could mean two very different things: (a) Vic pushes a notification with a pre-staged confirm chip that requires one tap, or (b) Vic sends the text automatically and tells Dave after. Only (a) is acceptable. The architecture (F11-FINAL §1) is correctly designed for (a), but the risk is a builder mis-reading "proactive" as "sends without a tap."

**Concrete rule for the builder:** The proactive push pipeline is:
- Server-side briefing-delta detection → `_issue_token(bid, tool, stored_args)` pre-issued → token encoded in push payload → Dave taps → `/assistant/confirm` redeems the token → STORED args execute.
- The push notification is NOT a send. The token redemption IS the send. Dave's tap IS the approval.
- If Dave does not tap: token expires (10-minute TTL), nothing sends. The briefing recomputes on next open.

**The "auto-send" test:** For any new proactive trigger being built, ask: "Does this reach a customer if Dave does not tap?" If YES, it is broken — the proactive push must always require a tap for customer-facing writes.

### P1-2: F11 Referential Ambiguity Guard — Do Not Guess the Recipient

**The honesty trap:** `_resolve_referent` currently falls to the most-recent lead when `entities` is empty (Dave typed "text her back" cold). Sending the wrong message to the wrong lead is not a UX bug — it is a customer-facing error that is harmful to Dave's reputation and potentially (if the message contains sensitive info) a privacy issue.

**The honest rule:** In `_h_text_lead` (or `_gated`): if `_is_referential(message)` is True AND `entities` is empty, Vic asks — "Which lead? Say a name or list your leads first" — before issuing any confirm token. No token is issued blind. No message is pre-staged without a resolved, DB-verified recipient. The `text_lead` confirm must always show the recipient name explicitly so Dave can catch an error.

**Code anchor:** `assistant.py:275` (`_h_text_lead`). This is F11-P1-4, one branch change, ~5 lines.

### P1-3: F11 7 AM Push — One Push Per Morning, Not Per Event

**The honesty trap:** Without burst control, 5 leads arriving in the same hour could generate 5 separate push notifications, each with a pre-staged chip. This is spam — the opposite of set-it-and-forget-it.

**The honest rule:** The 7 AM morning push is ONE notification per business per day. It is the consolidated briefing (top 3 actionable items), not per-event notifications. Per-event notifications (F09 alerts) are the separate channel for urgency — the 7 AM is the daily tray. The `reminders.py` ticker must enforce one-per-morning via the `alert_recent()` dedupe pattern (`db.py:1853`).

### P1-4: F13 "$X on the Table" Framing Must Be REAL

**The honesty trap:** If `avg_job_value` is unset, `growth.py:67` returns 0, and the morning tray says "$0 on the table." The CURRENT FIX (F13 S6) substitutes a trade-keyword default (e.g., "paint" → $2,500). This is honest as long as it is clearly labeled as an estimate.

**The honest rule:** The morning tray SMS and the dashboard money-left-behind figure MUST distinguish between:
- "~$7,500 on the table (based on your $2,500 avg job)" — when using Dave's actual stored value.
- "~$7,500 on the table (est. — tap to set your actual job value)" — when using the trade-keyword default.

Never show a dollar figure as precise when it is a trade default. The "tap to correct" affordance (F13-FINAL §3) is mandatory in the tray UI. This also applies to the F12 ROI block — the ROI multiple must be labeled "estimated" until Dave sets his actual avg job value.

### P1-5: F07 Auto-Graduation — Rescue Must Be Possible Before Graduation Fires

**The honesty trap:** Auto-graduation (`monitor → enforce`) fires after 7 days + ≥10 verdicts + 0 rescues (F07 S1). If the graduation clock and the "This was real" rescue path (F07 S2) are built in the wrong order, a contractor could have their screen graduated to `enforce` without ever having the rescue button available. One missed homeowner call during that gap is a real lead lost.

**The honest rule:** S2 (rescue tap + `business_false_positive_count` column) must ship before S1 (graduation cron). The graduation cron must not fire unless `false_positive_count` column exists (the column is proof S2 is deployed). Graduation notification must include the explicit "Pause it" one-tap link — not a settings path, a direct action.

### P1-6: F07 Cross-Tenant Spam Ledger — Poisoning Is Real

**The honesty trap:** CROWD_MIN=2 means one tenant can't trigger a hard screen alone, but a coordinated bad actor (or a misconfigured integration) bulk-flagging a competitor's regular customers can reach CROWD_MIN=2 quickly. When this happens, real homeowners get silently screened. Dave loses a real job. He doesn't know why.

**The honest rule:** The operator (us) must have a monitoring query on the cross-tenant ledger before the first tenant goes to `enforce`. Specifically: any number that accrues ≥5 cross-tenant flags in < 2 hours should generate an operator alert (not auto-block). Until an operator audit is wired, `SCREEN_CROWD_MIN` must remain at 2 with the understanding that single-tenant flags cannot reach HARD alone — the combinatorial requirement is the backstop.

**Builder note:** Do NOT raise CROWD_MIN lower (to 1) without the audit, regardless of how clean the signal seems.

### P1-7: F13 Win-Back — Narrow to Inbound-Initiated Customers Only

**The honesty trap:** Win-back plays fire for past customers at 12–18 months. If "past customer" includes any lead who was ever texted (i.e., Dave's bot reached out first and they never replied), the win-back is outreach to someone who never consented to contact. This is the highest TCPA exposure in the growth engine.

**The honest rule (F13-FINAL §4 TCPA posture):** Win-back plays are restricted to customers who had at least one inbound text (they contacted Dave first OR they replied to Dave's bot). Cold-only leads — Dave texted them, they never replied — are excluded from win-backs. The `growth.scan()` query for win-back must include `AND EXISTS(SELECT 1 FROM messages m WHERE m.lead_id=l.id AND m.direction='in')`. If this WHERE clause is missing, win-backs are going to people who never consented to ongoing contact.

---

## P2 ITEMS — IMPORTANT BUT NOT SPRINT BLOCKERS

### P2-1: F06 Cold Follow-Up — Robocaller Exclusion is First Build, Not Last

**The honesty trap:** `followup_candidate_rows` gates on "had a prior inbound SMS" — a spam caller that texted a one-word reply qualifies as a follow-up candidate without the exclusion. A builder reading F06 might do S1 (robocaller exclusion in the SQL WHERE clause) last because it "just adds a filter." It must be first — it prevents the product from following up with spam callers.

**The honest rule:** `leads.triage_flag NOT IN ('robocaller','spam','solicitor')` must be in `followup_candidate_rows` before Touch 1 is ever enqueued in production. Check if `leads.triage_flag` column exists; add via migration if not. F06-S1 is the first task, not optional cleanup.

### P2-2: F08 Contacts Import — Consent Basis for Suggested Contacts

**The honesty trap:** The nightly Google Contacts re-sync pulls a contractor's personal phone contacts into the FirstBack directory. Some of these contacts may be personal friends or non-customers. The auto-booking `learn_customer()` path is safe (it only fires on confirmed appointments). The behavioral scan suggestion path is also safe (it requires ≥2 bookings or ≥3 missed calls + 0 replies). The risk is the import pre-sort — contacts carrying certain signals get suggested as `vendor` or `customer` without Dave having explicitly tagged them.

**The honest rule:** Import suggestions (from Google Contacts or vCard/CSV) are NEVER auto-applied. They always go to the pending suggestion inbox. "Accept all" is a batch confirm — Dave did tap. The `contact_import.py:9-18` invariant ("imports never auto-apply") must not be weakened. If a builder adds an "auto-confirm after 30 days" shortcut, it must be an explicit opt-in setting, not a default. The F08 plan's resolution on this (grouped card after 30 days, not auto-accept) is correct and must be honored.

### P2-3: F10 Voice — Do Not Claim "AI Voice" on a Pricing Tier Until Gate Passes

**The honesty trap:** The pricing page currently has "AI Voice Callback" as a Pro/Crew feature. Until all 7 quality-gate checks pass, this is a claim for a feature that is not deployed and has not been validated. This is a honesty violation per standing rules ("never claim simulated/undeployed as live").

**The honest rule:** Until S-6 (quality gate) passes, the pricing page shows "AI Voice Callback (coming soon)" or equivalent. The checkmark only appears when a real end-to-end call has been placed and all 7 checks documented. The quality gate document (S-6 output) becomes the internal proof artifact. A builder must not flip the pricing page to "live" without running the gate.

### P2-4: F13 Frequency Cap is the Spam Line

**The honesty trap:** Without the `growth_touch_log` cross-kind 30-day cap (F13-G3), a past customer could receive a review request one day and a win-back 3 days later (different kinds, each within their own cooldown). From the customer's view: they got two unsolicited texts in a week from a contractor. That is spam behavior. That is exactly what TCPA and carrier spam filters flag.

**The honest rule (to bake into the sub-phase spec):** G3 (`growth_touch_log` + 30-day cross-kind cap) ships with S5 — before the tray UI (M1, M2) goes live. The morning tray must enforce the cap at display time (excluded plays show as grayed/held, not in the sendable batch). A held play that was already sent once in the last 30 days must NOT appear as a GO candidate.

---

## FEATURE RISK RANKING

| Feature | Riskiest single failure | Tag |
|---|---|---|
| **F10 Voice** | Streaming not built → robotic dead air → Dave turns it off and tells his network it's terrible | P0 |
| **F13 Growth** | `growth_mode='auto'` enabled before L2 streak-gate → marketing SMS without Dave tap → TCPA | P0 |
| **F11 Vic Proactive** | Confirm token not server-bound → same-origin request sends wrong text to wrong lead | P0 |
| **F07 Screening** | Rescue path not built before graduation fires → real homeowner silently screened during window | P1 |
| **F11 Ambiguity** | "Text her back" cold → most-recent lead receives message intended for different person | P1 |
| **F13 Win-back** | Win-back fires for cold-only leads (never inbound) → outreach without consent → TCPA | P1 |
| **F06 Follow-up** | Robocaller exclusion missing → spam caller gets a contextual follow-up → embarrassing | P2 |
| **F08 Contacts** | Auto-accept added as a default → contacts applied without Dave's tap → trust violation | P2 |

**Riskiest feature overall: F10 Voice.** It is the only feature where a quality failure actively damages Dave's business reputation (homeowner hangs up, tells others the "AI was terrible") and where the product is currently claiming a capability that has not been deployed. The other features have consent or legal risks that are serious but recoverable. Voice quality failure is a brand-reputation risk that competes with Dave's livelihood.

---

## HONEST GAPS — THE BUILDER MUST KNOW

1. **A2P is still the master gate for all customer-facing sends.** Until SF-8 (Trust Hub WRITE API) ships and a tenant is approved, F13 growth sends, F11 Vic writes, and F06 follow-ups all return `'simulated'` status. The honest posture during Phase 5 build: every new send path must pass through `messaging.send_sms()` which returns `'simulated'` until A2P is approved. Do not paper over this. Do not tell Dave his growth texts "sent" when they simulated. The tray log entry for simulated sends must say "Sent (simulated — activate your number to reach customers)."

2. **Voice is 4/10 autonomy today.** The Render voice service is commented out in `render.yaml:64–81`. `FIRSTBACK_VOICE_URL` is unset in production. No real ConversationRelay call has ever been placed. Every voice-path test is simulated. This must be disclosed in Phase 5 planning status — voice is not a "needs polish" feature, it is an undeployed feature.

3. **The ticker is a single env var from silent death.** Nine Phase 5 features ride `FIRSTBACK_RUN_TICKER=1`. If this var is missing on Render, the 7 AM morning push does not fire, graduation does not happen, follow-ups do not send, growth plays do not draft. Confirm SF-3 (external Render cron → `/tasks/run-due` every 60s + heartbeat row) before writing any Phase 5 scheduled feature.

4. **`growth_mode='auto'` has no UI today.** `growth_on` defaults OFF and has no Settings UI (`db.py:480`). The S3 migration from `growth_on` to `growth_mode` must happen before any tray UI ships. A builder who builds the tray UI against `growth_on` will need to rewrite it when S3 lands.

---

## BUILD SEQUENCING GUARD (CONSENT/HONESTY ORDER)

The following order is not optional — it is the honesty-correct sequence:

```
SF-6 confirm token (P0-A) 
  → F11 proactive push pipeline can be built (tokens are now trustworthy)
  → F13 S3 growth_mode migration
  → F13 S4 'held' status + release API
  → F13 S5 growth_touch_log + frequency cap (G3)
  → F13 M1 morning tray SMS (now has a consent-safe send path)
  → F13 M5 growth_approvals audit log
  → [Only then] F13 M3 growth_mode toggle UI (with 'auto' locked)

F07 S2 rescue tap (must ship BEFORE graduation cron)
  → F07 S1 auto-graduation cron
  → F07 M1 velocity burst signal

F10 S-1 deploy voice service
  → F10 S-2 streaming (required before quality gate)
  → F10 S-3 premium voice ear-test
  → F10 S-4 pre-call guards (STOP revocation, spam gate, de-dupe)
  → F10 S-5 voice metering
  → F10 S-6 quality gate (ALL 7 checks pass)
  → [Only then] flip pricing page checkmark
```

Any deviation from this order that ships a customer-facing action before its consent gate is a honesty violation and a potential TCPA exposure.

---

*Pre-Build 2 of 2 — HONESTY/CONSENT/RISK lane. Detail lives here; the build plan is PREBUILD-1.md.*
