"""F03 Brain guards (Phase 2). Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_f03_brain.py

Covers:
  - Turn cap fires at 12 inbound messages, not at 11
  - Handoff reply includes business phone
  - Price guard strips $500, "five hundred dollars", "500 bucks"
  - Price guard does NOT strip "estimate is free", "3 rooms", bare numbers
  - Length guard trims at sentence boundary around 480 chars
  - generate_reply signature unchanged
"""
import os
import sys
import re
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"

import config as _config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
_config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

import ai

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- Turn cap tests ----
_BIZ = {
    "id": 99999,
    "name": "Test Biz",
    "trade": "Painting",
    "service_area": "Test area",
    "hours": "Mon-Fri",
    "owner_name": "Bob",
    "phone": "(555) 999-1234",
    "ai_instructions": "You are a test assistant.",
}

# 11 inbound messages: should NOT trigger cap.
_history_11 = [{"direction": "in", "body": f"msg {i}"} for i in range(11)]
_history_11_with_out = []
for i, m in enumerate(_history_11):
    _history_11_with_out.append(m)
    _history_11_with_out.append({"direction": "out", "body": f"reply {i}"})

# The turn-cap guard counts only "in" messages.
def _make_history(n_inbound):
    h = []
    for i in range(n_inbound):
        h.append({"direction": "in", "body": f"customer msg {i}"})
        h.append({"direction": "out", "body": f"bot reply {i}"})
    return h

# 11 inbound: no cap
h11 = _make_history(11)
reply11, _ = ai.generate_reply(_BIZ, h11)
check("turn cap does NOT fire at 11 inbound", "(555) 999-1234" not in reply11)
check("reply at 11 is a normal reply (not handoff)", len(reply11) < 300)

# 12 inbound: cap fires
h12 = _make_history(12)
reply12, booking12 = ai.generate_reply(_BIZ, h12)
check("turn cap fires at 12 inbound", "(555) 999-1234" in reply12)
check("booking is None at cap", booking12 is None)
check("handoff message includes business phone", _BIZ["phone"] in reply12)

# Business with no phone: handoff still works, doesn't crash
_biz_no_phone = dict(_BIZ, phone="")
h12b = _make_history(12)
reply12b, _ = ai.generate_reply(_biz_no_phone, h12b)
check("handoff works when business has no phone", reply12b is not None)

# ---- Price guard tests ----

# Direct tests on _apply_price_guard
pg = ai._apply_price_guard

SCRUB = ai._PRICE_SCRUB

# Should scrub:
check("$500 is scrubbed", SCRUB in pg("The job costs $500."))
check("$1,200 is scrubbed", SCRUB in pg("That would be $1,200."))
check("$ 500 is scrubbed (space)", SCRUB in pg("Around $ 500 total."))
check("500 dollars is scrubbed", SCRUB in pg("About 500 dollars for the work."))
check("500 bucks is scrubbed", SCRUB in pg("Around 500 bucks."))
check("five hundred dollars is scrubbed", SCRUB in pg("That runs five hundred dollars."))

# Should NOT scrub:
check("'estimate is free' NOT scrubbed", SCRUB not in pg("The estimate is free."))
check("'3 rooms' NOT scrubbed", SCRUB not in pg("We can paint 3 rooms."))
check("bare '500' NOT scrubbed", SCRUB not in pg("We have 500 customers."))
check("'free estimate' NOT scrubbed", SCRUB not in pg("We offer a free estimate."))
check("'2 coats' NOT scrubbed", SCRUB not in pg("We apply 2 coats of paint."))
check("'100% satisfaction' NOT scrubbed", "100%" in pg("We guarantee 100% satisfaction."))

# Price guard in generate_reply output
_biz_price = dict(_BIZ)
history_with_price = [
    {"direction": "in", "body": "How much does it cost?"},
]
# Patch the demo reply to inject a price mention
_orig_demo = ai._demo_reply
def _mock_demo_with_price(business, history, slots):
    return "It costs $500 for a standard room."
ai._demo_reply = _mock_demo_with_price
reply_price, _ = ai.generate_reply(_biz_price, history_with_price)
ai._demo_reply = _orig_demo
check("price in demo reply is scrubbed via generate_reply", SCRUB in reply_price)
check("'$500' not in scrubbed reply", "$500" not in reply_price)

# ---- Length guard tests ----

lg = ai._apply_length_guard

short = "This is a short reply."
check("short reply unchanged", lg(short) == short)

# Build a reply >480 chars with clear sentence boundaries
long_reply = ("This is sentence one. " * 25).strip()
check("long reply > 480 chars before trim", len(long_reply) > 480)
trimmed = lg(long_reply)
check("trimmed reply <= 480 chars", len(trimmed) <= 480)
check("trimmed reply ends at sentence boundary (period)", trimmed.endswith("."))

# Reply exactly at boundary: no trim
boundary_reply = "A" * 479 + "."
check("reply at 479 chars unchanged", lg(boundary_reply) == boundary_reply)

# No sentence boundary available: hard trim with ellipsis
no_boundary = "x" * 500
trimmed_hard = lg(no_boundary)
check("no-boundary trim ends in '...'", trimmed_hard.endswith("..."))
check("no-boundary trim is 483 chars or fewer", len(trimmed_hard) <= 483)

# Length guard applied via generate_reply
def _mock_demo_long(business, history, slots):
    return "This is a long sentence about painting. " * 20
ai._demo_reply = _mock_demo_long
reply_long, _ = ai.generate_reply(_biz_price, [{"direction": "in", "body": "hi"}])
ai._demo_reply = _orig_demo
check("generate_reply applies length guard", len(reply_long) <= 483)

print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
