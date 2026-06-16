"""Caller triage (v1) checks. Run: python3 test_triage.py

Proves the directory-based screen: known non-prospects (the owner's mom, the power
company, a blocked number) and opted-out callers are logged but never texted, while
unknown callers and returning customers are engaged -- plus auto-learn on booking.
No framework, no network: a throwaway temp DB + the deterministic demo brain, so
the real ringback.db is untouched. Exits non-zero on any failure.
"""
import base64
import hashlib
import hmac
import os
import tempfile

os.environ["RINGBACK_PROVIDER"] = "demo"          # deterministic, no network
os.environ["RINGBACK_SCREEN_MODE"] = "enforce"    # these checks assert the screen ACTS on its verdict
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_AUTH_TOKEN = "tok"   # require_twilio_signature validates against this
messaging.TWILIO_ACCOUNT_SID = ""     # configured() False -> send_sms simulates (no network)

import triage
import app
client = app.app.test_client()

# Business 1 has a RingBack number but NO forward-to cell, so an inbound call goes
# straight to the missed-call text-back path (where triage runs).
BIZ_NUM = "+15553140000"
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
    """Signed POST, exactly as Twilio would send it."""
    url = "http://localhost" + path
    return client.post(path, data=params, headers={"X-Twilio-Signature": _sign(url, params)})


def call_logged(call_sid):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM calls WHERE call_sid=?", (call_sid,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---- should_engage (pure) --------------------------------------------------
check("unknown caller engages", triage.should_engage(None) is True)
check("prospect engages", triage.should_engage({"category": "prospect"}) is True)
check("returning customer engages", triage.should_engage({"category": "customer"}) is True)
for cat in ("personal", "vendor", "blocked"):
    check(f"{cat} is screened out", triage.should_engage({"category": cat}) is False)


# ---- screen_caller against the directory + opt-out ledger -------------------
db.set_contact(1, "+14155550000", "personal", name="Mom")
v = triage.screen_caller(1, "(415) 555-0000")   # format-independent match
check("known personal number is screened out",
      v["engage"] is False and v["category"] == "personal")
check("unknown number is engaged", triage.screen_caller(1, "+14155551234")["engage"] is True)
check("directory is scoped per business",
      triage.screen_caller(2, "+14155550000")["engage"] is True)
db.set_opt_out(1, "+14155559999")
check("opted-out caller is screened regardless of directory",
      triage.screen_caller(1, "+14155559999")["engage"] is False)


# ---- missed call from a known non-prospect: logged, NO lead, NO text --------
VENDOR = "+18005551000"
db.set_contact(1, VENDOR, "vendor", name="Power Co")
post("/webhooks/twilio/voice/inbound", {"To": BIZ_NUM, "From": VENDOR, "CallSid": "CAv"})
check("screened caller creates no lead", db.get_lead_by_phone(1, VENDOR) is None)
cv = call_logged("CAv")
check("screened call is logged engaged=0 with its category",
      bool(cv) and cv["engaged"] == 0 and cv["category"] == "vendor")


# ---- missed call from an unknown number: lead + opening text-back (engaged) -
PROSPECT = "+14155552222"
post("/webhooks/twilio/voice/inbound", {"To": BIZ_NUM, "From": PROSPECT, "CallSid": "CAp"})
lead = db.get_lead_by_phone(1, PROSPECT)
check("unknown caller creates a lead", lead is not None)
check("unknown caller gets the opening text-back",
      bool(lead) and any(m["direction"] == "out" for m in db.get_messages(lead["id"])))
cp = call_logged("CAp")
check("engaged call is logged engaged=1, category prospect",
      bool(cp) and cp["engaged"] == 1 and cp["category"] == "prospect")


# ---- a blocked number that texts in first stays silent ----------------------
BLOCKED = "+18005552000"
db.set_contact(1, BLOCKED, "blocked")
r = post("/webhooks/twilio/sms/inbound",
         {"To": BIZ_NUM, "From": BLOCKED, "Body": "buy our SEO package", "MessageSid": "SMb"})
check("blocked inbound SMS gets an empty TwiML (silence)",
      r.get_data(as_text=True).strip().endswith("<Response/>"))
check("blocked inbound SMS creates no lead", db.get_lead_by_phone(1, BLOCKED) is None)


# ---- auto-learn: a booking marks the number a customer, tag-safe ------------
db.learn_customer(1, "+14155553333", name="Jane")
check("learn_customer marks a fresh number as a customer",
      (db.get_contact(1, "+14155553333") or {}).get("category") == "customer")
db.set_contact(1, "+14155554444", "personal", name="Dad")
db.learn_customer(1, "+14155554444", name="Dad")
check("learn_customer never overrides an owner's personal tag",
      db.get_contact(1, "+14155554444")["category"] == "personal")


# ---- owner-facing routes (signed in as the seeded owner) -------------------
client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
r = client.post("/api/contacts",
                json={"number": "+1 312 555 7777", "category": "vendor", "name": "Paint Supply"})
check("POST /api/contacts tags a screened number", r.status_code == 200)
listing = client.get("/api/contacts").get_json()
check("GET /api/contacts returns the managed entry",
      any(c["category"] == "vendor" and c["name"] == "Paint Supply"
          for c in (listing.get("managed") or [])))
check("GET /api/contacts reports the auto-learned customer count",
      listing.get("customers", 0) >= 1)
check("POST /api/contacts rejects a non-owner category (400)",
      client.post("/api/contacts", json={"number": "+13125557777", "category": "customer"}).status_code == 400)
client.post("/api/contacts/delete", json={"number": "+1 312 555 7777"})
check("POST /api/contacts/delete forgets the entry", db.get_contact(1, "+13125557777") is None)

# the screened vendor call (CAv, from VENDOR) surfaces on the pipeline cockpit...
check("pipeline renders the 'Screened calls' strip",
      "Screened calls" in client.get("/pipeline").get_data(as_text=True))
vendor_call = [s for s in db.recent_screened_calls(1, 8) if s["category"] == "vendor"][0]
# ...and the one-tap override engages them: forgets the tag, creates the lead, texts back.
eng = client.post(f"/api/calls/{vendor_call['id']}/engage").get_json()
check("engage override links/creates a lead", bool(eng) and eng.get("lead_id"))
check("engage override forgets the screen tag", db.get_contact(1, VENDOR) is None)
_eng_lead = db.get_lead_by_phone(1, VENDOR)
check("engage override texts the caller back",
      bool(_eng_lead) and any(m["direction"] == "out" for m in db.get_messages(_eng_lead["id"])))


# ---- suggestion engine (QuickBooks-style: observe -> recommend -> confirm) ---
check("suggest: repeat booker -> client (customer)",
      triage.suggest_category({"booked": 2, "missed_calls": 0, "inbound_msgs": 0})[0] == "customer")
check("suggest: repeat caller, never replied -> spam (blocked)",
      triage.suggest_category({"booked": 0, "missed_calls": 3, "inbound_msgs": 0})[0] == "blocked")
check("suggest: a caller who replied is NOT flagged spam",
      triage.suggest_category({"booked": 0, "missed_calls": 4, "inbound_msgs": 1}) is None)
check("suggest: an ordinary caller gets no suggestion",
      triage.suggest_category({"booked": 0, "missed_calls": 1, "inbound_msgs": 1}) is None)

# Seed behavior: a repeat no-reply caller (spam-shaped) and a legacy repeat booker.
SPAMNUM = "+18885550000"
for sid in ("CSpam1", "CSpam2", "CSpam3"):
    db.log_call(1, sid, from_number=SPAMNUM, to_number=BIZ_NUM, dial_status="no-answer", missed=1)
BOOKER = "+17775551234"
_bl = db.create_lead(1, "Repeat Client", BOOKER)
db.book_appointment(1, _bl, "Mon Jun 23 · 9:00 AM", day="2026-06-23", slot_time="09:00")
db.book_appointment(1, _bl, "Tue Jun 24 · 9:00 AM", day="2026-06-24", slot_time="09:00")
triage.scan_suggestions(1)
sugs = {s["number"]: s for s in db.list_suggestions(1, "pending")}
check("scan suggests 'blocked' for the repeat no-reply caller",
      sugs.get("8885550000", {}).get("suggested_category") == "blocked")
check("scan suggests 'customer' for the repeat booker",
      sugs.get("7775551234", {}).get("suggested_category") == "customer")
check("scan never suggests for an already-classified number (Mom)", "4155550000" not in sugs)

check("GET /api/suggestions returns the pending queue",
      isinstance(client.get("/api/suggestions").get_json().get("suggestions"), list))
# Accept the spam suggestion -> writes to the directory + leaves the queue.
client.post(f"/api/suggestions/{sugs['8885550000']['id']}/accept")
check("accept writes the suggested category to the directory",
      (db.get_contact(1, SPAMNUM) or {}).get("category") == "blocked")
check("accepted suggestion leaves the pending queue",
      all(s["number"] != "8885550000" for s in db.list_suggestions(1, "pending")))
# Dismiss the booker suggestion -> gone, and a re-scan must not resurrect it.
client.post(f"/api/suggestions/{sugs['7775551234']['id']}/dismiss")
triage.scan_suggestions(1)
check("dismissed suggestion is not resurrected by a re-scan",
      all(s["number"] != "7775551234" for s in db.list_suggestions(1, "pending")))


# ---- review inbox: tabs (status + counts), undo, bulk, the /callers page --------
gp = client.get("/api/suggestions?status=accepted").get_json()
check("GET ?status=accepted returns the Sorted tab",
      any(s["number"] == "8885550000" for s in gp["suggestions"]))
check("the response carries counts for all three tabs",
      set(gp.get("counts", {})) == {"pending", "accepted", "dismissed"} and gp["counts"]["accepted"] >= 1)
# Undo the accepted 'spam' classification -> back to To review + directory reverted.
acc_id = next(s["id"] for s in db.list_suggestions(1, "accepted") if s["number"] == "8885550000")
client.post(f"/api/suggestions/{acc_id}/reopen")
check("reopen moves a Sorted suggestion back to To review",
      any(s["number"] == "8885550000" for s in db.list_suggestions(1, "pending")))
check("reopen reverts the directory entry the accept had created",
      db.get_contact(1, "8885550000") is None)
# Bulk-accept two fresh spam-shaped callers at once.
for sid in ("Bk1", "Bk2", "Bk3"):
    db.log_call(1, sid, from_number="+12025550143", to_number=BIZ_NUM, dial_status="busy", missed=1)
for sid in ("Bk4", "Bk5", "Bk6"):
    db.log_call(1, sid, from_number="+12025550144", to_number=BIZ_NUM, dial_status="busy", missed=1)
triage.scan_suggestions(1)
bulk_ids = [s["id"] for s in db.list_suggestions(1, "pending")
            if s["number"] in ("2025550143", "2025550144")]
rb = client.post("/api/suggestions/bulk", json={"ids": bulk_ids, "action": "accept"}).get_json()
check("bulk accept applies to every selected suggestion", rb.get("count") == 2)
check("bulk-accepted numbers land in the directory",
      (db.get_contact(1, "2025550143") or {}).get("category") == "blocked"
      and (db.get_contact(1, "2025550144") or {}).get("category") == "blocked")
check("the /callers page renders the inbox + directory",
      all(t in client.get("/callers").get_data(as_text=True)
          for t in ("For review", "To review", "Screened numbers")))


os.unlink(_TMP.name)
print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
