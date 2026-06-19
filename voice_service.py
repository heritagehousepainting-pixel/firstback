"""FirstBack AI voice agent -- a SEPARATE async service (Phase 3).

Flask/WSGI cannot host a long-lived WebSocket, and Twilio ConversationRelay needs
one, so the voice leg runs here as its own ASGI app (FastAPI/uvicorn) alongside
the Flask app. Run it with:

    python voice_service.py            # uvicorn on FIRSTBACK_VOICE_PORT (default 8810)

It exposes two endpoints Twilio talks to during an outbound AI callback:
  * GET/POST /twiml -> the ConversationRelay TwiML (welcome greeting with the
    required AI + recording disclosure, the wss /ws URL, and the biz/lead ids).
  * WS /ws -> the ConversationRelay loop. Twilio does the speech-to-text and
    text-to-speech; we just turn each caller utterance into text, run it through
    the SAME shared conversation engine the simulator and SMS webhooks use
    (app.handle_inbound), and send the reply text back for Twilio to speak. So a
    voice call books the estimate, alerts the owner, and queues the reminder with
    zero extra logic.

The brain is whatever FIRSTBACK_PROVIDER selects (Claude for launch, the demo
responder offline) -- identical to the text path. Booking integrity is the DB
UNIQUE slot constraint, shared across both processes (SQLite in WAL mode).

See CALLBACK_SYSTEM_PLAN.md (Phase 3). The voice callback is placed only after the
customer texts "call me" (FCC AI-voice consent gate; see app.twilio_sms_inbound).

Phase 5G Slice 1 additions:
  S-2  Streaming /ws handler: filler frame -> SSE token stream -> last=True frame
       -> booking commit via POST /internal/voice/turn at stream END (P0-2).
  S-2  Barge-in: session-level cancel_flag (asyncio.Event); "interrupt" sets it;
       streaming loop checks after each send and aborts current utterance.
  M-2  ASR guard: consecutive_empty counter; 3 -> filler; 5 -> graceful close +
       recovery SMS.
  M-3  Transcript: turn_log accumulated per session; POST to /internal/voice/turn_log
       on WebSocketDisconnect or graceful close.
  M-5  Post-call recovery SMS per outcome (via web app relay, not direct Twilio).
"""
import asyncio
import json
import random
import sys
from enum import Enum

import httpx
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect

from config import (VOICE_PUBLIC_URL, CONVERSATIONRELAY_VOICE, VOICE_SERVICE_PORT,
                    WEB_INTERNAL_URL, INTERNAL_SECRET)

# This service does NOT import the Flask app or the DB at module load. In production
# (WEB_INTERNAL_URL set) it relays each turn to the web app's /internal/voice/turn
# over HTTP, so it stays stateless and never needs the shared SQLite disk. Only the
# local / in-process fallback (WEB_INTERNAL_URL unset) lazily imports app + db.

fastapi_app = FastAPI()

# ---------------------------------------------------------------------------
# Filler phrases (rotate randomly so the caller hears variety)
# ---------------------------------------------------------------------------
_FILLERS = [
    "Mm-hmm, one moment.",
    "Let me check on that.",
    "Sure, just a sec.",
]

# ---------------------------------------------------------------------------
# Session outcome states (for M-5 post-call SMS routing)
# ---------------------------------------------------------------------------
class _Outcome(str, Enum):
    BOOKED = "booked"
    NO_BOOKING = "no_booking"
    DROP = "drop"


def _xesc(s):
    """Escape a value for safe placement inside a TwiML attribute or text node."""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _isint(s):
    try:
        int(s)
        return True
    except (TypeError, ValueError):
        return False


def _wss_base():
    """Our public wss origin, from VOICE_PUBLIC_URL (https -> wss). Empty if unset
    (the caller then falls back to the request host)."""
    base = (VOICE_PUBLIC_URL or "").rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


def _greeting_name(biz_id, name=None):
    """Business name for the spoken greeting. In production it arrives as the ?name=
    query param the web app builds into the TwiML URL (no DB needed here); locally
    (no WEB_INTERNAL_URL) we look it up directly."""
    if name:
        return name
    if WEB_INTERNAL_URL:
        return "our team"   # stateless: never touch a DB this service doesn't own
    try:
        import db
        biz = db.get_business(int(biz_id)) if _isint(biz_id) else None
        return (biz or {}).get("name") or "our team"
    except Exception:
        return "our team"


def _process_turn(biz_id, lead_id, text):
    """Get the AI's reply for one spoken turn. Production (WEB_INTERNAL_URL set):
    POST to the web app's internal seam so booking writes stay single-writer on the
    DB owner. Local/tests: run the shared engine in-process."""
    if WEB_INTERNAL_URL:
        import requests
        r = requests.post(
            WEB_INTERNAL_URL.rstrip("/") + "/internal/voice/turn",
            json={"biz": biz_id, "lead": lead_id, "text": text},
            headers={"X-Internal-Secret": INTERNAL_SECRET}, timeout=30)
        r.raise_for_status()
        return (r.json() or {}).get("reply", "")
    import db
    import app as flask_app
    biz = db.get_business(int(biz_id)) if _isint(biz_id) else None
    lead = db.get_lead(int(lead_id)) if _isint(lead_id) else None
    if not biz or not lead:
        return ""
    reply, _booked, _urgent = flask_app.handle_inbound(biz, lead, text)
    return reply


async def _stream_tokens(biz_id, lead_id, text, history):
    """Open an async HTTP stream to /internal/voice/stream and yield raw SSE lines.

    Yields (kind, value) tuples:
      ("__TOKEN__", tok)  -- an intermediate token to speak with last=False
      ("__DONE__", full)  -- stream complete; full is the full reply text

    Falls back to _process_turn (sync) when WEB_INTERNAL_URL is unset (local/tests).
    In local mode emits only __DONE__ (no filler / intermediate tokens) so existing
    tests that read exactly one frame per turn continue to work.
    """
    if not WEB_INTERNAL_URL:
        # Local / test fallback: run the engine synchronously and yield done only.
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(None, _process_turn, biz_id, lead_id, text)
        yield ("__DONE__", reply)
        return

    url = WEB_INTERNAL_URL.rstrip("/") + "/internal/voice/stream"
    payload = {"biz": biz_id, "lead": lead_id, "text": text, "history": history}
    headers = {"X-Internal-Secret": INTERNAL_SECRET}
    full_text = ""
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for raw_line in resp.aiter_lines():
                raw_line = raw_line.strip()
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                data_str = raw_line[len("data:"):].strip()
                try:
                    chunk = json.loads(data_str)
                except ValueError:
                    continue
                if chunk.get("done"):
                    full_text = chunk.get("full", full_text)
                    yield ("__DONE__", full_text)
                    return
                tok = chunk.get("delta", "")
                if tok:
                    full_text += tok
                    yield ("__TOKEN__", tok)
    # If the stream ended without a {done} sentinel, emit done with what we have.
    yield ("__DONE__", full_text)


async def _post_turn_log(biz_id, lead_id, turn_log):
    """POST the accumulated turn_log to /internal/voice/turn_log (M-3).
    No-op when WEB_INTERNAL_URL is unset (local / tests without the web app)."""
    if not turn_log:
        return
    if not WEB_INTERNAL_URL:
        return
    try:
        payload = {"biz": biz_id, "lead": lead_id, "turns": turn_log}
        headers = {"X-Internal-Secret": INTERNAL_SECRET}
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                WEB_INTERNAL_URL.rstrip("/") + "/internal/voice/turn_log",
                json=payload, headers=headers)
    except Exception as exc:
        print(f"[firstback] turn_log POST failed: {exc}", file=sys.stderr, flush=True)


async def _send_recovery_sms(biz_id, lead_id, body):
    """Send a post-call recovery SMS via the web app relay (M-5).
    Uses /internal/voice/turn with a sentinel text that the app interprets as
    a direct send -- but we are in the voice service which has no Twilio creds.
    We relay via the web app's /internal/voice/turn by sending a special payload,
    OR fall back to in-process messaging if WEB_INTERNAL_URL is unset."""
    if not WEB_INTERNAL_URL:
        # Local / test fallback: attempt direct via messaging module.
        try:
            import db
            import messaging
            lead = db.get_lead(int(lead_id)) if _isint(lead_id) else None
            biz = db.get_business(int(biz_id)) if _isint(biz_id) else None
            if biz and lead and lead.get("phone"):
                messaging.send_sms(biz, lead["phone"], body)
        except Exception as exc:
            print(f"[firstback] recovery SMS local fallback failed: {exc}",
                  file=sys.stderr, flush=True)
        return
    try:
        payload = {"biz": biz_id, "lead": lead_id,
                   "text": f"__RECOVERY_SMS__:{body}"}
        headers = {"X-Internal-Secret": INTERNAL_SECRET}
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                WEB_INTERNAL_URL.rstrip("/") + "/internal/voice/turn",
                json=payload, headers=headers)
    except Exception as exc:
        print(f"[firstback] recovery SMS relay failed: {exc}", file=sys.stderr, flush=True)


def build_twiml(biz_id, lead_id, wss_base=None, name=None):
    """The ConversationRelay TwiML for an AI voice call. Pure + testable."""
    greeting = (f"Hi, this is the scheduling assistant for {_greeting_name(biz_id, name)}. "
                "This call may be recorded. How can I help you book your free estimate?")
    base = (wss_base or _wss_base() or "").rstrip("/")
    ws_url = f"{base}/ws?biz={biz_id}&lead={lead_id}"
    voice_attr = f' voice="{_xesc(CONVERSATIONRELAY_VOICE)}"' if CONVERSATIONRELAY_VOICE else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Connect>'
        f'<ConversationRelay url="{_xesc(ws_url)}"{voice_attr} '
        f'welcomeGreeting="{_xesc(greeting)}">'
        f'<Parameter name="biz" value="{_xesc(biz_id)}"/>'
        f'<Parameter name="lead" value="{_xesc(lead_id)}"/>'
        '</ConversationRelay></Connect></Response>')


@fastapi_app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request):
    biz_id = request.query_params.get("biz", "")
    lead_id = request.query_params.get("lead", "")
    name = request.query_params.get("name", "")
    # If VOICE_PUBLIC_URL isn't set (e.g. local ngrok), derive wss from the host
    # Twilio reached us on. Twilio always uses TLS, so wss:// is correct.
    fallback = None
    if not _wss_base():
        fallback = "wss://" + request.headers.get("host", "")
    return Response(content=build_twiml(biz_id, lead_id, fallback, name or None),
                    media_type="text/xml")


def _say(text, last=True):
    """A ConversationRelay 'text' frame (Twilio speaks it via TTS)."""
    return json.dumps({"type": "text", "token": text, "last": last})


@fastapi_app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    biz_id = lead_id = None

    # Session state
    cancel_flag = asyncio.Event()        # barge-in: set on "interrupt"
    consecutive_empty = 0               # M-2 ASR guard counter
    turn_log = []                       # M-3 transcript
    booked = False                      # M-5: did this call end in a booking?
    turn_count = 0                      # M-5: number of turns taken
    history = []                        # conversation history for /internal/voice/stream
    cleanup_done = False                # guard so cleanup runs exactly once

    async def _finalize(force_cutoff=False):
        """Post the M-3 turn log + send the M-5 recovery SMS exactly once, on ANY
        exit path. Centralized in a `finally` so the `error`-message `break` (5g P1)
        no longer silently drops the transcript + recovery SMS. `force_cutoff` is the
        M-2 graceful-close path which always sends the 'cut off' text."""
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        try:
            await _post_turn_log(biz_id, lead_id, turn_log)
        except Exception:
            pass
        if booked:
            return  # booking confirmation already fired via handle_inbound
        if force_cutoff or turn_count == 0:
            body = ("Looks like we got cut off -- you can keep texting here "
                    "or text 'call me' to try again.")
        else:
            body = "I enjoyed our chat -- any questions, just text here."
        try:
            await _send_recovery_sms(biz_id, lead_id, body)
        except Exception:
            pass

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            mtype = msg.get("type")

            if mtype == "setup":
                params = msg.get("customParameters") or {}
                biz_id = params.get("biz") or websocket.query_params.get("biz")
                lead_id = params.get("lead") or websocket.query_params.get("lead")

            elif mtype == "prompt":
                text = (msg.get("voicePrompt") or "").strip()

                # M-2: empty utterance tracking
                if not text:
                    consecutive_empty += 1
                    if consecutive_empty >= 5:
                        # Graceful close -- 5 consecutive empties
                        try:
                            await websocket.send_text(
                                _say("I'm having trouble hearing you. "
                                     "Feel free to text us any time.", last=True))
                        except Exception:
                            pass
                        # Turn log + "cut off" recovery SMS, exactly once (guarded).
                        await _finalize(force_cutoff=True)
                        await websocket.close()
                        return
                    elif consecutive_empty >= 3:
                        await websocket.send_text(
                            _say("I'm having trouble hearing you. Could you try again?",
                                 last=True))
                    continue

                if not _isint(biz_id) or not _isint(lead_id):
                    continue

                # Reset empty counter on a real prompt
                consecutive_empty = 0

                # ---------------------------------------------------------
                # S-2: Streaming handler
                # ---------------------------------------------------------
                cancel_flag.clear()

                # (a) Send a filler frame immediately so TTS starts while LLM warms up.
                # Only in streaming/production mode (WEB_INTERNAL_URL set); in local/test
                # mode we emit a single last=True frame per turn (backward compat with
                # test_voice.py which reads exactly one frame per prompt).
                if WEB_INTERNAL_URL:
                    filler = random.choice(_FILLERS)
                    try:
                        await websocket.send_text(_say(filler, last=False))
                    except Exception:
                        break

                # (b)/(c)/(d) Stream tokens from /internal/voice/stream
                full_reply = ""
                try:
                    async for (kind, val) in _stream_tokens(biz_id, lead_id, text, history):
                        if cancel_flag.is_set():
                            # Barge-in: caller interrupted; stop emitting frames for
                            # this utterance. The next "prompt" message starts fresh.
                            break
                        if kind == "__TOKEN__":
                            try:
                                await websocket.send_text(_say(val, last=False))
                            except Exception:
                                break
                            full_reply += val
                        elif kind == "__DONE__":
                            full_reply = val or full_reply
                            if not cancel_flag.is_set():
                                # (d) Final frame with last=True
                                try:
                                    await websocket.send_text(_say(full_reply, last=True))
                                except Exception:
                                    pass
                            break
                except Exception as exc:
                    print(f"[firstback] voice stream error: {exc}",
                          file=sys.stderr, flush=True)

                # (e) P0-2: commit booking ONCE at stream END via /internal/voice/turn
                # We do this only when NOT cancelled (partial barge-in) and we have a reply.
                # For the local fallback (_stream_tokens already called _process_turn), we
                # skip the duplicate commit -- the local path returns the full reply directly
                # and does not need a second POST to /internal/voice/turn.
                if full_reply and not cancel_flag.is_set() and WEB_INTERNAL_URL:
                    try:
                        import requests as _requests
                        _r = _requests.post(
                            WEB_INTERNAL_URL.rstrip("/") + "/internal/voice/turn",
                            json={"biz": biz_id, "lead": lead_id, "text": text},
                            headers={"X-Internal-Secret": INTERNAL_SECRET},
                            timeout=30)
                        _r.raise_for_status()
                        _commit_reply = (_r.json() or {}).get("reply", "")
                        if _commit_reply and _commit_reply != full_reply:
                            # booking may have updated reply (e.g. confirmed slot)
                            pass
                        # Check if booking happened in this turn
                        _commit_data = _r.json() or {}
                        if _commit_data.get("booked"):
                            booked = True
                    except Exception as exc:
                        print(f"[firstback] voice turn commit failed: {exc}",
                              file=sys.stderr, flush=True)

                # M-3: accumulate turn log
                if full_reply:
                    turn_log.append({"in": text, "out": full_reply})
                    turn_count += 1

                    # Track booking via the local fallback too
                    # (Local: check if lead status changed -- lightweight heuristic)
                    if not WEB_INTERNAL_URL and not booked:
                        try:
                            import db
                            lead_row = db.get_lead(int(lead_id))
                            if lead_row and lead_row.get("status") == "booked":
                                booked = True
                        except Exception:
                            pass

                # Update history for next turn
                history.append({"role": "user", "content": text})
                if full_reply:
                    history.append({"role": "assistant", "content": full_reply})

            elif mtype == "interrupt":
                # S-2 barge-in: set the cancel flag so the active streaming loop
                # stops emitting further frames for the current utterance.
                cancel_flag.set()

            elif mtype == "error":
                print(f"[firstback] voice relay error: {msg.get('description')}",
                      file=sys.stderr, flush=True)
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:  # never let one call crash the worker
        print(f"[firstback] voice ws error: {e}", file=sys.stderr, flush=True)
    finally:
        # M-3 turn log + M-5 recovery SMS on EVERY exit path -- including the
        # `error`-message `break` that previously fell through without cleanup (5g P1).
        # Idempotent: the M-2 graceful-close path already ran _finalize.
        await _finalize()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(fastapi_app, host="0.0.0.0", port=VOICE_SERVICE_PORT)
