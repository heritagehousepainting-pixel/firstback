# Phase 2 — Auditor B Implementation Spec
**Date:** 2026-06-18  
**Branch:** staging @ cecc076  
**Auditor:** B (independent; Auditor A runs in parallel)

---

## Executive summary

Seven work items. Three are pure-code (SF-4, F03, F05/reliability). Two require
code + a short owner-ops verification step (SF-5, F04). Two have an
owner-triggered real-world action that cannot be automated (SF-7 forwarding
confirmation, SF-5 timezone read from Google). All 28 tests must stay green
after each slice lands.

Collision files: `db.py`, `app.py`, `config.py`. The partition below isolates
all shared edits to Slice A so B and C stay file-disjoint.

---

## SHARED SEAMS

Every symbol touched by more than one slice. Build agents must use these
signatures exactly; diverging here breaks the merge.

### `biz_tz(business_id) -> ZoneInfo | tzinfo`  — **config.py**

```python
def biz_tz(business_id: int | None = None):
    """Per-business timezone. Preference order:
      1. businesses.timezone column (IANA name set on Google connect or owner-set)
      2. NPA area-code fallback from businesses.phone / businesses.twilio_number
      3. app_tz() (global FIRSTBACK_TZ env, then server local)
    Returns a tzinfo. Never raises.
    """
```
Called by: reminders.py (enqueue_reminder, compute_send_at, scan_followups,
_appt_passed, run_due_once), google_cal.py (_slot_dt), messaging.py (quiet
hours backstop already reads business["timezone"] directly — keep that, also
call biz_tz as fallback).

### `db.set_sms_delivery(provider_sid, status, business_id, lead_id)` — **db.py**

Extend the existing `set_message_delivery`. New signature:
```python
def set_sms_delivery(provider_sid: str, status: str,
                     business_id: int | None = None,
                     lead_id: int | None = None):
```
Keep `set_message_delivery(provider_sid, status)` as a shim
(`set_sms_delivery(provider_sid, status)`) so existing callers don't break.

### `db.queue_sms_retry(business_id, lead_id, to, body, attempt, send_at, kind="sms_retry")` — **db.py**

Uses the existing `scheduled_messages` table with `kind='sms_retry'`.
The `retry_count` column must be added (migration below).

### `db.set_forwarding_sentinel(business_id, call_sid, sent_at)` — **db.py**

```python
def set_forwarding_sentinel(business_id: int, call_sid: str, sent_at: str):
    """Record an in-flight sentinel call SID so the inbound webhook can
    match it and confirm forwarding is actually working."""
```
New column on businesses: `forwarding_sentinel_sid TEXT, forwarding_sentinel_at TEXT`.

### `db.set_google_event_id(appointment_id, google_event_id)` — **db.py**

New column on appointments: `google_event_id TEXT`.

### `db.get_appointment(business_id, appointment_id) -> dict | None` — **db.py**

Already likely exists; verify the function name before calling it in the patch path.
If missing, add:
```python
def get_appointment(business_id, appointment_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM appointments WHERE id=? AND business_id=?",
        (appointment_id, business_id)).fetchone()
    conn.close()
    return dict(row) if row else None
```

---

## DB MIGRATIONS

All use the repo's guarded pattern: `if "col" not in cols: c.execute(ALTER TABLE...)`.
Add to `init_db()` in `db.py` in the Slice A pass (once).

```python
# Phase 2 — SF-4: retry tracking on scheduled_messages
sched_cols = [r[1] for r in c.execute("PRAGMA table_info(scheduled_messages)").fetchall()]
if "retry_count" not in sched_cols:
    c.execute("ALTER TABLE scheduled_messages ADD COLUMN retry_count INTEGER DEFAULT 0")
if "retry_of" not in sched_cols:
    c.execute("ALTER TABLE scheduled_messages ADD COLUMN retry_of INTEGER")

# Phase 2 — SF-7: sentinel call tracking on businesses
biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
for col, ddl in (
        ("forwarding_sentinel_sid", "TEXT"),
        ("forwarding_sentinel_at",  "TEXT"),
        ("forwarding_last_probe_at","TEXT")):
    if col not in biz_cols:
        c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")

# Phase 2 — F04: Google Calendar event id on appointments
appt_cols = [r[1] for r in c.execute("PRAGMA table_info(appointments)").fetchall()]
if "google_event_id" not in appt_cols:
    c.execute("ALTER TABLE appointments ADD COLUMN google_event_id TEXT")
```

---

## ITEM-BY-ITEM SPECIFICATION

---

### SF-4 — SMS delivery receipts + retry/backoff

**Classification:** CODE

**What the code shows today:**
- `messaging.send_sms` (line 68) accepts `status_callback` param but NO call
  site passes it. The three call sites are:
  - `app.py:1955` — `messaging.send_sms(biz, caller, reply)` (missed-call text-back, inbound voice)
  - `app.py:2078` — `messaging.send_sms(biz, caller, reply)` (inbound SMS reply)
  - `app.py:1572` — `messaging.send_sms(biz, caller, reply)` (screen-override engage)
  - `app.py:1505` — `messaging.send_sms(...)` (appointment cancel notification)
  - `alerts.py:117` — `messaging.send_sms(business, sms_to, body, gate=False)` (owner alerts)
  - `reminders.py:194` — `messaging.send_sms(biz, phone, row["body"], lead_id=...)` (scheduled)
- `/webhooks/twilio/sms/status` (app.py:2082) already exists and calls
  `db.set_message_delivery(MessageSid, MessageStatus)` — the receiver IS WIRED.
  The DB column `messages.delivery_status` also EXISTS (added in migration at
  db.py:519). The gap is only that the callback URL is never passed to Twilio.

**Implementation:**

1. **`config.py`** — add helper (no new env var needed):
   ```python
   def sms_status_callback_url():
       """Absolute URL for Twilio's StatusCallback on an SMS send.
       Returns '' when PUBLIC_BASE_URL is unset (Twilio will just skip it).
       Never raises."""
       base = PUBLIC_BASE_URL.rstrip("/") if PUBLIC_BASE_URL else ""
       return f"{base}/webhooks/twilio/sms/status" if base else ""
   ```

2. **`messaging.py`** — change the real-send block (lines 147-165) to auto-inject
   `status_callback` when not passed:
   ```python
   if status_callback is None:
       from config import sms_status_callback_url
       status_callback = sms_status_callback_url() or None
   ```
   This is the ONLY change to messaging.py; all call sites gain the callback
   automatically without any caller change.

3. **`db.py`** — add two new functions (after `mark_scheduled`, line ~2169):
   ```python
   def queue_sms_retry(business_id, lead_id, to, body, attempt, send_at,
                       original_sid=None, kind="sms_retry"):
       """Enqueue a retry for a failed SMS. attempt=1..3. send_at UTC ISO."""
       conn = get_conn()
       try:
           cur = conn.execute(
               "INSERT INTO scheduled_messages "
               "(business_id, lead_id, kind, send_at, body, status, retry_count, created_at) "
               "VALUES (?,?,?,?,?,'pending',?,?)",
               (business_id, lead_id, kind, send_at, body, attempt, now_iso()))
           conn.commit()
           return cur.lastrowid
       except Exception:
           conn.rollback()
           return None
       finally:
           conn.close()

   def sms_retry_needed(business_id, lead_id, provider_sid):
       """True if this outbound SMS ended in a terminal failure (not queued for retry yet)."""
       conn = get_conn()
       row = conn.execute(
           "SELECT id FROM messages WHERE business_id=? AND lead_id=? "
           "AND provider_sid=? AND delivery_status IN ('failed','undelivered') LIMIT 1",
           ... # join messages to leads for business_id scope
       ).fetchone()
       conn.close()
       return bool(row)
   ```

4. **`reminders.py`** — in `run_due_once` (line 171), after `db.mark_scheduled(row["id"], "failed")`:
   add retry logic for `kind in ("sms_reply", "reminder", "followup", "sms_retry")` with attempt
   cap at 3. Backoff schedule: attempt 1 = 30s, 2 = 120s, 3 = 600s.
   ```python
   RETRY_DELAYS = [30, 120, 600]  # seconds

   def _schedule_retry(row, attempt):
       """Queue the next retry unless we've hit the cap (3 attempts)."""
       if attempt >= len(RETRY_DELAYS):
           return False  # give up
       from datetime import datetime, timezone, timedelta
       send_at = (datetime.now(timezone.utc) +
                  timedelta(seconds=RETRY_DELAYS[attempt - 1])).isoformat()
       db.queue_sms_retry(
           row["business_id"], row["lead_id"],
           row.get("lead_phone", ""), row["body"], attempt, send_at)
       return True
   ```

5. **`reminders.py`** — in `run_due_once`, when `status == "error"`:
   ```python
   elif status == "error":
       attempt = (row.get("retry_count") or 0) + 1
       if not _schedule_retry(row, attempt):
           # 3 retries exhausted: alert the owner
           _alert_sms_fail(row)
       db.mark_scheduled(row["id"], "failed")
   ```

6. **Owner-alert on final failure** — add `_alert_sms_fail(row)` in reminders.py:
   ```python
   def _alert_sms_fail(row):
       """Email/alert the owner when an SMS exhausts all retries."""
       try:
           biz = db.get_business(row["business_id"])
           import alerts
           alerts.notify_async(biz, "sms_fail", {
               "lead_id": row.get("lead_id"),
               "phone": row.get("lead_phone"),
               "body": row.get("body", "")[:80],
           })
       except Exception as e:
           import sys
           print(f"[firstback] sms_fail alert error: {e}", file=sys.stderr, flush=True)
   ```
   Wire `"sms_fail"` format_message in `alerts.py`.

7. **Surface delivery status in thread** — `messages.delivery_status` is already stored.
   No new DB column needed. The dashboard template that renders thread messages
   (templates/partials or lead detail) should show a small "(delivered)" / "(failed)"
   badge when `delivery_status` is set. This is a template change in the Slice A pass.

**Acceptance tests** (new file: `test_delivery_receipts.py`):
- Mock Twilio, send an SMS, confirm the status_callback URL is present in the POST body.
- Simulate a `failed` delivery callback; confirm a retry row is queued with `retry_count=1`.
- Simulate 3 failures; confirm no 4th retry is queued and `alerts.notify_async` was called.
- Confirm `run_due_once` processes a `sms_retry` row like a normal message.

---

### SF-5 — Per-business timezone

**Classification:** CODE (core math) + OWNER-OPS (verify timezone set after Google connect)

**What the code shows today:**
- `businesses.timezone TEXT` column EXISTS (db.py:494 migration).
- `app_tz()` in config.py (line 248) reads only `TIMEZONE` env var — ignores the DB column.
- `reminders.py` uses `app_tz()` everywhere (lines 71, 87, 149, 163, 166, 218, 221).
- `google_cal.py:_slot_dt` (line 118) calls `.astimezone()` with no explicit tz —
  uses the server's local zone, which is correct for a single-region deploy but wrong
  for a multi-tenant build where tenants are in different timezones than the server.
- `messaging.py:111-121` already reads `business["timezone"]` for the quiet-hours
  backstop (a partial SF-5 implementation from Phase 1). We extend this pattern
  system-wide.

**Implementation:**

1. **`config.py`** — add `biz_tz(business_id)` (the SHARED SEAM above). Implementation:
   ```python
   def biz_tz(business_id: int | None = None):
       if business_id:
           try:
               import db as _db
               biz = _db.get_business(business_id)
               tz_name = (biz or {}).get("timezone", "").strip()
               if tz_name:
                   from zoneinfo import ZoneInfo
                   return ZoneInfo(tz_name)
               # NPA area-code fallback
               phone = (biz or {}).get("phone") or (biz or {}).get("twilio_number") or ""
               npa = re.sub(r"\D", "", phone)[-10:][:3] if phone else ""
               if npa:
                   iana = _NPA_TZ.get(npa)
                   if iana:
                       from zoneinfo import ZoneInfo
                       return ZoneInfo(iana)
           except Exception:
               pass
       return app_tz()
   ```
   Add `_NPA_TZ` dict mapping major US area codes to IANA zones. Include at minimum:
   Eastern (201-202-203-212-215-401-404-407-516-551-609-631-646-718-732-845-908-917-973+),
   Central (214-312-469-512-602-630-713-763-815-940+),
   Mountain (303-480-505-520-602-720-801-970+),
   Pacific (206-209-213-253-310-323-408-415-425-503-510-530-619-650-661-707-760-805-818-858-909-916-925-949-971+),
   Alaska (907), Hawaii (808).
   Note: NPA fallback is a best-effort heuristic, not an authority — it's the
   "better than UTC/server" fallback until the owner connects Google.

2. **`google_cal.py`** — update `connect_with_code` to read `/calendars/primary` after
   token exchange and store `timeZone`:
   ```python
   def connect_with_code(business_id, code):
       # ... existing token exchange ...
       db.set_google_tokens(business_id, tok.get("access_token"),
                            tok.get("refresh_token"), _expiry_iso(tok), "primary")
       # NEW: read primary calendar timezone and persist it on the business
       _store_calendar_timezone(business_id)

   def _store_calendar_timezone(business_id):
       """Read the primary calendar's timeZone from Google and persist to businesses.timezone."""
       token = _access_token(business_id)
       if not token:
           return
       import requests
       try:
           r = requests.get(f"{API_BASE}/calendars/primary",
                            headers={"Authorization": f"Bearer {token}"}, timeout=10)
           r.raise_for_status()
           tz_name = r.json().get("timeZone", "")
           if tz_name:
               from zoneinfo import ZoneInfo
               ZoneInfo(tz_name)  # validate — raises if invalid
               conn = db.get_conn()
               conn.execute("UPDATE businesses SET timezone=? WHERE id=?",
                            (tz_name, business_id))
               conn.commit()
               conn.close()
       except Exception as e:
           print(f"[firstback] calendar timezone read failed (biz {business_id}): {e}",
                 file=sys.stderr, flush=True)
   ```

3. **`google_cal.py`** — update `_slot_dt` to accept `tz=None`:
   ```python
   def _slot_dt(day_iso, time_key_str, tz=None):
       from zoneinfo import ZoneInfo
       if tz is None:
           tz = datetime.now().astimezone().tzinfo
       y, m, d = (int(x) for x in day_iso.split("-"))
       hh, mm = (int(x) for x in time_key_str.split(":"))
       return datetime(y, m, d, hh, mm, tzinfo=tz)
   ```
   Update `busy_slot_ids`, `create_event`, `_slots_conflicting` to thread `tz` through.

4. **`reminders.py`** — replace all `app_tz()` calls with `biz_tz(business_id)` where a
   business_id is in scope. The key changes:
   - `enqueue_reminder(business, lead, day_iso, slot_time)` line 149: `tz = biz_tz(business["id"])`
   - `compute_send_at` signature stays pure (accepts `tz` param) — no change needed there.
   - `scan_followups` line 218: `now_local = datetime.fromisoformat(now).astimezone(biz_tz(biz["id"]))`
   - `_appt_passed` line 163: `tz = biz_tz(...)` — needs business_id plumbed in; refactor to
     accept it: `def _appt_passed(day_iso, slot_time, business_id=None)`.

5. **`db.py`** — `_today()` at line 1187 uses `app_tz()`; it backs `upcoming_slots` /
   `calendar_month`. Add an optional `tz` parameter:
   ```python
   def _today(tz=None):
       return datetime.now(tz or app_tz()).date()
   ```
   Pass `biz_tz(business_id)` from `upcoming_slots(business_id, ...)` and `calendar_month`.

**Owner-ops note:** After Google Calendar connects, the business timezone is auto-read.
Before connection, it can be set manually in Settings (add a timezone field to the settings
form). Add to `SETUP_NEEDED.md`: "Confirm businesses.timezone is set for each tenant;
check /settings or verify via Google Calendar connect."

**Acceptance tests** (add to `test_scheduling.py` or new `test_timezone.py`):
- `biz_tz` with a valid IANA name stored in businesses returns the correct ZoneInfo.
- `biz_tz` with no timezone stored falls back to NPA, then app_tz.
- `_store_calendar_timezone` with a mocked Google API response writes the right IANA name.
- `compute_send_at` with Eastern vs Pacific timezone produces the correct UTC difference.
- DST edge case: a slot at 2026-03-08 09:00 US/Eastern should convert at the right offset.

---

### SF-7 — Forwarding sentinel-call verification + weekly health probe

**Classification:** CODE + OWNER-OPS (owner must make the actual star-code call)

**What the code shows today:**
- `app.py:1184` — `db.set_forwarding_confirmed(biz["id"], True)` fires on button click in
  `/setup/forwarding` POST, with zero proof the carrier accepted the star code.
- No sentinel call, no weekly probe, no re-alert mechanism exists.
- The `twilio_voice_inbound` webhook already handles inbound calls and identifies the business
  by `To` number (line 1964).
- `place_call(business, to, twiml_url, status_callback)` exists in messaging.py (line 191).
- `forwarding_confirmed` column on businesses already exists (db.py:506).

**Implementation:**

1. **DB migration** (Slice A): add `forwarding_sentinel_sid TEXT`, `forwarding_sentinel_at TEXT`,
   `forwarding_last_probe_at TEXT` to businesses (shown in MIGRATIONS above).

2. **New function `connections.py` (or new file `forwarding.py`)** — `send_sentinel_call`:
   ```python
   SENTINEL_CALLER_ID = "+15005550006"  # Twilio test number; use real ALERT_FROM_NUMBER in prod

   def send_sentinel_call(business_id: int) -> dict:
       """Place an outbound call FROM the business's own FirstBack number TO the
       owner's cell (forward_to). When carrier forwarding is active, Twilio will
       call the FirstBack number, which triggers our inbound webhook, confirming the
       net is on. The sentinel is identified by its SID, stored on the business.
       Returns {"status": "placed"|"simulated"|"error"|"no_number"|"no_forward"}.
       """
       biz = db.get_business(business_id)
       forward_to = (biz.get("forward_to") or "").strip()
       if not forward_to:
           # catcher mode: no forward number means we can't test the ring-through path
           # In catcher mode, confirm immediately (any inbound call to the FirstBack number
           # proves the forwarding is set).
           return {"status": "catcher_mode"}
       twilio_num = (biz.get("twilio_number") or "").strip()
       if not twilio_num or not messaging.configured():
           return {"status": "no_number"}
       # TwiML: play a brief message, then hang up. The point is to ring forward_to.
       twiml_url = _public_base_for(business_id) + "/webhooks/twilio/voice/sentinel-twiml"
       result = messaging.place_call(biz, forward_to, twiml_url)
       if result.get("status") == "placed":
           db.set_forwarding_sentinel(business_id, result["sid"],
                                      datetime.utcnow().isoformat(timespec="seconds"))
       return result
   ```

3. **New TwiML route in `app.py`** — `/webhooks/twilio/voice/sentinel-twiml`:
   ```python
   @app.route("/webhooks/twilio/voice/sentinel-twiml", methods=["POST"])
   def twilio_sentinel_twiml():
       """Returns the TwiML for a sentinel call: brief message + hang up."""
       return _twiml(
           "<Response><Say>This is a FirstBack system test. "
           "Your call forwarding is working. Goodbye.</Say><Hangup/></Response>")
   ```
   No `@require_twilio_signature` needed on this specific TwiML — the caller is Twilio
   and it's just a TwiML response, but we should add it for safety. Actually: this
   endpoint is called BY the owner's phone dialing forward to it, so Twilio WILL sign it.
   Add `@require_twilio_signature`.

4. **Inbound webhook confirmation** — in `twilio_voice_inbound` (app.py:1959), after
   the business is resolved, check if an in-flight sentinel call exists:
   ```python
   # SF-7: if this inbound call matches a pending sentinel SID, confirm forwarding.
   call_sid = request.form.get("CallSid", "")
   sentinel_sid = biz.get("forwarding_sentinel_sid")
   if sentinel_sid and call_sid == sentinel_sid:
       db.set_forwarding_confirmed(biz["id"], True)
       db.set_forwarding_sentinel(biz["id"], None, None)  # clear the in-flight sentinel
       db.set_meta(f"forwarding_probe_{biz['id']}", datetime.utcnow().isoformat())
       # Don't process as a real call — hang up silently.
       return _twiml("<Response><Hangup/></Response>")
   ```

5. **Weekly health probe in `reminders.tick_once`** — add a call to
   `_check_forwarding_health()` in `tick_once`. Run it at most once per week per business:
   ```python
   def _check_forwarding_health():
       """Weekly: re-probe each connected business's forwarding. Alert if stale."""
       from datetime import datetime, timezone, timedelta
       for biz in db.list_businesses():
           if not biz.get("forwarding_confirmed"):
               continue
           last = biz.get("forwarding_last_probe_at")
           if last:
               age = (datetime.now(timezone.utc) -
                      datetime.fromisoformat(last).replace(tzinfo=timezone.utc)).total_seconds()
               if age < 7 * 24 * 3600:
                   continue
           # Re-probe: place another sentinel call
           # (This is a CODE path — can't trigger the actual phone call without Twilio)
           try:
               import connections as _conn
               _conn.send_sentinel_call(biz["id"])
               db.set_forwarding_probe(biz["id"])  # write forwarding_last_probe_at
           except Exception as e:
               print(f"[firstback] forwarding probe failed (biz {biz['id']}): {e}",
                     file=sys.stderr, flush=True)
   ```

6. **`/setup/forwarding` route** — stop calling `db.set_forwarding_confirmed(biz["id"], True)`
   directly. Instead:
   ```python
   # Replace the direct set_forwarding_confirmed call with sentinel initiation:
   from connections import send_sentinel_call
   result = send_sentinel_call(biz["id"])
   if result.get("status") == "catcher_mode":
       # catcher: any inbound will confirm; set a provisional confirmation
       # but mark it as unverified until the first real call comes in
       db.set_forwarding_confirmed(biz["id"], True)  # catcher is always on
   elif result.get("status") == "placed":
       # Sentinel placed: DO NOT confirm yet. Show "Verifying..." UI.
       pass  # forwarding_confirmed stays 0 until the inbound webhook fires
   else:
       # no Twilio: fall back to button-click confirmation (original behavior)
       db.set_forwarding_confirmed(biz["id"], True)
   ```

**Owner-ops:** The owner STILL dials the star code on their own phone — that is
irreducible. The sentinel call merely verifies the carrier accepted it. Add to
`SETUP_NEEDED.md`: "Owner must dial *72 (or carrier equivalent) before the sentinel
call test fires."

**Acceptance tests** (new file: `test_forwarding.py`):
- `send_sentinel_call` with a mocked `messaging.place_call` stores `forwarding_sentinel_sid`.
- Simulate an inbound webhook with matching `CallSid`: `forwarding_confirmed` flips to 1.
- Simulate with NON-matching `CallSid`: no flip.
- `_check_forwarding_health` with `forwarding_last_probe_at` 6 days ago: no re-probe.
- `_check_forwarding_health` with 8 days ago: probe fires.

---

### F04 — Google Calendar write-loop close

**Classification:** CODE + OWNER-OPS (Google must be connected for live behavior)

**What the code shows today:**
- `google_cal.create_event` (line 177) returns the event `id` but the caller
  (`app.py:1349`) uses `create_event_async` and discards the return value — event id
  is never stored.
- `appointments` table has no `google_event_id` column today.
- No `cancel_event` or `update_event` function exists.
- `_slots_conflicting` (line 153) parses Google busy intervals with
  `datetime.fromisoformat(iv["start"].replace("Z", "+00:00"))` — this handles
  timed events. But ALL-DAY events in the Google Calendar API return `{"date": "2026-06-15"}`
  NOT `{"dateTime": ...}`. The current code throws `KeyError` on `iv["start"]` for
  all-day events → the busy interval is silently skipped, leaving the slot open.
- `config.py:DEFAULT_BUFFER_MINUTES = 0` — the spec says it should be 60. Changing
  the default would affect existing tenants that have never customized it; their
  `buffer_minutes` column is 0 (explicitly stored on migration). Do NOT change
  `DEFAULT_BUFFER_MINUTES` globally — instead set the DEFAULT on new businesses
  during `create_business` to 60.
- `open_conversation` in app.py (line 1291) does NOT call `enqueue_reminder` after
  booking — a first-turn booking (where the AI immediately offers and books in the
  opening message) skips the reminder queue. `handle_inbound` does call
  `enqueue_reminder` (line 1353). This is the "first-turn booking unify" item.

**Implementation:**

1. **DB migration**: add `google_event_id TEXT` to appointments (shown in MIGRATIONS).

2. **`google_cal.py`** — add `cancel_event` and `update_event`:
   ```python
   def cancel_event(business_id, google_event_id):
       """Delete a Google Calendar event by its id. Returns True on success."""
       token = _access_token(business_id)
       if not token or not google_event_id:
           return False
       intg = db.get_integration(business_id, "google") or {}
       cal_id = intg.get("calendar_id") or "primary"
       import requests
       try:
           r = requests.delete(
               f"{API_BASE}/calendars/{cal_id}/events/{google_event_id}",
               headers={"Authorization": f"Bearer {token}"}, timeout=20)
           return r.status_code in (200, 204, 410)  # 410 = already deleted
       except Exception as e:
           print(f"[firstback] google event cancel failed (biz {business_id}): {e}",
                 file=sys.stderr, flush=True)
           return False

   def cancel_event_async(business_id, google_event_id):
       import threading
       threading.Thread(target=cancel_event,
                        args=(business_id, google_event_id), daemon=True).start()
   ```

3. **`google_cal.py`** — fix all-day event parsing in `_slots_conflicting` (lines 153-174):
   ```python
   def _slots_conflicting(intervals, today, tz=None):
       busy = []
       for iv in intervals:
           try:
               # Timed event: {"start": {"dateTime": "..."}}
               start_raw = iv.get("start") or {}
               end_raw   = iv.get("end") or {}
               if "dateTime" in start_raw:
                   bs = datetime.fromisoformat(start_raw["dateTime"].replace("Z", "+00:00"))
                   be = datetime.fromisoformat(end_raw["dateTime"].replace("Z", "+00:00"))
               elif "date" in start_raw:
                   # All-day event: occupies the entire local day
                   local_tz = tz or datetime.now().astimezone().tzinfo
                   bs = datetime.fromisoformat(start_raw["date"]).replace(
                       hour=0, minute=0, tzinfo=local_tz)
                   be = datetime.fromisoformat(end_raw["date"]).replace(
                       hour=23, minute=59, tzinfo=local_tz)
               else:
                   continue
               busy.append((bs, be))
           except (ValueError, KeyError, AttributeError):
               continue
       # ... rest unchanged
   ```

4. **`google_cal.py`** — make `create_event` synchronous version return the event id,
   and add a wrapper that stores it:
   ```python
   def create_event_and_store(business_id, appointment_id, summary, description,
                               day_iso, time_key_str, tz=None):
       """Create the calendar event AND persist the event id on the appointment."""
       event_id = create_event(business_id, summary, description, day_iso,
                                time_key_str, tz=tz)
       if event_id and appointment_id:
           db.set_google_event_id(appointment_id, event_id)
       return event_id

   def create_event_async(business_id, appointment_id, summary, description,
                           day_iso, time_key_str, tz=None):
       import threading
       threading.Thread(target=create_event_and_store,
                        args=(business_id, appointment_id, summary, description,
                               day_iso, time_key_str, tz),
                        daemon=True).start()
   ```
   Note: `create_event_async` now takes `appointment_id` — update the ONE call site in
   `handle_inbound` (app.py:1349) to pass it.

5. **`app.py`** — update `handle_inbound` call site (line 1349):
   ```python
   # Before:
   google_cal.create_event_async(biz["id"], summary, description, gday, gtime)
   # After:
   # find the appointment we just booked
   appt = db.find_appointment(biz["id"], lead_id, gday, gtime)
   google_cal.create_event_async(
       biz["id"], appt["id"] if appt else None,
       f"Estimate: {lead['name']}", f"...", gday, gtime)
   ```

6. **`app.py`** — update `cancel_appointment` route to also cancel the Google event:
   ```python
   # In the cancel route (around line 1495):
   appt = db.cancel_appointment(biz["id"], appt_id)
   if appt and appt.get("google_event_id"):
       google_cal.cancel_event_async(biz["id"], appt["google_event_id"])
   ```

7. **First-turn booking unify** — in `open_conversation` (app.py:1291), after the
   `db.book_appointment` call, add the same post-booking hooks that `handle_inbound` runs:
   ```python
   if booking:
       gday, gtime = db.parse_day(booking), db.time_key(booking)
       appt_row = db.book_appointment(biz["id"], lead["id"], booking)
       if appt_row:
           db.learn_customer(biz["id"], lead.get("phone"), lead.get("name"))
           if gday and gtime:
               appt = db.find_appointment(biz["id"], lead["id"], gday, gtime)
               google_cal.create_event_async(biz["id"], appt["id"] if appt else None,
                                              f"Estimate: {lead['name']}", "...",
                                              gday, gtime)
               reminders.enqueue_reminder(biz, lead, gday, gtime)
   ```

8. **Buffer default 60** — in `db.create_business` (line 798), set `buffer_minutes=60`
   as the initial value for new businesses. Do NOT change `DEFAULT_BUFFER_MINUTES` in
   config.py (that would retroactively affect existing tenants). Instead:
   ```python
   def create_business(fields):
       # Ensure new businesses get the Phase 2 default buffer
       if "buffer_minutes" not in fields:
           fields = dict(fields, buffer_minutes=60)
       ...
   ```

**Acceptance tests** (add to `test_scheduling.py` or new `test_google_cal.py`):
- `_slots_conflicting` with an all-day event returns the correct conflicting slots.
- `_slots_conflicting` with a timed event still works (regression).
- `create_event_and_store` with a mock HTTP response stores the event id on the appointment.
- `cancel_event` with a 410 response (already deleted) returns True (idempotent).
- `open_conversation` when the AI proposes a booking on turn 0 enqueues a reminder.

---

### F03 — Booking-brain guards

**Classification:** CODE  
**File:** `convos.py` (NOT trades_core/sync.py — edit firstback/convos.py directly)

**What the code shows today:**
- `convos.py` handles command-center (Vic) conversations, not the SMS booking brain.
  The SMS booking brain is in `ai.py` (`generate_reply`, `_system_prompt`).
- No turn cap exists. A stuck conversation can run indefinitely.
- The `_system_prompt` already says "Never quote prices" but this is an instruction
  to the model, not a post-reply guard. A jailbroken or hallucinating model can still
  emit prices.
- No post-reply length guard exists.
- Double-booking is handled at the DB layer (UNIQUE INDEX on `uniq_booked_slot`) and
  at `handle_inbound` (checks `prior` bookings). The gap is the AI may re-offer a slot
  that just became unavailable between turns.

**Implementation:**

1. **`ai.py`** — add turn cap:
   ```python
   MAX_CONVERSATION_TURNS = 12

   def generate_reply(business, history, exclude_slot_ids=None, lead_id=None):
       # Count inbound turns
       inbound_turns = sum(1 for m in history if m["direction"] == "in")
       if inbound_turns >= MAX_CONVERSATION_TURNS:
           return ("Thanks so much for your patience. Please give us a call directly "
                   "and we will get you sorted out right away."), None
       ...
   ```

2. **`ai.py`** — add post-reply price guard:
   ```python
   _PRICE_RE = re.compile(
       r"\$\s*\d[\d,]*(?:\.\d{2})?"      # $120 / $1,200 / $1,200.00
       r"|\b\d{2,5}\s*(?:dollars?|bucks?)"  # 500 dollars
       r"|\bquote\s+of\b"                 # "a quote of"
       r"|\best(?:imate)?\s+(?:is|of|around|about|roughly)\s+\$?\d",  # estimate is $
       re.I)

   def _scrub_price(text):
       """Remove price quotes from the reply; replace with a redirect."""
       if not _PRICE_RE.search(text):
           return text
       import sys
       print(f"[firstback] price guard fired, scrubbing reply", file=sys.stderr, flush=True)
       # Replace the price mention rather than discarding the whole reply.
       clean = _PRICE_RE.sub("[we will provide a quote at the estimate]", text)
       return clean
   ```
   Apply `_scrub_price` in `generate_reply` before returning.

3. **`ai.py`** — post-reply length guard (SMS is 160 chars per segment; long replies
   increase cost and look wrong on a phone):
   ```python
   MAX_REPLY_CHARS = 480  # 3 SMS segments is the soft cap

   def _trim_reply(text):
       if len(text) <= MAX_REPLY_CHARS:
           return text
       # Truncate at the last sentence boundary within the limit
       trimmed = text[:MAX_REPLY_CHARS]
       last_period = max(trimmed.rfind("."), trimmed.rfind("!"), trimmed.rfind("?"))
       if last_period > MAX_REPLY_CHARS // 2:
           return trimmed[:last_period + 1]
       return trimmed.rstrip() + "."
   ```

4. **`ai.py`** — double-booking recovery: when `book_appointment` returns False
   (slot taken), the current code in `handle_inbound` silently drops the booking.
   The AI doesn't know this — it may re-offer the same slot next turn. Add a recovery
   reply path:
   In `handle_inbound` (app.py:1330), after `db.book_appointment` returns False:
   ```python
   elif not db.book_appointment(biz["id"], lead_id, booking):
       # Slot was taken between turns. Generate a recovery reply offering alternatives.
       fresh_exclude = google_cal.busy_slot_ids(biz["id"])
       # Also exclude the just-failed slot
       fresh_exclude.add(f"{gday}@{gtime}")
       recovery_reply, new_booking = ai.generate_reply(
           biz, history + [{"direction": "out", "body": reply}],
           exclude_slot_ids=fresh_exclude, lead_id=lead_id)
       db.add_message(lead_id, "out", recovery_reply)  # replace the original reply
       reply = recovery_reply
       if new_booking:
           if db.book_appointment(biz["id"], lead_id, new_booking):
               booked = new_booking
               # ... same post-booking hooks
   ```
   Note: this adds a second LLM call on a rare code path (slot race). Acceptable cost.

**Acceptance tests** (new file: `test_booking_guards.py`):
- 12-inbound-turn conversation returns the cap reply without making an LLM call.
- 11-turn conversation still calls the brain normally.
- `_scrub_price` removes "$500" from a reply.
- `_scrub_price` leaves a price-free reply unchanged (no false positive).
- `_trim_reply` at 500 chars trims at a sentence boundary.
- `_trim_reply` at 200 chars returns unchanged.

---

### F05 — test_reminders.py + morning-of reminder + RSVP classification

**Classification:** CODE  
**The lie to fix:** `reminders.py` docstring (line 11) says "the scheduling math...is PURE
and unit-tested" — FALSE. `test_reminders.py` does not exist. This is item 1.

**What the code shows today:**
- `reminders.py` has these pure functions: `when_phrase`, `reminder_body`,
  `followup_body`, `next_send_time`, `compute_send_at`, `due_followup_leads`.
  All are testable without DB or Twilio.
- No morning-of reminder exists. The current `REMINDER_LEAD_HOURS = 24` sends one
  reminder 24h before. The spec asks for a morning-of reminder (day-of, e.g., 8 AM).
- No RSVP keyword classification (YES/NO/CANCEL/CONFIRM keywords from the customer reply
  after a reminder is sent) exists.

**Implementation:**

1. **Write `test_reminders.py`** — covering:
   - `when_phrase("2026-06-15", "14:00")` == "Mon Jun 15 at 2:00 PM"
   - `compute_send_at` with a future slot returns UTC ISO that's lead_hours before it
   - `compute_send_at` during quiet hours defers to quiet_start
   - `due_followup_leads` with a lead older than idle_hours returns it
   - `due_followup_leads` with a lead that `has_followup=True` skips it
   - `next_send_time` at 6 AM defers to quiet_start (8 AM default)
   - `next_send_time` at 10 AM stays at 10 AM
   - Run as: `.venv/bin/python test_reminders.py`

2. **Morning-of reminder** — add a second reminder kind `"morning_reminder"`:
   ```python
   def enqueue_morning_reminder(business, lead, day_iso, slot_time):
       """Queue a morning-of reminder for 8 AM on the day of the appointment."""
       if not reminders_on(business):
           return {"status": "disabled"}
       phone = (lead.get("phone") or "").strip()
       if not phone:
           return {"status": "skipped", "reason": "no phone"}
       tz = biz_tz(business["id"])
       # Morning-of: 8 AM on the estimate day
       try:
           from zoneinfo import ZoneInfo
           y, m, d = (int(x) for x in day_iso.split("-"))
           morning_local = datetime(y, m, d, 8, 0, tzinfo=tz)
       except (ValueError, TypeError):
           return {"status": "skipped", "reason": "bad date"}
       # Don't send a morning-of if the estimate is before 9 AM (would be same time or after)
       appt_hh = int((slot_time or "00:00").split(":")[0])
       if appt_hh < 10:
           return {"status": "skipped", "reason": "estimate too early for morning reminder"}
       now_local = datetime.now(tz)
       if morning_local <= now_local:
           return {"status": "skipped", "reason": "morning already passed"}
       send_at = morning_local.astimezone(timezone.utc).isoformat()
       appt = db.find_appointment(business["id"], lead["id"], day_iso, slot_time)
       if not appt:
           return {"status": "skipped", "reason": "appointment not found"}
       body = reminder_body(lead.get("name"), business.get("name") or "your contractor",
                            when_phrase(day_iso, slot_time))
       # Don't duplicate if a morning reminder is already queued
       existing = db.find_scheduled_message(business["id"], lead["id"], "morning_reminder")
       if existing:
           return {"status": "already_queued"}
       db.add_scheduled_message(business["id"], lead["id"], appt["id"],
                                 "morning_reminder", send_at, body)
       return {"status": "queued", "send_at": send_at}
   ```
   Add `db.find_scheduled_message(business_id, lead_id, kind)` to db.py.
   Call `enqueue_morning_reminder` from `handle_inbound` and `open_conversation`
   alongside `enqueue_reminder`.

3. **RSVP keyword classification** — add to `reminders.py`:
   ```python
   _RSVP_YES = re.compile(
       r"\b(?:yes|yeah|yep|confirm|confirmed|i.?ll be there|see you|"
       r"on my way|i.?m coming|sounds good|will be there|confirmed)\b", re.I)
   _RSVP_NO = re.compile(
       r"\b(?:no|cancel|can.?t make it|won.?t be|need to reschedule|"
       r"reschedule|not going to|can.?t come|sorry)\b", re.I)

   def classify_rsvp(text: str) -> str:
       """Classify an inbound customer reply after a reminder.
       Returns 'yes', 'no', or 'unknown'."""
       t = (text or "").strip()
       if _RSVP_YES.search(t):
           return "yes"
       if _RSVP_NO.search(t):
           return "no"
       return "unknown"
   ```
   Wire `classify_rsvp` into `handle_inbound` (app.py): when the lead has a booked
   appointment and the last outbound message was a reminder, classify the inbound reply.
   If "no" or "reschedule" → cancel the appointment + invite rebooking. If "yes" → log it
   and notify the owner ("Customer confirmed the estimate").

**Acceptance tests** (in the new `test_reminders.py`):
- `classify_rsvp("yes I'll be there")` == "yes"
- `classify_rsvp("sorry can't make it")` == "no"
- `classify_rsvp("what time again?")` == "unknown"
- `enqueue_morning_reminder` with a slot at 14:00 three days out returns `status: queued`
- `enqueue_morning_reminder` for a slot at 8:00 returns `status: skipped`

---

### Reliability — errorhandler pages + logging

**Classification:** CODE  
**File:** `app.py` (one section added, no other file changes needed)

**What the code shows today:**
- No `@app.errorhandler(404)` or `@app.errorhandler(500)` exist. Flask returns
  default HTML error pages.
- `import logging` is absent. All errors use `print(..., file=sys.stderr)`.
  The spec says "logging" — interpret as wiring Python's `logging` module so Render
  captures structured log lines, not just raw stderr prints.

**Implementation:**

1. **`app.py`** — add at module level (after imports, before `app = Flask(...)`):
   ```python
   import logging
   logging.basicConfig(
       level=logging.INFO,
       format="[firstback] %(asctime)s %(levelname)s %(name)s: %(message)s",
       datefmt="%Y-%m-%dT%H:%M:%SZ",
   )
   _log = logging.getLogger("firstback")
   ```

2. **`app.py`** — add error handlers (after the `@app.route` definitions, before `if __name__`):
   ```python
   @app.errorhandler(404)
   def not_found(e):
       if request.path.startswith("/api/") or request.path.startswith("/webhooks/"):
           return jsonify(error="Not found."), 404
       return render_template("errors/404.html"), 404

   @app.errorhandler(500)
   def server_error(e):
       _log.exception("Unhandled exception")
       if request.path.startswith("/api/") or request.path.startswith("/webhooks/"):
           return jsonify(error="An unexpected error occurred."), 500
       return render_template("errors/500.html"), 500
   ```

3. **Templates** — create `templates/errors/404.html` and `templates/errors/500.html`.
   Minimal but on-brand (uses the same base layout). No curly quotes.

**Acceptance tests** (add to `test_webhooks.py` or inline in a new `test_reliability.py`):
- `GET /nonexistent` returns 404 with an HTML response (not Werkzeug default).
- `GET /api/nonexistent` returns 404 JSON `{"error": "Not found."}`.

---

## PROPOSED 3-WAY PARTITION

### Slice A — Foundation (db.py + config.py + migrations)

**Files owned:** `db.py`, `config.py`, `messaging.py`  
**Work items:** All DB migrations, `biz_tz()`, `sms_status_callback_url()`, shared DB
functions (`set_forwarding_sentinel`, `set_google_event_id`, `queue_sms_retry`,
`find_scheduled_message`), `set_message_delivery` shim, delivery status badge in templates.

**Why here:** db.py and config.py are the collision files. Lock them here so B and C never
touch them.

**Tests to run:** `test_migration.py`, `test_scheduling.py`  
**Tests to add:** `test_timezone.py` (biz_tz unit tests), `test_delivery_receipts.py` (partial)

---

### Slice B — Calendar + Sentinel (google_cal.py + connections.py)

**Files owned:** `google_cal.py`, `connections.py`, `app.py` (3 targeted edits: sentinel
TwiML route, `twilio_voice_inbound` sentinel check, `cancel_appointment` route event cancel)

**Work items:** SF-5 Google timezone read, SF-7 sentinel call + probe, F04 all-day event parse fix,
cancel_event, create_event_and_store, `_slot_dt` tz threading.

**Tests to run:** `test_scheduling.py`, `test_webhooks.py`, `test_google_oauth.py`  
**Tests to add:** `test_google_cal.py` (all-day parse, event cancel), `test_forwarding.py`

---

### Slice C — Brain + Reminders (ai.py + reminders.py + app.py booking logic)

**Files owned:** `ai.py`, `reminders.py`, `app.py` (booking guard integration, first-turn
booking unify, RSVP classification, error handlers, logging setup)

**Work items:** F03 guards (turn cap, price guard, length guard, double-booking recovery),
F04 first-turn booking unify, F05 test_reminders.py + morning-of reminder + RSVP,
SF-4 retry logic in `run_due_once`, reliability error handlers.

**Tests to run:** `test_scheduling.py`, `test_webhooks.py`, `test_callback.py`  
**Tests to add:** `test_reminders.py` (the lie-fixing file), `test_booking_guards.py`

---

### Collision summary

| File | Touched by | Isolated to |
|---|---|---|
| `db.py` | All 3 items need new functions | Slice A |
| `config.py` | `biz_tz`, `sms_status_callback_url` | Slice A |
| `messaging.py` | SF-4 callback injection | Slice A |
| `app.py` | B (2 route edits), C (booking logic + error handlers) | Split by line range; B takes lines 1959-1978 (voice inbound) + 1495-1509 (cancel appt); C takes lines 1291-1357 (open_conv + handle_inbound) + error handlers at end |
| `google_cal.py` | SF-5 tz, F04 all-day, cancel event | Slice B |
| `reminders.py` | SF-4 retry, SF-5 tz threading, F05 | Slice C |
| `ai.py` | F03 guards | Slice C |
| `connections.py` | SF-7 sentinel | Slice B |

**app.py line-range split for B vs C:**
- Slice B edits: lines 1959 (voice inbound webhook), 1495 (cancel appt route)
- Slice C edits: lines 1291 (open_conversation), 1306 (handle_inbound), bottom of file
  (error handlers, logging import)
These ranges are non-overlapping. Merge order: Slice A first (migrations must land before
B/C run their tests), then B and C can merge in either order.

---

## TEST PLAN

### Existing tests to re-run after each slice

All slices: `test_migration.py` (migration integrity), `test_scheduling.py`, `test_webhooks.py`

| Slice | Also re-run |
|---|---|
| A | `test_config_hub.py`, `test_connect_hub.py` |
| B | `test_google_oauth.py`, `test_setup.py` |
| C | `test_callback.py`, `test_streaming.py`, `test_chaperone.py` |

Full suite after integration: all 28 existing files, then the new test files.

### New test files

| File | Slice | Tests (count est.) |
|---|---|---|
| `test_delivery_receipts.py` | A+C | 5-6 |
| `test_timezone.py` | A | 6-8 |
| `test_google_cal.py` | B | 5-6 |
| `test_forwarding.py` | B | 5-6 |
| `test_reminders.py` | C | 10-12 |
| `test_booking_guards.py` | C | 6-8 |
| `test_reliability.py` | C | 3-4 |

Run all as: `.venv/bin/python test_X.py` (NOT pytest).

---

## RISK LANE

The single most likely way a Sonnet build agent gets each item **wrong or dishonest**:

### SF-4 (delivery receipts + retry)
**Risk:** Agent implements retry by re-calling `messaging.send_sms` immediately in the
error handler rather than enqueueing to `scheduled_messages`. This produces an in-process
synchronous retry that (a) blocks the ticker thread, (b) bypasses the quiet-hours backstop,
and (c) can double-send if the original send actually succeeded but returned an error code
(Twilio occasionally returns 5xx on success). The only correct retry is an async re-enqueue
with a delay. Verify: confirm `run_due_once` has no synchronous retry loop; all retries are
new rows in `scheduled_messages`.

### SF-5 (per-business timezone)
**Risk:** Agent replaces `app_tz()` calls in `reminders.py` with `biz_tz(business_id)` but
forgets to handle DST transitions. Specifically, `compute_send_at` computes a local time,
converts to UTC — this is correct IF the tz object is a proper `ZoneInfo` (which handles DST).
The risk is the agent uses `pytz.timezone().localize()` (which exists in pytz but not
`zoneinfo`) or does naive arithmetic like `utc_offset = tz.utcoffset(datetime.now())` and
applies it statically. The project uses `zoneinfo` (already imported in messaging.py:116).
Verify: DST edge case test — 2026-03-08 01:30 AM EST (`-5`) should round to 2:00 AM, then
at 2:00 AM clocks spring forward to 3:00 AM — the test must use the IANA zone, not a fixed
offset.

### SF-7 (sentinel call verification)
**Risk:** Agent "confirms" forwarding by checking whether the sentinel call was "placed"
(Twilio accepted the API call), not whether it was RECEIVED as an inbound call. This is
exactly the same self-attestation bug we're fixing — the call might ring, the carrier might
not forward it, but the code marks `forwarding_confirmed=1` on `status == "placed"`. The
ONLY correct confirmation is in `twilio_voice_inbound` when the matching `CallSid` arrives.
Verify: the `setup_forwarding` route must NOT set `forwarding_confirmed=True` when Twilio
is configured — only the inbound webhook can.

### F04 (Google Calendar write-loop)
**Risk:** Agent adds `google_event_id` storage but doesn't handle the cancel path — when
an appointment is canceled (dashboard or by the AI rebook), the old Google event is never
deleted. Dave's calendar fills up with "ghost" estimates. Verify: the `cancel_appointment`
route (app.py:1495) must call `cancel_event_async` when `google_event_id` is set. Also
check: the all-day event fix must not break existing timed-event parsing (regression test
is critical here).

### F03 (booking-brain guards)
**Risk:** The price guard regex fires on legitimate replies containing numbers that aren't
prices — "I need about 3 rooms painted" contains "3" and might match a loose pattern. Or
the guard scrubs "the estimate is free" because it matches "estimate is". Build the regex
precisely (anchored to currency symbols and explicit dollar/buck words); run the test suite
against the existing demo conversations to ensure no false positive. Second risk: the turn
cap message is unhelpful for a real stuck conversation — it should offer a PHONE NUMBER
(from `business.phone`) so the customer can actually reach someone.

### F05 (test_reminders.py + morning-of)
**Risk:** Agent writes `test_reminders.py` but uses `import pytest` and `pytest.fixture`
instead of the project's standalone-script test pattern (`.venv/bin/python test_X.py`,
plain asserts, `sys.exit(1)` on failure). The NEXT-SESSION.md is explicit: "Tests run as
standalone scripts." Verify: `test_reminders.py` runs clean via `.venv/bin/python test_reminders.py`
and exits 0 with no pytest dependency. Second risk: the morning-of reminder sends before
the 24h reminder on same-day bookings — the `send_at` ordering must be checked (morning
reminder should only be enqueued if the estimate is tomorrow or later, otherwise it would
be before the existing 24h reminder).

### Reliability (errorhandler + logging)
**Risk:** Lowest risk item. The main failure mode is template syntax errors in the new
404/500 HTML templates — specifically using curly `"smart"` quotes in Jinja blocks
(`{{'error'}}` won't parse). The NEXT-SESSION.md warns explicitly: "smart quotes break
Jinja — never use curly quotes as `{% %}` / `{{ }}` string delimiters in templates."
Verify: render the 404 and 500 pages in the test by hitting a nonexistent route.

---

*Auditor B — written independently. Divergence from Auditor A's spec is the point; the
reconciler (Opus) decides which version of each seam wins.*
