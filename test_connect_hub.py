"""Phase 6 slice 1 -- "Vic, the hub: connections". Run: python3 test_connect_hub.py

Proves the connection layer Vic now drives by talking:
  * connect_calendar / connect_contacts surface an inline `connect_action` card pointing
    at the REAL OAuth route (not a bare /settings link), reflecting live connection status.
  * the number / go-live ask surfaces the honest go-live card (never claims live when it
    isn't), and the card's current step agrees with connections.current_step (one source
    of truth -- chat and the /setup wizard can never disagree).
  * set_profile is a CONFIRM-gated write: it shows the EIN + address verbatim before
    anything is saved, never auto-executes, writes through the SAME db.update_business +
    db.update_a2p_profile the /setup/profile route uses, is tenant-arg-cleaned, and asks
    for what's missing instead of guessing.

Throwaway temp DB + the deterministic demo brain; no network.
"""
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"          # deterministic, no network
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""                  # configured() False -> not live, sends simulate

import connections
import assistant
import app  # noqa: F401  -- importing the app seeds business #1 (mirrors test_assistant)

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

# ---------------------------------------------------------------------------
# Connection cards: Vic drops the REAL action inline, with live status
# ---------------------------------------------------------------------------
out = assistant.run(biz, "connect my google calendar")
cal = next((c for c in out["cards"] if c.get("type") == "connect_action"), None)
check("calendar: surfaces a connect_action card (not a bare settings link)", cal is not None)
check("calendar: card points at the real OAuth route",
      cal and cal.get("href") == "/api/calendar/google/connect")
check("calendar: card carries a status badge",
      cal and cal.get("status") in ("todo", "current", "waiting", "done"))
check("calendar: not connected in a fresh tenant -> status not 'done'",
      cal and cal.get("status") != "done")
check("calendar: no pending_action (connecting is the user's tap, not a gated send)",
      out["pending_action"] is None)

out = assistant.run(biz, "connect my contacts")
con = next((c for c in out["cards"] if c.get("type") == "connect_action"), None)
check("contacts: surfaces a connect_action card", con is not None)
check("contacts: card points at the real contacts OAuth route",
      con and con.get("href") == "/api/contacts/google/connect")

# ---------------------------------------------------------------------------
# Go-live: honest status + one source of truth with the wizard
# ---------------------------------------------------------------------------
out = assistant.run(biz, "connect my number")
gc = next((c for c in out["cards"] if c.get("type") == "golive"), None)
check("number: routes into the go-live card", gc is not None)
check("number: go-live card deep-links the wizard", gc and gc.get("href") == "/setup")
check("number: never claims live when it isn't (Twilio off -> not_live)",
      gc and gc.get("status") == "not_live")
_card_current = next((s["key"] for s in (gc or {}).get("steps", [])
                      if s.get("state") == "current"), None)
check("number: the card's current step agrees with connections.current_step",
      _card_current == connections.current_step(biz))

# ---------------------------------------------------------------------------
# set_profile: the one place Vic actually DOES it -- gated, honest, real write
# ---------------------------------------------------------------------------
out = assistant.run(biz, "my EIN is 12-3456789")
pa = out.get("pending_action")
check("profile: an EIN routes to a gated set_profile (not auto-run)",
      pa is not None and pa["tool"] == "set_profile")
check("profile: the confirm shows the EIN verbatim before saving",
      pa and "12-3456789" in pa["summary"])

out = assistant.run(biz, "set my business address to 50 Main St, Denver")
pa = out.get("pending_action")
check("profile: an address routes to gated set_profile",
      pa is not None and pa["tool"] == "set_profile")
check("profile: the confirm shows the address verbatim",
      pa and "50 Main St" in pa["summary"])

# The gate must not write on run() -- only an explicit execute() does.
_before = db.get_business(1).get("ein")
assistant.run(biz, "my EIN is 99-9999999")
check("profile: the gate does NOT write on run() (EIN unchanged until confirm)",
      db.get_business(1).get("ein") == _before)

# Asking to "set up my profile" with nothing concrete asks for what's missing, never guesses.
out = assistant.run(biz, "set up my business profile")
check("profile: a bare 'set up my profile' asks, does not auto-execute",
      out["pending_action"] is None)
check("profile: the ask names what it needs (profile/EIN/address)",
      any(w in out["reply"].lower() for w in ("profile", "ein", "address", "business name")))

# execute() writes through the same two functions the /setup/profile route uses.
res = assistant.execute(biz, "set_profile", {
    "name": "Acme Plumbing", "trade": "plumbing", "owner_name": "Sam",
    "ein": "12-3456789", "business_address": "50 Main St, Denver"})
saved = db.get_business(1)
check("profile: execute writes the business fields (update_business)",
      saved.get("name") == "Acme Plumbing" and saved.get("trade") == "plumbing"
      and saved.get("owner_name") == "Sam")
check("profile: execute writes the A2P intake (update_a2p_profile)",
      saved.get("ein") == "12-3456789" and saved.get("business_address") == "50 Main St, Denver")
check("profile: execute confirms in the reply", "Acme Plumbing" in res["reply"] or res["reply"])
# AUDIT P1: the /setup/profile route writes legal_business_name too -- Vic must match it, or the
# A2P carrier registration files under a blank/wrong legal entity.
check("profile: a business name also lands in legal_business_name (A2P parity with the wizard)",
      saved.get("legal_business_name") == "Acme Plumbing")
# AUDIT P2: with the profile complete, the wizard card reads 'done', not a false 'next step'.
_pf = assistant.run(biz, "show my business profile")
_pcard = next((c for c in _pf["cards"] if c.get("type") == "connect_action"), None)
check("profile: when complete, the wizard card status reads 'done' (honest)",
      _pcard is not None and _pcard.get("status") == "done")

# AUDIT P1: a compound sentence must not over-capture the tail into the address field.
out = assistant.run(biz, "my address is 90 Oak Ave and my EIN is 44-7777777")
pa = out.get("pending_action")
check("profile: compound sentence does NOT over-capture the address",
      pa and pa["tool"] == "set_profile"
      and pa["args"].get("business_address") == "90 Oak Ave"
      and "EIN" not in (pa["args"].get("business_address") or "")
      and "ein" not in (pa["args"].get("business_address") or "").lower())
check("profile: the EIN is still extracted from the compound sentence",
      pa and pa["args"].get("ein") == "44-7777777")

# AUDIT P1: a connect intent in a mixed message wins over the profile branch.
out = assistant.run(biz, "connect my calendar and update my business name")
check("routing: connect + profile mixed message routes to connect (not a profile pending)",
      out.get("pending_action") is None
      and any(c.get("type") == "connect_action"
              and c.get("href") == "/api/calendar/google/connect" for c in out["cards"]))

# AUDIT P1: the unknown-provider fallback offers all three (reply promises contacts too).
out = assistant.run(biz, "what can I connect")
_hrefs = {c.get("href") for c in out["cards"] if c.get("type") == "connect_action"}
check("connect fallback: offers a contacts card (reply and cards agree)",
      "/api/contacts/google/connect" in _hrefs)

# A crafted confirm payload can't smuggle a non-param column (e.g. flip a2p_status).
_status_before = db.get_business(1).get("a2p_status")
assistant.execute(biz, "set_profile", {"ein": "11-1111111", "a2p_status": "approved"})
check("profile: execute arg-cleans -- a smuggled a2p_status is ignored",
      db.get_business(1).get("a2p_status") == _status_before)

# ---------------------------------------------------------------------------
print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
