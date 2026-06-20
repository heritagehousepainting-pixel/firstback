"""Batch G -- voicemail->lead + web-chat widget (plan 10, code-only parts). Run standalone.

Both features are opt-in (voicemail_enabled / widget_enabled default OFF) and A2P-gated, so
they're safe no-ops until the owner turns them on. Deposit-link + GBP dashboard stay NEEDS-OWNER.
"""
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
db.init_db()

import messaging
messaging.TWILIO_ACCOUNT_SID = ""                       # send_sms simulates
messaging.valid_signature = lambda url, params, sig: True   # bypass Twilio sig for webhook tests
import compliance
compliance.a2p_ready = lambda b: True                   # don't gate the simulated send

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


# Set up business 1: a Twilio number, a widget slug, both features enabled.
db.set_business_twilio(1, "+15553330000", "PN_test")
_c = db.get_conn(); _c.execute("UPDATE businesses SET micro_site_slug=? WHERE id=1", ("heritage-abc",)); _c.commit(); _c.close()
db.update_reminder_prefs(1, {"voicemail_enabled": 1, "widget_enabled": 1})

REC = "/webhooks/twilio/voice/recording"


def outs(lead_id):
    return [m for m in db.get_messages(lead_id) if m.get("direction") == "out"]


# ============================================================================
# Feature 1 -- voicemail -> lead
# ============================================================================
check("migration: messages.recording_url column exists",
      "recording_url" in [r[1] for r in db.get_conn().execute("PRAGMA table_info(messages)").fetchall()])

r = client.post(REC, data={"To": "+15553330000", "From": "+15558887777",
                           "TranscriptionText": "Hi, I need a quote for exterior painting",
                           "RecordingUrl": "https://api.twilio.com/rec123"})
check("recording webhook returns 200", r.status_code == 200)
vm = db.get_lead_by_phone(1, "+15558887777")
check("voicemail creates a lead (source=voicemail)", vm and vm.get("source") == "voicemail")
_msgs = db.get_messages(vm["id"])
check("transcript injected as an inbound message", any("[Voicemail]" in (m.get("body") or "") for m in _msgs))
check("recording URL stored on the row (no fake 'vm_url' direction)",
      any((m.get("recording_url") or "") == "https://api.twilio.com/rec123" for m in _msgs)
      and all(m.get("direction") in ("in", "out") for m in _msgs))
check("empty thread gets greeted exactly once (no double-record)", len(outs(vm["id"])) == 1)

# second webhook for the same caller -> no duplicate lead, no re-greet (thread now has an outbound)
_before = len(db.get_messages(vm["id"]))
client.post(REC, data={"To": "+15553330000", "From": "+15558887777",
                       "TranscriptionText": "Following up", "RecordingUrl": "u2"})
vm2 = db.get_lead_by_phone(1, "+15558887777")
check("repeat voicemail -> same lead (no duplicate)", vm2["id"] == vm["id"])
check("repeat voicemail -> no second greeting (outbound count unchanged)", len(outs(vm["id"])) == 1)

# empty transcript on a fresh caller -> no [Voicemail] msg, but the empty thread is still greeted
client.post(REC, data={"To": "+15553330000", "From": "+15556665555", "TranscriptionText": "", "RecordingUrl": ""})
nt = db.get_lead_by_phone(1, "+15556665555")
check("empty transcript still creates the lead + greets", nt and len(outs(nt["id"])) == 1)
check("empty transcript injects no voicemail message",
      not any("[Voicemail]" in (m.get("body") or "") for m in db.get_messages(nt["id"])))

# unknown business number -> 200 no-op
r = client.post(REC, data={"To": "+19998887777", "From": "+15551112222", "TranscriptionText": "x"})
check("unknown business number -> 200 no-op", r.status_code == 200 and db.get_lead_by_phone(1, "+15551112222") is None)

# bad signature -> 403
messaging.valid_signature = lambda *a: False
check("recording webhook rejects a bad Twilio signature (403)",
      client.post(REC, data={"To": "+15553330000", "From": "+1"}).status_code == 403)
messaging.valid_signature = lambda url, params, sig: True

# dial-status voicemail prompt is gated on voicemail_enabled
ds = client.post("/webhooks/twilio/voice/dial-status",
                 data={"To": "+15553330000", "From": "+15554443333", "DialCallStatus": "no-answer", "CallSid": "CA1"})
check("dial-status offers a voicemail <Record> when enabled", "<Record" in ds.get_data(as_text=True))
db.update_reminder_prefs(1, {"voicemail_enabled": 0, "widget_enabled": 1})
ds2 = client.post("/webhooks/twilio/voice/dial-status",
                  data={"To": "+15553330000", "From": "+15554440000", "DialCallStatus": "no-answer", "CallSid": "CA2"})
check("dial-status does NOT record when voicemail disabled", "<Record" not in ds2.get_data(as_text=True))
db.update_reminder_prefs(1, {"voicemail_enabled": 1, "widget_enabled": 1})

# ============================================================================
# Feature 2 -- web-chat widget
# ============================================================================
cfg = client.get("/api/widget/heritage-abc/config.js")
ct = cfg.get_data(as_text=True)
check("config.js returns 200 JS for an enabled slug", cfg.status_code == 200 and "window.__fb" in ct)
check("config.js is CORS-open", cfg.headers.get("Access-Control-Allow-Origin") == "*")
check("config.js for an unknown slug is an empty config",
      "window.__fb={}" in client.get("/api/widget/nope/config.js").get_data(as_text=True))
check("/widget.js serves the embeddable loader", "fb-w" in client.get("/widget.js").get_data(as_text=True))

# CORS preflight
pf = client.open("/webhooks/widget/lead", method="OPTIONS")
check("widget OPTIONS preflight -> 204 + CORS", pf.status_code == 204 and pf.headers.get("Access-Control-Allow-Origin") == "*")

# valid submission -> lead created (source=web_widget), greeted once
r = client.post("/webhooks/widget/lead", json={"slug": "heritage-abc", "phone": "5551234567", "name": "Web Dave"})
check("widget POST -> 200 ok + CORS", r.status_code == 200 and r.headers.get("Access-Control-Allow-Origin") == "*")
wl = db.get_lead_by_phone(1, messaging.to_e164("5551234567"))
check("widget creates a lead (source=web_widget)", wl and wl.get("source") == "web_widget")
check("widget lead greeted exactly once", len(outs(wl["id"])) == 1)
# duplicate phone -> no new lead, no re-greet
client.post("/webhooks/widget/lead", json={"slug": "heritage-abc", "phone": "555 123 4567"})
check("widget duplicate phone -> no re-greet", len(outs(wl["id"])) == 1)
# invalid phone -> 400 (before the rate check)
check("widget invalid phone -> 400", client.post("/webhooks/widget/lead", json={"slug": "heritage-abc", "phone": "abc"}).status_code == 400)
# unknown / disabled slug -> 404
check("widget unknown slug -> 404", client.post("/webhooks/widget/lead", json={"slug": "nope", "phone": "5559998888"}).status_code == 404)

# rate limit: isolate the bucket, then 5 pass, 6th 429
appmod._WIDGET_RATE.clear()
codes = [client.post("/webhooks/widget/lead", json={"slug": "heritage-abc", "phone": f"55512300{i:02d}"}).status_code
         for i in range(6)]
check("widget rate limit: 6th submission from same IP -> 429", codes[:5] == [200] * 5 and codes[5] == 429)

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
