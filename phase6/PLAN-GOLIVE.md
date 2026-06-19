# Phase 6 — Go-Live Runbook
**Date:** 2026-06-18  
**State at entry:** `staging` branch, 58 tests green, NOT deployed. Live site = old version. Billing collects nothing. A2P unsubmitted.  
**Goal:** ordered path from "undeployed staging" to "live and collecting $99/mo."

---

## Reading this document

- **HARD BLOCKER** = nothing works / no revenue is possible until this is done.
- **Feature gate** = feature silently degrades or simulates until resolved; not a ship-stopper.
- **Order matters** = several steps must be done in the exact sequence given or you risk data loss.

---

## Step 1 — Render env reconcile + DB path safety (DO FIRST, before any deploy)

### Why this is the riskiest owner-op
`render.yaml` was renamed and restructured. If the old Render service still has stale env var names or the wrong `FIRSTBACK_DB_PATH`, the app will start, "work", but point at the wrong file — writing to the default ephemeral path instead of the persistent disk. Every deploy wipes the DB. You will not notice until data disappears.

### The data-loss ordering hazard — do NOT reverse these steps
1. **Open the Render dashboard BEFORE deploying the new code.** Do not deploy first.
2. Check what `FIRSTBACK_DB_PATH` is set to on the current live service. It should be `/var/data/firstback.db`. If it is pointing anywhere else (e.g., `/var/data/ringback.db` from the rename), **fix it now** and trigger a manual deploy of the OLD code first so the DB migrates to the correct path while data is still intact.
3. Only after confirming the path is correct do you proceed with the new code deploy.

### Full env checklist — set ALL of these in Render Environment before deploying

| Env var | Required value / action | Gates what |
|---|---|---|
| `FIRSTBACK_DB_PATH` | `/var/data/firstback.db` | **ALL DATA** — wrong value = DB reset on every deploy |
| `FIRSTBACK_DB_LOCAL_MIRROR` | `1` | Local-disk mirror (fixes network-FS boot hang) |
| `FIRSTBACK_SECRET` | Long random (`python3 -c "import secrets; print(secrets.token_hex(32))"`) | **HARD BLOCKER** — prod refuses to start without this (fail-fast in config.py) |
| `FIRSTBACK_OWNER_PASSWORD` | Strong password (not `firstback123` or the dev default) | **HARD BLOCKER** — prod refuses to start with the dev default |
| `FIRSTBACK_HTTPS` | `1` | Secure session cookies; required alongside FIRSTBACK_SECRET |
| `FIRSTBACK_RUN_TICKER` | `1` | In-process scheduler (belt); external cron is the suspenders |
| `FIRSTBACK_TASKS_SECRET` | Long random (different from SECRET) | Guards `/tasks/run-due`; unset = cron always 403, scheduler silently dead |
| `FIRSTBACK_TOKEN_KEY` | Long random (different from both above) | Encrypts Google OAuth tokens at rest; unset = tokens stored plaintext |
| `FIRSTBACK_TZ` | `America/New_York` (or your zone) | Date/time display; default = server zone, likely wrong |
| `FIRSTBACK_PUBLIC_URL` | `https://ringback-gixe.onrender.com` (or custom domain once set) | **SF-4 delivery receipts + retry**, **SF-7 sentinel verification** — both silently inert without this |
| `FIRSTBACK_SCREEN_MODE` | `monitor` | Safe default; enforce later after watching monitor numbers |
| `FIRSTBACK_OPERATOR_EMAILS` | `heritagehousepainting@gmail.com` | Operator allowlist for A2P SID recording in the wizard |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Claude brain; without it falls back to demo (safe, but no real AI) |
| `FIRSTBACK_PROVIDER` | `claude` | Locks the brain; without it config.py defaults to `claude` but key must also be set |
| `TWILIO_ACCOUNT_SID` | From Twilio console | Real SMS/voice |
| `TWILIO_AUTH_TOKEN` | From Twilio console (set as secret, never in git) | Real SMS/voice |
| `TWILIO_FROM_NUMBER` | `+12677562454` (or your provisioned number) | Real SMS |
| `ALERT_FROM_NUMBER` | Platform Twilio number for owner-alert SMS | Owner alerts off the tenant's A2P number |
| `SMTP_HOST` | `smtp.resend.com` | Email alerts (feature gate, not a blocker) |
| `SMTP_FROM` | `alerts@firstback.app` | Email alerts |
| `SMTP_USER` / `SMTP_PASS` | Resend API key as SMTP_PASS | Email alerts |

**Code is already written for all of the above.** Each env var is read in `config.py`. No code changes needed.

### Definition of "Step 1 live"
- App starts without a RuntimeError (SECRET + OWNER_PASSWORD fail-fast pass)
- `/login` loads and the login cookie sets Secure (HTTPS=1)
- `/health/ticker` returns `ok` and reports a recent heartbeat
- DB is at `/var/data/firstback.db` on the persistent disk (verify in Render disk panel)

---

## Step 2 — External cron → `/tasks/run-due`

### What this is
The in-process ticker (`FIRSTBACK_RUN_TICKER=1`) is the fallback. The external Render cron is the production-grade driver. `/health/ticker` reports staleness if the ticker hasn't fired in >10 minutes.

### What's already coded
- `POST /tasks/run-due` with header `X-Tasks-Secret: <value>` drives all scheduled work: reminders, follow-ups, growth tray, contacts sync, A2P status polling, screening graduation, stall nudges, morning digest, sentinel probes, and the weekly ROI digest fanout.
- Returns 403 if `FIRSTBACK_TASKS_SECRET` is unset (fails closed — correct behavior).

### Owner action
In the Render dashboard, create a **Cron Job** service (not a web service):
- **Schedule:** `* * * * *` (every minute)
- **Command:** `curl -s -X POST https://ringback-gixe.onrender.com/tasks/run-due -H "X-Tasks-Secret: <your FIRSTBACK_TASKS_SECRET value>" || true`
- The `|| true` keeps the cron from erroring on transient network issues.

Note: Render's Cron Job service is free on the starter plan. This is separate from the web service.

**Dependency:** Step 1 must be complete first (`FIRSTBACK_TASKS_SECRET` must be set before the cron can authenticate).

### Definition of "Step 2 live"
- `/health/ticker` reports `last_tick` within the last 2 minutes
- After a few minutes, the Render cron dashboard shows recent successful runs

---

## Step 3 — Stripe live: 6 Price IDs + keys

### What's already coded
All billing code is built and tested against mocked Stripe in `billing.py`:
- Checkout session creation, subscription webhooks (signature + idempotency), `usage_grants` table, subscription gate with migration guard (existing Heritage tenant is never locked out on first deploy).
- The webhook endpoint is `/webhooks/stripe`.

### Why this is a hard blocker for revenue
Until Stripe keys are set, the pricing page has no working checkout. No $99 flows.

### Owner action — exact sequence
1. **Stripe dashboard (test mode first):** go to https://dashboard.stripe.com → Products → Create Product for each of the three tiers, then create prices under each.

   Create exactly **6 Price IDs:**

   | Env var | Amount | Interval |
   |---|---|---|
   | `STRIPE_PRICE_STARTER` | $99.00 | monthly |
   | `STRIPE_PRICE_PRO` | $199.00 | monthly |
   | `STRIPE_PRICE_CREW` | $399.00 | monthly |
   | `STRIPE_PRICE_STARTER_ANNUAL` | $950.00 | yearly |
   | `STRIPE_PRICE_PRO_ANNUAL` | $1,910.00 | yearly |
   | `STRIPE_PRICE_CREW_ANNUAL` | $3,830.00 | yearly |

   Note: annual subscribers get the same monthly conversation allotment — the fuel gauge refills every calendar month regardless of billing interval.

2. **Set in Render env:**
   - `STRIPE_SECRET_KEY` = `sk_test_...` (test mode first; switch to `sk_live_...` when ready to collect real money)
   - `STRIPE_WEBHOOK_SECRET` = `whsec_...` (get from Stripe Dashboard → Webhooks → Add endpoint → `https://ringback-gixe.onrender.com/webhooks/stripe` → reveal signing secret)
   - All 6 Price IDs above

3. **Test before going live:** use Stripe's test card `4242 4242 4242 4242` to exercise the full Checkout → webhook → subscription gate flow before switching to live keys.

4. **Switch to live keys:** replace `sk_test_` with `sk_live_` and update the webhook endpoint to point to live mode. The webhook endpoint URL stays the same.

**Optional:** set `CLAUDE_DAILY_COST_CAP_USD` if you want a per-tenant AI spend cap other than the $1.00 default.

### Definition of "Step 3 live"
- A test signup completes Stripe Checkout and the user sees the dashboard (not a "subscribe to continue" gate)
- The `/webhooks/stripe` endpoint returns 200 for Stripe test events (verify in Stripe webhook logs)
- Switching to `sk_live_` means a real $99 charge can be collected

---

## Step 4 — A2P submission

### Context: why this is a hard blocker for tenants going live
Until a tenant's A2P campaign is approved, every outbound text-back is filtered by carriers. The app simulates sends (records them in the DB, shows them in the dashboard) but nothing reaches a real phone. The go-live wizard's honest `is_live` gate (`compliance.launch_blockers`) will not flip a tenant to "live" until A2P is approved.

### What's already coded
- `connections.py`: `submit_a2p` orchestration for both paths (sole-prop / LLC)
- Twilio Trust Hub WRITE API: `create_a2p_brand` / `create_a2p_messaging_service` / `create_a2p_campaign` — gated on `TWILIO_TRUST_PRODUCT_SID` being set (prevents accidental live submissions)
- `a2p_sync` status poller (runs on `/tasks/run-due` cron) — flips a tenant to `approved` automatically when Twilio reports VERIFIED
- Per-contractor micro-site at `/c/<slug>` (Path B LLC branding URL)
- Auto-flush of blocked text-backs on approval (6-rule safety gate)
- EIN fork: sole-prop path collects zero EIN; LLC path collects EIN + auto-generates the mini-site

### Heritage House dogfood (do this first — unblocks everything)

Before charging any customer, run the **first real submission** with Heritage House to validate the end-to-end path. This also resolves the existing A2P denial.

**The denial root cause (from SEAMLESS-VERDICT.md):** the original submission used a URL showing FirstBack's branding under Heritage's EIN (content mismatch), and/or the page lacked SMS-specific privacy policy + opt-in language.

**Two paths — pick one:**

**Path A — Sole Proprietor (fastest; Heritage has no EIN? Use this):**
- In the `/setup` wizard: choose "No EIN" at the business type fork
- Collect: legal name, personal address, personal Gmail (NOT heritagehousepainting@gmail.com as a company-domain email — use a personal Gmail if available, or confirm sole-prop accepts the Gmail)
- The wizard generates a sole-prop submission; Twilio sends an OTP; reply YES
- Approval time: minutes to hours
- Limit: ~1,000 msgs/day T-Mobile; fine for Heritage volume

**Path B — LLC / Standard (if Heritage has an EIN):**
- Register Heritage under its own business name with a page on **Heritage's own domain** (not firstback.app or firstback.io) that has: (a) Heritage's legal name + address, (b) an unchecked SMS opt-in checkbox, (c) a privacy policy with "we don't share mobile opt-in data with third parties" language
- Set `TWILIO_TRUST_PRODUCT_SID` in Render (the gate that turns the Write API from simulated to live)
- Submit via the `/setup` wizard (LLC path); the operator records the brand/campaign SIDs in the wizard's Installer disclosure once Twilio returns them
- `a2p_sync` (run by the cron) flips Heritage to approved automatically

### The 3 hard confirmations before scaling (HC-1, HC-2, HC-3)

These are DEFERRED — not blockers for Heritage dogfood, but must be confirmed before onboarding paying LLC customers:

- **HC-1:** One real LLC test submission with `[slug].firstback.io` as the brand URL to confirm the ISV subdomain passes TCR for a contractor brand. (Confidence: Likely, not Confirmed. If it fails, LLC path needs the contractor's own domain.)
- **HC-2:** One Twilio CSP call to confirm `{slug}@clients.firstback.com` satisfies the Authentication+ email rule for Standard brands. (The Cloudflare catch-all route is built; the carrier acceptance is unconfirmed.)
- **HC-3:** Confirm the exact sole-prop Starter-brand OTP mechanics end-to-end with the Heritage dogfood submission.

### DNS ops required for LLC path (Path B) — owner action
- Register `firstback.io` domain → wildcard DNS `*.firstback.io → ringback-gixe.onrender.com`
- Set up Cloudflare Email Routing catch-all `@clients.firstback.com → your forward address`
- Without these: `/c/<slug>` micro-site routes work in the app but subdomains don't resolve; the per-contractor email can't receive Twilio verification

### Set in Render env (Path B / Write API)
- `TWILIO_TRUST_PRODUCT_SID` — the gate; until set, `submit_a2p` returns "simulated" and submits nothing
- `TWILIO_A2P_RESELLER_SID` — optional; include on campaigns only if using the ISV reseller path

### Definition of "Step 4 live"
- Heritage's `a2p_status` = `approved` in the DB (visible on `/setup`)
- The `/setup` wizard shows "live" (not "test mode") for the Heritage business
- A real missed call to Heritage's forwarded number triggers an actual SMS text-back to the caller
- Auto-flush fires (any calls that came in during the pending window get their queued text-back replayed)

---

## Step 5 — Resend (email provider)

### What's already coded
`alerts.py` uses `mail.configured()` to gate email sends. When SMTP is not set, email alerts are skipped silently (SMS + in-app still work). The render.yaml already has `smtp.resend.com` as a placeholder.

### Why this is not a hard blocker
The product works without email. Owners get alerts via SMS and in-app. Email is a belt-and-suspenders path.

### Owner action
1. Create account at https://resend.com
2. Add and verify the `firstback.app` sending domain (DNS TXT + MX records in your domain registrar)
3. Create an API key; use it as `SMTP_PASS` (Resend uses the API key as the SMTP password)
4. Set in Render env:
   - `SMTP_HOST` = `smtp.resend.com`
   - `SMTP_PORT` = `587`
   - `SMTP_USER` = `resend` (literal string — Resend uses "resend" as the SMTP username)
   - `SMTP_PASS` = your Resend API key
   - `SMTP_FROM` = `alerts@firstback.app`

### Definition of "Step 5 live"
- Owner receives a real email alert when a test lead is created
- `mail.configured()` returns True (visible in logs on startup)

---

## Step 6 — Google Contacts creds (unlocks Phase 5f)

### What's already coded
`google_contacts.py` + `contact_import.py` handle the nightly contacts sync. The sync is **completely inert** until Google OAuth creds are set — it checks `google_contacts.is_connected(biz)` before every sync and skips without error when not connected. No false "synced" state.

### What this unlocks
Phase 5f (F08 nightly contacts sync): auto-suggest customer matches from Google Contacts so the owner can accept/reject them from the command center. "Accept all N" button. Useful for existing businesses with contacts already in Google.

### Owner action
1. Go to https://console.cloud.google.com → project used for Calendar → APIs & Services → Library
2. Enable **Google People API** (separate from Calendar API; same OAuth client can power both)
3. In the OAuth client's Authorized redirect URIs, add the production redirect URI:
   `https://ringback-gixe.onrender.com/api/contacts/google/callback`
4. Set in Render env (same values as Calendar if already set):
   - `GOOGLE_CLIENT_ID` = your OAuth client ID
   - `GOOGLE_CLIENT_SECRET` = your OAuth client secret
   - `GOOGLE_CONTACTS_REDIRECT_URI` = `https://ringback-gixe.onrender.com/api/contacts/google/callback`
   - `GOOGLE_REDIRECT_URI` = `https://ringback-gixe.onrender.com/api/calendar/google/callback` (update if not already set to the production URL)
5. After deploy: in the app Settings → Connect → connect Google Contacts (the button appears once the creds are set)

### Definition of "Step 6 live"
- The Google Contacts card in Settings shows a Connect button (not "Coming soon")
- After connecting, the nightly sync runs via `/tasks/run-due` and suggests matches

---

## Step 7 — Voice deploy decision (unlocks Phase 5g, behind a 7-check gate)

### Current status
Voice (`firstback-voice` / `voice_service.py`) is DEFERRED by owner choice. The pricing page correctly says "coming soon / beta." The code is built but the 7-check gate cannot be passed without a real deployment.

### The 7-check gate (all must pass before voice is sold as included)
1. Voice service deployed as a separate Render service
2. `FIRSTBACK_VOICE_URL` set (the public HTTPS base for the TwiML/WebSocket)
3. `FIRSTBACK_WEB_URL` set on the voice service (so it relays booking writes back to the web app via `/internal/voice/turn`)
4. `FIRSTBACK_INTERNAL_SECRET` set on both services (shared secret for the internal relay)
5. Premium TTS voice selected by ear (`FIRSTBACK_VOICE_TTS`)
6. One real call that books an estimate and sounds good (ear-test)
7. Voice metered into the credit economy (`FIRSTBACK_VOICE_MONTHLY_CAP_CENTS`, `FIRSTBACK_VOICE_CREDIT_RATE_CENTS`)

### The database sharing problem
`render.yaml` documents this clearly: a separate Render service cannot share the web app's SQLite disk. The voice service relays booking writes through `/internal/voice/turn` on the web app instead of writing the DB directly — keeping SQLite single-writer. `WEB_INTERNAL_URL` on the voice service points to the web app's URL; `INTERNAL_SECRET` is the shared gate. When `WEB_INTERNAL_URL` is empty (local/tests), the voice service runs the shared engine in-process.

### Dispatcher Call (already working without voice deploy)
`FIRSTBACK_VOICE_URL` also gates the Dispatcher Call (urgent caller → owner's phone rings, hears the caller's words, press-1 to connect). Until `VOICE_URL` is set, urgent leads still get an SMS alert — honest fallback, no broken feature.

### Owner action (when ready)
1. Uncomment the `firstback-voice` service in `render.yaml`
2. Set the 7 env vars above
3. Run through all 7 checks before removing "coming soon" from pricing
4. Do not sell voice as fully included until the ear-test passes on a real call

### Definition of "Step 7 live"
- A caller texts "CALL" and receives a real AI voice callback
- The call books an estimate and the booking appears in the dashboard
- Voice usage is metered and the monthly cap works

---

## Step 8 — Custom domain (production trust fix)

### Why this matters
The current live URL `ringback-gixe.onrender.com` reads as untrustworthy to contractors and homeowners. It also leaks the old "ringback" branding.

### Owner action
1. Register `firstback.app` (or confirm it is already registered)
2. In Render dashboard → your web service → Settings → Custom Domains → Add domain
3. Point DNS: CNAME `firstback.app` (or `app.firstback.app`) → `ringback-gixe.onrender.com`
4. After DNS propagates, update `FIRSTBACK_PUBLIC_URL` in Render env to the new domain
5. Re-point Twilio webhook URLs (voice inbound + SMS inbound) to the new domain LAST
6. Keep `ringback-gixe.onrender.com` reachable during the DNS cutover window (Render keeps the old URL live)
7. Update `GOOGLE_REDIRECT_URI` and `GOOGLE_CONTACTS_REDIRECT_URI` in Render env to the new domain and add the new redirect URIs to the Google OAuth client

**Note:** `FIRSTBACK_PUBLIC_URL` drives SF-4 delivery receipts + SF-7 sentinel verification. After the domain swap, verify both still work (check `/health/ticker` and look for `status_callback_url` being set correctly on an outbound SMS).

### Definition of "Step 8 live"
- `https://firstback.app` (or chosen domain) loads the login page
- Twilio webhooks are receiving signed requests at the new URL
- Google OAuth redirect works at the new URL

---

## Pre-deploy code audit gate

Before any production deploy of the `/webhooks/stripe` and `/auth/reset` paths, run a `be-audit`. Both are called out in SETUP_NEEDED.md as money/security gates. The audit is recommended, not a hard blocker for the initial deploy, but do not skip it before accepting real Stripe payments.

Outstanding punch-list from Phase 5 audit (none block local/simulated use; address before real customer sends):
- Growth auto-send (`growth_on=1`) makes the trust headline conditional — no UI toggle exists yet so this cannot be triggered from the app, but add a per-send approval step before building the toggle
- A failed growth touch cannot be retried (dedupe index blocks re-queue) — fix the `failed` status exclusion before growth goes live
- `/tasks/run-due` returns 403 when `FIRSTBACK_TASKS_SECRET` is unset — verified in config.py; set it (Step 1)

---

## Ordered critical path to first dollar

```
1. Render env reconcile (do BEFORE first deploy) ← riskiest op, data-loss hazard
2. Deploy staging branch to Render                ← app starts, passes fail-fast checks
3. External cron → /tasks/run-due               ← scheduler live
4. Stripe test mode: 6 Price IDs + keys          ← test a $99 checkout end-to-end
5. Heritage A2P dogfood (sole-prop OTP)          ← first real SMS send
6. Switch Stripe to live keys                    ← first real $99 collected
```

Steps 5 (Resend), 6 (Google Contacts), 7 (voice), 8 (custom domain) are improvements, not blockers to first revenue.

---

## Hard blockers to first dollar (summary)

| Blocker | What fails without it |
|---|---|
| `FIRSTBACK_SECRET` not set | App refuses to start (RuntimeError in config.py) |
| `FIRSTBACK_OWNER_PASSWORD` = dev default | App refuses to start (RuntimeError in config.py) |
| `FIRSTBACK_DB_PATH` wrong / stale | DB resets on every deploy — all data lost |
| `FIRSTBACK_TASKS_SECRET` not set | Cron always 403; scheduler silently never runs |
| Stripe keys + 6 Price IDs not set | No checkout; billing collects nothing |
| A2P not approved | No real SMS reaches any phone; tenants never flip "live" |

---

## The single riskiest owner-op

**Render env reconcile BEFORE the first deploy.** Specifically: confirming `FIRSTBACK_DB_PATH` is correct and the persistent disk is mounted at `/var/data` before pushing the new code. If you deploy first and the path is wrong, the app starts on an empty ephemeral DB, creates a fresh `firstback.db` in the wrong location, and you will not notice until you look for your data after the next deploy wipes it. The anti-clobber guard in `db.py` (`backup_to_durable` refuses to overwrite a populated backup with an empty live DB) is a safety net — but it only helps if the backup path is also correct. Do the env reconcile first.
