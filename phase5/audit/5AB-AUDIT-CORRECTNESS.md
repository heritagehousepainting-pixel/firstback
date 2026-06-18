# Phase 5a + 5b Correctness / Integration Audit

**Date:** 2026-06-18  
**Branch:** staging @ e21c2d2  
**Auditor:** Read-only correctness/integration pass. No source files were modified.  
**Suite result:** 50/50 green (full `test_*.py` run).

---

## Summary

5a and 5b merge is **solid**. The cross-slice integration contracts hold. The previously-fixed
120s-vs-daily dedupe window is correct in the committed code. One low-severity behavioral gap
was found in `run_stream` (P1-4 guard not wired in the Claude streaming path); two test-quality
weaknesses were noted. No P0 bugs. No wrongful sends. No token-gate bypass.

---

## P0 — Critical / Must fix before ship

**None found.**

---

## P1 — Spec drift / Behavioral gap

### P1-A: `run_stream` Claude path missing the P1-4 "bare referent → ask which lead" guard

**File:** `assistant.py:2229–2232`  
**What's wrong:** In `run_stream()` when `allow_llm=True and provider=="claude"`, the
confirm-gated write path (line 2229) is:

```python
if tool in _LEAD_TOOLS and entities and _is_referential(message):
    _apply_referent(message, args, entities)
yield from _stream_static(_gated(business, tool, args, message))
```

The condition `entities and _is_referential(message)` is `False` when `entities=None/[]`,
so it falls through to `_gated` directly. `_gated` only guards for a missing *message*
(one-tap draft path), not for a bare pronoun referent with no entities.

Compare with `run()` at line 2068–2073, which explicitly checks:

```python
if tool in _LEAD_TOOLS and _is_referential_lead(message):
    if entities:
        _apply_referent(...)
    elif not _is_named_or_pinned(args):
        return _say("Which lead? Tell me a name ...")
```

**What breaks:** When the owner says "text her back" through the streaming endpoint (Claude
provider, no entities shown), `run_stream` resolves to the most-recent lead and asks
"What should I text {Name}?" instead of "Which lead? Tell me a name". No wrongful send
occurs (pending_action is still None; a body is still required), but the wrong follow-up
question is asked, and the operator never gets a chance to name the right recipient.

**Observed via probe:** `run_stream("text her back", entities=None)` → reply: "What should I
text Mike Builder?" vs `run("text her back", entities=None)` → "Which lead? Tell me a name."

**Suggested fix:** Mirror `run()`'s guard in `run_stream()`:
```python
if tool in _LEAD_TOOLS and _is_referential_lead(message):
    if entities:
        _apply_referent(message, args, entities)
    elif not _is_named_or_pinned(args):
        yield from _stream_static(_say("Which lead? Tell me a name, or say \"list my leads\" first."))
        return
```

**Scope:** Only affects the live Claude provider streaming path (`allow_llm=True and
provider=="claude"`). Demo/MiniMax/budget-exhausted paths fall through to `run()` which
has the correct guard. Not a safety issue (no send without explicit tap), but a P1-4
spec violation in the most capable path.

---

## P2 — Test quality / Minor issues

### P2-A: `test_vic_proactive.py` urgency assertion is too weak

**File:** `test_vic_proactive.py:342–344`  
**What's wrong:**
```python
check("test3: >48h stall nudge escalates tone (body reflects urgency)",
      "still waiting" in body3b or "Open FirstBack" in body3b)
```
The condition `"still waiting" in body3b or "Open FirstBack" in body3b` is true for **any**
stall-nudge body, not just the >48h escalation. The actual urgency text
`"They may be shopping around."` (from `alerts.format_message` line 111) is never asserted.

**Impact:** The test would pass even if the urgency escalation were accidentally removed.  
**Suggested fix:** Assert `"shopping around" in body3b` to verify the actual escalation copy.

### P2-B: `test_vic_guard.py` BETA Test 2 regression guard is internally inconsistent

**File:** `test_vic_guard.py:78–82`  
**What's wrong:**
```python
check("'last lead' NOT caught as referential (regression guard)",
      not assistant._is_referential("text my last lead saying running late") or
      # if _is_referential catches it, entities being None should NOT block it since
      # the message contains 'last lead' which routes deterministically
      r2.get("pending_action") is not None)
```
This is an OR-condition that passes as long as either sub-expression is true. The second
clause (`r2.get("pending_action") is not None`) already passes unconditionally (it's asserted
separately on line 73), so this check never tests whether `_is_referential` correctly excludes
"my last lead". The check is vacuously true.  
**Impact:** Low. The actual behavior is correct (verified by probe), but the test doesn't
fail if `_is_referential` started catching "last lead."  
**Suggested fix:** Assert the first clause directly:
```python
check("'last lead' NOT caught as referential_lead",
      not assistant._is_referential_lead("text my last lead saying running late"))
```

### P2-C: `resets_at` field not surfaced to user in JS pill

**File:** `static/assistant.js:497–506`  
**Observation (not a bug — but a spec note):** The spec says set `resets_at` in the JSON;
the JS renders a static "Back to full power tomorrow." pill without using the `resets_at`
value. The field is in the payload (test verified parseable), but the pill text is hardcoded
regardless of the actual midnight time. This means a business in a DST transition could
show "tomorrow" when full power resets at 23:xx today. Low impact (the static copy is
accurate in the common case), but worth noting for future polish.

---

## Verified Correct

The following were audited, probed, and found correct:

| Area | Finding |
|---|---|
| **P1-1 briefing tail** | Appended to lead/booking alerts, capped at 320 chars. Core event line always survives. Empty on quiet briefing or exception. Verified via probe + test. |
| **P1-2 morning window [7,10) local** | Lower boundary (hour=7) fires; upper boundary (hour=10) does NOT (exclusive). Hour 9:59 fires. Correct `not (7 <= now_local.hour < 10)` guard. |
| **P1-2 dedupe** | `_DAILY_DEDUPE_SECONDS = 26*3600` is used for `vic_morning/vic_stall` kinds. Day-stamped `local_day` key rolls at local midnight. Second tick same day dedupes. Next day fires again. |
| **P1-2 quiet briefing gate** | `tone != "active"` or empty `items` → nothing sent. |
| **P1-3 stall threshold** | `warm_leads_idle(bid, 24)`: `<24h` excluded, `>=24h` included (using `HAVING MAX < cutoff` SQL). Semantically equivalent to stage='warm' (has replied). Urgent leads correctly excluded. |
| **P1-3 48h urgency boundary** | `idle_h > 48` (strictly greater, not >=). Exactly 48h → no urgency. 48.001h → "They may be shopping around." |
| **P1-3 no-name lead** | Falls back to "They" in copy; no crash. |
| **P1-4 referential guard in `run()`** | "text her back" + no entities → asks "Which lead?". Named "text Dana back" → one-tap draft. "text my last lead" → asks for body (most-recent fallback path, not referential). |
| **P1-5 vic_status surface** | Set when `allow_llm=False` in both `/assistant` (non-streaming) and `/assistant/stream` (SSE done frame). Not set when `allow_llm=True`. `resets_at` is parseable ISO with tz offset. |
| **P1-6 enforce two-stage** | First tap (no ack) → `pending_ack` status, token unconsumed. Multiple no-ack taps → still unconsumed. Second tap with `enforce_ack=true` → executes, consumes. Idempotent replay. Expired enforce → expires path fires first. |
| **5a token gate** | `pending_confirms` table correct. `issue/get/claim/set_result` functions correct. `claim_confirm_token` atomically conditional (race guard). Token scoped by business_id. |
| **5a token in `_apply_learning`** | Second pending-construction site (line 2279) correctly calls `_issue_token`. Verified. |
| **One-tap draft** | `_default_draft` uses `first` name or "there" for no-name leads. `_is_named_or_pinned` correctly rejects pronoun names. Draft is editable (5a body override path intact). |
| **JS enforce two-stage** | `.finally` resets `busy=false` before user can click ackBtn (verified by simulation). `runAction` re-posts with `enforce_ack="true"`. Cancel path supported. |
| **Cross-slice: ALPHA calls BETA** | `_briefing_tail` and `scan_morning_briefing` both lazy-`import assistant` and call `assistant.briefing(business)`. No import cycle. Exception-safe. |
| **No proactive sends to customer numbers** | All `vic_morning`/`vic_stall` sends go to `business.alert_sms`, never to lead phone. Gate is `alerts.notify` → `messaging.send_sms(business, sms_to, body, gate=False)`. |
| **`tick_once` exception isolation** | `scan_morning_briefing` and `scan_stall_nudges` wrapped in `try/except`; per-business errors also caught. Exceptions do not kill the tick. |
| **No-tz business fallback** | `_biz_tz(business)` falls back to `app_tz()`. `_next_local_midnight_iso` falls back to UTC midnight. No crash. |
| **`resets_at` format** | ISO-8601 with UTC offset (e.g. `2026-06-19T00:00:00-04:00`). Parseable by `datetime.fromisoformat()`. Correct midnight boundary for Chicago and ET. |
| **Truncation at room≤5** | `if room > 5` guard prevents partial tail of 1–5 chars (would be truncated mid-word). Core body always untruncated. |
| **Full suite** | 50/50 green after merge. No regressions. |

---

## Out-of-scope (deferred per spec)

- SMS deep-link one-tap (push/magic-link auth not built)
- P2-1 weekly Vic track record, P2-4 summarization, P2-5 voice input
- Redis overlay for token store (P2-6)
