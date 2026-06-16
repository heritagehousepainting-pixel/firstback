"""Shared compliance test suite for the trades_core spine.

Runs identically (a) standalone from trades_core/ and (b) vendored inside JobMagnet
and RingBack, so BOTH products exercise the exact same consent + messaging logic.
Framework-free (matches the apps' test_*.py style): run with any python, exit 0 = pass.

  python3 trades_core/test_compliance_core.py
"""
import sqlite3
import sys
from datetime import datetime

import consent
try:                       # vendored into each app as tc_messaging.py …
    import tc_messaging as tcm
except ImportError:        # … and is plain messaging.py canonically in trades_core/
    import messaging as tcm

_passed = _failed = 0


def check(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL  {label}")


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    consent.ensure_ledger(c)
    return c


# ---- Hole #2: natural-language opt-out (superset of exact keyword) ----
for msg in ["STOP", "stop", "stop please", "Unsubscribe", "please stop texting me",
            "take me off your list", "remove me", "do not text me again",
            "leave me alone", "no more texts please", "quit texting me",
            "stop sending me texts", "opt me out", "lose my number"]:
    check(f"opt-out detected: {msg!r}", consent.opt_out_nlu(msg) is True)

# must NOT opt out an engaged customer
for msg in ["Stop by tomorrow at 9am", "can you stop by Saturday",
            "cancel my appointment", "yes please book me", "what time works",
            "sounds good", "can you text me the address", ""]:
    check(f"engaged kept: {msg!r}", consent.opt_out_nlu(msg) is False)

# opt-in + opt-out-wins tie-break
check("opt-in: START", consent.opt_in_nlu("START") is True)
check("opt-in: 'resume texts'", consent.opt_in_nlu("resume texts") is True)
check("classify opt_out wins", consent.classify_inbound("stop texting me") == "opt_out")
check("classify opt_in", consent.classify_inbound("start") == "opt_in")
check("classify none", consent.classify_inbound("what time?") is None)

# ---- number normalization (one identity per phone) ----
check("normalize formats agree",
      consent.normalize_number("(555) 314-2270")
      == consent.normalize_number("+1 555-314-2270")
      == consent.normalize_number("15553142270") == "+15553142270")
check("normalize empty", consent.normalize_number("") == "")

# ---- append-only ledger: record / current_status / suppression / idempotency ----
c = _conn()
check("unknown number not suppressed", consent.is_suppressed(c, 1, "5553142270") is False)
check("record opt-out writes", consent.record(c, 1, "5553142270", "sms", "opted_out", "inbound STOP") is True)
check("opted-out number suppressed", consent.is_suppressed(c, 1, "5553142270") is True)
check("idempotent: same event no-ops",
      consent.record(c, 1, "5553142270", "sms", "opted_out", "retry") is False)
rows = c.execute("SELECT COUNT(*) n FROM consent_ledger").fetchone()["n"]
check("idempotency kept ledger at one row", rows == 1)
check("re-grant writes a new event", consent.record(c, 1, "5553142270", "sms", "granted", "inbound START") is True)
check("after re-grant, not suppressed", consent.is_suppressed(c, 1, "5553142270") is False)
check("history preserved (2 events)",
      c.execute("SELECT COUNT(*) n FROM consent_ledger").fetchone()["n"] == 2)
# multi-tenant isolation: another business's opt-out doesn't leak
check("tenant isolation", consent.is_suppressed(c, 2, "5553142270") is False)
# per-channel independence
check("voice channel independent", consent.current_status(c, 1, "5553142270", "voice") is None)

# ---- Hole #1: quiet hours, with the transactional carve-out ----
nine_pm = datetime(2026, 6, 16, 22, 0)   # inside 21->8 window
noon = datetime(2026, 6, 16, 12, 0)
check("quiet window wraps midnight (22:00 blocked)", tcm.in_quiet_hours(nine_pm, 21, 8) is True)
check("noon allowed", tcm.in_quiet_hours(noon, 21, 8) is False)
check("marketing blocked in quiet hours", tcm.quiet_blocked(nine_pm, 21, 8, transactional=False) is True)
check("transactional EXEMPT in quiet hours", tcm.quiet_blocked(nine_pm, 21, 8, transactional=True) is False)
check("nothing blocked at noon", tcm.quiet_blocked(noon, 21, 8, transactional=False) is False)

# ---- Twilio signature: fail-closed when unconfigured ----
check("empty auth token => reject", tcm.valid_signature("https://x/y", {"a": "1"}, "sig", "") is False)
check("bad signature rejected", tcm.valid_signature("https://x/y", {"a": "1"}, "wrong", "tok") is False)
# a correctly-computed signature must validate
import base64, hashlib, hmac
_url, _params, _tok = "https://x/y", {"b": "2", "a": "1"}, "tok"
_data = _url + "".join(f"{k}{_params[k]}" for k in sorted(_params))
_good = base64.b64encode(hmac.new(_tok.encode(), _data.encode(), hashlib.sha1).digest()).decode()
check("valid signature accepted", tcm.valid_signature(_url, _params, _good, _tok) is True)

print(f"==== {_passed} passed, {_failed} failed ====")
sys.exit(1 if _failed else 0)
