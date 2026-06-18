"""Phase 1 C — compliance backstop tests.  Run: python3 test_compliance_backstop.py

Tests:
  1. send_sms during quiet hours returns "deferred" (not "failed").
  2. send_sms during quiet hours with transactional=True is NOT deferred.
  3. STOP then START round-trips: opted-out -> re-subscribed.
  4. STOP still suppresses (is_suppressed stays True after STOP, before START).
  5. The inbound START branch re-subscribes via the app route (HTTP level).

Standalone-script style: print ok/FAIL, sys.exit non-zero on any failure.
No network (Twilio mocked).
"""
import os, sys, tempfile
from datetime import datetime

# ---- Throwaway DB + demo brain ----
os.environ.setdefault("FIRSTBACK_PROVIDER", "demo")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")
os.environ.setdefault("FIRSTBACK_PUBLIC_URL", "https://example.com")
os.environ.pop("FIRSTBACK_HTTPS", None)
os.environ.pop("FIRSTBACK_ENV", None)
os.environ.setdefault("FIRSTBACK_OWNER_PASSWORD", "testseedpw123")
# Set quiet hours explicitly so the test is deterministic.
os.environ["QUIET_START"] = "8"
os.environ["QUIET_END"] = "21"

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()

import config
config.DB_PATH = _TMP.name
config.QUIET_START = 8
config.QUIET_END = 21

import db
db.DB_PATH = _TMP.name

import messaging, tc_messaging, app as _app
client = _app.app.test_client()

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

# ============================================================
# 1. send_sms during quiet hours → "deferred"
# ============================================================
# Patch tc_messaging.quiet_blocked to always return True for this test.
_orig_quiet_blocked = tc_messaging.quiet_blocked
tc_messaging.quiet_blocked = lambda now, start, end, transactional=False: (not transactional)

# Patch messaging's tc_messaging reference too (it imported tc_messaging at module level).
import messaging as _msg_mod
_orig_msg_quiet = _msg_mod.tc_messaging
_msg_mod.tc_messaging = tc_messaging  # already the same object; patches are live

result_deferred = messaging.send_sms(biz, "+15550001111", "Hello after hours")
check("send_sms during quiet hours returns status='deferred'",
      result_deferred.get("status") == "deferred")
check("send_sms deferred reason is 'quiet_hours'",
      result_deferred.get("reason") == "quiet_hours")

# ============================================================
# 2. transactional=True bypasses quiet-hours gate
# ============================================================
result_txn = messaging.send_sms(biz, "+15550001111", "Immediate reply",
                                 transactional=True)
check("transactional send is NOT deferred during quiet hours",
      result_txn.get("status") != "deferred")

# ============================================================
# 3. gate=False (owner alert) bypasses quiet-hours gate
# ============================================================
result_alert = messaging.send_sms(biz, "+15550001111", "Owner alert",
                                   gate=False)
check("owner alert (gate=False) is NOT deferred during quiet hours",
      result_alert.get("status") != "deferred")

# Restore quiet_blocked.
tc_messaging.quiet_blocked = _orig_quiet_blocked

# ============================================================
# 4. STOP → is_suppressed True; START → is_suppressed False (round-trip)
# ============================================================
CONSUMER = "+15550002222"

db.set_opt_out(1, CONSUMER, source="sms-stop")
check("STOP: is_suppressed is True after set_opt_out", db.is_suppressed(1, CONSUMER))

db.set_opt_in(1, CONSUMER, source="sms-start")
check("START: is_suppressed is False after set_opt_in", not db.is_suppressed(1, CONSUMER))

# ============================================================
# 5. STOP still suppresses a re-opted-out number
# ============================================================
db.set_opt_out(1, CONSUMER, source="sms-stop-again")
check("second STOP: is_suppressed True again", db.is_suppressed(1, CONSUMER))

# ============================================================
# 6. Inbound START branch via the app: HTTP-level round-trip
# ============================================================
# Manufacture an inbound SMS from a suppressed number with body "START".
# We need a Twilio-signed request. The @require_twilio_signature decorator
# trusts the signature when Twilio creds are set; we'll short-circuit the
# decorator by patching valid_signature to return True.
import messaging as _m
_orig_valid = _m.valid_signature
_m.valid_signature = lambda url, params, sig, auth_token=None: True

# Ensure business 1 has a twilio_number so get_business_by_twilio_number works.
_biz_twilio_num = "+15559990000"
db.set_business_twilio(1, _biz_twilio_num, "PNtest", webhooks_wired=True)

# First opt out.
CONSUMER2 = "+15550003333"
db.set_opt_out(1, CONSUMER2, source="sms-stop")
check("pre-condition: CONSUMER2 is suppressed before START test", db.is_suppressed(1, CONSUMER2))

r = client.post(
    "/webhooks/twilio/sms/inbound",
    data={"From": CONSUMER2, "To": _biz_twilio_num, "Body": "START"},
    headers={"X-Twilio-Signature": "fake"}
)
check("inbound START returns 200 with TwiML", r.status_code == 200)
check("inbound START response mentions re-subscribed",
      b"re-subscribed" in r.data or b"subscribed" in r.data)
check("inbound START reverses suppression (is_suppressed=False)",
      not db.is_suppressed(1, CONSUMER2))

_m.valid_signature = _orig_valid

# ============================================================
# 7. opt_in_nlu recognizes START keyword variants
# ============================================================
import consent
for phrase in ("START", "start", "UNSTOP", "YES"):
    check(f"opt_in_nlu catches '{phrase}'", consent.opt_in_nlu(phrase))

for phrase in ("stop", "unsubscribe", "no more texts"):
    check(f"opt_out_nlu catches '{phrase}'", consent.opt_out_nlu(phrase))


# ---- Cleanup ----
try:
    os.unlink(_TMP.name)
except OSError:
    pass

print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
