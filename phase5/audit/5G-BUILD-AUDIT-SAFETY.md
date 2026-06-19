5G-BUILD-AUDIT-SAFETY.md  --  Phase 5g AI Voice Build Safety Audit
======================================================================
Date:    2026-06-18
Branch:  staging @ 5752ba6  (CODE-COMPLETE-BEHIND-THE-GATE)
Auditor: read-only (no source edits; audit file is the sole write)
Scope:   Merged slices 1-4 (voice_service.py, llm.py, config.py,
         db.py, messaging.py, app.py R1-R5) vs. the 5G-AUDIT-SAFETY.md
         pre-build checklist
Test suite at time of audit:
         test_voice.py: 11/11  test_voice_app.py: 41/41
         test_voice_llm.py: 13/13  test_voice_metering.py: 31/31
         test_voice_stream.py: 39/39  (135 voice tests, 0 failures)
Focus:   gate integrity, consent/revocation, pre-call guard order,
         voicemail/AMD, cost cap, PII, honesty, pricing


----------------------------------------------------------------------
EXECUTIVE VERDICT
----------------------------------------------------------------------

GATE INTEGRITY: SOUND.  Voice cannot accidentally go live or place
a call from this environment.  VOICE_PUBLIC_URL="" (FIRSTBACK_VOICE_URL
unset in render.yaml; config.py:210 default "").  Every call path
triple-gates on VOICE_PUBLIC_URL.  No merged slice sets FIRSTBACK_VOICE_URL,
uncomments render.yaml, or alters pricing templates.

PRE-BUILD P0s CLOSED: All 8 pre-build P0 gaps from 5G-AUDIT-SAFETY.md
are now closed in the merged code:
  - All 3 opt-out paths (STOP/detect_revocation/cancel) set voice_ok=0.
  - Pre-call guard order (consent -> quiet -> spam -> dedupe -> cap -> dial) wired.
  - voice_calls table, spend tracking, and pre-dial cap all present.
  - MachineDetection + AsyncAmd in place_call(add_amd=True).
  - WebSocketDisconnect handler sends recovery SMS per outcome.

ONE NEW P1 FINDING (introduced by this build): The __RECOVERY_SMS__
sentinel relay path is broken in production mode.  When WEB_INTERNAL_URL
is set, _send_recovery_sms() in voice_service.py POSTs the literal
string "__RECOVERY_SMS__:<body>" to /internal/voice/turn -- but that
endpoint has no sentinel handler and passes the raw string into
handle_inbound() (the booking brain).  The AI will try to respond to
"__RECOVERY_SMS__:I enjoyed our chat" as if it were a homeowner message.
The recovery SMS is never actually sent (no Twilio call is made from
/internal/voice/turn).  This breaks M-5 (post-call recovery SMS) in the
production relay path.  In local/test mode (WEB_INTERNAL_URL unset) the
path is correct.  Tests mock _send_recovery_sms() so the bug is masked.

PRE-BUILD P1s CLOSED: All 3 pre-build P1 gaps resolved (voice/status
endpoint built, retry quiet-hours honesty noted in spec risks, model
guard test present and passing).

P2 CARRY: render.yaml stale comment ("cannot share this one's SQLite
disk") is still wrong per the relay architecture.  Not a safety risk
today (block is commented out); must be fixed before the block is
uncommented in owner-ops step.

VERDICT:  After this build, voice is still not live.  No tenant is
activated.  Pricing templates are unchanged.  No call can be placed
without VOICE_PUBLIC_URL set by the owner.  The build is SAFE to merge
to staging with the ONE P1 FINDING (recovery SMS relay) below requiring
a targeted fix before the 7-check gate.

P0 FINDINGS (build-complete): 0  (all 8 pre-build P0s closed)
P1 FINDINGS (new):           1  (recovery SMS sentinel unhandled)
P2 FINDINGS (carry):         1  (stale render.yaml comment)


----------------------------------------------------------------------
SECTION 1 -- GATE INTEGRITY
----------------------------------------------------------------------

G-1  [PASS]  VOICE_PUBLIC_URL master gate
     config.py:210 -- VOICE_PUBLIC_URL = os.environ.get("FIRSTBACK_VOICE_URL", "")
     app.py:2715   -- `if norm in _CALL_WORDS and VOICE_PUBLIC_URL:`
     messaging.py:220 -- place_call() returns {"status":"simulated"} when
     not configured() or not twiml_url.
     EVIDENCE: No merged slice sets FIRSTBACK_VOICE_URL in code or render.yaml.
     VOICE_PUBLIC_URL="" at runtime by default.

G-2  [PASS]  render.yaml voice service block remains commented
     render.yaml:82-98 -- entire voice service block is commented.
     CARRY-P2: stale comment at line 82 ("cannot share this one's SQLite
     disk") is architecturally wrong (relay makes voice stateless).
     Must fix before uncommenting (owner-ops). Not a safety risk today.

G-3  [PASS]  render.yaml does not set FIRSTBACK_VOICE_URL
     grep confirms: zero occurrences of FIRSTBACK_VOICE_URL in render.yaml.
     Blueprint deploy will NOT activate voice.

G-4  [PASS]  Pricing templates unchanged
     templates/pricing.html:39  -- "coming soon" + "(beta -- not yet available)"
     templates/pricing.html:69  -- FAQ: "Today FirstBack handles everything by text."
     Neither string was modified by any merged slice.

G-5  [PASS]  VOICE_MONTHLY_CAP_CENTS is passive -- does not activate voice
     config.py:222-226 -- constant set to 2000 (default $20). Only checked
     pre-dial; has no effect unless VOICE_PUBLIC_URL is set.

G-6  [PASS]  /internal endpoints die without INTERNAL_SECRET
     app.py:2946-2948: secrets.compare_digest + returns 403 when
     INTERNAL_SECRET is empty. All four /internal/voice/* endpoints
     share this guard (/turn, /stream, /turn_log).
     app.py:2970, 3005: verified same guard on /stream and /turn_log.


----------------------------------------------------------------------
SECTION 2 -- FCC CONSENT / REVOCATION
----------------------------------------------------------------------

C-1  [PASS]  Affirmative opt-in required before any call
     app.py:2715-2716 -- FCC consent gate: `if norm in _CALL_WORDS and
     VOICE_PUBLIC_URL: db.set_voice_consent(biz["id"], caller, True)`
     Consent recorded BEFORE place_call(). Voice fires only on affirmative
     customer message. Double-gated by VOICE_PUBLIC_URL.

C-2  [PASS -- WAS P0]  All 3 opt-out paths now clear voice_ok
     Pre-build gap (5G-AUDIT-SAFETY.md C-2): STOP / detect_revocation /
     cancel->opt-out did NOT clear voice_ok. Now fixed:
       app.py:2664  cancel->opt-out: db.set_voice_consent(..., False)  # R1
       app.py:2669  STOP:            db.set_voice_consent(..., False)  # R1
       app.py:2676  detect_revocation: db.set_voice_consent(..., False)  # R1
     Code comments read "R1: revoke AI-voice consent on [path]".

C-3  [PASS]  voice_ok defaults 0 (opt-out-by-default)
     db.py: contacts_consent table has `voice_ok INTEGER DEFAULT 0`.
     A new consumer row has zero consent until they affirmatively
     send a call-me word.

C-4  [PASS]  is_suppressed() gate fires before _CALL_WORDS branch
     app.py:2705-2707: is_suppressed() check exits early before line 2715.
     An opted-out caller never reaches the voice consent path.


----------------------------------------------------------------------
SECTION 3 -- PRE-CALL GUARD ORDER
----------------------------------------------------------------------

Required order per spec §9: consent -> quiet -> spam -> dedupe -> cap -> dial.

PG-1  [PASS]  (i) opted_out / is_suppressed gate
      app.py:2705-2707 -- fires before the entire _CALL_WORDS block.

PG-2  [PASS -- WAS P0]  (ii) voice_ok re-check after writing consent
      app.py:2718-2722 -- re-reads db.get_consent() after set_voice_consent(True).
      If voice_ok is 0 (concurrent STOP cleared it): sends text fallback, returns.
      Comment: "R2 pre-call guard (ordered -- any fail -> text reply, never call).
      (i) Re-read consent after writing: concurrent STOP could have cleared voice_ok."

PG-3  [PASS -- WAS P0]  (iii) Spam score gate
      app.py:2728-2733 -- computes triage.spam_score(_caller_behavior(...)). 
      If score >= SCREEN_SCORE_HARD: send_sms text fallback, return _twiml("<Response/>").

PG-4  [PASS -- WAS P0]  (iv) 60-min de-dupe
      app.py:2734-2748 -- db.last_voice_call_at(biz["id"], caller).
      If within 60 minutes: text fallback "We already tried calling you".
      Malformed timestamp -> allow (fails open safely).
      db.py:3656-3677: last_voice_call_at() uses None-safe JOIN; returns None when
      no voice_calls row exists.

PG-5  [PASS]  (v) quiet hours gate
      app.py:2723-2727 -- compliance.voice_allowed_now(). After-hours text verbatim:
      "Thanks. It is currently after hours, so we will call you during business hours.
      You can also keep texting here any time."

PG-6  [PASS -- WAS P0]  (vi) Monthly cost cap pre-dial
      app.py:2749-2757 -- db.voice_spend_this_month(biz["id"]).
      If >= config.VOICE_MONTHLY_CAP_CENTS ($20): text fallback + alerts.notify_async(
      biz, "voice_cap", {"spend_cents":..., "cap_cents":...}), return.

PG-7  [PASS]  (vii) "Calling you now" honesty gate
      app.py:2770-2773 -- only when res.get("status") == "placed".
      Simulated / error returns a different status string; no false promise.

PG-8  [PASS]  add_amd compat guard
      app.py:2762-2767 -- inspect.signature(messaging.place_call) checks for
      add_amd param before passing it. Backward-compatible with test stubs.


----------------------------------------------------------------------
SECTION 4 -- VOICEMAIL / AMD
----------------------------------------------------------------------

VM-1  [PASS -- WAS P0]  MachineDetection params in place_call()
      messaging.py:227-231 -- when add_amd=True:
        data["MachineDetection"] = "Enable"
        data["AsyncAmd"] = "true"
        data["AsyncAmdStatusCallback"] = status_callback  (when set)
      messaging.py:223-224 -- auto-fills StatusCallback from PUBLIC_BASE_URL
      when add_amd=True and no explicit status_callback passed.
      app.py:2764-2765 -- place_call(... add_amd=True) called.

VM-2  [PASS -- WAS P0]  WebSocketDisconnect handler sends recovery SMS
      voice_service.py:431-451 -- except WebSocketDisconnect:
        _post_turn_log(biz_id, lead_id, turn_log)  # M-3
        if booked: no extra SMS (booking confirmation already sent)
        elif turn_count > 0: "I enjoyed our chat -- any questions, just text here."
        else: "Looks like we got cut off -- you can keep texting here or text
               'call me' to try again."

VM-3  [PASS -- WAS P1]  /webhooks/twilio/voice/status endpoint exists
      app.py:3039-3079 -- @require_twilio_signature gated.
      AnsweredBy machine_start / machine_end_silence / machine_end_other -> "voicemail".
      status no-answer / busy -> "no_answer".
      Voicemail path: queries voice_calls by twilio_sid, sends recovery SMS
      via messaging.send_sms() directly (correct -- this is the WEB process).

VM-4  [PASS]  Recovery SMS text matches spec
      app.py:3074-3078 -- "We tried to reach you by phone -- happy to keep
      chatting here. What are you looking to get painted?"

VM-5  [P1 -- NEW]  __RECOVERY_SMS__ sentinel unhandled in /internal/voice/turn
      FINDING: voice_service.py:220-227 -- _send_recovery_sms() in production
      mode (WEB_INTERNAL_URL set) POSTs payload:
        {"biz": biz_id, "lead": lead_id, "text": "__RECOVERY_SMS__:<body>"}
      to /internal/voice/turn (app.py:2944-2959).
      PROBLEM: internal_voice_turn() passes text.strip() directly to
      handle_inbound(). The booking brain receives the raw sentinel string
      as a user message. The recovery SMS is NEVER sent (handle_inbound
      does not know how to extract or forward the sentinel). The AI will
      generate a reply to the literal text "__RECOVERY_SMS__:I enjoyed our
      chat..." which will be discarded (no WebSocket to speak it on).

      SCOPE: This bug only manifests when WEB_INTERNAL_URL is set (i.e.,
      in production Render deploy with the voice service running separately).
      Local/test mode (WEB_INTERNAL_URL unset) uses the direct messaging
      fallback (voice_service.py:207-218) which calls messaging.send_sms()
      correctly.

      TESTS DO NOT CATCH THIS: test_voice_stream.py mocks
      _send_recovery_sms() entirely, bypassing the relay path.
      test_voice_app.py tests the /webhooks/twilio/voice/status recovery
      SMS (correct, different code path).

      SEVERITY: P1. Does not block gate integrity or consent. Breaks
      post-call SMS recovery in production relay mode (M-5 / Slice E).
      The homeowner gets no "We got cut off" follow-up text when a call
      drops mid-conversation during a live deployment.

      REQUIRED FIX: Either:
        (a) In app.py /internal/voice/turn, detect the sentinel and call
            messaging.send_sms() instead of handle_inbound:
              text = (data.get("text") or "").strip()
              if text.startswith("__RECOVERY_SMS__:"):
                  sms_body = text[len("__RECOVERY_SMS__:"):]
                  messaging.send_sms(biz, lead.get("phone"), sms_body)
                  return jsonify(ok=True)
              reply, booked, urgent = handle_inbound(biz, lead, text)
        (b) Or, preferred: route recovery SMS via a dedicated endpoint
            (e.g. /internal/voice/recovery_sms) rather than abusing
            /internal/voice/turn. Cleaner separation.

VM-6  [PASS]  Voicemail does NOT speak pitch into voicemail
      AMD fires before ConversationRelay /ws connects. On machine_* the
      twilio_voice_status webhook fires. No TwiML is generated that would
      speak into voicemail. SOUND.

VM-7  [NOTE -- CARRY]  One-retry policy not enforced in code
      Spec §2 SLICE D / 5G-AUDIT-SAFETY.md VM-5: one retry after 2 hours,
      through quiet-hours gate. No retry scheduler exists yet. This is
      correctly deferred (spec says "schedule retry: set retry_at = now + 2h
      in voice_calls row (ticker picks it up)"). The voice_calls schema has
      no retry_at column. This is a known future gap, not a ship-blocker
      for the current build. No call currently gets a retry.


----------------------------------------------------------------------
SECTION 5 -- COST CAP / METERING
----------------------------------------------------------------------

CC-1  [PASS -- WAS P0]  voice_calls table exists
      db.py:408-426 -- CREATE TABLE IF NOT EXISTS voice_calls with
      correct schema: id, biz_id, lead_id, twilio_sid, started_at,
      ended_at, duration_seconds, turns, outcome, cost_cents, created_at.
      INDEX on (biz_id, started_at).

CC-2  [PASS -- WAS P0]  All 4 db helpers present
      db.py:3628  insert_voice_call(biz_id, lead_id, twilio_sid) -> id
      db.py:3643  update_voice_call_outcome(twilio_sid, outcome, duration, cost_cents)
      db.py:3656  last_voice_call_at(biz_id, caller_number) -> iso|None
      db.py:3680  voice_spend_this_month(biz_id) -> cents

CC-3  [PASS -- WAS P0]  Pre-dial cap checked before place_call()
      app.py:2750-2757 -- voice_spend_this_month checked;
      if >= VOICE_MONTHLY_CAP_CENTS: skip call + text + alert.

CC-4  [PASS]  Cost computed from real Twilio CallDuration
      app.py:3048-3060 -- duration = int(request.form.get("CallDuration") or 0).
      blocks = math.ceil(duration / 30) if duration > 0 else 0.
      cost = blocks * config.VOICE_CREDIT_RATE_CENTS.
      No invented/flat cost. Metering is real.

CC-5  [PASS]  insert_voice_call() called on successful place_call
      app.py:2771-2772 -- only when res.get("status") == "placed":
      db.insert_voice_call(biz["id"], lead["id"], res.get("sid","")).
      Simulated/error calls do not open a voice_calls row.

CC-6  [PASS]  Daily LLM cap inherited by voice path
      config.py:52-56 -- CLAUDE_DAILY_COST_CAP_USD (default $1.00).
      Voice /internal/voice/turn calls llm.py, which inherits this cap.


----------------------------------------------------------------------
SECTION 6 -- PII
----------------------------------------------------------------------

PI-1  [PASS]  No caller numbers logged to stdout in voice_service.py
      voice_service.py error prints (lines 372, 397, 428, 454) log
      exception descriptions, not phone numbers.

PI-2  [PASS]  Recording disclosure hardcoded and non-configurable
      voice_service.py:234-235 -- "This call may be recorded." is baked
      into the greeting string in build_twiml(). Not a settings field.
      Tenant cannot remove it.

PI-3  [PASS]  INTERNAL_SECRET: constant-time compare, dead without secret
      app.py:2947 -- secrets.compare_digest(sent, INTERNAL_SECRET).
      Returns 403 when INTERNAL_SECRET is empty (line 2947:
      `if not INTERNAL_SECRET or not ...`). Same guard on all 3
      internal voice endpoints.

PI-4  [PASS]  Transcript bodies scrub phone numbers before storage
      app.py:3018-3030 -- /internal/voice/turn_log applies regex
      _phone_re = re.compile(r"\+?1?\d[\d\s\-().]{8,}\d") to both
      caller_text and ai_text before calling db.add_message.
      Substitutes "[number]". Stored as direction="system" with
      "[VOICE] caller: ..." / "[VOICE] ai: ..." prefix.

PI-5  [PASS]  SQL params are parameterized; biz_id/lead_id are integers
      All db.py voice helpers use parameterized queries. app.py:2951,
      2974 -- int() cast on biz/lead from POST body.


----------------------------------------------------------------------
SECTION 7 -- MODEL GATING
----------------------------------------------------------------------

MG-1  [PASS]  CLAUDE_MODEL_VOICE = "claude-haiku-4-5"
      config.py:49. Distinct from CLAUDE_MODEL (Sonnet). Env-overridable.

MG-2  [PASS -- WAS P1]  /internal/voice/stream uses CLAUDE_MODEL_VOICE
      app.py:2990 -- calls llm.complete_stream_voice(system, messages).
      llm.py:292-320 -- complete_stream_voice() hardcodes model=CLAUDE_MODEL_VOICE
      (claude-haiku-4-5). Not CLAUDE_MODEL. No caller override possible.
      Test: test_voice_llm.py "complete_stream_voice uses CLAUDE_MODEL_VOICE (Haiku)" -- PASS.

MG-3  [PASS]  M-4 confirmation-echo system prompt wired
      llm.py:45-55 -- VOICE_CONFIRM_BOOKING_PROMPT requires AI to speak
      slot back and get verbal confirmation before [[BOOK]].
      Instructs 1-2 sentence replies, natural speech, barge-in grace.
      app.py:2986 -- injected as system in /internal/voice/stream.

MG-4  [PASS]  Barge-in grace handled in system prompt
      llm.py:52-54 -- "If your previous reply appears cut off, treat it
      as complete and respond to the caller's new utterance."


----------------------------------------------------------------------
SECTION 8 -- STREAMING / BARGE-IN
----------------------------------------------------------------------

SB-1  [PASS]  Filler frame sent before streaming begins (production mode)
      voice_service.py:340-345 -- if WEB_INTERNAL_URL: random.choice(_FILLERS)
      sent as _say(filler, last=False) before _stream_tokens().
      Local/test mode: filler skipped (backward compat with test stubs).

SB-2  [PASS]  Cancel flag stops frame emission on interrupt
      voice_service.py:274 -- cancel_flag = asyncio.Event()
      voice_service.py:421-424 -- mtype=="interrupt": cancel_flag.set()
      voice_service.py:351-354 -- streaming loop: if cancel_flag.is_set(): break

SB-3  [PASS]  Final frame has last=True
      voice_service.py:363-368 -- _say(full_reply, last=True) on __DONE__
      (only when not cancelled).

SB-4  [PASS]  Booking commit fires ONCE at stream END via /internal/voice/turn
      voice_service.py:379-398 -- P0-2 guard: after stream completes (not cancelled,
      WEB_INTERNAL_URL set), POSTs to /internal/voice/turn. No mid-stream handle_inbound.
      Local path: _stream_tokens() already called _process_turn(); no duplicate POST.

SB-5  [PASS]  ASR empty counter (M-2)
      voice_service.py:300-323 -- consecutive_empty counter.
      At 3: filler frame.  At 5: clean_exit=True, graceful close,
      recovery SMS via _send_recovery_sms().
      NOTE: M-2 recovery SMS has the same __RECOVERY_SMS__ relay bug (VM-5)
      in production mode.

SB-6  [PASS]  Turn log accumulates, POSTs on disconnect (M-3)
      voice_service.py:400-403 -- turn_log.append per prompt+reply.
      voice_service.py:431-433 -- except WebSocketDisconnect: _post_turn_log()
      voice_service.py:453-458 -- except Exception: best-effort _post_turn_log()


----------------------------------------------------------------------
SECTION 9 -- WHAT MUST REMAIN TRUE (§9 INVARIANTS CHECK)
----------------------------------------------------------------------

[PASS]  Voice not live, no tenant activated, pricing unchanged.
        VOICE_PUBLIC_URL="" (FIRSTBACK_VOICE_URL not in render.yaml or code).
        pricing.html:39 -- "coming soon" / "(beta -- not yet available)" unchanged.
        pricing.html:69 -- FAQ unchanged.
        No DB migration sets voice_ok=1 for any lead.

[PASS]  No call placed without consent+quiet+spam+dedupe+cap all passing.
        Pre-call guard block (app.py:2717-2757) enforces all 5 checks in order.
        Any failure -> text fallback, never call.

[PASS]  No booking pitch spoken into voicemail.
        AMD via add_amd=True. AMD fires before /ws connects.
        twilio_voice_status sends recovery SMS, not spoken TwiML.

[PASS]  Cost cap enforced before dial with real metering.
        voice_spend_this_month() checked at app.py:2750.
        Cost from real Twilio CallDuration (app.py:3048-3060).

[PASS]  "Calling you now" only on place_call 'placed'.
        app.py:2770: `if res.get("status") == "placed"`.

[PASS]  Transcripts carry no raw phone numbers.
        app.py:3018-3030: phone number regex scrubbed before add_message.

[PASS]  Internal endpoints dead without INTERNAL_SECRET.
        app.py:2947, 2970, 3005: 403 when INTERNAL_SECRET is empty.

[PASS]  7-check gate is the sole unlock for pricing/tenant/live voice.
        Spec §4 and §9 are unmodified. No slice claims voice is live.


----------------------------------------------------------------------
SECTION 10 -- P0/P1/P2 REGISTER
----------------------------------------------------------------------

P0 FINDINGS: 0 (all 8 pre-build P0s closed by this build)

Pre-build P0 closure summary:
  P0-1  C-2   CLOSED: app.py:2664,2669,2676 revoke voice consent on all 3 opt-out paths
  P0-2  PG-2  CLOSED: app.py:2718-2722 re-read voice_ok before place_call
  P0-3  PG-3  CLOSED: app.py:2728-2733 spam score gate
  P0-4  PG-4  CLOSED: app.py:2734-2748 60-min de-dupe via last_voice_call_at
  P0-5  PG-6  CLOSED: app.py:2749-2757 monthly cost cap pre-dial
  P0-6  VM-1  CLOSED: messaging.py:227-231 MachineDetection + AsyncAmd
  P0-7  VM-2  CLOSED: voice_service.py:431-451 post-disconnect recovery SMS
  P0-8  CC-1  CLOSED: db.py:408-426 voice_calls table + 4 helpers

P1 FINDINGS: 1 (NEW -- introduced by this build)

  P1-1  VM-5  __RECOVERY_SMS__ sentinel unhandled in /internal/voice/turn
              voice_service.py:221-222 generates payload with
              text="__RECOVERY_SMS__:<body>"
              app.py:2944-2959 has no sentinel handler: passes text
              directly to handle_inbound() -- recovery SMS not sent;
              AI responds to the sentinel string as a user message.
              BUG ONLY IN PRODUCTION relay mode (WEB_INTERNAL_URL set).
              Tests mock _send_recovery_sms() -- bug is masked in suite.
              FIX: add sentinel detection in internal_voice_turn() before
              handle_inbound() call (see VM-5 above for code).

P2 FINDINGS: 1 (CARRY from pre-build audit)

  P2-1  G-2   render.yaml:82 stale comment ("cannot share this one's SQLite
              disk") is wrong per the relay architecture. Must be deleted
              before owner-ops uncomments the voice service block.
              Not a safety risk while the block is commented.


----------------------------------------------------------------------
SECTION 11 -- THE 7-CHECK GATE STATUS (unchanged from spec)
----------------------------------------------------------------------

All 7 checks remain BLOCKED: no Render deploy, no real Twilio, no
real calls. Unit tests verify shape/wiring only. The 7-check gate is
the sole unlock for pricing changes, tenant activation, or marking
5g DONE.

  [  ] CHECK 1 -- DEPLOY GATE (blocked: owner-ops)
  [  ] CHECK 2 -- END-TO-END REAL CALL (blocked: deploy + streaming)
  [  ] CHECK 3 -- STREAMING GATE <1.5s (blocked: deploy; latency unknown)
  [  ] CHECK 4 -- BARGE-IN GATE (blocked: deploy)
  [  ] CHECK 5 -- VOICEMAIL GATE (blocked: deploy; P1-1 must be fixed first)
  [  ] CHECK 6 -- QUIET-HOURS GATE (blocked: deploy)
  [  ] CHECK 7 -- PREMIUM VOICE EAR-TEST (blocked: deploy + owner ear-test)


----------------------------------------------------------------------
SECTION 12 -- HONEST OPEN RISKS (code-correct but unknown in production)
----------------------------------------------------------------------

Risk A -- Streaming latency unknown until real deploy.
  Even with Haiku + relay, the Render-to-Render SSE hop may push
  first-word latency over 1.5s. Gate 3 is the only real measurement.

Risk B -- Barge-in context (partial sentence in history).
  System prompt instructs AI to treat cut-off context as complete.
  Cannot verify without real calls (Gate 4).

Risk C -- Double-booking race (SMS + voice concurrent).
  Protected by DB UNIQUE slot constraint. No additional code needed;
  confirm in integration testing.

Risk D -- Render Starter cold start (15-30s) kills Twilio TwiML timeout.
  Operational: upgrade to Standard plan or health-ping cron.

Risk E -- Booking brain not voice-optimized (responses may be too long).
  System prompt caps at 1-2 sentences but the SMS brain was built for
  text. Must verify on real calls (Gate 2).


----------------------------------------------------------------------
END OF AUDIT
----------------------------------------------------------------------
