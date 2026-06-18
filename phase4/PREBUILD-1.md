# PREBUILD-1 — Phase 4 Scope / Sequencing / Gap Analysis
**Pre-Build Planner 1 of 3 — Lane: Scope / Code vs Owner-Ops / What Already Exists**
**Date:** 2026-06-18  
**Baseline:** staging @ ~45e9445, 40/40 green, Phases 0–3 built

---

## 1. Item-by-Item Existence Map

### 1.1 Production Domain

**Status: MISSING (code) / OWNER-OPS (everything real)**

The code is 100% env-var-driven. `PUBLIC_BASE_URL` reads `FIRSTBACK_PUBLIC_URL`
at boot (`config.py:186`). Every TwiML URL, webhook, micro-site link, and Stripe
return URL is built from that constant (`config.py:371–373`). There is literally
zero code to write for the domain swap itself.

**What OWNER-OPS must do (nothing else unblocks this):**
1. Register `firstback.app` (or `getfirstback.com` — pick one) at a registrar.
2. Add the domain in Render → Service → Settings → Custom Domains → Add.
3. Render shows a CNAME/ALIAS record. Set it at the registrar DNS.
4. Wait for TLS provisioning (~1–10 min on Render).
5. Update `FIRSTBACK_PUBLIC_URL` env var on both prod (`firstback`) and staging
   (`firstbackv2`) services in Render dashboard.
6. The preserved `ringback-gixe` Render host still serves (Render keeps old URL
   alive until explicitly removed). Leave it active during migration so Twilio
   webhooks don't break mid-flight; update Twilio webhook URLs last.

**Code delta: ZERO.** The env var update is the deploy.

**RISK:** `FIRSTBACK_PUBLIC_URL` is the same var that activates SF-4 delivery
receipts and SF-7 sentinel TwiML. Changing its value in prod must happen alongside
verifying those two features re-point correctly to the new domain. Not a code risk,
but a prod-ops sequencing hazard.

---

### 1.2 Site Proof: Testimonial / Founder / ROI + Fix Bait-and-Switch CTAs

**Status: PARTIAL**

**What's already clean (from SITE_TRUTH_AUDIT.md fixes, now in code):**
- `customers.html` — fabricated testimonials removed. Now shows honest "be first"
  placeholders. (`templates/customers.html:13–20`)
- `webinars.html` — fake live events removed. Now says "Coming soon" with a
  /contact CTA. (`templates/webinars.html:16–18`)
- "Sign up for free" / "Start free" CTAs — already removed from auth.html and
  the main flow; landing.html, pricing.html use "Get started" → `/signup`.
- `auth.html` — Terms + Privacy links point to real routes; Forgot password now
  goes to `/auth/forgot` (a working reset route). Dead `#` links purged.

**What is MISSING (Phase 4 build targets):**
- `landing.html:111–116` — placeholder testimonial block:
  `"[ Your first customer's quote goes here ]"`. Needs either a real customer
  quote OR a "real results land here" honest placeholder (matching customers.html
  pattern). NOT a build task — it's a content decision. **OWNER-OPS** (get the
  first real customer quote) or a one-line copy fix.
- **Founder/ROI proof section on landing.html** — no section exists at all.
  Blueprint calls for "testimonial/founder/ROI" proof on the marketing site.
  The founder section ("built by people who run a real crew") is flagged P2
  in the audit. **MISSING: needs a new section** — could be as small as 6 lines
  of HTML with a founder name/photo placeholder + an honest ROI framing
  ("contractors who use FirstBack recover an average of X missed calls / month"
  — but ONLY when we have real data; until then the site audit's "no invented
  stats" rule means it must be a forward-looking framing like "your ROI, live,
  every Sunday — from your own bookings").
- `pricing.html:64` — voice callback "included on Pro/Crew (beta)" — voice
  service is deactivated in prod. Marked P1 in site audit; needs "beta / coming
  soon" language or removal. DECISION item.
- `onboarding.html` — Jobber / Housecall Pro integration pills still visible at
  `onboarding.html:149`. Marked I1/P0 in site audit. Should be removed (only
  Google Calendar + "Your existing number" remain honest). SMALL CODE FIX.
- `landing.html:99–103` — same Jobber/Housecall Pro/Angi pills. This page is
  technically "dead/unrouted" per audit (marked I2/P2), but it's still served
  at `/` via the landing route. SMALL CODE FIX.

**Sequencing:** all site fixes are leaf-safe (templates only, no Python). Can be a
single slice build. The founder/ROI section is a design decision first.

---

### 1.3 F12 Analytics / ROI

#### 1.3a Trade-based job-value defaults + `avg_source`

**Status: MISSING**

`db.analytics()` (`db.py:2507`) returns `revenue = None` when `avg_job_value` is
unset. New accounts always get `revenue = None` because `avg_job_value` has no
column default (`db.py:556` — `"REAL"`, no `DEFAULT`). `businesses.trade` column
EXISTS in original `CREATE TABLE` (`db.py:215`) and is populated at signup
(`app.py:302`). The `TRADE_JOB_VALUE_DEFAULTS` dict and the `avg_source` fallback
logic do NOT exist in any file. The analytics payload does not include `plan_cost`,
`avg_source`, `using_default_job_value`, or `roi_multiple`.

**Delta:** ~30 lines in `db.py:2505–2510` (replace null fallback with trade
lookup), add `TRADE_JOB_VALUE_DEFAULTS` dict at `db.py` top level, add
`avg_source`/`plan_cost`/`roi_multiple` to the return dict at `db.py:2548`.
Validate `avg_job_value <= 0` as invalid in `app.py:~1003` (settings handler).

#### 1.3b ROI multiple + headline tile in analytics.html

**Status: MISSING**

`analytics.html` renders tiles via JS (`static/app.js:518–527`). Current tiles:
leads, booked, conversion %, est. revenue. No "FirstBack paid for itself Nx" tile
exists. `roi_multiple`, `plan_cost`, `avg_source`, `cost_per_booking` are not in
the API payload. The existing disclaimer at `analytics.html:26` ("Revenue is an
estimate...") is the right tone.

**Delta:** 2 new fields in `db.analytics()` return + new headline tile in JS +
three-state label (`ESTIMATE (industry default)` / `ESTIMATE (your avg)` /
`ACTUAL`) in `app.js:518`. ~10 Python lines + 1 day frontend work.

#### 1.3c ROI block in weekly digest email

**Status: MISSING**

`convos.digest_email()` (`convos.py:298`) is a WORKING weekly email that sends
AI gap stats + unmet requests. It does NOT call `db.analytics()` and contains
no ROI numbers. The `/tasks/digest` cron (`app.py:961`) fires per tenant, but
needs a **separate Render weekly cron** to actually schedule it (the 60s
`/tasks/run-due` cron does NOT trigger digest). The SMTP channel is set up
(platform `alerts@firstback.app` / Resend — pending OWNER-OPS Resend account).

**Delta:** ~15 lines in `convos.digest_email()` (`convos.py:298`): call
`db.analytics(bid, 7)` at top; prepend ROI block; suppress block (not whole
email) when `leads_n == 0 AND booked_n == 0`; add `?utm_source=digest` link.
**OWNER-OPS:** add a second Render cron → `POST /tasks/digest` weekly (Sunday
8am) with `X-Tasks-Secret` header. Without this cron the email never fires.

#### 1.3d Milestone SMS ("paid for itself")

**Status: MISSING**

No `roi_milestone_sent_at` column exists on `businesses`. No
`check_roi_milestone()` function exists. The post-booking code path in `app.py`
(`~1477`) does not call any milestone check. The Twilio outbound SMS path
(`messaging.send_sms`, `gate=False` for owner alerts) exists and is the correct
channel (`alerts.py:128` pattern).

**Delta:** 1 migration line (add `roi_milestone_sent_at TEXT` to businesses),
new `check_roi_milestone(biz_id)` function, bolt onto post-booking flow at
`app.py:~1480`. Guard: `avg_source == "industry_estimate"` requires `roi_multiple
>= 2.0`; `"owner"` requires `>= 1.0`. Suppress first 72 hours.

#### 1.3e "Calls recovered" metric

**Status: PARTIAL (V1 proxy exists, precise query missing)**

`leads.source DEFAULT 'missed_call'` (`db.py:225`) means every lead in the system
is implicitly a "recovered call" by attribution. The `db.analytics()` return
includes `leads_n` which serves as the V1 proxy. The precise B-spec query
(join `calls WHERE missed=1` to `leads` within 5 min via `from_number`) is NOT
implemented. The `calls.missed`, `calls.business_id`, `calls.from_number`,
`calls.lead_id` columns all exist (`db.py:249–255`).

**Decision:** use `leads_n` (where `source='missed_call'`) as V1 "calls recovered"
label — zero new code, just relabeling in the frontend. Add precise `db.calls_recovered()`
query as M8 in F12 build order (1 day, deferred). The V1 display must say
"leads from missed calls" not claim precision it doesn't have.

---

### 1.4 Shared Retention Path

#### 1.4a Show-Up-Prepared Briefing (structured owner SMS on booking)

**Status: PARTIAL**

The data exists: `_ensure_lead_notes()` (`app.py:1333`) computes structured
`{name, address, project_type, summary}` via `ai.summarize_lead()` and stores
it on the lead. `_compose_briefing()` (`assistant.py:453`) reads this data for
the dashboard briefing card. BUT the booking alert to the owner (`alerts.notify_async`
called at `app.py:1477`) passes ONLY `{lead_id, name, phone, when}` — it does NOT
include `address`, `project_type`, or `summary`. The `alerts.format_message("booking"
, ...)` function (`alerts.py:58`) produces "Estimate booked: {who} for {when}" —
no address, no project detail.

**Delta:** Enrich the booking context at `app.py:~1477`: after booking, fetch
`db.get_lead(lead_id)` to read `address`, `project_type`, `summary` (already
computed by `_schedule_notes` which fires before this point). Pass to
`notify_async`. Extend `alerts.format_message("booking", ...)` (`alerts.py:58`) to
include the structured fields when present:
```
"Estimate booked: {name} — {project_type} at {address}, {when}.
  Summary: {summary}. That's your {n}th booking this week."
```
~20 lines total across `app.py` and `alerts.py`.

**IMPORTANT GAP:** `_schedule_notes()` runs on a background thread. At the moment
`alerts.notify_async("booking", ...)` fires (same call path, `app.py:1477`), the
notes may not be computed yet (notes background thread may still be running). Fix:
read notes AFTER scheduling them, with a short synchronous fallback to the raw
lead name if notes are not yet available. Do NOT block the booking path on the LLM.

#### 1.4b Dispatcher Call TwiML

**Status: MISSING**

`messaging.place_call()` EXISTS (`messaging.py:209`) and is already called from
the sentinel (`connections.py:608`) and voice consent paths (`app.py:2238`). The
`/webhooks/twilio/voice/sentinel-twiml` TwiML endpoint EXISTS (`app.py:1282`).

MISSING: the urgency-alert TwiML endpoint (`/twiml/urgent-alert?lead_id=<id>`)
that reads the caller's last message and speaks it, with `<Gather>` to detect
"press 1" and `<Dial>` to bridge Dave to the customer's phone. MISSING: the
call to `messaging.place_call()` from `alerts._safe_notify` when `kind == "urgent"`.

**Delta:** 1 new route in `app.py` (`/twiml/urgent-alert`), ~30 TwiML lines; 1
new call to `messaging.place_call()` in `alerts._safe_notify()` after the SMS fires
for `kind == "urgent"` (`alerts.py:128`). The `FIRSTBACK_PUBLIC_URL` dependency is
already handled (returns simulated when unset). One call per urgency event, no retry.

**SEAM HAZARD:** The call fires from `alerts._safe_notify()` which runs on a
background thread. It needs the `alert_sms` (Dave's phone) and `lead_id` (to build
the TwiML URL). Both are available in the `notify()` signature flow: `alert_sms`
from `business.get("alert_sms")` and `lead_id` from `context.get("lead_id")`.
No architectural blocker.

#### 1.4c Weekly "while you worked" digest

**Status: PARTIAL (infrastructure exists, ROI block missing)**

The weekly digest infrastructure is FULLY built: `convos.digest_email()` sends AI
gap stats + unmet requests (`convos.py:298–323`), `/tasks/digest` per-tenant fanout
route exists (`app.py:961–972`), `mail.send_email()` channel works. `/digest/send`
manual trigger exists for testing (`app.py:951`).

What is MISSING is only the ROI block injection (covered in §1.3c above). The
SMTP channel (`alerts@firstback.app` / Resend) is OWNER-OPS to activate.

**OWNER-OPS:** Render weekly cron → `POST /tasks/digest` (weekly, Sunday 8am),
`X-Tasks-Secret` header. This is the same `FIRSTBACK_TASKS_SECRET` already needed
for the 60s run-due cron. NOT a new secret — just a second Render cron job pointing
at the same endpoint format.

---

## 2. Code vs OWNER-OPS Split (ruthless)

### OWNER-OPS (zero code needed, or at most one env var)

| Item | What owner must do | Code needed |
|---|---|---|
| Production domain | Registrar → buy domain; Render → add custom domain; DNS CNAME; update `FIRSTBACK_PUBLIC_URL` env on Render | ZERO |
| Weekly digest cron | Render → add a second Cron Job → `POST /tasks/digest` weekly + `X-Tasks-Secret` | ZERO (route exists) |
| SMTP / Resend | Create Resend account, verify `firstback.app` sending domain, set `SMTP_HOST/USER/PASS/FROM` in Render | ZERO |
| Testimonial content | Get first customer quote; replace placeholder in `landing.html:113` | 2-line HTML edit |
| Milestone SMS Twilio config | `ALERT_FROM_NUMBER` must be set in Render (already documented in SETUP_NEEDED, pending) | ZERO |

### CODE (these require actual Python/HTML/JS changes)

| Item | Files | Effort |
|---|---|---|
| Trade defaults + avg_source + roi_multiple in `db.analytics()` | `db.py:2505–2548` | S (~30 lines) |
| ROI headline tile + plan_cost + labels in `analytics.html` + `app.js` | `analytics.html`, `app.js:518` | S-M |
| ROI block injected into `convos.digest_email()` | `convos.py:298` | S (~15 lines) |
| `roi_milestone_sent_at` migration + `check_roi_milestone()` + post-booking hook | `db.py`, `app.py:~1480` | M (1 day) |
| Show-Up-Prepared: enrich booking alert context + `alerts.format_message()` | `app.py:~1477`, `alerts.py:58` | S (~20 lines) |
| Dispatcher Call TwiML endpoint + `place_call` in `alerts._safe_notify` | `app.py` (new route), `alerts.py:~128` | M (1 day) |
| Site fixes: remove Jobber/HCP pills + voice beta label | `templates/onboarding.html:149`, `templates/landing.html:99–103`, `templates/pricing.html:64` | S (< 10 lines) |
| ROI/founder proof section on landing.html | `templates/landing.html` | S (new section, design decision first) |

### DEFERRED (not Phase 4 scope)

| Item | Reason |
|---|---|
| F12 M6 month-over-month `analytics_compare()` | Low priority vs S1–S4 ROI impact; defer to Phase 5 |
| F12 M7 day-3 avg_job_value banner | Can ship with Phase 5 proactive Vic (F11) |
| F12 M8 precise "calls_recovered" query (calls JOIN leads 5-min window) | V1 proxy (leads_n) is honest; precise query is Phase 5 |
| F12 L9 "Mark as won" closed-job tracking | L-tier; deferred |
| F09 quiet hours for Dave's own alerts + burst cap + overnight batch | F09 plan items; not explicitly listed in Phase 4 blueprint |
| F11 Vic proactive (Phase 5) | Blueprint explicitly Phase 5 |

---

## 3. Gaps / Holes / Risks That Would Bite a Builder

### GAP 1 (BIGGEST): Show-Up-Prepared timing hazard
`_schedule_notes()` (`app.py:1357`) fires async (background thread). The booking
alert (`notify_async` at `app.py:1477`) fires SIMULTANEOUSLY on the same booking
event. The lead notes (address, project_type, summary) MAY NOT BE COMPUTED YET
when the alert fires. Builder must implement a synchronous read-or-fallback: check
`lead.get("address")` and `lead.get("project_type")` at alert-compose time; if
empty, the alert body falls back to the basic "Estimate booked: {name} for {when}"
(not broken, just not enriched). This is invisible to the owner — they get the rich
version once notes compute, but the booking alert is always immediate.

### GAP 2: Dispatcher Call requires `FIRSTBACK_PUBLIC_URL` to be set
`messaging.place_call()` builds the TwiML URL from `PUBLIC_BASE_URL` (the env var).
If `FIRSTBACK_PUBLIC_URL` is not set, `place_call()` returns `{"status": "simulated"}`
silently. The Dispatcher Call in `alerts._safe_notify` must check the return status
and log clearly when it cannot fire, rather than silently no-oping. This is the
same hazard as SF-7 sentinel — the pattern is already handled in `connections.py:603`
(returns `"simulated"` explicitly). Builder must replicate that honest fallback.

### GAP 3: Weekly digest has no Render cron wired yet
`/tasks/digest` is a complete, working endpoint. But there is NO Render Cron Job
pointing at it. The digest only fires when Dave manually hits `/digest/send` from
the Training page. This means the "while you worked" weekly heartbeat — a key
retention mechanism and the dead-man's switch for the ticker — is DEAD until a
cron is added. This is OWNER-OPS but is easy to forget. The PREBUILD must flag it
loudly as a deploy prerequisite, not an afterthought.

### GAP 4: `roi_milestone_sent_at` is billing-period aware in spec, not by calendar month
F12-FINAL spec says "once per billing period." Billing (Stripe) is set up in Phase
1 but the `businesses` table has no `billing_period_start` column. The simplest
safe implementation is "once per calendar month" (reset when `roi_milestone_sent_at`
is in a prior month), NOT per Stripe billing cycle. The spec allows this — read
the implementation note in F12-FINAL §6 M5 carefully; the 30-days-ago check is the
correct proxy.

### GAP 5: Source attribution in `db.analytics()` — leads are NOT filtered by source
`db.analytics()` (`db.py:2500`) queries ALL leads for the business regardless of
`source`. The F12 plan (S4) says ROI should only count `source='missed_call'` leads.
Current code is wrong for multi-source tenants (when growth-engine contacts are
added, they'd inflate the ROI count). Builder should add `AND source='missed_call'`
filter to the leads query in `db.analytics()` alongside the trade-defaults change
(natural build partner, same function, same touch).

### GAP 6: No new test file was specified for Phase 4 items
The existing test harness has 40 files. Each Phase 3 feature had its own
`test_sf8_*.py` file. Phase 4 builders must create `test_f12_roi.py` with at
least: trade-default logic, ROI multiple computation, milestone SMS idempotency,
digest ROI block guard (quiet week), and dispatcher TwiML happy path (simulated).
No test infrastructure gaps — the pattern is fully established.

### GAP 7: The landing.html ROI/founder proof section is underspecified
Blueprint says "site proof: testimonial/founder/ROI." The current landing page
(`templates/landing.html`) has: hero, value cards, how-it-works, trades pills,
and one placeholder testimonial. No founder section. No ROI framing section.
The blueprint calls for this but does NOT specify what the ROI section should
contain before real customer data exists. Builder needs a design decision:
(a) a forward-looking "your ROI, every Sunday" teaser (tied to F12 feature) or
(b) wait for first real customer data. The honest constraint from SITE_TRUTH_AUDIT
means no invented stats. This is a CONTENT DECISION that blocks the site-proof
build slice.

---

## 4. First-Cut Build Slice Partition (~3 file-disjoint slices)

The Phase 4 work partitions cleanly into 3 largely file-disjoint slices:

### Slice A — "The number that finds Dave" (F12 ROI core)
**Hot files (serialize):** `db.py`, `app.py`
**Leaf files (parallel-safe):** `analytics.html`, `app.js`, `convos.py`, `test_f12_roi.py`
- `db.py:2505–2548`: trade defaults + avg_source + roi_multiple + source filter
- `app.py:~1003`: validate avg_job_value > 0 in settings handler
- `app.py:~1480`: `check_roi_milestone()` call post-booking + migration
- `convos.py:298`: ROI block in digest_email
- `analytics.html` + `app.js:518`: headline tile + labels
- New `test_f12_roi.py`
**No overlap with Slice B or C.**

### Slice B — "Shared retention: briefing + dispatcher"
**Hot files:** `app.py`, `alerts.py`
**Leaf files:** none needed beyond those two
- `alerts.py:58`: enrich `format_message("booking", ...)` with structured fields
- `app.py:~1477`: pass address+project_type+summary in booking context (read-or-fallback pattern)
- `app.py` (new route): `/twiml/urgent-alert?lead_id=<id>` TwiML endpoint
- `alerts.py:~128`: `messaging.place_call()` call in `_safe_notify` for urgent
**No overlap with Slice A (different sections of app.py and alerts.py).**

### Slice C — "Proof on the site + domain ops guide"
**Hot files:** templates only (leaf-safe, no Python)
- `templates/onboarding.html:149`: remove Jobber/HCP pills
- `templates/landing.html:99–103`: remove false integration pills
- `templates/landing.html`: add ROI/founder proof section (content decision first)
- `templates/pricing.html:64`: voice beta label / removal
- `templates/landing.html:113`: replace placeholder testimonial (OWNER-OPS content)
- **OWNER-OPS checklist**: domain registrar → Render custom domain → DNS → `FIRSTBACK_PUBLIC_URL` env update → Twilio webhook URL update → Render weekly digest cron → Resend SMTP setup
**Pure template edits + an ops runbook. No Python, no conflicts with A or B.**

---

## 5. Summary: What Already Exists vs What Is Net-New

| Phase 4 Item | Status | Key Code Location | Delta |
|---|---|---|---|
| Production domain (code side) | EXISTS | `config.py:186` | ZERO — env var only |
| Site P0/P1 fixes | PARTIAL | `templates/*.html` | Small HTML edits |
| Landing ROI/founder proof | MISSING | `templates/landing.html` | New section (design first) |
| F12 trade defaults + avg_source | MISSING | `db.py:2507` | ~30 lines |
| F12 ROI multiple + tile | MISSING | `db.py`, `app.js:518`, `analytics.html` | ~10 Python + 1d frontend |
| F12 ROI block in digest email | MISSING | `convos.py:298` | ~15 lines |
| F12 milestone SMS | MISSING | `db.py` (migration), `app.py:~1480` | 1 day |
| F12 "calls recovered" V1 | EXISTS (proxy) | `db.py:2540` (leads_n) | Label rename only |
| Show-Up-Prepared briefing | PARTIAL | `alerts.py:58`, `app.py:1477` | ~20 lines + timing fix |
| Dispatcher Call TwiML | MISSING | `app.py` (new route), `alerts.py:~128` | 1 day |
| Weekly digest infrastructure | EXISTS | `convos.py:298`, `app.py:961` | ROI block only |
| Weekly digest cron | MISSING | N/A — OWNER-OPS | Render cron add |
| SMTP / Resend | MISSING | N/A — OWNER-OPS | Account + DNS |

---

*Written by: PREBUILD-1 — Phase 4 — 2026-06-18*
*Files read: AUTONOMY-BLUEPRINT.md, F12-FINAL.md, F09-FINAL.md, F05-FINAL.md, F02-FINAL.md, db.py, app.py, alerts.py, assistant.py, convos.py, reminders.py, messaging.py, config.py, render.yaml, SITE_TRUTH_AUDIT.md, SETUP_NEEDED.md, HANDOFF.md, GO_LIVE_RUNBOOK.md, templates/landing.html, templates/analytics.html, templates/pricing.html, templates/customers.html, templates/webinars.html, static/app.js*
