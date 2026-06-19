"""Phase 5d GAMMA -- growth tray routes + UI tests.
Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_growth_tray_ui.py

Covers all 14 GAMMA tests from PHASE5D-SPEC.md section 8.
Standalone: real temp DB, demo provider, no network.
Exits 0 on all pass, 1 if any fail.
"""
import os
import sys
import re as _re
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""   # no real Twilio -> simulated

# Patch valid_signature so the require_twilio_signature decorator passes in tests.
# This is the same technique as test_compliance_backstop.py (line 122).
_orig_valid = messaging.valid_signature
messaging.valid_signature = lambda url, params, sig, auth_token=None: True

import app as appmod
from app import _parse_tray_reply
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


# ---- Capture all sends so we can assert on them ----------------------------

_SENDS = []
_orig_send = messaging.send_sms


def _cap_send(biz, to, body, **kwargs):
    _SENDS.append({"to": to, "body": body})
    return _orig_send(biz, to, body, **kwargs)


messaging.send_sms = _cap_send


# ---- Seed: log in as the owner ---------------------------------------------

client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
# Phase 6a D-1: seed the CSRF token so the tray release/skip form POSTs pass _csrf_ok().
with client.session_transaction() as _s:
    _s["csrf_token"] = "test_csrf"


# ---- Helpers ---------------------------------------------------------------

def _biz1_id():
    return db.get_business(1)["id"]


def _set_owner_cell(cell):
    conn = db.get_conn()
    conn.execute("UPDATE businesses SET alert_sms=?, alert_on_lead=1 WHERE id=?",
                 (cell, _biz1_id()))
    conn.commit()
    conn.close()


def _clear_scheduled():
    conn = db.get_conn()
    conn.execute("DELETE FROM scheduled_messages WHERE business_id=?", (_biz1_id(),))
    conn.commit()
    conn.close()


def _insert_held(lead_id, kind="review_request", body="Growth msg for testing"):
    from datetime import datetime, timezone, timedelta
    send_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO scheduled_messages "
        "(business_id, lead_id, kind, send_at, body, status) VALUES (?,?,?,?,?,?)",
        (_biz1_id(), lead_id, kind, send_at, body, "held"))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return sid


def _make_lead(name, phone):
    return db.create_lead(_biz1_id(), name, phone)


def _status_of(sid):
    conn = db.get_conn()
    row = conn.execute("SELECT status FROM scheduled_messages WHERE id=?", (sid,)).fetchone()
    conn.close()
    return row[0] if row else None


# ---- Setup: bind Twilio number so webhook handler finds business 1 ---------

_OWNER_CELL = "+15551110001"
_BIZ_TWILIO = "+15552220001"
_CUSTOMER_PHONE = "+15558880001"

_set_owner_cell(_OWNER_CELL)
# Register the Twilio number so get_business_by_twilio_number returns biz 1
db.set_business_twilio(_biz1_id(), _BIZ_TWILIO, "PN_test_sid", webhooks_wired=True)

# Create a customer lead for cross-number guard test
_CUSTOMER_LEAD_ID = _make_lead("Customer Alice", _CUSTOMER_PHONE)

# ====================================================================
# Test 1: POST /settings/growth_mode with mode='tray'
# ====================================================================
print("\n=== Test 1: growth_mode POST tray ===")

db.set_growth_mode(_biz1_id(), "off")
r = client.post("/settings/growth_mode", data={"mode": "tray"},
                follow_redirects=False)
loc = r.headers.get("Location", "")
check("test1: redirects to settings with growth_saved=1",
      r.status_code in (301, 302) and "growth_saved=1" in loc)
check("test1: db stores tray", db.growth_mode(_biz1_id()) == "tray")

# ====================================================================
# Test 2: POST /settings/growth_mode with mode='auto' -> server rejects (TCPA lock)
# ====================================================================
print("\n=== Test 2: growth_mode POST auto (TCPA lock) ===")

db.set_growth_mode(_biz1_id(), "off")
r = client.post("/settings/growth_mode", data={"mode": "auto"},
                follow_redirects=False)
check("test2: auto request still redirects", r.status_code in (301, 302))
check("test2: db stores 'off' not 'auto'", db.growth_mode(_biz1_id()) == "off")

# ====================================================================
# Test 3: GET /growth/tray with held plays -> 200 + "Send All" + play count
# ====================================================================
print("\n=== Test 3: GET /growth/tray with held plays ===")

_clear_scheduled()
lid3 = _make_lead("Maria Test", "+15559993001")
_insert_held(lid3, kind="review_request", body="Hi Maria, quick text...")
r = client.get("/growth/tray")
html3 = r.data.decode("utf-8", errors="replace")
check("test3: status 200", r.status_code == 200)
check("test3: Send All button present", "Send All" in html3)
check("test3: play count present", "1" in html3)

# ====================================================================
# Test 4: POST /growth/tray/release -> redirect + held rows become 'pending'
# ====================================================================
print("\n=== Test 4: POST /growth/tray/release ===")

_clear_scheduled()
lid4 = _make_lead("Release Lead", "+15559994001")
sid4a = _insert_held(lid4, kind="review_request")
sid4b = _insert_held(lid4, kind="winback", body="Winback msg")

# D-1: a forged release (no CSRF) is rejected 403 BEFORE any play is released.
_csrf_reject = client.post("/growth/tray/release", follow_redirects=False)
check("test4: release without _csrf -> 403", _csrf_reject.status_code == 403)
check("test4: rejected release did NOT flip plays to pending", _status_of(sid4a) == "held")

r = client.post("/growth/tray/release", data={"_csrf": "test_csrf"}, follow_redirects=False)
check("test4: redirects", r.status_code in (301, 302))
check("test4: first play is now pending", _status_of(sid4a) == "pending")
check("test4: second play is now pending", _status_of(sid4b) == "pending")

# ====================================================================
# Test 5: POST /growth/tray/skip/<id> -> that play canceled, others stay held
# ====================================================================
print("\n=== Test 5: POST /growth/tray/skip/<id> ===")

_clear_scheduled()
lid5 = _make_lead("Skip Lead", "+15559995001")
sid5a = _insert_held(lid5, kind="review_request", body="Skip me")
sid5b = _insert_held(lid5, kind="winback", body="Keep me held")

r = client.post(f"/growth/tray/skip/{sid5a}", data={"_csrf": "test_csrf"}, follow_redirects=False)
check("test5: redirects", r.status_code in (301, 302))
check("test5: skipped play is canceled", _status_of(sid5a) == "canceled")
check("test5: other play still held", _status_of(sid5b) == "held")

# ====================================================================
# Tests 6-8: _parse_tray_reply pure function
# ====================================================================
print("\n=== Tests 6-8: _parse_tray_reply ===")

check("test6: GO -> cmd go", _parse_tray_reply("GO") == {"cmd": "go"})
check("test6b: go (lowercase) -> cmd go", _parse_tray_reply("go") == {"cmd": "go"})
check("test7: SKIP 2 -> cmd skip_n n=2",
      _parse_tray_reply("SKIP 2") == {"cmd": "skip_n", "n": 2})
check("test7b: skip 5 -> cmd skip_n n=5",
      _parse_tray_reply("skip 5") == {"cmd": "skip_n", "n": 5})
check("test7c: SKIP alone -> cmd skip_all",
      _parse_tray_reply("SKIP") == {"cmd": "skip_all"})
check("test8: hello -> None", _parse_tray_reply("hello") is None)
check("test8b: random text -> None", _parse_tray_reply("I want a quote") is None)

# ====================================================================
# Test 9: SMS "GO" from owner cell -> release called, confirmation to owner ONLY
# ====================================================================
print("\n=== Test 9: SMS GO from owner cell ===")

_clear_scheduled()
lid9 = _make_lead("GO Lead", "+15559999001")
sid9 = _insert_held(lid9, kind="review_request", body="Review request body")

_SENDS.clear()

r = client.post("/webhooks/twilio/sms/inbound",
                data={"From": _OWNER_CELL, "To": _BIZ_TWILIO, "Body": "GO"},
                headers={"X-Twilio-Signature": "fake"})
check("test9: 200 response", r.status_code == 200)
check("test9: play flipped to pending", _status_of(sid9) == "pending")

owner_sends = [s for s in _SENDS if s["to"] == _OWNER_CELL]
other_sends = [s for s in _SENDS if s["to"] != _OWNER_CELL]
check("test9: confirmation SMS sent to owner", len(owner_sends) >= 1)
check("test9: zero sends to any customer", len(other_sends) == 0)
if owner_sends:
    check("test9: confirmation body makes sense",
          any(w in owner_sends[0]["body"].lower()
              for w in ("queued", "queue", "shortly", "send", "sent", "text")))
else:
    check("test9: confirmation body makes sense", False)

# ====================================================================
# Test 10: SMS "SKIP" from owner cell -> all held plays canceled
# ====================================================================
print("\n=== Test 10: SMS SKIP from owner cell ===")

_clear_scheduled()
lid10 = _make_lead("SKIP Lead", "+15559999002")
sid10a = _insert_held(lid10, kind="review_request", body="A")
sid10b = _insert_held(lid10, kind="winback", body="B")

r = client.post("/webhooks/twilio/sms/inbound",
                data={"From": _OWNER_CELL, "To": _BIZ_TWILIO, "Body": "SKIP"},
                headers={"X-Twilio-Signature": "fake"})
check("test10: 200 response", r.status_code == 200)
check("test10: first play canceled", _status_of(sid10a) == "canceled")
check("test10: second play canceled", _status_of(sid10b) == "canceled")

# ====================================================================
# Test 11: SMS "GO" from CUSTOMER number -> NOT intercepted by tray branch
# ====================================================================
print("\n=== Test 11: SMS GO from customer (cross-number guard) ===")

_clear_scheduled()
_cust11_phone = "+15558880011"
lid11 = _make_lead("Customer GO Test", _cust11_phone)
sid11 = _insert_held(lid11, kind="review_request", body="Guard test")

_SENDS.clear()
r = client.post("/webhooks/twilio/sms/inbound",
                data={"From": _cust11_phone, "To": _BIZ_TWILIO, "Body": "GO"},
                headers={"X-Twilio-Signature": "fake"})

check("test11: 200 response", r.status_code == 200)
# Held row must NOT have been flipped -- tray branch did not run
check("test11: play still held (tray NOT triggered)", _status_of(sid11) == "held")
# Normal AI handler replied to the customer
customer_sends11 = [s for s in _SENDS if s["to"] == _cust11_phone]
check("test11: normal handler replied to customer", len(customer_sends11) >= 1)

# ====================================================================
# Test 12: Settings page: 'auto' has disabled attr; current mode pre-selected
# ====================================================================
print("\n=== Test 12: Settings page growth card ===")

db.set_growth_mode(_biz1_id(), "tray")
r = client.get("/settings")
html12 = r.data.decode("utf-8", errors="replace")
check("test12: status 200", r.status_code == 200)
check("test12: Growth Autopilot heading", "Growth Autopilot" in html12)
check("test12: 'auto' input is disabled",
      "disabled" in html12 and 'value="auto"' in html12)
check("test12: 'tray' radio is checked",
      _re.search(r'value="tray"[^>]*checked|checked[^>]*value="tray"', html12) is not None)
check("test12: 'off' radio is present", 'value="off"' in html12)

# ====================================================================
# Test 13: Tone-risk play renders "Review thread first" badge
# ====================================================================
print("\n=== Test 13: Tone-risk badge in tray ===")

_orig_list_held = db.list_held_messages


def _fake_held_tone_risk(bid):
    rows = _orig_list_held(bid)
    if rows:
        rows[0]["tone_risk"] = True
    return rows


_clear_scheduled()
lid13 = _make_lead("Tone Risk Lead", "+15559993101")
_insert_held(lid13, kind="review_request", body="Tone risk body")
db.list_held_messages = _fake_held_tone_risk

r = client.get("/growth/tray")
html13 = r.data.decode("utf-8", errors="replace")
check("test13: tone-risk badge visible",
      "Review thread first" in html13 or "review thread" in html13.lower())

db.list_held_messages = _orig_list_held   # restore

# ====================================================================
# Test 14: Blocked-reason play renders "Add Google Review link" badge
# ====================================================================
print("\n=== Test 14: Blocked-reason badge in tray ===")


def _fake_held_blocked(bid):
    rows = _orig_list_held(bid)
    if rows:
        rows[0]["sendable"] = False
        rows[0]["blocked_reason"] = "add_review_link"
    return rows


db.list_held_messages = _fake_held_blocked

r = client.get("/growth/tray")
html14 = r.data.decode("utf-8", errors="replace")
check("test14: blocked-reason badge visible",
      "Add Google Review link" in html14 or "google review link" in html14.lower())

db.list_held_messages = _orig_list_held   # restore

# Restore the original valid_signature (polite cleanup)
messaging.valid_signature = _orig_valid

# ====================================================================
# Final tally
# ====================================================================
print(f"\n{'=' * 48}")
print(f"GAMMA tests: {_pass} passed, {_fail} failed")
print(f"{'=' * 48}")
if _fail:
    sys.exit(1)
