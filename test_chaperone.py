"""Phase 6 Pillar C -- the first-run chaperone. Run: python3 test_chaperone.py

Vic proactively walks a brand-new owner through setup, one step at a time, reading real
state (never a fake counter), and RECEDES when told or when done:
  * a fresh business needs the chaperone; the FIRST step leads with money (avg job value).
  * steps advance off real state (set the value -> the next step is the profile).
  * the seam steps are honest: forwarding is the owner's tap on the carrier, calendar is
    Google's approval screen; voice + screening are never surfaced (deferred/landmine).
  * "not now" persists a dismissal and stops the proactive trigger -- but an explicit
    "help me get set up" always still works.
  * the done-state is one quiet sentence about the briefing -- no confetti.

Throwaway temp DB + the deterministic demo brain; no network.
"""
import os
import tempfile

os.environ["RINGBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""

import connections
import assistant
import app  # noqa: F401  -- seeds business #1

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---------------------------------------------------------------------------
# Trigger + first step (money first)
# ---------------------------------------------------------------------------
check("a fresh business needs the chaperone", assistant._needs_chaperone(db.get_business(1)) is True)

out = assistant.run(db.get_business(1), "help me get set up")
check("'help me get set up' routes to the chaperone", out["meta"]["tool"] == "chaperone")
check("the first step leads with money (avg job value)",
      any(w in out["reply"].lower() for w in ("worth", "job", "dollar")))
check("the chaperone offers a 'not now' dismiss",
      any("not now" in (c.get("body", "").lower()) for c in out["cards"]))

# ---------------------------------------------------------------------------
# Advance off real state
# ---------------------------------------------------------------------------
assistant.execute(db.get_business(1), "set_avg_job_value", {"value": "2400"})
out = assistant.run(db.get_business(1), "help me get set up")
check("after the value is saved, the next step is the business profile",
      any(w in out["reply"].lower() for w in ("ein", "business name", "address", "business info")))

# ---------------------------------------------------------------------------
# Honesty of the seam steps + nothing deferred is surfaced
# ---------------------------------------------------------------------------
g = connections.golive_summary(db.get_business(1))
fwd = assistant._chaperone_step_view(db.get_business(1), "forwarding", g)[0].lower()
check("forwarding step is honest it's the owner's tap on the carrier",
      any(p in fwd for p in ("can't", "your carrier", "dial it", "leave the app")))
cal = assistant._chaperone_step_view(db.get_business(1), "calendar", g)[0].lower()
check("calendar step names Google's screen, not Vic connecting it",
      "google" in cal and ("approve" in cal or "screen" in cal or "one tap" in cal))
check("voice + screening are NOT chaperone steps (deferred / landmine)",
      "voice" not in connections._CHAPERONE_STEPS
      and "screening" not in connections._CHAPERONE_STEPS
      and "screen_mode" not in connections._CHAPERONE_STEPS)

# ---------------------------------------------------------------------------
# Recede: dismiss persists + stops the proactive trigger; explicit ask still works
# ---------------------------------------------------------------------------
out = assistant.run(db.get_business(1), "not now")
check("'not now' routes to dismiss_chaperone", out["meta"]["tool"] == "dismiss_chaperone")
check("dismiss is short, not a nag", len(out["reply"]) < 140)
check("dismiss tells the owner how to resume",
      any(p in out["reply"].lower() for p in ("help me get set up", "finish setup", "whenever")))
check("dismiss persists a timestamp",
      db.get_business(1).get("chaperone_dismissed_at") is not None)
check("after a dismiss the chaperone no longer self-triggers",
      assistant._needs_chaperone(db.get_business(1)) is False)
out = assistant.run(db.get_business(1), "help me get set up")
check("but an explicit 'help me get set up' still works after a dismiss",
      out["meta"]["tool"] == "chaperone")

# ---------------------------------------------------------------------------
# Done-state: quiet, briefing-focused, no celebration
# ---------------------------------------------------------------------------
_real_golive = connections.golive_summary
connections.golive_summary = lambda b, *a, **k: {
    "status": "setup_complete", "is_live": True, "live_verified": False,
    "done": 4, "total": 4, "current": None, "blocker": None,
    "steps": [{"key": k2, "title": k2, "state": "done"}
              for k2 in ("profile", "number", "a2p", "forwarding")]}
_real_cal = assistant.google_cal.is_connected
assistant.google_cal.is_connected = lambda bid: True
db.update_business(1, {"name": "Acme"})
db.update_a2p_profile(1, {"ein": "12-3456789", "business_address": "50 Main St"})
db.update_alert_prefs(1, {"alert_sms": "5125551234"})
out = assistant.run(db.get_business(1), "help me get set up")
check("done-state mentions the briefing (the ongoing signal)", "briefing" in out["reply"].lower())
check("done-state has no celebration ('!', congrats, amazing, great job)",
      not any(w in out["reply"].lower() for w in ("congrats", "amazing", "great job", "!")))
connections.golive_summary = _real_golive
assistant.google_cal.is_connected = _real_cal

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
