# Phase 5a + 5b Security Audit — 5AB-AUDIT-SECURITY.md

**Date:** 2026-06-18
**Branch:** staging @ e21c2d2
**Auditor:** Claude Sonnet 4.6 (read-only; no source files modified)
**Scope:** Phase 5a (SF-6 server-bound confirm token) + Phase 5b (Vic proactive push engine)
**Lens:** security / auth / tenant-isolation / PII

---

## Test Suite

All **49 tests** green (un-stubbed HTTP via Flask test client, real SQLite, real send_sms path):

```
ok test_confirm_token.py
ok test_vic_guard.py
ok test_vic_proactive.py
ok test_vic_surface.py
... (49/49)
```

---

## Findings

### P2 — Defense-in-Depth Gap

**Finding 1 — `set_confirm_result` missing `business_id` in WHERE clause**

- **File:Line:** `db.py:2813`
- **Code:** `conn.execute("UPDATE pending_confirms SET result_json=? WHERE token_id=?", (result_json, token_id))`
- **Risk:** The `set_confirm_result` function writes to a row keyed only by `token_id` (no `AND business_id=?`). Exploitability is nil from an external HTTP path because the only caller (`app.py:959`) reaches this line only after `claim_confirm_token(biz["id"], token_id)` returns `True` — a DB-level atomic `UPDATE WHERE consumed=0 AND business_id=?`. The race cannot be won by a different tenant because `claim_confirm_token` is already scoped. However, as a defense-in-depth measure, a future refactor that calls `set_confirm_result` from a different context could inadvertently write to another tenant's token row.
- **Severity:** P2 (no practical exploit today; defense-in-depth gap only)
- **Suggested fix:** Add `AND business_id=?` to the UPDATE:
  ```python
  def set_confirm_result(token_id, business_id, result_json):
      conn.execute("UPDATE pending_confirms SET result_json=? WHERE token_id=? AND business_id=?",
                   (result_json, token_id, business_id))
  ```
  Update the single call site in `app.py:959` to pass `biz["id"]`.

---

**Finding 2 — No max-length cap on the editable `text_lead` message body**

- **File:Line:** `app.py:955–957`
- **Code:**
  ```python
  edited = (request.form.get("message") or "").strip()
  if edited:
      args["message"] = edited
  ```
- **Risk:** No length limit on the POST `message` field. Flask's default `MAX_CONTENT_LENGTH` is 16 MB unless overridden (not set in this codebase). An authenticated owner session could submit a very large body. Twilio's own 1600-char SMS limit means it would fail at delivery, but it wastes server resources and stores a large string in `args_json` / `result_json` in `pending_confirms`. This is self-harm (owner's own session) but should be capped for hygiene.
- **Severity:** P2 (authenticated-only; self-harm; no cross-tenant risk)
- **Suggested fix:** Trim or reject messages over 1600 chars at `app.py:assistant_confirm`. Alternatively, set `app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024` (1 MB) globally.

---

## Verified-Safe Properties (actively confirmed)

| Property | Verdict | How Verified |
|---|---|---|
| **Cross-tenant token isolation** | PASS | `get_confirm_token` and `claim_confirm_token` both scope `WHERE token_id=? AND business_id=?`. Throwaway probe (`/tmp/probe_cross_tenant2.py`) confirmed biz B cannot read or claim biz A's token. Existing `test_confirm_token.py` test 8 also exercises this via HTTP. |
| **Idempotency / no double-execute** | PASS | `claim_confirm_token` uses `UPDATE WHERE consumed=0` atomic test-and-set. Second call returns `rowcount=0`. Probe (`/tmp/probe_replay.py`) confirmed. `test_confirm_token.py` test 3 confirms no double-send at HTTP level. |
| **Expiry check** | PASS | `app.py:929` checks `expires_at < time.time()` before proceeding. Probe (`/tmp/probe_expiry.py`) confirmed TTL=-1 yields an already-expired row. `test_confirm_token.py` test 6 confirms no execution on expired token. |
| **Missing/unknown token path** | PASS | `get_confirm_token` returns `None` → 200 honest reply, no execution. `test_confirm_token.py` tests 7 covers both unknown token (200) and empty token (400). |
| **CSRF protection** | PASS | `_csrf_ok()` uses `secrets.compare_digest` against the session-bound token. Called as the first check in `assistant_confirm`. |
| **Enforce-gate token survival** | PASS | The gate (`app.py:935–939`) returns before `claim_confirm_token` — token stays `consumed=0`. `test_vic_surface.py` test 2 verifies `consumed==0` after first tap, `consumed==1` after second tap with `enforce_ack=true`. |
| **Enforce gate not bypassable to set enforce without consent** | PASS (UX-level) | The gate is a friction mechanism, not a security lock — by design, a determined owner can send `enforce_ack=true` directly (they own the session). The spec explicitly says "two taps" is UX protection against accident, not a security boundary. Case mismatch (`"True"` vs `"true"`) is conservative: wrong case triggers the warning (does NOT bypass). |
| **Stored args are authoritative for recipient** | PASS | `app.py` never reads `tool` or `args` from the POST body — only `confirm_token`. Client can only override the `text_lead` message body (not recipient, tool, or booking target). `test_confirm_token.py` test 4 confirms a POST with injected `args` is ignored. |
| **text_lead body override stays server-bound on recipient** | PASS | `app.py:954–957`: override applies `args["message"] = edited` but tool and `_lead_id` (recipient) remain from the stored row. Test 5 in `test_confirm_token.py` verifies edited body reaches the stored recipient, not any client-forged one. |
| **Opportunistic purge safety** | PASS | `issue_confirm_token` deletes `WHERE expires_at < time.time() - 3600`. A live token expires in the future (expires_at > now) which is always > (now - 3600). No live token can be purged by this clause. |
| **Proactive sends go to owner cell only** | PASS | `alerts.notify` sends to `business["alert_sms"]` (owner's own cell). `scan_morning_briefing` and `scan_stall_nudges` both call `alerts.notify(biz, ...)` — neither passes a consumer phone as the destination. `test_vic_proactive.py` asserts `recipient == owner cell; zero sends to any consumer number`. |
| **No raw phone in audit log** | PASS | `app.py:961–962`: `add_audit(biz["id"], f"confirm:{tool}", f"token={token_id[:8]} {str(args.get('message') or '')[:100]}")` — logs only the first 8 chars of the token ID and up to 100 chars of the message body. No phone number, no EIN. |
| **No PII in error log output** | PASS | `reminders.py:473` error log contains only numeric `lead.get('id')` and the exception string. No phone or name. |
| **SQL parameterization** | PASS | All new DB functions in Phase 5a/5b use `?` placeholders for all values. f-strings are used only to build column-name lists from server-defined constants (not user input). No string interpolation of user-supplied values into SQL. |
| **Token entropy** | PASS | `secrets.token_hex(16)` = 32 hex characters = 128 bits of entropy. Brute-force is computationally infeasible. |
| **Login-required gate** | PASS | `assistant_confirm` is decorated with `@login_required`. No unauthenticated path to the endpoint. |

---

## Functional Note (not a security issue)

**`warm_leads_idle` stage filter:** `db.py:2475` uses `l.status != 'booked'` rather than matching a stored `stage='warm'` column. The spec says "warm leads (stage=='warm')," but `stage` is a derived attribute (not a DB column). The query correctly uses the inbound-message existence proxy. The effect is that leads that have replied but are technically still in a "new" UI stage may also receive stall nudges — potentially over-notifying the owner. This is a functional accuracy concern (too many nudges, not too few). No security or tenant-isolation impact.

---

## Summary

**P0 findings: 0**
**P1 findings: 0**
**P2 findings: 2** (set_confirm_result missing business_id in WHERE; no message body length cap)

The Phase 5a + 5b implementation is **clean from a security standpoint**. The two P2 items are defense-in-depth improvements, not exploitable vulnerabilities. The most important structural property — cross-tenant token isolation through all read/claim/execute paths — is robustly enforced and well-tested.
