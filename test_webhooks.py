"""Phase 1 Twilio-webhook integration checks. Run: python3 test_webhooks.py

Drives the real Flask routes via the test client with correctly SIGNED requests,
against a throwaway temp DB and the deterministic demo brain (no network: Twilio
stays "not configured" so sends simulate). Exits non-zero on any failure.
"""
import base64
import hashlib
import hmac
import os
import tempfile

os.environ["RINGBACK_PROVIDER"] = "demo"          # deterministic, no network
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_AUTH_TOKEN = "tok"   # require_twilio_signature validates against this
messaging.TWILIO_ACCOUNT_SID = ""     # configured() False -> send_sms simulates (no network)

import app
client = app.app.test_client()

# A tenant with a RingBack number and a forward-to cell.
BIZ_NUM, CELL, CALLER = "+15553140000", "+15559990000", "+14155551212"
db.set_business_twilio(1, BIZ_NUM, "PN1", forward_to=CELL)

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


def _sign(url, params):
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(b"tok", data.encode(), hashlib.sha1).digest()).decode()


def post(path, params):
    """Signed POST, exactly as Twilio would send it."""
    url = "http://localhost" + path
    return client.post(path, data=params,
                       headers={"X-Twilio-Signature": _sign(url, params)})


# 1. Unsigned request is rejected.
r = client.post("/webhooks/twilio/sms/inbound",
                data={"To": BIZ_NUM, "From": CALLER, "Body": "hi"})
check("unsigned webhook is rejected (403)", r.status_code == 403)

# 2. Inbound call with a cell on file -> Dial TwiML to that cell, with a fallback action.
r = post("/webhooks/twilio/voice/inbound", {"To": BIZ_NUM, "From": CALLER, "CallSid": "CA1"})
xml = r.get_data(as_text=True)
check("voice inbound returns <Dial> to the contractor cell",
      r.status_code == 200 and "<Dial" in xml and CELL in xml)
check("voice inbound Dial has a dial-status fallback action",
      "voice/dial-status" in xml)

# 3. Dial leg ends no-answer -> missed: lead created, opening text-back recorded, call logged.
r = post("/webhooks/twilio/voice/dial-status",
         {"To": BIZ_NUM, "From": CALLER, "CallSid": "CA1", "DialCallStatus": "no-answer"})
lead = db.get_lead_by_phone(1, CALLER)
check("missed call creates the caller's lead", lead is not None)
check("missed call records the opening text-back",
      bool(lead) and any(m["direction"] == "out" for m in db.get_messages(lead["id"])))
conn = db.get_conn()
call = conn.execute("SELECT * FROM calls WHERE call_sid='CA1'").fetchone()
conn.close()
check("missed call is logged with missed=1", bool(call) and call["missed"] == 1)

# 4. Inbound SMS reply -> shared engine runs; customer message recorded on the same thread.
r = post("/webhooks/twilio/sms/inbound",
         {"To": BIZ_NUM, "From": CALLER, "Body": "I need my kitchen painted", "MessageSid": "SM1"})
check("inbound SMS returns 200 TwiML", r.status_code == 200 and "<Response" in r.get_data(as_text=True))
lead = db.get_lead_by_phone(1, CALLER)
ins = [m for m in db.get_messages(lead["id"]) if m["direction"] == "in"]
check("inbound SMS attaches to the caller's existing lead + records it",
      any("kitchen" in (m["body"] or "") for m in ins))

# 5. STOP -> opt out + a single confirmation.
r = post("/webhooks/twilio/sms/inbound", {"To": BIZ_NUM, "From": CALLER, "Body": "STOP", "MessageSid": "SM2"})
check("STOP opts the contact out", db.is_suppressed(1, CALLER) is True)
check("STOP returns an unsubscribe confirmation", "unsubscrib" in r.get_data(as_text=True).lower())

# 6. Unknown tenant number -> reject the call (don't route a stranger's number).
r = post("/webhooks/twilio/voice/inbound", {"To": "+19998887777", "From": CALLER, "CallSid": "CA2"})
check("voice inbound rejects an unknown tenant number", "<Reject" in r.get_data(as_text=True))

# 7. Delivery-status webhook reconciles a sent message by its provider sid.
db.add_message(lead["id"], "out", "x", provider_sid="SMxyz")
post("/webhooks/twilio/sms/status",
     {"MessageSid": "SMxyz", "MessageStatus": "delivered", "To": BIZ_NUM, "From": CALLER})
conn = db.get_conn()
row = conn.execute("SELECT delivery_status FROM messages WHERE provider_sid='SMxyz'").fetchone()
conn.close()
check("status webhook records delivery status", bool(row) and row["delivery_status"] == "delivered")


# 8. H1 A2P GATE on the real webhook path (audit-coverage close): a tenant that owns
# a RingBack number but is NOT a2p-approved must, when configured, record the outbound
# text-back on the thread for PARITY yet send NOTHING to the network (the gate blocks
# it). Once approved, the same path performs a real send. We spy on requests.post so a
# "blocked" result is provably one that never touched the wire (the tripwire stays in
# force -- any unstubbed call would raise).
import requests as _h1_requests

class _H1PostSpy:
    def __init__(self): self.calls = []
    def __call__(self, url, *a, **kw):
        self.calls.append((url, a, kw))
        class _R:
            def raise_for_status(self): pass
            def json(self): return {"sid": "SM_h1_ok"}
        return _R()

_h1_saved_post = _h1_requests.post
_h1_saved_sid = messaging.TWILIO_ACCOUNT_SID
_h1_saved_tok = messaging.TWILIO_AUTH_TOKEN
_h1_saved_from = messaging.TWILIO_FROM_NUMBER

# A fresh tenant on its own number + cell, configured but a2p UNREGISTERED (default).
H1_NUM, H1_CALLER = "+15553149999", "+14155557777"
_h1_biz_id = db.create_business({"name": "H1 Co"})
db.set_business_twilio(_h1_biz_id, H1_NUM, "PNh1", forward_to=CELL)
messaging.TWILIO_ACCOUNT_SID = "AC_fake"
messaging.TWILIO_AUTH_TOKEN = "tok"   # also the signing token, so signed posts validate
messaging.TWILIO_FROM_NUMBER = "+18005550100"

_spy = _H1PostSpy(); _h1_requests.post = _spy
# Drive the real inbound path: dial leg goes no-answer (missed) -> text-back attempt.
r = post("/webhooks/twilio/voice/dial-status",
         {"To": H1_NUM, "From": H1_CALLER, "CallSid": "CAh1", "DialCallStatus": "no-answer"})
_h1_lead = db.get_lead_by_phone(_h1_biz_id, H1_CALLER)
check("H1: missed call on an unapproved tenant still creates the lead", _h1_lead is not None)
_h1_outs = [m for m in db.get_messages(_h1_lead["id"]) if m["direction"] == "out"] if _h1_lead else []
check("H1: unapproved missed call STILL records the outbound text-back (parity)",
      len(_h1_outs) >= 1)
check("H1: unapproved tenant send was GATED -- zero network calls",
      len(_spy.calls) == 0)
# A benign inbound SMS on the same thread: also gated, also no network.
r = post("/webhooks/twilio/sms/inbound",
         {"To": H1_NUM, "From": H1_CALLER, "Body": "thanks", "MessageSid": "SMh1"})
check("H1: inbound SMS on an unapproved tenant returns 200",
      r.status_code == 200 and "<Response" in r.get_data(as_text=True))
check("H1: still zero network calls while unapproved", len(_spy.calls) == 0)

# Now APPROVE the tenant and drive a fresh missed call from a NEW caller (empty thread,
# so a real text-back is generated): the gate opens and exactly one send hits the wire.
db.set_a2p_status(_h1_biz_id, "approved")
H1_CALLER2 = "+14155558888"
_spy = _H1PostSpy(); _h1_requests.post = _spy
r = post("/webhooks/twilio/voice/dial-status",
         {"To": H1_NUM, "From": H1_CALLER2, "CallSid": "CAh1b", "DialCallStatus": "no-answer"})
check("H1: approved tenant's missed-call text-back DID hit the network once",
      len(_spy.calls) == 1)

# Restore network + creds for any later blocks / suite teardown.
_h1_requests.post = _h1_saved_post
messaging.TWILIO_ACCOUNT_SID = _h1_saved_sid
messaging.TWILIO_AUTH_TOKEN = _h1_saved_tok
messaging.TWILIO_FROM_NUMBER = _h1_saved_from


os.unlink(_TMP.name)
print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
