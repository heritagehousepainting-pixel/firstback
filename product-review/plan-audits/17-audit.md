# Plan Audit — Plan 17 (live inbound AI voice answering)
**Stage:** S2 PLAN-AUDIT · **Date:** 2026-06-23. **Verdict: GO-WITH-FIXES.**
All hook points, sentinel safety, no-regression gating, reuse claims, metering SID, and compliance
VERIFIED against real code (file:line). 3 P1 fixes the planner missed + 3 P2.

## Verified (CONFIRMED)
- `twilio_voice_inbound` app.py:3065–3098; **sentinel branch 3078–3084 returns `<Hangup/>` FIRST — a
  sentinel call can NEVER reach Hook A/B** (hard return on match). forward_to Dial 3085–3090 (answerOnBridge,
  18s); no-forward branch 3091–3098 = Hook B site.
- `twilio_voice_dial_status` 3101–3127; `_MISSED_DIAL` (app.py:2919) = {no-answer,busy,failed,canceled};
  Hook A site before `_missed_call_textback` (3108–3110); caller still bridged via answerOnBridge.
- Reuse all real: `voice_service.build_twiml(biz_id,lead_id,wss_base,name)` (greeting= is backward-compat add);
  `/twiml` handler; `db.insert_voice_call`/`voice_spend_this_month`/`last_voice_call_at`; `/voice/status`
  closes by CallSid; `/internal/voice/turn`→`handle_inbound`; `_missed_call_textback`(3007),
  `_screen_missed_caller`(2969), `@require_twilio_signature`(96), `_xesc`(2930), `_twiml`(2935),
  `get_business_by_twilio_number`(db.py:1375), `update_phone_voice`(db.py:1143), init_db ALTER pattern.
- **No regression when OFF:** gates (`VOICE_PUBLIC_URL` + `inbound_voice_enabled`) independent booleans → any
  fail → None → existing flow identical. Outbound callback / sentinel / voicemail / forwarding all untouched.
- **Metering race-free:** inbound parent CallSid used at insert == SID `/voice/status` closes. Benign in-progress window.
- **Compliance:** `compliance.voice_allowed_now()` outbound-only (docstring: consumer-initiated not gated) →
  inbound correctly NOT quiet-hours-gated; AI disclosure greeting fires before any exchange; inbound≈IVR (low
  TCPA); A2P unaffected; recording claim omitted (transcript relay). Attorney review = GO-LIVE gate.

## FIX LIST
**P1 (before build):**
- **FIX-1:** Hook A must gate `status != "canceled"` — on `canceled` the caller already hung up; do NOT
  route to AI (would open a dead ConversationRelay session). `canceled` falls straight to `_missed_call_textback`.
- **FIX-2:** AI-answered path must call `db.log_call(biz_id, call_sid, from_number=caller, to_number=biz
  twilio number, dial_status="ai-answered", missed=0, lead_id, engaged=1)` — else the call is invisible in
  the call log + screening stats + behavior scoring (`_missed_call_textback`, which normally logs, is skipped).
  `dial_status` is free-text (db.py:2001), so "ai-answered" is backward-compatible.
- **FIX-3:** Don't double-run `_screen_missed_caller` (it can trigger a paid reputation lookup). Compute the
  verdict ONCE at the hook site and pass it into `_connect_inbound_to_ai` (which then doesn't re-screen);
  fallback `_missed_call_textback` already screens.

**P2 (before ship):**
- **FIX-4:** `calls.engaged=1` for AI-answered is semantically "we engaged" (col comment says "texted back") —
  accept + the `ai-answered` dial_status disambiguates; note in PR.
- **FIX-5:** URL-encode the `greeting=` query param with `quote()` (already imported app.py:15), like `name=`.
- **FIX-6:** outbound greeting keeps "may be recorded" (build_twiml default unchanged); inbound passes custom
  greeting (no recording claim). 153 voice tests use default → unchanged.

## Q3 downtime mitigation (INCLUDE in build)
`<Connect><ConversationRelay>` has NO text fallback if the WS is down → caller hears dead air. Add a cheap
pre-flight: before returning ConversationRelay TwiML, GET `VOICE_PUBLIC_URL` health with a tight timeout
(~400ms); on ConnectionError/timeout → return None → `_missed_call_textback` fires (caller gets the text,
no dead air). ~300ms happy-path cost — worth it on a live customer call.

## Owner decisions (surface at handoff; defaults don't block build): Q1 recording disclosure (default omit) ·
Q2 60-min de-dupe inbound (default off) · Q3 downtime (mitigated w/ health probe) · Q4 always-AI=blank
forward_to · Q5 attorney review before real-customer rollout.
