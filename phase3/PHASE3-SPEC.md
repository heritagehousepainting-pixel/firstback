# Phase 3 — RECONCILED Build Spec ("time-to-first-value": automated A2P + done-for-you onboarding + auto-flush)
**Date:** 2026-06-18 · **Base:** staging @ 6295f05 (clean, 35/35 green)
**Reconciled by Opus from phase3/AUDITOR-A.md + AUDITOR-B.md.** Source of truth: `COO/firstback-blueprint/F14-ONBOARDING-SPEC.md` (supersedes F14-FINAL registration + blueprint SF-8).
Build agents: honor every **[DECIDED]** and the SHARED SEAMS table exactly. Tests are standalone: `.venv/bin/python test_X.py` (NEVER pytest).

## The principle (F14)
A2P 10DLC is an unavoidable carrier mandate. FirstBack **absorbs it invisibly** and **delivers value day-1 with VOICE (zero registration)** — call screening/forwarding already work. SMS text-back **registers in the background and auto-activates on approval**. Dave NEVER sees "Twilio / A2P / 10DLC / TCR / brand / campaign" — trades language only.

---

## GLOBAL DECISIONS

- **[DECIDED] WRITE API lives in messaging.py** (everything touching Twilio REST lives there; connections.py calls it, never the reverse). Three functions: `create_a2p_brand`, `create_a2p_messaging_service`, `create_a2p_campaign`. Each follows the gated+simulated pattern of `send_sms`/`provision_number`: returns a status dict, **never raises**, logs errors with `[firstback]` prefix.
- **[DECIDED] Separate Trust-Hub gate.** The write functions gate on a NEW `messaging.trust_hub_configured()` = `configured() and bool(TWILIO_TRUST_PRODUCT_SID)` — NOT just `configured()`. If Twilio creds exist but the Trust Hub product SID is unset, the writes **return `{"status":"simulated"}`** and make NO API call. This prevents accidental real, billable, garbage submissions. (Adopted from Auditor B — a real risk.)
- **[DECIDED] Honest status — the cardinal rule.** `submit_a2p` and the write functions may ONLY set `a2p_status="pending"`. **Only `connections.a2p_sync` (which polls Twilio's status endpoint) may ever set `a2p_status="approved"`.** A Twilio CREATE returning HTTP 200 means "submission accepted," NOT "TCR approved." Put this as a literal comment in `submit_a2p`: `# NEVER set status='approved' here -- only a2p_sync() may, after polling.`
- **[DECIDED] Blocked-send persistence = a dedicated `blocked_sends` table** (NOT a scheduled_messages kind — keeps the growth index untouched, and the table's explicit columns directly support the auto-flush safety rules). `send_sms`'s existing blocked branch (messaging.py:132-135) ALREADY calls `db.add_message(lead_id,"out",body)` so the thread shows the would-have-sent text — KEEP that; ADD a `db.queue_blocked_send(...)` call right after it. The `blocked_sends` row is what the flush replays.
- **[DECIDED] EIN fork via an explicit `business_type` column** (`sole_prop` | `llc` | `unknown`), set at signup from "Do you have an EIN?". `_profile_done`/`profile_complete` fork on it: **`sole_prop` → name + business_address (+ phone) is enough, NO EIN; everything else (`llc`/`unknown`) → name + ein + business_address required.** This relaxes the current EIN-always gate that PERMANENTLY BLOCKS sole-proprietors (the majority of solo trades) at the profile step today — fix this first. Path A collects ZERO EIN (an EIN actively disqualifies a sole-prop submission).
- **[DECIDED] No custom OTP token system in Phase 3.** Twilio owns the sole-prop OTP during brand vetting; building our own `otp_code`/verify route against unconfirmed sole-prop mechanics is premature (it's a DEFERRED live-confirmation item). The sole-prop path submits via the WRITE API → `a2p_status="pending"` → `a2p_sync` flips to approved when Twilio vets. We DO include the **priming COPY** (an informational, non-spammy heads-up sent from `ALERT_FROM_NUMBER`, never on page load) but no token redemption. Mark exact OTP mechanics DEFERRED.
- **[DECIDED] Micro-site = a Flask route on the existing app** (`/c/<slug>`), not static generation. Submitted brand opt-in URL = `https://<slug>.<MICRO_SITE_DOMAIN>` which maps to the same handler once the `*.firstback.io` wildcard DNS (OWNER-OPS) is live; `/c/<slug>` works for tests today. The page renders ONLY the contractor's legal name/address/services + SMS opt-in + links to our `/privacy` + `/terms`. **No FirstBack branding visible** (the original denial root cause). No smart/curly quotes (Jinja break).
- **[DECIDED] Auto-flush trigger = inside `a2p_sync`** on the `pending→approved` transition only (the single transition point; fires whether sync came from cron or page-load; idempotent + capped). Wrap the flush call in its own try/except so a flush failure never breaks the sync tick.
- **[DECIDED] convos.py / llm.py NOT touched** (trades_core kernels). Preserve the live host `ringback-gixe.onrender.com`.

---

## AUTO-FLUSH SAFETY SPEC (the highest-risk surface — `connections.flush_blocked_sends`)
Every rule is a HARD gate; violation = skip that row (record `skip_reason`). All flushed sends go through `messaging.send_sms(biz, to, body, lead_id=..., gate=True, transactional=True)` — re-applying the (now-approved) A2P gate, opt-out, and quiet-hours; `transactional=True` makes a solicited text-back quiet-hours-exempt (correct).
1. **Freshness window** — `FIRSTBACK_FLUSH_MAX_AGE_HOURS` (default **6**). A blocked send older than the window is skipped `skip_reason='stale'`. A "we'll text you right back" that lands days later is incoherent (the stored body is the original real-time reply). Path B (1-3 day) will commonly be all-stale → that's correct; surface it to the owner honestly (rule 7), don't send absurd stale texts.
2. **Opt-out (mandatory, pre-send)** — `db.is_suppressed(business_id, to)` → skip `skip_reason='opted_out'`.
3. **Quiet-hours** — inherited via `send_sms(..., transactional=True)`; don't bypass send_sms.
4. **Dedupe / no flush-loop** — `flushed=1` is the state gate; query `WHERE flushed=0`. Set `flushed=1` BEFORE calling send_sms; on a send error, record `skip_reason='send_error'` (do NOT reset to 0 → at most one attempt per row, never a per-tick resend loop).
5. **Ordering + cap** — `ORDER BY blocked_at ASC`, cap **50 per tenant per call**.
6. **Conversation-coherence** — skip `skip_reason='conversation_progressed'` if the lead has any real subsequent message (a `direction in ('in','out')` row with a non-null `provider_sid`, `created_at > blocked_at`) — don't text "thanks for calling!" to someone who already booked/replied.
7. **All-stale degenerate case** — if every row skips stale, that's correct; the honest owner-facing copy is "Texting is now live — past callers from the activation window were too old to auto-text; new callers are texted instantly." Do NOT re-text multi-day-old callers.
8. **Still-blocked guard** — if `send_sms` returns `blocked` during flush (should be impossible post-approval), log an error and STOP the flush (state inconsistency; no retry).
`flush_blocked_sends(business_id) -> {"flushed":N,"skipped":N,"errors":N}`, never raises.

---

## SHARED SEAMS (canonical signatures — match exactly)

| Seam | Owner | Callers |
|---|---|---|
| `config.TWILIO_TRUST_PRODUCT_SID` / `TWILIO_A2P_RESELLER_SID` / `GOOGLE_PLACES_API_KEY` / `MICRO_SITE_DOMAIN`(="firstback.io") / `CLIENTS_EMAIL_DOMAIN`(="clients.firstback.com") / `FLUSH_MAX_AGE_HOURS`(=6) | **A1** (config.py) | A1/A2/A3 |
| `messaging.trust_hub_configured() -> bool` | **A1** | A1, A2 |
| `messaging.create_a2p_brand(business) -> dict` (status: simulated/created[+brand_sid]/error) | **A1** | A2 |
| `messaging.create_a2p_messaging_service(business) -> dict` (+messaging_service_sid) | **A1** | A2 |
| `messaging.create_a2p_campaign(business, messaging_service_sid, brand_sid) -> dict` (+campaign_sid; UseCase=CUSTOMER_CARE) | **A1** | A2 |
| `db.queue_blocked_send(business_id, lead_id, to, body, blocked_at=None) -> int|None` | **A1** | A1 (in send_sms), A2 (flush reads) |
| `db.get_blocked_sends(business_id, flushed=False, limit=50) -> list[dict]` | **A1** | A2 |
| `db.mark_flushed(blocked_send_id)` / `db.mark_flush_skipped(blocked_send_id, reason)` | **A1** | A2 |
| `connections.registration_path(biz) -> "sole_prop"|"llc"|"unknown"` | **A2** | A2, A3 |
| `connections.submit_a2p(business_id) -> dict` (status: simulated/submitted/error; NEVER approved) | **A2** | A3 (setup_a2p mode=auto) |
| `connections.flush_blocked_sends(business_id) -> dict` | **A2** | A2 (a2p_sync hook) |
| `connections.build_slug(name, business_id) -> str` / `build_contact_email(slug) -> str` | **A2** | A2, A3 (microsite display) |
| `db.set_business_type(business_id, business_type)` | **A1** | A3 (signup) |
| `db.set_micro_site(business_id, slug, contact_email)` | **A1** | A2 (submit_a2p, Path B) |

**Existing (confirmed; do not redefine):** `db.set_a2p_registration`(957), `db.set_a2p_status`(948), `db.is_suppressed`(1944), `db.list_businesses`(2231), `connections.a2p_sync`(243, MODIFIED for the flush hook), `connections.a2p_sync_all`(263), `connections.profile_complete`(43)/`_profile_done`(39, MODIFIED to fork), `messaging.send_sms`(69), `messaging.fetch_a2p_campaign_status`(323), `messaging.configured`(46), `compliance.a2p_ready/a2p_status`.

---

## MIGRATIONS (db.init_db, guarded; ALL owned by A1)
```python
# Phase 3 — A2P path fork (business_type) on businesses
biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
for col, ddl in (("business_type", "TEXT DEFAULT 'unknown'"),
                 ("micro_site_slug", "TEXT"),
                 ("a2p_contact_email", "TEXT")):
    if col not in biz_cols:
        c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")

# Phase 3 — blocked_sends: persisted text-backs to replay on A2P approval (auto-flush)
c.execute("""CREATE TABLE IF NOT EXISTS blocked_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL, lead_id INTEGER,
    to_number TEXT NOT NULL, body TEXT NOT NULL, blocked_at TEXT NOT NULL,
    flushed INTEGER DEFAULT 0, flushed_at TEXT, skip_reason TEXT)""")
c.execute("CREATE INDEX IF NOT EXISTS idx_blocked_sends_biz ON blocked_sends(business_id, flushed)")
```
(No scheduled_messages / growth-index change — blocked_sends is self-contained.)

---

## CLASSIFICATION
**CODE (build + test now, mocked):** all 3 write functions + `trust_hub_configured`; `submit_a2p` dispatch; `flush_blocked_sends` + all 8 safety rules; EIN fork (`business_type`, `_profile_done`/`registration_path`); `queue_blocked_send` in send_sms + the blocked_sends table/functions; `/c/<slug>` route + microsite.html; `/api/places/lookup` (gated on key); signup EIN fork; setup_a2p `mode=auto`; honest wait copy; privacy.html SMS section; build_slug/build_contact_email; the a2p_sync flush hook.
**OWNER-OPS:** `firstback.io` domain + `*.firstback.io` wildcard DNS → app; Cloudflare Email Routing catch-all `@clients.firstback.com`; `TWILIO_TRUST_PRODUCT_SID` (+ optional reseller SID) in Render; `GOOGLE_PLACES_API_KEY`; the Heritage dogfood submission. **No test calls real Twilio** (brand=$4/campaign=$10 + real TCR).
**DEFERRED (un-verifiable until the 2 live confirmations in F14):** (HC-1) does `<slug>.firstback.io` pass TCR for a contractor brand (Likely, not Confirmed); (HC-2) does `<slug>@clients.firstback.com` satisfy Twilio Authentication+ for Standard brands; (HC-3) the exact sole-prop Starter-brand payload + OTP mechanics. Ship these behind config/gates, label "pending live confirmation" in code comments + test notes, and NEVER let UI claim "verified/live" off a 200 — only `a2p_sync` confirms.

---

## PARTITION — clean file-disjoint 3-way (NO shared files, NO app.py line-splitting)

### AGENT 1 — Foundation: db.py + config.py + messaging.py
- config: the 6 new vars above (all `os.environ.get(..., default)`).
- db migrations (above) + `set_business_type`, `set_micro_site`, `queue_blocked_send`, `get_blocked_sends`, `mark_flushed`, `mark_flush_skipped`.
- messaging: `trust_hub_configured()`; `create_a2p_brand`/`create_a2p_messaging_service`/`create_a2p_campaign` (gated on `trust_hub_configured()`, never raise, CUSTOMER_CARE hardcoded, include reseller `IsrId` only when `TWILIO_A2P_RESELLER_SID` set, **never log EIN/address values** — log business_id + HTTP status only; Path-A sole-prop brand payload differs from Standard — add a `# DEFERRED HC-3: confirm sole-prop Starter payload with one real submission` comment, mock in tests). In `send_sms`'s blocked branch (after the existing `add_message`), add `db.queue_blocked_send(business["id"], lead_id, to, body)`.
- **Tests:** `test_sf8_write_api.py` (simulated when trust_hub unconfigured; correct payload assembled on mocked 200; error dict on mocked 4xx; reseller IsrId present only when set; CUSTOMER_CARE present; **EIN/address NOT in any stderr log**), `test_sf8_persist.py` (send_sms blocked → a real blocked_sends row exists in a real DB; get_blocked_sends/mark_flushed/mark_flush_skipped round-trip; migration idempotent over two init_db()).
- **Re-run:** test_migration, test_config_hub, test_compliance, test_compliance_core, test_webhooks.

### AGENT 2 — Orchestration: connections.py
- `registration_path(biz)` (reads business_type); fork `_profile_done`/`profile_complete` per [DECIDED] (sole_prop relaxes EIN; llc/unknown require it).
- `build_slug(name, business_id)` (lowercase, `[^a-z0-9]+`→`-`, strip, ≤40, append `-{id}` for uniqueness) / `build_contact_email(slug)` = `{slug}@{CLIENTS_EMAIL_DOMAIN}`.
- `submit_a2p(business_id)`: dispatch by `registration_path`. Path B: `build_slug`+`build_contact_email`→`db.set_micro_site`, re-fetch biz, then `create_a2p_brand`→`create_a2p_messaging_service`→`create_a2p_campaign` in order, short-circuit + return `{"status":"error","step":...}` on any error; on full success `db.set_a2p_registration(..., status="pending", submitted_at=now)`. Path A: same call chain with sole-prop biz (no micro-site, no EIN field). Returns simulated when `not trust_hub_configured()`. **NEVER sets approved** (literal comment).
- `flush_blocked_sends(business_id)` — the full safety spec above.
- Modify `a2p_sync`: when `mapped == "approved" and current != "approved"`, after `db.set_a2p_status`, call `flush_blocked_sends(biz["id"])` inside its own try/except (log + continue; never break the sync).
- **Tests:** `test_sf8_connections.py` — `registration_path`; `_profile_done` fork (sole_prop ok w/o EIN; llc needs EIN; unknown needs EIN — so existing tests stay green); `build_slug`/`build_contact_email`; `submit_a2p` simulated when trust-hub off, calls brand→svc→campaign in order on mocked success, returns error+step on first failure WITHOUT calling later steps, **never sets approved**; `flush_blocked_sends` against a REAL db (real blocked_sends rows) with `messaging.send_sms` patched — assert: fresh row flushed; stale row skipped `stale`; opted-out skipped `opted_out`; conversation-progressed skipped; dedupe (flushed=1 before send, no resend on second call); cap 50; `a2p_sync` fires flush exactly once on pending→approved and NOT on approved→approved. Stub A1's not-yet-existing funcs (`create_a2p_*`, `trust_hub_configured`, `queue/get_blocked_sends`, `set_micro_site`, `set_business_type`) so tests pass standalone.
- **Re-run:** test_connect_hub, test_setup, test_compliance.

### AGENT 3 — Surface: app.py + templates
- `/signup` POST: read `has_ein` checkbox → `db.set_business_type(bid, "llc" if has_ein else "sole_prop")` after create_business. (phone already → alert_sms.)
- `setup_a2p` (1126): add `mode in ("auto","submit")` → guard `connections.profile_complete`, call `connections.submit_a2p`, redirect `?saved=a2p` / `?err=a2p_submit`; default mode → "auto"; **keep `mode=record` operator path unchanged**.
- NEW `/c/<slug>` route (public, near /privacy ~492): look up business by `micro_site_slug`, 404 if none, render `templates/microsite.html` with ONLY contractor name/address/services + SMS opt-in + /privacy + /terms links. **No FirstBack branding; no smart quotes.**
- NEW `/api/places/lookup` (login_required, gated on `GOOGLE_PLACES_API_KEY`, returns `{}` when unset/error) — business-name prefill.
- Honest wait UX (setup.html a2p step): plain trades English — "Your AI is already answering calls. Texting turns on automatically, usually within a day." + the priming line for sole-prop ("You'll get a text shortly — reply YES to switch texting on") shown on a button, NOT auto-sent. **Zero Twilio/A2P/10DLC/TCR/brand/campaign jargon anywhere Dave sees.**
- privacy.html: add a `Text messaging` section (SMS opt-out + "we don't share mobile opt-in data") before Data retention. Straight ASCII quotes only.
- **Tests:** `test_sf8_microsite.py` (GET /c/<slug> 200 + shows legal name + "Reply STOP" + NO "Twilio"/"A2P" in HTML + /privacy link; unknown slug 404; privacy.html has the SMS section), `test_sf8_signup_fork.py` (signup with has_ein → business_type llc; without → sole_prop; setup_a2p mode=auto calls submit_a2p — patched). Stub connections/db seams.
- **Re-run:** test_setup, test_demo_public, test_webhooks.

**Merge order:** A1 first (migrations + seams), then A2, then A3. Full suite green after each.

## REVIEW GATE (Opus, post-merge) — Phase-2 lesson: stubbed tests hide integration bugs.
Run a REAL un-stubbed end-to-end after merge: real db.init_db → create business (sole_prop, a2p unregistered) + lead → `send_sms` (configured patched True, a2p_ready False) → assert a real `blocked_sends` row → flip `set_a2p_status("approved")` → `a2p_sync` (or `flush_blocked_sends`) with only Twilio HTTP patched → assert the row flushed (and a stale one + an opted-out one are correctly skipped). Then `be-audit`-style pass on the A2P/PII path (EIN not logged; trust_hub gate; never-approved-on-200).

## OWNER-OPS (append to SETUP_NEEDED.md): firstback.io domain + `*.firstback.io` wildcard DNS → app · Cloudflare Email Routing catch-all `@clients.firstback.com` · `TWILIO_TRUST_PRODUCT_SID` (+ optional `TWILIO_A2P_RESELLER_SID`) · `GOOGLE_PLACES_API_KEY` · Heritage dogfood submission · the 2 live confirmations (HC-1 ISV-subdomain passes TCR; HC-2 catch-all email passes Authentication+).
