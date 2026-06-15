# Setup Needed — to make every claim fully live & true

The site truth-audit (see `SITE_TRUTH_AUDIT.md`) made the copy honest about what's live
vs. gated. This is the list of things **you** must do to turn the honest-but-gated /
beta / placeholder states into fully live ones. Integration credential steps live in
`USER_TO_DO.md`; this file tracks the truth-audit follow-ups.

## To drop the "beta" label on AI voice callback
- Deploy the voice service and set `VOICE_PUBLIC_URL` (re-add the `ringback-voice` service
  to `render.yaml` once it has a shared DB / write-relay — see `ringback-render-deploy`).
- Until then the copy correctly says **"in beta / rolling out on Pro and Crew"** and the
  product falls back to text. Don't sell it as fully included until it's deployed in prod.

## To make "works with Google Calendar / texts / email" say LIVE instead of simulated
- Add the credentials in `USER_TO_DO.md`: Twilio (SMS), Google OAuth (Calendar + Contacts),
  SMTP (email). Each is a gated no-op that simulates in-app until configured — the UI
  already says so honestly.

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
