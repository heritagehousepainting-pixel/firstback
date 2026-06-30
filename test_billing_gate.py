"""PS-3 billing-gate tests. Run: python3 test_billing_gate.py

Verifies decisions.md PS-3: a paid subscription must NOT be allowed to start until
BOTH (1) activation_state has reached voice_live AND (2) a real AI call has been
answered (first_call_nudge_sent). Pure-function tests on billing.checkout_gate_* —
no network, no Stripe account, no DB. Standalone; exits 0 all-green, 1 on any fail.
"""
import os
import sys

# Minimal env so importing billing/config/db doesn't complain.
os.environ.setdefault("FIRSTBACK_PROVIDER", "demo")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_fake")

import billing

_pass = _fail = 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}" + (f" ({detail})" if detail else ""))


def biz(state, nudge):
    return {"id": 1, "activation_state": state, "first_call_nudge_sent": nudge}


print("\n=== PS-3 gate CLOSED until voice-live AND a real call ===")
check("setup + no call -> blocked", not billing.checkout_gate_ok(biz("setup", 0)))
check("setup + call -> blocked (forwarding not confirmed)",
      not billing.checkout_gate_ok(biz("setup", 1)))
check("voice_live + no call -> blocked",
      not billing.checkout_gate_ok(biz("voice_live", 0)))
check("None business -> blocked", not billing.checkout_gate_ok(None))
check("missing flags -> blocked (default deny)",
      not billing.checkout_gate_ok({"id": 1}))

print("\n=== PS-3 gate OPEN once both conditions are met ===")
check("voice_live + call -> allowed", billing.checkout_gate_ok(biz("voice_live", 1)))
check("live_sms + call -> allowed (later state still allowed)",
      billing.checkout_gate_ok(biz("live_sms", 1)))

print("\n=== Owner-facing gate reasons ===")
check("open gate -> empty reason",
      billing.checkout_gate_reason(biz("voice_live", 1)) == "")
check("setup -> reason mentions forwarding",
      "forwarding" in billing.checkout_gate_reason(biz("setup", 0)).lower())
check("voice_live + no call -> reason mentions first call",
      "first call" in billing.checkout_gate_reason(biz("voice_live", 0)).lower())

print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
