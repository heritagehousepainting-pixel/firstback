"""Phase 5a — SF-6 server-bound confirm token. Run: python3 test_confirm_token.py

Proves "you approve exactly what you saw" is enforced at the SERVER, not just the UI:
a gated action is stored under an opaque token; /assistant/confirm redeems by token only,
re-runs the STORED tool+args, is idempotent (one redeem, no double-send), expires, and is
scoped per-tenant. The editable body (text_lead) is the one client-overridable field; the
recipient + action stay server-bound. Throwaway temp DB + deterministic demo brain; no network.
"""
import os
import re as _re
import time
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"          # deterministic, no network
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""                  # configured() False -> sends simulate

import assistant
import app
from werkzeug.security import generate_password_hash

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


# --- count real send attempts so a double-send is observable ---
_sends = []
_orig_send = messaging.send_sms


def _counting_send(business, to, body, **kw):
    _sends.append({"to": to, "body": body})
    return _orig_send(business, to, body, **kw)


messaging.send_sms = _counting_send

# Seed a lead for biz 1 and log in as the seed owner.
lead_id = db.create_lead(1, "Dana Homeowner", "+15551234567")
biz = db.get_business(1)
client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
html = client.get("/dashboard").get_data(as_text=True)
_cm = _re.search(r'id="csrfToken" value="([^"]+)"', html)
CSRF = _cm.group(1) if _cm else ""


def propose_text(message):
    """Run a turn that proposes a gated text_lead; return its pending_action."""
    out = assistant.run(biz, message)
    return out.get("pending_action")


def redeem(token, **extra):
    data = {"_csrf": CSRF, "confirm_token": token or ""}
    data.update(extra)
    return client.post("/assistant/confirm", data=data)


# 1) A gated proposal now carries a server token (not just tool+args).
pa = propose_text("text my last lead saying running ten minutes late")
check("gated proposal carries a server-issued token_id",
      bool(pa) and pa.get("tool") == "text_lead" and isinstance(pa.get("token_id"), str)
      and len(pa["token_id"]) >= 16)
token = pa["token_id"]

# 2) Redeeming by token executes the STORED action (right recipient, real send).
_sends.clear()
r = redeem(token)
res = r.get_json()
check("redeem runs the stored action through the gated send",
      r.status_code == 200 and any(c.get("type") == "note" for c in (res.get("cards") or [])))
check("redeem sent to the STORED recipient exactly once",
      len(_sends) == 1 and _sends[0]["to"] == "+15551234567")

# 3) Idempotent replay: a second tap on the same token does NOT re-send.
_sends.clear()
r2 = redeem(token)
check("replaying the same token does not re-execute (no double-send)", len(_sends) == 0)
check("replay returns the stored result, not an error",
      r2.status_code == 200 and (r2.get_json() or {}).get("reply"))

# 4) Forged client tool/args are ignored — only the token decides what runs.
pa = propose_text("text my last lead saying second message")
_sends.clear()
r = client.post("/assistant/confirm", data={"_csrf": CSRF, "confirm_token": pa["token_id"],
                                            "tool": "text_lead",
                                            "args": '{"_lead_id": 999999, "message": "HACKED"}'})
check("client-supplied tool/args are ignored; stored recipient is used",
      len(_sends) == 1 and _sends[0]["to"] == "+15551234567"
      and _sends[0]["body"] != "HACKED")

# 5) Editable body override (text_lead only): edited body, STORED recipient.
pa = propose_text("text my last lead saying original body")
_sends.clear()
redeem(pa["token_id"], message="edited on the confirm card")
check("an owner edit to the body is honored, recipient stays server-bound",
      len(_sends) == 1 and _sends[0]["to"] == "+15551234567"
      and _sends[0]["body"] == "edited on the confirm card")

# 6) Expired token: no execution, honest reply.
pa = propose_text("text my last lead saying will expire")
row = db.get_confirm_token(1, pa["token_id"])
check("a fresh token is unconsumed with a future expiry",
      row and row["consumed"] == 0 and float(row["expires_at"]) > time.time())
_c = db.get_conn()
_c.execute("UPDATE pending_confirms SET expires_at=? WHERE token_id=?",
           (time.time() - 5, pa["token_id"]))
_c.commit(); _c.close()
_sends.clear()
r = redeem(pa["token_id"])
check("an expired token does not execute", len(_sends) == 0)
check("an expired token returns an honest reply (not a crash)",
      r.status_code == 200 and "expire" in ((r.get_json() or {}).get("reply", "").lower()))

# 7) Unknown / missing token: no execution.
_sends.clear()
r = redeem("deadbeefdeadbeefdeadbeefdeadbeef")
check("an unknown token does not execute", len(_sends) == 0 and r.status_code in (200, 400))
r = redeem("")
check("a missing token is rejected", r.status_code == 400 and len(_sends) == 0)

# 8) Cross-tenant: biz 2 cannot redeem biz 1's token.
bid2 = db.create_business({"name": "Second Tenant", "trade": "plumbing"})
db.create_user("tenant2@example.com", generate_password_hash("pw-two-12345"), bid2)
pa = propose_text("text my last lead saying tenant-scoped")  # issued for biz 1
token_b1 = pa["token_id"]
check("biz 2 lookup of biz 1's token returns nothing (scoped)",
      db.get_confirm_token(bid2, token_b1) is None)
client2 = app.app.test_client()
client2.post("/login", data={"email": "tenant2@example.com", "password": "pw-two-12345"})
html2 = client2.get("/dashboard").get_data(as_text=True)
csrf2 = (_re.search(r'id="csrfToken" value="([^"]+)"', html2) or [None, ""])[1]
_sends.clear()
r = client2.post("/assistant/confirm", data={"_csrf": csrf2, "confirm_token": token_b1})
check("biz 2 cannot execute biz 1's token", len(_sends) == 0)
# biz 1's token is still redeemable by biz 1 (cross-tenant attempt didn't consume it).
_sends.clear()
client.post("/assistant/confirm", data={"_csrf": CSRF, "confirm_token": token_b1})
check("biz 1's token survived the cross-tenant attempt and still runs once",
      len(_sends) == 1 and _sends[0]["to"] == "+15551234567")

# 9) Read/info tools issue NO token (nothing to approve).
out = assistant.run(biz, "how many leads came in this week?")
check("a read tool carries no pending action / token",
      out.get("pending_action") is None)

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
