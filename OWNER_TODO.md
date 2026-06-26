# FirstBack — Everything YOU Need To Do (single source of truth)

This one file is your complete owner to-do list. It merges the prod go-live ops
(`GO_LIVE_CHECKLIST.md`) and the step-by-step credential guides (`USER_TO_DO.md`)
into one place. **Work top to bottom.** Each item is something the code *cannot* do
for itself — the app ships honest-but-gated and only goes live when you do these.

How to read it: ☐ = a task · `CODE` = a Render environment variable to set ·
*How:* = the click-by-click when it's not obvious. Tiers 0–1 make the core
"missed call → text back" promise actually work; Tiers 2+ unlock money and extras.

> Generate any "random string" with:
> `python3 -c "import secrets; print(secrets.token_hex(32))"`

---

## TIER 0 — Security & boot (set these first or the app degrades/refuses to boot)

- ☐ **`FIRSTBACK_ENV=production`** (or `FIRSTBACK_HTTPS=1`). *The single most important
  security flag* — it arms Secure login cookies and the two fail-fasts below. The safety
  net is inert without it. (Leave it OFF only for local http testing.)
- ☐ **`FIRSTBACK_SECRET`** = a random string. Signs login sessions (fail-fast in prod).
- ☐ **`FIRSTBACK_TOKEN_KEY`** = a *different* random string. Encrypts Google/OAuth tokens
  at rest. **Boot raises an error in prod if this is empty** — set it even if you launch
  text-only. Don't rotate it casually (old tokens become unreadable → reconnect once).
- ☐ **`FIRSTBACK_TASKS_SECRET`** + **`FIRSTBACK_INTERNAL_SECRET`** = random strings.
  Without them `/tasks/run-due` returns 403 and the scheduler silently never runs.
- ☐ **Reconcile every `FIRSTBACK_*` env var + the DB path in Render** before the next
  deploy. The RingBack→firstback rename changed env names + the DB filename; a mismatch
  makes the app fall back to defaults / reset the database.
- ☐ **Change the owner password** (Settings → Password card), or set
  `FIRSTBACK_OWNER_PASSWORD` in Render before first load. Never leave a seeded default live.
- ☐ **Never set `FIRSTBACK_DEBUG=1` in prod** (it exposes a remote code console). It's off by default.

## TIER 1 — Core loop: missed call → instant text back (the headline promise)

### 1a. Twilio account + credentials
- ☐ Sign up at **twilio.com**, buy an SMS-enabled number, copy your Account SID + Auth Token.
- ☐ Set in Render: **`TWILIO_ACCOUNT_SID`**, **`TWILIO_AUTH_TOKEN`**, **`TWILIO_FROM_NUMBER`**
  (`+1…` E.164). One shared platform account, set once.

### 1b. Public URL + production domain
- ☐ **`FIRSTBACK_PUBLIC_URL`** = your production domain. *The single most important
  functional env* — drives SMS delivery receipts, the call-forwarding sentinel, and the
  dispatcher/voice TwiML. Blank = no delivery receipts/retries.
- ☐ Register a custom domain → add it in Render → point DNS (CNAME) → set
  `FIRSTBACK_PUBLIC_URL` → **re-point Twilio webhooks LAST.** Keep the old `*.onrender.com`
  reachable during cutover; re-verify receipts + forwarding after the swap.
- ☐ In the Twilio number's settings, set both webhooks to **POST**:
  - Voice → A call comes in: `https://<your-domain>/webhooks/twilio/voice/inbound`
  - Messaging → A message comes in: `https://<your-domain>/webhooks/twilio/sms/inbound`

### 1c. A2P 10DLC registration (required before real US texting)
- ☐ **`TWILIO_TRUST_PRODUCT_SID`** (+ optional `TWILIO_A2P_RESELLER_SID`) — turns the
  registration WRITE API from simulated to live. Until set, nothing is submitted.
  *How:* Twilio Console → Messaging → Regulatory Compliance → A2P 10DLC. EIN business =
  Standard brand; true sole-proprietor (no EIN) = Sole Proprietor brand. Approval: hours → ~2 weeks.
- ☐ **`firstback.io` + `*.firstback.io` wildcard DNS → the Render app** — so
  `<slug>.firstback.io` contractor micro-sites resolve (the opt-in URL TCR inspects).
- ☐ **Cloudflare Email Routing catch-all `@clients.firstback.com`** — so each contractor's
  authorized-rep email can receive Twilio's verification.
- ☐ **Heritage House dogfood:** run the first real **sole-prop** submission (reply YES to the
  OTP) to validate the path end-to-end before charging customers.
- ☐ *(Recommended)* set up Trust Hub + STIR/SHAKEN + Voice Integrity so callbacks aren't
  flagged "Spam Likely".

### 1d. Scheduler, email, alerts, monitoring
- ☐ **External cron → `POST /tasks/run-due` every 60s** with header `X-Tasks-Secret`
  (reminders + growth scan + A2P status sync). The in-process ticker is only a fallback.
- ☐ **`FIRSTBACK_RUN_TICKER=1`** — enable the in-process fallback ticker.
- ☐ **Email:** create a **Resend** account, verify the `firstback.app` domain, then set
  **`SMTP_HOST=smtp.resend.com`**, **`SMTP_FROM=alerts@firstback.app`** (+ the API key/pass).
  *(Gmail alt: `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER`, `SMTP_PASS`=16-char
  App Password, `SMTP_FROM`.)* Email alerts are skipped (in-app only) until this is set.
- ☐ **`ALERT_FROM_NUMBER`** = a platform Twilio number for owner alerts.
- ☐ **External uptime monitor on `GET /health/ticker`** (UptimeRobot / Render health check),
  alerting on `fresh:false` — the only thing that catches total scheduler death.
- ☐ **TCPA attorney review** of the consent flow before real customers (text-back is
  informational; AI voice only after the customer asks; STOP/quiet-hours honored — get sign-off).

## TIER 2 — Money: collect the first $99

- ☐ **Stripe (test mode first):** `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, and **all 6
  Price IDs** — `STRIPE_PRICE_{STARTER,PRO,CREW}` ($99/$199/$399 /mo) **+**
  `STRIPE_PRICE_{STARTER,PRO,CREW}_ANNUAL` ($950/$1,910/$3,830 /yr). Set all 6 before the
  first real subscriber or a live renewal logs a warning and grants starter.
- ☐ **Decide how the first $99 is collected — there is NO subscribe button wired yet.**
  Either (a) a Stripe **Payment Link** out-of-band, or (b) wire a subscribe/upgrade button
  that POSTs to `/billing/checkout` with `{{ csrf_token }}` (route is CSRF-guarded).
- ☐ **Security/money audit of `/webhooks/stripe` + `/auth/reset`** before the prod deploy.
- ☐ Optional **`FIRSTBACK_DAILY_COST_CAP`** — per-tenant daily AI spend cap (default $5).

## TIER 2 — AI brain quality (optional, recommended)

- ☐ **`ANTHROPIC_API_KEY`** + **`FIRSTBACK_PROVIDER=claude`** (`CLAUDE_MODEL` default
  `claude-opus-4-8`) for the best multi-step replies. *Claude path is code-verified but not
  yet live-fired — smoke-test after setting the key.* No key = honest keyword router runs everything.
- ☐ **`FIRSTBACK_TZ`** = your IANA zone (e.g. `America/New_York`) so dates never drift.
- ☐ Cost knobs (defaults safe): `FIRSTBACK_ASSISTANT_RPM` (60/min), `FIRSTBACK_ASSISTANT_DAILY` (400/day).

## TIER 3 — Extended features (each OFF/inert by default — turn on as needed)

### AI voice (currently "beta")
- ☐ Complete the **`firstback-voice`** service in `render.yaml` (uvicorn
  `voice_service:fastapi_app`, ~$7/mo). Env: `FIRSTBACK_WEB_URL`, `FIRSTBACK_INTERNAL_SECRET`
  (**same value on both services**).
- ☐ Set **`FIRSTBACK_VOICE_URL`** on the web service (master switch: activates the CALL path
  + dispatcher call + auto-flips marketing copy to "live").
- ☐ Per tenant: `voice_callback_enabled=1` ("reply CALL" callback) and/or
  `inbound_voice_enabled=1` ("AI answers inbound calls"), via Settings.
- ☐ Owner calls: **attorney review** of inbound AI voice; add a **recording disclosure** to the
  greeting only if you enable audio recording. Cost cap `FIRSTBACK_VOICE_MONTHLY_CAP_CENTS`
  (default $20/biz/mo). Price as a $29–$49/mo add-on once billing is live.

### Call screening (ships in `monitor` — logs, never silences)
- ☐ **`FIRSTBACK_SCREEN_MODE`** `off|monitor|enforce` — flip to `enforce` once the monitor
  numbers look right (`off` = instant rollback). Thresholds `FIRSTBACK_SCREEN_HARD` (80) /
  `FIRSTBACK_SCREEN_MID` (45).
- ☐ Optional paid reputation: **`FIRSTBACK_REPUTATION_PROVIDER`** `twilio_nomorobo|hiya`
  (+ `HIYA_API_KEY`). Optional AI screen: **`FIRSTBACK_SCREEN_AI=1`** (needs a real LLM key).

### Calendars & field-service sync
- ☐ **Google Calendar + Contacts OAuth** (live booking; bookings stay local-only until connected).
  *How:* console.cloud.google.com → New Project → enable **Google Calendar API** → OAuth
  consent screen (External; add your Gmail as a test user) → Credentials → OAuth client ID
  (Web app) → Authorized redirect URI = `https://<your-domain>/api/calendar/google/callback`
  → set **`GOOGLE_CLIENT_ID`**, **`GOOGLE_CLIENT_SECRET`**, **`GOOGLE_REDIRECT_URI`** → connect
  in Settings. (To let *anyone* connect, submit Google's app-verification review.)
- ☐ **Outlook / MS 365:** Azure AD app (delegated `Calendars.ReadWrite` + `offline_access`),
  then `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_REDIRECT_URI`
  (+ optional `MICROSOFT_TENANT_ID`, default `common`).
- ☐ **FSM sync — pick ONE:** Jobber (`JOBBER_CLIENT_ID/SECRET/REDIRECT_URI`, needs Jobber
  Connect tier; recommended) **or** Housecall Pro (`HCP_CLIENT_ID/SECRET/REDIRECT_URI`; confirm
  scope strings at registration; push-back is no-op in v1). If both connect, HCP wins.

### Growth, widgets, places
- ☐ Per business: set **`review_link`** (your Google review URL) so review-request drafts link right.
- ☐ **`growth_on`** (per business, default OFF) = auto-queued growth texts. Before any UI toggle
  for it, **add a per-send/per-batch approval** so "we don't text anyone you haven't approved"
  stays true.
- ☐ **`GOOGLE_PLACES_API_KEY`** — signup business-name autocomplete + Google review tracking.
- ☐ **Voicemail → lead:** flip *Voicemail* on in Settings + enable Twilio recording/transcription.
- ☐ **Web-chat "Text us" widget:** flip *Widget* on in Settings, paste the one-line embed (A2P-gated).
- ☐ Deferred (NOT built): **Google Business Profile connector** (negative-review reply, the
  5-Star moment) — needs a GBP OAuth + reviews store.

## TIER 4 — Content honesty & cleanup (before/around launch)

- ☐ **Real customer stories** — `/customers` shows honest placeholders; replace with consented
  quotes (written sign-off before naming a business).
- ☐ **Webinars** — `/webinars` is coming-soon; schedule a real event + wire registration first.
- ☐ **Password reset** — the `/auth/forgot → /auth/reset` flow exists but needs **SMTP** (Tier 1)
  to deliver the email; until then "Forgot password?" routes to `/contact` (manual reset).
- ☐ **`/static/og-default.png`** (1200×630, dark + wordmark + "Miss a call. We text back. They
  book.") then swap the link-preview image. *(A `favicon-512.png` fallback is already wired so
  there's no broken preview meanwhile.)*
- ☐ Delete the dead, unrouted **`landing.html`** (still carries old Jobber/Housecall/Angi logos).
- ☐ Clear demo/test leads from the prod DB before real use (ask if you want this done).

## TIER 4 — Reliability fast-follows (not launch-blocking)

- ☐ Harden the login rate-limit (trusts `X-Forwarded-For` → add `ProxyFix` / email-keying).
- ☐ Finish the `_csrf` sweep on remaining authed config forms (`setup/*`, `training/*`);
  SameSite=Lax covers them today (`/settings/growth_mode` already done).
- ☐ Make the Stripe `seen`+`mark` atomic (`INSERT OR IGNORE`) before running multiple workers.

---

## Founder decisions — already resolved (no action)
- 30-day money-back guarantee badge — **NO** (not shipped).
- Hero leads with "it books the job" + Vic morning briefing — **DONE** (live).
- Paid caller-reputation tier — **GO, deferred** until `/pricing` billing is live.
- Soft-overage billing — **HOLD**; keep the "we'll alert you" FAQ wording until billing is wired.

## Already handled (engineering — no action needed)
- Promote `staging` → `main` and push to GitHub — **DONE** (live on `main`).
- Site-wide responsive: zero horizontal overflow at 360/768/1280/1440/1920 (verified).
- Marketing SEO: canonical + OG + JSON-LD on the indexable pages.
- iOS input-zoom fix; icon-arrow SVGs; color tokenization.
- Test suite green on any machine (fixed a hardcoded test path); all 80 test files pass assertions.

---
_Supersedes `GO_LIVE_CHECKLIST.md` and `USER_TO_DO.md` — those remain for historical/reference
detail, but this is the file to work from._
