"""Number-reputation seam checks. Run: python3 test_reputation.py

Proves Tier 2 of the phone screen behaves safely: it's a no-op until configured,
caches per number (so we never re-pay for the same lookup), and FAILS OPEN -- any
provider error returns {} so a slow/broken vendor can never silence a real caller.
No framework, no network: the provider HTTP call is monkeypatched. Exits non-zero on
any failure.
"""
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
db.init_db()

import reputation

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- off by default: no-op, no network -------------------------------------
reputation.REPUTATION_PROVIDER = "off"
check("configured() is False when no provider is set", reputation.configured() is False)
check("lookup() returns {} when off", reputation.lookup("+15551110000") == {})
check("provider_label() reads 'Off'", reputation.provider_label() == "Off")


# ---- configured: a provider verdict is returned AND cached ------------------
reputation.REPUTATION_PROVIDER = "twilio_nomorobo"
reputation.TWILIO_ACCOUNT_SID = "ACxxxx"
reputation.TWILIO_AUTH_TOKEN = "tok"
check("configured() is True once provider + creds are set", reputation.configured() is True)

_calls = {"n": 0}


def _fake_provider(number):
    _calls["n"] += 1
    return {"line_type": "nonFixedVoip", "spam_score": 100}


reputation._twilio_nomorobo = _fake_provider

res = reputation.lookup("+1 (555) 222-0000")
check("first lookup returns the provider verdict",
      res.get("spam_score") == 100 and res.get("line_type") == "nonFixedVoip"
      and res.get("from_cache") is False)
check("first lookup called the provider once", _calls["n"] == 1)

res2 = reputation.lookup("5552220000")        # same number, different formatting
check("second lookup is served FROM CACHE (no second provider call)",
      res2.get("from_cache") is True and _calls["n"] == 1)
check("cached verdict carries the same score", res2.get("spam_score") == 100)


# ---- fail-open: any provider error yields {} (never raises, never silences) -
def _boom(number):
    raise RuntimeError("vendor down / timeout")


reputation._twilio_nomorobo = _boom
check("a fresh number that errors fails OPEN ({})",
      reputation.lookup("+15559990000") == {})


# ---- cache TTL: a stale row is ignored -------------------------------------
db.set_reputation("+15558880000", line_type="mobile", spam_score=0, source="test")
check("a fresh cache row is returned within TTL",
      db.get_reputation("5558880000", max_age_hours=24) is not None)
check("a cache row older than max_age is treated as absent",
      db.get_reputation("5558880000", max_age_hours=0) is None)


print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
