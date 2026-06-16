# RingBack

Instant missed-call text-back + AI booking for home-services contractors.
When a call goes unanswered, RingBack texts the caller within seconds, answers
their questions, and books an estimate -- automatically.

## Run it

```bash
cd ~/ringback
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:8800** and sign in. The seeded owner login and the
one-time setup steps live in [USER_TO_DO.md](USER_TO_DO.md).

## The AI brain

Pluggable, chosen by `RINGBACK_PROVIDER` in `.env`:

- **`minimax`** -- MiniMax (the default today; key already in `.env`).
- **`claude`** -- Anthropic Claude, for the public launch (`ANTHROPIC_API_KEY`).
- **`demo`** -- a zero-setup scripted fallback used automatically if the chosen
  provider has no key or errors, so the app never breaks.

## The surfaces

- **/simulator** -- the live demo you show contractors. Fire a missed call, reply
  as the homeowner, watch it book the estimate. (Free, offline -- no real texts.)
- **/dashboard** -- the contractor's cockpit: leads, conversations, booked
  estimates (with reminder state), and recent owner alerts.
- **/settings** -- business profile, owner alerts, reminders, and calendar
  connection. **The *AI instructions* field is where you shape the conversation.**

## Where the code lives

| File | What it does |
|------|--------------|
| `config.py` | Branding, provider/model choice, scheduling + feature knobs, `.env` loader. |
| `db.py` | SQLite storage, multi-tenant (everything scoped by `business_id`). |
| `ai.py` | **The conversation brain** + booking resolution + lead-note summaries + content screen. |
| `triage.py` | **The call screen** -- the tiered verdict that decides who gets the text-back. |
| `reputation.py` | Gated robocall-reputation lookup (Tier 2 of the screen; dormant until configured). |
| `app.py` | Flask routes, the JSON API, the shared conversation engine, the scheduler. |
| `google_cal.py` | Real, gated Google Calendar sync (freebusy + event-on-book). |
| `messaging.py` | Outbound SMS seam + Twilio plumbing (gated; simulated until configured). |
| `alerts.py` / `mail.py` | Owner alerts (SMS + email + always-on in-app feed). |
| `reminders.py` | Pre-estimate reminders + cold-lead follow-ups (background scheduler). |
| `templates/` | Jinja2 pages. Product UI extends `app_shell.html`; marketing extends `marketing_base.html`. |
| `static/` | `ui.css` (design tokens/components) + `app.css` (product) + `app.js`; marketing CSS is separate. |

## What's real vs. gated

Everything below is built and **safely dormant until you add credentials** -- each
integration is a no-op that simulates in-app until configured, and the UI says so
honestly. Setup steps for each are in [USER_TO_DO.md](USER_TO_DO.md).

- **Login + multi-tenant** -- real signup/login; every tenant's data is scoped by
  `business_id`. (Built.)
- **Google Calendar** -- real OAuth + busy-time sync + event-on-book once
  `GOOGLE_CLIENT_ID/SECRET` are set. Outlook/Apple/Yahoo are honest "Coming soon."
- **Owner alerts** -- SMS (Twilio) + email (SMTP) + always-on in-app feed; pick the
  events per business in Settings.
- **Call screening ("knows who to text")** -- a tiered, precision-first screen
  (`triage.screen_caller`) texts back real prospects, skips spam/robocalls, and leaves
  known callers (auto-derived from bookings -- no import) to the owner. Rolls out
  **safely** via `RINGBACK_SCREEN_MODE` (`off` | `monitor` | `enforce`, default
  `monitor`: log what it *would* screen without silencing anyone). Optional paid robocall
  reputation (`RINGBACK_REPUTATION_PROVIDER`) and AI message screening
  (`RINGBACK_SCREEN_AI`) are gated add-ons; the free tiers screen spam without them.
- **Reminders & follow-ups** -- a background scheduler texts a reminder before each
  estimate and one nudge to a cold lead; simulated onto the thread until Twilio is set.
- **Real phone/SMS (Twilio)** -- `messaging.send_sms` is the outbound seam; the
  inbound call/SMS webhooks (`/webhooks/twilio/*`) are Twilio-signature-verified.
  Provision a number + set creds to go live (see `CALLBACK_SYSTEM_PLAN.md`).

## Reset the data

Deleting `ringback.db` reseeds the default business + owner login -- it wipes all
leads and appointments, so don't do it casually. Migrations run automatically on
boot; the DB and `.env` are gitignored.
