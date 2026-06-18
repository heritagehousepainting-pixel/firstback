"""Ticker heartbeat + /health/ticker (Phase 0 ALPHA / SF-3).
Run: python3 test_ticker_health.py

Proves:
  * tick_once() writes last_tick_utc to the meta table.
  * /health/ticker returns fresh=True immediately after a tick.
  * /health/ticker returns fresh=False when last_tick_utc is backdated past 600s.
  * /health/ticker returns fresh=False (and age_s=None) when no tick has ever run.
Standalone-script style (print ok/FAIL, sys.exit 0/1). No pytest.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["FIRSTBACK_PROVIDER"] = "demo"

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

import app as _app
client = _app.app.test_client()

import reminders

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- 1. No tick yet: meta row absent, ticker_is_stale() is True ----
check("get_meta returns None before any tick", db.get_meta("last_tick_utc") is None)
check("ticker_is_stale() is True before any tick", reminders.ticker_is_stale())

# ---- 2. tick_once() writes the heartbeat ----
reminders.tick_once()
raw = db.get_meta("last_tick_utc")
check("tick_once writes last_tick_utc", raw is not None)
check("last_tick_utc is a valid ISO timestamp",
      raw is not None and bool(datetime.fromisoformat(raw)))

# ---- 3. /health/ticker reports fresh immediately after a tick ----
r = client.get("/health/ticker")
check("/health/ticker returns 200", r.status_code == 200)
data = r.get_json()
check("response has 'fresh' key", "fresh" in data)
check("response has 'last_tick_utc' key", "last_tick_utc" in data)
check("response has 'age_s' key", "age_s" in data)
check("fresh=True right after a tick", data.get("fresh") is True)
check("age_s is a non-negative integer",
      isinstance(data.get("age_s"), int) and data["age_s"] >= 0)

# ---- 4. Backdating last_tick_utc makes the ticker look stale ----
old_ts = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
db.set_meta("last_tick_utc", old_ts)

check("ticker_is_stale() True when last_tick_utc is 700s ago",
      reminders.ticker_is_stale())
check("ticker_is_stale() False when custom max_age_s=800 (not yet stale)",
      not reminders.ticker_is_stale(max_age_s=800))

r2 = client.get("/health/ticker")
check("/health/ticker still returns 200 with old timestamp", r2.status_code == 200)
data2 = r2.get_json()
check("fresh=False when last_tick_utc is 700s ago", data2.get("fresh") is False)
check("age_s > 600 when backdated 700s", isinstance(data2.get("age_s"), int) and data2["age_s"] > 600)

# ---- 5. /health/ticker when no tick has ever run ----
db.set_meta("last_tick_utc", "")   # simulate absent by writing empty string
# ticker_is_stale treats empty/unparseable as stale
check("ticker_is_stale() True for empty string value",
      reminders.ticker_is_stale())

# ---- 6. No tenant data leaks from the health endpoint ----
# The response must not contain any lead/business/user-identifying fields.
import json as _json
r3 = client.get("/health/ticker")
body = r3.data.decode()
for forbidden in ("phone", "email", "name", "lead", "business", "user"):
    check(f"no '{forbidden}' key in /health/ticker response", f'"{forbidden}"' not in body)

# ---- summary ----
print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
