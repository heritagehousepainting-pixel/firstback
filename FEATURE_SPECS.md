# FirstBack — Next Feature Specs

A build brief for the next four features, in enough detail to implement without
re-deriving the architecture. Written for the dev agent that will pick these up
**after the current audit-backlog work lands** (especially audit item #8's
timezone unification — several specs below depend on it; see "Cross-cutting
dependencies").

Today's date when written: 2026-06-14.

---

## How to read this

Each feature section has the same shape:

- **Goal / Why** — what it does and the contractor pain it removes.
- **Data model** — new tables/columns (follow the existing migration style).
- **Backend** — routes, functions, and which existing seams to reuse.
- **Frontend** — pages/components (reuse the product design system).
- **Gating & honesty** — what happens when a dependency isn't configured.
- **Edge cases** — the traps to handle.
- **Verify** — how to prove it works.
- **Out of scope** — explicitly deferred, so the change stays focused.

Build features in the **recommended order** below — the first one establishes an
outbound-messaging abstraction the next two ride on.

---

## Cross-cutting conventions (read once, apply to all four)

These mirror patterns already in the codebase. Match them so the new code reads
like the old code.

1. **Reuse the gated-integration pattern** from [google_cal.py](google_cal.py):
   a module-level `configured()` (are the app credentials present?) plus a
   per-business `is_connected(business_id)` (does this tenant have it linked?).
   **Every entry point is a safe no-op when not configured/connected.** All
   network/API calls are wrapped in try/except that swallows + logs to stderr
   with the `[firstback]` prefix and never breaks a reply or a booking. Use lazy
   `import requests` / `import smtplib` inside the function, not at module top.

2. **Never block the hot path.** Anything that sends a message, calls an LLM, or
   hits a third party runs **off the request thread**, exactly like
   [`app._schedule_notes`](app.py) — a daemon thread with an in-flight + dirty
   guard so bursts coalesce and failures can't crash the process.
   `ai.generate_reply` and the HTTP handlers must stay fast.

3. **Multi-tenant scoping is mandatory.** Every new table carries `business_id`;
   every query filters by it. Get the current tenant via
   `current_business()` in [app.py](app.py). Leads, messages, and appointments
   are already per-business — keep that invariant.

4. **One outbound channel.** Introduce a single `messaging.send_sms(business, to,
   body)` (Feature 3) that all of reminders, owner alerts, and (eventually) the
   AI's replies route through. When Twilio is configured it sends for real; when
   it isn't, it **records the message as an outbound row on the lead's thread**
   (so the simulator still shows it) and returns a "simulated" status. This keeps
   reminders/alerts useful in demo mode and honest about what really went out.

5. **Honesty (audit #4).** Don't render a feature as working when its dependency
   is off. Mirror the Settings "Coming soon" → real "Connect" flip. If SMS isn't
   configured, the UI says reminders are *simulated in-app*, not "sent."

6. **Design system.** Product UI extends [app_shell.html](templates/app_shell.html)
   and uses [app.css](static/app.css). Reuse the existing macros:
   `card`, `stat_row`/`stat_tile`, `data_table`, `pill`, `button`, `empty_state`
   (see [dashboard.html](templates/dashboard.html) for usage). Marketing pages
   are a separate base — don't cross the streams.

7. **Dependency-light.** The project deliberately avoids heavy SDKs (Google sync
   uses raw `requests`). Use `requests` for Twilio's REST API and stdlib
   `smtplib` for email rather than vendor SDKs, unless there's a strong reason.

8. **Idempotency for anything that sends.** A restart, a double tick, or a retry
   must never double-send. Track delivery state in the DB and flip it to `sent`
   under a transaction before/around the send.

9. **Config & secrets.** Add new settings to [config.py](config.py) reading from
   `os.environ` with safe defaults (the `.env` loader already does
   `setdefault`, so real env vars win). Document every new key in
   [USER_TO_DO.md](USER_TO_DO.md) and `.env`.

10. **Tests.** No framework yet, but `google_cal` shipped with mocked unit checks
    — follow suit. Make the time/decision logic **pure and testable** (e.g.
    "compute a reminder's `send_at`", "is this row due?", "should this lead get a
    follow-up?") and unit-test those without network. Mock Twilio/SMTP.

### Cross-cutting dependencies

- **Timezone (audit #8).** Reminders compute "send 24h before a 2:00 PM
  appointment," which is wall-clock-sensitive, and `db.now_iso()` is UTC while
  `db._today()` is local. **Do not build reminder send-time math until #8 lands**
  its business-local-time convention, then use that same convention. Flag any
  spot where you assume server-local == business-local.
- **Outbound abstraction first.** Features 1 and 2 both need to send messages.
  Build the `messaging.send_sms` shim (Feature 3, "Phase A") before them, even if
  real Twilio creds come later — the shim works in simulated mode immediately.

---

## Recommended build order

1. **Feature 3, Phase A only** — the `messaging` outbound abstraction (works
   simulated; no Twilio account needed yet). Unblocks 1 and 2.
2. **Feature 2 — Owner alerts.** Smallest, highest "I didn't have to watch the
   dashboard" payoff; exercises the messaging shim + a second channel (email).
3. **Feature 1 — Reminders & follow-ups.** Needs the scheduler tick; depends on
   #8 timezone.
4. **Feature 4 — ROI dashboard.** Independent (read-side); can slot in anytime.
5. **Feature 3, Phases B–C** — real Twilio inbound/outbound, when the contractor
   provisions a number.

---

# Feature 1 — Reminders & follow-ups

### Goal / Why
Two of the biggest revenue leaks for a contractor are **no-show estimates** and
**warm leads that go cold**. FirstBack already books the estimate; this closes the
loop:
- **Reminder:** auto-text the customer before their booked estimate.
- **Follow-up:** if a lead replied but never booked and then went quiet,
  re-engage them once with a gentle nudge.

### Data model
New table `scheduled_messages` (the outbound queue):

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| business_id | INTEGER | scoping |
| lead_id | INTEGER | who it's for |
| appointment_id | INTEGER NULL | set for reminders, null for follow-ups |
| kind | TEXT | `reminder` \| `followup` |
| send_at | TEXT | ISO; when it becomes due (business-local → UTC per #8) |
| body | TEXT | the message text (may be AI-generated, see below) |
| status | TEXT | `pending` \| `sent` \| `canceled` \| `skipped` |
| created_at | TEXT | |
| sent_at | TEXT NULL | |

Add via the existing `CREATE TABLE IF NOT EXISTS` block in
[`db.init_db`](db.py). Index `(status, send_at)` for the due-query.

### Backend
- **Enqueue a reminder** when a booking succeeds. In
  [`app.sim_reply`](app.py) (and later the real inbound webhook), right after
  `db.book_appointment(...)` returns truthy, compute
  `send_at = appointment_datetime − REMINDER_LEAD_HOURS` (default 24h; config
  key `REMINDER_LEAD_HOURS`) and insert a `pending` `reminder` row. The
  appointment's `day` + `slot_time` give you the datetime — reuse `db.parse_day`
  / `db.time_key` shapes already stored on the appointment.
- **Enqueue a follow-up** for cold warm-leads. A lead is a candidate when:
  stage is `warm` (replied, not booked), last message was inbound or stale by
  `FOLLOWUP_IDLE_HOURS` (default 24h), and it has no prior `followup` row. The
  cleanest trigger is the scheduler tick (below) scanning for candidates, so a
  lead that goes cold organically is caught without a per-request hook.
- **The scheduler tick.** The app is a single process
  (`use_reloader=False` in [app.py](app.py)) with no scheduler today. Add a
  daemon "ticker" thread started at app boot: wake every `TICK_SECONDS`
  (default 60s), in a try/except loop:
  1. `SELECT … WHERE status='pending' AND send_at <= now` (per business).
  2. For each, flip to `sent` **then** call `messaging.send_sms(business,
     lead.phone, body)` (idempotent: claim the row first so a second tick or a
     restart mid-send can't double-send; on send failure, log and optionally
     leave a `failed` state for one retry).
  3. Scan for new follow-up candidates and enqueue them.
  - Put the pure logic (`due_rows(now)`, `followup_candidates(now)`,
    `compute_send_at(appt, lead_hours)`) in testable functions; the thread is a
    thin wrapper. Mirror the structure of `_schedule_notes`'s `_run`.
  - **Production note (document, don't necessarily build):** an in-process
    ticker dies with the process. Offer an alternative: a protected
    `POST /tasks/run-due` endpoint (shared-secret header) that an external cron
    calls every minute. Build the in-process ticker now; leave the endpoint as a
    documented option.
- **Message copy.** Default to a clean template
  (`"Hi {name}, this is {business} — reminder of your free estimate {when}.
  Reply C to confirm or R to reschedule."`). Optionally generate it with
  `ai._llm_complete(provider, system, user_text)` for a warmer tone, but
  template-first keeps it deterministic and free.
- **Two-way handling (minimum viable):** if the customer replies to a reminder
  (`C`/`R`/free text), it arrives as a normal inbound message and the AI handles
  it. A reschedule that re-books should `cancel` the old reminder and enqueue a
  new one. Cancellation of an appointment must set its pending reminders to
  `canceled`.

### Frontend
- On the dashboard's **Scheduled estimates** card, add a small muted line or pill
  per row showing reminder state (`Reminder set · Jun 14 2:00 PM` /
  `Reminder sent` / `—`). Reuse `pill`.
- Settings: a "Reminders & follow-ups" card with toggles (reminders on/off,
  follow-ups on/off) and the lead-time hours. Persist on the business
  (`reminders_enabled`, `followups_enabled`, `reminder_lead_hours`).

### Gating & honesty
- If `messaging` is in simulated mode (no Twilio), reminders still fire — they
  post as outbound rows on the thread and the UI labels them *simulated*, not
  *sent*.
- One follow-up per lead, ever (no nagging). Make this a hard rule.

### Edge cases
- Appointment in the past / `send_at` already past at enqueue → send within the
  next tick (don't skip), but never send a reminder for an appointment that has
  already started.
- Appointment canceled/rescheduled → cancel/replace pending reminders.
- Lead with no usable phone → mark `skipped`, log.
- DST / timezone → comes from #8; until then, document the assumption.
- Quiet hours: don't text before `QUIET_START` / after `QUIET_END`
  (default 8am–9pm business-local); defer to the next allowed window.

### Verify
- Unit: `compute_send_at`, `due_rows`, `followup_candidates`, "one follow-up
  per lead," quiet-hours deferral — all pure, no network.
- Manual: in the simulator, book an estimate, set `REMINDER_LEAD_HOURS=0` and
  `TICK_SECONDS=5`, watch the reminder post to the thread within a tick. Cancel
  → reminder goes `canceled`.

### Out of scope
Recurring/multi-touch drip sequences, customer-initiated reschedule UI, SMS
keyword parsing beyond passing replies to the existing AI.

---

# Feature 2 — Owner alerts

### Goal / Why
The contractor is on a ladder, not at the dashboard. Alert them the moment
something needs them: **a new lead came in** or **an estimate just got booked**
(and optionally **a lead was flagged urgent**). This is the "I can trust it while
I work" feature.

### Data model
- On `businesses`, add alert preferences:
  `alert_email` (default to the owner's login `users.email`),
  `alert_sms` (a real cell number, distinct from the FirstBack `phone`),
  and booleans `alert_on_lead`, `alert_on_booking`, `alert_on_urgent`.
- Optional `alerts` log table (`id, business_id, kind, channel, target, status,
  created_at`) for an audit trail and de-dupe. Recommended but not required for v1.

### Backend
- A small `alerts.py` with `notify(business, kind, context)` that fans out to the
  enabled channels:
  - **SMS** via `messaging.send_sms` (Feature 3 shim).
  - **Email** via stdlib `smtplib`, gated by config
    (`SMTP_HOST/PORT/USER/PASS/FROM`); `configured()` returns False → no-op +
    log. Keep the same defensive try/except pattern as `google_cal`.
  - **In-app (free, always on):** also fine to drop a row the dashboard can show
    as an unread badge; optional for v1.
- **Trigger points** (all already exist):
  - New lead: [`app.sim_incoming`](app.py) after `create_lead` (and the real
    inbound webhook later). Alert kind `lead`.
  - Booking: [`app.sim_reply`](app.py) inside the `if booking and
    db.book_appointment(...)` block. Alert kind `booking`.
  - Urgent: where `db.mark_lead_urgent` is called. Alert kind `urgent`.
- **Off the hot path:** wrap the `notify(...)` call in a daemon thread (reuse the
  `_schedule_notes` thread shape) so a slow SMTP/Twilio call never delays the
  text-back to the customer.
- **Throttle:** collapse a burst (e.g. several rapid bookings) sensibly, and
  de-dupe identical alerts within a short window using the `alerts` log.

### Frontend
- Settings: an "Alerts" card — channel fields (`alert_email`, `alert_sms`) and
  the three toggles. Show channel readiness honestly: if SMTP/Twilio isn't
  configured, the toggle explains alerts are simulated/disabled.
- Message copy: short and actionable, e.g.
  `New lead: Marcus Bell (555) 314-2270 — "kitchen repaint". Open FirstBack →`.

### Gating & honesty
- Email channel needs SMTP config; SMS needs Twilio. If neither is configured,
  the feature degrades to in-app only and says so.
- Default `alert_email` to the logged-in owner's email so it works on day one if
  SMTP is set, with zero extra setup.

### Edge cases
- Don't alert on the **opening text-back** itself (that's not a customer action).
- Don't alert the owner for their own simulator testing if you can distinguish it
  (optional; low priority).
- Missing/invalid `alert_sms` or `alert_email` → skip that channel, log, still
  try the others.

### Verify
- Unit: channel selection from prefs, throttle/de-dupe, copy formatting.
- Manual: set SMTP to a catcher (or log-only), trigger a sim lead + booking,
  confirm exactly one alert per event on each enabled channel and none on the
  opening text-back.

### Out of scope
Daily/weekly digest emails, mobile push (native app), per-user (vs per-business)
alert routing.

---

# Feature 3 — Make it real (Twilio)

### Goal / Why
Everything today is simulated. This wires the existing **TWILIO SEAM**
(see the comment above the API section in [app.py](app.py)) to a real phone
number so FirstBack handles **actual missed calls and real SMS**. This is the leap
from demo to live product. Build in phases so value lands incrementally.

### Phase A — outbound abstraction (build first, no Twilio account needed)
Create `messaging.py` mirroring `google_cal.py`'s structure:
- `configured()` → `bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and a from
  number)`.
- `send_sms(business, to, body)`:
  - If configured: `POST https://api.twilio.com/2010-04-01/Accounts/{SID}/
    Messages.json` with basic auth (`SID`/`AUTH_TOKEN`), form fields
    `From` (the business's FirstBack number), `To`, `Body`. Lazy `import
    requests`, 20s timeout, swallow+log errors, return a status dict.
  - If **not** configured: record the message as an `out` row via
    `db.add_message(lead_id, "out", body)` when a lead context exists, and
    return `{"status": "simulated"}`. This is the shim Features 1 & 2 depend on.
  - Centralize here so the AI's replies can later route through the same function
    instead of only `db.add_message`.

### Phase B — inbound SMS (homeowner replies by real text)
- Provision a Twilio number per business; store it (e.g. `businesses.twilio_number`)
  and map inbound `To` → business. Keep `businesses.phone` as the display value
  or unify them.
- Route `POST /webhooks/twilio/sms`:
  - **Verify the Twilio signature** (`X-Twilio-Signature`, HMAC with the auth
    token over the URL + params) — reject unsigned/forged requests. This is the
    security-critical bit; don't skip it.
  - Look up the lead by `From` phone within that business (most recent open
    conversation) or `create_lead` if new. Then mirror
    [`app.sim_reply`](app.py): `add_message("in")` → `detect_urgency` →
    `generate_reply` → `add_message("out")` → maybe `book_appointment` (+ Google
    event) → `_schedule_notes` → owner alert.
  - Respond with TwiML (or send via the REST API) so the homeowner gets the
    reply. Factor the shared logic out of `sim_reply` so both the simulator and
    the webhook call one function (avoid duplicating the booking flow).

### Phase C — missed-call detection (the actual "missed call" trigger)
- Twilio Voice webhook on the business number. Recommended simple flow: on an
  incoming call, `POST /webhooks/twilio/voice` returns TwiML that rings the
  contractor's real cell (`Dial`) with a short timeout; on no-answer/busy/failed
  (status callback or `Dial` action URL), fire the instant text-back — exactly
  what [`app.sim_incoming`](app.py) does (`create_lead` + `generate_reply` +
  send). That "call them, and if they don't pick up, text the caller" is the core
  promise.
- Document the alternative (a Twilio Studio flow) but implement the webhook
  version to keep config in-repo.

### Config / secrets
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` (or per-business
`twilio_number`). Add to [config.py](config.py), `.env`, and a new
"Connect your phone number (Twilio)" section in [USER_TO_DO.md](USER_TO_DO.md)
with the step-by-step (buy a number, set the Voice + Messaging webhooks to the
two routes, paste creds, restart) — same tone as the Google Calendar section.

### Gating & honesty
- Unconfigured = the whole real path is dormant; the simulator and the
  `send_sms` shim keep working. Settings shows a real "Connect" only when
  configured, like Google.

### Edge cases
- Signature verification failures → 403, logged.
- Duplicate Twilio webhook deliveries (they retry) → idempotent on
  `MessageSid`/`CallSid` (store and dedupe).
- Unknown inbound number → new lead.
- A2P 10DLC registration is required for US business SMS at volume — call this
  out in USER_TO_DO as a real-world gotcha; not a code task.
- Costs: real SMS/voice cost money — keep the simulator as the free demo path.

### Verify
- Unit: signature verification (known-good/known-bad), inbound→lead mapping,
  `send_sms` payload shape (mock `requests`).
- Manual: with a real Twilio test number, text the number and watch a lead +
  AI reply appear in the dashboard; call and don't answer to see the text-back.

### Out of scope (v1)
MMS/photos, voicemail transcription, multiple numbers per business, WhatsApp.

---

# Feature 4 — ROI dashboard

### Goal / Why
Make the product **visibly pay for itself**: leads captured, estimates booked,
conversion rate, and an estimate of **revenue recovered** over time. This is the
retention and launch-story feature ("FirstBack booked you 9 estimates worth ~$X
this month").

### Data model
- No new event tables needed for v1 — `leads.created_at`, `messages`, and
  `appointments.created_at`/`day` already carry the timeline.
- Add `businesses.avg_job_value` (number, owner-set in Settings, default e.g.
  `4500` with a clear "edit this" hint). "Revenue recovered" = booked estimates
  × `avg_job_value` (label it an **estimate**, not actuals — honesty).
- (Optional, later) a `won`/`lost` outcome on appointments to compute real
  closed revenue; out of scope for v1.

### Backend
- Add aggregation queries to [db.py](db.py), all `business_id`-scoped:
  - leads per day/week over a range,
  - estimates booked per day/week,
  - conversion rate (booked ÷ leads),
  - totals for the selected window (this month / last 30 / all time).
- `GET /api/analytics?range=30d` returns the series + totals as JSON
  (login-required, tenant-scoped). Keep it read-only and fast.

### Frontend
- Either a new `/analytics` page (sidebar nav) or a section on the dashboard.
  Reuse `stat_row`/`stat_tile` for the headline numbers (Leads, Booked,
  Conversion %, Est. revenue recovered) — the dashboard already uses these.
- One simple trend chart. **Dependency-light:** render inline SVG bars/line
  yourself (matches the "no heavy SDK" ethos) rather than pulling a charting lib.
  A range switcher (30d / 90d / all) re-fetches `/api/analytics`.
- Empty state via the `empty_state` macro when there's no data yet.

### Gating & honesty
- Label revenue as an **estimate** based on the owner's `avg_job_value`; never
  present it as booked/collected money. If `avg_job_value` is unset, show counts
  only and prompt them to set it.

### Edge cases
- New account with little/no data → graceful empty/low-N states, no divide-by-zero
  on conversion rate.
- Timezone of "per day" buckets → use the #8 business-local convention so a
  late-night lead lands on the right day.

### Verify
- Unit: the aggregation/bucketing functions against a seeded fixture (counts,
  conversion %, revenue math, empty range).
- Manual: load with the existing ~24 demo leads; confirm tiles + chart match a
  hand count, and the range switcher works.

### Out of scope (v1)
Per-source attribution, won/lost pipeline, CSV export, cross-business benchmarks.

---

## Summary table

| # | Feature | Core dependency | Effort | Lands value |
|---|---|---|---|---|
| 3A | Messaging abstraction | none (simulated) | S | Enabler for 1 & 2 |
| 2 | Owner alerts | 3A + SMTP/Twilio | S–M | Trust while working |
| 1 | Reminders & follow-ups | 3A + scheduler + #8 tz | M | No-shows ↓, cold leads ↓ |
| 4 | ROI dashboard | none (read-side) | M | Retention / launch story |
| 3B–C | Real Twilio in/out | Twilio account | L | Demo → live product |

Build 3A → 2 → 1 → 4, with 3B–C when a real number is provisioned. Coordinate the
timezone-sensitive parts of #1 and #4 with audit-backlog item #8.
