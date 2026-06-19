# Batch D Security Audit — Plan 05 (Owner Notifications / Set-and-Forget)
**Audited:** 2026-06-19  
**Auditor:** be-audit lane (READ-ONLY — no source files modified)  
**Diff scope:** `alerts.py` + `reminders.py` uncommitted changes only  
**Red-team input:** `product-review/plan-audits/05-audit.md`

---

## Verdict

**SHIP-WITH-FIXES**

The TCPA separation is clean and the core quiet-hours gate is correctly sequenced. Two real issues must be resolved before shipping: (1) the `sms_held` row shares the same dedupe key as the in-app claim, meaning the 120-second event-alert dedupe window can suppress a legitimate resend of the same event if attempted within that window — acceptable in practice but the 26h window for stall/digest kinds would silently prevent an SMS ever going out the same day; and (2) the SSRF surface on the webhook is not mitigated and deserves a private-IP block or at minimum a URL-scheme allowlist before exposing it in Settings UI. Everything else — gate ordering, TCPA isolation, audit trail integrity, tick_stale fan-out, honesty about the flush — is correct.

---

## Findings

| Severity | File:line | Issue | Fix |
|---|---|---|---|
| **P1** | `alerts.py:395–399` | **`sms_held` row shares the event's dedupe key, which blocks a retry within the dedupe window.** For event kinds (`lead`, `booking`, `vic_stall`), the dedupe window is 120 seconds — negligible. But `vic_stall` is in `_DAILY_DEDUPE_KINDS` (26h window, line 39). If a `vic_stall` fires at 11pm (held), the next morning's `scan_stall_nudges` pass would find `alert_recent(bid, "vic_stall:{lead_id}:{day}", 26h)` = True (because the in-app row was written with that key), and the SMS will never go out that calendar day. The owner misses the stall nudge SMS entirely. The `sms_held` row is written under the same dedupe key at line 399, which reinforces the block but the root cause is the in-app claim at line 383. | The `sms_held` row's dedupe key must either use a distinct suffix (e.g. `held:{dedupe}`) or the 26h daily-dedupe for `vic_stall` must be reduced to a shorter resend window. Given the 8am digest already surfaces the top stall, the simplest fix is: accept that the SMS is not re-fired (the digest covers it) and update the `sms_held` row's `status` to `"held_quiet_no_resend"` to make the behavior honest to future readers. Do NOT write a separate `sms_held` row with a different key — that would allow a storm. |
| **P1** | `alerts.py:427–453` | **SSRF via owner-supplied webhook URL.** `_send_webhook` uses `urllib.request.urlopen` with an owner-controlled URL, making a server-side HTTP POST. In a single-tenant contractor SaaS the threat is real but reduced: the owner IS the attacker only if their account is compromised. However, if the Settings form is ever reachable by non-owner staff or if the URL field can be set via an API, this becomes a straightforward SSRF. The 5-second timeout and fire-and-forget design bound the denial-of-service risk, but do NOT prevent probing internal metadata endpoints (e.g. `http://169.254.169.254/`, `http://localhost:5432/`). No URL validation occurs at line 420–422. | Add a URL allowlist or private-IP block before calling `urlopen`. Minimum: enforce `https://` scheme and block RFC-1918 ranges + loopback. A 5-line helper is enough: parse with `urllib.parse.urlparse`, check scheme == `https`, resolve host, block `10.x`, `172.16–31.x`, `192.168.x`, `127.x`, `169.254.x`. Alternatively, document this as an accepted risk with a Settings-level note that the URL must be a public HTTPS endpoint (Slack/Teams/Zapier only ever are). |
| **P2** | `alerts.py:395–396` | **`q_start == q_end` disabled-window case is correct but relies on an implicit invariant.** The comment at line 390 says "q_start == q_end means no quiet hours (window is empty)." This is TRUE because neither branch of `in_quiet` fires when `q_start == q_end`: the `q_start > q_end` branch is false, and the `q_start < q_end` branch is also false. So `in_quiet = False` always when equal — correct behavior. However, `q_start == q_end == 0` (midnight to midnight) would also produce no quiet hours, which might surprise a user who tries to set "always quiet." Low risk, but the settings UI should prevent `q_start == q_end` or document it explicitly. | Document the invariant in a UI hint ("Setting the same start and end hour disables quiet hours"). No code change needed in `alerts.py`. |
| **P2** | `alerts.py:427–453` | **Webhook is synchronous inside `notify`, which is called from `notify_async`'s daemon thread (fine) but also from `scan_daily_digest` -> `alerts.notify` on the scheduler thread.** The 5-second timeout per business means N tenants with webhooks can delay the scheduler by 5N seconds per digest pass. At current scale (single tenant) this is a non-issue. At 20 tenants with slow webhooks, the 8am digest pass takes 100 seconds, risking the `scan_daily_digest` call being still running when the next tick fires. | Acceptable at current scale. For future scale: move webhook sends inside `notify_async` only (not the sync path), or spawn a per-business thread for the webhook inside `_send_webhook`. No immediate action required; add a TODO comment. |
| **P2** | `alerts.py:398–399` | **`sms_held` row writes the alert_sms phone number into `target` column** at the time the hold is recorded, not at the time of send. If the owner changes their phone number between 10pm and 8am, the held marker records the old number but (since there is no flush/resend path) there is no downstream harm — the morning digest comes through `notify` which re-reads `business["alert_sms"]` live. Purely an audit-trail stale-data note. | No fix needed; document that `sms_held.target` is a snapshot for audit only. |

---

## Verified-good

### Lane 1: TCPA / Customer Safety — CLEAN

The customer-facing TCPA quiet-hours backstop lives entirely in `messaging.py:120–134`:

```python
# messaging.py:120
if gate and not transactional:
    ...
    if tc_messaging.quiet_blocked(now_local, QUIET_START, QUIET_END, ...):
        return {"status": "deferred", "reason": "quiet_hours"}
```

Owner alerts call `messaging.send_sms(business, sms_to, body, gate=False)` at `alerts.py:407`. The `gate=False` argument makes the `if gate and not transactional:` condition at `messaging.py:120` evaluate to `False` unconditionally — the TCPA backstop is **never entered** on the owner-alert path. This was true before Batch D and remains unchanged after it.

The new owner quiet-hours gate is a **completely separate early-return** inserted at `alerts.py:391–400`, BEFORE `send_sms` is even called. It operates entirely within `alerts.notify()` and has zero interaction with `messaging.send_sms`'s gate logic.

Confirmed: `grep -n "gate=False" reminders.py` shows lines 566 and 704 — both pre-existing, both correct owner-cell paths. Zero new `gate=False` hits from Batch D. The diff adds no changes to `reminders.py` that touch the `gate` argument.

**The customer TCPA path is untouched. messaging.py:120–134 is unchanged.**

### Lane 2: Quiet-Hours Gate Correctness — CLEAN (with P1 caveat above)

- **In-app row always written first:** `db.add_alert(bid, kind, "inapp", ...)` at `alerts.py:383` runs inside `_dedupe_lock`, BEFORE the quiet-hours gate at line 391. `attempted.append(("inapp", "recorded"))` at line 384 also runs before the gate. The gate's early-return at line 400 returns `attempted` which already contains `("inapp", "recorded")`. Audit trail is always complete.

- **Gate insertion point:** correctly AFTER the in-app claim (line 384) and BEFORE SMS (line 403). The diff shows the gate was inserted at exactly the right place.

- **Wrap-midnight math (`q_start > q_end`):** `in_quiet = (22 > 7 and (local_h >= 22 or local_h < 7))`. At 23h: `True`. At 6h: `True`. At 7h: `False` (exclusive end, correct). At 21h: `False`. ✓

- **Same-day window (`q_start < q_end`):** `in_quiet = ... or (q_start < q_end and q_start <= local_h < q_end)`. E.g. `q_start=2, q_end=6`, hour=3: `2 <= 3 < 6 = True`. ✓

- **Disabled (`q_start == q_end`):** Both branches false → `in_quiet = False`. No quiet period. ✓

- **`_int_pref` handles `val=0` correctly:** The implementation (`alerts.py:59–69`) checks `if val is None: return default`, then `int(val)`. A stored `0` (midnight) returns `0`, not the default. This FIXES a subtle bug in the plan's proposed version (`int((business or {}).get(key) or default)`) which would have coerced `0` to `default`. Builder caught and fixed it. ✓

- **`_URGENT_BYPASS_KINDS`:** `{"urgent", "sms_fail", "forwarding_lost", "tick_stale"}`. All fire-alarm operational alerts — owner needs these immediately regardless of hour. `lead`, `booking`, `canceled`, `vic_stall`, `daily_digest`, `roi_milestone`, `screening_graduated`, `growth_tray`, `a2p_approved` are held. Correct set — no debate about a missed kind.

### Lane 3: Webhook — PARTIAL (P1 SSRF, P2 latency; see Findings)

- **Context sanitization prevents JSON crash:** `alerts.py:440–441` filters context to `(str, int, float, bool, type(None))` only before `json.dumps`. A dict value, bytes, or object in context cannot crash serialization. ✓

- **Webhook failure never breaks fan-out:** `_send_webhook` wraps everything in `try/except Exception` (line 433). The caller at line 422 checks `ok = _send_webhook(...)` — a failure returns `False`, `attempted` records `("webhook", "failed")`, and `notify` returns normally. The status is **honest**: `"failed"` not `"sent"`. ✓ (This is an improvement over the plan's spec, which recorded `"sent"` unconditionally.)

- **SSRF risk:** Confirmed present (see P1 finding). No private-IP block in place. Realistic risk bounded by single-tenant nature of the product; must be addressed before Settings UI ships.

- **5-second synchronous timeout in scheduler path:** `scan_daily_digest` → `alerts.notify` → `_send_webhook(timeout=5)`. At N=1 tenant, 5 seconds is fine. At scale, this compounds. See P2 finding.

### Lane 4: Honesty re: Held Alert Flush — CLEAN

The pre-build red-team (05-audit.md) flagged that `scan_daily_digest` does NOT flush `sms_held` rows. **The builder chose not to implement a flush**, which is an honest and correct decision: the morning `daily_digest` independently surfaces overnight leads, held plays, and top stalls via its own DB queries. The `sms_held` row is an audit trail entry, not a queue item.

There is no UI copy, no notification text, and no code comment that says "we'll re-text you" or "held until morning resend." The in-app row (`channel="inapp", status="recorded"`) means the owner sees the event in their dashboard feed immediately. The comment at `alerts.py:387–400` says explicitly: "the 8am daily digest summarizes overnight leads." This matches reality. **No false promise is being made.** ✓

The `sms_held` row at line 398 records `status="held_quiet"` — an honest description. The `target` column records the owner's phone at hold time (audit purposes only). No resend mechanism exists and none is promised.

### Lane 5: tick_stale Fan-out Dedupe Storm — CLEAN

The fix at `reminders.py` loops `for _biz in db.list_businesses()` and calls `alerts.notify(_biz, "tick_stale", {...})`. The dedupe key is `tick_stale:{day}` (from `alerts.py:307–309`), and `db.alert_recent` always filters by `business_id` (confirmed at `db.py:2466`). Each business has its own independent 26h dedupe window. N tenants all firing at once generates exactly N alerts (one per business), not N² — and subsequent ticker passes within the same calendar day are collapsed by the dedupe. **No storm possible.** ✓

The `tick_stale` kind is also in `_URGENT_BYPASS_KINDS` — it bypasses quiet hours (correct: a scheduler outage is fire-alarm level). The `_enabled_for` check still applies (the owner can disable urgent alerts), which is the correct behavior. The bypass means "ignore quiet hours," not "ignore the toggle."

---

## Summary Table

| Lane | Status | Severity |
|---|---|---|
| TCPA / customer safety (gate=False preserved, messaging.py untouched) | PASS | — |
| Quiet-hours gate ordering (in-app before gate, correct math) | PASS | — |
| `_int_pref` val=0 fix (midnight hour handled correctly) | PASS (better than plan) | — |
| `_URGENT_BYPASS_KINDS` coverage | PASS | — |
| `sms_held` dedupe key collision (26h daily kinds) | FAIL | P1 |
| SSRF on owner-supplied webhook URL | FAIL | P1 |
| `q_start == q_end` disabled-window documented but untested in UI | NOTE | P2 |
| Webhook synchronous in scheduler path (5s × N tenants) | NOTE | P2 |
| Webhook honest sent/failed status (improvement on plan) | PASS | — |
| Webhook failure never breaks fan-out | PASS | — |
| Context sanitization prevents JSON crash | PASS | — |
| Held alert flush — no false promise, no phantom resend | PASS | — |
| tick_stale fan-out per-business dedupe | PASS | — |
