"""Call-screening ("phone screen") checks. Run: python3 test_screening.py

Proves the layered screen end to end: the pure spam score is precision-first (no
single weak signal hard-screens), a KNOWN/saved caller is passed to the owner (never
cold-pitched, faithful-Apple), an unknown spammer is screened with NO text, an
ambiguous unknown is engaged-but-flagged, the verdict is persisted on the call log,
and the owner's 'Mark spam' override blocks the number + feeds the cross-tenant
ledger. No framework, no network: a throwaway temp DB + the demo brain + signed
webhooks. Exits non-zero on any failure.
"""
import base64
import hashlib
import hmac
import os
import tempfile

os.environ["RINGBACK_PROVIDER"] = "demo"           # deterministic, no network
os.environ["RINGBACK_SCREEN_MODE"] = "enforce"     # act on verdicts (monitor is tested below)
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_AUTH_TOKEN = "tok"                # require_twilio_signature checks this
messaging.TWILIO_ACCOUNT_SID = ""                  # configured() False -> sends simulate

import triage
import app
client = app.app.test_client()

BIZ_NUM = "+15553140000"                           # area 555, prefix 314
db.set_business_twilio(1, BIZ_NUM, "PN1")

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
    url = "http://localhost" + path
    return client.post(path, data=params, headers={"X-Twilio-Signature": _sign(url, params)})


def call_logged(sid):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM calls WHERE call_sid=?", (sid,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---- pure spam_score: precision-first (no weak signal alone reaches HARD) ----
HARD, MID = config.SCREEN_SCORE_HARD, config.SCREEN_SCORE_MID
check("a clean caller scores 0", triage.spam_score({})[0] == 0)
check("attestation A LOWERS the score (trust)",
      triage.spam_score({"attestation": "TN-Validation-Passed-A"})[0] == 0)
check("neighbor-spoof alone does NOT hard-screen",
      triage.spam_score({"neighbor_spoof": True})[0] < HARD)
check("a single repeat-call signal alone does NOT hard-screen",
      triage.spam_score({"behavior": {"missed_calls": 4}})[0] < HARD)
check("corroborated signals (attC + spoof + behavior) DO reach HARD",
      triage.spam_score({"attestation": "TN-Validation-Failed-C", "neighbor_spoof": True,
                         "behavior": {"missed_calls": 3}})[0] >= HARD)
check("an authoritative reputation verdict (100) reaches HARD alone",
      triage.spam_score({"reputation_score": 100})[0] >= HARD)
check("crowdsource alone stays SHORT of HARD (precision-first)",
      triage.spam_score({"crowd_count": 9})[0] < HARD)
check("neighbor_spoof() matches same area code + prefix",
      triage.neighbor_spoof(BIZ_NUM, "+15553149999") is True)
check("neighbor_spoof() rejects a different prefix",
      triage.neighbor_spoof(BIZ_NUM, "+15559990000") is False)


# ---- screen_caller verdict labels ------------------------------------------
db.set_opt_out(1, "+14155550001")
check("opted-out -> opted_out (no engage)",
      triage.screen_caller(1, "+14155550001")["status"] == "opted_out")
db.set_contact(1, "+14155550002", "blocked")
check("known blocked -> screened_contact",
      triage.screen_caller(1, "+14155550002")["status"] == "screened_contact")
db.set_contact(1, "+14155550003", "customer", name="Returning Jane")
v_known = triage.screen_caller(1, "+14155550003")
check("known/saved customer -> trusted, NOT engaged (faithful-Apple)",
      v_known["status"] == "trusted" and v_known["engage"] is False)
check("a clean unknown -> prospect (engaged)",
      triage.screen_caller(1, "+12125550000")["status"] == "prospect")

# A booked appointment makes a number 'known' even with no directory tag.
_bl = db.create_lead(1, "Booked Bob", "+13035550000")
db.book_appointment(1, _bl, "Mon Jun 22 · 9:00 AM", day="2026-06-22", slot_time="09:00")
check("a number with a booked estimate is known -> trusted",
      triage.screen_caller(1, "+13035550000")["status"] == "trusted")

# Mid-band (engaged but flagged) vs hard-screen, driven by signals.
v_mid = triage.screen_caller(1, "+15553149911", attestation="TN-Validation-Failed-C",
                             neighbor_spoof=True)            # 30 + 25 = 55
check("attC + neighbor-spoof -> review (engaged, flagged)",
      v_mid["status"] == "review" and v_mid["engage"] is True and MID <= v_mid["score"] < HARD)
v_spam = triage.screen_caller(1, "+15553149922", attestation="TN-Validation-Failed-C",
                              neighbor_spoof=True,
                              reputation={"line_type": "nonFixedVoip", "spam_score": None})
check("attC + neighbor-spoof + VoIP -> screened_spam (no engage)",
      v_spam["status"] == "screened_spam" and v_spam["engage"] is False)


# ---- HOT PATH: an unknown spammer is screened, logged, and NEVER texted -----
SPAM = "+15553149999"                              # neighbor-spoofs the biz prefix
for sid in ("CSp1", "CSp2", "CSp3"):               # 3 prior missed, never replied
    db.log_call(1, sid, from_number=SPAM, to_number=BIZ_NUM, dial_status="no-answer", missed=1)
post("/webhooks/twilio/voice/inbound",
     {"To": BIZ_NUM, "From": SPAM, "CallSid": "CSpam",
      "StirVerstat": "TN-Validation-Failed-C"})    # +30 attC, +25 spoof, +30 behavior = 85
cs = call_logged("CSpam")
check("spam call is logged engaged=0 with status screened_spam",
      bool(cs) and cs["engaged"] == 0 and cs["screen_status"] == "screened_spam")
check("spam call persists a spam_score and reasons",
      bool(cs) and (cs["spam_score"] or 0) >= HARD and bool(cs["screen_reasons"]))
check("a screened spammer creates NO lead (no text-back)",
      db.get_lead_by_phone(1, SPAM) is None)


# ---- HOT PATH: a clean unknown is engaged + persists status prospect --------
GOOD = "+12015550000"                               # different area, no signals
post("/webhooks/twilio/voice/inbound", {"To": BIZ_NUM, "From": GOOD, "CallSid": "CGood"})
lead = db.get_lead_by_phone(1, GOOD)
check("a clean unknown creates a lead", lead is not None)
check("a clean unknown gets the opening text-back",
      bool(lead) and any(m["direction"] == "out" for m in db.get_messages(lead["id"])))
cg = call_logged("CGood")
check("the engaged call persists status prospect, engaged=1",
      bool(cg) and cg["engaged"] == 1 and cg["screen_status"] == "prospect")


# ---- crowdsource: flags from OTHER businesses raise the score (still precise) -
CROWD = "+18185550000"
for other_biz in (2, 3, 4):
    db.add_spam_flag(other_biz, CROWD)
check("global_spam_count tallies distinct OTHER businesses",
      db.global_spam_count(CROWD, exclude_business_id=1) == 3)
vc = triage.screen_caller(1, CROWD)                 # 3 others -> 40 + 15 = 55 -> review
check("a number 3 businesses flagged is engaged-but-flagged for review (not silenced)",
      vc["status"] == "review")


# ---- 'Mark spam' override: blocks locally + feeds the cross-tenant ledger ----
client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
flag_res = client.post(f"/api/calls/{cg['id']}/flag-spam")
check("flag-spam returns ok", flag_res.status_code == 200 and flag_res.get_json().get("ok"))
check("flag-spam blocks the number for this business",
      (db.get_contact(1, GOOD) or {}).get("category") == "blocked")
check("flag-spam records a cross-tenant spam flag",
      db.global_spam_count(GOOD) >= 1)
check("flag-spam 404s on an unknown call id",
      client.post("/api/calls/999999/flag-spam").status_code == 404)


# ---- the dashboard surfaces the screened spammer + the stat -----------------
page = client.get("/pipeline").get_data(as_text=True)
check("pipeline shows the screened spammer with a Spam pill", "Spam" in page)
check("pipeline shows the 'Calls screened' stat", "Calls screened" in page)
stats = db.screening_stats(1)
check("screening_stats counts the screened spam call", stats["spam"] >= 1)
check("enforced spam is counted as actually suppressed", stats["enforced"] >= 1)


# ---- MONITOR mode: compute + log the verdict, but still text everyone --------
# (The safe-rollout posture: prove precision before it can silence anyone.)
app.SCREEN_MODE = "monitor"                          # flip the imported hot-path mode
MON = "+15553147777"                                 # neighbor-spoofs the biz prefix
for sid in ("CMon1", "CMon2", "CMon3"):              # 3 prior missed, never replied
    db.log_call(1, sid, from_number=MON, to_number=BIZ_NUM, dial_status="no-answer", missed=1)
post("/webhooks/twilio/voice/inbound",
     {"To": BIZ_NUM, "From": MON, "CallSid": "CMon",
      "StirVerstat": "TN-Validation-Failed-C"})      # would score >= HARD
cm = call_logged("CMon")
check("monitor: the spam verdict is still computed + logged",
      bool(cm) and cm["screen_status"] == "screened_spam" and cm["screen_mode"] == "monitor")
check("monitor: the caller is STILL engaged (texted), never silenced",
      bool(cm) and cm["engaged"] == 1)
mon_lead = db.get_lead_by_phone(1, MON)
check("monitor: a lead is created and gets the text-back",
      bool(mon_lead) and any(m["direction"] == "out" for m in db.get_messages(mon_lead["id"])))
mstats = db.screening_stats(1)
check("monitor: counted as 'would have screened', NOT as enforced",
      mstats["would_screen"] >= 1)
app.SCREEN_MODE = "enforce"                           # restore


# ---- Simulator demo: spam/known show the screen WITHOUT creating a lead ------
client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
_before = len(db.list_leads(1))
sd = client.post("/api/sim/incoming", json={"scenario": "spam"}).get_json()
check("sim spam returns a screened spam verdict",
      sd.get("screened") is True and sd.get("status") == "screened_spam" and sd.get("reasons"))
check("sim spam creates NO lead (pure demo)", len(db.list_leads(1)) == _before)
sk = client.post("/api/sim/incoming", json={"scenario": "known"}).get_json()
check("sim known returns a trusted verdict", sk.get("status") == "trusted")
sp = client.post("/api/sim/incoming",
                 json={"scenario": "prospect", "name": "SimProspect", "phone": "+14155550142"}).get_json()
check("sim prospect still runs the normal text-back conversation",
      bool(sp.get("reply")) and bool(sp.get("lead_id")))
check("simulator page renders the spam/known demo buttons",
      all(t in client.get("/simulator").get_data(as_text=True)
          for t in ("trigger-spam", "trigger-known")))


# ---- 'Mark spam' from the conversation panel (lead-centric) -----------------
LEADSPAM = "+13125559000"
_lsid = db.create_lead(1, "Pushy Vendor", LEADSPAM)
fl = client.post(f"/api/leads/{_lsid}/flag-spam")
check("lead flag-spam returns ok", fl.status_code == 200 and fl.get_json().get("ok"))
check("lead flag-spam blocks the lead's number",
      (db.get_contact(1, LEADSPAM) or {}).get("category") == "blocked")
check("lead flag-spam feeds the cross-tenant ledger", db.global_spam_count(LEADSPAM) >= 1)
check("lead flag-spam 404s on an unknown lead", client.post("/api/leads/999999/flag-spam").status_code == 404)
# Cross-tenant: a lead that belongs to business 2 must 404 for business 1.
_other = db.create_lead(2, "Other Biz Lead", "+13125559111")
check("lead flag-spam rejects a cross-tenant lead id (404)",
      client.post(f"/api/leads/{_other}/flag-spam").status_code == 404)
check("conversation panel renders the Mark-as-spam control",
      "convo-flag-spam" in client.get("/pipeline").get_data(as_text=True))
check("dashboard JS wires the lead flag-spam endpoint",
      "/flag-spam" in open("static/app.js").read())


# ---- Per-business screening mode (overrides the app-wide default) -----------
# db setter: valid values stored, anything else clears to NULL (inherit).
db.set_screen_mode(1, "monitor")
check("set_screen_mode stores a valid mode", db.get_business(1).get("screen_mode") == "monitor")
db.set_screen_mode(1, "bogus")
check("set_screen_mode clears an invalid mode to NULL (inherit)",
      db.get_business(1).get("screen_mode") is None)
# Effective-mode resolver: a business setting wins over the env default (enforce here).
check("effective mode falls back to the app default when unset",
      app._effective_screen_mode(db.get_business(1)) == "enforce")
check("a business setting overrides the app default",
      app._effective_screen_mode({"screen_mode": "off"}) == "off")

# Hot path honors the per-business mode: set business 1 to OFF and a clearly-spam
# caller is ENGAGED anyway (no screen), even though the app default is enforce.
db.set_screen_mode(1, "off")
OFFNUM = "+15553146543"                               # neighbor-spoofs the biz prefix
for sid in ("COff1", "COff2", "COff3"):
    db.log_call(1, sid, from_number=OFFNUM, to_number=BIZ_NUM, dial_status="no-answer", missed=1)
post("/webhooks/twilio/voice/inbound",
     {"To": BIZ_NUM, "From": OFFNUM, "CallSid": "COff",
      "StirVerstat": "TN-Validation-Failed-C"})
co = call_logged("COff")
check("per-business OFF engages a spam-shaped caller (no screen)",
      bool(co) and co["engaged"] == 1 and co["screen_status"] == "prospect")
check("per-business OFF leaves screen_mode unset on the call", bool(co) and not co["screen_mode"])
db.set_screen_mode(1, None)                           # restore inherit

# Settings POST round-trips the mode (include name so the profile isn't blanked).
client.post("/settings", data={"name": db.get_business(1)["name"], "screen_mode": "enforce"})
check("settings POST persists the chosen screening mode",
      db.get_business(1).get("screen_mode") == "enforce")
client.post("/settings", data={"name": db.get_business(1)["name"], "screen_mode": ""})
check("settings POST with blank clears back to inherit",
      db.get_business(1).get("screen_mode") is None)
check("settings page renders the screening-mode selector",
      'name="screen_mode"' in client.get("/settings").get_data(as_text=True))


print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
