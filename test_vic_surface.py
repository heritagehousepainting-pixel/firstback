"""SLICE GAMMA tests -- Vic surface (P1-5 resting pill + P1-6 enforce 2nd ack).
Run: python test_vic_surface.py

1. allow_llm=False turn -> JSON has vic_status=="resting" + parseable resets_at;
   allow_llm=True turn does NOT.
2. enforce two-tap: first redeem without enforce_ack -> warning + token NOT consumed;
   second redeem WITH enforce_ack=true -> executes.
3. Regression: plain text_lead token redeems once with no ack needed.
"""
import os
import re as _re
import json
import time
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""

import ai
import assistant
import app

client = app.app.test_client()
_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# Seed a lead and log in.
lead_id = db.create_lead(1, "Dana Homeowner", "+15551234567")
biz = db.get_business(1)

client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
html = client.get("/dashboard").get_data(as_text=True)
_cm = _re.search(r'id="csrfToken" value="([^"]+)"', html)
CSRF = _cm.group(1) if _cm else ""
check("CSRF token present", len(CSRF) >= 32)


# ---- TEST 1: vic_status resting surface ----

# Force allow_llm=False by monkeypatching _assistant_budget in app.
_orig_budget = app._assistant_budget


def _budget_resting(biz, message):
    return False, False   # allow_llm=False, not throttled


def _budget_live(biz, message):
    return True, False    # allow_llm=True, not throttled


# 1a: allow_llm=False -> vic_status=="resting" and resets_at in JSON
app._assistant_budget = _budget_resting
r = client.post("/assistant", data={"_csrf": CSRF, "message": "how many leads do I have"})
body = r.get_json() or {}
check("allow_llm=False: response has vic_status==resting",
      body.get("vic_status") == "resting")

resets_at = body.get("resets_at", "")
_parseable = False
if resets_at:
    try:
        from datetime import datetime
        datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        _parseable = True
    except Exception:
        pass
check("allow_llm=False: resets_at is a parseable ISO string", _parseable)

# 1b: allow_llm=True -> NO vic_status key (or not "resting")
app._assistant_budget = _budget_live
r2 = client.post("/assistant", data={"_csrf": CSRF, "message": "how many leads do I have"})
body2 = r2.get_json() or {}
check("allow_llm=True: vic_status NOT present or not resting",
      body2.get("vic_status") != "resting")

# Restore original budget
app._assistant_budget = _orig_budget


# ---- TEST 2: enforce two-tap ----

# Mint an enforce-mode token by running assistant directly
_sends = []
_orig_send = messaging.send_sms
def _counting_send(business, to, body, **kw):
    _sends.append({"to": to, "body": body})
    return _orig_send(business, to, body, **kw)
messaging.send_sms = _counting_send

out = assistant.run(biz, "turn on call screening enforce")
pa = out.get("pending_action")
check("set_screen_mode enforce produces a pending_action with token",
      pa is not None and pa.get("tool") == "set_screen_mode"
      and (pa.get("args") or {}).get("mode") == "enforce"
      and isinstance(pa.get("token_id"), str) and len(pa["token_id"]) >= 16)

token_enforce = pa["token_id"] if pa else ""

# First tap: no enforce_ack -> warning, token NOT consumed
_sends.clear()
r_first = client.post("/assistant/confirm", data={
    "_csrf": CSRF,
    "confirm_token": token_enforce,
})
res_first = r_first.get_json() or {}
check("enforce first tap (no ack): returns 200",
      r_first.status_code == 200)
check("enforce first tap: reply mentions 'silences' or is a warning",
      "silence" in (res_first.get("reply") or "").lower()
      or "tap again" in (res_first.get("reply") or "").lower())
check("enforce first tap: no execution (no SMS send)",
      len(_sends) == 0)

# Token must still be unconsumed
row = db.get_confirm_token(biz["id"], token_enforce)
check("enforce first tap: token still unconsumed (consumed==0)",
      row is not None and row["consumed"] == 0)

# Second tap: with enforce_ack=true -> executes
_sends.clear()
r_second = client.post("/assistant/confirm", data={
    "_csrf": CSRF,
    "confirm_token": token_enforce,
    "enforce_ack": "true",
})
res_second = r_second.get_json() or {}
check("enforce second tap (with ack): returns 200",
      r_second.status_code == 200)
check("enforce second tap: reply is not the warning",
      "tap again" not in (res_second.get("reply") or "").lower())
row_after = db.get_confirm_token(biz["id"], token_enforce)
check("enforce second tap: token is now consumed",
      row_after is not None and row_after["consumed"] == 1)

# ---- TEST 3: plain text_lead token redeems once, no ack needed ----

out2 = assistant.run(biz, "text my last lead saying check-in from Vic")
pa2 = out2.get("pending_action")
check("text_lead produces a pending_action token",
      pa2 is not None and pa2.get("tool") == "text_lead")

token_text = pa2["token_id"] if pa2 else ""

_sends.clear()
r_txt = client.post("/assistant/confirm", data={
    "_csrf": CSRF,
    "confirm_token": token_text,
})
res_txt = r_txt.get_json() or {}
check("text_lead redeems immediately (no enforce_ack needed)",
      r_txt.status_code == 200
      and "tap again" not in (res_txt.get("reply") or "").lower())
check("text_lead actually executes (send happens)",
      len(_sends) == 1)

row_text = db.get_confirm_token(biz["id"], token_text)
check("text_lead token is consumed after one redemption",
      row_text is not None and row_text["consumed"] == 1)

# Restore
messaging.send_sms = _orig_send

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
