# Build loop — Live inbound AI voice answering
**Started 2026-06-23 (loop 2 of 2 after Housecall Pro). Self-paced /loop.**

Goal: the AI **answers an incoming call live** (the customer dials the FirstBack number and the AI picks up,
talks through the job, books the estimate) — distinct from today's **caller-requested callback** (caller
texts CALL → AI phones back). Reuse the existing ConversationRelay infra (`voice_service.py`).

## Key existing context (the plan must assess first — do NOT rebuild what exists)
- `voice_service.py` — ConversationRelay WebSocket service (already powers the outbound callback).
- `app.py` `/webhooks/twilio/voice/{inbound,dial-status,sentinel-twiml}` — TODAY these implement the
  forwarding/sentinel flow (ring the owner's cell first; sentinel verifies forwarding). Inbound AI answering
  must slot in WITHOUT breaking that (likely: when the owner doesn't pick up / no forward set, connect the
  call to ConversationRelay instead of voicemail/text-only).
- Gating: reuse `FIRSTBACK_VOICE_URL` (the voice service must be deployed). Add a separate opt-in so a
  business chooses live-answer vs forward-first. Inert by default.
- Compliance: inbound (customer-initiated) is lower robocall risk than outbound, BUT still needs an **AI
  disclosure** at answer ("You're speaking with an AI assistant for <business>"). Keep quiet-hours N/A for
  inbound (the customer chose to call), but confirm. Metering/cost still applies.

## Hard rules
- Build on `staging` only. **Owner gates every staging→main promotion — never push `main`.**
- Inert by default: no behavior change unless the voice service is deployed AND the business opts into
  live-answer. Must NOT break the existing forwarding/sentinel/callback flows.
- Honest copy; AI disclosure on answer. Mocked tests; no live telephony.
- ASCII-only Jinja delimiters (smart quotes broke /settings twice — scan after any template edit).

## Stages / state
- [x] **S1 ASSESS+PLAN** (sonnet) → DONE. `product-review/plans/17-inbound-voice.md`. Model: FALLBACK
      (forward-first, AI on no-answer; always-AI = blank forward_to, no code). Reuses voice service unchanged;
      net-new is surgical (1 col + 1 helper + 2 hooks + greeting param + toggle). Compliance: quiet-hours N/A,
      AI disclosure in greeting, attorney review = go-live gate not build gate.
- [x] **S2 AUDIT** (sonnet) → DONE. **GO-WITH-FIXES** (`plan-audits/17-audit.md`). Verified hooks +
      sentinel-can't-hijack + no-regression + metering race-free + compliance. 3 P1 fixes caught:
      FIX-1 (canceled→skip AI), FIX-2 (log_call ai-answered), FIX-3 (no double screen). + health-probe for downtime.
- [ ] **S3 BUILD** (sonnet, write-capable) → implement plan 17 with FIX-1..6 + health probe; mocked tests
      green; don't break existing flows; ASCII Jinja only. ← **IN PROGRESS**
- [ ] **S4 BUILD-AUDIT** (sonnet/inline) → review (compliance/honesty/regression/smart-quote) + tests green. Commit/push staging.
- [ ] **S5 HANDOFF** → SETUP_NEEDED inbound-voice go-live + memory; loop stops; notify owner. END of the 2-loop run.

## Log
- 2026-06-23: loop created after HCP shipped; S1 assess+plan agent dispatched.
