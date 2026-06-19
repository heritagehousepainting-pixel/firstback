# Phase 6 Integration Plan — FirstBack Go-Live

**Lens:** whole-product integration cohesion + production reliability
**Date:** 2026-06-18
**Branch:** staging (56/56 tests green, NOT deployed)

---

## 1. End-to-End Product Flow Audit

### 1a. The Core Flow (missed-call → text-back → conversation → booking → reminder)

**Flow trace — does it cohere?**

```
Missed call arrives at Twilio
  -> /webhooks/twilio/voice/inbound (app.py)
  -> triage.screen() [monitor or enforce mode]
     - Tier 0: opted-out? known contact? -> screened_contact/trusted/opted_out
     - Tier 1: STIR/SHAKEN, neighbor-spoof, repeat-call behavior -> spam_score
     - Tier 2 (paid gated): reputation.lookup
     - Tier 2.5: crowdsourced global_spam_count
     -> verdict: engage / screened_spam / review / prospect / trusted
  IF engage=True AND not screened_spam:
    -> db.create_lead()
    -> open_conversation() [ai.py -> brain (Claude/MiniMax/demo)]
    -> messaging.send_sms(business, lead.phone, reply, gate=True, transactional=True)
       - a2p gate: if not a2p_ready -> db.queue_blocked_send() -> "blocked"
       - quiet hours: transactional=True -> EXEMPT (correct)
       -> text delivered to customer
  INBOUND customer reply:
    -> /webhooks/twilio/sms/inbound
    -> _dispatcher_lead_owned() [cross-tenant safety — 5h patch]
    -> handle_inbound(biz, lead, body)
    -> ai.run_conversation() [Claude Sonnet]
    -> detect_urgency() -> if urgent: alerts.notify_async("urgent")
    -> detect booking marker [[BOOK:day|slot]]
       -> db.book_appointment()
       -> google_cal.create_event_async() [if connected]
       -> reminders.enqueue_reminder()
       -> reminders.enqueue_morning_reminder()
       -> alerts.notify_async("booking") [with _briefing_tail enrichment]
```

**Verdict: COHERES.** The core flow has no structural gaps. Key seams correctly wired:
- `transactional=True` on all solicited text-backs and reminders (quiet-hours exempt) ✓
- `transactional=False` on followup/followup_2 (5e fix — marketing path gets quiet-hours gate) ✓
- A2P gate correctly applies to customer sends, bypassed for owner alerts (gate=False) ✓
- `cancel_pending_followup_touches` wired to inbound arrival + booking (5e) ✓
- Cross-tenant dispatcher patch (5h) prevents a lead from one tenant triggering another's flow ✓
- Triage screening graduation auto-promotes monitor -> enforce after 7d + 10 spam verdicts (5c) ✓

**One unresolved gap: `cancel_appointment` -> reminder ordering.**
`SETUP_NEEDED.md` documents this: cancel writes the appointment row, then cancels its reminders on a separate DB connection. The ticker re-checks appointment status before sending (`run_due_once` line 315-323), so no double-text, but a crash between the two writes leaves an orphaned pending reminder that shows up as "skipped" rather than "canceled." Not a data-corruption risk, but a cosmetic correctness bug.

---

### 1b. Ticker Scan: Order + Mutual Safety

`tick_once()` at `reminders.py:743` runs this sequence every 60s:

```
1. db.set_meta("last_tick_utc", ...)      # heartbeat FIRST -- correct
2. triage.scan_all_suggestions()           # reads all businesses/leads/messages (read-heavy)
3. scan_followups(now)                     # per-biz: followup_candidate_rows + add_scheduled_message
4. growth.scan(now)                        # per-biz: growth_candidates (correlated subqueries) + add_scheduled_message
5. connections.check_forwarding_health()   # per-biz: place_call if sentinel timed out
6. scan_morning_briefing(now)              # per-biz: assistant.briefing (LLM call!) if 7-10am
7. scan_growth_tray(now)                   # per-biz: list_held_messages + alerts.notify
8. scan_stall_nudges(now)                  # per-biz: warm_leads_idle + alerts.notify per-lead
9. scan_screening_graduation(now)          # per-biz: screening_stats + db.promote_screening
10. google_contacts_sync_all(now)          # per-biz: google_contacts.sync if connected
11. run_due_once(now)                      # per-row: claim + send all due scheduled_messages
```

**Ordering hazards found:**

**H1 (Medium): `scan_followups` (step 3) runs BEFORE `growth.scan` (step 4).**
Both call `db.add_scheduled_message`. `growth.scan` uses `growth_touch_index` to avoid double-queuing, but `followup`/`followup_2` use a separate dedupe path (`followup_candidate_rows` checks `has_followup`/`has_followup_2`). These are independent kinds with separate dedupe, so no double-queue. Order is safe. However: if a lead books between step 3 and step 4, the followup gets queued at step 3, then the growth play also queues for the same now-booked lead at step 4 before the claim fires. Step 11's live-status re-check (`run_due_once` line 333-336) catches this and cancels the followup before send. Acceptable.

**H2 (LOW but real): `scan_morning_briefing` (step 6) makes a real LLM call.**
`assistant.briefing(biz)` is called inside the ticker thread. The briefing builds from DB reads, but if Claude is configured, this is a real Anthropic API call per business per tick during the 7-10am window. At scale (10+ businesses in the morning window simultaneously), this stacks LLM latency into the ticker thread, potentially delaying `run_due_once` (step 11). Dedupe (26h window) prevents re-fire, but the first 7am tick per business IS a synchronous LLM call in the ticker. Mitigation: `briefing()` has its own `try/except` (line 744), so a slow LLM call doesn't crash the ticker, but it CAN delay step 11 sends for the duration of the LLM timeout.

**H3 (Medium): `connections.check_forwarding_health` (step 5) places real outbound Twilio calls.**
One sentinel call per business every 7 days. Low frequency, but it's a real Twilio API call inside the ticker thread. Timeout is bounded by `messaging.place_call`'s `requests.post(timeout=20)`. No crash risk (wrapped in try/except), but 20s of network I/O in the ticker thread delays `run_due_once` by 20s per timed-out sentinel. At 10 businesses hitting a weekly sentinel simultaneously this is a 200s delay on scheduled sends. Low probability in practice (sentinel fires only once/week per business), but not zero.

**H4 (LOW): `google_contacts_sync_all` (step 10) runs before `run_due_once` (step 11).**
If connected and the cadence gate passes, `google_contacts.sync()` is a real Google API call (network I/O) inside the ticker. This delays `run_due_once`. Acceptable for now (sync is guarded by `configured()` + cadence key), but worth noting for production.

**Ordering verdict:** The current order is correct for data integrity (heartbeat first, queue before send, graduation before sends to prevent sending to just-screened contacts). The LLM call in step 6 is the most impactful latency risk for step 11 at real tenant volume. See recommendation W2 below.

---

## 2. Cross-Feature Integration Risks

### R1 (HIGH — OWNER NOTIFICATION VOLUME): The Morning SMS Storm

A single owner can receive ALL of the following in a single morning:

| Kind | Source | Window | Toggle |
|------|--------|--------|--------|
| `vic_morning` | `scan_morning_briefing` | 7-10am local | `alert_on_lead` |
| `growth_tray` | `scan_growth_tray` | 8-9am local | `alert_on_lead` |
| `vic_stall` (per stalled lead) | `scan_stall_nudges` | every tick | `alert_on_lead` |
| `lead` (new lead arrived) | inbound call handler | real-time | `alert_on_lead` |
| `booking` (appt booked) | conversation handler | real-time | `alert_on_booking` |
| `screening_graduated` | `scan_screening_graduation` | one-time | `alert_on_urgent` |

**Worst-case morning scenario for Heritage House Painting (or any contractor with 4 stalled leads, a growth tray with 5 held plays, and one new call arriving at 8:01am):**

```
07:00  vic_morning digest (1 SMS) — "4 leads need you, ~$10,000 on the table..."
08:05  growth_tray digest (1 SMS) — "Good morning. 5 texts ready: ..."
08:05  vic_stall Lead 1 (1 SMS) — "Maria replied 26h ago and is still waiting..."
08:05  vic_stall Lead 2 (1 SMS) — "Carlos replied 30h ago and is still waiting..."
08:05  vic_stall Lead 3 (1 SMS) — "Alex replied 48h ago... They may be shopping around."
08:05  vic_stall Lead 4 (1 SMS) — "James replied 52h ago..."
08:07  new call -> lead alert (1 SMS) — "New lead: Jessica (555) 867-5309 about painting..."
```

**Total: 7 SMS in one 7-minute window.** For a contractor on a job site, this is a notification flood. The `vic_morning` and `vic_stall` digests are both toggled by `alert_on_lead` — the same switch. There is NO way to get the morning digest without also getting per-lead stall nudges. There is NO way to get the growth tray without the morning digest if `alert_on_lead` is on.

**The deeper conflict:** `vic_morning` and `vic_stall` are semantically REDUNDANT for stalled leads. The morning digest already surfaces "leads need you" at a summary level. The stall nudge then re-pings on each of those same leads individually within the same tick window. The two features fight each other by design when both are on.

**Files/lines:**
- `alerts.py:52` — both `vic_morning` and `vic_stall` share `alert_on_lead` toggle
- `alerts.py:38` — `_DAILY_DEDUPE_KINDS` dedupes `vic_stall` per (lead, local day), not per "already covered in morning digest"
- `reminders.py:775-788` — `scan_morning_briefing` then `scan_growth_tray` then `scan_stall_nudges` — all fire in the same tick with no coordination

---

### R2 (MEDIUM): `vic_morning` vs `growth_tray` window overlap

- `vic_morning` window: **7am-10am** local (3h band)
- `growth_tray` window: **8am-9am** local (1h band)

Both are deduped per local day (26h window). But at exactly 8am local, the ticker can fire both on the SAME pass (one tick = one `tick_once` call). The owner receives two SMS within seconds of each other. From a contractor's perspective, these read as one "morning" digest that was split in two for no clear reason. They serve related but distinct purposes (pipeline brief vs growth queue), but the owner doesn't see that distinction — they see two "Good morning" texts arrive back-to-back.

**File:** `reminders.py:775` (`scan_morning_briefing`) fires before line 781 (`scan_growth_tray`). Because dedupe keys are different (`vic_morning` vs `growth_tray`), both fire in the same tick at 8am local. No shared coordination.

---

### R3 (MEDIUM): `briefing_tail` enriches lead/booking alerts WITH assistant.briefing()

`alerts._briefing_tail()` (`alerts.py:204`) calls `assistant.briefing(business)` synchronously (inside `notify()`, which is called from `notify_async()` on a daemon thread). This is a DB-read-only briefing, not an LLM call under normal load, but if `briefing()` internally exercises LLM paths it could be slow. Currently `briefing()` appears to be a DB-aggregation function (checking leads/appointments), so this is likely safe. However, this means every `lead` and `booking` alert carries a briefing computation — at high alert volume, this is N DB reads per alert.

**File:** `alerts.py:204-220` — `_briefing_tail` imports assistant lazily, calls `briefing()` on every `lead`/`booking` alert. No cache.

---

### R4 (LOW): `followup` and `growth.scan` can both queue for the same lead in the same tick

`scan_followups` queues `followup` (Touch-1) and `followup_2` kinds. `growth.scan` queues `quote_followup` and `reactivation` kinds. For a lead that is "warm but cold" (replied, then went quiet 24h+), both `followup` AND `quote_followup` can be eligible simultaneously, queuing two similar customer texts. They are:
- Deduped by kind: `followup` and `quote_followup` are different kinds, so the uniqueness index doesn't block both.
- The `followup` kind goes out as Touch-1 (transactional=False, quiet-hours gated). The `quote_followup` kind is a growth play (held in tray mode, released by owner). In tray mode these never auto-fire together (owner must release the growth play), so in practice the risk is low. But in `auto` mode (UI-locked for now) or if a future admin unlocks it, both could fire for the same lead.

**File:** `reminders.py:414` (`scan_followups`) and `growth.py:389` (`scan`) both run in the same `tick_once` call. The kinds are separate. No explicit cross-kind exclusion.

---

### R5 (LOW): Forwarding sentinel probe fires a real outbound Twilio call in the ticker thread

`connections.check_forwarding_health()` (`connections.py:633`) places a real Twilio call when `forwarding_confirmed=True` and the weekly probe window passes. This is a synchronous Twilio API call inside the ticker thread. A 20s Twilio timeout = 20s of blocking for the ticker. The probe fires at most once per 7 days per business, so in steady state this is rare. But at initial deploy, ALL forwarding-confirmed businesses could hit the probe on the first tick, causing a cascade of Twilio calls in the same tick pass.

---

## 3. Production Reliability: Ticker + Proactive Engine

### Ticker reliability assessment

**Architecture:** The ticker is a single daemon thread (`reminders.py:826-851`). It wakes every 60s (`TICK_SECONDS`). An external Render cron (`POST /tasks/run-due` every 60s) is the intended production driver. The in-process thread is a fallback.

**Critical gaps for production:**

**G1 (P0): The external cron secret is required in production but NOT currently set.**
`SETUP_NEEDED.md` line 237: "`/tasks/run-due` returns 403 unless `FIRSTBACK_TASKS_SECRET` is set." Without it, the cron silently 403s on every call, and the in-process ticker alone runs. When Render recycles the dyno (Render's free/starter tier recycles on inactivity), the in-process thread dies and scheduled messages pile up until the next cold start. This is a P0 for production.

**G2 (MEDIUM): SQLite contention at tick time.**
Every `tick_once` opens multiple DB connections across 8+ scan functions, each calling `db.get_conn()` individually. The DB is `WAL` mode (write-ahead logging not explicitly mentioned but assumed from `busy_timeout=5000`). Under concurrent load (a real inbound SMS handler writing to `messages` simultaneously), 5s busy timeout means the ticker can block for 5 full seconds on a write conflict. At 60s tick interval this is fine; at high traffic volume (many inbound messages per minute), tick latency compounds.

**G3 (MEDIUM): `scan_morning_briefing` makes LLM calls in the ticker.**
See H2 above. At 7am local, every business with actionable leads triggers a real Claude API call from inside the ticker thread. If Claude is slow (cold start, rate limit), step 6 blocks steps 7-11. A 10s LLM response per business x 5 businesses = 50s of delay before `run_due_once`. Reminders scheduled for 7:00am could fire at 7:00:50am or later.

**G4 (LOW): `google_contacts_sync_all` has no timeout bound.**
`google_contacts.sync()` is a Google API call with no explicit timeout documented. If Google is slow or returns a large contacts list, this blocks the ticker. Cadence gate (once/UTC-day) prevents it from running every tick, but a slow sync on the day it runs delays all subsequent steps.

**G5 (LOW): `run_due_once` sends SMS one at a time.**
`run_due_once` iterates scheduled messages sequentially (`for row in db.due_scheduled_messages(now)`). Each real Twilio `requests.post(timeout=20)` call blocks the loop. For a batch of 10 due reminders, this is potentially 200s of sequential Twilio calls in a single tick. At current scale (1 tenant) this is fine. At 10+ tenants with reminders firing simultaneously, this is a concern.

---

### Proactive engine reliability

**The proactive push (morning digest, stall nudge, growth tray) is correctly deduped:**
- `vic_morning`: day-stamped dedupe key, 26h window. One per biz per day. ✓
- `vic_stall`: day-stamped per (lead, day), 26h window. One per lead per day. ✓
- `growth_tray`: day-stamped per day, 26h window. One per biz per day. ✓
- `screening_graduated`: 365-day window. One per biz lifetime. ✓

**The dedupe lock is a threading.Lock() (process-local).** In production on Render with a single dyno, this is sufficient. If Render ever scales to multiple workers (unlikely on the current plan but possible), the dedupe lock is NOT distributed and the 26h window in the DB (`alert_recent`) is the only protection against double-send across processes. The DB check alone is correct but less precise than the lock. Acceptable for now.

---

## 4. Recommended Phase-6 Workstreams

Ranked by impact on the "set-and-forget promise" (Dave test: non-tech contractor, no monitoring):

---

### W1 (P0 — MUST DO BEFORE ANY REAL SENDS): Fix the production cron + secret

**Problem:** `FIRSTBACK_TASKS_SECRET` and `FIRSTBACK_INTERNAL_SECRET` not set in Render env → `/tasks/run-due` returns 403 → external cron silently fails → scheduled messages don't go out.

**Fix:** Set both secrets in Render env before any deploy. Add a `/health/ticker` check that explicitly tests the external cron path (not just the in-process heartbeat) — a separate `/health/cron` endpoint that returns the last time `/tasks/run-due` was called successfully.

**File:** `app.py` (`/tasks/run-due` handler), `config.py` (`TASKS_SECRET`), Render dashboard.

**Owner-ops:** Set `FIRSTBACK_TASKS_SECRET`, `FIRSTBACK_INTERNAL_SECRET`, `FIRSTBACK_RUN_TICKER=1` in Render env before first deploy.

---

### W2 (P1 — HIGH IMPACT): Consolidate owner morning SMS into ONE daily digest

**Problem:** See R1. An owner with 4 stalled leads and growth tray enabled gets 7 SMS in 8 minutes at 8am. This fails the Dave test. A contractor on a roof doesn't want 7 buzzes — they want 1 clear summary.

**Proposed solution: A unified 8am daily digest that absorbs `vic_morning`, `growth_tray`, and `vic_stall` into one SMS.**

Design:
```
[8:00am local] ONE SMS per business:
  "Good morning [name]. Today: 4 leads need you (~$10K), 
  5 outreach texts ready. Reply GO to send all or open 
  FirstBack to review. One stall: Maria (26h, ~$2.5K)."
```

Implementation path:
1. Create `scan_daily_digest(now)` in `reminders.py` that fires at 8am local.
2. It queries: `assistant.briefing()` counts, `db.list_held_messages()` count, `db.warm_leads_idle()` top-1 most urgent stall.
3. Builds one compact SMS body (cap 320 chars).
4. Dedupes via a new `daily_digest` kind in `alerts.ALERT_KINDS` (26h window, day-stamped).
5. DISABLE `scan_morning_briefing` (7am window), DISABLE per-lead `scan_stall_nudges` SMS in the morning window if daily digest already fired.

**Critical:** `vic_morning` (7-10am) and `growth_tray` (8-9am) windows would be retired into `scan_daily_digest` (8am exactly). `vic_stall` per-lead nudges would remain for AFTERNOON/EVENING (after 12pm local) to catch leads that went cold during the day, but would be suppressed in the morning if the daily digest already fired.

**Alerting toggle consolidation:** Add a single `alert_on_daily_digest` toggle (separate from `alert_on_lead`) so Dave can turn off the morning buzz without losing real-time lead/booking alerts.

**Files to edit:** `reminders.py` (new `scan_daily_digest`, modify `tick_once`), `alerts.py` (new kind, new dedupe key), `app.py` (`/settings` alert prefs), settings template.

**Estimated impact:** Reduces worst-case morning SMS from 7 to 1. Highest "feels set-and-forget" improvement available.

---

### W3 (P1 — HIGH IMPACT): Move LLM call out of ticker thread

**Problem:** See H2. `scan_morning_briefing` calls `assistant.briefing(biz)` inside the ticker thread, which can make a real Claude API call. This blocks `run_due_once` (step 11) during the morning window.

**Fix option A (preferred for W2):** If W2 is implemented, `scan_daily_digest` calls `assistant.briefing()` but should do so with a timeout guard and a pre-computed fallback. The DB-aggregation path of `briefing()` doesn't need Claude — only the "headline" copy does. Split `briefing()` into `briefing_data(biz)` (pure DB, fast) and `briefing_headline(data)` (LLM, optional). The digest uses `briefing_data` only, building the SMS from counts directly.

**Fix option B (if W2 not built):** Wrap `scan_morning_briefing`'s `assistant.briefing()` call with a `threading.Thread` that pre-caches the result N minutes before the 7am window, or cache the last briefing result with a short TTL.

**Files:** `reminders.py:488-492`, `assistant.py` (`briefing` function).

---

### W4 (P1 — MEDIUM IMPACT): Ticker budget guard — cap work-per-tick

**Problem:** The ticker does unbounded per-business work. With 50 businesses: `scan_all_suggestions` (50 DB queries), `growth.scan` (50 `growth_candidates` correlated subqueries + `plays()` full computation), `warm_leads_idle` (50 queries), `list_held_messages` (50 queries). This is 400+ DB calls per 60s tick. At current scale (1-2 tenants) fine. At 20-50 tenants this is 400+ queries every 60 seconds.

**Fix:** Add a per-tick budget:
1. Stagger heavy scans (e.g., `growth.scan` and `triage.scan_all_suggestions` on alternating ticks, not every tick).
2. Add a soft tick budget timer: if `tick_once` runs longer than `TICK_SECONDS * 0.8` (48s), log a warning and skip the non-critical steps (contacts sync, graduation).
3. Move `google_contacts_sync_all` to a dedicated `/tasks/contacts-sync` endpoint driven by a separate, less-frequent cron (daily, not 60s).

**Files:** `reminders.py:743-804` (`tick_once`).

---

### W5 (P1 — MEDIUM IMPACT): Failed growth touch retry gate

**Problem:** `SETUP_NEEDED.md` line 228 documents this. A growth touch that fails (Twilio error after A2P is live) lands in `failed` status. `growth_touch_index` excludes only `canceled` touches, so a `failed` touch blocks re-queue — the slot is permanently held by the failed row. The customer never gets the text and the owner never knows.

**Fix:** In `db.growth_touch_index` (db.py line ~2594), change `status!='canceled'` to `status NOT IN ('canceled','failed')`. Add a `test_growth_failed_retry.py` regression. Also: when `run_due_once` marks a scheduled message `failed` (line 349), check if it's a growth kind and proactively fire an owner alert (`sms_fail` kind, already in ALERT_KINDS).

**Files:** `db.py:2594-2606` (`growth_touch_index`), `reminders.py:349` (failed send path).

---

### W6 (P2 — LOW FRICTION): `followup` vs `quote_followup` mutual exclusion

**Problem:** See R4. A warm-but-cold lead can get both a `followup` Touch-1 (automated) and a `quote_followup` growth play (held in tray) queued in the same tick. If the owner releases the growth tray at 8am and the followup fires at 8:05am, the customer gets two texts within minutes.

**Fix:** In `scan_followups`, after queuing a `followup` for a lead, call `db.cancel_lead_growth_touches(lead_id, {"quote_followup"})`. Conversely, in `growth.scan`, before queuing a `quote_followup`, check if a `followup` row already exists in `scheduled_messages` for this lead (the `growth_touch_index` already loads this if we include followup kinds).

**Files:** `reminders.py:444` (post-queue in `scan_followups`), `growth.py:408-446` (`scan()` pre-check).

---

### W7 (P2 — COSMETIC): Cancel-then-reminder transaction fix

**Problem:** See section 1a gap. `cancel_appointment` writes cancel + reminder-cancel on separate connections. Rare crash leaves orphaned pending reminder.

**Fix:** Wrap both writes in a `BEGIN IMMEDIATE` transaction in `db.cancel_appointment`. One connection, atomic.

**Files:** `db.py` (`cancel_appointment` and `cancel_lead_pending_reminders`).

---

## 5. Integration Risk Summary Table

| Risk | Severity | Impact | Resolution |
|------|----------|--------|------------|
| Cron secret not set (G1) | P0 | Scheduled sends silently fail in prod | W1 — set before deploy |
| Owner morning SMS storm (R1) | P1 | 7+ SMS in 8 min, fails Dave test | W2 — unified digest |
| LLM call in ticker (H2) | P1 | Delays `run_due_once` during morning | W3 — decouple briefing |
| Ticker weight unbounded (W4) | P1 | 400+ DB calls/tick at scale | W4 — stagger + budget |
| Failed growth touch not retried (R5/W5) | P1 | Silent customer miss after Twilio live | W5 — exclude failed |
| vic_morning + vic_stall redundancy (R1) | P1 | Semantic overlap in same digest window | W2 |
| followup + quote_followup double-text (R4) | P2 | Two similar texts to same lead | W6 |
| Sentinel call in ticker thread (H3) | P2 | 20s I/O blocking, low frequency | Future async |
| Cancel/reminder transaction (section 1a) | P2 | Orphaned reminder row, cosmetic | W7 |

---

## 6. Phase 6 Recommended Build Order

**Phase 6a (pre-deploy hardening, ~2-4 hours, CODE + OWNER-OPS):**
- W1: Set cron + task secrets in Render env. Add `/health/cron` endpoint.
- Owner-ops: set `FIRSTBACK_TASKS_SECRET`, `FIRSTBACK_INTERNAL_SECRET`, reconcile all `FIRSTBACK_*` env, set `FIRSTBACK_PUBLIC_URL`.

**Phase 6b (notification consolidation, ~1 day, CODE):**
- W2: Build `scan_daily_digest` (replaces `vic_morning` + `growth_tray` + morning stall nudges). Ship as a feature-flagged toggle: `FIRSTBACK_UNIFIED_DIGEST=1`.
- W3: Decouple `assistant.briefing()` from the ticker thread; use `briefing_data()` DB-only path in digest.
- New alert kind `daily_digest` in `alerts.py`. New toggle in settings.

**Phase 6c (reliability + edge cases, ~half day, CODE):**
- W4: Ticker budget guard + stagger heavy scans.
- W5: Failed growth touch gate fix in `db.growth_touch_index`.
- W6: followup vs quote_followup mutual exclusion.
- W7: Cancel/reminder transaction fix.

**Phase 6d (integration audit, ~half day, READ-ONLY):**
- Run the full 56-test suite on `staging`.
- Un-stubbed e2e: one business with 3 stalled leads + 2 held growth plays — verify EXACTLY 1 SMS fired at 8am local (the digest), not 6.
- Verify forwarding sentinel fires at most once per business across a simulated 2-tick test.
- Confirm `failed` growth touch can re-queue after W5 fix.

**Phase 6e (deploy, OWNER-OPS):**
- Render deploy with reconciled env.
- External Render cron wired to `/tasks/run-due` (every 60s) + `/tasks/digest` (weekly).
- Heritage House Painting dogfood: verify one real missed-call text-back goes through A2P.

---

## Key Files for Phase 6 Builders

- `reminders.py:743-804` — `tick_once` ordering (W2, W3, W4)
- `reminders.py:469-523` — `scan_morning_briefing` (retire into W2 unified digest)
- `reminders.py:527-596` — `scan_growth_tray` (retire into W2 unified digest)
- `reminders.py:599-638` — `scan_stall_nudges` (adjust to afternoon-only after W2)
- `alerts.py:32-57` — `ALERT_KINDS`, `_TOGGLE_COL`, dedupe windows (add `daily_digest`)
- `alerts.py:204-220` — `_briefing_tail` (N DB reads per alert — cache opportunity)
- `db.py:2594-2606` — `growth_touch_index` (W5: exclude `failed`)
- `app.py:1127-1134` — settings alert prefs (add `daily_digest` toggle)
- `connections.py:633-689` — `check_forwarding_health` (async candidate for future)
- `SETUP_NEEDED.md` — owner-ops queue (read before every deploy)
