"""Batch A / Tier-0 fixes -- regression tests.
Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_tier0_fixes.py

  F1  voice honesty: _missed_call_textback returns True ONLY when a text actually went out;
      the voice route says "be in touch soon" (not "we sent you a text") when it didn't.
  F2  EIN no longer browser-required; signup has_ein -> business_type llc / sole_prop.
  F3  the typed phone carries through signup -> alert_sms (+ hidden field on GET ?phone=).
  F6  auth page no longer shows a self-review 5-star block.
  F7  solutions page no longer claims "live AI voice" present-tense (says coming soon).
  F5  marketing demo CTAs point at the public /demo, not the login-walled /simulator.
  F8  A2P approval fires the "a2p_approved" go-live owner alert.
  F9  the alert_on_roi_milestone toggle persists (whitelist fix).
"""
import os
import sys
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
db.init_db()
import messaging
messaging.TWILIO_ACCOUNT_SID = ""
import app as appmod
import alerts
import connections

client = appmod.app.test_client()
_pass = _fail = 0
def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1; print(f"  ok   {name}")
    else:
        _fail += 1; print(f"FAIL   {name}")


# ===========================================================================
# F1 -- voice honesty
# ===========================================================================
print("\n=== F1: voice text-back honesty ===")
biz1 = db.get_business(1)
_o_screen = appmod._screen_missed_caller
_o_open = appmod.open_conversation
_o_send = messaging.send_sms
appmod._screen_missed_caller = lambda b, c: {"engage": True, "status": "prospect",
    "score": 0, "category": "prospect", "reasons": []}
appmod.open_conversation = lambda b, l: "Hi! Thanks for calling -- happy to help."
try:
    messaging.send_sms = lambda b, to, body, **k: {"status": "blocked", "reason": "a2p_not_approved"}
    r_blocked = appmod._missed_call_textback(biz1, "+14155550101", "CA1", "no-forward")
    check("F1: returns False when the send is BLOCKED (A2P wait -> no text went out)",
          r_blocked is False)
    messaging.send_sms = lambda b, to, body, **k: {"status": "sent", "sid": "SM1"}
    r_sent = appmod._missed_call_textback(biz1, "+14155550102", "CA2", "no-forward")
    check("F1: returns True when the send actually went out", r_sent is True)
finally:
    appmod._screen_missed_caller = _o_screen
    appmod.open_conversation = _o_open
    messaging.send_sms = _o_send


# ===========================================================================
# F2 -- EIN not required + has_ein -> business_type
# ===========================================================================
print("\n=== F2: EIN gate + business_type ===")
# setup.html no longer hard-requires EIN: the rendered field has no required attr.
# (Render via a logged-in /setup is heavier; assert the template source instead.)
_setup_src = open("templates/setup.html").read()
check("F2: setup.html EIN field is not browser-required",
      "name='ein'" in _setup_src and "required=true, help='Required by U.S. carriers" not in _setup_src)
r_llc = client.post("/signup", data={"email": "llc@example.com", "password": "passpass12",
    "business": "LLC Co", "owner": "Pat", "has_ein": "1"})
_b_llc = db.get_business_by_owner_email("llc@example.com") if hasattr(db, "get_business_by_owner_email") else None
def _biz_for_email(email):
    c = db.get_conn()
    row = c.execute("SELECT b.* FROM businesses b JOIN users u ON u.business_id=b.id "
                    "WHERE u.email=?", (email,)).fetchone()
    c.close()
    return dict(row) if row else None
check("F2: signup with has_ein=1 sets business_type='llc'",
      (_biz_for_email("llc@example.com") or {}).get("business_type") == "llc")
client.get("/logout")
client.post("/signup", data={"email": "sole@example.com", "password": "passpass12",
    "business": "Sole Co", "owner": "Sam"})
check("F2: signup without has_ein sets business_type='sole_prop'",
      (_biz_for_email("sole@example.com") or {}).get("business_type") == "sole_prop")
client.get("/logout")


# ===========================================================================
# F3 -- the typed phone carries through signup -> alert_sms
# ===========================================================================
print("\n=== F3: phone wired through signup ===")
_html = client.get("/signup?phone=5551234567").get_data(as_text=True)
check("F3: GET /signup?phone= renders the hidden phone field populated",
      'name="phone" value="5551234567"' in _html)
client.post("/signup", data={"email": "phone@example.com", "password": "passpass12",
    "business": "Phone Co", "owner": "Lee", "phone": "+14155559000"})
check("F3: signup with a phone sets the business alert_sms",
      (_biz_for_email("phone@example.com") or {}).get("alert_sms") == "+14155559000")
client.get("/logout")


# ===========================================================================
# F6 / F7 / F5 -- honesty + demo CTA on public pages
# ===========================================================================
print("\n=== F6/F7/F5: public-page honesty ===")
_auth = client.get("/signup").get_data(as_text=True)
check("F6: auth page no longer shows a 5-star self-review", 'aria-label="5 out of 5"' not in _auth)
_sol = client.get("/solutions").get_data(as_text=True)
check("F7: solutions no longer claims present-tense 'live AI voice'", "live AI voice" not in _sol)
check("F7: solutions hedges voice as coming soon",
      "coming soon" in _sol.lower())
_home = client.get("/").get_data(as_text=True)
check("F5: a marketing page links the public /demo (not /simulator)",
      "/demo" in _home and 'href="/simulator"' not in _home)


# ===========================================================================
# F8 -- A2P approval fires the go-live alert
# ===========================================================================
print("\n=== F8: A2P-approved go-live alert ===")
check("F8: 'a2p_approved' is a registered alert kind", "a2p_approved" in alerts.ALERT_KINDS)
_copy = alerts.format_message("a2p_approved", {})
check("F8: a2p_approved copy says they're live", "live" in _copy.lower())
# Drive the pending->approved transition.
db.set_a2p_status(1, "pending")
_o_fetch = messaging.fetch_a2p_campaign_status
_o_notify = alerts.notify_async
_o_flush = connections.flush_blocked_sends
_fired = []
messaging.fetch_a2p_campaign_status = lambda *a, **k: "APPROVED"
alerts.notify_async = lambda biz, kind, ctx: _fired.append(kind)
connections.flush_blocked_sends = lambda bid: None
try:
    biz1b = db.get_business(1)
    biz1b["a2p_messaging_service_sid"] = "MG1"; biz1b["a2p_campaign_sid"] = "CMP1"
    connections.a2p_sync(biz1b)
    check("F8: pending->approved fires notify_async('a2p_approved')", "a2p_approved" in _fired)
    # Idempotent: a second sync (already approved) does not re-fire.
    _fired.clear()
    connections.a2p_sync(db.get_business(1))
    check("F8: a second sync (already approved) does NOT re-fire", "a2p_approved" not in _fired)
finally:
    messaging.fetch_a2p_campaign_status = _o_fetch
    alerts.notify_async = _o_notify
    connections.flush_blocked_sends = _o_flush


# ===========================================================================
# F9 -- alert_on_roi_milestone toggle persists (whitelist fix)
# ===========================================================================
print("\n=== F9: roi_milestone toggle persists ===")
db.update_alert_prefs(1, {"alert_on_roi_milestone": 0})
check("F9: update_alert_prefs can set alert_on_roi_milestone=0",
      (db.get_business(1) or {}).get("alert_on_roi_milestone") == 0)
db.update_alert_prefs(1, {"alert_on_roi_milestone": 1})
check("F9: update_alert_prefs can set alert_on_roi_milestone=1 (whitelisted)",
      (db.get_business(1) or {}).get("alert_on_roi_milestone") == 1)


print(f"\n{'='*46}")
print(f"Results: {_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
