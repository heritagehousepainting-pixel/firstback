# FirstBack — The Callback System: Course of Action

How we turn the simulated missed-call loop into a **real, professional callback
system**: an instant SMS text-back on every missed call, escalating to an AI
voice call when the customer asks for one. Provider: **Twilio**. Brain: **Claude**.

This is the deep, build-ready plan for what [FEATURE_SPECS.md](FEATURE_SPECS.md)
Feature 3 sketched. It supersedes that sketch for the telephony work; the
`messaging` abstraction (Feature 3 "Phase A") is the shared first step here.

Researched 2026-06-14 against current Twilio docs, FCC/TCPA rulings, and live
pricing (sources at the end). **Compliance notes are engineering guidance, not
legal advice — a TCPA attorney must review consent flows before launch.**

---

## 1. What "professional" means here (the bar)

Not just "it sends a text." Production-grade means:

1. **Reliable missed-call detection** — including the voicemail-swallow case.
2. **It actually gets delivered / answered** — A2P 10DLC registered, STIR/SHAKEN
   A-attestation, clean number reputation, so texts aren't filtered and calls
   aren't labeled "Spam Likely."
3. **It's legal** — consent posture right for SMS *and* (separately) AI voice;
   STOP/opt-out honored instantly; quiet hours respected.
4. **It can't double-book or crash** — reuses the booking-integrity guards
   (audit #1/#2) and never blocks the customer-facing reply.
5. **Multi-tenant from day one** — every number, registration, and suppression
   entry is scoped to a business.

If we ship the code without #2 and #3, the product looks done but silently fails
in the field (filtered texts, spam-labeled calls, legal exposure). Treat them as
part of the feature, not afterthoughts.

---

## 2. The flow (what actually happens)

```
  Customer calls the contractor's FirstBack number
        │
        ▼
  [Twilio Voice webhook] ── TwiML ──► <Dial answerOnBridge timeout=18
        │                              action=/webhooks/twilio/voice/dial-status>
        │                                  └─ rings contractor's real cell
        │
        ├─ contractor answers ─────────► normal call, done (no text)
        │
        └─ no-answer / busy / failed ──► dial-status webhook fires
                                              │
                                              ▼
                              MISSED → messaging.send_sms(business, caller,
                                       "Sorry we missed you — reply here and
                                        we'll get you booked.")   [REST API]
                                              │
                                              ▼
                              Customer texts back ──► [SMS inbound webhook]
                                              │
                                              ▼
                              handle_inbound(business, lead, body)  ◄── the SAME
                                  • detect_urgency / mark urgent        function
                                  • ai.generate_reply (Claude)          the
                                  • book via existing booking path      simulator
                                  • _schedule_notes + owner alert       calls
                                              │
                                              ▼
                              messaging.send_sms(reply)
                                              │
                       ┌──────────────────────┴───────────────────────┐
                       │  If the AI offers / the customer asks for a   │
                       │  call:  "Reply CALL and we'll ring you now."  │
                       └──────────────────────┬───────────────────────┘
                                              │ customer replies "CALL" (= consent)
                                              ▼
                              client.calls.create(url=/voice-svc/twiml)
                                              │
                                              ▼
                       [Voice service · FastAPI] <Connect><ConversationRelay>
                                  Claude over WebSocket: STT/TTS by Twilio,
                                  books the estimate via a tool call back
                                  into Flask's booking endpoint.
```

The text path is the core promise. The voice path is the upsell, gated behind an
explicit "CALL" (this gate is a **compliance requirement**, not just UX — see §6).

---

## 3. How this grafts onto the existing code

The simulator already does the whole conversation; we're swapping the transport
from in-app fetches to real Twilio webhooks. Concretely:

| Today (simulated) | Becomes (real) |
|---|---|
| `POST /api/sim/incoming` ([app.py](app.py)) — create lead + opening reply | Twilio **voice dial-status** webhook fires the opening text-back on a missed call |
| `POST /api/sim/reply` ([app.py](app.py)) — inbound turn → `generate_reply` → book | Twilio **SMS inbound** webhook runs the same logic |
| `db.add_message(lead_id,"out",reply)` | `messaging.send_sms(...)` (real Twilio when configured, else in-app) |
| `business.phone` (display only) | real provisioned `businesses.twilio_number` + a tenant lookup |

**Refactor first (do this before wiring webhooks):** extract the body of
`sim_reply` into `handle_inbound(business, lead, body) -> (reply, booked, urgent)`
so the simulator **and** the SMS webhook call one function. This keeps the
booking-integrity logic (audit #1/#2) in a single place and means the simulator
stays a perfect offline mirror of production. Same for `sim_incoming` →
`open_conversation(business, lead)`.

**Reuse, don't reinvent:**
- `ai.generate_reply(business, history, exclude_slot_ids=...)` — unchanged.
- The off-hot-path daemon pattern (`app._schedule_notes`) — every Twilio API
  call (send SMS, place call) goes through it or returns fast; webhooks must
  answer in <~10s or Twilio retries.
- The gated-integration pattern (`google_cal.py`'s `configured()` /
  `is_connected()`, swallow+log errors) — `messaging.py` mirrors it exactly.
- Booking writes stay **single-writer through Flask** (critical once the voice
  service is a second process hitting SQLite — see §5).

---

## 4. Data model additions

Follow the existing migration style in [`db.init_db`](db.py)
(`CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info` guarded `ALTER`).

**`businesses` — new columns:**
- `twilio_number` (E.164), `twilio_number_sid` (PN…)
- `forward_to` — the contractor's real cell we `<Dial>`
- `timezone` — for quiet hours (ties to audit #8; until then, derive from the
  number's area code as a proxy)
- `a2p_brand_sid`, `a2p_campaign_sid`, `a2p_status`
  (`unregistered|pending|approved|failed`)
- `trust_profile_sid`, `shaken_status` — STIR/SHAKEN attestation state
- `voice_callback_enabled` (bool), `caller_id_name`

**`calls`** (new) — inbound call log + idempotency + analytics:
`id, business_id, lead_id, call_sid (UNIQUE), from_number, to_number,
dial_status, answered_by (AMD verdict, nullable), missed (bool), created_at`.
The UNIQUE `call_sid` dedupes Twilio's webhook retries.

**`contacts_consent`** (new) — the suppression / consent ledger, per
(business, consumer):
`id, business_id, consumer_number, sms_ok (bool), voice_ok (bool),
opted_out (bool), opted_out_at, source, updated_at`.
UNIQUE `(business_id, consumer_number)`. **Checked before every outbound
SMS and every voice call.**

**`messages` — augment:** add `provider_sid` (Twilio MessageSid) and
`delivery_status` so we can reconcile delivery webhooks.

**`voice_calls`** (new, Phase 3): `id, business_id, lead_id, call_sid, status,
booked (bool), transcript (TEXT, optional), created_at`.

---

## 5. Architecture: two processes, one repo

Real-time voice needs a persistent WebSocket, which **WSGI/Flask cannot host
well**. The standard, Twilio-documented shape:

```
                         ┌──────────────────────────────────────┐
  Twilio webhooks ─────► │  Flask (WSGI, gunicorn) — EXISTING    │
  (voice + SMS, HTTP)    │   • dashboards, settings, booking DB  │
                         │   • all Twilio webhooks               │
                         │   • places outbound calls (REST)      │
                         │   • SINGLE WRITER for bookings        │
                         └───────────────┬──────────────────────┘
                                         │ shared SQLite (read) / internal HTTP (write)
                         ┌───────────────▼──────────────────────┐
  Twilio ConversationRelay ◄──wss──►     │  Voice service (ASGI, uvicorn) — NEW   │
                         │   FastAPI: GET /twiml, WS /ws         │
                         │   Claude streaming + tool-call book   │
                         └───────────────────────────────────────┘
```

- **Flask stays exactly as is** for everything HTTP. Don't convert it.
- **Voice service = FastAPI on uvicorn**, a sibling process. Only exists once we
  build Phase 3; Phases 0–2 are pure Flask.
- **SQLite + two writers is a hazard.** Rule: the **voice service never writes
  bookings directly** — when Claude's booking tool fires, it calls a Flask
  internal endpoint (`POST /internal/book`, shared-secret auth) so the audit
  #1/#2 double-book guards remain the one writer. Enable WAL mode regardless.
  (At real scale, migrate SQLite → Postgres; note it, don't block on it.)
- **Both must be public over TLS/`wss://`.** Local dev: ngrok. Prod: one domain,
  reverse-proxy `/voice-ws` → uvicorn, everything else → gunicorn.

---

## 6. Compliance & deliverability — the launch gate

**Not legal advice.** These are the rules the research surfaced; get counsel to
sign off. Distinilled to what we must build:

### A2P 10DLC (required for US SMS from local numbers)
- We are an **ISV**. Register **FirstBack once** as the ISV/primary Trust Hub
  profile; register **each contractor** as its own **secondary profile + brand +
  campaign**, programmatically via Twilio's API.
- Branch on **"do you have an EIN?"** at onboarding: EIN → **Low-Volume
  Standard** brand (most users; avoids the sole-prop daily caps); no EIN →
  **Sole Proprietor** brand.
- One **"customer care"** campaign per tenant (our text-back is conversational
  customer-care, *not* marketing — this classification keeps us in the lower
  consent tier and helps vetting pass).
- Registration is **async and slow** (hours to ~2 weeks) and costs ~$2/mo +
  ~$19 one-time per contractor. **Onboarding must have a "registration pending"
  state — a new contractor cannot text on day one.**

### Consent
- **SMS text-back:** keep it **strictly informational** ("we missed you, reply to
  book"). A consumer who *just called* + informational content = the strong
  footing (prior express consent, not the stricter written consent). **Never let
  the auto-reply drift into promotion** ("10% off!") — that changes the legal
  tier.
- **AI voice callback (the big one):** the FCC (Feb 2024) treats **AI-generated
  voice as "artificial/prerecorded"** under TCPA. The "they called us" theory is
  *weaker* for an artificial-voice call. **→ Gate the voice leg behind an
  affirmative reply** ("Reply CALL / YES and we'll ring you"). That YES is the
  consent. Do **not** auto-dial an AI voice call on a bare missed call.
- **Disclose at call open:** "Hi, this is an AI scheduling assistant for
  {business}; this call may be recorded." Covers the AI-identity rules (TX/CA
  today; pending federal) and recording consent in one line.

### Opt-out (required)
- Honor STOP/UNSUBSCRIBE/CANCEL/QUIT/HELP (Twilio does this on its numbers; also
  handle ourselves).
- The 2025 FCC rule lets consumers revoke by **"any reasonable means"** — so
  keyword matching isn't enough. **Use NLU on inbound replies** (Claude can flag
  "stop texting me" / "no thanks") → write to `contacts_consent.opted_out` →
  **suppress both SMS and voice**, honored instantly.

### Quiet hours (required)
- No automated texts or AI calls before **8am / after 8pm in the recipient's
  local time** (strict state window; safe everywhere). Use `businesses.timezone`
  / area-code proxy. A *same-moment* informational reply to a call they just
  placed is the strongest footing; **suppress AI voice outside the window
  regardless.**

### Deliverability (so it's not flagged spam)
- **IMPLEMENTED — inbound call screen** (`triage.screen_caller`): a tiered, precision-first
  verdict decides who gets the text-back, so we never blast auto-texts at spam/robocalls (a
  top cause of a number getting carrier-flagged) or at known/saved contacts. Auto-derives the
  "known" set from bookings (no import). Tiers: identity → STIR/SHAKEN + neighbor-spoof +
  behavior → optional paid reputation (`reputation.py`) → crowdsourced cross-tenant flags →
  optional AI content screen. Rolls out via `FIRSTBACK_SCREEN_MODE` (monitor → enforce). The
  same `StirVerstat` attestation read below feeds the screen.
- **STIR/SHAKEN A-attestation** needs **Twilio-owned numbers** in a vetted Trust
  Hub product → another reason to **provision a Twilio number per business**
  rather than dialing from their cell (which caps at B).
- Enroll callback numbers in **Voice Integrity** (registers with carrier
  analytics engines). Branded Calling/CNAM (business name + logo on screen) is a
  later differentiator.
- **Hygiene:** low volume per number, no over-dialing, honor opt-outs — a
  "callback within seconds, once" pattern is naturally clean.

---

## 7. Number strategy

- **Default: provision a new local Twilio number** in the contractor's area code
  (instant via API, A-attestation eligible, easy to cancel → low-friction
  trials). Detect missed calls via the `<Dial>`-and-fallback architecture (§2).
- **Upsell: port their existing business number** into Twilio (1–4 weeks; texts
  then come *from* the number customers know — higher trust, stickier
  retention).
- **Local, not toll-free** — locals get answered; toll-free reads "telemarketer"
  for a neighborhood contractor.
- Carrier "conditional call forwarding" (keep your number, forward-on-no-answer)
  is offered by some carriers but is **brittle and not API-controllable**
  (Verizon non-standard, T-Mobile→VoIP flaky). Document as a power-user option,
  don't build onboarding around it.

### The voicemail-swallow problem
If the contractor's **carrier voicemail** answers, Twilio sees `completed` and we
*miss the miss*. Fix with **Answering Machine Detection** (`machineDetection` on
`<Number>`, ~$0.0075/answered call, US/CA, ~94% accurate) — treat
`AnsweredBy=machine_*` as missed and text back. **Ship Phase 1 on
`DialCallStatus` alone; add AMD in Phase 4** once we confirm it's hurting
conversion (it adds latency + cost + a fiddly async correlation by CallSid).

---

## 8. Phased roadmap

**Committed scope (decided 2026-06-14): build Phases 0–3** — real text-back +
compliance + the AI voice agent. Phase 4 is the fast-follow. Each phase is
independently shippable, so we integrate and test as we go rather than big-bang
at the end.

**STATUS (2026-06-14): Phases 0, 1, 3, and the code half of Phase 2 are BUILT +
tested** — 68 passing checks across `test_callback.py`, `test_webhooks.py`,
`test_voice.py`, and `test_compliance.py`. The whole system stays dormant until
Twilio credentials + a provisioned number are set (the simulator is unchanged).
Phase 2's *remaining* work is ACCOUNT + LEGAL, not code — A2P 10DLC registration,
STIR/SHAKEN attestation, and a TCPA review (steps in
[USER_TO_DO.md](USER_TO_DO.md)). Phase 4 (AMD voicemail detection, number porting,
branded calling, AI-minute metering) is the fast-follow.

### Phase 0 — Foundations (shared with FEATURE_SPECS Feature 3A/B)
- `messaging.py`: `configured()`, `send_sms(business, to, body)` — real Twilio
  REST when configured, else record an in-app outbound row (the simulated shim).
  Mirror `google_cal.py` structure.
- Twilio signature-validation decorator (`X-Twilio-Signature` via
  `RequestValidator`) on every webhook. **Budget time for the proxy/ngrok
  URL-reconstruction gotcha** — it's the #1 integration bug.
- Number provisioning helper (search `AvailablePhoneNumbers`, buy via
  `IncomingPhoneNumbers`, set webhooks).
- Tenant-by-number lookup (`To` → business).
- Config keys: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, default from-number.
- Refactor `sim_reply`/`sim_incoming` → shared `handle_inbound` /
  `open_conversation`.
- Migrations: the §4 `businesses` columns, `calls`, `contacts_consent`.

### Phase 1 — Real text-back MVP (the actual callback)
- `POST /webhooks/twilio/voice/inbound` → TwiML `<Dial answerOnBridge timeout=18
  action=…>` to `forward_to`.
- `POST /webhooks/twilio/voice/dial-status` → on missed status, log `calls` row,
  fire the opening text-back via `messaging.send_sms`.
- `POST /webhooks/twilio/sms/inbound` → tenant lookup, lead by `From` (or
  create), `handle_inbound`, send reply via REST. STOP/opt-out + quiet-hours
  gating here.
- `POST /webhooks/twilio/sms/status` → reconcile delivery.
- **Result:** a real missed call → real text-back → Claude books by text. (Needs
  Phase 2's 10DLC to actually deliver at scale, so these ship close together.)

### Phase 2 — Compliance & deliverability (the gate to real customers)
- A2P 10DLC ISV registration pipeline (secondary profile + brand + campaign per
  tenant), with onboarding "pending" states and status polling.
- Trust Hub business profile + STIR/SHAKEN product → A-attestation; Voice
  Integrity enrollment.
- NLU-based opt-out → `contacts_consent` suppression (SMS + voice).
- Quiet-hours enforcement (needs `businesses.timezone` / audit #8).
- USER_TO_DO.md section: buy/port number, EIN question, registration steps,
  costs.

### Phase 3 — The "then voice" AI callback
- New FastAPI voice service (uvicorn): `GET /twiml` →
  `<Connect><ConversationRelay url=wss://… welcomeGreeting=…>`; `WS /ws` →
  Claude loop (token streaming for low latency, `interrupt` handling, `tool_use`
  for booking → calls Flask `/internal/book`).
- Reuse `FIRSTBACK_PROVIDER`/Claude config; **Claude Haiku 4.5** for
  latency/cost; prompt-cache the system prompt + tools.
- Consent gate: only place the call after an affirmative "CALL/YES"; outbound via
  `client.calls.create(url=voice-svc/twiml)`.
- AI + recording disclosure in `welcomeGreeting`. Decide record vs
  transcript-only (transcript-only sidesteps two-party-consent exposure).
- Deployment: two processes, TLS, internal booking endpoint.

### Phase 4 — Hardening & polish
- AMD voicemail detection (§7).
- Number porting flow.
- Branded Calling / CNAM.
- **AI-minute metering + caps** (voice minutes are the only meaningful variable
  cost — protect margin).
- Fallbacks: if a voice call is unanswered/voicemail, drop back to SMS;
  retry/backoff on Twilio errors.
- Analytics: answer rates, text→book conversion, voice→book conversion (feeds
  FEATURE_SPECS Feature 4 ROI dashboard).

---

## 9. Cost model & pricing implication

Per contractor / month (US list, ConversationRelay path, local number, sole-prop
10DLC):

| Scenario | Make-up | ~Cost/mo |
|---|---|---|
| Fixed floor | number $1.15 + 10DLC campaign $2.00 | **~$3.15** |
| Light (20 calls, text only) | floor + ~$1 SMS + amortized setup | **~$5–6** |
| Heavy (50 calls, 15 × 3-min AI calls) | floor + SMS + ~$5 AI voice + setup | **~$10–12** |

- All-in **AI voice ≈ $0.10–0.13/min** (ConversationRelay $0.07 + voice $0.014 +
  Haiku ~$0.02–0.05). A 3-min booking call ≈ **$0.30**.
- **AI minutes are the only cost that scales** — meter/cap them (Phase 4).
- **Pricing:** a $29–49/mo plan → **75–90% gross margin** even on heavy AI users.
  A cheap **text-only tier ($15–19)** still clears ~70%. Per-contractor 10DLC
  overhead (~$2/mo + ~$19 one-time) is real but small; eat it or fold into setup.

*Verify live rates in the Twilio console before committing to pricing — carrier
SMS surcharges and the toll-free verification fee moved during 2025.*

---

## 10. Decisions locked + remaining risks

**Locked (2026-06-14):**
- **Voice leg = AI voice agent** (Claude via ConversationRelay), built now — not
  the human-bridge interim. The "reply CALL" consent gate still applies; a human
  bridge / voicemail drop is kept only as a **fallback** when the AI service is
  unavailable.
- **First build = Phases 0–3 in one push** (real text-back + compliance + AI
  voice callback). Phase 4 (AMD, porting, branded calling, metering) is the
  fast-follow.

**Remaining risks / to handle:**
- **Legal review** of consent (SMS *and* AI voice), recording posture, and ISV
  10DLC registration — **required before real customers** (not before building or
  testing on your own number with willing test callers).
- **Recording posture:** decide record-audio vs transcript-only for the AI call
  (transcript-only sidesteps two-party-consent exposure).
- **SQLite → Postgres** once the voice service is a second writer at scale
  (near-term: route booking writes through Flask + enable WAL).
- **Onboarding latency:** 10DLC + porting mean a contractor isn't live the
  instant they sign up — design the "getting set up" UX honestly.
- **Quiet hours** use business-local time — audit #8 (timezone unification) is
  now **DONE**, so this is unblocked.

---

## 11. Sources

Twilio Voice/TwiML: [Dial](https://www.twilio.com/docs/voice/twiml/dial) ·
[Number/AMD](https://www.twilio.com/docs/voice/twiml/number) ·
[AMD](https://www.twilio.com/docs/voice/answering-machine-detection) ·
[Messages API](https://www.twilio.com/docs/messaging/api/message-resource) ·
[Incoming/Available numbers](https://www.twilio.com/docs/phone-numbers/api/incomingphonenumber-resource) ·
[Webhook security](https://www.twilio.com/docs/usage/webhooks/webhooks-security) ·
[Flask signature validation](https://www.twilio.com/en-us/blog/validating-webhook-signatures-python-flask)

ConversationRelay: [overview](https://www.twilio.com/docs/voice/conversationrelay) ·
[TwiML noun](https://www.twilio.com/docs/voice/twiml/connect/conversationrelay) ·
[WebSocket messages](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages) ·
[GA](https://www.twilio.com/en-us/blog/conversationrelay-generally-available) ·
Claude integration: [basic](https://www.twilio.com/en-us/blog/integrate-anthropic-twilio-voice-using-conversationrelay) ·
[streaming+interrupts](https://www.twilio.com/en-us/blog/anthropic-conversationrelay-token-streaming-interruptions-javascript) ·
[function calling](https://www.twilio.com/en-us/blog/developers/tutorials/product/function-calling-twilio-voice-anthropic-claude-integration)

Compliance: [A2P 10DLC](https://www.twilio.com/docs/messaging/compliance/a2p-10dlc) ·
[ISV onboarding](https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/onboarding-isv) ·
[FCC: AI voice = TCPA](https://www.fcc.gov/document/fcc-confirms-tcpa-applies-ai-technologies-generate-human-voices) ·
[FCC consent-revocation 2025](https://www.jdsupra.com/legalnews/fcc-s-tcpa-consent-revocation-rule-8022288/) ·
[STIR/SHAKEN on Twilio](https://www.twilio.com/en-us/blog/developers/best-practices/shaken-stir-sign-twilio-calls) ·
[quiet hours](https://fedsoc.org/commentary/fedsoc-blog/navigating-a-tcpa-minefield-understanding-the-quiet-hours-rule)

Pricing: [Voice US](https://www.twilio.com/en-us/voice/pricing/us) ·
[SMS US](https://www.twilio.com/en-us/sms/pricing/us) ·
[Anthropic pricing](https://www.cloudzero.com/blog/anthropic-claude-api-pricing/)
