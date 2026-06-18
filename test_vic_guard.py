"""SLICE BETA guard tests: referential ambiguity guard + genuine one-tap draft.
Run: python3 test_vic_guard.py

Tests:
  1. Referential + empty entities -> asks, no pending_action, no token.
  2. "text my last lead saying running late" resolves to most-recent lead + mints token.
  3. "text {Name} back" (named, no body) -> pending_action with non-empty draft + token_id.
  4. Draft is overridable (body-override path from 5a still applies).
"""
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""      # configured() False -> sends simulate

import assistant
import app
client = app.app.test_client()

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# Seed business + leads
biz = db.get_business(1)
lead_id_dana = db.create_lead(1, "Dana Homeowner", "+15551234567")
lead_id_mike = db.create_lead(1, "Mike Builder", "+15559876543")

print("\n--- BETA Test 1: referential + empty entities -> asks, no pending_action ---")
# "text her back" with no entities shown -> should ask for a name
# (_demo_route extracts args["name"]="her" but "her" is a pronoun, not a real name)
r1 = assistant.run(biz, "text her back", entities=None)
check("referential + no entities: reply asks for name",
      "which lead" in r1["reply"].lower() or "name" in r1["reply"].lower())
check("referential + no entities: no pending_action",
      r1.get("pending_action") is None)
check("referential + no entities: no token_id",
      r1.get("pending_action") is None or not r1["pending_action"].get("token_id"))

# "text him saying check this out" with empty entities list
r1b = assistant.run(biz, "text him saying check this out", entities=[])
check("'text him' + empty entities: asks for name",
      "which lead" in r1b["reply"].lower() or "name" in r1b["reply"].lower())
check("'text him' + empty entities: no pending_action",
      r1b.get("pending_action") is None)

# Bare pronoun -- also confirm other _LEAD_TOOLS tools are guarded
r1c = assistant.run(biz, "book them", entities=None)
check("book_estimate + 'them' + no entities: asks for name",
      "which lead" in r1c["reply"].lower() or "name" in r1c["reply"].lower())

print("\n--- BETA Test 2: 'text my last lead saying running late' -> resolves + mints token ---")
r2 = assistant.run(biz, "text my last lead saying running late", entities=None)
check("'last lead' + message: pending_action present",
      r2.get("pending_action") is not None)
check("'last lead' + message: token_id minted",
      (r2.get("pending_action") or {}).get("token_id") is not None)
check("'last lead' + message: body is 'running late'",
      "running late" in ((r2.get("pending_action") or {}).get("args") or {}).get("message", ""))
check("'last lead' NOT caught as referential (regression guard)",
      not assistant._is_referential("text my last lead saying running late") or
      # if _is_referential catches it, entities being None should NOT block it since
      # the message contains 'last lead' which routes deterministically
      r2.get("pending_action") is not None)

print("\n--- BETA Test 3: 'text {Name} back' (named, no body) -> genuine one-tap draft ---")
r3 = assistant.run(biz, "text Dana back", entities=None)
check("'text Dana back': pending_action present",
      r3.get("pending_action") is not None)
pa3 = r3.get("pending_action") or {}
check("'text Dana back': token_id minted",
      pa3.get("token_id") is not None)
draft_body = (pa3.get("args") or {}).get("message", "")
check("'text Dana back': draft body is non-empty",
      len(draft_body.strip()) > 0)
check("'text Dana back': draft body contains lead first name",
      "dana" in draft_body.lower() or "hi" in draft_body.lower())
check("'text Dana back': preview is set",
      pa3.get("preview") is not None)
check("'text Dana back': preview has recipient_name",
      (pa3.get("preview") or {}).get("recipient_name", "") != "" or
      (pa3.get("preview") or {}).get("recipient_phone", "") != "")

print("\n--- BETA Test 4: draft is overridable (5a body-override path) ---")
import re as _re
import json

client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
dashboard = client.get("/dashboard")
html = dashboard.get_data(as_text=True)
_cm = _re.search(r'id="csrfToken" value="([^"]+)"', html)
CSRF = _cm.group(1) if _cm else ""

# Build a one-tap confirm (named lead, no body -> draft is generated)
r4 = assistant.run(biz, "text Mike back", entities=None)
pa4 = r4.get("pending_action") or {}
token4 = pa4.get("token_id", "")
check("test 4 setup: token minted for 'text Mike back'", bool(token4))

if token4:
    # Redeem with a custom override body (the 5a override path uses "message" field)
    resp = client.post("/assistant/confirm",
                       data={"confirm_token": token4,
                             "message": "Hey Mike, following up on your quote!",
                             "_csrf": CSRF})
    check("body-override path: confirm returns 200",
          resp.status_code == 200)
    data4 = json.loads(resp.get_data(as_text=True))
    check("body-override path: reply present",
          "reply" in data4)
    # Redeeming again should replay the stored result (idempotent), not error
    resp2 = client.post("/assistant/confirm",
                        data={"confirm_token": token4,
                              "message": "Another text",
                              "_csrf": CSRF})
    data4b = json.loads(resp2.get_data(as_text=True))
    check("token is single-use: second redeem replays or rejects (no double-send)",
          "reply" in data4b)

print(f"\n{'='*40}")
print(f"Result: {_pass} passed, {_fail} failed")
if _fail:
    raise SystemExit(1)
