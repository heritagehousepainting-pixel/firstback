# 5G Build Correctness Audit
Date: 2026-06-18
Branch: staging
Commit: 5752ba6
Auditor: read-only correctness/integration lens (no source edits)

Suite baseline: 60/60 standalone tests green (test_voice.py 11, test_voice_stream.py 39,
test_voice_app.py 41, test_voice_metering.py 31, test_voice_llm.py 13).

---

## FINDINGS

---

### P1-1 — _send_recovery_sms sentinel passes __RECOVERY_SMS__: text to handle_inbound

**Files:** `voice_service.py:221-227`, `app.py:2944-2959`
**Severity:** P1 — production data corruption, no code path handles it

**What happens:** In production (WEB_INTERNAL_URL set), `_send_recovery_sms()` calls
`/internal/voice/turn` with `{"text": "__RECOVERY_SMS__:Hello test"}`. The
`internal_voice_turn()` endpoint at `app.py:2958` passes that sentinel verbatim to
`handle_inbound()` as a customer utterance. `handle_inbound` treats it as real customer
text, generates a bot reply to `__RECOVERY_SMS__:...`, records the sentinel in
`messages`, and may charge an LLM credit for it.

**Confirmed by probe:** `POST /internal/voice/turn` with `text="__RECOVERY_SMS__:Hello"`
→ `handle_inbound` called with `['__RECOVERY_SMS__:Hello test']`. No sentinel stripping exists.

**Not exercised by any test:** All M-5 tests mock `_send_recovery_sms` at the
`voice_service` level. The sentinel production path (`WEB_INTERNAL_URL` set) has zero coverage.

**Fix options (pick one):**
1. Add a new `/internal/voice/sms` endpoint in `app.py` that takes `{biz, lead, body}`
   and calls `messaging.send_sms()` directly. Update `_send_recovery_sms` in
   `voice_service.py:225-227` to POST to `/internal/voice/sms` instead of `/internal/voice/turn`.
2. Detect the sentinel in `internal_voice_turn()` before passing to `handle_inbound`:
   ```python
   text_raw = (data.get("text") or "").strip()
   if text_raw.startswith("__RECOVERY_SMS__:"):
       body = text_raw[len("__RECOVERY_SMS__:"):]
       # send_sms to lead.phone and return
       ...
   ```
Option 1 is cleaner — no sentinel magic leaking into a booking endpoint.

---

### P1-2 — mtype=="error" break silently drops turn_log and recovery SMS

**File:** `voice_service.py:426-429`, `431-459`
**Severity:** P1 — M-3 transcript and M-5 recovery SMS are silently dropped

**What happens:** When Twilio sends a ConversationRelay `error` message, the handler
logs it and calls `break` (line 429). `break` exits the `while True` loop normally,
exiting the `try` block without raising an exception. Neither the `except WebSocketDisconnect`
(line 431) nor the `except Exception` (line 453) handler fires for a normal `break`.
Result: `_post_turn_log()` is never called and recovery SMS is never sent.

**Fix:** Add a `finally` block after the two `except` clauses that always posts the
turn log, or restructure to call `_post_turn_log` before every exit point:
```python
finally:
    # Ensure turn log is always posted regardless of exit path
    try:
        asyncio.get_event_loop().run_until_complete(
            _post_turn_log(biz_id, lead_id, turn_log))
    except Exception:
        pass
```
Or more cleanly: extract the cleanup into a helper and call it before every `break`/`return`.

---

### P1-3 — AMD voicemail detection missing machine_end_beep

**Files:** `app.py:3051`, `phase5/PHASE5G-SPEC.md:208-210`
**Severity:** P1 — voicemail calls with audible beep silently fall to "error" outcome

**What happens:** Twilio AsyncAMD can return `AnsweredBy` values:
`machine_start`, `machine_end_silence`, `machine_end_other`, **`machine_end_beep`**, `human`, `fax`.
The code (and the spec body it mirrors) checks only the first three:
```python
if answered_by in ("machine_start", "machine_end_silence", "machine_end_other"):
    outcome = "voicemail"
```
`machine_end_beep` — the most common voicemail-reached-the-beep result — falls through
to the `else: outcome = "error"` branch. The call gets no recovery SMS. The booking
pitch IS spoken into the voicemail inbox (ConversationRelay opens the WebSocket).

**Fix:** Add `"machine_end_beep"` to the tuple check at `app.py:3051`:
```python
if answered_by in ("machine_start", "machine_end_silence",
                   "machine_end_other", "machine_end_beep"):
```

---

## P2 FINDINGS

---

### P2-1 — status=completed sets outcome="completed", not in spec enum

**File:** `app.py:3055-3056`
**Severity:** P2 — outcome value is not in spec-defined enum; "booked" outcome is never set

**What happens:** When Twilio posts `CallStatus=completed`, the status webhook sets
`outcome = "completed"` (line 3056). The spec-defined enum for `voice_calls.outcome` is
`booked | no_answer | voicemail | abandoned | error | in_progress`. "completed" is not in
this set. The spec body (§3, line 214) said `outcome = "booked"` with a note "actual booking
state is in voice_calls.outcome updated by /ws disconnect handler." The code comment at
line 3037 says "completed → keep existing outcome (set by /ws)" — but the `/ws` disconnect
handler in `voice_service.py` has no mechanism to update `voice_calls.outcome` (it has no
DB access and no endpoint to call for this). So a successful booked call ends with
`voice_calls.outcome = "completed"`, which no downstream query treats as "booked".

This is a semantic deviation: the `voice_spend_this_month()` and `last_voice_call_at()`
helpers work on the `voice_calls` table and don't care about the outcome value, so
metering is unaffected. However, any future reporting or "was this call a booking?" query
will not find a "booked" outcome for voice calls.

**Fix options:** Either set `outcome = "booked"` for `CallStatus = completed` (simpler,
aligns with spec body), or add a new `/internal/voice/outcome` endpoint that
`voice_service.py` can POST to on WebSocketDisconnect to set the real outcome.

---

### P2-2 — /internal/voice/stream uses only M-4 confirmation-echo prompt, not the booking brain

**File:** `app.py:2986`, `llm.py:45-55`
**Severity:** P2 — UX tokens heard by caller are generic; misaligned with committed reply

**What happens:** The SSE streaming endpoint at `app.py:2986` sets:
```python
system = _llm.VOICE_CONFIRM_BOOKING_PROMPT
```
This is the 6-sentence M-4 instruction ("before writing [[BOOK]]..."). The full booking
brain system prompt — which includes business context, persona, appointment-slot logic,
and conversational rules — is NOT included. The LLM generating the UX tokens has no
knowledge of the business, what services they offer, or the booking flow. It will produce
generic filler ("Got it!" / "Let me check...") or hallucinate business details.

Meanwhile, `/internal/voice/turn` calls `handle_inbound()` which uses the full booking
brain and produces the real committed reply. The caller HEARS the stream tokens (generic)
but the BOOKING WRITES use the turn reply (full brain). If the caller barges in at the
right moment they may confirm a booking slot that differs from what the stream said.

The design intent (P0-2: UX-only stream + commit once at turn end) is architecturally
sound. The gap is that the stream LLM call uses a degenerate system prompt. At minimum,
the booking brain's condensed context should be appended to `VOICE_CONFIRM_BOOKING_PROMPT`
for the stream call.

**Note:** This is a design gap, not a safety regression — the actual booking commit always
goes through `handle_inbound()` with the real brain. The risk is caller confusion from
hearing generic text.

---

### P2-3 — Barge-in cancel_flag cannot interrupt mid-stream in single-coroutine asyncio

**File:** `voice_service.py:268-430`, `test_voice_stream.py:323-509`
**Severity:** P2 — tests imply mid-stream barge-in works; it does not

**What happens:** The `/ws` handler is a single asyncio coroutine. It processes messages
sequentially: `await websocket.receive_text()` → process → repeat. While executing the
`async for (kind, val) in _stream_tokens(...)` loop, the only yields to the event loop
are within the httpx streaming client. No `receive_text()` is awaited during streaming.
Therefore, an `interrupt` message from Twilio sits in the WebSocket receive buffer until
the current stream completes and the outer `while True` loop calls `receive_text()` again.
The `cancel_flag` is only SET after the stream finishes, not during it.

The practical effect: the booking commit is correctly skipped when barge-in fires between
turns (cancel_flag is set on the next message receive). But the TTS frames for the CURRENT
utterance are all sent regardless of barge-in. The caller sees Twilio stop speaking (Twilio
discards queued TTS on barge-in) but the voice service still generates and sends all frames.
This is exactly what the spec's RISK 2 acknowledges.

**Test gap:** The barge-in tests (test_voice_stream.py:323-509) test the cancel_flag
BETWEEN turns (sequential), not truly mid-stream. The check names "Barge-in: cancel_flag
stops frame emission on interrupt" is misleading — it tests clearing behavior between
turns, not actual mid-stream interruption. The test comment at line 327-333 correctly
documents this limitation. This is a test-name clarity issue, not a test logic error.

**Fix for true mid-stream barge-in:** Run a second concurrent coroutine that monitors
WebSocket messages for `interrupt` during streaming. This requires `asyncio.create_task()`
for the stream + listen concurrently — a meaningful refactor. Deploy-gated (Check 4)
to discover actual real-world impact before investing.

---

## VERIFIED CORRECT (confirmed by code + tests)

1. **P0-1 (Model):** `complete_stream_voice()` hardcodes `CLAUDE_MODEL_VOICE`
   (`claude-haiku-4-5`). `tool_complete_stream(model=None)` defaults to `CLAUDE_MODEL`
   (Sonnet) — no regression. The new `model` param on `tool_complete_stream` is correct.
   Verified by `test_voice_llm.py` tests 3-5 with real anthropic SDK mock.

2. **P0-2 (No double-book/double-LLM):** `/internal/voice/stream` does NOT call
   `handle_inbound` (confirmed by `test_voice_app.py R3` spy). The booking commit
   POSTs to `/internal/voice/turn` exactly once at stream END, gated on
   `full_reply and not cancel_flag.is_set() and WEB_INTERNAL_URL`
   (`voice_service.py:379-398`). Verified.

3. **SSE frame shape:** `_say(text, last=False)` → `{"type":"text","token":text,"last":false}`.
   `_say(text, last=True)` → `{"type":"text","token":text,"last":true}`. Conforms to
   Twilio ConversationRelay spec. Verified by `test_voice_stream.py §1`.

4. **INTERNAL_SECRET header:** Both sides use `X-Internal-Secret`. `voice_service.py:124`
   sends it; `app.py:2947/2970/3004` reads and constant-time-compares it with
   `secrets.compare_digest`. Shapes match across slices.

5. **Request/response shapes across slices:**
   - `/internal/voice/stream`: `POST {biz,lead,text,history}` →
     `SSE {"delta":tok}... {"done":true,"full":text}`. Matches `voice_service.py:156-157`
     (send) and `app.py:2988-2993` (emit).
   - `/internal/voice/turn`: `POST {biz,lead,text}` → `{"reply","booked","urgent"}`.
     Matches `voice_service.py:383-398` (consume) and `app.py:2958-2959` (emit).
   - `/internal/voice/turn_log`: `POST {biz,lead,turns:[{in,out}...]}` → `{"ok","written"}`.
     Matches `voice_service.py:191` (send) and `app.py:3016-3031` (receive).

6. **Pre-call guard ORDER (consent → quiet-hours → spam → 60-min dedupe → cost cap):**
   Exactly matches spec §9 R2 order. Verified by `test_voice_app.py R2a-R2d`.

7. **R1 STOP/detect_revocation/cancel all revoke voice_ok:**
   `db.set_voice_consent(biz_id, caller, False)` called on all three exit paths.
   `app.py:2664, 2669, 2676`. Verified by `test_voice_app.py R1`.

8. **Cost math:** `math.ceil(duration / 30) * VOICE_CREDIT_RATE_CENTS`. Duration=0 → 0 cents.
   Duration=45 → `ceil(1.5)=2 → 50 cents`. Matches spec and test R5.

9. **voice_calls table schema:** All 11 columns match spec §2 schema definition exactly.
   Verified by `test_voice_metering.py §1`.

10. **AMD params in place_call:** `MachineDetection="Enable"`, `AsyncAmd="true"`,
    `AsyncAmdStatusCallback` wired to the same StatusCallback URL. `messaging.py:227-231`.
    Verified by `test_voice_metering.py §5`.

11. **M-2 ASR guard:** 3 empties → filler frame. 5 empties → close + recovery SMS.
    `voice_service.py:300-323`. Verified by `test_voice_stream.py §5`.

12. **M-3 transcript:** `turn_log` accumulates per prompt, POSTed to `/internal/voice/turn_log`
    on `WebSocketDisconnect`. Phone numbers redacted via `_phone_re` regex before writing.
    Verified by `test_voice_app.py R4` and `test_voice_stream.py §6`.

13. **M-4 confirmation echo prompt:** `VOICE_CONFIRM_BOOKING_PROMPT` in `llm.py:45-55`
    correctly references `[[BOOK]]` and requires slot confirmation. Used in
    `/internal/voice/stream`. Verified by `test_voice_llm.py §2`.

14. **M-5 recovery SMS outcomes:**
    - booked → no extra SMS (standard confirmation fires via `handle_inbound`).
    - no-booking (turn_count > 0) → "I enjoyed our chat" SMS.
    - drop (turn_count == 0) → "Looks like we got cut off" SMS.
    Local fallback path verified by `test_voice_stream.py §7`.

15. **inspect shim for add_amd:** `app.py:2762-2767` uses `inspect.signature` to check
    whether `place_call` has `add_amd` before passing it. `messaging.place_call` DOES
    have `add_amd` (confirmed). The shim is currently dead code (always takes the
    `if "add_amd" in _pc_params` branch), but it is safe and non-fragile.

16. **clean_exit flag:** Prevents double-SMS in M-2 path. Set to `True` before
    `websocket.close(); return` — the `except WebSocketDisconnect` block checks
    `if not clean_exit:` before sending recovery SMS. `voice_service.py:304, 436`.

17. **FCC recording disclosure:** `build_twiml()` greeting always includes "may be recorded".
    `voice_service.py:234`. Non-configurable by design.

---

## DEPLOY-GATED — CANNOT VERIFY HERE

The following cannot be verified without a real deployment and real Twilio credentials:

- **Check 2:** End-to-end real call with booking (real Twilio + real Claude + real /ws).
- **Check 3:** Streaming latency < 1.5s first word. The Render-to-Render relay hop
  (`/internal/voice/stream`) adds 50-200ms per spec RISK 1. Cannot measure offline.
- **Check 4:** Real barge-in behavior (see P2-3: structural limit in single-coroutine asyncio).
- **Check 5:** Voicemail drop — AMD real `AnsweredBy` values; recovery SMS delivery.
- **Check 6:** Quiet-hours real call suppression (test configures `QUIET_START=0, QUIET_END=24`).
- **Check 7:** TTS voice ear-test (FIRSTBACK_VOICE_TTS not set; no voice selected yet).
- **FIRSTBACK_INTERNAL_SECRET / FIRSTBACK_WEB_URL / FIRSTBACK_VOICE_URL:** None set;
  all production inter-service auth is untested.

---

## SUMMARY TABLE

| ID    | Severity | Area                     | Status |
|-------|----------|--------------------------|--------|
| P1-1  | P1       | RECOVERY_SMS sentinel unhandled in /internal/voice/turn | BUG |
| P1-2  | P1       | error mtype break drops M-3 turn_log + M-5 SMS | BUG |
| P1-3  | P1       | machine_end_beep missing from AMD voicemail check | BUG |
| P2-1  | P2       | completed not in spec outcome enum; booked never set | DEVIATION |
| P2-2  | P2       | /internal/voice/stream uses degenerate system prompt | DESIGN GAP |
| P2-3  | P2       | Barge-in cancel_flag is between-turns only, not mid-stream | LIMITATION |

3 P1 bugs. 3 P2 issues. 0 P0 issues.
All 60 standalone tests pass. All P1 bugs are in paths that tests do not exercise.
