# Phase 3 — AUDITOR A Spec
# "Time-to-First-Value: Automated A2P + Done-For-You Onboarding + Auto-Flush"

**Date:** 2026-06-18
**Base:** staging @ 6295f05 (clean, 35/35 green)
**Auditor role:** Code audit + implementation spec only. No product code written here.
**Primary source of truth:** `~/apps/COO/firstback-blueprint/F14-ONBOARDING-SPEC.md` (supersedes all prior A2P sections).

Tests are standalone scripts: `.venv/bin/python test_X.py` (NEVER pytest). Plain asserts; exit non-zero on fail.

---

## VERIFIED EXISTING STATE (do NOT duplicate)

The following were confirmed present in the real code before speccing anything:

| Symbol | File | Location | What it does |
|---|---|---|---|
| `a2p_brand_sid`, `a2p_campaign_sid`, `a2p_messaging_service_sid`, `a2p_status`, `a2p_submitted_at`, `legal_business_name`, `ein`, `business_address`, `website` | `db.py` | :492-514 | A2P columns on `businesses` (already migrated, guarded). |
| `db.set_a2p_registration(business_id, brand_sid, campaign_sid, messaging_service_sid, status, submitted_at)` | `db.py` | :957 | Partial-write setter — writes only non-None fields. Already used by the manual SID-paste path. |
| `db.set_a2p_status(business_id, status)` | `db.py` | :948 | Status-only write (unregistered|pending|approved|failed). |
| `db.update_a2p_profile(business_id, fields)` | `db.py` | :934 | Writes legal_business_name/ein/business_address/website. |
| `connections.a2p_sync(business)` | `connections.py` | :243 | Twilio campaign_status -> our 4-state; called from /setup GET. |
| `connections.a2p_sync_all()` | `connections.py` | :263 | Syncs all businesses with a campaign SID; called from /tasks/run-due. |
| `messaging.fetch_a2p_campaign_status(service_sid, campaign_sid)` | `messaging.py` | :323 | Raw Twilio campaign_status read (US A2P endpoint). |
| `compliance.a2p_ready(biz)` | `compliance.py` | :42 | True iff a2p_status == "approved". |
| `compliance.a2p_status(biz)` | `compliance.py` | :38 | Returns the status string. |
| `connections.step_state` / `launch_blockers` | `connections.py` | :64, `compliance.py`:60 | Wizard step display + go-live gate. |
| `messaging.send_sms(...)` returns `{"status":"blocked","reason":"a2p_not_approved"}` | `messaging.py` | :132-135 | A2P gate in send path — blocked sends are recorded on the thread but NOT persisted for later flush. **This is the gap.** |
| `messaging.provision_number(business_id, phone, area_code, base_url)` | `messaging.py` | :343 | Buys + webhooks a number. |
| `messaging.configured()` | `messaging.py` | :46 | Twilio creds present gate. |
| `/signup` POST (app.py:274) | `app.py` | :274-316 | Creates business; no EIN fork yet. |
| `setup_a2p` route (app.py:1126) | `app.py` | :1126-1162 | Manual founder-paste path — Phase 3 replaces the "submit" mode; keeps the `mode=record` operator SID-override path as a fallback. |
| `/terms`, `/privacy` routes | `app.py` | :485-492 | FirstBack-level privacy/terms already exist. |
| `connections._profile_done(biz)` | `connections.py` | :39 | Currently requires `name + ein + business_address`. **Must fork for sole-prop (no EIN).** |
| `_GROWTH_EXCLUSION` in scheduled_messages index | `db.py` | :646 | Already excludes sms_retry. New kind `blocked_textback` must be added to this exclusion. |
| `run_due_once` / `tick_once` | `reminders.py` | :294, :381 | The scheduler loop. Auto-flush rides here. |

---

## GLOBAL DECISIONS

- **[DECIDED] Trust Hub WRITE API location:** New functions live in `messaging.py` (not connections.py), following the established pattern: everything that touches Twilio's REST API lives in messaging.py. connections.py calls messaging.py, never the reverse.
- **[DECIDED] Gated+simulated pattern for Trust Hub writes:** All three write functions (`create_a2p_brand`, `create_a2p_messaging_service`, `create_a2p_campaign`) must follow the exact gated+simulated pattern of `send_sms` and `provision_number`: return a status dict with `"status": "simulated"` when `not configured()`, never raise, log all errors with `[firstback]` prefix.
- **[DECIDED] Sole-prop profile gate:** `connections._profile_done` and `connections.profile_complete` must fork on EIN: Path A (no EIN) requires only `name + phone` (the signup always captures these); Path B (has EIN) requires `name + ein + business_address`. The fork is: `has_ein = bool(biz.get("ein"))`. Existing tenants with EIN already set are unaffected.
- **[DECIDED] Slug generation:** `{slug}.firstback.io` slug is a URL-safe ASCII slug derived from the business name: lowercase, strip non-alphanumeric to hyphens, collapse consecutive hyphens, strip leading/trailing hyphens, cap at 40 chars. Stored on the business as `micro_site_slug`. Collision-handled by appending the business_id (`daves-painting-47`).
- **[DECIDED] Blocked-send persistence:** A new `scheduled_messages` kind `"blocked_textback"` stores the original text-back body (body from the inbound call's AI reply) for each call where send returned `blocked`. The lead_id is known at block time. Auto-flush in `a2p_sync_all` sends all pending `blocked_textback` rows for a business when the status transitions to "approved". The SF-6 quiet-hours backstop and opt-out check STILL apply at flush time via the existing `send_sms` gate — no bypass.
- **[DECIDED] Auto-flush triggers:** `connections.a2p_sync_all()` is already called from `/tasks/run-due`. When `a2p_sync` transitions a business from non-approved to "approved", it calls a new `connections.flush_blocked_textbacks(business_id)`. This is the ONLY flush trigger — no flush on page load, no flush in the request thread.
- **[DECIDED] Per-contractor email (Path B):** The `{slug}@clients.firstback.com` address is RECORDED on the business as `a2p_contact_email` but is NOT sent/provisioned by code — Cloudflare Email Routing catch-all is OWNER-OPS. The signup/setup UI displays it to Dave as his authorized-rep email for the brand submission. No code sends email FROM that address.
- **[DECIDED] Micro-site routes:** Path B micro-sites are served from the EXISTING FirstBack app under `/c/{slug}` (contractor landing) and share `/privacy` and `/terms` (already live). A slug-specific privacy link reads `/privacy` (already SMS-opt-in-language-needed — check). The micro-site is a Jinja template, NOT a separate service or generator. No smart/curly quotes in templates.
- **[DECIDED] OTP for sole-prop (Path A):** The "reply YES" OTP is an outbound SMS to the contractor's signup phone saying: "You're almost live on FirstBack. Reply YES to start getting texts from missed callers. (Reply STOP anytime to opt out.)" — sent via `messaging.send_sms(biz, owner_phone, body, gate=False)` (owner alert path, bypasses A2P gate). The OTP reply is received on the OWNER's line from Twilio, not from a customer. This is NOT carrier opt-in for the contractor's customers — it is the contractor's own consent to receive test/system texts. **The `a2p_status` for Path A is set to "pending" on submit; it transitions to "approved" when the Twilio sole-prop campaign vets (same `a2p_sync` path).** The OTP is informational only and does NOT set a2p_status.
- **[DECIDED] Business-name lookup prefill:** A new `/api/places/lookup` endpoint queries the Google Places API (Text Search, nearbysearch) using the business name field in the signup form. Returns `{legal_name, address}` as JSON for JS to prefill. Gated on `GOOGLE_PLACES_API_KEY` being set; returns `{}` when not configured (JS silently skips). This is a CODE task against a mock; the real Places API key is OWNER-OPS.
- **[DECIDED] The manual `mode=record` path in `setup_a2p` is KEPT** as an operator escape hatch (it requires `_is_operator()`). Phase 3 adds `mode=auto` (the new path); `mode=submit` becomes an alias for `mode=auto` for existing callers.
- **[DECIDED] convos.py / llm.py are NOT touched.** Per Phase 2 rule, these are trades_core kernels.
- **[DECIDED] Privacy policy for micro-site:** The existing `/privacy` already mentions "we do not sell it" but does NOT contain SMS-specific opt-out language. The privacy template MUST add a section "Text messaging" before the "Data retention" section. This is a template edit (not a new file). The contractor micro-site links to the shared `/privacy`.
- **[DECIDED] Reseller ID on every Trust Hub submit:** `TWILIO_A2P_RESELLER_SID` env var (config.py). When set, every `create_a2p_brand` call includes `IsrId={TWILIO_A2P_RESELLER_SID}` in the POST body. When not set, the field is omitted (simulated or CSP mode). This is OWNER-OPS to obtain, but the code param must be wired now.
- **[DECIDED] Customer-Care use case:** Every `create_a2p_campaign` call uses `UseCase=CUSTOMER_CARE` (the correct Twilio constant for service-oriented, non-marketing messages). This is hardcoded, not configurable.

---

## NEW DB COLUMNS / MIGRATIONS

All in `db.init_db()`, guarded `if col not in cols` pattern. **ALL owned by A1.**

```python
# Phase 3 — A2P path fork + micro-site + blocked textback persistence
biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
for col, ddl in (
    # "A" = sole-prop (no EIN), "B" = LLC/standard (has EIN). Set at signup,
    # can change if contractor later adds EIN. NULL = not yet determined (old rows).
    ("a2p_registration_path", "TEXT"),
    # Path B only: the URL slug for daves-painting.firstback.io
    ("micro_site_slug",       "TEXT"),
    # Path B only: the provisioned contact email shown to Dave as authorized-rep.
    # NOT actually sent FROM by code; Cloudflare catch-all is OWNER-OPS.
    ("a2p_contact_email",     "TEXT"),
):
    if col not in biz_cols:
        c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")

# Phase 3 — blocked_textback is a NEW kind in scheduled_messages.
# It must be excluded from the growth-touch unique index (same as sms_retry).
# The existing _GROWTH_EXCLUSION already lists sms_retry and morning_reminder;
# blocked_textback stacks per-lead (one per blocked send), never deduped.
# No schema change needed for scheduled_messages itself — kind is TEXT, body is TEXT,
# lead_id + business_id + send_at + status work as-is. BUT the growth-touch unique
# index must be rebuilt to add blocked_textback to the exclusion list.
# Pattern: DROP + recreate (same approach as the sms_retry addition in Phase 2).
_PHASE3_GROWTH_EXCLUSION = "('reminder','followup','sms_retry','morning_reminder','blocked_textback')"
sched_idx = [r[1] for r in c.execute("PRAGMA index_list(scheduled_messages)").fetchall()]
_idx_sql = c.execute(
    "SELECT sql FROM sqlite_master WHERE type='index' AND name='uniq_growth_touch_per_lead'"
).fetchone()
if _idx_sql and "blocked_textback" not in (_idx_sql[0] or ""):
    c.execute("DROP INDEX IF EXISTS uniq_growth_touch_per_lead")
    c.execute(
        f"DELETE FROM scheduled_messages WHERE kind NOT IN {_PHASE3_GROWTH_EXCLUSION} "
        "AND status!='canceled' AND id NOT IN ("
        f"  SELECT MIN(id) FROM scheduled_messages "
        f"  WHERE kind NOT IN {_PHASE3_GROWTH_EXCLUSION} AND status!='canceled' "
        "  GROUP BY lead_id, kind)")
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_growth_touch_per_lead "
        f"ON scheduled_messages(lead_id, kind) "
        f"WHERE kind NOT IN {_PHASE3_GROWTH_EXCLUSION} AND status!='canceled'")
```

**CRITICAL:** The index rebuild must NOT run on a DB that already has `blocked_textback` in the index SQL (the check above handles this). Test `test_migration.py` must re-run after A1 merges.

---

## SHARED SEAMS (canonical signatures — ALL agents must match exactly)

| Seam | Owner (defines) | Callers |
|---|---|---|
| `config.TWILIO_A2P_RESELLER_SID: str` | **A1** (config.py) | A1 (messaging.py) |
| `config.GOOGLE_PLACES_API_KEY: str` | **A1** (config.py) | A2 (app.py /api/places/lookup) |
| `config.MICRO_SITE_DOMAIN: str` (default `"firstback.io"`) | **A1** (config.py) | A1 (connections.py slug builder), A2 (app.py /c/<slug>) |
| `config.CLIENTS_EMAIL_DOMAIN: str` (default `"clients.firstback.com"`) | **A1** (config.py) | A1 (connections.py email builder) |
| `messaging.create_a2p_brand(business) -> dict` | **A1** (messaging.py) | A2 (connections.py `submit_a2p`) |
| `messaging.create_a2p_messaging_service(business) -> dict` | **A1** (messaging.py) | A2 (connections.py `submit_a2p`) |
| `messaging.create_a2p_campaign(business, messaging_service_sid, brand_sid) -> dict` | **A1** (messaging.py) | A2 (connections.py `submit_a2p`) |
| `connections.submit_a2p(business_id) -> dict` | **A2** (connections.py) | A2 (app.py setup_a2p `mode=auto`) |
| `connections.flush_blocked_textbacks(business_id) -> int` | **A2** (connections.py) | A2 (connections.a2p_sync_all on transition) |
| `connections.build_slug(name, business_id) -> str` | **A2** (connections.py) | A2 (connections.submit_a2p), A3 (/c/<slug> route) |
| `connections.build_contact_email(slug) -> str` | **A2** (connections.py) | A2 (connections.submit_a2p), A3 (setup display) |
| `db.queue_blocked_textback(business_id, lead_id, body) -> int|None` | **A1** (db.py) | A3 (app.py handle_inbound / open_conversation on "blocked") |
| `db.pending_blocked_textbacks(business_id) -> list[dict]` | **A1** (db.py) | A2 (connections.flush_blocked_textbacks) |
| `db.set_a2p_registration_path(business_id, path) -> None` | **A1** (db.py) | A2 (connections.submit_a2p), A3 (/signup POST) |
| `db.set_micro_site(business_id, slug, contact_email) -> None` | **A1** (db.py) | A2 (connections.submit_a2p) |

**Existing functions confirmed present (do not redefine):**
`db.set_a2p_registration` (:957), `db.set_a2p_status` (:948), `connections.a2p_sync` (:243),
`connections.a2p_sync_all` (:263), `messaging.send_sms` (:69), `messaging.provision_number` (:343),
`messaging.configured` (:46), `compliance.a2p_ready` (:42), `connections._profile_done` (:39 — MODIFIED not replaced).

---

## TASK CLASSIFICATION (CODE vs OWNER-OPS vs DEFERRED)

### CODE (buildable + testable now against mocks)

- Trust Hub WRITE API client (`create_a2p_brand`, `create_a2p_messaging_service`, `create_a2p_campaign`) with gated+simulated pattern — mockable with `responses` or `unittest.mock.patch("requests.post")`.
- `connections.submit_a2p(business_id)` orchestrator (calls the three write functions in order, stores SIDs via `db.set_a2p_registration`, handles errors).
- `db.queue_blocked_textback` / `db.pending_blocked_textbacks` (new db.py functions).
- `connections.flush_blocked_textbacks(business_id)` — retrieves pending blocked_textbacks, calls `messaging.send_sms` for each (which re-applies all gates including quiet-hours + opt-out + dedupe), marks rows sent/failed.
- `connections.a2p_sync_all` modification: detect status transition to "approved" and call `flush_blocked_textbacks`.
- Signup EIN fork: `has_ein` field on `/signup` POST, `db.set_a2p_registration_path` on business creation.
- `connections._profile_done` / `profile_complete` fork on EIN.
- `connections.build_slug(name, business_id)` / `connections.build_contact_email(slug)`.
- `connections.submit_a2p` for Path B also calls `db.set_micro_site(business_id, slug, contact_email)`.
- Micro-site route `/c/<slug>` (serves a Jinja template; data from the business row).
- Privacy template update (SMS opt-out section added — no smart quotes).
- `/api/places/lookup` prefill endpoint (mocked against GOOGLE_PLACES_API_KEY gate).
- `app.py` handle_inbound / open_conversation: when `send_sms` returns `"blocked"`, call `db.queue_blocked_textback(biz["id"], lead_id, body)`.
- `app.py` setup_a2p route: add `mode=auto` path calling `connections.submit_a2p`.
- Config vars: `TWILIO_A2P_RESELLER_SID`, `GOOGLE_PLACES_API_KEY`, `MICRO_SITE_DOMAIN`, `CLIENTS_EMAIL_DOMAIN`.
- Wizard branch copy: setup.html EIN-fork display (Path A vs Path B messaging — plain English, zero Twilio/A2P/10DLC/brand/campaign jargon).
- All DB migrations above.

### OWNER-OPS (cannot be done by code agents)

- **`firstback.io` domain registration + wildcard DNS `*.firstback.io` -> Render app.** Without this, micro-site routes 404. Code is buildable (serves `/c/<slug>`) but not verifiable on the live host until DNS is live.
- **Cloudflare Email Routing catch-all for `@clients.firstback.com`.** The `a2p_contact_email` is generated and stored by code; actually receiving email at `{slug}@clients.firstback.com` requires Cloudflare catch-all configured. Code just displays the address.
- **Real Twilio reseller/ISV SID (`TWILIO_A2P_RESELLER_SID`).** Obtained from Twilio account team. Until set, the code sends without the IsrId field (still valid for a direct-submit CSP; ISV reseller just improves throughput). **The three write API functions are mockable without this.**
- **Twilio Trust Hub API credentials.** The write API uses the same `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` as the rest of the Twilio layer — no new creds. But the Brand/Campaign creation endpoints are REAL Twilio costs ($4/brand, $10/campaign) and submit to TCR for real. **No test should call real Twilio.**
- **Google Places API key (`GOOGLE_PLACES_API_KEY`).** For the business-name prefill. Code is written and gated; key is OWNER-OPS.
- **Heritage House dogfood submission** — the first real sole-prop OTP test (or LLC test) to validate the actual TCR submission flow.

### DEFERRED (verifiable only after live confirmations)

- **Does `[slug].firstback.io` actually pass TCR for a contractor brand?** Per F14-ONBOARDING-SPEC: "Two confirmations before scaling (NOT blockers to building)." The code is built; the TCR acceptance is a real-world confirmation that cannot be unit-tested. Confidence: Likely (ISV subdomains pass, carriers check content not WHOIS), not Confirmed.
- **Does `{slug}@clients.firstback.com` satisfy Twilio's Authentication+ email rule for Standard brands?** Requires a real Twilio CSP call per F14-ONBOARDING-SPEC. Code provisions the address; acceptance is deferred.
- **Sole-prop OTP path approval timing** ("minutes to hours" claim). Code submits; actual carrier vetting time is external and cannot be tested.

---

## 3-WAY PARTITION (file-disjoint slices)

**Collision files: db.py, config.py, messaging.py, connections.py, app.py.** Each collision file is owned by exactly ONE agent.

---

### AGENT 1 — Foundation (db + config + messaging)

**Owns exclusively:** `db.py`, `config.py`, `messaging.py`, `templates/privacy.html`.

**Work:**

#### config.py
Add after the existing `ALERT_FROM_NUMBER` block (~line 158):
```python
# Phase 3 — A2P Trust Hub write API
# ISV/Reseller SID — obtained from Twilio; omit field when empty (direct CSP mode).
TWILIO_A2P_RESELLER_SID = os.environ.get("TWILIO_A2P_RESELLER_SID", "")
# Google Places API key for business-name prefill at signup.
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
# Subdomain domain for contractor micro-sites (e.g. "firstback.io" -> slug.firstback.io).
MICRO_SITE_DOMAIN = os.environ.get("FIRSTBACK_MICRO_SITE_DOMAIN", "firstback.io")
# Email domain for contractor authorized-rep addresses.
CLIENTS_EMAIL_DOMAIN = os.environ.get("FIRSTBACK_CLIENTS_DOMAIN", "clients.firstback.com")
```

#### db.py — Migrations (add after existing Phase 2 migrations, ~line 685)
The exact migration block from the DB MIGRATIONS section above. Three new columns on businesses + growth index rebuild.

#### db.py — New functions (add near existing A2P setters ~line 975)
```python
def set_a2p_registration_path(business_id, path):
    """Record the registration path: 'A' (sole-prop, no EIN) or 'B' (LLC/standard).
    Called at signup (when has_ein is known) and at submit_a2p."""
    conn = get_conn()
    conn.execute("UPDATE businesses SET a2p_registration_path=? WHERE id=?",
                 (path, business_id))
    conn.commit()
    conn.close()


def set_micro_site(business_id, slug, contact_email):
    """Record the Path B micro-site slug and provisioned contact email.
    Called by connections.submit_a2p after slug generation."""
    conn = get_conn()
    conn.execute("UPDATE businesses SET micro_site_slug=?, a2p_contact_email=? WHERE id=?",
                 (slug, contact_email, business_id))
    conn.commit()
    conn.close()


def queue_blocked_textback(business_id, lead_id, body):
    """Persist a blocked text-back for auto-flush when A2P is approved.
    Returns the new scheduled_messages row id, or None on error.
    Kind = 'blocked_textback'; send_at = now (flush as soon as approved + quiet hours allow)."""
    from db import now_iso
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO scheduled_messages "
            "(business_id, lead_id, kind, send_at, body, status, created_at) "
            "VALUES (?,?,'blocked_textback',?,?,'pending',?)",
            (business_id, lead_id, now_iso(), body, now_iso()))
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error as e:
        conn.rollback()
        print(f"[firstback] queue_blocked_textback failed (biz {business_id}): {e}",
              file=sys.stderr, flush=True)
        return None
    finally:
        conn.close()


def pending_blocked_textbacks(business_id):
    """All pending blocked_textback rows for a business, with lead phone.
    Used by connections.flush_blocked_textbacks on A2P approval."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT s.*, l.phone AS lead_phone "
        "FROM scheduled_messages s JOIN leads l ON l.id = s.lead_id "
        "WHERE s.business_id=? AND s.kind='blocked_textback' AND s.status='pending' "
        "ORDER BY s.send_at",
        (business_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

#### messaging.py — Trust Hub WRITE API client (add after `fetch_a2p_campaign_status` ~line 342)

**Gated+simulated pattern: mirrors send_sms/provision_number exactly. Never raises.**

```python
# ---- A2P 10DLC Trust Hub WRITE API (Phase 3 / SF-8) ----
# All three functions follow the gated+simulated pattern: return a status dict,
# never raise, log all errors with [firstback] prefix. Real Twilio calls cost
# real money ($4/brand, $10/campaign) and submit to TCR — NEVER call in tests.
# Mock requests.post in all tests that exercise this path.
#
# Trust Hub base: https://trusthub.twilio.com/v1
# Messaging Service base: https://messaging.twilio.com/v1
# A2P Campaign base: https://messaging.twilio.com/v1/Services/{sid}/Compliance/Usa2p
_TRUST_HUB_BASE = "https://trusthub.twilio.com/v1"
_MESSAGING_BASE = "https://messaging.twilio.com/v1"


def create_a2p_brand(business):
    """Create a Customer Profile (brand) in Twilio Trust Hub for the given business.
    Path B (LLC) only — Path A (sole-prop) uses Twilio's Starter (OTP) brand creation
    which is a separate endpoint; this function covers the Standard brand flow.

    Returns a status dict:
      "simulated"  -- Twilio not configured; no API call made
      "created"    -- brand SID in result["brand_sid"]
      "error"      -- API failed; result["error"] has detail

    The Reseller SID (TWILIO_A2P_RESELLER_SID from config) is included when set.
    Use case is hardcoded to CUSTOMER_CARE per F14 spec.
    """
    from config import TWILIO_A2P_RESELLER_SID, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
    biz = (business if isinstance(business, dict) else {})
    biz_id = biz.get("id")
    if not configured():
        print(f"[firstback] create_a2p_brand simulated (biz {biz_id}): Twilio not configured",
              file=sys.stderr, flush=True)
        return {"status": "simulated"}
    import requests
    data = {
        "FriendlyName": (biz.get("legal_business_name") or biz.get("name") or ""),
        "BusinessName": (biz.get("legal_business_name") or biz.get("name") or ""),
        "BusinessRegistrationIdentifier": "EIN",
        "BusinessRegistrationNumber": (biz.get("ein") or ""),
        "BusinessType": "Corporation",
        "BusinessIndustry": "CONSTRUCTION",
        "BusinessRegionsOfOperation": "USA",
        "WebsiteUrl": _brand_website(biz),
        "BusinessIdentity": "direct_customer",
        "Email": (biz.get("a2p_contact_email") or biz.get("alert_email") or ""),
    }
    if TWILIO_A2P_RESELLER_SID:
        data["IsrId"] = TWILIO_A2P_RESELLER_SID
    try:
        r = requests.post(
            f"{_TRUST_HUB_BASE}/CustomerProfiles",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), data=data, timeout=30)
        r.raise_for_status()
        sid = r.json().get("sid")
        return {"status": "created", "brand_sid": sid}
    except Exception as e:
        print(f"[firstback] create_a2p_brand failed (biz {biz_id}): {e}",
              file=sys.stderr, flush=True)
        return {"status": "error", "error": str(e)}


def _brand_website(biz):
    """The URL submitted as the brand opt-in page. Path B uses the contractor
    micro-site slug; falls back to any stored website; last resort is the firstback.io
    root (acceptable for sole-prop / early submissions)."""
    from config import MICRO_SITE_DOMAIN
    slug = (biz or {}).get("micro_site_slug")
    if slug:
        return f"https://{slug}.{MICRO_SITE_DOMAIN}"
    website = (biz or {}).get("website") or ""
    if website:
        return website
    return f"https://www.{MICRO_SITE_DOMAIN}"


def create_a2p_messaging_service(business):
    """Create a Twilio Messaging Service to anchor the campaign. Returns:
      "simulated" / "created" (result["messaging_service_sid"]) / "error"."""
    from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
    biz = (business if isinstance(business, dict) else {})
    biz_id = biz.get("id")
    if not configured():
        return {"status": "simulated"}
    import requests
    name = (biz.get("legal_business_name") or biz.get("name") or f"biz-{biz_id}")
    data = {"FriendlyName": f"FirstBack-{name}",
            "UseInboundWebhookOnNumber": "false"}
    try:
        r = requests.post(
            f"{_MESSAGING_BASE}/Services",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), data=data, timeout=30)
        r.raise_for_status()
        sid = r.json().get("sid")
        return {"status": "created", "messaging_service_sid": sid}
    except Exception as e:
        print(f"[firstback] create_a2p_messaging_service failed (biz {biz_id}): {e}",
              file=sys.stderr, flush=True)
        return {"status": "error", "error": str(e)}


def create_a2p_campaign(business, messaging_service_sid, brand_sid):
    """Register a US A2P campaign under the given messaging service + brand.
    Use case: CUSTOMER_CARE (hardcoded per F14 spec). Returns:
      "simulated" / "created" (result["campaign_sid"]) / "error"."""
    from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
    biz = (business if isinstance(business, dict) else {})
    biz_id = biz.get("id")
    if not configured():
        return {"status": "simulated"}
    if not messaging_service_sid:
        return {"status": "error", "error": "messaging_service_sid required"}
    import requests
    data = {
        "BrandRegistrationSid": brand_sid or "",
        "Description": (
            f"Customer service texts for {biz.get('name', 'contractor')} — "
            "missed-call text-backs and appointment reminders."
        ),
        "MessageFlow": (
            "Customers call the contractor's FirstBack number; when the call goes unanswered, "
            "they receive an automatic text-back. They may reply to schedule an estimate. "
            "Opt-out by replying STOP at any time."
        ),
        "MessagingServiceSid": messaging_service_sid,
        "HasEmbeddedLinks": "false",
        "HasEmbeddedPhone": "false",
        "UseCase": "CUSTOMER_CARE",
        "OptInType": "VERBAL",
    }
    try:
        r = requests.post(
            f"{_MESSAGING_BASE}/Services/{messaging_service_sid}/Compliance/Usa2p",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), data=data, timeout=30)
        r.raise_for_status()
        sid = r.json().get("sid")
        return {"status": "created", "campaign_sid": sid}
    except Exception as e:
        print(f"[firstback] create_a2p_campaign failed (biz {biz_id}): {e}",
              file=sys.stderr, flush=True)
        return {"status": "error", "error": str(e)}
```

#### templates/privacy.html — SMS opt-out section
Add a new `<h2>Text messaging</h2>` section BEFORE the `<h2>Data retention</h2>` section (currently at line 41). Content:
```html
<h2>Text messaging</h2>
<p>When you contact a business using FirstBack, you may receive automated text messages from that business's FirstBack number. Message frequency varies. Message and data rates may apply. To stop receiving texts from a specific business, reply STOP to any message from that number. We do not share mobile opt-in data or phone numbers with third parties for marketing purposes.</p>
```
**No smart/curly quotes. No em-dashes typed as characters — use plain hyphens in template HTML.**

**A1 writes tests:** `test_sf8_write_api.py` + `test_sf8_migration.py`.
**A1 re-runs:** `test_migration.py`, `test_config_hub.py`, `test_connect_hub.py`, `test_compliance.py`, `test_compliance_core.py`.

---

### AGENT 2 — Connections + App.py signup/setup/micro-site/flush

**Owns exclusively:** `connections.py` AND `app.py` edits ONLY at:
- `/signup` POST route (app.py ~line 274-316) — add EIN fork
- `/setup/a2p` route (app.py ~line 1126) — add `mode=auto` path
- NEW `/api/places/lookup` route
- NEW `/c/<slug>` micro-site route

**Depends on A1:** All seam functions from A1 must exist before A2 merges.

**Work:**

#### connections.py — EIN-fork on profile gate (~line 39)
Change `_profile_done` to fork on EIN:
```python
def _profile_done(biz):
    """Path A (sole-prop, no EIN): name + phone sufficient.
    Path B (LLC, has EIN): name + ein + business_address required."""
    if not biz.get("name"):
        return False
    if biz.get("ein"):
        # Path B: must have legal address too
        return bool(biz.get("business_address"))
    # Path A: phone must be present (captured at signup via alert_sms)
    return bool(biz.get("alert_sms") or biz.get("phone"))
```

#### connections.py — Slug + email builders (add near line 220)
```python
import re as _re

def build_slug(name, business_id):
    """URL-safe slug from business name: lowercase, alphanum+hyphens, max 40 chars.
    Falls back to biz-{id} if name produces an empty slug."""
    raw = (name or "").lower()
    raw = _re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:40]
    if not raw:
        raw = f"biz-{business_id}"
    # Append business_id to avoid collisions (idempotent: same id always same suffix).
    return f"{raw}-{business_id}"


def build_contact_email(slug):
    """The authorized-rep email for a contractor's Path B brand submission.
    OWNER-OPS note: requires Cloudflare catch-all on clients.firstback.com."""
    from config import CLIENTS_EMAIL_DOMAIN
    return f"{slug}@{CLIENTS_EMAIL_DOMAIN}"
```

#### connections.py — submit_a2p orchestrator (add after `a2p_sync_all` ~line 272)
```python
def submit_a2p(business_id):
    """Automated A2P registration via Trust Hub WRITE API (SF-8 / Phase 3).
    Replaces the manual founder-paste concierge path for the 'auto' mode.

    Path A (sole-prop): creates a Starter brand (OTP opt-in); no micro-site needed.
    Path B (LLC): generates the micro-site slug + contact email, then creates
                  brand + messaging service + campaign in order.

    Returns a dict with 'status': one of:
      'simulated'  -- Twilio not configured; no API calls made
      'submitted'  -- all three created; brand_sid/campaign_sid/messaging_service_sid present
      'error'      -- a step failed; 'step' names which, 'error' has detail
    Never raises.
    """
    biz = db.get_business(business_id)
    if not biz:
        return {"status": "error", "error": "business not found"}

    # Determine path from EIN presence.
    has_ein = bool(biz.get("ein"))
    path = "B" if has_ein else "A"
    db.set_a2p_registration_path(business_id, path)

    if not messaging.configured():
        db.set_a2p_registration(business_id, status="pending",
                                submitted_at=__import__("datetime").datetime.utcnow()
                                .isoformat(timespec="seconds"))
        return {"status": "simulated"}

    if path == "B":
        # Generate and store the micro-site slug + contact email BEFORE brand creation
        # so _brand_website() can read the slug from the business row.
        slug = build_slug(biz.get("legal_business_name") or biz.get("name"), business_id)
        contact_email = build_contact_email(slug)
        db.set_micro_site(business_id, slug, contact_email)
        # Re-fetch so messaging.create_a2p_brand sees the slug.
        biz = db.get_business(business_id)

        # 1. Create brand (Customer Profile).
        brand_result = messaging.create_a2p_brand(biz)
        if brand_result.get("status") == "error":
            return {"status": "error", "step": "brand", "error": brand_result.get("error")}
        brand_sid = brand_result.get("brand_sid")

        # 2. Create messaging service.
        svc_result = messaging.create_a2p_messaging_service(biz)
        if svc_result.get("status") == "error":
            return {"status": "error", "step": "messaging_service",
                    "error": svc_result.get("error")}
        messaging_service_sid = svc_result.get("messaging_service_sid")

        # 3. Create campaign.
        campaign_result = messaging.create_a2p_campaign(biz, messaging_service_sid, brand_sid)
        if campaign_result.get("status") == "error":
            return {"status": "error", "step": "campaign",
                    "error": campaign_result.get("error")}
        campaign_sid = campaign_result.get("campaign_sid")

        submitted_at = __import__("datetime").datetime.utcnow().isoformat(timespec="seconds")
        db.set_a2p_registration(business_id,
                                brand_sid=brand_sid,
                                campaign_sid=campaign_sid,
                                messaging_service_sid=messaging_service_sid,
                                status="pending",
                                submitted_at=submitted_at)
        return {"status": "submitted", "path": "B",
                "brand_sid": brand_sid, "campaign_sid": campaign_sid,
                "messaging_service_sid": messaging_service_sid}

    else:
        # Path A: sole-prop Starter brand (OTP flow).
        # Twilio's Starter Brand is created via a separate "Starter" endpoint.
        # For Phase 3 this means: submit a simpler brand with BusinessType=Sole_Proprietorship,
        # no EIN field, and the OTP opt-in type. The messaging service + campaign are still created.
        # NOTE: The Starter Brand API endpoint and exact field names MUST be confirmed with
        # one real submission (DEFERRED confirmation per F14). Code is mockable; do NOT test live.
        brand_result = messaging.create_a2p_brand({
            **biz,
            "a2p_registration_path": "A",
            # Override to sole-prop fields for the Starter brand.
            "ein": None,
            "website": None,
        })
        if brand_result.get("status") == "error":
            return {"status": "error", "step": "brand", "error": brand_result.get("error")}
        brand_sid = brand_result.get("brand_sid")

        svc_result = messaging.create_a2p_messaging_service(biz)
        if svc_result.get("status") == "error":
            return {"status": "error", "step": "messaging_service",
                    "error": svc_result.get("error")}
        messaging_service_sid = svc_result.get("messaging_service_sid")

        campaign_result = messaging.create_a2p_campaign(biz, messaging_service_sid, brand_sid)
        if campaign_result.get("status") == "error":
            return {"status": "error", "step": "campaign",
                    "error": campaign_result.get("error")}
        campaign_sid = campaign_result.get("campaign_sid")

        submitted_at = __import__("datetime").datetime.utcnow().isoformat(timespec="seconds")
        db.set_a2p_registration(business_id,
                                brand_sid=brand_sid,
                                campaign_sid=campaign_sid,
                                messaging_service_sid=messaging_service_sid,
                                status="pending",
                                submitted_at=submitted_at)
        return {"status": "submitted", "path": "A",
                "brand_sid": brand_sid, "campaign_sid": campaign_sid,
                "messaging_service_sid": messaging_service_sid}
```

**IMPORTANT NOTE on Path A (Starter Brand):** Twilio's Starter/Sole-Proprietor brand API field names are different from the Standard brand fields. The exact payload for the Starter brand registration must be validated against the real Twilio docs before the live submission. The mock test covers the orchestration; the real field names are DEFERRED to the Heritage dogfood test per F14.

#### connections.py — flush_blocked_textbacks (add after `submit_a2p`)
```python
def flush_blocked_textbacks(business_id):
    """Send any pending blocked_textback rows for a business that just got A2P approved.
    Called by a2p_sync_all when a status transitions to 'approved'.

    Each flush still goes through messaging.send_sms (which re-applies opt-out, quiet-hours,
    and the A2P gate -- now approved). SF-6 quiet-hours backstop applies: transactional=True
    so the flush is quiet-hours EXEMPT (missed-call text-back is solicited/transactional).
    Opt-out and duplicate suppression apply as always.

    Returns the count successfully sent."""
    sent = 0
    biz = db.get_business(business_id) if not isinstance(business_id, dict) else business_id
    if not biz:
        return 0
    rows = db.pending_blocked_textbacks(biz["id"] if isinstance(biz, dict) else business_id)
    for row in rows:
        if not db.claim_scheduled_message(row["id"]):
            continue  # another process got it
        phone = (row.get("lead_phone") or "").strip()
        if not phone:
            db.mark_scheduled(row["id"], "skipped")
            continue
        try:
            res = messaging.send_sms(biz, phone, row["body"],
                                     lead_id=row["lead_id"],
                                     transactional=True)  # exempt from quiet hours
            status = res.get("status")
            if status in ("sent", "simulated"):
                sent += 1
            else:
                # Still blocked (shouldn't happen -- we just approved), suppressed, or error.
                db.mark_scheduled(row["id"], "failed")
                print(f"[firstback] flush_blocked_textback unexpected status "
                      f"{status!r} (biz {biz['id']}, row {row['id']})",
                      file=sys.stderr, flush=True)
        except Exception as e:
            db.mark_scheduled(row["id"], "failed")
            print(f"[firstback] flush_blocked_textback failed (biz {biz['id']}, "
                  f"row {row['id']}): {e}", file=sys.stderr, flush=True)
    return sent
```

#### connections.py — a2p_sync_all modification (~line 263)
Add flush call on approved transition:
```python
def a2p_sync_all():
    """Sync every business that has a campaign registered. Returns how many statuses changed."""
    changed = 0
    for biz in db.list_businesses():
        if biz.get("a2p_campaign_sid"):
            before = compliance.a2p_status(biz)
            after = a2p_sync(biz)
            if after != before:
                changed += 1
                # Auto-flush blocked text-backs when a business just got approved.
                if after == "approved":
                    try:
                        flushed = flush_blocked_textbacks(biz["id"])
                        if flushed:
                            print(f"[firstback] auto-flushed {flushed} blocked text-back(s) "
                                  f"for biz {biz['id']}", file=sys.stderr, flush=True)
                    except Exception as e:
                        print(f"[firstback] flush_blocked_textbacks failed (biz {biz['id']}): {e}",
                              file=sys.stderr, flush=True)
    return changed
```

#### app.py — /signup POST (~line 296-315): EIN fork
Add `has_ein` field read and call `db.set_a2p_registration_path` after `db.create_business`:
```python
# After bid = db.create_business({...}):
has_ein = (request.form.get("has_ein") or "").strip().lower() in ("1", "yes", "true", "on")
db.set_a2p_registration_path(bid, "B" if has_ein else "A")
```
Also collect `phone` from the signup form (already done at :305) and store as `alert_sms` (already done at :308). No other signup changes.

#### app.py — /setup/a2p route (~line 1126): add mode=auto
Inside `setup_a2p()`, before the existing `if mode == "record":` block:
```python
if mode in ("auto", "submit"):
    if not connections.profile_complete(biz):
        return redirect("/setup?err=profile")
    result = connections.submit_a2p(biz["id"])
    if result.get("status") == "error":
        print(f"[firstback] submit_a2p failed (biz {biz['id']}): {result}",
              file=sys.stderr, flush=True)
        return redirect("/setup?err=a2p_submit")
    return redirect("/setup?saved=a2p")
```
Change the default `mode = request.form.get("mode") or "auto"` (was `"submit"`). Keep `mode=record` unchanged.

#### app.py — /api/places/lookup (add before /setup routes, ~line 1024)
```python
@app.route("/api/places/lookup")
@login_required
def places_lookup():
    """Business-name prefill via Google Places Text Search. Returns
    {legal_name, address} or {} when not configured or on any error.
    Dave types the business name; JS calls this and prefills the legal form fields."""
    from config import GOOGLE_PLACES_API_KEY
    if not GOOGLE_PLACES_API_KEY:
        return jsonify({})
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({})
    import requests as _req
    try:
        r = _req.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": query, "key": GOOGLE_PLACES_API_KEY}, timeout=5)
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return jsonify({})
        top = results[0]
        return jsonify({
            "legal_name": top.get("name", ""),
            "address": top.get("formatted_address", ""),
        })
    except Exception as e:
        print(f"[firstback] places_lookup failed: {e}", file=sys.stderr, flush=True)
        return jsonify({})
```

#### app.py — /c/<slug> micro-site route (add near /terms and /privacy routes, ~line 492)
```python
@app.route("/c/<slug>")
def contractor_microsite(slug):
    """Per-contractor micro-site for Path B A2P opt-in URL.
    Serves the contractor's legal business name, address, services, and SMS opt-in notice.
    No auth required (this is what TCR inspects). Never renders internal data."""
    from config import MICRO_SITE_DOMAIN
    conn = __import__("db").get_conn()
    row = conn.execute(
        "SELECT name, legal_business_name, business_address, trade, service_area "
        "FROM businesses WHERE micro_site_slug=? LIMIT 1", (slug,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    biz = dict(row)
    return render_template("microsite.html", biz=biz, slug=slug,
                           domain=MICRO_SITE_DOMAIN)
```

**A2 also creates:** `templates/microsite.html` — a minimal plain-English page (NO smart/curly quotes):
- Legal business name + address (from DB).
- Services and service area (from DB).
- An unchecked SMS opt-in checkbox with language: "By providing your phone number and texting back, you consent to receive automated text messages from this business regarding your inquiry, appointment scheduling, and reminders. Reply STOP at any time to opt out. Message and data rates may apply."
- Links to `/privacy` and `/terms`.
- No FirstBack branding visible to TCR inspection (contractor's info only). Use minimal plain HTML, no JS.
- Extends `marketing_base.html` for consistency but overrides the header block.

**A2 writes tests:** `test_sf8_connections.py` (submit_a2p orchestration mocked, flush_blocked_textbacks, build_slug, build_contact_email, _profile_done fork, a2p_sync_all flush trigger), `test_sf8_microsite.py` (microsite route 200, unknown slug 404, privacy section present).
**A2 re-runs:** `test_setup.py`, `test_connect_hub.py`, `test_webhooks.py`, `test_migration.py`.

---

### AGENT 3 — App.py blocked-send persistence (handle_inbound / open_conversation)

**Owns exclusively:** `app.py` edits ONLY at:
- `open_conversation` (~line 1291)
- `handle_inbound` (~line 1306-1357) — the blocked-send persistence gap

**Depends on A1:** `db.queue_blocked_textback` must exist.

**Work:**

#### app.py — handle_inbound (~line 1306): persist blocked text-backs

In `handle_inbound`, after the AI reply is generated and `messaging.send_sms(...)` is called, if the result status is `"blocked"`, call `db.queue_blocked_textback`:

Locate the `send_sms` call in `handle_inbound` (the main customer-facing reply path). The call returns a dict. Add:
```python
sms_result = messaging.send_sms(biz, caller, reply, lead_id=lead_id)
if sms_result.get("status") == "blocked":
    # A2P not yet approved -- persist the reply so it auto-flushes on approval.
    db.queue_blocked_textback(biz["id"], lead_id, reply)
```

**CRITICAL:** The `blocked` branch in `send_sms` already calls `db.add_message(lead_id, "out", body)` (messaging.py:133-134) so the text shows on the thread. Do NOT call `add_message` again here — that would double-record it. Only add the `queue_blocked_textback` call.

#### app.py — open_conversation (~line 1291): same persistence

`open_conversation` is the first-turn handler that fires when a new lead is created. It also calls `send_sms`. Apply the same blocked-send persistence pattern there.

Find the `send_sms` call in `open_conversation` and add the same `queue_blocked_textback` call on `"blocked"` status.

**A3 writes tests:** `test_sf8_autoflush.py` — THE most important new test and the one most likely to hide cross-agent bugs if stubbed poorly.

**This test MUST be un-stubbed across the A1/A2/A3 seam.** Specifically it MUST:
1. Create a real in-memory SQLite DB via `db.init_db()`.
2. Create a real business with `a2p_status="unregistered"`.
3. Create a real lead.
4. Call `messaging.send_sms` (with `configured()` patched True and `compliance.a2p_ready` patched False to return `"blocked"`) and verify `db.queue_blocked_textback` was called — check the DB directly, not a mock call count.
5. Manually insert a `blocked_textback` row via `db.queue_blocked_textback`.
6. Flip `a2p_status` to `"approved"` via `db.set_a2p_status`.
7. Call `connections.flush_blocked_textbacks(business_id)` with `messaging.send_sms` patched to return `{"status": "sent", "sid": "SM_test"}`.
8. Assert the `scheduled_messages` row is now `status="sent"` in the real DB.
9. Assert quiet-hours and opt-out are re-applied (test one suppressed number — `db.set_opt_out` then queue a blocked_textback — call flush and assert the row is NOT re-sent, it goes to "suppressed").

**Why this matters:** A Phase-2 lesson was that mocking across seams hid real integration bugs. The flush path touches db.py (A1), connections.py (A2), and the send_sms gating (messaging.py / A1). Testing with real DB + patched Twilio catches column-name mismatches, missing imports, and wrong gate ordering that a fully-mocked test would pass.

**A3 re-runs:** `test_scheduling.py`, `test_webhooks.py`, `test_callback.py`, `test_compliance.py`.

---

## TEST PLAN

### New test files (all standalone `.venv/bin/python test_X.py`, no pytest)

| File | Owner | Key assertions |
|---|---|---|
| `test_sf8_write_api.py` | A1 | `create_a2p_brand` simulated when not configured; returns brand_sid on mocked 200; returns error dict on mocked 4xx; Reseller SID is in POST body when set; CUSTOMER_CARE in campaign body; no smart quotes in any message flow; no description field leaks private EIN outside of the API call. |
| `test_sf8_migration.py` | A1 | New columns `a2p_registration_path`, `micro_site_slug`, `a2p_contact_email` exist after `init_db()`; `blocked_textback` excluded from growth index; growth index rebuild is idempotent (running `init_db()` twice doesn't error). |
| `test_sf8_connections.py` | A2 | `build_slug` produces valid URL-safe slug; collision suffix appended; `build_contact_email` returns correct format; `_profile_done` returns True for Path A with name+phone only; `_profile_done` returns True for Path B with name+ein+address; `_profile_done` returns False for Path B missing address; `submit_a2p` returns "simulated" when not configured; `submit_a2p` calls brand+svc+campaign in order on mocked Twilio 200s; `submit_a2p` returns error dict + correct step on first-step failure without calling subsequent steps; `a2p_sync_all` calls `flush_blocked_textbacks` only on approved transition, not on pending->pending. |
| `test_sf8_microsite.py` | A2 | GET `/c/{slug}` returns 200 for existing slug; returns 404 for unknown slug; page contains the business's legal name; page contains opt-out language ("Reply STOP"); no "Twilio" or "A2P" in rendered HTML; privacy link present. |
| `test_sf8_autoflush.py` | A3 | Full integration test (un-stubbed DB, patched Twilio) per the spec above. 9 assertions listed. Most important test in Phase 3. |

### Existing tests to re-run per agent (must remain green after each merge)

| Agent | Re-run |
|---|---|
| A1 | `test_migration.py`, `test_config_hub.py`, `test_connect_hub.py`, `test_compliance.py`, `test_compliance_core.py`, `test_callback.py` |
| A2 | `test_setup.py`, `test_connect_hub.py`, `test_webhooks.py`, `test_migration.py`, `test_scheduling.py` |
| A3 | `test_scheduling.py`, `test_webhooks.py`, `test_callback.py`, `test_compliance.py`, `test_sf7_sentinel.py` |

### Merge order
**A1 first** (migrations + config + messaging.py seams must exist). Then A2 and A3 (either order). Full suite (35 existing + 5 new = 40) green after each merge before proceeding.

---

## app.py LINE-RANGE OWNERSHIP (A2 and A3 both edit app.py — disjoint regions)

| Agent | Lines owned |
|---|---|
| A2 | ~274-316 (/signup POST, EIN fork only) · ~492 (new /c/<slug> route) · ~1024 (new /api/places/lookup) · ~1126-1162 (/setup/a2p, mode=auto addition only) |
| A3 | ~1291 (open_conversation, blocked-send persistence only) · ~1306-1357 (handle_inbound, blocked-send persistence only) |

No line-range overlaps. The `/setup/a2p` route is A2's; the `/handle_inbound`-family is A3's. Neither agent touches the other's regions.

---

## GOTCHAS (honor exactly)

1. **convos.py / llm.py are NOT touched.** Known trades_core sync drift. Any AI reply changes go in `ai.py`.
2. **No smart/curly quotes in ANY Jinja template.** The microsite.html and the privacy.html addition must use only straight ASCII quotes. The existing codebase uses this pattern — follow it.
3. **Auto-flushed text-backs use `transactional=True`** (already the default). This makes them quiet-hours EXEMPT — correct, because a missed-call text-back is a solicited transactional response, not marketing. Do NOT change this.
4. **Opt-out applies at flush time.** `messaging.send_sms` already checks `db.is_suppressed` before the A2P gate. An opted-out lead who was blocked will be suppressed at flush time — the `queue_blocked_textback` row will be marked `"suppressed"` (via the send_sms return, then `db.mark_scheduled`). This is correct behavior; do NOT bypass the opt-out check.
5. **Dedupe: blocked text-backs should not stack indefinitely.** If a lead generates multiple missed calls during the pending window, each call generates a `blocked_textback` row. All will flush on approval. For Phase 3 this is acceptable (a lead who called 3 times waiting gets 3 texts back). A future dedupe (one per lead per pending window) is a Phase 4 cleanup. Do NOT add dedupe logic now — it would hide the flush test.
6. **Dave NEVER sees Twilio/A2P/10DLC/TCR/brand/campaign.** All UI copy uses: "Your texting number is being activated" / "Texts turn on automatically, usually within a day" / "Your AI is already answering calls." Never: "A2P," "brand," "campaign," "TCR," "10DLC," "Twilio."
7. **Preserve the live host `ringback-gixe.onrender.com`.** No changes to RENDER config, `FIRSTBACK_PUBLIC_URL`, or the host string.
8. **`mode=record` in setup_a2p is kept intact.** It's the operator escape hatch. A3 does not touch it. A2 only ADDS `mode=auto`; the existing `mode=record` block is untouched.
9. **Never call real Twilio in tests.** All three Trust Hub write functions must be tested with `unittest.mock.patch("requests.post")`. Any test that calls a real Twilio endpoint is a broken test.
10. **Path A Starter Brand API fields:** The exact Twilio Starter Brand endpoint and payload differ from the Standard brand (different URL, fewer required fields, `OtpEnabled=true`). Phase 3 code should mock this clearly and add a `# TODO: confirm Path A Starter Brand payload with Twilio docs before live Heritage test` comment in `create_a2p_brand` for the `a2p_registration_path == "A"` branch. Do NOT assume the Standard brand payload works for sole-prop.

---

## OWNER-OPS CHECKLIST (append to `SETUP_NEEDED.md` at review)

- [ ] **`firstback.io` domain** — register + set wildcard DNS `*.firstback.io -> Render app`. Until done: `/c/<slug>` routes work locally but 404 in production (or return the Render default). The code is ready.
- [ ] **Cloudflare Email Routing catch-all** — `@clients.firstback.com` forward-to operator email. Until done: `a2p_contact_email` is generated and displayed but not functional.
- [ ] **`TWILIO_A2P_RESELLER_SID`** — Twilio account team / ISV agreement. Until done: brand submissions work without it (CSP direct) but without ISV throughput benefit.
- [ ] **`GOOGLE_PLACES_API_KEY`** — Enable Places Text Search API in Google Cloud Console. Until done: prefill returns `{}` (JS silently skips, Dave types manually — fine).
- [ ] **`FIRSTBACK_MICRO_SITE_DOMAIN`** — Set to `firstback.io` in Render env (default in config but must be explicitly set in production).
- [ ] **`FIRSTBACK_CLIENTS_DOMAIN`** — Set to `clients.firstback.com` in Render env.
- [ ] **Heritage House dogfood** — First real sole-prop OTP submission to validate Path A Starter Brand payload and TCR timing claim. Do this BEFORE scaling to paid customers.
- [ ] **One LLC test submission** — Confirm `[slug].firstback.io` passes TCR (the unconfirmed assumption per F14). Do with a test business before real customer Path B goes live.
- [ ] **Twilio CSP call** — Confirm `{slug}@clients.firstback.com` passes Authentication+ email rule for Standard brands.
- [ ] **Privacy policy privacy@firstback.com mailbox** — The existing `/privacy` template references `privacy@firstback.com`; ensure this is reachable.

---

## BIGGEST CODE-vs-OWNER-OPS LINE

**The Trust Hub WRITE API is CODE (mockable, testable, buildable now). But the actual TCR submission to real Twilio is OWNER-OPS until the two live confirmations land.** Specifically:
- Building and mocking `create_a2p_brand` / `create_a2p_campaign` is CODE — ship it.
- Whether `[slug].firstback.io` actually passes TCR vetting is DEFERRED — a real submission with a real slug is needed once DNS is live.
- Whether `{slug}@clients.firstback.com` satisfies Twilio Authentication+ for Standard brands is DEFERRED — needs the Twilio CSP call.

The auto-flush (`flush_blocked_textbacks`) is 100% CODE and fully testable with a real DB + patched Twilio. It is the highest-value deliverable in Phase 3 and the one most likely to be poorly tested by agents — hence the un-stubbed integration test requirement.

---

## TOP 3 COLLISION RISKS

1. **`connections.a2p_sync_all` in connections.py** — A2 modifies this function to add the flush call. If the Phase 2 base has already modified `a2p_sync_all` (e.g., a hotfix between staging @ 6295f05 and the build start), A2's diff will conflict. **Mitigation:** A2 must read the exact current `a2p_sync_all` body before writing the modification; do not apply a diff, rewrite the function from scratch using the confirmed code.

2. **`app.py handle_inbound blocked-send branch** — A3 adds the `queue_blocked_textback` call adjacent to the existing `messaging.send_sms` call. The EXACT line of the `send_sms` call in `handle_inbound` must be located in the live code (not assumed from memory). If Phase 2's A3 already restructured `handle_inbound`, the line numbers in this spec are approximate. **Mitigation:** A3 must grep for `send_sms` in the 1306-1357 range and apply the change to the actual live line, not a hardcoded offset.

3. **`db.py scheduled_messages growth index rebuild** — The `_GROWTH_EXCLUSION` string and the growth index are already modified by Phase 2 (to add `sms_retry` and `morning_reminder`). Phase 3's A1 adds `blocked_textback`. If the rebuild guard checks for `"blocked_textback" not in sql` but the sql string formatting changes (e.g., extra whitespace), the check could silently not rebuild. **Mitigation:** The guard in the migration block must check for the presence of the literal string `"blocked_textback"` in the index SQL retrieved from `sqlite_master`. Test `test_sf8_migration.py` must verify the final index SQL contains `blocked_textback` in its WHERE clause after `init_db()` runs twice (idempotency test).
