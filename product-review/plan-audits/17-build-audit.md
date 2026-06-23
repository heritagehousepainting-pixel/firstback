# Build Audit — Plan 17 (live inbound AI voice answering)
**Stage:** S4 BUILD-AUDIT (inline by orchestrator) · **Date:** 2026-06-23. **Verdict: SHIP**, no P1.

## Verified against the real code
- **`_connect_inbound_to_ai`** (app.py:3066): gates in order — VOICE_PUBLIC_URL set; `inbound_voice_enabled`;
  monthly cap; FIX-3 uses passed-in verdict (no re-screen, no extra paid reputation call); Q3 health probe
  `requests.get(VOICE_PUBLIC_URL, timeout=0.4)` → any exception returns None (caller falls to text-back, no
  dead air). FIX-2 `db.log_call(... dial_status="ai-answered", missed=0, engaged=1)` + `insert_voice_call`.
  FIX-5 `quote()` on name AND greeting. FIX-6 greeting AI-disclosed, no recording claim (`--` ASCII).
  Returns `<Redirect method="GET">` (xesc'd) to the voice service `/twiml` → reuses `build_twiml(greeting=)`
  (same mechanism as the outbound callback path). Sound.
- **Hook A** (dial-status): FIX-1 `if status != "canceled"` (caller already gone on cancel) → verdict once → helper. ✓
- **Hook B** (inbound no-forward, after the `if forward:` Dial branch; sentinel returns above it). ✓
- **build_twiml greeting=** backward-compatible (default unchanged → 153 existing voice tests green).
- **Inert when off:** both gates independent → any fail → None → existing `_missed_call_textback`/forwarding/
  voicemail/sentinel/outbound-callback flows identical. Confirmed.
- **Sentinel safety:** sentinel branch returns `<Hangup/>` before either hook — cannot be routed to AI.
- **Metering:** inbound parent CallSid → `insert_voice_call`; `/voice/status` closes by same SID. Race-free.
- **Compliance:** quiet-hours not applied (consumer-initiated); AI disclosure in greeting before any exchange.

## Tests (independently re-run)
test_inbound_voice 38/0; test_voice_app 61/0; test_voice 11/0; test_voice_stream 42/0; test_webhooks 18/0;
test_dispatcher_call 29/0 (+ voice_llm 13, voice_metering 31 per build report) = **243/0, zero regressions.**
`import app, voice_service` clean; settings.html parses; no smart-quote delimiter corruption.

## Notes (non-blocking)
- `<Redirect>` (vs inline ConversationRelay) is the correct, simplest reuse — confirmed acceptable.
- Edge case: if helper returns None AFTER computing the verdict (cap/health/spam), `_missed_call_textback`
  re-screens once — at most one extra screen only in that rare fallback; normal path screens once. Acceptable.
