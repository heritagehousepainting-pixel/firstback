# FirstBack — Go-Live Checklist

> Synthesized from `SETUP_NEEDED.md` (the chronological ops ledger) into one ordered,
> actionable list. Everything here is **owner-ops the code cannot do for itself** — the
> code ships honest-but-gated and only goes live when these are set. Work top-down:
> Tier 0/1 are required for the core "missed call → text back" promise; Tiers 2+ unlock
> revenue and the extended features.
>
> Legend: ☐ to do · each item names the exact Render env var(s) or action.

---

## Tier 0 — Boot & security (the app degrades or refuses to boot without these)

- ☐ **`FIRSTBACK_ENV=production`** (or `FIRSTBACK_HTTPS=1`) — arms the prod safety net:
  Secure cookies, the `FIRSTBACK_SECRET` fail-fast, and the TOKEN_KEY guard below. *Single
  most important security flag — the net is inert without it.*
- ☐ **`FIRSTBACK_SECRET`** = long random string — session signing (fail-fast in prod).
- ☐ **`FIRSTBACK_TOKEN_KEY`** = long random string, **different** from `FIRSTBACK_SECRET` —
  encrypts OAuth tokens at rest. **Boot raises RuntimeError in prod if empty.** Set it even
  if launching text-only (any non-empty value).
- ☐ **`FIRSTBACK_TASKS_SECRET`** + **`FIRSTBACK_INTERNAL_SECRET`** = random strings — without
  them `POST /tasks/run-due` returns 403 and the scheduler silently never runs.
- ☐ **Reconcile every `FIRSTBACK_*` env var + the DB path in Render** before the next deploy.
  The RingBack→firstback rename changed env-var names and the DB filename; a mismatch makes
  the app fall back to defaults / reset the DB.

## Tier 1 — Core loop: missed call → text back (the headline promise)

### Twilio + public URL
- ☐ **`TWILIO_ACCOUNT_SID`**, **`TWILIO_AUTH_TOKEN`**, **`TWILIO_FROM_NUMBER`** (one shared
  platform account, set once by the operator).
- ☐ **`FIRSTBACK_PUBLIC_URL`** = the production domain. **Single most important functional
  env** — drives SMS delivery receipts (SF-4), the forwarding sentinel (SF-7), and the
  dispatcher TwiML base. Blank = no delivery receipts/retries and forwarding falls back to
  manual confirm.
- ☐ **Production custom domain** on Render → point DNS (CNAME) → set `FIRSTBACK_PUBLIC_URL`
  → **re-point the Twilio voice/SMS webhook URLs LAST.** Keep the old `*.onrender.com`
  reachable during cutover; re-verify SF-4/SF-7 after the swap.

### A2P 10DLC registration (required before real SMS sends)
- ☐ **`TWILIO_TRUST_PRODUCT_SID`** (+ optional `TWILIO_A2P_RESELLER_SID`) — the gate that turns
  the Trust Hub WRITE API from simulated to live. Until set, `submit_a2p` submits nothing.
- ☐ **`firstback.io` + `*.firstback.io` wildcard DNS → the Render app** — so `<slug>.firstback.io`
  micro-sites resolve (the brand opt-in URL TCR inspects).
- ☐ **Cloudflare Email Routing catch-all `@clients.firstback.com`** — so the per-contractor
  authorized-rep email can receive Twilio's verification.
- ☐ **Heritage House dogfood:** run the first real **sole-prop** A2P submission (reply YES to
  the OTP) to validate Path A end-to-end before charging customers.
- ☐ *Cardinal rule (already enforced in code):* a Twilio CREATE 200 = "submission accepted",
  NOT "approved" — only the status poll flips a tenant live. Nothing to do; don't undo it.

### Scheduler + email + alerts
- ☐ **External cron → `POST /tasks/run-due` every 60s** with header `X-Tasks-Secret`
  (reminders + growth scan + A2P sync). The in-process ticker is only a fallback.
- ☐ **`FIRSTBACK_RUN_TICKER=1`** — enable the in-process fallback ticker.
- ☐ **Resend account** + verify the `firstback.app` sending domain, then set
  **`SMTP_HOST=smtp.resend.com`**, **`SMTP_FROM=alerts@firstback.app`** (+ the API key).
- ☐ **`ALERT_FROM_NUMBER`** = a platform Twilio number for owner alerts.
- ☐ **External uptime monitor on `GET /health/ticker`** (UptimeRobot / Render health check),
  alerting on `fresh:false` — the only thing that catches total scheduler death.

## Tier 2 — Revenue (collect the first $99)

- ☐ **Stripe (test mode first):** `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, and **6 Price
  IDs** — `STRIPE_PRICE_{STARTER,PRO,CREW}` ($99/$199/$399 /mo) **+**
  `STRIPE_PRICE_{STARTER,PRO,CREW}_ANNUAL` ($950/$1,910/$3,830 /yr). Set **all 6** before the
  first real subscriber, or a live renewal logs a BILLING WARNING and grants starter.
- ☐ **Wire the "Subscribe" path — there is no subscribe button in the shipped UI.** Either
  (a) a Stripe **Payment Link** out-of-band, or (b) a subscribe/upgrade button whose form
  includes `{{ csrf_token }}` and POSTs to `/billing/checkout` (route is CSRF-guarded). Decide
  before "first $99".
- ☐ **`be-audit` the `/webhooks/stripe` + `/auth/reset` paths** before the production deploy
  (money/security gate).
- ☐ Optional **`FIRSTBACK_DAILY_COST_CAP`** — per-tenant daily AI spend cap (default $5.00).

## Tier 2 — AI brain quality (optional but recommended)

- ☐ **`ANTHROPIC_API_KEY`** + **`FIRSTBACK_PROVIDER=claude`** (`CLAUDE_MODEL` default
  `claude-opus-4-8`) for the richest multi-step writes. *Status: Claude path is code-verified
  against the Messages API but NOT live-fired — smoke-test after setting the key.* With no key
  the deterministic keyword router runs everything (honest, single-step).
- ☐ Cost knobs (defaults are safe): `FIRSTBACK_ASSISTANT_RPM` (60/min), `FIRSTBACK_ASSISTANT_DAILY` (400/day).

## Tier 3 — Extended features (each OFF/inert by default — enable as needed)

### AI voice (currently "beta")
- ☐ Complete the **`firstback-voice`** service block in `render.yaml` (uvicorn
  `voice_service:fastapi_app`, ~$7/mo). Env: `FIRSTBACK_WEB_URL`, `FIRSTBACK_INTERNAL_SECRET`
  (**same value on both services**).
- ☐ Set **`FIRSTBACK_VOICE_URL`** on the web service (master switch: activates the CALL path,
  the dispatcher call, and auto-flips marketing copy to "live").
- ☐ Per-tenant flip: **`voice_callback_enabled=1`** ("reply CALL" callback) and/or
  **`inbound_voice_enabled=1`** ("AI answers inbound calls") in Settings.
- ☐ Owner decisions before real-customer use: **attorney review** of inbound AI voice (≈ IVR);
  **recording disclosure** in the greeting only if you enable audio recording.
- ☐ Cost cap `FIRSTBACK_VOICE_MONTHLY_CAP_CENTS` (default $20/biz/mo). Price as a $29–$49/mo add-on once billing is live.
- ☐ Until deployed, keep the **"beta / rolling out on Pro & Crew"** copy — don't sell it as included.

### Call screening (ships in `monitor` — logs, doesn't silence)
- ☐ **`FIRSTBACK_SCREEN_MODE`** `off|monitor|enforce` — flip to `enforce` once the monitor
  "Would screen" numbers look right (`off` = instant rollback). Thresholds
  `FIRSTBACK_SCREEN_HARD` (80) / `FIRSTBACK_SCREEN_MID` (45).
- ☐ Optional paid reputation: **`FIRSTBACK_REPUTATION_PROVIDER`** `twilio_nomorobo|hiya`
  (+ `HIYA_API_KEY` for Hiya). Optional AI screen: **`FIRSTBACK_SCREEN_AI=1`** (needs a real LLM key).

### Calendars & field-service sync
- ☐ **Google Calendar + Contacts OAuth** — required for live event create/cancel (until then
  bookings persist locally only). Timezone auto-reads from the primary calendar on connect.
- ☐ **Outlook / MS 365:** `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_REDIRECT_URI`
  (+ optional `MICROSOFT_TENANT_ID`, default `common`). Azure AD app with delegated
  `Calendars.ReadWrite` + `offline_access`.
- ☐ **FSM sync — pick ONE:** Jobber (`JOBBER_CLIENT_ID/SECRET/REDIRECT_URI`, needs Jobber
  Connect tier; recommended) **or** Housecall Pro (`HCP_CLIENT_ID/SECRET/REDIRECT_URI`; confirm
  scope strings at registration, push-back is no-op in v1). If both ever connect, HCP wins.

### Growth engine & widgets
- ☐ Per business: set **`review_link`** (Google review URL) so review-request drafts link
  correctly.
- ☐ **`growth_on`** (per business, default OFF) — auto-queued growth texts. Before shipping a
  UI toggle for it, **add a per-send/per-batch approval step** so the "we don't text anyone you
  haven't approved" promise stays unconditional.
- ☐ **`GOOGLE_PLACES_API_KEY`** — signup business-name autocomplete + Google review tracking
  (inert without it).
- ☐ **Voicemail → lead:** flip *Voicemail* on in Settings + enable Twilio recording/transcription
  on the voice number.
- ☐ **Web-chat "Text us" widget:** flip *Widget* on in Settings, paste the one-line embed onto
  the site (sends are A2P-gated).
- ☐ Deferred (NOT built): **GBP connector** for negative-review response / the 5-Star moment —
  needs a Google Business Profile OAuth + reviews store.

## Tier 4 — Content honesty & cleanup (before/around launch)

- ☐ **Real customer stories** — `/customers` shows honest placeholders today; replace with
  consented quotes (written sign-off before naming a business).
- ☐ **Webinars** — `/webinars` is coming-soon; schedule a real event + wire registration before
  re-adding.
- ☐ **Password reset** — the reset-token flow exists (`/auth/forgot` → `/auth/reset`) but needs
  **SMTP configured** (Tier 1) to deliver the email; otherwise "Forgot password?" routes to
  `/contact` (manual reset).
- ☐ **`/static/og-default.png`** (1200×630, dark + wordmark + "Miss a call. We text back. They
  book.") — then upgrade the link-preview image. *(Note: this session already added an `og:image`
  pointing at `favicon-512.png` as a no-404 fallback; swap to the real OG image when generated.)*
- ☐ Delete the dead, unrouted **`landing.html`** (still carries old Jobber/Housecall/Angi logos).

## Tier 4 — Reliability fast-follows (not launch-blocking)

- ☐ Harden the login rate-limit (it trusts `X-Forwarded-For` — add `ProxyFix` / email-keying).
- ☐ Finish the defense-in-depth `_csrf` sweep on the remaining authenticated config forms
  (`setup/*`, `training/*`); SameSite=Lax covers them today. *(`/settings/growth_mode` already done.)*
- ☐ Make the Stripe `seen`+`mark` atomic (`INSERT OR IGNORE`) before running multiple workers.
- ☐ Optional Phase-5 polish: exclude `failed` from the growth-touch uniqueness index; fold
  reminder-cancel into the cancel-appointment transaction; wrap `incr_rate` in `BEGIN IMMEDIATE`.

---

## Founder decisions (already resolved, 2026-06-23)
- 30-day money-back guarantee badge — **NO** (not shipped).
- Hero leads with "it books the job" + Vic morning briefing — **DONE** (live on homepage).
- Paid caller-reputation tier — **GO, but deferred** until `/pricing` billing is live.
- Soft-overage billing — **HOLD**; keep the "we'll alert you" FAQ wording until billing is wired.

## Already handled (this polish session)
- Site-wide responsive: zero horizontal overflow at 360/768/1280/1440/1920 (verified).
- Marketing SEO: canonical + OG + JSON-LD on indexable pages.
- iOS input-zoom fix; icon-arrow SVGs; exact-match color tokenization.
- `test_inbound_voice` hardcoded-path bug fixed (suite is now fully green on any machine).
