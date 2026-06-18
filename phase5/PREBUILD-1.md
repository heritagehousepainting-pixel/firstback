# Phase 5 Pre-Build Assessment — Lane 1: Scope / Sequencing / What-Exists

**Date:** 2026-06-18  
**Assessor:** Pre-Build Planner 1 of 2  
**Repo state:** `staging` ~bfd6ceb, 46/46 green  
**Lane:** READ-ONLY. Scope / sequencing / what-exists / sub-phase breakdown / code-vs-owner-ops.

---

## 1. Per-Feature Inventory — EXISTS / PARTIAL / MISSING

### SF-6 — Server-Bound Confirm Token

**Status: MISSING (the security gate; everything gated downstream is partially exposed)**

`app.py:864–884` `/assistant/confirm` receives `tool` + `args` directly from the client POST body.
`assistant.py:1897–1955` `_gated()` returns `pending_action = {"tool": tool, "args": args, "summary": ...}` with the full args embedded. The client echoes `tool` + `args` back on confirm; the server blindly executes them. No token issuance, no storage, no redemption path. CSRF + SameSite are the only guards.

The SF-6 blueprint's other two items are **already built**:
- Transmit-time quiet-hours backstop: `messaging.py:132` calls `tc_messaging.quiet_blocked()`. EXISTS.
- START/opt-in re-subscribe: `app.py:2298–2304` wires `consent.opt_in_nlu` → `db.set_opt_in`. EXISTS.

**Delta to target:** ~40 lines across 3 files: `db.py` (new `pending_confirms` table), `assistant.py:_gated` (new `_issue_token`), `app.py:582–602` (rewrite confirm redemption to look up token, not echo args). Client change: send `confirm_token` not `tool`/`args`.

---

### F11 — Vic Proactive

**Status: PARTIAL — read/compute layer built; proactive push layer is 100% missing**

Built and correct:
- `assistant.py:544` `briefing()` public entry point
- `assistant.py:453` `_compose_briefing()` full money-ranked lead/booking/setup feed
- `assistant.py:549` `briefing_signature()` hash for change detection
- `assistant.py:570` `adaptive_suggestions()` state-derived chips
- `assistant.py:1897` `_gated()` gate exists; `pending_action` dict returned

Not built:
- Proactive push: no briefing-delta polling, no token pre-issuance from server side, no scheduled 7 AM push
- `assistant.py:1897` `_gated` returns `{tool, args}` in `pending_action` — no `token_id`. Entire confirm integrity model is absent (depends on SF-6)
- Warm-lead stall nudge: no hourly scan in `reminders.py`
- Referential ambiguity guard: `_h_text_lead` resolves `entities` or falls to most-recent lead (gap at `assistant.py:1328`); no "ask before guessing" branch
- Briefing-enhanced alert SMS: `alerts.format_message` not enriched with briefing context
- Dollar-based cost cap: `app.py:191` checks turn count (`FIRSTBACK_ASSISTANT_DAILY=400`), not dollars. No `vic_spend_usd` column
- LLM model: `config.py:46` still defaults to `claude-opus-4-8`; Sonnet switch is planned but unconfirmed from source read

**Delta:** SF-6 must ship first (unblocks token pre-issuance). Then: briefing-enhanced alert SMS (P1-1), 7 AM push via reminders ticker (P1-2), stall nudge (P1-3), referential ambiguity guard (P1-4), dollar cap (P0-3), Opus→Sonnet switch (P0-2).

---

### F07 — Spam Screening

**Status: PARTIAL — scoring engine complete; auto-graduation engine is 100% missing**

Built and correct:
- `triage.py:70–193` `spam_score()` + `screen_caller()` with all scoring tiers
- `db.py:924` `set_screen_mode()` (off/monitor/enforce)
- `db.py:1816` `screening_stats()` with enforced/would_screen split
- `triage.py:205` `suggest_category()` + `scan_all_suggestions()` behavioral scanner wired to ticker

Not built:
- `screening_promoted_at` timestamp column on `businesses` — does not exist in `db.py`
- `business_false_positive_count` column — does not exist
- Auto-graduation ticker job in `reminders.py`: no 7-day/10-verdict/0-rescue check, no `screen_mode='enforce'` write, no graduation notification
- "This was real" rescue tap: no `POST /calls/<id>/rescue` endpoint in `app.py`
- Sensitivity slider: HARD/MID thresholds are global env vars (`config.py:82–83`); no per-business `screen_hard`/`screen_mid` columns
- Per-tenant reputation toggle: `reputation.configured()` is global env var only; no `reputation_enabled` per-business flag
- Velocity burst signal: `db.global_spam_count()` is lifetime total; no 24h window variant
- "Spam Shield: Learning (Day N of 7)" UI card

**Delta:** Two new business columns → graduation cron in ticker → rescue endpoint + DB function → sensitivity slider backend. Credits-saved counter exists as query but needs UI surface.

---

### F13 — Growth Engine

**Status: PARTIAL — plays/scan engine built; tray/batch-release/mode engine missing**

Built and correct:
- `growth.py:173` `plays()` money-ranked opportunity engine
- `growth.py:299` `money_left_behind()`
- `growth.py:328` `scan()` enqueue loop (uses `growth_on` boolean, default OFF)
- `growth.py:313` `growth_on()` boolean check
- `reminders.py:397–406` `tick_once` calls `growth.scan()`
- `messaging.outbound_mode()` opt-out gate wired in `growth.py:192`

Not built:
- `growth_mode TEXT` column: `db.py:515` has `("growth_on", "INTEGER DEFAULT 0")` — still the old boolean. `set_growth_mode()` does not exist.
- `status='held'` for `scheduled_messages`: `due_scheduled_messages` query (`db.py:1957`) does not exclude `'held'` rows — **held rows would auto-fire if inserted**
- `db.release_growth_batch()` / `db.list_held_messages()` / `db.cancel_growth_play()` — none exist
- Morning tray SMS digest (8 AM, per-business local time)
- Inbound "GO" / "SKIP" reply parser
- `growth_touch_log` table: does not exist in `db.py`
- Cross-kind 30-day frequency cap: nothing enforces cross-kind cooldown today
- `growth_approvals` audit log table: does not exist
- Growth Tray dashboard screen (`/growth/tray`)
- Auto-default job value by trade keyword in `_job_value()` (returns 0 on miss)
- G9 tone-risk flag; G8 active-prospect suppression

**Delta:** Largest S-count in Phase 5 outside F10. Must build `growth_mode` + `held` status + release API before anything reaches the tray. Morning SMS and reply parser are M items but are the visible face of the feature.

---

### F10 — AI Voice

**Status: PARTIAL (foundation built; deployment and streaming are 100% missing)**

Built and correct:
- `voice_service.py:1–185` full ConversationRelay TwiML + WS relay service (185 lines, complete)
- `app.py:1780–1794` consent gate, quiet-hours check, `place_call` trigger
- `voice_service.py:108–109` recording disclosure in greeting (non-negotiable; built)
- `voice_service.py:84–103` `_process_turn` stateless relay to `/internal/voice/turn`
- `app.py:2494` `/internal/voice/turn` endpoint exists
- FCC consent gate: `db.set_voice_consent()` wired at `app.py:1785`
- Quiet-hours enforcement: `compliance.voice_allowed_now()` at `app.py:1786`

Not built / not deployed:
- `render.yaml:71–81` voice service is **commented out** — service does not run on Render
- `FIRSTBACK_VOICE_URL` is **unset** in production — no real calls have ever been placed
- Token streaming (S-2): `voice_service.py:168` sends single `last=True` frame; no streaming; barge-in is a no-op (`continue` at line 172). Dead air exists without this. **Must ship before selling.**
- AMD / voicemail detection: `MachineDetection=Enable` not in `place_call` call
- Pre-call guard additions: STOP-revocation clear of voice consent (`db.set_voice_consent(False)`) not wired to STOP handler; spam-score gate not wired; 60-min de-dupe not wired
- Voice credit metering: no `voice_calls` table; no `StatusCallback` webhook; no per-call credit deduction
- Monthly cost cap: no cap enforcement in pre-call path
- Post-call transcript stored in lead thread: not built
- 8-minute hard duration cap session timer: not built
- Premium voice ear-test: `CONVERSATIONRELAY_VOICE` env var wired in config but ear-test is an operator ops task; value not set

**Delta:** S-1 is an ops task (uncomment render.yaml, wire 6 env vars). S-2 (streaming) is the highest-variance code build in all of Phase 5. Without S-2, voice must not be sold. The quality gate (all 7 checks) must pass before any tenant activation.

**OWNER-OPS requirement:** Render voice service deploy + env wiring is operator work (no code). Premium voice selection is a listening task. These precede and gate all F10 code builds.

---

### F06 — Cold Follow-Up

**Status: PARTIAL — detection and generic template built; contextual Sonnet copy + Touch 2 + robocaller exclusion are missing**

Built and correct:
- `reminders.py:212` `scan_followups()` detection loop
- `reminders.py:97` `due_followup_leads()` filter
- `db.py:2039` `followup_candidate_rows()` with inbound-only gate and booked exclusion
- `reminders.py:122` `followups_on()` per-business toggle (default ON)
- `reminders.py:76` `followup_body()` generic template (wired at `reminders.py:371`)

Not built:
- Contextual Touch 1 Sonnet copy (M1): `followup_body()` always returns the generic template; `followup_candidate_rows` does not include last inbound text
- Touch 2 / `followup_2` kind: no second enqueue in `scan_followups`; no `has_followup_2` EXISTS check
- Robocaller exclusion: `followup_candidate_rows` does not filter `triage_flag IN ('robocaller','spam')` — `leads.triage_flag` column may not exist (needs schema check)
- Live status check in `run_due_once` for followup kinds (S2 race-condition close)
- `cancel_pending_followup_touches(lead_id)` DB function + `handle_inbound` wiring (S3)
- Opt-out check at enqueue time (M3)
- `test_reminders.py`: zero tests exist despite docstring claim (`reminders.py:16`)

**Delta:** Robocaller exclusion (S1) ships first — embarrassing if spam number gets a follow-up. Touch 2 (S4) depends on S2+S3. Contextual Sonnet copy (M1) is the feature's "wow" but can trail. Tests (S7) must exist before any new sends go live.

---

### F08 — Contacts / Recognition

**Status: PARTIAL — core recognition + import infrastructure built; nightly re-sync + UI flows are missing**

Built and correct:
- `contact_import.py:218` `ingest()` pre-sort + suggestion queue
- `contact_import.py:42` `presort()` pure sort (booking → customer, org → vendor)
- `contact_import.py:60` `parse_vcard()`, `parse_csv()`, `parse_file()` parsers
- `google_contacts.py:160` `sync()` fetch + ingest
- `google_contacts.py:37` `configured()` / `is_connected()` / `auth_url()` / `connect_with_code()`
- `app.py:1994` `/google_contacts/connect` OAuth route
- `app.py:2004` `/google_contacts/callback` route
- `app.py:2020` `/google_contacts/sync` manual sync route
- `triage.py:135` `screen_caller()` → `db.is_known_caller()` — already respects directory + booking history

Not built:
- **Nightly automatic re-sync**: `reminders.py` `tick_once()` does NOT call `google_contacts.sync()` for connected businesses. The sync is manual-only today. ~20 lines needed in `tick_once`.
- Bulk-accept UI on the suggestion inbox (mentioned in F08-FINAL §1 "Accept all 47 customers — two taps")
- Grouped repeat-client card ("3 repeat clients still waiting for review") after 30 days pending
- Badge on inbox icon when new suggestions created by nightly sync
- Jobber/HCP CSV support: `parse_csv()` handles generic CSV; Jobber/HCP-specific headers need testing but likely work via header detection

**Delta:** Nightly re-sync (~20 lines in `tick_once`) is the highest-leverage net-new item. Bulk-accept UI is a frontend task. OAuth credentials (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_CONTACTS_REDIRECT_URI`) are operator-wired env vars — already in `config.py:159`.

**OWNER-OPS requirement:** `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` must be configured on Render for OAuth to work. `GOOGLE_CONTACTS_REDIRECT_URI` must be registered in Google Cloud Console.

---

### SF-10 — Crew Multi-Tenant

**Status: MISSING — schema not designed; single-user assumption throughout auth/billing**

No `org_id`, `team_id`, or multi-seat model exists in `db.py`. No `crew` references found. The blueprint says "design now, build later" — even the schema design is not done. Auth (`app.py`) and billing (Stripe) are currently single-business-per-account.

**Delta:** Schema design doc (account/org → users → businesses → numbers) must be written before Phase 5 ends so Starter billing is built on a schema that can be extended. The build-out is L-effort and ships last.

---

## 2. Recommended Sub-Phase Breakdown

Phase 5 is too large for one loop. The below breaks it into ordered, independently-shippable sub-phases with explicit dependencies and risk-ordering (voice last; SF-6 first).

### Phase 5a — SF-6: Server-Bound Confirm Token (S effort, ~1 day)
**Unblocks:** F11 proactive (pre-issued tokens), F13 tray release (idempotent GO batch)  
**Build-ready:** Yes — spec is complete in F11-FINAL §4.1  
**Files:** `db.py` (`pending_confirms` table), `assistant.py:_gated` (`_issue_token`), `app.py:582–602` (rewrite confirm redemption)  
**Client change:** POST `confirm_token` instead of `tool`/`args`  
**Tests to write:** token issuance returns token_id; expired token → 400 "confirm expired"; second tap returns stored result without re-execution; wrong-bid token → 400; tool/args from client ignored on confirm  
**Risk:** Low. Contained to 3 files + 1 table. No side effects on existing sends.

---

### Phase 5b — F11: Vic Proactive (M-L effort, ~3–5 days)
**Depends on:** 5a (SF-6 token)  
**Build-ready:** After 5a ships  
**Order within 5b:**
1. P0-2: Opus→Sonnet switch (`config.py:46`) — S effort, no-risk config change
2. P0-3: Dollar-based budget cap (`vic_spend_usd` column, tier cap check before `_tool_loop`, honest degradation "Vic's resting") — M effort
3. P1-1: Briefing-enhanced alert SMS (enrich `alerts.format_message` with briefing item from `assistant.briefing()`) — M effort
4. P1-4: Referential ambiguity guard (`_h_text_lead` → ask before guessing when `entities` empty) — S effort
5. P1-2: 7 AM morning push with pre-staged tokens (ticker pattern from reminders, pre-issue tokens per briefing item) — S effort
6. P1-3: Warm-lead stall nudge hourly scan (reminders ticker, 24h/48h thresholds) — M effort
7. P1-5: "Vic is resting" UI surface — S effort
8. P1-6: Enforce-mode two-stage confirm — S effort

---

### Phase 5c — F07: Screening Auto-Graduation (S-M effort, ~2 days)
**Depends on:** Nothing in Phase 5 (self-contained)  
**Build-ready:** Yes  
**Order within 5c:**
1. S2: "This was real" rescue endpoint + `business_false_positive_count` column
2. S1: Auto-graduation ticker job (`screening_promoted_at` column, 7d/10-verdict/0-rescue check, write `screen_mode='enforce'`, fire graduation notification)
3. S4: Credits-saved counter in UI (existing query; frontend surface)
4. M2: Per-tenant reputation toggle (`reputation_enabled` per-business column)
5. S3+M3: Sensitivity slider (backend columns + Settings UI)
6. M1: Velocity burst signal (24h window in `db.global_spam_count`)

---

### Phase 5d — F13: Growth Tray Engine (M-L effort, ~3–5 days)
**Depends on:** SF-6 (5a) for idempotent batch release; F07 screening for velocity burst signal used in G9 tone-risk (loose dependency — can parallelize)  
**Note:** G1 (quiet-hours backstop) and G2 (START re-subscribe) are already built. No need to re-do.  
**Order within 5d:**
1. S3: `growth_mode` column migration from `growth_on` boolean
2. S4: `status='held'` for `scheduled_messages` + `due_scheduled_messages` guard + release API (`list_held_messages`, `release_growth_batch`, `release_growth_play`, `cancel_growth_play`)
3. S5: `growth_touch_log` table + cross-kind 30-day frequency cap + 12-month rolling cap
4. S6: Auto-default job value by trade keyword in `_job_value()`
5. M1: Morning tray SMS digest + "GO"/"SKIP" inbound reply parser
6. M2: Growth Tray dashboard screen (`/growth/tray`)
7. M3: Growth mode toggle in Settings UI
8. M4: Reply handling for growth texts + `growth_touch_log` outcome tracking
9. M5: `growth_approvals` audit log table

---

### Phase 5e — F06: Cold Follow-Up Hardening (S-M effort, ~2 days)
**Depends on:** Nothing in Phase 5 (can parallelize with 5c or 5d)  
**Build-ready:** Yes  
**Order within 5e:**
1. S7: Write `test_reminders.py` — FIRST. Zero tests exist. Nothing ships without them.
2. S1: Robocaller exclusion in `followup_candidate_rows` (schema check + WHERE clause)
3. S2: Live status check in `run_due_once` for followup kinds (race-condition close)
4. S3: `cancel_pending_followup_touches()` + `handle_inbound` wiring
5. S4: `followup_2` kind + Touch 2 enqueue in `scan_followups`
6. S5: `followups_enabled` UI toggle in Settings
7. M1: Contextual Sonnet Touch 1 copy (the "wow" — add `last_in_text` to candidate rows, Sonnet call with template fallback)
8. M3: Opt-out check at enqueue time

---

### Phase 5f — F08: Contacts Nightly Sync + UI (S-M effort, ~1–2 days)
**Depends on:** Nothing in Phase 5 (can parallelize)  
**Build-ready:** Yes (operator must wire Google OAuth creds on Render first)  
**Order within 5f:**
1. Operator: Confirm `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_CONTACTS_REDIRECT_URI` wired on Render
2. ~20 lines in `reminders.py tick_once()`: call `google_contacts.sync(biz_id)` for each connected business; badge on inbox icon if new suggestions
3. Bulk-accept UI on suggestion inbox
4. Grouped repeat-client card after 30-day pending threshold
5. Jobber/HCP CSV: manual test with sample exports to confirm header detection works

---

### Phase 5g — F10: AI Voice (L effort, ~5–10 days + ops)
**Depends on:** Nothing in Phase 5 (code-independent); MUST pass quality gate before any tenant activation  
**OWNER-OPS prerequisite (gates everything else):** Deploy voice service on Render (uncomment `render.yaml:71–81`); wire 6 env vars; delete stale SQLite comment  
**Order within 5g:**
1. S-1: Deploy voice service (ops) — gate for all subsequent items
2. S-3: Premium voice ear-test (ops — audition 4+ voices, set `FIRSTBACK_VOICE_TTS` env var)
3. S-4: Pre-call guard additions (STOP revocation clear of voice consent, spam-score gate, 60-min de-dupe)
4. S-2: Token streaming + barge-in (highest-variance build; required before selling; replaces `run_in_executor` + single `last=True` frame with streaming generator + `last=false` per sentence + cancel flag on interrupt)
5. M-1: AMD / voicemail detection (`MachineDetection=Enable` + `AnsweredBy` webhook handler + no-AI-voicemail rule)
6. M-2: Empty ASR guard (consecutive empty turns → filler → graceful close)
7. M-3: Post-call transcript to lead thread
8. S-5: Credit metering (`voice_calls` table + `StatusCallback` endpoint + per-30s credit deduction)
9. M-4: Booking confirmation echo prompt instruction
10. M-5: Recovery SMS on WebSocketDisconnect
11. Quality gate: run all 7 checks before activating for any tenant

---

### Phase 5h — SF-10: Crew Multi-Tenant Schema Design (S effort, design-only)
**Depends on:** Nothing  
**Build-ready:** Immediately — design doc only, no code  
**Task:** Write `phase5/SF10-SCHEMA.md` documenting the account/org → users → businesses → numbers hierarchy, how Starter billing maps to the 1-seat case, and what must NOT be built single-user-assumption in Phase 5 builds (auth, Stripe subscription lifecycle). No DB migrations until Crew tier is ready to ship.

---

## 3. CODE vs OWNER-OPS Per Feature

| Feature | Code Work | Owner-Ops (Jonathan) |
|---------|-----------|----------------------|
| SF-6 | db.py + assistant.py + app.py (≈40 lines) | None |
| F11 | config.py + assistant.py + reminders.py + alerts.py | Confirm Opus→Sonnet env var not overridden on Render |
| F07 | db.py + reminders.py + app.py + Settings UI | Set `FIRSTBACK_REPUTATION_PROVIDER` on Render for paid tier |
| F13 | db.py + growth.py + reminders.py + app.py + templates | None |
| F10 | voice_service.py + app.py + messaging.py | **Uncomment render.yaml voice service + wire 6 env vars + ear-test 4 voices** |
| F06 | db.py + reminders.py + app.py | Confirm `FIRSTBACK_RUN_TICKER=1` on Render |
| F08 | reminders.py (20 lines) + templates | **Wire GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_CONTACTS_REDIRECT_URI on Render + register redirect URI in Google Cloud Console** |
| SF-10 | Schema design doc only (no code) | None |

---

## 4. Gaps / Holes / Risks That Would Bite a Builder

### Gap 1: SF-6 is the trust primitive for everything proactive (CRITICAL)
`app.py:864–884` executes whatever `tool`+`args` the client sends. A same-origin JS client with a valid CSRF token can POST `text_lead` with arbitrary body/recipient. The existing `_clean_args` strips unknown keys but does not verify the action was ever proposed. **Ship SF-6 (5a) before any proactive push or batch-release lands.** F11 pre-staged tokens and F13 GO-batch release both need idempotent server-bound tokens to be trustworthy.

### Gap 2: `status='held'` has no guard today — held rows would auto-fire
`db.py due_scheduled_messages` does not exclude `status='held'`. If F13's S4 (held insert) ships before the query guard is updated, the morning tray items would be picked up and sent by the next ticker tick without Dave's approval. **S4 must add the query guard and the insert in the same commit.**

### Gap 3: Voice streaming is the highest-variance build (L effort, unknown latency)
`voice_service.py:168` sends a single blocking `last=True` frame after `run_in_executor` completes. Barge-in is a no-op. The streaming path (S-2) requires a new streaming endpoint on the web app (`/internal/voice/stream`), async generator in the voice service, sentence-boundary detection, and cancel-flag propagation. Latency on Haiku must be < 1.5s first-word-to-TTS or the call sounds broken. **Do not sell voice until streaming passes the 1.5s gate on real Twilio hardware.** Voice ships last in Phase 5 (5g) for this reason.

### Gap 4: `test_reminders.py` does not exist — F06 ships blind without it
`reminders.py:16` docstring claims unit tests exist. They do not. `test_followup_body` and `test_due_followup_leads` are not in any test file. Shipping Touch 2 (S4) or contextual Sonnet copy (M1) without tests is a reliability risk. **Write `test_reminders.py` (S7 in F06 plan) before any F06 S-builds go live.**

### Gap 5: Google Contacts OAuth creds are an operator prerequisite for F08
`google_contacts.configured()` returns False unless `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set. The nightly re-sync (F08's highest-value addition) is a no-op until the operator wires these. The Google Cloud Console redirect URI registration is not automated. **Jonathan must complete this before the F08 build loop starts.**

### Gap 6: F13 tray depends on `growth_mode` migration from boolean — ordering matters
`db.py:515` column is `("growth_on", "INTEGER DEFAULT 0")`. The migration to `TEXT DEFAULT 'off'` must convert existing rows (0 → 'off', 1 → 'tray') atomically. The `ADDCOLUMN_SENTINEL` pattern in `db.py` handles additive migrations; a rename is more complex. **S3 must use a safe migration: add `growth_mode` TEXT column, backfill from `growth_on`, then remove `growth_on` reads (keep column for backward compat).**

### Gap 7: F07 auto-graduation has no false-positive count mechanism yet
The graduation algorithm (`screened_count >= 10 AND false_positive_count == 0`) requires `business_false_positive_count` to exist on the `businesses` table AND be incremented by the "This was real" rescue endpoint. **S2 (rescue) must ship before or in the same commit as S1 (graduation), or the graduation logic will always see 0 false positives and graduate immediately.**

### Gap 8: SF-10 schema design is a hidden dependency on Phase 5 billing/auth decisions
Phase 5 builds F11 alert routing, F13 growth tray, and F07 reputation toggles — all of which write to `businesses` and read from auth context. If these are built with no org-layer in mind, retrofitting Crew later means touching every feature built in Phase 5. **Write SF10-SCHEMA.md (5h) in the first or second sub-phase loop so builders know which columns go where.**

### Gap 9: Opus is still the default model (cost risk during builds)
`config.py:46` defaults to `claude-opus-4-8`. All test runs, staging sends, and dev builds use Opus. At Phase 5 test volumes this may be fine, but any load test or extended automation run burns margin. **Switch to Sonnet (P0-2 in F11 plan) in the first 5b build loop.** It is a config change, not a code change, and the 46-test suite is model-agnostic.

### Gap 10: No voice cost safeguard exists until F10 S-5 ships
Until `voice_calls` table and `StatusCallback` webhook are built, there is no cap on how much voice time a single tenant can consume. **Do not activate voice for any paying tenant until S-5 (credit metering) and P1-6 (monthly cost cap) ship.** The quality gate checklist is the correct enforcer.

---

## 5. First Sub-Phase to Hand to the Build Loop

**Phase 5a — SF-6 Server-Bound Confirm Token**

Build-ready now. Self-contained. ~40 lines across 3 files. No operator prerequisites. Unblocks 5b (F11 proactive tokens) and 5d (F13 idempotent GO batch). The trust primitive that makes every downstream proactive feature honest.

**Handoff spec (complete from F11-FINAL §4.1):**

```
Files:
  db.py     — CREATE TABLE IF NOT EXISTS pending_confirms (
                 token_id TEXT PRIMARY KEY,
                 bid INTEGER NOT NULL,
                 tool TEXT NOT NULL,
                 args_json TEXT,
                 preview_hash TEXT,
                 expires_at REAL,
                 consumed INTEGER DEFAULT 0,
                 result_json TEXT
               )
  assistant.py:_gated (~line 1897)
              — add _issue_token(bid, tool, args) → token_id = secrets.token_hex(16)
              — INSERT into pending_confirms; expires_at = now + 600 (10 min)
              — return {token_id} in pending_action; NOT {tool, args}
  app.py:582–602
              — rewrite /assistant/confirm:
                  POST body: {confirm_token, csrf}
                  server: look up pending_confirms WHERE token_id=? AND bid=?
                  verify: not expired, consumed == 0
                  execute: stored args (ignore any client-supplied tool/args)
                  mark: consumed=1, store result_json
                  idempotency: second tap returns stored result_json
                  no token: 400. expired: reply "This confirm expired — ask me again."
                  add token_id to db.add_audit row

Tests needed (new test file or extend test_assistant.py):
  - token issuance returns only token_id in pending_action (no tool/args)
  - valid token executes stored args; marks consumed
  - expired token returns 400
  - second tap returns stored result_json without re-executing
  - wrong-bid token returns 400
  - client-supplied tool/args in POST body are ignored
```

---

## 6. Biggest Gap Summary (one sentence each)

- **Trust:** No server-bound confirm token → same-origin client can execute any write tool with arbitrary args → SF-6 ships first, nothing proactive before it.
- **F11:** Entire proactive push layer (7 AM SMS, stall nudge, briefing-enhanced alerts) is unbuilt; depends on SF-6.
- **F07:** Auto-graduation is the feature's core promise and is entirely absent; false-positive rescue must ship in the same commit as the graduation job.
- **F13:** `held` status has no query guard — inserting held rows today would cause auto-fire; S4 must be atomic.
- **F10:** Voice is a deployed-service gap (render.yaml commented out) + a streaming gap (no barge-in); do not sell until both ops and S-2 stream gate pass.
- **F06:** Zero tests for `reminders.py`; write them before shipping Touch 2 or Sonnet copy.
- **F08:** Nightly re-sync is a 20-line add to `tick_once`; biggest gap is the operator OAuth credential prerequisite.
- **SF-10:** No schema design done; design now before Phase 5 auth/billing decisions calcify single-user assumptions.
