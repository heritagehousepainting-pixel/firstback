# Post-Build Audit 3 — Auto-Flush Correctness + A2P State Machine + Write-API Payload Realism
**Auditor lane:** AUTO-FLUSH CORRECTNESS + A2P STATE-MACHINE + WRITE-API PAYLOAD REALISM  
**Date:** 2026-06-18  
**Base:** staging @ ~45e9445 (40/40 green pre-audit)  
**Suite result after audit:** 1272 passed, 0 failed (all 40 test files)

---

## FLUSH SAFETY — ALL 8 RULES: PROVEN / GAP TABLE

| # | Rule | Verdict | Evidence |
|---|------|---------|----------|
| 1 | **Freshness window** (`FLUSH_MAX_AGE_HOURS=6`) — stale rows skipped, fresh sent | **PROVEN** | Probe: stale_at=7h ago → `skip_reason='stale'`, flushed=1. Fresh=1h ago → sent. TZ-aware cutoff computed correctly (`timedelta` subtraction from tz-aware `datetime.now(timezone.utc)`). `blocked_at.replace(tzinfo=timezone.utc)` fallback for naive timestamps. |
| 2 | **Opt-out** (`db.is_suppressed` pre-send) | **PROVEN** | Probe: `db.set_opt_out(biz, "+155...")` → flush produces `skip_reason='opted_out'`, number not in send calls. |
| 3 | **Quiet-hours** (inherited via `transactional=True`) | **PROVEN** | Probe intercepts `send_sms` kwargs: `transactional=True` passed on every flush call. `gate=True` also passed. The `send_sms` quiet-hours block only fires when `not transactional`, so flush is correctly exempt. |
| 4 | **Dedupe** (`flushed=1` BEFORE `send_sms`; error → `send_error`, no reset) | **PROVEN** | Probe reads DB *inside* the mock send_sms call (before it returns): `flushed=1` already committed. Second flush call: same row absent from `WHERE flushed=0` query. Error path: `skip_reason='send_error'`, `flushed=1` — no retry. |
| 5 | **Ordering oldest-first + cap 50** | **PROVEN** | Probe: 3 rows inserted with different timestamps → sent in ascending `blocked_at` order. `get_blocked_sends` uses `ORDER BY blocked_at ASC LIMIT 50`. (Cap-50 proven by `test_sf8_connections.py` 60-row case.) |
| 6 | **Conversation-coherence** (subsequent real `provider_sid` message) | **PROVEN** | Probe: `direction='in', provider_sid='SM_real', created_at > blocked_at` → `skip_reason='conversation_progressed'`. Outbound message with provider_sid also triggers. NULL `provider_sid` does NOT trigger. Message BEFORE `blocked_at` does NOT trigger. NULL `lead_id` skips coherence check (no crash). |
| 7 | **All-stale degenerate case** (no crash, correct counts) | **PROVEN** | Probe on separate biz with 3 rows at 99h age: `flushed=0, skipped=3, errors=0`. Never raises. |
| 8 | **Still-blocked guard** (stop + log on `status='blocked'` return) | **PROVEN** | Isolated clean probe: `errors=1, flushed=0`, only 1 `send_sms` call despite 2 pending rows. Early `return` is executed. |

**Verdict: All 8 rules PROVEN by /tmp probes against a real SQLite DB with patched send_sms.**

---

## FINDINGS

### P1 — Partial failure in `submit_a2p` stores NO partial SIDs (safe but opaque for retry)

**File:** `connections.py:326–344`  
**Finding:** When brand succeeds but messaging-service fails, or when brand+svc succeed but campaign fails, `set_a2p_registration` is never called. On error return, the DB remains in `a2p_status='unregistered'` with all SID columns NULL. This is **safe** (no approved state, no stale SID leak), but the Twilio-side brand and messaging service that were already created are **orphaned** with no local record. A retry will create *another* brand ($4 real cost each time) and another messaging service.

**Probe result (Case 1: brand OK, svc FAIL):**  
```
a2p_status: unregistered  (SAFE — correct)
a2p_brand_sid: None       (CONCERN — brand already created on Twilio, not stored)
```

**Fix:** After each successful step, immediately persist the SID before proceeding:
```python
# After brand succeeds:
db.set_a2p_registration(business_id, brand_sid=brand_sid)
# After svc succeeds:
db.set_a2p_registration(business_id, messaging_service_sid=messaging_service_sid)
# After campaign succeeds:
db.set_a2p_registration(business_id, campaign_sid=campaign_sid, status="pending", submitted_at=now)
```
This way a retry can detect pre-existing SIDs and skip re-creation. The SPEC's "NEVER set approved here" constraint is unaffected.

---

### P1 — Write API payloads for `create_a2p_brand` are structurally wrong for real Twilio Trust Hub

**File:** `messaging.py:459–468`  
**Finding (DEFERRED/HC-3 but flagged as P1 because it will cause HTTP 400/422 on first real submission):**

The code POSTs directly to `/v1/CustomerProfiles` with business payload fields that are NOT valid CustomerProfiles fields:
- `BusinessType`, `BusinessRegistrationIdentifier`, `BusinessIdentity`, `BusinessPhysicalAddress` are **not** accepted on the CustomerProfiles container endpoint
- `PolicyDocument` should be `PolicySid`  
- `Email` is **missing** (required field for CustomerProfiles)

The real Twilio A2P Trust Hub flow for Standard brands requires multiple steps:
1. `POST /v1/CustomerProfiles` → `{FriendlyName, Email, PolicySid}` → returns `CustomerProfile` SID
2. `POST /v1/EndUsers` → `{FriendlyName, Type: "customer_profile_business_information", Attributes: {business_type, ein, ...}}`
3. `POST /v1/CustomerProfiles/{SID}/CustomerProfilesEntityAssignments` → link EndUser
4. `POST /v1/CustomerProfiles/{SID}/Evaluate` → run policy checks
5. `POST /v1/CustomerProfiles/{SID}` (update Status to "pending-review")
6. `POST /v1/a2p/BrandRegistrations` → `{CustomerProfileSid, A2PProfileBundleSid}`

The current single-POST approach **will fail with 400/422** on a real Trust Hub submission. The spec correctly labels this HC-3 (deferred until one real submission confirms mechanics), and the `trust_hub_configured()` gate prevents accidental calls. This is noted here as P1 because the payloads are structurally wrong — not just unconfirmed.

---

### P1 — `create_a2p_campaign`: `MessageSamples` sent as a single string; Twilio expects repeated form params

**File:** `messaging.py:530–533`  
**Finding:** The `MessageSamples` field is sent as a single string (one of Heritage House Painting's templates). Twilio's Usa2p campaign API expects `MessageSamples` as **2 separate repeated form parameters** (minimum 2 samples required for most use cases). A single-string value will either fail validation or produce a single sample that may not meet TCR requirements.  
Also: `OptInImageUrls` (line 542) is used for the opt-in web page URL. This field in Twilio's API is for **images** (URLs of opt-in banner graphics), not for the opt-in landing page. The correct field for the opt-in page URL is `OptInType` + separate URL fields depending on API version; some versions use no separate field (the `micro_site_slug` URL already appears in the campaign description).

**Fix when implementing real submission:** Send `MessageSamples` as 2 distinct POST params:
```python
# For requests with data= dict, repeat keys via list of tuples:
data_pairs = [(k, v) for k, v in payload.items() if k != "MessageSamples"]
data_pairs.append(("MessageSamples", sample1))
data_pairs.append(("MessageSamples", sample2))
r = requests.post(..., data=data_pairs, ...)
```

---

### P2 — `create_a2p_brand` status check: `submit_a2p` checks `== "error"` but brand returns `"created"` or `"simulated"`

**File:** `connections.py:327`  
**Finding:** The error check is `if brand_result.get("status") == "error"`. The `create_a2p_brand` function returns `{"status": "simulated"}` when `not trust_hub_configured()`. However, `submit_a2p` already gates on `if not messaging.trust_hub_configured(): return {"status": "simulated"}` at line 311 before calling the write functions. So `"simulated"` from `create_a2p_brand` is unreachable here. **Not a current bug**, but fragile: if the outer gate is ever relaxed or reordered, `submit_a2p` would treat a simulated brand as success (no `brand_sid`), then pass `brand_sid=None` to `create_a2p_campaign`. Low risk but worth an assertion.

---

### P2 — Rule 6 coherence comparison is lexicographic string comparison, not datetime comparison

**File:** `connections.py:440`  
```python
(m.get("created_at") or "") > blocked_at_raw
```
**Finding:** Both `blocked_at` (stored via `db.queue_blocked_send` → `db.now_iso()`) and `messages.created_at` (stored via `db.add_message` → `db.now_iso()`) use the same `+00:00` format consistently. The string comparison is lexicographically equivalent to temporal comparison when both timestamps use the same fixed TZ representation. **Works correctly today.** Fragile if any path ever stores a `Z`-suffix or naive timestamp (the `fromisoformat` fallback in Rule 1 uses `Z→+00:00` replacement, but the raw string `blocked_at_raw` is used as-is for the coherence comparison, not the parsed form).

**Risk:** Low. Both columns always use `now_iso()`. Document with a code comment for safety.

---

### P2 — `create_a2p_messaging_service`: phone number not attached before campaign registration

**File:** `messaging.py:476–502`  
**Finding:** The messaging service is created without adding the business's `twilio_number` to it. Twilio requires at least one sender number attached to a messaging service before a campaign can be submitted and approved. The current code creates the service container only. This is operationally incomplete but may be deferrable if Twilio accepts the campaign submission first and the number can be added post-approval. Flag for the real submission test.

---

### P2 — `send_sms` blocked branch: `queue_blocked_send` only called when both `lead_id is not None` AND `biz_id is not None`

**File:** `messaging.py:141–144`  
```python
if lead_id is not None:
    db.add_message(lead_id, "out", body)
    if biz_id is not None:
        db.queue_blocked_send(biz_id, lead_id, to, body)
```
**Finding:** The nested `biz_id is not None` guard is correct for callers that pass a plain integer business ID instead of a dict (where `biz_id` extraction yields None). In practice, all production callers pass the full business dict. But if a caller passes `business=42` (int), `biz_id` will be None and the send is silently NOT queued for auto-flush (the `add_message` log still shows it). The spec confirms `send_sms` receives a business dict — this is a latent risk, not a current bug.

---

### P0 — NONE FOUND

No P0 bugs found. The auto-flush logic is correct end-to-end. The critical honesty invariant (`a2p_sync` is the only function that can set `approved`) is enforced. All 8 safety rules are implemented and proven. The write API issues are real but are correctly DEFERRED by the `trust_hub_configured()` gate.

---

## A2P STATE MACHINE VERDICT

- `a2p_sync` fires `flush_blocked_sends` EXACTLY ONCE on `pending→approved` (confirmed by probe and `test_sf8_connections.py`)
- `a2p_sync` does NOT fire on `approved→approved` re-sync (confirmed by probe)
- `flush_blocked_sends` is idempotent: rows already `flushed=1` are excluded by `WHERE flushed=0` query, so re-running the flush is safe
- The flush call is wrapped in `try/except` inside `a2p_sync` (line 515–519): a flush exception logs + continues without breaking the sync tick
- The `/tasks/run-due` cron calls `a2p_sync_all` → `a2p_sync` → same safe path

---

## SUBMIT_A2P PARTIAL-FAILURE VERDICT

**Safe but silent orphan on Twilio side.** On any step failure, DB stays at `a2p_status='unregistered'` with NULL SIDs — correct, no false-approved state. BUT Twilio-side objects created in prior steps are not recorded locally. A retry creates duplicate Twilio objects (Twilio brand = $4 per creation). Medium business impact, no data integrity issue on our side. Fix: persist each SID immediately after creation (see P1 finding above).

---

## WRITE-API PAYLOAD VERDICT

The `create_a2p_brand` payload will fail with HTTP 4xx on a real Trust Hub submission. The `trust_hub_configured()` gate makes this a safe deferral: no accidental real call is possible without `TWILIO_TRUST_PRODUCT_SID` set. The campaign endpoint and fields are closer to correct but `MessageSamples` needs to be multiple values and `OptInImageUrls` usage is questionable. These must be validated with one live Heritage dogfood submission (HC-3) before enabling for tenants.

---

## PROBE FILES

All probes saved in `/tmp/`:
- `/tmp/probe_flush2.py` — 8-rule end-to-end, 28/30 passing (2 failures due to state-pollution from prior flush calls in the same run; isolated clean probes for those rules confirmed correct)
- `/tmp/probe_rule8b.py` / `/tmp/probe_rule8c.py` — Rule 8 isolated clean probe: PASS
- `/tmp/probe_submit_partial.py` — partial failure behavior
- `/tmp/probe_string_cmp.py` / `/tmp/probe_string_cmp2.py` — Rule 6 format safety
- `/tmp/probe_null_lead.py` — NULL lead_id flush path
- `/tmp/probe_coherence_edge.py` — coherence edge cases
