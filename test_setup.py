"""Go-Live connection wizard (connections.py + /setup routes). Run: python3 test_setup.py

Proves a contractor can get themselves live without a shell or the Twilio console:
  * step_state walks profile -> number -> a2p -> forwarding, exposing the current step,
  * the wizard is honest -- never "live" until launch_blockers is empty,
  * a2p_sync maps Twilio's campaign status onto our state and persists it,
  * carrier forwarding codes resolve (with the FirstBack number baked in),
  * the /setup routes save the profile, buy/attach a number, submit A2P, and set
    forwarding -- all auth-gated and tenant-isolated, with Twilio HTTP mocked.
Throwaway temp DB + the deterministic demo brain; no network.
"""
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
# A from-number so messaging.configured()-style checks behave; real sends are mocked.
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")
os.environ.setdefault("FIRSTBACK_PUBLIC_URL", "https://ringback-gixe.onrender.com")
os.environ.setdefault("FIRSTBACK_TASKS_SECRET", "tasks_secret_test")

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
import connections
import messaging
import app
client = app.app.test_client()

# SOCKET/REQUESTS TRIPWIRE: any UNSTUBBED Twilio call must fail LOUDLY rather than be
# swallowed by the modules' broad `except Exception`. We raise BaseException so it
# defeats those except blocks. messaging/connections do a lazy `import requests`
# inside functions and call requests.get/post as attribute lookups, so reassigning
# these module attrs is honored. The suite stubs every Twilio seam at the function
# level, so this stays green; if it trips, that exposes a real unstubbed call -- FIX
# the stub (don't remove the guard).
import requests as _rq_guard
class _NetworkLeak(BaseException): pass
def _no_net(*a, **k):
    raise _NetworkLeak(f"unstubbed network call: {a[0] if a else '?'}")
_rq_guard.get = _no_net
_rq_guard.post = _no_net

# Phase 3 SF-8: stub A1/A2 seams not yet implemented
# db.set_business_type (A1) -- called from /signup
def _stub_set_business_type(business_id, business_type):
    try:
        conn = db.get_conn()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()]
        if "business_type" not in cols:
            conn.execute("ALTER TABLE businesses ADD COLUMN business_type TEXT DEFAULT 'unknown'")
        conn.execute("UPDATE businesses SET business_type=? WHERE id=?",
                     (business_type, business_id))
        conn.commit()
        conn.close()
    except Exception:
        pass
db.set_business_type = _stub_set_business_type

# connections.submit_a2p (A2) -- called from setup_a2p mode=auto/submit
# Simulates the submit: sets a2p_status=pending + a2p_submitted_at (the real
# connections.submit_a2p will do this after the merge; here we replicate the
# minimum so test_setup assertions on status/submitted_at still hold).
from datetime import datetime as _dt
def _stub_submit_a2p(business_id):
    db.set_a2p_registration(business_id, status="pending",
                            submitted_at=_dt.utcnow().isoformat(timespec="seconds"))
    return {"status": "simulated"}
connections.submit_a2p = _stub_submit_a2p


_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


def login():
    return client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                                        "password": config.SEED_OWNER_PASSWORD})


def reset_biz():
    """Blank the wizard-relevant columns on business 1 so each block starts clean."""
    conn = db.get_conn()
    conn.execute("UPDATE businesses SET ein='', business_address='', website='', "
                 "twilio_number='', twilio_number_sid='', a2p_status='unregistered', "
                 "a2p_brand_sid='', a2p_campaign_sid='', a2p_messaging_service_sid='', "
                 "a2p_submitted_at='', forwarding_confirmed=0 WHERE id=1")
    conn.commit(); conn.close()


# ====================== connections.py units ======================
reset_biz()
biz = db.get_business(1)
state = connections.step_state(biz)
check("step_state returns the four ordered steps",
      [s["key"] for s in state] == list(connections.STEPS))
check("profile is the current step on a fresh tenant",
      connections.current_step(biz) == "profile")
check("a fresh tenant is not live", connections.is_live(biz, sms_configured=True) is False)
check("blockers name the missing pieces",
      any("number" in b.lower() for b in connections.blockers(biz, True)))

# advance the profile
db.update_a2p_profile(1, {"ein": "12-3456789", "business_address": "1 Main St, Philadelphia PA"})
biz = db.get_business(1)
check("profile step completes once name+EIN+address are set",
      connections.step_state(biz)[0]["status"] == "done")
check("number is now the current step", connections.current_step(biz) == "number")

# default area code is derived from the business phone
db.update_business(1, {"phone": "+1 (267) 555-1212"})
check("default_area_code pulls the area code off the phone",
      connections.default_area_code(db.get_business(1)) == "267")

# ---- a2p_sync maps Twilio status -> our model and persists ----
db.set_a2p_registration(1, campaign_sid="CMtest", messaging_service_sid="MGtest",
                        status="pending")
_orig_fetch = messaging.fetch_a2p_campaign_status
messaging.fetch_a2p_campaign_status = lambda svc, camp: "IN_PROGRESS"
check("a2p_sync keeps an in-progress campaign pending", connections.a2p_sync(db.get_business(1)) == "pending")
messaging.fetch_a2p_campaign_status = lambda svc, camp: "VERIFIED"
check("a2p_sync flips a verified campaign to approved", connections.a2p_sync(1) == "approved")
check("the approved status persisted", db.get_business(1)["a2p_status"] == "approved")
messaging.fetch_a2p_campaign_status = lambda svc, camp: None
check("a2p_sync is a no-op (keeps status) when Twilio returns nothing",
      connections.a2p_sync(1) == "approved")
# No-op DB-state: a None fetch must not WRITE a new status, not merely return the old one.
db.set_a2p_status(1, "approved")
messaging.fetch_a2p_campaign_status = lambda svc, camp: None
connections.a2p_sync(1)
check("a2p_sync with None leaves the stored status untouched (no downgrade write)",
      db.get_business(1)["a2p_status"] == "approved")
# Every terminal/failure status maps as expected and persists; restore to approved after.
for raw, expected in (("FAILED", "failed"), ("REJECTED", "failed"), ("SUSPENDED", "failed"),
                      ("DELETED", "failed"), ("EXPIRED", "failed"), ("REGISTERED", "pending")):
    messaging.fetch_a2p_campaign_status = lambda svc, camp, _r=raw: _r
    returned = connections.a2p_sync(1)
    check(f"a2p_sync maps {raw} -> {expected}",
          returned == expected and db.get_business(1)["a2p_status"] == expected)
    db.set_a2p_status(1, "approved")
messaging.fetch_a2p_campaign_status = lambda svc, camp: "VERIFIED"
check("a2p_sync_all reports zero changes when already approved", connections.a2p_sync_all() == 0)

# ---- M3: an UNMAPPED campaign_status leaves the stored status unchanged ----
# Twilio could return a status we don't model (a new string). a2p_sync must log it
# and make NO change -- never silently downgrade or corrupt the business's state.
import io
import contextlib
db.set_a2p_status(1, "approved")
messaging.fetch_a2p_campaign_status = lambda svc, camp: "WAT_NEW_STATUS"
_m3_err = io.StringIO()
with contextlib.redirect_stderr(_m3_err):
    _m3_ret = connections.a2p_sync(1)
check("M3: an unmapped campaign_status returns the unchanged current status",
      _m3_ret == "approved")
check("M3: an unmapped campaign_status writes no change (status stays approved)",
      db.get_business(1)["a2p_status"] == "approved")
check("M3: an unmapped campaign_status is logged (unmapped note fires)",
      "unmapped campaign_status" in _m3_err.getvalue())
db.set_a2p_status(1, "approved")

messaging.fetch_a2p_campaign_status = _orig_fetch

# ---- M1: done_count must collapse when the SERVER can't send (sms_configured=False) ----
# A fully tenant-ready business (profile + number + webhooks + a2p approved + forwarding)
# is "4 of 4 done" ONLY when the server has Twilio creds. With sms_configured=False the
# server literally cannot send, so only the profile step (the one thing that doesn't
# depend on the server) may count done -- the stepper must never show 4 of 4 then.
_m1_biz = {"name": "Ready Co", "ein": "12-3456789",
           "business_address": "1 Main St, Philadelphia PA",
           "twilio_number": "+12677562454", "webhooks_wired": 1,
           "a2p_status": "approved", "forwarding_confirmed": 1}
_m1_done_true = sum(1 for s in connections.step_state(_m1_biz, sms_configured=True) if s["done"])
_m1_done_false = sum(1 for s in connections.step_state(_m1_biz, sms_configured=False) if s["done"])
check("M1: a fully-ready business shows 4 of 4 done when sms_configured=True",
      _m1_done_true == 4)
check("M1: only the profile step (1 of 4) counts done when sms_configured=False",
      _m1_done_false == 1)
check("M1: with sms_configured=False the one done step is 'profile'",
      [s["key"] for s in connections.step_state(_m1_biz, sms_configured=False) if s["done"]] == ["profile"])

# ---- carrier forwarding codes ----
vz = connections.forwarding_code("verizon", "+12677562454")
check("verizon code bakes in the number", vz["activate"] == "*71+12677562454")
check("verizon carries a cancel code", vz["cancel"] == "*73")
tm = connections.forwarding_code("tmobile", "+12677562454")
check("t-mobile uses the GSM conditional code", tm["activate"] == "**61*+12677562454#")
unknown = connections.forwarding_code("nope", "+12677562454")
check("an unknown carrier falls back to the universal GSM code",
      unknown["activate"] == "**004*+12677562454#" and unknown["carrier"] == "other")


# ====================== /setup routes (Phase 1) ======================
# Auth gate: anonymous is redirected to login, never shown the wizard.
anon = app.app.test_client()
r = anon.get("/setup")
check("/setup requires login", r.status_code in (301, 302) and "/login" in r.headers.get("Location", ""))
r = anon.post("/setup/profile", data={"name": "X"})
check("/setup/profile requires login", r.status_code in (301, 302))

reset_biz()
login()
r = client.get("/setup")
check("/setup renders for a logged-in owner", r.status_code == 200 and b"Go Live" in r.data)
check("the wizard shows the profile step as current", b"Business name" in r.data and b"EIN" in r.data)
check("the wizard is honest that we're not live yet", b"steps done" in r.data)

# Save the profile -> profile completes, number becomes the current step.
r = client.post("/setup/profile", data={
    "name": "Heritage House Painting", "trade": "Painting", "owner_name": "Jon",
    "service_area": "Philadelphia", "legal_business_name": "M&M Drywall Services",
    "ein": "12-3456789", "business_address": "1 Main St, Philadelphia PA 19100",
    "website": "https://heritagehousepainting.com"})
check("profile POST redirects back to the wizard", r.status_code in (301, 302))
saved = db.get_business(1)
check("profile POST persists the EIN + address", saved["ein"] == "12-3456789"
      and "Main St" in (saved["business_address"] or ""))
check("profile POST persists the business name via update_business",
      saved["name"] == "Heritage House Painting")
check("profile is now done, number is the current step", connections.current_step(saved) == "number")
# Hoisted: now that 'number' is the current step, the very next /setup GET renders the
# number step and calls messaging.search_numbers. Install its stub BEFORE that GET so
# no real search fires (the tripwire above turns an un-hoisted stub into a hard fail).
_orig_search = messaging.search_numbers
messaging.search_numbers = lambda area_code=None, contains=None, limit=10: \
    ["+12675550001", "+12675550002"]
r = client.get("/setup")
check("the completed profile shows a summary with an Edit link",
      b"EIN on file" in r.data and b"/setup?edit=profile" in r.data)


# ====================== Number step (Phase 2, Twilio mocked) ======================
# Search: the wizard lists available numbers for an area code. (search_numbers stub
# was hoisted above the first number-step GET; it's still in force here.)
r = client.get("/setup?edit=number&area_code=267")
check("the number step lists available numbers from search", b"+12675550001" in r.data)

# Attach: bind a number already owned (the 267 we wired by hand). Twilio ownership +
# wiring HTTP are mocked so the harness stays offline; attach now confirms ownership
# and wires webhooks via messaging.account_owns_number / attach_owned_number.
_orig_owns = messaging.account_owns_number
_orig_attach = messaging.attach_owned_number
messaging.account_owns_number = lambda e164: True
def _fake_attach(e164, business_id, base_url=None):
    db.set_business_twilio(business_id, e164, "PNattach", webhooks_wired=True)
    return True
messaging.attach_owned_number = _fake_attach
r = client.post("/setup/number", data={"mode": "attach", "number": "+12677562454"})
check("attach binds the number + redirects", r.status_code in (301, 302))
check("attach persists the twilio_number", db.get_business(1)["twilio_number"] == "+12677562454")
check("number step is now done; a2p is current", connections.current_step(db.get_business(1)) == "a2p")
messaging.account_owns_number = _orig_owns
messaging.attach_owned_number = _orig_attach
r = client.get("/setup")
check("the bound number shows in its step summary", b"+12677562454" in r.data)

# Buy: provision a fresh number (provision_number mocked to bind + return it).
conn = db.get_conn(); conn.execute("UPDATE businesses SET twilio_number='' WHERE id=1"); conn.commit(); conn.close()
_orig_provision = messaging.provision_number
def _fake_provision(business_id, phone=None, area_code=None, base_url=None):
    num = phone or "+12675550009"
    db.set_business_twilio(business_id, num, "PNfake")
    return num
messaging.provision_number = _fake_provision
r = client.post("/setup/number", data={"mode": "buy", "area_code": "267",
                                        "number": "+12675550001"})
check("buy provisions + binds the picked number",
      r.status_code in (301, 302) and db.get_business(1)["twilio_number"] == "+12675550001")
# Provision FAILS (Twilio refused/None): the route must redirect with err=buy and must
# NOT write a number. Clear the column first so a stale value can't mask a bad write.
conn = db.get_conn(); conn.execute("UPDATE businesses SET twilio_number='' WHERE id=1"); conn.commit(); conn.close()
messaging.provision_number = lambda business_id, phone=None, area_code=None, base_url=None: None
r = client.post("/setup/number", data={"mode": "buy", "area_code": "267"})
check("a failed provision -> err=buy", "err=buy" in r.headers.get("Location", ""))
check("a failed provision leaves the number unset", not db.get_business(1)["twilio_number"])
messaging.provision_number = _fake_provision
# attach with a too-short number is rejected (kept on the number step)
conn = db.get_conn(); conn.execute("UPDATE businesses SET twilio_number='' WHERE id=1"); conn.commit(); conn.close()
r = client.post("/setup/number", data={"mode": "attach", "number": "123"})
check("a too-short attach is rejected", "err=number" in r.headers.get("Location", ""))
check("a rejected attach leaves the number unset", not db.get_business(1)["twilio_number"])
# restore + re-bind so later phases start from a numbered business
messaging.search_numbers = _orig_search
messaging.provision_number = _orig_provision
db.set_business_twilio(1, "+12677562454", "PNheritage", webhooks_wired=True)


# ====================== A2P step (Phase 3, Twilio mocked) ======================
# With a number bound, A2P is the current step and offers to submit.
db.set_a2p_registration(1, status="unregistered")
r = client.get("/setup")
check("a2p step offers carrier registration", b"Activate texting" in r.data)

# Submit -> pending + a submission timestamp (the gated founder email is simulated).
r = client.post("/setup/a2p", data={"mode": "submit"})
check("a2p submit redirects", r.status_code in (301, 302))
saved = db.get_business(1)
check("a2p submit sets status pending", saved["a2p_status"] == "pending")
check("a2p submit records a submitted_at", bool(saved["a2p_submitted_at"]))
check("a2p pending is NOT live yet", connections.is_live(saved, sms_configured=True) is False)
r = client.get("/setup")
check("a2p pending shows the honest wait copy", b"already answering calls" in r.data)

# Submit is refused without the registration intake (no EIN -> err=profile).
db.update_a2p_profile(1, {"ein": ""})
r = client.post("/setup/a2p", data={"mode": "submit"})
check("a2p submit without an EIN is refused", "err=profile" in r.headers.get("Location", ""))
db.update_a2p_profile(1, {"ein": "12-3456789"})

# Operator records the campaign SIDs -> sync confirms approval immediately.
# record is operator-gated (H3): make the seeded login an operator for this block.
messaging.fetch_a2p_campaign_status = lambda svc, camp: "VERIFIED"
_orig_ops = config.OPERATOR_EMAILS
config.OPERATOR_EMAILS = frozenset({config.SEED_OWNER_EMAIL.strip().lower()})
r = client.post("/setup/a2p", data={"mode": "record", "brand_sid": "BNx",
                                    "messaging_service_sid": "MGx", "campaign_sid": "CMx"})
check("recording SIDs + a verified campaign flips to approved",
      db.get_business(1)["a2p_status"] == "approved")
check("the a2p step is now done", connections.step_state(db.get_business(1))[2]["done"] is True)
config.OPERATOR_EMAILS = _orig_ops

# The cron seam syncs every tenant: flip back to pending, run the tick, it re-approves.
db.set_a2p_status(1, "pending")
r = client.post("/tasks/run-due", headers={"X-Tasks-Secret": "tasks_secret_test"})
check("/tasks/run-due reports an A2P status change", r.get_json().get("a2p_synced", 0) >= 1)
check("/tasks/run-due re-approved the campaign", db.get_business(1)["a2p_status"] == "approved")
r = client.post("/tasks/run-due", headers={"X-Tasks-Secret": "wrong"})
check("/tasks/run-due rejects a bad secret", r.status_code == 403)
r = client.post("/tasks/run-due")  # no X-Tasks-Secret header at all
check("/tasks/run-due rejects a missing secret header (403)", r.status_code == 403)
messaging.fetch_a2p_campaign_status = _orig_fetch


# ====================== Forwarding step + go-live (Phase 4) ======================
# With profile+number+a2p done, forwarding is the last open step.
db.set_forwarding_confirmed(1, False)
check("forwarding is the current step before it's confirmed",
      connections.current_step(db.get_business(1)) == "forwarding")
check("not live until forwarding is set", connections.is_live(db.get_business(1), True) is False)

# The wizard shows the carrier's exact star code (Verizon here).
r = client.get("/setup?edit=forwarding&carrier=verizon")
check("forwarding step shows the carrier star code", b"*71+12677562454" in r.data)
check("forwarding step offers a tap-to-dial link", b"tel:*71+12677562454" in r.data)

# SF-7: stub send_sentinel_call so no real outbound call fires here. The full
# sentinel + inbound-confirm path is covered in test_sf7_sentinel.py; this test
# pins the route's HONESTY contract: a placed sentinel must NOT self-confirm.
_orig_send_sentinel = connections.send_sentinel_call
connections.send_sentinel_call = lambda biz_id, to_number=None: {"status": "placed", "sid": "CAtest"}

# Catcher model: forwarding is verified by a sentinel to the owner's cell (forward_to
# stays blank), NOT self-attested. confirmed stays 0 until the sentinel rings back.
r = client.post("/setup/forwarding", data={"mode": "catcher"})
check("forwarding confirm redirects", r.status_code in (301, 302))
check("catcher fires a sentinel (verifying), not a self-confirm",
      "verifying=1" in r.headers.get("Location", ""))
saved = db.get_business(1)
check("catcher does NOT self-attest forwarding_confirmed", saved["forwarding_confirmed"] == 0)
check("catcher confirm keeps forward_to blank", not saved["forward_to"])
check("not live until the sentinel confirms forwarding", connections.is_live(saved, True) is False)
# Simulate the sentinel ringing back -> the inbound webhook is what confirms (see
# test_sf7_sentinel.py). Now the business is live.
db.set_forwarding_confirmed(1, True)
saved = db.get_business(1)
check("business is LIVE once forwarding is confirmed", connections.is_live(saved, True) is True)
connections.send_sentinel_call = _orig_send_sentinel
r = client.get("/setup")
# Honest banner (DESIGN_AGENT_GOLIVE_BRIEF §G): blockers are clear but no test call has
# been texted back yet, so it shows "setup complete / make a test call" — NOT "You're live".
check("the wizard shows setup-complete, not yet 'live' (gated on live_verified)",
      b"Setup complete" in r.data and b"You're live" not in r.data)

# ---- webhooks_wired gate: a provisioned number with no webhooks isn't live ----
# Everything else (profile, a2p approved, forwarding) is satisfied here, so this
# isolates the webhooks-wired blocker. Rebind with webhooks_wired False, then True.
db.set_business_twilio(1, "+12677562454", "PNheritage", webhooks_wired=False)
unwired = db.get_business(1)
check("a numbered-but-unwired business is flagged 'not wired'",
      any("isn't wired to receive calls and texts" in b
          for b in connections.blockers(unwired, True)))
check("an unwired number is NOT live", connections.is_live(unwired, True) is False)
db.set_business_twilio(1, "+12677562454", "PNheritage", webhooks_wired=True)
wired = db.get_business(1)
check("wiring the webhooks clears the 'not wired' blocker",
      not any("isn't wired to receive calls and texts" in b
              for b in connections.blockers(wired, True)))
check("a fully wired + approved + forwarded business is LIVE",
      connections.is_live(wired, True) is True)

# Dial-through (advanced): rings a cell first. SF-7 honesty: a placed sentinel
# leaves the wizard "verifying" and never self-confirms; the inbound webhook is the
# only thing that confirms (covered in test_sf7_sentinel.py).
_orig_send_sentinel = connections.send_sentinel_call
connections.send_sentinel_call = lambda biz_id, to_number=None: {"status": "placed", "sid": "CAtest"}
db.set_forwarding_confirmed(1, False)
r = client.post("/setup/forwarding", data={"mode": "dial", "forward_to": "+15551234567"})
check("dial mode stores the cell to ring", db.get_business(1)["forward_to"] == "+15551234567")
check("dial mode fires a sentinel (verifying) and does NOT self-confirm",
      "verifying=1" in r.headers.get("Location", "")
      and db.get_business(1)["forwarding_confirmed"] == 0)
# Configured-but-unplaceable sentinel (e.g. FIRSTBACK_PUBLIC_URL unset on a real
# deploy) must NOT silently self-attest forwarding -- that's the lie SF-7 removed.
connections.send_sentinel_call = lambda biz_id, to_number=None: {"status": "simulated"}
db.set_forwarding_confirmed(1, False)
r = client.post("/setup/forwarding", data={"mode": "dial", "forward_to": "+15551234567"})
check("dial mode does NOT self-confirm when the sentinel can't be placed",
      "unverified=1" in r.headers.get("Location", "")
      and db.get_business(1)["forwarding_confirmed"] == 0)
r = client.post("/setup/forwarding", data={"mode": "dial", "forward_to": "123"})
check("a too-short ring-first number is refused", "err=forward" in r.headers.get("Location", ""))
connections.send_sentinel_call = _orig_send_sentinel
# restore catcher model for a clean live state
db.update_phone_voice(1, forward_to="")
db.set_forwarding_confirmed(1, True)


# ====================== Go-live verify + assistant route (Phase 5) ======================
import assistant
# The assistant routes setup/connect/go-live asks to the wizard (capability honesty).
for ask in ("how do I go live", "I need to connect my number", "it's not texting customers"):
    topic = assistant._route_topic(ask)
    check(f"assistant routes {ask!r} to /setup",
          bool(topic) and any(c.get("href") == "/setup" for c in topic.get("cards", [])))

# A real inbound test call surfaces on the (live) Go-Live page as verification.
r = client.get("/setup")
check("setup-complete page invites a test call when none seen yet", b"make a test call" in r.data)
db.log_call(1, "CAverify", from_number="+12157914043", to_number="+12677562454",
            missed=1, engaged=1)
check("last_inbound_call returns the test call", db.last_inbound_call(1)["from_number"] == "+12157914043")
r = client.get("/setup")
check("the live page confirms the test call was texted back",
      b"Last test call from +12157914043" in r.data and b"texted them back" in r.data)

# ====================== Hardening (H3/H4/M2/M4 + null-guard) ======================

# ---- H3: /setup/a2p mode=record is operator-gated ----
# A logged-in NON-operator may not record campaign SIDs (would forge approval).
_orig_ops = config.OPERATOR_EMAILS
config.OPERATOR_EMAILS = frozenset()                       # nobody is an operator
db.set_a2p_registration(1, status="pending", brand_sid="", campaign_sid="",
                        messaging_service_sid="")
before = db.get_business(1)
r = client.post("/setup/a2p", data={"mode": "record", "brand_sid": "BNhack",
                                    "messaging_service_sid": "MGhack", "campaign_sid": "CMhack"})
check("H3: non-operator record is forbidden (403)", r.status_code == 403)
after = db.get_business(1)
check("H3: non-operator record wrote nothing (status unchanged)",
      after["a2p_status"] == before["a2p_status"] and not after["a2p_brand_sid"])

# An operator (login email in OPERATOR_EMAILS) CAN record the SIDs.
config.OPERATOR_EMAILS = frozenset({config.SEED_OWNER_EMAIL.strip().lower()})
messaging.fetch_a2p_campaign_status = lambda svc, camp: "VERIFIED"
r = client.post("/setup/a2p", data={"mode": "record", "brand_sid": "BNok",
                                    "messaging_service_sid": "MGok", "campaign_sid": "CMok"})
check("H3: operator record redirects (saved)", r.status_code in (301, 302))
check("H3: operator record persisted the campaign SID",
      db.get_business(1)["a2p_campaign_sid"] == "CMok")
messaging.fetch_a2p_campaign_status = _orig_fetch

# The contractor submit path stays OPEN regardless of operator status.
config.OPERATOR_EMAILS = frozenset()
db.set_a2p_registration(1, status="unregistered")
r = client.post("/setup/a2p", data={"mode": "submit"})
check("H3: contractor mode=submit still works for a non-operator",
      r.status_code in (301, 302) and db.get_business(1)["a2p_status"] == "pending")
config.OPERATOR_EMAILS = _orig_ops

# ---- H4: attach hardened (canonicalize, confirm ownership, wire) ----
conn = db.get_conn(); conn.execute("UPDATE businesses SET twilio_number='' WHERE id=1"); conn.commit(); conn.close()
_orig_owns = messaging.account_owns_number
_orig_attach = messaging.attach_owned_number
# (a) junk input -> err=number, no ownership check, no write
_owns_calls = []
messaging.account_owns_number = lambda e164: (_owns_calls.append(e164), True)[1]
r = client.post("/setup/number", data={"mode": "attach", "number": "123"})
check("H4: un-canonicalizable attach -> err=number", "err=number" in r.headers.get("Location", ""))
check("H4: junk attach never checks ownership", _owns_calls == [])
check("H4: junk attach leaves the number unset", not db.get_business(1)["twilio_number"])
# (b) valid number not owned by the account -> err=not_owned, no write
messaging.account_owns_number = lambda e164: False
messaging.attach_owned_number = lambda e164, business_id, base_url=None: (_ for _ in ()).throw(
    AssertionError("attach must not run when ownership fails"))
r = client.post("/setup/number", data={"mode": "attach", "number": "(267) 756-2454"})
check("H4: not-owned attach -> err=not_owned", "err=not_owned" in r.headers.get("Location", ""))
check("H4: not-owned attach leaves the number unset", not db.get_business(1)["twilio_number"])
# (c) owned + attach succeeds -> saved=number, attach called with the CANONICAL e164
_attach_args = {}
messaging.account_owns_number = lambda e164: True
def _spy_attach(e164, business_id, base_url=None):
    _attach_args["e164"] = e164
    db.set_business_twilio(business_id, e164, "PNspy", webhooks_wired=True)
    return True
messaging.attach_owned_number = _spy_attach
r = client.post("/setup/number", data={"mode": "attach", "number": "(267) 756-2454"})
check("H4: owned attach -> saved=number", "saved=number" in r.headers.get("Location", ""))
check("H4: attach received the canonical +1 e164, not the raw input",
      _attach_args.get("e164") == "+12677562454")
check("H4: owned attach persisted the number", db.get_business(1)["twilio_number"] == "+12677562454")
messaging.account_owns_number = _orig_owns
messaging.attach_owned_number = _orig_attach
db.set_business_twilio(1, "+12677562454", "PNheritage", webhooks_wired=True)

# ---- M4: forwarding dial number is canonicalized (or refused) ----
# SF-7: stub send_sentinel_call; real path in test_sf7_sentinel.py.
connections.send_sentinel_call = lambda biz_id, to_number=None: {"status": "simulated"}
db.set_forwarding_confirmed(1, False)
db.update_phone_voice(1, forward_to="KEEP")
r = client.post("/setup/forwarding", data={"mode": "dial", "forward_to": "call me 555 123"})
check("M4: an un-parseable dial number -> err=forward", "err=forward" in r.headers.get("Location", ""))
check("M4: a refused dial number leaves forward_to unchanged",
      db.get_business(1)["forward_to"] == "KEEP")
r = client.post("/setup/forwarding", data={"mode": "dial", "forward_to": "(555) 123-4567"})
check("M4: a messy dial number is stored as canonical e164",
      db.get_business(1)["forward_to"] == "+15551234567")
# Honesty: an unplaceable sentinel (simulated) must NOT self-confirm forwarding.
check("M4: dial confirm does NOT self-attest when the sentinel can't be placed",
      db.get_business(1)["forwarding_confirmed"] == 0)
connections.send_sentinel_call = _orig_send_sentinel
# restore catcher model for a clean live state
db.update_phone_voice(1, forward_to="")
db.set_forwarding_confirmed(1, True)

# ---- M2: live_verified requires a real, engaged inbound call ----
# Business is fully live here; with no engaged inbound call, is_live is True but
# live_verified must be False. (Mirrors the boolean computed in the /setup GET.)
# Restore the approved A2P state the earlier H3 block left as pending.
db.set_a2p_status(1, "approved")
conn = db.get_conn(); conn.execute("DELETE FROM calls WHERE business_id=1"); conn.commit(); conn.close()
live_biz = db.get_business(1)
check("M2: fully-configured business is live", connections.is_live(live_biz, True) is True)
# sms_configured=False (no server Twilio creds) keeps an otherwise-ready business NOT
# live, and the ONLY blocker is the server-credentials one (everything tenant-side done).
check("not live when the server has no Twilio creds (sms_configured=False)",
      connections.is_live(live_biz, sms_configured=False) is False)
_no_cfg_blockers = connections.blockers(live_biz, False)
check("the only blocker with sms_configured=False is the server-credentials one",
      _no_cfg_blockers == ["Twilio credentials are not set on the server."])
no_call = db.last_inbound_call(1)
live_verified_before = bool(connections.is_live(live_biz, True) and no_call and no_call.get("engaged"))
check("M2: live but NOT verified before any engaged call", live_verified_before is False)
db.log_call(1, "CAm2", from_number="+12157914043", to_number="+12677562454",
            missed=1, engaged=1)
engaged = db.last_inbound_call(1)
check("M2: last_inbound_call reports the engaged test call", bool(engaged and engaged.get("engaged")))
live_verified_after = bool(connections.is_live(db.get_business(1), True) and engaged and engaged.get("engaged"))
check("M2: live AND verified once an engaged call is logged", live_verified_after is True)

# ---- null-business guard: a logged-in user with no business is redirected, not 500'd ----
_orig_cb = app.current_business
app.current_business = lambda: None
r = client.get("/setup")
check("null-guard: /setup with no business 302s to /dashboard",
      r.status_code in (301, 302) and "/dashboard" in r.headers.get("Location", ""))
app.current_business = _orig_cb

# ---- golive_summary + command-center golive card (backend contract for the design-dev agent) ----
# Business 1 is fully live with an engaged test call at this point (see the M2 block above).
g_live = connections.golive_summary(db.get_business(1), sms_configured=True)
check("golive: a fully verified business reports status 'live'", g_live["status"] == "live")
check("golive: live summary is_live + live_verified true",
      g_live["is_live"] and g_live["live_verified"])
check("golive: live summary has no blocker", g_live["blocker"] is None)
check("golive: summary exposes 4 steps with key/title/state",
      len(g_live["steps"]) == 4 and all({"key", "title", "state"} <= set(s) for s in g_live["steps"]))
check("golive: done == total when live", g_live["done"] == g_live["total"] == 4)

# the assistant returns a golive CARD (not a bare link) when it has the tenant
gc = assistant._route_topic("how do I go live", db.get_business(1))
check("golive card: assistant returns a card of type 'golive'", gc["cards"][0]["type"] == "golive")
check("golive card: deep-links to /setup", gc["cards"][0]["href"] == "/setup")
check("golive card: carries the live status", gc["cards"][0]["status"] == "live")
# a no-tenant call still falls back to a plain link card (keeps prior behavior / unit callers)
check("golive card: falls back to a link card without a tenant",
      assistant._route_topic("how do I go live")["cards"][0]["type"] == "link")

# setup_complete: blockers clear but the most recent call wasn't engaged (forwarding unconfirmed)
db.log_call(1, "CAsc", from_number="+12157914043", to_number="+12677562454", missed=1, engaged=0)
g_sc = connections.golive_summary(db.get_business(1), sms_configured=True)
check("golive: is_live but last call not engaged -> 'setup_complete'",
      g_sc["status"] == "setup_complete")
check("golive: setup_complete is live but not verified",
      g_sc["is_live"] and not g_sc["live_verified"])

# not_live: A2P pending re-blocks go-live
db.set_a2p_status(1, "pending")
g_nl = connections.golive_summary(db.get_business(1), sms_configured=True)
check("golive: a2p pending -> status 'not_live'", g_nl["status"] == "not_live")
check("golive: not_live exposes a plain-English blocker",
      isinstance(g_nl["blocker"], str) and bool(g_nl["blocker"]))
check("golive: not_live done < total", g_nl["done"] < g_nl["total"])

# the command center route renders with the golive context present
check("dashboard renders with golive context", client.get("/dashboard").status_code == 200)


# ====================== Fully-set-up tier (recommended connections) ======================
# Pure status aggregation with dependency-injected signals; must never touch the live tier.
rec_empty = connections.recommended_setup({}, ai_default="DEFAULT")
check("recommended: exposes 9 items", len(rec_empty["items"]) == 9)
check("recommended: total counts all items", rec_empty["total"] == 9)
check("recommended: nothing configured -> done 0", rec_empty["done"] == 0)
check("recommended: items carry key/title/href/cta/done/optional",
      all(set(it) >= {"key", "title", "href", "cta", "done", "optional"} for it in rec_empty["items"]))
check("recommended: every row deep-links somewhere", all(it["href"] for it in rec_empty["items"]))

biz_cfg = {"ai_instructions": "Talk like a pro", "alert_sms": "+12150000000",
           "screen_mode": "enforce", "reminders_enabled": 1, "voice_callback_enabled": 1,
           "estimate_times": "9:00 AM"}
rec_cfg = connections.recommended_setup(biz_cfg, calendar_connected=True,
                                        contacts_connected=True, password_changed=True,
                                        ai_default="DEFAULT")
done_keys = {it["key"] for it in rec_cfg["items"] if it["done"]}
check("recommended: all nine detect done when configured", rec_cfg["done"] == 9)
check("recommended: calendar/contacts/password done from injected signals",
      {"calendar", "contacts", "password"} <= done_keys)

# Honest: the untouched default AI instructions do NOT count as "taught your AI".
rec_default_ai = connections.recommended_setup({"ai_instructions": "DEFAULT"}, ai_default="DEFAULT")
check("recommended: default AI instructions are NOT 'done'",
      not any(it["key"] == "ai" and it["done"] for it in rec_default_ai["items"]))

# ISOLATION: a fully-recommended business with no bound number is still NOT live.
not_live_biz = {"name": "X", "ein": "12-3456789", "business_address": "123 St"}
fully_rec = connections.recommended_setup(not_live_biz, calendar_connected=True,
                                          contacts_connected=True, password_changed=True)
check("recommended: never flips a unbound business to live",
      connections.is_live(not_live_biz, True) is False and fully_rec["done"] >= 3)

# The /setup route renders the recommended section + meter alongside the live stepper.
db.set_a2p_status(1, "approved")   # avoid the on-view Twilio a2p_sync (network) in this render check
r_setup = client.get("/setup")
check("setup page renders the 'fully set up' section", b"Get the most out of" in r_setup.data)
check("setup page shows the recommended meter", b"fs-meter" in r_setup.data and b"set up" in r_setup.data)


# ====================== Go-Live nav placement + first-login landing ======================
# Force the non-A2P prerequisites so toggling A2P alone flips is_live on/off deterministically.
db.set_business_twilio(1, "+12677562454", "PNheritage", webhooks_wired=True)
db.set_forwarding_confirmed(1, True)

# Not live -> Go Live is pinned to the top (accent-emphasized), no completion check, and login lands there.
db.set_a2p_status(1, "pending")
nav_nl = client.get("/dashboard").data
check("nav: incomplete -> Go Live pinned to top (emphasized)", b"nav-item-golive" in nav_nl)
check("nav: incomplete -> no completion check shown", b"nav-check" not in nav_nl)
check("landing: incomplete -> login lands on the Go Live wizard",
      login().headers.get("Location", "").endswith("/setup"))

# Live -> Go Live retires to the bottom with a check, and login lands on the command center.
db.set_a2p_status(1, "approved")
nav_live = client.get("/dashboard").data
check("nav: complete -> completion check shown", b"nav-check" in nav_live)
check("nav: complete -> no longer top-emphasized", b"nav-item-golive" not in nav_live)
check("landing: complete -> login lands on the command center",
      login().headers.get("Location", "").endswith("/dashboard"))


# ============ Durable local-disk backup/restore (the network-FS boot-hang fix) ============
import tempfile as _tf, sqlite3 as _sq
def _seed_biz(path, names):
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    cc = _sq.connect(path)
    cc.execute("CREATE TABLE IF NOT EXISTS businesses(id INTEGER PRIMARY KEY, name TEXT)")
    for n in names:
        cc.execute("INSERT INTO businesses(name) VALUES(?)", (n,))
    cc.commit(); cc.close()

_bd = _tf.mkdtemp()
_live, _bak = os.path.join(_bd, "live.db"), os.path.join(_bd, "var", "backup.db")
_seed_biz(_live, ["Heritage"])
db.backup_to_durable(live=_live, backup=_bak)
check("backup: durable snapshot is written with the data", db._business_count(_bak) == 1)
os.remove(_live)
db.restore_from_backup_if_needed(live=_live, backup=_bak)
check("restore: seeds the live DB from backup when the local file is missing", db._business_count(_live) == 1)
_seed_biz(_live, ["Newer"])   # live now fresher (2 rows) than backup (1)
db.restore_from_backup_if_needed(live=_live, backup=_bak)
check("restore: never clobbers an existing (fresher) live DB", db._business_count(_live) == 2)
db.backup_to_durable(live=_live, backup=_bak)   # backup now has 2
_empty = os.path.join(_bd, "empty.db"); _seed_biz(_empty, [])
db.backup_to_durable(live=_empty, backup=_bak)
check("backup: anti-clobber refuses to overwrite a populated backup with an empty live DB",
      db._business_count(_bak) == 2)


print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
import sys
sys.exit(1 if _fail else 0)
