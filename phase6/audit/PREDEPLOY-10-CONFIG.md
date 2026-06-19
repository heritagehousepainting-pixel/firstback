# CONFIG / DEPLOY-READINESS / BOOT / OPS — Pre-Deploy Audit
**Auditor lane:** #10 of 10 — Config, deploy readiness, boot, ops  
**Branch:** staging @ 55d2601  
**Date:** 2026-06-19  
**Files audited:** `config.py`, `render.yaml`, `db.py` (boot/storage), `app.py` (ticker/tasks), `billing.py` (Stripe env), `messaging.py` (trust gates), `SETUP_NEEDED.md`

---

## 1. ENV-VAR INVENTORY

All `FIRSTBACK_*`, `STRIPE_*`, `TWILIO_*`, `ANTHROPIC_*`, `SMTP_*`, `GOOGLE_*` vars with failure mode:

| Variable | Source | What it does | If unset / wrong | Fail mode |
|---|---|---|---|---|
| `FIRSTBACK_SECRET` | `config.py:266` | Signs Flask session cookie | If unset, uses insecure default `"dev-insecure-secret-change-me"` | **FAIL LOUD** — RuntimeError at import when `FIRSTBACK_HTTPS=1` (`config.py:276-280`). If `FIRSTBACK_HTTPS` is also unset, this guard is **disarmed** — see P0-2. |
| `FIRSTBACK_TOKEN_KEY` | `config.py:288` | Encrypts Google OAuth refresh tokens at rest | If unset in prod, tokens stored plaintext | **FAIL LOUD** — RuntimeError at import when `FIRSTBACK_HTTPS=1` (`config.py:293-298`). Same disarm risk as above. |
| `FIRSTBACK_OWNER_PASSWORD` | `config.py:429` | Seeds the owner login for business 1 | If unset, dev default `"dev-change-me-not-for-prod"` is used | **FAIL LOUD** — RuntimeError when `_is_prod` and password is the dev default (`config.py:430-434`). |
| `FIRSTBACK_HTTPS` | `config.py:273` | Arms all three prod fail-fasts + sets Secure cookie flag | **If unset, the entire prod safety net is disarmed** (SECRET, TOKEN_KEY, OWNER_PASSWORD guards all become inert). | **SILENT** — no error; app runs with insecure defaults. |
| `FIRSTBACK_ENV` | `config.py:274` | Alternative to FIRSTBACK_HTTPS to set `_is_prod` | Same disarm risk; either one suffices | SILENT |
| `FIRSTBACK_DB_PATH` | `config.py:442` | Durable at-rest DB location | If unset, falls back to `BASE_DIR/firstback.db` (code dir, ephemeral on Render) | **SILENT data loss** on redeploy — see P0-1 analysis below |
| `FIRSTBACK_DB_LOCAL_MIRROR` | `config.py:450` | Enables local-disk/network-backup split to prevent WAL hang | If unset, SQLite opens `/var/data/firstback.db` directly (could WAL-hang) | SILENT (boot hang, not crash) |
| `FIRSTBACK_DB_LOCAL_PATH` | `config.py:452` | Local disk path for SQLite when mirror enabled | Defaults to `/tmp/firstback.db` | SILENT (uses /tmp default) |
| `FIRSTBACK_DB_BACKUP_PATH` | `config.py:455` | Alternate backup path when mirror is OFF | Only used when mirror is off | SILENT |
| `FIRSTBACK_TASKS_SECRET` | `config.py:523` | Guards `POST /tasks/run-due` cron endpoint | If unset, endpoint always returns 403 | **FAIL CLOSED** — no tasks run. Scheduler silently dead. |
| `FIRSTBACK_INTERNAL_SECRET` | `config.py:241` | Guards `/internal/voice/turn` (voice relay) | If unset, endpoint always returns 403 | FAIL CLOSED — voice relay disabled (safe since voice is off) |
| `FIRSTBACK_RUN_TICKER` | `app.py:3150` | Starts the in-process reminder ticker at gunicorn load | If unset, ticker never starts; `/tasks/run-due` is the only path | SILENT — no reminders/follow-ups |
| `ANTHROPIC_API_KEY` | `config.py:47` | Enables Claude AI brain | If unset, falls back to demo brain | SILENT fallback (safe, but AI features silently degraded) |
| `FIRSTBACK_PROVIDER` | `config.py:35` | Which AI provider to use | Defaults to `"claude"` | SILENT fallback to demo when key absent |
| `FIRSTBACK_DAILY_COST_CAP` | `config.py:54` | Per-tenant daily AI spend cap | Defaults to $1.00 | SILENT (low default may be too tight for active use) |
| `CLAUDE_MODEL` | `config.py:48` | Claude model for SMS/booking | Defaults to `"claude-sonnet-4-6"` | SILENT |
| `CLAUDE_MODEL_VOICE` | `config.py:49` | Claude model for voice turns | Defaults to `"claude-haiku-4-5"` | SILENT |
| `STRIPE_SECRET_KEY` | `billing.py:26` | Stripe API access | If unset, checkout raises RuntimeError | FAIL LOUD at call time (not boot) |
| `STRIPE_WEBHOOK_SECRET` | `billing.py:27` | Validates Stripe webhook signatures | If unset, webhook handler raises RuntimeError | FAIL LOUD at call time |
| `STRIPE_PRICE_STARTER` | `billing.py:33` | Starter plan price ID | If unset, unrecognized price → logs BILLING WARNING → downgrades to starter | FAIL LOUD (log + email) but STILL SILENTLY grants starter |
| `STRIPE_PRICE_PRO` | `billing.py:34` | Pro plan price ID | Same | Same |
| `STRIPE_PRICE_CREW` | `billing.py:35` | Crew plan price ID | Same | Same |
| `STRIPE_PRICE_STARTER_ANNUAL` | `billing.py:36` | Annual starter price ID | Same | Same |
| `STRIPE_PRICE_PRO_ANNUAL` | `billing.py:37` | Annual pro price ID | Same | Same |
| `STRIPE_PRICE_CREW_ANNUAL` | `billing.py:38` | Annual crew price ID | Same | Same |
| `TWILIO_ACCOUNT_SID` | `config.py:192` | Twilio API access | If unset, `configured()=False`; all SMS is simulated | SILENT simulated mode |
| `TWILIO_AUTH_TOKEN` | `config.py:193` | Twilio authentication | Same | Same |
| `TWILIO_FROM_NUMBER` | `config.py:194` | Default outbound SMS number | If unset + no per-business number, sends are simulated | SILENT simulated mode |
| `ALERT_FROM_NUMBER` | `config.py:198` | Platform Twilio number for owner-alert SMS | If unset, falls back to tenant's own from-number | SILENT (may route owner alerts through A2P number) |
| `TWILIO_TRUST_PRODUCT_SID` | `config.py:190` | Gates A2P write API (brand/campaign creation) | If unset, `trust_hub_configured()=False`; A2P submissions simulated | FAIL CLOSED — keeps A2P safe by default |
| `TWILIO_A2P_RESELLER_SID` | `config.py:191` | ISV reseller SID (optional) | If unset, omitted from submissions | SILENT (optional) |
| `FIRSTBACK_PUBLIC_URL` | `config.py:202` | Public base URL for Twilio webhooks, status callbacks | If unset, SMS status callbacks blank, sentinel TwiML URL can't build | SILENT — delivery receipts (SF-4) and forwarding verification (SF-7) silently disabled |
| `FIRSTBACK_VOICE_URL` | `config.py:210` | Voice service public URL | If unset, voice leg is OFF | FAIL CLOSED — voice off by default (correct) |
| `FIRSTBACK_INTERNAL_SECRET` | `config.py:241` | Shared secret for voice service → web app relay | If unset, `/internal/voice/turn` always 403 | FAIL CLOSED (voice is off, so moot today) |
| `SMTP_HOST` | `config.py:250` | SMTP server for email alerts | If unset, `mail.configured()=False`; emails skipped | SILENT simulated mode |
| `SMTP_FROM` | `config.py:255` | From address for emails | Same gating | SILENT |
| `SMTP_USER` | `config.py:253` | SMTP auth username | If unset with SMTP_HOST set, sends will fail at SMTP AUTH | SILENT (runtime failure) |
| `SMTP_PASS` | `config.py:254` | SMTP password | Same | Same |
| `GOOGLE_CLIENT_ID` | `config.py:168` | Google OAuth client | If unset, Calendar/Contacts shows "Coming soon" | FAIL CLOSED |
| `GOOGLE_CLIENT_SECRET` | `config.py:169` | Google OAuth secret | Same | Same |
| `GOOGLE_REDIRECT_URI` | `config.py:170-171` | Calendar OAuth callback URI | Defaults to localhost (wrong in prod) | SILENT wrong callback |
| `GOOGLE_CONTACTS_REDIRECT_URI` | `config.py:175-177` | Contacts OAuth callback URI | Same localhost default | SILENT wrong callback |
| `GOOGLE_PLACES_API_KEY` | `config.py:145` | Business name prefill at signup | If unset, returns `{}` (owner types manually) | FAIL CLOSED |
| `FIRSTBACK_OPERATOR_EMAILS` | `config.py:530` | Comma-separated list of operator emails | If unset, no operator exists; A2P record action closed to everyone | SILENT |
| `FIRSTBACK_SCREEN_MODE` | `config.py:75` | Call-screening rollout mode: off/monitor/enforce | Defaults to `"monitor"` | SILENT (monitor is the safe default) |
| `FIRSTBACK_TZ` | `config.py:311` | App timezone (IANA) | If unset, uses server local zone | SILENT |
| `FIRSTBACK_VOICE_MONTHLY_CAP_CENTS` | `config.py:223-226` | Monthly voice spend cap per business | Defaults to 2000 cents ($20) | SILENT |
| `FIRSTBACK_VOICE_CREDIT_RATE_CENTS` | `config.py:229-231` | Cost per 30-sec billing block | Defaults to 25 cents | SILENT |
| `MINIMAX_API_KEY` | `config.py:38` | MiniMax AI provider | If unset, falls back to demo | SILENT |
| `HIYA_API_KEY` | `config.py:129` | Hiya reputation API key | Required when `FIRSTBACK_REPUTATION_PROVIDER=hiya` | SILENT wrong config (lookups fail open) |

---

## 2. RISK ANALYSIS

### P0-1 — DB-PATH DATA-WIPE TRAP (config.py:442-455 + render.yaml:36-39)

**Verdict: CONTAINED — but requires owner action to stay safe.**

**What happens if FIRSTBACK_DB_PATH is unset:**  
`config.py:442`: `_db_at_rest = os.environ.get("FIRSTBACK_DB_PATH", "").strip() or str(BASE_DIR / "firstback.db")`  
If the env var is missing, DB falls back to the CODE DIRECTORY (`BASE_DIR/firstback.db`), which is ephemeral on Render. Every redeploy wipes data silently.

**In render.yaml (line 36-37), it IS set:**
```yaml
- key: FIRSTBACK_DB_PATH
  value: /var/data/firstback.db
```
And `FIRSTBACK_DB_LOCAL_MIRROR=1` (line 39) is also set, so:
- `DB_BACKUP_PATH = /var/data/firstback.db` (the durable network disk)
- `DB_PATH = /tmp/firstback.db` (local fast disk)

This is the CORRECT configuration. SQLite runs on `/tmp`, never on the network FS. The `/var/data` disk holds only the backup snapshot.

**The trap fires if:**
- The operator deploys manually without applying the blueprint, or
- The blueprint is applied but the env var is accidentally deleted from the dashboard

**Fail mode: SILENT** — no error, starts on empty DB, no warnings visible to operator.

**SETUP_NEEDED.md coverage (line 12):** "Reconcile all `FIRSTBACK_*` env + the DB path in Render BEFORE the next deploy" — mentioned but not a step-by-step checklist item. The runbook (`GO_LIVE_RUNBOOK.md:37`) does list `FIRSTBACK_DB_PATH` explicitly as required.

**Risk: P1** — render.yaml IS correct, but the silent fallback is the single most dangerous misconfig. The `SETUP_NEEDED.md` warning is adequate but could be stronger.

---

### P0-2 — PROD SIGNAL DISARMS ALL FAIL-FASTS (config.py:272-298, render.yaml:48-49)

**Verdict: WIRED CORRECTLY in render.yaml.**

The three boot-time RuntimeErrors (SECRET_KEY, TOKEN_KEY, OWNER_PASSWORD) only fire when `_is_prod` is True. `_is_prod` requires `FIRSTBACK_HTTPS=1` or `FIRSTBACK_ENV=production`.

`render.yaml:48-49` sets `FIRSTBACK_HTTPS=1`. So the safety net IS armed on this blueprint deploy.

**However:** `FIRSTBACK_TOKEN_KEY` is NOT in `render.yaml` — it is only mentioned as a comment note in `SETUP_NEEDED.md:12`. Since `FIRSTBACK_HTTPS=1` IS set in the blueprint, boot WILL FAIL with RuntimeError (`config.py:293-298`) unless the operator sets `FIRSTBACK_TOKEN_KEY` in the Render dashboard before deploying.

**This is a P0**: the blueprint will produce a boot crash on first deploy unless the operator pre-sets `FIRSTBACK_TOKEN_KEY` in the dashboard. The `SETUP_NEEDED.md:12` does mention it, but render.yaml should at minimum include a `generateValue: true` entry for it (as it does for FIRSTBACK_SECRET and FIRSTBACK_TASKS_SECRET).

**File:line:** `config.py:293-298`, `render.yaml` (missing `FIRSTBACK_TOKEN_KEY` entry)

---

### P0-3 — FIRSTBACK_OWNER_PASSWORD NOT IN render.yaml (config.py:430-434)

**Verdict: BOOT WILL CRASH on first deploy unless set in dashboard.**

`render.yaml:79` only mentions `FIRSTBACK_OWNER_EMAIL / FIRSTBACK_OWNER_PASSWORD` as a comment. No `generateValue: true` entry exists. Since `FIRSTBACK_HTTPS=1` IS set (which arms `_is_prod`), boot raises RuntimeError at `config.py:430-434` if `FIRSTBACK_OWNER_PASSWORD` is unset.

`SETUP_NEEDED.md` does not call this out as a required pre-deploy step in the checklist items. The render.yaml should have a `generateValue: true` for `FIRSTBACK_OWNER_PASSWORD`.

**File:line:** `config.py:430-434`, `render.yaml:79` (comment only, no actual env var entry)

**Risk: P0** — blocks boot.

---

### Gate 1 — VOICE_PUBLIC_URL (config.py:210)

**Verdict: CORRECTLY GATED.**

`FIRSTBACK_VOICE_URL` defaults to `""`. `VOICE_PUBLIC_URL` stays empty. Voice is off. The voice service block in `render.yaml:81-97` is entirely commented out. This is correct — voice is in beta.

---

### Gate 2 — trust_hub_configured for A2P (messaging.py:53-58)

**Verdict: CORRECTLY GATED.**

`trust_hub_configured()` requires both `TWILIO_ACCOUNT_SID/AUTH_TOKEN` AND `TWILIO_TRUST_PRODUCT_SID`. `render.yaml` does not set `TWILIO_TRUST_PRODUCT_SID`; it is only mentioned in `SETUP_NEEDED.md:41`. A2P write API cannot fire accidentally. Correct.

---

### Gate 3 — FIRSTBACK_TASKS_SECRET (config.py:523, app.py:2864-2868)

**Verdict: FAIL CLOSED — CORRECTLY wired. One ops gap.**

`/tasks/run-due` always returns 403 when `FIRSTBACK_TASKS_SECRET` is unset. It IS in `render.yaml:42-43` with `generateValue: true`. Good.

However, `FIRSTBACK_INTERNAL_SECRET` (needed for the voice relay endpoint `/internal/voice/turn`) is NOT in `render.yaml` and only mentioned parenthetically in `SETUP_NEEDED.md:238`. Since voice is off this is not a P0, but it is a gap if voice is ever enabled. The internal relay fails closed (403) when unset, which is safe.

**Risk: P2** — moot today (voice is off), but not documented in render.yaml.

---

### Gate 4 — ALERT_FROM_NUMBER (config.py:198, render.yaml:64-65)

**Verdict: PLACEHOLDER — alerts will fall back to tenant number.**

`render.yaml:64-65` sets `ALERT_FROM_NUMBER` to `""` with `sync: false`. If the operator doesn't set a real E.164 number in the dashboard, owner-alert SMS will fall back to using the tenant's own `from_number` (the A2P customer-facing number). This is the pre-alert behavior, not a crash. Risk: **P2** — noted in SETUP_NEEDED.md:12.

---

### Item 4 — TICKER RUN MODE (config.py:517, app.py:3150-3151, render.yaml:44-45)

**Verdict: CORRECTLY CONFIGURED.**

`render.yaml:44-45` sets `FIRSTBACK_RUN_TICKER=1`. `app.py:3150-3151` checks this at module load (not just `__main__`) and starts the ticker. With gunicorn `--workers 1`, exactly one ticker runs. The architecture is correct.

SETUP_NEEDED.md also calls for an external Render cron → `POST /tasks/run-due` as a redundant path, and an external uptime monitor on `/health/ticker`. These are owner-ops gaps (not code bugs). The endpoint itself correctly requires `FIRSTBACK_TASKS_SECRET`.

---

### Item 5 — render.yaml INTERNAL CONSISTENCY

**COMPLETE CHECK:**

| Property | Value | OK? |
|---|---|---|
| `buildCommand` | `pip install -r requirements.txt` | OK |
| `startCommand` | `gunicorn app:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT` | OK — 1 worker for SQLite single-writer |
| `disk.mountPath` | `/var/data` | OK — matches `FIRSTBACK_DB_PATH` value |
| `disk.sizeGB` | `1` | OK for single-business SQLite |
| `FIRSTBACK_DB_PATH` | `/var/data/firstback.db` | OK |
| `FIRSTBACK_DB_LOCAL_MIRROR` | `"1"` | OK — SQLite on /tmp, /var/data is backup only |
| `FIRSTBACK_HTTPS` | `"1"` | OK — arms all prod fail-fasts |
| `FIRSTBACK_SCREEN_MODE` | `"monitor"` | OK — safe rollout default |
| `FIRSTBACK_RUN_TICKER` | `"1"` | OK |
| `FIRSTBACK_TZ` | `America/New_York` | OK |
| `FIRSTBACK_SECRET` | `generateValue: true` | OK — auto-generated |
| `FIRSTBACK_TASKS_SECRET` | `generateValue: true` | OK — auto-generated |
| `healthCheckPath` | `/login` | OK — `/login` route exists at app.py:380 |
| `FIRSTBACK_TOKEN_KEY` | **MISSING** | **P0 — boot crash** (see P0-2) |
| `FIRSTBACK_OWNER_PASSWORD` | **MISSING** | **P0 — boot crash** (see P0-3) |
| `ANTHROPIC_API_KEY` | comment only | OK (falls back to demo, not a crash) |
| `STRIPE_SECRET_KEY` | comment only | OK (raises at call time, not boot) |
| `TWILIO_*` | comment only | OK (simulated mode, not a crash) |
| Voice service block | commented out | OK — voice off by design |
| `ringback-gixe.onrender.com` | Not touched | Preserved in tests + runbook |

**Internal consistency: GOOD** except the two missing required vars.

---

### Item 6 — Live URL preservation

`ringback-gixe.onrender.com` is the existing live Render URL. render.yaml does NOT set `FIRSTBACK_PUBLIC_URL` (only a comment at line 74). The live URL is preserved — nothing in config or render.yaml renames it or breaks it. Tests correctly set it as a default in `os.environ.setdefault(...)` calls. SETUP_NEEDED.md:50 explicitly says to keep it reachable during cutover. **CORRECT.**

---

### Item 7 — GOOGLE_REDIRECT_URI LOCALHOST DEFAULT (config.py:170-177)

Both `GOOGLE_REDIRECT_URI` and `GOOGLE_CONTACTS_REDIRECT_URI` default to `http://127.0.0.1:8800/...`. In production, Google OAuth will reject these callbacks when the user tries to connect Google Calendar/Contacts. Neither is set in render.yaml.

SETUP_NEEDED.md covers Google OAuth setup but does not list the redirect URIs as a required env var to set before deploy. Since `GOOGLE_CLIENT_ID/SECRET` are also not set, Google Calendar/Contacts will show "Coming soon" and this won't be reached. **Risk: P2** — silent failure if someone sets `GOOGLE_CLIENT_ID/SECRET` without also setting the redirect URIs.

---

### Item 8 — SMTP_HOST/FROM IN render.yaml (render.yaml:55-60)

`render.yaml:55-60` sets placeholder values (`smtp.resend.com`, `alerts@firstback.app`) with `sync: false`. The `sync: false` flag means Render will display these values but NOT update them if the blueprint is re-synced (protecting values the operator has set to real values in the dashboard). This is correct behavior. `SMTP_USER`/`SMTP_PASS` are not in the blueprint (secrets should be set in dashboard only). **OK.**

---

## 3. SETUP_NEEDED.md COMPLETENESS CHECK

| Required step | In SETUP_NEEDED? | In render.yaml? | Gap? |
|---|---|---|---|
| `FIRSTBACK_HTTPS=1` to arm fail-fasts | Yes (line 331) | Yes (line 48-49) | None |
| `FIRSTBACK_TOKEN_KEY` required in prod | Yes (line 12, 291, 326) | **NO entry** | **P0 gap — will boot-crash** |
| `FIRSTBACK_OWNER_PASSWORD` required | Implied by line 79 comment only | **NO entry** | **P0 gap — will boot-crash** |
| `FIRSTBACK_SECRET` | Yes | `generateValue: true` | None |
| `FIRSTBACK_TASKS_SECRET` | Yes (line 12) | `generateValue: true` | None |
| External cron → `/tasks/run-due` | Yes (line 17-18) | Not in yaml (external) | Ops gap, not code |
| Resend account + domain | Yes (line 19) | Placeholder values | Ops gap |
| `FIRSTBACK_PUBLIC_URL` | Yes (Phase 2 section) | Comment only | Ops gap — delivery receipts off until set |
| `FIRSTBACK_INTERNAL_SECRET` | Mentioned line 238 | **NOT in render.yaml, NOT in checklist** | P2 gap (moot while voice off) |
| Stripe Price IDs (6 vars) | Yes (Phase 1 section) | Comment only | Ops gap — billing needs these |
| Twilio credentials | Yes (multiple sections) | Comment only | Ops gap |
| `TWILIO_TRUST_PRODUCT_SID` | Yes (Phase 3 section) | Not in yaml | Ops gap — A2P stays simulated |
| `ALERT_FROM_NUMBER` | Yes (line 12) | Placeholder `""` | Owner must set real number |
| External uptime monitor `/health/ticker` | Yes (Phase 6b) | N/A | Ops gap |
| Google OAuth redirect URIs | Not listed as a required env var | Not in yaml | P2 gap |

---

## 4. FINDINGS SUMMARY

### P0 Issues (BLOCK DEPLOY)

**P0-A: FIRSTBACK_TOKEN_KEY missing from render.yaml — boot crash**  
`render.yaml` has `FIRSTBACK_HTTPS=1` which arms `_is_prod=True`. `config.py:293-298` raises `RuntimeError` at import if `FIRSTBACK_TOKEN_KEY` is empty and `_is_prod` is True. render.yaml does NOT include a `generateValue: true` entry for `FIRSTBACK_TOKEN_KEY`. The app will refuse to boot on first deploy unless the operator manually sets this in the Render dashboard BEFORE deploying.  
Fix: add `- key: FIRSTBACK_TOKEN_KEY` / `generateValue: true` to render.yaml (same as FIRSTBACK_SECRET).

**P0-B: FIRSTBACK_OWNER_PASSWORD missing from render.yaml — boot crash**  
`config.py:430-434` raises `RuntimeError` when `_is_prod=True` and `SEED_OWNER_PASSWORD` is the dev default. render.yaml line 79 only mentions it in a comment. No `generateValue: true` entry exists.  
Fix: add `- key: FIRSTBACK_OWNER_PASSWORD` / `generateValue: true` to render.yaml.

### P1 Issues (HIGH RISK, NOT boot-blocking if owner acts pre-deploy)

**P1-A: FIRSTBACK_DB_PATH silent data-loss if env var deleted**  
`config.py:442` fallback to `BASE_DIR/firstback.db` (ephemeral code dir) is completely silent. No log, no error. render.yaml IS correct (line 36-37), but a manual deploy or env-var deletion silently wipes all leads/bookings on next redeploy. There is no startup assertion that `DB_PATH` is on a persistent mount.  
Recommendation: add a startup check in `db.py:init_db` that logs a loud `[CRITICAL]` warning if `DB_PATH` starts with `/tmp` but `FIRSTBACK_DB_LOCAL_MIRROR` is not set (or if DB_PATH resolves to the code directory).

**P1-B: FIRSTBACK_INTERNAL_SECRET missing from SETUP_NEEDED checklist**  
SETUP_NEEDED.md mentions it parenthetically (line 238) but does not include it in any actionable checklist. The voice relay endpoint (`/internal/voice/turn`) fails closed (403) without it, which is safe today. But when voice is enabled, this omission will silently break the relay. Should be in the Phase 3/4 ops checklist.

### P2 Issues (LOW RISK / GAPS)

**P2-A: GOOGLE_REDIRECT_URI defaults to localhost**  
`config.py:171, 176`: Both Google OAuth redirect URIs default to `http://127.0.0.1:8800/...`. Neither is in render.yaml. In prod, connecting Google will silently fail with an OAuth mismatch. Safe today since `GOOGLE_CLIENT_ID` is also not set, but should be in the setup docs as a required pair with the client credentials.

**P2-B: ALERT_FROM_NUMBER is a blank placeholder in render.yaml**  
`render.yaml:65` sets `ALERT_FROM_NUMBER` to `""`. Owner-alert SMS falls back to tenant's A2P number. Not a crash, but the "platform ops" vs "tenant A2P" separation is lost. SETUP_NEEDED.md:12 covers this.

**P2-C: SMTP_USER/SMTP_PASS not in render.yaml at all**  
The render.yaml comments mention setting them as secrets in the dashboard, but there's no reminder entry (even commented). If `SMTP_HOST` is set but credentials are omitted, email sends will fail at SMTP AUTH with no boot-time warning.

**P2-D: No startup validation that disk is mounted before DB open**  
`db.py:init_db` creates the parent directory (`makedirs`) but does not verify that `/var/data` is actually a Render disk mount vs. a temporary container directory. If the disk fails to attach, the app starts on an ephemeral directory silently.

---

## 5. DEPLOY-READINESS VERDICT

**NOT SAFE TO DEPLOY AS-IS.**

The blueprint, when applied, will produce a boot crash because `FIRSTBACK_TOKEN_KEY` and `FIRSTBACK_OWNER_PASSWORD` are not wired with `generateValue: true` in render.yaml, and `FIRSTBACK_HTTPS=1` is present (arming the fail-fasts). The operator MUST either (a) add these two entries to render.yaml, or (b) manually pre-set them in the Render dashboard before deploying.

**Top 3 must-set env vars before ANY deploy:**
1. `FIRSTBACK_TOKEN_KEY` — any long random string; required or boot crashes (config.py:293)
2. `FIRSTBACK_OWNER_PASSWORD` — strong password; required or boot crashes (config.py:430)
3. `FIRSTBACK_SECRET` — already `generateValue: true` in render.yaml; confirm it generates before first request

Everything else (Twilio, Stripe, Google, AI) degrades gracefully to simulated/gated mode. Only the above two will hard-crash boot.
