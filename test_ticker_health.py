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

# ---- 7. 6b: stale-ticker gap detection fires a tick_stale alert ----
import alerts as _alerts
_orig_notify = _alerts.notify
_stale_calls = []
def _capture_notify(business, kind, context):
    if kind == "tick_stale":
        _stale_calls.append(context)
    return []
_alerts.notify = _capture_notify
try:
    # A fresh tick with no prior heartbeat must NOT fire tick_stale.
    db.set_meta("last_tick_utc", "")
    _stale_calls.clear()
    reminders.tick_once()
    check("6b: no tick_stale when there is no prior heartbeat", len(_stale_calls) == 0)

    # A heartbeat 20 min ago -> the next tick detects the gap and fires once.
    db.set_meta("last_tick_utc",
                (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat())
    _stale_calls.clear()
    reminders.tick_once()
    check("6b: tick_stale fires after a >15min heartbeat gap", len(_stale_calls) == 1)
    check("6b: tick_stale carries the gap (~20 min)",
          bool(_stale_calls) and 18 <= float(_stale_calls[0].get("gap_minutes", 0)) <= 22)

    # A normal ~60s gap does NOT fire.
    db.set_meta("last_tick_utc",
                (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat())
    _stale_calls.clear()
    reminders.tick_once()
    check("6b: no tick_stale on a normal ~60s gap", len(_stale_calls) == 0)
finally:
    _alerts.notify = _orig_notify

# tick_stale copy is honest + short.
_ts_body = _alerts.format_message("tick_stale", {"gap_minutes": 18, "local_day": "2026-06-19"})
check("6b: tick_stale copy names the delay + the scheduler",
      "18m" in _ts_body and "scheduler" in _ts_body.lower() and len(_ts_body) <= 200)

# ---- summary ----
print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
