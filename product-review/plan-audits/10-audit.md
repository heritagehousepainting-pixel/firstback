# Plan 10 Audit — New Lead-Source & Conversion Features

**Audited:** 2026-06-19
**Auditor:** read-only red-team pass against current code
**Plan file:** `product-review/plans/10-NEW-FEATURES.md`

---

## Verdict

**READY-WITH-FIXES**

The architecture is sound and all four features attach cleanly to real seams. Two features (Voicemail->Lead, Web-Chat Widget) can be built entirely without owner credentials and deployed as safe no-ops. Three correctible code-level issues must be resolved before a builder touches code: (1) `get_messages()` has no `direction=` parameter — the plan's no-double-greeting guard calls a non-existent signature; (2) `db.add_message(lead["id"], "vm_url", recording_url)` uses an undocumented direction-type that is neither "in" nor "out" — this will silently store a row but break any code that filters on direction; (3) the plan's claimed line refs for `open_conversation` (1731), `handle_inbound` (1789), `ai.generate_reply` (467), and `_missed_call_textback` (2580) are all off — actual lines are cited below. Deposit link and GBP dashboard are correctly classified as NEEDS-OWNER. No structural reworks required.

---

## Feature classification

| Feature | Classification | Dependency | Gating approach |
|---------|---------------|------------|-----------------|
| 1. Voicemail Transcription -> Lead | BUILD-NOW | Twilio creds + `PUBLIC_BASE_URL` — both already required for missed-call core flow | Gate on `VOICE_PUBLIC_URL` (already used at `app.py:1822`); no-op if unset. Recording webhook simply does not fire until `recordingStatusCallback` URL is wired into `<Dial>`. |
| 2. Web-Chat "Text Us" Widget | BUILD-NOW | `micro_site_slug` populated (set by setup wizard, `db.py:3588`); A2P must be approved | `compliance.a2p_ready(biz)` in `messaging.send_sms` already gates the outbound SMS (`messaging.py:140`). Widget endpoint is inert (no SMS fires) until A2P live. |
| 3. Deposit Link at Booking | NEEDS-OWNER | Owner must create a Stripe Payment Link in their own Stripe dashboard and paste the URL | Feature is zero-Stripe-API for MVP — just a stored URL field. Safe no-op when `deposit_link` is NULL/blank. |
| 4. GBP Review Dashboard | NEEDS-OWNER | Google re-auth required (scope upgrade forces re-consent); GBP My Business API must be enabled in GCP console | Gate on `google_gbp.configured() and google_gbp.is_connected(biz["id"])` — matches existing `google_cal.py` pattern. Dashboard tile simply doesn't render when disconnected. |

---

## Corrected anchors

| Item | Plan says | Reality (file:line + real code) | Action |
|------|-----------|--------------------------------|--------|
| `open_conversation` line | "line 1731" | `app.py:1732`: `def open_conversation(biz, lead):` | Off by one; use text match |
| `handle_inbound` line | "line 1789" | `app.py:1794`: `def handle_inbound(biz, lead, body):` | Off by five; use text match |
| `ai.generate_reply` line | "ai.py:467" | `ai.py:504`: `def generate_reply(business, history, exclude_slot_ids=None, lead_id=None):` — line 467 is inside `_PRICE_RE` regex | Wrong line by 37; use text match |
| `_missed_call_textback` line | "app.py:2580" | `app.py:2585`: `def _missed_call_textback(biz, caller, call_sid="", dial_status=""):` | Off by five; use text match |
| `/webhooks/twilio/voice/inbound` route | "app.py:2623" | `app.py:2643`: `@app.route("/webhooks/twilio/voice/inbound", methods=["POST"])` | Off by 20; use text match |
| `/webhooks/twilio/voice/dial-status` route | "app.py:2657" | `app.py:2679`: `@app.route("/webhooks/twilio/voice/dial-status", methods=["POST"])` | Off by 22; use text match |
| `alerts.notify_async(biz, "booking", ctx)` | "app.py:1887" | `app.py:1892` (handle_inbound booking path) / `app.py:1776` (open_conversation booking path) | Off; two sites — use text match on both |
| `compliance.a2p_ready(business)` gate | "messaging.py:140" | `messaging.py:140`: `if gate and configured() and not compliance.a2p_ready(business):` | **Exact match** — confirmed |
| `google_cal.is_connected(business_id)` | "google_cal.py:42" | `google_cal.py:42`: `def is_connected(business_id):` | **Exact match** — confirmed |
| `db.get_integration(business_id, "google")` | "google_cal.py:44" | `google_cal.py:44`: `intg = db.get_integration(business_id, "google")` | **Exact match** — confirmed |
| `db.create_lead` | "db.py:1313" | `db.py:1313`: `def create_lead(business_id, name, phone, source="missed_call"):` | **Exact match** — confirmed |
| `db.update_business` | "db.py:1050" | `db.py:1050`: `def update_business(business_id, fields):` | **Exact match** — confirmed |
| `_BUSINESS_COLS` definition | "db.py:20-26" | `db.py:20`: `_BUSINESS_COLS = ["name", "trade", ...]` ends `db.py:26` | **Exact match** — confirmed |
| `db.book_appointment` | "db.py:1476" | `db.py:1476`: `def book_appointment(business_id, lead_id, scheduled_for, ...` | **Exact match** — confirmed |
| `messaging.send_sms` | "messaging.py:77" | `messaging.py:77`: `def send_sms(business, to, body, lead_id=None, ...)` | **Exact match** — confirmed |
| `messaging.place_call` | "messaging.py:210" | `messaging.py:210`: `def place_call(business, to, twiml_url, ...)` | **Exact match** — confirmed |
| `billing.py` + Stripe SDK | "billing.py:1" | `billing.py:1`: module docstring; `import stripe` at line 19; `STRIPE_SECRET_KEY` at line 26 | Confirmed, Stripe SDK imported and key gated |
| Google `SCOPES` constant | "google_cal.py — SCOPES constant" | `google_cal.py:33`: `SCOPES = ("https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly")` — currently TWO scopes only | Confirmed — `business.manage` scope is NOT present; must be added as the plan specifies |

---

## Blockers (must fix before building)

### B1 — `get_messages()` has no `direction=` parameter (Feature 1, no-double-greeting guard)

Plan (Feature 1, webhook code):
```python
if not db.get_messages(lead["id"], direction="out"):
    reply = open_conversation(biz, lead)
```

Actual `db.py:1466`:
```python
def get_messages(lead_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE lead_id=? ORDER BY id", (lead_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

`get_messages` takes only `lead_id`. Calling it with `direction="out"` will raise `TypeError: get_messages() got an unexpected keyword argument 'direction'` at runtime.

**Fix:** Either (a) add an optional `direction=` filter to `get_messages`, or (b) inline-filter in the webhook: `if not any(m["direction"] == "out" for m in db.get_messages(lead["id"])):`. Option (b) requires no DB change. The same pattern exists already in `_missed_call_textback` at `app.py:2631`: `if not db.get_messages(lead["id"]):` — which checks for ANY message, not just outbound. The voicemail webhook needs outbound-only, so simple option (b) is correct.

---

### B2 — `vm_url` direction-type is non-standard (Feature 1)

Plan:
```python
db.add_message(lead["id"], "vm_url", recording_url)  # store for UI
```

All existing `add_message` calls use `direction` values of `"in"` or `"out"`. Direction is a plain TEXT column with no CHECK constraint, so `"vm_url"` stores without error — but any existing UI code, digest query, or export that filters `direction IN ('in','out')` will silently skip it. The `_missed_call_textback` outbound check above (`db.get_messages` any-message guard) would incorrectly treat this row as "has messages" and suppress an open_conversation call.

**Fix:** Store the recording URL as a column on the message row, not as a separate direction-type row. The plan already proposes adding `recording_url TEXT` to the messages table (Migration C). The cleaner pattern: when injecting the transcript with `db.add_message(lead["id"], "in", f"[Voicemail] {transcript}")`, also set `recording_url` on that same row (requires a `recording_url=` parameter to `add_message` or a follow-up UPDATE). Alternatively, add `recording_url TEXT` to `messages` and update the insert in `add_message` to accept it.

---

### B3 — Deposit link hook location in `handle_inbound` is misdescribed (Feature 3)

Plan says the deposit suffix should be appended at "line 1857, after `booked = booking` is set." Actual code at `app.py:1853–1863`:

```python
db.add_message(lead_id, "out", reply)   # line 1853 — reply already recorded here
booked = None
if booking:
    gday, gtime = db.parse_day(booking), db.time_key(booking)
    prior = db.lead_booked_appointments(biz["id"], lead_id)
    if any(a.get("day") == gday ...):   # line 1858 — re-confirmation path
        booked = booking
    elif db.book_appointment(biz["id"], lead_id, booking):  # line 1862 — new booking
        booked = booking
```

`reply` is already written to DB at line 1853 — before the booking confirmation. If the deposit suffix is appended AFTER `db.book_appointment` succeeds, the plan is correct that the outbound message row must be updated/re-written, as it notes: "the reply is already recorded in `db.add_message` BEFORE the deposit suffix; the suffix replaces/updates the last outbound row." The plan's "cleaner alternative" (pass `deposit_suffix` into `open_conversation` and append before `db.add_message`) is the right approach for both code paths. A double-write-then-update pattern risks leaving a gap if the process crashes between the two DB calls.

**Fix:** The builder should use the cleaner path: compute `deposit_suffix` before the `db.add_message(lead_id, "out", reply)` call at `app.py:1853`, concatenate it into `reply` if `biz.get("deposit_link")` is set, then call `db.add_message` once with the combined text. This requires knowing `booked` status before the write — restructure the booking block to compute `booked` first, then write the augmented reply.

---

### B4 — `GBP_API_BASE` URL in plan is wrong (Feature 4)

Plan:
```python
GBP_API_BASE = "https://mybusinessbusiness.googleapis.com/v1"
```

The correct Google My Business API v5 base URL is `https://mybusiness.googleapis.com/v4` (or `/v1` on the Business Profile API). The double-word `mybusinessbusiness` is a typo that will produce 404s on every GBP API call. The current GBP Reviews endpoint per Google's documentation is `https://mybusiness.googleapis.com/v4/accounts/{accountId}/locations/{locationId}/reviews`.

**Fix:** Builder must verify the current GBP REST API base URL against Google's API Explorer before implementation. The plan's module code should not be copied verbatim.

---

## Migrations

### Feature 1 — `messages.recording_url` (ADD COLUMN)

```python
# db.py init_db():
msg_cols = [r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()]
if "recording_url" not in msg_cols:
    c.execute("ALTER TABLE messages ADD COLUMN recording_url TEXT")
```

Safe: `ALTER TABLE ... ADD COLUMN` only, idempotent guard present, no DEFAULT needed (NULL is correct for non-voicemail rows). No existing rows affected.

### Feature 3 — `businesses.deposit_link`, `businesses.deposit_amount` (ADD COLUMN x2)

```python
# db.py init_db():
biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
if "deposit_link" not in biz_cols:
    c.execute("ALTER TABLE businesses ADD COLUMN deposit_link TEXT")
if "deposit_amount" not in biz_cols:
    c.execute("ALTER TABLE businesses ADD COLUMN deposit_amount TEXT")
```

Safe: both ADD COLUMN only, NULL default is correct (no deposit = feature off). Must also add both to `_BUSINESS_COLS` at `db.py:20–26` so the Settings form saves and the seed INSERT includes them.

### Feature 4 — `gbp_reviews` table + index (CREATE TABLE IF NOT EXISTS)

```sql
CREATE TABLE IF NOT EXISTS gbp_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id     INTEGER NOT NULL,
    review_id       TEXT NOT NULL,
    reviewer_name   TEXT,
    star_rating     INTEGER,
    comment         TEXT,
    create_time     TEXT,
    update_time     TEXT,
    reply_text      TEXT,
    draft_response  TEXT,
    responded_at    TEXT,
    synced_at       TEXT,
    UNIQUE(business_id, review_id)
);
CREATE INDEX IF NOT EXISTS idx_gbp_reviews_biz ON gbp_reviews(business_id, create_time);
```

Safe: `CREATE TABLE IF NOT EXISTS` is idempotent. No existing table to conflict with (confirmed: no `gbp_reviews` references anywhere in `db.py`). New table only; no columns dropped or type-changed.

### Feature 2 — optional `widget_enabled` column

Plan marks this optional for MVP. If added, it is `ALTER TABLE businesses ADD COLUMN widget_enabled INTEGER DEFAULT 0` — safe, idempotent with PRAGMA guard. Default 0 matches the plan's "opt-in" stance.

**Summary: all four migrations are ADD-only. None modify existing columns, rename tables, or require data backfills. All safe for zero-downtime deploy on SQLite.**

---

## Notes

### N1 — Smart-quote risk: plan uses `'` apostrophe in plain-text prose only

Scanned all plan code blocks. All string literals use ASCII single quotes (`'`). No curly/smart quotes detected in code samples. Low risk, but builder should paste code into an editor that renders fonts faithfully before committing.

### N2 — `google_gbp.py` does not exist yet

The plan correctly calls it a "New module." No existing `google_gbp.py` in the repo. Builder creates it fresh. The shape (mirrors `google_cal.py`: `configured()`, `is_connected()`, token refresh, defensive `try/except`) is well-specified. The `is_connected` passthrough to `google_cal.is_connected` is correct — both share the same OAuth grant row in the `integrations` table.

### N3 — Re-auth impact on existing Google Calendar tenants

Adding `business.manage` to `SCOPES` in `google_cal.py` will invalidate existing OAuth tokens on next access (Google requires re-consent for new scopes). The `prompt="consent"` at `google_cal.py:57` ensures re-auth works without code changes, but existing tenants will see a Google consent screen on their next Calendar sync. Coordinate the scope bump with a user communication or a conditional: only add `business.manage` when GBP feature is enabled per-tenant.

### N4 — Widget: `_WIDGET_RATE` in-memory dict is process-scoped

Plan correctly notes this is "in-memory, same pattern as login limiter." On Render, multiple workers mean rate limiting is per-process, not per-deployment. The login limiter (`_LOGIN_FAILURES` at `app.py:357`) has the same limitation and is accepted. Acceptable for MVP — consistent with existing practice.

### N5 — Voicemail: Twilio free transcription latency vs. missed-call text race

Twilio's free basic transcription delivers `TranscriptionText` 30–60s after the call ends. The missed-call text (`_missed_call_textback`) fires within seconds of `dial-status`. The plan's no-double-greeting guard (the `direction="out"` check in the recording webhook) correctly handles this: the outbound message will already exist from the missed-call text-back, so `open_conversation` is skipped. The transcript is injected as an inbound message for context. This is the right sequence.

### N6 — Feature build order: plan's recommendation is correct

1 (Voicemail) first: S-effort, zero new UI, zero new credentials, attaches to existing seams exactly. 3 (Deposit link) second: S-effort, pure URL storage + template, immediate no-show value. 4 (GBP dashboard) third: M-effort, new module + new table, compounds with retention work. 2 (Widget) last: M-effort, most moving parts (anti-abuse, CORS JS, new public endpoint, A2P compliance for new source). This order correctly sequences by risk and dependency.
