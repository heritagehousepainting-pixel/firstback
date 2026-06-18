# Phase 2 — RECONCILED Build Spec ("core loop is trustworthy")
**Date:** 2026-06-18 · **Base:** staging @ cecc076 (clean, 28/28 green)
**Reconciled by Opus from AUDITOR-A.md + AUDITOR-B.md.** Where the two auditors diverged, the
decision + rationale is recorded inline. This is the authoritative spec the 3 build agents follow.
Build agents: do NOT re-litigate decisions marked **[DECIDED]**.

Tests are standalone scripts: `.venv/bin/python test_X.py` (NEVER pytest). Plain asserts; exit non-zero on fail.

---

## GLOBAL DECISIONS (apply everywhere)

- **[DECIDED] biz_tz signature** — `biz_tz(business)` accepts a **dict OR an int id** (config.py). When given a
  dict (the hot path in reminders) it reads `business["timezone"]` with no DB hit; when given an int it does a
  lazy `import db` inside the function and `db.get_business(id)`. Never raises; always returns a tzinfo. Uses
  `zoneinfo.ZoneInfo` (the repo already uses zoneinfo — NO pytz, NO static utcoffset arithmetic; DST must work).
- **[DECIDED] Turn cap counts INBOUND** messages (`direction == "in"`), cap = 12, and the handoff reply MUST
  include the business phone (`business.get("phone")`) so a stuck customer can reach a human.
- **[DECIDED] SF-7 honesty rule** — `forwarding_confirmed` is set TRUE in **exactly one place**: inside
  `twilio_voice_inbound` when the inbound `CallSid` matches the stored sentinel SID. The `/setup/forwarding`
  route NEVER sets confirmed=True when Twilio is configured (it only places the sentinel + shows "Verifying…").
  When Twilio is NOT configured (local/dev), keep a clearly-labelled manual fallback. No "confirmed on placed".
- **[DECIDED] SF-4 retry is always ASYNC** — a failed send schedules a NEW delayed row in `scheduled_messages`
  (kind `sms_retry`). NEVER a synchronous retry loop in the send path or the webhook (would block the ticker
  and risk double-sends on Twilio's occasional 5xx-on-success). Backoff 30s / 2m / 10m; cap 3 then owner alert.
- **[DECIDED] Buffer default 60** — set `buffer_minutes=60` on NEW businesses in `db.create_business` ONLY.
  Do NOT change `DEFAULT_BUFFER_MINUTES` in config and do NOT touch existing tenants' stored buffer (protects
  the live Heritage tenant from a silent behavior change).
- **[DECIDED] RSVP** — `classify_rsvp(text) -> "yes" | "no" | "unknown"` (3-value). On the wire: "yes" → log +
  owner-notify ("customer confirmed"); "no" → owner-notify + let the existing AI handle rebooking. Do **NOT**
  auto-cancel the appointment (a false-positive would destroy a real booking — defer destructive action). The
  classifier itself is the tested deliverable.
- **[DECIDED] Reliability logging** — add `@app.errorhandler(404/500)` + templates only. For "logging," the 500
  handler uses the existing `print("[firstback] 500: …", file=sys.stderr, flush=True)` convention. Do NOT
  introduce `logging.basicConfig` (avoid changing Render's existing stderr format / double-logging).
- **convos.py / llm.py are trades_core-synced kernels — NOT touched in Phase 2.** F03 guards live in **ai.py**.
- Curly/smart quotes break Jinja — never use them as `{% %}` / `{{ }}` delimiters in the new error templates.

---

## SHARED SEAMS (canonical signatures — agents must match exactly)

| Seam | Owner (defines) | Callers |
|---|---|---|
| `config.biz_tz(business)` (dict|int) | **A1** (config.py) | A2 (google_cal), A3 (reminders) |
| `config.sms_status_callback_url()` -> str("" if no base) | **A1** (config.py) | A1 (messaging) |
| `config.NPA_TO_IANA` dict (~50 US NPAs, all 6 zones) | **A1** (config.py) | — |
| `db.set_business_timezone(business_id, tz_name)` | **A1** | A2 |
| `db.set_google_event_id(appointment_id, event_id)` | **A1** | A3 (via google_cal store path) / A2 |
| `db.set_forwarding_sentinel(business_id, call_sid, sent_at)` (None,None clears) | **A1** | A2 |
| `db.set_forwarding_probe(business_id)` (sets forwarding_last_probe_at=now) | **A1** | A2 |
| `db.queue_sms_retry(business_id, lead_id, to, body, attempt, send_at, kind="sms_retry")` | **A1** | A3 |
| `db.get_message_by_provider_sid(provider_sid) -> dict|None` | **A1** | A3 |
| `db.find_scheduled_message(business_id, lead_id, kind) -> dict|None` | **A1** | A3 |
| `google_cal._slot_dt(day_iso, time_key_str, tz=None)` (adds tz) | **A2** | internal |
| `google_cal.create_event(business_id, summary, description, day_iso, time_key_str, tz=None)` (adds tz, returns id) | **A2** | A2 |
| `google_cal.create_event_and_store(business_id, appointment_id, summary, description, day_iso, time_key_str, tz=None)` | **A2** | A3 call sites |
| `google_cal.create_event_async(business_id, appointment_id, summary, description, day_iso, time_key_str, tz=None)` (signature CHANGES: adds appointment_id + tz) | **A2** | A3 call sites |
| `google_cal.cancel_event_async(business_id, google_event_id)` | **A2** | A2 (cancel route) |
| `connections.send_sentinel_call(business_id) -> dict` | **A2** | A2 (setup route) |
| `connections.check_forwarding_health()` (weekly probe driver) | **A2** | A3 (one call in tick_once) |
| `reminders.classify_rsvp(text) -> str` | **A3** | A3 (app.py wire) |
| `alerts` kinds `"sms_fail"`, `"forwarding_lost"` | **A1** | A3 / A2 |

**Existing functions confirmed present** (do not redefine): `db.find_appointment(business_id, lead_id, day, slot_time)`:2112,
`db.book_appointment`:1148, `db.cancel_appointment(business_id, appointment_id)`:2274, `db.create_business`:798,
`db.get_business`:812, `db.add_scheduled_message`:2092, `db.set_message_delivery` (already wired in the status webhook),
`messages.delivery_status` column, `messaging.send_sms(...status_callback=None...)`:68, `messaging.place_call`:191.

---

## DB MIGRATIONS — all in `db.init_db`, guarded `if col not in cols`, ALL owned by **A1**

```python
# Phase 2 — SF-4 retry tracking on scheduled_messages
sched_cols = [r[1] for r in c.execute("PRAGMA table_info(scheduled_messages)").fetchall()]
for col, ddl in (("retry_count", "INTEGER DEFAULT 0"), ("retry_of", "INTEGER")):
    if col not in sched_cols:
        c.execute(f"ALTER TABLE scheduled_messages ADD COLUMN {col} {ddl}")

# Phase 2 — SF-7 sentinel tracking on businesses
biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
for col, ddl in (("forwarding_sentinel_sid", "TEXT"),
                 ("forwarding_sentinel_at",  "TEXT"),
                 ("forwarding_last_probe_at","TEXT")):
    if col not in biz_cols:
        c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")

# Phase 2 — F04 Google event id on appointments
appt_cols = [r[1] for r in c.execute("PRAGMA table_info(appointments)").fetchall()]
if "google_event_id" not in appt_cols:
    c.execute("ALTER TABLE appointments ADD COLUMN google_event_id TEXT")
```

---

## PARTITION (3 file-disjoint slices; app.py split by non-overlapping line ranges)

### AGENT 1 — Foundation (db + config + messaging + alerts)
**Owns exclusively:** `db.py`, `config.py`, `messaging.py`, `alerts.py`.
**Work:**
- All 3 migration blocks above.
- `config.biz_tz` + `NPA_TO_IANA` (lazy `import db`; dict-or-int; zoneinfo; never raises).
- `config.sms_status_callback_url()`.
- `messaging.send_sms`: when `status_callback is None`, auto-inject `sms_status_callback_url() or None`
  (one block, ~line 147 before the data dict). The ONLY messaging.py change.
- All shared `db.*` functions in the seam table owned by A1 (set_business_timezone, set_google_event_id,
  set_forwarding_sentinel, set_forwarding_probe, queue_sms_retry, get_message_by_provider_sid,
  find_scheduled_message).
- `db.create_business`: default `buffer_minutes=60` for new businesses only (per [DECIDED]).
- `alerts`: add `"sms_fail"` and `"forwarding_lost"` message kinds to the existing dispatch (sms + email, gate=False owner path).
**Writes tests:** `test_sf5_timezone.py` (biz_tz: column hit, NPA fallback, bad-IANA fallback, global fallback,
DST sanity via a real ZoneInfo), `test_sf4_db.py` (queue_sms_retry, get_message_by_provider_sid,
find_scheduled_message, sms_status_callback_url present/empty, status_callback auto-injected in a mocked send).
**Re-runs:** test_migration.py, test_scheduling.py, test_config_hub.py, test_connect_hub.py, test_alert_channel.py, test_compliance.py, test_compliance_core.py.

### AGENT 2 — Calendar + Sentinel (google_cal + connections + app.py forwarding/voice/cancel routes)
**Owns exclusively:** `google_cal.py`, `connections.py`, and app.py edits ONLY at:
`/setup/forwarding` (1165), a NEW sentinel-TwiML route, `twilio_voice_inbound` (1961), the cancel-appointment route (~1495).
**Work:**
- **SF-5 (google side):** in `connect_with_code`, after token exchange, read `/calendars/primary` → `timeZone`,
  validate via `ZoneInfo(...)`, persist via `db.set_business_timezone`. Fail-open. `_slot_dt(…, tz=None)` +
  `create_event(…, tz=None)` thread tz through; internal callers pass `biz_tz(business_id)`.
- **SF-7 sentinel:** `connections.send_sentinel_call(business_id)` places an outbound call via `messaging.place_call`
  to the owner's `forward_to`, stores the SID via `db.set_forwarding_sentinel`. New app route
  `/webhooks/twilio/voice/sentinel-twiml` (POST, `@require_twilio_signature`) returns a brief Say+Hangup TwiML.
  In `twilio_voice_inbound`: if inbound `CallSid == biz["forwarding_sentinel_sid"]` → `db.set_forwarding_confirmed(True)`
  + clear sentinel + `db.set_forwarding_probe` + Hangup TwiML (don't process as a real call). `/setup/forwarding`:
  call `send_sentinel_call`; on "placed" leave confirmed=0 (UI shows Verifying); honor the [DECIDED] honesty rule.
  `connections.check_forwarding_health()`: for each forwarding_confirmed biz whose `forwarding_last_probe_at` is
  null/>7d, place a sentinel + record probe; if a prior probe never confirmed within timeout, flip confirmed=0 +
  fire `forwarding_lost` alert.
- **F04:** `create_event_and_store` + `create_event_async` (new signature w/ appointment_id+tz; store id via
  `db.set_google_event_id`); `cancel_event` + `cancel_event_async`; all-day-event parse fix in `_slots_conflicting`
  (handle `{"date":…}` as a full local day; keep timed-event path working — regression test mandatory).
  Wire the cancel route (~1495) to `cancel_event_async` when `google_event_id` is set.
**Writes tests:** `test_f04_google.py` (all-day parse + timed regression, create_event_and_store stores id,
cancel_event idempotent on 410), `test_sf7_sentinel.py` (sentinel stored on place; inbound match confirms;
non-match does NOT; probe fires at >7d, not at <7d; **confirmed is never set on "placed"**).
**Re-runs:** test_scheduling.py, test_webhooks.py, test_google_oauth.py, test_setup.py, test_callback.py.

### AGENT 3 — Brain + Reminders + Reliability (ai + reminders + app.py booking/status/error)
**Owns exclusively:** `ai.py`, `reminders.py`, `templates/errors/`, and app.py edits ONLY at:
`open_conversation` (1291), `handle_inbound` (1306–~1357), `twilio_sms_status` (2084), and the error handlers (top/bottom).
**Work:**
- **F03 (ai.py):** turn cap (count `direction=="in"`, cap 12, handoff reply incl. `business.get("phone")`);
  post-reply price guard — PRECISE regex anchored to `$`/explicit "dollars|bucks" (NOT bare numbers); MUST NOT
  scrub "estimate is free" or "3 rooms" (false-positive tests required); scrub → "[we'll provide a quote at the
  estimate]". Post-reply length guard (cap ~480 chars, trim at sentence boundary). Apply all in `generate_reply`
  (signature stays `generate_reply(business, history, exclude_slot_ids=None, lead_id=None)`).
- **F03 double-booking recovery (handle_inbound):** when `db.book_appointment` returns False, the slot was taken
  between turns — generate ONE recovery reply offering the next open slot (one extra LLM call on this rare race
  is acceptable); replace the outbound reply.
- **SF-4 retry (twilio_sms_status @2084 + reminders.run_due_once):** on `MessageStatus` failed/undelivered →
  look up the original via `db.get_message_by_provider_sid`, compute backoff for attempt N (30s/2m/10m), if
  attempt≤3 `db.queue_sms_retry`, else fire `sms_fail` owner alert. `run_due_once` processes `sms_retry` rows
  like normal sends; if a send errors synchronously, route through the SAME async re-enqueue (never sync-retry).
- **SF-5 (reminders threading):** replace `app_tz()` with `biz_tz(business)`/`biz_tz(biz["id"])` in
  `enqueue_reminder` (149), `_appt_passed` (refactor to accept business/id), `scan_followups` (218). `compute_send_at`
  stays pure (takes a tz arg).
- **F04 first-turn unify (open_conversation @1291) + call-site updates:** mirror handle_inbound's post-booking
  hooks (find appointment → `google_cal.create_event_async(biz_id, appt_id, …, tz=biz_tz(biz))` →
  `reminders.enqueue_reminder`). Update the existing create_event_async call site at 1349 to the new signature.
- **F05 (reminders.py):** write `test_reminders.py` (the docstring's "unit-tested" claim is currently FALSE) covering
  when_phrase/reminder_body/followup_body/next_send_time/compute_send_at/due_followup_leads + the new functions;
  `enqueue_morning_reminder` (8 AM local on estimate day; skip if estimate <10:00, skip if morning already past,
  dedupe via `db.find_scheduled_message`); `run_due_once` handles kind `"morning_reminder"` like `"reminder"`;
  `classify_rsvp` + wire into handle_inbound per [DECIDED].
- **Reliability:** `@app.errorhandler(404/500)` (JSON for /api & /webhooks paths, HTML otherwise); 500 prints via
  the existing stderr convention; create `templates/errors/404.html` + `500.html` (on-brand, no smart quotes).
- **One-line seam:** add `connections.check_forwarding_health()` call inside `reminders.tick_once` (A2 defines the function).
**Writes tests:** `test_reminders.py` (the lie-fix; ~12 cases incl. classify_rsvp + morning-of guards),
`test_f03_brain.py` (turn cap fires at 12 / not at 11; price guard strips $ and word-prices; NO false positive on
"free"/"3 rooms"; length trim), `test_reliability.py` (404 HTML, 404 JSON on /api, 500 path).
**Re-runs:** test_screening.py, test_scheduling.py, test_assistant.py, test_streaming.py, test_chaperone.py, test_callback.py.

---

## app.py LINE-RANGE OWNERSHIP (both A2 & A3 edit app.py — disjoint regions, Opus merges)
- **A2:** 1165 (/setup/forwarding) · NEW sentinel-twiml route · ~1495 (cancel route) · 1961 (twilio_voice_inbound)
- **A3:** 1291 (open_conversation) · 1306–~1357 (handle_inbound) · 2084 (twilio_sms_status) · error handlers (top+bottom)
- No region is shared. handle_inbound's create_event call site (1349) belongs to A3 (it owns 1306–1357); A2 only
  changes the function definition in google_cal.py.

## MERGE ORDER (Opus): **A1 first** (migrations + seams must exist), then A2 and A3 (either order). Full suite green after each.

## RISK LANES (from Auditor B — verify these at review)
1. **SF-7:** confirming on "placed" instead of inbound receipt = the same lie we're killing. Grep that
   `set_forwarding_confirmed(…, True)` appears in app.py ONLY inside `twilio_voice_inbound` (+ the labelled no-Twilio fallback).
2. **SF-4:** any synchronous retry loop = reject. All retries are new delayed `scheduled_messages` rows.
3. **SF-5:** static utcoffset / pytz = reject. Must be `ZoneInfo`; DST test mandatory.
4. **F04:** cancel path must delete the Google event (no ghost events); all-day fix must not regress timed events.
5. **F03:** price-guard false positives ("free", "3 rooms") — precise currency-anchored regex + negative tests.
6. **F05:** test_reminders.py must run via `.venv/bin/python` with NO pytest import; morning-of must not fire before the 24h reminder on near-term bookings.
7. **Reliability:** smart quotes in the Jinja error templates = broken render. Hit a 404 route in the test to prove it renders.

## OWNER-OPS (append to SETUP_NEEDED.md at review)
- SF-4: `FIRSTBACK_PUBLIC_URL` must be set in Render for the status callback to auto-wire.
- SF-5: owner saves timezone in Settings; Google connect auto-reads it.
- SF-7: owner still dials the carrier star code once; the sentinel now VERIFIES it (phone must ring within ~30s of tapping Verify).
- F04: Google Calendar must be connected for live event create/cancel.
```

