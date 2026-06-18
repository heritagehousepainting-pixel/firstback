# PREBUILD-3 — Data Model, Integration Seams, Shared Seams & Collision Map
## Phase 4: Convert & Prove — Pre-Build Planning Report

**Lane:** Data Model + Integration + Shared Seams + Collision Map
**Date:** 2026-06-18
**Basis:** AUTONOMY-BLUEPRINT.md §2–§4, F12-FINAL.md, F05/F06/F09-FINAL, live code reads

---

## 1. DATA MODEL — What Phase 4 Needs

### 1.1 Derived vs. Stored Analysis

**Can be DERIVED (no new columns needed):**
- `calls_recovered_n` (V1 proxy): `COUNT(*) FROM leads WHERE business_id=? AND source='missed_call'` — already in `db.analytics()` at `db.py:2513`
- `roi_multiple`: `revenue / PLAN_COST_MONTHLY` — computed at query time; never stored
- `conversion`: `booked_n / leads_n × 100` — already computed at `db.py:2543`
- `cost_per_booked_job`: `PLAN_COST_MONTHLY / booked_n` — derived
- Trade-based job value default: read from `businesses.trade` (exists, `db.py:215`) + in-memory `TRADE_JOB_VALUE_DEFAULTS` dict — no new column
- `avg_source` label (`ESTIMATE (industry default)` vs `ESTIMATE (your avg)` vs `ACTUAL`): derived from `avg_job_value` null-check + `trade` value; no new column

**Must be STORED (idempotency / state):**

### 1.2 New Columns — businesses table (guarded `if col not in cols` pattern, `db.py:689+`)

```python
# Phase 4 — ROI milestone idempotency
("roi_milestone_sent_at", "TEXT"),         # ISO ts of last milestone SMS; NULL = never sent
# Phase 4 — Dispatcher Call log (avoid double-calling per urgency event)
("dispatcher_call_last_at", "TEXT"),       # ISO ts of last outbound dispatcher call
# Phase 4 — Day-3 job value prompt dismissal (separate from chaperone_dismissed_at)
("job_value_prompt_dismissed_at", "TEXT"), # NULL = show prompt; ISO ts = user dismissed
```

All three added in one migration block, following the existing pattern at `db.py:689`:
```python
biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
for col, ddl in (
        ("roi_milestone_sent_at",         "TEXT"),
        ("dispatcher_call_last_at",       "TEXT"),
        ("job_value_prompt_dismissed_at", "TEXT")):
    if col not in biz_cols:
        c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")
```

### 1.3 New Columns — appointments table

None required for Phase 4 core. `closed_at` / `actual_value` (L9, "mark as won") is explicitly deferred to a later tier.

### 1.4 New Tables

None. Dispatcher Call log is the per-business timestamp on `businesses` (see above), not a separate table — one call per urgency event, dedupe within 60s via existing `ALERT_DEDUPE_SECONDS` pattern.

### 1.5 New Constants in config.py

```python
# Phase 4 — ROI plan cost (single source; never hardcode $99 inline)
PLAN_COST_MONTHLY = int(os.environ.get("FIRSTBACK_PLAN_COST", "99"))

# Phase 4 — Trade-based job value defaults (in db.py or config.py; see §2.1)
TRADE_JOB_VALUE_DEFAULTS = {
    "plumbing":   850,
    "hvac":      1200,
    "electrical":  900,
    "roofing":   4500,
    "painting":  2800,
    "landscaping": 650,
    "general":     900,
    "default":     800,
}
```

**Placement decision:** `TRADE_JOB_VALUE_DEFAULTS` belongs in `db.py` at the top (alongside `_BUSINESS_COLS`) because `db.analytics()` is the only consumer and placing it in `config.py` creates a circular-import risk (config is imported by db, so db importing a constant FROM config for a dict that config doesn't own is odd). The `PLAN_COST_MONTHLY` constant goes in `config.py` because it's a business/billing parameter, not DB-layer logic.

### 1.6 "Calls Recovered" + "Earned $" Computation (Precise Definition)

**V1 — proxy (ships in Phase 4):**
```sql
-- calls_recovered_n (V1 proxy): all missed-call-sourced leads (already in db.analytics())
SELECT COUNT(*) FROM leads
WHERE business_id = ? AND source = 'missed_call'
[AND created_at >= cutoff]  -- if days window

-- revenue (booked pipeline estimate):
booked_n * avg_job_value
-- where avg_job_value = businesses.avg_job_value if set and >0,
--       else TRADE_JOB_VALUE_DEFAULTS[businesses.trade or "default"]

-- ROI multiple:
revenue / PLAN_COST_MONTHLY  -- where PLAN_COST_MONTHLY = config.PLAN_COST_MONTHLY
```

**V2 — precise (M8, deferred; not Phase 4):**
```sql
-- "calls recovered" = missed calls that produced a lead within 5 minutes
SELECT COUNT(DISTINCT c.id)
FROM calls c
JOIN leads l ON l.phone = c.from_number
    AND l.business_id = c.business_id
    AND l.source = 'missed_call'
    AND (strftime('%s', l.created_at) - strftime('%s', c.created_at)) BETWEEN 0 AND 300
WHERE c.business_id = ? AND c.missed = 1
[AND c.created_at >= cutoff]
```

Note: this V2 query requires the join path `calls.from_number → leads.phone`. Both columns confirmed: `calls.from_number` at `db.py:252`, `leads.phone` at `db.py:225`. The join does NOT need `calls.lead_id` (which may be NULL for pre-Phase-4 rows).

**Milestone SMS guard logic:**
```python
def check_roi_milestone(biz_id):
    biz = db.get_business(biz_id)
    a = db.analytics(biz_id, 30)          # current 30-day window
    revenue = a["totals"]["revenue"]
    booked_n = a["totals"]["booked"]
    if not revenue or booked_n < 1:
        return False
    roi_multiple = revenue / config.PLAN_COST_MONTHLY
    # Threshold: 1.0x if Dave set his own job value; 2.0x if using trade default
    using_default = biz.get("avg_job_value") in (None, "", 0)
    threshold = 2.0 if using_default else 1.0
    if roi_multiple < threshold:
        return False
    # Idempotency: one SMS per 30-day window
    last_sent = biz.get("roi_milestone_sent_at")
    if last_sent:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        if last_sent >= cutoff:
            return False
    # First 72 hours: suppress (no signal yet)
    created = db.owner_email_row(biz_id)  # use users.created_at as account age proxy
    # ... (72h guard: check first user's created_at)
    # Fire
    alerts.notify(biz, "roi_milestone", {"revenue": revenue, "roi_multiple": roi_multiple,
                                          "booked_n": booked_n, "plan_cost": config.PLAN_COST_MONTHLY})
    db.set_roi_milestone_sent(biz_id, datetime.now(timezone.utc).isoformat())
    return True
```

New DB helper: `db.set_roi_milestone_sent(biz_id, ts)` — a simple UPDATE to `businesses.roi_milestone_sent_at`.

---

## 2. INTEGRATION SEAMS — EXTEND vs BUILD-NEW

### 2.1 Weekly Digest — EXTEND `convos.digest_email()` (`convos.py:298`)

**Verdict: EXTEND (15 lines delta)**

The function exists, is per-tenant (`business` dict passed in), and is called from both `/digest/send` (`app.py:955`) and `/tasks/digest` (`app.py:960`). The cron infrastructure is live.

**Delta:**
1. At top of `digest_email()` (after `bid = business["id"]`): call `a = db.analytics(bid, 7)` and `a_totals = a["totals"]`
2. Prepend a ROI block to `lines` before the Vic-gap content:
   ```
   "ROI block: N calls recovered, M estimates booked — ~$X pipeline vs your $99/mo. [Nx ROI]"
   ```
3. Guard: if `a_totals["leads"] == 0 and a_totals["booked"] == 0`: substitute "Quiet week — FirstBack is ready for your next missed call." — suppress ROI block, not whole email.
4. Add UTM param `?utm_source=digest&utm_medium=email` to the analytics link.

**Risk:** `db.analytics()` called per-tenant in the cron loop. For 100 tenants this is 100 DB reads in a serial loop — acceptable for now; add background-thread fan-out when tenant count warrants.

**Existing tests at risk:** None directly — `test_streaming.py` covers the Vic stream path. The digest email has no current test. New: `test_phase4_roi.py::test_digest_email_roi_block`.

### 2.2 Show-Up-Prepared Briefing — EXTEND `alerts.notify()` + `alerts.format_message()` (`alerts.py:47,101`)

**Verdict: EXTEND (the booking alert already fires; extend its copy + wire the structured briefing)**

The booking alert is already live at `alerts.py:58-62`:
```python
if kind == "booking":
    when = (context.get("when") or "").strip()
    return f"Estimate booked: {who} for {when}.{tail}".rstrip()
```

This is generic. The "Show-Up-Prepared" briefing (F09-FINAL §3: "name, address, project, personal detail") requires the lead's `summary` and `address` from `leads.summary` / `leads.address` (both exist, added in migration at `db.py:439`).

**Delta:**
1. In `alerts.format_message()` (`alerts.py:47`): for `kind == "booking"`, extract `context.get("address")`, `context.get("project_type")`, `context.get("summary")` (pre-computed by `_ensure_lead_notes` via `ai.summarize_lead`).
2. Build structured briefing SMS (≤160 chars): `"Estimate booked: {who} — {project_type} at {address}. {when}. {summary[:40]}"`
3. At the booking call sites in `app.py` (`app.py:1443`, `app.py:1477`): enrich the context dict with `address`, `project_type`, `summary` from `db.get_lead(lead_id)` (or inline from the already-fetched lead dict).

**Note:** `_ensure_lead_notes` runs async off the hot path. The booking alert fires immediately (`notify_async`). On the FIRST booking, summary may not yet be computed. Two options:
- Option A: `notify_async` fires immediately with whatever is in the lead; summary arrives when notes finish (best UX, slightly incomplete first alert).
- Option B: Delay the booking alert 3s to let notes compute. **Reject** — adds latency, fragile.
- **Decision:** Option A. If `summary` is empty, format without it. No guard needed — `context.get("summary")` returns `""` gracefully.

**Existing tests at risk:** `test_alert_channel.py` — tests `format_message` pure function; adding new fields to the booking branch must not break existing assertions that check the old format. New test: `test_phase4_roi.py::test_show_up_prepared_format`.

### 2.3 Dispatcher Call — BUILD-NEW TwiML endpoint + wire to `messaging.place_call()`

**Verdict: BUILD-NEW one endpoint; `messaging.place_call()` (`messaging.py:209`) already exists**

`place_call()` is production-ready: it calls Twilio, passes a TwiML URL, returns `{"status": "placed"|"simulated"|"error"}`. It is already wired once at `app.py:2238` for the AI voice callback. The Dispatcher Call needs:

**New items:**
1. **`/twiml/dispatcher` endpoint in `app.py`** (new route, ~20 lines):
   ```python
   @app.route("/twiml/dispatcher", methods=["GET", "POST"])
   def twiml_dispatcher():
       """TwiML for the outbound Dispatcher Call: reads the caller's exact words to Dave,
       then bridges Dave to the caller. Query params: biz=<id>, lead=<id>"""
       biz_id = request.args.get("biz", type=int)
       lead_id = request.args.get("lead", type=int)
       caller_number = ...  # from leads.phone
       caller_words = ...   # from leads.summary or last inbound message body
       # TwiML: <Say> the caller's words, <Dial> the caller's number
       # Press 1 to connect / press 2 to hang up (AMD: if voicemail detected, leave message)
   ```

2. **TwiML structure (no external TTS needed — Twilio's built-in voice):**
   ```xml
   <Response>
     <Say>FirstBack alert. {caller_name} says: "{caller_words}". Press 1 to connect now.</Say>
     <Gather numDigits="1" action="/twiml/dispatcher/connect?lead={lead_id}">
       <Say>Press 1 to connect.</Say>
     </Gather>
     <Say>No response. Hanging up. Check FirstBack for the lead.</Say>
   </Response>
   ```

3. **`/twiml/dispatcher/connect` endpoint** (~10 lines): `<Dial>` the caller's number.

4. **AMD handling:** Pass `MachineDetection=Enable` and `AsyncAmdStatusCallback=/webhooks/voice/amd` to `place_call()`. On voicemail detected: leave a message instead of bridging. New webhook handler needed.

5. **Trigger point in `app.py`** — wire to the urgency path. In `handle_inbound()` (`app.py:1433`), when `urgent=True` fires, after `alerts.notify_async(biz, "urgent", ...)`: check `biz.get("forward_to")` (Dave's cell), call `messaging.place_call()` with the dispatcher TwiML URL, log to `businesses.dispatcher_call_last_at`. Dedupe: skip if `dispatcher_call_last_at` is within 60s (same urgency event can double-trigger).

6. **`db.set_dispatcher_call_sent(biz_id, ts)`** — new helper, UPDATE `businesses.dispatcher_call_last_at`.

**Existing tests at risk:** `test_voice.py` — tests the voice/TwiML path; the new dispatcher endpoint lives alongside but may share TwiML helpers. `test_f03_brain.py` — tests urgency detection; unaffected (urgency detection itself unchanged).

### 2.4 Milestone SMS — EXTEND `alerts.py` + wire to post-booking in `app.py`

**Verdict: EXTEND (new `kind="roi_milestone"` in alerts + `check_roi_milestone()` function in new `roi.py`)**

The alert channel (`alerts.notify()`) already fans out to SMS + in-app + email with de-dupe. Rather than building a standalone SMS sender, the milestone SMS rides `alerts.notify()` as a new kind `"roi_milestone"`.

**Delta:**
1. Add `"roi_milestone"` to `ALERT_KINDS` in `alerts.py:30`.
2. Add `"roi_milestone": "alert_on_booking"` to `_TOGGLE_COL` (rides the booking toggle — if Dave turned off booking alerts he'd also suppress this, which is correct).
3. Add `format_message("roi_milestone", context)` branch: `"FirstBack update: {booked_n} estimates booked this month — ~${revenue:,} in pipeline vs your $99/mo. That's {roi_multiple:.0f}×. [Details: link]"` (~155 chars).
4. Add `_subject("roi_milestone")`: `"FirstBack paid for itself — ROI alert"`.
5. **`check_roi_milestone(biz_id)`** — place in new `roi.py` (see §3 collision map), called from `app.py` post-booking hook (after `db.book_appointment()` succeeds, `app.py:1464`).

**Suppression guard:** `roi_milestone_sent_at` on businesses; within-30-days check in `check_roi_milestone()`.

### 2.5 ROI block in `db.analytics()` — EXTEND (S1/S2 from F12-FINAL)

**Verdict: EXTEND**

`db.analytics()` at `db.py:2500` returns `{"totals": {...}, "series": [...], "avg_job_value": avg, "days": days}`. Add:
- `avg_source`: `"owner"` if `businesses.avg_job_value` set and valid, else `"industry_estimate"`
- `using_default_job_value`: bool
- `plan_cost`: `config.PLAN_COST_MONTHLY`
- `roi_multiple`: `round(revenue / plan_cost, 1) if revenue else None`
- `cost_per_booked_job`: `round(plan_cost / booked_n, 2) if booked_n else None`
- `by_source`: dict — add `GROUP BY source` to the leads query

Replace `avg = None` fallback (`db.py:2507`) with:
```python
if biz.get("avg_job_value") not in (None, "", 0) and float(biz["avg_job_value"]) > 0:
    avg = float(biz["avg_job_value"])
    avg_source = "owner"
else:
    trade = (biz.get("trade") or "").lower().strip()
    avg = TRADE_JOB_VALUE_DEFAULTS.get(trade) or TRADE_JOB_VALUE_DEFAULTS["default"]
    avg_source = "industry_estimate"
```

### 2.6 `/tasks/digest` cron wiring — NO CHANGE NEEDED

`tasks_digest()` (`app.py:960`) already iterates all tenants and calls `convos.digest_email()`. Extending `digest_email()` (§2.1) means the ROI block automatically reaches every tenant on the next cron run. No route changes needed.

---

## 3. SHARED SEAMS — Symbols Multiple Agents Touch

The following symbols are the "hot wires" — changing them risks breaking other agents' work:

| Symbol | File:Line | Phase 4 change | Risk |
|--------|-----------|----------------|------|
| `db.analytics()` | `db.py:2500` | Extend return dict (additive) | Low — additive only; existing keys unchanged |
| `convos.digest_email()` | `convos.py:298` | Prepend ROI block | Medium — existing test (none) + callers at `app.py:955,970` |
| `alerts.format_message()` | `alerts.py:47` | Extend `"booking"` branch + new `"roi_milestone"` kind | Medium — existing tests check booking format |
| `alerts.ALERT_KINDS` | `alerts.py:30` | Add `"roi_milestone"` | Low — additive |
| `alerts._TOGGLE_COL` | `alerts.py:37` | Add `"roi_milestone"` key | Low |
| `handle_inbound()` | `app.py:1430` | Add dispatcher call trigger + `check_roi_milestone()` call | High — core message loop; urgent path + booking path |
| `db.init_db()` | `db.py:195` | Add 3 migration columns | Medium — must use guarded pattern; migrations must be idempotent |
| `config.py` | — | Add `PLAN_COST_MONTHLY` + import it in `db.py` | Low — config is already imported by db |
| `TRADE_JOB_VALUE_DEFAULTS` | `db.py` (new, top) | New dict | Low — new symbol |

---

## 4. COLLISION MAP

### 4.1 High-collision files for Phase 4

| File | Why it collides | Which agents touch it |
|------|-----------------|----------------------|
| `db.py` | `analytics()` extension + migration columns + `TRADE_JOB_VALUE_DEFAULTS` + new helpers | All 3 build agents |
| `app.py` | `handle_inbound()` (dispatcher + ROI milestone) + new `/twiml/dispatcher*` routes | Agents A & C |
| `alerts.py` | New kind + format_message extension | Agents A & B |
| `convos.py` | `digest_email()` ROI block | Agent B only |
| `config.py` | `PLAN_COST_MONTHLY` constant | Agent B (db.analytics) |

### 4.2 Proposed 3-Way File-Disjoint Partition

**Agent A — ROI Data Layer + Milestone**
Owns: `db.py` (analytics extension + migrations + new helpers), new `roi.py` (check_roi_milestone, TRADE_JOB_VALUE_DEFAULTS lives here or in db.py), `config.py` (PLAN_COST_MONTHLY constant addition)
Test file: `test_phase4_roi.py`

**Agent B — Digest + Briefing Copy**
Owns: `convos.py` (digest_email ROI block), `alerts.py` (format_message booking extension + roi_milestone kind), `analytics.html` (headline tile + three-state label UI)
Test file: `test_phase4_digest.py`

**Agent C — Dispatcher Call + TwiML**
Owns: `app.py` (new `/twiml/dispatcher` + `/twiml/dispatcher/connect` + AMD webhook, wiring in `handle_inbound()` urgency path, `check_roi_milestone()` call in booking path)
Test file: `test_phase4_dispatcher.py`

**Critical seam:** `app.py:1464` (post-`db.book_appointment()` success block) is touched by Agent C (milestone trigger) only. Agent C must not touch `db.py`. Agent A must not touch `app.py`. Agent B must not touch either. **db.py is Agent A's exclusive file.**

### 4.3 Ordering Constraint

Agent A must write its DB helpers (`set_roi_milestone_sent`, `set_dispatcher_call_sent`, extended `analytics()`) **before** Agents B and C reference them. Agent A's work is a prerequisite. Suggested order: A first, then B and C in parallel.

---

## 5. TEST PLAN SKETCH

### New test files (one per build agent):

**`test_phase4_roi.py`** (Agent A's test file)
- `test_trade_defaults_plumbing()` — analytics() returns industry_estimate when avg_job_value unset, trade='plumbing' → 850
- `test_trade_defaults_blank_trade()` — returns 800 all-trades floor
- `test_roi_multiple_computed()` — `revenue / PLAN_COST_MONTHLY` correct
- `test_avg_source_owner()` — avg_source='owner' when avg_job_value set and >0
- `test_avg_source_industry_estimate()` — when unset
- `test_avg_job_value_zero_treated_as_unset()` — guard for <=0
- `test_milestone_idempotent_within_30_days()` — second call in 30d returns False
- `test_milestone_suppressed_first_72h()` — account <72h old, no SMS
- `test_milestone_fires_at_threshold_owner_value()` — roi >= 1.0 with owner value
- `test_milestone_fires_at_threshold_default_value()` — roi >= 2.0 with trade default
- `test_by_source_attribution_split()` — analytics() groups missed_call vs manual
- `test_migration_idempotent()` — runs init_db() twice, no error

**`test_phase4_digest.py`** (Agent B's test file)
- `test_digest_email_has_roi_block()` — digest_email() output contains leads/booked/pipeline
- `test_digest_email_quiet_week_no_roi_block()` — 0 leads 0 booked → quiet message
- `test_digest_email_subject_unchanged_structure()` — subject format
- `test_booking_alert_show_up_prepared()` — format_message("booking", ctx_with_address)
- `test_booking_alert_no_address()` — format_message("booking", ctx_without_address) — graceful
- `test_roi_milestone_alert_format()` — format_message("roi_milestone", ctx) ≤ 160 chars
- `test_roi_milestone_kind_in_alert_kinds()` — ALERT_KINDS contains "roi_milestone"

**`test_phase4_dispatcher.py`** (Agent C's test file)
- `test_dispatcher_twiml_returns_valid_xml()` — `/twiml/dispatcher?biz=1&lead=1` returns TwiML with Say + Gather
- `test_dispatcher_deduped_within_60s()` — second trigger within 60s returns early
- `test_dispatcher_no_forward_to()` — no forward_to on business → skips call
- `test_dispatcher_place_call_simulated_when_unconfigured()` — Twilio not configured → "simulated"
- `test_dispatcher_connect_twiml()` — `/twiml/dispatcher/connect` returns `<Dial>`
- `test_amd_voicemail_handling()` — AMD callback with `AnsweredBy=machine_start` → no bridge

### Existing tests at risk:

| Test file | Risk | Why |
|-----------|------|-----|
| `test_alert_channel.py` | Medium | `format_message("booking", ...)` branch extended; existing assertions on booking format string must still match or be updated |
| `test_sf4_db.py` | Low | `db.analytics()` return dict gains new keys; tests checking exact keys will fail if they assert `==` rather than `in` |
| `test_assistant.py` | Low | `briefing()` calls `_compose_briefing()` which calls `_job_value()`. No change to briefing logic; but if analytics() changes avg_job_value fallback, briefing money line changes. Needs audit. |
| `test_migration.py` | Low | New columns in init_db() must not break the migration idempotency test. |
| `test_voice.py` | Low | New dispatcher TwiML routes live alongside existing voice routes; no changes to voice_service.py. |

---

## 6. GAPS — Where Blueprint / F12 Underspecifies

### Gap 1: Dispatcher Call — caller words source (HIGH PRIORITY)
F09/F12 say "reads the caller's exact words" but don't specify the source. Options:
- A: `leads.summary` (computed by `ai.summarize_lead`) — clean but may be empty at fire time (async)
- B: Last inbound `messages.body` — always synchronously available; less polished
- **Recommendation:** Last inbound message body as primary; fall back to `leads.summary` if available. Add `db.get_last_inbound_message(lead_id)` helper (2-line query).

### Gap 2: Dispatcher Call — Dave's number source
`businesses.forward_to` is Dave's cell (set during go-live). But `forward_to` is a Twilio forwarding target, not necessarily his personal cell. If he configured a different forward_to (e.g., a landline), calling it doesn't reach him.
- **Recommendation:** Use `businesses.alert_sms` as the Dispatcher Call destination (it IS set from signup phone, and is the same SMS we already text him on). If `alert_sms` is empty, fall back to `forward_to`. Document this in the code.

### Gap 3: Milestone SMS — ALERT_FROM_NUMBER vs business twilio_number
The alert channel at `alerts.py:117` uses `gate=False` which bypasses A2P and uses `ALERT_FROM_NUMBER`. The milestone SMS is an owner-facing message (not customer-facing), so it correctly rides this path. **This is fine and consistent.** No gap — but build agents should know it uses the platform number, not Dave's business number.

### Gap 4: "Show-Up-Prepared" as a push alert vs. a format change
The blueprint §2 calls it a "structured booking alert" — but it's never specified whether this is:
- A: A format change to the existing booking alert SMS (extend `format_message("booking", ...)`)
- B: A *second* message sent separately (a dedicated "prep briefing" text)
**F09-FINAL §3 says "structured" booking alert, implying format change.** The blueprint says "build the data path once, reused everywhere." Verdict: Option A — extend the format. A second SMS within seconds of booking would be confusing. **Note this in build spec.**

### Gap 5: Month-over-month analytics (M6) — 14-day minimum signal
F12 specifies "only show delta after 14+ days" but doesn't define what "show" means on day 1-14. Build agents should: suppress the delta tiles (not show `null` or "—") and show the raw current-period numbers only. The `db.analytics_compare()` function should return `delta = None` when current period < 14 days, not zero.

### Gap 6: Digest ROI block — which `days` window?
`digest_email()` is called weekly (7-day cadence). F12 S3 says "call `db.analytics(bid, 7)`". But the existing `convos.digest()` already computes a 7-day window. To avoid two DB reads, the 7-day analytics call can be shared. **Build agent B should merge: compute analytics(bid, 7) once, use it for both the Vic-gap digest and the ROI block in a single call.**

### Gap 7: Dispatcher Call — AMD webhook authentication
The `/webhooks/voice/amd` endpoint will receive Twilio POST requests. The existing voice endpoints use `@require_twilio_signature`. The new AMD webhook must use the same decorator. Blueprint doesn't specify — assume required.

### Gap 8: `roi_milestone` alert kind — does it send email OR just SMS?
The milestone "holy shit" moment should primarily be an SMS (lockscreen visible). An email 2 hours later is noise. The `alerts.notify()` fan-out sends both. Options:
- A: Add a channel filter to `notify()` for roi_milestone (SMS only)
- B: Let it fan out to both (consistent with the existing pattern)
- **Recommendation:** B — consistency wins; Dave can turn off email alerts globally if he wants. The SMS is the headline; email is the backup audit trail.

---

## 7. SUMMARY

**3 new `businesses` columns:** `roi_milestone_sent_at TEXT`, `dispatcher_call_last_at TEXT`, `job_value_prompt_dismissed_at TEXT` — all guarded `if col not in cols`, one migration block.

**1 new constant in `config.py`:** `PLAN_COST_MONTHLY = 99` (single source of truth for $99).

**1 new dict in `db.py`:** `TRADE_JOB_VALUE_DEFAULTS` (plumbing $850 → roofing $4,500).

**0 new tables.** Everything derivable is derived; the only state that must be stored is idempotency timestamps.

**"Calls recovered" computation:** V1 = `leads WHERE source='missed_call'` (already in `db.analytics()`); V2 = 5-minute join on `calls.from_number + leads.phone` (deferred M8).

**EXTEND vs BUILD-NEW verdicts:**
- Weekly digest: **EXTEND** `convos.digest_email()` (~15 lines)
- Show-Up-Prepared briefing: **EXTEND** `alerts.format_message("booking", ...)` context enrichment
- Dispatcher Call TwiML: **BUILD-NEW** (2 routes in `app.py`, trigger in `handle_inbound()`)
- Milestone SMS: **EXTEND** `alerts.notify()` (new kind) + new `check_roi_milestone()` in `roi.py`
- `db.analytics()`: **EXTEND** (additive new fields in return dict)

**Proposed 3-way partition:**
- Agent A → `db.py` + `config.py` + `roi.py` (data layer)
- Agent B → `convos.py` + `alerts.py` + `analytics.html` (digest + copy)
- Agent C → `app.py` only (dispatcher TwiML + booking hook + milestone trigger call)

**Biggest gap:** Dispatcher Call caller-words source is underspecified — use last inbound `messages.body` as synchronously available primary, not the async `leads.summary`. Also: "Show-Up-Prepared" is a format extension of the existing booking alert SMS, not a second SMS — this must be stated explicitly in the build spec to prevent Agent C from building a duplicate send.

---

*PREBUILD-3 written by Pre-Build Planner 3 of 3, 2026-06-18. Read-only pass. No code changed.*
