PHASE 5G SPEC -- F10 AI VOICE (locked, build-ready)
====================================================
Date: 2026-06-18
Branch: staging
Status: CODE-COMPLETE BEHIND GATE -- voice cannot be marked live from this
        environment. Requires owner-ops deploy + 7-check acceptance gate on
        a real Twilio call before any tenant activation or pricing change.

----------------------------------------------------------------------
0. HARD TRUTHS UP FRONT
----------------------------------------------------------------------
- No real call has ever been placed. The voice service has never run in
  production. render.yaml:88-98 is commented out. FIRSTBACK_VOICE_URL
  is unset. FIRSTBACK_INTERNAL_SECRET is unset.
- Streaming is absent. voice_service.py:168 sends one _say(reply, last=True)
  frame per turn -- a single blocking HTTP POST to /internal/voice/turn
  completes first, THEN Twilio starts speaking. Dead air of 1-3s is baked
  in today. This is disqualifying for a phone call.
- Barge-in is a no-op. voice_service.py:169-172 receives mtype=="interrupt"
  and does `continue`. Because the full reply is already sent as a single
  last=True frame before the interrupt can arrive, real barge-in is
  impossible without streaming.
- CLAUDE_MODEL_VOICE = "claude-haiku-4-5" is correctly configured in
  config.py:49. The web app's CLAUDE_MODEL (Sonnet) must NOT be used on
  the voice path -- Opus latency (1.5-3s) disqualifies it.
- Mediocre voice is WORSE than none. A robot with dead air and no barge-in
  damages Dave's reputation with homeowners. This spec does not authorize
  shipping voice until all 7 quality checks pass on a real deployment.

----------------------------------------------------------------------
1. CURRENT STATE (file:line)
----------------------------------------------------------------------

BUILT AND CORRECT:
  voice_service.py:1-185    -- FastAPI ASGI service, full file
  voice_service.py:107-120  -- build_twiml(), ConversationRelay TwiML
  voice_service.py:108-109  -- welcomeGreeting with recording disclosure
                               (FCC-required; non-configurable by design)
  voice_service.py:112      -- CONVERSATIONRELAY_VOICE attr wired to env
  voice_service.py:142-180  -- /ws WebSocket loop: setup, prompt, error,
                               WebSocketDisconnect handler
  voice_service.py:84-103   -- _process_turn(): relay to web app or local
  voice_service.py:88-95    -- HTTP relay to /internal/voice/turn with
                               INTERNAL_SECRET header (stateless voice svc)
  app.py:2533               -- "if norm in _CALL_WORDS and VOICE_PUBLIC_URL"
                               -- FCC AI-voice consent gate
  app.py:2534               -- db.set_voice_consent(biz["id"], caller, True)
  app.py:2535               -- compliance.voice_allowed_now() quiet-hours check
  app.py:2536-2538          -- after-hours text response (verbatim, correct)
  app.py:2539-2541          -- twiml_url construction and place_call()
  app.py:2542-2545          -- "Calling you now" only when status=="placed"
  app.py:2716-2731          -- /internal/voice/turn endpoint (secret-gated)
  config.py:49              -- CLAUDE_MODEL_VOICE = "claude-haiku-4-5"
  config.py:210             -- VOICE_PUBLIC_URL from FIRSTBACK_VOICE_URL
  config.py:215             -- CONVERSATIONRELAY_VOICE from FIRSTBACK_VOICE_TTS
  config.py:224             -- WEB_INTERNAL_URL from FIRSTBACK_WEB_URL
  config.py:225             -- INTERNAL_SECRET from FIRSTBACK_INTERNAL_SECRET
  messaging.py:209-231      -- place_call() with optional status_callback param
                               (StatusCallback already wired -- not yet passed)
  compliance.py:25-32       -- voice_allowed_now() -- built and tested
  db.py:2057-2066           -- set_voice_consent() upsert
  db.py:2072-2087           -- get_consent() reader

CRITICAL GAPS (net-new code required before the 7-check gate can pass):
  S-2  streaming + barge-in  -- ABSENT. voice_service.py:165-168 runs
       _process_turn() blocking in executor, then sends one last=True frame.
       No /internal/voice/stream endpoint in app.py. llm.py has
       tool_complete_stream() (line 235) for Claude but it is not wired
       to the voice path.
  S-4  pre-call guard additions -- PARTIAL. STOP revocation does NOT call
       set_voice_consent(False) at app.py:2498-2505. Spam-score gate absent.
       60-min de-dupe absent.
  S-5  voice metering -- ABSENT. No voice_calls table in db.py. place_call()
       does not pass StatusCallback. No /webhooks/twilio/voice/status endpoint.
       No credit-wallet check before dialing.
  M-1  AMD / voicemail detection -- ABSENT. place_call() does not pass
       MachineDetection or asyncAmd. No AnsweredBy handler.
  M-2  ASR garbage guard -- ABSENT. voice_service.py:160 skips empty text
       but has no consecutive-empty counter or escalation path.
  M-3  post-call transcript storage -- ABSENT. WS loop does not accumulate
       a turn log or write [VOICE] messages to lead thread on disconnect.
  M-4  confirmation echo -- ABSENT. No voice-path system prompt instruction
       requiring the AI to speak slot details back before [[BOOK]].
  M-5  post-call text to lead -- ABSENT. WebSocketDisconnect at line 177
       is a bare `pass` -- no outcome check, no recovery SMS.

STALE COMMENT (must update, not a code bug):
  render.yaml:82-86  -- "a separate Render service cannot share this one's
  SQLite disk" -- this is now WRONG. voice_service.py:88-95 already relays
  writes to the web app; the voice service is stateless. The comment must be
  deleted when uncommenting the service definition.

----------------------------------------------------------------------
2. WHAT IS CODE-BUILDABLE NOW (behind the gate, no deploy needed)
----------------------------------------------------------------------
These slices can be written, unit-tested, and merged to staging without
any deploy or real Twilio credential. They do not claim voice is live.

  SLICE A -- S-2: streaming + barge-in
    Files:
      voice_service.py (replace /ws prompt handler)
      app.py (add /internal/voice/stream SSE/streaming endpoint)
      llm.py (voice path calls complete() with model=CLAUDE_MODEL_VOICE --
              or a new complete_stream_voice() that reuses tool_complete_stream
              with model override)

    Design:
      1. New /internal/voice/stream endpoint in app.py (POST, secret-gated).
         Calls llm.complete_stream or tool_complete_stream with
         model=CLAUDE_MODEL_VOICE (haiku). Yields SSE lines: each token as
         JSON {"delta": t} or {"done": true, "full": text}.
         Also runs the booking write (handle_inbound) on the FIRST [[BOOK]]
         detection in the stream so the slot commits before the call ends.

      2. In voice_service.py /ws prompt handler:
         a. Immediately send a filler frame (rotating set) BEFORE starting
            the stream: "Mm-hmm, one moment." / "Let me check on that." /
            "Sure, just a sec." -- picks one at random with last=False so
            TTS starts speaking while the LLM warms up.
         b. Open an async HTTP stream to /internal/voice/stream.
         c. Per token/sentence boundary: await websocket.send_text(_say(tok, last=False))
         d. On stream end: send last=True frame.
         e. Maintain a session-level cancel_flag (asyncio.Event).
         f. On mtype=="interrupt": set cancel_flag; the async generator
            checks the flag after each send and stops. Twilio discards
            unspoken tokens. Process the next prompt normally.

    Unit tests (no deploy):
      test_voice.py additions:
        - _say() with last=False produces correct JSON shape
        - filler frame is sent before the stream begins (mock the stream)
        - on "interrupt" message: cancel flag is set and no further frames
          are sent for the prior utterance
        - final frame from stream has last=True
      NOTE: cannot test real latency (<1.5s) or real barge-in behavior
      without a live Twilio connection -- those are gates 2 and 3 in the
      7-check acceptance gate, not unit tests.

  SLICE B -- S-4: pre-call guard additions
    Files: app.py (STOP handler + pre-call path)

    What to add:
      a. In the STOP handler (app.py:2498-2505), after db.set_opt_out():
         also call db.set_voice_consent(biz["id"], caller, False) to
         revoke AI-voice consent. Same for compliance.detect_revocation()
         path at line 2502-2506.
      b. Before place_call() at app.py:2533, add ordered guard:
         (i)  check db.get_consent(biz["id"], caller) -- if voice_ok==0:
              skip call, reply by text.
         (ii) if triage.spam_score({...})[0] >= SCREEN_SCORE_HARD
              (config.py:92): skip call, text continues.
         (iii) check if the same caller was already called in the last
              60 minutes -- needs a new db.last_voice_call_at(biz_id, caller)
              helper (reads voice_calls table; returns None if table absent,
              so this is a no-op until S-5 creates the table -- but write
              it now so it is wired when S-5 lands).

    Unit tests:
      - After STOP text, voice_ok is 0 and pre-call guard skips place_call
      - After detect_revocation() path, voice_ok is 0
      - Caller with spam score >= 80 is not called
      - Caller called 30 min ago: guard skips (once S-5 table exists)

  SLICE C -- S-5: voice metering + cost enforcement
    Files:
      db.py (new voice_calls table + helpers)
      messaging.py (pass StatusCallback to place_call)
      app.py (new /webhooks/twilio/voice/status endpoint)

    voice_calls table schema:
      CREATE TABLE IF NOT EXISTS voice_calls (
        id INTEGER PRIMARY KEY,
        biz_id INTEGER NOT NULL,
        lead_id INTEGER,
        twilio_sid TEXT,
        started_at TEXT,
        ended_at TEXT,
        duration_seconds INTEGER,
        turns INTEGER DEFAULT 0,
        outcome TEXT DEFAULT 'in_progress',
        -- outcome: booked|no_answer|voicemail|abandoned|error|in_progress
        cost_cents INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
      );

    New db helpers:
      insert_voice_call(biz_id, lead_id, twilio_sid) -> id
      update_voice_call_outcome(twilio_sid, outcome, duration, cost_cents)
      last_voice_call_at(biz_id, caller_number) -> datetime or None
        -- JOINs voice_calls -> leads to get caller number; used for de-dupe
      voice_spend_this_month(biz_id) -> cents
        -- SUM(cost_cents) WHERE biz_id=? AND started_at >= first of month

    In messaging.py place_call():
      Pass status_callback when PUBLIC_BASE_URL is set:
        status_callback = (PUBLIC_BASE_URL.rstrip("/")
                           + "/webhooks/twilio/voice/status")
      Already has the status_callback parameter -- just needs the value.

    New endpoint in app.py:
      @app.route("/webhooks/twilio/voice/status", methods=["POST"])
      @require_twilio_signature
      def twilio_voice_status():
        sid = request.form.get("CallSid")
        status = request.form.get("CallStatus")
        duration = int(request.form.get("CallDuration", 0) or 0)
        answered_by = request.form.get("AnsweredBy", "")
        if answered_by in ("machine_start", "machine_end_silence",
                           "machine_end_other"):
            outcome = "voicemail"
        elif status in ("no-answer", "busy"):
            outcome = "no_answer"
        elif status == "completed":
            outcome = "booked"  # actual booking state is in voice_calls.outcome
                                # updated by /ws disconnect handler
        else:
            outcome = "error"
        blocks = math.ceil(duration / 30) if duration else 0
        VOICE_CREDIT_RATE_CENTS = 25  # 25 cents per 30-second block (default)
        cost = blocks * VOICE_CREDIT_RATE_CENTS
        db.update_voice_call_outcome(sid, outcome, duration, cost)
        return jsonify(ok=True)

    Pre-call credit check (add to app.py before place_call()):
      VOICE_MONTHLY_CAP_CENTS = 2000  # $20 default -- add to config.py
      if db.voice_spend_this_month(biz["id"]) >= VOICE_MONTHLY_CAP_CENTS:
          # Alert Dave, skip call, text fallback
          ...

    Unit tests:
      - insert_voice_call / update_voice_call_outcome round-trip
      - voice_spend_this_month sums correctly within month
      - last_voice_call_at returns None when no calls, correct time when
        a call was placed within 60 min
      - /webhooks/twilio/voice/status with AnsweredBy=machine_start sets
        outcome=voicemail
      - monthly cap exceeded: place_call is skipped

  SLICE D -- M-1: AMD / voicemail detection
    Files: messaging.py (place_call), app.py (twilio_voice_status above)

    Add to place_call() data dict:
      data["MachineDetection"] = "Enable"
      data["AsyncAmd"] = "true"
      data["AsyncAmdStatusCallback"] = status_callback (same URL)

    In twilio_voice_status, AnsweredBy values trigger the voicemail path
    (already covered in S-5 above). When voicemail detected:
      - Do NOT send any TwiML that would speak into voicemail
      - Send recovery SMS to lead: "We tried to reach you by phone -- happy
        to keep chatting here. What are you looking to get painted?"
      - Schedule retry: set retry_at = now + 2 hours in voice_calls row
        (ticker picks it up); same quiet-hours gate on retry.
      - After one retry: no further calls on this consent. Text-only resumes.
        Dave gets: "AI couldn't reach [name] -- continuing by text."

    Unit tests:
      - place_call() data dict contains MachineDetection and AsyncAmd
      - voicemail detection sends correct SMS (mock db.add_message path)

  SLICE E -- M-2/M-3/M-4/M-5: voice session hygiene
    Files: voice_service.py (/ws loop)

    M-2 ASR guard: add consecutive_empty counter to WS session state dict.
      After 3 empties: send filler frame ("I'm having trouble hearing you...").
      After 5: graceful close + recovery SMS.

    M-3 transcript: accumulate turn_log = [] in /ws. On each prompt+reply:
      turn_log.append({"in": text, "out": reply}). On WebSocketDisconnect
      or graceful close: POST to /internal/voice/turn_log (new endpoint)
      which calls db.add_message for each turn as direction="system",
      body="[VOICE] caller: ..." / "[VOICE] ai: ...".

    M-4 confirmation echo: add to llm.py voice system prompt builder
      (or pass as an extra instruction in _process_turn):
      "Before writing [[BOOK]], speak the booking slot back to the caller
      and confirm they said yes. Example: 'So that is Thursday the 19th at
      2 PM -- does that work for you?' Only write [[BOOK]] after they confirm."
      This is a prompt instruction only -- no new code branch.

    M-5 post-call text: in /ws on WebSocketDisconnect, check outcome state:
      if booked: standard confirmation SMS already fires (handle_inbound path)
      if no booking after turn limit or timeout:
        messaging.send_sms(biz, caller, "I enjoyed our chat -- any questions,
        just text here.")
      if call drop mid-conversation:
        messaging.send_sms(biz, caller, "Looks like we got cut off -- you can
        keep texting here or text 'call me' to try again.")

    Unit tests:
      - 5 consecutive empty prompts triggers WS close (mock close)
      - turn_log accumulates and POSTs on disconnect (mock HTTP)
      - post-call SMS fires with correct body on each outcome type

----------------------------------------------------------------------
3. WHAT IS GATED / OWNER-OPS
----------------------------------------------------------------------
These CANNOT be done from this environment. They require the owner to
take action on a real Render deployment with a real Twilio account.

GATE 0 -- PREREQUISITE ENV VARS (all must be set before ANY voice works):
  On the voice service in Render:
    FIRSTBACK_WEB_URL = https://<web-service>.onrender.com
    FIRSTBACK_INTERNAL_SECRET = <generate: python -c "import secrets; print(secrets.token_hex(32))">
    ANTHROPIC_API_KEY = sk-ant-...
    CLAUDE_MODEL_VOICE = claude-haiku-4-5  (override -- web app uses Sonnet)
    FIRSTBACK_PROVIDER = claude
    FIRSTBACK_VOICE_PORT = <PORT>  (Render sets $PORT automatically; use that)

  On the web service in Render (add/update):
    FIRSTBACK_VOICE_URL = https://<voice-service>.onrender.com
    FIRSTBACK_INTERNAL_SECRET = <same value as above>
    FIRSTBACK_PUBLIC_URL = https://<web-service>.onrender.com  (for StatusCallback)

  Total: 6 env vars to set, 2 shared across both services.

OWNER-OPS STEP 1: Uncomment render.yaml:88-98 and fix the stale comment at
  render.yaml:82-86 (delete "cannot share this one's SQLite disk" -- wrong
  since voice_service.py:88-95 writes relay). Commit and push. Render
  Blueprint provisions the new voice service.

OWNER-OPS STEP 2: Set env vars above in Render dashboard (Environment tab
  on each service). Do not commit secrets to git.

OWNER-OPS STEP 3: Premium voice ear-test (S-3). Log into Render voice
  service shell. Make real test calls using at least 4 Twilio
  ConversationRelay voice IDs on a realistic trades/appointment script.
  Pick the one that sounds warm and human (not corporate). Set
  FIRSTBACK_VOICE_TTS = <chosen-voice-id> on the voice service.
  Document choice in config.py:215 comment.

OWNER-OPS STEP 4: Run the 7-check acceptance gate (Section 4 below).

OWNER-OPS STEP 5 (only after all 7 pass): Flip pricing page to remove
  "coming soon" / "(beta)" from Pro/Crew voice checkmark. This is the
  only pricing change authorized by this spec -- and only after the gate.

----------------------------------------------------------------------
4. THE 7-CHECK ACCEPTANCE GATE
----------------------------------------------------------------------
All 7 must pass on a REAL deployment against real Twilio + real Claude/Haiku
+ real ConversationRelay. These cannot be verified in unit tests. They are
the gate before any tenant activation or pricing change.

Status of each gate from this build environment: ALL BLOCKED (no deploy).

  [  ] CHECK 1 -- DEPLOY GATE
       Render voice service is running.
       FIRSTBACK_VOICE_URL is set on the web service.
       All 6 env vars are correctly set.
       VERIFY: curl https://<voice-service>.onrender.com/twiml?biz=1&lead=1
               returns valid ConversationRelay TwiML with wss:// URL.

  [  ] CHECK 2 -- END-TO-END REAL CALL
       Text "call me" from a real phone to Dave's FirstBack number.
       Confirm: Twilio dials the caller's number within 5 seconds.
       AI greets with recording disclosure ("This call may be recorded").
       Complete a 4-turn booking conversation.
       Confirm: estimate row in DB (check command center or /api/appointments).
       Owner alert SMS received with caller name, address, time.
       BLOCKED BY: Check 1 + streaming (Check 3) must land first.

  [  ] CHECK 3 -- STREAMING GATE (<1.5s first word)
       BLOCKED BY: Slice A (S-2) must be built and deployed.
       VERIFY: Time from caller finishing speech to first TTS word.
               Must be consistently under 1.5 seconds with Haiku.
               If over 2s: diagnose (network, model, relay latency).

  [  ] CHECK 4 -- BARGE-IN GATE
       BLOCKED BY: Slice A (S-2) must be built and deployed.
       VERIFY: Interrupt the AI mid-sentence. It stops speaking immediately.
               Next utterance is processed correctly (no double-processing).

  [  ] CHECK 5 -- VOICEMAIL GATE
       BLOCKED BY: Slice D (M-1) must be built and deployed.
       VERIFY: Text "call me". Let the call ring to voicemail.
               Confirm: AI does NOT speak a booking pitch into voicemail.
               Confirm: Recovery SMS arrives within 30 seconds.
               Confirm: voice_calls row shows outcome=voicemail.

  [  ] CHECK 6 -- QUIET-HOURS GATE
       VERIFY: Text "call me" after 9 PM (or adjust QUIET_END for test).
               Confirm: No call is placed.
               Confirm: Correct after-hours text is sent verbatim:
               "Thanks. It is currently after hours, so we will call you
               during business hours. You can also keep texting here any time."

  [  ] CHECK 7 -- PREMIUM VOICE EAR-TEST
       BLOCKED BY: Owner-ops Step 3 above.
       VERIFY: The selected TTS voice sounds warm and human on a realistic
               painting/appointment script. Not corporate. Not robotic.
               At least 4 voices auditioned before selecting.

GATE POLICY: If any of checks 1-7 fail, fix before activating for any tenant.
Until all 7 pass: pricing page shows voice as "coming soon" on Pro/Crew.
This is already true today -- do not change it until the gate is passed.

----------------------------------------------------------------------
5. FILE-DISJOINT SLICE SPLIT (full file lists)
----------------------------------------------------------------------
Slices are ordered by dependency. Build in this order.

  SLICE A -- S-2 Streaming + barge-in
    Touches: voice_service.py, app.py, llm.py
    New endpoints: /internal/voice/stream (app.py)
    Does NOT touch: config.py, db.py, messaging.py, compliance.py

  SLICE B -- S-4 Pre-call guard additions
    Touches: app.py (STOP handler + pre-call guard block)
    Depends on: S-5 for last_voice_call_at() but writes the guard now
                with a None-safe fallback
    Does NOT touch: voice_service.py, llm.py, messaging.py, db.py (yet)

  SLICE C -- S-5 Voice metering
    Touches: db.py (new table + helpers), messaging.py (StatusCallback),
             app.py (new /webhooks/twilio/voice/status endpoint + credit check),
             config.py (VOICE_MONTHLY_CAP_CENTS constant)
    Does NOT touch: voice_service.py, llm.py

  SLICE D -- M-1 AMD / voicemail
    Touches: messaging.py (MachineDetection params), app.py (voicemail path
             in twilio_voice_status -- built in Slice C)
    Does NOT touch: voice_service.py, db.py, llm.py, config.py

  SLICE E -- M-2/M-3/M-4/M-5 Session hygiene
    Touches: voice_service.py (/ws loop), app.py (new /internal/voice/turn_log),
             llm.py (voice system prompt addition for M-4)
    Does NOT touch: db.py, messaging.py, config.py (except db.add_message usage)

  OWNER-OPS only (not code):
    render.yaml (uncomment + fix stale comment) -- owner deploys
    Render dashboard env vars -- owner sets
    FIRSTBACK_VOICE_TTS env var -- owner sets after ear-test

----------------------------------------------------------------------
6. UNIT TESTS (all runnable without deploy)
----------------------------------------------------------------------
All new tests go in test_voice.py (already exists with 9 passing checks).

  Slice A tests:
    - _say(text, last=False) produces {"type":"text","token":text,"last":false}
    - /ws sends filler frame before streaming begins (mock stream)
    - /ws cancel_flag stops frame emission on "interrupt"
    - /ws final frame has last=True
    - /internal/voice/stream uses CLAUDE_MODEL_VOICE (Haiku), not CLAUDE_MODEL

  Slice B tests:
    - STOP text sets voice_ok=0 via set_voice_consent(biz_id, caller, False)
    - detect_revocation() path also clears voice_ok
    - Pre-call guard skips place_call when voice_ok=0
    - Pre-call guard skips place_call when spam score >= SCREEN_SCORE_HARD
    - last_voice_call_at returns None when no voice_calls table (safe no-op)

  Slice C tests:
    - init_db() creates voice_calls table with correct columns
    - insert_voice_call() + update_voice_call_outcome() round-trip
    - voice_spend_this_month() sums correctly within calendar month
    - voice_spend_this_month() excludes prior months
    - /webhooks/twilio/voice/status sets outcome=voicemail on AnsweredBy=machine_start
    - /webhooks/twilio/voice/status sets outcome=no_answer on status=no-answer
    - place_call() passes StatusCallback when PUBLIC_BASE_URL is set
    - monthly cap exceeded: place_call is skipped + text fallback sent

  Slice D tests:
    - place_call() data dict contains MachineDetection="Enable" and AsyncAmd="true"
    - voicemail detection: recovery SMS sent, no spoken TwiML to voicemail

  Slice E tests:
    - 5 consecutive empty prompts triggers graceful WS close
    - turn_log accumulates and POSTs [VOICE] messages on disconnect
    - post-call SMS fires with "cut off" body on unexpected disconnect
    - post-call SMS fires with "keep texting" body on turn-limit exit

  INTEGRATION TESTS (need deploy -- cannot run here):
    - Real end-to-end call with booking (Gate 2)
    - Latency measurement for Gate 3 (<1.5s first word)
    - Real barge-in test (Gate 4)
    - Real voicemail drop (Gate 5)
    - Quiet-hours real test (Gate 6)

----------------------------------------------------------------------
7. RISKS
----------------------------------------------------------------------
  RISK 1 -- STREAMING LATENCY (highest variance, hard to predict offline)
    Even with Haiku, the relay chain is:
      Caller -> Twilio STT -> /ws -> /internal/voice/stream -> LLM (Haiku)
      -> token stream back -> /ws -> Twilio TTS -> caller
    Each hop adds latency. The Render-to-Render internal HTTP hop for
    /internal/voice/stream could add 50-200ms. If Haiku + relay consistently
    exceeds 1.5s, options are:
      (a) Run the LLM call IN the voice service process (bypass relay)
          by importing llm.py directly when WEB_INTERNAL_URL is set --
          but this means the voice service needs ANTHROPIC_API_KEY and
          the booking write still goes through the relay for DB safety.
      (b) Switch the internal relay to a WebSocket stream (avoids SSE).
      (c) Pre-generate short fillers for common transitions ("Let me
          check..." buys 500ms).
    CANNOT KNOW until real calls are measured. This is the single highest
    variance item in Phase 5.

  RISK 2 -- BARGE-IN RECONCILIATION
    Twilio's ConversationRelay interrupt event arrives AFTER Twilio has
    already stopped speaking. The cancel_flag approach in Slice A stops
    sending NEW tokens, but tokens already sent and being spoken cannot
    be recalled. This is the expected behavior (Twilio discards queued
    TTS when barge-in fires) but the AI must handle mid-sentence context
    gracefully. If the homeowner barges in mid-sentence, the next LLM turn
    will get a conversation history that ends with a partial AI sentence.
    The system prompt should instruct the AI to treat partial context as
    complete and respond to the new utterance.

  RISK 3 -- DOUBLE-BOOKING RACE (voice + SMS concurrent)
    If a homeowner texts "call me" and then texts a slot in the 2-3 seconds
    before the call connects, two booking paths could run concurrently. The
    DB UNIQUE slot constraint (pre-existing) prevents double-commit, but the
    voice path might respond "booked" while the SMS path already booked and
    the slot was taken. Guard: check appointment availability inside
    _process_turn BEFORE the [[BOOK]] turn, same as the SMS path does.
    This is already protected by the UNIQUE constraint; the voice path
    inherits it through handle_inbound. No additional code needed, but
    confirm in integration testing.

  RISK 4 -- RENDER STARTER PLAN COLD START
    Render Starter plan ($7/mo) has no CPU reservation. Under low traffic
    the voice service may cold-start (15-30s) on the first call of the day.
    This would cause Twilio to time out the TwiML request. Fix: upgrade
    voice service to Render Standard plan ($25/mo) which has reserved CPU,
    OR implement a lightweight health-ping cron to keep the service warm.
    This is an operational decision for the owner after initial deploy.

  RISK 5 -- BOOKING BRAIN NOT VOICE-OPTIMIZED
    handle_inbound was designed for SMS (multi-turn, async). On voice it
    runs synchronously with the call in progress. The booking brain may:
      (a) produce replies that are too long for TTS (homeowner hangs up
          waiting for the AI to finish a 3-sentence response)
      (b) ask for information the homeowner already gave verbally
      (c) not handle "um", "uh", ASR fillers gracefully
    Mitigation: add a voice-path system prompt instruction to keep replies
    to 1-2 sentences maximum, speak naturally, and handle ASR uncertainty.
    This is a prompt change (no code), but it must be tested on real calls
    before the gate passes. Low cost, not zero risk.

----------------------------------------------------------------------
8. BUILD RECOMMENDATION
----------------------------------------------------------------------
BUILD NOW vs DEFER:

  BUILD NOW (code-completable without owner):
    Slice A (S-2 streaming)  -- highest-impact, blocks the quality gate
    Slice B (S-4 pre-call guards)  -- legal requirement, small
    Slice C (S-5 metering)  -- cost protection, medium
    Slice D (M-1 AMD)  -- never babble into voicemail, medium
    Slice E (M-2/M-3/M-4/M-5 hygiene)  -- no lead stranded

  DEFER UNTIL OWNER IS READY TO DEPLOY:
    Everything in Section 3 (owner-ops) -- env vars, uncomment render.yaml,
    ear-test, 7-check gate.

  RECOMMENDATION: Build all 5 code slices (A through E) now. They are
  unit-testable, file-disjoint, and do not activate voice for any user.
  The gate in Section 4 is the firewall. 5g is "code-complete behind the
  gate" after the 5 slices pass their unit tests. Mark 5g DONE only after
  a real deployment passes all 7 checks. That step requires the owner.

  DO NOT ship 5g as "live" from this environment. The 7-check gate is
  non-negotiable. Mediocre voice is the only failure mode in Phase 5 that
  actively damages Dave's reputation with homeowners.

----------------------------------------------------------------------
END OF SPEC
----------------------------------------------------------------------
