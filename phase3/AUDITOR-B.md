# Phase 3 — Auditor B Spec
**Date:** 2026-06-18 · Branch: staging @ 6295f05 · Role: risk/honesty lens, parallel to Auditor A
**Author note:** I read the real code before writing this. Every anchor is verified. Where I say "does not exist," I checked.

---

## 0. What I verified before specifying

| Claim in brief | Verified in code |
|---|---|
| A2P columns exist on businesses | db.py:495–505 — yes, all 7 columns |
| `set_a2p_registration` exists | db.py:957 — yes |
| READ side `a2p_sync` / `a2p_sync_all` exist | connections.py:243, 263 — yes |
| `fetch_a2p_campaign_status` exists | messaging.py:323 — yes |
| `a2p_ready` / `a2p_status` exist | compliance.py:42, 38 — yes |
| `send_sms` returns `blocked` when not approved | messaging.py:132–135 — yes |
| Blocked sends are **dropped, not persisted** | WRONG — they ARE persisted as `direction='out'` rows (messaging.py:133-134: `db.add_message(lead_id, "out", body)`) but with NO `send_status` column to distinguish them from real sends |
| WRITE API (create_brand / create_campaign) | DOES NOT EXIST — confirmed by grep across all .py files |
| EIN fork / sole-prop path | DOES NOT EXIST — `_profile_done` (connections.py:39) requires EIN always |
| Micro-site generator | DOES NOT EXIST |
| Per-contractor email provisioning | DOES NOT EXIST |
| OTP verification for sole-prop | DOES NOT EXIST |
| Auto-flush on approval | DOES NOT EXIST |

**Critical discovery:** `_profile_done` (connections.py:39) gates progress on `biz.get("ein")`. Sole Proprietors (Path A, the majority) have no EIN. They are currently **permanently blocked** at the profile step. Phase 3 must fix this fork before the WRITE API is meaningful.

---

## 1. Work-stream Partition

### WS-1: EIN Fork — Signup Bifurcation (db.py + app.py + connections.py)
The missing day-one split. Without it, sole-props can never register and the WRITE API serves zero of them.

**RISK LANE:** A Sonnet agent mis-routes by collecting EIN from a sole-prop ("just in case"), which actively disqualifies them (Twilio rejects sole-prop submissions that include an EIN, treating them as mismatched Standard brands). The sole-prop path MUST ask for zero EIN — just name, personal address, Gmail, mobile number.

### WS-2: Twilio Trust Hub WRITE API — Sole-Prop Path A (messaging.py / connections.py)
`create_sole_prop_brand(business_id)` → Twilio `/2010-04-01/Regulatory/...` + OTP SMS submission. The sole-prop path skips the micro-site entirely; the OTP IS the opt-in verification.

**RISK LANE:** The WRITE API fires unconditionally when `messaging.configured()` is True. If Twilio creds are set but the Trust Hub Reseller ID (`TWILIO_TRUST_PRODUCT_SID`) is missing or wrong, real (billable, garbage) submissions go to Twilio. This must be gated on a SEPARATE env var (`TWILIO_TRUST_PRODUCT_SID`) that defaults to empty, with a simulated path when unset — exactly like `messaging.configured()` gates SMS sends.

### WS-3: Micro-site Generator + Per-contractor Email — Path B LLC (new: microsite.py)
`generate_microsite(business_id)` → writes `[slug].firstback.io` page (Jinja template → static file or Flask route). Email routing is an OWNER-OPS step (Cloudflare catch-all rule), not code.

**RISK LANE:** (a) The micro-site template must render the **contractor's** legal business name, address, and services — not any FirstBack branding. FirstBack branding on the page is the original denial root cause. (b) Smart quotes in the Jinja template (`"`, `'`, `'`) break Jinja rendering silently (Jinja2 parses them as unknown character references, not string delimiters). All template strings must use ASCII quotes. (c) The `/privacy` route must include the SMS-specific "we never sell mobile opt-in data" language carriers check; a generic privacy policy from TermsFeed is rejected.

### WS-4: Trust Hub WRITE API — Standard/LLC Path B (messaging.py / connections.py)
`create_standard_brand(business_id)` + `create_messaging_service(business_id)` + `create_a2p_campaign(business_id)` using the micro-site URL and per-contractor email as the brand opt-in URL and authorized-rep contact. Must pass `reseller_sid=TWILIO_TRUST_PRODUCT_SID` on every call.

**RISK LANE:** Same as WS-2: gated on `TWILIO_TRUST_PRODUCT_SID`. Additional risk: the agent marks `a2p_status='pending'` immediately after the CREATE calls succeed, without waiting for TCR confirmation. This is correct — but it must NOT mark `a2p_status='approved'` until `a2p_sync` confirms via Twilio's status endpoint. The cardinal sin: setting `approved` locally because the HTTP POST returned 200 from Twilio (that just means "submission accepted," not "TCR approved").

### WS-5: OTP Verification — Sole-Prop (app.py route + db.py)
Route: `POST /setup/a2p/verify-otp`. Sends a verification SMS to the contractor's mobile number, stores a time-limited token (not in the `businesses` table — add `otp_code TEXT, otp_expires_at TEXT` to businesses or a separate table), and redeems it. On redemption, triggers WS-2.

**RISK LANE:** OTP must be sent from the PLATFORM alert number (`ALERT_FROM_NUMBER`), not the tenant's A2P number (which isn't approved yet and can't send). If `ALERT_FROM_NUMBER` is unset, the OTP can't send — gate this with a clear honest error, not a silent drop. Time-limit: 10 minutes max; single-use.

### WS-6: Status Polling + Auto-Activation (connections.py + app.py /tasks/run-due)
When `a2p_sync` transitions any business from `pending` → `approved`, trigger auto-flush (WS-7). Already hooked into `/tasks/run-due` (app.py:2262), but the transition callback doesn't exist yet.

**RISK LANE:** The status check runs inside `/tasks/run-due` which uses a single cron tick. If `a2p_sync_all` flips a status to `approved` but the flush raises an exception, the exception is currently swallowed (the try/except at app.py:2263 swallows all). The auto-flush must be a separate try/except so a flush failure doesn't block the next cron tick, AND must be idempotent so a partial flush can be retried.

### WS-7: Auto-Flush — Replay Blocked Text-backs on Approval (db.py + messaging.py + connections.py)
The only new table: `blocked_sends`. On every `send_sms` that returns `blocked`, persist to `blocked_sends` (not just `messages`). On approval, flush with safety rules (see §4 below).

**RISK LANE:** This is the highest-risk correctness surface. See §4.

### WS-8: Honest Wait UX + Drip (app.py + setup.html + templates)
While A2P is pending: show honest wait copy ("Your AI is already answering calls. Texting turns on in the background — usually within a day."). No jargon. No Dave-facing "A2P/10DLC/TCR/brand/campaign." Voice is the day-1 value; the wait is the hook to retain.

---

## 2. Three-Way File Partition (collision isolation)

### db.py
**New additions ONLY — no edits to existing functions:**
- `WS-1:` Add `business_type TEXT DEFAULT 'unknown'` (values: `sole_prop`, `llc`) + `otp_code TEXT` + `otp_expires_at TEXT` to businesses (guarded ALTER TABLE migration)
- `WS-5:` `set_otp(business_id, code, expires_at)` · `get_otp(business_id)` · `clear_otp(business_id)`
- `WS-7:` New table `blocked_sends` (schema below) + `queue_blocked_send(business_id, lead_id, to, body, created_at)` + `get_blocked_sends(business_id)` + `mark_flushed(blocked_send_id)` + `mark_flush_skipped(blocked_send_id, reason)`

**Do not touch:** `set_a2p_registration`, `set_a2p_status`, `add_message`, `get_messages`, any existing `scheduled_messages` functions, any `contacts_consent` functions.

### messaging.py
**New additions ONLY:**
- `WS-2:` `create_sole_prop_brand(business_id)` → returns `{"status": "submitted", "sid": ...}` or `{"status": "simulated"}` or `{"status": "error", "error": ...}`
- `WS-4:` `create_standard_brand(business_id)` · `create_messaging_service(business_id)` · `create_a2p_campaign(business_id, brand_sid, service_sid)`
- `WS-7:` `flush_blocked_sends(business_id)` — the safety-gated replay (see §4)
- New env read: `TWILIO_TRUST_PRODUCT_SID = os.environ.get("TWILIO_TRUST_PRODUCT_SID", "")` · `def trust_hub_configured(): return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_TRUST_PRODUCT_SID)`

**Edit (carefully — surgical):**
- `send_sms` lines 132–135: add `db.queue_blocked_send(...)` call alongside the existing `db.add_message`. The message row on the thread stays (UI still shows the "would-have-sent" message); the blocked_send row is what the flush uses. Preserve the existing `if lead_id is not None: db.add_message(lead_id, "out", body)` so the dashboard thread is unchanged.

**Do not touch:** `send_sms` gate logic · `configured()` · `fetch_a2p_campaign_status` · `provision_number` · `valid_signature` · `place_call`

### connections.py
**New additions ONLY:**
- `WS-1:` `registration_path(biz)` → `"sole_prop"` | `"llc"` | `"unknown"` (reads `business_type`)
- `WS-2/4:` `submit_a2p(business_id)` — dispatches to `messaging.create_sole_prop_brand` or `messaging.create_standard_brand` based on `registration_path`; updates `set_a2p_registration` on success; honest: never sets `approved` from this function, only `pending` or `submitted`
- `WS-6:` Modify `a2p_sync` to return `(new_status, changed: bool)` — **backward-compat**: the callers that use the return as a status string still work if we return just the status; change the internal logic to also call `flush_blocked_sends` when `changed and new_status == "approved"`. This is the only surgical edit to an existing function.

**Edit (surgical):**
- `a2p_sync` (connections.py:243–260): wrap the `db.set_a2p_status` call to also call `messaging.flush_blocked_sends(biz["id"])` when the status flips to `approved`. Keep return value as status string for backward compat.
- `_profile_done` (connections.py:38–40): EIN is now optional for sole-props. Change to `return bool(biz.get("name") and biz.get("business_address") and (biz.get("ein") or biz.get("business_type") == "sole_prop"))`. This is the fix for the sole-prop profile block.

**Do not touch:** `step_state` · `is_live` · `blockers` · `golive_summary` · `recommended_setup` · `forwarding_code` · `send_sentinel_call`

### app.py
**New routes ONLY:**
- `POST /setup/a2p/path` — sets `business_type` (sole_prop or llc) from the fork form; redirects to /setup
- `POST /setup/a2p/otp/send` — sends OTP to owner's mobile (WS-5)
- `POST /setup/a2p/otp/verify` — redeems OTP, triggers `connections.submit_a2p` for sole-prop (WS-5)
- `POST /setup/a2p/submit` — replaces (or extends) `setup_a2p` to dispatch to WRITE API for both paths (WS-2/4); the old `mode=record` operator paste stays as a fallback

**Edit setup_a2p (app.py:1126):** The current route becomes the fallback; `mode=submit` now routes through `connections.submit_a2p` when `trust_hub_configured()`, else falls through to the existing operator-email flow. Preserve the `mode=record` operator path unchanged.

### New file: microsite.py (WS-3)
`generate_microsite(business_id)` · `microsite_url(business_id)` · `microsite_slug(biz_name)` · `privacy_url()` (points to FirstBack's own `/privacy` with SMS language)

### New file: test_phase3_a2p.py (test harness — see §5)

---

## 3. Guarded Migrations

All new columns must use the EXISTING pattern in `db.py` (checked against the col list before ALTER TABLE):

```python
# In db.init() after existing biz_cols checks (around line 515):
for col, ddl in (
    ("business_type", "TEXT DEFAULT 'unknown'"),
    ("otp_code",      "TEXT"),
    ("otp_expires_at","TEXT"),
):
    if col not in biz_cols:
        c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")

# New table — blocked_sends (create-if-not-exists is safe to repeat):
c.execute("""
    CREATE TABLE IF NOT EXISTS blocked_sends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL,
        lead_id INTEGER,
        to_number TEXT NOT NULL,
        body TEXT NOT NULL,
        blocked_at TEXT NOT NULL,
        flushed INTEGER DEFAULT 0,
        flushed_at TEXT,
        skip_reason TEXT
    )
""")
c.execute("""
    CREATE INDEX IF NOT EXISTS idx_blocked_sends_biz
    ON blocked_sends(business_id, flushed)
""")
```

**Migration ordering:** `blocked_sends` creation must run AFTER the `messages` migration block (currently around line 517) so it sees the same connection. Add it in the same `init()` pass — no separate migration file needed.

---

## 4. Auto-Flush Safety Spec

This is the highest-risk correctness surface. Every rule below is a HARD gate — violation = don't flush that row.

### 4.1 Freshness window
**MAX_FLUSH_AGE_HOURS = 6** (configurable via `FIRSTBACK_FLUSH_MAX_AGE_HOURS`, default 6).
A blocked send older than 6 hours is **skipped** with `skip_reason='stale'`. Rationale: a "sorry I missed your call" that arrives 3 days later is not only useless, it damages trust. 6 hours is the upper bound where the caller still remembers calling and the text is still coherent.

### 4.2 Opt-out check (mandatory, pre-flush)
Call `db.is_suppressed(business_id, to_number)` before every flush attempt. If True, skip with `skip_reason='opted_out'`. A lead who sent STOP during the A2P pending window must never be texted on flush.

### 4.3 Quiet-hours backstop
The flush is a deferred transactional send. It must pass through the same `send_sms` call (not bypass it). This means: use `send_sms(business, to, body, lead_id=lead_id, gate=True, transactional=True)`. This inherits the A2P gate (now approved — passes), the opt-out gate (passes), and the quiet-hours gate. However: flushed sends are transactional responses, not marketing — they MUST pass `transactional=True` so quiet-hours doesn't block them. This is correct behavior: a caller who called at 2pm and you're approving at midnight should receive the text in the morning. But the quiet-hours backstop only applies to `transactional=False`. Since flushed text-backs ARE transactional (response to a call the person placed), `transactional=True` is correct and they go immediately.

**EXCEPTION:** A caller who called during quiet hours and whose text-back was blocked because A2P wasn't approved — not because of quiet hours — should still get the text now. This is fine: `transactional=True` sends immediately, which is correct.

### 4.4 Dedupe (prevent duplicate flush)
`flushed=1` in `blocked_sends` is the state gate. `mark_flushed` sets `flushed=1, flushed_at=now`. The flush query is `WHERE flushed=0 AND business_id=?`. The sequence must be atomic enough that a retry tick doesn't double-send: set `flushed=1` BEFORE calling `send_sms`, then if `send_sms` fails (returns `error`), set `flushed=0` back or log `skip_reason='send_error'` (one retry max). This prevents the "flush loop" where an error causes re-sending on every tick.

### 4.5 Ordering
Flush oldest first (`ORDER BY blocked_at ASC`). If two text-backs exist for the same lead, send them in the order they were blocked. Cap total flush to 50 per tenant per tick to prevent a burst if a tenant had a long pending window.

### 4.6 Conversation coherence check
Before flushing, check whether the lead has ALREADY received or replied to messages since the block. Use:
```python
recent = db.get_messages(lead_id)  # existing function
has_subsequent = any(m["created_at"] > blocked_at for m in recent 
                     if m["direction"] in ("in", "out") and m.get("provider_sid"))
```
If `has_subsequent` (a real subsequent message exists with a provider_sid, meaning it actually sent), skip with `skip_reason='conversation_progressed'`. This prevents sending "Hi, thanks for calling!" AFTER the lead has already booked, replied, or received a follow-up.

### 4.7 The degenerate case: what if ALL leads from the pending window are stale?
This is the common case for a tenant who was pending for 3+ days. The flush runs, 100% skip with `skip_reason='stale'`. This is correct — those text-backs are dead. The honest UX is: show the owner "Your messaging is now live. Past missed callers from the approval window were too old to contact — new callers from here will be texted instantly." Do NOT try to re-text 3-day-old callers.

### 4.8 What if `send_sms` returns `blocked` again?
This would mean A2P is still not approved at flush time, which should be impossible (flush only fires from `a2p_sync` on `approved` transition). Guard: if `send_sms` returns `blocked`, log an error and stop the flush. Do NOT retry — this is a state inconsistency that needs operator attention.

---

## 5. Shared Seams

### Seam A: `a2p_sync` transition callback
`a2p_sync` (connections.py:243) currently returns status string. The flush trigger must fire exactly once per `pending→approved` transition, not on every sync of an already-approved tenant. Guard: `if mapped == "approved" and current != "approved": flush`. The existing `if mapped and mapped != current` check (line 257) is the right anchor.

### Seam B: `send_sms` blocked path
`send_sms` (messaging.py:132–135) currently writes `add_message` on block. Phase 3 adds `db.queue_blocked_send(...)` at the same point. These two writes must be in the same code path — don't split them or one can be skipped by a code path change. The `queue_blocked_send` call goes AFTER the `add_message` call (preserving existing behavior first).

### Seam C: `/tasks/run-due` error isolation
`a2p_sync_all` (app.py:2262) is already in a `try/except`. The `flush_blocked_sends` call added inside `a2p_sync` must also be wrapped: if the flush raises, it must log and continue — never fail the sync tick.

### Seam D: OTP send uses `ALERT_FROM_NUMBER`
The OTP for Path A sole-prop goes to the contractor's mobile. This is NOT a customer-facing text — it's an owner alert. Use `gate=False` and `ALERT_FROM_NUMBER`. Do NOT attempt to send it from the tenant's provisioned number (not A2P approved yet).

### Seam E: `_profile_done` / `profile_complete` fork
Both `connections._profile_done` and `connections.profile_complete` are called from test_setup.py. The edit to `_profile_done` must not break existing tests. The existing test at line 290-293 of test_setup.py (`db.update_a2p_profile(1, {"ein": ""}); r = client.post("/setup/a2p", data={"mode": "submit"})`) expects `err=profile` when EIN is blank for what is currently always an LLC-implied business. After the fork, this test must be updated to set `business_type='llc'` before removing the EIN.

---

## 6. New Env Vars (all optional — safe no-op when unset)

| Var | Default | Purpose |
|---|---|---|
| `TWILIO_TRUST_PRODUCT_SID` | `""` | Trust Hub Reseller product SID. Empty = simulated WRITE API (no-op, like unconfigured Twilio). |
| `FIRSTBACK_FLUSH_MAX_AGE_HOURS` | `6` | Max age in hours for a blocked send to be flushed. |
| `FIRSTBACK_MICROSITE_BASE_URL` | `""` | Base URL for contractor micro-sites (e.g. `https://clients.firstback.io`). Empty = no micro-site, Path B falls back to operator paste. |

---

## 7. Function Signatures

```python
# db.py
def queue_blocked_send(business_id, lead_id, to, body, blocked_at=None): ...
def get_blocked_sends(business_id, flushed=False, limit=50): ...
def mark_flushed(blocked_send_id): ...
def mark_flush_skipped(blocked_send_id, reason): ...
def set_otp(business_id, code, expires_at): ...
def get_otp(business_id): ...  # returns {"otp_code": ..., "otp_expires_at": ...} or None
def clear_otp(business_id): ...

# messaging.py
TWILIO_TRUST_PRODUCT_SID = os.environ.get("TWILIO_TRUST_PRODUCT_SID", "")

def trust_hub_configured() -> bool:
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_TRUST_PRODUCT_SID)

def create_sole_prop_brand(business_id) -> dict:
    """Submit a Sole Proprietor A2P brand via Trust Hub WRITE API.
    Returns {"status": "submitted", "brand_sid": ...} | {"status": "simulated"} | {"status": "error", "error": ...}.
    Safe no-op (simulated) when trust_hub_configured() is False."""

def create_standard_brand(business_id) -> dict:
    """Submit a Standard (LLC) A2P brand. Requires legal_business_name, ein, business_address,
    and a microsite URL. Returns same shape as create_sole_prop_brand."""

def create_messaging_service(business_id, brand_sid) -> dict:
    """Create a Twilio Messaging Service and attach the tenant's number."""

def create_a2p_campaign(business_id, messaging_service_sid) -> dict:
    """Register an A2P campaign (use case: Customer Care) on the messaging service."""

def flush_blocked_sends(business_id) -> dict:
    """Replay all un-flushed blocked sends for a business that has just been approved.
    Returns {"flushed": N, "skipped": N, "errors": N}. Never raises."""

# connections.py
def registration_path(biz) -> str:
    """'sole_prop' | 'llc' | 'unknown' based on biz['business_type']."""

def submit_a2p(business_id) -> dict:
    """Dispatch to the WRITE API for the tenant's registration path.
    Returns {"status": "submitted" | "simulated" | "pending_otp" | "error", ...}.
    Never raises. Updates set_a2p_registration on success."""

# microsite.py
def microsite_slug(biz_name: str) -> str:
    """Lowercase, hyphenated, alpha-numeric slug from business name."""

def generate_microsite(business_id) -> str:
    """Render the per-contractor Jinja template to the micro-site path.
    Returns the public URL (FIRSTBACK_MICROSITE_BASE_URL/slug) or '' if unconfigured."""

def microsite_url(business_id) -> str:
    """The public URL for a contractor's micro-site, or '' if not generated."""
```

---

## 8. Honest-Ceiling Section

The following cannot be verified until real live confirmations happen. These MUST ship behind gates and be labeled honestly in any UI copy or test comments.

### HC-1: ISV-subdomain TCR acceptance (PENDING LIVE CONFIRMATION)
F14 spec §"Two confirmations before scaling": does a `[slug].firstback.io` page actually pass TCR for a contractor brand? Confidence: Likely, not Confirmed. Until confirmed:
- Path B (LLC) must show "submitted — pending carrier review (1–3 business days)" — exactly what it shows now for the operator-paste path.
- The agent must NOT claim "your site has been verified" after the CREATE API call returns 200. Twilio's 200 = submission accepted; TCR approval is a separate asynchronous step that `a2p_sync` polls.
- The `generate_microsite` function may run and the URL may be submitted, but the spec note in the test file must say: `# NOTE: ISV-subdomain passing TCR is Likely, not Confirmed (HC-1). One real test submission required.`

### HC-2: Authentication+ email rule for Standard brands (PENDING LIVE CONFIRMATION)
F14 spec §"Two confirmations before scaling": does `{slug}@clients.firstback.com` satisfy Twilio's CSP Authentication+ requirement for Standard brand authorized-rep contact? Confidence: Likely, not Confirmed. Until confirmed:
- The email field in `create_standard_brand` uses the per-contractor Cloudflare catch-all address.
- Ship behind a `FIRSTBACK_A2P_EMAIL_DOMAIN` env var (default `clients.firstback.com`). If the live confirmation fails, the operator changes the env var without a code deploy.
- Owner-ops requirement: Cloudflare Email Routing catch-all must be live BEFORE any LLC tenant is submitted. This is an OWNER-OPS prerequisite, not code.

### HC-3: What tests prove vs. what they don't
All WRITE API tests use a stubbed Twilio HTTP layer (the `_no_net` tripwire pattern from test_setup.py). Green tests prove:
- Correct JSON is assembled for Trust Hub requests
- The `simulated` path fires when `trust_hub_configured()` is False
- `set_a2p_registration` is called with the right SIDs
- `a2p_sync` flips status only from actual polled confirmation (not from the POST response)
- Auto-flush respects all 7 safety rules

Green tests DO NOT prove:
- That Twilio's Trust Hub API actually accepts the payload structure
- That TCR approves submissions from a FirstBack ISV sub-account
- That the Cloudflare catch-all email satisfies Authentication+

These require the 2 live confirmations in F14.

---

## 9. Risk Lanes (per work-stream)

### RL-1: Sole-prop EIN collection
**Most likely failure:** Agent asks for EIN from sole-props "as an optional field" or "in case they have one." This is wrong: collecting EIN from a sole-prop during Twilio Trust Hub submission triggers a mismatch — Twilio's sole-prop path expects SSN (not submitted) or zero tax ID. The form for Path A must have zero EIN field. The fork is binary: no EIN = Path A (sole-prop), has EIN = Path B (standard).

**How to detect:** Check the Jinja template for Path A setup — if the EIN field is visible for `business_type == 'sole_prop'`, it's wrong.

### RL-2: Claiming SMS "live/approved" before confirmation
**Most likely failure:** `create_sole_prop_brand` or `create_standard_brand` returns 200 from Twilio, and the agent sets `a2p_status='approved'` (or marks the step "done") immediately. Twilio's CREATE response = submission accepted. TCR approval is days later, polled via `fetch_a2p_campaign_status`. The agent may confuse the two because the API response is success-shaped.

**Guard:** `submit_a2p` must ONLY call `db.set_a2p_registration(..., status='pending', submitted_at=now)`. Only `a2p_sync` (which polls the status endpoint) may set `status='approved'`. This is an architectural rule — enforce it with a comment in `submit_a2p`: `# NEVER set status='approved' here. Only a2p_sync() may do that.`

### RL-3: Auto-flush replaying stale / conversation-progressed sends
**Most likely failure:** Flush fires, ignores the 6-hour freshness window, sends "Hi, we just missed your call!" to a lead who called 4 days ago, has since booked, and gets a confusing duplicate. The stale-check (§4.1) and conversation-coherence-check (§4.6) together prevent this. Both must be in `flush_blocked_sends`, not just one.

### RL-4: WRITE API fires when unconfigured
**Most likely failure:** `trust_hub_configured()` returns False because `TWILIO_TRUST_PRODUCT_SID` is unset, but `configured()` (messaging.py:46) is True (Twilio creds are set). The agent calls `create_sole_prop_brand` anyway because `messaging.configured()` is True, hits the real Twilio API with a blank Reseller SID, and gets a real error back (possibly billing a brand submission attempt). The guard: `create_*` functions check `trust_hub_configured()` (not just `configured()`) and return `{"status": "simulated"}` when False.

### RL-5: Micro-site branding leak
**Most likely failure:** The Jinja micro-site template includes FirstBack logo, color scheme, or any FirstBack identity. The page must look like the CONTRACTOR'S business page. FirstBack's name must appear only in the footer privacy link (linking to FirstBack's hosted `/privacy`). The page title must be the contractor's business name.

### RL-6: EIN / PII logging and storage security
**Most likely failure:** `create_standard_brand` logs the EIN in `print(..., file=sys.stderr)` on success or error (as messaging.py does for Twilio errors). EIN is PII — must be truncated in logs: log only the business_id and HTTP status, never the EIN string. The EIN is already stored in `businesses.ein` (encrypted at rest only if `FIRSTBACK_TOKEN_KEY` is set — which it may not be). The Trust Hub submission sends EIN to Twilio (required), but the local log must not contain it. Guard: the `[firstback]` log prefix pattern must NOT include the EIN value.

### RL-7: OTP sent but not primed → looks like spam
F14 spec explicitly says: "Prime it first so it doesn't look like spam: 'You'll get a text in ~1 minute — reply YES to turn your number on.'" The most likely failure is the agent sends the OTP immediately without first showing the contractor the "you're about to get a text" screen. The flow is: (1) show the priming screen + button → (2) contractor taps "Send my verification code" → (3) OTP sends. Never send the OTP on page load.

---

## 10. Acceptance Tests (test_phase3_a2p.py)

All tests: standalone `python3 test_phase3_a2p.py` (NOT pytest). Pattern: temp DB, network tripwire, stub Twilio seams.

```
Test group 1: EIN fork
  ok   sole-prop path skips EIN requirement in profile gate
  ok   llc path still requires EIN in profile gate
  ok   registration_path returns sole_prop for business_type='sole_prop'
  ok   registration_path returns llc for business_type='llc'
  ok   profile step does not complete for llc without EIN
  ok   profile step completes for sole_prop with name+address only

Test group 2: WRITE API (stubbed Twilio HTTP)
  ok   create_sole_prop_brand returns simulated when trust_hub_configured() is False
  ok   create_sole_prop_brand assembles correct Trust Hub payload structure
  ok   create_sole_prop_brand calls set_a2p_registration with status=pending on success
  ok   create_standard_brand includes reseller_sid in the payload
  ok   create_standard_brand assembles the microsite URL as opt_in_url
  ok   submit_a2p NEVER sets a2p_status=approved (only pending or submitted)
  ok   submit_a2p returns simulated when trust_hub_configured() is False
  ok   submit_a2p dispatches to sole_prop_brand for business_type=sole_prop
  ok   submit_a2p dispatches to standard_brand for business_type=llc

Test group 3: OTP
  ok   set_otp stores the code and expiry on the business
  ok   get_otp returns None after expiry (time-travel test)
  ok   clear_otp removes the stored code
  ok   OTP send route requires ALERT_FROM_NUMBER (simulates if unset)
  ok   OTP verify route rejects an expired code
  ok   OTP verify route rejects a wrong code
  ok   OTP verify route calls submit_a2p on success

Test group 4: Auto-flush safety
  ok   blocked send is persisted to blocked_sends table on send_sms blocked
  ok   flush skips sends older than FLUSH_MAX_AGE_HOURS (stale)
  ok   flush skips sends for opted-out recipients
  ok   flush skips sends where conversation has progressed (subsequent provider_sid message)
  ok   flush marks flushed=1 after successful send_sms
  ok   flush marks skip_reason=stale on stale sends
  ok   flush marks skip_reason=opted_out on opted-out sends
  ok   flush marks skip_reason=conversation_progressed on coherence fail
  ok   flush returns {"flushed": N, "skipped": N, "errors": N}
  ok   flush is triggered by a2p_sync when status flips pending->approved
  ok   flush is NOT triggered when status was already approved (no-op re-sync)
  ok   flush never sends if send_sms returns blocked (log error, stop)

Test group 5: Honest status (anti-premature-approval)
  ok   a2p_sync is the ONLY function that may set a2p_status=approved (negative: submit_a2p cannot)
  ok   approved status not set when Twilio CREATE returns 200 (submission != approval)
  ok   a2p_sync flips to approved only when Twilio status endpoint returns VERIFIED
  ok   /setup a2p step shows 'pending carrier review' not 'approved' immediately after submit

Test group 6: HC gating
  ok   trust_hub_configured() is False when TWILIO_TRUST_PRODUCT_SID is unset
  ok   all create_* functions return simulated when trust_hub_configured() is False
  ok   microsite_url returns empty string when FIRSTBACK_MICROSITE_BASE_URL is unset
  ok   generate_microsite is a no-op when FIRSTBACK_MICROSITE_BASE_URL is unset

Test group 7: PII / security
  ok   create_standard_brand does not log the EIN value to stderr
  ok   create_sole_prop_brand does not log the business address to stderr
```

---

## 11. OWNER-OPS Prerequisites (not code — must happen before path B can submit)

These are NOT blockers to building — they're blockers to a real live LLC submission:

1. **Cloudflare Email Routing catch-all** for `clients.firstback.com` must be live so `{slug}@clients.firstback.com` receives email. Set up once; Twilio may send a verification email to this address.
2. **`firstback.io` DNS / subdomain wildcard** for `*.firstback.io` must resolve to the Flask app (or a static host) so micro-sites are reachable. Without this, the micro-site URL 404s and TCR rejects it.
3. **Twilio Trust Hub Reseller SID** (`TWILIO_TRUST_PRODUCT_SID` env var on Render) — the ISV product SID from the Twilio console. Without it, the WRITE API is in simulated mode.
4. **One real LLC test submission** to confirm HC-1 (ISV-subdomain passes TCR).
5. **One Twilio CSP call** to confirm HC-2 (catch-all email satisfies Authentication+).

Mark these as DEFERRED (CODE: done, OWNER-OPS: pending) in the build agent's output.

---

## 12. Summary: What This Spec Does and Does Not Try to Prove

**Ships with confidence (all covered by tests):**
- EIN fork bifurcates correctly; sole-props never forced into LLC path
- WRITE API is a true no-op when unconfigured (simulated, like messaging.py)
- `approved` status only ever comes from `a2p_sync` polling, never from a CREATE response
- Auto-flush passes all 7 safety gates; stale/opted-out/progressed sends are skipped
- OTP is sent from the platform alert number, not the (unapproved) tenant number
- EIN/PII not in any log line

**Ships behind a gate (HC-1 + HC-2 pending live confirmation):**
- Micro-site URL actually passing TCR for a contractor brand
- Per-contractor catch-all email satisfying Authentication+ for Standard brands

**The honest ceiling on time-to-value:**
Sole-prop (majority): pay → 6 taps → hear AI answer a call today → reply YES to OTP → texts live in minutes to hours. LLC: same day-1 voice, texts in 1–3 business days. The EXTERNAL ceiling is TCR vetting (1–10 business days). No code change removes this — we say so honestly, and voice is the day-1 retention bridge.
