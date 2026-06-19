# Phase 6b Pre-Build Audit — Alerts, Settings, Consent/Honesty
**Status:** READ-ONLY. No source files were modified.
**Ground-truth date:** 2026-06-19
**Files verified:** `alerts.py`, `db.py`, `app.py`, `templates/settings.html`, `reminders.py`, `messaging.py`, `test_growth_tray_sms.py`, `test_vic_proactive.py`, `test_alert_channel.py`, `test_ticker_health.py`

All line numbers below are verified against the actual files on disk. PLAN-INTEGRATION.md line references at §6 (`alerts.py:32-57`, `app.py:1127-1134`) are **accurate** — no drift on those seams.

---

## 1. New `daily_digest` Alert Kind

### 1a. Current code at each insertion site

**ALERT_KINDS — `alerts.py` lines 30-32 (current):**
```python
ALERT_KINDS = ("lead", "booking", "urgent", "canceled", "sms_fail", "forwarding_lost",
               "roi_milestone", "vic_morning", "vic_stall", "screening_graduated",
               "growth_tray")
```

**`_DAILY_DEDUPE_KINDS` — `alerts.py` line 38 (current):**
```python
_DAILY_DEDUPE_KINDS = ("vic_morning", "vic_stall", "growth_tray")
```

**`_TOGGLE_COL` — `alerts.py` lines 46-57 (current):**
```python
_TOGGLE_COL = {"lead": "alert_on_lead", "booking": "alert_on_booking",
               "urgent": "alert_on_urgent", "canceled": "alert_on_booking",
               "sms_fail": "alert_on_urgent", "forwarding_lost": "alert_on_urgent",
               "roi_milestone": "alert_on_roi_milestone",
               "vic_morning": "alert_on_lead", "vic_stall": "alert_on_lead",
               "screening_graduated": "alert_on_urgent",
               "growth_tray": "alert_on_lead"}
```

**`format_message` — end of function, `alerts.py` line 156 (current):**
```python
    return f"FirstBack alert ({kind})."
```

**`_subject` dict — `alerts.py` lines 159-170 (current):**
```python
def _subject(kind):
    return {"lead": "New lead -- FirstBack",
            ...
            "growth_tray": "Your morning growth tray -- FirstBack"}.get(kind, "FirstBack alert")
```

**`_dedupe_key` — `alerts.py` lines 179-201 (current):**
The function ends at line 201 with the fallback:
```python
    base = f"{kind}:{context.get('lead_id')}"
    return base + (f":{context.get('when')}" if kind in ("booking", "canceled") else "")
```

### 1b. Exact diffs

**Diff 1: Add `daily_digest` to `ALERT_KINDS` (alerts.py line 30-32)**
```diff
-ALERT_KINDS = ("lead", "booking", "urgent", "canceled", "sms_fail", "forwarding_lost",
-               "roi_milestone", "vic_morning", "vic_stall", "screening_graduated",
-               "growth_tray")
+ALERT_KINDS = ("lead", "booking", "urgent", "canceled", "sms_fail", "forwarding_lost",
+               "roi_milestone", "vic_morning", "vic_stall", "screening_graduated",
+               "growth_tray", "daily_digest")
```

**Diff 2: Add `daily_digest` to `_DAILY_DEDUPE_KINDS` (alerts.py line 38)**
```diff
-_DAILY_DEDUPE_KINDS = ("vic_morning", "vic_stall", "growth_tray")
+_DAILY_DEDUPE_KINDS = ("vic_morning", "vic_stall", "growth_tray", "daily_digest")
```

**Diff 3: Add `daily_digest` to `_TOGGLE_COL` (alerts.py after line 57)**
```diff
               # Growth tray digest (5d BETA): rides the lead-alert toggle -- no new column needed.
-              "growth_tray": "alert_on_lead"}
+              "growth_tray": "alert_on_lead",
+              # Unified 8am morning digest (Phase 6b): own toggle so Dave can kill the
+              # morning buzz without losing real-time lead/booking alerts.
+              "daily_digest": "alert_on_daily_digest"}
```

**Diff 4: Add `format_message` branch for `daily_digest` (alerts.py — insert before line 156 fallback)**

Insert before the final `return f"FirstBack alert ({kind})."` line (currently line 156):

```python
    if kind == "daily_digest":
        # Honest one-SMS morning summary:
        #   - Leads waiting (count + optional money). Never "tap to send" -- in-app owns that.
        #   - Held growth plays + GO/SKIP instruction (only when plays exist).
        #   - Top stall (only when present).
        # Labels estimated money. Caps at 320 chars.
        n_leads = context.get("n_leads", 0)
        money = (context.get("money") or "").strip()
        plays_count = context.get("plays_count", 0)
        plays_summary = (context.get("plays_summary") or "").strip()
        total_str = (context.get("total_str") or "").strip()
        is_estimated = context.get("is_estimated", False)
        top_stall_name = (context.get("top_stall_name") or "").strip()
        top_stall_hours = context.get("top_stall_hours", 0)
        local_day = (context.get("local_day") or "").strip()

        # Leads segment
        lead_word = "lead needs" if n_leads == 1 else "leads need"
        money_part = f", ~{money} on the table" if money else ""
        estimated_flag = " (est.)" if (money and is_estimated) else ""
        leads_seg = f"{n_leads} {lead_word} you{money_part}{estimated_flag}."

        # Growth plays segment (only when plays > 0)
        plays_seg = ""
        if plays_count > 0:
            s = "s" if plays_count != 1 else ""
            plays_short = f" {plays_summary}" if plays_summary else ""
            plays_seg = f" {plays_count} text{s} ready:{plays_short} Reply GO to send all, SKIP to hold."

        # Stall segment (only when present)
        stall_seg = ""
        if top_stall_name:
            try:
                stall_h = int(round(float(top_stall_hours)))
            except (TypeError, ValueError):
                stall_h = 0
            stall_seg = f" One stall: {top_stall_name} {stall_h}h."

        base = "Good morning." + f" {leads_seg}" + plays_seg + stall_seg

        # Cap at 320 chars; truncate plays_summary first, then stall, keeping leads segment.
        if len(base) > 320:
            # Try without plays_summary detail
            plays_seg_bare = f" {plays_count} text{'s' if plays_count != 1 else ''} ready. Reply GO to send all, SKIP to hold." if plays_count > 0 else ""
            base = "Good morning." + f" {leads_seg}" + plays_seg_bare + stall_seg
        if len(base) > 320:
            # Drop stall
            base = "Good morning." + f" {leads_seg}" + plays_seg_bare
        if len(base) > 320:
            base = base[:317] + "..."
        return base
```

**Required `ctx` keys for `daily_digest`:**

| Key | Type | Description |
|-----|------|-------------|
| `n_leads` | int | Count of leads needing attention |
| `money` | str | Money total string e.g. `"~$4,000"` (may be empty) |
| `plays_count` | int | Count of held growth plays (0 if none) |
| `plays_summary` | str | e.g. `"1) Maria (review), 2) Carlos (win-back)"` (may be empty) |
| `total_str` | str | Growth plays money e.g. `"~$2,000"` (presently unused in body — keep for future) |
| `is_estimated` | bool | True when money is estimated (avg_job_value unset) |
| `top_stall_name` | str | First name of top stalled lead (empty if none) |
| `top_stall_hours` | float/int | Hours since top stall last replied |
| `local_day` | str | YYYY-MM-DD for dedupe key |

**Diff 5: Add `_subject` entry for `daily_digest` (alerts.py lines 159-170)**
```diff
     return {"lead": "New lead -- FirstBack",
             "booking": "Estimate booked -- FirstBack",
             "urgent": "Urgent lead -- FirstBack",
             "canceled": "Estimate canceled -- FirstBack",
             "sms_fail": "SMS delivery failed -- FirstBack",
             "forwarding_lost": "Call forwarding issue -- FirstBack",
             "roi_milestone": "FirstBack paid for itself -- FirstBack",
             "vic_morning": "Your morning briefing -- FirstBack",
             "vic_stall": "Lead still waiting -- FirstBack",
             "screening_graduated": "Spam Shield is now active -- FirstBack",
-            "growth_tray": "Your morning growth tray -- FirstBack"}.get(kind, "FirstBack alert")
+            "growth_tray": "Your morning growth tray -- FirstBack",
+            "daily_digest": "Your morning summary -- FirstBack"}.get(kind, "FirstBack alert")
```

**Diff 6: Add `_dedupe_key` branch for `daily_digest` (alerts.py — insert before line 194 `if kind == "growth_tray":` block)**
```diff
+    if kind == "daily_digest":
+        # Day-stamped per business: one digest per local calendar day (26h window).
+        day = (context.get("local_day") or "").strip()
+        return f"daily_digest:{day}"
     if kind == "growth_tray":
```

### 1c. Hazards

- **`n_leads == 0` case:** If `scan_daily_digest` is called when there are held plays but no stalled leads, `n_leads` will be 0. The body will say "0 leads need you" — ugly. `scan_daily_digest` must guard: only fire if `n_leads > 0 OR plays_count > 0`. Consider suppressing the leads segment entirely when `n_leads == 0`.
- **`money` vs `total_str`:** The design conflates two money figures — leads pipeline money (`money`) and growth plays money (`total_str`). The combined body currently only shows `money` in the leads segment. Be explicit in `scan_daily_digest` about which figure populates each ctx key.
- **320-char budget:** With 4 stalled leads, 5 plays, and a long plays_summary, the full body can exceed 320 chars. The truncation cascade above handles it; test with a long `plays_summary`.

### 1d. Test to add
**File:** `test_alert_channel.py` or new `test_daily_digest.py`
```python
# One-liner: daily_digest format_message caps at 320, has GO, labels estimated money
body = alerts.format_message("daily_digest", {"n_leads": 3, "money": "~$6,000", "plays_count": 2, "plays_summary": "1) Maria (review), 2) Carlos (win-back)", "total_str": "~$4,000", "is_estimated": True, "top_stall_name": "Maria", "top_stall_hours": 26, "local_day": "2026-06-19"})
assert len(body) <= 320 and "GO" in body and "(est.)" in body and "Maria 26h" in body
```

---

## 2. New `alert_on_daily_digest` Column + Settings UI

### 2a. Migration pattern (how other `alert_on_*` columns are added)

**Pattern 1 — bundled ADD at init (db.py lines 584-590):**
```python
    for col, ddl in (("alert_email", "TEXT"), ("alert_sms", "TEXT"),
                     ("alert_on_lead", "INTEGER DEFAULT 1"),
                     ("alert_on_booking", "INTEGER DEFAULT 1"),
                     ("alert_on_urgent", "INTEGER DEFAULT 1")):
        if col not in biz_cols:
            c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")
```

**Pattern 2 — solo ALTER for a later phase (db.py lines 837-838):**
```python
    if "alert_on_roi_milestone" not in biz_cols:
        c.execute("ALTER TABLE businesses ADD COLUMN alert_on_roi_milestone INTEGER DEFAULT 1")
```
`alert_on_daily_digest` follows Pattern 2 (later addition). The `biz_cols` refresh at line 831 reads the current schema before the check; the same pattern applies here.

### 2b. Exact diff — `db.py` (insert after line 838)

```diff
     if "alert_on_roi_milestone" not in biz_cols:
         c.execute("ALTER TABLE businesses ADD COLUMN alert_on_roi_milestone INTEGER DEFAULT 1")
+    # Phase 6b: unified morning digest toggle. Default ON so existing tenants get the new
+    # single-digest behaviour without any opt-in. The 26h dedupe prevents double-sends.
+    if "alert_on_daily_digest" not in biz_cols:
+        c.execute("ALTER TABLE businesses ADD COLUMN alert_on_daily_digest INTEGER DEFAULT 1")
```

Note: `biz_cols` at line 831 is re-read before the roi_milestone block, so it already includes any columns added earlier in init(). The `alert_on_daily_digest` check at line 839 will see the fresh schema. No additional `biz_cols` refresh needed.

### 2c. Exact diff — `db.update_alert_prefs` (db.py lines 2382-2383)

Current:
```python
    cols = ["alert_email", "alert_sms", "alert_on_lead", "alert_on_booking",
            "alert_on_urgent"]
```

Diff:
```diff
-    cols = ["alert_email", "alert_sms", "alert_on_lead", "alert_on_booking",
-            "alert_on_urgent"]
+    cols = ["alert_email", "alert_sms", "alert_on_lead", "alert_on_booking",
+            "alert_on_urgent", "alert_on_daily_digest"]
```

**Note:** `alert_on_roi_milestone` is also missing from this list — a pre-existing gap. The settings UI doesn't render an roi_milestone toggle (no checkbox in the template), so writes to it go through assistant.py's `set_alerts` action only. Do NOT add it here unless you're adding the UI checkbox too. Keep scope tight.

### 2d. Exact diff — `app.py` settings POST handler (lines 1134-1140)

Current:
```python
        db.update_alert_prefs(biz["id"], {
            "alert_email": request.form.get("alert_email", "").strip(),
            "alert_sms": request.form.get("alert_sms", "").strip(),
            "alert_on_lead": 1 if request.form.get("alert_on_lead") else 0,
            "alert_on_booking": 1 if request.form.get("alert_on_booking") else 0,
            "alert_on_urgent": 1 if request.form.get("alert_on_urgent") else 0,
        })
```

Diff:
```diff
         db.update_alert_prefs(biz["id"], {
             "alert_email": request.form.get("alert_email", "").strip(),
             "alert_sms": request.form.get("alert_sms", "").strip(),
             "alert_on_lead": 1 if request.form.get("alert_on_lead") else 0,
             "alert_on_booking": 1 if request.form.get("alert_on_booking") else 0,
             "alert_on_urgent": 1 if request.form.get("alert_on_urgent") else 0,
+            "alert_on_daily_digest": 1 if request.form.get("alert_on_daily_digest") else 0,
         })
```

### 2e. Exact diff — `templates/settings.html` (after line 201)

Current (lines 198-201):
```html
    <div class="toggle-list">
      {{ alert_toggle('alert_on_lead', 'A new lead comes in', business.alert_on_lead) }}
      {{ alert_toggle('alert_on_booking', 'An estimate gets booked', business.alert_on_booking) }}
      {{ alert_toggle('alert_on_urgent', 'A lead is flagged urgent', business.alert_on_urgent) }}
    </div>
```

Diff:
```diff
     <div class="toggle-list">
       {{ alert_toggle('alert_on_lead', 'A new lead comes in', business.alert_on_lead) }}
       {{ alert_toggle('alert_on_booking', 'An estimate gets booked', business.alert_on_booking) }}
       {{ alert_toggle('alert_on_urgent', 'A lead is flagged urgent', business.alert_on_urgent) }}
+      {{ alert_toggle('alert_on_daily_digest', 'One morning digest (leads, held texts, top stall in one 8am SMS)', business.alert_on_daily_digest) }}
     </div>
```

### 2f. Default-ON for existing tenants — confirmed

Two-layer guarantee:
1. **DB column default:** `INTEGER DEFAULT 1` on the ALTER means SQLite sets the value to 1 for all existing rows at migration time.
2. **`_enabled_for` logic (`alerts.py` line 176):** `return True if val is None else bool(val)` — a NULL from a missed migration is also treated as enabled.
3. **Template macro (`settings.html` line 17):** `{{ ' checked' if (on is none or on) else '' }}` — NULL or missing renders the checkbox checked.

All three layers agree: existing tenants default ON without any opt-in.

### 2g. Signup defaults — needs one more edit

`app.py` lines 337-339 currently set `alert_on_lead`, `alert_on_booking`, `alert_on_urgent` to 1 on signup. Add:

```diff
             "alert_on_lead": 1,
             "alert_on_booking": 1,
             "alert_on_urgent": 1,
+            "alert_on_daily_digest": 1,
```

(Lines 334-340, inside the `db.update_alert_prefs(bid, {...})` call at signup.)

### 2h. Hazards

- `update_alert_prefs` has a whitelist of allowed `cols`. Forgetting to add `alert_on_daily_digest` to that list means the POST saves everything else but silently drops the daily_digest toggle. **This is the most likely mistake** — verify the list at `db.py:2382`.
- The `alert_on_roi_milestone` gap noted above is pre-existing; don't fix it here.

### 2i. Test to add
**File:** `test_alert_channel.py` — extend Test 2
```python
# After signup test: verify alert_on_daily_digest = 1 on new rows
check("new signup row has alert_on_daily_digest = 1",
      row is not None and row["alert_on_daily_digest"] == 1)
```
Also add a settings-POST test (standalone, using `client.post("/settings", data={...})`) verifying that omitting `alert_on_daily_digest` from the form sets it to 0 (unchecked checkbox behavior), and including it sets it to 1.

---

## 3. New `tick_stale` Ops Alert Kind

### 3a. Context from PLAN-HARDENING.md (verified)

Section 3A (line 136): "Add a `tick_stale` alert kind to `alerts.py` so Dave gets an SMS if the ticker has been stale >15 min and the cron is also down."

Tier 2 checklist item (line 197): "Add a `tick_stale` alert when `/health/ticker` sees >15 min stale and the cron is down."

`ticker_is_stale()` already exists at `reminders.py:807-818`. The `/health/ticker` endpoint at `app.py:2943-2955` already reads it. **There is no existing `tick_stale` alert kind** — this is purely additive.

### 3b. Exact diffs

**Diff 1: ALERT_KINDS (after daily_digest addition)**
```diff
-               "growth_tray", "daily_digest")
+               "growth_tray", "daily_digest", "tick_stale")
```

**Diff 2: `_TOGGLE_COL` — map to `alert_on_urgent`**

`tick_stale` is an operational alert (scheduler down = delayed texts/reminders = revenue risk). It belongs with `sms_fail` and `forwarding_lost` on the urgent toggle. No new DB column.

```diff
               "growth_tray": "alert_on_lead",
               "daily_digest": "alert_on_daily_digest",
+              # tick_stale: ops alert — scheduler hasn't run; rides the urgent toggle.
+              "tick_stale": "alert_on_urgent"}
```

**Diff 3: `format_message` branch (insert before the final fallback `return`)**
```python
    if kind == "tick_stale":
        gap_minutes = context.get("gap_minutes", 0)
        try:
            gap_minutes = int(round(float(gap_minutes)))
        except (TypeError, ValueError):
            gap_minutes = 0
        return (f"FirstBack's scheduler hasn't run in ~{gap_minutes}m "
                f"— texts and reminders may be delayed. "
                f"Check your Render cron or restart the service.")
```

**Diff 4: `_subject` entry**
```diff
             "daily_digest": "Your morning summary -- FirstBack",
+            "tick_stale": "Scheduler may be down -- FirstBack",
             }.get(kind, "FirstBack alert")
```

**Diff 5: `_dedupe_key` branch (insert before the daily_digest branch)**
```python
    if kind == "tick_stale":
        # Day-stamped: one alert per local calendar day per business.
        # A 26h window (same as other daily kinds) means at most one "scheduler down"
        # SMS per day even if the cron stays broken all day. This prevents an outage
        # from becoming an SMS storm (one per tick = every 60s = hundreds/day).
        day = (context.get("local_day") or "").strip()
        return f"tick_stale:{day}"
```

**Diff 6: Add to `_DAILY_DEDUPE_KINDS` (same window: 26h)**
```diff
-_DAILY_DEDUPE_KINDS = ("vic_morning", "vic_stall", "growth_tray", "daily_digest")
+_DAILY_DEDUPE_KINDS = ("vic_morning", "vic_stall", "growth_tray", "daily_digest", "tick_stale")
```

### 3c. Dedupe window decision

**Use `tick_stale:{local_day}` with the existing 26h `_DAILY_DEDUPE_SECONDS` window.**

Rationale:
- The external cron fires every 60s; a broken scheduler would trigger `ticker_is_stale()` on every pass.
- A daily-keyed 26h window caps it at one SMS per outage per day — acceptable ops noise.
- Alternative (coarse time bucket e.g. `tick_stale:{YYYY-MM-DD-HH}`) would allow 24 SMS/day during a sustained outage — worse.
- A per-business `local_day` key is consistent with `vic_morning`, `growth_tray`, `daily_digest`.

### 3d. Where to fire `tick_stale`

`tick_stale` should be fired from the `/health/ticker` endpoint or a new `scan_tick_stale()` called inside `tick_once` — but there's a bootstrap catch: `tick_once` IS the ticker, so if the ticker is stale, `tick_once` never runs, so `scan_tick_stale` inside it never fires.

**Correct approach:** fire from the `/tasks/run-due` endpoint handler or from the `/health/ticker` endpoint after detecting staleness. The external Render cron hits `/tasks/run-due` every 60s — if the in-process ticker is stale but the external cron is running, the cron can call `tick_once` AND trigger the stale alert. Or: fire it from a separate `/tasks/health-check` cron. **This call-site decision belongs to `reminders.py` Phase 6b builder** — the alerts.py surface is ready.

### 3e. Hazards

- **ctx keys needed:** `{"gap_minutes": int, "local_day": "YYYY-MM-DD"}`. The caller must compute these from `reminders.ticker_is_stale()` + `db.get_meta("last_tick_utc")`.
- **bootstrap problem noted above:** `tick_stale` alert cannot fire from inside `tick_once` (defeats the purpose). Must fire from an external trigger.
- **No new DB column needed:** rides `alert_on_urgent`, already exists everywhere.

### 3f. Test to add
**File:** `test_ticker_health.py` — extend
```python
# tick_stale format_message is honest and under 160 chars
import alerts
body = alerts.format_message("tick_stale", {"gap_minutes": 18, "local_day": "2026-06-19"})
assert "18m" in body and "scheduler" in body.lower() and len(body) <= 160
```

---

## 4. Consent / Honesty (Critical Gates)

### 4a. SMS recipient is always `business["alert_sms"]` — CONFIRMED

**Trace through `alerts.notify` (`alerts.py` lines 228-291):**
```
sms_to = (business.get("alert_sms") or "").strip()   # line 277
if sms_to:
    res = messaging.send_sms(business, sms_to, body, gate=False)   # line 280
```

The recipient is hardcoded to `business["alert_sms"]`. There is **no path to a customer phone number** in this function.

**`gate=False` meaning (`messaging.py` lines 77-149):**
- `gate=False` skips the A2P 10DLC customer-traffic check entirely.
- The from-number is `ALERT_FROM_NUMBER` (platform-wide) when set, falling back to the tenant's number — never a customer's number.
- From `messaging.py` line 280: "Owner-facing alert: goes to the contractor's OWN cell, not a consumer, so it's exempt from the A2P 10DLC customer-traffic gate."

**Confirmed:** `daily_digest` via `alerts.notify` → `alert_sms` (owner cell) only, `gate=False`. Zero path to customer phone. No change needed.

### 4b. GO-parser gating — CONFIRMED SAFE, folding does NOT break GO

**The GO parser (`app.py` lines 2717-2724):**
```python
owner_cell = messaging.to_e164((biz.get("alert_sms") or "").strip())
is_owner = bool(owner_cell and caller and messaging.to_e164(caller) == owner_cell)
if not owner_cell:
    is_owner = False
if is_owner:
    tray_cmd = _parse_tray_reply(body)
    if tray_cmd:
        return _handle_tray_reply(biz, tray_cmd)
```

**What gates GO:**
1. The SMS came from `biz["alert_sms"]` (the owner's cell) — checked via E.164 comparison.
2. `_parse_tray_reply(body)` returns `{"cmd": "go"}` when body == "GO".
3. `_handle_tray_reply` calls `db.release_growth_batch(biz["id"], approved_via="sms_go")`.

**What does NOT gate GO:**
- There is NO check that a `growth_tray` alert was ever sent.
- There is NO check that the held plays came from a `growth_tray` scan vs a `daily_digest` scan.
- `db.release_growth_batch` queries `list_held_messages(biz["id"])` directly — it looks at the `scheduled_messages` table's `status='held'` rows, independent of any alert kind.

**Verdict: FOLDING IS SAFE.** The new `daily_digest` says "Reply GO to send all" — the inbound parser will receive GO from the owner cell, gate passes, held plays are released. The only dependency is: plays must be held in `scheduled_messages`. The `scan_daily_digest` function will surface those same plays, so GO will work identically.

**One edge case to verify:** if `daily_digest` fires at 8am but the owner replies GO at 8:05am — the plays are still in `held` status because `daily_digest` only alerts, never auto-releases. The GO command then releases them. This is correct. Confirm `scan_daily_digest` does NOT call `db.release_growth_batch` itself (that would release without owner approval — TCPA violation).

### 4c. Retiring `vic_morning` + `growth_tray` morning sends — KIND RETIREMENT DECISION

**Current usages of `"vic_morning"` in non-test files:**
- `alerts.py:31` — ALERT_KINDS definition
- `alerts.py:38` — _DAILY_DEDUPE_KINDS
- `alerts.py:52` — _TOGGLE_COL
- `alerts.py:100` — format_message branch
- `alerts.py:167` — _subject
- `alerts.py:187` — _dedupe_key
- `reminders.py:517` — `alerts.notify(biz, "vic_morning", ctx)` in `scan_morning_briefing`

**Current usages of `"growth_tray"` in non-test files:**
- Same pattern in `alerts.py:31,38,57,132,170,194`
- `reminders.py:590` — `alerts.notify(biz, "growth_tray", ctx)` in `scan_growth_tray`

**Recommendation: KEEP BOTH KINDS DEFINED. Do NOT remove them.**

Reasons:
1. **The GO parser does NOT depend on these kinds** — safe either way. But the kinds appear in `ALERT_KINDS` which is validated at `alerts.notify` entry (`if kind not in ALERT_KINDS`). If they're removed and any code still calls `notify(biz, "vic_morning", ...)`, it silently returns `[]` instead of error — testable but silent.
2. **`test_vic_proactive.py` and `test_growth_tray_sms.py`** use these kinds extensively. Removing the kinds breaks those tests without removing the tests first.
3. **Afternoon `vic_stall` is KEEPING its kind** and still uses the `vic_stall` kind via `alerts.notify`. The stall kind is safe and untouched.
4. **`scan_morning_briefing` and `scan_growth_tray`** should be GATED (not deleted) in Phase 6b using a `FIRSTBACK_UNIFIED_DIGEST=1` env flag, so they can be re-enabled if the digest regresses. Keep the kind definitions for back-compat with existing `alerts` DB rows (the in-app feed shows historical `vic_morning` / `growth_tray` entries).
5. **No other feature depends on these kinds** beyond the scan functions — no route handler, no billing gate, no customer-facing path checks for them.

**Action:** In `tick_once` (`reminders.py` lines 776-788), gate the old scans:
```python
    # P1-2: morning digest — replaced by scan_daily_digest when unified digest is on.
    _unified = os.environ.get("FIRSTBACK_UNIFIED_DIGEST") == "1"
    if not _unified:
        try:
            scan_morning_briefing(now)
        except Exception as e:
            ...
    try:
        scan_growth_tray(now) if not _unified else None
    ...
    # scan_stall_nudges: afternoon-only when unified digest is on (digest covers morning stall)
    try:
        scan_stall_nudges(now)
    ...
```

`scan_stall_nudges` currently has **no hour gating** (`reminders.py:599-638` — no `if not (hour...)` guard). The plan says stall nudges move to afternoon-only after Phase 6b. Phase 6b builder must add a `if now_local.hour >= 12:` guard inside `scan_stall_nudges`, or suppress morning stalls inside `scan_daily_digest` (the latter is cleaner since the top stall is already surfaced in the digest).

### 4d. One-tap stays one-tap; no silent customer sends

**Verified:**
- `daily_digest` via `alerts.notify` → `business["alert_sms"]` only, `gate=False`. No customer phone, no A2P gate.
- The "Reply GO" in the digest releases held plays — those plays are then run through the normal `run_due_once` queue which applies the A2P gate (`gate=True`) and all compliance checks before any text reaches a customer.
- The in-app one-tap (`/growth/tray/release`) calls `db.release_growth_batch(biz["id"], approved_via="ui_tap")` — unchanged, still works.
- No change introduces a silent customer send.

---

## 5. Honesty of the Combined Copy

### 5a. format_message for `daily_digest` — honesty audit

The proposed copy above:
- `"Good morning. {n} {lead_word} you{money_part}{estimated_flag}. {plays_count} text(s) ready: ... Reply GO to send all, SKIP to hold. One stall: {name} {h}h."`

**Honesty checklist:**
- ❌ **Does NOT say "tap to send" for leads.** The leads segment says "3 leads need you, ~$6,000 on the table." It directs to no action for leads (in-app one-tap owns that). PASS.
- ❌ **Does NOT claim a text was sent to a customer.** Only "ready" plays are referenced. PASS.
- ✅ **Labels estimated money.** `is_estimated` → `"(est.)"` flag in body. PASS.
- ❌ **Does NOT say GO/SKIP about leads** — only about "texts ready" (the growth plays). A customer reading over the owner's shoulder can't use GO to trigger a text to themselves. PASS.
- ✅ **"One stall: Maria 26h"** — honest factual statement. Does not say "Maria is ready to book" or anything alarmist. PASS.

### 5b. Existing `format_message` honesty checks that must be preserved

The `vic_morning` branch (line 100-109) has this comment (preserved, not deleted):
```
# NEVER "tap to send" -- the in-app briefing chip is where the one-tap lives.
```

The `growth_tray` branch (line 132-155) already labels estimated money and never claims a text was sent.

Both branches are KEPT per item 4c recommendation — no honesty regression.

---

## 6. Test File Coverage Summary

| Test file | Covers |
|-----------|--------|
| `test_alert_channel.py` | SMS from-number (ALERT_FROM_NUMBER), signup alert defaults, gate=False path |
| `test_vic_proactive.py` | `vic_morning` dedupe, hour gating, "no tap to send", quiet briefing suppression |
| `test_growth_tray_sms.py` | `growth_tray` hour gating, dedupe, owner-cell-only (Test 4), body content including GO/SKIP |
| `test_growth_tray_ui.py` | Growth tray web UI (not alert kind directly) |
| `test_ticker_health.py` | `ticker_is_stale()`, heartbeat write |
| `test_vic_guard.py` | `vic_morning`/`vic_stall` toggle guard |

**Missing coverage for Phase 6b (tests to add):**

1. **GO-still-works regression** (`test_growth_tray_sms.py` or new `test_daily_digest.py`):
   ```python
   # After scan_daily_digest fires at 8am, owner replies GO → plays released
   # Assert: db.list_held_messages(bid) == [] after _handle_tray_reply(biz, {"cmd": "go"})
   # Assert: no customer phone received any text from scan_daily_digest itself
   check("GO after daily_digest releases held plays",
         len(db.list_held_messages(bid)) == 0)
   check("daily_digest itself sent zero customer texts",
         all(r[0] == OWNER_CELL for r in _CAPTURED_RECIPIENTS))
   ```

2. **Owner-cell-only assertion for `daily_digest`** (mirror of test_growth_tray_sms.py Test 4):
   ```python
   check("daily_digest: owner cell received SMS", OWNER in recipients)
   check("daily_digest: lead phone received ZERO", LEAD_PHONE not in recipients)
   ```

3. **`alert_on_daily_digest` signup default** (extend `test_alert_channel.py` Test 2):
   ```python
   check("new signup row has alert_on_daily_digest = 1",
         row is not None and row["alert_on_daily_digest"] == 1)
   ```

4. **`daily_digest` dedupe (26h window)** — verify second call same day does not fire.

5. **`tick_stale` format** — see §3f above.

---

## 7. Summary of Plan Line Number Drift

`PLAN-INTEGRATION.md` §6 cites:
- `alerts.py:32-57` — accurate (ALERT_KINDS + _TOGGLE_COL)
- `app.py:1127-1134` — accurate (settings route + update_alert_prefs call)
- `reminders.py:469-523` / `527-596` / `599-638` — accurate

No drift on Phase 6b seams. The Phase 6a audit found drift in `PLAN-HARDENING.md` (different surface) — not relevant here.

---

## 8. Landmine Index

| # | Landmine | Severity | Notes |
|---|----------|----------|-------|
| L1 | `update_alert_prefs` cols whitelist not updated | HIGH | Silent drop of `alert_on_daily_digest` writes. Most likely mistake. |
| L2 | `scan_daily_digest` calls `db.release_growth_batch` directly | CRITICAL/TCPA | Must NOT happen. Digest only alerts; GO inbound handler releases. |
| L3 | `scan_stall_nudges` has no morning hour gate | MEDIUM | Will fire alongside `daily_digest` at 8am on same tick. Add `if now_local.hour >= 12:` guard before Phase 6b ships. |
| L4 | `tick_stale` fired from inside `tick_once` | HIGH | Defeats the purpose — if ticker is stale, tick_once never runs. Must fire from external cron endpoint. |
| L5 | `n_leads == 0` body says "0 leads need you" | LOW-UX | Guard in `scan_daily_digest`: skip leads segment if 0; or suppress entire digest if neither leads nor plays. |
| L6 | Removing `vic_morning`/`growth_tray` from ALERT_KINDS | MEDIUM | Breaks `test_vic_proactive.py`, `test_growth_tray_sms.py`. Keep kinds, gate scans with `FIRSTBACK_UNIFIED_DIGEST=1`. |
