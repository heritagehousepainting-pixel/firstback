"""Phase 5c UI -- screening endpoints + surfaces. Run: python3 test_screening_ui.py

Proves the owner-facing half of F07 graduation: the "This was real" rescue endpoint
(trusts the number + records the false-positive that defers graduation + re-engages, never
double-texting, never re-texting an opt-out), per-tenant sensitivity thresholds flowing
through _screen_missed_caller, the per-tenant paid-reputation gate, and the dashboard
surfaces (blocked counter + graduation cards) rendering. Throwaway temp DB, demo brain.
"""
import os
import re as _re
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""           # configured() False -> sends simulate

import triage
import reputation
import app as appmod
client = appmod.app.test_client()

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# count real send attempts so a double-text is observable
_sends = []
_orig_send = messaging.send_sms
messaging.send_sms = lambda b, to, body, **k: (_sends.append((to, body)),
                                               _orig_send(b, to, body, **k))[1]

biz = db.get_business(1)
client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
# Phase 6a D-1: seed the session CSRF token so mutating-family POSTs pass _csrf_ok().
with client.session_transaction() as _s:
    _s["csrf_token"] = "test_csrf"
CSRF = {"_csrf": "test_csrf"}


def _screened_call(number, status="screened_spam"):
    """Log a missed call with the given screen verdict; return its call id."""
    db.log_call(1, f"CA{number[-7:]}", from_number=number, to_number="+15550000000",
                missed=1, engaged=0, category="spam", screen_status=status,
                spam_score=88, screen_reasons="looks like spam", screen_mode="enforce")
    rows = db.recent_screened_calls(1, 20)
    return next(c["id"] for c in rows if c["from_number"] == number)


# --- 1) "This was real" rescue: trusts + records false-positive + re-engages once ---
RESCUE = "+15557770001"
cid = _screened_call(RESCUE)
_fp_before = (db.get_business(1).get("screening_false_positives") or 0)
_sends.clear()
r = client.post(f"/api/calls/{cid}/real", data=CSRF)
check("rescue endpoint returns ok", r.status_code == 200 and r.get_json().get("ok"))
check("rescued number is now a trusted customer contact",
      (db.get_contact(1, RESCUE) or {}).get("category") == "customer")
check("rescue incremented the false-positive counter",
      (db.get_business(1).get("screening_false_positives") or 0) == _fp_before + 1)
check("rescue texted the caller back exactly once", len(_sends) == 1 and _sends[0][0] == RESCUE)
# a second rescue tap must not re-open + re-text (thread now exists)
_sends.clear()
client.post(f"/api/calls/{cid}/real", data=CSRF)
check("a second rescue tap does not double-text", len(_sends) == 0)

# --- 2) rescue resets the graduation window (the safety-valve seam) ---
_ws = db.get_business(1).get("screening_window_start")
check("rescue reset the observation window (graduation deferred)", bool(_ws))

# --- 3) opted-out number can't be rescued ---
OPT = "+15557770002"
cid_opt = _screened_call(OPT)
db.set_opt_out(1, OPT, source="test")
_sends.clear()
r = client.post(f"/api/calls/{cid_opt}/real", data=CSRF)
check("an opted-out caller cannot be rescued (no re-text)",
      r.status_code == 400 and len(_sends) == 0)

# --- 4) per-tenant thresholds flow through _screen_missed_caller ---
# A caller flagged by 2 OTHER businesses scores 40 (crowd) -> 'review' band by default
# (hard=80). With a per-tenant hard of 35, the same 40 becomes 'screened_spam'.
SCORED = "+15558880003"
b2 = db.create_business({"name": "B2"}); b3 = db.create_business({"name": "B3"})
db.add_spam_flag(b2, SCORED); db.add_spam_flag(b3, SCORED)
with appmod.app.test_request_context("/"):
    v_default = appmod._screen_missed_caller(db.get_business(1), SCORED)
_c = db.get_conn(); _c.execute("UPDATE businesses SET screen_hard=35, screen_mid=20 WHERE id=1")
_c.commit(); _c.close()
with appmod.app.test_request_context("/"):
    v_strict = appmod._screen_missed_caller(db.get_business(1), SCORED)
check("default thresholds: a crowd=2 caller is NOT hard-screened",
      v_default["status"] != "screened_spam")
check("a stricter per-tenant screen_hard flips the same caller to screened_spam",
      v_strict["status"] == "screened_spam")
# reset thresholds for the next check
_c = db.get_conn(); _c.execute("UPDATE businesses SET screen_hard=NULL, screen_mid=NULL WHERE id=1")
_c.commit(); _c.close()

# --- 5) per-tenant paid-reputation gate ---
_lookups = []
reputation.configured = lambda: True
reputation.lookup = lambda n: (_lookups.append(n), {})[1]
REPCALLER = "+15559990004"
b4 = db.create_business({"name": "B4"}); b5 = db.create_business({"name": "B5"})
db.add_spam_flag(b4, REPCALLER); db.add_spam_flag(b5, REPCALLER)   # crowd=2 -> ambiguous band
_c = db.get_conn(); _c.execute("UPDATE businesses SET reputation_enabled=0 WHERE id=1")
_c.commit(); _c.close()
_lookups.clear()
with appmod.app.test_request_context("/"):
    appmod._screen_missed_caller(db.get_business(1), REPCALLER)
check("reputation toggle OFF: no paid lookup even when provider configured", len(_lookups) == 0)
_c = db.get_conn(); _c.execute("UPDATE businesses SET reputation_enabled=1 WHERE id=1")
_c.commit(); _c.close()
_lookups.clear()
with appmod.app.test_request_context("/"):
    appmod._screen_missed_caller(db.get_business(1), REPCALLER)
check("reputation toggle ON: paid lookup fires in the ambiguous band", len(_lookups) == 1)

# --- 6) cockpit surfaces render (blocked counter + shield card) ---
# The screened-calls strip + Spam Shield card live on /pipeline (the manual cockpit),
# not the conversational command center at /dashboard. biz 1 was rescued above, so its
# observation window is open -> the "Learning" shield card should show.
d = client.get("/pipeline")
html = d.get_data(as_text=True)
check("cockpit renders 200 with the screening surfaces", d.status_code == 200)
check("cockpit exposes the Spam Shield card", "Spam Shield" in html)

# --- 7) Phase 6a D-1: CSRF guard on the mutating API family ---
# A logged-in owner whose POST carries no _csrf (or a wrong one) is rejected 403 BEFORE
# any state change -- defends the rescue/engage/flag-spam family against cross-site POSTs.
_csrf_cid = _screened_call("+15557770007")
check("D-1 /real without _csrf -> 403",
      client.post(f"/api/calls/{_csrf_cid}/real").status_code == 403)
check("D-1 /real with a WRONG _csrf -> 403",
      client.post(f"/api/calls/{_csrf_cid}/real", data={"_csrf": "nope"}).status_code == 403)
check("D-1 /engage without _csrf -> 403",
      client.post(f"/api/calls/{_csrf_cid}/engage").status_code == 403)
check("D-1 /calls/<id>/flag-spam without _csrf -> 403",
      client.post(f"/api/calls/{_csrf_cid}/flag-spam").status_code == 403)
_csrf_lead = db.create_lead(1, "Csrf Lead", "+15557770008")
check("D-1 /leads/<id>/flag-spam without _csrf -> 403",
      client.post(f"/api/leads/{_csrf_lead}/flag-spam").status_code == 403)
# The CSRF guard fires WITHOUT mutating: the number is not blocked by the rejected call.
check("D-1 rejected flag-spam did NOT block the number",
      (db.get_contact(1, "+15557770008") or {}).get("category") != "blocked")

# --- 8) Phase 6a D-4: MAX_CONTENT_LENGTH caps oversize bodies at 413 ---
# The global ceiling is 6 MB (just above the 5 MB /api/contacts/import limit). A body
# past it is rejected before the handler runs.
_big = "x" * (6 * 1024 * 1024 + 1024)   # 6 MB + 1 KB, over the cap
check("D-4 an over-6MB POST body is rejected 413",
      client.post(f"/api/calls/{_csrf_cid}/real",
                  data={"_csrf": "test_csrf", "pad": _big}).status_code == 413)

# --- 9) D-5 regression: mark_call_engaged with a mismatched business_id is a no-op ---
_eng_cid = _screened_call("+15557770009")
db.mark_call_engaged(_eng_cid, None, business_id=2)   # wrong tenant
check("D-5 mark_call_engaged(wrong tenant) does NOT flip engaged",
      (db.get_call(_eng_cid, 1) or {}).get("engaged") == 0)
db.mark_call_engaged(_eng_cid, None, business_id=1)   # correct tenant
check("D-5 mark_call_engaged(correct tenant) flips engaged",
      (db.get_call(_eng_cid, 1) or {}).get("engaged") == 1)

# --- 10) D-3 regression: set_confirm_result with a mismatched business_id is a no-op ---
db.issue_confirm_token(1, "tok_d3_test", "text_lead", {"message": "hi"}, "hash")
db.set_confirm_result("tok_d3_test", '{"leak":true}', business_id=2)   # wrong tenant
check("D-3 set_confirm_result(wrong tenant) did NOT write result_json",
      not (db.get_confirm_token(1, "tok_d3_test") or {}).get("result_json"))
db.set_confirm_result("tok_d3_test", '{"ok":true}', business_id=1)     # correct tenant
check("D-3 set_confirm_result(correct tenant) wrote result_json",
      bool((db.get_confirm_token(1, "tok_d3_test") or {}).get("result_json")))

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
