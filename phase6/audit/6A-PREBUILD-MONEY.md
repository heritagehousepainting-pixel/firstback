# Phase 6a Pre-Build Audit — Money + Multi-Tenant DB Surface
**Date:** 2026-06-18  
**Auditor:** read-only pass, no source files modified  
**Branch:** staging  
**Files examined:** billing.py, db.py, app.py, alerts.py, config.py, mail.py, test_billing.py, test_confirm_token.py, test_screening_ui.py  

---

## AUDIT METHODOLOGY

All line numbers independently verified against the actual files. The plan doc (`phase6/PLAN-HARDENING.md`) cites several line numbers that have **drifted** — every discrepancy is called out below.

---

## D-2 — Stripe Dual-Billing Silent Downgrade (billing.py)

### Function locations (PLAN vs ACTUAL)

| Function | Plan line | Actual line |
|---|---|---|
| `_price_to_plan` | ~56 | **52** (function def), fallback `return "starter"` on **line 56** ✓ confirmed |
| `_on_invoice_paid` | ~247–278 | **247–278** ✓ confirmed exactly |
| `_on_checkout_completed` | (no line cited) | **200** |

### Exact current code — `_price_to_plan` (lines 52–56)

```python
# Map Stripe Price ID → internal plan key (works for both monthly and annual IDs).
def _price_to_plan(price_id: str) -> str:
    for (plan, _interval), pid in PRICE_IDS.items():
        if pid and pid == price_id:
            return plan
    return "starter"  # safe fallback — MUST have a valid price id in practice
```

**Key detail:** `if pid and pid == price_id` — the `pid and` guard means empty-string PRICE_IDS values (unset env vars, which default to `""`) are skipped, so they never match. A live Stripe `price_id` that isn't in `PRICE_IDS` will iterate all 6 entries, find no match (all `""` or mismatched), and return `"starter"` silently.

### Exact current code — `_on_invoice_paid` (lines 247–278)

```python
def _on_invoice_paid(invoice_obj):
    business_id = _business_id_from_obj(invoice_obj)
    if not business_id:
        return

    sub_id   = invoice_obj.get("subscription")
    period_s = invoice_obj.get("period_start") or invoice_obj.get("lines", {}).get("data", [{}])[0].get("period", {}).get("start")
    period_e = invoice_obj.get("period_end")   or invoice_obj.get("lines", {}).get("data", [{}])[0].get("period", {}).get("end")

    plan = "starter"                                      # L258: pessimistic default
    lines = (invoice_obj.get("lines") or {}).get("data", [])
    for line in lines:
        price = (line.get("price") or {}).get("id", "")
        if price:
            plan = _price_to_plan(price)                  # L263: first price hit; if unrecognized → "starter"
            break
    # Also check subscription metadata (set at checkout).
    sub_meta_plan = (invoice_obj.get("subscription_details") or {}).get("metadata", {}).get("plan")
    if sub_meta_plan and sub_meta_plan in PLAN_GRANTS:
        plan = sub_meta_plan                              # L268: sub_meta_plan WINS if present + valid

    granted = PLAN_GRANTS.get(plan, PLAN_GRANTS["starter"])
    db.add_usage_grant(business_id, ...)
    db.update_billing(business_id, subscription_status="active", plan=plan)
```

### Plan resolution trace (the critical path)

1. `plan = "starter"` — pessimistic init (L258)
2. Loop invoice lines → call `_price_to_plan(price_id)`:
   - If `price_id` matches a configured `PRICE_IDS` entry → returns `"pro"` / `"crew"` / `"starter"` correctly
   - **If `price_id` is a real live Stripe price NOT in `PRICE_IDS`** (env var missing in Render) → returns `"starter"` silently
3. Then check `sub_meta_plan` from `subscription_details.metadata.plan`:
   - This is set at Stripe Checkout time via `subscription_data.metadata` (see `create_checkout_session` L92)
   - If present + valid → **overrides** the price lookup result
   - **This is the current partial safety net**: so long as Stripe preserves the subscription metadata, the sub_meta_plan path should save Pro/Crew customers from the downgrade
4. BUT: if `sub_meta_plan` is absent or empty (e.g., older subscriptions created before metadata was added, or Stripe strips it on a plan upgrade/downgrade), the `_price_to_plan` fallback is the ONLY resolution — and it silently returns `"starter"`

### (a) How PRICE_IDS is built — distinguishing unrecognized from legitimate starter

`PRICE_IDS` is built at module import time (lines 29–36), reading 6 env vars. Any unset env var → empty string `""`. At webhook time `PRICE_IDS` is frozen.

**To distinguish "unrecognized price_id" from "legitimate starter":**

```python
def _price_to_plan(price_id: str) -> str:
    for (plan, _interval), pid in PRICE_IDS.items():
        if pid and pid == price_id:
            return plan
    # At this point price_id didn't match any configured entry.
    # "Legitimate starter" would have matched PRICE_IDS[("starter","month")] or [("starter","year")].
    # Reaching here means either:
    #   (a) price_id is a real Stripe ID not in our PRICE_IDS (env mismatch) — DANGER
    #   (b) price_id is empty string "" — shouldn't reach here; caller filters `if price`
    return "starter"  # ← this is the silent-downgrade path
```

The fix is to emit a loud log/alert before returning `"starter"` when `price_id` is non-empty (a real ID that wasn't configured):

### (b) Alert mechanism available

`billing.py` currently imports only: `os`, `stripe`, `db`, `VOICE_PUBLIC_URL from config`.  
No `mail`, `alerts`, or `sys` is imported.

**`alerts.notify` signature** (alerts.py:228):
```python
def notify(business, kind, context):
```
- Requires a `business` dict (needs `id`, `alert_sms`, `alert_email`, etc.)
- `kind` must be in `ALERT_KINDS` tuple: `("lead", "booking", "urgent", "canceled", "sms_fail", "forwarding_lost", "roi_milestone", "vic_morning", "vic_stall", "screening_graduated", "growth_tray")`
- **There is no `"billing_error"` kind**. No platform-wide PLATFORM biz or OWNER_CELL env concept exists in billing.py.
- The closest kind for a billing system alert is `"urgent"` (rides the `alert_on_urgent` toggle).

**Platform/operator notification options:**
1. `mail.send_email(SEED_OWNER_EMAIL, subject, body)` — directly email the operator. `SEED_OWNER_EMAIL` is `config.SEED_OWNER_EMAIL` = `os.environ.get("FIRSTBACK_OWNER_EMAIL", "heritagehousepainting@gmail.com")`. Available if SMTP is configured.
2. `sys.stderr` — always works; captured by Render logs and any log drain.
3. `alerts.notify(biz, "urgent", context)` — notifies the affected tenant, NOT the operator. Useful as a secondary.

**Recommended approach:** `sys.stderr` (guaranteed) + `mail.send_email` to `SEED_OWNER_EMAIL` (best-effort). This avoids importing `alerts` (which imports `messaging` → heavier dependency chain) and correctly targets the platform operator, not just the affected tenant.

### (c) Exact minimal diff — D-2

**billing.py — change 1: add `sys` and `mail` imports at top (after existing imports)**

Current (lines 16–20):
```python
import os
import stripe

import db
from config import VOICE_PUBLIC_URL  # re-use the base URL pattern
```

New:
```python
import os
import sys
import stripe

import db
import mail
from config import VOICE_PUBLIC_URL, SEED_OWNER_EMAIL  # re-use the base URL pattern
```

**billing.py — change 2: replace `_price_to_plan` (lines 52–56)**

Current:
```python
def _price_to_plan(price_id: str) -> str:
    for (plan, _interval), pid in PRICE_IDS.items():
        if pid and pid == price_id:
            return plan
    return "starter"  # safe fallback — MUST have a valid price id in practice
```

New:
```python
def _price_to_plan(price_id: str) -> str:
    for (plan, _interval), pid in PRICE_IDS.items():
        if pid and pid == price_id:
            return plan
    # price_id did not match any configured PRICE_IDS entry.
    # If price_id is non-empty this means a REAL Stripe price ID is missing from env —
    # returning "starter" here would silently downgrade a paying Pro/Crew customer.
    if price_id:
        msg = (
            f"[firstback] BILLING WARNING: unrecognized Stripe price_id {price_id!r} "
            f"not found in PRICE_IDS — falling back to 'starter'. "
            f"Check STRIPE_PRICE_* env vars on Render. "
            f"Affected invoice will be granted starter-tier conversations."
        )
        print(msg, file=sys.stderr, flush=True)
        # Best-effort operator email alert (no-op if SMTP not configured).
        mail.send_email(
            SEED_OWNER_EMAIL,
            "BILLING WARNING: unrecognized Stripe price_id — FirstBack",
            msg,
        )
    return "starter"  # retain safe fallback; caller should investigate
```

**Rationale:** We still grant `starter` (safe — better than holding nothing and letting the customer think their account is broken). The operator is alerted loudly via stderr (always) and email (if SMTP configured). No change to legitimate cases: a real starter subscriber's `price_id` matches `PRICE_IDS[("starter","month")]` before reaching this path.

**Risk/gotcha:** `mail.send_email` is a synchronous SMTP call. On a webhook thread this could add ~20s latency if SMTP is slow. The comment in `mail.py` says "any send error is swallowed + logged" so it won't raise, but it could block the webhook response. **Mitigation:** wrap in `threading.Thread(target=mail.send_email, ..., daemon=True).start()` instead of calling directly. This is the same pattern `alerts.notify_async` uses.

**Better diff with async email:**

```python
    if price_id:
        msg = (
            f"[firstback] BILLING WARNING: unrecognized Stripe price_id {price_id!r} "
            f"not found in PRICE_IDS — falling back to 'starter'. "
            f"Check STRIPE_PRICE_* env vars on Render."
        )
        print(msg, file=sys.stderr, flush=True)
        import threading
        threading.Thread(
            target=mail.send_email,
            args=(SEED_OWNER_EMAIL,
                  "BILLING WARNING: unrecognized Stripe price_id — FirstBack",
                  msg),
            daemon=True,
        ).start()
```

---

## D-3 — `set_confirm_result` Tenant Scope (db.py)

### PLAN WRONG — ALREADY FIXED

**Plan claims:** `db.py:2813` — "missing `business_id` in WHERE clause"  
**Actual line:** `db.py:3188` (plan line number drifted by +375 lines)  
**Actual state:** THE FIX IS ALREADY IMPLEMENTED

### Exact current code (lines 3188–3199, actual)

```python
def set_confirm_result(token_id, result_json, business_id=None):
    """Store the executed result so a replayed tap returns it verbatim (no re-execution).
    Scoped by business_id (defense-in-depth) when the caller supplies it."""
    conn = get_conn()
    if business_id is None:
        conn.execute("UPDATE pending_confirms SET result_json=? WHERE token_id=?",
                     (result_json, token_id))
    else:
        conn.execute("UPDATE pending_confirms SET result_json=? WHERE token_id=? "
                     "AND business_id=?", (result_json, token_id, business_id))
    conn.commit()
    conn.close()
```

The function already accepts `business_id` and conditionally adds `AND business_id=?` to the WHERE clause.

### Call sites

**Only one call site exists** (the plan's claimed `app.py:959` is also drifted):

```
app.py:981    db.set_confirm_result(token_id, json.dumps(out), business_id=biz["id"])
```

`biz["id"]` is already passed. The `business_id` parameter is already used in production code.

### Verdict

**D-3 is fully implemented.** No code changes required. The plan's diagnosis was correct but was filed before the fix was applied (or the fix landed as part of a prior phase). The line number drift (2813 → 3188 = +375) is consistent with ~375 lines of new code added to db.py since the audit was written.

**What DOES remain:** There is no test that explicitly verifies the cross-tenant write isolation for `set_confirm_result`. The `test_confirm_token.py` test at line "8) Cross-tenant" tests that biz2 cannot *redeem* biz1's token (via `get_confirm_token` tenant scope), but does **not** directly test that `set_confirm_result` with a mismatched `business_id` is a no-op. Add a test (see Section 4 below).

---

## D-5 — `mark_call_engaged` Tenant Scope (db.py)

### PLAN WRONG — ALREADY FIXED

**Plan claims:** `db.py:1821` — "missing `AND business_id=?` in UPDATE"  
**Actual line:** `db.py:1889` (plan line number drifted by +68 lines)  
**Actual state:** THE FIX IS ALREADY IMPLEMENTED

### Exact current code (lines 1889–1900, actual)

```python
def mark_call_engaged(call_id, lead_id=None, business_id=None):
    """Flip a previously-screened call to engaged (the owner's dashboard override).
    Scoped by business_id (defense-in-depth) when the caller supplies it."""
    conn = get_conn()
    if business_id is None:
        conn.execute("UPDATE calls SET engaged=1, lead_id=COALESCE(?, lead_id) WHERE id=?",
                     (lead_id, call_id))
    else:
        conn.execute("UPDATE calls SET engaged=1, lead_id=COALESCE(?, lead_id) "
                     "WHERE id=? AND business_id=?", (lead_id, call_id, business_id))
    conn.commit()
    conn.close()
```

The function already accepts `business_id` and conditionally adds `AND business_id=?`.

### Call sites (ALL verified)

Both call sites in `app.py` already pass `business_id=biz["id"]`:

```
app.py:2150    db.mark_call_engaged(call_id, lead["id"], business_id=biz["id"])
               # in api_engage_screened_call; call is already tenant-scoped via db.get_call(call_id, biz["id"]) at L2136
               
app.py:2179    db.mark_call_engaged(call_id, lead["id"], business_id=biz["id"])
               # in api_rescue_screened_call; call is already tenant-scoped via db.get_call(call_id, biz["id"]) at L2163
```

No other call sites exist (confirmed by `grep -rn "mark_call_engaged" --include="*.py"`).

### Verdict

**D-5 is fully implemented.** No code changes required. Line number drifted from plan's 1821 to actual 1889 (+68 lines). Both call sites pass `business_id` correctly. The pre-existing `get_call(call_id, biz["id"])` tenant scope at the call sites means this is defense-in-depth as described in the plan.

**What DOES remain:** No test explicitly verifies that `mark_call_engaged` with a mismatched `business_id` is a no-op. `test_screening_ui.py` tests the happy-path HTTP endpoints but does not verify cross-tenant isolation at the DB layer. Add a test (see Section 4 below).

---

## SECTION 4 — Existing Test Coverage + Regression Tests to Add

### Coverage map

| Surface | Existing test files | Coverage level |
|---|---|---|
| `_price_to_plan` / `_on_invoice_paid` | `test_billing.py` | Tests correct plans (pro, starter, crew grant amounts) and idempotency. **Gap: no test for unrecognized price_id path.** |
| `set_confirm_result` / confirm tokens | `test_confirm_token.py` | Tests cross-tenant redeem isolation, expiry, idempotency. **Gap: no direct test of `set_confirm_result` with wrong `business_id`.** |
| `mark_call_engaged` | `test_screening_ui.py` (HTTP), `test_screening_graduation.py` | Tests HTTP endpoint rescues. **Gap: no test of `mark_call_engaged` with mismatched `business_id`.** |

### Regression tests to add

#### D-2 — add to `test_billing.py`

**Test name:** `"unrecognized price_id triggers stderr log and does not silently downgrade without warning"`

Approach:
1. Set `STRIPE_PRICE_PRO = ""` (or monkeypatch `billing.PRICE_IDS` to remove the pro entry)
2. Send an `invoice.paid` event with `price_id = "price_pro_LIVE_BUT_UNCONFIGURED"`
3. Capture `sys.stderr` output (via `io.StringIO` redirect or `unittest.mock.patch`)
4. Assert: (a) the grant was written as starter (safe fallback still executed), (b) stderr contains `"BILLING WARNING"` and the price_id string, (c) optionally assert `mail.send_email` was called with the operator email

**One-liner description:** "An invoice with a price_id absent from PRICE_IDS still grants starter conversations but emits a BILLING WARNING to stderr and calls mail.send_email to the operator."

#### D-3 — add to `test_confirm_token.py`

**Test name:** `"set_confirm_result with wrong business_id is a DB no-op"`

Approach:
1. Create a `pending_confirms` row for biz 1
2. Call `db.set_confirm_result(token_id, '{"ok":true}', business_id=2)` (wrong tenant)
3. Assert the row's `result_json` is still NULL (or unchanged)
4. Call `db.set_confirm_result(token_id, '{"ok":true}', business_id=1)` (correct tenant)
5. Assert the row's `result_json` is now set

**One-liner description:** "`set_confirm_result` with a mismatched `business_id` does not write to another tenant's token row."

#### D-5 — add to `test_screening_ui.py`

**Test name:** `"mark_call_engaged with wrong business_id is a DB no-op"`

Approach:
1. Insert a call row for biz 1 (`engaged=0`)
2. Call `db.mark_call_engaged(call_id, lead_id=None, business_id=2)` (wrong tenant)
3. Fetch the call row; assert `engaged` is still 0
4. Call `db.mark_call_engaged(call_id, lead_id=None, business_id=1)` (correct tenant)
5. Assert `engaged` is now 1

**One-liner description:** "`mark_call_engaged` with a mismatched `business_id` does not flip another tenant's call to `engaged=1`."

---

## SUMMARY TABLE

| Item | Plan line(s) | Actual line(s) | Status | Action required |
|---|---|---|---|---|
| D-2 `_price_to_plan` fallback | billing.py:56 | billing.py:52 (def), **56** (return) ✓ | **NOT YET FIXED** — the silent downgrade path is real | Add `sys` + `mail` imports, add detection block in `_price_to_plan` before `return "starter"` when `price_id` is non-empty. Use async thread for email. |
| D-2 `_on_invoice_paid` | billing.py:247–278 | billing.py:247–278 ✓ | Plan description accurate | No change to this function; only `_price_to_plan` needs the new detection |
| D-3 `set_confirm_result` | db.py:2813 | db.py:**3188** (+375 drift) | **ALREADY FIXED** — `business_id` param + conditional WHERE already in code; call site already passes `biz["id"]` | No code change. Add regression test in `test_confirm_token.py`. |
| D-5 `mark_call_engaged` | db.py:1821 | db.py:**1889** (+68 drift) | **ALREADY FIXED** — `business_id` param + conditional WHERE already in code; both call sites already pass `biz["id"]` | No code change. Add regression test in `test_screening_ui.py`. |

---

## LANDMINES / RISKS

1. **D-2 `sub_meta_plan` partial safety net:** The `subscription_details.metadata.plan` path (line 266–268) is the ONLY thing currently preventing the silent downgrade for most customers. If Stripe ever stops returning this metadata (e.g., on a plan change where the metadata isn't re-applied), or for subscriptions created without this metadata, the downgrade happens silently. Do NOT remove the `_price_to_plan` warning even after verifying all 6 Stripe price IDs are configured — the warning is future-proof defense.

2. **D-2 mail.send_email is synchronous:** Without the async thread wrapper, a slow or failed SMTP server could block the Stripe webhook response for up to 20 seconds. Stripe's webhook timeout is 30 seconds; this is cutting it close. **Use the async thread pattern.**

3. **D-3 and D-5 line number drift:** The plan's line numbers (2813 for `set_confirm_result`, 1821 for `mark_call_engaged`) are wrong by +375 and +68 respectively. Any automated tools or future PRs that reference these plan line numbers will point at wrong code. Update the plan doc with the real line numbers.

4. **No platform/operator ALERT_KIND exists:** `alerts.ALERT_KINDS` does not include a `"billing_error"` or `"system_error"` kind for operator-only notifications. The billing warning alert goes to the platform operator via `mail.send_email(SEED_OWNER_EMAIL, ...)` directly — not through the `alerts.notify` framework — because `alerts.notify` is tenant-scoped and requires a `business` dict. This is correct design but means the billing warning is not recorded in the `alerts` table. This is acceptable; stderr + operator email is the right channel for infrastructure errors.

5. **PRICE_IDS are module-level constants:** If `STRIPE_PRICE_*` env vars are set correctly on Render, `PRICE_IDS` will be populated correctly at boot and `_price_to_plan` will never hit the fallback path for normal invoices. The warning fires only on misconfiguration — it is safe to add without any runtime overhead in the happy path.
