# 6B Pre-Build Audit: Ticker / Proactive Engine
**Surface:** `reminders.py` · `assistant.py` · `alerts.py` · `llm.py`  
**Auditor role:** READ-ONLY  
**Date:** 2026-06-19  
**Branch:** staging (56/56 tests green)

---

## Ground-truth summary (before the design items)

The PLAN-INTEGRATION.md line-number citations for `tick_once` reference line 743. The **real current line is 743** — confirmed correct. All other cited line numbers were verified below against the actual file.

---

## Item W2 — Unified 8am Daily Digest

### A. `_compose_briefing` / `assistant.briefing` dict shape — CONFIRMED

`assistant._compose_briefing` (lines 454–542) is **100% DB-only**. It calls `db.leads_with_stage`, `db.list_appointments`, and (via `_needs_chaperone`) `connections.golive_summary` and `google_cal.is_connected`. No `llm.complete` call, no Anthropic SDK call anywhere in this path. The dict returned is:

```python
{
    "type": "briefing",      # always "briefing"
    "tone": "quiet" | "active",
    "headline": str,         # e.g. "4 leads open, about $10,000 on the table."
    "sub": str,              # e.g. "2 estimates booked."
    "items": [               # list of action items, may be []
        {
            "title": str,
            "sub": str,
            "tone": "warn" | "hot" | "ok" | "new",
            "label": str,
            "action": str,
        },
        ...
    ]
}
```

The `money` substring in `headline` is a `f"${val * len(open_leads):,}"` string (e.g. `$10,000`). The `n` (lead count) is `len(open_leads)`. The digest can extract these directly from the `headline` string or from the raw counts.

**For `scan_daily_digest`, use the counts directly — do NOT parse the headline string.** Use:
- `n = len(open_leads)` — but the function only returns the card, not the raw lists. Use `db.leads_with_stage(bid)` + filter in the digest scanner, OR read `card.get("items")` length. The **cleanest approach**: call `assistant.briefing(biz)` to get the card, then extract `card.get("headline")` and `len(card.get("items", []))` for display, BUT also make a separate `db.warm_leads_idle(biz["id"], 24)` call for the stall line. Do NOT re-parse the headline for a number — call `db.leads_with_stage(biz["id"])` directly for `n` and `money`.

`assistant.briefing` public entry point is line 545–547:
```python
def briefing(business):
    """Public: the briefing card dict for the dashboard route to server-render on load."""
    return _compose_briefing(business)
```

### B. `scan_growth_tray` data calls and return shapes — CONFIRMED

**`db.list_held_messages(business_id)`** (db.py line 2648–2657):
```python
# returns: list of dicts
[
    {
        # all scheduled_messages columns, plus:
        "lead_name": str,    # from JOIN leads
        "lead_phone": str,
        "kind": str,         # e.g. "review_request", "winback"
        "id": int,
        "business_id": int,
        "lead_id": int,
        "body": str,
        "send_at": str,
        "status": "held",
        ...
    },
    ...
]
```

**`growth._job_value(biz)`** (growth.py lines 75–88): Returns an int, never 0. Uses `biz["avg_job_value"]` when set; falls back to trade-keyword defaults (`paint`→2500, `roof`→8000, etc.); final fallback is 2000.

**`db.warm_leads_idle(business_id, hours)`** (db.py lines 2827–2858):
```python
# returns: list of dicts
[
    {
        "id": int,
        "name": str,
        "phone": str,
        "avg_job_value": int | None,
        "last_msg_at": str,       # ISO timestamp
        "idle_hours": float,      # computed (elapsed since last_msg_at)
    },
    ...
]
```

### C. The 8am window check — CONFIRMED pattern from `scan_growth_tray`

`scan_growth_tray` (reminders.py lines 527–596) uses:
```python
if not (8 <= now_local.hour < 9):
    continue
```
The new `scan_daily_digest` should use this EXACT pattern. The morning briefing used `7 <= now_local.hour < 10`; the design spec says 8am exactly — mirror `scan_growth_tray`'s `8 <= hour < 9`.

### D. Dedupe contract for `alerts.py` (other auditor's surface)

The digest needs `alerts.notify(biz, "daily_digest", ctx)` where `ctx` is:
```python
ctx = {
    "n": int,              # count of open leads needing action
    "money": str,          # e.g. "~$10,000" or ""
    "held_count": int,     # count of held growth plays
    "top_stall_name": str, # first name of most-urgent stalled lead, or ""
    "top_stall_h": float,  # idle hours of that lead, or 0
    "local_day": str,      # "YYYY-MM-DD" for dedupe key
}
```

The alerts auditor must add to `alerts.py`:
- `"daily_digest"` to `ALERT_KINDS` (line 30–32)
- `"daily_digest": "alert_on_lead"` to `_TOGGLE_COL` (line 46–57)
- `"daily_digest"` to `_DAILY_DEDUPE_KINDS` (line 38) — 26h window
- A `_dedupe_key` case: `f"daily_digest:{day}"` (same shape as `growth_tray`)
- A `format_message` case producing a compact body (cap 320 chars)

### E. Exact lines to REMOVE from `tick_once` and what to ADD

Current `tick_once` (reminders.py lines 775–788):
```python
    # P1-2: morning digest (proactive owner push).
    try:
        scan_morning_briefing(now)
    except Exception as e:
        print(f"[firstback] morning briefing tick failed: {e}", file=sys.stderr, flush=True)
    # 5d BETA B2: growth tray 8am digest (held plays awaiting owner approval).
    try:
        scan_growth_tray(now)
    except Exception as e:
        print(f"[firstback] growth tray scan failed: {e}", file=sys.stderr, flush=True)
```

**EXACT DIFF for `tick_once` (lines 775–788):**

```diff
-    # P1-2: morning digest (proactive owner push).
-    try:
-        scan_morning_briefing(now)
-    except Exception as e:
-        print(f"[firstback] morning briefing tick failed: {e}", file=sys.stderr, flush=True)
-    # 5d BETA B2: growth tray 8am digest (held plays awaiting owner approval).
-    try:
-        scan_growth_tray(now)
-    except Exception as e:
-        print(f"[firstback] growth tray scan failed: {e}", file=sys.stderr, flush=True)
+    # 6b W2: unified 8am daily digest (replaces vic_morning + growth_tray).
+    try:
+        scan_daily_digest(now)
+    except Exception as e:
+        print(f"[firstback] daily digest tick failed: {e}", file=sys.stderr, flush=True)
```

The `scan_stall_nudges` call at lines 785–788 (which becomes 779–782 after the removal) STAYS in `tick_once` — it is NOT removed. It is only modified to be afternoon-only (see Item below).

### F. `scan_daily_digest` — exact new function

Insert this function in `reminders.py` BEFORE `tick_once` (i.e., after `scan_stall_nudges` around line 638, before line 641 `scan_screening_graduation`). Exact insertion point: after line 638 (end of `scan_stall_nudges`), before line 641 (`def scan_screening_graduation`).

```python
def scan_daily_digest(now=None):
    """6b W2: Fire ONE unified 8am digest SMS per business per local day.
    Combines: (a) leads-need-you count + money from assistant.briefing,
    (b) held growth plays count + 'Reply GO to send all',
    (c) the single most-urgent stall from db.warm_leads_idle.
    Deduped via alerts.notify 'daily_digest' kind (day-stamped, 26h window).
    Goes to business['alert_sms'] (owner cell only), gate=False, A2P-exempt.
    Returns count of digests fired."""
    now = now or db.now_iso()
    fired = 0
    for biz in db.list_businesses():
        try:
            tz = _biz_tz(biz)
            try:
                now_local = datetime.fromisoformat(now).astimezone(tz)
            except (TypeError, ValueError):
                now_local = datetime.now(tz)
            # Only fire in the [8, 9) window -- mirrors scan_growth_tray exactly.
            if not (8 <= now_local.hour < 9):
                continue
            local_day = now_local.strftime("%Y-%m-%d")
            # (a) Leads-need-you from assistant.briefing (DB-only, no LLM).
            try:
                import assistant as _assistant
                card = _assistant.briefing(biz)
            except Exception:
                card = {}
            tone = card.get("tone", "")
            items = card.get("items") or []
            headline = (card.get("headline") or "").strip()
            # Extract lead count and money directly from headline.
            import re as _re
            n_match = _re.search(r"(\d+)\s+lead", headline, _re.I)
            n = int(n_match.group(1)) if n_match else 0
            money_match = _re.search(r"~?\$[\d,]+", headline)
            money = money_match.group(0) if money_match else ""
            # Only digest when there is actionable pipeline OR held plays.
            # (b) Held growth plays.
            try:
                held_rows = db.list_held_messages(biz["id"])
            except Exception:
                held_rows = []
            held_count = len(held_rows)
            # (c) Top stall: most-idle warm lead (first result, already sorted by idle_hours desc
            # via MAX(m.created_at) < cutoff ordering -- lowest last_msg_at = most idle).
            try:
                stalls = db.warm_leads_idle(biz["id"], 24)
                # Sort by idle_hours desc to get the most urgent.
                stalls.sort(key=lambda r: r.get("idle_hours", 0), reverse=True)
            except Exception:
                stalls = []
            top_stall = stalls[0] if stalls else None
            top_stall_name = ""
            top_stall_h = 0
            if top_stall:
                raw_name = (top_stall.get("name") or "").strip()
                top_stall_name = raw_name.split()[0] if raw_name else "a lead"
                top_stall_h = top_stall.get("idle_hours", 0)
            # Gate: only fire if there is something to report.
            if n == 0 and held_count == 0 and not top_stall_name:
                continue
            ctx = {
                "n": n,
                "money": money,
                "held_count": held_count,
                "top_stall_name": top_stall_name,
                "top_stall_h": top_stall_h,
                "local_day": local_day,
            }
            result = alerts.notify(biz, "daily_digest", ctx)
            if result:
                fired += 1
        except Exception as e:
            print(f"[firstback] daily digest scan failed (biz {biz.get('id')}): {e}",
                  file=sys.stderr, flush=True)
    return fired
```

### G. Hazards / Gotchas

1. **`warm_leads_idle` sort order**: `db.warm_leads_idle` returns rows in SQL `GROUP BY l.id` order — NOT sorted by idle_hours. The function above adds `stalls.sort(key=lambda r: r.get("idle_hours", 0), reverse=True)` to get the most urgent stall first. Do NOT assume the DB returns them in idle-time order.

2. **`assistant.briefing` depends on `connections` and `google_cal`**: `_needs_chaperone` inside `_compose_briefing` calls `connections.golive_summary` and `google_cal.is_connected`. These are cheap DB reads, but they ARE imported. In test environments that stub these modules, the briefing call will work only if those stubs are in place (they are in `test_ticker_health.py` and `test_vic_proactive.py`).

3. **`n=0` when tone is "quiet"**: When no leads exist, `briefing()` returns `tone="quiet"` and `headline="Nothing waiting yet."` — the regex for lead count returns 0. The gate `if n == 0 and held_count == 0 and not top_stall_name: continue` correctly suppresses the digest in this case.

4. **Two SMS at 8am for businesses that had both `vic_morning` and `growth_tray` active**: After the switch, those businesses will receive exactly 1 SMS. The old `growth_tray` dedupe key (`growth_tray:YYYY-MM-DD`) and old `vic_morning` key (`vic_morning:YYYY-MM-DD`) are different from the new `daily_digest:YYYY-MM-DD` key — so on the first day after deploy, there is NO residual dedupe conflict from old alerts rows.

### H. Test coverage — what breaks, what to add

**Tests that DIRECTLY call `scan_morning_briefing`:**
- `test_vic_proactive.py` lines 227, 235, 249, 259, 270: All call `reminders.scan_morning_briefing(...)` directly. These tests are NOT testing `tick_once` — they test the function directly. Since `scan_morning_briefing` is KEPT in reminders.py (just removed from `tick_once`), these tests **do not break**.

**Tests that call `scan_growth_tray`:**
- `test_growth_tray_sms.py` lines 117, 126, 135, 147, 166, 169, 188, 206, 234: All call `reminders.scan_growth_tray(...)` directly. Since `scan_growth_tray` is kept in the module, these tests **do not break**.

**Tests that call `tick_once`:**
- `test_ticker_health.py` line 50: Calls `reminders.tick_once()`. This test only asserts that `last_tick_utc` is written (lines 52–54) and that `/health/ticker` reports fresh. It does NOT assert that `scan_morning_briefing` or `scan_growth_tray` fire. **Safe — will not break.**

**New test to add** (suggest adding to `test_vic_proactive.py` or a new `test_daily_digest.py`):
```python
# One-liner to add to a new test file:
# verify scan_daily_digest fires at 8am with actionable data and dedupes
import reminders
now_8am = datetime(2026, 6, 19, 8, 0, tzinfo=timezone.utc).isoformat()
count1 = reminders.scan_daily_digest(now_8am)
count2 = reminders.scan_daily_digest(now_8am)  # second tick -> must be 0
assert count1 >= 0 and count2 == 0, "daily digest must dedupe on second tick same day"
```

---

## Item: Stall Nudges → Afternoon-Only

### Current code (reminders.py lines 599–638)

`scan_stall_nudges` has NO hour guard. It fires on every tick. The per-(lead, day) dedupe (via `alerts.notify` with `vic_stall:{lead_id}:{local_day}` key, 26h window) prevents re-firing the same lead twice in a day, but it CAN fire at any hour — including 7am, before or alongside the morning digest.

### Exact diff

Add the hour guard **after** computing `now_local` and `local_day` (after line 611), before the `idle_leads` call (line 614):

```diff
         local_day = now_local.strftime("%Y-%m-%d")
+        # 6b W2: afternoon-only -- skip morning so the daily digest's top-stall
+        # line isn't duplicated by per-lead stall SMS. Catches leads that go cold
+        # during the day (after 12pm local). The per-(lead,day) dedupe is unaffected.
+        if now_local.hour < 12:
+            continue
         # Find warm leads idle >24h (not urgent, replied, not booked).
         idle_leads = db.warm_leads_idle(biz["id"], 24)
```

**Exact location:** Between lines 612 and 614 of `reminders.py`. Line 612 is:
```python
            local_day = now_local.strftime("%Y-%m-%d")
```
Line 614 is:
```python
            idle_leads = db.warm_leads_idle(biz["id"], 24)
```

### Does this break the per-(lead,day) dedupe?

No. The dedupe is implemented in `alerts.notify` via `_dedupe_key("vic_stall", ctx)` which returns `f"vic_stall:{lead_id}:{day}"`, checked against `db.alert_recent(bid, dedupe, 26*3600)`. The hour guard is applied BEFORE `alerts.notify` is called — so if a stall fires at 2pm, its dedupe key is written, and any subsequent attempt at 3pm for the same (lead, day) is blocked by the existing key. The `26h` window covers the whole local day. This is unchanged.

### What breaks in tests

**`test_vic_proactive.py` lines 308, 314, 335, 419, 425**: All call `reminders.scan_stall_nudges(now3)` where `now3 = datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc).isoformat()` (hour=14, well past noon). **These tests are unaffected** — 14:00 UTC >= 12:00 local.

**Regression block (lines 419–425)**: Calls `reminders.scan_stall_nudges()` with no argument — defaults to `db.now_iso()` (current UTC time). If tests run before noon UTC, this could now return 0 and break the regression test. **This IS a hazard.**

Fix: In the regression block's `reminders.scan_stall_nudges()` call (lines 419 and 425), pass an explicit afternoon timestamp:
```diff
-reminders.scan_stall_nudges()   # 10 min later -> must STILL dedupe
+reminders.scan_stall_nudges(datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc).isoformat())
```
And the first call at line 419 should also be explicit:
```diff
-reminders.scan_stall_nudges()
+reminders.scan_stall_nudges(datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc).isoformat())
```

**New test to add** (`test_vic_proactive.py`):
```python
# Verify stall nudge is suppressed in morning (hour < 12)
now_morning = datetime(2026, 6, 19, 9, 0, tzinfo=timezone.utc).isoformat()
_before_morning = _alert_count(biz3["id"], "vic_stall")
reminders.scan_stall_nudges(now_morning)
check("stall nudge suppressed in morning (hour=9)", 
      _alert_count(biz3["id"], "vic_stall") == _before_morning)
```

---

## Item W3 — The Real Ticker LLM Call

### W3a: Is `assistant._compose_briefing` DB-only? — CONFIRMED YES

`_compose_briefing` (assistant.py lines 454–542) makes ZERO LLM calls. Every data access is:
- `db.leads_with_stage(bid)`
- `db.list_appointments(bid)`
- `connections.golive_summary(biz)` (DB reads only)
- `google_cal.is_connected(bid)` (DB read)
- `google_contacts.is_connected(bid)` (DB read)

No `llm.complete`, no `anthropic.Anthropic`, no `requests.post`. **The daily digest path that calls `assistant.briefing()` is fully LLM-free.**

The PLAN-INTEGRATION.md H2 concern ("scan_morning_briefing calls assistant.briefing — which can be an LLM call") was a false alarm for the briefing path. However, it was correctly identifying the risk for the broader `assistant.run()` path used in chat. The TICKER's specific path through `briefing()` is safe.

### W3b: The ACTUAL synchronous LLM call — `followup_body_contextual`

`reminders.followup_body_contextual` (lines 383–411), called from `scan_followups` (line 443), IS the real blocking LLM call in the ticker.

**Exact call chain:**
- `tick_once` (line 759) calls `scan_followups(now)`
- `scan_followups` (line 443) calls `followup_body_contextual(lead.get("name"), biz_name, last_in_text)`
- `followup_body_contextual` (line 403) calls `_llm.complete(provider, system, [...], max_tokens=80, temperature=0.7)`

**`llm.complete` signature (llm.py line 134):**
```python
def complete(provider, system, messages, *, max_tokens, temperature=0.8,
             model=None, return_usage=False):
```

**Current Claude SDK call (llm.py lines 164–178):**
```python
if provider == "claude":
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    _model = model or CLAUDE_MODEL
    system_block = [{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}]
    resp = client.messages.create(model=_model, max_tokens=max_tokens,
                                   system=system_block, messages=list(messages))
```

**PROBLEM:** `anthropic.Anthropic()` with no `timeout=` argument defaults to httpx's default connection timeout (5 seconds) and a read timeout of 600 seconds (10 minutes). A slow Sonnet response can block the ticker thread for up to 600 seconds, stalling `run_due_once` and all scheduled sends.

The ticker runs on a daemon thread (`threading.Thread(..., daemon=True)`), so `signal.alarm` is invalid (signals only reach the main thread on CPython). The fix MUST be a client-level timeout.

### W3c: Exact minimal fix — add `timeout=` to `llm.complete` + thread to SDK call

**Step 1: Add `timeout=None` param to `llm.complete` (llm.py line 134):**

```diff
-def complete(provider, system, messages, *, max_tokens, temperature=0.8,
-             model=None, return_usage=False):
+def complete(provider, system, messages, *, max_tokens, temperature=0.8,
+             model=None, return_usage=False, timeout=None):
```

**Step 2: Thread timeout into the SDK call (llm.py lines 164–173):**

```diff
     if provider == "claude":
         import anthropic
-        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
+        import httpx
+        _timeout = timeout if timeout is not None else 30
+        client = anthropic.Anthropic(
+            api_key=ANTHROPIC_API_KEY,
+            timeout=httpx.Timeout(_timeout, connect=5.0),
+        )
         _model = model or CLAUDE_MODEL
```

Anthropic's Python SDK accepts a `timeout=` parameter on the client constructor (it's passed through to httpx). `httpx.Timeout(total, connect=5.0)` sets a 5s connect timeout and a `total`-second total timeout. A `total=30` is appropriate for a short-body completion (`max_tokens=80`); Sonnet typically responds in 2–5s for this size.

**MiniMax path** already has `timeout=30` on its `requests.post` call (llm.py line 157) — no change needed.

**Step 3: Thread the timeout into `followup_body_contextual` (reminders.py line 403):**

```diff
-        text = _llm.complete(provider, system,
-                             [{"role": "user", "content": user_msg}],
-                             max_tokens=80, temperature=0.7)
+        text = _llm.complete(provider, system,
+                             [{"role": "user", "content": user_msg}],
+                             max_tokens=80, temperature=0.7, timeout=10)
```

`timeout=10` (10 seconds) for a `max_tokens=80` Sonnet call is generous. The fallback at lines 409–411 (`return followup_body(name, biz_name)`) already handles any exception, including `anthropic.APITimeoutError` / `httpx.TimeoutException`. No additional fallback logic is needed — the existing `except Exception: pass` (line 409) catches the timeout.

**Sane default for `complete`'s `timeout` param:** Keep `timeout=None` defaulting to 30s in the SDK client when caller doesn't specify. This ensures existing callers (the chat path in `assistant.py` which calls `llm.tool_complete` — not `llm.complete` — are unaffected). Only the ticker's `followup_body_contextual` needs the tighter 10s bound.

### W3d: Other callers of `llm.complete`

Search shows `llm.complete` is called from:
1. `reminders.followup_body_contextual` (line 403) — the ticker path. Fix: `timeout=10`.
2. `assistant.py` `_chat_or_route` and `_llm_complete` equivalents — these run on the REQUEST thread, not the ticker. They are unaffected by the ticker fix but would benefit from the timeout param being available.
3. `voice_service.py` uses `llm.complete_stream_voice` (a different function) — unaffected.

### W3e: Hazards

- `anthropic.Anthropic(timeout=...)` requires the `anthropic` SDK >= 0.20.0 (which accepts a `timeout` kwarg on the client). Confirm the installed version in `.venv`: `pip show anthropic`. If the SDK is older, use `anthropic.Anthropic(api_key=..., http_client=httpx.Client(timeout=...))` instead.
- `httpx` is already a dependency of `anthropic`; no new install needed.
- The MiniMax path (`requests.post(timeout=30)`) is unaffected by this change.

**New test to add** (`test_reminders.py`):
```python
# Verify followup_body_contextual falls back to generic on LLM timeout
import reminders
# Patch _llm.complete to raise a timeout-like exception
import llm as _llm_mod
_orig = _llm_mod.complete
_llm_mod.complete = lambda *a, **kw: (_ for _ in ()).throw(Exception("timeout"))
result = reminders.followup_body_contextual("Dave", "Acme Painting", "hello")
_llm_mod.complete = _orig
assert "Acme Painting" in result, "fallback must be the generic template"
```

---

## Item: Stale-Ticker Alert (Gap Detection)

### Current `tick_once` heartbeat code (reminders.py lines 747–753)

```python
def tick_once(now=None):
    now = now or db.now_iso()
    # Record the heartbeat FIRST so a partial failure still timestamps the tick.
    _tick_utc = datetime.now(timezone.utc).isoformat()
    try:
        db.set_meta("last_tick_utc", _tick_utc)
    except Exception as e:
        print(f"[firstback] heartbeat write failed: {e}", file=sys.stderr, flush=True)
```

### Exact diff to add gap detection

The read of the PREVIOUS heartbeat must happen BEFORE the write. Insert after line 747 (`now = now or db.now_iso()`), before line 749 (`_tick_utc = ...`):

```diff
 def tick_once(now=None):
     now = now or db.now_iso()
+    # 6b: read the PREVIOUS heartbeat before overwriting it (gap detection).
+    _prev_tick_utc = db.get_meta("last_tick_utc")
     # Record the heartbeat FIRST so a partial failure still timestamps the tick.
     _tick_utc = datetime.now(timezone.utc).isoformat()
     try:
         db.set_meta("last_tick_utc", _tick_utc)
     except Exception as e:
         print(f"[firstback] heartbeat write failed: {e}", file=sys.stderr, flush=True)
+    # Gap detection: if the previous tick was too long ago, fire a tick_stale alert.
+    # Uses 15 min (900s) as the threshold -- 2x the configured TICK_SECONDS (60s typical),
+    # generous enough to survive a Render dyno recycle but tight enough to catch a dead ticker.
+    _STALE_GAP_S = 900
+    try:
+        if _prev_tick_utc:
+            _prev_dt = datetime.fromisoformat(_prev_tick_utc)
+            if _prev_dt.tzinfo is None:
+                _prev_dt = _prev_dt.replace(tzinfo=timezone.utc)
+            _gap_s = (datetime.now(timezone.utc) - _prev_dt).total_seconds()
+            if _gap_s > _STALE_GAP_S:
+                _gap_min = round(_gap_s / 60, 1)
+                _ops_biz = db.get_business(1)
+                alerts.notify(_ops_biz, "tick_stale", {"gap_minutes": _gap_min})
+    except Exception as _ge:
+        print(f"[firstback] tick_stale gap check failed: {_ge}", file=sys.stderr, flush=True)
```

### Recipient for the `tick_stale` alert

**Recommendation: `db.get_business(1)` — the seed/operator business.**

Rationale:
- `db.get_business(business_id=1)` (db.py line 1000) is the default — Heritage House Painting, the seed business whose `alert_sms` is the operator's own cell.
- There is no `ALERT_FROM_NUMBER`-based platform channel in the current alerts architecture. `alerts.notify` sends to `business.get("alert_sms")`, and the only "global operator" cell in the system is the seed business's owner.
- A `tick_stale` alert is an operational event (the scheduler stalled) that the operator/owner (Heritage House) needs to know about. It is NOT a per-tenant customer-data event.
- If Heritage House's `alert_sms` is set, the alert reaches the operator's cell. If not set, the alert falls through to their `alert_email` (the login email `heritagehousepainting@gmail.com`), which is always available.
- The alerts auditor must add `"tick_stale"` to `ALERT_KINDS` and a `_TOGGLE_COL` entry (suggest `"alert_on_urgent"` — operational alert that rides the same toggle as `forwarding_lost` and `sms_fail`).

**Contract for the alerts auditor:**
```python
alerts.notify(ops_biz, "tick_stale", {"gap_minutes": float})
# ops_biz = db.get_business(1)
# ctx keys: {"gap_minutes": float}  -- e.g. 22.5 for a 22.5-minute gap
```

### Position of the gap check

**AFTER writing the new heartbeat** (not before). This ensures:
1. Even if the gap-check or alert fails, the new heartbeat is already written (the next tick won't also fire a stale alert for the same gap).
2. The gap is computed from `_prev_tick_utc` (read before the write) against `datetime.now(timezone.utc)` at the time of the new tick — this is the true elapsed time between ticks.

The placement in the diff above is correct: gap check runs immediately after `db.set_meta("last_tick_utc", _tick_utc)`.

### Total death (both cron + in-process ticker down)

Cannot be self-detected. The `tick_stale` alert requires `tick_once` to run — if nothing runs, nothing alerts. Flag this as owner-ops: configure an **external uptime monitor** (e.g. Render's built-in uptime check, or UptimeRobot) on `GET /health/ticker`. The endpoint already exists (`app.py` line 2946) and returns `{"fresh": bool, "last_tick_utc": str, "age_s": float}`. An external monitor that alerts on `fresh=False` covers total death. Do NOT attempt to solve this in code.

### `tick_stale` dedupe

The alert should dedupe per day (not per gap) to prevent a cascade: if the ticker was dead for 2 hours and then fires 3 ticks in a row catching up, only ONE `tick_stale` alert should go out. The alerts auditor should add `"tick_stale"` to `_DAILY_DEDUPE_KINDS` with a 26h window. The dedupe key: `f"tick_stale:{local_day}"` where `local_day` is the UTC date at alert time.

**However**, the current `notify` function takes `kind` and `context` but computes the dedupe key internally. The `tick_stale` context should include `local_day` for the dedupe key:

```python
# In tick_once:
_gap_min = round(_gap_s / 60, 1)
_ops_biz = db.get_business(1)
_local_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
alerts.notify(_ops_biz, "tick_stale", {
    "gap_minutes": _gap_min,
    "local_day": _local_day,   # for alerts.py dedupe key
})
```

### Test to add (`test_ticker_health.py`):
```python
# Verify gap detection fires a tick_stale alert when last_tick_utc is old
from datetime import timedelta
old_tick = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
db.set_meta("last_tick_utc", old_tick)
_before_stale = db.get_conn().execute(
    "SELECT COUNT(*) FROM alerts WHERE kind='tick_stale'").fetchone()[0]
db.get_conn().close()
reminders.tick_once()
# NOTE: tick_stale is the other auditor's surface; this test only verifies
# tick_once calls alerts.notify with the right kind (stub alerts.notify if needed).
```

---

## Consolidated `tick_once` Edit Checklist

Current `tick_once` body (lines 743–804). After 6b, the following changes apply in order:

| # | Line(s) | Action | Detail |
|---|---------|--------|--------|
| 1 | After 747 | INSERT | `_prev_tick_utc = db.get_meta("last_tick_utc")` |
| 2 | After 753 | INSERT | Gap-detection block (reads `_prev_tick_utc`, fires `tick_stale` if gap > 900s) |
| 3 | 775–788 | REPLACE | Remove `scan_morning_briefing` + `scan_growth_tray` calls; add `scan_daily_digest(now)` |
| 4 | 786–789 | MODIFY | Add `if now_local.hour < 12: continue` inside `scan_stall_nudges` (NOT in tick_once; this edit is in `scan_stall_nudges` body) |

Lines 759 (`scan_followups`), 762–768 (`growth.scan`), 770–774 (`connections.check_forwarding_health`), 790–794 (`scan_screening_graduation`), 795–801 (`google_contacts_sync_all`), and 802–803 (`run_due_once`) are UNCHANGED.

---

## Test File Coverage Matrix

| Surface | Test file | Calls function directly? | Calls via tick_once? |
|---------|-----------|--------------------------|----------------------|
| `scan_morning_briefing` | `test_vic_proactive.py` | YES (lines 227–270) | No |
| `scan_growth_tray` | `test_growth_tray_sms.py` | YES (9 call sites) | No |
| `scan_stall_nudges` | `test_vic_proactive.py` | YES (lines 308–425) | No |
| `tick_once` | `test_ticker_health.py` | YES (line 50) | N/A |
| `followup_body_contextual` | `test_reminders.py` | No (no direct test) | No (via scan_followups) |
| `scan_daily_digest` | (none yet) | None | None |

**Conclusion on test safety:**
- Removing `scan_morning_briefing` + `scan_growth_tray` from `tick_once` does NOT break any test — no test asserts those functions fire from `tick_once`.
- The afternoon-only stall change DOES risk breaking `test_vic_proactive.py` lines 419 and 425 if they run before noon UTC (those two calls use `db.now_iso()` as the default time). Fix both calls to pass an explicit afternoon timestamp.
- No test currently covers `followup_body_contextual` with a timeout scenario. Add one.
- No test currently covers `scan_daily_digest`. Must be written as part of 6b.
- `test_ticker_health.py`'s `tick_once()` call (line 50) will run `scan_daily_digest` after the change. Since the test DB has no leads and no held messages, `scan_daily_digest` will find nothing to fire and return 0. **Test is safe.**

---

## Landmines Summary

1. **`warm_leads_idle` is NOT sorted by idle_hours.** The function returns rows in arbitrary SQL GROUP BY order. The digest's "top stall" logic MUST sort manually. (Diff above includes the sort.)

2. **`test_vic_proactive.py` lines 419 + 425** use `reminders.scan_stall_nudges()` with no timestamp arg — these will fail if the test suite runs before noon UTC after the afternoon-only guard is added. Fix both lines to pass an explicit 14:00 UTC timestamp.

3. **`anthropic.Anthropic(timeout=...)` requires SDK >= 0.20.0.** Check with `pip show anthropic` in `.venv`. If older, use the `http_client=httpx.Client(timeout=...)` approach instead.

4. **`tick_stale` is NOT yet in `ALERT_KINDS`.** The `alerts.notify` call in `tick_once` will silently return `[]` (line 232: `if kind not in ALERT_KINDS`) until the alerts auditor adds it. The gap-detection code is safe to ship before that — it will just no-op until `tick_stale` is registered.

5. **`db.get_business(1)` for ops alerts**: This works correctly only if business id=1 exists (it always does for the seed business). On a fresh DB with no seed, `get_business(1)` returns `dict(DEFAULT_BUSINESS, id=1)` which has no `alert_sms` — the SMS send is skipped but the in-app alert is still written. Acceptable.

6. **`scan_daily_digest` with `n=0` but `held_count > 0`**: The gate `if n == 0 and held_count == 0 and not top_stall_name: continue` correctly handles the case where there are held plays but no open leads (e.g., a business with growth plays queued for past customers but no new leads). The digest fires, mentions the held plays, and skips the lead line. The `format_message` implementation in alerts.py (other auditor's surface) must handle `n=0` gracefully (omit the lead line rather than showing "0 leads need you").
