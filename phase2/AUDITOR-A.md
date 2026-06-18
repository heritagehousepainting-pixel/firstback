# Phase 2 — Auditor A Implementation Spec
**Date:** 2026-06-18  
**Repo:** ~/apps/firstback (Flask + SQLite, branch staging, 28/28 tests green)  
**Scope:** SF-4, SF-5, SF-7, F04, F03, F05, Reliability  
**Model:** This spec is build-ready and collision-free. Read SHARED SEAMS before partitioning agents.

---

## HOW TO READ THIS SPEC

Each item lists:
- **Files / line anchors** — exact file + approx line for every change site
- **DB migrations** — using the repo's guarded `if col not in cols` pattern (see db.py:390+)
- **New functions / signatures** — canonical form, defined once in SHARED SEAMS
- **Acceptance tests** — what the test must prove
- **Classification** — CODE (we build it) or OWNER-OPS (owner does it post-deploy)

---

## 1. SF-4 — SMS Delivery Receipts + Retry/Backoff + Owner-Alert-on-Fail

### What exists today
- `messages.provider_sid` TEXT and `messages.delivery_status` TEXT columns: **exist** (db.py:519)
- `db.set_message_delivery(provider_sid, status)`: **exists** (db.py:1126) — updates correctly
- `/webhooks/twilio/sms/status` route in app.py:2082 — **exists and calls `set_message_delivery`**
- `send_sms` in messaging.py:68 has a `status_callback=None` param — **exists but NO call site passes it**
- Retry logic: **does not exist anywhere**
- Owner-alert on send failure: **does not exist**
- `delivery_status` surface in thread UI: **not checked**

### Gap: status_callback is never wired
Every `messaging.send_sms(...)` call in the codebase passes NO `status_callback`. Twilio therefore never POSTs to `/webhooks/twilio/sms/status`, so `delivery_status` is always NULL in every live row.

Call sites that need `status_callback` wired:
1. `app.py:1955` — `messaging.send_sms(biz, caller, reply)` (first text-back after a missed call)
2. `app.py:2078` — `messaging.send_sms(biz, caller, reply)` (Twilio SMS inbound webhook reply)
3. `app.py:1505-1508` — cancellation text (send_sms in cancel_appointment route)
4. `app.py:1572` — `messaging.send_sms(biz, caller, reply)` (engage screened call)
5. `reminders.py:194` — `messaging.send_sms(biz, phone, row["body"], lead_id=row["lead_id"])`
6. `alerts.py:117` — `messaging.send_sms(business, sms_to, body, gate=False)` (owner alerts)

### Gap: no retry logic
When Twilio returns `"failed"` or `"undelivered"` status to the webhook, nothing retries. The row stays failed and the owner never knows.

### Approach — three-layer solution

**Layer 1 — Wire status_callback on every real send (messaging.py)**  
Add a helper `_status_callback_url(base_url=None)` that returns `PUBLIC_BASE_URL + "/webhooks/twilio/sms/status"` or `None` when the base URL is unset. Modify `send_sms` to auto-inject it when not explicitly passed:
```python
# messaging.py — inside send_sms, just before the Twilio POST:
if not status_callback and PUBLIC_BASE_URL:
    status_callback = PUBLIC_BASE_URL.rstrip("/") + "/webhooks/twilio/sms/status"
```
This is a one-line addition inside the existing `data` dict build (messaging.py:147-149). No call site changes needed — the default `status_callback=None` param becomes self-wiring when `PUBLIC_BASE_URL` is set.

**Layer 2 — Retry table + retry queue (db.py + reminders.py)**  
Add retry tracking to `scheduled_messages`:
```sql
-- NEW columns on scheduled_messages (guarded migration in db.py init_db):
retry_count  INTEGER DEFAULT 0
retry_at     TEXT    -- UTC ISO; NULL = not scheduled for retry
```
Migration guard (db.py, after existing scheduled_messages migrations ~line 574):
```python
sched_cols = [r[1] for r in c.execute("PRAGMA table_info(scheduled_messages)").fetchall()]
for col, ddl in (("retry_count", "INTEGER DEFAULT 0"), ("retry_at", "TEXT")):
    if col not in sched_cols:
        c.execute(f"ALTER TABLE scheduled_messages ADD COLUMN {col} {ddl}")
```

Add `db.schedule_retry(sched_id, retry_at_iso, retry_count)` — sets `status='pending'`, `retry_count`, `retry_at`, `send_at=retry_at_iso` (so `due_scheduled_messages` picks it up normally).

In `/webhooks/twilio/sms/status` (app.py:2082), when `MessageStatus` is `"failed"` or `"undelivered"`:
- Look up the `scheduled_messages` row by `provider_sid` (new db function: `db.get_sched_by_provider_sid(sid)`)
- If `retry_count < 3`, compute the next backoff: 30s, 2m, 10m (index 0,1,2)
- Call `db.schedule_retry(row["id"], retry_at, row["retry_count"] + 1)`
- If `retry_count >= 3`, fire owner alert via `alerts.notify_async(biz, "sms_fail", {...})`

**New function signatures (Layer 2):**
```python
# db.py
def get_sched_by_provider_sid(provider_sid: str) -> dict | None: ...
def schedule_retry(sched_id: int, retry_at_iso: str, retry_count: int) -> None: ...

# alerts.py — new alert kind "sms_fail"
# Extend the existing dispatch in alerts.py notify() to handle kind="sms_fail":
#   body = f"A text to {phone} failed after 3 attempts. Check your Twilio number."
#   send to owner via email + SMS (existing gate=False path)
```

**Layer 3 — Surface delivery status in thread UI (templates, not in scope for an agent, deferred to OWNER-OPS or future)**  
The `delivery_status` column is already returned by `db.get_messages`; the template merely needs to render it. This is a UI-only change — lowest risk, assign to Agent 3 (reliability) as a small add.

### DB columns needed
- `scheduled_messages.retry_count INTEGER DEFAULT 0` (guarded migration)
- `scheduled_messages.retry_at TEXT` (guarded migration)

### Acceptance tests (test_sf4_delivery.py — new file)
1. `test_status_callback_auto_wired` — call `send_sms` with `PUBLIC_BASE_URL` set; assert `StatusCallback` in the mock POST data
2. `test_retry_schedule_on_failed` — simulate a `failed` status webhook; assert a new `pending` row with `retry_count=1` and `send_at` ~30s out
3. `test_retry_schedule_on_undelivered` — same with `undelivered` status
4. `test_no_retry_after_third` — simulate `retry_count=3` already; assert no new retry row, assert owner alert fired
5. `test_no_retry_on_delivered` — `delivered` status fires no retry
6. `test_get_sched_by_provider_sid` — unit test the new db function

### Classification
- Wiring `status_callback` auto-inject: **CODE** (messaging.py, ~2 lines)
- Retry table migration + new db functions: **CODE** (db.py)
- Retry dispatch in status webhook: **CODE** (app.py)
- `alerts.py` new `sms_fail` kind: **CODE** (alerts.py)
- Twilio status URL configured: **OWNER-OPS** (Render env: `FIRSTBACK_PUBLIC_URL` must be set — already in SETUP_NEEDED.md)

---

## 2. SF-5 — Per-Business Timezone

### What exists today
- `businesses.timezone` TEXT column: **exists** (db.py:492-516 migration block)
- `app_tz()` in config.py:248: reads only `FIRSTBACK_TZ` env var — **ignores the column**
- `messaging.py:114-121`: ALREADY reads `business["timezone"]` for the quiet-hours backstop — **partial implementation exists**
- `reminders.py:149`: calls `app_tz()` directly, ignoring business timezone — **bug**
- `reminders.py:164`: same
- `reminders.py:218-220`: same
- `google_cal.py:122`: `_slot_dt` calls `.astimezone()` with no tz arg — uses server local tz — **bug**
- `db.py:1190`: `_today()` calls `datetime.now(app_tz()).date()` — app-wide tz — **bug for multi-tenant**
- `db.py:2348`: `app_tz()` in ROI stats — **bug**
- `app.py:130,140`: `fmt_time`/`fmt_date` use `app_tz()` — **minor: display only, not scheduling**
- `app.py:644`: dashboard greeting uses `app_tz()` — display only

### NPA area-code → IANA fallback
A small lookup table. No external API needed. Include the top 50 US area codes → IANA zone. Store in `config.py` as a dict constant `NPA_TO_IANA`. Phone-based lookup from the `businesses.phone` or `businesses.twilio_number` field.

### Approach

**Step 1 — Add `biz_tz(business_id_or_dict)` to config.py**  
Canonical location: `config.py`, defined after `app_tz()` (line ~258).
```python
def biz_tz(business):
    """Per-business timezone: reads businesses.timezone (IANA string), falls back
    to NPA area-code lookup from the business's phone, then app_tz() global default.
    Accepts a business dict (preferred) or a business_id int (does a DB read)."""
    from zoneinfo import ZoneInfo
    if isinstance(business, int):
        import db as _db
        business = _db.get_business(business) or {}
    tz_name = (business or {}).get("timezone", "")
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    # NPA fallback: extract area code from twilio_number or phone
    phone = (business or {}).get("twilio_number") or (business or {}).get("phone") or ""
    digits = re.sub(r"\D", "", phone)
    npa = digits[1:4] if len(digits) == 11 and digits.startswith("1") else digits[:3]
    if npa in NPA_TO_IANA:
        try:
            return ZoneInfo(NPA_TO_IANA[npa])
        except Exception:
            pass
    return app_tz()
```

**Step 2 — Read primary calendar timezone on Google connect (google_cal.py)**  
In `connect_with_code` (google_cal.py:61), after exchanging tokens, fetch `/calendars/primary` and extract `timeZone`:
```python
# google_cal.py connect_with_code, after db.set_google_tokens(...):
try:
    cal_r = requests.get(f"{API_BASE}/calendars/primary",
                         headers={"Authorization": f"Bearer {tok['access_token']}"},
                         timeout=10)
    if cal_r.ok:
        tz_name = cal_r.json().get("timeZone", "")
        if tz_name:
            db.set_business_timezone(business_id, tz_name)
except Exception:
    pass  # fail-open: timezone stays as-is
```

New db function: `db.set_business_timezone(business_id, tz_name)` — single UPDATE on `businesses.timezone`.

**Step 3 — Thread biz_tz through reminders.py**  
`enqueue_reminder` (reminders.py:136) already has `business` as a param. Replace:
```python
# reminders.py:149  — OLD:
tz = app_tz()
# NEW:
from config import biz_tz
tz = biz_tz(business)
```
Same for `_appt_passed` (reminders.py:163-168) — it calls `app_tz()` and has no business context. Change signature to `_appt_passed(day_iso, slot_time, tz)` and pass `biz_tz(business)` from `run_due_once` where the `biz` dict is already loaded (reminders.py:192).

`scan_followups` (reminders.py:212): already iterates `biz` dicts from `db.list_businesses()`. Replace `app_tz()` at reminders.py:218-220 with `biz_tz(biz)`.

**Step 4 — Thread biz_tz through google_cal._slot_dt**  
`_slot_dt` (google_cal.py:118-122) currently calls `.astimezone()` with no arg. Add a `tz` param:
```python
def _slot_dt(day_iso, time_key_str, tz=None):
    from zoneinfo import ZoneInfo
    from config import app_tz
    tz = tz or app_tz()
    y, m, d = (int(x) for x in day_iso.split("-"))
    hh, mm = (int(x) for x in time_key_str.split(":"))
    return datetime(y, m, d, hh, mm, tzinfo=tz)
```
All internal callers of `_slot_dt` in google_cal.py (lines 135, 185) pass `tz` from `biz_tz(business_id)`.

**Step 5 — db._today() stays app-global (acceptable)**  
`db._today()` (db.py:1190) is used in `upcoming_slots` for the booking horizon window — a wall-clock "what is today" that does not need per-business precision for a slot-availability query. Leave it app-global for now (the difference is a few hours at timezone boundaries, safe for this use).

**Step 6 — Leave app.py fmt_time/fmt_date as app-global for Phase 2**  
These are display-only formatters for the dashboard (single-tenant UI for this phase). Making them per-business requires thread-context plumbing not worth the risk. Defer to Phase 5 (multi-tenant Crew).

### DB columns needed
- `businesses.timezone` TEXT: **already exists** — no migration needed
- New function: `db.set_business_timezone(business_id, tz_name)` — CODE only

### NPA_TO_IANA table (config.py addition)
Include the ~50 most common US NPAs covering Eastern, Central, Mountain, Pacific zones. Example subset:
```python
NPA_TO_IANA = {
    "212": "America/New_York", "646": "America/New_York", "718": "America/New_York",
    "213": "America/Los_Angeles", "310": "America/Los_Angeles", "323": "America/Los_Angeles",
    "312": "America/Chicago", "773": "America/Chicago", "303": "America/Denver",
    # ... ~50 entries total
}
```

### Acceptance tests (test_sf5_timezone.py — new file)
1. `test_biz_tz_reads_column` — business dict with `timezone="America/Denver"` returns Denver tz
2. `test_biz_tz_npa_fallback` — business with no timezone but `phone="(312) 555-1234"` returns Chicago tz
3. `test_biz_tz_global_fallback` — business with no timezone and unknown NPA returns `app_tz()`
4. `test_biz_tz_invalid_iana_fallback` — `timezone="not/real"` falls back to NPA/app_tz
5. `test_slot_dt_with_tz` — `_slot_dt("2026-07-01", "09:00", ZoneInfo("America/New_York"))` produces a UTC-offset-aware datetime
6. `test_enqueue_reminder_uses_biz_tz` — mock `biz_tz` to return a known tz; assert `compute_send_at` is called with it
7. `test_google_connect_sets_tz` — mock the `/calendars/primary` response; assert `db.set_business_timezone` is called

### Classification
- `biz_tz` function + NPA table: **CODE** (config.py)
- `db.set_business_timezone`: **CODE** (db.py)
- Google connect timezone read: **CODE** (google_cal.py)
- Reminders threading: **CODE** (reminders.py)
- `_slot_dt` tz param: **CODE** (google_cal.py)
- Setting `businesses.timezone` via Settings UI: **OWNER-OPS** (already exposed via the settings form; the column exists; owner saves their timezone once)

---

## 3. SF-7 — Forwarding Sentinel-Call Verification + Weekly Health Probe

### What exists today
- `businesses.forwarding_confirmed` INTEGER DEFAULT 0: **exists** (db.py:506)
- `db.set_forwarding_confirmed(business_id, confirmed)`: **exists** (db.py:935)
- `app.py:1184` — `db.set_forwarding_confirmed(biz["id"], True)` fires on button click with ZERO proof
- No outbound sentinel call logic anywhere
- No weekly health probe

### What we need to build

**Phase A — Replace button-click self-attestation with real sentinel flow**

The current route at app.py:1165-1185 (POST `/setup/forwarding`) calls `set_forwarding_confirmed(True)` on a button press. We replace this with a 3-step flow:

1. **Initiate sentinel** (POST `/setup/forwarding/test`) — place an outbound call via `messaging.place_call` to the business's `forward_to` number using a TwiML endpoint that plays a DTMF tone + hangs up. Store a `sentinel_call_sid` on the business. Set `forwarding_confirmed=0`.

2. **Inbound sentinel webhook** (POST `/webhooks/twilio/voice/sentinel`) — Twilio calls this when the call connects to the forwarded number (the owner's real cell). Verify the `CallSid` matches the stored `sentinel_call_sid`. If it matches → call `db.set_forwarding_confirmed(biz_id, True)` + clear the sentinel SID.

3. **Poll/confirm UI** — The setup page polls GET `/api/forwarding/status` to show "Verifying..." → "Confirmed" without a page reload.

**New DB columns (guarded migrations in db.py):**
```sql
sentinel_call_sid     TEXT    -- the outbound call SID we are waiting to see inbound
sentinel_initiated_at TEXT    -- UTC ISO when we placed the sentinel; for timeout (5 min)
forwarding_probe_at   TEXT    -- UTC ISO of the last weekly health probe
```
Migration guard (db.py init_db, after existing biz migration block ~line 516):
```python
biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
for col, ddl in (("sentinel_call_sid", "TEXT"),
                 ("sentinel_initiated_at", "TEXT"),
                 ("forwarding_probe_at", "TEXT")):
    if col not in biz_cols:
        c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")
```

**New TwiML endpoint (app.py)**  
`GET /webhooks/twilio/voice/sentinel-twiml` — returns TwiML that plays a short beep and hangs up. No auth (Twilio webhooks are verified by signature). No body needed; the call reaching the owner's phone IS the proof.
```xml
<Response><Say>Forwarding verified. You can hang up now.</Say><Hangup/></Response>
```

**New app.py routes:**
```python
POST /setup/forwarding/test         # initiate sentinel
POST /webhooks/twilio/voice/sentinel # inbound confirmation webhook
GET  /api/forwarding/status         # poll for UI
```

**New db functions:**
```python
def set_sentinel_call(business_id, call_sid, initiated_at): ...
def clear_sentinel_call(business_id): ...
def get_business_by_sentinel_sid(call_sid): ...
def set_forwarding_probe_at(business_id, ts_iso): ...
```

**Phase B — Weekly health probe**  
Add a new kind `"forwarding_probe"` to `reminders.tick_once` (reminders.py:238). Each tick_once checks: for each business with `forwarding_confirmed=1`, if `forwarding_probe_at` is NULL or older than 7 days, schedule a sentinel call. On success, update `forwarding_probe_at`. On failure (no inbound within 5 min timeout), flip `forwarding_confirmed=0` and fire an owner alert (kind `"forwarding_lost"`).

The probe call uses the same sentinel flow (Phase A). The 5-minute timeout is checked in tick_once: if `sentinel_initiated_at` is >5 min ago and `forwarding_confirmed=0`, the probe failed.

**New alert kind in alerts.py:**
```python
# alerts.py: add kind "forwarding_lost" to the dispatch dict:
"forwarding_lost": {
    "sms": "FirstBack alert: your call forwarding may be down. Tap here to recheck: {setup_url}",
    "email_subject": "Action needed: FirstBack call forwarding may be down",
}
```

### DB columns needed
- `businesses.sentinel_call_sid TEXT` (guarded migration)
- `businesses.sentinel_initiated_at TEXT` (guarded migration)
- `businesses.forwarding_probe_at TEXT` (guarded migration)

### Acceptance tests (test_sf7_sentinel.py — new file)
1. `test_initiate_sentinel_places_call` — POST `/setup/forwarding/test`; assert `place_call` was called with the sentinel TwiML URL; assert `sentinel_call_sid` stored
2. `test_sentinel_webhook_confirms` — POST `/webhooks/twilio/voice/sentinel` with matching `CallSid`; assert `forwarding_confirmed=1`, sentinel SID cleared
3. `test_sentinel_webhook_wrong_sid` — mismatched CallSid; assert no confirmation
4. `test_forwarding_status_poll` — GET `/api/forwarding/status`; assert JSON `{"confirmed": false/true}`
5. `test_weekly_probe_schedules` — `tick_once` with a business that has `forwarding_probe_at` >7 days ago; assert a sentinel call was placed
6. `test_probe_timeout_alerts` — business with `sentinel_initiated_at` >5 min ago and `forwarding_confirmed=0`; assert `forwarding_confirmed` flipped false and owner alert fired

### Classification
- Sentinel call initiation + confirmation webhook: **CODE** (app.py, db.py)
- TwiML endpoint: **CODE** (app.py)
- Weekly probe in tick_once: **CODE** (reminders.py)
- New alert kinds: **CODE** (alerts.py)
- Owner must physically confirm forwarding once: **OWNER-OPS** (unchanged; the sentinel verifies it)

---

## 4. F04 — Google Write-Loop Close

### What exists today
- `google_cal.create_event(business_id, summary, description, day_iso, time_key_str)` returns an event ID (google_cal.py:177) — but **nobody stores the returned ID**
- `google_cal.create_event_async` (google_cal.py:202) fires the thread but discards the return value
- No `update_event` or `delete_event` functions exist
- No `google_event_id` column on `appointments`
- All-day-event parse in `_slots_conflicting` (google_cal.py:153-173): `datetime.fromisoformat(iv["start"].replace("Z", "+00:00"))` — will fail on all-day events whose `start` is `{"date": "2026-06-20"}` (no `"dateTime"` key) — **silent exception swallowed at line 163 except block**
- `DEFAULT_BUFFER_MINUTES = 0` (config.py:309) — the buffer default is 0, not 60 as required
- First-turn booking: `open_conversation` (app.py:1291) calls `ai.generate_reply(biz, [], ...)` then if booking → `db.book_appointment` but does NOT call `google_cal.create_event_async` — **first-turn bookings never hit Google Calendar**

### Changes

**4a — Persist google_event_id on appointments**  
New column:
```sql
-- appointments table, guarded migration in db.py init_db:
google_event_id  TEXT
```
Modify `book_appointment` signature (db.py:1148) to accept an optional `google_event_id=None`:
```python
def book_appointment(business_id, lead_id, scheduled_for, notes="", day=None,
                     slot_time=None, google_event_id=None):
```
And include it in the INSERT. Update `handle_inbound` (app.py:1331) after `db.book_appointment(...)` succeeds to capture the event id from a new `create_event_sync_id` helper.

Better: change `create_event_async` to return the event id via a callback:
```python
# google_cal.py — replace create_event_async:
def create_event_and_store(business_id, appt_id, summary, description, day_iso, time_key_str):
    """Fire-and-forget event creation that stores the returned event ID on the appointment."""
    def _run():
        event_id = create_event(business_id, summary, description, day_iso, time_key_str)
        if event_id and appt_id:
            db.set_appointment_google_event(appt_id, event_id)
    import threading
    threading.Thread(target=_run, daemon=True).start()
```
New db function: `db.set_appointment_google_event(appt_id, event_id)`.

**4b — cancel/patch event on appointment cancel**  
New google_cal functions:
```python
def delete_event(business_id, event_id): ...  # DELETE /calendars/{cal_id}/events/{event_id}
def update_event(business_id, event_id, summary=None, description=None,
                 day_iso=None, time_key_str=None): ...  # PATCH
```
`cancel_appointment` in db.py (db.py:2274) currently does not touch Google. Wire it in app.py `cancel_appointment_api` (app.py:1493-1509): after `db.cancel_appointment(...)`, fetch the `google_event_id` from the returned appt dict and call `google_cal.delete_event(...)` async.

**4c — All-day event parse fix**  
In `_slots_conflicting` (google_cal.py:153-173), handle both `dateTime` and `date` keys:
```python
# google_cal.py:158-163 — replace:
try:
    start_raw = iv.get("start") or {}
    end_raw = iv.get("end") or {}
    # All-day events have "date" key, not "dateTime"
    if "dateTime" in start_raw:
        bs = datetime.fromisoformat(start_raw["dateTime"].replace("Z", "+00:00"))
        be = datetime.fromisoformat(end_raw["dateTime"].replace("Z", "+00:00"))
    elif "date" in start_raw:
        from datetime import date as _date
        bs = datetime.fromisoformat(start_raw["date"]).replace(tzinfo=timezone.utc)
        be = datetime.fromisoformat(end_raw["date"]).replace(tzinfo=timezone.utc) + timedelta(days=1)
    else:
        continue
    busy.append((bs, be))
except (ValueError, KeyError, AttributeError):
    continue
```

**4d — Buffer default 60**  
`config.py:309`: `DEFAULT_BUFFER_MINUTES = 0` → change to `DEFAULT_BUFFER_MINUTES = 60`.  
**Note:** This will affect existing tenants who have `buffer_minutes=NULL` in the DB (they inherit the config default). The migration guard at db.py:572 only adds the column; it does NOT set a default value for existing rows. Existing rows with `buffer_minutes=0` will NOT be updated. This is safe: 0 is an explicit setting; only NULL rows will pick up the new default via `scheduling_prefs` fallback at db.py:1326.

**4e — First-turn booking: wire Google Calendar + reminders**  
`open_conversation` (app.py:1291-1303): currently calls `db.book_appointment` but misses `google_cal.create_event_async` and `reminders.enqueue_reminder`. Add them mirroring `handle_inbound` (app.py:1348-1353):
```python
# app.py open_conversation, after db.book_appointment(biz["id"], lead["id"], booking):
if booking:
    appt = db.find_appointment(biz["id"], lead["id"], db.parse_day(booking), db.time_key(booking))
    gday, gtime = db.parse_day(booking), db.time_key(booking)
    if gday and gtime:
        google_cal.create_event_and_store(
            biz["id"], appt["id"] if appt else None,
            f"Estimate: {lead['name']}",
            f"FirstBack booked a free estimate for {lead['name']} ({lead['phone']}).",
            gday, gtime)
        reminders.enqueue_reminder(biz, lead, gday, gtime)
```

### DB columns needed
- `appointments.google_event_id TEXT` (guarded migration in db.py init_db)
- New db functions: `set_appointment_google_event`, `get_appointment_google_event`

### Acceptance tests (extend existing test_scheduling.py + new test_f04_google.py)
1. `test_create_event_stores_id` — `create_event_and_store` mock; assert `db.set_appointment_google_event` called
2. `test_cancel_appointment_deletes_event` — appointment with `google_event_id`; cancel it; assert `delete_event` called
3. `test_all_day_event_blocked` — `_slots_conflicting` with an all-day `{"start": {"date": "2026-07-04"}}` interval; assert the whole day is treated as busy
4. `test_first_turn_booking_creates_event` — `open_conversation` with a slot-booking reply; assert `create_event_and_store` called
5. `test_buffer_default_60` — `scheduling_prefs` for a business with NULL `buffer_minutes`; assert `60` returned

### Classification
- google_event_id persistence: **CODE** (db.py, google_cal.py, app.py)
- cancel/patch: **CODE** (google_cal.py, app.py)
- All-day fix: **CODE** (google_cal.py)
- Buffer default: **CODE** (config.py, db.py scheduling_prefs)
- First-turn wire-up: **CODE** (app.py)

---

## 5. F03 — Booking-Brain Guards

### What exists today
- `ai.generate_reply` (ai.py:421) has NO turn cap
- System prompt (ai.py:74-108) says "Never quote prices" but there is NO post-reply guard — the model can still hallucinate a price range in its reply
- No double-booking recovery path beyond the DB constraint (which just returns `False` to `book_appointment`)
- No length guard on the outbound reply

### What we need to build

**5a — Turn cap (12 max outbound turns)**  
Add guard at the top of `generate_reply` (ai.py:421):
```python
MAX_BOOKING_TURNS = 12
outbound_turns = sum(1 for m in history if m["direction"] == "out")
if outbound_turns >= MAX_BOOKING_TURNS:
    return (
        "It looks like we've been chatting for a while! You can reach us directly "
        "at our number to finalize your free estimate. We look forward to hearing "
        "from you.", None)
```
The constant `MAX_BOOKING_TURNS = 12` lives at module level in ai.py.

**5b — Post-reply price-quote guard**  
After the model returns `raw` (ai.py, before `_clean_punct`), scan for price patterns:
```python
_PRICE_RE = re.compile(r"\$\s*\d[\d,]*|\b\d+\s*(?:hundred|thousand|dollars|bucks)\b", re.I)

def _has_price_quote(text):
    return bool(_PRICE_RE.search(text or ""))
```
In `generate_reply`, after getting `raw` from the model (ai.py ~line 450):
```python
if _has_price_quote(raw):
    import sys
    print(f"[firstback] price-quote guard: stripped price from reply", file=sys.stderr, flush=True)
    raw = _PRICE_RE.sub("[estimate provided at visit]", raw)
```

**5c — Post-reply length guard**  
Cap the visible reply at 320 characters (the SMS segment boundary):
```python
_SMS_MAX_CHARS = 320
if len(raw) > _SMS_MAX_CHARS:
    # Hard truncation at a sentence boundary to stay coherent
    truncated = raw[:_SMS_MAX_CHARS]
    last_period = truncated.rfind(".")
    raw = truncated[:last_period + 1] if last_period > 100 else truncated[:_SMS_MAX_CHARS]
```

**5d — Double-booking recovery reply**  
When `db.book_appointment` returns `False` in `handle_inbound` (app.py:1331), currently `booked` stays `None` and no special message is sent. Add a recovery path:
```python
elif not db.book_appointment(biz["id"], lead_id, booking):
    # Slot was taken between when we offered it and when they replied.
    # Offer the next available window inline without another AI turn.
    import ai as _ai
    fresh_slots = _ai._open_slots(biz["id"], exclude_ids=exclude)
    if fresh_slots:
        nxt = fresh_slots[0]
        recovery_reply = (f"That slot was just taken, sorry! The next available "
                          f"time is {nxt['label']}. Would that work?")
        db.add_message(lead_id, "out", recovery_reply)
        return recovery_reply, None, urgent
```
This is a small addition in `handle_inbound` (app.py:1329-1332).

### Acceptance tests (test_f03_brain.py — new file)
1. `test_turn_cap_fires` — history with 12 outbound turns; assert `generate_reply` returns the cap message and no booking
2. `test_turn_cap_not_fires_at_11` — 11 outbound turns; assert normal reply path
3. `test_price_guard_strips_dollar` — model reply contains "$450-600"; assert stripped
4. `test_price_guard_strips_range` — model reply contains "four hundred dollars"; assert stripped
5. `test_length_guard` — model reply >320 chars; assert output <= 320
6. `test_double_booking_recovery` — `book_appointment` returns False; assert recovery text with next slot offered
7. `test_double_booking_recovery_no_slots` — `book_appointment` returns False AND no slots left; assert graceful message

### Classification
All: **CODE** (ai.py, app.py)

---

## 6. F05 — test_reminders.py + Morning-of Reminder + RSVP Classification

### What exists today
- `reminders.py` module docstring claims "The scheduling math ... is PURE and **unit-tested**"
- `test_reminders.py`: **DOES NOT EXIST** (confirmed: `ls` found no such file)
- Morning-of reminder: the existing `enqueue_reminder` sends ONE reminder at `REMINDER_LEAD_HOURS` (24h) before. No morning-of (day-of) reminder is sent
- RSVP classification: no keyword-based inbound reply classifier in reminders.py

### Changes

**6a — test_reminders.py (the "lying docstring" fix)**  
Create `test_reminders.py` covering all pure functions in reminders.py:
- `when_phrase` — 6 cases (morning, afternoon, missing time, etc.)
- `reminder_body` / `followup_body` — copy includes name and business name
- `next_send_time` — before quiet, during quiet, after quiet, boundary cases
- `compute_send_at` — reminder before estimate, deferred out of quiet, estimate already passed
- `due_followup_leads` — idle cutoff, has_followup flag, no phone, borderline timing

**6b — Morning-of reminder**  
Add a second scheduled message type `"morning_reminder"`. In `enqueue_reminder` (reminders.py:136), after queuing the standard reminder, also queue a morning-of reminder at 8:00 AM on the estimate day:
```python
# After the standard reminder is queued:
morning_send_at = compute_morning_of(day_iso, biz_tz(business), QUIET_START)
if morning_send_at:  # None if the estimate IS today (too late to schedule)
    morning_body = morning_reminder_body(lead.get("name"), business.get("name") or "your contractor",
                                         when_phrase(day_iso, slot_time))
    db.add_scheduled_message(business["id"], lead["id"], appt["id"], "morning_reminder",
                              morning_send_at, morning_body)
```

New pure functions in reminders.py:
```python
def compute_morning_of(day_iso: str, tz, quiet_start: int) -> str | None:
    """UTC ISO time to send a morning-of reminder: quiet_start hour on the estimate day,
    or None if that time is already past."""

def morning_reminder_body(name: str, business_name: str, when: str) -> str:
    return (f"Good morning {_first_name(name)}, just a quick reminder: "
            f"{business_name} is scheduled to come for your free estimate {when}. "
            "See you soon!")
```

Also update `run_due_once` (reminders.py:171) to handle `kind="morning_reminder"` identically to `kind="reminder"` (same cancel/skip logic).

**6c — RSVP keyword classification**  
Add a `classify_rsvp(body: str) -> str` function to reminders.py:
```python
# Returns: "confirm" | "cancel" | "reschedule" | "none"
_RSVP_CONFIRM = re.compile(r"\b(yes|confirm|confirmed|see you|will be there|on my way|coming|ok|okay|sounds good|i'll be there)\b", re.I)
_RSVP_CANCEL  = re.compile(r"\b(cancel|can't make it|cannot make it|won't be there|not coming|reschedule me)\b", re.I)
_RSVP_RESCHED = re.compile(r"\b(reschedule|different time|another time|change it|move it)\b", re.I)

def classify_rsvp(body: str) -> str:
    text = (body or "").lower()
    if _RSVP_CONFIRM.search(text): return "confirm"
    if _RSVP_RESCHED.search(text): return "reschedule"
    if _RSVP_CANCEL.search(text): return "cancel"
    return "none"
```

Wire it in the Twilio SMS inbound webhook (app.py:2020-2079): when an inbound message comes in and the lead has a booked appointment:
```python
# app.py handle_inbound or twilio_sms_inbound, after adding the message:
rsvp = reminders.classify_rsvp(body)
if rsvp == "confirm":
    # no-op for now; future: mark confirmed, stop morning reminder
    pass
elif rsvp in ("cancel", "reschedule"):
    # cancel the appointment, alert owner, let AI handle the rebooking
    # ... existing cancel path ...
    pass
```
For Phase 2 the RSVP classifier is wired but only used to gate a future owner alert. The classification itself is the deliverable; full action handling is Phase 4.

### DB columns needed
- None new. The `morning_reminder` kind reuses `scheduled_messages.kind TEXT` (any string works).

### Acceptance tests (test_reminders.py — new file, covers all of 6a + 6b + 6c)
1. `test_when_phrase_morning` — `when_phrase("2026-07-01", "09:00")` produces readable output
2. `test_when_phrase_afternoon` — `when_phrase("2026-07-01", "14:00")`
3. `test_next_send_time_before_quiet` — 5 AM input → 8 AM output
4. `test_next_send_time_after_quiet` — 10 PM input → next day 8 AM
5. `test_compute_send_at_deferred` — estimate at 9 AM, lead_hours=24, quiet_start=8 → day before at 9 AM (already in window) → 9 AM day before
6. `test_compute_send_at_past` — estimate already passed → returns a time >= estimate (capped logic)
7. `test_due_followup_leads_filters` — rows with/without phones, with/without has_followup, time edge cases
8. `test_compute_morning_of` — estimate on 2026-07-02, tz=UTC → send_at = 2026-07-02T08:00:00+00:00
9. `test_compute_morning_of_past` — estimate today → returns None
10. `test_morning_reminder_body` — contains name and business name
11. `test_classify_rsvp_confirm` — "yes i'll be there" → "confirm"
12. `test_classify_rsvp_cancel` — "sorry i cant make it" → "cancel"
13. `test_classify_rsvp_reschedule` — "can we reschedule" → "reschedule"
14. `test_classify_rsvp_none` — "what color are you painting" → "none"

### Classification
- `test_reminders.py`: **CODE** (new test file)
- `compute_morning_of`, `morning_reminder_body`, `classify_rsvp`: **CODE** (reminders.py)
- Morning-of reminder enqueue: **CODE** (reminders.py)
- run_due_once handle `morning_reminder` kind: **CODE** (reminders.py)
- RSVP wire-up in app.py: **CODE** (app.py — 5-line block)

---

## 7. Reliability — @app.errorhandler + Logging

### What exists today
- No `@app.errorhandler(404)` or `@app.errorhandler(500)` in app.py (confirmed by grep)
- Logging: `print(f"[firstback] ...", file=sys.stderr, flush=True)` used throughout — functional but not structured
- No JSON 404/500 for API routes vs. HTML for page routes

### Changes

**7a — Global error handlers (app.py)**  
Add after the `app` object is created (app.py ~line 54):
```python
@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/") or request.path.startswith("/webhooks/"):
        return jsonify(error="Not found.", code=404), 404
    return render_template("errors/404.html"), 404

@app.errorhandler(500)
def server_error(e):
    import sys
    print(f"[firstback] 500: {e}", file=sys.stderr, flush=True)
    if request.path.startswith("/api/") or request.path.startswith("/webhooks/"):
        return jsonify(error="Internal server error.", code=500), 500
    return render_template("errors/500.html"), 500
```

Create `templates/errors/404.html` and `templates/errors/500.html` — minimal, brand-consistent pages with a "Return to dashboard" link.

**7b — Structured logging (optional, Phase 2 is print-based — this is low-risk)**  
The existing `[firstback] ...` print-to-stderr convention is sufficient for Render logs. Do NOT introduce Python `logging` module or change the convention (risk of breaking existing behavior). Instead, standardize the prefix format in new code to always include `[firstback]` + a context tag.

### Acceptance tests (extend existing test files or new test_reliability.py)
1. `test_404_api_returns_json` — GET `/api/nonexistent`; assert status 404 and JSON `{"error": ...}`
2. `test_404_page_returns_html` — GET `/nonexistent-page`; assert status 404 and HTML
3. `test_500_api_returns_json` — route that raises; assert 500 JSON
4. Create `templates/errors/404.html` and `templates/errors/500.html`

### Classification
- Error handlers + templates: **CODE** (app.py, templates/)
- Structured logging: **deferred** (out of Phase 2 scope; current convention is fine)

---

## SHARED SEAMS

> Every symbol more than one agent will touch. Canonical location defined once. Agents must NOT redefine these.

### SS-1 — `biz_tz(business)` — config.py (defined by Agent 1 / SF-5)
```python
# config.py, after app_tz() (~line 258)
NPA_TO_IANA: dict[str, str]  # top-50 US NPAs (defined above biz_tz)
def biz_tz(business: dict | int) -> tzinfo:
    """Per-business tz: reads businesses.timezone, then NPA, then app_tz()."""
```
All agents import it as `from config import biz_tz`. No agent defines it elsewhere.

### SS-2 — `db.set_business_timezone(business_id, tz_name)` — db.py (defined by Agent 1 / SF-5)
```python
def set_business_timezone(business_id: int, tz_name: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE businesses SET timezone=? WHERE id=?", (tz_name, business_id))
    conn.commit(); conn.close()
```

### SS-3 — `google_cal._slot_dt(day_iso, time_key_str, tz=None)` — google_cal.py (defined by Agent 1 / SF-5 + F04)
Signature change: adds optional `tz` param. All internal callers must pass `tz`. Agent 2 (F04) reuses this signature; Agent 1 defines it.

### SS-4 — `google_cal.create_event_and_store(business_id, appt_id, summary, description, day_iso, time_key_str)` — google_cal.py (defined by Agent 2 / F04)
Replaces `create_event_async`. The old function name is KEPT as an alias for one release:
```python
create_event_async = create_event_and_store  # back-compat; remove in Phase 3
```
app.py call sites at lines 1349, and new first-turn site both use `create_event_and_store`.

### SS-5 — `db.set_appointment_google_event(appt_id, event_id)` — db.py (defined by Agent 2 / F04)
```python
def set_appointment_google_event(appt_id: int, event_id: str) -> None: ...
```

### SS-6 — `appointments.google_event_id TEXT` column — db.py migration (defined by Agent 2 / F04)
Migration guard placed in db.py `init_db` after existing appt column migrations (~line 425):
```python
appt_cols = [r[1] for r in c.execute("PRAGMA table_info(appointments)").fetchall()]
if "google_event_id" not in appt_cols:
    c.execute("ALTER TABLE appointments ADD COLUMN google_event_id TEXT")
```

### SS-7 — `businesses` new columns (defined by Agent 1 / SF-7 migration block)
Three new columns added in ONE guarded migration block in `db.init_db`:
- `sentinel_call_sid TEXT`
- `sentinel_initiated_at TEXT`
- `forwarding_probe_at TEXT`
The migration block is placed **after** the existing Twilio/business column block (~db.py:516).

### SS-8 — `scheduled_messages` new columns (defined by Agent 1 / SF-4)
Two new columns:
- `retry_count INTEGER DEFAULT 0`
- `retry_at TEXT`
Migration placed after existing scheduled_messages column block in `db.init_db`.

### SS-9 — `app.errorhandler` (defined by Agent 3 / Reliability)
Two handlers added to app.py, after the `app` object is configured (~line 54). Agent 3 owns this; no other agent touches it.

### SS-10 — `reminders.classify_rsvp` + `compute_morning_of` (defined by Agent 3 / F05)
Both pure functions, no DB writes. Agent 3 adds them to reminders.py. Agent 2 (F04 / google_cal) does not touch reminders.py.

### SS-11 — `db.get_sched_by_provider_sid(provider_sid)` + `db.schedule_retry(...)` (defined by Agent 1 / SF-4)
```python
def get_sched_by_provider_sid(provider_sid: str) -> dict | None: ...
def schedule_retry(sched_id: int, retry_at_iso: str, retry_count: int) -> None: ...
```

---

## PROPOSED 3-WAY PARTITION

### Collision analysis of shared files

| File | Edits needed | Assigned to |
|---|---|---|
| `db.py` | SS-2, SS-5, SS-6, SS-7, SS-8, SS-11 (6 migration blocks + 5 new functions) | **Agent 1** owns ALL db.py changes |
| `config.py` | SS-1 (`biz_tz`, `NPA_TO_IANA`), buffer default (line 309) | **Agent 1** owns ALL config.py changes |
| `app.py` | SF-7 routes, SF-4 webhook tweak, F04 first-turn fix, F03 double-booking, F05 RSVP wire, Reliability errorhandlers | **Agent 2** owns all app.py changes except SS-9 (errorhandlers → **Agent 3**) |
| `reminders.py` | SF-5 threading (`biz_tz`), SF-7 probe in tick_once, F05 morning_reminder + classify_rsvp | **Agent 1** for SF-5 tz threading; **Agent 3** for F05 new functions |
| `google_cal.py` | SF-5 tz in `_slot_dt` + Google connect tz read, F04 all-day fix + event persistence | **Agent 2** owns ALL google_cal.py changes |
| `messaging.py` | SF-4 status_callback auto-inject (1 line) | **Agent 1** |
| `alerts.py` | SF-4 sms_fail kind, SF-7 forwarding_lost kind | **Agent 1** |
| `ai.py` | F03 turn cap, price guard, length guard | **Agent 3** |
| `convos.py` | NONE — no Phase 2 changes needed | — |
| `llm.py` | NONE — no Phase 2 changes needed | — |

### AGENT 1 — Delivery + Timezone + Sentinel
**Files owned exclusively:** `messaging.py`, `alerts.py`, `config.py`, `db.py`, and reminders.py SF-5 section only

**Work items:**
- **SF-4**: Auto-inject status_callback in messaging.py; new retry/get_sched db functions; retry + owner-alert logic in the status webhook (`/webhooks/twilio/sms/status` is in app.py:2082 — see collision note below)
- **SF-5**: `biz_tz` + `NPA_TO_IANA` in config.py; `set_business_timezone` in db.py; thread `biz_tz` in reminders.py (lines 149, 164, 218-220)
- **SF-7 DB migrations only**: Add sentinel + probe columns to db.py (SS-7); add `set_sentinel_call`, `clear_sentinel_call`, `get_business_by_sentinel_sid`, `set_forwarding_probe_at` to db.py
- **SF-4 DB migrations**: retry columns on scheduled_messages (SS-8)
- **SF-6/alerts**: new alert kinds `sms_fail`, `forwarding_lost` in alerts.py
- **config.py buffer default**: change `DEFAULT_BUFFER_MINUTES = 0` to `= 60`

**New test files to write:** `test_sf4_delivery.py`, `test_sf5_timezone.py`

**Collision note:** The retry dispatch logic inside `/webhooks/twilio/sms/status` (app.py:2082) is Agent 2's file. Agent 1 writes the db + messaging layer; Agent 2 wires the retry dispatch in the route. Coordinate via SS-11 (function signatures defined by Agent 1, called by Agent 2).

### AGENT 2 — Google Calendar + app.py Core Logic
**Files owned exclusively:** `google_cal.py`, `app.py` (all changes except errorhandlers)

**Work items:**
- **SF-7 app routes**: POST `/setup/forwarding/test`, POST `/webhooks/twilio/voice/sentinel`, GET `/api/forwarding/status` — using db functions from Agent 1 (SS-7)
- **F04 all-of-it**: `_slot_dt` tz param; all-day event parse fix; `create_event_and_store`; `delete_event`; `update_event`; google_cal.py timezone read in `connect_with_code`; first-turn booking wire in `open_conversation`; cancel_appointment Google delete; uses `biz_tz` (from Agent 1) and `db.set_appointment_google_event` (its own new function in db.py — **exception: this one db function can be added by Agent 2 under Agent 1's supervision since Agent 1 already owns db.py; coordinate with Agent 1 to land it in the same db.py commit**)
- **SF-4 retry dispatch**: `/webhooks/twilio/sms/status` retry logic using `db.get_sched_by_provider_sid` + `db.schedule_retry` (Agent 1 defines those)
- **F03 double-booking recovery**: `handle_inbound` (app.py:1329-1332)
- **F05 RSVP wire**: 5-line block in inbound webhook (app.py)

**New test files to write:** `test_f04_google.py`

**Existing tests to re-run:** `test_scheduling.py`, `test_webhooks.py`, `test_callback.py`, `test_connect_hub.py`

### AGENT 3 — Brain Guards + Reminders Test + Reliability
**Files owned exclusively:** `ai.py`, `templates/errors/`, `test_reminders.py` (new), and reminders.py F05 section only (pure functions appended to bottom of file)

**Work items:**
- **F03**: Turn cap, price guard, length guard in `ai.py`
- **F05 pure functions**: `compute_morning_of`, `morning_reminder_body`, `classify_rsvp` appended to reminders.py (pure functions only; no changes to existing functions which Agent 1 already owns for SF-5)
- **F05 enqueue**: Add morning_reminder enqueue inside `enqueue_reminder` (reminders.py:136) — Agent 3 owns this one targeted edit to the body of `enqueue_reminder` (adds 4 lines after line 158)
- **F05 run_due_once**: Add `morning_reminder` kind handling to `run_due_once` (reminders.py:171) — 2-line change
- **Reliability**: Add `@app.errorhandler(404/500)` to app.py (SS-9); create `templates/errors/404.html` and `templates/errors/500.html`
- **test_reminders.py**: Write all 14 tests listed in F05

**New test files to write:** `test_f03_brain.py`, `test_reminders.py`, `test_reliability.py`

**Existing tests to re-run:** `test_screening.py`, `test_scheduling.py`, `test_assistant.py`

### Partition coordination rules
1. **db.py and config.py are Agent 1's exclusively.** Agents 2 and 3 call Agent 1's functions; they never edit these files.
2. **app.py split**: Agent 2 owns all logic routes; Agent 3 owns only the two `@app.errorhandler` stubs at the top of the file (after line 54). These are disjoint additions — no line conflict if Agent 3 appends to the top section.
3. **reminders.py split**: Agent 1 edits existing function bodies (SF-5 tz threading, SF-7 probe). Agent 3 appends pure functions to the bottom and edits `enqueue_reminder` body (F05). These are non-overlapping edits if the file is read carefully — but this is a **medium collision risk** (see top 3 risks below).
4. **google_cal.py**: Agent 2 only.
5. **ai.py**: Agent 3 only.
6. **alerts.py**: Agent 1 only.
7. **messaging.py**: Agent 1 only.

---

## TEST PLAN

### New test files (per agent)
| File | Owner Agent | Tests |
|---|---|---|
| `test_sf4_delivery.py` | Agent 1 | 6 tests |
| `test_sf5_timezone.py` | Agent 1 | 7 tests |
| `test_sf7_sentinel.py` | Agent 2 | 6 tests |
| `test_f04_google.py` | Agent 2 | 5 tests |
| `test_f03_brain.py` | Agent 3 | 7 tests |
| `test_reminders.py` | Agent 3 | 14 tests |
| `test_reliability.py` | Agent 3 | 4 tests |

**Total new tests: ~49**

### Existing tests each agent must re-run (green) before PR
| Agent | Must re-run |
|---|---|
| Agent 1 | `test_webhooks.py`, `test_scheduling.py`, `test_config_hub.py`, `test_connect_hub.py`, `test_alert_channel.py`, `test_compliance.py`, `test_compliance_core.py` |
| Agent 2 | `test_webhooks.py`, `test_scheduling.py`, `test_callback.py`, `test_connect_hub.py`, `test_google_oauth.py` |
| Agent 3 | `test_screening.py`, `test_scheduling.py`, `test_assistant.py`, `test_streaming.py` |
| **All agents re-run full suite** after merge (28+49 = ~77 tests must be green) |

---

## TOP 3 COLLISION RISKS

### Risk 1 — reminders.py has two agents editing it (HIGH)
Agent 1 edits the bodies of existing functions (`enqueue_reminder` line 149, `_appt_passed` signature change, `scan_followups` line 218). Agent 3 appends new pure functions to the bottom AND edits the body of `enqueue_reminder` (line 156+) to add morning_reminder enqueue. If both agents work from the same base commit, they will produce overlapping diffs. **Mitigation:** Agent 1 lands its SF-5 tz threading changes first (they are early in each function body); Agent 3's enqueue_reminder edit is at the end of the function body. Define the exact line numbers for Agent 3's insertion point AFTER Agent 1 merges, not before. Alternatively, have Agent 1 add a stub hook at the bottom of `enqueue_reminder` that Agent 3 fills in.

### Risk 2 — db.py: Agent 2 needs `set_appointment_google_event` but Agent 1 owns db.py (MEDIUM)
Agent 2 (F04) needs `db.set_appointment_google_event` which must be added to db.py by Agent 1. If Agent 1 doesn't add it, Agent 2's google_cal.py code will NameError at test time. **Mitigation:** Include `set_appointment_google_event` explicitly in Agent 1's db.py task list (it's in SS-5 above). Agent 1 writes it as a stub; Agent 2 calls it. Both commit independently and the merge review wires them.

### Risk 3 — app.py: Agent 2 edits the retry dispatch in `/webhooks/twilio/sms/status` (line 2082) which is 2 lines from Agent 3's errorhandler (which goes at line ~54, far away in the file but same file) (LOW)
These edits are well-separated in the file (top vs. bottom). The real risk is that both agents write `from config import biz_tz` or similar new imports at the top of app.py. **Mitigation:** Agent 1 defines `biz_tz` in config.py; app.py already imports from config (app.py:39-42). Agents 2 and 3 add only to the existing `from config import (...)` block at line 39. They must coordinate the exact import additions to avoid a duplicate `from config import` line.

---

## OWNER-OPS SUMMARY (additions to SETUP_NEEDED.md)

1. **SF-4**: `FIRSTBACK_PUBLIC_URL` must be set in Render env for status_callback to auto-wire. (Already in SETUP_NEEDED.md; confirm it's set before deploying SF-4.)
2. **SF-5**: After deploy, owner saves their timezone in Settings. Google connect auto-reads it.
3. **SF-7**: Owner still needs to physically dial the star code (carrier forwarding). But now FirstBack verifies it with a real call. Owner taps "Verify" in /setup; phone must ring within 30s.
4. **F04**: No new owner action. Google Calendar must be connected for events to appear.
5. **F03**: No new owner action.
6. **F05**: No new owner action. Morning-of reminders auto-queue on next booking after deploy.
7. **Reliability**: No new owner action.

---

*Spec written: 2026-06-18 by Auditor A. Per-feature detail is above; the orchestrator reconciles this with Auditor B before starting the build.*
