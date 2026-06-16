# Setup Needed — to make every claim fully live & true

The site truth-audit (see `SITE_TRUTH_AUDIT.md`) made the copy honest about what's live
vs. gated. This is the list of things **you** must do to turn the honest-but-gated /
beta / placeholder states into fully live ones. Integration credential steps live in
`USER_TO_DO.md`; this file tracks the truth-audit follow-ups.

## Go-Live wizard (`/setup`) — contractor self-serve connection
The **Go Live** page (`connections.py` + `templates/setup.html`) takes a contractor from
signup to live missed-call text-back without a shell or the Twilio console: business profile
+ A2P intake → buy/attach a number (auto-wires webhooks) → submit A2P registration with live
status → carrier call-forwarding code. Honest by construction (driven by
`compliance.launch_blockers` — never "live" until the number is bound, A2P is **approved**,
and forwarding is confirmed).

**Automated:** number provisioning + webhook wiring, A2P status **sync** from Twilio (on view
+ via the `/tasks/run-due` cron), the carrier star-code guide, and the go-live gate.

**Still operator/concierge (v1 — by design):**
- **Server Twilio credentials** (`TWILIO_ACCOUNT_SID/AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
  `RINGBACK_PUBLIC_URL`) are set once in Render env by the operator (one shared account).
- **A2P brand + campaign submission** is concierge: "Submit for registration" marks the tenant
  `pending` and emails the operator (gated `mail` seam) the packet; the operator registers the
  brand+campaign in Twilio, then pastes the **campaign SIDs** into the wizard's *Installer*
  disclosure. `connections.a2p_sync` then flips the tenant to `approved` automatically. **Next
  phase:** submit brand+campaign via the Twilio Trust Hub API so even this is self-serve.
- **Carrier conditional-forwarding** happens on the contractor's phone (a star code); the
  wizard gives the exact code + tap-to-dial + a test-call check, but can't be set server-side.

## To drop the "beta" label on AI voice callback
- Deploy the voice service and set `VOICE_PUBLIC_URL` (re-add the `ringback-voice` service
  to `render.yaml` once it has a shared DB / write-relay — see `ringback-render-deploy`).
- Until then the copy correctly says **"in beta / rolling out on Pro and Crew"** and the
  product falls back to text. Don't sell it as fully included until it's deployed in prod.

## To roll out & tune call screening (the "phone screen")
- **Rollout mode** — `RINGBACK_SCREEN_MODE` (`off` | `monitor` | `enforce`, default `monitor`)
  is the **app-wide default**. It ships in **monitor**: it logs what it *would* screen (see the
  "Would screen" list + banner on the dashboard) but still texts everyone, so nothing real is
  silenced. Each business can also pick its own mode in **Settings → Call screening** (which
  overrides the env default; "Use the default" inherits it). When the monitor numbers look
  right, switch to **Enforce**. `off` is the instant rollback. Thresholds:
  `RINGBACK_SCREEN_HARD` (default 80) / `RINGBACK_SCREEN_MID` (45).
- **Optional paid robocall reputation (Tier 2)** — `RINGBACK_REPUTATION_PROVIDER`
  (`off` | `twilio_nomorobo` | `hiya`). `twilio_nomorobo` reuses your Twilio creds (Lookup +
  Nomorobo Spam Score add-on); `hiya` needs `HIYA_API_KEY`. Cached, fail-open. The free tiers
  screen spam without it.
- **Optional AI message screening (Tier 3)** — `RINGBACK_SCREEN_AI=1` reads a caller's first
  reply to bail on an obvious sales/robocall message (needs a real AI provider key; the demo
  brain always passes — fail-open).

## To make "works with Google Calendar / texts / email" say LIVE instead of simulated
- Add the credentials in `USER_TO_DO.md`: Twilio (SMS), Google OAuth (Calendar + Contacts),
  SMTP (email). Each is a gated no-op that simulates in-app until configured — the UI
  already says so honestly.

## To encrypt stored Google tokens at rest (recommended before prod)
- Set **`RINGBACK_TOKEN_KEY`** (any long random string, different from `RINGBACK_SECRET`).
  With it set, every Google access/refresh token is encrypted in the SQLite file
  (`token_crypto.py`: stdlib HKDF + SHA-256 keystream + HMAC, encrypt-then-MAC, marked
  `enc:v1:`). **Unset = safe no-op** so local dev and the current DB keep working.
- **Migration-safe / dual-read:** already-connected businesses are untouched — their
  legacy plaintext token still reads, and the next token refresh re-stores it encrypted.
  No code mutates the live DB. **One-time re-encrypt path:** have each connected business
  click **Disconnect → Connect** once (or just wait for the hourly refresh to roll them
  over). Rotating the key makes old-key tokens unreadable → those businesses reconnect once.
- See `USER_TO_DO.md → A2` for the step-by-step.

## Password reset (currently a gap)
- There is **no automated password-reset flow**. "Forgot password?" on the login page now
  routes to `/contact` (honest: you reset it manually) instead of a dead `#` link.
- To deliver self-serve reset: build a reset-token email flow (needs SMTP configured).

## Real customer stories (placeholders today)
- `/customers` now shows honest **placeholders** instead of invented testimonials.
- Replace with real, consented quotes once contractors are live. Keep the "no invented
  quotes" promise — get written sign-off before naming a business.

## Webinars (coming-soon today)
- `/webinars` no longer lists a fake dated event or "watch on demand" recordings.
- Before re-adding a live event: actually schedule it and wire "Get notified" (`/contact`)
  to a real list, or add a registration route.

## Pricing / "free" wording
- CTAs changed from "Sign up for free" to **"Get started"** (pricing is paid-only: $99/$199/$399).
- There is **no billing system** yet. Before charging, add billing (e.g. Stripe) so the
  "cancel anytime / no per-call fees" promises in pricing/FAQ are actually enforceable.
- If you want a real free trial, add trial copy + logic and the CTAs can say "Start free trial".

## Optional cleanup flagged by the audit
- Delete the dead, unrouted `landing.html` (still contains the old Jobber/Housecall/Angi
  logos; harmless since it isn't served, but worth removing — roadmap already flags it).
