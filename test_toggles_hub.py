"""Phase 6 Pillar A -- operational toggles. Run: python3 test_toggles_hub.py

Vic flips the day-to-day switches by talking, each behind the confirm gate:
  * set_reminders  -- reminders / follow-ups on/off (honest: "no NEW reminders queued").
  * set_alerts     -- owner pings on lead/booking/urgent; warns when there's no destination.
  * set_screen_mode -- off | monitor | enforce. The honesty landmine: "enforce" SILENCES
    screened callers, so its confirm must say "silenced" + steer to monitor first, and a bare
    "turn on screening" defaults to MONITOR, never enforce.
  (set_voice deferred until the voice service is deployed -- VOICE_PUBLIC_URL is empty here.)

Throwaway temp DB + the deterministic demo brain; no network.
"""
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import assistant
import app  # noqa: F401  -- seeds business #1 (with an owner user -> owner_email is set)

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


biz = db.get_business(1)

# ---------------------------------------------------------------------------
# Reminders + follow-ups
# ---------------------------------------------------------------------------
out = assistant.run(biz, "stop the reminder texts")
pa = out.get("pending_action")
check("reminders: 'stop the reminder texts' routes to a gated set_reminders",
      pa is not None and pa["tool"] == "set_reminders")
check("reminders: it does NOT misroute to text_lead",
      pa is not None and pa["tool"] == "set_reminders")
check("reminders: confirm is honest about already-queued rows (says 'new', not 'no more')",
      pa and "new" in pa["summary"].lower())

_before = db.get_business(1).get("reminders_enabled")
assistant.run(biz, "stop the reminder texts")
check("reminders: the gate does NOT write on run()",
      db.get_business(1).get("reminders_enabled") == _before)

assistant.execute(biz, "set_reminders", {"reminders_enabled": 0})
check("reminders: execute turns reminders off", db.get_business(1).get("reminders_enabled") == 0)
assistant.execute(biz, "set_reminders", {"reminders_enabled": 1})
check("reminders: execute turns reminders back on", db.get_business(1).get("reminders_enabled") == 1)

out = assistant.run(biz, "turn off lead follow-ups")
pa = out.get("pending_action")
check("followups: 'turn off lead follow-ups' gates set_reminders on the followups field",
      pa and pa["tool"] == "set_reminders" and "follow" in pa["summary"].lower())
assistant.execute(biz, "set_reminders", {"followups_enabled": 0})
check("followups: execute turns follow-ups off", db.get_business(1).get("followups_enabled") == 0)

_a2p = db.get_business(1).get("a2p_status")
assistant.execute(biz, "set_reminders", {"reminders_enabled": 0, "a2p_status": "approved"})
check("reminders: execute arg-cleans a smuggled a2p_status",
      db.get_business(1).get("a2p_status") == _a2p)

# ---------------------------------------------------------------------------
# Owner alerts
# ---------------------------------------------------------------------------
out = assistant.run(biz, "alert me when a lead comes in")
pa = out.get("pending_action")
check("alerts: 'alert me when a lead comes in' routes to a gated set_alerts",
      pa and pa["tool"] == "set_alerts")
check("alerts: with an owner email on file, the confirm does NOT warn about a missing destination",
      pa and "don't see" not in pa["summary"].lower() and "add one" not in pa["summary"].lower())

out = assistant.run(biz, "text me at 512-555-1234 when a lead comes in")
pa = out.get("pending_action")
check("alerts: a destination number routes to set_alerts (not text_lead) and shows verbatim",
      pa and pa["tool"] == "set_alerts" and "512-555-1234" in pa["summary"])

# No-destination honesty: clear the channel and confirm Vic warns instead of over-claiming.
_real_owner_email = db.owner_email
db.owner_email = lambda bid: ""
db.update_alert_prefs(1, {"alert_sms": "", "alert_email": ""})
out = assistant.run(db.get_business(1), "turn on lead alerts")
pa = out.get("pending_action")
check("alerts: with NO destination, the confirm warns instead of claiming you'll be pinged",
      pa and any(w in pa["summary"].lower() for w in ("don't see", "add one", "no phone", "no email")))
db.owner_email = _real_owner_email

assistant.execute(biz, "set_alerts", {"alert_on_lead": 1, "alert_sms": "5125551234"})
saved = db.get_business(1)
check("alerts: execute writes alert_on_lead + alert_sms",
      saved.get("alert_on_lead") == 1 and saved.get("alert_sms") == "5125551234")
assistant.execute(biz, "set_alerts", {"alert_on_lead": 0})
check("alerts: execute turns the lead alert off", db.get_business(1).get("alert_on_lead") == 0)

# ---------------------------------------------------------------------------
# Call screening mode -- the honesty landmine
# ---------------------------------------------------------------------------
out = assistant.run(biz, "turn on call screening")
pa = out.get("pending_action")
check("screen: a bare 'turn on screening' routes to set_screen_mode",
      pa and pa["tool"] == "set_screen_mode")
check("screen: a bare 'turn on screening' defaults to MONITOR, never enforce",
      pa and pa["args"].get("mode") == "monitor")

out = assistant.run(biz, "put screening in monitor mode")
pa = out.get("pending_action")
check("screen: monitor confirm does NOT claim callers are silenced/blocked",
      pa and "silenc" not in pa["summary"].lower() and "block" not in pa["summary"].lower())

out = assistant.run(biz, "switch screening to enforce")
pa = out.get("pending_action")
check("screen: enforce confirm says screened callers are SILENCED (honest about the cost)",
      pa and ("silenc" in pa["summary"].lower()
              or "won't get a text" in pa["summary"].lower()
              or "will not get a text" in pa["summary"].lower()))
check("screen: enforce confirm steers to monitor first",
      pa and "monitor" in pa["summary"].lower())

_before = db.get_business(1).get("screen_mode")
assistant.run(biz, "switch screening to enforce")
check("screen: the gate does NOT write on run()",
      db.get_business(1).get("screen_mode") == _before)

assistant.execute(biz, "set_screen_mode", {"mode": "monitor"})
check("screen: execute writes monitor", db.get_business(1).get("screen_mode") == "monitor")
assistant.execute(biz, "set_screen_mode", {"mode": "enforce"})
check("screen: execute writes enforce", db.get_business(1).get("screen_mode") == "enforce")
assistant.execute(biz, "set_screen_mode", {"mode": "off"})
check("screen: execute writes off", db.get_business(1).get("screen_mode") == "off")
assistant.execute(biz, "set_screen_mode", {"mode": "potato"})
check("screen: execute refuses a garbage mode (not stored as 'potato')",
      db.get_business(1).get("screen_mode") != "potato")

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
