# Phase 5a + 5b Consent & Honesty Audit
**Date:** 2026-06-18  
**Auditor:** Read-only, adversarial pass — no source files edited  
**Branch:** staging @ e21c2d2 (5a + 5b built)  
**Lens:** Silent customer sends · One-tap integrity · Channel honesty · Frequency caps · Referential guard · Enforce double-confirm · Honest degradation · Copy accuracy  

---

## FINDINGS

### P1 — Guard asymmetry: streaming Claude path skips the "ask before guess" referential check

**File:** `assistant.py` lines 2229–2233 (`run_stream` deterministic confirm block) and lines 2179–2181 (`_tool_loop_stream`)  
**Consent risk:** The P1-4 "never guess a recipient" rule requires that a bare pronoun with nothing shown triggers "Which lead?" — not a guess-and-confirm. Two of the four code paths (the streaming Claude path) are missing the `elif not _is_named_or_pinned(args): return _say("Which lead?")` branch.

**Exact gap:**
- `run()` line 2068–2073 — full guard present ✓  
- `_tool_loop` (non-stream) line 1545–1553 — full guard present ✓  
- `run_stream` confirm block line 2229–2233 — guard MISSING  
- `_tool_loop_stream` line 2179–2181 — guard MISSING  

**What happens instead:** "text her back saying hey" with entities=None + Claude live provider → `run_stream`'s deterministic confirm block calls `_gated` directly with `name="her"` + body → `_gated` routes to a confirm card showing the most-recent lead (e.g. "Bob · +15550002").

**Mitigation that prevents a P0 (silent send):** The confirm card always shows recipient name + phone in the preview. Dave must tap "Send." No text ever reaches a customer without his explicit tap. The token is server-bound. This is NOT a silent send.

**Why it's still P1:** The spec's intent for this scenario is "ask which lead, don't guess." Showing a confirm card with the wrong guess-lead is less alarming than a silent send, but it can cause Dave to accidentally send "hey" to the wrong customer if he taps without reading the recipient name. A contractor's reputation depends on sending the right message to the right person.

**Suggested fix (3 lines):**
```python
# In run_stream() around line 2229-2233, after the entities block:
if spec and spec["confirm"]:
    if tool in _LEAD_TOOLS and _is_referential_lead(message):
        if entities:
            _apply_referent(message, args, entities)
        elif not _is_named_or_pinned(args):   # ADD THIS
            yield from _stream_static(_say("Which lead? Tell me a name, or say \"list my leads\" first."))
            return
    yield from _stream_static(_gated(business, tool, args, message))
    return
# Similarly add the elif guard to _tool_loop_stream lines 2179-2181.
```

---

### P2 — Minor: `vic_morning`/`vic_stall` share the `alert_on_lead` toggle without a dedicated setting

**File:** `alerts.py` line 47  
**Consent risk:** Turning off lead-arrival alerts (`alert_on_lead=0`) also silences the morning briefing digest and stall nudges. The owner may not know this. Conversely, the owner cannot turn off proactive pushes independently of transactional lead alerts.

**Impact:** Low — no silent customer send risk. Dave asked to be notified of leads; the proactive pushes share that preference. But if an owner turns off lead alerts to reduce noise and still wants morning digests, both stop without explanation.

**Suggested fix:** Document this toggle coupling in the Settings UI. A future phase can add `alert_on_proactive` column without a migration (same pattern as other toggles).

---

## VERIFIED-HONEST (gates confirmed to hold)

The following gates were confirmed empirically (test runs, throwaway probes):

1. **No silent customer sends — proactive engine** (`test_vic_proactive.py` 31/31 green; `/tmp` probe 5 ticks × 2 scan functions): zero outbound sends reached any lead/consumer phone across all proactive paths. All SMS recipients matched owner `alert_sms` cells. `alerts.notify` sends with `gate=False` to `business.get("alert_sms")` — never to a lead number.

2. **One-per-day frequency caps hold** (test2 + regression dedupe test): morning digest fires once in the [7,10) window; a simulated "later tick on the same day" (alert backdated 10 min) does not re-fire. Stall nudge deduplicates per lead per local day. Both use `_DAILY_DEDUPE_SECONDS = 26 * 3600` (not the 120s event window), confirmed to close the double-fire bug.

3. **One-tap stays one-tap — token gate** (`test_confirm_token.py` 16/16 green): tokens are server-bound, single-use, expiring, and tenant-scoped. Second tap returns stored result; expired token returns honest reply; cross-tenant attempt fails closed; missing/unknown token returns honest 400. The stored args (recipient + tool) cannot be swapped by the client POST body.

4. **Editable body stays recipient-bound** (`test_confirm_token.py` test 6; `test_vic_guard.py` BETA Test 4): the owner may edit the `text_lead` body on the confirm card; the `message` field override is accepted. The stored `_lead_id`/phone are not overridable — a forged recipient in the POST is silently ignored.

5. **No "tap to send" copy in owner SMS** (`test_vic_proactive.py` tests 2–3; direct source read): every `vic_morning` and `vic_stall` body ends with "Open FirstBack." / "Open FirstBack to text them back." The strings never say "tap to send," "tap here to send," "reply to send," or imply the SMS itself executes a customer send. Code comment at `alerts.py:92` explicitly enforces this.

6. **`set_screen_mode=enforce` double-confirm** (`test_vic_surface.py` 16/16 green): first tap without `enforce_ack` returns warning + meta `pending_ack`, token remains `consumed=0`. Second tap with `enforce_ack=true` executes and marks consumed. The token is not claimed on the first tap — race-safe.

7. **Referential guard — deterministic paths** (`test_vic_guard.py` BETA Test 1, 20/20 green): "text her back" and "text him saying …" with `entities=None` ask "Which lead?" in both `run()` and `_tool_loop`. "text my last lead saying running late" correctly resolves (not over-blocked). "text Dana back" (named, no body) mints a genuine one-tap confirm.

8. **`_default_draft` never auto-sends** (code + test): `_default_draft` returns a short template string; it is placed in `args["message"]` and then `_gated` mints a token + pending_action. The owner sees the draft on the confirm card and can edit or cancel. No execution path skips the confirm.

9. **"Vic's resting" is accurate** (`test_vic_surface.py` test 1): `vic_status="resting"` is only set when `allow_llm=False`; the confirm gate (`/assistant/confirm`) does NOT check `allow_llm` — it only checks CSRF + token — so "briefing and one-tap still work" is true. The `resets_at` field is a parseable ISO timestamp.

10. **Simulated-send honesty**: `_h_text_lead` post-confirm reply for simulated status reads "(simulated until Twilio is connected)" — no green tint, no "sent" claim. The `outbound_mode` preview badge shows "Test mode — not sent" before the owner taps.

11. **Money claims grounded**: morning digest extracts `~$X` from `assistant.briefing()`'s `headline` (derived from real pipeline data × `avg_job_value`). Stall nudge uses `biz.get("avg_job_value")` (owner-configured). Both produce empty string when unset. No invented dollar amounts.

12. **A2P gate on scheduled customer sends**: `run_due_once` calls `messaging.send_sms(biz, phone, body, lead_id=…)` with default `gate=True`, which enforces A2P approval and opt-out checks before any real send.

---

## OVERALL ASSESSMENT

The consent model for Phase 5a + 5b is **structurally sound**. No silent customer sends are possible. Every proactive output goes to the owner's own `alert_sms` cell. The token gate (SF-6) correctly binds recipient + action at the server. The enforce double-confirm works and is race-safe.

The single P1 finding (guard asymmetry in two streaming paths) is a **UX fidelity gap**, not a consent breakdown: a confirm card is still shown, and Dave must tap before any customer text goes out. The risk is a misrouted "hey" to the wrong lead if Dave taps without reading the recipient name. Fix cost: 3 lines per path.

The P2 finding (toggle coupling) is informational — no action required before shipping, but document it.

**Verdict: safe to ship with P1 fix applied; P1 is not a blocker if the confirm card's recipient preview is considered sufficient catch.**

---

## SUITE RESULTS

| Test file | Result |
|---|---|
| `test_confirm_token.py` | 16/16 green |
| `test_vic_proactive.py` | 31/31 green |
| `test_vic_guard.py` | 20/20 green |
| `test_vic_surface.py` | 16/16 green |
| `test_assistant.py` | 129/129 green |
| `test_reminders.py` | 34/34 green |
| `test_compliance_core.py` | 47/47 green |
| `test_briefing.py` | 22/22 green |
| Empirical consumer-send probe (5 ticks) | ZERO consumer sends |
