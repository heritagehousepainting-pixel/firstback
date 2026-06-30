# Deploy Voice — runbook (P0-A)

Turns on the AI voice answering. The code is built + verified (the voice service boots, `/twiml`
returns valid ConversationRelay TwiML with the wss URL + AI/recording disclosure). What remains
is **infrastructure you own** — a second Render service, secrets, and a Twilio webhook. None of it
is code. Estimated time: ~20–30 min.

## Architecture (decided)
- `voice_service.py` runs as a SEPARATE Render web service (Flask can't host the long-lived
  ConversationRelay WebSocket).
- It needs NO database: it relays every turn + booking to the main web app over HTTP
  (`/internal/voice/*`), authed by a shared `FIRSTBACK_INTERNAL_SECRET`. So no Postgres migration.
- Flow on a missed call: Twilio Voice webhook → main web app `/webhooks/twilio/voice/inbound`
  → (voice configured + enabled) → `<Redirect>` to the voice service `/twiml` → ConversationRelay
  over wss `/ws`. The voice service relays each turn back to the web app.

---

## Step 1 — Generate the shared secret (do this first)
On your machine, generate one value you'll paste on BOTH services:
```
openssl rand -hex 32
```
Keep it handy. It is NOT stored in this repo. (Call it `<INTERNAL_SECRET>` below.)

## Step 2 — Deploy the voice service on Render
New web service from this repo (or uncomment the `firstback-voice` block in `render.yaml`):
- **Start command:** `uvicorn voice_service:fastapi_app --host 0.0.0.0 --port $PORT`
- **Build command:** `pip install -r requirements.txt`
- **Plan:** Starter (always-on; no disk needed).
- **Env vars:**
  | Key | Value |
  |---|---|
  | `PYTHON_VERSION` | `3.12.7` |
  | `FIRSTBACK_PROVIDER` | `claude` |
  | `ANTHROPIC_API_KEY` | your Anthropic key (`sk-ant-…`) |
  | `FIRSTBACK_WEB_URL` | the MAIN web app's https URL (e.g. `https://<main>.onrender.com`) |
  | `FIRSTBACK_INTERNAL_SECRET` | `<INTERNAL_SECRET>` from Step 1 |
  | `FIRSTBACK_VOICE_TTS` | a real TTS voice id (don't leave blank — default sounds robotic) |

Deploy. Note its public URL, e.g. `https://firstback-voice.onrender.com` → call it `<VOICE_URL>`.

## Step 3 — Point the MAIN web service at the voice service
On the EXISTING main web service, add:
| Key | Value |
|---|---|
| `FIRSTBACK_VOICE_URL` | `<VOICE_URL>` from Step 2 |
| `FIRSTBACK_INTERNAL_SECRET` | the SAME `<INTERNAL_SECRET>` |

(Confirm the main service already has `ANTHROPIC_API_KEY` + `FIRSTBACK_PROVIDER=claude` for the
brain, and `FIRSTBACK_PUBLIC_URL` set to its own URL.) Redeploy/restart the main service.

**The moment `FIRSTBACK_VOICE_URL` is set, the whole app flips voice-on automatically** — the
homepage becomes voice-led, the pricing/solutions pages stop saying "rolling out," etc. (all the
`voice_configured` gates).

## Step 4 — Twilio Voice webhook
Each FirstBack-provisioned number already has its Voice webhook pointed at the main web app
(`provision_number` wires `/webhooks/twilio/voice/inbound` at buy time). If a number was attached
manually, set its **Voice → A Call Comes In** webhook to:
```
https://<main>.onrender.com/webhooks/twilio/voice/inbound   (HTTP POST)
```

## Step 5 — Enable inbound AI answering per contractor
Inbound AI answering is gated per-business by `inbound_voice_enabled` (default 0 = off — falls
through to text-back). Turn it on for a contractor once you want their missed calls answered by
the AI. (There is also a monthly voice spend cap, `FIRSTBACK_VOICE_MONTHLY_CAP` cents, and a
400 ms health-probe of `FIRSTBACK_VOICE_URL` so a down voice service degrades to text-back, never
dead air.)

---

## Smoke test (proves it end-to-end)
1. After Steps 2–3, open the live site — the homepage should now read voice-led (the CALL toggle
   tells the real voice story, no "rolling out").
2. Set `inbound_voice_enabled=1` for your own test business.
3. Forward a number to your FirstBack number and let it ring through unanswered → the AI should
   answer the call, talk, and the transcript should appear in the dashboard.
4. The first answered call also fires the OA-9 owner nudge SMS.

## Verified already (so you don't debug code)
- `uvicorn voice_service:fastapi_app` boots clean; `/twiml` returns `<Connect><ConversationRelay
  url="wss://…/ws">` with the disclosure greeting.
- `requirements.txt` has fastapi, uvicorn[standard], websockets, httpx.
- The relay endpoints (`/internal/voice/turn`, `/stream`, `/turn_log`) exist on the web app and
  auth on `FIRSTBACK_INTERNAL_SECRET`.

*Secrets (ANTHROPIC_API_KEY, INTERNAL_SECRET, Twilio creds) are entered by you in the Render/Twilio
dashboards — they are never committed to this repo.*
