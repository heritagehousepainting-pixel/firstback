"""Phase 2 compliance checks. Run: python3 test_compliance.py

Pure unit tests for the code half of compliance (opt-out detection, quiet-hours
gating, registration readiness). No DB, no network. Exits non-zero on failure.
"""
import compliance

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


class _Clock:
    def __init__(self, hour):
        self.hour = hour


# ---- detect_revocation: plain-language opt-outs (must catch) ----
for t in ["stop texting me", "please don't text me", "do not text me",
          "take me off your list", "remove me", "unsubscribe me",
          "no more texts", "leave me alone", "quit messaging me", "STOP TEXTING"]:
    check(f"revocation caught: {t!r}", compliance.detect_revocation(t) is True)

# ---- detect_revocation: ordinary messages (must NOT trip) ----
for t in ["I'll stop by the house tomorrow", "can you come by", "yes that works",
          "what time", "I need my kitchen painted", "call me", "sounds good"]:
    check(f"not a revocation: {t!r}", compliance.detect_revocation(t) is False)

# ---- voice_allowed_now: quiet-hours window ----
check("9am is allowed (8-21)", compliance.voice_allowed_now(_Clock(9), 8, 21) is True)
check("7am is blocked (8-21)", compliance.voice_allowed_now(_Clock(7), 8, 21) is False)
check("9pm is blocked (8-21)", compliance.voice_allowed_now(_Clock(21), 8, 21) is False)
check("8pm allowed under 8-21", compliance.voice_allowed_now(_Clock(20), 8, 21) is True)
check("8pm blocked under strict 8-20", compliance.voice_allowed_now(_Clock(20), 8, 20) is False)

# ---- registration readiness ----
check("a2p not ready by default", compliance.a2p_ready({}) is False)
check("a2p ready when approved", compliance.a2p_ready({"a2p_status": "approved"}) is True)
ready = {"twilio_number": "+15553140000", "a2p_status": "approved",
         "forward_to": "+15559990000", "webhooks_wired": 1}
check("no blockers when fully set up + configured",
      compliance.launch_blockers(ready, True) == [])
check("blockers list everything when nothing is set up",
      len(compliance.launch_blockers({}, False)) >= 3)

# ---- webhooks_wired gate: a number with no webhooks can't read as live ----
unwired = {"twilio_number": "+15553140000", "a2p_status": "approved",
           "forward_to": "+15559990000"}  # webhooks_wired falsy
check("a numbered but unwired business is flagged 'not wired'",
      any("isn't wired to receive calls and texts" in b
          for b in compliance.launch_blockers(unwired, True)))
check("the unwired number does not double-flag 'no number provisioned'",
      not any("No FirstBack phone number" in b
              for b in compliance.launch_blockers(unwired, True)))
wired = dict(unwired, webhooks_wired=1)
check("wiring the webhooks clears the 'not wired' blocker",
      not any("isn't wired to receive calls and texts" in b
              for b in compliance.launch_blockers(wired, True)))
check("a fully wired + approved + forwarded business has no blockers",
      compliance.launch_blockers(wired, True) == [])

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
