# Batch G Audit — Voicemail→Lead + Web-Chat Widget (Plan 10, parts 1 & 2)

**Audited:** 2026-06-20
**Auditor:** read-only security + correctness pass against the uncommitted diff
**Scope:** `git diff HEAD -- app.py db.py templates/settings.html` + `static/widget.js` (new file)
**Red-team baseline:** `product-review/plan-audits/10-audit.md` (flagged B1, B2)

---

## Verdict

**SHIP**

Both B1 and B2 are correctly fixed. All new public endpoints are properly defended: phone validated to E.164 before any DB write, rate-limited per (slug,IP), slug→biz lookup requires `widget_enabled=1` (parameterized, no injection), and the A2P gate in `messaging.send_sms` is untouched. Existing tenants are strict no-ops until they opt in. No SQL injection, no double-record, no dual live-call recording, TCPA consent present. One P2 note (rate counter burns a slot on blocked slugs before widget_enabled check — see Findings), but it is cosmetic and not exploitable for SMS spam. Safe to ship.

---

## Findings

| Severity | File:line | Issue | Fix |
|----------|-----------|-------|-----|
| P2 | `app.py:2904-2908` (`_widget_blocked`) | Rate-limiter increments the counter **before** the slug→biz check. A bad actor probing non-existent slugs burns through the 5-slot window for a legitimate user sharing their IP. The counter increments even if the slug resolves to nothing. Not exploitable for SMS spam (the slug check still blocks the downstream send) but a vandal could prevent a real visitor on a shared IP (NAT, coffee shop) from submitting. | Move `_WIDGET_RATE[key].append(...)` inside `widget_lead` **after** `_biz_id_by_widget_slug` confirms the slug is real and enabled. Alternatively, pre-check slug validity before calling `_widget_blocked`. |
| P2 | `static/widget.js:87-88` | Client-side digit check (`replace(/\D/g, "").length < 10`) is a UX guard only — it has no bearing on security (server validates to E.164 via `messaging.to_e164`). However it will accept international numbers with 10 non-digit chars that fail server E.164 validation (e.g. `020 7946 0000` UK), giving the user a spinner then a silent error. | On server 400, surface the error string from the JSON body (`r.json().error`) rather than just "Try again" so the user understands their number format was rejected. (UX, not security.) |

---

## Verified-good

### B1 FIXED — inline outbound-filter (no `direction=` kwarg)
`app.py:2880`: `has_outbound = any(m.get("direction") == "out" for m in db.get_messages(lead["id"]))` — correctly calls `get_messages(lead_id)` with the only argument it accepts, then filters in Python. No `TypeError` at runtime. Matches the recommended fix in `10-audit.md § B1`.

### B2 FIXED — no fake `vm_url` direction; recording_url on the message row
`app.py:2878`: `db.add_message(lead["id"], "in", f"[Voicemail] {transcript}", recording_url=recording_url)` — direction is `"in"` (standard inbound), not `"vm_url"`. The recording URL travels as a separate column on the same row. `db.py:1530-1537` confirms `add_message` now accepts `recording_url=None` and includes it in the INSERT. No fake direction-type row; digests/exports that filter `direction IN ('in','out')` will never skip it.

### Inert-when-disabled (voicemail)
`app.py:2844`: the `<Record>` TwiML block is inside `if biz.get("voicemail_enabled"):`. When `voicemail_enabled` is 0 (the column default, `db.py:889`), the existing "We just sent you a text. Goodbye." path executes unchanged. Live call flow is completely unmodified for existing tenants.

### Inert-when-disabled (widget)
`_biz_id_by_widget_slug` (`app.py:2910-2918`) requires `widget_enabled=1` in the SQL predicate. If the column is 0 or the slug is unknown, the function returns `None` and every public endpoint (config.js, widget/lead) returns an empty/error response. No DB writes, no SMS, no cross-tenant data.

### A2P-gated send
Both new `send_sms` calls (`app.py:2883`, `app.py:2970`) pass no `gate=` override — they use the default `gate=True`. `messaging.py:140` confirms: `if gate and configured() and not compliance.a2p_ready(business): ... return {"status": "blocked", "reason": "a2p_not_approved"}`. The widget does not bypass the A2P carrier gate.

### No double-record
`open_conversation` (`app.py:1887`) calls `db.add_message(lead["id"], "out", reply)` at `app.py:1900` and returns the reply text. Both callers then call `messaging.send_sms(biz, ..., reply)` with **no `lead_id`** argument. `messaging.py:77` signature is `send_sms(business, to, body, lead_id=None, ...)`. Without `lead_id`, `send_sms` does not write a second `"out"` row (`messaging.py:97-103`, `messaging.py:153-156`). Tests that assert "greeted exactly once" will hold.

### Single-party voicemail (no dual live-call recording)
The `<Dial>` TwiML at `app.py:2818-2820` has no `record=` attribute. The `<Record>` at `app.py:2849` fires only in the `twilio_voice_dial_status` handler — i.e., **after the dial leg has ended** (the contractor already hung up or let it ring out). This is a standard caller-leaving-a-message voicemail, not a recorded live conversation. TCPA single-party consent holds; no all-party-consent jurisdiction risk from the <Dial> leg.

### TCPA consent in widget.js
`static/widget.js:61`: `"By submitting, you agree to receive texts from <span class=\"fb-w-biz\">us</span>. Reply STOP to opt out."` — displayed before submit, business name filled in from config.js. STOP opt-out language present.

### No SQL injection
`_biz_id_by_widget_slug` uses a parameterized query `WHERE micro_site_slug=? AND widget_enabled=1` with `(slug,)`. All other new DB calls (`create_lead`, `get_lead_by_phone`, `add_message`) are the existing parameterized functions. No f-string SQL in new code.

### CORS wildcard on mutating POST — acceptable
The `POST /webhooks/widget/lead` endpoint carries `Access-Control-Allow-Origin: *`. No auth cookie or session is set or read on this endpoint — it is entirely stateless from a browser-auth standpoint. A CSRF attack via CORS requires a cookie/credential to exploit; there is none here. The endpoint mutates the DB, but any party on the internet can already POST it without CORS — the wildcard adds nothing to the threat surface. This is the correct posture for a public embed.

### Info-leak in config.js
`app.py:2937`: `cfg = {"slug": slug, "biz": biz.get("name") or "", "endpoint": "/webhooks/widget/lead"}` — only the business display name (already public), the slug (the caller already knows it), and a hardcoded endpoint path are returned. No phone numbers, no `ai_instructions`, no email, no Twilio SIDs, no owner data.

### Migrations are ADD-only and idempotent
`db.py:887-891`: `voicemail_enabled INTEGER DEFAULT 0` and `widget_enabled INTEGER DEFAULT 0` added via the existing PRAGMA-guarded `ALTER TABLE` loop. `db.py:899-901`: `recording_url TEXT` added to `messages` with the same idempotency guard. All three are `ADD COLUMN` only — no column drops, type changes, or data backfills. Zero-downtime safe on SQLite.

### Settings toggles save correctly
`app.py:1252-1253`: both toggles are extracted from the form and passed to `db.update_reminder_prefs`. `db.py:2558`: both column names are in the `cols` whitelist inside `update_reminder_prefs`. The function builds `UPDATE businesses SET voicemail_enabled=?, widget_enabled=? WHERE id=?` with parameterized values — no injection risk.

### `@require_twilio_signature` on recording webhook
`app.py:2860`: `@require_twilio_signature` decorates `twilio_voice_recording`. The decorator at `app.py:92` reconstructs the public HTTPS URL (honouring `X-Forwarded-Proto`) and validates the Twilio signature; returns 403 on mismatch. An unauthenticated POST cannot inject fake voicemail transcripts.

### widget.js slug read and graceful degradation
`static/widget.js:16-18`: slug is read from the script's own `src` via regex on `document.currentScript` (with a `getElementsByTagName` fallback for async). If the slug is missing, the script returns immediately — no bubble renders. If `config.js` fails to load, `bizName` stays `"us"` and the bubble still functions. If the `fetch` to `/webhooks/widget/lead` fails, the button re-enables with "Try again" — no silent failure.

### Deposit-link and GBP intentionally absent
Confirmed: no `deposit_link`, `deposit_amount`, or GBP-related code appears in the diff. These are correctly deferred to NEEDS-OWNER per the plan.
